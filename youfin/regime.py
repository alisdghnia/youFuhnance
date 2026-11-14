"""Regime detection for the market desk: is the market ALREADY pricing a credit event?

1. BOCPD (Adams & MacKay 2007, "Bayesian Online Changepoint Detection"): Normal-Gamma prior,
   Student-t predictive, constant hazard 1/250. Run on standardized daily series (abnormal-
   return z, log-volume z), so one generic prior fits every ticker. Reported number:
   P(the current run started within the last 3 bars | data <= D), i.e. P(change point
   within the last 3 days). Baseline under no change is ~4/250 = 0.016.
2. Markov switching (Hamilton; statsmodels MarkovRegression): 2 regimes with switching mean
   and variance on an equal-weight basket of the counterparty's non-market proxies'
   abnormal returns, fit on an expanding window 2023-01-01..D. The stress regime is the
   high-variance one; P(stress) is its filtered probability at D (no smoothing past D).
   A fit that did not converge, has non-finite output, or whose two regimes are not
   separated in variance (the "stress" label would be arbitrary) yields no P(stress).
3. Window labels (open / closing / closed), from the thresholds below. Every label keeps
   the list of rules that fired, so a human can see why.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy.special import gammaln

HAZARD = 1 / 250
RECENT_BARS = 3
PRIOR = dict(mu=0.0, kappa=1.0, alpha=1.0, beta=1.0)   # data are z-scores: prior scale ~1
MS_START = "2023-01-01"
MS_MIN_SD_RATIO = 1.5      # stress sd / calm sd below this: the regimes are not distinct

# ------------------------------------------------------- window thresholds
# Credit stress shows up as NEGATIVE abnormal returns for every proxy role used here
# (exposed lenders, peers, customers, sector and credit ETFs), so the return rules are
# downside-only; an upside earnings pop is not the market pricing a credit loss. Volume
# has no sign, so it only counts on a down day.
# CLOSED  = the market has already repriced the risk (the cheap hedge is gone):
#   car5_z <= -3 (a 5-day abnormal fall of 3 sd), or
#   BOCPD(abnormal) >= 0.5 together with shock_z <= -3 (a regime break on a 3-sd down day).
# CLOSING = repricing has started; act now or accept a worse entry:
#   car5_z <= -2, shock_z <= -2.5, BOCPD(abnormal) >= 0.3 on a down move, or heavy-volume
#   selling: (vol_z >= 2 or BOCPD(volume) >= 0.5) on a day with ar_z <= -1. Volume alone
#   (earnings, index events, option expiry) does not make a hedge dearer, so it needs a down day.
# OPEN    = none of the above: the proxy is still trading normally.
T = dict(closed_car=3.0, closed_shock=3.0, closed_bocpd=0.5,
         closing_car=2.0, closing_shock=2.5, closing_bocpd=0.3,
         closing_vol=2.0, closing_vol_bocpd=0.5, closing_vol_ar=1.0)
# Overall (counterparty) label, from the DIRECT proxies (exposed_lender / peer / customer)
# and the basket regime (stress only counts while the basket is falling: car5 < 0):
#   closed  if basket P(stress) >= 0.8, or at least half of the direct proxies are closed;
#   closing if basket P(stress) >= 0.5, any direct proxy is closed, or 2+ are closing;
#   open    otherwise.
OVERALL = dict(closed_pstress=0.8, closed_share=0.5, closing_pstress=0.5, closing_count=2)
DIRECT_ROLES = ("exposed_lender", "peer", "customer")
BREAK_P = 0.5      # study: a proxy "breaks regime" when BOCPD(abnormal) >= 0.5 on a down move


# ------------------------------------------------------------------ BOCPD
def _student_t_logpdf(x: float, mu, kappa, alpha, beta):
    df = 2 * alpha
    scale2 = beta * (kappa + 1) / (alpha * kappa)
    z2 = (x - mu) ** 2 / scale2
    return (gammaln((df + 1) / 2) - gammaln(df / 2) - 0.5 * np.log(np.pi * df * scale2)
            - (df + 1) / 2 * np.log1p(z2 / df))


def bocpd_series(s: pd.Series, **kw) -> pd.Series:
    """BOCPD on a pandas series (index preserved); leading NaNs are skipped."""
    s = s.astype(float)
    first = s.first_valid_index()
    out = pd.Series(np.nan, index=s.index)
    if first is None:
        return out
    sub = s.loc[first:]
    out.loc[first:] = bocpd(sub.to_numpy(), **kw)
    return out


def bocpd(x: np.ndarray, hazard: float = HAZARD, prior: dict = PRIOR,
          recent: int = RECENT_BARS) -> np.ndarray:
    """P(change point within the last `recent` bars) after each observation.

    Run length r_t = number of observations in the current run. In the A&M recursion the
    change-point mass P(r_t = 0) equals the hazard; a break at bar s shows up as mass on
    r_t = t - s + 1 once x_s is better explained by a fresh run (prior predictive) than by
    the long run. We report P(r_t <= recent). Run lengths are tracked explicitly, so
    pruning negligible long runs is safe. NaN inputs carry the previous value forward and
    do not advance run lengths (`recent` then counts observations, not calendar bars).
    """
    n = len(x)
    out = np.full(n, np.nan)
    rl = np.array([0])
    p = np.array([1.0])
    mu, kappa = np.array([prior["mu"]]), np.array([prior["kappa"]])
    alpha, beta = np.array([prior["alpha"]]), np.array([prior["beta"]])
    last = np.nan
    for t in range(n):
        xt = x[t]
        if not np.isfinite(xt):
            out[t] = last
            continue
        logpred = _student_t_logpdf(xt, mu, kappa, alpha, beta)
        w = np.exp(logpred - logpred.max()) * p
        cp = w.sum() * hazard
        p = np.concatenate([[cp], w * (1 - hazard)])
        p /= p.sum()
        rl = np.concatenate([[0], rl + 1])
        mu_new = (kappa * mu + xt) / (kappa + 1)
        beta_new = beta + kappa * (xt - mu) ** 2 / (2 * (kappa + 1))
        mu = np.concatenate([[prior["mu"]], mu_new])
        kappa = np.concatenate([[prior["kappa"]], kappa + 1])
        alpha = np.concatenate([[prior["alpha"]], alpha + 0.5])
        beta = np.concatenate([[prior["beta"]], beta_new])
        keep = (p > 1e-12) | (rl <= recent + 1)
        if not keep.all():
            rl, p, mu, kappa, alpha, beta = (a[keep] for a in (rl, p, mu, kappa, alpha, beta))
            p /= p.sum()
        last = float(p[rl <= recent].sum())
        out[t] = last
    return out


# ------------------------------------------------------- Markov switching
def markov_stress(basket: pd.Series, as_of, start: str = MS_START,
                  start_params=None) -> dict:
    """Fit a 2-regime switching-variance model on basket[start..as_of]; P(stress) at as_of."""
    from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

    y = pd.to_numeric(basket, errors="coerce").astype(float)
    y = y.loc[start:pd.Timestamp(as_of)].dropna() * 100           # percent, for conditioning
    if len(y) < 120:
        return {"p_stress": None, "error": "too few bars"}
    mod = MarkovRegression(y.to_numpy(), k_regimes=2, trend="c", switching_variance=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            res = mod.fit(start_params=start_params, disp=False, maxiter=200)
            names = list(mod.param_names)
            params = np.asarray(res.params, dtype=float)
            sig = [float(params[names.index(f"sigma2[{k}]")]) for k in range(2)]
            stress = int(np.argmax(sig))
            p = float(np.asarray(res.filtered_marginal_probabilities)[-1, stress])
            stay = float(params[names.index(f"p[{stress}->{stress}]")]) if stress == 0 else \
                1.0 - float(params[names.index("p[1->0]")])
            converged = bool((getattr(res, "mle_retvals", None) or {}).get("converged", True))
        except Exception as e:  # numerical failure must not break an assessment
            return {"p_stress": None, "error": f"{type(e).__name__}: {e}"}
    info = {"n_bars": int(len(y)), "bar_date": y.index[-1].date().isoformat()}
    if not (np.isfinite(params).all() and np.isfinite(p) and min(sig) > 0):
        return {"p_stress": None, "error": "non-finite Markov fit", **info}
    if not converged:
        return {"p_stress": None, "error": "Markov fit did not converge", **info}
    ratio = float(np.sqrt(max(sig) / min(sig)))
    if ratio < MS_MIN_SD_RATIO:
        return {"p_stress": None, **info,
                "error": f"regimes not separated (sd ratio {ratio:.2f} < {MS_MIN_SD_RATIO:g})"}
    return {
        "p_stress": min(max(p, 0.0), 1.0),
        "sd_calm_pct": float(np.sqrt(min(sig))), "sd_stress_pct": float(np.sqrt(max(sig))),
        "mean_stress_pct": float(params[names.index(f"const[{stress}]")]),
        # P(stay in stress): near 0.4 means "fat-tail days", not a persistent stress state.
        "stress_persistence": stay,
        **info, "params": params,
    }


# --------------------------------------------------------------- labels
def classify_proxy(f: dict) -> tuple[str, list[str]]:
    """open / closing / closed for one proxy, plus the rules that fired."""
    car, shock, volz = _nz(f.get("car5_z")), _nz(f.get("shock_z")), _nz(f.get("vol_z"))
    arz = _nz(f.get("ar_z"))
    b_ar, b_vol = _nz(f.get("bocpd_ar")), _nz(f.get("bocpd_vol"))
    closed, closing = [], []
    if car <= -T["closed_car"]:
        closed.append(f"car5_z={car:.1f}<=-{T['closed_car']:g}")
    if b_ar >= T["closed_bocpd"] and shock <= -T["closed_shock"]:
        closed.append(f"bocpd_ar={b_ar:.2f}>={T['closed_bocpd']:g} & shock_z={shock:.1f}<=-{T['closed_shock']:g}")
    if closed:
        return "closed", closed
    if car <= -T["closing_car"]:
        closing.append(f"car5_z={car:.1f}<=-{T['closing_car']:g}")
    if shock <= -T["closing_shock"]:
        closing.append(f"shock_z={shock:.1f}<=-{T['closing_shock']:g}")
    if b_ar >= T["closing_bocpd"] and shock < 0:
        closing.append(f"bocpd_ar={b_ar:.2f}>={T['closing_bocpd']:g} (down)")
    if arz <= -T["closing_vol_ar"] and (volz >= T["closing_vol"] or b_vol >= T["closing_vol_bocpd"]):
        closing.append(f"heavy-volume selling: ar_z={arz:.1f}, vol_z={volz:.1f}, bocpd_vol={b_vol:.2f}")
    return ("closing", closing) if closing else ("open", ["no threshold crossed"])


def classify_overall(rows: list[dict], p_stress: float | None,
                     basket_car5: float | None) -> tuple[str, list[str]]:
    # Proxies without data ("n/a") are neither moving nor quiet: leave them out entirely.
    direct = [r for r in rows if r["role"] in DIRECT_ROLES and r.get("label") != "n/a"]
    closed = [r["ticker"] for r in direct if r["label"] == "closed"]
    hot = [r["ticker"] for r in direct if r["label"] != "open"]
    falling = _nz(basket_car5) < 0
    ps = _nz(p_stress) if falling else 0.0
    tag = f"basket P(stress)={_nz(p_stress):.2f}" + ("" if falling else " (basket not falling)")
    why = []
    if ps >= OVERALL["closed_pstress"]:
        why.append(f"{tag}>={OVERALL['closed_pstress']:g}")
    if direct and len(closed) / len(direct) >= OVERALL["closed_share"]:
        why.append(f"{len(closed)}/{len(direct)} direct proxies closed")
    if why:
        return "closed", why
    if ps >= OVERALL["closing_pstress"]:
        why.append(f"{tag}>={OVERALL['closing_pstress']:g}")
    if closed:
        why.append("closed: " + ", ".join(closed))
    if len(hot) >= OVERALL["closing_count"]:
        why.append(f"{len(hot)} direct proxies moving: " + ", ".join(hot))
    if why:
        return "closing", why
    return "open", [f"{tag}; direct proxies moving: {', '.join(hot) or 'none'}"]


def _nz(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if not np.isfinite(x) else x
