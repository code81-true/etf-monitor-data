#!/usr/bin/env python3
"""
Phone-shaped weekly report -> reports/<run_date>_report_doc.md

This file is uploaded to Drive WITH markdown->Google Doc conversion enabled,
so it becomes a native Google Doc: real headings, real tables, real bullets,
readable in the Drive app on a phone without downloading anything.

Design rules, because it is read on a ~50-character-wide screen:
  * the verdict is the first thing on the page, before any table
  * the headline table is 4 columns; anything wider wraps and becomes soup
  * the detail table is separate and further down, for whoever wants it
  * no horizontal rules, no nested bullets, no code fences
"""
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) \
    if os.path.basename(os.path.dirname(os.path.abspath(__file__))) == "code" \
    else os.path.dirname(os.path.abspath(__file__))

ARROW = {"Buy": "BUY", "Held": "HOLD", "Candidate": "WATCH",
         "Value Watch": "VALUE", "Exit": "EXIT", "Exit-Watch": "TRIM?",
         "Avoid": "AVOID", "Crowded": "CROWDED", "Neutral": "-"}


def _delta(v):
    if v == "" or v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:+.1f}"


def build(rd, rows, on, conds, fails, extra, hold, first, gf, top5,
          diagnostic_line=None):
    L = []
    L.append("# ETF Sector Rotation Monitor")
    L.append("")
    L.append(f"{rd} · Regime {'ON' if on else 'OFF'}"
             + (" · FIRST RUN, states provisional" if first else ""))
    L.append("")

    # ---------------------------------------------------------- the verdict
    buys = [r for r in rows if r["state"] == "Buy"]
    exits = [r for r in rows if r["state"] == "Exit"]
    exitw = [r for r in rows if r["state"] == "Exit-Watch"]
    cands = [r for r in rows if r["state"] == "Candidate"]
    vw = [r for r in rows if r["state"] == "Value Watch"]
    crowd = [r for r in rows if r["state"] in ("Crowded", "Avoid")]

    L.append("## This week")
    L.append("")
    if buys:
        L.append(f"**BUY: {', '.join(r['etf'] for r in buys)}**")
    elif first:
        L.append("**No new Buy.** A Buy needs the Trigger up 10+ versus two "
                 "runs ago, and there are no prior runs yet.")
    else:
        L.append("**No new Buy.**")
    L.append("")
    if exits:
        L.append(f"**EXIT: {', '.join(r['etf'] for r in exits)}** — acts "
                 "immediately, no confirmation needed.")
        L.append("")
    if exitw:
        L.append(f"**Watch for exit: {', '.join(r['etf'] for r in exitw)}.**")
        L.append("")
    if cands:
        t = cands[0]
        miss = t["reason"].split("missing: ")[-1].split(";")[0]
        L.append(f"Closest to a Buy is **{t['etf']}** at {t['trigger']:.1f}. "
                 f"Still missing: {miss}.")
        L.append("")
    if vw:
        b = max(vw, key=lambda r: r["fv_score"])
        L.append(f"Cheapest thing not yet moving: **{b['etf']}** "
                 f"(value score {b['fv_score']:.0f}, forward P/E "
                 f"{b['fwd_pe']:.1f}).")
        L.append("")
    if crowd:
        L.append("Stay away from: "
                 + ", ".join(f"{r['etf']} ({r['state'].lower()})" for r in crowd)
                 + ".")
        L.append("")

    # ------------------------------------------------- integrity, if it bites
    rejected = [r["etf"] for r in rows if "SERIES_REJECTED" in r["flags"]]
    twin_rej = [r["etf"] for r in rows if "TWIN_REJECTED" in r["flags"]]
    gapflag = [g for g in (gf or []) if g.startswith("RUN_GAP_")]
    if rejected or twin_rej or gapflag:
        L.append("## Read this before acting")
        L.append("")
        if rejected:
            L.append(f"Price data failed validation for: {', '.join(rejected)}. "
                     "Their scores are placeholders, not measurements.")
            L.append("")
        if twin_rej:
            L.append(f"Breadth data failed for: {', '.join(twin_rej)}. "
                     "Their breadth score is a placeholder.")
            L.append("")
        if gapflag:
            days = gapflag[0].replace("RUN_GAP_", "").replace("D", "")
            L.append(f"It has been {days} days since the last run, not the usual 7. "
                     "Anything confirmed this week spans a longer window than intended.")
            L.append("")

    # ------------------------------------------------------- headline table
    L.append("## Ranked")
    L.append("")
    L.append("| ETF | Trigger | Change | Call |")
    L.append("|---|---|---|---|")
    for r in rows:
        mark = " !" if "REJECTED" in r["flags"] else ""
        L.append(f"| {r['etf']}{mark} | {r['trigger']:.1f} | "
                 f"{_delta(r['d_last'])} | {ARROW.get(r['state'], r['state'])} |")
    L.append("")
    L.append("Trigger runs 0 to 100. Above 60 with confirmation is a Buy; "
             "below 45 is Avoid. Change is versus last week.")
    L.append("")

    # ---------------------------------------------------------- what moved
    if not first:
        movers = sorted([r for r in rows if r["d_last"] not in ("", None)],
                        key=lambda r: -abs(float(r["d_last"])))[:3]
        if movers:
            L.append("## Biggest moves")
            L.append("")
            for r in movers:
                d = float(r["d_last"])
                L.append(f"- **{r['etf']}** {d:+.1f} to {r['trigger']:.1f} "
                         f"({r['state']})")
            L.append("")

    # ------------------------------------------------------ under the bonnet
    L.append("## Detail")
    L.append("")
    L.append("| ETF | RS | Accel | Breadth | Value | P/E |")
    L.append("|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['etf']} | {r['rs']:.0f} | {r['acc']:.0f} | "
                 f"{r['br']:.0f} | {r['fv_score']:.0f} | {r['fwd_pe']:.1f} |")
    L.append("")
    L.append("RS is 20-day return versus the equal-weight benchmark. Accel is "
             "whether that is speeding up. Breadth is the equal-weight version "
             "of the sector against the cap-weight one — high means the move is "
             "broad, low means a few big names are carrying it.")
    L.append("")

    # ------------------------------------------------------------ holdings
    if diagnostic_line:
        diag = [r for r in rows
                if r["state"] in ("Candidate", "Held", "Exit-Watch")]
        if diag:
            L.append("## Inside the movers")
            L.append("")
            for r in diag:
                shape, note = diagnostic_line(r["etf"], hold, top5)
                L.append(f"- **{r['etf']} — {shape}.** {note}")
            L.append("")

    # -------------------------------------------------------------- regime
    L.append("## Market backdrop")
    L.append("")
    L.append(f"Regime is **{'ON' if on else 'OFF'}**"
             + (". New Buys are allowed." if on else
                ". No new Buys this week whatever the scores; exits still apply."))
    L.append("")
    if extra.get("hy_jump_bp") is not None:
        L.append(f"- Credit spreads {extra['hy_jump_bp']:+.0f}bp over 20 sessions "
                 "(alarm at +50)")
    if extra.get("vix") is not None:
        L.append(f"- VIX {extra['vix']:.1f} (alarm at 30)")
    for k, v in conds.items():
        if k.endswith("200DMA"):
            L.append(f"- {k.replace('>200DMA', '')} "
                     + ("above" if v else "below") + " its 200-day average")
    L.append("")
    L.append("This is decision support. It is not an automatic buy or sell "
             "system, and every state here is a rule applied to price data, "
             "not a forecast.")
    return "\n".join(L)


def write(rd, text):
    out = os.path.join(BASE, "reports", f"{rd}_report_doc.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write(text)
    return out
