# Pipeline de dados MT5 no homelab

Este documento formaliza a arquitetura da 4ª fonte de dados do "Day Trade
SMC": um scraper MT5 rodando continuamente numa VM Windows, alimentando o
TimescaleDB compartilhado do homelab através de um serviço `processor`, e
uma `api` de leitura que serve o app de análise e qualquer outro consumidor.

Serve como referência de continuidade — o que já foi implementado no
código, o que ainda depende de execução no seu ambiente, e as decisões de
design por trás de cada peça.

## Por que isso existe

As três fontes de dados originais (`daytrade_smc.py`, `DATA_SOURCES`)
tinham uma lacuna: "Yahoo Finance" funciona em qualquer lugar mas com
15-20min de atraso; "MetaTrader 5" direto é tempo real mas só rodando na
mesma máquina do terminal; "GitHub (MT5 de casa)" tentava ser uma ponte
pra usar dado real do MT5 a partir da nuvem, mas nunca foi terminada
(`mt5_bridge/update_data.py` e `data/mt5_snapshot.json`, que o workflow
`.github/workflows/mt5-update.yml` espera, nunca existiram no repo) — e,
mesmo terminada, seria só sob demanda (botão), não contínua.

Com um homelab disponível, a solução deixa de depender do GitHub Actions e
passa a ser uma pipeline própria: VM Windows com MT5 aberto → scraper →
processor (HTTP) → TimescaleDB → api (HTTP) → app.

## Decisões de arquitetura (já tomadas, não reabrir sem motivo novo)

| Decisão | Escolha | Por quê |
|---|---|---|
| Banco | TimescaleDB **compartilhado** do cluster (`default/timescaledb`), database `daytrade` | Já existe, já tem backup e volume. Subir um Postgres próprio seria um segundo banco pra operar sem ganho nenhum. Isolamento é por database + role dedicada, mesmo padrão do fcar. |
| Transporte scraper → processor | HTTP (sem fila) | Volume pequeno (poucas linhas por POST); fila adicionaria infra sem ganho real. |
| Leitura | Só via serviço `api` | Nenhum cliente fora do backend tem credencial de banco. Antes o Streamlit falava SQL direto; passar por HTTP tirou o acoplamento e deu um caminho único e reutilizável de leitura. |
| Separação processor / api | Dois Deployments, **mesma imagem**, `command:` diferente | Escrita e leitura têm exposições diferentes: o processor não precisa (nem tem) DNS público. Compartilham `db.py`/`models.py`, então não há duplicação. Mesmo padrão do fcar (`fcar-backend` roda server, domain-reconciler e migrate). |
| Onde o app roda | No k3s do homelab, publicado em `acoes.dondon.services` | Mesmo cluster do banco, entregue por ArgoCD junto com o resto. |
| Cadência do scraper | Loop contínuo (poucos segundos) | Sensação de tempo quase real, no mesmo nível do modo MT5 direto local. |
| Credencial de banco | Só `processor`, `api` e o Job de migration têm | O scraper (VM Windows, mais exposta) e o Streamlit falam só HTTP — reduz superfície de risco. |
| Ponte GitHub existente | Mantida, intocada, dormente | Superseded em intenção por este pipeline, mas remoção é decisão separada, não tomada ainda. |

## Arquitetura

```
VM Windows (fora do cluster)          k3s — ns acoes
┌────────────────────────┐            ┌──────────────────────────────────┐
│ MT5 + acoes-scraper    │            │ processor  :8000   (só escrita)  │
│ NIC1 macvtap → RDP     │  HTTPS     │   POST /candles                  │
│ NIC2 → 192.168.122.50  │ ─────────▶ │   GET  /watchlist  (p/ o scraper)│
│ hosts:                 │  :443      └────────────────┬─────────────────┘
│  192.168.122.1         │                             │ SQL write
│  acoes-processor.…     │                             ▼
└────────────────────────┘            ┌──────────────────────────────────┐
                                      │ ns default — timescaledb         │
                                      │   database: daytrade             │
                                      └────────────────┬─────────────────┘
                                                       │ SQL read
                                      ┌────────────────▼─────────────────┐
                                      │ api        :8000   (só leitura)  │
                                      │   GET /candles  GET /watchlist   │
                                      │   PUT /watchlist   GET /status   │
                                      └───▲──────────────────▲───────────┘
                                          │ ClusterIP        │
                                   ┌──────┴──────┐   internet│
                                   │ streamlit   │           │
                                   │   :8501     │  acoes-api.dondon.services
                                   └──────▲──────┘
                                          │ acoes.dondon.services
```

O scraper nunca fala com o Postgres — só conhece o `processor`. O Streamlit
também não: consome a `api` por HTTP. **Só o backend fala SQL.**

O scraper busca a watchlist no próprio `processor`, não na `api`. Por isso
`GET /watchlist` existe nos dois serviços: é uma função de rota repetida,
contra dar dois hostnames e duas entradas de `hosts` pra VM.

## Rede da VM Windows (gotcha do macvtap)

A VM `windows-11` tem **duas** placas de rede, e isso não é acidente:

| NIC | Tipo | Endereço | Para quê |
|---|---|---|---|
| NIC1 | macvtap modo bridge sobre `enp2s0` (`type='direct'`) | `192.168.1.x` (DHCP do roteador) | Acesso via área de trabalho remota pela LAN |
| NIC2 | rede virtual `default` do libvirt (NAT, `virbr0`) | `192.168.122.50/24` estático, **sem gateway** | Alcançar o cluster |

**O motivo:** macvtap em modo bridge dá à VM um IP próprio na LAN — ótimo pro
RDP — mas isola o guest do *próprio host que o hospeda*. A VM enxerga o
roteador e todas as outras máquinas da rede, menos o `192.168.1.183`. Os
pacotes saem pela placa física e o hardware não os devolve pra pilha do host.
Como todo o cluster é publicado em portas **do host**, o scraper com só a NIC1
nunca alcançaria o `processor` — o pipeline morreria no primeiro POST, com um
"connection timed out" que não parece ter nada a ver com virtualização.

A segunda placa contorna isso pelo caminho mais barato: `192.168.122.1` é o
gateway da rede NAT do libvirt, que também é um IP do host. E as portas do
`istio-ingressgateway` respondem ali — a regra de DNAT do klipper-lb do k3s é
por porta de destino, sem casar IP, então vale pra qualquer interface do host.
Verificado: `curl --resolve <host>:443:192.168.122.1` devolve 200 com o
certificado validando.

A NIC2 é configurada **sem default gateway e sem DNS** de propósito: com duas
placas, o DHCP do libvirt entregaria uma segunda rota default e o Windows
passaria a alternar a saída de internet entre as interfaces. Sem gateway, a
NIC2 só atende `192.168.122.0/24` e todo o resto continua saindo pela NIC1.

Como consequência, quem resolve `acoes-processor.dondon.services` na VM é o
arquivo `hosts` (ver `scraper/README.md`), apontando pra `192.168.122.1`.

Para diagnosticar de dentro da VM:

```powershell
Test-NetConnection 192.168.122.1 -Port 443   # deve dar True
Test-NetConnection 192.168.1.183  -Port 443   # deve dar False (isolamento macvtap)
```

Se um dia a NIC2 sumir, esse é o sintoma que volta.

## Exposição — o que é público e o que não é

| Host | Destino | DNS público |
|---|---|---|
| `acoes.dondon.services` | streamlit | **sim** |
| `acoes-api.dondon.services` | api (leitura) | **sim** |
| `acoes-processor.dondon.services` | processor (escrita) | **não** — só a entrada em `hosts` da VM |

Os três hosts são declarados no `acoes-gateway`, mas `acoes-processor` fica
fora do `DOMAINS` do `cloudflare-ddns` de propósito: sem registro público, o
caminho de escrita não é endereçável pela internet.

`ACOES_API_KEY` vazia faz o backend **pular** a checagem. Enquanto o serviço
só existia dentro da LAN isso era aceitável; com a `api` publicada,
preenchê-la é o único controle sobre `POST /candles` e `PUT /watchlist` —
não é mais defesa em profundidade, é o controle principal. `GET /candles`,
`GET /watchlist` e `GET /status` seguem sem auth por construção.

O Streamlit **não tem autenticação nenhuma** e a watchlist é editável pela
UI, então `acoes.dondon.services` público significa que qualquer visitante lê
e edita. Decisão consciente; o ponto de enxerto pra fechar depois é uma
`AuthorizationPolicy` no `acoes-gateway`, sem mexer em mais nada.

## Entrega (ArgoCD)

Os manifests **não moram neste repo** — moram em
`homelab/applications/acoes/`, sincronizados pela Application `acoes`
(`syncPolicy: automated`, com `selfHeal` e `prune`).

| Onde | O quê |
|---|---|
| `homelab/applications/acoes/` | namespace, os três Deployments + Services, Gateway, VirtualService, Job de migration |
| `homelab/bootstrap/applications/acoes.yaml` | a Application, criada pelo app-of-apps |
| `homelab/secrets/acoes/acoes-db.enc.yaml` | Secret SOPS+age — **fora do sync do Argo**, aplicado à mão por `secrets/scripts/apply-secret.sh` |

Não há registry: o `Makefile` deste repo builda as imagens e importa direto
no containerd (`docker save | sudo k3s ctr images import -`), com tag
`<sha>[-dirty.<timestamp>]`, e carimba essa tag nos manifests do `homelab`.
`make release` faz build → import → manifests → publish; o Argo pega em até
3 min, ou `make sync` força na hora.

**O Makefile não aplica manifest, e não deve passar a aplicar** — ver o
cabeçalho dele para o incidente que motivou a regra.

O schema é aplicado por um Job com `argocd.argoproj.io/hook: PreSync`, que
roda a **cada** sync usando a mesma imagem dos Deployments. Por isso
`backend/schema.sql` é inteiramente idempotente (`CREATE ... IF NOT EXISTS`,
`create_hypertable(..., if_not_exists => TRUE)`); antes ele era
`docker-entrypoint-initdb.d` do Compose, que roda uma vez só num banco vazio.

Role e database são criados fora do GitOps por `scripts/init-tenant-db.sh`
(`make db-init`), porque exigem superusuário — e o `pg_hba` da instância
bloqueia `postgres` remoto justamente pra isso não virar rotina.

### Dois caminhos de entrega

O `scraper` não cabe no modelo acima: ele depende do MetaTrader5, que é DLL de
Windows, então não há imagem, não há container e o Argo não o alcança. É o
único componente entregue por **push**.

```
                    ┌──────────────────────────────────────┐
   make release ───▶│ docker build → k3s ctr images import │
   (PULL, k3s)      │ sed da tag → git push (repo homelab) │
                    └──────────────────┬───────────────────┘
                                       │  ArgoCD faz o pull
                                       ▼
                          processor · api · streamlit

                    ┌──────────────────────────────────────┐
   make release-    │ scp dos arquivos → C:\acoes          │
   scraper          │ nssm stop/start AcoesScraper         │
   (PUSH, ssh)      └──────────────────┬───────────────────┘
                                       │  ssh 192.168.122.50
                                       ▼
                             VM Windows · scraper
```

Os dois são **deliberadamente separados** — mesmo precedente de
`release`/`release-vm` no `platform-fcar`. O deploy no cluster não pode falhar
porque a VM Windows estava desligada.

O alvo usa `BatchMode=yes` e `ConnectTimeout=10` (padrão herdado de
`homelab/backup/scripts/backup-oracle-postgres.sh`, não do Makefile do fcar,
que roda `ssh` pelado e trava quando o destino some). O IP `192.168.122.50` é
fixo por **reserva DHCP na rede `default` do libvirt** (MAC
`52:54:00:c6:88:12`), não por configuração dentro do Windows — assim a VM
continua em DHCP e o endereço não depende de nada lá dentro.

O equivalente da tag de imagem, do lado da VM, é o arquivo `C:\acoes\DEPLOY-INFO`
(`VERSION`, `REQS_HASH`, `DEPLOYED_AT`), gravado a cada push. O `scraper.py` lê
o `VERSION` e o loga no start, então o log do serviço diz sozinho qual versão
está rodando. O `REQS_HASH` faz o `release-scraper` avisar quando os
`requirements*.txt` mudaram e o `make scraper-deps` precisa rodar — em vez de
rodar `pip` toda vez (lento) ou quebrar em silêncio.

Bootstrap da VM (OpenSSH Server, chave pública, NSSM) está em
`scraper/README.md` — é manual e roda uma vez só.

## Status da implementação

**Código pronto, neste repo:**

| Componente | Arquivo(s) | O que faz |
|---|---|---|
| Schema | `backend/schema.sql` | `candles` (hypertable) + `watchlist`, chave composta `(symbol, timeframe, time)` pra upsert idempotente |
| Migration | `backend/migrate.py` | Aplica o schema numa transação; roda como hook PreSync |
| Processor (escrita) | `backend/processor.py` | `GET /health`, `POST /candles`, `GET /watchlist` |
| API (leitura) | `backend/api.py` | `GET /health`, `GET /candles`, `GET /watchlist`, `PUT /watchlist`, `GET /status` |
| Auth | `backend/auth.py` | `X-API-Key` contra `ACOES_API_KEY`, compartilhado pelos dois |
| Scraper | `scraper/scraper.py`, `config.py` | Loop contínuo, reaproveita `daytrade_smc.fetch_ohlcv(..., source="MetaTrader 5")`, reenvia as últimas `TRAILING_WINDOW` velas a cada loop (ver "Por que sem watermark") |
| Fonte no motor | `daytrade_smc.py` (`DATA_SOURCES`, `_fetch_ohlcv_api`, `ACOES_API_URL`) | Lê candles pela api seguindo o mesmo contrato de `fetch_ohlcv` (índice UTC tz-aware, colunas `[open,high,low,close,volume]`, ordem ascendente) |
| Watchlist via API | `daytrade_smc.py` (`load_symbols`/`save_symbols`, `_load_symbols_api`/`_save_symbols_api`) | `GET`/`PUT /watchlist` quando `ACOES_API_URL` está configurada; cai pro arquivo local senão |
| Streamlit | `streamlit_app.py` (injeção de `ACOES_API_URL`, `_cached_mtf_api`, `SOURCE_LABELS`) | Sidebar mostra "Homelab (API)" como 4ª opção |
| Imagens | `backend/Dockerfile`, `Dockerfile.streamlit` | `acoes-backend` (3 entrypoints) e `acoes-streamlit` (sem driver de banco) |
| Release k3s | `Makefile`, `scripts/init-tenant-db.sh` | build → containerd → tag nos manifests → push |
| Release scraper | `Makefile` (alvos `scraper-*`) | scp pra `C:\acoes` + restart do serviço, com `DEPLOY-INFO` de versão |

**Já feito no cluster:**

- [x] Rede VM Windows ↔ cluster: segunda NIC na rede `default` do libvirt
- [x] IP da VM fixado em `192.168.122.50` por reserva DHCP no libvirt
- [x] `make db-init` — role e database `daytrade` criados
- [x] `secrets/acoes/acoes-db.enc.yaml` criado (SOPS+age) e aplicado
- [x] `make release` + `bootstrap/applications/acoes.yaml` — Application `Synced`/`Healthy`
- [x] Migration PreSync rodou: `candles` (hypertable) + `watchlist` com 11 símbolos
- [x] DNS de `acoes` e `acoes-api` — hoje por **Cloudflare Tunnel** (`cloudflared` como serviço systemd no host); o `cloudflare-ddns` está em `replicas: 0`

**Já feito na VM Windows:**

- [x] MT5 instalado, aberto e logado (`C:\Program Files\Clear Investimentos MT5 Terminal\`)
- [x] Bootstrap SSH: OpenSSH Server + chave em `administrators_authorized_keys`, conta `admin`
- [x] Entrada em `hosts`: `192.168.122.1  acoes-processor.dondon.services`
- [x] `make scraper-files` + `make scraper-deps` — código e dependências em `C:\acoes`
- [x] **Pipeline validado ponta a ponta**: scraper → processor → TimescaleDB → api

**Falta:**

- [ ] Instalar o NSSM na VM (e deixá-lo no `PATH`) e registrar o `AcoesScraper`
- [ ] Confirmar que o serviço sobrevive a um reboot da VM
- [ ] Conferir "Homelab (API)" na sidebar do Streamlit com o gráfico atualizando

Enquanto o serviço não existe, o scraper só roda à mão
(`python scraper\scraper.py` em `C:\acoes`) e o `make scraper-check` falha na
última guarda, de propósito.

### Detalhes descobertos na VM

- A conta do SSH precisa estar no grupo **Administradores** — só assim o sshd
  lê o `administrators_authorized_keys`. Aqui é a `admin`; `daniel` não existe
  e o `Administrador` embutido está desabilitado.
- **`BRA50` não existe na corretora** (Clear Investimentos) e gera warning a
  cada ciclo. Os outros 10 ativos do seed funcionam. Vale removê-lo da
  watchlist pela sidebar, ou trocar pelo código correto do índice.
- Python 3.14 na VM, com wheel de `MetaTrader5 5.0.6090` disponível — não foi
  preciso rebaixar a versão.
- O `requirements.txt` arrasta `streamlit`, `plotly` e `yfinance` pra VM, que o
  scraper nunca usa. Funciona, mas é peso morto; separar um
  `requirements-scraper.txt` é uma limpeza possível, não feita ainda.

## Por que sem "watermark" de última vela enviada

`copy_rates_from_pos(..., 0, count)` sempre inclui a vela ainda em
formação, cujo OHLC muda a cada tick até fechar. Se o scraper só reenviasse
linhas mais novas que uma marca lembrada, perderia toda atualização
intra-vela da barra em formação. Em vez disso, a cada loop reenvia as
últimas `TRAILING_WINDOW` (padrão 10) velas de cada symbol/timeframe, e o
`ON CONFLICT DO UPDATE` do processor absorve as repetidas como no-op. Pouco
tráfego, e correto contra reinícios, loops perdidos e o problema da vela em
formação, sem nenhum estado local pra persistir ou corromper.

## Endpoints (referência rápida)

**`processor`** — escrita, sem DNS público:

| Método | Rota | Auth | Uso |
|---|---|---|---|
| GET | `/health` | não | liveness |
| POST | `/candles` | `X-API-Key` | upsert em lote — usado pelo scraper |
| GET | `/watchlist` | não | símbolos ativos — usado pelo scraper |

**`api`** — leitura, publicada em `acoes-api.dondon.services`:

| Método | Rota | Auth | Uso |
|---|---|---|---|
| GET | `/health` | não | liveness |
| GET | `/candles?symbol=&timeframe=&count=` | não | últimas `count` velas, ordem ascendente; 404 se não houver |
| GET | `/watchlist` | não | símbolos ativos |
| PUT | `/watchlist` | `X-API-Key` | substitui a lista **inteira** (ver abaixo) |
| GET | `/status` | não | `MAX(time)`/`MAX(ingested_at)` por symbol+timeframe |

`PUT` e não `POST` porque `save_symbols` sempre teve semântica de
sobrescrever tudo de uma vez: o servidor desativa quem saiu e (re)ativa quem
está na lista, numa transação só. Um `POST` por símbolo somado a um `DELETE`
por símbolo removido não expressa isso atomicamente.

## Variáveis de ambiente

| Variável | Onde | Padrão | Uso |
|---|---|---|---|
| `DATABASE_URL` | processor, api, migrate | — | DSN do Postgres; montado no manifest a partir do Secret, nunca guardado inteiro nele |
| `ACOES_API_KEY` | processor, api, streamlit, scraper | vazio (**sem auth — não deixe assim**) | header `X-API-Key` |
| `ACOES_API_URL` | streamlit | — | URL da api; no cluster é `http://api.acoes.svc.cluster.local:8000`. Também aceito via `st.secrets["acoes_api_url"]` |
| `PROCESSOR_URL` | scraper | `http://localhost:8000` | no homelab é `https://acoes-processor.dondon.services` |
| `POLL_INTERVAL_SECONDS` | scraper | `5` | intervalo do loop |
| `SCRAPER_TIMEFRAMES` | scraper | `M15,H1,H4,D1` | timeframes coletados (W1 fica fora do tempo real por baixo valor numa cadência de segundos) |
| `TRAILING_WINDOW` | scraper | `10` | quantas velas reenviar a cada loop |
| `WATCHLIST_REFRESH_SECONDS` | scraper | `60` | intervalo de releitura da watchlist |
| `REQUEST_TIMEOUT_SECONDS` | scraper | `10` | timeout das chamadas HTTP |

## O que fica obsoleto (mantido, não removido)

`_fetch_ohlcv_github`, `trigger_github_update`, `fetch_snapshot_timestamp`,
os globais `GITHUB_BRIDGE_REPO`/`GITHUB_BRIDGE_TOKEN` e
`.github/workflows/mt5-update.yml` continuam no repo, intocados. Remover é
uma decisão separada, ainda não tomada.

## Fase opcional (não bloqueia o pipeline funcionando)

Legenda de "última atualização" na sidebar usando `GET /status`; rotação de
log do scraper; incluir o database `daytrade` na rotina de backup do
`homelab/backup/`.
