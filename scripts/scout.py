"""Run the Scout agent live or as a historical replay.

Examples:
  python scripts/scout.py live --counterparty tricolor
  python scripts/scout.py replay --counterparty tricolor \
      --start 2025-05-01 --end 2025-09-10 --step 7 --daily-from 2025-08-25 --event 2025-09-10
  python scripts/scout.py timeline --counterparty tricolor --event 2025-09-10
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youfin.agents.scout import Scout  # noqa: E402
from youfin.config import PORTFOLIO_PATH  # noqa: E402
from youfin.db import connect, load_portfolio  # noqa: E402
from youfin.youcom import YouClient  # noqa: E402

SEV = {0: "info", 1: "low", 2: "watch", 3: "elevated", 4: "high", 5: "CRITICAL"}


def replay_dates(start: date, end: date, step: int, daily_from: date | None):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1 if daily_from and d >= daily_from else step)


def print_timeline(conn, cp_id: str, event: date | None, min_sev: int) -> None:
    rows = conn.execute(
        "SELECT s.*, a.title, a.domain FROM signals s JOIN articles a USING (url) "
        "WHERE s.counterparty_id=? AND s.severity>=? ORDER BY s.published_at, s.severity DESC",
        (cp_id, min_sev),
    ).fetchall()
    print(f"\n=== Timeline: {cp_id} ({len(rows)} signals, severity >= {min_sev}) ===")
    for r in rows:
        lead = ""
        if event and r["published_at"]:
            days = (event - date.fromisoformat(r["published_at"])).days
            lead = f"T-{days:>3}d"
        tags = ",".join(json.loads(r["tags"])) or "-"
        print(f"{r['published_at'] or '????-??-??'} {lead:>6} [{SEV[r['severity']]:>8}] "
              f"{tags:<28} {r['title'][:80]}  ({r['domain']})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "replay", "timeline", "retag"])
    ap.add_argument("--counterparty", action="append")
    ap.add_argument("--lookback", type=int, default=7)
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--step", type=int, default=7)
    ap.add_argument("--daily-from", type=date.fromisoformat)
    ap.add_argument("--event", type=date.fromisoformat, help="credit event date, for lead-time")
    ap.add_argument("--min-severity", type=int, default=0)
    ap.add_argument("--fetch", type=int, default=None,
                    help="fetch full text (Contents API) for new signals >= this severity")
    args = ap.parse_args()

    conn = connect()
    load_portfolio(conn, json.loads(PORTFOLIO_PATH.read_text()))
    cps = args.counterparty or [r["id"] for r in conn.execute("SELECT id FROM counterparties")]

    if args.mode == "retag":
        n = Scout(conn, client=object()).retag()  # no API client needed
        print(f"re-scored {n} stored signals")
        return
    elif args.mode != "timeline":
        client = YouClient()
        scout = Scout(conn, client)
        if args.mode == "live":
            dates = [date.today()]
        else:
            if not (args.start and args.end):
                ap.error("replay needs --start and --end")
            dates = list(replay_dates(args.start, args.end, args.step, args.daily_from))
        for d in dates:
            lookback = args.lookback if args.mode == "live" else max(args.step, 1) + 2
            new = scout.sweep(d, lookback_days=lookback, mode=args.mode,
                              counterparty_ids=cps, fetch_min_severity=args.fetch)
            top = max((s.severity for s in new), default=0)
            flag = " <-- ALERT" if top >= 4 else ""
            print(f"[{args.mode} as_of {d}] new signals: {len(new):>2}  max severity: {SEV[top]}{flag}")
            for s in sorted(new, key=lambda s: -s.severity):
                if s.severity >= 3:
                    print(f"      {s.published_at} [{SEV[s.severity]}] {s.title[:90]} ({s.domain})")
        print(f"\nAPI usage: {client.calls}  (~${client.calls['search'] * 0.005:.2f} search "
              f"+ ${client.calls['contents_pages'] * 0.001:.3f} contents)")

    for cp in cps:
        print_timeline(conn, cp, args.event, args.min_severity)


if __name__ == "__main__":
    main()
