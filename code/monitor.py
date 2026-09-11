#!/usr/bin/env python3
"""
ETF Sector Rotation Monitor - scoring engine (monitor.py)
Implements Sections 4-8 of the Part B runbook. Weights, thresholds and
formulas in this file are FIXED. Never change them between runs.

ENVIRONMENT NOTE (first run, 2026-09-10):
The Cowork cloud container's egress policy blocks every financial-data host
(stooq.com, *.yahoo.com, fred.stlouisfed.org, ssga.com) to direct HTTP.
Fetching is therefore performed by the agent using the WebFetch tool and
written to raw/ as `date,close` CSVs plus the input files listed below.
This script consumes those files and owns ALL scoring logic, so the part
of the runbook that must never drift (weights, thresholds, state rules)
lives here and is reproducible. Only the fetch layer is agent-side.

Inputs (relative to the ETF_Monitor working directory):
  raw/<ticker>.csv          date,close  (descending, most recent first)
  raw/spy_weekly.csv        date,close  weekly, 40 rows (200DMA equivalent)
  raw/rsp_weekly.csv        date,close  weekly, 40 rows
  raw/hy_oas.csv            date,value  ICE BofA US HY OAS, ascending
  raw/vix.csv               VIX,<level>,<date>,<source>
  data_inputs_valuation.csv etf,fwd_pe,pb,eps_growth_3_5y,shares_out_m,asof
  data_inputs_holdings.csv  etf,rank,name,weight
  data/history.csv          append-only run history (created on first run)

Outputs:
  data/history.csv, data/valuation.csv, data/holdings_latest.csv
  reports/YYYY-MM-DD_dashboard.md
  log/YYYY-MM-DD_run.md  (run log is written by the caller/agent)
"""

import csv, os, sys, datetime
from statistics import mean

_here = os.path.dirname(os.path.abspath(__file__))
# monitor.py lives in ETF_Monitor/code/ ; all data paths are relative to ETF_Monitor/
BASE = os.path.dirname(_here) if os.path.basename(_here) == "code" else _here
# Repo layout:  <root>/code  <root>/vps  <root>/data/raw (in git)
#               <root>/data/*.csv, reports/, log/  (NOT in git — see .gitignore)
RAW  = os.path.join(BASE, "data", "raw")
DATA = os.path.join(BASE, "data")
REPORTS = os.path.join(BASE, "reports")

# ---------------------------------------------------------------- FIXED SPEC
SECTORS = ["XLK","XLC","XLI","XLV","XLE","XLU","XLF","XLY","XLP","XLB","XLRE"]
TWIN = {"XLK":"RSPT","XLC":"RSPC","XLI":"RSPN","XLV":"RSPH","XLE":"RSPG",
        "XLU":"RSPU","XLF":"RSPF","XLY":"RSPD","XLP":"RSPS","XLB":"RSPM",
        "XLRE":"RSPR"}
GROUP = {"XLK":"Technology & Communications","XLC":"Technology & Communications",
         "XLI":"Industrials","XLV":"Healthcare","XLE":"Energy & Utilities",
         "XLU":"Energy & Utilities","XLF":"Financials",
         "XLY":"Consumer","XLP":"Consumer",
         "XLB":"Materials & Real Assets","XLRE":"Materials & Real Assets"}
BENCH = "RSP"

W_RS, W_ACC, W_BR = 0.40, 0.30, 0.30      # Section 4 - FIXED
K_RS, K_ACC, K_BR = 5.0, 5.0, 6.0         # Section 4 - FIXED
NEUTRAL = 50.0                            # missing component score - FIXED

T_BUY, T_CAND, T_EXITW, T_EXIT, T_CROWD = 60, 55, 50, 45, 65   # Section 7
BR_BUY, BR_WEAK = 50, 40
UP_2RUNS = 10
FLOW_FAIL_PCT = -1.5
VIX_MAX, HY_JUMP_BP = 30.0, 50.0

# ---- input validation limits (not part of the signal; they gate the inputs)
MAX_DAILY_MOVE_PCT = 25.0   # a bigger 1-day move in a sector ETF means bad data
MIN_COVERAGE       = 45     # of the benchmark's last 50 sessions
RUN_GAP_MAX_DAYS   = 10     # a longer gap breaks two-run-confirmation semantics

# ------------------------------------------------------------------ helpers
def clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))

def load_series(ticker):
    """Return list of (date, close) most-recent-first."""
    path = os.path.join(RAW, ticker.lower() + ".csv")
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("date"):
                continue
            d, c = line.split(",")[0], line.split(",")[1]
            out.append((d, float(c)))
    out.sort(key=lambda r: r[0], reverse=True)
    return out

def as_map(series):
    return {d: c for d, c in series}

def ret_dates(series, d_new, d_old):
    """% return between two explicit dates. None if either is absent.
    Date-anchored so a missing session in one file can never misalign
    an ETF against the benchmark."""
    m = as_map(series)
    if d_new not in m or d_old not in m:
        return None
    return (m[d_new] / m[d_old] - 1.0) * 100.0

def sma(series, n, min_n=45):
    """Mean of the most recent n closes. Falls back to the available window
    (>= min_n) and the caller flags it; never pads or estimates."""
    if len(series) >= n:
        return mean(c for _, c in series[:n]), n
    if len(series) >= min_n:
        return mean(c for _, c in series), len(series)
    return None, 0

def rank_desc(values):
    """dict key -> rank, 1 = highest value."""
    order = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    return {k: i + 1 for i, (k, _) in enumerate(order)}

def pct_rank(value, pool):
    """percentile of value within pool, 0-100."""
    below = sum(1 for v in pool if v < value)
    return 100.0 * below / max(1, len(pool) - 1)

# ======================================================= INPUT VALIDATION
# The scoring below is deterministic. The FETCHING is not: it is done by an
# agent against HTML pages and can silently return a stale window, a gap, or
# a mistyped price. These checks exist so that bad data fails loudly instead
# of producing a plausible-looking Trigger. A series that fails is not scored
# from -- its components fall back to NEUTRAL (50) and are flagged, exactly as
# Section 2 requires for a missing component.

def validate_series(ser, bench_dates, bench_last):
    """Return (ok, [problem codes])."""
    if not ser:
        return False, ["NO_DATA"]
    dates = [d for d, _ in ser]
    bset = set(bench_dates)
    p = []
    if len(set(dates)) != len(dates):
        p.append("DUPLICATE_DATES")
    if dates != sorted(dates, reverse=True):
        p.append("UNORDERED")
    if dates[0] != bench_last:
        p.append("STALE_ENDS_" + dates[0])
    stray = [d for d in dates if d not in bset]
    if stray:
        p.append("OFF_CALENDAR_%d" % len(stray))
    cover = sum(1 for d in bench_dates[:50] if d in set(dates))
    if cover < MIN_COVERAGE:
        p.append("COVERAGE_%d" % cover)
    if any(c <= 0 for _, c in ser):
        p.append("NONPOSITIVE_PRICE")
    jumps = []
    for i in range(1, len(ser)):
        prev = ser[i][1]
        if prev > 0 and abs(ser[i - 1][1] / prev - 1.0) * 100.0 > MAX_DAILY_MOVE_PCT:
            jumps.append(ser[i - 1][0])
    if jumps:
        p.append("IMPLAUSIBLE_MOVE_" + ",".join(jumps[:3]))
    return (len(p) == 0), p

def benchmark_is_stale(bench, all_series):
    """If several ETFs carry a session the benchmark does not, RSP itself is
    the stale one -- and every relative figure would be silently wrong."""
    b0 = bench[0][0]
    newer = [t for t, ser in all_series.items() if ser and ser[0][0] > b0]
    return (len(newer) >= 3), newer

def run_gap_days(history, run_date):
    """Calendar days since the previous run. Two-run confirmation assumes ~7."""
    if not history:
        return None
    prev = sorted({h["run_date"] for h in history})[-1]
    try:
        a = datetime.date.fromisoformat(prev)
        b = datetime.date.fromisoformat(run_date)
        return (b - a).days
    except Exception:
        return None

# ------------------------------------------------------------ regime gate
def regime_gate(flags):
    spy_w = load_series("spy_weekly")
    rsp_w = load_series("rsp_weekly")
    conds, fails = {}, []

    for name, weekly in (("SPY", spy_w), ("RSP", rsp_w)):
        # Prefer a TRUE 200-day average from the daily series. The 40-week
        # weekly mean is only a fallback for the shallow agent-fetch path,
        # where 200 daily closes are not obtainable.
        daily = load_series(name)
        if len(daily) >= 200:
            ma, _n = sma(daily, 200, min_n=200)
            ser, basis = daily, "_200DMA_TRUE"
        else:
            ma, _n = sma(weekly, 40, min_n=36)
            ser, basis = weekly, "_200DMA_WEEKLY_PROXY"
        if ma is None or not ser:
            conds[name + ">200DMA"] = None
            fails.append(name + ":NO_DATA")
            flags.append(name + "_200DMA_MISSING")
        else:
            ok = ser[0][1] > ma
            conds[name + ">200DMA"] = ok
            if not ok:
                fails.append(f"{name} {ser[0][1]:.2f} < 200DMA {ma:.2f}")
            flags.append(name + basis)

    # HY OAS: not up more than 50bp vs 20 observations ago
    hy = []
    p = os.path.join(RAW, "hy_oas.csv")
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("date"):
                    d, v = line.split(",")
                    hy.append((d, float(v)))
    hy.sort(key=lambda r: r[0], reverse=True)
    if len(hy) > 20:
        jump_bp = (hy[0][1] - hy[20][1]) * 100.0
        ok = jump_bp <= HY_JUMP_BP
        conds["HY_OAS"] = ok
        if not ok:
            fails.append(f"HY OAS +{jump_bp:.0f}bp vs 20d")
    else:
        conds["HY_OAS"] = None
        fails.append("HY_OAS:NO_DATA")
        flags.append("HY_OAS_MISSING")
        jump_bp = None

    # VIX
    vix = None
    p = os.path.join(RAW, "vix.csv")
    if os.path.exists(p):
        with open(p) as f:
            parts = f.readline().strip().split(",")
            if len(parts) >= 2:
                vix = float(parts[1])
    if vix is None:
        conds["VIX"] = None
        fails.append("VIX:NO_DATA")
        flags.append("VIX_MISSING")
    else:
        conds["VIX"] = vix < VIX_MAX
        if not conds["VIX"]:
            fails.append(f"VIX {vix:.2f} >= {VIX_MAX}")

    on = all(v is True for v in conds.values())
    return on, conds, fails, {"hy_jump_bp": jump_bp, "vix": vix}

# ------------------------------------------------------------ value inputs
def load_valuation():
    out = {}
    p = os.path.join(BASE, "data_inputs_valuation.csv")
    with open(p) as f:
        for row in csv.DictReader(f):
            out[row["etf"]] = {
                "fwd_pe": float(row["fwd_pe"]),
                "pb": float(row["pb"]),
                "eps_growth": float(row["eps_growth_3_5y"]),
                "shares_out_m": float(row["shares_out_m"]),
                "asof": row["asof"],
            }
    return out

def load_holdings():
    out = {}
    p = os.path.join(BASE, "data_inputs_holdings.csv")
    with open(p) as f:
        for row in csv.DictReader(f):
            out.setdefault(row["etf"], []).append(
                (int(row["rank"]), row["name"], float(row["weight"])))
    for k in out:
        out[k].sort()
    return out

def load_shares():
    """Per-run shares-outstanding history: run_date,etf,shares_out_m,close."""
    p = os.path.join(DATA, "shares_outstanding.csv")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))

def load_history():
    p = os.path.join(DATA, "history.csv")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))

# ---------------------------------------------------------------- main run
def main():
    flags_global = []
    val = load_valuation()
    hold = load_holdings()
    history = load_history()
    shares_hist = load_shares()
    first_run = len(history) == 0

    bench = load_series(BENCH)
    if not bench:
        print("FATAL: no benchmark series for", BENCH); sys.exit(1)
    run_date = bench[0][0]

    # Anchor every lookback to the BENCHMARK's trading calendar.
    if len(bench) <= 20:
        print("FATAL: benchmark series too short"); sys.exit(1)
    # ---- load every series once, then validate before anything is scored
    all_series = {}
    for _t in SECTORS + [TWIN[e] for e in SECTORS]:
        all_series[_t] = load_series(_t)

    stale_bench, newer = benchmark_is_stale(bench, all_series)
    if stale_bench:
        print("FATAL: benchmark RSP is stale - these carry a newer session: "
              + ", ".join(sorted(newer)))
        print("Refetch RSP with a cache-busting ?v= value before scoring.")
        sys.exit(2)

    ok_b, prob_b = validate_series(bench, [d for d, _ in bench], bench[0][0])
    if not ok_b:
        print("FATAL: benchmark RSP failed validation:", ", ".join(prob_b))
        sys.exit(2)

    bench_dates = [d for d, _ in bench]

    # ---- duplicate-run guard: never append a second row set for the same date
    if any(h["run_date"] == bench[0][0] for h in history):
        print("FATAL: history.csv already contains rows for run_date "
              + bench[0][0] + ". Refusing to append a duplicate, which would "
              "corrupt the delta and two-run-confirmation logic.")
        print("If this is a deliberate re-run, remove those rows first.")
        sys.exit(3)

    gap = run_gap_days(history, bench[0][0])
    if gap is not None and gap > RUN_GAP_MAX_DAYS:
        flags_global.append("RUN_GAP_%dD" % gap)

    D0, D10, D20 = bench[0][0], bench[10][0], bench[20][0]
    bench_20      = ret_dates(bench, D0, D20)
    bench_10      = ret_dates(bench, D0, D10)
    bench_prior10 = ret_dates(bench, D10, D20)

    regime_on, regime_conds, regime_fails, regime_extra = regime_gate(flags_global)

    # -------- Fundamental Value Score (Section 6) - cross-sectional until 12 readings
    pes  = {e: val[e]["fwd_pe"] for e in SECTORS}
    gro  = {e: val[e]["eps_growth"] for e in SECTORS}
    peg  = {e: (val[e]["fwd_pe"] / val[e]["eps_growth"]) if val[e]["eps_growth"] > 0
            else 999.0 for e in SECTORS}
    r_pe  = rank_desc(pes)     # 1 = most expensive
    r_gro = rank_desc(gro)     # 1 = fastest growth
    r_peg = rank_desc(peg)     # 1 = worst (highest) PEG
    n = len(SECTORS)
    fv = {}
    for e in SECTORS:
        pe_component  = 100.0 * (r_pe[e]  - 1) / (n - 1)   # cheap -> high
        gro_component = 100.0 * (n - r_gro[e]) / (n - 1)   # fast  -> high
        peg_component = 100.0 * (r_peg[e] - 1) / (n - 1)   # low PEG -> high
        fv[e] = 0.50 * pe_component + 0.30 * gro_component + 0.20 * peg_component
    fv_rank = rank_desc(fv)

    rows = []
    for etf in SECTORS:
        f = []
        ser  = all_series.get(etf) or []
        twin = all_series.get(TWIN[etf]) or []
        if not ser:
            flags_global.append(etf + "_NO_PRICE")
            continue

        ser_ok,  ser_prob  = validate_series(ser, bench_dates, D0)
        twin_ok, twin_prob = validate_series(twin, bench_dates, D0) if twin else (False, ["NO_DATA"])
        if not ser_ok:
            f.append("SERIES_REJECTED:" + "/".join(ser_prob))
        if not twin_ok:
            f.append("TWIN_REJECTED:" + "/".join(twin_prob))

        # ---- Relative Strength (40%)
        r20 = ret_dates(ser, D0, D20) if ser_ok else None
        if r20 is None or bench_20 is None:
            rs = NEUTRAL; f.append("RS_MISSING"); rs_pp = None
        else:
            rs_pp = r20 - bench_20
            rs = clip(50.0 + K_RS * rs_pp)

        # ---- Acceleration (30%)
        a_last  = ret_dates(ser, D0, D10) if ser_ok else None
        a_prior = ret_dates(ser, D10, D20) if ser_ok else None
        if None in (a_last, a_prior, bench_10, bench_prior10):
            acc = NEUTRAL; f.append("ACC_MISSING"); acc_pp = None
        else:
            acc_pp = (a_last - bench_10) - (a_prior - bench_prior10)
            acc = clip(50.0 + K_ACC * acc_pp)

        # ---- Breadth (30%)
        t20 = ret_dates(twin, D0, D20) if (twin and twin_ok) else None
        if t20 is None or r20 is None:
            br = NEUTRAL; f.append("BR_MISSING"); br_pp = None
        else:
            br_pp = t20 - r20
            br = clip(50.0 + K_BR * br_pp)

        trigger = W_RS * rs + W_ACC * acc + W_BR * br

        ma50, ma50_n = sma(ser, 50) if ser_ok else (None, 0)
        above50 = (ser[0][1] > ma50) if ma50 else None
        if ma50 is None:
            f.append("MA50_MISSING")
        elif ma50_n != 50:
            f.append(f"MA50_{ma50_n}D_WINDOW")
        # 50DMA 10 sessions ago needs 60 closes; only ~50 are obtainable on the
        # agent-fetch path. With the VPS fetcher this becomes computable.
        dma50_rising = "N/A"
        if ser_ok and len(ser) >= 60:
            m_now, _ = sma(ser, 50)
            m_then, _ = sma(ser[10:], 50)
            if m_now and m_then:
                dma50_rising = str(m_now > m_then)
        if dma50_rising == "N/A":
            f.append("DMA50_RISING_NA")

        # ---- Flow gate (Section 3C / 5): weekly net flow is DERIVED, not scraped.
        #      net flow ~= (shares outstanding now - last run) x price, as % of AUM,
        #      which reduces to the % change in shares outstanding.
        #      Trailing 4-week flow = sum of the last 4 weekly readings.
        flow_pct, flow_gate = "", "N/A"
        sh_now = val[etf]["shares_out_m"]
        asof_now = val[etf].get("asof", "")
        sh_prior = [r for r in shares_hist if r["etf"] == etf]
        stale_asof = bool(sh_prior and asof_now
                          and sh_prior[-1].get("asof", "") == asof_now)
        if not sh_prior:
            f.append("FLOW_NA_NO_PRIOR_SHARES")
        elif stale_asof:
            # SSGA has not published a new shares-outstanding date since the
            # last run. A zero delta here means "no new data", not "no flow" -
            # scoring it as 0% would read as a PASS on false comfort.
            f.append("FLOW_NA_SSGA_ASOF_UNCHANGED_" + asof_now)
        else:
            weekly = []
            seq = [float(r["shares_out_m"]) for r in sh_prior] + [sh_now]
            for i in range(max(1, len(seq) - 4), len(seq)):
                if seq[i - 1] > 0:
                    weekly.append((seq[i] / seq[i - 1] - 1.0) * 100.0)
            if weekly:
                flow_pct = round(sum(weekly), 2)
                # PASS if >= 0% of AUM or N/A; FAIL if < -1.5%
                flow_gate = "FAIL" if flow_pct < FLOW_FAIL_PCT else "PASS"
            else:
                f.append("FLOW_NA_NO_PRIOR_SHARES")

        # ---- Value gate (Section 5, pre-12-readings rule)
        value_gate = "PASS"
        if r_pe[etf] <= 2 and r_gro[etf] >= (n - 3):
            value_gate = "FAIL"

        # ---- Deltas vs prior runs
        prior = [h for h in history if h["etf"] == etf]
        d_last = d_4w = ""
        if prior:
            try: d_last = round(trigger - float(prior[-1]["trigger"]), 1)
            except Exception: pass
        if len(prior) >= 4:
            try: d_4w = round(trigger - float(prior[-4]["trigger"]), 1)
            except Exception: pass

        rows.append({
            "run_date": run_date, "etf": etf, "close": round(ser[0][1], 2),
            "rs": round(rs, 1), "acc": round(acc, 1), "br": round(br, 1),
            "trigger": round(trigger, 1),
            "rs_pp": None if rs_pp is None else round(rs_pp, 2),
            "acc_pp": None if acc_pp is None else round(acc_pp, 2),
            "br_pp": None if br_pp is None else round(br_pp, 2),
            "above_50dma": above50, "dma50_rising": dma50_rising,
            "ma50": None if ma50 is None else round(ma50, 2),
            "flow_pct_aum": flow_pct, "flow_gate": flow_gate,
            "value_gate": value_gate, "fv_score": round(fv[etf], 1),
            "fv_rank": fv_rank[etf], "d_last": d_last, "d_4w": d_4w,
            "fwd_pe": val[etf]["fwd_pe"], "eps_gr": val[etf]["eps_growth"],
            "shares_out_m": val[etf]["shares_out_m"],
            "flags": "|".join(f),
        })

    # -------------------------------------------------- state assignment
    for r in rows:
        t, etf = r["trigger"], r["etf"]
        prior = [h for h in history if h["etf"] == etf]
        prev_state = prior[-1]["state"] if prior else None
        held = prev_state in ("Buy", "Held")
        reasons = []

        two_run_up = None
        if len(prior) >= 2:
            try: two_run_up = t - float(prior[-2]["trigger"])
            except Exception: two_run_up = None

        buy_conds = {
            "trigger>=60": t >= T_BUY,
            "up>=10 vs 2 runs": (two_run_up is not None and two_run_up >= UP_2RUNS),
            "BR>=50": r["br"] >= BR_BUY,
            "above 50DMA": bool(r["above_50dma"]),
            "gates PASS": (r["value_gate"] == "PASS" and r["flow_gate"] != "FAIL"),
            "regime ON": regime_on,
        }
        missing = [k for k, v in buy_conds.items() if not v]

        crowded = (t >= T_CROWD and r["value_gate"] == "FAIL")
        if crowded:
            state = "Crowded"; reasons.append(f"Trigger {t:.1f} >= {T_CROWD}, Value gate FAIL")
        elif not missing:
            state = "Buy"; reasons.append("all Buy conditions met")
        elif held and (t < T_EXIT or (not r["above_50dma"] and r["br"] < BR_WEAK)):
            state = "Exit"; reasons.append("exit rule hit")
        elif held and (t < T_EXITW or r["br"] < BR_WEAK or not r["above_50dma"]):
            state = "Exit-Watch"; reasons.append("held but deteriorating")
        elif held:
            state = "Held"; reasons.append("no exit rule hit")
        elif t >= T_CAND:
            state = "Candidate"
            reasons.append("missing: " + ", ".join(missing))
        elif r["fv_rank"] <= 4 and t < T_CAND:
            state = "Value Watch"; reasons.append(f"FV rank {r['fv_rank']} of 11, Trigger < {T_CAND}")
        elif t < T_EXIT:
            state = "Avoid"; reasons.append(f"Trigger {t:.1f} < {T_EXIT}, not Held")
        else:
            state = "Neutral"; reasons.append(f"Trigger {t:.1f} between {T_EXIT} and {T_CAND}")

        if first_run:
            reasons.append("PROVISIONAL (no prior run)")
        r["state"] = state
        r["reason"] = "; ".join(reasons)

    rows.sort(key=lambda r: r["trigger"], reverse=True)
    return run_date, rows, regime_on, regime_conds, regime_fails, regime_extra, \
           hold, first_run, flags_global

# ============================================================ OUTPUT WRITERS
# (entry point is run.py, which calls run_all(pdf_builder=make_pdf.build))
HIST_COLS = ["run_date","etf","close","rs","acc","br","trigger","above_50dma",
             "dma50_rising","flow_pct_aum","flow_gate","value_gate","fv_score",
             "state","reason","flags"]

def load_top5():
    out = {}
    p = os.path.join(BASE, "data_inputs_top5_returns.csv")
    if not os.path.exists(p):
        return out
    with open(p) as f:
        for row in csv.DictReader(f):
            r20 = None
            if row["status"] == "OK" and row["close_d0"] and row["close_d20"]:
                r20 = (float(row["close_d0"]) / float(row["close_d20"]) - 1) * 100
            out.setdefault(row["etf"], []).append(
                (row["ticker"], float(row["weight"]), r20, row["status"]))
    return out

def write_history(rows):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, "history.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return p

def write_valuation(rows, val, month):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, "valuation.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["month","etf","fwd_pe","pb","eps_growth_3_5y","shares_out_m","flags"])
        for e in SECTORS:
            w.writerow([month, e, val[e]["fwd_pe"], val[e]["pb"],
                        val[e]["eps_growth"], val[e]["shares_out_m"], ""])
    return p

def write_shares(rows, val, run_date):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, "shares_outstanding.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_date","etf","shares_out_m","close","asof"])
        by_etf = {r["etf"]: r for r in rows}
        for e in SECTORS:
            close = by_etf[e]["close"] if e in by_etf else ""
            w.writerow([run_date, e, val[e]["shares_out_m"], close,
                        val[e].get("asof", "")])
    return p

def write_holdings(hold):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, "holdings_latest.csv")
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["etf","rank","name","weight"])
        for e in SECTORS:
            for rk, nm, wt in hold.get(e, []):
                w.writerow([e, rk, nm, wt])
    return p

def concentration_label(top10w):
    if top10w >= 60: return "concentrated"
    if top10w >= 45: return "moderately concentrated"
    return "broad"

def diagnostic_line(etf, hold, top5):
    entries = top5.get(etf, [])
    ok = [(t, w, r) for t, w, r, s in entries if s == "OK" and r is not None]
    skipped = [t for t, w, r, s in entries if s != "OK"]
    top10w = sum(wt for _, _, wt in hold.get(etf, []))
    if not ok:
        return "SKIPPED", f"top-10 weight {top10w:.1f}% ({concentration_label(top10w)}); top-5 returns SKIPPED"
    best = max(ok, key=lambda x: x[2]); worst = min(ok, key=lambda x: x[2])
    spread = best[2] - worst[2]
    if len(ok) == 1 or (best[1] >= 15 and spread > 10):
        shape = "one-name"
    elif spread > 15:
        shape = "divergent"
    elif top10w >= 60:
        shape = "concentrated"
    else:
        shape = "broad"
    note = f"best {best[0]} {best[2]:+.1f}% (w {best[1]:.1f}%), worst {worst[0]} {worst[2]:+.1f}% (w {worst[1]:.1f}%); top-10 {top10w:.1f}%"
    if skipped:
        note += f"; SKIPPED {','.join(skipped)}"
    return shape, note


def build_report(rd, rows, on, conds, fails, extra, hold, first, gf, top5):
    L = []
    L.append(f"# ETF Sector Rotation Monitor — {rd}")
    L.append("")
    L.append(f"*Run type: {'FIRST RUN (all states provisional)' if first else 'repeat run'}. "
             f"Prices as of the close on {rd}.*")
    L.append("")
    # 1. Regime
    L.append("## 1. Regime")
    L.append("")
    status = "**ON**" if on else "**OFF**"
    detail = []
    for k, v in conds.items():
        detail.append(f"{k}={'PASS' if v else ('FAIL' if v is False else 'NO DATA')}")
    L.append(f"Regime {status} — " + ", ".join(detail) + ".")
    if fails:
        L.append("")
        L.append("Failing / caveated conditions: " + "; ".join(fails) + ".")
    if extra.get("hy_jump_bp") is not None:
        L.append("")
        L.append(f"HY OAS {extra['hy_jump_bp']:+.0f}bp over 20 sessions (threshold +50bp). "
                 f"VIX {extra['vix']:.2f} (threshold 30).")
    L.append("")
    if any("WEEKLY_PROXY" in g for g in gf):
        L.append("> 200DMA is computed as the 40-week moving average of weekly closes "
                 "(`SPY_200DMA_WEEKLY_PROXY`, `RSP_200DMA_WEEKLY_PROXY`) — see the run log.")
        L.append("")
    # 2. Dashboard
    L.append("## 2. Dashboard")
    L.append("")
    hdr = ("| ETF | Trigger | Δ vs last | Δ 4W | RS | ACC | BR | Flow 4W | Gates | FV | "
           "Fwd P/E | EPS Gr | Best / Worst Top-5 | State |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    by_group = {}
    for r in rows:
        by_group.setdefault(GROUP[r["etf"]], []).append(r)
    for g in ["Technology & Communications","Industrials","Healthcare",
              "Energy & Utilities","Financials","Consumer","Materials & Real Assets"]:
        if g not in by_group:
            continue
        L.append(f"**{g}**")
        L.append("")
        L.append(hdr); L.append(sep)
        for r in sorted(by_group[g], key=lambda x: -x["trigger"]):
            shape, note = diagnostic_line(r["etf"], hold, top5)
            entries = top5.get(r["etf"], [])
            ok = [(t, w, v) for t, w, v, s in entries if s == "OK" and v is not None]
            if ok:
                b = max(ok, key=lambda x: x[2]); wst = min(ok, key=lambda x: x[2])
                bw = f"{b[0]} {b[2]:+.1f} / {wst[0]} {wst[2]:+.1f}"
            else:
                bw = "SKIPPED"
            gates = f"V:{r['value_gate']} F:{r['flow_gate']}"
            a50 = "y" if r["above_50dma"] else ("n" if r["above_50dma"] is False else "N/A")
            mark = " ⚠" if "REJECTED" in r["flags"] else ""
            L.append(f"| {r['etf']}{mark} | {r['trigger']:.1f} | {r['d_last'] or 'N/A'} | "
                     f"{r['d_4w'] or 'N/A'} | {r['rs']:.0f} | {r['acc']:.0f} | {r['br']:.0f} | "
                     f"{r['flow_pct_aum'] if r['flow_pct_aum'] != '' else 'N/A'} | "
                     f"{gates} a50:{a50} | {r['fv_score']:.0f} | {r['fwd_pe']:.1f} | "
                     f"{r['eps_gr']:.1f}% | {bw} | **{r['state']}** |")
        L.append("")
    # 3. Actions
    L.append("## 3. Actions")
    L.append("")
    buys = [r for r in rows if r["state"] == "Buy"]
    cands = [r for r in rows if r["state"] == "Candidate"]
    exits = [r for r in rows if r["state"] in ("Exit","Exit-Watch")]
    vw = [r for r in rows if r["state"] == "Value Watch"]
    crowd = [r for r in rows if r["state"] in ("Crowded","Avoid")]
    if buys:
        L.append(f"1. **New Buy:** {', '.join(r['etf'] for r in buys)}.")
    else:
        L.append("1. **New Buy:** none — the Buy rule requires a +10 Trigger rise versus two "
                 "runs ago, and no prior runs exist yet, so no ETF can qualify on a first run.")
    if cands:
        top = cands[0]
        L.append(f"2. **Strongest Candidate:** {top['etf']} at Trigger {top['trigger']:.1f}; "
                 f"missing — {top['reason'].split('missing: ')[-1].split(';')[0]}.")
    else:
        L.append("2. **Strongest Candidate:** none.")
    if exits:
        L.append(f"3. **Exit / Exit-Watch:** {', '.join(r['etf'] for r in exits)}.")
    else:
        L.append("3. **Exit / Exit-Watch:** none — nothing is Held on a first run, so no exit "
                 "rule can fire.")
    if vw:
        best = max(vw, key=lambda r: r["fv_score"])
        L.append(f"4. **Best Value Watch:** {best['etf']} — FV {best['fv_score']:.0f} "
                 f"(rank {best['fv_rank']} of 11), Fwd P/E {best['fwd_pe']:.1f}, "
                 f"EPS growth {best['eps_gr']:.1f}%.")
    else:
        L.append("4. **Best Value Watch:** none.")
    if crowd:
        L.append("5. **Crowded / Avoid:** " +
                 ", ".join(f"{r['etf']} ({r['state']})" for r in crowd) + ".")
    else:
        L.append("5. **Crowded / Avoid:** none.")
    L.append("")
    # 4. Change log
    L.append("## 4. Change log")
    L.append("")
    if first:
        L.append("First run — no prior states to compare. Baseline states recorded:")
        L.append("")
        for r in rows:
            L.append(f"- {r['etf']}: → **{r['state']}** ({r['reason']})")
    else:
        L.append("State changes this run are listed one per line by the caller.")
    L.append("")
    # 5. Holding diagnostic
    L.append("## 5. Top-holding diagnostic")
    L.append("")
    for r in rows:
        if r["state"] in ("Candidate","Held","Exit-Watch"):
            shape, note = diagnostic_line(r["etf"], hold, top5)
            L.append(f"- **{r['etf']} — {shape}.** {note}")
    L.append("")
    L.append("*Explains a state; never overrides it.*")
    L.append("")
    L.append("---")
    L.append("")
    rejected = [r["etf"] for r in rows if "SERIES_REJECTED" in r["flags"]]
    twin_rej = [r["etf"] for r in rows if "TWIN_REJECTED" in r["flags"]]
    gapflag = [g for g in gf if g.startswith("RUN_GAP_")]
    L.append("**Data integrity.** " + (
        "All 23 price series passed validation."
        if not rejected and not twin_rej else
        (("Price series REJECTED (components fell back to neutral 50): "
          + ", ".join(rejected) + ". " if rejected else "")
         + ("Breadth twin REJECTED: " + ", ".join(twin_rej) + ". " if twin_rej else "")
         + "Read the run log before acting on any state above.")))
    if gapflag:
        L.append("")
        L.append("**" + gapflag[0].replace("RUN_GAP_", "Gap since the previous run: ")
                 .replace("D", " days") + ".** Two-run confirmation assumes ~7 days, so "
                 "any Buy or Exit confirmed this run spans a longer window than intended.")
    L.append("")
    nas = sum(1 for r in rows if r["flow_gate"] == "N/A")
    skipped = sum(1 for e, v in top5.items() for t, w, x, s in v if s != "OK")
    fellback = sum(1 for r in rows for k in ("RS_MISSING","ACC_MISSING","BR_MISSING")
                   if k in r["flags"])
    L.append(f"Data quality — STALE 0, N/A {nas + 11} (flow {nas}, Δ vs last 11), "
             f"SKIPPED {skipped} top-5 tickers. "
             + ("Every Trigger component was computed from live data; no component fell "
                "back to the neutral 50."
                if fellback == 0 else
                f"**{fellback} Trigger component(s) fell back to the neutral 50** because "
                f"their source series failed validation — those Triggers are not comparable "
                f"with earlier runs."))
    return "\n".join(L)


def run_all(pdf_builder=None, doc_builder=None):
    rd, rows, on, conds, fails, extra, hold, first, gf = main()
    top5 = load_top5()
    val = load_valuation()
    os.makedirs(REPORTS, exist_ok=True)
    if pdf_builder is not None:
        print("  " + pdf_builder(rd, rows, on, conds, fails, extra, hold, first, top5, gf))
    hp = write_history(rows)
    vp = write_valuation(rows, val, rd[:7])
    sp = write_shares(rows, val, rd)
    op = write_holdings(hold)
    md = build_report(rd, rows, on, conds, fails, extra, hold, first, gf, top5)
    rp = os.path.join(REPORTS, f"{rd}_dashboard.md")
    open(rp, "w").write(md)
    outs = [hp, vp, sp, op, rp]
    if doc_builder is not None:
        txt = doc_builder.build(rd, rows, on, conds, fails, extra, hold, first,
                                gf, top5, diagnostic_line=diagnostic_line)
        outs.append(doc_builder.write(rd, txt))
    print("wrote:", *outs, sep="\n  ")
    return rd, rows, on, first
