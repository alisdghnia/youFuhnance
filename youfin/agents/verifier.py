"""Verifier agent: turns raw Scout signals into verified, de-duplicated events.

For each simulated (or real) day:
  1. cluster  - attach new signals to an existing event or open a new one
                (syndicated copies of one story become one event)
  2. verify   - for high-severity events that are not yet confirmed, run a
                You.com corroboration search restricted to what was published
                up to that day, and keep only hits that support the same claim
  3. decide   - verdict from source quality / independence, then alert level

Every source attached to an event keeps a quote: the sentence that supports it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..risk_tags import RULES
from ..sources import publisher, tier
from ..llm import Judge
from ..youcom import YouClient
from .scout import JUNK_DOMAINS, _pub_date, matches, score

TAG_PRIORITY = [
    "insolvency", "fraud", "lender_action", "operations", "diligence_red_flag",
    "lender_disclosure", "sec_filing", "credit", "regulatory", "funding", "litigation",
    "financing",
]
# How the Verifier phrases a corroboration search for each kind of claim.
TAG_QUERY = {
    "insolvency": "bankruptcy filing",
    "fraud": "fraud investigation",
    "lender_action": "bank seizes takes control",
    "operations": "suspends operations furloughs layoffs",
    "diligence_red_flag": "review of financing quality of earnings",
    "lender_disclosure": "lender exposure loans",
    "sec_filing": "bank filing loan exposure",
    "credit": "debt loans drop downgrade",
    "regulatory": "regulator enforcement",
    "funding": "creditors advisers refinancing",
    "litigation": "lawsuit",
}
VERDICT_CONFIDENCE = {
    "confirmed": 0.9, "corroborated": 0.75, "weakly_corroborated": 0.5, "single_source": 0.3,
}
STOP = set("a an the of to in for on and or as at by with from is are be its it after amid over "
           "says said report reports news update latest us group holdings inc llc".split())

CLUSTER_DAYS = 2           # same development if published within this many days
VERIFY_WINDOW_DAYS = 5     # keep re-checking an unconfirmed event for this long
MAX_VERIFY_ATTEMPTS = 3
LLM_MIN_SIM = 0.15         # below this title overlap, never ask the LLM about a merge
LLM_MAX_PER_SIGNAL = 2     # bound LLM merge checks per new signal


def primary_tag(tags: list[str]) -> str:
    for t in TAG_PRIORITY:
        if t in tags:
            return t
    return "other"


def title_tokens(title: str, aliases: list[str]) -> set[str]:
    t = title.lower()
    t = re.split(r"\s[|\-–]\s(?=[^|\-–]{0,40}$)", t)[0]  # drop trailing " - Site Name"
    for a in aliases:
        t = t.replace(a, " ")
    return {w for w in re.findall(r"[a-z0-9$]+", t) if w not in STOP and len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)        # links -> anchor text
    text = re.sub(r"^\s*(#+|\||[-*]\s)", " ", text, flags=re.M)  # headings, tables, bullets
    return re.sub(r"\s+", " ", text)


def _sentence_with(text: str, terms: list[str], limit: int) -> str | None:
    for s in re.split(r"(?<=[.!?])\s+", _clean_markdown(text)):
        if len(s) >= 40 and any(t in s.lower() for t in terms):
            return s.strip()[:limit]
    return None


def best_quote(full: str, short: str, terms: list[str], limit: int = 300) -> str:
    """Sentence that supports the claim: full text first, then the search snippet."""
    return (_sentence_with(full, terms, limit) or _sentence_with(short, terms, limit)
            or _clean_markdown(short).strip()[:limit])


@dataclass
class Verdict:
    verdict: str
    confidence: float
    n_publishers: int
    best_tier: int | None


def judge(sources: list[sqlite3.Row]) -> Verdict:
    pubs: dict[str, int] = {}
    for s in sources:
        pubs[s["publisher"]] = min(pubs.get(s["publisher"], 9), s["tier"])
    tiers = list(pubs.values())
    best = min(tiers) if tiers else None
    if best is not None and best <= 1:
        v = "confirmed"
    elif sum(t <= 2 for t in tiers) >= 2:
        v = "corroborated"
    elif len(pubs) >= 2:
        v = "weakly_corroborated"
    else:
        v = "single_source"
    conf = VERDICT_CONFIDENCE[v] - (0.1 if v == "single_source" and best == 3 else 0)
    return Verdict(v, round(conf, 2), len(pubs), best)


def alert_level(severity: int, verdict: str) -> str:
    if severity >= 4 and verdict in ("confirmed", "corroborated"):
        return "ALERT"
    if severity >= 4 or (severity >= 3 and verdict == "confirmed"):
        return "watch"
    return "log"


class Verifier:
    def __init__(self, conn: sqlite3.Connection, client: YouClient | None = None,
                 min_severity: int = 3, verify_min_severity: int = 4, judge: Judge | None = None,
                 use_llm: bool = True):
        self.conn = conn
        self.judge = judge if judge is not None else (Judge(conn) if use_llm else None)
        self.client = client
        self.min_severity = min_severity
        self.verify_min_severity = verify_min_severity

    # ---------------------------------------------------------------- helpers
    def _cp(self, cp_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM counterparties WHERE id=?", (cp_id,)).fetchone()

    def _article_text(self, url: str) -> tuple[str, str]:
        a = self.conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
        short = " ".join([a["title"] or "", a["description"] or "",
                          *json.loads(a["snippets"] or "[]")])
        return short, (a["markdown"] or short)

    def _add_source(self, event_id: int, url: str, domain: str, role: str,
                    published: str | None, terms: list[str]) -> None:
        short, full = self._article_text(url)
        quote = best_quote(full, short, terms)
        self.conn.execute(
            "INSERT OR IGNORE INTO event_sources VALUES (?,?,?,?,?,?,?,?)",
            (event_id, url, domain, publisher(domain), tier(domain), role, published, quote),
        )

    def _refresh(self, event_id: int, as_of: date) -> None:
        ev = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        sources = self.conn.execute(
            "SELECT * FROM event_sources WHERE event_id=?", (event_id,)).fetchall()
        v = judge(sources)
        level = alert_level(ev["severity"], v.verdict)
        # Name the event after its best source, not whichever article arrived first.
        # Dockets and filings are the best evidence but make poor headlines: rank them
        # after major press. If no readable title exists, use the receipt quote.
        best = self.conn.execute(
            "SELECT a.title FROM event_sources es JOIN articles a USING (url) "
            "WHERE es.event_id=? AND length(a.title) >= 25 AND a.title NOT LIKE 'Case number%' "
            "ORDER BY CASE es.tier WHEN 0 THEN 1.5 ELSE es.tier END, es.published_at LIMIT 1",
            (event_id,)).fetchone()
        if best:
            headline = best[0]
        else:
            src = self.conn.execute(
                "SELECT domain, quote FROM event_sources WHERE event_id=? ORDER BY tier LIMIT 1",
                (event_id,)).fetchone()
            headline = f"[{src['domain']}] {src['quote'][:120]}" if src and src["quote"] else ev["headline"]
        self.conn.execute("UPDATE events SET headline=? WHERE id=?", (headline, event_id))
        alerted = ev["alerted_as_of"] or (as_of.isoformat() if level == "ALERT" else None)
        watch = ev["watch_as_of"] or (as_of.isoformat() if level in ("watch", "ALERT") else None)
        self.conn.execute(
            "UPDATE events SET verdict=?, confidence=?, n_publishers=?, best_tier=?, "
            "alert_level=?, alerted_as_of=?, watch_as_of=? WHERE id=?",
            (v.verdict, v.confidence, v.n_publishers, v.best_tier, level, alerted, watch, event_id),
        )

    # ---------------------------------------------------------------- 1. cluster
    def cluster(self, as_of: date) -> int:
        rows = self.conn.execute(
            "SELECT s.*, a.title, a.domain FROM signals s JOIN articles a USING (url) "
            "WHERE s.event_id IS NULL AND s.first_seen_as_of <= ? AND s.severity >= ? "
            "ORDER BY s.published_at, s.id",
            (as_of.isoformat(), self.min_severity),
        ).fetchall()
        touched: set[int] = set()
        for s in rows:
            cp = self._cp(s["counterparty_id"])
            aliases = [a.lower() for a in json.loads(cp["aliases"])]
            tags = json.loads(s["tags"])
            ptag = primary_tag(tags)
            pub = _pub_date(s["published_at"]) or date.fromisoformat(s["first_seen_as_of"])
            toks = title_tokens(s["title"], aliases)

            best, best_score = None, 0.0
            borderline: list[tuple[float, sqlite3.Row]] = []
            for ev in self.conn.execute(
                "SELECT * FROM events WHERE counterparty_id=? AND first_published BETWEEN ? AND ?",
                (cp["id"], (pub - timedelta(days=CLUSTER_DAYS)).isoformat(),
                 (pub + timedelta(days=CLUSTER_DAYS)).isoformat()),
            ):
                sim = jaccard(toks, title_tokens(ev["headline"], aliases))
                same_claim = 0.6 if ev["primary_tag"] == ptag else 0.0
                same_day = ev["first_published"] == pub.isoformat()
                # Same-day stories need less title overlap to count as one development.
                threshold = 0.3 if same_day else 0.5
                s_score = max(sim, same_claim)
                if s_score >= threshold and s_score > best_score:
                    best, best_score = ev, s_score
                elif sim >= LLM_MIN_SIM or (same_day and ev["primary_tag"] != ptag):
                    borderline.append((sim, ev))
            decision = None
            if best is None and self.judge and borderline:
                borderline.sort(key=lambda x: -x[0])
                text = self._article_text(s["url"])[0]
                for _, ev in borderline[:LLM_MAX_PER_SIGNAL]:
                    res = self.judge.same_event(ev["headline"], ev["primary_tag"], s["title"], text,
                                                as_of.isoformat())
                    if res is None:
                        break
                    decision = self.judge.last_decision
                    if res[0]:
                        best = ev
                        self.judge.annotate(decision, ev["id"], "merged")
                        break
                    self.judge.annotate(decision, ev["id"], "kept separate")
            if best is not None:
                event_id = best["id"]
                merged = sorted(set(json.loads(best["tags"])) | set(tags))
                self.conn.execute(
                    "UPDATE events SET severity=MAX(severity, ?), tags=? WHERE id=?",
                    (s["severity"], json.dumps(merged), event_id),
                )
            else:
                cur = self.conn.execute(
                    "INSERT INTO events (counterparty_id, primary_tag, headline, first_published, "
                    "first_seen_as_of, severity, tags) VALUES (?,?,?,?,?,?,?)",
                    (cp["id"], ptag, s["title"], pub.isoformat(), s["first_seen_as_of"],
                     s["severity"], json.dumps(tags)),
                )
                event_id = cur.lastrowid
            if self.judge and decision and best is None:
                self.judge.annotate(decision, event_id, "kept separate")
            self.conn.execute("UPDATE signals SET event_id=? WHERE id=?", (event_id, s["id"]))
            self._add_source(event_id, s["url"], s["domain"], "scout", s["published_at"],
                             json.loads(s["matched_terms"]))
            touched.add(event_id)
        for eid in touched:
            self._refresh(eid, as_of)
        self.conn.commit()
        return len(rows)

    # ---------------------------------------------------------------- 2. verify
    def verify(self, as_of: date, mode: str = "replay") -> list[tuple[int, str, int]]:
        """Search for corroboration; returns [(event_id, verdict, n_new_sources)]."""
        if self.client is None:
            self.client = YouClient()
        due = self.conn.execute(
            "SELECT * FROM events WHERE verdict != 'confirmed' AND severity >= ? "
            "AND verify_attempts < ? AND first_seen_as_of <= ? AND first_seen_as_of >= ? "
            "AND (last_verified_as_of IS NULL OR last_verified_as_of < ?)",
            (self.verify_min_severity, MAX_VERIFY_ATTEMPTS, as_of.isoformat(),
             (as_of - timedelta(days=VERIFY_WINDOW_DAYS)).isoformat(), as_of.isoformat()),
        ).fetchall()
        out = []
        for ev in due:
            cp = self._cp(ev["counterparty_id"])
            aliases = [a.lower() for a in json.loads(cp["aliases"])]
            context = [c.lower() for c in json.loads(cp["context"] or "[]")]
            start = date.fromisoformat(ev["first_published"]) - timedelta(days=CLUSTER_DAYS)
            freshness = f"{start.isoformat()}to{as_of.isoformat()}"
            query = f"{cp['name']} {TAG_QUERY.get(ev['primary_tag'], '')}".strip()
            results = self.client.search(query, count=20, freshness=freshness,
                                         exclude_domains=JUNK_DOMAINS)
            n_new = 0
            for r in results:
                url = r.get("url")
                pub = _pub_date(r.get("page_age"))
                if not url or pub is None or pub > as_of or pub < start:
                    continue  # undated / future (look-ahead) / stale
                text = " ".join([r.get("title") or "", r.get("description") or "",
                                 *(r.get("snippets") or [])])
                if not matches(text, aliases, context):
                    continue
                domain = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*", r"\1", url))
                _, tags, terms = score(text, domain)
                kw_ok = ev["primary_tag"] in tags
                if self.judge:
                    res = self.judge.supports_claim(ev["primary_tag"], ev["headline"], text,
                                                    as_of.isoformat(), subject=url)
                    if res is not None:
                        self.judge.annotate(
                            self.judge.last_decision, ev["id"],
                            ("accepted" if res[0] else "rejected")
                            + (" (keyword agreed)" if kw_ok == res[0] else
                               " (overrode keyword tagger)"))
                        if not res[0]:
                            continue  # LLM: does not support the claim
                    elif not kw_ok:
                        continue
                elif not kw_ok:
                    continue  # mentions the company but does not support this claim
                self.conn.execute(
                    "INSERT OR IGNORE INTO articles (url, title, description, snippets, page_age, "
                    "domain) VALUES (?,?,?,?,?,?)",
                    (url, r.get("title"), r.get("description"),
                     json.dumps(r.get("snippets") or []), r.get("page_age"), domain),
                )
                before = self.conn.total_changes
                self._add_source(ev["id"], url, domain, "corroboration", pub.isoformat(), terms)
                n_new += self.conn.total_changes > before
            self._refresh(ev["id"], as_of)
            verdict = self.conn.execute("SELECT verdict FROM events WHERE id=?",
                                        (ev["id"],)).fetchone()[0]
            self.conn.execute(
                "UPDATE events SET verify_attempts=verify_attempts+1, last_verified_as_of=? "
                "WHERE id=?", (as_of.isoformat(), ev["id"]))
            self.conn.execute(
                "INSERT INTO event_verifications (event_id, as_of, query, freshness, n_results, "
                "n_supporting, verdict_after) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], as_of.isoformat(), query, freshness, len(results), n_new, verdict),
            )
            self.conn.commit()
            out.append((ev["id"], verdict, n_new))
        return out

    def step(self, as_of: date, mode: str = "replay"):
        n = self.cluster(as_of)
        return n, self.verify(as_of, mode)
