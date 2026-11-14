"""Build data/dashboard.html: one self-contained file (inline CSS/JS/JSON, charts as base64 PNG).

  python3 scripts/dashboard.py --open [--db path/to/youfin.db]
Replays Aug 25 -> Sep 25 2025 day by day; every panel is strictly as-of the slider date.
"""
from __future__ import annotations

import argparse
import base64
import json
import webbrowser

import demo_data as X  # noqa: E402


def build(conn) -> dict:
    dates = list(X.daterange(X.START, X.END))
    data = {"dates": dates, "disclaimer": X.DISCLAIMER, "cps": {}, "market": X.has_market(conn)}
    for c in X.counterparties(conn):
        cp = c["id"]
        ps, tot = X.exposure(conn, cp)
        days = {}
        for d in dates:
            evs = X.events_at(conn, cp, d)
            evs = [e for e in evs if e["level"] != "log" or e["new"] or e["severity"] >= 4]
            for e in evs:
                e["sources"] = e["sources"][:5]
                for s in e["sources"]:
                    s["quote"] = (s["quote"] or "")[:300]
            level = max((X.LEVEL_RANK[e["level"]] for e in evs), default=0)
            days[d] = {"events": evs, "level": ["log", "watch", "ALERT"][level],
                       "market": X.market_at(conn, cp, d) if level >= 1 or data["market"] else None,
                       "actions": X.actions_at(conn, cp, d), "feed": X.feed_at(conn, cp, d),
                       "brief": X.render_cp(conn, cp, c["name"], d)}
        png = X.ROOT / "data" / f"market_study_{cp}.png"
        data["cps"][cp] = {"name": c["name"], "role": c["role"], "sector": c["sector"], "positions": ps,
                           "exposure": tot, "marks": X.study_marks(conn, cp), "days": days,
                           "chart": base64.b64encode(png.read_bytes()).decode() if png.exists() else None}
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
