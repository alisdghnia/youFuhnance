"""Internet Archive lookups: what did this page say on a given date?

Replays must not score today's page text (which may link to or mention later events).
`as_of_text` returns the last Wayback snapshot at/before the replay date, reduced to
readable text. Every lookup is cached in SQLite; requests are sequential and polite.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser

CDX = "https://web.archive.org/cdx/search/cdx"
RAW = "https://web.archive.org/web/{ts}id_/{url}"
UA = "youfin-replay/0.1 (research; wayback as-of lookups)"
MIN_GAP = 1.0          # seconds between requests
MAX_RETRIES = 3
TIMEOUT = 30
MAX_TEXT = 20000
MAX_CONSECUTIVE_FAILURES = 3   # then stop asking archive.org for the rest of the run

SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_snapshots (
    url         TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    snapshot_ts TEXT,
    status      TEXT NOT NULL,   -- ok | no_snapshot | error
    text        TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (url, as_of)
);
"""

_SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "header", "form", "svg"}


class _Extract(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: list[str] = []
        self.paras: list[str] = []
        self._in_title = False
        self._in_p = False
        self._skip = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "twitter:description") and a.get("content"):
                self.meta.append(a["content"])
        elif tag in _SKIP:
            self._skip += 1
        elif tag in ("p", "h1", "h2", "li") and not self._skip:
            self._flush()  # an unclosed <p>/<li> must not lose the text before it
            self._in_p = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("p", "h1", "h2", "li") and self._in_p:
            self._flush()
            self._in_p = False

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if len(t) >= 40:
            self.paras.append(t)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_p and not self._skip:
            self._buf.append(data)


def extract_text(raw_html: str) -> str:
    p = _Extract()
    try:
        p.feed(raw_html)
        p.close()
        p._flush()
    except Exception:  # malformed markup: keep what we parsed
        pass
    parts = [re.sub(r"\s+", " ", html.unescape(p.title)).strip(), *p.meta, *p.paras]
    return " ".join(x for x in parts if x)[:MAX_TEXT]


class LookAhead(RuntimeError):
    """Wayback offered a capture taken after the replay date."""


class Archive:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(SCHEMA)
        self.requests = 0
        self.lookups = 0       # uncached lookups performed
        self.cache_hits = 0
        self._last = 0.0
        self._failures = 0     # consecutive failed lookups
        self.down = False      # circuit breaker: archive.org unreachable, stop trying

    def _get(self, url: str, not_after: str | None = None) -> str:
        """GET with polite spacing and retries. `not_after` (YYYYMMDD): refuse a response
        that Wayback served from a later capture (it redirects to the nearest one)."""
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    served = re.search(r"/web/(\d{8})\d*", resp.geturl() or "")
                    if not_after and served and served.group(1) > not_after:
                        raise LookAhead(f"served capture {served.group(1)} > {not_after}")
                    return resp.read(3_000_000).decode(resp.headers.get_content_charset() or "utf-8",
                                                       "replace")
            except urllib.error.HTTPError as e:
                if (e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES
                        and not self._failures):
                    time.sleep(5 * 2 ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < MAX_RETRIES and not self._failures:  # already failing: fail fast
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise
        raise RuntimeError("unreachable")

    def as_of_text(self, url: str, as_of: date | str) -> tuple[str | None, str | None, str]:
        """(text, snapshot_timestamp, status); status is ok | no_snapshot | error."""
        as_of = as_of if isinstance(as_of, str) else as_of.isoformat()
        row = self.conn.execute("SELECT snapshot_ts, status, text FROM archive_snapshots "
                                "WHERE url=? AND as_of=?", (url, as_of)).fetchone()
        if not row or row["status"] == "error":
            # An answer cached for a LATER date also answers this one when its snapshot is
            # not after `as_of` (it is still the last one), or when nothing existed at all.
            day = as_of.replace("-", "")
            row = self.conn.execute(
                "SELECT snapshot_ts, status, text FROM archive_snapshots WHERE url=? AND as_of>? "
                "AND ((status='ok' AND substr(snapshot_ts,1,8)<=?) OR "
                "(status='no_snapshot' AND snapshot_ts IS NULL)) ORDER BY as_of LIMIT 1",
                (url, as_of, day)).fetchone()
        if row and row["status"] != "error":
            self.cache_hits += 1
            return row["text"], row["snapshot_ts"], row["status"]
        if self.down:
            return None, None, "error"  # not cached: retried on the next run
        self.lookups += 1
        ts, text, status = None, None, "no_snapshot"
        try:
            q = urllib.parse.urlencode({
                "url": url, "to": as_of.replace("-", ""), "output": "json", "limit": "-1",
                "filter": "statuscode:200", "fl": "timestamp,original"})
            rows = json.loads(self._get(f"{CDX}?{q}") or "[]")
            if len(rows) > 1:  # row 0 is the header
                ts, original = rows[-1]
                if ts[:8] > as_of.replace("-", ""):
                    raise LookAhead(f"CDX returned {ts} for to={as_of}")
                text = extract_text(self._get(RAW.format(ts=ts, url=original),
                                              not_after=as_of.replace("-", "")))
                status = "ok" if text else "no_snapshot"
            self._failures = 0
        except LookAhead:
            ts, text, status = None, None, "error"  # never score a later capture
        except Exception:
            ts, text, status = None, None, "error"
            self._failures += 1
            if self._failures >= MAX_CONSECUTIVE_FAILURES and not self.down:
                self.down = True
                print(f"[archive] archive.org unreachable after {self._failures} failed lookups; "
                      "scoring the rest of this run on title + description only", file=sys.stderr)
        self.conn.execute("INSERT OR REPLACE INTO archive_snapshots (url, as_of, snapshot_ts, status, "
                          "text) VALUES (?,?,?,?,?)", (url, as_of, ts, status, text))
        self.conn.commit()
        return text, ts, status
