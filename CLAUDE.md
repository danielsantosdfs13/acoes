# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Brazilian stock/day-trade technical analysis tool ("Day Trade SMC"). It combines Smart Money Concepts (SMC), Price Action, Moving Averages, and VWAP readings into a confluence score, exposed as a Streamlit web app, with the signals it produces persisted and scored for hit rate. All comments, docstrings, UI text, and commit messages in this repo are in Portuguese — match that when editing existing files.

## Two things to know before editing anything

**1. The Streamlit app is frozen (2026-08-10).** It stays up and keeps working — Plotly charts, the Scanner and retro-check have no equivalent anywhere else — but it gets no new features. New capability goes to the agents platform (`../../agents-runtime` + `homelab/applications/agents/`), which consumes this repo through the API. Bug fixes here are fine; new UI is not.

**2. `backend/api.py` is now an agent interface, not just an HTTP API.** Its `/openapi.json` is snapshotted into `homelab/applications/agents/configmap-agentgateway-openapi-acoes.yaml` and converted to MCP tools by the agentgateway. Concretely: `operation_id` is a **tool name** and `description` is the text a model reads to decide whether to call it — renaming either is a breaking interface change, not a cosmetic edit. `include_in_schema=False` on a route means "not a tool", never "not a route": those routes are still served and supported (see the reason next to each). Full contract in the comment block at the top of `api.py`.

See `docs/homelab-pipeline.md` for the full design and implementation status of the homelab MT5 data pipeline (`backend/`, `scraper/`) mentioned throughout this file.

## Repository layout

- `daytrade_smc.py` — the entire analysis engine (~2,530 lines), plus a small CLI that prints a one-symbol report. No UI code lives here: the Tkinter GUI was removed in the 2026-08-06 cleanup, since the Streamlit app had superseded it. `streamlit_app.py` intentionally only imports from this file rather than duplicating logic.
- `streamlit_app.py` — the web UI layer. Imports functions/constants from `daytrade_smc` and renders charts (Plotly), signal panels, the Scanner, and the retro-check mode. Contains no analysis logic of its own.
- `requirements.txt` — cloud-safe dependencies (Streamlit Cloud runs on Linux).
- `requirements-local.txt` — Windows-only extra (`MetaTrader5`, a DLL binding). Never merge this into `requirements.txt` — installing it on the Linux cloud deploy breaks the build.
- `daytrade_symbols.json` (generated at runtime next to `daytrade_smc.py`, not committed) — persisted watchlist, written by `save_symbols`/`load_symbols`, used only when `ACOES_API_URL` isn't configured. On Streamlit Cloud this resets on every redeploy since the filesystem isn't durable.
- `backend/` — the FastAPI side of the homelab pipeline, built as one image (`acoes-backend`) with **four entrypoints**, chosen per-workload via `command:`: `processor.py` (write path — `POST /candles`, plus `GET /watchlist` for the scraper), `api.py` (read path — `GET /candles`, `GET`/`PUT /watchlist`, `GET /status`, `POST /analisar`, plus `/profiles` and `/signals`), `analyzer.py` (the signal worker — sweeps the watchlist writing `signals`, then evaluates pending outcomes; also has a one-shot `--backfill` that reconstructs signals from the candles already stored), and `migrate.py` (applies `schema.sql`, run as an ArgoCD PreSync hook). `db.py`, `models.py`, `auth.py` and `candles.py` are shared — `candles.py` holds the two candle readers the engine consumes, extracted from `analyzer.py` in 2026-08-10 when `POST /analisar` came to need the same thing. Reuse them rather than writing a third reader: the "drop the still-forming candle" guard in `ler_candles` is the trap that would silently poison the whole hit-rate dataset if duplicated and then diverged. Keeping the migration in the same image as the app is what stops schema and code drifting apart between deploys. The image builds **from the repo root** (`docker build -f backend/Dockerfile .`) because `analyzer.py` runs the engine, and `daytrade_smc.py` lives outside `backend/`.
- `scraper/` — runs continuously on a Windows VM alongside a logged-in MT5 terminal, polling `daytrade_smc.fetch_ohlcv(..., source="MetaTrader 5")` and POSTing to `backend/processor.py`. The only component that does **not** run in k3s (it needs the MT5 Windows DLL). It is now the **sole caller** of `source="MetaTrader 5"` — see the `DATA_SOURCES` note below before touching that branch.
- `Dockerfile.streamlit` — image `acoes-streamlit`. Installs only `requirements.txt`: no MetaTrader5, and **no Postgres driver**. The app reaches data exclusively through the `api` service over HTTP; the absence of `psycopg` here is the guard that keeps that decoupling from silently regressing.
- `Makefile` + `scripts/init-tenant-db.sh` — the release path. There is no registry: images are built locally and imported straight into the k3s containerd (`docker save | sudo k3s ctr images import -`), then the tag is stamped into the manifests. **The Makefile must never gain an `apply` target** — see its header for the measured incident behind that rule.
- Kubernetes manifests do **not** live in this repo. They are in the `homelab` repo under `applications/acoes/`, delivered by ArgoCD.

## Running

No test suite or lint config exists in this repo.

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py          # web UI, the entry point

python daytrade_smc.py VALE3 --timeframe M15 --count 250 --risco 500   # CLI report, one symbol
```

For the MetaTrader 5 data source (Windows only, same machine as a logged-in MT5 terminal):
```bash
pip install -r requirements-local.txt
```

## Architecture: the data sources

`fetch_ohlcv(symbol, timeframe, count, source)` in `daytrade_smc.py` dispatches on `source`. **`DATA_SOURCES` is the UI menu, not the dispatch table** — the two deliberately differ, and conflating them breaks the pipeline:

1. **"Homelab (API)"** (`_fetch_ohlcv_api`) — calls `GET /candles` on the homelab `api` service, which serves what the `scraper/` persisted into the shared TimescaleDB (see `backend/` and `scraper/` above). Near-real-time (the scraper polls every few seconds), and the recommended path. Configured via `st.secrets["acoes_api_url"]`/`["acoes_api_key"]` or the `ACOES_API_URL`/`ACOES_API_KEY` env vars (env vars matter because the container in k3s has no mounted `secrets.toml`), wired into the module-level `ACOES_API_URL`/`ACOES_API_KEY` globals at the top of `streamlit_app.py`. `load_symbols()`/`save_symbols()` also become API-backed (`GET`/`PUT /watchlist`) whenever `ACOES_API_URL` is set, falling back to the local JSON file otherwise. **This source speaks HTTP, never SQL.** It used to connect to Postgres directly with `psycopg`; that was replaced so no client outside `backend/` holds a DB credential. `save_symbols` maps to `PUT /watchlist` (not `POST`) because it has always had replace-the-whole-list semantics, which a per-symbol POST/DELETE pair can't express atomically.
2. **"Yahoo Finance"** (`_fetch_ohlcv_yahoo`) — the fallback: works anywhere, no homelab needed, ~15-20min delayed. H4 candles aren't native to Yahoo, so they're synthesized by fetching H1 and resampling (`_resample_to_h4`), anchored to local midnight (Brasília). It is the only rate-limited source, which is why the Scanner's inter-symbol pause (`_PAUSA_YAHOO` in `streamlit_app.py`) is charged to it **by name** — the guard used to be written as "everyone except MT5", so each new source was born paying Yahoo's toll by default.
3. **"MetaTrader 5"** (`_fetch_ohlcv_mt5`) — **dispatches, but is not in `DATA_SOURCES`.** It's a local DLL binding, not a network API, so it only works on the machine with an open, logged-in MT5 terminal — never the web app, which never runs there. `scraper/scraper.py` is its sole caller and the pipeline's whole ingestion path. Removing this branch because it's absent from the UI menu would silently kill all data collection.

Which source the sidebar defaults to depends on config: Homelab when `ACOES_API_URL` is set, Yahoo when not (`source_select` in the session-state block). The top-of-page delay warning is chosen from the selected source for the same reason — hardcoded to Yahoo's text, it announced a 20-minute delay over near-real-time data.

Two sources were removed on 2026-08-06: the `"GitHub (MT5 de casa)"` bridge (it read a `data/mt5_snapshot.json` written by an `mt5_bridge/update_data.py` that never existed in this repo — broken, not merely dormant) and its `.github/workflows/mt5-update.yml`. Don't reintroduce a snapshot-over-git data path; the homelab pipeline is the answer to that problem.

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
- `@st.cache_data` wraps the per-source fetch+analyze calls — one fixed function per source (`_cached_mtf_api` at 3s, `_cached_mtf_yahoo` at 60s), never a dynamically decorated one. When adding a data source, follow that pattern rather than caching inside `daytrade_smc.py`. `_ultima_vela` (the sidebar freshness caption, 30s) is cached the same way because it runs on every rerun including auto-refresh ones.
- Auto-refresh on the individual-analysis view uses `st.fragment` (`_auto_refresh_30/60/120/300` in `streamlit_app.py`) so only that panel reruns, not the whole page.
- Analysis parameters cross `@st.cache_data` as a **tuple of pairs** (`AnalysisParams.to_items()`), never as the dataclass and never via a module global. Streamlit's hasher guarantees tuples of primitives; a dataclass either raises `UnhashableParamError` or — worse — gets hashed by identity, which fails silently: switching profiles would keep serving the previous profile's scores for the whole TTL, and the Scanner would rank on them.
- Anything that changes which profile is selected (the save/remove buttons) writes a **pending key** (`perfil_pendente`) and reruns, rather than assigning `perfil_select` directly — the selectbox already exists by then, and Streamlit forbids mutating a widget's key after instantiation. Same shape as the existing `jump_to_symbol`. The profile selectbox also needs its session-state default set explicitly, or it silently selects the alphabetically-first profile instead of `padrão`.

## Analysis parameters and signal history

`AnalysisParams` (frozen dataclass in `daytrade_smc.py`) holds ~30 curated engine thresholds. It lives in that file, not a new module, because `Makefile` ships exactly `daytrade_smc.py` + `scraper/{scraper,config}.py` to the Windows VM — a new first-party import would break the scraper silently.

It reaches every scoring/risk function through a single trailing `params` field on `MarketContext`, so those signatures never changed. Defaults reproduce the old behavior exactly; `scripts/conferir-refactor-params.py` proves it by running both engines over the same series and requiring zero differences. Run it before touching any of those numbers — there is no test suite.

Two couplings to preserve: `normalizacao_score` and `filtro_isolada_score_max` are both 79.0 by construction (an isolated reading is capped at 79 so it normalizes to exactly 1.0), and `alternative_targets`' viability threshold is wired to `rr_alvo_1`.

`POST /analisar` runs the same engine on demand (it's the tool behind "how does VALE3 look right now?") and deliberately **persists nothing** — consultation is not measurement, and writing there would mix readings nobody traded into the hit rate. Its payloads carry `origem='consulta'`, a value that exists in no row of `signals`, precisely so a response forwarded into `POST /signals` shows up as an anomaly instead of passing for `'manual'`. It also analyses the confirmation timeframes even when they weren't asked for, discarding their readings: without that, asking only for D1 would report `mtf_confirmado=false` because nobody looked, in the same field where false otherwise means the timeframes disagree.

Signals are persisted by three producers — the `analyzer` worker (`origem='worker'`), the UI's "salvar sinal" button (`'manual'`), and the one-shot historical pass `analyzer.py --backfill` (`'backfill'`) — which must emit identical rows, so all three build the body with the single `signal_payload()` in `daytrade_smc.py`. `origem` is part of the dedup key precisely so the three can describe the same candle without colliding, and so real-time measurement stays separable from the reconstructed one. Because the backfill writes old candles with today's `criado_em`, `GET /signals` and `/signals/stats` window on **`candle_time`**, never `criado_em`. Unlike the watchlist and profiles, signals have **no local-file fallback**: a hit rate computed over whichever half a process happened to see is worse than no hit rate. Dedup is the unique index on `(symbol, timeframe, modalidade, candle_time, perfil, origem)`; `candle_time` is always UTC, and building it from the displayed (Brasília) timestamp would silently create a second row three hours off.
