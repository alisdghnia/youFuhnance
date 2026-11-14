"""SQLite store shared by all agents (the team's memory on disk)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS counterparties (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    role      TEXT,              -- originator, servicer, sponsor, lender, obligor
    sector    TEXT,
    aliases   TEXT NOT NULL,     -- JSON list; one must appear in a hit to keep it
    context   TEXT,              -- JSON list; one must also appear (rules out namesakes)
    queries   TEXT NOT NULL      -- JSON list of Scout search queries
);

CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    asset_class     TEXT NOT NULL,   -- ABF, NAV
    facility_type   TEXT,
    collateral      TEXT,
    commitment_usd  REAL,
    hypothetical    INTEGER NOT NULL DEFAULT 1,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS position_counterparties (
    position_id     TEXT REFERENCES positions(id),
    counterparty_id TEXT REFERENCES counterparties(id),
    relationship    TEXT,
    PRIMARY KEY (position_id, counterparty_id)
);

CREATE TABLE IF NOT EXISTS scout_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    mode            TEXT NOT NULL,   -- live | replay
    as_of           TEXT NOT NULL,   -- simulated 'now' (YYYY-MM-DD)
    counterparty_id TEXT,
    query           TEXT,
    freshness       TEXT,
    n_results       INTEGER,
    n_kept          INTEGER
);

CREATE TABLE IF NOT EXISTS articles (
    url         TEXT PRIMARY KEY,
    title       TEXT,
    description TEXT,
    snippets    TEXT,       -- JSON list
    page_age    TEXT,       -- publish timestamp reported by You.com
    domain      TEXT,
    markdown    TEXT,       -- full text via Contents API (high-severity hits)
    fetched_at  TEXT,
    highlights  TEXT        -- JSON list: query-relevant passages (Search highlights)
);

CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    counterparty_id  TEXT NOT NULL REFERENCES counterparties(id),
    url              TEXT NOT NULL REFERENCES articles(url),
    published_at     TEXT,
    first_seen_as_of TEXT NOT NULL,   -- the date the agent would have seen it
    run_id           INTEGER REFERENCES scout_runs(id),
    severity         INTEGER NOT NULL, -- 0 info .. 5 critical
    tags             TEXT NOT NULL,    -- JSON list of risk categories
    matched_terms    TEXT NOT NULL,    -- JSON list: the receipt for each tag
    status           TEXT NOT NULL DEFAULT 'raw',  -- raw | verified | dismissed
    UNIQUE (counterparty_id, url)
);
CREATE INDEX IF NOT EXISTS idx_signals_cp ON signals(counterparty_id, published_at);

-- Verifier: one event = one real-world development, however many articles cover it.
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    counterparty_id     TEXT NOT NULL REFERENCES counterparties(id),
    primary_tag         TEXT NOT NULL,
    headline            TEXT NOT NULL,
    first_published     TEXT NOT NULL,
    first_seen_as_of    TEXT NOT NULL,
    severity            INTEGER NOT NULL,
    tags                TEXT NOT NULL,          -- JSON list
    verdict             TEXT NOT NULL DEFAULT 'single_source',
    confidence          REAL NOT NULL DEFAULT 0.3,
    n_publishers        INTEGER NOT NULL DEFAULT 0,
    best_tier           INTEGER,
    alert_level         TEXT NOT NULL DEFAULT 'log',  -- log | watch | ALERT
    alerted_as_of       TEXT,                   -- first date it became ALERT
    watch_as_of         TEXT,                   -- first date it became watch
    verify_attempts     INTEGER NOT NULL DEFAULT 0,
    last_verified_as_of TEXT
);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id     INTEGER NOT NULL REFERENCES events(id),
    url          TEXT NOT NULL REFERENCES articles(url),
    domain       TEXT NOT NULL,
    publisher    TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    role         TEXT NOT NULL,     -- scout | corroboration
    published_at TEXT,
    quote        TEXT,              -- the receipt: sentence supporting the claim
    PRIMARY KEY (event_id, url)
);

CREATE TABLE IF NOT EXISTS event_verifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id),
    as_of         TEXT NOT NULL,
    query         TEXT NOT NULL,
    freshness     TEXT NOT NULL,
    n_results     INTEGER NOT NULL,
    n_supporting  INTEGER NOT NULL,
    verdict_after TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
    if "event_id" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN event_id INTEGER REFERENCES events(id)")
        conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    if "highlights" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN highlights TEXT")  # JSON list
        conn.commit()


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def load_portfolio(conn: sqlite3.Connection, portfolio: dict) -> None:
    """Upsert counterparties and positions from portfolio.json."""
    with conn:
        for cp in portfolio["counterparties"]:
            conn.execute(
                "INSERT OR REPLACE INTO counterparties VALUES (?,?,?,?,?,?,?)",
                (cp["id"], cp["name"], cp.get("role"), cp.get("sector"),
                 json.dumps(cp["aliases"]), json.dumps(cp.get("context", [])),
                 json.dumps(cp["queries"])),
            )
        for p in portfolio["positions"]:
            conn.execute(
                "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?)",
                (p["id"], p["name"], p["asset_class"], p.get("facility_type"),
                 p.get("collateral"), p.get("commitment_usd"),
                 int(p.get("hypothetical", True)), p.get("notes")),
            )
            for link in p["counterparties"]:
                conn.execute(
                    "INSERT OR REPLACE INTO position_counterparties VALUES (?,?,?)",
                    (p["id"], link["id"], link.get("relationship")),
                )
