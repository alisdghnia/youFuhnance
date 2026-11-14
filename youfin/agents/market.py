"""Market Signals & Hedging desk.

For each verified ALERT on the bus it answers, with no look-ahead (alerts are dated, not
timed, so only sessions that closed BEFORE the alert date are used; see market.price_cutoff):
  1. which liquid public proxies carry this counterparty's risk (data/portfolio.json),
  2. is the market ALREADY pricing it: per-proxy features, BOCPD change-point probability,
     basket Markov-switching P(stress), and an open / closing / closed action window,
  3. which proxies are the best hedge candidates (explainable score + caveats).
It writes one `market_assessments` row per alert and publishes a compact message on the
`actions` topic. Output is analysis for the deal team, never a trade instruction.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .. import market as M
from .. import regime as R

log = logging.getLogger("youfin.market")

STALE_DAYS = 5   # calendar days between the price cut-off and the last bar before we warn

DISCLAIMER = ("Analysis for the deal team, not investment advice. Any hedge needs compliance "
              "sign-off first: as a lender we may hold MNPI on the borrower and its lenders.")

# Hedge score = 0.45 role relevance + 0.25 liquidity + 0.30 "move not yet happened".
ROLE_RELEVANCE = {"exposed_lender": 1.0, "peer": 0.8, "customer": 0.6,
                  "sector_etf": 0.5, "credit": 0.5, "market": 0.1}
SCORE_W = {"role": 0.45, "liquidity": 0.25, "unspent": 0.30}
LIQ_LO, LIQ_HI = 6.0, 9.0      # log10 ADV: $1M/day -> 0, $1B/day -> 1
UNSPENT_Z = (1.0, 3.0)         # abnormal fall already in the price: <= 1 sd is noise (unspent 1),
                               # >= 3 sd means the move happened (unspent 0); linear in between

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_assessments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    counterparty_id TEXT NOT NULL,
    as_of           TEXT NOT NULL,      -- assessment date (the alert date)
    bar_date        TEXT,               -- last market close used (< as_of unless after-close)
    event_id        INTEGER,            -- verified event that triggered it (NULL from CLI)
    trigger         TEXT NOT NULL,      -- bus message id, or 'cli'
    window          TEXT NOT NULL,      -- open | closing | closed
    p_stress        REAL,               -- basket Markov-switching P(stress regime)
    top_hedges      TEXT NOT NULL,      -- JSON list of tickers, best first
    payload         TEXT NOT NULL,      -- JSON: every number behind every label
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (counterparty_id, as_of, trigger)
);
"""


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _num(x, nd: int = 4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(x) else round(x, nd)


# ---------------------------------------------------------------- panel
@dataclass
class Panel:
    """Everything the desk computes for one counterparty from bars dated <= `end`."""
    cp: str
    end: date
    prox: list[dict]
    prices: dict
    feats: dict = field(default_factory=dict)     # ticker -> DataFrame (features + bocpd)
    basket: pd.DataFrame | None = None            # ar, z, car5_z, bocpd


def build_panel(conn: sqlite3.Connection, cp: str, end) -> Panel:
    end = _d(end)
    prox = M.proxies(cp)
    if not prox:
        raise ValueError(f"no market proxies configured for {cp} in portfolio.json")
    tickers = M.all_tickers(prox)
    M.ensure_prices(conn, tickers, end)
    prices = M.load_prices(conn, tickers, end)          # <= end: the no-look-ahead cut
    pn = Panel(cp, end, prox, prices)
    for p in prox:
        f = M.features(prices, p["ticker"], p.get("vs"))
        f["bocpd_ar"] = R.bocpd_series(f["ar_z"])
        f["bocpd_vol"] = R.bocpd_series(f["vol_z"])
        pn.feats[p["ticker"]] = f
    # A proxy without bars (delisted, typo, failed download) must not poison the basket:
    # its empty object-dtype column used to make the Markov fit fail for everyone.
    members = {p["ticker"]: pn.feats[p["ticker"]]["ar"].astype(float) for p in prox
               if p["role"] != "market" and len(pn.feats[p["ticker"]])}
    if members:
        ars = pd.concat(members, axis=1)
        b = pd.DataFrame({"ar": ars.mean(axis=1, skipna=True).astype(float)})
    else:
        b = pd.DataFrame({"ar": pd.Series(dtype=float)})
    sd = b["ar"].rolling(M.WINDOW, min_periods=M.WINDOW).std().shift(1)
    b["z"] = b["ar"] / sd
    b["car5"] = b["ar"].rolling(5).sum()
    b["car5_z"] = b["car5"] / (sd * np.sqrt(5))
    b["bocpd"] = R.bocpd_series(b["z"])
    pn.basket = b
    return pn


def proxy_row(pn: Panel, p: dict, day: pd.Timestamp) -> dict:
    f = pn.feats[p["ticker"]]
    row = {"ticker": p["ticker"], "role": p["role"], "vs": p.get("vs"),
           "relevance": p.get("relevance", ROLE_RELEVANCE.get(p["role"], 0.3)),
           "note": p.get("note", "")}
    if day not in f.index:
        row.update(label="n/a", drivers=["no bar"])
        return row
    x = f.loc[day]
    for k in ("close", "r1", "r3", "r5", "ar", "beta", "ar_z", "car5_z", "shock_z", "ar_vs_z",
              "vol_z", "rv_ratio", "dd20", "adv60_usd", "bocpd_ar", "bocpd_vol"):
        row[k] = _num(x.get(k), 2 if k == "adv60_usd" else 4)
    if row["ar_z"] is None and row["car5_z"] is None:
        # Too little history (or no benchmark bars) to say anything: not the same as "open".
        row.update(label="n/a", drivers=["insufficient history"])
        return row
    row["label"], row["drivers"] = R.classify_proxy(row)
    return row


def rank_hedges(rows: list[dict]) -> list[dict]:
    """Explainable hedge-candidate score; the market proxy is a reference, not a candidate."""
    out = []
    for r in rows:
        if r["role"] == "market" or r.get("label") == "n/a":
            continue
        adv = r.get("adv60_usd") or 0
        liq = min(max((math.log10(adv) - LIQ_LO) / (LIQ_HI - LIQ_LO), 0.0), 1.0) if adv > 0 else 0.0
        spent_z = max(0.0, -(r.get("car5_z") or 0), -(r.get("shock_z") or 0))
        lo, hi = UNSPENT_Z
        unspent = 1 - min(max((spent_z - lo) / (hi - lo), 0.0), 1.0)
        rel = float(r["relevance"])
        score = SCORE_W["role"] * rel + SCORE_W["liquidity"] * liq + SCORE_W["unspent"] * unspent
        out.append({"ticker": r["ticker"], "role": r["role"], "score": round(score, 3),
                    "components": {"relevance": round(rel, 2), "liquidity": round(liq, 2),
                                   "unspent": round(unspent, 2)},
                    "adv60_musd": round(adv / 1e6, 1), "label": r["label"],
                    "caveat": caveat(r, adv)})
    return sorted(out, key=lambda h: -h["score"])


def caveat(r: dict, adv: float) -> str:
    c = []
    role, t = r["role"], r["ticker"]
    if adv and adv < 20e6:
        c.append(f"thin (ADV ${adv / 1e6:.1f}M): wide spreads, borrow may be scarce/expensive")
    if role == "exposed_lender":
        c.append("equity reflects the whole bank; exposure may be small vs capital (dilution)")
    elif role in ("peer", "customer"):
        c.append("basis risk: moves on its own earnings/fundamentals, only indirectly on our loss")
    elif role == "sector_etf":
        c.append("broad basket: hedges contagion, not the idiosyncratic loss")
    elif role == "credit":
        c.append("HY index basis (rates/duration, not ABS/private credit); ETF borrow fee; "
                 "CDX HY is the institutional alternative")
    if t == "UBS":
        c.append("ADR (primary listing Zurich)")
    c.append("short needs a locate; recall risk")
    return "; ".join(c)


def assess_day(pn: Panel, day: pd.Timestamp, ms: dict, as_of: date | None = None) -> dict:
    rows = [proxy_row(pn, p, day) for p in pn.prox]
    b = pn.basket.loc[day] if day in pn.basket.index else None
    basket = {"p_stress": _num(ms.get("p_stress")), "ar": _num(b["ar"] if b is not None else None, 5),
              "z": _num(b["z"] if b is not None else None),
              "car5_z": _num(b["car5_z"] if b is not None else None),
              "bocpd": _num(b["bocpd"] if b is not None else None),
              "members": [p["ticker"] for p in pn.prox
                          if p["role"] != "market" and len(pn.feats[p["ticker"]])],
              **{k: _num(v) for k, v in ms.items() if k in ("sd_calm_pct", "sd_stress_pct",
                                                             "mean_stress_pct")},
              "ms_bars": ms.get("n_bars"), "ms_error": ms.get("error")}
    basket["stress_persistence"] = _num(ms.get("stress_persistence"))
    window, why = R.classify_overall(rows, basket["p_stress"], basket["car5_z"])
    warn = [f"{r['ticker']}: {r['drivers'][0]}" for r in rows if r["label"] == "n/a"]
    if ms.get("error"):
        warn.append(f"basket Markov model unavailable: {ms['error']}")
    return {"counterparty": pn.cp, "as_of": (as_of or day.date()).isoformat(),
            "bar_date": day.date().isoformat(), "window": window, "window_drivers": why,
            "data_warnings": warn,
            "basket": basket, "proxies": rows, "hedges": rank_hedges(rows),
            "thresholds": {"proxy": R.T, "overall": R.OVERALL}, "disclaimer": DISCLAIMER}


def assess(conn: sqlite3.Connection, cp: str, as_of, after_close: bool = False) -> dict:
    """Full assessment for an alert dated `as_of`.

    Uses only sessions that closed before `as_of` (the alert's time of day is unknown, so
    that day's close may be after it). after_close=True states the alert came after the
    close of `as_of` and lets that session in."""
    as_of = _d(as_of)
    cut = M.price_cutoff(as_of, after_close)
    pn = build_panel(conn, cp, cut)
    bar = M.last_bar(pn.prices, cut)
    if bar is None:
        raise ValueError(f"no market data on or before {cut}")
    day = pd.Timestamp(bar)
    ms = R.markov_stress(pn.basket["ar"], day)
    a = assess_day(pn, day, ms, as_of)
    a["price_rule"] = ("alert after the close: sessions dated <= as_of" if after_close else
                       "alert time unknown: only sessions closed before as_of (dated < as_of)")
    if (cut - bar).days > STALE_DAYS:
        a["data_warnings"].insert(0, f"stale prices: last bar {bar} is {(cut - bar).days} days "
                                     f"before the cut-off {cut}")
    return a


def save(conn: sqlite3.Connection, a: dict, trigger: str = "cli", event_id: int | None = None) -> int:
    conn.executescript(SCHEMA)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO market_assessments (counterparty_id, as_of, bar_date, event_id, "
            "trigger, window, p_stress, top_hedges, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (a["counterparty"], a["as_of"], a["bar_date"], event_id, trigger, a["window"],
             a["basket"]["p_stress"], json.dumps([h["ticker"] for h in a["hedges"][:3]]),
             json.dumps(a, default=str)))
    return conn.execute("SELECT id FROM market_assessments WHERE counterparty_id=? AND as_of=? "
                        "AND trigger=?", (a["counterparty"], a["as_of"], trigger)).fetchone()[0]


def summary(a: dict) -> str:
    moving = [f"{r['ticker']}:{r['label']}" for r in a["proxies"] if r.get("label") not in ("open", "n/a")]
    ps = a["basket"]["p_stress"]
    warn = f"; DATA WARNINGS: {len(a['data_warnings'])}" if a.get("data_warnings") else ""
    return (f"MARKET {a['counterparty']} as of {a['as_of']} (close {a['bar_date']}): window "
            f"{a['window'].upper()}; basket P(stress) {'n/a' if ps is None else f'{ps:.2f}'}; "
            f"moving: {', '.join(moving) or 'none'}; hedge candidates: "
            f"{', '.join(h['ticker'] for h in a['hedges'][:3]) or 'none'}{warn} "
            f"(analysis, not advice)")


# ----------------------------------------------------------------- study
def study(conn: sqlite3.Connection, cp: str, start, end) -> tuple[pd.DataFrame, pd.DataFrame, Panel]:
    """Daily assessments over [start, end], each row AT THAT DAY'S CLOSE (what an alert
    dated the next day would see). Features/BOCPD are causal (computed once on bars
    <= end, each value uses only earlier bars); the Markov model is refit on every day's
    expanding window exactly as `assess` does (no warm start, so both give the same numbers)."""
    start, end = _d(start), _d(end)
    pn = build_panel(conn, cp, end)
    days = M.trading_days(pn.prices)
    days = days[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))]
    prows, orows = [], []
    for day in days:
        ms = R.markov_stress(pn.basket["ar"], day)
        a = assess_day(pn, day, ms)
        for r in a["proxies"]:
            prows.append({"date": day, **{k: v for k, v in r.items() if k not in ("drivers", "note")},
                          "drivers": "; ".join(r.get("drivers", []))})
        orows.append({"date": day, "window": a["window"], "drivers": "; ".join(a["window_drivers"]),
                      **{f"basket_{k}": v for k, v in a["basket"].items()
                         if k in ("p_stress", "ar", "z", "car5_z", "bocpd")}})
    if not orows:
        raise ValueError(f"no trading days with market data in {start}..{end}")
    return pd.DataFrame(prows), pd.DataFrame(orows).set_index("date"), pn


def lead_sessions(days: pd.DatetimeIndex, alert, brk) -> int | None:
    """Trading sessions from the alert to the break (break session counted; negative = the
    market broke before our alert). Sunday alert + Wednesday break = 3.

    Deliberately the conservative convention for OUR lead: an alert dated on a trading day
    is treated as arriving after that day's close, so a break on the alert day itself is
    lead 0 (no lead claimed), and the next session is +1."""
    if brk is None or alert is None:
        return None
    a = days[days <= pd.Timestamp(_d(alert))]
    if not len(a):
        return None
    ia = days.get_loc(a[-1])
    ib = days.get_loc(pd.Timestamp(brk))
    return int(ib - ia)


def first_breaks(prows: pd.DataFrame, orows: pd.DataFrame) -> dict:
    """First date each proxy / the basket / the overall window breaks."""
    out = {}
    for t, g in prows.groupby("ticker", sort=False):
        g = g.set_index("date")
        down = g[(g["bocpd_ar"] >= R.BREAK_P) & (g["shock_z"] < 0)]
        closing = g[g["label"].isin(["closing", "closed"])]
        closed = g[g["label"] == "closed"]
        out[t] = {"role": g["role"].iloc[0],
                  "bocpd_break": down.index[0] if len(down) else None,
                  "first_closing": closing.index[0] if len(closing) else None,
                  "first_closed": closed.index[0] if len(closed) else None,
                  "min_car5_z": _num(g["car5_z"].min(), 2)}
    falling = orows["basket_car5_z"].fillna(0) < 0
    st = orows[(orows["basket_p_stress"].fillna(0) >= 0.5) & falling]
    ow_c = orows[orows["window"].isin(["closing", "closed"])]
    ow_x = orows[orows["window"] == "closed"]
    out["BASKET"] = {"role": "basket", "bocpd_break": None,
                     "ms_break": st.index[0] if len(st) else None,
                     "first_closing": ow_c.index[0] if len(ow_c) else None,
                     "first_closed": ow_x.index[0] if len(ow_x) else None,
                     "max_p_stress": _num(orows["basket_p_stress"].max(), 2)}
    return out


# ------------------------------------------------------------------ agent
class MarketDesk:
    """Bus agent: `alerts` in, `market_assessments` rows + `actions` messages out."""
    name = "market"

    def __init__(self, conn: sqlite3.Connection, bus=None):
        self.conn = conn
        self.bus = bus
        self._cache: dict[tuple[str, str], dict] = {}   # (cp, as_of) -> assessment
        self.errors: list[str] = []                     # alerts that could not be assessed
        conn.executescript(SCHEMA)
        M.init(conn)
        if bus is not None:
            bus.subscribe(self.name, ["alerts"])

    def reset(self) -> None:
        """Drop bus-triggered assessments (CLI ones stay); called when a replay clears the bus."""
        with self.conn:
            self.conn.execute("DELETE FROM market_assessments WHERE trigger != 'cli'")
        self._cache.clear()

    def handle_alert(self, payload: dict, trigger: str) -> dict | None:
        cp, as_of = payload.get("counterparty"), payload.get("as_of")
        try:
            if not cp or not as_of or not M.proxies(cp):
                return None  # nothing to assess: unknown counterparty or no proxies listed
        except KeyError:
            return None
        key = (cp, str(as_of))
        if key not in self._cache:          # several alerts on one day share one assessment
            self._cache[key] = assess(self.conn, cp, as_of)
        a = dict(self._cache[key])
        a["event"] = {k: payload.get(k) for k in ("event_id", "headline", "verdict", "tag",
                                                    "receipt_url")}
        aid = save(self.conn, a, trigger, payload.get("event_id"))
        msg = {
            "kind": "market_assessment", "assessment_id": aid, "counterparty": cp,
            "event_id": payload.get("event_id"), "as_of": a["as_of"], "bar_date": a["bar_date"],
            "window": a["window"], "window_drivers": a["window_drivers"][:3],
            "data_warnings": a.get("data_warnings", [])[:3],
            "basket_p_stress": a["basket"]["p_stress"],
            "moving": [{"ticker": r["ticker"], "label": r["label"], "car5_z": r.get("car5_z")}
                       for r in a["proxies"] if r.get("label") not in ("open", "n/a")],
            "hedge_candidates": [{k: h[k] for k in ("ticker", "role", "score", "label")}
                                 for h in a["hedges"][:3]],
            "disclaimer": "analysis, not advice; compliance/MNPI check before any trade",
            "summary": summary(a),
        }
        if self.bus is not None:
            self.bus.publish("actions", f"action-market-{payload.get('event_id')}", msg, self.name)
        return msg

    def step(self, as_of=None) -> list[dict]:
        if self.bus is None:
            return []
        out = []
        for m in self.bus.poll(self.name):
            if m["topic"] != "alerts":
                continue
            try:
                r = self.handle_alert(m["payload"], m["id"])
            except Exception as e:  # market data / model trouble must never stop the team
                self.errors.append(f"{m['id']}: {type(e).__name__}: {e}")
                log.warning("market desk could not assess %s: %s: %s", m["id"],
                            type(e).__name__, e)
                continue
            if r:
                out.append(r)
        return out
