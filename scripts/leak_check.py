"""Replay-integrity proof: outputs for day D must not change when everything dated after D is deleted.

  python3 scripts/leak_check.py [--db path] [--cases tricolor:2025-09-05,tricolor:2025-09-07,...]
For each (counterparty, D) it copies the DB, deletes all later data (prices >= D per the prior-close rule,
articles/signals/events/sources/bus/market/covenant rows first seen or dated after D, and the future
alert/watch dates stored on kept events), recomputes, and compares with the full DB:
market desk assess, covenant assess, brief, dashboard per-day payload (+ chart series rows < D, pins <= D).
Prints PASS/FAIL per consumer and date with the first differing field. Exit code 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import demo_data as X
import dashboard as DASH
from youfin import market as M
from youfin.agents import covenant as C

DEFAULT_CASES = "tricolor:2025-09-05,tricolor:2025-09-07,first_brands:2025-09-02,first_brands:2025-09-19,tricolor:2025-09-09,first_brands:2025-09-20"


def truncate(src: Path, dst: Path, D: str) -> sqlite3.Connection:
    shutil.copy(src, dst)
    c = sqlite3.connect(dst)
    c.row_factory = sqlite3.Row
    cut = M.price_cutoff(D).isoformat()          # last bar a day-D consumer may use
    x = c.execute
    x("DELETE FROM market_prices WHERE date > ?", (cut,))
    x("DELETE FROM signals WHERE first_seen_as_of > ?", (D,))
    x("DELETE FROM event_sources WHERE event_id IN (SELECT id FROM events WHERE first_seen_as_of > ?)", (D,))
    x("DELETE FROM event_sources WHERE substr(published_at,1,10) > ?", (D,))
    x("DELETE FROM event_verifications WHERE as_of > ? OR event_id IN (SELECT id FROM events WHERE first_seen_as_of > ?)", (D, D))
    x("DELETE FROM events WHERE first_seen_as_of > ?", (D,))
    # future alert/watch stamps on events that were already visible
    x("UPDATE events SET alerted_as_of=NULL, alert_level=CASE WHEN watch_as_of<=? THEN 'watch' ELSE 'log' END WHERE alerted_as_of > ?", (D, D))
    x("UPDATE events SET watch_as_of=NULL, alert_level='log' WHERE watch_as_of > ?", (D,))
    x("DELETE FROM articles WHERE substr(page_age,1,10) > ? OR url NOT IN (SELECT url FROM signals) AND url NOT IN (SELECT url FROM event_sources)", (D,))
    x("DELETE FROM scout_runs WHERE as_of > ?", (D,))
    x("DELETE FROM archive_snapshots WHERE as_of > ?", (D,))
    x("DELETE FROM llm_decisions WHERE as_of > ?", (D,))
    x("DELETE FROM market_assessments WHERE as_of > ?", (D,))
    x("DELETE FROM covenant_trigger_status WHERE assessment_id IN (SELECT id FROM covenant_assessments WHERE as_of > ?)", (D,))
    x("DELETE FROM covenant_assessments WHERE as_of > ?", (D,))
    for r in c.execute("SELECT id, payload FROM bus_messages").fetchall():
        try:
            a = json.loads(r["payload"]).get("as_of")
        except ValueError:
            continue
        if a and a > D:
            x("DELETE FROM bus_messages WHERE id=?", (r["id"],))
    # Reset the stored FINAL state of the events that remain: an as-of view that still reads these
    # columns (instead of recomputing from sources/signals dated <= D) differs from the full DB.
    x("UPDATE events SET verdict='single_source', confidence=0.3, n_publishers=0, best_tier=NULL, "
      "alert_level='log', severity=0, headline='' ")
    c.commit()
    return c


def norm(o):
    return json.loads(json.dumps(o, default=str, sort_keys=True))


def first_diff(a, b, path=""):
    if type(a) != type(b):
        return f"{path}: {str(a)[:80]!r} != {str(b)[:80]!r}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}/{k}: present in only one side"
            d = first_diff(a[k], b[k], f"{path}/{k}")
            if d:
                return d
    elif isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
    elif a != b:
        if isinstance(a, str) and isinstance(b, str):
            k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            return f"{path}@{k}: {a[max(0, k - 20):k + 50]!r} != {b[max(0, k - 20):k + 50]!r}"
        return f"{path}: {str(a)[:100]!r} != {str(b)[:100]!r}"
    return None


def consumers(conn, cp, D, dash_full=None):
    """name -> JSON-able output computed from `conn` for counterparty cp on day D."""
    X._MARKET_CACHE.clear()
    out = {}
    from youfin.agents import market as DM
    out["market assess"] = norm(DM.assess(conn, cp, D))
    cov = {}
    for p in C.load_positions():
        cov[p["id"]] = norm(C.assess(conn, p, D))   # an exception here is a FAIL, never a silent equal
    out["covenant assess"] = cov
    X._MARKET_CACHE.clear()
    out["brief"] = X.render_brief(conn, D)
    X._MARKET_CACHE.clear()
    return out


def dash_payload(conn, cp, D):
    X._MARKET_CACHE.clear()
    data = DASH.build(conn, cps=[cp], last=D, marks=False)
    c = data["cps"][cp]
    ms = c["ms"]
    keep = [i for i, d in enumerate(ms["dates"]) if d < D] if ms else []
    def cut(v):
        return [x for i, x in enumerate(v) if i in set(keep)]
    msr = None
    if ms:
        msr = {k: ({t: cut(a) for t, a in v.items()} if isinstance(v, dict) else cut(v) if k != "tickers" else v)
               for k, v in ms.items()}
    return norm({"day": c["days"][D], "ms_rows_before_D": msr, "pins": [p for p in c["pins"] if p["d"] <= D]})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    a = ap.parse_args()
    src = Path(a.db or X.DEFAULT_DB)
    tmp = Path(tempfile.mkdtemp())
    full = X.open_db(src)
    fails = 0
    full_dash = {}
    for case in a.cases.split(","):
        cp, D = case.split(":")
        print(f"--- {cp} as of {D} (prices used: dated <= {M.price_cutoff(D)})", flush=True)
        cut = truncate(src, tmp / "cut.db", D)
        try:
            ref, got = consumers(full, cp, D), consumers(cut, cp, D)
        except Exception as e:
            print(f"  FAIL  consumers raised {type(e).__name__}: {e}")
            fails += 1
            continue
        if cp not in full_dash:
            full_dash[cp] = DASH.build(full, cps=[cp], marks=False)
        X._MARKET_CACHE.clear()
        d = full_dash[cp]["cps"][cp]
        ms = d["ms"]
        keep = {i for i, x in enumerate(ms["dates"]) if x < D} if ms else set()
        cutf = lambda v: [x for i, x in enumerate(v) if i in keep]   # noqa: E731
        ref["dashboard day"] = norm({"day": d["days"][D], "ms_rows_before_D": ({k: ({t: cutf(v2) for t, v2 in v.items()} if isinstance(v, dict) else v if k == "tickers" else cutf(v)) for k, v in ms.items()} if ms else None),
                                     "pins": [p for p in d["pins"] if p["d"] <= D]})
        got["dashboard day"] = dash_payload(cut, cp, D)
        for name in ref:
            diff = first_diff(norm(ref[name]), norm(got[name]))
            print(f"  {'FAIL' if diff else 'PASS'}  {name:<16} {D} {cp}" + (f"\n        first diff {diff}" if diff else ""), flush=True)
            fails += bool(diff)
    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
