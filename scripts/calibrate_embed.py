"""Cosine distributions for picking YOUFIN_EMBED_LOW / YOUFIN_EMBED_HIGH (no You.com calls).

Pairs of stored signals of the same counterparty, embedded on title + description:
  copies    - published within 2 days with title Jaccard >= 0.5 (syndicated copies, same event)
  different - published >= 5 days apart (different developments of the same company)
HIGH should sit above nearly all `different` pairs; LOW near their p90 (the LLM judges the band).

  python scripts/calibrate_embed.py            # model from YOUFIN_EMBED_MODEL
"""
from __future__ import annotations

import itertools
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youfin.agents.verifier import EMBED_HIGH, EMBED_LOW, jaccard, title_tokens  # noqa: E402
from youfin.db import connect  # noqa: E402
from youfin.embed import Embedder, cosine  # noqa: E402


def pct(xs: list[float], q: float) -> float:
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    conn = connect()
    emb = Embedder(conn)
    rows = conn.execute(
        "SELECT s.counterparty_id cp, s.published_at pub, a.title, a.description FROM signals s "
        "JOIN articles a USING (url) WHERE s.published_at IS NOT NULL").fetchall()
    vecs = emb.embed([" ".join([r["title"] or "", r["description"] or ""]).strip() for r in rows])
    conn.commit()
    if vecs is None:
        sys.exit(f"embedding model {emb.model} unavailable (ollama pull {emb.model}?)")
    aliases = {c["id"]: [a.lower() for a in json.loads(c["aliases"])]
               for c in conn.execute("SELECT id, aliases FROM counterparties")}
    copies, different = [], []
    for (i, a), (j, b) in itertools.combinations(enumerate(rows), 2):
        if a["cp"] != b["cp"]:
            continue
        gap = abs((date.fromisoformat(a["pub"][:10]) - date.fromisoformat(b["pub"][:10])).days)
        al = aliases[a["cp"]]
        jac = jaccard(title_tokens(a["title"] or "", al), title_tokens(b["title"] or "", al))
        if gap <= 2 and jac >= 0.5:
            copies.append(cosine(vecs[i], vecs[j]))
        elif gap >= 5:
            different.append(cosine(vecs[i], vecs[j]))
    print(f"model {emb.model} (prefix {emb.prefix!r}); current LOW={EMBED_LOW} HIGH={EMBED_HIGH}")
    for name, xs in (("copies", copies), ("different", different)):
        xs.sort()
        if not xs:
            print(f"{name:>9}: no pairs")
            continue
        print(f"{name:>9}: n={len(xs):<5} min={xs[0]:.3f} p10={pct(xs, .1):.3f} "
              f"p50={pct(xs, .5):.3f} p90={pct(xs, .9):.3f} p99={pct(xs, .99):.3f} max={xs[-1]:.3f} "
              f">=HIGH {sum(x >= EMBED_HIGH for x in xs) / len(xs):.0%} "
              f"in band {sum(EMBED_LOW <= x < EMBED_HIGH for x in xs) / len(xs):.0%}")


if __name__ == "__main__":
    main()
