"""Build data/dashboard.html: one self-contained file (inline CSS/JS/JSON, charts drawn live as inline SVG).

  python3 scripts/dashboard.py --open [--db path/to/youfin.db]
Replays Aug 25 -> Sep 25 2025 day by day; every panel is strictly as-of the slider date.
"""
from __future__ import annotations

import argparse
import json
import webbrowser

import demo_data as X  # noqa: E402


def build(conn, cps=None, last=None, marks=True) -> dict:
    """cps: restrict counterparties; last: stop at this day (leak_check); marks: hindsight lead-time cards."""
    dates = list(X.daterange(X.START, min(last or X.END, X.END)))
    data = {"dates": dates, "disclaimer": X.DISCLAIMER, "cps": {}, "market": X.has_market(conn)}
    for c in X.counterparties(conn):
        cp = c["id"]
        if cps and cp not in cps:
            continue
        ps, tot = X.exposure(conn, cp)
        ms = X.market_series(conn, cp) if data["market"] else None
        days, pins, seen = {}, [], set()
        prev_acts = []
        for d in dates:
            evs = X.events_at(conn, cp, d)
            evs = [e for e in evs if e["level"] != "log" or e["new"] or e["severity"] >= 4]
            for e in evs:
                e["sources"] = e["sources"][:5]
                for s in e["sources"]:
                    s["quote"] = (s["quote"] or "")[:300]
            level = max((X.LEVEL_RANK[e["level"]] for e in evs), default=0)
            acts = X.actions_at(conn, cp, d)
            for e in evs:
                if e["id"] not in seen or e["new"]:
                    r = X.best_receipt(e) or {}
                    pins.append({"d": d, "id": e["id"], "level": e["level"], "kind": ("escalated" if e["escalated"] else e["new"] or "seen"),
                                 "headline": e["headline"], "verdict": e["verdict"], "sev": e["severity"],
                                 "src": r.get("domain"), "tier": r.get("tier"), "tier_name": r.get("tier_name"),
                                 "quote": (r.get("quote") or "")[:240]})
                    seen.add(e["id"])
            days[d] = {"team": X.team_at(conn, cp, c["name"], d, evs, ms, acts, prev_acts, tot, len(ps)), "events": evs, "level": ["log", "watch", "ALERT"][level],
                       "market": X.market_at(conn, cp, d) if level >= 1 or data["market"] else None,
                       "actions": acts, "feed": X.feed_at(conn, cp, d),
                       "brief": X.render_cp(conn, cp, c["name"], d)}
            prev_acts = acts
        data["cps"][cp] = {"name": c["name"], "role": c["role"], "sector": c["sector"], "positions": ps,
                           "exposure": tot, "marks": X.study_marks(conn, cp) if marks else None, "days": days,
                           "ms": ms, "pins": pins}
        print(f"built {cp}", flush=True)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--out", default=str(X.ROOT / "data" / "dashboard.html"))
    a = ap.parse_args()
    data = build(X.open_db(a.db))
    tpl = (X.ROOT / "scripts" / "dashboard_template.html").read_text()
    blob = json.dumps(data, default=str).replace("</", "<\\/")
    open(a.out, "w").write(tpl.replace("__DATA__", blob))
    print("wrote", a.out)
    if a.open:
        webbrowser.open("file://" + a.out)


if __name__ == "__main__":
    main()
