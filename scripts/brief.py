"""Morning ACTION BRIEF for a replay date, strictly as-of.

  python3 scripts/brief.py --as-of 2025-09-07 [--counterparty tricolor] [--db path] [--no-market]
Prints Markdown and writes data/briefs/<date>.md (or <date>_<cp>.md with --counterparty).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import demo_data as X

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--counterparty", action="append")
    ap.add_argument("--db")
    ap.add_argument("--no-market", action="store_true")
    a = ap.parse_args()
    conn = X.open_db(a.db)
    md = X.render_brief(conn, a.as_of, a.counterparty, market=not a.no_market)
    out = X.ROOT / "data" / "briefs"
    out.mkdir(parents=True, exist_ok=True)
    name = a.as_of + ("_" + "_".join(a.counterparty) if a.counterparty else "") + ".md"
    (out / name).write_text(md)
    print(md)
    print(f"[wrote {Path('data/briefs') / name}]")


if __name__ == "__main__":
    main()
