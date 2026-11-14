"""Run the Verifier agent over Scout signals.

Examples:
  python scripts/verify.py replay --start 2025-08-25 --end 2025-09-10 --event tricolor=2025-09-10
  python scripts/verify.py report --counterparty tricolor --event tricolor=2025-09-10
  python scripts/verify.py live
  python scripts/verify.py requote      # re-derive receipt quotes from stored data
  python scripts/verify.py reset        # drop events so a replay can be re-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youfin.agents.verifier import Verifier, receipt_sources  # noqa: E402
from youfin.db import connect  # noqa: E402
from youfin.llm import SCHEMA_SQL  # noqa: E402
from youfin.sources import TIER_NAMES  # noqa: E402
from youfin.youcom import YouClient  # noqa: E402

SEV = {0: "info", 1: "low", 2: "watch", 3: "elevated", 4: "high", 5: "CRITICAL"}
LEVEL_ICON = {"ALERT": "!!", "watch": "? ", "log": "  "}


def parse_events(items: list[str] | None) -> dict[str, date]:
    out = {}
    for it in items or []:
        cp, _, d = it.partition("=")
        out[cp] = date.fromisoformat(d)
    return out


def lead(d: str | None, event: date | None) -> str:
    if not (d and event):
        return "      "
    return f"T-{(event - date.fromisoformat(d)).days:>3}d"


def report(conn, cps: list[str], events: dict[str, date], min_level: str, receipts: int) -> None:
    levels = {"log": 0, "watch": 1, "ALERT": 2}
    for cp in cps:
        ev_date = events.get(cp)
        rows = conn.execute(
            "SELECT * FROM events WHERE counterparty_id=? ORDER BY first_published, id", (cp,)
        ).fetchall()
        rows = [r for r in rows if levels[r["alert_level"]] >= levels[min_level]]
        print(f"\n=== {cp}: {len(rows)} events (level >= {min_level}) ===")
        first_alert = min((r["alerted_as_of"] for r in rows if r["alerted_as_of"]), default=None)
        first_watch = min((r["watch_as_of"] for r in rows if r["watch_as_of"]), default=None)
        print(f"first WATCH: {first_watch} {lead(first_watch, ev_date)}   "
              f"first verified ALERT: {first_alert} {lead(first_alert, ev_date)}")
        for r in rows:
            n_src = conn.execute("SELECT COUNT(*) FROM event_sources WHERE event_id=?",
                                 (r["id"],)).fetchone()[0]
            print(f"\n{LEVEL_ICON[r['alert_level']]} {r['first_published']} "
                  f"{lead(r['first_published'], ev_date)} [{SEV[r['severity']]:>8}] "
                  f"{r['primary_tag']:<18} {r['verdict']} ({r['n_publishers']} publishers, "
                  f"{n_src} articles, conf {r['confidence']:.2f})")
            print(f"     {r['headline'][:110]}")
            if r["alerted_as_of"]:
                print(f"     ALERT raised as of {r['alerted_as_of']} {lead(r['alerted_as_of'], ev_date)}")
            srcs = receipt_sources(conn, r["id"], receipts)
            for d in conn.execute("SELECT * FROM llm_decisions WHERE event_id=? ORDER BY id",
                                  (r["id"],)).fetchall():
                print(f"       ~ LLM {d['kind']} -> {'yes' if d['answer'] else 'no'} "
                      f"[{d['applied']}] {(d['subject'] or '')[:60]}: {d['reason']}")
            for s in srcs:
                print(f"       - [{TIER_NAMES[s['tier']]}/{s['role']}] {s['domain']} "
                      f"({s['published_at']}): \"{(s['quote'] or '')[:160]}\"")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["replay", "live", "report", "reset", "requote"])
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--counterparty", action="append")
    ap.add_argument("--event", action="append", help="cp=YYYY-MM-DD credit event, for lead time")
    ap.add_argument("--min-level", default="watch", choices=["log", "watch", "ALERT"])
    ap.add_argument("--receipts", type=int, default=3)
    args = ap.parse_args()

    conn = connect()
    conn.executescript(SCHEMA_SQL)
    cps = args.counterparty or [r["id"] for r in conn.execute("SELECT id FROM counterparties")]
    events = parse_events(args.event)

    if args.mode == "reset":
        with conn:
            conn.execute("UPDATE signals SET event_id=NULL")
            conn.executescript(SCHEMA_SQL)
            for t in ("event_verifications", "event_sources", "events", "llm_decisions"):
                conn.execute(f"DELETE FROM {t}")
        print("events cleared")
        return

    if args.mode == "requote":
        v = Verifier(conn, YouClient())
        stats = v.requote()
        print(f"requote: {stats}")
        print(f"API usage: {v.client.calls} (~${v.client.calls['search'] * 0.005:.2f})")

    if args.mode in ("replay", "live"):
        client = YouClient()
        v = Verifier(conn, client)
        if args.mode == "live":
            days = [date.today()]
        else:
            if not (args.start and args.end):
                ap.error("replay needs --start and --end")
            days = [args.start + timedelta(n) for n in range((args.end - args.start).days + 1)]
        for d in days:
            n, verified = v.step(d, args.mode)
            alerts = conn.execute(
                "SELECT counterparty_id, headline FROM events WHERE alerted_as_of=?",
                (d.isoformat(),)).fetchall()
            if n or verified or alerts:
                vs = ", ".join(f"#{e}:{vd}(+{k})" for e, vd, k in verified) or "-"
                print(f"[{d}] clustered {n:>2} signals | verified: {vs}")
                for a in alerts:
                    print(f"      !! ALERT {a['counterparty_id']}: {a['headline'][:90]}")
        j = v.judge
        if j:
            avg = j.live_seconds / j.live_calls if j.live_calls else 0
            print(f"LLM ({j.model}): {j.live_calls} live calls, avg {avg:.1f}s, "
                  f"{j.cache_hits} cache hits, available={j.available}")
        print(f"\nAPI usage: {client.calls} (~${client.calls['search'] * 0.005:.2f})")
        a = getattr(v, "archive", None)
        if a:
            print(f"archive.org: {a.requests} requests, {a.lookups} lookups, {a.cache_hits} cache hits")

    report(conn, cps, events, args.min_level, args.receipts)


if __name__ == "__main__":
    main()
