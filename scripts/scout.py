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
            lead = f"T{'-' if days >= 0 else '+'}{abs(days):>3}d"  # T+ = after the event
        tags = ",".join(json.loads(r["tags"])) or "-"
        print(f"{r['published_at'] or '????-??-??'} {lead:>6} [{SEV[r['severity']]:>8}] "
              f"{tags:<28} {(r['title'] or '')[:80]}  ({r['domain']})")


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
    ap.add_argument("--as-of-replay", action="store_true",
                    help="retag: force as-of scoring (Wayback snapshot at first_seen_as_of) for "
                         "every signal; replayed signals always use it")
    ap.add_argument("--min-severity", type=int, default=0)
    ap.add_argument("--fetch", type=int, default=None,
                    help="fetch full text (Contents API) for new signals >= this severity")
    args = ap.parse_args()

    if args.step < 1 or args.lookback < 1:
        ap.error("--step and --lookback must be >= 1")  # --step 0 would sweep (and bill) forever
    if args.start and args.end and args.start > args.end:
        ap.error("--start is after --end")

    conn = connect()
    portfolio = json.loads(PORTFOLIO_PATH.read_text())
    load_portfolio(conn, portfolio)
    # Sweep what the portfolio lists today, not ids left in the DB by an older portfolio.json.
    known = [cp["id"] for cp in portfolio["counterparties"]]
    unknown = [c for c in args.counterparty or [] if c not in known]
    if unknown:
        ap.error(f"unknown counterparty {', '.join(unknown)} (portfolio has: {', '.join(known)})")
    cps = args.counterparty or known

    if args.mode == "retag":
        scout = Scout(conn, client=object())  # no You.com client needed
        n = scout.retag(as_of_replay=args.as_of_replay)
        print(f"re-scored {n} stored signals (replayed signals on as-of text, live ones on "
              "today's snippets)")
        a = scout._archive()
        print(f"archive.org: {a.requests} requests, {a.lookups} lookups, {a.cache_hits} cache hits"
              + ("  (UNREACHABLE: fell back to title + description)" if a.down else ""))
        for b, k in conn.execute("SELECT score_basis, COUNT(*) FROM signals GROUP BY 1"):
            print(f"  score_basis {b}: {k}")
        if conn.execute("SELECT 1 FROM signals WHERE event_id IS NOT NULL").fetchone():
            print("note: existing events keep their old severity/tags; rebuild them with "
                  "`scripts/verify.py reset` and a verify replay")
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
        try:
            for d in dates:
                lookback = args.lookback if args.mode == "live" else max(args.step, 1) + 2
                new = scout.sweep(d, lookback_days=lookback, mode=args.mode,
                                  counterparty_ids=cps, fetch_min_severity=args.fetch)
                top = max((s.severity for s in new), default=0)
                # Raw, unverified signals: only the Verifier raises a (verified) ALERT.
                flag = " <-- high-severity signal (unverified)" if top >= 4 else ""
                print(f"[{args.mode} as_of {d}] new signals: {len(new):>2}  "
                      f"max severity: {SEV[top]}{flag}")
                for s in sorted(new, key=lambda s: -s.severity):
                    if s.severity >= 3:
                        print(f"      {s.published_at} [{SEV[s.severity]}] {s.title[:90]} ({s.domain})")
        finally:  # the bill is owed even if the run is interrupted
            print(f"\nAPI usage: {client.calls}  (~${client.calls['search'] * 0.005:.2f} search "
                  f"+ ${client.calls['contents_pages'] * 0.001:.3f} contents)")
        if scout.errors:
            print(f"WARNING: {len(scout.errors)} You.com call(s) failed; those windows have gaps. "
                  "Re-run the same command to fill them (stored signals are not duplicated):")
            for e in scout.errors:
                print(f"  {e}")
        a = getattr(scout, "archive", None)
        if a is not None and a.down:
            print("WARNING: archive.org was unreachable; affected signals were scored on "
                  "title + description only. `scripts/scout.py retag` re-scores them later.")

    for cp in cps:
        print_timeline(conn, cp, args.event, args.min_severity)


if __name__ == "__main__":
    main()
