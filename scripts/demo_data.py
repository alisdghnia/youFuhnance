"""Shared, read-only replay logic for the brief and the dashboard (strictly as-of: nothing dated after the chosen day)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from youfin import market as M  # noqa: E402
from youfin.agents import market as D  # noqa: E402
from youfin.sources import TIER_NAMES  # noqa: E402

DEFAULT_DB = ROOT / "data" / "youfin.db"
START, END = "2025-08-25", "2025-09-25"
FILINGS = {"tricolor": "2025-09-10", "first_brands": "2025-09-25"}   # Chapter 7 / Chapter 11
DISCLAIMER = "Analysis for the deal team — not investment or legal advice."
LEVEL_RANK = {"log": 0, "watch": 1, "ALERT": 2}


def open_db(path=None) -> sqlite3.Connection:
    """Work on a temp copy so nothing (price cache, migrations) can ever touch the real DB."""
    src = Path(path or DEFAULT_DB)
    if not src.exists():
        sys.exit(f"database not found: {src} (pass --db)")
    tmp = Path(tempfile.mkdtemp()) / "youfin.db"
    shutil.copy(src, tmp)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    return conn


def has_market(conn) -> bool:
    try:
        return conn.execute("SELECT COUNT(*) FROM market_prices").fetchone()[0] > 0
    except sqlite3.Error:
        return False


def daterange(a: str, b: str):
    d, e = date.fromisoformat(a), date.fromisoformat(b)
    while d <= e:
        yield d.isoformat()
        d += timedelta(days=1)


def counterparties(conn):
    return [dict(r) for r in conn.execute("SELECT id, name, role, sector FROM counterparties ORDER BY name")]


def positions(conn, cp):
    return [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, p.facility_type, p.commitment_usd, pc.relationship FROM positions p "
        "JOIN position_counterparties pc ON pc.position_id=p.id WHERE pc.counterparty_id=? ORDER BY p.id", (cp,))]


def level_at(ev, d):
    if ev["alerted_as_of"] and ev["alerted_as_of"] <= d:
        return "ALERT"
    if ev["watch_as_of"] and ev["watch_as_of"] <= d:
        return "watch"
    return "log"


def events_at(conn, cp, d):
    """Events visible on day d, with level/verdict/receipts as they stood on d."""
    out = []
    for r in conn.execute("SELECT * FROM events WHERE counterparty_id=? ORDER BY id", (cp,)):
        ev = dict(r)
        seen = min(x for x in (ev["first_seen_as_of"], ev["watch_as_of"], ev["alerted_as_of"]) if x)
        if seen > d:
            continue
        v = conn.execute("SELECT verdict_after FROM event_verifications WHERE event_id=? AND as_of<=? "
                         "ORDER BY as_of DESC, id DESC LIMIT 1", (ev["id"], d)).fetchone()
        srcs = [dict(s) for s in conn.execute(
            "SELECT domain, publisher, tier, role, published_at, quote, url FROM event_sources WHERE event_id=? "
            "AND (published_at IS NULL OR substr(published_at,1,10)<=?) ORDER BY tier, published_at", (ev["id"], d))]
        for s in srcs:
            s["tier_name"] = TIER_NAMES.get(s["tier"], "?")
        lvl = level_at(ev, d)
        out.append({"id": ev["id"], "headline": ev["headline"], "tag": ev["primary_tag"], "severity": ev["severity"],
                    "level": lvl, "verdict": v[0] if v else ev["verdict"],
                    "watch_as_of": ev["watch_as_of"], "alerted_as_of": ev["alerted_as_of"],
                    "new": ("ALERT" if ev["alerted_as_of"] == d else "watch" if ev["watch_as_of"] == d and lvl == "watch" else None),
                    "escalated": bool(ev["alerted_as_of"] == d and ev["watch_as_of"] and ev["watch_as_of"] < d),
                    "sources": srcs})
    return out


def best_receipt(ev):
    srcs = [s for s in ev["sources"] if s.get("quote")]
    return srcs[0] if srcs else None


_MARKET_CACHE: dict = {}


def market_at(conn, cp, d):
    key = (cp, d)
    if key not in _MARKET_CACHE:
        _MARKET_CACHE[key] = _market_at(conn, cp, d)
    return _MARKET_CACHE[key]


def _market_at(conn, cp, d):
    if not has_market(conn):
        return None
    try:
        a = D.assess(conn, cp, d)
    except Exception as e:  # no bars etc.
        return {"error": str(e)}
    return {"window": a["window"], "drivers": a["window_drivers"], "bar_date": a["bar_date"],
            "p_stress": a["basket"]["p_stress"], "warnings": a["data_warnings"],
            "moving": [{"ticker": r["ticker"], "label": r["label"], "car5_z": r.get("car5_z")}
                       for r in a["proxies"] if r["label"] not in ("open", "n/a")],
            "hedges": [{"ticker": h["ticker"], "role": h["role"], "score": h["score"], "label": h["label"],
                        "adv_musd": h["adv60_musd"], "caveat": h["caveat"]} for h in a["hedges"][:5]]}


def actions_at(conn, cp, d):
    """Non-market `actions` messages (covenant desk, anything new): today's and the standing latest per key."""
    rows = []
    for r in conn.execute("SELECT id, sender, payload FROM bus_messages WHERE topic='actions' AND sender!='market' ORDER BY id"):
        try:
            p = json.loads(r["payload"])
        except ValueError:
            continue
        if p.get("counterparty") == cp and (p.get("as_of") or "9") <= d:
            rows.append({"sender": r["sender"], "as_of": p.get("as_of"), "key": p.get("position") or p.get("kind") or r["id"],
                         "summary": p.get("summary") or p.get("kind") or r["id"], "stage": p.get("stage_label"),
                         "url": p.get("receipt_url"), "quote": p.get("quote")})
    latest = {}
    for x in rows:
        latest[(x["sender"], x["key"])] = x
    out = sorted(latest.values(), key=lambda x: (x["as_of"], x["key"]))
    for x in out:
        x["today"] = x["as_of"] == d
    return out


def feed_at(conn, cp, d):
    """Agent-message feed for day d: who -> topic (signals collapsed unless severity >= 3)."""
    msgs, nsig = [], 0
    for r in conn.execute("SELECT id, topic, sender, payload FROM bus_messages ORDER BY created_at, rowid"):
        try:
            p = json.loads(r["payload"])
        except ValueError:
            continue
        if p.get("as_of") != d or (r["topic"] != "brief" and p.get("counterparty") != cp):
            continue
        if r["topic"] == "signals":
            nsig += 1
            if (p.get("severity") or 0) < 3:
                continue
        msgs.append({"sender": r["sender"], "topic": r["topic"], "text": p.get("summary") or r["id"]})
    if nsig:
        msgs.insert(0, {"sender": "scout", "topic": "signals", "text": f"{nsig} signals for this counterparty (severity >= 3 listed below)"})
    return msgs


def exposure(conn, cp):
    ps = positions(conn, cp)
    return ps, sum(p["commitment_usd"] or 0 for p in ps)


def money(x):
    return f"${x / 1e6:,.0f}M"


def render_cp(conn, cp, name, d, market=True):
    """Markdown block for one counterparty on day d."""
    evs = events_at(conn, cp, d)
    top = max((LEVEL_RANK[e["level"]] for e in evs), default=-1)
    status = {2: "ALERT", 1: "WATCH", 0: "no verified alert (log only)", -1: "nothing seen yet"}[top]
    L = [f"## {name}  —  status: {status}", ""]
    new = [e for e in evs if e["new"]]
    standing = [e for e in evs if e["level"] != "log" and not e["new"]]
    for title, group in (("New / escalated today", new), ("Standing", standing)):
        if not group:
            continue
        L.append(f"**{title}**")
        for e in group:
            tag = ("ESCALATED to ALERT" if e["escalated"] else f"NEW {e['new']}") if e["new"] else e["level"]
            L.append(f"- [{tag}] **{e['verdict']}** · sev {e['severity']} · {e['tag']} — {e['headline']}")
            r = best_receipt(e)
            if r and (e["new"] or e["level"] == "ALERT"):
                L.append(f"  > \"{r['quote'].strip()[:260]}\"")
                L.append(f"  > receipt: {r['domain']} (tier {r['tier']}, {r['tier_name']}), {str(r['published_at'] or '')[:10]} — {r['url']}")
        L.append("")
    if not new and not standing:
        L += ["No new or standing verified alerts.", ""]
    if top >= 1:
        ps, tot = exposure(conn, cp)
        L.append(f"**Affected positions** (hypothetical): total {money(tot)}")
        L += [f"- {p['id']} {p['name']} — {money(p['commitment_usd'] or 0)} ({p['facility_type']}; {p['relationship']})" for p in ps]
        L.append("")
        m = market_at(conn, cp, d) if market else None
        if m and "window" in m:
            ps_txt = "n/a" if m["p_stress"] is None else f"{m['p_stress']:.2f}"
            L.append(f"**Market window: {m['window'].upper()}** (last close used {m['bar_date']}; basket P(stress) {ps_txt}) — {'; '.join(m['drivers'])}")
            for i, h in enumerate(m["hedges"][:3], 1):
                L.append(f"- hedge candidate {i}: {h['ticker']} ({h['role']}, score {h['score']:.2f}, {h['label']}) — {h['caveat']}")
            L.append("")
        elif market:
            L += ["**Market window:** n/a (no price cache in this database)", ""]
    acts = [a for a in actions_at(conn, cp, d)]
    if acts:
        L.append("**Desk actions** (from `actions` topic)")
        for a in acts:
            L.append(f"- {'NEW ' if a['today'] else ''}[{a['sender']}] {a['summary']}")
        L.append("")
    return "\n".join(L)


def render_brief(conn, d, cps=None, market=True):
    allc = counterparties(conn)
    sel = [c for c in allc if not cps or c["id"] in cps]
    parts = [f"# ACTION BRIEF — {d}", f"_Strictly as-of {d}: only data dated on or before this day._", ""]
    parts += [render_cp(conn, c["id"], c["name"], d, market) for c in sel]
    parts += ["---", f"_{DISCLAIMER}_", ""]
    return "\n".join(parts)


def study_marks(conn, cp):
    """First watch/alert from the events table; market break + lead times from the market study."""
    r = conn.execute("SELECT MIN(watch_as_of), MIN(alerted_as_of) FROM events WHERE counterparty_id=?", (cp,)).fetchone()
    out = {"first_watch": r[0], "first_alert": r[1], "filing": FILINGS.get(cp), "market_break": None,
           "lead_sessions": None, "lead_days_to_filing": None, "break_ticker": None}
    if r[1] and FILINGS.get(cp):
        out["lead_days_to_filing"] = (date.fromisoformat(FILINGS[cp]) - date.fromisoformat(r[1])).days
    if not has_market(conn) or not r[1]:
        return out
    prows, orows, pn = D.study(conn, cp, START, "2025-11-14")
    fb = D.first_breaks(prows, orows)
    direct = {t: b["bocpd_break"] for t, b in fb.items()
              if b.get("role") in ("exposed_lender", "peer", "customer") and b.get("bocpd_break") is not None}
    days = M.trading_days(pn.prices)
    if direct:
        t = min(direct, key=lambda k: direct[k])
        out["break_ticker"], out["market_break"] = t, direct[t].date().isoformat()
        out["lead_sessions"] = D.lead_sessions(days, r[1], direct[t])
    elif fb.get("BASKET", {}).get("ms_break") is not None:   # no single-proxy BOCPD break: basket regime break
        b = fb["BASKET"]["ms_break"]
        out["break_ticker"], out["market_break"] = "basket", b.date().isoformat()
        out["lead_sessions"] = D.lead_sessions(days, r[1], b)
    return out
