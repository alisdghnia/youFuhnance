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
- Sticky top bar: counterparty, slider/Play, status chip for the day (e.g. "ALERT · window CLOSING · covenant stage 2/4") and the small lead-time numbers.
- Left (60%, decision material): the three market charts (returns + news pins, window heatmap, probabilities; future is blank), then "Today for the team", then the action checklist (covenant actions + hedge candidates). Try Tricolor Sep 5 -> 7 -> 10 and First Brands Sep 2 -> 22.
- Right (40%, own scroll): live feed, newest first: verified events (verdict badges, tier chips, receipt quotes/links), then agent messages (who -> topic). Today's new items are highlighted.
- Play (speed 0.5x-4x), Space = play/pause, left/right arrows = step a day. Deep link: `dashboard.html#cp=tricolor&d=2025-09-07`.
- Theme button toggles dark/light.

## Honest caveats
- **Replay integrity:** pretrained models' cutoffs predate the events and no model is ever trained on our data; `python3 scripts/leak_check.py` deletes everything dated after D from a DB copy and proves market, covenant, brief and dashboard outputs for D are identical (PASS on Tricolor Sep 5/7, First Brands Sep 2/19).
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
