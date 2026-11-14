"""Message bus for the agent team (Scout -> Verifier -> humans).

Local backend (always on): SQLite tables, fast, one row per message, per-agent cursors.
Mirror backend (YOUFIN_BUS=mirror): every message still goes through the local bus, and
COARSE topics (events, alerts, brief) are additionally posted to the OpenCrawl board via
the `opencrawl` CLI. The demo never depends on the network: on the first CLI failure or
timeout the mirror switches itself off (logged once) and the run continues locally.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid

log = logging.getLogger("youfin.bus")

TOPICS = ("signals", "events", "alerts", "brief")
MIRROR_TOPICS = ("events", "alerts", "brief", "actions")     # raw signals stay local
BOARD_PREFIX = "youfin-"
BOARD_API = os.environ.get("YOUFIN_BOARD_API", "https://app.getopencrawl.com")
CLI_TIMEOUT_S = 20
MAX_TEXT = 1500
AGENT_TASKS = {
    "scout": "YouFin Scout: sweeps the web for credit-risk news on portfolio counterparties",
    "verifier": "YouFin Verifier: clusters signals into events, corroborates them, raises alerts",
    "market": "YouFin Market desk: checks whether public proxies already price an alert",
    "covenant": "YouFin Covenant desk: maps verified alerts to contract triggers and lender actions",
    "summarizer": "YouFin Summarizer: writes the daily executive summary from the other agents' board posts",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bus_messages (
    id TEXT PRIMARY KEY, topic TEXT NOT NULL, sender TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS bus_subs (
    agent TEXT NOT NULL, topic TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, topic));
CREATE TABLE IF NOT EXISTS bus_agents (
    agent TEXT PRIMARY KEY, session_id TEXT NOT NULL, board_name TEXT NOT NULL,
    registered_at TEXT, left_at TEXT);
CREATE TABLE IF NOT EXISTS bus_mirror (
    msg_id TEXT PRIMARY KEY, topic TEXT, ok INTEGER, secs REAL,
    posted_at TEXT NOT NULL DEFAULT (datetime('now')));
"""


def board_cli(*args: str, timeout: float = CLI_TIMEOUT_S) -> dict:
    """Run `opencrawl board <args> --json`; returns the parsed `data` dict or raises."""
    exe = shutil.which("opencrawl")
    if not exe:
        raise RuntimeError("opencrawl CLI not found")
    out = subprocess.run([exe, "board", *args, "--json", "--api", BOARD_API],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "failed").strip()[:200])
    return json.loads(out.stdout).get("data", {})


class MessageRejected(RuntimeError):
    """The board refused this one message in every layout (not a network failure)."""


class Bus:
    def __init__(self, conn: sqlite3.Connection, backend: str | None = None):
        self.conn = conn
        self.backend = (backend or os.environ.get("YOUFIN_BUS", "local")).lower()
        self.mirror = self.backend == "mirror"
        self.max_posts = int(os.environ.get("YOUFIN_BUS_MAX_POSTS", "30"))
        self.mirror_error: str | None = None
        self._variant = 0                    # layout used by the last successful post
        self._last_error: str | None = None  # board error text from the last rejected layout
        self.transcript: list[dict] = []     # messages published in this process
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bus_mirror)")}
        for col, typ in (("variant", "INTEGER"), ("error", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE bus_mirror ADD COLUMN {col} {typ}")
        conn.commit()

    # ------------------------------------------------------------------ local
    def subscribe(self, agent: str, topics: list[str] | tuple[str, ...]) -> None:
        for t in topics:
            # New subscribers start at "now": they never see stale history.
            top = self.conn.execute("SELECT COALESCE(MAX(rowid),0) FROM bus_messages").fetchone()[0]
            self.conn.execute("INSERT OR IGNORE INTO bus_subs (agent, topic, cursor) VALUES (?,?,?)",
                              (agent, t, top))
        self.conn.commit()

    def publish(self, topic: str, msg_id: str, payload: dict, sender: str) -> bool:
        """Idempotent on msg_id. Returns True if the message was new."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO bus_messages (id, topic, sender, payload) VALUES (?,?,?,?)",
            (msg_id, topic, sender, json.dumps(payload, separators=(",", ":"))))
        self.conn.commit()
        if cur.rowcount == 0:
            return False
        self.transcript.append({"id": msg_id, "topic": topic, "sender": sender, "payload": payload})
        if self.mirror and topic in MIRROR_TOPICS:
            self._mirror(topic, msg_id, payload, sender)
        return True

    def poll(self, agent: str, limit: int = 1000) -> list[dict]:
        subs = self.conn.execute("SELECT topic, cursor FROM bus_subs WHERE agent=?",
                                 (agent,)).fetchall()
        if not subs:
            return []
        cursors = {s["topic"]: s["cursor"] for s in subs}
        lo = min(cursors.values())
        marks = ",".join("?" * len(cursors))
        rows = self.conn.execute(
            f"SELECT rowid AS rid, * FROM bus_messages WHERE rowid > ? AND topic IN ({marks}) "
            "ORDER BY rowid", (lo, *cursors)).fetchall()
        out = []
        for r in rows:
            if r["rid"] <= cursors[r["topic"]]:
                continue
            if len(out) >= limit:
                break
            out.append({"id": r["id"], "topic": r["topic"], "sender": r["sender"],
                        "payload": json.loads(r["payload"]), "created_at": r["created_at"]})
            self.conn.execute("UPDATE bus_subs SET cursor=? WHERE agent=? AND topic=?",
                              (r["rid"], agent, r["topic"]))
        self.conn.commit()
        return out

    def clear(self) -> None:
        """Drop local messages/cursors (board identities and mirror log are kept)."""
        self.conn.execute("DELETE FROM bus_messages")
        self.conn.execute("DELETE FROM bus_subs")
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT topic, COUNT(*) FROM bus_messages GROUP BY topic")}

    # ----------------------------------------------------------------- mirror
    def _disable(self, why: str) -> None:
        if self.mirror:
            log.warning("OpenCrawl mirror disabled, continuing locally: %s", why)
            print(f"[bus] OpenCrawl mirror disabled, continuing locally: {why}")
        self.mirror, self.mirror_error = False, why

    def _session(self, agent: str) -> str | None:
        """Register the agent's board identity once (persisted); returns the session id."""
        row = self.conn.execute("SELECT * FROM bus_agents WHERE agent=?", (agent,)).fetchone()
        if row and row["registered_at"] and not row["left_at"]:
            return row["session_id"]
        sid = row["session_id"] if row else f"youfin-{agent}-{uuid.uuid4().hex[:8]}"
        name = f"youfin:{agent}"
        board_cli("register", "--session", sid, "--name", name, "--task",
                  AGENT_TASKS.get(agent, f"YouFin {agent}"), "--harness", "other")
        topics = [BOARD_PREFIX + t for t in MIRROR_TOPICS]
        args = ["subscribe", "--session", sid]
        for t in topics:
            args += ["--topic", t]
        board_cli(*args)
        self.conn.execute(
            "INSERT OR REPLACE INTO bus_agents (agent, session_id, board_name, registered_at, left_at) "
            "VALUES (?,?,?,datetime('now'),NULL)", (agent, sid, name))
        self.conn.commit()
        return sid

    def _mirror(self, topic: str, msg_id: str, payload: dict, sender: str) -> None:
        posted = self.conn.execute("SELECT COUNT(*) FROM bus_mirror WHERE ok=1").fetchone()[0]
        if posted >= self.max_posts:
            self._disable(f"post cap {self.max_posts} reached")
            return
        t0 = time.time()
        try:
            sid = self._session(sender)
            if not self._publish_board(sid, msg_id, topic, sender, payload):
                return  # already on the board from an earlier run: keep that mirror record
        except MessageRejected as e:  # skip this message only; mirror stays on
            log.warning("board rejected %s: %s", msg_id, e)
            self.conn.execute(
                "INSERT OR REPLACE INTO bus_mirror (msg_id, topic, ok, secs, variant, error) "
                "VALUES (?,?,0,?,NULL,?)", (msg_id, topic, round(time.time() - t0, 2), str(e)[:500]))
            self.conn.commit()
            return
        except Exception as e:  # network/CLI failure must never break the run
            self._disable(f"{type(e).__name__}: {e}")
            return
        # `error` keeps the rejection text of any richer layout that was refused first,
        # so the cause of the board's INVALID_USAGE can be diagnosed later.
        self.conn.execute(
            "INSERT OR REPLACE INTO bus_mirror (msg_id, topic, ok, secs, variant, error) "
            "VALUES (?,?,1,?,?,?)",
            (msg_id, topic, round(time.time() - t0, 2), self._variant, self._last_error))
        self.conn.commit()

    def _publish_board(self, sid: str, msg_id: str, topic: str, sender: str, payload: dict) -> bool:
        """Try text layouts from richest to plainest for EVERY message (the board rejected some
        bodies with INVALID_USAGE, cause unknown); a fallback for one message must not strip
        the readable summary from later ones. Returns False if the post id already existed."""
        board_id = re.sub(r"[^A-Za-z0-9.-]", "-", msg_id)
        variants = render_variants(topic, sender, payload)
        last: Exception | None = None
        self._last_error = None
        for i in range(len(variants)):
            try:
                board_cli("publish", "--session", sid, "--id", board_id,
                          "--topic", BOARD_PREFIX + topic, "--text", variants[i])
                self._variant = i
                self._last_error = str(last)[:500] if last else None
                return True
            except RuntimeError as e:
                if "BOARD_CONFLICT" in str(e):
                    return False                 # this post id is already on the board
                if "INVALID_USAGE" not in str(e):
                    raise
                last = e
        raise MessageRejected(str(last))

    def leave(self) -> list[tuple[str, str]]:
        done = []
        for r in self.conn.execute("SELECT * FROM bus_agents WHERE left_at IS NULL").fetchall():
            try:
                board_cli("leave", "--session", r["session_id"])
                self.conn.execute("UPDATE bus_agents SET left_at=datetime('now') WHERE agent=?",
                                  (r["agent"],))
                done.append((r["board_name"], "left"))
            except Exception as e:
                done.append((r["board_name"], f"failed: {e}"))
        self.conn.commit()
        return done

    def mirror_stats(self) -> dict:
        r = self.conn.execute("SELECT COUNT(*), AVG(secs), MAX(secs) FROM bus_mirror WHERE ok=1").fetchone()
        return {"posts": r[0], "avg_publish_s": round(r[1] or 0, 2), "max_publish_s": r[2] or 0}


def render_variants(topic: str, sender: str, p: dict) -> list[str]:
    """Text layouts, each under MAX_TEXT chars: readable line + JSON, JSON with summary, JSON."""
    head = (p.get("summary") or f"{topic} from {sender}")[:200]
    body = {k: v for k, v in p.items() if k != "summary"}

    def fit(make, quote: bool = True) -> str:
        text = ""
        for limit in (300, 160, 80, 0):
            b = dict(body)
            if b.get("quote"):
                b["quote"] = _ascii(b["quote"])[:limit] if quote else ""
            if not quote:
                b.pop("quote", None)
            if b.get("headline"):
                b["headline"] = _ascii(b["headline"])[:max(limit, 80)]
            text = make(json.dumps(b, separators=(",", ":"), ensure_ascii=True))
            if len(text) < MAX_TEXT:
                return text
        return text[:MAX_TEXT - 1]

    head = _ascii(head)
    return [
        fit(lambda j: f"youfin {sender} - {head}\n{j}"),
        fit(lambda j: json.dumps({"agent": f"youfin:{sender}", "summary": head,
                                  **json.loads(j)}, separators=(",", ":"))),
        # The board refuses some bodies (content filter?); fall back to text without the quote.
        fit(lambda j: f"youfin {sender} - {head}\n{j}", quote=False),
        fit(lambda j: j, quote=False),
    ]


def _ascii(t: str) -> str:
    return re.sub(r"\s+", " ", t.encode("ascii", "ignore").decode()).strip()


def render_text(topic: str, sender: str, p: dict) -> str:
    return render_variants(topic, sender, p)[0]
