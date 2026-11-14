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
- [x] **Verifier agent**: merges syndicated copies into events, rates sources by tier, corroborates via You.com, quotes a receipt for every source
- [ ] Sector/Ratings · Contagion mapper · Morning brief
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

## First Brands replay result (see `data/replay_first_brands.log`)
| Date | Lead | Signal |
|---|---|---|
| **2025-09-01** | **T-24d** | FT: Jefferies among private credit investors exposed; Deloitte appointed to review invoice factoring. **First `high` alert** (`diligence_red_flag`) |
| 2025-09-03 | T-22d | Deloitte quality-of-earnings review confirmed by multiple outlets (EN/DE) |
| 2025-09-12 | T-13d | Apollo short position reported; loans slump |
| 2025-09-18 to 09-20 | T-7 to T-5d | Creditors hire advisers; company "goes quiet"; **CRITICAL** |
| 2025-09-22 | T-3d | WSJ/Bloomberg: preparing a possible bankruptcy filing |
| 2025-09-25 | T-0 | Chapter 11 |

Cost: 99 searches plus 36 pages, about $0.53.

```bash
python scripts/scout.py replay --counterparty first_brands --start 2025-07-01 --end 2025-09-25 \
    --step 7 --daily-from 2025-09-01 --event 2025-09-25 --fetch 4 --min-severity 2
# After changing rules in youfin/risk_tags.py, re-score stored hits without new API calls:
python scripts/scout.py retag
```

## Verifier agent
Runs day by day over Scout signals (severity 3 and above):
1. **Cluster:** syndicated copies of one story become one *event* (same claim within 2 days, similar titles,
   or similar embeddings: see *Embedding clustering* below).
2. **Verify:** for unconfirmed high/critical events, a You.com corroboration search limited to articles
   published *up to that day*. Hits count only if they support the same claim.
3. **Judge:** source tiers (`youfin/sources.py`), counting syndicated outlets once:
   tier 0 primary (SEC, courts, rating agencies) · tier 1 major press · tier 2 trade/wire · tier 3 aggregator/social.

| Verdict | Rule | Alert (severity >= 4) |
|---|---|---|
| `confirmed` | any tier 0/1 source | **ALERT** |
| `corroborated` | 2+ independent tier 2 sources | **ALERT** |
| `weakly_corroborated` | 2+ sources, including aggregators/social | watch |
| `single_source` | one publisher | watch |

```bash
python scripts/verify.py replay --start 2025-07-01 --end 2025-09-25 \
    --event tricolor=2025-09-10 --event first_brands=2025-09-25
python scripts/verify.py report --min-level ALERT --event tricolor=2025-09-10 --event first_brands=2025-09-25
python scripts/verify.py reset   # clear events to re-run
```

**Result** (`data/verify_alerts.txt`, 10 corroboration searches, about $0.05):
- **Tricolor:** the Sep 6 "bank seizes Tricolor" story stays **watch** (only aggregator sources).
  The first **verified ALERT is Sep 7 (T-3d)**: Origin Bank's SEC 8-K, *"total loan commitments to
  Tricolor of approximately $30.1 million"*.
- **First Brands:** the first **verified ALERT is Sep 2 (T-23d)**: FT, Jefferies exposed to First Brands invoices
  (6 publishers). The Sep 12 Apollo short is confirmed (9 publishers) but stays watch (severity elevated).

**Known limitations**
- Replays use pages as they look *today*: a snippet can contain later links (e.g. Fitch's July page lists
  the later CCC downgrade). Publish dates are enforced, but snippet text can leak.
- Paywalled pages (FT) can return navigation text in the quote.
- Clustering keeps the same-tag-within-2-days rule, which can still merge two different developments
  that share a risk tag. `nomic-embed-text` is English-first: Spanish copies score low and stay separate.

## Embedding clustering

`youfin/embed.py` embeds each article's as-of text (title + description) with a local Ollama model
(`ollama pull nomic-embed-text`, ~270 MB; `YOUFIN_EMBED_MODEL`, `off` disables; nomic's `clustering: `
prefix). Vectors are cached in SQLite (`embeddings`). A new signal is compared with the closest article
already in each candidate event (same counterparty, within 2 days):

| cosine | decision |
|---|---|
| >= 0.94 (`YOUFIN_EMBED_HIGH`) | same event |
| 0.87 to 0.94 (`YOUFIN_EMBED_LOW`) | ask llama3.1 `same_event` (replay: title + description only) |
| < 0.87 | different, unless the old tag/title rule merges (it keeps precedence) |

Signals below severity 3 never open an event or reach the LLM, but join one at cos >= 0.94. If Ollama or the
model is missing, the previous rules run unchanged. `~ EMB` lines in the report show the cosine and rule.

**Calibration** (`python3 scripts/calibrate_embed.py`, 159 stored signals): syndicated copies 0.88 to 1.00
(median 0.98); different developments of the same company median 0.77, p90 0.84, max 0.93; the Apollo
paraphrases 0.87 to 0.88. The first guess (0.70/0.85) would merge 8% of different-development pairs
outright and send 70% to the LLM.

**Models never upgrade a verdict.** `event_sources.merge_basis` records how each source joined
(`opened | rule | embedding_high | embedding_high_xtag | llm_borderline | corroboration`). Sources attached
by the LLM, or by an embedding match across primary tags, stay on the event ("attached by a model, not
counted") but `judge()` ignores them and they do not raise severity. `embedding_high` (same primary tag)
would count, but the same-tag rule already merges those, so in practice it never occurs.

| Replay 07-01 to 09-25 | before | after |
|---|---|---|
| First Brands events (ALERT / watch) | 8 (5 / 3) | 7 (5 / 2) |
| Tricolor events (ALERT / watch / log) | 7 (4 / 2 / 1) | 8 (4 / 3 / 1) |
| LLM `same_event` calls | 6 | 5 |
| cluster time, cold caches | 14.7 s | 12.8 s (2.9 s of it embedding) |
| first verified ALERT | First Brands 09-02, Tricolor 09-07 | unchanged |

Known misses: "Apollo Global's High-Stakes Bet" (09-12) has no risk keywords (severity 0) and scores 0.88
against the Apollo event, so it stays out. "Diameter and Apollo unwind shorts" (09-23) now joins "Refi on
Pause" (09-22) via the same-tag rule. The Spanish Univision copy (cos 0.72) is its own event.

## Agent team bus

Scout and Verifier talk over `youfin/bus.py` (SQLite `bus_messages` + per-agent cursors; the DB writes stay as before).

| topic | publisher | content |
|---|---|---|
| `signals` | Scout | one message per new signal (id, counterparty, severity, tags); local only |
| `events` | Verifier | event created or verdict/alert level changed (headline, verdict, receipt url + quote) |
| `alerts` | Verifier | event first becomes ALERT |
| `brief` | Verifier | one line per active day |

`YOUFIN_BUS=local` (default) keeps everything in SQLite. `YOUFIN_BUS=mirror` also posts `events`, `alerts`, `brief`
to the OpenCrawl board as topics `youfin-events|alerts|brief`, from identities `youfin:scout` / `youfin:verifier`
(session ids kept in `bus_agents`). If the CLI fails the mirror switches itself off and the run continues locally.
`YOUFIN_BUS_MAX_POSTS` (default 30) caps board posts per DB.

```
python3 scripts/team.py replay --start 2025-08-25 --end 2025-09-25 --from-db   # zero You.com cost for Scout
python3 scripts/team.py status    # bus counts + registered board agents
python3 scripts/team.py leave     # leave the board
```

## Replay integrity

Replays enforce publish dates, but You.com returns pages as they look *today*, which can
mention later events (e.g. Fitch's July First Brands page now links to its later downgrade).
In replay mode, signals and corroboration hits are therefore scored on text that existed on
the replay date: the last Internet Archive snapshot at/before that date (`youfin/archive.py`,
cached in `archive_snapshots`, no API key) plus title + description. With no snapshot, only
title + description are used; today's snippets/markdown/highlights are dropped. The Wayback
lookup is made only for signals with severity >= 3 and for corroboration hits. The basis is
stored in `signals.score_basis` / `event_sources.score_basis` (`snapshot` | `title_desc` |
`live`). Re-score stored data with `python3 scripts/scout.py retag --as-of-replay`.
Live mode is unchanged.
