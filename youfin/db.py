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
    fetched_at  TEXT
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
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
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
