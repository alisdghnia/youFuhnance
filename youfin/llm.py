"""Local-LLM judgment layer (Ollama). Used only for borderline calls.

Both judgments return (bool, short_reason), or None when Ollama is unreachable so that
callers fall back to keyword heuristics. Answers are cached in SQLite (llm_cache) and every
decision is logged in llm_decisions so reports can show why. temperature=0, text-only input
that the caller already filtered by replay date (no hindsight is added here).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from . import config  # noqa: F401  (loads .env before the settings below are read)

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not HOST.startswith("http"):
    HOST = "http://" + HOST
MODEL = os.environ.get("YOUFIN_LLM_MODEL", "qwen3:8b")
# Models with a reasoning ("thinking") mode: turn it off; we want a short JSON verdict.
THINKING_MODELS = ("qwen3", "deepseek-r1", "gpt-oss")
TIMEOUT = float(os.environ.get("YOUFIN_LLM_TIMEOUT", "90"))
MAX_TEXT = 1500
MAX_FAILURES = 3   # consecutive slow / malformed replies before the judge is switched off


def is_down(e: Exception) -> bool:
    """True when Ollama itself is unreachable or the model is missing (retrying is pointless);
    False for a timeout or a malformed reply, which says nothing about the next call."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 404                       # model not pulled
    if isinstance(e, urllib.error.URLError):
        return not isinstance(e.reason, TimeoutError)   # refused / no route / DNS
    return isinstance(e, ConnectionError)

TAG_MEANING = {
    "insolvency": "bankruptcy, insolvency or restructuring of the company",
    "fraud": "fraud, forged or double-pledged collateral, or a criminal investigation",
    "lender_action": "lenders seizing assets or taking control of the company",
    "operations": "the company suspending operations, going silent, or laying off staff",
    "diligence_red_flag": "a review of the company's financing or earnings quality, or lender red flags",
    "lender_disclosure": "lenders or funds disclosing exposure to the company's debt",
    "sec_filing": "a bank filing disclosing loan exposure to the company",
    "credit": "a credit rating, debt price or loan-market move for the company",
    "regulatory": "regulatory enforcement against the company",
    "funding": "creditors, advisers or refinancing efforts",
    "litigation": "a lawsuit involving the company",
    "financing": "a financing transaction by the company",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, response TEXT);
CREATE TABLE IF NOT EXISTS llm_decisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,       -- same_event | supports_claim | embed (cluster rule)
    as_of     TEXT,
    event_id  INTEGER,
    subject   TEXT,                -- new title / url that was judged
    answer    INTEGER NOT NULL,
    reason    TEXT,
    model     TEXT,
    cached    INTEGER NOT NULL,
    latency_s REAL NOT NULL,
    applied   TEXT,                -- what the verifier did with the answer
    score     REAL                 -- embedding cosine behind the decision, if any
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    if "score" not in {r[1] for r in conn.execute("PRAGMA table_info(llm_decisions)")}:
        conn.execute("ALTER TABLE llm_decisions ADD COLUMN score REAL")
        conn.commit()


def log_decision(conn: sqlite3.Connection, kind: str, as_of: str | None, subject: str,
                 answer: bool, reason: str, model: str, cached: bool, latency_s: float,
                 event_id: int | None = None, applied: str | None = None,
                 score: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO llm_decisions (kind, as_of, event_id, subject, answer, reason, model, cached, "
        "latency_s, applied, score) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (kind, as_of, event_id, (subject or "")[:200], int(answer), reason, model, int(cached),
         round(latency_s, 2), applied, None if score is None else round(score, 4)))
    return cur.lastrowid


_FORMAT = {"type": "object", "required": ["answer", "reason"], "properties": {
    "answer": {"type": "boolean"}, "reason": {"type": "string"}}}


class Judge:
    def __init__(self, conn: sqlite3.Connection, model: str = MODEL):
        self.conn = conn
        self.model = model
        self.available: bool | None = None
        self.live_calls = 0
        self.live_seconds = 0.0
        self.cache_hits = 0
        self.failed_calls = 0          # calls that got no answer (the caller used its fallback)
        self._streak = 0
        self.last_decision: int | None = None
        ensure_schema(conn)

    def _failed(self, e: Exception) -> None:
        """One bad reply only costs that call. The judge is switched off (and says so, once)
        when Ollama is unreachable or keeps failing: a silent switch-off would let later
        keyword matches through without the veto and make runs differ for no visible reason."""
        self.failed_calls += 1
        self._streak += 1
        if is_down(e) or self._streak >= MAX_FAILURES:
            self.available = False
            print(f"[llm] {self.model} unavailable ({type(e).__name__}); continuing without the "
                  "LLM veto / merge check for the rest of this run", file=sys.stderr)

    # ------------------------------------------------------------------ plumbing
    def _chat(self, system: str, user: str) -> tuple[dict | None, bool, float]:
        key = hashlib.sha256(f"{self.model}\n{system}\n{user}".encode()).hexdigest()
        row = self.conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
        if row:
            self.cache_hits += 1
            return json.loads(row[0]), True, 0.0
        if self.available is False:
            return None, False, 0.0
        payload = {
            "model": self.model, "stream": False, "format": _FORMAT,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.model.startswith(THINKING_MODELS):
            payload["think"] = False
        body = json.dumps(payload).encode()
        req = urllib.request.Request(f"{HOST}/api/chat", body, {"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = json.loads(json.loads(r.read())["message"]["content"])
            out = {"answer": bool(out["answer"]), "reason": str(out.get("reason", ""))[:240]}
        except Exception as e:  # heuristics take over for this call
            self._failed(e)
            return None, False, 0.0
        dt = time.time() - t0
        self.available = True
        self._streak = 0
        self.live_calls += 1
        self.live_seconds += dt
        self.conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?)", (key, json.dumps(out)))
        return out, False, dt

    def _ask(self, kind: str, system: str, user: str, subject: str, as_of: str | None
             ) -> tuple[bool, str] | None:
        out, cached, dt = self._chat(system, user)
        if out is None:
            self.last_decision = None
            return None
        self.last_decision = log_decision(self.conn, kind, as_of, subject, out["answer"],
                                          out["reason"], self.model, cached, dt)
        return out["answer"], out["reason"]

    def annotate(self, decision_id: int | None, event_id: int | None, applied: str,
                 score: float | None = None) -> None:
        if decision_id:
            self.conn.execute(
                "UPDATE llm_decisions SET event_id=?, applied=?, score=COALESCE(?, score) WHERE id=?",
                (event_id, applied, None if score is None else round(score, 4), decision_id))

    # ------------------------------------------------------------------ judgments
    def same_event(self, event_headline: str, event_tag: str, new_title: str, new_text: str,
                   as_of: str | None = None) -> tuple[bool, str] | None:
        system = ("You are a credit-risk analyst deduplicating news. Two articles are the SAME EVENT "
                  "only if they report the same specific real-world development (same action, same "
                  "actors), even if worded differently or from different angles. Different follow-ups "
                  "or different developments are NOT the same event. Reply as JSON with fields "
                  "answer (boolean) and reason (one short sentence).")
        user = (f"Existing event headline: {event_headline}\nExisting event type: "
                f"{TAG_MEANING.get(event_tag, event_tag)}\n\nNew article title: {new_title}\n"
                f"New article text: {new_text[:MAX_TEXT]}\n\nIs the new article reporting the same event?")
        return self._ask("same_event", system, user, new_title, as_of)

    def supports_claim(self, event_tag: str, event_headline: str, article_text: str,
                       as_of: str | None = None, subject: str = "") -> tuple[bool, str] | None:
        system = ("You are a credit-risk analyst verifying news. Decide whether the article text "
                  "itself states or reports the claim, i.e. it is about this development. Merely "
                  "mentioning the company, or an unrelated topic, does not support the claim. Reply as "
                  "JSON with fields answer (boolean) and reason (one short sentence).")
        # The caller passes the evidence passages (see verifier.evidence_window); the cut here
        # is only a backstop for other callers.
        user = (f"Claim: {event_headline}\nClaim type: {TAG_MEANING.get(event_tag, event_tag)}\n\n"
                f"Article text: {article_text[:MAX_TEXT]}\n\nDoes the article support the claim?")
        return self._ask("supports_claim", system, user, subject or article_text[:100], as_of)
