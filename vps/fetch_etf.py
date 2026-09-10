#!/usr/bin/env python3
"""
ETF Monitor — data fetcher. Runs on the VPS, NOT in Cowork.

Why this exists: the Cowork cloud container's egress policy blocks every
financial-data host, so its only route to prices is an LLM reading HTML,
which caps history at ~50 rows and is not reproducible. The VPS has open
internet. It fetches real series, commits them to GitHub, and Cowork (which
CAN reach github.com) pulls them and scores deterministically.

SOURCES — both verified working from the VPS on 2026-09-10:
  primary   stockanalysis.com JSON API   5 years, field "a" = adjusted close
  fallback  Yahoo chart API v8           needs a browser User-Agent or the
                                         edge returns "Too Many Requests"

The runbook's original sources are both dead for automation:
  * stooq.com now serves a JavaScript proof-of-work challenge, not CSV
  * Yahoo's /v7/finance/download endpoint returns 401 (retired)

BASIS — adjusted closes, deliberately:
The sector SPDRs go ex-dividend quarterly (~3rd week of Mar/Jun/Sep/Dec) and
their yields differ sharply: XLU carries ~2.8%/yr of dividend, XLK ~0.5%. On
unadjusted closes, a 20-day window containing an ex-div date understates the
high-yield sector's return by the whole dividend — ~0.57pp between XLU and XLK
per quarter, which is ~2 Trigger points of one-directional error against the
defensive sectors, four times a year. Every series here is total-return
adjusted, and so is the benchmark, so the comparison is like for like.

Output: data/raw/<ticker>.csv  (date,adjusted_close ascending)
Then:   git commit + push. Cowork clones and runs code/run.py against it.

Cron on the VPS (Mondays 06:30 UTC, ahead of the Cowork task at 07:00):
    30 6 * * 1 cd /opt/etf_monitor && /usr/bin/python3 vps/fetch_etf.py >> /var/log/etf_fetch.log 2>&1
"""
import csv, io, json, os, subprocess, sys, time, urllib.request, urllib.error
from datetime import date, datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(BASE, "data", "raw")

SECTORS = ["XLK", "XLC", "XLI", "XLV", "XLE", "XLU", "XLF", "XLY", "XLP", "XLB", "XLRE"]
TWINS = ["RSPT", "RSPC", "RSPN", "RSPH", "RSPG", "RSPU", "RSPF", "RSPD", "RSPS", "RSPM", "RSPR"]
TICKERS = SECTORS + TWINS + ["RSP", "SPY"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}

MIN_ROWS = 300          # 260 needed for a true 200DMA, plus buffer
BACKTEST_ROWS = 1000    # below this, the 5-year backtest is not possible
CROSS_CHECK = ["RSP", "SPY", "XLK", "XLE", "XLU"]
CROSS_TOL_PCT = 0.5


def get(url, timeout=40):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------------------------------------- sources
def stockanalysis(ticker, kind="e"):
    """Primary. kind 'e' = etf, 's' = stock. Returns [(date, adj_close)]."""
    url = (f"https://stockanalysis.com/api/symbol/{kind}/{ticker.lower()}"
           f"/history?range=5Y&period=Daily")
    d = json.loads(get(url))
    if d.get("status") != 200:
        raise RuntimeError(f"status {d.get('status')}")
    out = []
    for r in d.get("data", []):
        px = r.get("a", r.get("c"))
        if r.get("t") and px not in (None, ""):
            out.append((r["t"], float(px)))
    return out


def yahoo(ticker, rng="5y"):
    """Named fallback and cross-check source. Uses adjclose, same basis as
    the primary. Needs the browser User-Agent set above."""
    sym = "%5EVIX" if ticker.upper() == "VIX" else ticker.upper()
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range={rng}&interval=1d")
    d = json.loads(get(url))
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    try:
        px = res["indicators"]["adjclose"][0]["adjclose"]
    except (KeyError, IndexError):
        px = res["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, px):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        out.append((day, float(c)))
    return out


def fred_hy():
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2")
    rows = []
    for r in csv.reader(io.StringIO(txt)):
        if len(r) >= 2 and r[0][:1].isdigit() and r[1] not in (".", ""):
            rows.append((r[0], float(r[1])))
    return rows


# ------------------------------------------------------------- helpers
def cross_check(ticker, primary):
    """Compare the last 5 closes against the other source. A disagreement
    means one of them is wrong; we keep the primary but say so loudly,
    because a silently wrong price becomes a silently wrong signal."""
    try:
        other = dict(yahoo(ticker, rng="1mo"))
    except Exception as e:
        return None, f"cross-check unavailable ({type(e).__name__})"
    p = dict(primary)
    diffs = [abs(c / other[d] - 1.0) * 100.0
             for d, c in sorted(p.items())[-5:]
             if d in other and other[d] > 0]
    if not diffs:
        return None, "cross-check: no overlapping dates"
    worst = max(diffs)
    if worst <= CROSS_TOL_PCT:
        return worst, "cross-check OK (max %.2f%%)" % worst
    return worst, "CROSS-CHECK DISAGREEMENT %.2f%% — verify before trusting" % worst


def write(name, rows):
    """Ascending, deduped. Never overwrite a good file with a short one."""
    rows = sorted({d: c for d, c in rows}.items())
    path = os.path.join(RAW, name + ".csv")
    if len(rows) < MIN_ROWS and os.path.exists(path):
        print(f"  {name}: only {len(rows)} rows — keeping existing file")
        return False
    os.makedirs(RAW, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        for d, c in rows:
            w.writerow([d, round(c, 6)])
    depth = "" if len(rows) >= BACKTEST_ROWS else "  (short for backtest)"
    print(f"  {name}: {len(rows)} rows, {rows[0][0]} to {rows[-1][0]}{depth}")
    return True


def weekly_from_daily(rows):
    """Last close of each ISO week, for the regime gate's weekly fallback."""
    by_week = {}
    for d, c in rows:
        y, w, _ = date.fromisoformat(d).isocalendar()
        by_week[(y, w)] = (d, c)
    return [v for _, v in sorted(by_week.items())]


# ------------------------------------------------------------------ main
def main():
    failures, latest, warnings, shallow = [], {}, [], []

    for t in TICKERS:
        rows, src = None, None
        try:
            rows = stockanalysis(t)
            src = "stockanalysis"
        except Exception as e:
            print(f"  {t}: primary failed ({type(e).__name__}: {e}); trying fallback")
        if not rows or len(rows) < MIN_ROWS:
            try:
                rows = yahoo(t)
                src = "yahoo"
            except Exception as e:
                print(f"  {t}: fallback failed ({type(e).__name__}: {e})")
        if not rows:
            failures.append(t)
            continue

        write(t.lower(), rows)
        latest[t] = rows[-1][0]
        if len(rows) < BACKTEST_ROWS:
            shallow.append(t)
        if t in ("SPY", "RSP"):
            write(t.lower() + "_weekly", weekly_from_daily(rows))
        if t in CROSS_CHECK and src == "stockanalysis":
            worst, msg = cross_check(t, rows)
            print(f"  {t}: {msg}")
            if worst is not None and worst > CROSS_TOL_PCT:
                warnings.append(f"{t} {msg}")
        time.sleep(0.5)

    # VIX — index, so Yahoo only. Written in the one-line form monitor.py reads.
    try:
        v = yahoo("VIX", rng="1mo")
        os.makedirs(RAW, exist_ok=True)
        with open(os.path.join(RAW, "vix.csv"), "w") as f:
            f.write(f"VIX,{v[-1][1]:.2f},{v[-1][0]},yahoo\n")
        print(f"  VIX: {v[-1][1]:.2f} on {v[-1][0]}")
    except Exception as e:
        print("  VIX failed:", e)
        failures.append("VIX")

    try:
        write("hy_oas", fred_hy())
    except Exception as e:
        print("  hy_oas failed:", e)
        failures.append("HY_OAS")

    lagging = {}
    if latest:
        newest = max(latest.values())
        lagging = {k: v for k, v in latest.items() if v != newest}
        if lagging:
            print("WARNING lagging series:", lagging)

    open(os.path.join(RAW, "_fetched_at.txt"), "w").write(
        f"{date.today().isoformat()}\n"
        f"basis: total-return adjusted closes\n"
        f"primary: stockanalysis.com 5Y | fallback: yahoo chart v8\n"
        f"failures: {failures}\n"
        f"lagging: {lagging}\n"
        f"shallow (<%d rows, backtest limited): {shallow}\n" % BACKTEST_ROWS
        + f"cross_check_warnings: {warnings}\n")

    for cmd in (["git", "add", "-A", "data/raw"],
                ["git", "commit", "-m", f"data: {date.today().isoformat()}"],
                ["git", "push"]):
        r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            print("git step failed:", " ".join(cmd), r.stderr.strip())
            sys.exit(1)

    print("done. failures:", failures or "none",
          "| lagging:", lagging or "none",
          "| cross-check warnings:", warnings or "none")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
