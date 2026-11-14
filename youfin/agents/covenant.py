"""Covenant & Collateral Rights desk.

For a lender the most valuable lever is to use its contractual rights BEFORE a bankruptcy
petition: from the filing on, the automatic stay (11 U.S.C. 362) stops self-help. For every
verified ALERT, and for every later change in the evidence about that counterparty, the desk:
  1. finds the positions exposed to the counterparty (data/portfolio.json, mock `terms`),
  2. maps each verified event's risk tags + verdict onto every contractual trigger
     -> likely_tripped | possible | not_indicated, keeping the receipt (event, verdict, quote, URL),
  3. builds an ordered action checklist with urgency, which escalates as the evidence strengthens.

Status rules are deterministic (see `match`); the optional local LLM (youfin.llm.Judge) only
drafts a plain-English note FROM the finished result and cannot change a status.
Strict as-of: only events, signals and sources first seen / published on or before the as-of
date are used, and an event never counts at a higher level than the Verifier had raised by then.
Considerations for the deal team and counsel, not legal advice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import PORTFOLIO_PATH
from ..risk_tags import RULES
from .verifier import alert_level, is_junk, is_paywalled
from .verifier import judge as verdict_of

log = logging.getLogger("youfin.covenant")

LIKELY, POSSIBLE, NOT = "likely_tripped", "possible", "not_indicated"
STATUS_RANK = {NOT: 0, POSSIBLE: 1, LIKELY: 2}
STRONG = ("confirmed", "corroborated")          # Verifier verdicts that count as direct proof
VERDICT_RANK = {"confirmed": 3, "corroborated": 2, "weakly_corroborated": 1, "single_source": 0}
BASIS_RANK = {"direct": 0, "direct_no_text": 1, "indirect": 2}
HOURS, H24, WEEK = "within hours", "within 24h", "this week"
URGENCY_RANK = {HOURS: 0, H24: 1, WEEK: 2}
STAGES = {0: "monitor", 1: "watch", 2: "verified alert", 3: "pre-filing", 4: "filed: automatic stay"}
DISCLAIMER = ("Considerations for the deal team and counsel, not legal advice. Contract terms are "
              "hypothetical (data/portfolio.json).")

# Text a trigger's evidence must ALSO contain (on top of the tag) to count as direct proof.
TEXT_RULES = {
    # A petition actually filed, not one being prepared, expected or "as soon as today".
    "filing": re.compile(
        r"\b(filed|files)\b[\w\s,'’/-]{0,30}?\b(chapter\s*(7|11)|bankruptcy|petition)\b"
        r"|(?<!to )(?<!will )(?<!could )(?<!may )(?<!might )(?<!would )"
        r"\bfile (for )?(chapter (7|11) )?bankruptcy\b"
        r"|\bhits ch(apter|\.)\s?(7|11)\b|\bgoes bankrupt\b|\b\d:\d{2}-bk-\d{3,6}\b", re.I),
}
TAG_RE = {cat: [re.compile(p, re.I) for p in pats] for cat, (_, pats) in RULES.items()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS covenant_assessments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    trigger      TEXT NOT NULL,       -- bus message id, or 'cli'
    stage        INTEGER NOT NULL,    -- 0 monitor, 1 watch, 2 verified alert, 3 pre-filing, 4 filed
    n_likely     INTEGER NOT NULL,
    n_possible   INTEGER NOT NULL,
    digest       TEXT NOT NULL,       -- statuses + checklist urgencies (escalation detector)
    payload      TEXT NOT NULL,       -- JSON: triggers with evidence, checklist
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (position_id, as_of, trigger)
);
CREATE TABLE IF NOT EXISTS covenant_trigger_status (
    assessment_id INTEGER NOT NULL REFERENCES covenant_assessments(id),
    trigger_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,      -- likely_tripped | possible | not_indicated
    basis         TEXT,               -- direct | direct_no_text | indirect | derived
    event_id      INTEGER,            -- strongest evidence (events.id at assessment time)
    verdict       TEXT,               -- that event's verdict as of the assessment date
    receipt_url   TEXT,
    quote         TEXT,
    PRIMARY KEY (assessment_id, trigger_id)
);
"""


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)[:10]


# ------------------------------------------------------------------ contract terms
@dataclass(frozen=True)
class Trigger:
    id: str
    kind: str
    label: str
    description: str
    direct: tuple[str, ...] = ()
    indirect: tuple[str, ...] = ()
    requires_text: str | None = None
    severe: bool = False
    observable: bool = True
    max_status: str = LIKELY
    derived_from: tuple[str, ...] = ()
    cure: str = ""
    consequence: str = ""


# (path in `terms`, kind): every entry with an `id` is a trigger.
SECTIONS = (("review_events", "review_event"), ("events_of_default", "event_of_default"),
            ("termination_events", "termination_event"), ("cross_default", "cross_default"),
            ("servicer.termination_events", "servicer_termination"),
            ("performance_triggers", "performance"), ("sponsor_reps", "rep_breach"),
            ("mac_clause", "mac"), ("reporting_covenants", "reporting"),
            ("cash_management.springing_dominion", "cash_dominion"))


def triggers(terms: dict) -> list[Trigger]:
    out = []
    for path, kind in SECTIONS:
        node = terms
        for k in path.split("."):
            node = node.get(k) if isinstance(node, dict) else None
        for t in [node] if isinstance(node, dict) else (node or []):
            if not isinstance(t, dict) or not t.get("id"):
                continue
            ev = t.get("evidence") or {}
            rule = ev.get("requires_text")
            if rule and rule not in TEXT_RULES:
                raise ValueError(f"{t['id']}: unknown requires_text rule {rule!r}")
            out.append(Trigger(
                id=t["id"], kind=kind, label=t.get("label") or t["id"],
                description=t.get("description", ""), direct=tuple(ev.get("direct", [])),
                indirect=tuple(ev.get("indirect", [])), requires_text=rule,
                severe=bool(t.get("severe")), observable=t.get("observable", True) is not False,
                max_status=POSSIBLE if t.get("max_status") == POSSIBLE else LIKELY,
                derived_from=tuple(t.get("derived_from", [])), cure=t.get("cure", ""),
                consequence=t.get("consequence", "")))
    return out


def resolve(ids, known) -> list[str]:
    """Trigger ids, with 'PREFIX-*' wildcards, in the order of `known`."""
    out = []
    for k in known:
        if any(k == i or (i.endswith("*") and k.startswith(i[:-1])) for i in ids):
            out.append(k)
    return out


def load_positions(portfolio: dict | None = None) -> list[dict]:
    pf = portfolio or json.loads(PORTFOLIO_PATH.read_text())
    return [p for p in pf["positions"] if p.get("terms")]


def get_position(pid: str, portfolio: dict | None = None) -> dict:
    for p in load_positions(portfolio):
        if p["id"] == pid:
            return p
    raise KeyError(f"no position {pid} with terms in portfolio.json")


# ------------------------------------------------------------------ evidence as of a date
@dataclass
class EventView:
    id: int
    counterparty: str
    headline: str
    tags: set
    severity: int
    verdict: str
    level: str
    best_tier: int | None
    first_seen: str
    primary_tag: str
    sources: list[dict]


def _text(s: dict) -> str:
    return f"{s.get('title') or ''} {s.get('quote') or ''}"


def _headline(srcs: list[dict]) -> str:
    """The Verifier's naming rule, applied only to sources known on the date (no later title)."""
    good = [s for s in srcs if len(s["title"] or "") >= 25 and not s["title"].startswith("Case number")]
    if good:
        s = min(good, key=lambda s: (1.5 if s["tier"] == 0 else s["tier"], s["published_at"] or ""))
        return s["title"]
    s = min(srcs, key=lambda s: s["tier"])
    return f"[{s['domain']}] {(s['quote'] or '')[:120]}"


def events_as_of(conn: sqlite3.Connection, cp: str, as_of) -> list[EventView]:
    """The Verifier's events for `cp` rebuilt from what was known on `as_of`.

    tags/severity: union/max over the event's signals first seen <= as_of.
    sources: scout sources whose signal was seen <= as_of; corroboration sources published
    <= as_of AND found by a verification run on or before as_of. The verdict is re-judged on
    those sources, and the level is capped at what the Verifier had actually raised by then.
    """
    d = _iso(as_of)
    out = []
    for ev in conn.execute("SELECT * FROM events WHERE counterparty_id=? AND first_seen_as_of<=? "
                           "ORDER BY first_seen_as_of, id", (cp, d)).fetchall():
        sigs = conn.execute("SELECT tags, severity FROM signals WHERE event_id=? AND first_seen_as_of<=?",
                            (ev["id"], d)).fetchall()
        if not sigs:
            continue
        found = [r[0] for r in conn.execute(
            "SELECT as_of FROM event_verifications WHERE event_id=? AND n_supporting>0 AND as_of<=?",
            (ev["id"], d))]
        srcs = []
        for r in conn.execute(
                "SELECT es.*, a.title, s.first_seen_as_of AS seen FROM event_sources es "
                "LEFT JOIN articles a ON a.url=es.url "
                "LEFT JOIN signals s ON s.url=es.url AND s.event_id=es.event_id "
                "WHERE es.event_id=?", (ev["id"],)):
            pub = (r["published_at"] or "")[:10]
            if pub and pub > d:
                continue
            if r["role"] == "scout":
                seen = (r["seen"] or pub)[:10]
                if not seen or seen > d:
                    continue
            elif not pub or not any(pub <= f for f in found):
                continue
            srcs.append({"url": r["url"], "domain": r["domain"], "publisher": r["publisher"],
                         "tier": r["tier"], "role": r["role"], "published_at": pub,
                         "merge_basis": r["merge_basis"] if "merge_basis" in r.keys() else r["role"],
                         "title": r["title"] or "", "quote": r["quote"] or ""})
        if not srcs:
            continue
        sev = max(s["severity"] for s in sigs)
        v = verdict_of(srcs)
        level = alert_level(sev, v.verdict)
        if level == "ALERT" and not (ev["alerted_as_of"] and ev["alerted_as_of"] <= d):
            level = "watch"
        if level == "watch" and not (ev["watch_as_of"] and ev["watch_as_of"] <= d):
            level = "log"
        out.append(EventView(ev["id"], cp, _headline(srcs),
                             set().union(*(json.loads(s["tags"]) for s in sigs)), sev, v.verdict,
                             level, v.best_tier, ev["first_seen_as_of"], ev["primary_tag"], srcs))
    return out


# ------------------------------------------------------------------ rules
def match(t: Trigger, ev: EventView) -> tuple[str, str, list[str]] | None:
    """Deterministic rule: (strength, basis, matched tags) or None.

    likely_tripped  a DIRECT tag on a confirmed/corroborated event (+ the text rule if any)
    possible        a direct tag on a weaker (watch-level) event, a direct tag without the
                    required text (e.g. bankruptcy prepared, not yet filed), or an indirect tag
    """
    direct = sorted(ev.tags & set(t.direct))
    if direct:
        if t.requires_text and not any(TEXT_RULES[t.requires_text].search(_text(s)) for s in ev.sources):
            return POSSIBLE, "direct_no_text", direct
        return (LIKELY if ev.verdict in STRONG else POSSIBLE), "direct", direct
    ind = sorted(ev.tags & set(t.indirect))
    return (POSSIBLE, "indirect", ind) if ind else None


def _receipt(ev: EventView, t: Trigger, basis: str, tags: list[str]) -> dict:
    """The source that best evidences THIS trigger: text that matches the trigger's rule first,
    then clean quotes, best tier, open (non-paywalled) pages, earliest."""
    if basis == "direct" and t.requires_text:
        pats = [TEXT_RULES[t.requires_text]]
    else:
        pats = [p for tg in tags for p in TAG_RE.get(tg, [])]

    def key(s):
        hit = any(p.search(_text(s)) for p in pats)
        return (not hit, is_junk(s["quote"]), s["tier"], is_paywalled(s["domain"]), s["published_at"])

    s = min(ev.sources, key=key)
    return {k: s[k] for k in ("publisher", "domain", "tier", "published_at", "title", "url")} | {
        "quote": s["quote"][:300]}


def _evidence(ev: EventView, t: Trigger, strength: str, basis: str, tags: list[str]) -> dict:
    return {"event_id": ev.id, "verdict": ev.verdict, "level": ev.level, "strength": strength,
            "basis": basis, "tags": tags, "first_seen": ev.first_seen, "headline": ev.headline[:160],
            "receipt": _receipt(ev, t, basis, tags)}


def _cite(r: dict) -> str:
    e = r["evidence"][0] if r["evidence"] else None
    return f"{r['id']} {r['status']}" + (f" [ev{e['event_id']} {e['verdict']}]" if e else "")


PREFILING_WINDOW_DAYS = 30
PREFILING_TAGS = {"insolvency", "lender_action", "fraud"}


def _prefiling_backed(r: dict, as_of: str | None) -> bool:
    """Stage 3 needs a corroborated insolvency-type event from a tier<=2 source dated in the last
    ~30 days; stale retrospectives and weak sources cap at stage 2."""
    if as_of is None:
        return True
    lo = (date.fromisoformat(as_of) - timedelta(days=PREFILING_WINDOW_DAYS)).isoformat()
    for e in r["evidence"]:
        rc = e["receipt"]
        if (e["verdict"] in STRONG and rc["tier"] <= 2 and (rc["published_at"] or "")[:10] >= lo
                and set(e["tags"]) & PREFILING_TAGS and "stale_context" not in e["tags"]):
            return True
    return False


def stage_of(rows: list[dict], as_of: str | None = None) -> tuple[int, list[str]]:
    likely = [r for r in rows if r["status"] == LIKELY]
    filed = [r for r in likely if r["requires_text"] == "filing"]
    if filed:
        return 4, [_cite(r) for r in filed]
    severe = [r for r in likely if r["severe"]]
    pending = [r for r in rows if r["status"] == POSSIBLE and r["basis"] == "direct_no_text"
               and r["evidence"][0]["verdict"] in STRONG]
    severe = [r for r in severe if _prefiling_backed(r, as_of)]
    pending = [r for r in pending if _prefiling_backed(r, as_of)]
    if severe or pending:
        return 3, [_cite(r) for r in severe + pending]
    backed = [r for r in rows if r["status"] == POSSIBLE
              and any(e["level"] == "ALERT" for e in r["evidence"])]
    if likely or backed:
        return 2, [_cite(r) for r in likely or backed]
    poss = [r for r in rows if r["status"] == POSSIBLE]
    return (1, [_cite(r) for r in poss]) if poss else (0, [])


# ------------------------------------------------------------------ checklist playbooks
@dataclass(frozen=True)
class Item:
    id: str
    short: str
    why: str
    owner: str
    variants: tuple            # ((min status of `needs`, action text, {stage: urgency}), ...)
    needs: tuple = ()          # trigger ids / 'PREFIX-*'; empty = applies at every active stage
    phase: str = "pre"         # pre: stages 1-3 | post: stage 4 (after the petition)
    counsel: bool = False


def _always(action: str, urg: dict) -> tuple:
    return ((NOT, action, urg),)


POST = {4: HOURS}
COUNSEL_NOW = ("Engage restructuring counsel now, before any filing: the automatic stay (11 U.S.C. 362) "
               "will freeze sweeps, servicer replacement and foreclosure; ask counsel to weigh "
               "preference-period risk (11 U.S.C. 547, 90 days) on sweeps and paydowns.")

PLAYBOOKS: dict[str, list[Item]] = {
    "warehouse": [
        Item("draw_stop", "draw stop",
             "Advances need no Default, true sole-lien/title reps and a current borrowing base; "
             "new money into a deteriorating credit may be stayed or lost.", "deal team / agent",
             _always("Stop new advances (draw stop): hold any borrowing request until the draw "
                     "conditions are re-established in writing.", {1: H24, 2: HOURS, 3: HOURS})),
        Item("reservation_of_rights", "reservation of rights",
             "Dealing on without reserving rights invites waiver/estoppel arguments.", "counsel",
             _always("Send a reservation-of-rights letter and invoke the information and inspection "
                     "rights (no waiver by continued dealing).", {1: H24, 2: HOURS, 3: HOURS}),
             counsel=True),
        Item("interim_bbc", "interim BBC + data tape",
             "Shows whether advances are still covered; the VIN-level tape feeds the double-pledge check.",
             "deal team",
             _always("Demand an interim borrowing base certificate and loan-level data tape (VIN, "
                     "balance, days past due, title status) within 2 business days; re-test eligibility.",
                     {1: H24, 2: HOURS, 3: HOURS})),
        Item("title_custodian", "titles with custodian",
             "Double-pledged collateral only shows up by matching VINs to titles and custodial receipts.",
             "deal team / custodian",
             _always("Verify titles with the Custodian: reconcile the data tape against custodial "
                     "receipts and the exception report; confirm the Lender is lienholder of record "
                     "(double-pledge check).", {1: H24, 2: HOURS, 3: HOURS})),
        Item("notice_of_default", "notice of default",
             "A notice starts cure periods and supports acceleration and termination of the commitment.",
             "counsel",
             ((LIKELY, "Deliver a notice of default for the likely-tripped Events of Default (counsel to "
                       "confirm any cure period has run); consider acceleration and terminating the "
                       "commitment.", {2: H24, 3: HOURS}),),
             needs=("EOD-*", "XDEF"), counsel=True),
        Item("ucc_search", "UCC / lien search",
             "Competing financing statements or other lenders' VIN lists reveal double pledges.",
             "counsel",
             _always("Order UCC and lien searches on the Borrower SPV, Tricolor and the Parent (TX, DE); "
                     "through counsel, compare VIN lists with other warehouse lenders.",
                     {1: WEEK, 2: H24, 3: HOURS}), counsel=True),
        Item("cash_dominion", "cash dominion",
             "Collections are the collateral; after a petition, activating control or sweeping is "
             "stayed. Counsel to weigh preference risk on pre-petition sweeps (lower if fully secured).",
             "deal team / collection bank",
             ((LIKELY, "Activate springing cash dominion: deliver the notice of exclusive control to the "
                       "Collection Bank so the lockbox sweeps daily to the Lender account.",
               {2: H24, 3: HOURS}),
              (POSSIBLE, "Prepare cash dominion: draft the notice of exclusive control and confirm the "
                         "Collection Bank contact, ready once an Event of Default is confirmed.",
               {1: WEEK, 2: H24, 3: HOURS})),
             needs=("CASH-DOM",), counsel=True),
        Item("servicer_backup", "servicer termination / backup",
             "A distressed servicer stops collecting and reporting; a warm backup needs weeks, and "
             "after a filing its activation needs stay relief.", "deal team / backup servicer",
             ((LIKELY, "Servicer termination: deliver the termination notice and activate the backup "
                       "servicer (data transfer, obligor payment redirection, title files).",
               {2: H24, 3: HOURS}),
              (POSSIBLE, "Put the backup servicer on notice: get the latest data file and a 30-day "
                         "transition plan.", {1: WEEK, 2: H24, 3: H24})),
             needs=("STE-*",)),
        Item("field_exam", "field exam",
             "An on-site audit tests what the certificates claim (at the Borrower's cost after a "
             "review event).", "deal team / field examiner",
             _always("Schedule a field exam: sample contract files against originals, test collections "
                     "and the servicing system.", {1: WEEK, 2: H24, 3: H24})),
        Item("co_lenders", "co-lender coordination",
             "Other lenders seizing or disclosing exposure means competing claims to the same contracts.",
             "deal team / counsel",
             _always("Coordinate with co-lenders, other warehouse lenders and securitization trustees: "
                     "share collateral lists through counsel, avoid a race to the same contracts.",
                     {1: WEEK, 2: H24, 3: HOURS})),
        Item("counsel", "engage counsel",
             "Every remedy above is cheaper and safer before the petition than after it.", "counsel",
             _always(COUNSEL_NOW, {1: H24, 2: H24, 3: HOURS}), counsel=True),
        # ---- after the petition (automatic stay)
        Item("stay_compliance", "stop self-help (stay)",
             "Violating the automatic stay risks sanctions and voids the act.", "counsel",
             _always("Stop all self-help: no sweeps, setoff, repossession or servicer replacement "
                     "without stay relief. The commitment terminated on the filing: no further advances.",
                     POST), phase="post", counsel=True),
        Item("custody_hold", "custodian hold",
             "Title and contract files are the proof of the Lender's lien.", "deal team / custodian",
             _always("Instruct the Custodian to hold every title and contract file and release nothing "
                     "without the Lender's consent or a court order.", POST), phase="post"),
        Item("cash_collateral", "cash collateral",
             "Collections are the Lender's cash collateral (11 U.S.C. 363(c)(2)).", "counsel",
             _always("Object to any use of collections without adequate protection (11 U.S.C. 361, 363); "
                     "in a Chapter 7, contact the trustee at once.", POST), phase="post", counsel=True),
        Item("stay_relief", "stay relief",
             "Enforcement and the servicer transfer now need the court.", "counsel",
             _always("Consider a motion for relief from the stay (11 U.S.C. 362(d)) to enforce against "
                     "the collateral and move servicing to the backup servicer.", {4: H24}),
             phase="post", counsel=True),
        Item("double_pledge_reconcile", "double-pledge reconciliation",
             "Perfection and title notation decide who owns each contract.", "deal team / counsel",
             _always("Finish the VIN-level double-pledge reconciliation with the other lenders.",
                     {4: H24}), phase="post"),
        Item("proof_of_claim", "proof of claim",
             "Preserves the claim and the perfection record.", "counsel",
             _always("Prepare the proof of claim with perfection evidence (UCC-1s, title notations, "
                     "custodial receipts).", {4: WEEK}), phase="post", counsel=True),
        Item("preference_review", "preference review",
             "Sweeps and paydowns in the 90 days before the petition can be attacked.", "counsel",
             _always("Counsel: preference review of sweeps and paydowns in the 90 days before the "
                     "petition.", {4: WEEK}), phase="post", counsel=True),
    ],
    "abs_notes": [
        Item("contact_trustee", "contact trustee",
             "A Class B holder acts only through the trustee, which also sees the remittance data first.",
             "deal team",
             _always("Contact the Indenture Trustee: latest servicer report, collection-account balances, "
                     "confirmation that collections are deposited within 2 business days, and whether any "
                     "Servicer Termination Event notice has been given.", {1: H24, 2: HOURS, 3: HOURS})),
        Item("ste_thresholds", "check STE thresholds",
             "Only the Controlling Class can direct; know the votes before you need them.", "deal team",
             ((POSSIBLE, "Check the Servicer Termination Events and who can act: removal needs a majority "
                         "of the Controlling Class (Class A while outstanding); Class B alone cannot "
                         "direct the trustee.", {1: WEEK, 2: H24, 3: HOURS}),),
             needs=("STE-*", "PERF-*")),
        Item("backup_servicer", "backup servicer",
             "Servicer replacement is simplest before a filing makes it need court approval.",
             "deal team / trustee",
             ((LIKELY, "Press the trustee and the Controlling Class to terminate the servicer and activate "
                       "the backup servicer now, before a filing.", {2: H24, 3: HOURS}),
              (POSSIBLE, "Ask the trustee to confirm the backup servicer's readiness and transition "
                         "timeline (data-file test, obligor notices, payment redirection).",
               {1: WEEK, 2: H24, 3: H24})),
             needs=("STE-*",)),
        Item("noteholder_coordination", "noteholder coordination",
             "Direction thresholds need Class A; the trustee wants indemnity before acting.", "deal team",
             _always("Coordinate with Class A (Controlling Class) and other Class B holders to reach the "
                     "direction thresholds and agree the indemnity.", {2: H24, 3: HOURS})),
        Item("exit_handoff", "exit-liquidity hand-off",
             "Class B has few levers; price is the main one left.", "exit-liquidity desk",
             _always("Hand off to the exit-liquidity desk: price discovery and bids for the Class B notes "
                     "before further deterioration (compliance / MNPI check first).",
                     {1: WEEK, 2: H24, 3: HOURS})),
        Item("repurchase_review", "repurchase review",
             "Ineligible receivables must be bought back, but the claim is only as good as the sponsor.",
             "counsel",
             ((POSSIBLE, "Review sponsor repurchase obligations for ineligible (e.g. double-pledged) "
                         "receivables; ask the trustee to put the sponsor on notice.",
               {1: WEEK, 2: WEEK, 3: H24}),),
             needs=("REP-*",), counsel=True),
        Item("counsel", "engage counsel",
             "Sponsor bankruptcy brings true-sale and servicer-replacement fights.", "counsel",
             _always("Engage counsel: true-sale / substantive-consolidation risk if the sponsor files, and "
                     "how to replace a servicer once the automatic stay protects it.",
                     {1: WEEK, 2: H24, 3: HOURS}), counsel=True),
        Item("trustee_plan", "trustee plan (post-petition)",
             "Ipso facto termination is largely unenforceable once the servicer is a debtor.", "counsel",
             _always("Get the trustee's plan: servicer termination now needs stay relief or the debtor's "
                     "consent; press for an interim servicing order.", POST), phase="post", counsel=True),
        Item("collections_segregation", "segregate collections",
             "Trust collections held by the servicer can be commingled with the estate.", "deal team / trustee",
             _always("Confirm trust collections are segregated and not swept into the debtor's estate; "
                     "demand turnover of commingled collections.", POST), phase="post"),
        Item("exit_post", "exit-liquidity re-price",
             "This sponsor's ABS trades at distressed levels after the filing.", "exit-liquidity desk",
             _always("Exit-liquidity desk: re-price and decide hold vs sell.", {4: H24}), phase="post"),
        Item("cnl_monitor", "CNL trigger watch",
             "A net loss trigger turns the waterfall sequential and defers Class B principal.", "deal team",
             _always("Track the cumulative net loss trigger and Class B principal deferral in each "
                     "servicer report.", {4: WEEK}), phase="post"),
        Item("repurchase_claim", "repurchase claim",
             "Repurchase and indemnity claims become claims against the estate.", "counsel",
             _always("Counsel: repurchase / indemnity claims against the sponsor's estate (proof of claim).",
                     {4: WEEK}), phase="post", counsel=True),
    ],
    "receivables_purchase": [
        Item("stop_purchases", "stop purchases",
             "Each new purchase adds invoices that may be double-factored or already paid.", "deal team",
             _always("Stop purchases: decline new purchase requests (purchases are discretionary and need "
                     "no Review or Termination Event).", {1: H24, 2: HOURS, 3: HOURS})),
        Item("verify_invoices", "verify invoices with account debtors",
             "Double-factoring check: only the account debtor can confirm what it owes and to whom.",
             "deal team",
             _always("Verify invoices directly with account debtors: existence, amount, due date, no other "
                     "financier's notice of assignment, still unpaid; largest balances first.",
                     {1: H24, 2: HOURS, 3: HOURS})),
        Item("financing_schedule", "schedule of all financings",
             "Off-balance-sheet factoring elsewhere is where double-sold invoices hide.", "deal team",
             ((LIKELY, "Demand the Review Event deliverables within 3 business days: a schedule of ALL "
                       "receivables, supply-chain and off-balance-sheet financings plus invoice-level data.",
               {2: HOURS, 3: HOURS}),
              (POSSIBLE, "Request a schedule of all other receivables financings and invoice-level data.",
               {1: H24, 2: H24, 3: HOURS})),
             needs=("REV-*",)),
        Item("reservation_of_rights", "reservation of rights",
             "Dealing on without reserving rights invites waiver arguments.", "counsel",
             _always("Send a reservation-of-rights letter; a notice of Termination Event only for "
                     "likely-tripped events.", {1: H24, 2: HOURS, 3: HOURS}), counsel=True),
        Item("ucc_search", "UCC search",
             "Other financing statements covering accounts mean someone else may own the same invoices.",
             "counsel",
             _always("UCC search on the Seller and each selling subsidiary for other financing statements "
                     "covering accounts; through counsel, compare invoice numbers with other financiers.",
                     {1: WEEK, 2: H24, 3: HOURS}), counsel=True),
        Item("daily_remittance", "daily remittance",
             "Collections missing for invoices the debtor says it paid point to diversion or a second buyer.",
             "deal team",
             _always("Move servicer remittance from weekly to daily and reconcile collections against "
                     "purchased invoices.", {1: WEEK, 2: H24, 3: HOURS})),
        Item("redirect_collections", "redirect collections",
             "After notice, paying the Seller no longer discharges the account debtor (UCC 9-406); "
             "after a petition, sending notices may be stayed.", "deal team / counsel",
             ((LIKELY, "Redirect collections: activate the DACA and send the notices of assignment so "
                       "account debtors pay the Purchaser directly.", {2: H24, 3: HOURS}),
              (POSSIBLE, "Prepare to redirect collections (notices of assignment, DACA activation); weigh "
                         "with counsel the signal to customers, which can hasten the Seller's failure.",
               {1: WEEK, 2: H24, 3: H24})),
             needs=("CASH-DOM",), counsel=True),
        Item("withhold_dpp", "withhold deferred price",
             "The deferred purchase price is the Purchaser's cushion against dilution and repurchases.",
             "deal team",
             ((POSSIBLE, "Withhold the deferred purchase price and net it against dilution and "
                         "repurchase claims while the review runs.", {2: H24, 3: HOURS}),),
             needs=("TE-*", "REV-*")),
        Item("servicer_replacement", "servicer replacement",
             "A Seller that stops remitting or reporting cannot be left holding collections.",
             "deal team",
             ((LIKELY, "Terminate the Seller as servicer and appoint the backup collection agent.",
               {2: H24, 3: HOURS}),
              (POSSIBLE, "Line up a backup servicer / collection agent to take over collections.",
               {1: WEEK, 2: WEEK, 3: H24})),
             needs=("TE-SERV", "TE-INS")),
        Item("co_financiers", "co-financier coordination",
             "Competing claims to the same invoices are settled by priority, then by negotiation.",
             "deal team / counsel",
             _always("Coordinate with other receivables financiers and the Seller's ABL / term lenders "
                     "(competing claims, intercreditor position).", {2: WEEK, 3: H24})),
        Item("counsel", "engage counsel",
             "True-sale and priority questions decide who owns the invoices in a bankruptcy.", "counsel",
             _always("Engage counsel now: true-sale / recharacterization risk, priority if invoices were sold "
                     "twice (first UCC-1 filed), preference exposure on redirected collections, and what "
                     "the automatic stay will freeze.", {1: WEEK, 2: H24, 3: HOURS}), counsel=True),
        Item("stay_compliance", "stop self-help (stay)",
             "The debtor will argue the purchased invoices are estate property.", "counsel",
             _always("Stop self-help: no new notices to account debtors, sweeps or setoff without "
                     "counsel.", POST), phase="post", counsel=True),
        Item("assert_ownership", "assert ownership",
             "A true sale keeps the invoices out of the estate, if it holds.", "counsel",
             _always("Assert ownership of the purchased invoices; demand that collections held by the "
                     "debtor be segregated and turned over; oppose their use as cash collateral.", POST),
             phase="post", counsel=True),
        Item("verification_finish", "finish verification",
             "The evidence for any priority fight with other financiers.", "deal team",
             _always("Finish the invoice verification and double-factoring reconciliation.", {4: H24}),
             phase="post"),
        Item("adequate_protection", "adequate protection",
             "If the sale is recharacterized as a loan, the Purchaser is a secured creditor.", "counsel",
             _always("If recharacterized as a loan: seek adequate protection and oppose cash-collateral use "
                     "at the first-day hearings.", {4: H24}), phase="post", counsel=True),
        Item("proof_of_claim", "proof of claim",
             "Recourse, dilution and repurchase claims are claims against the estate.", "counsel",
             _always("Proof of claim for recourse, dilution and repurchase claims; preserve UCC and "
                     "verification records.", {4: WEEK}), phase="post", counsel=True),
        Item("preference_review", "preference review",
             "Collections in the 90 days before the petition can be attacked.", "counsel",
             _always("Counsel: preference review of collections received in the 90 days before the "
                     "petition.", {4: WEEK}), phase="post", counsel=True),
    ],
}


def checklist(playbook: str | None, rows: dict[str, dict], stage: int, stage_why: list[str]) -> list[dict]:
    """Ordered actions: urgency first, then the playbook's own order."""
    if stage == 0:
        return []
    out = []
    for order, it in enumerate(PLAYBOOKS.get(playbook or "", [])):
        if (it.phase == "post") != (stage == 4):
            continue
        gate = [rows[i] for i in resolve(it.needs, rows)]
        best = max((STATUS_RANK[r["status"]] for r in gate), default=0)
        for min_status, action, urg in it.variants:
            if stage not in urg or (it.needs and best < max(1, STATUS_RANK[min_status])):
                continue
            because = [_cite(r) for r in gate if r["status"] != NOT][:3] if it.needs else stage_why[:2]
            out.append({"id": it.id, "short": it.short, "urgency": urg[stage], "action": action,
                        "why": it.why, "because": because, "owner": it.owner, "counsel": it.counsel,
                        "_order": order})
            break
    out.sort(key=lambda c: (URGENCY_RANK[c["urgency"]], c["_order"]))
    for n, c in enumerate(out, 1):
        del c["_order"]
        c["n"] = n
    return out


# ------------------------------------------------------------------ assessment
def assess(conn: sqlite3.Connection, pos: dict, as_of, judge=None) -> dict:
    d = _iso(as_of)
    terms = pos["terms"]
    trig = triggers(terms)
    seen = [e for c in pos["counterparties"] for e in events_as_of(conn, c["id"], d)]
    evs = [e for e in seen if e.level in ("watch", "ALERT")]     # log-level noise never counts
    rows: dict[str, dict] = {}
    for t in trig:
        base = {"id": t.id, "kind": t.kind, "label": t.label, "description": t.description,
                "cure": t.cure, "consequence": t.consequence, "severe": t.severe,
                "observable": t.observable, "requires_text": t.requires_text,
                "capped": t.max_status != LIKELY}
        if t.derived_from:
            rows[t.id] = base
            continue
        hits = [(e, *m) for e in evs if (m := match(t, e))]
        # strongest first; ties go to the event whose own headline claim is the matched tag
        hits.sort(key=lambda h: (-STATUS_RANK[h[1]], BASIS_RANK[h[2]], -VERDICT_RANK.get(h[0].verdict, 0),
                                 h[0].level != "ALERT", h[0].primary_tag not in h[3],
                                 h[0].first_seen, h[0].id))
        status = hits[0][1] if hits else NOT
        if STATUS_RANK[status] > STATUS_RANK[t.max_status]:
            status = t.max_status
        rows[t.id] = base | {"status": status, "basis": hits[0][2] if hits else None,
                             "evidence": [_evidence(e, t, st, b, tg) for e, st, b, tg in hits[:3]]}
    for t in trig:                                   # derived triggers (e.g. springing cash dominion)
        if not t.derived_from:
            continue
        src = [rows[i] for i in resolve(t.derived_from, rows) if "status" in rows[i]]
        top = max(src, key=lambda r: STATUS_RANK[r["status"]], default=None)
        status = top["status"] if top else NOT
        rows[t.id] |= {"status": status, "basis": "derived" if status != NOT else None,
                       "derived_from": [r["id"] for r in src if r["status"] == status != NOT],
                       "evidence": top["evidence"][:1] if status != NOT else []}
    ordered = [rows[t.id] for t in trig]
    stage, why = stage_of(ordered, d)
    a = {"position": {k: pos.get(k) for k in ("id", "name", "facility_type", "commitment_usd",
                                               "collateral", "counterparties")},
         "as_of": d, "playbook": terms.get("playbook"), "stage": stage, "stage_label": STAGES[stage],
         "stage_why": why[:4],
         "events_used": [{"event_id": e.id, "level": e.level, "verdict": e.verdict,
                          "tags": sorted(e.tags), "headline": e.headline[:120]} for e in evs],
         "n_events_seen": len(seen),
         "triggers": ordered, "checklist": checklist(terms.get("playbook"), rows, stage, why),
         "terms_note": terms.get("_hypothetical", "hypothetical terms"),
         "preference_note": (terms.get("cash_management") or {}).get("preference_consideration"),
         "disclaimer": DISCLAIMER}
    a["digest"] = digest(a)
    if judge is not None:
        a["draft"] = draft_note(judge, a)
    return a


def digest(a: dict) -> str:
    key = {"s": a["stage"], "t": [(r["id"], r["status"]) for r in a["triggers"]],
           "c": [(c["id"], c["urgency"]) for c in a["checklist"]]}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def diff(prev: dict | None, cur: dict) -> list[str]:
    """Human-readable escalation since the previous assessment of the same position."""
    out = []
    if prev is None or prev["stage"] != cur["stage"]:
        out.append(f"stage {prev['stage'] if prev else '-'} -> {cur['stage']} ({cur['stage_label']})")
    before = {r["id"]: r for r in prev["triggers"]} if prev else {}
    for r in cur["triggers"]:
        old = before.get(r["id"])
        e = r["evidence"][0] if r["evidence"] else None
        cite = f" [ev{e['event_id']} {e['verdict']}]" if e else ""
        if (old["status"] if old else NOT) != r["status"]:
            out.append(f"{r['id']}: {old['status'] if old else NOT} -> {r['status']}{cite}")
        elif (e and old and old["evidence"] and r["basis"] in BASIS_RANK and old["basis"] in BASIS_RANK
              and BASIS_RANK[r["basis"]] < BASIS_RANK[old["basis"]]):
            note = " (reported, not yet filed)" if r["basis"] == "direct_no_text" else ""
            out.append(f"{r['id']}: {r['status']}, stronger evidence: {r['basis']}{note}{cite}")
    urg = {c["id"]: c["urgency"] for c in prev["checklist"]} if prev else {}
    if cur["checklist"] and not urg:
        out.append(f"checklist opened: {len(cur['checklist'])} actions")
        return out
    for c in cur["checklist"]:
        if urg.get(c["id"]) != c["urgency"]:
            out.append(f"{c['short']}: {urg.get(c['id'], 'new')} -> {c['urgency']}")
    return out


def timeline(conn: sqlite3.Connection, pos: dict, start, end) -> list[dict]:
    s, e = date.fromisoformat(_iso(start)), date.fromisoformat(_iso(end))
    out, prev = [], None
    for n in range((e - s).days + 1):
        a = assess(conn, pos, s + timedelta(n))
        a["changes"] = diff(prev, a)
        out.append(a)
        prev = a
    return out


def draft_note(judge, a: dict) -> str | None:
    """Optional local-LLM wording for the deal team. It gets the finished, rule-based result and
    only returns prose; nothing it says is read back into a status or the checklist. A draft that
    names a trigger the rules did not flag is dropped."""
    likely = [r for r in a["triggers"] if r["status"] == LIKELY]
    poss = [r for r in a["triggers"] if r["status"] == POSSIBLE]
    first = [f"{c['short']} ({c['urgency']})" for c in a["checklist"][:3]]
    system = ("You draft wording for a private-credit deal team. Write ONE plain sentence of at most 200 "
              "characters from the facts. Say 'likely tripped' for likely-tripped items. Do not add facts, "
              "do not soften or upgrade any status, no legal advice. Reply as JSON with fields answer "
              "(true) and reason (the sentence).")
    user = (f"Position: {a['position']['name']}\nAs of: {a['as_of']}\nStage: {a['stage_label']}\n"
            f"Likely tripped: {', '.join(r['label'] for r in likely) or 'none'}\n"
            f"Possible: {len(poss)} triggers\nFirst actions: {'; '.join(first) or 'monitor'}")
    try:
        res = judge._ask("covenant_draft", system, user, f"{a['position']['id']} {a['as_of']}", a["as_of"])
    except Exception as e:  # the draft is optional; never let it break an assessment
        log.warning("covenant draft failed: %s", e)
        return None
    if not res or not res[1]:
        return None
    text = res[1].strip()
    flagged = {r["id"] for r in likely + poss}
    if any(t["id"] in text and t["id"] not in flagged for t in a["triggers"]):
        return None
    if len(text) >= 238 and "." in text[:-1]:      # llm.py caps replies at 240 chars
        text = text[:text.rindex(".", 0, len(text) - 1) + 1]
    return text


# ------------------------------------------------------------------ storage / bus
def save(conn: sqlite3.Connection, a: dict, trigger: str = "cli") -> int:
    conn.executescript(SCHEMA)
    pid = a["position"]["id"]
    with conn:
        old = conn.execute("SELECT id FROM covenant_assessments WHERE position_id=? AND as_of=? AND trigger=?",
                           (pid, a["as_of"], trigger)).fetchone()
        if old:
            conn.execute("DELETE FROM covenant_trigger_status WHERE assessment_id=?", (old[0],))
            conn.execute("DELETE FROM covenant_assessments WHERE id=?", (old[0],))
        cur = conn.execute(
            "INSERT INTO covenant_assessments (position_id, as_of, trigger, stage, n_likely, n_possible, "
            "digest, payload) VALUES (?,?,?,?,?,?,?,?)",
            (pid, a["as_of"], trigger, a["stage"],
             sum(r["status"] == LIKELY for r in a["triggers"]),
             sum(r["status"] == POSSIBLE for r in a["triggers"]), a["digest"],
             json.dumps(a, default=str)))
        aid = cur.lastrowid
        for r in a["triggers"]:
            e = r["evidence"][0] if r["evidence"] else {}
            rc = e.get("receipt") or {}
            conn.execute("INSERT INTO covenant_trigger_status VALUES (?,?,?,?,?,?,?,?,?)",
                         (aid, r["id"], r["kind"], r["status"], r["basis"], e.get("event_id"),
                          e.get("verdict"), rc.get("url"), rc.get("quote")))
    return aid


def _u(urgency: str) -> str:
    return {HOURS: "hours", H24: "24h", WEEK: "week"}.get(urgency, urgency)


def summary(a: dict) -> str:
    lk = [r["id"] for r in a["triggers"] if r["status"] == LIKELY]
    ps = [r["id"] for r in a["triggers"] if r["status"] == POSSIBLE]
    first = [f"{c['short']} ({_u(c['urgency'])})" for c in a["checklist"][:3]]
    return (f"COVENANT {a['position']['id']} stage {a['stage']} ({a['stage_label']}): likely "
            f"{', '.join(lk) or 'none'}; possible {len(ps)}; first: {'; '.join(first) or 'monitor'} "
            f"(considerations, not legal advice)")


def compact(a: dict, assessment_id: int, alert_ids: list) -> dict:
    """Bus message: small enough for the board mirror, with the strongest receipt."""
    strongest = next((r for r in sorted(a["triggers"], key=lambda r: -STATUS_RANK[r["status"]])
                      if r["evidence"]), None)
    rc = strongest["evidence"][0]["receipt"] if strongest else {}
    return {
        "kind": "covenant_assessment", "assessment_id": assessment_id,
        "position": a["position"]["id"],
        "counterparty": ",".join(c["id"] for c in a["position"]["counterparties"]),
        "as_of": a["as_of"], "alert_event_ids": alert_ids, "stage": a["stage"],
        "stage_label": a["stage_label"],
        "likely_tripped": [{"id": r["id"], "event_id": r["evidence"][0]["event_id"],
                            "verdict": r["evidence"][0]["verdict"]}
                           for r in a["triggers"] if r["status"] == LIKELY],
        "possible": [r["id"] for r in a["triggers"] if r["status"] == POSSIBLE],
        "changes": a.get("changes", [])[:4],
        "checklist": [{"n": c["n"], "urgency": _u(c["urgency"]), "id": c["id"]} for c in a["checklist"][:5]],
        "receipt_url": rc.get("url"), "quote": (rc.get("quote") or "")[:200],
        "draft": a.get("draft"),
        "disclaimer": "considerations for the deal team and counsel, not legal advice; terms hypothetical",
        "summary": summary(a),
    }


class CovenantDesk:
    """Bus agent: `alerts` (and later `events` changes) in; `covenant_assessments` rows and
    `actions` messages out (one per position per day at most, id action-covenant-<pos>-<date>).

    An ALERT always produces a message for every exposed position. A non-alert event change
    produces one only if the position's statuses, stage or checklist urgencies changed."""
    name = "covenant"

    def __init__(self, conn: sqlite3.Connection, bus=None, judge=None, portfolio: dict | None = None):
        self.conn = conn
        self.bus = bus
        self.judge = judge
        self.positions = load_positions(portfolio)
        self._last: dict[str, dict] = {}       # position id -> last published assessment
        self.errors: list[str] = []
        conn.executescript(SCHEMA)
        if bus is not None:
            bus.subscribe(self.name, ["alerts", "events"])

    def reset(self) -> None:
        """Drop bus-triggered assessments (CLI ones stay); called when a replay clears the bus."""
        with self.conn:
            self.conn.execute("DELETE FROM covenant_trigger_status WHERE assessment_id IN "
                              "(SELECT id FROM covenant_assessments WHERE trigger != 'cli')")
            self.conn.execute("DELETE FROM covenant_assessments WHERE trigger != 'cli'")
        self._last.clear()

    def step(self, as_of) -> list[dict]:
        if self.bus is None:
            return []
        d = _iso(as_of)
        alerts: dict[str, list] = {}
        touched: set[str] = set()
        for m in self.bus.poll(self.name):
            cp = m["payload"].get("counterparty")
            if not cp:
                continue
            touched.add(cp)
            if m["topic"] == "alerts":
                alerts.setdefault(cp, []).append(m["payload"].get("event_id"))
        out = []
        for pos in self.positions:
            cps = {c["id"] for c in pos["counterparties"]}
            if not cps & touched:
                continue
            try:
                msg = self.handle(pos, d, [e for cp in sorted(cps) for e in alerts.get(cp, [])])
            except Exception as e:  # a bad position or data must never stop the team
                self.errors.append(f"{pos['id']} {d}: {type(e).__name__}: {e}")
                log.warning("covenant desk could not assess %s on %s: %s", pos["id"], d, e)
                continue
            if msg:
                out.append(msg)
        return out

    def handle(self, pos: dict, d: str, alert_ids: list) -> dict | None:
        a = assess(self.conn, pos, d)
        prev = self._last.get(pos["id"])
        if not alert_ids and (a["stage"] == 0 or (prev is not None and prev["digest"] == a["digest"])):
            return None
        a["changes"] = diff(prev, a)
        if self.judge is not None:
            a["draft"] = draft_note(self.judge, a)
        mid = f"action-covenant-{pos['id']}-{d}"
        msg = compact(a, save(self.conn, a, mid), alert_ids)
        if self.bus is not None:
            self.bus.publish("actions", mid, msg, self.name)
        self._last[pos["id"]] = a
        return msg
