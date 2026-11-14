"""Summarizer agent (`youfin:summarizer`): writes the dashboard's daily Executive summary with the
local LLM, grounded ONLY in messages the youfin runtime agents posted on the OpenCrawl board.

No hindsight, by construction:
  * context = board messages on `youfin-*` topics by `youfin:*` runtime agents (scout/verifier/market/
    covenant, never the summarizer itself), kept only if the `as_of` INSIDE the payload is <= the day
    (post time is ignored). OpenCrawl workspace search over coding sessions/history is never used.
  * if the board is unreachable the identical messages come from the local bus.
  * fact guard: every number, ticker/acronym, company name, date and domain in the LLM text must occur
    in the input messages (or be the counterparty name / the day itself); otherwise the text is
    discarded and the rule-based summary is used. The outcome is stored in `exec_summaries`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request

from .. import llm
from ..bus import BOARD_API, BOARD_PREFIX, Bus, render_text

AGENT = "summarizer"
WORKSPACE = "org_01M7DCEDB4KPEXR0TPBEBZECMR"
RUNTIME_AGENTS = ("scout", "verifier", "market", "covenant")      # who may inform a summary
CONTEXT_TOPICS = ("events", "alerts", "actions", "brief")
MAX_MSGS = 16
MAX_WORDS = 20
FIELDS = ("headline", "news", "verifier", "market", "covenant", "team")

SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_summaries (
    counterparty_id TEXT NOT NULL, as_of TEXT NOT NULL,
    source TEXT NOT NULL,            -- llm (passed the guard) | rule (fallback)
    summary TEXT,                    -- JSON of the LLM text (kept even when rejected, for audit)
    n_msgs INTEGER NOT NULL, context_src TEXT, model TEXT, reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (counterparty_id, as_of));
"""


# ----------------------------------------------------------------------------- context
def _parse(text: str) -> dict | None:
    i = text.find("{")
    if i < 0:
        return None
    try:
        p = json.loads(text[i:])
    except ValueError:
        return None
    return p if isinstance(p, dict) else None


def _msg(src: str, topic: str, sender: str, text: str, payload: dict | None = None) -> dict | None:
    p = payload if payload is not None else _parse(text)
    if not p or not p.get("as_of"):
        return None
    return {"src": src, "topic": topic, "sender": sender, "text": text, "payload": p, "as_of": str(p["as_of"])[:10]}


class BoardReader:
    """Reads the youfin runtime agents' posts from the board (`board fetch message:N`, batched)."""

    def __init__(self, workspace: str = WORKSPACE):
        self.workspace, self._cache = workspace, None
        self.error: str | None = None

    def _fetch(self, lo: int, hi: int) -> list[dict]:
        exe = shutil.which("opencrawl")
        if not exe:
            raise RuntimeError("opencrawl CLI not found")
        refs = [f"message:{n}" for n in range(lo, hi)]
        out = subprocess.run([exe, "board", "fetch", *refs, "--workspace", self.workspace, "--json",
                              "--api", BOARD_API], capture_output=True, text=True, timeout=60)
        if not out.stdout.strip():
            raise RuntimeError((out.stderr or "empty reply").strip()[:200])
        d = json.loads(out.stdout)
        if "data" not in d:
            raise RuntimeError(str(d)[:200])
        return d["data"].get("results", [])

    def messages(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        msgs, n = [], 1
        while True:
            res = self._fetch(n, n + 40)
            hit = 0
            for r in res:
                if "data" not in r:
                    continue
                hit += 1
                m = r["data"]["message"]
                name, topic = str(m.get("name") or ""), str(m.get("topic") or "")
                sender = name.removeprefix("youfin:")
                if not name.startswith("youfin:") or sender not in RUNTIME_AGENTS \
                        or not topic.startswith(BOARD_PREFIX):
                    continue                      # coding sessions / other agents: never read
                t = topic.removeprefix(BOARD_PREFIX)
                if t not in CONTEXT_TOPICS or r["data"].get("nextOffset"):
                    continue
                x = _msg("board", t, sender, r["data"].get("text") or "")
                if x:
                    x["ref"] = r["id"]
                    msgs.append(x)
            if not hit:
                break
            n += 40
        self._cache = msgs
        return msgs


def local_messages(conn: sqlite3.Connection) -> list[dict]:
    """The same messages from the local bus, rendered exactly as they are posted to the board."""
    msgs = []
    for r in conn.execute("SELECT id, topic, sender, payload FROM bus_messages "
                          "WHERE topic IN (?,?,?,?) ORDER BY rowid", CONTEXT_TOPICS):
        if r["sender"] not in RUNTIME_AGENTS:
            continue
        p = json.loads(r["payload"])
        x = _msg("local", r["topic"], r["sender"], render_text(r["topic"], r["sender"], p), p)
        if x:
            x["ref"] = r["id"]
            msgs.append(x)
    return msgs


def _first_seen(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, first_seen_as_of FROM events")}


def _consistent(m: dict, seen: dict[int, str]) -> bool:
    """A message must not claim an event before the day the event was first seen (guards against
    mis-stamped posts: a 2026-11-01 post about an event first seen on 2026-11-14 is ignored)."""
    p = m["payload"]
    ids = [p["event_id"]] if isinstance(p.get("event_id"), int) else []
    ids += [i for i in (p.get("alert_event_ids") or []) if isinstance(i, int)]
    return all(seen.get(i, "9999") <= m["as_of"] for i in ids)   # unknown event: not usable


def select_context(msgs: list[dict], cp: str, day: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Strictly by the payload's as_of <= day; this counterparty (or team-level briefs); compact."""
    seen = _first_seen(conn) if conn is not None else {}
    keep = [m for m in msgs if m["as_of"] <= day and m["payload"].get("counterparty") == cp
            and _consistent(m, seen)]       # team-level briefs (no counterparty) carry only counts: skipped
    latest: dict = {}
    for m in sorted(keep, key=lambda m: (m["as_of"], m["topic"], m["text"])):
        p = m["payload"]
        if m["topic"] == "brief":
            key = ("brief", m["as_of"])
        elif m["topic"] == "actions":
            key = ("actions", m["sender"], p.get("position") or p.get("event_id") or m["text"])
        else:
            key = (m["topic"], p.get("event_id") or m["text"])
        latest[key] = m                          # newest state of each event / position wins
    out = sorted(latest.values(), key=lambda m: (m["as_of"], m["topic"], m["text"]))
    return out[-MAX_MSGS:]


def gather(conn: sqlite3.Connection, cp: str, day: str, reader: BoardReader | None = None,
           use_board: bool = True) -> tuple[list[dict], str]:
    """(context messages, 'board' | 'board+local' | 'local'). Board first. Messages the board refused
    (it rejects every `actions` post) or an unreachable board come from the local bus: same content."""
    local = local_messages(conn)
    msgs, src = local, "local"
    if use_board and reader is not None:
        try:
            board = reader.messages()
            seen = {(m["topic"], m["sender"], m["text"]) for m in board}
            extra = [m for m in local if (m["topic"], m["sender"], m["text"]) not in seen]
            msgs, src = board + extra, "board+local" if extra else "board"
        except Exception as e:
            reader.error = f"{type(e).__name__}: {e}"
    ctx = select_context(msgs, cp, day, conn)
    if src == "board+local" and not any(m["src"] == "local" for m in ctx):
        src = "board"
    return ctx, src


# ----------------------------------------------------------------------------- fact guard
_NUM_WORDS = r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|hundred|thousand|million|billion"
_STARTERS = {w.lower() for w in (
    "The A An No New Standing Single Both All Open Verified Confirmed Corroborated Weakly Not Nothing "
    "None Several Multiple Market Verifier Covenant Team News Alert Watch Log Stage Position Positions "
    "Event Events Source Sources Desk Board Messages Hedge Hedges Basket Window Awaiting Pending Review "
    "Consider Discuss Whether Should Is Are Was Has Have Based Per One Another Likely Possible Unclear "
    "Quiet Elevated Calm Stress What Why How Which When Where Who Any Recent Latest Current Currently "
    "Newest Newly Open Closed Active Only Still Given Since After Before With Without Does Do Did Can Could "
    "Will Would May Might Need Needs Should Confirm Check Assess Monitor Flag Escalate Ready High Low Strong "
    "Weak Mixed Early Late Single Dual Triple Second First Third Last Next Prior Fresh Further More Less Most "
    "Many Few Some Each Every This That These Those It Its Their There Here").split()}


def _norm(t: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3})", "", t).casefold()


def guard(fields: dict, inputs: list[str], allowed: list[str], day: str) -> list[str]:
    """Problems found in the LLM text ([] = every fact is grounded in the input messages)."""
    corpus = _norm("\n".join(inputs) + "\n" + "\n".join(allowed))
    probs: list[str] = []
    for k in FIELDS:
        v = fields.get(k)
        if not isinstance(v, str) or not v.strip():
            probs.append(f"{k}: empty")
            continue
        if len(v.split()) > MAX_WORDS:
            probs.append(f"{k}: {len(v.split())} words > {MAX_WORDS}")
        t = re.sub(r"(?<=\d),(?=\d{3})", "", v)
        for d in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", t):
            if d > day:
                probs.append(f"{k}: date {d} after {day}")
        for n in re.findall(r"\d+(?:\.\d+)?", t):
            if n not in corpus:
                probs.append(f"{k}: number {n} not in inputs")
        for w in re.findall(rf"\b(?:{_NUM_WORDS})\b", t, flags=re.I):
            if w.casefold() not in corpus:
                probs.append(f"{k}: number word {w} not in inputs")
        for dom in re.findall(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", t, flags=re.I):
            if dom.casefold() not in corpus:
                probs.append(f"{k}: domain {dom} not in inputs")
        for tk in re.findall(r"\b[A-Z][A-Z0-9&]+\b", t):
            if not tk.isdigit() and tk.casefold() not in corpus:
                probs.append(f"{k}: ticker/acronym {tk} not in inputs")
        for m in re.finditer(r"\b[A-Z][a-z][A-Za-z&-]*\b", t):     # possessive 's is not part of the name
            w = m.group(0).casefold()
            if w in corpus or w in _STARTERS:
                continue
            probs.append(f"{k}: name '{m.group(0)}' not in inputs")
        for m in re.finditer(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|Jun(?:e)?|Jul(?:y)?|"
                             r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b", t):
            probs.append(f"{k}: calendar date wording '{m.group(0)}' (use ISO dates from the messages)")
    return list(dict.fromkeys(probs))


# ----------------------------------------------------------------------------- LLM
_SYSTEM = (
    "You write the daily executive summary for a lender's credit team about ONE counterparty, using ONLY "
    "the numbered board messages provided (posted by the Scout, Verifier, Market and Covenant agents). "
    "Return JSON with string fields headline, news, verifier, market, covenant, team. Each field is at most "
    "20 words. Rules: every number, ticker, company name, date and website domain you write must be copied "
    "exactly from the messages; write dates as YYYY-MM-DD as they appear; never add outside knowledge, "
    "never predict outcomes, never mention anything not in the messages. If the messages say nothing for a "
    "field write 'no board message on this'. news = newest verified item with its source domain; verifier = "
    "alert level and verdict; market = market desk read; covenant = covenant desk stage/triggers; team = one "
    "short open question for the deal team. Plain words, no markdown.")
_FORMAT = {"type": "object", "required": list(FIELDS), "properties": {k: {"type": "string"} for k in FIELDS}}


def build_prompt(name: str, day: str, ctx: list[dict]) -> str:
    lines = [f"[{i}] {m['topic']} / youfin:{m['sender']}: {m['text']}" for i, m in enumerate(ctx, 1)]
    return f"Counterparty: {name}\nDay: {day}\n\nBoard messages:\n" + "\n".join(lines) + "\n\nWrite the summary."


def _chat(conn: sqlite3.Connection, user: str, model: str) -> dict:
    conn.executescript("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, response TEXT);")
    key = hashlib.sha256(f"{model}\nsummarizer\n{_SYSTEM}\n{user}".encode()).hexdigest()
    row = conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    payload = {"model": model, "stream": False, "format": _FORMAT, "options": {"temperature": 0},
               "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]}
    if model.startswith(llm.THINKING_MODELS):
        payload["think"] = False
    req = urllib.request.Request(f"{llm.HOST}/api/chat", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=llm.TIMEOUT * 2) as r:
        out = json.loads(json.loads(r.read())["message"]["content"])
    out = {k: str(out.get(k, "")).strip() for k in FIELDS}
    conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?)", (key, json.dumps(out)))
    conn.commit()
    return out


# ----------------------------------------------------------------------------- agent
class Summarizer:
    def __init__(self, conn: sqlite3.Connection, bus: Bus | None = None, model: str = llm.MODEL,
                 use_board: bool = True):
        self.conn, self.bus, self.model = conn, bus, model
        self.reader = BoardReader() if use_board else None
        self.use_board = use_board
        conn.executescript(SCHEMA)
        conn.commit()

    def summarize(self, cp: str, name: str, day: str) -> dict:
        """Write + guard + store (+ publish to `brief`) the summary for one day. Returns the stored row."""
        ctx, src = gather(self.conn, cp, day, self.reader, self.use_board)
        source, reason, fields = "rule", "", None
        if not ctx:
            reason = "no board messages dated <= day"
        else:
            inputs = [m["text"] for m in ctx]
            allowed = [name, day]
            try:
                prompt = build_prompt(name, day, ctx)
                fields = _chat(self.conn, prompt, self.model)
                probs = guard(fields, inputs, allowed, day)
                if probs:   # one retry that names the violations
                    fields2 = _chat(self.conn, prompt + "\n\nYour previous attempt was rejected: "
                                    + "; ".join(probs[:6]) + ". Rewrite using only facts copied from the messages.",
                                    self.model)
                    probs2 = guard(fields2, inputs, allowed, day)
                    if len(probs2) <= len(probs):
                        fields, probs = fields2, probs2
                if probs:
                    reason = "guard: " + "; ".join(probs[:4])
                else:
                    source = "llm"
            except Exception as e:
                reason = f"llm unavailable: {type(e).__name__}: {e}"[:200]
        row = {"counterparty_id": cp, "as_of": day, "source": source,
               "summary": json.dumps(fields) if fields else None, "n_msgs": len(ctx),
               "context_src": src, "model": self.model, "reason": reason}
        self.conn.execute(
            "INSERT OR REPLACE INTO exec_summaries (counterparty_id, as_of, source, summary, n_msgs, "
            "context_src, model, reason) VALUES (:counterparty_id,:as_of,:source,:summary,:n_msgs,"
            ":context_src,:model,:reason)", row)
        self.conn.commit()
        if self.bus is not None:
            mid = f"summary-{cp}-{day}"
            self.conn.execute("DELETE FROM bus_messages WHERE id=?", (mid,))   # re-runs republish
            body = {"as_of": day, "counterparty": cp, "kind": "exec_summary", "source": source,
                    "n_msgs": len(ctx)}
            if source == "llm":
                body["lines"] = fields
            head = (fields["headline"] if source == "llm" else f"{name} {day}: rule-based summary used")
            body["summary"] = head
            self.bus.publish("brief", mid, body, AGENT)
        return row


def stored(conn: sqlite3.Connection, cp: str, day: str) -> dict | None:
    """The stored summary row for display (None when no row or the table does not exist)."""
    try:
        r = conn.execute("SELECT * FROM exec_summaries WHERE counterparty_id=? AND as_of=?", (cp, day)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(r) if r else None


def apply_to_rule_summary(conn: sqlite3.Connection, cp: str, day: str, rule: dict) -> dict:
    """LLM text when it passed the guard, else the rule-based summary unchanged."""
    r = stored(conn, cp, day)
    if r and r["source"] == "llm" and r["summary"]:
        out = json.loads(r["summary"])
        return {**{k: out[k] for k in FIELDS}, "llm": r["n_msgs"]}
    return rule


def verify_stored(conn: sqlite3.Connection, cp: str, name: str, day: str, ctx: list[dict]) -> list[str]:
    """Re-run the guard for a stored LLM summary against the given inputs (leak_check)."""
    r = stored(conn, cp, day)
    if not r or r["source"] != "llm":
        return []
    return guard(json.loads(r["summary"]), [m["text"] for m in ctx], [name, day], day)
