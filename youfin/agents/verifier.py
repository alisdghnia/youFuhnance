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
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta

from ..embed import Embedder, cosine, enabled as embed_enabled
from ..risk_tags import RULES
from ..sources import publisher, tier
from ..llm import Judge, ensure_schema, log_decision
from ..youcom import YouClient
from .scout import JUNK_DOMAINS, _pub_date, matches, score, score_as_of

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
# Embedding "same event?" bands (cosine of title + description vs the closest article already
# in the candidate event). >= HIGH: same event. [LOW, HIGH): ask the LLM. < LOW: different
# unless the tag/title rule merges. Calibrated for nomic-embed-text on data/youfin.db
# (scripts/calibrate_embed.py): different developments of one company reach 0.93 (p90 0.84),
# syndicated copies 0.88-1.00, paraphrases ~0.87-0.88. Re-calibrate if YOUFIN_EMBED_MODEL changes.
EMBED_HIGH = float(os.environ.get("YOUFIN_EMBED_HIGH", "0.94"))
EMBED_LOW = float(os.environ.get("YOUFIN_EMBED_LOW", "0.87"))


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


PAYWALL_DOMAINS = ("ft.com", "wsj.com", "bloomberg.com")
# Navigation, consent and sign-up boilerplate that is never a supporting sentence.
BOILERPLATE = re.compile(
    r"skip to|accessibility help|sign in|log in|subscribe|subscription|newsletter|"
    r"cookie|privacy policy|terms of (use|service)|all rights reserved|"
    r"javascript|advertisement|read more|follow us|share this|menu|"
    r"create (an )?account|already a (subscriber|member)|register (now|for free)|"
    r"free trial|setze dich|sign up|unlock|premium|paywall|click here",
    re.I,
)


def is_junk(text: str | None) -> bool:
    """True for navigation / paywall / cookie-banner text and link-heavy fragments."""
    if not text or len(text) < 40:
        return True
    if BOILERPLATE.search(text) or text.rstrip().endswith(("...", "…")):
        return True  # boilerplate, or a truncated search-result description
    if text.count("http") >= 2 or text.count("|") >= 3 or text.count(">") >= 3:
        return True
    words = text.split()
    caps = sum(w[:1].isupper() for w in words)
    return len(words) >= 8 and caps / len(words) > 0.7  # menu-style Title Case runs


def is_paywalled(domain: str | None) -> bool:
    d = (domain or "").lower()
    return any(d == p or d.endswith("." + p) for p in PAYWALL_DOMAINS)


def _clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)        # links -> anchor text
    text = re.sub(r"^\s*(#+|\||[-*]\s)", " ", text, flags=re.M)  # headings, tables, bullets
    text = re.sub(r"[*_`]{1,3}", "", text)                       # emphasis markers
    return re.sub(r"\s+", " ", text)


def _sentence_with(text: str, terms: list[str], limit: int) -> str | None:
    for s in re.split(r"(?<=[.!?])\s+", _clean_markdown(text)):
        if len(s) >= 40 and any(t in s.lower() for t in terms) and not is_junk(s[:limit]):
            return s.strip()[:limit]
    return None


def best_quote(full: str, short: str, terms: list[str], limit: int = 300,
               highlights: list[str] | None = None) -> str:
    """Sentence that supports the claim: highlights first, then full text, then snippets.

    Returns "" when nothing clean is found, so callers can fall back to another source.
    """
    for h in highlights or []:
        q = _sentence_with(h, terms, limit)
        if q:
            return q
    q = _sentence_with(full, terms, limit) or _sentence_with(short, terms, limit)
    if q:
        return q
    for h in highlights or []:
        c = _clean_markdown(h).strip()[:limit]
        if not is_junk(c):
            return c
    c = _clean_markdown(short).strip()[:limit]
    return "" if is_junk(c) else c


def receipt_sources(conn: sqlite3.Connection, event_id: int, n: int = 1) -> list[sqlite3.Row]:
    """Sources to show as receipts: clean quotes from open sources first, best tier first."""
    rows = conn.execute("SELECT * FROM event_sources WHERE event_id=? ORDER BY tier, published_at",
                        (event_id,)).fetchall()
    rows.sort(key=lambda r: (is_junk(r["quote"]), is_paywalled(r["domain"])))  # stable
    return rows[:n]


@dataclass
class Verdict:
    verdict: str
    confidence: float
    n_publishers: int
    best_tier: int | None


# How a source joined its event (event_sources.merge_basis). Sources a model attached on its own
# (LLM borderline merge, or an embedding merge across primary tags) stay on the event for display
# and clustering but never count as corroboration and never raise its severity: models may veto,
# never upgrade. 'embedding_high' (cos >= EMBED_HIGH, same primary tag) counts like the tag rule.
NOT_COUNTED = ("llm_borderline", "embedding_high_xtag")


def counted(source: sqlite3.Row) -> bool:
    return source["merge_basis"] not in NOT_COUNTED


def judge(sources: list[sqlite3.Row]) -> Verdict:
    pubs: dict[str, int] = {}
    for s in sources:
        if not counted(s):
            continue
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
                 use_llm: bool = True, bus=None, embedder: Embedder | None = None,
                 use_embed: bool = True):
        self.conn = conn
        self.bus = bus
        if bus is not None:
            bus.subscribe("verifier", ["signals"])
        self.judge = judge if judge is not None else (Judge(conn) if use_llm else None)
        self.embedder = embedder if embedder is not None else (
            Embedder(conn) if use_embed and embed_enabled() else None)
        ensure_schema(conn)
        self.stats = {"cluster_s": 0.0, "same_event_calls": 0, "same_event_live": 0}
        self.client = client
        self.min_severity = min_severity
        self.verify_min_severity = verify_min_severity

    # ---------------------------------------------------------------- helpers
    def _archive(self):
        if getattr(self, "archive", None) is None:
            from ..archive import Archive
            self.archive = Archive(self.conn)
        return self.archive

    def _cp(self, cp_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM counterparties WHERE id=?", (cp_id,)).fetchone()

    def _article_text(self, url: str) -> tuple[str, str]:
        a = self.conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
        short = " ".join([a["title"] or "", a["description"] or "",
                          *json.loads(a["snippets"] or "[]")])
        return short, (a["markdown"] or short)

    def _highlights(self, url: str) -> list[str]:
        a = self.conn.execute("SELECT highlights FROM articles WHERE url=?", (url,)).fetchone()
        return json.loads(a["highlights"] or "[]") if a else []

    def _add_source(self, event_id: int, url: str, domain: str, role: str,
                    published: str | None, terms: list[str], basis: str = "live",
                    merge_basis: str | None = None) -> None:
        short, full = self._article_text(url)
        quote = best_quote(full, short, terms, highlights=self._highlights(url))
        self.conn.execute(
            "INSERT OR IGNORE INTO event_sources (event_id, url, domain, publisher, tier, role, "
            "published_at, quote, score_basis, merge_basis) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, url, domain, publisher(domain), tier(domain), role, published, quote, basis,
             merge_basis or role),
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
    def _as_of_text(self, url: str) -> str:
        """As-of text only: title + description (what replay scoring trusts; no hindsight)."""
        a = self.conn.execute("SELECT title, description FROM articles WHERE url=?",
                              (url,)).fetchone()
        return " ".join([a["title"] or "", a["description"] or ""]).strip() if a else ""

    def _event_cosines(self, url: str, events: list[sqlite3.Row]) -> dict[int, float] | None:
        """Cosine of the new article vs the closest article already in each candidate event.
        None when embeddings are off or unavailable (callers then use the old heuristics)."""
        if self.embedder is None or self.embedder.available is False:
            return None
        members = {ev["id"]: [r[0] for r in self.conn.execute(
            "SELECT url FROM event_sources WHERE event_id=?", (ev["id"],))] for ev in events}
        urls = list(dict.fromkeys([url, *(u for us in members.values() for u in us)]))
        vecs = self.embedder.embed([self._as_of_text(u) for u in urls])
        if vecs is None:
            return None
        vec = dict(zip(urls, vecs))
        return {eid: max((cosine(vec[url], vec[u]) for u in us), default=0.0)
                for eid, us in members.items()}

    def _log(self, as_of: date, s: sqlite3.Row, event_id: int | None, merged: bool,
             applied: str, reason: str, score: float | None) -> None:
        log_decision(self.conn, "embed", as_of.isoformat(), s["title"] or s["url"], merged, reason,
                     self.embedder.model, True, 0.0, event_id, applied, score)

    def cluster(self, as_of: date, signal_ids: list[int] | None = None,
                mode: str = "replay") -> int:
        """signal_ids: cluster only these signals (the ones received over the bus).
        mode="replay": the LLM sees only as-of text (title + description), never today's snippets.

        Candidates are events of the same counterparty first published within CLUSTER_DAYS.
        With embeddings: cos >= EMBED_HIGH merges; the tag/title rule still merges; else
        EMBED_LOW <= cos < EMBED_HIGH asks the LLM; below EMBED_LOW stays separate. Signals
        below min_severity (no risk keywords, e.g. a paraphrase) never open an event or reach
        the LLM, but join one when cos >= EMBED_HIGH. Without embeddings: the previous rules.
        """
        t_start = time.time()
        only = ""
        if signal_ids is not None:
            only = f"AND s.id IN ({','.join(str(int(i)) for i in signal_ids) or 'NULL'}) "
        attach = self.embedder is not None and self.embedder.available is not False
        rows = self.conn.execute(
            "SELECT s.*, a.title, a.domain FROM signals s JOIN articles a USING (url) "
            "WHERE s.event_id IS NULL AND s.first_seen_as_of <= ? AND (s.severity >= ? OR "
            "(? AND s.first_seen_as_of = ?)) " + only + "ORDER BY s.published_at, s.id",
            (as_of.isoformat(), self.min_severity, int(attach), as_of.isoformat()),
        ).fetchall()
        touched: set[int] = set()
        n_clustered = 0
        for s in rows:
            cp = self._cp(s["counterparty_id"])
            aliases = [a.lower() for a in json.loads(cp["aliases"])]
            tags = json.loads(s["tags"])
            ptag = primary_tag(tags)
            pub = _pub_date(s["published_at"]) or date.fromisoformat(s["first_seen_as_of"])
            toks = title_tokens(s["title"], aliases)
            attach_only = s["severity"] < self.min_severity
            cands = self.conn.execute(
                "SELECT * FROM events WHERE counterparty_id=? AND first_published BETWEEN ? AND ?",
                (cp["id"], (pub - timedelta(days=CLUSTER_DAYS)).isoformat(),
                 (pub + timedelta(days=CLUSTER_DAYS)).isoformat()),
            ).fetchall()
            coss = self._event_cosines(s["url"], cands) if cands else None
            if attach_only and coss is None:
                continue  # below min severity: only an embedding match can attach it

            best, best_score, rule = None, (0.0 if coss is None else -2.0), None
            borderline: list[tuple[float, sqlite3.Row]] = []
            rule_ok: dict[int, bool] = {}
            for ev in cands:
                sim = jaccard(toks, title_tokens(ev["headline"], aliases))
                same_claim = 0.6 if ev["primary_tag"] == ptag else 0.0
                same_day = ev["first_published"] == pub.isoformat()
                # Same-day stories need less title overlap to count as one development.
                threshold = 0.3 if same_day else 0.5
                s_score = max(sim, same_claim)
                if coss is None:  # no embeddings: previous behaviour
                    if s_score >= threshold and s_score > best_score:
                        best, best_score, rule = ev, s_score, "rule"
                    elif sim >= LLM_MIN_SIM or (same_day and ev["primary_tag"] != ptag):
                        borderline.append((sim, ev))
                    continue
                cos = coss[ev["id"]]
                ok = rule_ok[ev["id"]] = s_score >= threshold and not attach_only
                if ok or cos >= EMBED_HIGH:
                    # The tag/title rule keeps precedence (its merges count); cos breaks ties.
                    if best is None or (ok, cos) > (rule == "rule", best_score):
                        best, best_score, rule = ev, cos, ("rule" if ok else "high")
                elif cos >= EMBED_LOW and not attach_only:
                    borderline.append((cos, ev))
            decision, band_cos = None, None
            if best is None and self.judge and borderline:
                borderline.sort(key=lambda x: -x[0])
                text = (self._as_of_text(s["url"]) if mode == "replay"
                        else self._article_text(s["url"])[0])
                for score_, ev in borderline[:LLM_MAX_PER_SIGNAL]:
                    live_before = self.judge.live_calls
                    res = self.judge.same_event(ev["headline"], ev["primary_tag"], s["title"], text,
                                                as_of.isoformat())
                    if res is None:
                        break
                    self.stats["same_event_calls"] += 1
                    self.stats["same_event_live"] += self.judge.live_calls - live_before
                    decision = self.judge.last_decision
                    band_cos = score_ if coss is not None else None
                    if res[0]:
                        best, rule = ev, "llm"
                        self.judge.annotate(decision, ev["id"], "merged (not counted)", band_cos)
                        break
                    self.judge.annotate(decision, ev["id"], "kept separate", band_cos)
            if best is None and attach_only:
                continue
            if best is None:
                merge_basis = "opened"
            elif rule == "llm":
                merge_basis = "llm_borderline"
            elif rule == "high":
                merge_basis = ("embedding_high" if ptag == best["primary_tag"]
                               else "embedding_high_xtag")
            else:
                merge_basis = "rule"
            if best is not None:
                event_id = best["id"]
                if merge_basis not in NOT_COUNTED:  # a model-only merge never raises severity
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
                self.judge.annotate(decision, event_id, "kept separate", band_cos)
            if coss:  # log why the embedding path merged or split (report: "~ EMB")
                top_id = max(coss, key=coss.get)
                top = coss[top_id]
                if rule == "rule" and best_score >= EMBED_HIGH:
                    self._log(as_of, s, event_id, True, "merged (rule + embedding)",
                              f"cos {best_score:.3f} >= high {EMBED_HIGH}; tag/title rule agrees",
                              best_score)
                elif rule == "rule":
                    self._log(as_of, s, event_id, True, "merged (tag/title rule)",
                              f"same tag or title overlap; cos {best_score:.3f}", best_score)
                elif rule == "high":
                    sub = ", below min severity" if attach_only else ""
                    cnt = "not counted" if merge_basis in NOT_COUNTED else "counted"
                    self._log(as_of, s, event_id, True, f"merged (embedding{sub}; {cnt})",
                              f"cos {best_score:.3f} >= high {EMBED_HIGH}; tag/title rule would "
                              "not merge", best_score)
                elif rule is None and decision is None:
                    why = (f"cos {top:.3f} < low {EMBED_LOW}" if top < EMBED_LOW else
                           f"cos {top:.3f} in band, LLM unavailable")
                    self._log(as_of, s, event_id, False, "new event",
                              f"{why} vs event {top_id}", top)
            self.conn.execute("UPDATE signals SET event_id=? WHERE id=?", (event_id, s["id"]))
            self._add_source(event_id, s["url"], s["domain"], "scout", s["published_at"],
                             json.loads(s["matched_terms"]), s["score_basis"] or "live",
                             merge_basis)
            touched.add(event_id)
            n_clustered += 1
        for eid in touched:
            self._refresh(eid, as_of)
        self.conn.commit()
        self.stats["cluster_s"] += time.time() - t_start
        return n_clustered

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
                                         exclude_domains=JUNK_DOMAINS, highlights=True)
            n_new = 0
            for r in results:
                url = r.get("url")
                pub = _pub_date(r.get("page_age"))
                if not url or pub is None or pub > as_of or pub < start:
                    continue  # undated / future (look-ahead) / stale
                hl = (r.get("contents") or {}).get("highlights") or []
                text = " ".join([r.get("title") or "", r.get("description") or "",
                                 *(r.get("snippets") or []), *hl])
                if not matches(text, aliases, context):
                    continue
                domain = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*", r"\1", url))
                basis = "live"
                if mode == "replay":
                    # Score on the page as it was on as_of, not today's snippets.
                    _, tags, terms, basis, text = score_as_of(
                        r.get("title") or "", r.get("description") or "", text, domain, url,
                        as_of, self._archive())
                else:
                    _, tags, terms = score(text, domain)
                kw_ok = ev["primary_tag"] in tags
                if not kw_ok:
                    continue  # mentions the company but does not support this claim
                # The LLM is a veto only: it can reject a keyword match but never add
                # corroboration on its own (a small model is too permissive to trust
                # with upgrading a verdict).
                if self.judge:
                    res = self.judge.supports_claim(ev["primary_tag"], ev["headline"], text,
                                                    as_of.isoformat(), subject=url)
                    if res is not None:
                        self.judge.annotate(
                            self.judge.last_decision, ev["id"],
                            "accepted (keyword agreed)" if res[0] else "vetoed keyword match")
                        if not res[0]:
                            continue  # LLM: does not support the claim
                self.conn.execute(
                    "INSERT OR IGNORE INTO articles (url, title, description, snippets, page_age, "
                    "domain) VALUES (?,?,?,?,?,?)",
                    (url, r.get("title"), r.get("description"),
                     json.dumps(r.get("snippets") or []), r.get("page_age"), domain),
                )
                if hl:
                    self.conn.execute("UPDATE articles SET highlights=? WHERE url=?",
                                      (json.dumps(hl), url))
                before = self.conn.total_changes
                self._add_source(ev["id"], url, domain, "corroboration", pub.isoformat(), terms,
                                 basis)
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

    # ---------------------------------------------------------------- re-quote
    def requote(self, search: bool = True) -> dict:
        """Re-derive every receipt quote from stored data (no API calls); then, for events
        whose best receipt is still junk or paywalled, fetch highlights once per event."""
        stats = {"updated": 0, "searches": 0, "stored_highlights": 0}
        stats["updated"] += self._requote_all()
        if search and self.client is not None:
            for ev in self.conn.execute("SELECT * FROM events WHERE alert_level='ALERT' "
                                        "ORDER BY id").fetchall():
                best = receipt_sources(self.conn, ev["id"], 1)
                if best and not is_junk(best[0]["quote"]) and not is_paywalled(best[0]["domain"]):
                    continue
                cp = self._cp(ev["counterparty_id"])
                start = date.fromisoformat(ev["first_published"]) - timedelta(days=CLUSTER_DAYS)
                end = date.fromisoformat(ev["first_published"]) + timedelta(days=VERIFY_WINDOW_DAYS)
                query = f"{cp['name']} {TAG_QUERY.get(ev['primary_tag'], '')}".strip()
                results = self.client.search(query, count=20, highlights=True,
                                             freshness=f"{start.isoformat()}to{end.isoformat()}",
                                             exclude_domains=JUNK_DOMAINS)
                stats["searches"] += 1
                for r in results:
                    hl = (r.get("contents") or {}).get("highlights") or []
                    if hl and self.conn.execute("SELECT 1 FROM event_sources WHERE url=?",
                                                (r["url"],)).fetchone():
                        self.conn.execute("UPDATE articles SET highlights=? WHERE url=?",
                                          (json.dumps(hl), r["url"]))
                        stats["stored_highlights"] += 1
            stats["updated"] += self._requote_all()
        return stats

    def _requote_all(self) -> int:
        changed = 0
        rows = self.conn.execute(
            "SELECT es.event_id, es.url, es.quote, s.matched_terms FROM event_sources es "
            "LEFT JOIN signals s ON s.url=es.url AND s.event_id=es.event_id").fetchall()
        for r in rows:
            terms = json.loads(r["matched_terms"]) if r["matched_terms"] else self._terms(r)
            short, full = self._article_text(r["url"])
            quote = best_quote(full, short, terms, highlights=self._highlights(r["url"]))
            if quote != (r["quote"] or ""):
                self.conn.execute("UPDATE event_sources SET quote=? WHERE event_id=? AND url=?",
                                  (quote, r["event_id"], r["url"]))
                changed += 1
        self.conn.commit()
        return changed

    def _terms(self, row: sqlite3.Row) -> list[str]:
        """Corroboration sources have no signal row: re-score the stored text."""
        short, full = self._article_text(row["url"])
        domain = self.conn.execute("SELECT domain FROM articles WHERE url=?",
                                   (row["url"],)).fetchone()[0] or ""
        return score(" ".join([short, *self._highlights(row["url"])]), domain)[2]

    def step(self, as_of: date, mode: str = "replay"):
        if self.bus is None:
            n = self.cluster(as_of, mode=mode)
            return n, self.verify(as_of, mode)
        before = self._event_state()
        msgs = self.bus.poll("verifier")
        n = self.cluster(as_of, [m["payload"]["id"] for m in msgs if m["topic"] == "signals"],
                         mode)
        verified = self.verify(as_of, mode)
        self._publish_changes(before, as_of, n, len(msgs))
        return n, verified

    # ---------------------------------------------------------------- bus output
    def _event_state(self) -> dict[int, tuple]:
        return {r["id"]: (r["verdict"], r["alert_level"]) for r in self.conn.execute(
            "SELECT id, verdict, alert_level FROM events")}

    def _publish_changes(self, before: dict, as_of: date, n_clustered: int, n_msgs: int) -> None:
        d = as_of.isoformat()
        changed = 0
        for ev in self.conn.execute("SELECT * FROM events ORDER BY id").fetchall():
            old = before.get(ev["id"])
            now = (ev["verdict"], ev["alert_level"])
            if old == now:
                continue
            changed += 1
            rec = receipt_sources(self.conn, ev["id"], 1)
            rec = rec[0] if rec else None
            cp = ev["counterparty_id"]
            payload = {
                "event_id": ev["id"], "counterparty": cp, "verdict": ev["verdict"],
                "alert_level": ev["alert_level"], "severity": ev["severity"],
                "tag": ev["primary_tag"], "headline": ev["headline"], "as_of": d,
                "receipt_url": rec["url"] if rec else None,
                "quote": (rec["quote"] or "")[:300] if rec else "",
            }
            payload["summary"] = (f"{d} {ev['alert_level']} {cp} event {ev['id']} "
                                  f"{ev['verdict']} sev{ev['severity']}: {ev['headline'][:80]}")
            self.bus.publish("events", f"event-{ev['id']}-{ev['verdict']}-{ev['alert_level']}",
                             payload, "verifier")
            if ev["alert_level"] == "ALERT" and (old is None or old[1] != "ALERT"):
                payload = dict(payload, summary=f"ALERT {cp} {d}: {ev['headline'][:100]}")
                self.bus.publish("alerts", f"alert-{ev['id']}", payload, "verifier")
        if changed or n_clustered:
            self.bus.publish("brief", f"brief-{d}", {
                "as_of": d, "signals_received": n_msgs, "clustered": n_clustered,
                "events_changed": changed,
                "summary": f"{d}: {n_msgs} signals in, {n_clustered} clustered, "
                           f"{changed} event updates",
            }, "verifier")
