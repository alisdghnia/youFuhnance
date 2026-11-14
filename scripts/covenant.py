"""Covenant & Collateral Rights desk CLI (considerations for the deal team and counsel, not legal advice).

  python3 scripts/covenant.py assess --position abf-001 --as-of 2025-09-07
  python3 scripts/covenant.py assess --position abf-003 --as-of 2025-09-02 --llm   # + local-LLM wording
  python3 scripts/covenant.py timeline --position abf-001 --start 2025-09-01 --end 2025-09-12 \\
        --event 2025-09-10
  python3 scripts/covenant.py terms --position abf-002      # the mock contract triggers
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youfin.agents import covenant as C  # noqa: E402
from youfin.db import connect  # noqa: E402
from youfin.sources import TIER_NAMES  # noqa: E402

STATUS_TXT = {C.LIKELY: "LIKELY TRIPPED", C.POSSIBLE: "possible", C.NOT: "-"}
W = 112


def wrap(text: str, indent: int, hang: int | None = None) -> str:
    return textwrap.fill(text, W, initial_indent=" " * indent,
                         subsequent_indent=" " * (indent if hang is None else hang))


def short_cure(cure: str) -> str:
    c = (cure or "-").split(";")[0].split(" (")[0]
    return c if len(c) <= 22 else c[:21] + "~"


def header(a: dict) -> None:
    p = a["position"]
    rel = ", ".join(f"{c['id']} ({c['relationship']})" for c in p["counterparties"])
    print(f"\n=== COVENANT & COLLATERAL RIGHTS | {p['id']} {p['name']} ===")
    print(f"{p['facility_type']} | ${p['commitment_usd'] / 1e6:.0f}M | {rel} | TERMS HYPOTHETICAL")


def print_assessment(a: dict) -> None:
    header(a)
    used = a["events_used"]
    print(f"as of {a['as_of']}: strict as-of (events/sources seen or published <= {a['as_of']}); "
          f"{len(used)} watch/ALERT events used of {a['n_events_seen']} seen")
    print(f"STAGE {a['stage']}/4 {a['stage_label'].upper()}"
          + (f"  <- {'; '.join(a['stage_why'])}" if a["stage_why"] else ""))

    print(f"\nTRIGGERS ({sum(r['status'] == C.LIKELY for r in a['triggers'])} likely tripped, "
          f"{sum(r['status'] == C.POSSIBLE for r in a['triggers'])} possible)")
    print(f"  {'status':<15} {'id':<15} {'trigger':<44} {'cure':<24} evidence")
    order = {r["id"]: i for i, r in enumerate(a["triggers"])}
    for r in sorted(a["triggers"], key=lambda r: (-C.STATUS_RANK[r["status"]], order[r["id"]])):
        e = r["evidence"][0] if r["evidence"] else None
        if e:
            ev = f"ev{e['event_id']} {e['verdict']}/{e['level']} ({r['basis']}: {','.join(e['tags'])})"
            if r["basis"] == "derived":
                ev = f"via {', '.join(r.get('derived_from', [])[:3])}; " + ev.split(" (")[0]
        else:
            ev = "not observable in public news" if not r["observable"] else "-"
        cap = "*" if r["capped"] and r["status"] == C.POSSIBLE else ""
        print(f"  {STATUS_TXT[r['status']] + cap:<15} {r['id']:<15} {r['label'][:44]:<44} "
              f"{short_cure(r['cure']):<24} {ev}")
    if any(r["capped"] and r["status"] == C.POSSIBLE for r in a["triggers"]):
        print("  * capped at 'possible': a MAC clause is contestable; rely on specific defaults and reserve rights")

    receipts: dict[tuple, dict] = {}
    for r in a["triggers"]:
        for e in r["evidence"][:1]:
            if r["basis"] == "derived":
                continue
            k = (e["event_id"], e["receipt"]["url"])
            receipts.setdefault(k, {"e": e, "for": []})["for"].append(r["id"])
    if receipts:
        print("\nRECEIPTS (the source behind each status, as known on the date)")
    for (eid, _), v in sorted(receipts.items()):
        e, rc = v["e"], v["e"]["receipt"]
        print(f"  [ev{eid}] {e['verdict']} / {e['level']} | {rc['publisher']} "
              f"({TIER_NAMES.get(rc['tier'], rc['tier'])}) {rc['published_at']} | for {', '.join(v['for'])}")
        print(wrap(f"title: {rc['title'][:150]}", 7))
        if rc["quote"]:
            print(wrap(f"quote: \"{rc['quote'][:280]}\"", 7))
        print(f"       {rc['url']}")

    print("\nCHECKLIST (ordered by urgency)")
    if a["stage"] == 4:
        print(wrap("A petition has been filed: pre-filing self-help is now subject to the automatic stay. "
                   "Post-petition considerations only.", 2))
    if not a["checklist"]:
        print("  monitor: no contractual trigger indicated by verified news")
    for c in a["checklist"]:
        print(wrap(f"{c['n']:>2}. [{c['urgency']}] {c['action']}", 2, 8))
        print(wrap(f"why: {c['why']}", 8))
        if c["because"]:
            print(wrap(f"because: {'; '.join(c['because'])}", 8))
        print(f"        owner: {c['owner']}{' (counsel)' if c['counsel'] and 'counsel' not in c['owner'] else ''}")
    if a.get("preference_note") and any(c["id"] in ("cash_dominion", "redirect_collections", "preference_review")
                                        for c in a["checklist"]):
        print(wrap(f"note: {a['preference_note']}", 2))
    if a.get("draft"):
        print("\nLLM-drafted wording (local model; statuses above are rule-based and unchanged):")
        print(wrap(a["draft"], 2))
    print(f"\n{a['disclaimer']}")


def cmd_assess(args, conn) -> None:
    pos = C.get_position(args.position)
    judge = None
    if args.llm:
        from youfin.llm import Judge
        judge = Judge(conn)
    a = C.assess(conn, pos, args.as_of, judge=judge)
    if judge is not None and a.get("draft") is None:
        print("[llm] local model unavailable: no draft wording (statuses unaffected)")
    if args.json:
        print(json.dumps(a, indent=1, default=str))
        return
    print_assessment(a)
    if args.save:
        print(f"\nsaved covenant_assessments id={C.save(conn, a, 'cli')}")


def cmd_timeline(args, conn) -> None:
    pos = C.get_position(args.position)
    rows = C.timeline(conn, pos, args.start, args.end)
    header(rows[0])
    ev = date.fromisoformat(args.event) if args.event else None
    print(f"timeline {args.start}..{args.end}" + (f" | credit event {ev}" if ev else "")
          + " | each day uses only what was known that day")
    print(f"  {'date':<10} {'lead':>5}  {'stage':<23} {'likely':>6} {'poss':>5}  first actions")
    for i, a in enumerate(rows):
        if not (args.all or i == 0 or a["changes"]):
            continue
        d = date.fromisoformat(a["as_of"])
        lead = f"T{(d - ev).days:+d}" if ev else ""
        nl = sum(r["status"] == C.LIKELY for r in a["triggers"])
        npos = sum(r["status"] == C.POSSIBLE for r in a["triggers"])
        first = "; ".join(f"{c['short']} ({C._u(c['urgency'])})" for c in a["checklist"][:3]) or "monitor"
        print(f"  {a['as_of']:<10} {lead:>5}  {str(a['stage']) + ' ' + a['stage_label']:<23} {nl:>6} {npos:>5}  {first}")
        if i == 0:
            continue
        prev = {r["id"]: r["status"] for r in rows[i - 1]["triggers"]}
        for st in (C.LIKELY, C.POSSIBLE):
            up = [r for r in a["triggers"] if r["status"] == st and prev[r["id"]] != st]
            if up:
                print(wrap(f"+ {st}: " + ", ".join(
                    f"{r['id']} [ev{r['evidence'][0]['event_id']} {r['evidence'][0]['verdict']}]"
                    for r in up), 14, 16))
        tids = {r["id"] for r in a["triggers"]}
        for c in a["changes"]:
            if "stronger evidence" in c:
                print(wrap(f"+ {c}", 14, 16))
        other = [c for c in a["changes"] if c.split(":")[0] not in tids and not c.startswith("stage ")]
        if other:
            print(wrap(f"^ {'; '.join(other)}", 14, 16))
    last = rows[-1]
    print(f"\nat {last['as_of']}: stage {last['stage']} {last['stage_label']}; likely tripped: "
          f"{', '.join(r['id'] for r in last['triggers'] if r['status'] == C.LIKELY) or 'none'}")
    print(C.DISCLAIMER)


def cmd_terms(args, conn) -> None:
    pos = C.get_position(args.position)
    t = pos["terms"]
    print(f"\n=== {pos['id']} {pos['name']}: {t.get('_hypothetical')} ===  playbook: {t.get('playbook')}")
    for tr in C.triggers(t):
        ev = (f"derived from {', '.join(tr.derived_from)}" if tr.derived_from else
              f"direct {list(tr.direct)}{' + ' + tr.requires_text + ' text' if tr.requires_text else ''}; "
              f"indirect {list(tr.indirect)}" + ("" if tr.observable else "; NOT observable in news")
              + ("; capped at possible" if tr.max_status == C.POSSIBLE else ""))
        print(f"  {tr.id:<15} {tr.kind:<21} {tr.label}")
        print(wrap(f"evidence: {ev} | cure: {tr.cure or '-'} | consequence: {tr.consequence or '-'}", 19))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assess", help="trigger table + action checklist with receipts")
    a.add_argument("--position", required=True)
    a.add_argument("--as-of", type=date.fromisoformat, required=True)
    a.add_argument("--llm", action="store_true", help="add local-LLM drafted wording (never changes a status)")
    a.add_argument("--save", action="store_true", help="store in covenant_assessments (trigger 'cli')")
    a.add_argument("--json", action="store_true")
    t = sub.add_parser("timeline", help="how statuses and the checklist escalate day by day")
    t.add_argument("--position", required=True)
    t.add_argument("--start", type=date.fromisoformat, required=True)
    t.add_argument("--end", type=date.fromisoformat, required=True)
    t.add_argument("--event", help="credit event date YYYY-MM-DD, for the T-n lead column")
    t.add_argument("--all", action="store_true", help="print every day, not only days with changes")
    m = sub.add_parser("terms", help="print a position's mock contract triggers")
    m.add_argument("--position", required=True)
    args = ap.parse_args()
    if args.cmd == "timeline" and args.start > args.end:
        ap.error("--start is after --end")
    try:
        C.get_position(args.position)
    except KeyError as e:
        ap.error(str(e).strip("'\""))
    conn = connect()
    {"assess": cmd_assess, "timeline": cmd_timeline, "terms": cmd_terms}[args.cmd](args, conn)


if __name__ == "__main__":
    main()
