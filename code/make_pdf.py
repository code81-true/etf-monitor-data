#!/usr/bin/env python3
"""One-page landscape dashboard PDF (Section 9)."""
import os, csv, datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import monitor as m

_here = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(_here) if os.path.basename(_here) == "code" else _here
INK, MUTED = "#1a1a1a", "#6b6b6b"
STATE_COLOR = {"Buy":"#1a7f5a","Candidate":"#2f6fa8","Held":"#1a7f5a",
               "Value Watch":"#8a6d1f","Neutral":"#8c8c8c","Avoid":"#a63a34",
               "Exit":"#a63a34","Exit-Watch":"#c07a1f","Crowded":"#7d3c98"}

def build(rd, rows, on, conds, fails, extra, hold, first, top5, gf=None):
    fig = plt.figure(figsize=(16.54, 11.69))   # A3 landscape, prints down to A4
    fig.patch.set_facecolor("white")
    gs = fig.add_gridspec(3, 2, height_ratios=[1.00, 1.05, 0.46],
                          hspace=0.30, wspace=0.16,
                          left=0.055, right=0.965, top=0.855, bottom=0.055)

    fig.text(0.055, 0.972, "ETF Sector Rotation Monitor", fontsize=25,
             fontweight="bold", color=INK, va="top")
    regime_txt = ("REGIME ON" if on else "REGIME OFF")
    fig.text(0.055, 0.938, f"As of close {rd}   ·   {regime_txt}   ·   "
             + ("FIRST RUN — all states provisional" if first else "repeat run"),
             fontsize=12.5, color=MUTED, va="top")
    cond_txt = "  ".join(f"{k}:{'PASS' if v else ('FAIL' if v is False else 'N/A')}"
                         for k, v in conds.items())
    fig.text(0.965, 0.972, cond_txt, fontsize=10, color=MUTED, va="top", ha="right")

    # ---------------- ranked Trigger bars with FV alongside
    ax = fig.add_subplot(gs[0, 0])
    srt = sorted(rows, key=lambda r: r["trigger"])
    y = range(len(srt))
    ax.barh([i + 0.19 for i in y], [r["trigger"] for r in srt], height=0.38,
            color=[STATE_COLOR.get(r["state"], "#8c8c8c") for r in srt], label="Trigger")
    ax.barh([i - 0.19 for i in y], [r["fv_score"] for r in srt], height=0.38,
            color="#c9c9c9", label="FV score")
    ax.set_yticks(list(y)); ax.set_yticklabels([r["etf"] for r in srt], fontsize=10.5)
    ax.set_xlim(0, 100); ax.set_xlabel("score (0–100)", fontsize=10, color=MUTED)
    for x, lbl in ((45, "45"), (55, "55"), (60, "60"), (65, "65")):
        ax.axvline(x, color="#d0d0d0", lw=0.8, ls="--", zorder=0)
        ax.text(x, len(srt) - 0.35, lbl, fontsize=8, color="#b0b0b0", ha="center")
    ax.set_title("Trigger score, ranked (bar colour = state); FV score in grey",
                 fontsize=12, color=INK, loc="left", pad=10)
    ax.legend(fontsize=9, loc="lower right", frameon=False)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9)

    # ---------------- quadrant FV (x) vs Trigger (y)
    ax2 = fig.add_subplot(gs[0, 1])
    ax2.add_patch(Rectangle((50, 55), 50, 45, facecolor="#e8f2ec",
                            edgecolor="none", zorder=0))
    ax2.axvline(50, color="#c8c8c8", lw=1); ax2.axhline(55, color="#c8c8c8", lw=1)
    for r in rows:
        ax2.scatter(r["fv_score"], r["trigger"], s=140,
                    color=STATE_COLOR.get(r["state"], "#8c8c8c"),
                    edgecolor="white", linewidth=1.4, zorder=3)
        ax2.annotate(r["etf"], (r["fv_score"], r["trigger"]),
                     textcoords="offset points", xytext=(0, 11),
                     ha="center", fontsize=9.5, color=INK)
    ax2.set_xlim(0, 100); ax2.set_ylim(30, 90)
    ax2.set_xlabel("Fundamental Value score →  cheaper / faster growth", fontsize=10, color=MUTED)
    ax2.set_ylabel("Trigger score →  stronger, accelerating", fontsize=10, color=MUTED)
    ax2.set_title("Cheap and accelerating (shaded) is where new positions come from",
                  fontsize=12, color=INK, loc="left", pad=10)
    for s in ("top", "right"): ax2.spines[s].set_visible(False)
    ax2.tick_params(colors=MUTED, labelsize=9)

    # ---------------- table
    ax3 = fig.add_subplot(gs[1, :]); ax3.axis("off")
    cols = ["ETF","Trigger","Δ last","Δ 4W","RS","ACC","BR","Flow 4W","Gates",
            "FV","Fwd P/E","EPS Gr","Best / Worst Top-5","State"]
    data = []
    for r in sorted(rows, key=lambda x: -x["trigger"]):
        e = top5.get(r["etf"], [])
        ok = [(t, w, v) for t, w, v, s in e if s == "OK" and v is not None]
        if ok:
            b = max(ok, key=lambda x: x[2]); wst = min(ok, key=lambda x: x[2])
            bw = f"{b[0]} {b[2]:+.1f}% / {wst[0]} {wst[2]:+.1f}%"
        else:
            bw = "SKIPPED"
        a50 = "y" if r["above_50dma"] else ("n" if r["above_50dma"] is False else "—")
        flag = " !" if "REJECTED" in r["flags"] else ""
        data.append([r["etf"] + flag, f"{r['trigger']:.1f}", r["d_last"] or "N/A",
                     r["d_4w"] or "N/A", f"{r['rs']:.0f}", f"{r['acc']:.0f}",
                     f"{r['br']:.0f}",
                     str(r["flow_pct_aum"]) if r["flow_pct_aum"] != "" else "N/A",
                     f"V:{r['value_gate']} >50DMA:{a50}", f"{r['fv_score']:.0f}",
                     f"{r['fwd_pe']:.1f}", f"{r['eps_gr']:.1f}%", bw, r["state"]])
    tbl = ax3.table(cellText=data, colLabels=cols, cellLoc="center", loc="upper center",
                    colWidths=[.045,.058,.05,.045,.04,.04,.04,.052,.125,.038,.055,.052,.185,.078])
    tbl.auto_set_font_size(False); tbl.set_fontsize(9.5); tbl.scale(1, 1.42)
    for (row, col), cell in tbl.get_celld().items():
        cell.set_edgecolor("#e6e6e6"); cell.set_linewidth(0.7)
        if row == 0:
            cell.set_facecolor("#f2f2f2"); cell.set_text_props(fontweight="bold", color=INK)
        else:
            if col == 13:
                st = data[row-1][13]
                cell.set_text_props(color=STATE_COLOR.get(st, INK), fontweight="bold")
            if col == 0 and data[row-1][0].endswith("!"):
                cell.set_text_props(color="#a63a34", fontweight="bold")
            if row % 2 == 0:
                cell.set_facecolor("#fafafa")
    ax3.set_title("Dashboard — one row per ETF, ranked by Trigger   ( ! = series failed validation )",
                  fontsize=12, color=INK, loc="left", pad=2)

    # ---------------- actions
    ax4 = fig.add_subplot(gs[2, :]); ax4.axis("off")
    buys = [r for r in rows if r["state"] == "Buy"]
    cands = [r for r in rows if r["state"] == "Candidate"]
    exits = [r for r in rows if r["state"] in ("Exit","Exit-Watch")]
    vw = [r for r in rows if r["state"] == "Value Watch"]
    crowd = [r for r in rows if r["state"] in ("Crowded","Avoid")]
    acts = []
    acts.append("1. New Buy: " + (", ".join(r["etf"] for r in buys) if buys else
        "none — the Buy rule needs a +10 Trigger rise vs two runs ago; no prior runs exist."))
    if cands:
        t = cands[0]
        miss = t["reason"].split("missing: ")[-1].split(";")[0]
        acts.append(f"2. Strongest Candidate: {t['etf']} at {t['trigger']:.1f} — missing {miss}.")
    else:
        acts.append("2. Strongest Candidate: none.")
    acts.append("3. Exit / Exit-Watch: " + (", ".join(r["etf"] for r in exits) if exits else
        "none — nothing is Held on a first run."))
    if vw:
        b = max(vw, key=lambda r: r["fv_score"])
        acts.append(f"4. Best Value Watch: {b['etf']} — FV {b['fv_score']:.0f}, "
                    f"Fwd P/E {b['fwd_pe']:.1f}, EPS growth {b['eps_gr']:.1f}%.")
    else:
        acts.append("4. Best Value Watch: none.")
    acts.append("5. Crowded / Avoid: " + (", ".join(f"{r['etf']} ({r['state']})" for r in crowd)
                                          if crowd else "none."))
    ax4.set_ylim(0, 1); ax4.set_xlim(0, 1)
    ax4.text(0, 1.14, "Actions", fontsize=12, color=INK, va="top", fontweight="bold")
    for i, a in enumerate(acts):
        ax4.text(0, 0.90 - i * 0.20, a, fontsize=10.5, color=INK, va="top")

    nas = 11 + 11
    skipped = sum(1 for e, v in top5.items() for t, w, x, s in v if s != "OK")
    rejected = [r["etf"] for r in rows if "REJECTED" in r["flags"]]
    gapflag = [g for g in (gf or []) if g.startswith("RUN_GAP_")]
    if rejected or gapflag:
        warn = "DEGRADED RUN — "
        if rejected:
            warn += "price series rejected by validation: " + ", ".join(sorted(set(rejected))) + ". "
        if gapflag:
            warn += gapflag[0].replace("RUN_GAP_", "gap since previous run: ").replace("D", " days") + ". "
        warn += "Read the run log before acting on any state below."
        fig.text(0.055, 0.905, warn, fontsize=11, color="#a63a34", va="top", fontweight="bold")
    foot = (f"Sources: stockanalysis.com (daily & weekly closes), ssga.com fund pages "
            f"(shares outstanding, holdings, valuation), FRED BAMLH0A0HYM2 (HY OAS), "
            f"cboe.com (VIX).   Data quality: STALE 0 · N/A {nas} · SKIPPED {skipped}.   "
            f"200DMA via 40-week weekly proxy.   Generated {datetime.date.today().isoformat()}. "
            f"Decision support, not an automatic buy/sell system.")
    fig.text(0.055, 0.012, foot, fontsize=8.5, color=MUTED, va="bottom", wrap=True)

    out = os.path.join(BASE, "reports", f"{rd}_dashboard.pdf")
    fig.savefig(out, format="pdf", facecolor="white")
    plt.close(fig)
    return out

if __name__ == "__main__":
    rd, rows, on, conds, fails, extra, hold, first, gf = m.main()
    top5 = m.load_top5()
    print(build(rd, rows, on, conds, fails, extra, hold, first, top5, gf))
