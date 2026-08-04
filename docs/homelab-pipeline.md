# Pipeline de dados MT5 no homelab

Este documento formaliza a arquitetura da 4ª fonte de dados do "Day Trade
SMC": um coletor MT5 rodando continuamente numa VM Windows do homelab,
alimentando um banco Postgres/TimescaleDB via um serviço processor, que o
app de análise (rodando também no homelab) consome diretamente via SQL.

Serve como referência de continuidade — o que já foi implementado no
código, o que ainda depende de infraestrutura real (VM, Docker, rede) pra
funcionar de ponta a ponta, e as decisões de design por trás de cada peça.

## Por que isso existe

As três fontes de dados originais (`daytrade_smc.py:264`, `DATA_SOURCES`)
tinham uma lacuna: "Yahoo Finance" funciona em qualquer lugar mas com
15-20min de atraso; "MetaTrader 5" direto é tempo real mas só rodando na
mesma máquina do terminal; "GitHub (MT5 de casa)" tentava ser uma ponte
pra usar dado real do MT5 a partir da nuvem, mas nunca foi terminada
(`mt5_bridge/update_data.py` e `data/mt5_snapshot.json`, que o workflow
`.github/workflows/mt5-update.yml` espera, nunca existiram no repo) — e,
mesmo terminada, seria só sob demanda (botão), não contínua.

Com um homelab disponível, a solução deixa de depender do GitHub Actions
e passa a ser uma pipeline própria: VM Windows com MT5 aberto → coletor →
processor (HTTP) → Postgres/TimescaleDB → app (SQL direto).

## Decisões de arquitetura (já tomadas, não reabrir sem motivo novo)

| Decisão | Escolha | Por quê |
|---|---|---|
| Banco | PostgreSQL + TimescaleDB | Hypertable dá partição/retenção de série temporal de graça; SQL direto é trivial de consumir com `pandas.read_sql`. |
| Transporte coletor → processor | HTTP (sem fila) | Volume de dados é pequeno (poucas linhas por POST, LAN); fila adicionaria um componente de infra sem ganho real aqui. |
| Onde o app roda | No homelab, acesso remoto via Tailscale | Mesma rede do banco → SQL direto, sem expor banco pra internet. Reaproveita o padrão de Tailscale já documentado no README pro modo MT5 direto. |
| Cadência do coletor | Loop contínuo (poucos segundos) | Sensação de tempo quase real, no mesmo nível do modo MT5 direto local (que já roda a cada ~3s). |
| Credencial de banco | Só o processor e o app têm; o coletor nunca tem | O coletor (VM Windows, mais exposta por rodar o MT5) fala só HTTP com o processor — reduz superfície de risco. |
| Ponte GitHub existente | Mantida, intocada, dormente | Superseded em intenção por este pipeline, mas remoção é decisão separada, não tomada ainda. |

## Arquitetura

```
VM Windows (MT5 aberto)                         Homelab (Docker)
┌───────────────────────────┐   HTTP POST      ┌──────────────────────────┐
│ collector/collector.py     │ ──/candles────▶  │ processor/ (FastAPI)     │
│ (loop contínuo, serviço    │ ◀──/watchlist──  │                          │
│ via NSSM)                  │   HTTP GET       └───────────┬──────────────┘
└───────────────────────────┘                               │ SQL (psycopg)
                                                              ▼
                                                 ┌──────────────────────────┐
                                                 │ TimescaleDB (Postgres)   │
                                                 │ tabelas: candles,        │
                                                 │ watchlist                │
                                                 └───────────▲──────────────┘
                                                              │ SQL direto (DSN)
                                                 ┌───────────┴──────────────┐
                                                 │ streamlit_app.py         │
                                                 │ (daytrade_smc.py)        │
                                                 └──────────────────────────┘
```

O coletor nunca fala direto com o Postgres — só conhece a URL HTTP do
processor. O app fala direto com o Postgres via SQL. O processor é
principalmente um gateway de escrita (ingest) mais um endpoint de leitura
(`/watchlist`) que só o coletor precisa.

## Status da implementação

**Código pronto (Fases 0-4 do plano original, tudo neste repo):**

| Componente | Arquivo(s) | O que faz |
|---|---|---|
| Schema do banco | `deploy/init/001_schema.sql` | `candles` (hypertable) + `watchlist`, chave composta `(symbol, timeframe, time)` pra upsert idempotente |
| Processor | `processor/main.py`, `db.py`, `models.py` | FastAPI: `GET /health`, `POST /candles`, `GET/POST /watchlist`, `DELETE /watchlist/{symbol}`, `GET /status` |
| Coletor | `collector/collector.py`, `config.py` | Loop contínuo, reaproveita `daytrade_smc.fetch_ohlcv(..., source="MetaTrader 5")`, reenvia as últimas `TRAILING_WINDOW` velas a cada loop (ver "Por que sem watermark" abaixo) |
| Nova fonte no motor | `daytrade_smc.py:264` (`DATA_SOURCES`), `daytrade_smc.py:315` (`_fetch_ohlcv_homelab`), `daytrade_smc.py:277` (`HOMELAB_DB_DSN`) | Lê candles do Postgres seguindo o mesmo contrato de `fetch_ohlcv` (índice UTC tz-aware, colunas `[open,high,low,close,volume]`, ordem ascendente) |
| Watchlist DB-aware | `daytrade_smc.py:2005` (`load_symbols`), `daytrade_smc.py:2020` (`save_symbols`), `_load_symbols_db`/`_save_symbols_db` (linhas 1978/1986) | Lê/escreve a tabela `watchlist` quando `HOMELAB_DB_DSN` está configurado; cai pro arquivo local (`_load_symbols_file`/`_save_symbols_file`) senão |
| Streamlit | `streamlit_app.py:71` (injeção de `HOMELAB_DB_DSN`), `streamlit_app.py:126` (`_cached_mtf_homelab`), `streamlit_app.py:96` (`SOURCE_LABELS`) | Sidebar já mostra "Homelab (Postgres)" como 4ª opção, com legenda própria |
| Deploy | `deploy/docker-compose.yml`, `deploy/streamlit.Dockerfile`, `processor/Dockerfile` | TimescaleDB + processor + o próprio Streamlit, todos em container |
| Dependências | `requirements-homelab.txt` (`psycopg[binary,pool]`) | Isolado de `requirements.txt`/`requirements-local.txt`, mesmo padrão de split já existente |

**Ainda depende de infraestrutura real (não é código, é execução no seu homelab):**

- [ ] Provisionar a VM Windows com MT5 instalado, aberto e logado
- [ ] Subir `docker compose up -d` no host Linux/homelab (TimescaleDB + processor + Streamlit)
- [ ] Instalar o coletor na VM Windows e rodar manualmente uma vez pra validar
- [ ] Confirmar upsert idempotente e ausência de duplicatas/buracos no banco
- [ ] Empacotar o coletor como serviço NSSM (sobrevive a reboot)
- [ ] Configurar Tailscale (ou equivalente) entre VM Windows ↔ processor, e entre onde você acessa remotamente ↔ app Streamlit
- [ ] Escolher e configurar `HOMELAB_DB_DSN` (env var ou `secrets.toml`) no container do Streamlit
- [ ] Testar ponta a ponta: selecionar "Homelab (Postgres)" na sidebar e ver o gráfico atualizando

Essa segunda lista é o que resta pra "continuar a implementação" — é trabalho de operação/infra no seu ambiente, não algo que o Claude Code consiga executar por não ter acesso ao seu homelab.

## Por que sem "watermark" de última vela enviada

`copy_rates_from_pos(..., 0, count)` sempre inclui a vela ainda em
formação, cujo OHLC muda a cada tick até fechar. Se o coletor só
reenviasse linhas mais novas que uma marca lembrada, perderia toda
atualização intra-vela da barra em formação. Em vez disso, a cada loop
reenvia as últimas `TRAILING_WINDOW` (padrão 10) velas de cada
symbol/timeframe, e o `ON CONFLICT DO UPDATE` do processor absorve as
repetidas como no-op. Pouco tráfego numa LAN, e correto contra reinícios,
loops perdidos e o problema da vela em formação, sem nenhum estado local
pra persistir ou corromper.

## Endpoints do processor (referência rápida)

| Método | Rota | Auth | Uso |
|---|---|---|---|
| GET | `/health` | não | liveness |
| POST | `/candles` | `X-API-Key` (opcional) | upsert em lote — usado pelo coletor |
| GET | `/watchlist` | não | símbolos ativos — usado pelo coletor |
| POST | `/watchlist` | `X-API-Key` (opcional) | adicionar símbolo (conveniência; o app normalmente escreve direto no Postgres) |
| DELETE | `/watchlist/{symbol}` | `X-API-Key` (opcional) | soft-delete (`active=false`) |
| GET | `/status` | não | `MAX(time)`/`MAX(ingested_at)` por symbol+timeframe |

`PROCESSOR_API_KEY` sem definir = checagem pulada. A proteção real é o
processor não estar exposto fora da LAN/tailnet do homelab — a API key é
defesa em profundidade, não o controle principal.

## Variáveis de ambiente

| Variável | Onde | Padrão | Uso |
|---|---|---|---|
| `HOMELAB_DB_DSN` | app Streamlit | — | DSN do Postgres (`postgresql://user:pass@host:5432/daytrade`); também aceito via `st.secrets["homelab_db_dsn"]` |
| `DATABASE_URL` | processor | — | mesmo DSN, formato esperado pelo `psycopg_pool` |
| `PROCESSOR_API_KEY` | processor + coletor | vazio (sem auth) | header `X-API-Key` |
| `PROCESSOR_URL` | coletor | `http://localhost:8000` | endereço do processor (Tailscale/LAN) |
| `POLL_INTERVAL_SECONDS` | coletor | `5` | intervalo do loop |
| `COLLECTOR_TIMEFRAMES` | coletor | `M15,H1,H4,D1` | timeframes coletados (W1 fica fora do tempo real por baixo valor numa cadência de segundos) |
| `TRAILING_WINDOW` | coletor | `10` | quantas velas reenviar a cada loop |
| `WATCHLIST_REFRESH_SECONDS` | coletor | `60` | intervalo de releitura da watchlist via `/watchlist` |

## O que fica obsoleto (mantido, não removido)

`_fetch_ohlcv_github`, `trigger_github_update`, `fetch_snapshot_timestamp`,
os globais `GITHUB_BRIDGE_REPO`/`GITHUB_BRIDGE_TOKEN` e
`.github/workflows/mt5-update.yml` continuam no repo, intocados. Remover é
uma decisão separada, ainda não tomada.

## Próximos passos sugeridos (fora do código)

1. Docker/Compose no host do homelab (`cd deploy && cp .env.example .env` → preencher → `docker compose up -d`).
2. Coletor na VM Windows (ver `collector/README.md` pra setup completo + NSSM).
3. Rede: Tailscale ligando VM Windows ↔ processor ↔ onde você acessa o app.
4. Validação ponta a ponta (checklist acima).
5. Fase 5 opcional (não bloqueia o pipeline funcionando): legenda de "última atualização" na sidebar usando `/status`, logging/rotação de log, estratégia de backup do Postgres.
