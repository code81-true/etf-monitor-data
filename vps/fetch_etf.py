#!/usr/bin/env python3
"""
ETF Monitor — data fetcher. Runs on the Hetzner VPS, NOT in Cowork.

Why this exists: the Cowork cloud container's egress policy blocks every
financial-data host, so its only route to prices is an LLM reading HTML,
which caps history at ~50 rows and is not reproducible. The VPS has
unrestricted internet. It fetches real CSVs, commits them to GitHub, and
Cowork (which CAN reach github.com) pulls them and scores deterministically.

Output: data/raw/<ticker>.csv  (date,adjusted_close ascending), 2y of history.

ACCURACY NOTE — why ADJUSTED closes:
The sector SPDRs go ex-dividend quarterly (~3rd week of Mar/Jun/Sep/Dec) and
their yields differ sharply: XLU carries ~2.8%/yr of dividend, XLK ~0.5%.
On an UNADJUSTED close series, a 20-day window containing an ex-div date
understates the high-yield sector's return by the full dividend. That is
~0.57pp of pure artefact between XLU and XLK per quarter, which is ~2 Trigger
points (RS and ACC both move), one-directional, four times a year, against
exactly the defensive sectors. So every series here is total-return adjusted,
and the benchmark is too, so the comparison is like for like.
Then: git commit + push. Cowork clones and runs code/run.py against it.

Cron on the VPS (Mondays 06:30 UTC, ahead of the Cowork task at 07:00):
    30 6 * * 1  cd /opt/etf_monitor && /usr/bin/python3 vps/fetch_etf.py >> /var/log/etf_fetch.log 2>&1
"""
import csv, io, os, subprocess, sys, time, urllib.request
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW  = os.path.join(BASE, "data", "raw")

SECTORS = ["XLK","XLC","XLI","XLV","XLE","XLU","XLF","XLY","XLP","XLB","XLRE"]
TWINS   = ["RSPT","RSPC","RSPN","RSPH","RSPG","RSPU","RSPF","RSPD","RSPS","RSPM","RSPR"]
TICKERS = SECTORS + TWINS + ["RSP", "SPY"]

UA = {"User-Agent": "Mozilla/5.0 (compatible; etf-monitor/1.0)"}
MIN_ROWS = 400          # ~2y; refuse to write a short file over a good one


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def stooq(ticker):
    """Primary source: Stooq daily CSV. Stooq US series are already adjusted
    for splits and dividends, so Close is the total-return basis."""
    sym = "^vix" if ticker.upper() == "VIX" else ticker.lower() + ".us"
    txt = get(f"https://stooq.com/q/d/l/?s={sym}&i=d")
    rows = []
    for r in csv.DictReader(io.StringIO(txt)):
        if r.get("Date") and r.get("Close") not in (None, "", "N/A"):
            rows.append((r["Date"], float(r["Close"])))
    return rows


def yahoo(ticker):
    """Named fallback, exactly one attempt, per the runbook.
    Uses Adj Close, not Close, so it is on the same basis as Stooq."""
    sym = "^VIX" if ticker.upper() == "VIX" else ticker.upper()
    end = int(time.time()); start = end - 3 * 365 * 24 * 3600
    txt = get("https://query1.finance.yahoo.com/v7/finance/download/"
              f"{sym}?period1={start}&period2={end}&interval=1d&events=history")
    rows = []
    for r in csv.DictReader(io.StringIO(txt)):
        px = r.get("Adj Close") or r.get("Close")
        if r.get("Date") and px not in (None, "", "null"):
            rows.append((r["Date"], float(px)))
    return rows


def fred_hy():
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2")
    rows = []
    for r in csv.reader(io.StringIO(txt)):
        if len(r) >= 2 and r[0][:1].isdigit() and r[1] not in (".", ""):
            rows.append((r[0], float(r[1])))
    return rows


def cross_check(ticker, primary):
    """Fetch the same series from the fallback and compare the last 5 closes.
    A disagreement means one source is wrong; we keep the primary but say so
    loudly, because a silently wrong price becomes a silently wrong signal."""
    try:
        other = dict(yahoo(ticker))
    except Exception as e:
        return None, f"cross-check unavailable ({e})"
    p = dict(primary)
    diffs = []
    for d, c in sorted(p.items())[-5:]:
        if d in other and other[d] > 0:
            diffs.append(abs(c / other[d] - 1.0) * 100.0)
    if not diffs:
        return None, "cross-check: no overlapping dates"
    worst = max(diffs)
    return worst, ("cross-check OK (max %.2f%%)" % worst if worst <= 0.5
                   else "CROSS-CHECK DISAGREEMENT %.2f%% — verify before trusting" % worst)


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
            w.writerow([d, c])
    print(f"  {name}: {len(rows)} rows, latest {rows[-1][0]}")
    return True


def main():
    failures, latest, warnings = [], {}, []
    for t in TICKERS + ["VIX"]:
        rows = None
        try:
            rows = stooq(t)
        except Exception as e:
            print(f"  {t}: stooq failed ({e}); trying fallback")
        if not rows or len(rows) < MIN_ROWS:
            try:
                rows = yahoo(t)                      # one fallback attempt only
            except Exception as e:
                print(f"  {t}: yahoo failed ({e})")
        if not rows:
            failures.append(t); continue
        write(t.lower(), rows)
        latest[t] = rows[-1][0]
        if t in ("RSP", "SPY", "XLK", "XLE", "XLU"):   # spot-check a sample
            worst, msg = cross_check(t, rows)
            print(f"  {t}: {msg}")
            if worst is not None and worst > 0.5:
                warnings.append(f"{t} {msg}")
        time.sleep(0.4)

    try:
        write("hy_oas", fred_hy())
    except Exception as e:
        print("  hy_oas failed:", e); failures.append("HY_OAS")

    # every price series must end on the same session
    lagging = {}
    if latest:
        newest = max(latest.values())
        lagging = {k: v for k, v in latest.items() if v != newest}
        if lagging:
            print("WARNING lagging series:", lagging)

    stamp = os.path.join(BASE, "data", "raw", "_fetched_at.txt")
    open(stamp, "w").write(
        f"{date.today().isoformat()}\n"
        f"basis: total-return adjusted closes\n"
        f"failures: {failures}\n"
        f"lagging: {lagging if latest else {}}\n"
        f"cross_check_warnings: {warnings}\n")

    for cmd in (["git", "add", "-A", "data/raw"],
                ["git", "commit", "-m", f"data: {date.today().isoformat()}"],
                ["git", "push"]):
        r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            print("git step failed:", cmd, r.stderr.strip()); sys.exit(1)

    print("done. failures:", failures or "none",
          "| cross-check warnings:", warnings or "none")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
