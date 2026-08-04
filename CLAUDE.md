# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Brazilian stock/day-trade technical analysis tool ("Day Trade SMC"). It combines Smart Money Concepts (SMC), Price Action, Moving Averages, and VWAP readings into a confluence score, exposed both as a Tkinter desktop GUI and as a Streamlit web app. All comments, docstrings, UI text, and commit messages in this repo are in Portuguese — match that when editing existing files.

See `docs/homelab-pipeline.md` for the full design and implementation status of the homelab MT5 data pipeline (`processor/`, `collector/`, `deploy/`) mentioned throughout this file.

## Repository layout

- `daytrade_smc.py` — the entire analysis engine (~2,480 lines), plus a Tkinter GUI and a CLI. This is treated as the original, hand-built engine; `streamlit_app.py` intentionally only imports from it rather than duplicating logic.
- `streamlit_app.py` — the web UI layer. Imports functions/constants from `daytrade_smc` and renders charts (Plotly), signal panels, the Scanner, and the retro-check mode. Contains no analysis logic of its own.
- `requirements.txt` — cloud-safe dependencies (Streamlit Cloud runs on Linux).
- `requirements-local.txt` — Windows-only extra (`MetaTrader5`, a DLL binding). Never merge this into `requirements.txt` — installing it on the Linux cloud deploy breaks the build.
- `.github/workflows/mt5-update.yml` — a `workflow_dispatch`-only Action that runs on a **self-hosted runner** (the user's home PC), meant to invoke `mt5_bridge/update_data.py` and commit `data/mt5_snapshot.json`. Note: neither `mt5_bridge/update_data.py` nor `data/mt5_snapshot.json` currently exist in this repo — the "GitHub (MT5 de casa)" data source and this workflow reference a bridge script that was never finished. **Superseded by the homelab pipeline below but left in place, dormant, on purpose.**
- `daytrade_symbols.json` (generated at runtime next to `daytrade_smc.py`, not committed) — persisted watchlist, written by `save_symbols`/`load_symbols`, used only when `HOMELAB_DB_DSN` isn't configured. On Streamlit Cloud this resets on every redeploy since the filesystem isn't durable.
- `processor/` — a small FastAPI service (new, homelab-only) that ingests candle batches over HTTP and upserts them into Postgres/TimescaleDB; also exposes `/watchlist` so the Windows collector can discover which symbols to poll without ever holding DB credentials.
- `collector/` — a script meant to run continuously on a Windows VM alongside a logged-in MT5 terminal, polling `daytrade_smc.fetch_ohlcv(..., source="MetaTrader 5")` and POSTing results to `processor/`. Deliberately named differently from `mt5_bridge/` (see above) since it's a different execution model — a persistent loop, not a one-shot GitHub Actions job.
- `deploy/` — Docker Compose stack for the homelab side: TimescaleDB + `processor/` + the Streamlit app itself (which also runs in the homelab now, not Streamlit Cloud, once `HOMELAB_DB_DSN` is set). `deploy/init/001_schema.sql` defines the `candles` hypertable and `watchlist` table.
- `requirements-homelab.txt` — extra dependency (`psycopg`) needed only where the app/processor actually connect to Postgres; kept separate from `requirements.txt`/`requirements-local.txt` for the same reason those two are split.

## Running

No test suite or lint config exists in this repo.

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py          # web UI, default entry point

python daytrade_smc.py                  # opens the Tkinter desktop GUI
python daytrade_smc.py VALE3 --timeframe M15 --count 250 --risco 500   # CLI report, one symbol
```

For the MetaTrader 5 data source (Windows only, same machine as a logged-in MT5 terminal):
```bash
pip install -r requirements-local.txt
```

## Architecture: the four data sources

`fetch_ohlcv(symbol, timeframe, count, source)` in `daytrade_smc.py` dispatches to one of four backends, selected by the user in the Streamlit sidebar (`DATA_SOURCES`):

1. **"Yahoo Finance"** (`_fetch_ohlcv_yahoo`) — default, works anywhere (cloud or local), ~15-20min delayed. H4 candles aren't native to Yahoo, so they're synthesized by fetching H1 and resampling (`_resample_to_h4`), anchored to local midnight (Brasília).
2. **"MetaTrader 5"** (`_fetch_ohlcv_mt5`) — real-time, but only works when the app runs on the same machine as an open, logged-in MT5 terminal (it's a local DLL binding, not a network API). Raises a clear `RuntimeError` rather than crashing when run elsewhere (e.g. Streamlit Cloud).
3. **"GitHub (MT5 de casa)"** (`_fetch_ohlcv_github`) — a half-finished bridge for using real MT5 data *from the cloud* via a GitHub Actions self-hosted runner and a committed JSON snapshot. Superseded by source 4 below; kept dormant, not removed.
4. **"Homelab (Postgres)"** (`_fetch_ohlcv_homelab`) — reads candles persisted in Postgres/TimescaleDB by the `collector/` script running continuously on a homelab Windows VM (see `processor/` and `collector/` above). Near-real-time (collector polls every few seconds), and this is the recommended path once the homelab is set up: no GitHub Actions round-trip, no on-demand button. Configured via `st.secrets["homelab_db_dsn"]` or the `HOMELAB_DB_DSN` env var (env var matters here because the Docker container running the app in the homelab won't have a mounted `secrets.toml`), wired into the module-level `HOMELAB_DB_DSN` global at the top of `streamlit_app.py` — same pattern as `GITHUB_BRIDGE_REPO`/`GITHUB_BRIDGE_TOKEN`. `load_symbols()`/`save_symbols()` also become DB-backed (reading/writing the `watchlist` table) whenever `HOMELAB_DB_DSN` is set, falling back to the local JSON file otherwise.

## Architecture: the analysis pipeline

1. `fetch_ohlcv` → raw OHLCV `DataFrame`.
2. `build_context(df)` → a `MarketContext` dataclass: ATR/ATR%, RVOL, volatility bucket, EMAs (9/21/50/200), daily VWAP + slope/distance/rejection, swing highs/lows (`detect_swings`), BOS/CHoCH structure events (`detect_structure`), candle patterns, breakout/retest flags, FVG setup.
3. Four independent signal generators consume `MarketContext`, each returning a `Signal` (direction, score, confidence, reasons, alerts, `RiskPlan`):
   - `smc_signal` — structure/BOS-CHoCH/FVG based.
   - `price_action_signal` — candle patterns + breakout/retest.
   - `moving_average_signal` — EMA stack/slope.
   - `vwap_signal` — VWAP distance/slope/rejection.
   - `confluence_signal` — combines the other four into one blended read.
4. `apply_market_filter` clamps direction/score/confidence based on volatility regime (e.g. low volatility blocks entries entirely; isolated single-modality reads are capped below "confirmed" thresholds).
5. `attach_risk` fills in each signal's `RiskPlan` (entry/stop/targets/RR) — stops come from `structural_stop`/`stop_for_signal`, targets from `alternative_targets` (ATR, Fibonacci, structure, statistical expectancy — the same formulas across all timeframes/styles, they just scale with the data).
6. `analyze(df)` runs the full pipeline for one timeframe; `analyze_symbol_mtf(...)` runs it across the confirmation + context timeframes for one symbol, returning a `MultiTimeframeResult`.

## Multi-timeframe confirmation & operating style

Two "styles," each requiring a different pair of timeframes to agree before a signal counts as confirmed (`STYLES` dict in `streamlit_app.py`, `DAYTRADE_*`/`SWING_*` constants in `daytrade_smc.py`):

| Style | Required agreement | Context only |
|---|---|---|
| Day Trade | M15 + H1 | H4, D1 |
| Swing Trade | D1 + W1 | H4 |

If the two required timeframes disagree, the final recommendation is forced to NEUTRO even if one timeframe looks strong alone. This is enforced identically in both the individual-analysis badge and the Scanner's "Confirmado" column.

## Modality filter

The sidebar "Modalidade" selector picks which signal drives the recommendation: one specific reading (Confluência/SMC/Price Action/Médias Móveis/VWAP), or `ALL_MODALITIES_OPTION` ("Todas as modalidades"), which averages the score across all five (`overall_score`) and takes a majority vote on direction (`overall_direction`, tie → NEUTRO). The Scanner always ranks by this overall score.

## Streamlit-specific gotchas to preserve

- Session-state defaults (`watchlist`, `symbol_select`, `jump_to_symbol`) are set up **before** the sidebar widgets are created — Streamlit doesn't let you mutate a widget's bound session-state key after the widget exists in the same run.
- `@st.cache_data(ttl=60, ...)` wraps the per-source fetch+analyze calls (`_cached_mtf_yahoo`/`_cached_mtf_mt5`/`_cached_mtf_github`/`_cached_mtf_homelab`); when adding a new data source, follow the same pattern rather than caching inside `daytrade_smc.py`.
- Auto-refresh on the individual-analysis view uses `st.fragment` (`_auto_refresh_30/60/120/300` in `streamlit_app.py`) so only that panel reruns, not the whole page.
