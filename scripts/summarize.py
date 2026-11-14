"""Write the LLM executive summaries (youfin:summarizer) for counterparty/day ranges.

  YOUFIN_BUS=mirror python3 scripts/summarize.py tricolor 2025-09-05 2025-09-12 [--no-board]
Reads context from the OpenCrawl board (youfin-* topics, payload as_of <= day), falls back to the local
bus, guards every fact, stores the result in exec_summaries and publishes it on `brief`.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from youfin.agents.summarizer import Summarizer  # noqa: E402
from youfin.bus import Bus  # noqa: E402
from youfin.db import connect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("counterparty")
    ap.add_argument("start", type=date.fromisoformat)
    ap.add_argument("end", type=date.fromisoformat)
    ap.add_argument("--no-board", action="store_true", help="use the local bus only")
    a = ap.parse_args()
    conn = connect()
    name = conn.execute("SELECT name FROM counterparties WHERE id=?", (a.counterparty,)).fetchone()["name"]
    s = Summarizer(conn, bus=Bus(conn), use_board=not a.no_board)
    d = a.start
    while d <= a.end:
        r = s.summarize(a.counterparty, name, d.isoformat())
        print(f"{d} {r['source']:<4} src={r['context_src']} msgs={r['n_msgs']} {r['reason'][:160]}", flush=True)
        d += timedelta(1)
    if s.reader and s.reader.error:
        print("board note:", s.reader.error)
    print("mirror:", s.bus.mirror_stats())


if __name__ == "__main__":
    main()
