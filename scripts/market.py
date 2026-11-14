"""Market Signals & Hedging desk CLI (analysis for the deal team, not advice).

  python3 scripts/market.py assess --counterparty tricolor --as-of 2025-09-07
  python3 scripts/market.py study --counterparty tricolor --start 2025-08-01 --end 2025-09-30 \\
        --event 2025-09-10 --alert 2025-09-07
  python3 scripts/market.py fetch            # prefetch/cache prices for every proxy
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youfin import market as M  # noqa: E402
from youfin import regime as R  # noqa: E402
from youfin.agents import market as D  # noqa: E402
from youfin.config import DATA_DIR, PORTFOLIO_PATH  # noqa: E402
from youfin.db import connect  # noqa: E402

LABEL_CH = {"open": ".", "closing": "~", "closed": "X", "n/a": "?"}


def fmt(x, spec: str, na: str = "-") -> str:
    return na if x is None or (isinstance(x, float) and pd.isna(x)) else format(x, spec)


def print_assessment(a: dict) -> None:
    print(f"\n=== {a['counterparty']} as of {a['as_of']} (last close used: {a['bar_date']}) ===")
    if a.get("price_rule"):
        print(f"price rule: {a['price_rule']}")
    for w in a.get("data_warnings", []):
        print(f"WARNING: {w}")
    hdr = (f"{'ticker':<6} {'role':<14} {'close':>8} {'r1%':>6} {'r3%':>6} {'r5%':>6} {'ar_z':>6} "
           f"{'car5z':>6} {'shk_z':>6} {'vs_z':>6} {'vol_z':>6} {'rv5/60':>6} {'dd20%':>6} "
           f"{'ADV$M':>7} {'bcp_ar':>6} {'bcp_v':>6}  window")
    print(hdr)
    print("-" * len(hdr))
    for r in a["proxies"]:
        pct = lambda k: fmt(r.get(k) * 100 if r.get(k) is not None else None, ".1f")  # noqa: E731
        print(f"{r['ticker']:<6} {r['role']:<14} {fmt(r.get('close'), '.2f'):>8} {pct('r1'):>6} "
              f"{pct('r3'):>6} {pct('r5'):>6} {fmt(r.get('ar_z'), '.1f'):>6} "
              f"{fmt(r.get('car5_z'), '.1f'):>6} {fmt(r.get('shock_z'), '.1f'):>6} "
              f"{fmt(r.get('ar_vs_z'), '.1f'):>6} {fmt(r.get('vol_z'), '.1f'):>6} "
              f"{fmt(r.get('rv_ratio'), '.2f'):>6} {pct('dd20'):>6} "
              f"{fmt((r.get('adv60_usd') or 0) / 1e6, '.1f'):>7} {fmt(r.get('bocpd_ar'), '.2f'):>6} "
              f"{fmt(r.get('bocpd_vol'), '.2f'):>6}  {r['label']}")
        if r["label"] != "open":
            print(f"{'':<22}why: {'; '.join(r['drivers'])}")
    b = a["basket"]
    print(f"\nbasket (equal-weight abnormal returns of {', '.join(b['members'])}):")
    if b.get("ms_error"):
        print(f"  Markov model unavailable: {b['ms_error']}")
    print(f"  Markov P(stress)={fmt(b['p_stress'], '.2f')}  (regime sd calm {fmt(b.get('sd_calm_pct'), '.2f')}% "
          f"vs stress {fmt(b.get('sd_stress_pct'), '.2f')}%/day, {b.get('ms_bars')} bars since {R.MS_START})"
          f"  basket z={fmt(b['z'], '.1f')} car5_z={fmt(b['car5_z'], '.1f')} BOCPD={fmt(b['bocpd'], '.2f')}")
    print(f"\nACTION WINDOW: {a['window'].upper()}  <- {'; '.join(a['window_drivers'])}")
    print("\nhedge candidates (score = 0.45 relevance + 0.25 liquidity + 0.30 unspent move):")
    for i, h in enumerate(a["hedges"], 1):
        c = h["components"]
        print(f"  {i}. {h['ticker']:<5} {h['role']:<14} score {h['score']:.2f}  (rel {c['relevance']:.2f}, "
              f"liq {c['liquidity']:.2f}, unspent {c['unspent']:.2f}; ADV ${h['adv60_musd']:.1f}M; {h['label']})")
        print(f"     caveat: {h['caveat']}")
    print(f"\n{a['disclaimer']}")


def cmd_assess(args, conn) -> None:
    a = D.assess(conn, args.counterparty, args.as_of, after_close=args.after_close)
    print_assessment(a)
    if args.save:
        print(f"\nsaved market_assessments id={D.save(conn, a, 'cli')}")
    if args.json:
        print(json.dumps(a, indent=1, default=str))


def cmd_fetch(args, conn) -> None:
    pf = json.loads(PORTFOLIO_PATH.read_text())
    tickers = sorted({t for cp in pf["counterparties"] for t in M.all_tickers(cp.get("proxies", []))})
    M.ensure_prices(conn, tickers, args.end)
    for r in conn.execute("SELECT ticker, COUNT(*), MIN(date), MAX(date) FROM market_prices "
                          "GROUP BY ticker ORDER BY ticker"):
        print(f"  {r[0]:<5} {r[1]:>5} bars {r[2]}..{r[3]}")


def _d(s):
    return None if s is None else s.date().isoformat()


def cmd_study(args, conn) -> None:
    cp = args.counterparty
    alert = args.alert or _db_alert(conn, cp)
    for name, v in (("--alert", alert), ("--event", args.event)):
        if v:
            date.fromisoformat(v)  # fail before the slow part, not in the middle of the table
    prows, orows, pn = D.study(conn, cp, args.start, args.end)
    days = M.trading_days(pn.prices)
    tick = [p["ticker"] for p in pn.prox]
    print(f"\n=== {cp}: daily market study {args.start}..{args.end}  "
          f"(our first verified ALERT {alert}, credit event {args.event}) ===")
    print("cell = abnormal-return z vs SPY + window (. open, ~ closing, X closed)")
    print(f"{'date':<10} {'window':<8} {'Pstress':>7} {'bkt_z':>6} " + " ".join(f"{t:>7}" for t in tick))
    piv_z = prows.pivot(index="date", columns="ticker", values="ar_z")
    piv_l = prows.pivot(index="date", columns="ticker", values="label")
    for day, o in orows.iterrows():
        mark = ""
        if alert and day.date() >= date.fromisoformat(alert) and not getattr(cmd_study, "_a", False):
            mark, cmd_study._a = "  <- first trading day after/at our ALERT", True
        if args.event and day.date() == date.fromisoformat(args.event):
            mark += "  <- credit event filed"
        cells = " ".join(f"{fmt(piv_z.at[day, t], '+.1f'):>6}{LABEL_CH.get(piv_l.at[day, t], '?')}"
                         for t in tick)
        print(f"{day.date()} {o['window']:<8} {fmt(o['basket_p_stress'], '.2f'):>7} "
              f"{fmt(o['basket_z'], '+.1f'):>6} {cells}{mark}")

    fb = D.first_breaks(prows, orows)
    print(f"\nFIRST REGIME BREAK vs our ALERT ({alert}); lead = trading sessions from the alert to the "
          "break (break session counted; negative = market moved first;\n  an alert on a trading day "
          "is treated as after that close: a break that same day is lead +0)")
    print(f"  {'proxy':<7} {'role':<14} {'regime break':<12} {'lead':>5}  {'1st closing':<11} {'lead':>5}  "
          f"{'1st closed':<11} {'lead':>5}  min car5_z")
    for t, b in fb.items():
        brk = b.get("ms_break") if t == "BASKET" else b["bocpd_break"]
        ld = lambda x: fmt(D.lead_sessions(days, alert, x) if x is not None else None, "+d")  # noqa: E731
        extra = (f"max P(stress) {b['max_p_stress']}" if t == "BASKET" else f"{b['min_car5_z']}")
        print(f"  {t:<7} {b['role']:<14} {_d(brk) or 'none':<12} {ld(brk):>5}  "
              f"{_d(b['first_closing']) or 'none':<11} {ld(b['first_closing']):>5}  "
              f"{_d(b['first_closed']) or 'none':<11} {ld(b['first_closed']):>5}  {extra}")
    print(f"  regime break: proxy = BOCPD P(change point in last 3 days) >= {R.BREAK_P} on a down move; "
          f"BASKET = Markov P(stress) >= 0.5 while the basket is falling.\n"
          "  closing/closed = that proxy's action window (BASKET row: the overall window).")

    _check_no_lookahead(conn, cp, prows, orows, [alert, args.event])
    out = Path(args.out) if args.out else DATA_DIR / f"market_study_{cp}.png"
    chart(cp, prows, orows, pn, alert, args.event, out, fb)
    print(f"\nchart: {out}")
    if args.csv:
        csv = out.with_suffix(".csv")
        prows.merge(orows.reset_index(), on="date").to_csv(csv, index=False)
        print(f"csv:   {csv}")


def _db_alert(conn, cp) -> str | None:
    r = conn.execute("SELECT MIN(alerted_as_of) FROM events WHERE counterparty_id=? "
                     "AND alerted_as_of IS NOT NULL", (cp,)).fetchone()
    return r[0] if r else None


def _check_no_lookahead(conn, cp, prows, orows, dates) -> None:
    """Re-run assess() on data truncated at each date; it must match the study row."""
    for d in [x for x in dates if x]:
        a = D.assess(conn, cp, d)
        day = pd.Timestamp(a["bar_date"])
        if day not in orows.index:
            continue
        s = prows[prows["date"] == day].set_index("ticker")
        bad = [r["ticker"] for r in a["proxies"]
               if r.get("label") != s.at[r["ticker"], "label"]
               or abs((r.get("ar_z") or 0) - (s.at[r["ticker"], "ar_z"] or 0)) > 1e-6]
        ps_a, ps_s = a["basket"]["p_stress"], orows.at[day, "basket_p_stress"]
        print(f"no-look-ahead check @ {d} (close {a['bar_date']}): features/labels "
              f"{'match' if not bad else 'DIFFER ' + str(bad)}; window {a['window']} vs "
              f"{orows.at[day, 'window']}; P(stress) {fmt(ps_a, '.3f')} vs {fmt(ps_s, '.3f')}")


# ------------------------------------------------------------------ chart
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STATUS = {"open": "#ecebe7", "closing": "#fab219", "closed": "#d03b3b"}


def chart(cp, prows, orows, pn, alert, event, out: Path, fb: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    direct = [p["ticker"] for p in pn.prox if p["role"] in R.DIRECT_ROLES]
    context = [p["ticker"] for p in pn.prox if p["role"] in ("sector_etf", "credit")]
    dates = orows.index
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(11, 8.6), sharex=True, facecolor=SURFACE,
        gridspec_kw={"height_ratios": [3.2, 0.2 * (len(direct) + 1), 1.5], "hspace": 0.22})
    for ax in (ax1, ax2, ax3):
        ax.set_facecolor(SURFACE)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)

    # 1) cumulative abnormal return vs SPY since the study start
    car = prows.pivot(index="date", columns="ticker", values="ar").fillna(0).cumsum() * 100
    for t in context:
        ax1.plot(car.index, car[t], color=MUTED, lw=1.2, ls=(0, (3, 2)), zorder=2)
    ends = []
    for i, t in enumerate(direct):
        ax1.plot(car.index, car[t], color=SERIES[i], lw=2, zorder=3, label=t)
        ends.append((car[t].iloc[-1], t))
    # direct end labels, nudged apart so they never collide
    ends.sort()
    y_prev = None
    span = max(1.0, car[direct].max().max() - car[direct].min().min())
    for y, t in ends:
        y_lab = y if y_prev is None else max(y, y_prev + span * 0.045)
        ax1.annotate(t, (car.index[-1], y), xytext=(car.index[-1] + pd.Timedelta(days=1.5), y_lab),
                     va="center", fontsize=8.5, color=INK, fontweight="bold")
        y_prev = y_lab
    ax1.axhline(0, color=INK2, lw=0.8)
    ax1.set_ylabel("cumulative abnormal return\nvs SPY since start (%)")
    ax1.grid(axis="y", color=GRID, lw=0.8)
    leg = ax1.legend(loc="lower left", ncol=len(direct), frameon=False, fontsize=8.5,
                     handlelength=1.6, title="direct proxies (dashed grey: " + ", ".join(context) + ")",
                     title_fontsize=8)
    leg.get_title().set_color(INK2)

    # 2) action-window strip: one row per direct proxy + the overall window
    order = {"open": 0, "closing": 1, "closed": 2, "n/a": 0}
    lab = prows.pivot(index="date", columns="ticker", values="label").reindex(dates)
    rows = [lab[t].map(order).fillna(0).to_numpy() for t in direct]
    rows.append(orows["window"].map(order).fillna(0).to_numpy())
    names = direct + ["OVERALL"]
    edges = list(dates) + [dates[-1] + pd.Timedelta(days=1)]
    ax2.pcolormesh(mdates.date2num(edges), range(len(names) + 1), rows,
                   cmap=ListedColormap([STATUS["open"], STATUS["closing"], STATUS["closed"]]),
                   vmin=-0.5, vmax=2.5, edgecolors="none")
    for i in range(1, len(names)):
        ax2.axhline(i, color=SURFACE, lw=2.5 if i == len(direct) else 1.5)
    ax2.set_yticks([i + 0.5 for i in range(len(names))], names, fontsize=8)
    ax2.invert_yaxis()
    ax2.tick_params(axis="y", length=0)
    ax2.legend(handles=[Patch(color=STATUS[k], label=k) for k in ("open", "closing", "closed")],
               loc="upper left", bbox_to_anchor=(1.0, 1.05), frameon=False, fontsize=8,
               title="action window", title_fontsize=8)

    # 3) regime probabilities
    ax3.fill_between(dates, orows["basket_p_stress"].astype(float), color=SERIES[0], alpha=0.15, lw=0)
    ax3.plot(dates, orows["basket_p_stress"].astype(float), color=SERIES[0], lw=2,
             label="basket P(stress regime), Markov switching")
    ax3.plot(dates, orows["basket_bocpd"].astype(float), color=SERIES[1], lw=2,
             label="basket BOCPD P(change point in last 3 days)")
    bmax = prows[prows["role"].isin(R.DIRECT_ROLES)].pivot(index="date", columns="ticker",
                                                              values="bocpd_ar").max(axis=1)
    ax3.plot(bmax.index, bmax, color=SERIES[6], lw=1.2, ls=(0, (1, 1.5)),
             label="max BOCPD across direct proxies")
    ax3.axhline(0.5, color=MUTED, lw=0.8, ls=(0, (4, 3)))
    ax3.set_ylim(0, 1.04)
    ax3.set_ylabel("probability")
    ax3.grid(axis="y", color=GRID, lw=0.8)
    ax3.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, frameon=False, fontsize=8,
               borderaxespad=0.2)

    # alert / filing markers
    for ax in (ax1, ax2, ax3):
        if alert:
            ax.axvline(pd.Timestamp(alert), color=INK, lw=1.4, zorder=5)
        if event:
            ax.axvline(pd.Timestamp(event), color=STATUS["closed"], lw=1.4, ls=(0, (4, 2)), zorder=5)
    tr = ax1.get_xaxis_transform()
    if alert:
        ax1.text(pd.Timestamp(alert), 1.01, f"our verified ALERT {alert}", transform=tr,
                 ha="right", va="bottom", fontsize=8.5, color=INK, fontweight="bold")
    if event:
        ax1.text(pd.Timestamp(event), 1.01, f"filing {event}", transform=tr, ha="left",
                 va="bottom", fontsize=8.5, color=STATUS["closed"], fontweight="bold")
    ax3.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax3.set_xlim(dates[0] - pd.Timedelta(days=1), dates[-1] + pd.Timedelta(days=5))

    name = {"tricolor": "Tricolor", "first_brands": "First Brands"}.get(cp, cp)
    bk = fb.get("BASKET", {})
    fig.suptitle(f"{name}: was the market already pricing it when we alerted?", x=0.06, ha="left",
                 fontsize=13, color=INK, fontweight="bold", y=0.985)
    fig.text(0.06, 0.952, f"Daily, no look-ahead (each day uses only closes up to that day). Basket max "
             f"P(stress) {bk.get('max_p_stress')}. Hypothetical positions; analysis, not advice.",
             fontsize=8.5, color=INK2)
    fig.subplots_adjust(left=0.1, right=0.86, top=0.91, bottom=0.05)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assess")
    a.add_argument("--counterparty", required=True)
    a.add_argument("--as-of", required=True)
    a.add_argument("--after-close", action="store_true",
                   help="the alert is known to have come after the close of --as-of, so that "
                        "session's bar may be used (default: only sessions closed before --as-of)")
    a.add_argument("--save", action="store_true", help="store a market_assessments row")
    a.add_argument("--json", action="store_true")
    s = sub.add_parser("study")
    s.add_argument("--counterparty", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--event", help="credit event (filing) date")
    s.add_argument("--alert", help="our first verified ALERT date (default: from the events table)")
    s.add_argument("--out", help="chart path (default data/market_study_<cp>.png)")
    s.add_argument("--csv", action="store_true", help="also write the daily series as CSV")
    f = sub.add_parser("fetch")
    f.add_argument("--end", default=date.today().isoformat())
    args = ap.parse_args()
    conn = connect()
    try:
        {"assess": cmd_assess, "study": cmd_study, "fetch": cmd_fetch}[args.cmd](args, conn)
    except (ValueError, KeyError) as e:  # unknown counterparty, bad date, no market data
        sys.exit(f"market.py {args.cmd}: {e}")


if __name__ == "__main__":
    main()
