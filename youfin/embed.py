"""Local text embeddings (Ollama /api/embed) for the Verifier's "same event?" check.

Vectors are cached in SQLite (`embeddings`, keyed by model + exact input) so a replay
embeds each article once. `embed()` returns None when Ollama or the model is unavailable,
so callers fall back to the title/claim heuristics. Cosine is plain Python (no numpy).
Callers pass only as-of text (title + description); nothing is added here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
from array import array

from .llm import HOST, MAX_FAILURES, is_down

MODEL = os.environ.get("YOUFIN_EMBED_MODEL", "nomic-embed-text")  # "" or "off" disables
TIMEOUT = float(os.environ.get("YOUFIN_EMBED_TIMEOUT", "60"))
MAX_CHARS = 2000
# Task prefix the model authors recommend for clustering / similarity (none for most models).
PREFIX = {"nomic-embed-text": "clustering: "}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    key    TEXT PRIMARY KEY,   -- sha256(model + input)
    model  TEXT NOT NULL,
    dim    INTEGER NOT NULL,
    vector BLOB NOT NULL       -- float32, L2-normalised
);
"""


def enabled(model: str = MODEL) -> bool:
    return bool(model) and model.lower() not in ("off", "none", "0")


def cosine(a: array, b: array) -> float:
    """Vectors are stored normalised, so the dot product is the cosine."""
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    def __init__(self, conn: sqlite3.Connection, model: str = MODEL):
        self.conn = conn
        self.model = model
        base = model.split(":")[0].rsplit("/", 1)[-1]
        self.prefix = os.environ.get("YOUFIN_EMBED_PREFIX", PREFIX.get(base, ""))
        self.available: bool | None = None
        self.live_texts = 0
        self.live_seconds = 0.0
        self.cache_hits = 0
        self.failed_calls = 0
        self._streak = 0
        self._mem: dict[str, array] = {}
        conn.executescript(SCHEMA_SQL)

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\n{text}".encode()).hexdigest()

    def embed(self, texts: list[str]) -> list[array] | None:
        """One vector per text, or None if any text cannot be embedded."""
        inputs = [self.prefix + (t or "")[:MAX_CHARS] for t in texts]
        keys = [self._key(i) for i in inputs]
        todo: dict[str, str] = {}
        for k, inp in zip(keys, inputs):
            if k in self._mem or k in todo:
                continue
            row = self.conn.execute("SELECT vector FROM embeddings WHERE key=?", (k,)).fetchone()
            if row:
                v = array("f")
                v.frombytes(row[0])
                self._mem[k] = v
                self.cache_hits += 1
            else:
                todo[k] = inp
        if todo:
            if self.available is False:
                return None
            body = json.dumps({"model": self.model, "input": list(todo.values()),
                               "truncate": True}).encode()
            req = urllib.request.Request(f"{HOST}/api/embed", body,
                                         {"Content-Type": "application/json"})
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    vecs = json.loads(r.read())["embeddings"]
                if len(vecs) != len(todo) or not all(vecs):
                    raise ValueError("embedding count mismatch")
            except Exception as e:  # this call falls back to the tag/title heuristics
                self.failed_calls += 1
                self._streak += 1
                if is_down(e) or self._streak >= MAX_FAILURES:
                    self.available = False
                    print(f"[embed] {self.model} unavailable ({type(e).__name__}); clustering "
                          "falls back to the tag/title rules for the rest of this run",
                          file=sys.stderr)
                return None
            self.available = True
            self._streak = 0
            self.live_seconds += time.time() - t0
            self.live_texts += len(todo)
            for k, vec in zip(todo, vecs):
                norm = sum(x * x for x in vec) ** 0.5 or 1.0
                v = array("f", (x / norm for x in vec))
                self._mem[k] = v
                self.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)",
                                  (k, self.model, len(v), v.tobytes()))
        return [self._mem[k] for k in keys]
