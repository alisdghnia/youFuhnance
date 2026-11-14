# youFuhnance

An agent team for a private-credit (ABF) deal team. It watches the public web for
news about the counterparties behind private positions, and every claim links to
its source. Built for the *Ground Truth* hackathon on the You.com API.

> The portfolio in `data/portfolio.json` is **hypothetical**. The counterparties are
> real public companies; all positions, sizes and terms are invented.

## Status
- [x] **Scout agent**: You.com Search sweeps per counterparty, with a no-look-ahead replay mode
- [x] SQLite store (`data/youfin.db`): counterparties, positions, runs, articles, signals
- [x] Deterministic risk tagger; each tag stores the terms that triggered it
- [ ] Verifier (corroboration) · Sector/Ratings · Contagion mapper · Morning brief
- [ ] OpenCrawl board as the inter-agent bus

## Setup
```bash
pip install -r requirements.txt
echo "YOU_COM_API_KEY=..." > .env      # gitignored
```

## Run
```bash
# Live sweep (last 7 days)
python scripts/scout.py live --counterparty tricolor

# Replay: Tricolor (Chapter 7 filed 2025-09-10). Weekly sweeps, then daily from Aug 25
python scripts/scout.py replay --counterparty tricolor --start 2025-05-01 --end 2025-09-10 \
    --step 7 --daily-from 2025-08-25 --event 2025-09-10 --fetch 4 --min-severity 2

# Re-print the timeline from the DB without calling the API
python scripts/scout.py timeline --counterparty tricolor --event 2025-09-10 --min-severity 2
```

## Tricolor replay result (see `data/replay_tricolor.log`)
| Date | Lead | Signal |
|---|---|---|
| 2025-06-04 to 06-18 | | KBRA / S&P ratings, $217M securitization (info) |
| **2025-09-06** | **T-4d** | Bank seizes Tricolor locations: **first CRITICAL alert** (low-quality sources, so the Verifier must corroborate) |
| 2025-09-07 | T-3d | Origin Bank SEC 8-K discloses $30.1M of loan commitments to Tricolor |
| 2025-09-08 | T-2d | Operations suspended, workforce furloughed (trade press) |
| 2025-09-09 | T-1d | Bankruptcy filing reported; bonds drop (Bloomberg) |
| 2025-09-10 | T-0 | Chapter 7; Fifth Third discloses a $200M fraud |

Cost of a full replay: 124 searches plus 45 pages fetched, about $0.67.
