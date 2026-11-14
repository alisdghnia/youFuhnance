"""Run Scout + Verifier + Market desk as a team that talks over the bus (optionally mirrored to
OpenCrawl). Verifier `alerts` trigger the Market desk, which publishes on `actions`.

  YOUFIN_BUS=local  python scripts/team.py replay --start 2025-08-25 --end 2025-09-25 --from-db
  YOUFIN_BUS=mirror python scripts/team.py replay --start 2025-09-05 --end 2025-09-10 \\
        --from-db --counterparty tricolor
  python scripts/team.py status
  python scripts/team.py leave
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youfin.agents.scout import Scout  # noqa: E402
from youfin.agents.verifier import Verifier  # noqa: E402
from youfin.bus import Bus  # noqa: E402
from youfin.db import connect  # noqa: E402
from youfin.llm import SCHEMA_SQL  # noqa: E402


def make_market_desk(conn, bus):
    """The market desk is optional: without its numeric stack (pandas, scipy, statsmodels)
    or on any start-up failure, Scout + Verifier still run."""
    try:
        from youfin.agents.market import MarketDesk
        desk = MarketDesk(conn, bus=bus)     # subscribes to `alerts`, publishes `actions`
        desk.reset()                         # fresh market_assessments for this replay
        return desk
    except Exception as e:
        print(f"[team] market desk disabled: {type(e).__name__}: {e}")
        return None


def replay(args, conn) -> None:
    bus = Bus(conn)
    bus.clear()
    cps = args.counterparty or None
    scout = Scout(conn, bus=bus)
    verifier = Verifier(conn, bus=bus)   # subscribes to `signals`
    market = make_market_desk(conn, bus)
    print(f"bus backend: {'mirror' if bus.mirror else 'local'}")
    days = [args.start + timedelta(n) for n in range((args.end - args.start).days + 1)]
    for i, d in enumerate(days):
        mark = len(bus.transcript)
        if args.from_db:
            scout.emit_from_db(d, first_day=(i == 0), counterparty_ids=cps)
        else:
            scout.sweep(d, mode="replay", counterparty_ids=cps)
        verifier.step(d, "replay")
        if market is not None:
            market.step(d)               # never raises: failed alerts land in market.errors
        msgs = bus.transcript[mark:]
        if not msgs:
            continue
        print(f"\n[{d}]")
        sig = [m for m in msgs if m["topic"] == "signals"]
        if sig:
            by_cp: dict[str, int] = {}
            for m in sig:
                by_cp[m["payload"]["counterparty"]] = by_cp.get(m["payload"]["counterparty"], 0) + 1
            print(f"  scout    -> signals : {len(sig)} new ("
                  + ", ".join(f"{k}:{v}" for k, v in sorted(by_cp.items())) + ")")
        for m in msgs:
            if m["topic"] != "signals":
                print(f"  {m['sender']:<8} -> {m['topic']:<7}: {m['payload'].get('summary', '')[:110]}")
    print_alerts(conn, cps, args.start, args.end)
    print_actions(bus)
    for e in (market.errors if market is not None else []):
        print(f"  market desk could not assess {e}")
    client = getattr(verifier, "client", None)
    if client is not None:  # the Verifier's corroboration searches are billed even with --from-db
        print(f"\nYou.com usage (verifier): {client.calls} (~${client.calls['search'] * 0.005:.2f})")
    if bus.mirror or bus.mirror_error or bus.backend == "mirror":
        print(f"\nmirror: {bus.mirror_stats()}" + (f"  (disabled: {bus.mirror_error})" if bus.mirror_error else ""))


def print_alerts(conn, cps, start, end) -> None:
    print(f"\nfirst verified ALERT per counterparty raised in this replay ({start}..{end}):")
    for r in conn.execute("SELECT counterparty_id, MIN(alerted_as_of) a FROM events "
                          "WHERE alerted_as_of BETWEEN ? AND ? GROUP BY counterparty_id",
                          (start.isoformat(), end.isoformat())):
        if not cps or r[0] in cps:
            print(f"  {r[0]}: {r['a']}")


def print_actions(bus) -> None:
    acts = [m for m in bus.transcript if m["topic"] == "actions"]
    print(f"\nmarket desk actions: {len(acts)}")
    for m in acts:
        p = m["payload"]
        hedges = ", ".join(f"{h['ticker']}({h['score']:.2f})" for h in p["hedge_candidates"])
        print(f"  {p['counterparty']} event {p['event_id']} as of {p['as_of']} (close {p['bar_date']}): "
              f"window {p['window']}, basket P(stress) {p['basket_p_stress']}, hedges {hedges}")


def status(conn) -> None:
    bus = Bus(conn)
    print("bus_messages:", bus.counts() or "empty")
    print("subscriptions:", [tuple(r) for r in conn.execute("SELECT agent, topic FROM bus_subs")])
    print("mirror:", bus.mirror_stats())
    rows = conn.execute("SELECT * FROM bus_agents").fetchall()
    for r in rows:
        print(f"  board agent {r['board_name']} session={r['session_id']} "
              f"registered={r['registered_at']} left={r['left_at']}")
    if not rows:
        print("  no board agents registered")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--start", type=date.fromisoformat, required=True)
    r.add_argument("--end", type=date.fromisoformat, required=True)
    r.add_argument("--from-db", action="store_true", help="Scout re-emits stored signals (no Scout searches); the Verifier may "
                        "still run billed You.com corroboration searches")
    r.add_argument("--counterparty", action="append")
    sub.add_parser("status")
    sub.add_parser("leave")
    args = ap.parse_args()
    if args.cmd == "replay" and args.start > args.end:
        ap.error("--start is after --end")
    conn = connect()
    conn.executescript(SCHEMA_SQL)
    if args.cmd == "replay":
        replay(args, conn)
    elif args.cmd == "status":
        status(conn)
    else:
        for name, res in Bus(conn).leave():
            print(f"{name}: {res}")


if __name__ == "__main__":
    main()
