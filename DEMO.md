# youFuhnance — 2-minute demo script

## Setup (once, before going on stage)
```bash
# Needs a DB with market prices + covenant actions (the covenant-desk/market-desk branch DB). Replays are NOT re-run.
python3 scripts/dashboard.py --open --db path/to/youfin.db     # builds data/dashboard.html (~1 min) and opens it
python3 scripts/brief.py --as-of 2025-09-07 --counterparty tricolor --db path/to/youfin.db
```
`data/dashboard.html` is committed and self-contained: if the build fails, just open it.

## Pitch (about 2 minutes)
1. **Problem (15s).** Tricolor and First Brands blew up in September 2025. Private-credit lenders found out from headlines, after the market and after the filing. A deal team wants to know *first*, with proof, and know what to do.
2. **Live-web agents (25s).** Scout searches the live web (You.com) for each counterparty in our book. Verifier clusters the hits, scores source tier (SEC filing > FT/WSJ > trade press > social), and only promotes to ALERT when independent sources agree. Agents talk over a message bus (signals, events, alerts, actions).
3. **Receipts (25s).** Dashboard: drag the slider to Sep 7, Tricolor. The ALERT is an SEC 8-K from Origin Bank: "total loan commitments to Tricolor of approximately $30.1 million". Every claim carries a quote, tier chip and link. Nothing is a bare assertion.
4. **Market lead time (30s).** Tricolor: first verified ALERT Sep 7, Chapter 7 Sep 10, market proxies (OBK) broke 4 sessions after our alert. First Brands: first verified ALERT Sep 2 (FT/Deloitte invoice-factoring review), Chapter 11 Sep 25, the market basket broke 24 sessions later. Show the market-window badge: OPEN -> CLOSING and the hedge table (with caveats).
5. **Action layer (25s).** Click "Action brief" on Sep 9 and Sep 25: affected positions and exposure, market window, hedge candidates, and the covenant desk's checklist (draw stop, reservation of rights...). Close: "alert, proof, price, next step, before the filing".

## What to click
- Header cards: the two lead-time numbers.
- Counterparty selector: Tricolor, then First Brands.
- Slider (or Play): Sep 2 (First Brands ALERT) -> Sep 6 watch -> Sep 7 Tricolor ALERT -> Sep 9 -> Sep 10 filing -> Sep 20-25.
- Timeline strip: first WATCH, first ALERT, market break, filing.
- Event cards: tier chips, quotes, source links. Feed: who -> topic. Chart at bottom: the market study.
- Theme button toggles dark/light.

## Honest caveats
- The portfolio is **hypothetical**; counterparties are real public companies, positions/sizes/terms are invented.
- **Replay integrity:** sources are scored on as-of text (Wayback snapshots / publish-date filtering), and every panel only uses data dated <= the slider day. Market assessments use only sessions closed before the day. Verdicts shown come from the verification history as of that day.
- **Proxies were chosen with hindsight** (we knew who was exposed). A live system must pick them before the event.
- Two case studies, not a backtest. The "market break" definition (BOCPD on a proxy; basket Markov regime for First Brands) is ours.
- Hedge ideas are analysis, not trade instructions (MNPI/compliance check first); covenant output is not legal advice.

## What's next
- Exit-liquidity desk (who could buy us out, at what discount, how fast).
- Live mode: scheduled Scout runs, alerts pushed to the team instead of replayed.
- NAV loans: extend the counterparty/position model to fund-level and GP exposures.

*Analysis for the deal team — not investment or legal advice.*
