# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Brazilian stock/day-trade technical analysis tool ("Day Trade SMC"). It combines Smart Money Concepts (SMC), Price Action, Moving Averages, and VWAP readings into a confluence score, exposed as a Streamlit web app, with the signals it produces persisted and scored for hit rate. All comments, docstrings, UI text, and commit messages in this repo are in Portuguese — match that when editing existing files.

## Two things to know before editing anything

**1. The Streamlit app is in maintenance, not frozen (revised 2026-08-11).** The 2026-08-10 rule said "no new UI"; it was overtaken the next day by the signal-tracking screens and then by a full navigation/layout redesign, so it no longer described the repo. The rule that actually holds: **new analytical capability goes to the agents platform** (`../../agents-runtime` + `homelab/applications/agents/`), which consumes this repo through the API — the app is not where new engine features land. Fixing, restructuring and de-densifying the existing screens is in scope; growing a seventh screen that duplicates something an agent should do is not.

**2. `backend/api.py` is now an agent interface, not just an HTTP API.** Its `/openapi.json` is snapshotted into `homelab/applications/agents/configmap-agentgateway-openapi-acoes.yaml` and converted to MCP tools by the agentgateway. Concretely: `operation_id` is a **tool name** and `description` is the text a model reads to decide whether to call it — renaming either is a breaking interface change, not a cosmetic edit. `include_in_schema=False` on a route means "not a tool", never "not a route": those routes are still served and supported (see the reason next to each). Full contract in the comment block at the top of `api.py`.

See `docs/homelab-pipeline.md` for the full design and implementation status of the homelab MT5 data pipeline (`backend/`, `scraper/`) mentioned throughout this file.

## Repository layout

- `daytrade_smc.py` — the entire analysis engine (~2,530 lines), plus a small CLI that prints a one-symbol report. No UI code lives here: the Tkinter GUI was removed in the 2026-08-06 cleanup, since the Streamlit app had superseded it. `streamlit_app.py` intentionally only imports from this file rather than duplicating logic.
- `streamlit_app.py` — the web UI layer. Imports functions/constants from `daytrade_smc` and renders charts (Plotly), signal panels, the Scanner, and the retro-check screen. Contains no analysis logic of its own. Six routes under four nav groups since the 2026-08-11 redesign (`/`, `/scanner`, `/ativo`, `/retroativa`, `/acompanhar`, `/assertividade`) — see "Streamlit-specific gotchas" for why the route lives in the URL path.
- `.streamlit/config.toml` — the theme. Not cosmetic: it declares the dark palette `build_chart` had always assumed, and `Dockerfile.streamlit` needs its own `COPY` line for it to reach k3s.
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
3. **"MetaTrader 5"** (`_fetch_ohlcv_mt5`) — **dispatches, but is not in `DATA_SOURCES`.** It's a local DLL binding, not a network API, so it only works on the machine with an open, logged-in MT5 terminal — never the web app, which never runs there. `scraper/scraper.py` is its sole caller and the pipeline's whole ingestion path. Removing this branch because it's absent from the UI menu would silently kill all data collection. **Timezone gotcha (fixed 2026-08-10):** MT5 returns bar times in the server's *local* wall clock as if it were UTC (for Clear, Brasília, UTC-3). `_fetch_ohlcv_mt5` must interpret the epoch as `LOCAL_TZ` and convert to UTC (`tz_localize(LOCAL_TZ).tz_convert("UTC")`) — reverting to a plain `utc=True` reintroduces a 3h shift that silently defeats the `ler_candles` forming-candle guard and makes signals on forming candles. See the runbook in `docs/homelab-pipeline.md` ("O fuso do servidor MT5").

Which source the sidebar defaults to depends on config: Homelab when `ACOES_API_URL` is set, Yahoo when not (`source_select` in the session-state block). The top-of-page delay warning is chosen from the selected source for the same reason — hardcoded to Yahoo's text, it announced a 20-minute delay over near-real-time data.

Two sources were removed on 2026-08-06: the `"GitHub (MT5 de casa)"` bridge (it read a `data/mt5_snapshot.json` written by an `mt5_bridge/update_data.py` that never existed in this repo — broken, not merely dormant) and its `.github/workflows/mt5-update.yml`. Don't reintroduce a snapshot-over-git data path; the homelab pipeline is the answer to that problem.

## Architecture: the analysis pipeline

1. `fetch_ohlcv` → raw OHLCV `DataFrame`.
2. `build_context(df)` → a `MarketContext` dataclass: ATR/ATR%, RVOL, volatility bucket, EMAs (9/21/50/200), daily VWAP + slope/distance/rejection, swing highs/lows (`detect_swings`), BOS/CHoCH structure events (`detect_structure`), candle patterns, breakout/retest flags, FVG setup, IFR (`compute_rsi`) plus the Diário's IFR when the caller supplies it (`higher_rsi`).
3. Five independent signal generators consume `MarketContext`, each returning a `Signal` (direction, score, confidence, reasons, alerts, `RiskPlan`):
   - `smc_signal` — structure/BOS-CHoCH/FVG based.
   - `price_action_signal` — candle patterns + breakout/retest.
   - `moving_average_signal` — EMA stack/slope.
   - `vwap_signal` — VWAP distance/slope/rejection.
   - `rsi_signal` — IFR **exhaustion only** (≤10 / ≥90 by default), so NEUTRO is its normal answer. There is deliberately no "approaching the zone" band: a reading that scores outside the extreme is just another trend-follower, which the other four already are.
   - `confluence_signal` — combines the **four structural** readings into one blended read. **The IFR is not one of them** — see below.
4. `apply_market_filter` clamps direction/score/confidence based on volatility regime (e.g. low volatility blocks entries entirely; isolated single-modality reads are capped below "confirmed" thresholds).
5. `attach_risk` fills in each signal's `RiskPlan` (entry/stop/targets/RR) — stops come from `structural_stop`/`stop_for_signal`, targets from `alternative_targets` (ATR, Fibonacci, structure, statistical expectancy — the same formulas across all timeframes/styles, they just scale with the data).
6. `analyze(df)` runs the full pipeline for one timeframe; `analyze_symbol_mtf(...)` runs it across the confirmation + context timeframes for one symbol, returning a `MultiTimeframeResult`.

**D1 is analysed first, everywhere.** Its IFR is injected as `higher_rsi` into every other timeframe (`rsi_signal` discounts a reading by 0.55 when the Diário is exhausted the opposite way). `analyze_symbol_mtf`, the `analyzer` worker's sweep and `POST /analisar` all reorder for this — `/analisar` pulls D1 in even when nobody asked for it, the same way it already pulls the confirmation timeframes, so the agent and the UI never disagree about the same candle. The one place that can't: `analyzer.py --backfill` and `check_signal_as_of` walk a single timeframe candle-by-candle, where reconstructing the Diário as-of each past bar would risk peeking at the future. They pass `higher_rsi=None`, so a `'backfill'` row never carries the opposing-timeframe discount that a `'worker'` row does — `origem` is in the dedup key, so the two stay separable.

## Multi-timeframe confirmation & operating style

Two "styles," each requiring a different pair of timeframes to agree before a signal counts as confirmed (`STYLES` dict in `streamlit_app.py`, `DAYTRADE_*`/`SWING_*` constants in `daytrade_smc.py`):

| Style | Required agreement | Context only |
|---|---|---|
| Day Trade | M15 + H1 | H4, D1 |
| Swing Trade | D1 + W1 | H4 |

If the two required timeframes disagree, the final recommendation is forced to NEUTRO even if one timeframe looks strong alone. This is enforced identically in both the individual-analysis badge and the Scanner's "Confirmado" column.

## Mini Índice (WINFUT)

Not a screen of its own since 2026-08-11: **`WINFUT` is just an entry in the asset selector**, and `/ativo` swaps to its timeframes via `_estilo_do_ativo`. Its own timeframes are confirmation M5+M15, context M2+H1 (`WINFUT_*` in `daytrade_smc.py`). M2/M5 were added to `TIMEFRAMES`, `DEFAULT_TF_COUNTS`, `_MT5_TIMEFRAME_MAP_NAMES` and `_ANALISE_COUNTS` for it, and are used by nothing else.

**`"WINFUT"` is a logical name, not a ticker.** `yahoo_symbol` passes it through untouched and Yahoo has no such symbol, so the mode is **Homelab-only** and the UI blocks it on any other source rather than surfacing a confusing "symbol not found". The MT5 contract name is broker-specific and rolls quarterly (`WIN$` continuous vs `WINZ25`); the scraper translates it via `SCRAPER_SYMBOL_MT5`, so the stored series stays continuous across the roll while only the mapping changes.

`SCRAPER_TIMEFRAMES_POR_SYMBOL` exists because the scraper loop is a `symbol × timeframe` cross product — putting M2/M5 in the global `SCRAPER_TIMEFRAMES` would collect them for all eleven stocks too, two extra MT5 calls per symbol per loop for data no stock screen reads.

The sidebar's "Estilo" control is built from `STYLES.keys()`, so the WINFUT style deliberately lives in `_ESTILOS_TODOS` instead; resolve styles through `estilo(nome)`, never `STYLES[...]`, or selecting WINFUT raises `KeyError`.

**Known limit:** the `analyzer` worker does *not* record WINFUT signals on WINFUT's timeframes. Its `CONFIRMACAO` and `TIMEFRAMES_VARRIDOS` are global and Day Trade only, so adding WINFUT to the watchlist gets it swept at M15/H1/H4/D1 with M15+H1 confirmation. Live analysis in the UI is correct; the hit-rate history for the mini index would need per-symbol confirmation in the worker.

## Modality filter

The sidebar "Leitura" selector (named "Modalidade" before the 2026-08-11 redesign; the session key is still `modality_select`) picks which signal drives the recommendation: one specific reading (Confluência/SMC/Price Action/Médias Móveis/VWAP/IFR), or `ALL_MODALITIES_OPTION` ("Todas as modalidades"), which averages the score across the **aggregable** readings (`overall_score`) and takes a majority vote on direction (`overall_direction`, tie → NEUTRO). The Scanner always ranks by this overall score.

## The IFR is a sixth reading, but it aggregates into nothing

`rsi_signal` is in `MODALITIES`, is selectable, gets its own `signals` rows and its own Scanner column — but it is excluded from **both** aggregates: `confluence_signal` never receives it (`analyze` computes the confluence over the four structural readings, *then* appends the IFR), and `overall_score`/`overall_direction`/`overall_agreement` filter it out via `MODALIDADES_FORA_DO_AGREGADO`.

That is not tidiness, it is two measured regressions. The IFR is a contrarian exhaustion read that is NEUTRO almost always (0 firings in 20 real series, matching the upstream audit's 0-in-60):

- **In the confluence**, it diluted every score by ~10-12% while contributing no information, and broke comparability with every row already stored for that modality.
- **In the aggregate**, it was worse: `overall_direction` needs an *absolute* majority, so a reading that never votes raised the bar from 3-of-5 to 4-of-6. Measured: the general direction collapsed to NEUTRO in **9 of 20 series** and the Score Geral fell ~6 points — a silent tightening of the criterion that nobody chose, in the default modality that also ranks the Scanner.

`overall_agreement` filters by the same rule, or the screen would print "3 de 6" next to a direction decided by a 3-of-5 vote. If another non-voting reading is ever added, put it in `MODALIDADES_FORA_DO_AGREGADO` too.

Upstream (`kleverson01/acoes`) reached the same conclusion for the confluence and documents it in its README, but did **not** apply it to `overall_*`, so it still carries the second defect.

## Streamlit-specific gotchas to preserve

- Session-state defaults (`watchlist`, `symbol_select`, `jump_to_symbol`) are set up **before** the sidebar widgets are created — Streamlit doesn't let you mutate a widget's bound session-state key after the widget exists in the same run.
- `@st.cache_data` wraps the per-source fetch+analyze calls — one fixed function per source (`_cached_mtf_api` at 3s, `_cached_mtf_yahoo` at 60s), never a dynamically decorated one. When adding a data source, follow that pattern rather than caching inside `daytrade_smc.py`. `_ultima_vela` (the sidebar freshness caption, 30s) is cached the same way because it runs on every rerun including auto-refresh ones.
- Auto-refresh on the individual-analysis view uses `st.fragment` (`_auto_refresh_30/60/120/300` in `streamlit_app.py`) so only that panel reruns, not the whole page.
- Analysis parameters cross `@st.cache_data` as a **tuple of pairs** (`AnalysisParams.to_items()`), never as the dataclass and never via a module global. Streamlit's hasher guarantees tuples of primitives; a dataclass either raises `UnhashableParamError` or — worse — gets hashed by identity, which fails silently: switching profiles would keep serving the previous profile's scores for the whole TTL, and the Scanner would rank on them.
- Anything that changes which profile is selected (the save/remove buttons) writes a **pending key** (`perfil_pendente`) and reruns, rather than assigning `perfil_select` directly — the selectbox already exists by then, and Streamlit forbids mutating a widget's key after instantiation. Same shape as the existing `jump_to_symbol`. The profile selectbox also needs its session-state default set explicitly, or it silently selects the alphabetically-first profile instead of `padrão`.
- **The route lives in the URL path, never in a query param** (`st.navigation` + `st.Page` with callables, `position="hidden"`). This is what makes the browser's back button work: page-level back/forward was fixed (streamlit#5293 → PR #6271), while query-param back/forward is still broken (streamlit#13963, open, and specific to apps using `st.navigation` — the URL changes, the rerun happens, and `st.query_params` still returns the stale value). Consequence: `symbol`/`perfil`/`modality` ride in query params for shareable links and are **read but never written** — writing them on change would fill the history with entries that #13963 cannot restore, so back would appear to do nothing. `_ALIAS_MODOS` + `_redirecionar_link_antigo` translate the pre-2026-08-11 `?mode=` links; deleting that map silently sends old shared links to the landing page.
- **Everything runs in one file.** `Dockerfile.streamlit` copies exactly `daytrade_smc.py` + `streamlit_app.py` (plus `.streamlit/`), and `st.Page` accepts callables, so there is no `pages/` directory. `st.Page` callables take **no arguments** — what the sidebar computes reaches them through the module-level `CTX` dict, which is why `st.navigation()` runs *before* the sidebar and `pg.run()` *after* it.
- The nav pills reflect the URL, not session state: `_render_nav` re-seeds `nav_grupo`/`nav_sub` **only when `_nav_url_vista` differs from the current route**. Re-seeding every rerun would erase the user's click before it could be read — on the click's rerun the URL is still the old one, and `st.switch_page` is what changes it. Those two widgets deliberately pass no `default=`, since the key always exists by then and passing both makes Streamlit warn on every rerun.
- **Never `st.tabs` to choose between expensive bodies** — tabs execute every tab's body on every rerun. The Dashboard used to put one profile per tab, so a four-profile setup ran five full watchlist sweeps per widget click; the individual-analysis view nested `expander → tabs → panel` and rendered all five reading panels to show one. Both are now `st.segmented_control`, which preserves the if/else.
- Cards are `st.container(border=True)`, never an HTML `<div>` opened in one `st.markdown` and closed in another: Streamlit renders each element in its own DOM block, so that pattern draws a stray line above and below instead of a border around the widgets.
- `.streamlit/config.toml` declares the dark theme that `build_chart` already assumed (`plotly_dark` on `#0a0e13`). Without it the chart is dark inside a light page. Its colors and the `PALETA` dict at the top of `streamlit_app.py` are the same values — change one, change the other.

## Analysis parameters and signal history

`AnalysisParams` (frozen dataclass in `daytrade_smc.py`) holds ~35 curated engine thresholds. It lives in that file, not a new module, because `Makefile` ships exactly `daytrade_smc.py` + `scraper/{scraper,config}.py` to the Windows VM — a new first-party import would break the scraper silently.

It reaches every scoring/risk function through a single trailing `params` field on `MarketContext`, so those signatures never changed. `scripts/conferir-refactor-params.py` runs two engines over the same series and diffs them field by field. Run it before touching any of those numbers — there is no test suite. It compares signals **positionally** and gives up when the count differs, so a change that adds or removes a reading needs a name-matched comparison instead.

Two couplings to preserve: `normalizacao_score` and `filtro_isolada_score_max` are both 79.0 by construction (an isolated reading is capped at 79 so it normalizes to exactly 1.0), and `alternative_targets`' viability threshold is wired to `rr_alvo_1`.

`from_dict` pads and truncates tuple fields to the default's length. That is load-bearing, not defensive: `multiplicador_concordancia` went from 5 entries to 6 when the IFR arrived, and a profile saved before that with a customised tuple would otherwise `IndexError` the first time five readings agreed — months later, in the worker, on a rare path.

`atr_suavizacao` exists so `params_hash` can tell the two ATR conventions apart. `compute_atr` used a plain rolling mean until 2026-08-10, when it moved to Wilder's smoothing (SMMA/RMA, the MT5/TradingView convention). Since `atr_periodo` stayed 14, without this field the old and new hit-rate rows would have been indistinguishable. `"simples"` reproduces the old behaviour for comparison; it is not an operating mode, which is why it has no sidebar widget. **The ATR reaches further than stops:** `detect_structure` validates BOS/CHoCH against `amplitude/ATR ≥ estrutura_range_min`, so changing the ATR changes which structure events count at all — measured on real B3 series, the two conventions differ by ~8% on average and >20% at the 95th percentile, worst on M15.

`STYLE_RSI_THRESHOLDS` + `params_para_estilo` give Swing Trade 20/80 and Day Trade 10/90, but **only when the profile left both at the default** — a profile that picked its own thresholds wins. The style-adjusted params are what the UI hashes and what gets stored, so a Swing signal stays distinguishable from a Day Trade one. The `analyzer` worker never calls this: it is Day Trade only (`CONFIRMACAO` fixed at M15+H1).

`POST /analisar` runs the same engine on demand (it's the tool behind "how does VALE3 look right now?") and deliberately **persists nothing** — consultation is not measurement, and writing there would mix readings nobody traded into the hit rate. Its payloads carry `origem='consulta'`, a value that exists in no row of `signals`, precisely so a response forwarded into `POST /signals` shows up as an anomaly instead of passing for `'manual'`. It also analyses the confirmation timeframes even when they weren't asked for, discarding their readings: without that, asking only for D1 would report `mtf_confirmado=false` because nobody looked, in the same field where false otherwise means the timeframes disagree.

Signals are persisted by three producers — the `analyzer` worker (`origem='worker'`), the UI's "salvar sinal" button (`'manual'`), and the one-shot historical pass `analyzer.py --backfill` (`'backfill'`) — which must emit identical rows, so all three build the body with the single `signal_payload()` in `daytrade_smc.py`. `origem` is part of the dedup key precisely so the three can describe the same candle without colliding, and so real-time measurement stays separable from the reconstructed one. Because the backfill writes old candles with today's `criado_em`, `GET /signals` and `/signals/stats` window on **`candle_time`**, never `criado_em`. Unlike the watchlist and profiles, signals have **no local-file fallback**: a hit rate computed over whichever half a process happened to see is worse than no hit rate. Dedup is the unique index on `(symbol, timeframe, modalidade, candle_time, perfil, origem)`; `candle_time` is always UTC, and building it from the displayed (Brasília) timestamp would silently create a second row three hours off.

## Operator feedback (`signal_feedback`)

The decision a person makes about a signal — ACOMPANHAR, OPERAR, IGNORAR, OPEREI, CANCELEI — lives in `signal_feedback`, one row per decision, **not** a column on `signals`. The table is a history: a signal can be marked ACOMPANHAR and later OPEREI, and "the" feedback of a signal is always the **most recent** row. Everything that reads it resolves that with the same `LEFT JOIN LATERAL … ORDER BY criado_em DESC LIMIT 1` (`_FEEDBACK_LATERAL` in `api.py`) — a `GROUP BY` would answer a different question.

`GET /signals` returns each signal already carrying `feedback` + `feedback_origem`, and both it and `/signals/stats` accept an `acao` filter; `/signals/stats` also returns a `por_feedback` breakdown. That exists because the interesting question is "did I do better on the ones I chose to trade than on the average?", and it is unanswerable while the join happens in the client. Until 2026-08-11 the Streamlit screen fetched signals and feedbacks separately and crossed the two lists in the browser, so the answer was limited to whatever fit inside both pagination limits at once.

**`acao=PENDENTE` is a query value, not a stored one.** No row has `acao='PENDENTE'`; it is how you ask for "nobody decided anything about this signal", and it is also the label `por_feedback` groups those under.

**`feedback_origem='auto'` is not a human decision.** The `analyzer` worker applies the `auto_acompanhamento` rules at the end of each sweep (`aplicar_auto_acompanhamento`), writing ACOMPANHAR with `origem='auto'`. Folding that into `'web'` would destroy the very measurement the feedback exists to enable — the same reason `origem='consulta'` exists on the `/analisar` payloads. Three constraints in that SQL are load-bearing: it only touches signals created **during that sweep** (a new rule must not retroactively invent decisions over resolved history), it skips any signal that already has feedback (a human decision wins), and it uses `NOT EXISTS` rather than `ON CONFLICT` because the table deliberately has no unique key per signal.

Two bugs behind the "everything is pending forever" symptom, both fixed 2026-08-11 and both worth not reintroducing: `save_feedback`/`fetch_feedback` in `daytrade_smc.py` used `requests` without the late `import requests` every other API helper in that file has, so the write path raised `NameError` on every click; and the rules table plus its two endpoints shipped without ever changing `analyzer.py`, so rules were written and never read. The screen hid both behind a bare `except Exception: fb_map = {}` — which is why the acompanhamento screen must never swallow a fetch failure again: "no feedback" and "could not read feedback" render identically and only one of them is a number you can trust.
