"""Scout agent: sweeps the live web for news on each counterparty.

Works in two modes:
  live   - as_of = today, window = last N days
  replay - as_of = a past date; anything published after as_of (or undated) is
           discarded so the replay never 'sees the future'.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlparse

from ..risk_tags import tag
from ..youcom import YouClient, YouComError

# Low-signal sites that match company names but carry no credit information.
JUNK_DOMAINS = [
    "trustpilot.com", "seamless.ai", "zoominfo.com", "rocketreach.co", "glassdoor.com",
    "indeed.com", "yelp.com", "bbb.org", "facebook.com", "instagram.com", "tiktok.com",
    "ziprecruiter.com", "simplyhired.com", "wayup.com", "jobs.thetrucker.com", "dnb.com",
    # encyclopedia / profile / stock-quote pages: retrospective or boilerplate, never news
    "wikipedia.org", "grokipedia.com", "whobrands.com", "macroaxis.com", "britannica.com",
    "fandom.com", "crunchbase.com", "companiesmarketcap.com", "stockanalysis.com",
    "marketscreener.com", "wallstreetzen.com", "simplywall.st", "stocktwits.com",
]
# Stock-quote / profile page titles ("Carvana (CVNA) Price, News, Quote ...").
QUOTE_PAGE = re.compile(r"price,? (news|quote)|stock (price|quote)|\bquote\b.{0,20}\bhistory\b"
                        r"|company (profile|overview)|\bwiki\b", re.I)
PROFILE_SEV_CAP = 1
PROFILE_DOMAINS = {d for d in JUNK_DOMAINS[JUNK_DOMAINS.index("wikipedia.org"):]}
JOB_POST = re.compile(r"\bhiring\b|\bjobs?\b|\bcareers?\b|\btechnician\b|\bspecialist\b", re.I)
# "cuts 500 jobs" is a credit signal, not a job posting.
JOB_LOSS = re.compile(r"\b(cut|cuts|cutting|slash\w*|shed\w*|axe[sd]?|eliminat\w*|los(e|es|ing|t|s))\b"
                      r"|layoff|lays? off|furlough", re.I)


def matches(text: str, aliases: list[str], context: list[str]) -> bool:
    """Alias hit, plus (if configured) a context word to rule out namesakes."""
    t = text.lower()
    if not any(a in t for a in aliases):
        return False
    return not context or any(c in t for c in context)


@dataclass
class NewSignal:
    counterparty_id: str
    url: str
    title: str
    domain: str
    published_at: str | None
    severity: int
    tags: list[str]
    matched_terms: list[str]
    id: int | None = None


def score(text: str, domain: str, pub=None, title: str = "") -> tuple[int, list[str], list[str]]:
    year = pub.year if hasattr(pub, "year") else (int(str(pub)[:4]) if pub else None)
    severity, tags, terms = tag(text, year)
    profile = any(domain == d or domain.endswith("." + d) for d in PROFILE_DOMAINS)
    if (profile or QUOTE_PAGE.search(title or text[:200])) and severity > PROFILE_SEV_CAP:
        severity = PROFILE_SEV_CAP
        tags.append("profile_page")
    if domain == "sec.gov" or domain.endswith(".sec.gov"):
        # A regulatory filing that names our counterparty is never just 'info'.
        tags.append("sec_filing")
        severity = max(severity, 3)
    return severity, tags, terms


ARCHIVE_MIN_SEVERITY = 3  # only look up the Wayback Machine for signals this serious


def score_as_of(title: str, description: str, live_text: str, domain: str, url: str,
                as_of: date | str, archive, pub=None) -> tuple[int, list[str], list[str], str, str]:
    """Replay scoring without hindsight. Returns (severity, tags, terms, basis, text).

    Today's snippets/markdown/highlights may mention later events, so they are never used.
    If the page was archived at/before `as_of`, score title + description + that snapshot
    ('snapshot'); otherwise title + description only ('title_desc').
    """
    base = " ".join([title or "", description or ""])
    if score(live_text, domain, pub, title)[0] >= ARCHIVE_MIN_SEVERITY:
        snap, _, _ = archive.as_of_text(url, as_of)
        if snap:
            text = " ".join([base, snap])
            return (*score(text, domain, pub, title), "snapshot", text)
    return (*score(base, domain, pub, title), "title_desc", base)


def _pub_date(page_age: str | None) -> date | None:
    if not page_age:
        return None
    try:
        return date.fromisoformat(page_age[:10])
    except ValueError:
        return None


class Scout:
    def __init__(self, conn: sqlite3.Connection, client: YouClient | None = None, bus=None):
        self.conn = conn
        self.client = client  # created lazily so --from-db replays need no API key
        self.bus = bus
        self.errors: list[str] = []  # searches / fetches that failed (their results are missing)

    def _client(self) -> YouClient:
        if self.client is None:
            self.client = YouClient()
        return self.client

    def _announce(self, sig_id: int, cp_id: str, severity: int, tags: list[str],
                  url: str, as_of: str) -> None:
        if self.bus is not None:
            self.bus.publish("signals", f"signal-{sig_id}", {
                "id": sig_id, "counterparty": cp_id, "severity": severity, "tags": tags,
                "url": url, "as_of": as_of,
                "summary": f"signal {sig_id} {cp_id} sev{severity} {','.join(tags) or '-'}",
            }, "scout")

    def emit_from_db(self, as_of: date, first_day: bool = False,
                     counterparty_ids: list[str] | None = None) -> int:
        """Zero-cost replay: re-announce stored signals first seen on `as_of` (all earlier
        ones too on the first day) without any You.com calls."""
        op = "<=" if first_day else "="
        rows = self.conn.execute(
            f"SELECT id, counterparty_id, url, severity, tags, first_seen_as_of FROM signals "
            f"WHERE first_seen_as_of {op} ? ORDER BY id", (as_of.isoformat(),)).fetchall()
        rows = [r for r in rows if not counterparty_ids or r["counterparty_id"] in counterparty_ids]
        for r in rows:
            self._announce(r["id"], r["counterparty_id"], r["severity"], json.loads(r["tags"]),
                           r["url"], r["first_seen_as_of"])
        return len(rows)

    def sweep(
        self,
        as_of: date,
        lookback_days: int = 7,
        mode: str = "live",
        counterparty_ids: list[str] | None = None,
        fetch_min_severity: int | None = None,
    ) -> list[NewSignal]:
        start = as_of - timedelta(days=lookback_days)
        freshness = f"{start.isoformat()}to{as_of.isoformat()}"
        cps = self.conn.execute("SELECT * FROM counterparties").fetchall()
        if counterparty_ids:
            cps = [c for c in cps if c["id"] in counterparty_ids]

        new: list[NewSignal] = []
        for cp in cps:
            aliases = [a.lower() for a in json.loads(cp["aliases"])]
            context = [c.lower() for c in json.loads(cp["context"] or "[]")]
            for query in json.loads(cp["queries"]):
                try:
                    results = self._client().search(
                        query, count=20, freshness=freshness, exclude_domains=JUNK_DOMAINS
                    )
                except YouComError as e:
                    # One failed query must not abort a long replay, but it must be visible:
                    # this window has a gap until the sweep is re-run.
                    self.errors.append(f"{as_of} {cp['id']} search {query!r}: {e}")
                    print(f"[scout] WARNING search failed, results missing: {self.errors[-1]}",
                          file=sys.stderr)
                    continue
                kept = 0
                cur = self.conn.execute(
                    "INSERT INTO scout_runs (mode, as_of, counterparty_id, query, freshness, n_results) "
                    "VALUES (?,?,?,?,?,?)",
                    (mode, as_of.isoformat(), cp["id"], query, freshness, len(results)),
                )
                run_id = cur.lastrowid
                for r in results:
                    sig = self._ingest(r, cp["id"], aliases, context, as_of, start, mode, run_id)
                    if sig:
                        kept += 1
                        new.append(sig)
                self.conn.execute("UPDATE scout_runs SET n_kept=? WHERE id=?", (kept, run_id))
                self.conn.commit()

        if fetch_min_severity is not None:
            self._fetch_full_text([s for s in new if s.severity >= fetch_min_severity])
        return new

    def _ingest(self, r, cp_id, aliases, context, as_of, start, mode, run_id) -> NewSignal | None:
        url = r.get("url")
        if not url:
            return None
        snippets = r.get("snippets") or []
        title = r.get("title") or ""
        text = " ".join([title, r.get("description") or "", *snippets])
        if not matches(text, aliases, context):
            return None  # off-target hit (namesake, plant, flag, ...)
        if JOB_POST.search(title) and not JOB_LOSS.search(title):
            return None  # job postings

        pub = _pub_date(r.get("page_age"))
        if mode == "replay" and (pub is None or pub > as_of):
            return None  # no look-ahead in replays
        if pub is not None and pub < start:
            return None  # stale page that slipped past the freshness filter

        domain = urlparse(url).netloc.lower().removeprefix("www.")
        if self.conn.execute("SELECT 1 FROM signals WHERE counterparty_id=? AND url=?",
                             (cp_id, url)).fetchone():
            return None  # already seen in an earlier sweep (skip before any archive lookup)
        self.conn.execute(
            "INSERT OR IGNORE INTO articles (url, title, description, snippets, page_age, domain) "
            "VALUES (?,?,?,?,?,?)",
            (url, title, r.get("description"), json.dumps(snippets), r.get("page_age"), domain),
        )
        basis = "live"
        if mode == "replay":
            severity, tags, terms, basis, _ = score_as_of(
                title, r.get("description") or "", text, domain, url, as_of, self._archive(), pub)
        else:
            severity, tags, terms = score(text, domain, pub, title)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signals (counterparty_id, url, published_at, first_seen_as_of, "
            "run_id, severity, tags, matched_terms, score_basis) VALUES (?,?,?,?,?,?,?,?,?)",
            (cp_id, url, pub.isoformat() if pub else None, as_of.isoformat(), run_id,
             severity, json.dumps(tags), json.dumps(terms), basis),
        )
        if cur.rowcount == 0:
            return None  # already seen in an earlier sweep
        self._announce(cur.lastrowid, cp_id, severity, tags, url, as_of.isoformat())
        return NewSignal(cp_id, url, title, domain, pub.isoformat() if pub else None,
                         severity, tags, terms, cur.lastrowid)

    def _archive(self):
        if getattr(self, "archive", None) is None:
            from ..archive import Archive
            self.archive = Archive(self.conn)
        return self.archive

    def retag(self, as_of_replay: bool = False) -> int:
        """Re-score stored signals with the current rules (no You.com calls).

        Signals collected by a replay are always re-scored on the text that existed on
        their first_seen_as_of (Wayback snapshot, else title + description): scoring them
        on today's snippets would put hindsight back into the replay. Signals from live
        sweeps use today's snippets. as_of_replay forces as-of scoring for every signal."""
        rows = self.conn.execute(
            "SELECT s.id, s.url, s.first_seen_as_of, s.score_basis, r.mode, a.title, "
            "a.description, a.snippets, a.domain, s.published_at "
            "FROM signals s JOIN articles a USING (url) LEFT JOIN scout_runs r ON r.id = s.run_id"
        ).fetchall()
        for r in rows:
            text = " ".join([r["title"] or "", r["description"] or "",
                             *json.loads(r["snippets"] or "[]")])
            replayed = r["mode"] == "replay" or r["score_basis"] in ("snapshot", "title_desc")
            if as_of_replay or replayed:
                severity, tags, terms, basis, _ = score_as_of(
                    r["title"] or "", r["description"] or "", text, r["domain"] or "",
                    r["url"], r["first_seen_as_of"], self._archive(), r["published_at"])
            else:
                severity, tags, terms = score(text, r["domain"] or "", r["published_at"], r["title"] or "")
                basis = "live"
            self.conn.execute(
                "UPDATE signals SET severity=?, tags=?, matched_terms=?, score_basis=? WHERE id=?",
                (severity, json.dumps(tags), json.dumps(terms), basis, r["id"]),
            )
        self.conn.commit()
        return len(rows)

    def _fetch_full_text(self, signals: list[NewSignal]) -> None:
        urls = sorted({s.url for s in signals})
        for i in range(0, len(urls), 10):
            batch = urls[i:i + 10]
            try:
                pages = self._client().contents(batch)
            except YouComError as e:  # full text is optional: keep the signals, report the gap
                self.errors.append(f"contents ({len(batch)} pages): {e}")
                print(f"[scout] WARNING full-text fetch failed: {e}", file=sys.stderr)
                continue
            for n, page in enumerate(pages):
                if not page.get("markdown"):
                    continue
                url = page.get("url")
                if url not in batch and len(pages) == len(batch):
                    url = batch[n]  # the API echoed a normalised/redirected URL
                self.conn.execute(
                    "UPDATE articles SET markdown=?, fetched_at=datetime('now') WHERE url=?",
                    (page["markdown"], url),
                )
        self.conn.commit()
