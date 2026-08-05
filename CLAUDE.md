# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Brazilian stock/day-trade technical analysis tool ("Day Trade SMC"). It combines Smart Money Concepts (SMC), Price Action, Moving Averages, and VWAP readings into a confluence score, exposed both as a Tkinter desktop GUI and as a Streamlit web app. All comments, docstrings, UI text, and commit messages in this repo are in Portuguese — match that when editing existing files.

See `docs/homelab-pipeline.md` for the full design and implementation status of the homelab MT5 data pipeline (`backend/`, `scraper/`) mentioned throughout this file.

## Repository layout

- `daytrade_smc.py` — the entire analysis engine (~2,480 lines), plus a Tkinter GUI and a CLI. This is treated as the original, hand-built engine; `streamlit_app.py` intentionally only imports from it rather than duplicating logic.
- `streamlit_app.py` — the web UI layer. Imports functions/constants from `daytrade_smc` and renders charts (Plotly), signal panels, the Scanner, and the retro-check mode. Contains no analysis logic of its own.
- `requirements.txt` — cloud-safe dependencies (Streamlit Cloud runs on Linux).
- `requirements-local.txt` — Windows-only extra (`MetaTrader5`, a DLL binding). Never merge this into `requirements.txt` — installing it on the Linux cloud deploy breaks the build.
- `.github/workflows/mt5-update.yml` — a `workflow_dispatch`-only Action that runs on a **self-hosted runner** (the user's home PC), meant to invoke `mt5_bridge/update_data.py` and commit `data/mt5_snapshot.json`. Note: neither `mt5_bridge/update_data.py` nor `data/mt5_snapshot.json` currently exist in this repo — the "GitHub (MT5 de casa)" data source and this workflow reference a bridge script that was never finished. **Superseded by the homelab pipeline below but left in place, dormant, on purpose.**
- `daytrade_symbols.json` (generated at runtime next to `daytrade_smc.py`, not committed) — persisted watchlist, written by `save_symbols`/`load_symbols`, used only when `ACOES_API_URL` isn't configured. On Streamlit Cloud this resets on every redeploy since the filesystem isn't durable.
- `backend/` — the FastAPI side of the homelab pipeline, built as one image (`acoes-backend`) with **four entrypoints**, chosen per-workload via `command:`: `processor.py` (write path — `POST /candles`, plus `GET /watchlist` for the scraper), `api.py` (read path — `GET /candles`, `GET`/`PUT /watchlist`, `GET /status`, plus `/profiles` and `/signals`), `analyzer.py` (the signal worker — sweeps the watchlist writing `signals`, then evaluates pending outcomes; also has a one-shot `--backfill` that reconstructs signals from the candles already stored), and `migrate.py` (applies `schema.sql`, run as an ArgoCD PreSync hook). `db.py`, `models.py` and `auth.py` are shared. Keeping the migration in the same image as the app is what stops schema and code drifting apart between deploys. The image builds **from the repo root** (`docker build -f backend/Dockerfile .`) because `analyzer.py` runs the engine, and `daytrade_smc.py` lives outside `backend/`.
- `scraper/` — runs continuously on a Windows VM alongside a logged-in MT5 terminal, polling `daytrade_smc.fetch_ohlcv(..., source="MetaTrader 5")` and POSTing to `backend/processor.py`. The only component that does **not** run in k3s (it needs the MT5 Windows DLL). Deliberately named differently from `mt5_bridge/` (see above) since it's a different execution model — a persistent loop, not a one-shot GitHub Actions job.
- `Dockerfile.streamlit` — image `acoes-streamlit`. Installs only `requirements.txt`: no MetaTrader5, and **no Postgres driver**. The app reaches data exclusively through the `api` service over HTTP; the absence of `psycopg` here is the guard that keeps that decoupling from silently regressing.
- `Makefile` + `scripts/init-tenant-db.sh` — the release path. There is no registry: images are built locally and imported straight into the k3s containerd (`docker save | sudo k3s ctr images import -`), then the tag is stamped into the manifests. **The Makefile must never gain an `apply` target** — see its header for the measured incident behind that rule.
- Kubernetes manifests do **not** live in this repo. They are in the `homelab` repo under `applications/acoes/`, delivered by ArgoCD.

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
4. **"Homelab (API)"** (`_fetch_ohlcv_api`) — calls `GET /candles` on the homelab `api` service, which serves what the `scraper/` persisted into the shared TimescaleDB (see `backend/` and `scraper/` above). Near-real-time (the scraper polls every few seconds), and this is the recommended path once the homelab is set up: no GitHub Actions round-trip, no on-demand button. Configured via `st.secrets["acoes_api_url"]`/`["acoes_api_key"]` or the `ACOES_API_URL`/`ACOES_API_KEY` env vars (env vars matter because the container in k3s has no mounted `secrets.toml`), wired into the module-level `ACOES_API_URL`/`ACOES_API_KEY` globals at the top of `streamlit_app.py` — same pattern as `GITHUB_BRIDGE_REPO`/`GITHUB_BRIDGE_TOKEN`. `load_symbols()`/`save_symbols()` also become API-backed (`GET`/`PUT /watchlist`) whenever `ACOES_API_URL` is set, falling back to the local JSON file otherwise.

**This source speaks HTTP, never SQL.** It used to connect to Postgres directly with `psycopg`; that was replaced so no client outside `backend/` holds a DB credential. `save_symbols` maps to `PUT /watchlist` (not `POST`) because it has always had replace-the-whole-list semantics, which a per-symbol POST/DELETE pair can't express atomically.

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
- `@st.cache_data(ttl=60, ...)` wraps the per-source fetch+analyze calls (`_cached_mtf_yahoo`/`_cached_mtf_mt5`/`_cached_mtf_github`/`_cached_mtf_api`); when adding a new data source, follow the same pattern rather than caching inside `daytrade_smc.py`.
- Auto-refresh on the individual-analysis view uses `st.fragment` (`_auto_refresh_30/60/120/300` in `streamlit_app.py`) so only that panel reruns, not the whole page.
- Analysis parameters cross `@st.cache_data` as a **tuple of pairs** (`AnalysisParams.to_items()`), never as the dataclass and never via a module global. Streamlit's hasher guarantees tuples of primitives; a dataclass either raises `UnhashableParamError` or — worse — gets hashed by identity, which fails silently: switching profiles would keep serving the previous profile's scores for the whole TTL, and the Scanner would rank on them.
- Anything that changes which profile is selected (the save/remove buttons) writes a **pending key** (`perfil_pendente`) and reruns, rather than assigning `perfil_select` directly — the selectbox already exists by then, and Streamlit forbids mutating a widget's key after instantiation. Same shape as the existing `jump_to_symbol`. The profile selectbox also needs its session-state default set explicitly, or it silently selects the alphabetically-first profile instead of `padrão`.

## Analysis parameters and signal history

`AnalysisParams` (frozen dataclass in `daytrade_smc.py`) holds ~30 curated engine thresholds. It lives in that file, not a new module, because `Makefile` ships exactly `daytrade_smc.py` + `scraper/{scraper,config}.py` to the Windows VM — a new first-party import would break the scraper silently.

It reaches every scoring/risk function through a single trailing `params` field on `MarketContext`, so those signatures never changed. Defaults reproduce the old behavior exactly; `scripts/conferir-refactor-params.py` proves it by running both engines over the same series and requiring zero differences. Run it before touching any of those numbers — there is no test suite.

Two couplings to preserve: `normalizacao_score` and `filtro_isolada_score_max` are both 79.0 by construction (an isolated reading is capped at 79 so it normalizes to exactly 1.0), and `alternative_targets`' viability threshold is wired to `rr_alvo_1`.

Signals are persisted by three producers — the `analyzer` worker (`origem='worker'`), the UI's "salvar sinal" button (`'manual'`), and the one-shot historical pass `analyzer.py --backfill` (`'backfill'`) — which must emit identical rows, so all three build the body with the single `signal_payload()` in `daytrade_smc.py`. `origem` is part of the dedup key precisely so the three can describe the same candle without colliding, and so real-time measurement stays separable from the reconstructed one. Because the backfill writes old candles with today's `criado_em`, `GET /signals` and `/signals/stats` window on **`candle_time`**, never `criado_em`. Unlike the watchlist and profiles, signals have **no local-file fallback**: a hit rate computed over whichever half a process happened to see is worse than no hit rate. Dedup is the unique index on `(symbol, timeframe, modalidade, candle_time, perfil, origem)`; `candle_time` is always UTC, and building it from the displayed (Brasília) timestamp would silently create a second row three hours off.
