# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Korean stock (KOSPI/KOSDAQ) screening and alert bot. It runs continuously in a Docker
container on a Synology NAS, screens stocks daily using KIS (Korea Investment Securities)
market data + GPT evaluation, posts recommendations/alerts to Discord, and manages a
paper-trading lifecycle (entry sizing, trailing stops, partial exits, sell signals) for
each pick. There is no web UI — Discord is the entire user interface, both for outbound
alerts and inbound commands.

## Running it

This is deployed via Docker Compose, not run locally with a venv.

```bash
docker compose up -d --build     # build + start (rebuild needed after requirements.txt changes)
docker compose restart           # after editing main.py or src/** (volume-mounted, no rebuild needed)
docker compose logs -f           # tail logs (also written to logs/app.log, rotated daily/30d retention)
docker exec stock-alert-bot python build_db.py                        # manually rebuild the all-stocks name/ticker DB
docker exec stock-alert-bot python run_backtest.py --start 20260101 --end 20260410 [--top 50]
```

`docker-compose.yml` mounts `./src`, `./main.py`, `./run_backtest.py`, `./logs`, `./data`
directly into the container — code edits take effect on `docker compose restart` alone.
`network_mode: host` is used (NAS networking constraint). Memory is capped at 512M.

There is no pytest suite. The root-level `test_*.py` files are standalone manual scripts
(run directly with `python test_x.py`, several hit the live KIS API) used for ad-hoc API
exploration, not an automated test suite — don't try to run them as a batch or treat
failures as CI-relevant.

Config is entirely env-driven via `.env` (KIS keys, OpenAI key, Discord webhooks/bot
token, schedule times, screening thresholds like `MIN_STOCK_PRICE`/`MIN_MARKET_CAP`/
`MAX_SPREAD_PCT`, risk params like `STOP_LOSS_RATIO`/`TAKE_PROFIT_RATIO`). `KIS_IS_REAL`
toggles between the real KIS endpoint and the mock-trading endpoint — check this before
assuming any KIS call touches real infrastructure.

## Architecture

### Scheduler-driven, single process (`main.py`)

`main.py` is the sole entry point. It uses the `schedule` library (not cron/Celery) in a
`while True: schedule.run_pending(); sleep(30)` loop, running in KST. On startup it also
spawns the Discord bot (`src/notifier/discord_bot.py`) on a background thread for
interactive commands, and a background thread to load the stock name/ticker DB.

The daily job sequence (all times KST, weekdays only unless noted) tells the story of the
trading day: `06:30` US market close data → `07:00` overnight news scan → `08:40` premarket
analysis → `09:05` stock DB rebuild → `09:07` macro judgment (risk-on/off) → `09:10` morning
screening + picks (the main event) → `13:00` afternoon supplementary screening → every
`interval` (default 30) min: realtime target/stop/trailing checks + watchlist timing checks
→ every 5 min: surge detection → `15:35` closing stop-loss check → `16:00` closing summary →
`16:05` supply/demand data collection → `16:10` simulation P&L update → Friday `16:30` weekly
review + strategy learning.

`today_picks` and `macro_result` are in-memory module-level globals shared across scheduled
jobs within one process run; `today_picks` is also persisted to `data/today_picks.json` and
restored on container restart (`_restore_today_picks`) since a NAS reboot must not lose the
day's picks.

### Screening pipeline (`src/analyzers/screener.py`)

`StockScreener.run()` is the core pipeline, called from both the 09:10 morning job and the
13:00 afternoon job (with stricter thresholds the second time):

1. Fetch current market regime (`regime_classifier.py`, cached 6h in `data/market_regime.json`)
   and adjust `final_picks` count / `min_score` / tech-vs-supply weighting based on it
   (bear/trending_down/sideways/trending_up).
2. Apply previously learned rules (`failure_analyzer.py` → `data/learned_rules.json`) —
   sector exclusions/boosts derived from past simulated trade outcomes.
3. Collect candidates from KIS volume ranking, filtered for ETF/ETN/SPAC/penny-stock/
   small-cap/wide-spread/imminent-earnings names.
4. Per-candidate technical (`technical.py`) + supply/demand (`supply_demand.py`) scoring,
   plus several point adjustments (52-week high position, consecutive-up-day fatigue,
   regime-preferred sector type, learned sector weight).
5. Top-N by score go through GPT evaluation (`ai_evaluator.py`); only 강력매수/매수 survive.
6. Sector diversity filter, KOSPI/KOSDAQ balance filter, news-sentiment filter, then
   position sizing (`utils/position_sizer.py`) and ATR-based entry price calc
   (`utils/entry_calculator.py`).
7. Picks beyond the cutoff (and strong picks that don't make the cut) are auto-registered
   into a watchlist for pullback re-entry tracking (`utils/watchlist.py`), not discarded.

### Post-pick trade lifecycle

Once a stock is picked, several independent mechanisms track it going forward — these are
separate modules coordinated from `main.py`, not one class:

- `utils/trailing_stop.py` — partial exit at first target (sell half, move stop to
  breakeven), second partial exit + trailing activation, then trailing-stop-triggered final
  exit; on final trailing exit, strong scorers get re-registered into the watchlist for a
  possible re-entry after a pullback.
- `utils/sell_signal.py` — independently watches user-declared *actual* holdings
  (`data/holdings.json`, managed via the Discord `!보유` command) for sell signals, separate
  from the bot's own picks.
- `utils/trade_simulator.py` + `utils/detailed_collector.py` — every pick is also registered
  as a paper trade and tracked to closure purely for performance measurement/learning,
  independent of the trailing-stop alerts sent to Discord.
- `utils/failure_analyzer.py` reads the accumulated simulated trade history and produces
  `data/learned_rules.json`, which `screener.py` applies on the *next* run — this is the
  bot's self-adjusting feedback loop, driven weekly by `utils/weekly_review.py` (Friday
  16:30 job) but read every screening run.

### External integrations

- `src/api/kis_client.py` — singleton KIS REST client; caches the OAuth token at the class
  level (not per-instance) since KIS recommends only one token issuance per day; switches
  real/mock base URL from `KIS_IS_REAL`.
- `src/analyzers/ai_evaluator.py` / `macro_agent.py` — GPT calls (OpenAI SDK) for per-stock
  evaluation and daily macro risk-on/off judgment; `macro_agent.py` also pulls VIX/USDKRW/
  US10Y/NASDAQ/S&P500 via `utils/yahoo_direct.py` (direct Yahoo chart API calls, not the
  `yfinance` package, for reliability from the NAS).
- `src/notifier/discord.py` — outbound webhook notifications (embeds) for picks, alerts,
  summaries.
- `src/notifier/discord_bot.py` — inbound bot (discord.py) with Korean-named prefix commands
  (`!분석`, `!비교`, `!주간리포트`, `!전략적용`, `!전략현황`, `!스크리닝`, `!데이터수집`,
  `!학습규칙`, `!시뮬레이션`, `!추적`, `!보유`, `!트레일링`, `!국면`, `!IC분석`, `!포지션`,
  `!도움말`) — this is the primary way a human inspects/overrides bot state at runtime.

### State persistence (`data/`)

Everything the bot needs to survive a container restart lives in `data/*.json`, volume-mounted
from the NAS host — treat this directory as a lightweight database, not scratch output:
`stock_db.json` (all-stock name↔ticker map), `today_picks.json`, `active_trades.json`,
`trailing_stops.json`, `watchlist.json`, `holdings.json`, `market_regime.json` (6h cache),
`learned_rules.json`, `performance.json`, `trade_history.json`/`detailed_trades.json`
(simulation ledger for learning), `supply_history/` (daily supply/demand snapshots for
backtesting accuracy).

### Backtesting

`src/backtest/engine.py`, invoked via `run_backtest.py`, is a separate offline path from the
live scheduler — it does not share state with the live pipeline's trailing-stop/simulator
mechanics; it's used to validate strategy changes against historical data before they're
allowed to affect live screening thresholds.
