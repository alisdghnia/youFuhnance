"""Daily market data for liquid public proxies, cached in SQLite, plus trailing features.

No look-ahead: every public function takes an as-of date D and works only on bars dated
<= D (an alert raised on a weekend therefore uses Friday's close). All rolling statistics
are computed on windows that END at the bar they describe; the estimation windows for
abnormal returns and volume end the bar BEFORE it.

Features per proxy and bar t (WINDOW = 60 bars):
  r1, r3, r5      simple returns over 1/3/5 bars (adjusted close)
  ar, ar_z        abnormal return vs SPY: market model (alpha, beta, residual sd) fit on the
                  60 bars ending t-1; ar_z = ar / residual sd
  car5_z          5-bar cumulative abnormal return / (sd * sqrt 5)
  shock_z         the largest-magnitude ar_z of the last 3 bars (signed)
  ar_vs_z         same as ar_z but against the proxy's sector ETF (`vs` in portfolio.json)
  vol_z           log volume vs mean/sd of log volume over the 60 bars ending t-1
  rv_ratio        realized vol of the last 5 returns / last 60 returns
  dd20            close / 20-bar high - 1
  adv60_usd       60-bar average dollar volume (raw close * volume)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import PORTFOLIO_PATH

log = logging.getLogger("youfin.market")

HIST_START = date(2022, 6, 1)    # enough warm-up for 60-bar windows before 2023-01-01
WINDOW = 60
MARKET = "SPY"

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_prices (
    ticker TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
    PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS market_fetch (
    ticker TEXT PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')));
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


# ------------------------------------------------------------------ portfolio
def proxies(cp_id: str, portfolio: dict | None = None) -> list[dict]:
    """[{ticker, role, vs?, note?}] for a counterparty (from data/portfolio.json)."""
    portfolio = portfolio or json.loads(PORTFOLIO_PATH.read_text())
    for cp in portfolio["counterparties"]:
        if cp["id"] == cp_id:
            return list(cp.get("proxies", []))
    raise KeyError(f"unknown counterparty {cp_id}")


def all_tickers(prox: list[dict]) -> list[str]:
    out = {MARKET}
    for p in prox:
        out.add(p["ticker"])
        if p.get("vs"):
            out.add(p["vs"])
    return sorted(out)


# ---------------------------------------------------------------------- cache
def ensure_prices(conn: sqlite3.Connection, tickers: list[str], end: date | str) -> None:
    """Download (yfinance) any ticker whose cache does not cover HIST_START..end."""
    init(conn)
    end = min(_d(end), date.today() - timedelta(days=1))
    need = []
    for t in tickers:
        r = conn.execute("SELECT start, end FROM market_fetch WHERE ticker=?", (t,)).fetchone()
        if not r or _d(r[0]) > HIST_START or _d(r[1]) < end:
            need.append(t)
    if not need:
        return
    import yfinance as yf  # imported lazily: cached runs need no network

    fetch_end = date.today() + timedelta(days=1)          # yfinance `end` is exclusive
    log.info("downloading %s %s..%s", need, HIST_START, fetch_end)
    df = yf.download(need, start=HIST_START.isoformat(), end=fetch_end.isoformat(),
                     auto_adjust=False, actions=False, progress=False, group_by="ticker",
                     threads=True)
    for t in need:
        try:
            sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
        except KeyError:
            log.warning("no data for %s", t)
            continue
        sub = sub.dropna(subset=["Close"])
        if sub.empty:
            log.warning("no data for %s", t)
            continue
        rows = [(t, idx.date().isoformat(), _f(r["Open"]), _f(r["High"]), _f(r["Low"]),
                 _f(r["Close"]), _f(r["Adj Close"]), _f(r["Volume"]))
                for idx, r in sub.iterrows()]
        with conn:
            conn.executemany("INSERT OR REPLACE INTO market_prices VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.execute("INSERT OR REPLACE INTO market_fetch (ticker, start, end) VALUES (?,?,?)",
                         (t, HIST_START.isoformat(), rows[-1][1]))


def _f(x) -> float | None:
    return None if pd.isna(x) else float(x)


def load_prices(conn: sqlite3.Connection, tickers: list[str], as_of: date | str) -> dict[str, pd.DataFrame]:
    """{ticker: DataFrame[close, adj_close, volume]} with bars dated <= as_of only."""
    init(conn)
    out = {}
    for t in tickers:
        df = pd.read_sql_query(
            "SELECT date, close, adj_close, volume FROM market_prices WHERE ticker=? AND date<=? "
            "ORDER BY date", conn, params=(t, _d(as_of).isoformat()), parse_dates=["date"])
        out[t] = df.set_index("date")
    return out


# ------------------------------------------------------------------- features
def _market_model(r: pd.Series, m: pd.Series) -> pd.DataFrame:
    """Abnormal return of r against benchmark m; parameters from the WINDOW bars ending t-1."""
    j = pd.concat({"r": r, "m": m}, axis=1).dropna()
    roll = j.rolling(WINDOW, min_periods=WINDOW)
    mean_r, mean_m = roll["r"].mean().shift(1), roll["m"].mean().shift(1)
    var_m = roll["m"].var().shift(1)
    var_r = roll["r"].var().shift(1)
    cov = roll["r"].cov(j["m"]).shift(1)
    beta = cov / var_m
    alpha = mean_r - beta * mean_m
    sd = np.sqrt((var_r - cov ** 2 / var_m).clip(lower=1e-12))
    ar = j["r"] - alpha - beta * j["m"]
    return pd.DataFrame({"ar": ar, "sd": sd, "beta": beta})


def features(prices: dict[str, pd.DataFrame], ticker: str, vs: str | None = None) -> pd.DataFrame:
    """Daily trailing features for one ticker (index = bar date). Causal by construction."""
    px = prices[ticker]
    adj, close, vol = px["adj_close"], px["close"], px["volume"]
    r = adj.pct_change()
    f = pd.DataFrame(index=px.index)
    f["close"] = close
    f["r1"] = r
    f["r3"] = adj / adj.shift(3) - 1
    f["r5"] = adj / adj.shift(5) - 1
    if ticker == MARKET:
        # The market has no benchmark: its "abnormal" return is its own standardized move.
        sd = r.rolling(WINDOW, min_periods=WINDOW).std().shift(1)
        mm = pd.DataFrame({"ar": r, "sd": sd, "beta": 1.0})
    else:
        mm = _market_model(r, prices[MARKET]["adj_close"].pct_change()).reindex(px.index)
    f["ar"] = mm["ar"]
    f["beta"] = mm["beta"]
    f["ar_z"] = mm["ar"] / mm["sd"]
    f["car5_z"] = mm["ar"].rolling(5).sum() / (mm["sd"] * np.sqrt(5))
    z3 = f["ar_z"].rolling(3)
    hi, lo = z3.max(), z3.min()
    f["shock_z"] = np.where(hi.abs() >= lo.abs(), hi, lo)
    if vs and vs in prices:
        mv = _market_model(r, prices[vs]["adj_close"].pct_change()).reindex(px.index)
        f["ar_vs_z"] = mv["ar"] / mv["sd"]
    else:
        f["ar_vs_z"] = np.nan
    lv = np.log(vol.where(vol > 0) + 1.0)
    lroll = lv.rolling(WINDOW, min_periods=WINDOW)
    f["vol_z"] = (lv - lroll.mean().shift(1)) / lroll.std().shift(1)
    f["rv_ratio"] = r.rolling(5).std() / r.rolling(WINDOW, min_periods=WINDOW).std()
    f["dd20"] = close / close.rolling(20, min_periods=1).max() - 1
    f["adv60_usd"] = (close * vol).rolling(WINDOW, min_periods=20).mean()
    return f


def trading_days(prices: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    return prices[MARKET].index


def last_bar(prices: dict[str, pd.DataFrame], as_of: date | str) -> date | None:
    idx = trading_days(prices)
    idx = idx[idx <= pd.Timestamp(_d(as_of))]
    return idx[-1].date() if len(idx) else None
