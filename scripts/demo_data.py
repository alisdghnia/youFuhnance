"""Shared, read-only replay logic for the brief and the dashboard (strictly as-of: nothing dated after the chosen day)."""
from __future__ import annotations

import json
import re
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
from youfin.agents import covenant as C  # noqa: E402
from youfin.sources import TIER_NAMES  # noqa: E402

DEFAULT_DB = ROOT / "data" / "youfin.db"
START, END = "2025-08-25", "2025-09-25"
LIVE = {"carvana": ("2026-10-15", "2026-11-14")}   # live counterparties: outcome unknown, no filing/hindsight marks
FILINGS = {"tricolor": "2025-09-10", "first_brands": "2025-09-25"}   # Chapter 7 / Chapter 11
DISCLAIMER = "Analysis for the deal team — not investment or legal advice."
LEVEL_RANK = {"log": 0, "watch": 1, "ALERT": 2}


EXTEND = {"first_brands": "2025-10-15"}   # slider runs past the 09-25 filing marker (basket stress crossing, JEF close)


def cp_range(cp):
    return LIVE.get(cp, (START, EXTEND.get(cp, END)))


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
    """Events visible on day d, rebuilt AS OF d: verdict/severity/headline/level are re-judged by the
    Verifier's own judge()/alert_level() (via covenant.events_as_of) on sources and signals dated <= d,
    never read from the stored (final) columns. Alert/watch stamps are shown only if <= d."""
    out = []
    stamps = {r["id"]: r for r in conn.execute("SELECT id, watch_as_of, alerted_as_of FROM events WHERE counterparty_id=?", (cp,))}
    for ev in C.events_as_of(conn, cp, d):
        st = stamps[ev.id]
        watch = st["watch_as_of"] if st["watch_as_of"] and st["watch_as_of"] <= d else None
        alert = st["alerted_as_of"] if st["alerted_as_of"] and st["alerted_as_of"] <= d else None
        srcs = sorted(({k: s.get(k) for k in ("domain", "publisher", "tier", "role", "published_at", "quote", "url")} for s in ev.sources),
                      key=lambda s: (s["tier"], s["published_at"] or ""))
        for s in srcs:
            s["tier_name"] = TIER_NAMES.get(s["tier"], "?")
        lvl = ev.level
        out.append({"id": ev.id, "headline": ev.headline, "tag": ev.primary_tag, "severity": ev.severity,
                    "level": lvl, "verdict": ev.verdict, "watch_as_of": watch, "alerted_as_of": alert,
                    "new": ("ALERT" if alert == d and lvl == "ALERT" else "watch" if watch == d and lvl == "watch" else None),
                    "escalated": bool(alert == d and lvl == "ALERT" and watch and watch < d),
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
        if r["sender"] == "summarizer" and p.get("counterparty") != cp:
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


def render_cp(conn, cp, name, d, market=True, summary=None):
    """Markdown block for one counterparty on day d."""
    evs = events_at(conn, cp, d)
    top = max((LEVEL_RANK[e["level"]] for e in evs), default=-1)
    status = {2: "ALERT", 1: "WATCH", 0: "no verified alert (log only)", -1: "nothing seen yet"}[top]
    L = [f"## {name}  —  status: {status}", ""]
    if summary:
        L += render_summary(summary)
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
    parts += [render_cp(conn, c["id"], c["name"], d, market, summary_for(conn, c["id"], c["name"], d) if market else None) for c in sel]
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


# ------------------------------------------------------------------ live-replay market series + team panel
def _f(x, nd=3):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else round(x, nd)


def market_series(conn, cp):
    """Per-trading-day AS-OF series from the market study. Row `date` = that session's close; the slider
    day d may only show rows dated < d. Labels/probabilities are each day's expanding-window values
    (what `assess` would have said), never a final retrospective fit."""
    if not has_market(conn):
        return None
    try:
        prows, orows, pn = D.study(conn, cp, *cp_range(cp))
    except Exception as e:  # no proxies / no bars
        print(f"market series skipped for {cp}: {e}", flush=True)
        return None
    from youfin import regime as R
    tick = [{"t": p["ticker"], "role": p["role"], "direct": p["role"] in R.DIRECT_ROLES,
             "ctx": p["role"] in ("sector_etf", "credit")} for p in pn.prox if p["role"] != "market"]
    car = prows.pivot(index="date", columns="ticker", values="ar").fillna(0).cumsum() * 100
    pv = {k: prows.pivot(index="date", columns="ticker", values=k) for k in ("label", "ar_z", "car5_z", "vol_z", "bocpd_ar")}
    dates = [x.date().isoformat() for x in orows.index]
    out = {"tickers": tick, "dates": dates, "win": list(orows["window"]),
           "ps": [_f(v) for v in orows["basket_p_stress"]], "bocpd": [_f(v) for v in orows["basket_bocpd"]],
           "bcar5z": [_f(v, 2) for v in orows["basket_car5_z"]], "car": {}, "lab": {}, "arz": {}, "car5z": {}, "volz": {}, "bo": {}}
    direct = [x["t"] for x in tick if x["direct"]]
    for x in tick:
        t = x["t"]
        out["car"][t] = [_f(v, 2) for v in car[t].reindex(orows.index)]
        out["lab"][t] = [None if v is None or v != v else v for v in pv["label"][t].reindex(orows.index)]
        out["arz"][t] = [_f(v, 2) for v in pv["ar_z"][t].reindex(orows.index)]
        out["car5z"][t] = [_f(v, 2) for v in pv["car5_z"][t].reindex(orows.index)]
        out["volz"][t] = [_f(v, 2) for v in pv["vol_z"][t].reindex(orows.index)]
        out["bo"][t] = [_f(v) for v in pv["bocpd_ar"][t].reindex(orows.index)]
    out["bmax"] = [max([out["bo"][t][i] or 0 for t in direct], default=0) for i in range(len(dates))]
    return out


def _row(ms, d):
    """Index of the last market row dated strictly before day d (None if none)."""
    i = None
    for k, x in enumerate(ms["dates"]):
        if x < d:
            i = k
    return i


def _prev(d):
    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()


def team_at(conn, cp, name, d, evs, ms, acts, acts_prev, exp_total, npos):
    """Rule-based 'Today for the team': what changed since the previous day + 2-4 discussion prompts,
    every prompt citing its evidence. No LLM."""
    changes, prompts = [], []
    new = [e for e in evs if e["new"]]
    new.sort(key=lambda e: (e["new"] != "ALERT", -e["severity"]))
    standing_alert = [e for e in evs if e["level"] == "ALERT"]
    i = _row(ms, d) if ms else None
    j = _row(ms, _prev(d)) if ms else None
    fresh = ms is not None and i is not None and i != j     # a new session closed since yesterday
    win = ms["win"][i] if i is not None else None
    direct = [x["t"] for x in ms["tickers"] if x["direct"]] if ms else []
    labs = {t: ms["lab"][t][i] for t in direct} if i is not None else {}
    still_open = [t for t, l in labs.items() if l == "open"]
    ev_cite = lambda e: (best_receipt(e) or {})   # noqa: E731

    for e in new:
        r = ev_cite(e)
        kind = "escalated to ALERT" if e["escalated"] else f"new {e['new']}"
        changes.append({"kind": "event", "text": f"Verified event {kind}: {e['headline']} ({e['verdict'].replace('_', ' ')}, sev {e['severity']})",
                        "evidence": f"{r.get('domain', 'event')} T{r.get('tier', '?')}" if r else f"event #{e['id']}"})
    if ms and i is not None:
        if fresh and j is not None and ms["win"][j] != win:
            changes.append({"kind": "window", "text": f"Overall market window flipped {ms['win'][j].upper()} to {win.upper()}",
                            "evidence": f"close {ms['dates'][i]}"})
        if fresh and j is not None:
            for t in direct:
                a, b = ms["lab"][t][j], ms["lab"][t][i]
                if a != b and b not in (None, "n/a"):
                    changes.append({"kind": "proxy", "text": f"{t} window {str(a).upper()} to {str(b).upper()}",
                                    "evidence": f"{t} close {ms['dates'][i]}, car5_z {ms['car5z'][t][i]}"})
        if fresh:
            for x in ms["tickers"]:
                t = x["t"]
                z, v = ms["arz"][t][i], ms["volz"][t][i]
                if z is not None and z <= -2:
                    changes.append({"kind": "z", "text": f"{t} abnormal return vs SPY z={z:+.1f}" + (f", volume z={v:+.1f}" if v is not None and v >= 2 else ""),
                                    "evidence": f"{t} close {ms['dates'][i]}"})
                elif v is not None and v >= 3:
                    changes.append({"kind": "vol", "text": f"{t} volume spike z={v:+.1f}", "evidence": f"{t} close {ms['dates'][i]}"})
            p1 = ms["ps"][i]
            p0 = ms["ps"][j] if j is not None else None
            if p1 is not None and p0 is not None and (p1 - p0 >= 0.2 or (p0 < 0.5 <= p1)):
                changes.append({"kind": "stress", "text": f"Basket P(stress) jumped {p0:.2f} to {p1:.2f}", "evidence": f"basket close {ms['dates'][i]}"})
    prev_stage = {(a["sender"], a["key"]): a["stage"] for a in acts_prev}
    stage_new = [a for a in acts if a["today"] and a["stage"] and prev_stage.get((a["sender"], a["key"])) != a["stage"]]
    for a in stage_new:
        changes.append({"kind": "covenant", "text": f"Covenant desk: {a['summary']} (stage {a['stage']})", "evidence": f"actions/{a['sender']} {a['as_of']}"})

    # ---- prompts, most decision-relevant first
    def win_txt():
        if not ms or i is None:
            return "no market read yet"
        return f"market window {win.upper()}" + (f" ({', '.join(still_open)} still open)" if still_open and win != "open" else
                                                 f" on {', '.join(still_open)}" if still_open else "")
    ps_txt = ""
    if ms and i is not None and ms["ps"][i] is not None:
        ps_txt = f", basket P(stress) {ms['ps'][i]:.2f}"
    for e in new[:1]:
        r = ev_cite(e)
        who = r.get("publisher") or r.get("domain") or "a source"
        q = (r.get("quote") or "").strip().replace("\n", " ")
        q = (q[:150] + "...") if len(q) > 150 else q
        src = f"{who} ({r.get('tier_name', '?')}, {str(r.get('published_at') or '')[:10]})" if r else "the verifier"
        if e["new"] == "ALERT":
            ask = ("the market has not priced it yet: do we issue a draw stop and call counsel today?" if win in (None, "open") else
                   "the market is starting to move: do we issue a draw stop today and escalate to IC?" if win == "closing" else
                   "the market already moved: do we shift to protecting our position (reservation of rights, draw stop)?")
            prompts.append({"text": f"{src}: “{q}”. {win_txt()[0].upper() + win_txt()[1:]}{ps_txt}. {name} exposure ${exp_total / 1e6:,.0f}M over {npos} positions. {ask[0].upper() + ask[1:]}" if q else
                            f"{name} ALERT ({e['headline']}); {win_txt()}. {ask}",
                            "evidence": f"event #{e['id']}, {r.get('domain', '?')} T{r.get('tier', '?')}; {win_txt()}"})
        else:
            prompts.append({"text": f"New WATCH on {name}: “{e['headline']}” from {src}. It is {e['verdict'].replace('_', ' ')}, not yet a verified ALERT. Which independent second source would confirm it, and who owns checking it today?",
                            "evidence": f"event #{e['id']}, verdict {e['verdict']}"})
    if ms and fresh and i is not None:
        movers = [(ms["arz"][x["t"]][i], x["t"]) for x in ms["tickers"] if ms["arz"][x["t"]][i] is not None and ms["arz"][x["t"]][i] <= -2]
        if movers:
            z, t = min(movers)
            tail = ("The verified news already points the same way: does the price move add urgency to the draw stop?" if standing_alert else
                    "No verified news explains it: should Scout dig into this name today?")
            prompts.append({"text": f"{t} fell z={z:+.1f} vs SPY on the {ms['dates'][i]} close (window {labs.get(t, 'n/a').upper()}). {tail}",
                            "evidence": f"{t} ar_z {z:+.1f}, car5_z {ms['car5z'][t][i]}, close {ms['dates'][i]}"})
        p1 = ms["ps"][i]
        if p1 is not None and p1 >= 0.5 and not any(p["evidence"].startswith("basket") for p in prompts):
            prompts.append({"text": f"The basket of proxies is in a stress regime (P(stress) {p1:.2f}); window {win.upper()}. Is this contagion we should size across the book?",
                            "evidence": f"basket P(stress) {p1:.2f}, close {ms['dates'][i]}"})
    for a in stage_new[:1]:
        prompts.append({"text": f"Covenant desk now reads: {a['summary']} Who owns the notice or reservation-of-rights letter, and by when?",
                        "evidence": f"actions/{a['sender']} {a['as_of']}" + (f", receipt {a['url']}" if a.get("url") else "")})
    if len(prompts) < 2 and standing_alert:
        e = standing_alert[0]
        prompts.append({"text": f"ALERT standing since {e['alerted_as_of']} ({e['headline']}); {win_txt()}{ps_txt}. Has anything changed our draw-stop and hedge decision since?",
                        "evidence": f"event #{e['id']}, alerted {e['alerted_as_of']}"})
    if len(prompts) < 2 and not standing_alert and any(e["level"] == "watch" for e in evs):
        e = [x for x in evs if x["level"] == "watch"][0]
        prompts.append({"text": f"WATCH since {e['watch_as_of']}: “{e['headline']}”, still no ALERT. What would move it to ALERT, and do we pre-position counsel?",
                        "evidence": f"event #{e['id']}, watch {e['watch_as_of']}"})
    if not prompts:
        prompts.append({"text": f"Quiet day for {name}: no verified change{', market ' + win_txt() if ms and i is not None else ''}. Anything the team should ask Scout to widen?",
                        "evidence": "no new verified events or market triggers"})
    return {"changes": changes[:8], "prompts": prompts[:4]}


# ------------------------------------------------------------------ executive summary (rule-based, as-of only)
def _clip(t, n):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "..."


def _cov_line(conn, cp, d):
    try:
        res = []
        for p in C.load_positions():
            if cp in [c["id"] for c in p.get("counterparties", [])]:
                a = C.assess(conn, p, d)
                res.append((a["stage"], a["stage_label"], p["id"], a["checklist"]))
    except Exception as e:  # never break the dashboard on a covenant error
        return f"unavailable ({type(e).__name__})"
    if not res:
        return "no position on this counterparty"
    st, lab, pid, ck = max(res, key=lambda r: r[0])
    if not st:
        return f"stage 0/4 on {pid}: no trigger tripped or possible from public news"
    top = "; ".join(f"{c['short']} ({c['urgency']})" for c in ck[:2])
    return f"stage {st}/4 {lab} on {pid}: {top}"


def summary_at(conn, cp, name, d, evs, ms, team, exp_total, npos):
    """Executive summary for day d from the same as-of pieces as the rest of the dashboard. No LLM, no
    data dated after d (events_at/market rows/covenant are all as-of). T-n is hindsight: only for replays."""
    live = cp in LIVE
    top = max(evs, key=lambda e: (LEVEL_RANK[e["level"]], e["severity"]), default=None)
    lvl = top["level"] if top else "log"
    new = sorted([e for e in evs if e["new"]], key=lambda e: (-LEVEL_RANK[e["level"]], -e["severity"]))
    i = _row(ms, d) if ms else None
    win = ms["win"][i] if i is not None else None
    tn = ""
    if not live and FILINGS.get(cp) and lvl == "ALERT":
        n = (date.fromisoformat(FILINGS[cp]) - date.fromisoformat(d)).days
        tn = f" (T{'-' if n >= 0 else '+'}{abs(n)})"
    word = {"ALERT": "ALERT", "watch": "WATCH", "log": "quiet"}[lvl]
    if lvl == "log":
        head = f"{name}: no verified alert; " + (f"market window {win.upper()}" if win else "no market read yet")
    else:
        pre = "New verified ALERT" if any(e["new"] == "ALERT" for e in evs) else "New WATCH" if new else f"Standing {word}"
        head = (f"{pre}{tn}: ${exp_total / 1e6:,.0f}M over {npos} position{'s' if npos != 1 else ''}"
                + (f", market window {win.upper()}" if win else ""))
    # news
    pick = new[0] if new else top
    if pick and lvl != "log":
        r = best_receipt(pick) or {}
        q = _clip(r.get("quote"), 90)
        news = (f"{r.get('domain', 'source')} ({r.get('tier_name', '?')}, {str(r.get('published_at') or '')[:10]}): "
                f"{_clip(re.sub(r'^\[[^]]*\]\s*', '', pick['headline']), 80)}" + (f" - “{q}”" if q and pick["new"] else "")) + ("" if new else " [standing]")
    else:
        news = "no new verified item"
    # verifier
    if top and lvl != "log":
        doms = {x["domain"] for x in top["sources"]}
        tier = min((x["tier"] for x in top["sources"]), default="?")
        why = {"corroborated": "independent sources agree", "confirmed": "primary-source confirmation"}.get(top["verdict"], "not independently confirmed yet")
        ver = f"{word} - {top['verdict'].replace('_', ' ')}: {len(doms)} publisher{'s' if len(doms) != 1 else ''}, best tier T{tier}; {why}"
    else:
        ver = "nothing above log level"
    # market
    if ms and i is not None:
        direct = [x["t"] for x in ms["tickers"] if x["direct"]]
        labs = ", ".join(f"{t} {str(ms['lab'][t][i]).upper()}" for t in direct if ms["lab"][t][i] not in (None, "n/a"))
        mv = [(t, ms["arz"][t][i]) for t in [x["t"] for x in ms["tickers"]] if ms["arz"][t][i] is not None and ms["arz"][t][i] <= -2]
        ctxs = [x["t"] for x in ms["tickers"] if x["ctx"] and x["role"] == "sector_etf"]
        sector_down = any((ms["arz"][t][i] or 0) <= -1.5 for t in ctxs)
        bits = [f"window {win.upper()}" + (f" ({labs})" if labs else "")]
        if mv:
            tag = "moved with the sector (likely noise)" if sector_down else "no sector move (counterparty-specific?)"
            bits.append("notable: " + ", ".join(f"{t} z={z:+.1f}" for t, z in mv[:3]) + f", {tag}")
        else:
            bits.append("no proxy z<=-2")
        ps, bo = ms["ps"][i], ms["bmax"][i]
        if ps is not None:
            bits.append(f"P(stress) {ps:.2f}" + (f", BOCPD {bo:.2f}" if bo else ""))
        gap = (date.fromisoformat(d) - date.fromisoformat(ms["dates"][i])).days
        if gap > 1:
            bits.append(f"weekend/holiday: prices from prior close {ms['dates'][i]}")
        mk = "; ".join(bits)
    else:
        mk = "no market data"
    pt = team["prompts"][0]["text"] if team and team.get("prompts") else "no open question"
    sent = [x for x in re.split(r"(?<=[.?!])\s+", pt) if x]
    prompt = _clip(sent[-1] if sent else pt, 170)
    rule = {"headline": head, "news": news, "verifier": ver, "market": mk, "covenant": _cov_line(conn, cp, d), "team": prompt}
    from youfin.agents.summarizer import apply_to_rule_summary   # LLM text only if it passed the fact guard
    return apply_to_rule_summary(conn, cp, d, rule)


def render_summary(s):
    lab = [f"_LLM · grounded in {s['llm']} board messages_"] if s.get("llm") else []
    return lab + [f"**Executive summary: {s['headline']}**", f"- News: {s['news']}", f"- Verifier: {s['verifier']}",
            f"- Market: {s['market']}", f"- Covenant: {s['covenant']}", f"- Team: {s['team']}", ""]


def summary_for(conn, cp, name, d):
    """Standalone summary (brief.py): builds the as-of pieces itself."""
    evs = [e for e in events_at(conn, cp, d) if e["level"] != "log" or e["new"] or e["severity"] >= 4]
    ms = market_series(conn, cp) if has_market(conn) else None
    ps, tot = exposure(conn, cp)
    team = team_at(conn, cp, name, d, evs, ms, actions_at(conn, cp, d), actions_at(conn, cp, _prev(d)), tot, len(ps))
    return summary_at(conn, cp, name, d, evs, ms, team, tot, len(ps))
