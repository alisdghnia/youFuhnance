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
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlparse

from ..risk_tags import tag
from ..youcom import YouClient

# Low-signal sites that match company names but carry no credit information.
JUNK_DOMAINS = [
    "trustpilot.com", "seamless.ai", "zoominfo.com", "rocketreach.co", "glassdoor.com",
    "indeed.com", "yelp.com", "bbb.org", "facebook.com", "instagram.com", "tiktok.com",
    "ziprecruiter.com", "simplyhired.com", "wayup.com", "jobs.thetrucker.com", "dnb.com",
]
JOB_POST = re.compile(r"\bhiring\b|\bjobs?\b|\bcareers?\b|\btechnician\b|\bspecialist\b", re.I)


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


def score(text: str, domain: str) -> tuple[int, list[str], list[str]]:
    severity, tags, terms = tag(text)
    if domain.endswith("sec.gov"):
        # A regulatory filing that names our counterparty is never just 'info'.
        tags.append("sec_filing")
        severity = max(severity, 3)
    return severity, tags, terms


ARCHIVE_MIN_SEVERITY = 3  # only look up the Wayback Machine for signals this serious


def score_as_of(title: str, description: str, live_text: str, domain: str, url: str,
                as_of: date | str, archive) -> tuple[int, list[str], list[str], str, str]:
    """Replay scoring without hindsight. Returns (severity, tags, terms, basis, text).

    Today's snippets/markdown/highlights may mention later events, so they are never used.
    If the page was archived at/before `as_of`, score title + description + that snapshot
    ('snapshot'); otherwise title + description only ('title_desc').
    """
    base = " ".join([title or "", description or ""])
    if score(live_text, domain)[0] >= ARCHIVE_MIN_SEVERITY:
        snap, _, _ = archive.as_of_text(url, as_of)
        if snap:
            text = " ".join([base, snap])
            return (*score(text, domain), "snapshot", text)
    return (*score(base, domain), "title_desc", base)


def _pub_date(page_age: str | None) -> date | None:
    if not page_age:
        return None
    try:
        return date.fromisoformat(page_age[:10])
    except ValueError:
        return None


class Scout:
    def __init__(self, conn: sqlite3.Connection, client: YouClient | None = None):
        self.conn = conn
        self.client = client or YouClient()

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
                results = self.client.search(
                    query, count=20, freshness=freshness, exclude_domains=JUNK_DOMAINS
                )
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
        if JOB_POST.search(title):
            return None  # job postings

        pub = _pub_date(r.get("page_age"))
        if mode == "replay" and (pub is None or pub > as_of):
            return None  # no look-ahead in replays
        if pub is not None and pub < start:
            return None  # stale page that slipped past the freshness filter

        domain = urlparse(url).netloc.removeprefix("www.")
        self.conn.execute(
            "INSERT OR IGNORE INTO articles (url, title, description, snippets, page_age, domain) "
            "VALUES (?,?,?,?,?,?)",
            (url, title, r.get("description"), json.dumps(snippets), r.get("page_age"), domain),
        )
        basis = "live"
        if mode == "replay":
            severity, tags, terms, basis, _ = score_as_of(
                title, r.get("description") or "", text, domain, url, as_of, self._archive())
        else:
            severity, tags, terms = score(text, domain)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signals (counterparty_id, url, published_at, first_seen_as_of, "
            "run_id, severity, tags, matched_terms, score_basis) VALUES (?,?,?,?,?,?,?,?,?)",
            (cp_id, url, pub.isoformat() if pub else None, as_of.isoformat(), run_id,
             severity, json.dumps(tags), json.dumps(terms), basis),
        )
        if cur.rowcount == 0:
            return None  # already seen in an earlier sweep
        return NewSignal(cp_id, url, title, domain, pub.isoformat() if pub else None,
                         severity, tags, terms)

    def _archive(self):
        if getattr(self, "archive", None) is None:
            from ..archive import Archive
            self.archive = Archive(self.conn)
        return self.archive

    def retag(self, as_of_replay: bool = False) -> int:
        """Re-score stored signals with the current rules (no You.com calls).

        as_of_replay: score each signal on the text that existed on its first_seen_as_of
        (Wayback snapshot, else title + description) instead of today's snippets."""
        rows = self.conn.execute(
            "SELECT s.id, s.url, s.first_seen_as_of, a.title, a.description, a.snippets, a.domain "
            "FROM signals s JOIN articles a USING (url)"
        ).fetchall()
        for r in rows:
            text = " ".join([r["title"] or "", r["description"] or "",
                             *json.loads(r["snippets"] or "[]")])
            if as_of_replay:
                severity, tags, terms, basis, _ = score_as_of(
                    r["title"] or "", r["description"] or "", text, r["domain"] or "",
                    r["url"], r["first_seen_as_of"], self._archive())
            else:
                severity, tags, terms = score(text, r["domain"] or "")
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
            for page in self.client.contents(urls[i:i + 10]):
                if page.get("markdown"):
                    self.conn.execute(
                        "UPDATE articles SET markdown=?, fetched_at=datetime('now') WHERE url=?",
                        (page["markdown"], page.get("url")),
                    )
        self.conn.commit()
