"""
backend/api.py

Caminho de LEITURA do pipeline: único jeito de consumir os dados gravados
pelo `processor`. É quem o Streamlit consulta (`daytrade_smc`, fonte
"Homelab (API)"), e o que fica publicado em `acoes-api.dondon.services`
para outros consumidores.

  - GET /candles?symbol=&timeframe=&count=   velas em ordem ascendente
  - GET /watchlist                           símbolos ativos
  - PUT /watchlist                           substitui a lista inteira
  - GET /status                              última vela/ingestão por par
  - GET /health                              liveness

  - GET    /profiles                         perfis de análise ativos
  - GET    /profiles/{nome}                  um perfil
  - PUT    /profiles/{nome}                  cria ou substitui um perfil
  - DELETE /profiles/{nome}                  desativa um perfil (soft-delete)

  - POST   /signals                          grava um sinal (dedup por vela)
  - GET    /signals                          histórico com filtros
  - GET    /signals/stats                    assertividade por recorte

Com este serviço no lugar, nenhum cliente fora do backend precisa de
credencial de banco — o Streamlit deixou de falar SQL.

AUTENTICAÇÃO: as rotas antigas seguem a regra histórica "GET aberto,
escrita com X-API-Key", porque OHLCV é dado público de mercado. As rotas
NOVAS (/profiles, /signals) são autenticadas INCLUSIVE nos GETs: perfis
de calibragem e histórico de sinais são pesquisa do usuário, não dado
público, e este serviço fica publicado na internet. Custa nada ao
cliente — `daytrade_smc._api_headers()` já manda a chave em toda
chamada. Ver docs/homelab-pipeline.md, seção "Exposição".

Deploy: `uvicorn api:app`, a partir da mesma imagem `acoes-backend` que
roda `processor.py`, `analyzer.py` e `migrate.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg import Connection
from psycopg.types.json import Jsonb

import auth
from auth import require_api_key
from candles import ler_candles
from db import get_conn
from models import (
    AnaliseIn,
    AnaliseResponse,
    CandleOut,
    CandlesResponse,
    ProfileOut,
    ProfileIn,
    ProfilesResponse,
    SignalIn,
    SignalOut,
    SignalSaveResult,
    SignalsResponse,
    StatsResponse,
    StatsRow,
    SymbolStatus,
    WatchlistReplace,
    WatchlistResponse,
)

# `candles.py` já fez o `sys.path.insert` que põe a raiz do repo no caminho —
# é de lá que este import se resolve no checkout. Na imagem tudo está achatado
# em /app e resolveria de qualquer jeito.
from daytrade_smc import (  # noqa: E402
    DAYTRADE_CONFIRMATION_TIMEFRAMES,
    DEFAULT_PROFILE_NAME,
    MODALITIES,
    AnalysisParams,
    analyze,
    mtf_confirmation,
    signal_payload,
)

log = logging.getLogger("api")

# ---------------------------------------------------------------------------
# 🔴 ESTA SPEC É A INTERFACE DE UM AGENTE, não só documentação.
#
# Desde 2026-08-10 o `/openapi.json` gerado aqui é snapshotado no ConfigMap
# `agentgateway-openapi-acoes` (repo homelab) e convertido em tools MCP pelo
# agentgateway. Consequências práticas, todas fáceis de esquecer:
#
#   - `operation_id` vira NOME DE TOOL e `description` vira o texto que o
#     modelo lê pra decidir se chama. Não são cosméticos. Quando o decorator
#     traz `description=`, o docstring da função é ignorado NA SPEC de
#     propósito: o docstring fala com quem lê o código, a `description` fala
#     com o modelo, e os dois querem coisas diferentes.
#   - `include_in_schema=False` aqui significa "NÃO vira tool", e não "não é
#     rota". As rotas escondidas continuam existindo, servidas e suportadas —
#     só não são coisas que faça sentido um modelo chamar (ver o motivo em
#     cada uma).
#   - Mexeu aqui? Regerar o ConfigMap E reiniciar o gateway. Mount com
#     `subPath` não é recarregado pelo kubelet: sem o restart o ArgoCD diz
#     `Synced` e o gateway serve a spec velha por tempo indeterminado.
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Ações — API (leitura)",
    description=(
        "Análise técnica de ações da B3 (Smart Money Concepts, Price Action, "
        "médias móveis e VWAP) sobre dados de mercado coletados continuamente. "
        "Serve o painel Streamlit e, desde 2026-08-10, o agente Ações da "
        "plataforma. Só analisa e mede: não envia ordem, não opera, não tem "
        "conexão com corretora."
    ),
    # O ClusterIP, e não o host público `acoes-api.dondon.services`: quem
    # consome esta spec é o agentgateway, que roda dentro do cluster e não deve
    # sair pro ingress e voltar só pra falar com o serviço do lado.
    servers=[
        {"url": "http://api.acoes.svc.cluster.local:8000",
         "description": "ClusterIP no namespace `acoes`"},
    ],
)

# Perfil que representa "o motor sem nenhum ajuste". Não pode ser apagado
# nem desativado: é o fallback de `load_profiles` no cliente e o alvo da
# FK de todo sinal gerado sem perfil escolhido.
PERFIL_PADRAO = "padrão"


# Aviso no import (é quando o uvicorn carrega o módulo, uma vez por pod):
# `auth.API_KEY` vazia faz `require_api_key` virar no-op, e TODAS as rotas
# de escrita, mais os perfis e o histórico de sinais, ficam abertas. Isso
# era aceitável quando o serviço só existia na LAN; com a `api` publicada,
# um Secret faltando num deploy vira exposição silenciosa.
#
# Avisa em vez de recusar subir de propósito: um CrashLoopBackOff aqui
# tiraria do ar também a leitura de candles, que é pública de qualquer jeito.
if not auth.API_KEY:
    log.warning(
        "ACOES_API_KEY VAZIA — autenticação DESLIGADA. As rotas de escrita, "
        "os perfis de análise e o histórico de sinais estão abertos a quem "
        "alcançar este serviço."
    )


@app.get(
    "/health",
    operation_id="acoes_health",
    summary="Liveness da API de ações",
    description="Responde `{'status':'ok'}` se o serviço está de pé. Não diz nada sobre o "
                "frescor dos dados — pra isso use `acoes_status_dados`.",
)
def health() -> dict:
    return {"status": "ok"}


# Fora da spec de propósito (não vira tool): 250 velas OHLCV em JSON é um
# despejo de milhares de tokens do qual um modelo não extrai nada que
# `analisar_simbolo` não entregue já interpretado. Continua sendo rota pública
# e suportada — é como o Streamlit lê os dados.
@app.get("/candles", response_model=CandlesResponse, include_in_schema=False)
def get_candles(
    symbol: str = Query(..., min_length=1),
    timeframe: str = Query(..., min_length=1),
    count: int = Query(500, ge=1, le=5000),
    conn: Connection = Depends(get_conn),
) -> CandlesResponse:
    """Últimas `count` velas de um symbol/timeframe.

    A consulta ordena DESC pra usar o índice `candles_symbol_tf_time_desc_idx`
    e cortar no LIMIT, mas a resposta sai ASC — que é o contrato que
    `fetch_ohlcv` mantém em todas as fontes."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE symbol = %(symbol)s AND timeframe = %(timeframe)s
            ORDER BY time DESC
            LIMIT %(count)s
            """,
            {"symbol": symbol.strip().upper(), "timeframe": timeframe.strip().upper(), "count": count},
        )
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail=f"Sem candles para {symbol} em {timeframe}.")

    candles = [
        CandleOut(time=row[0], open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5])
        for row in reversed(rows)
    ]
    return CandlesResponse(symbol=symbol, timeframe=timeframe, candles=candles)


@app.get(
    "/watchlist",
    response_model=WatchlistResponse,
    operation_id="listar_watchlist",
    summary="Ações monitoradas hoje",
    description="Lista os símbolos que o coletor está acompanhando e sobre os quais existe "
                "dado. Um símbolo fora desta lista não tem candle no banco, então "
                "`analisar_simbolo` vai falhar nele até ser incluído.",
)
def get_watchlist(conn: Connection = Depends(get_conn)) -> WatchlistResponse:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        symbols = [row[0] for row in cur.fetchall()]
    return WatchlistResponse(symbols=symbols)


@app.put(
    "/watchlist",
    response_model=WatchlistResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="substituir_watchlist",
    summary="Substituir a watchlist inteira",
    description=(
        "🔴 SUBSTITUI a lista inteira de ações monitoradas pela que for enviada — não "
        "acrescenta. Qualquer símbolo ausente do corpo PARA de ser coletado. "
        "Para adicionar ou remover uma ação, chame `listar_watchlist` primeiro, altere a "
        "lista recebida e mande ela COMPLETA de volta. Mandar só o símbolo novo apaga "
        "todos os outros e interrompe a coleta deles."
    ),
)
def replace_watchlist(body: WatchlistReplace, conn: Connection = Depends(get_conn)) -> WatchlistResponse:
    """Substitui a watchlist inteira, na mesma semântica "sobrescreve tudo de
    uma vez" que `save_symbols` sempre teve: desativa quem saiu, (re)ativa
    quem está na lista. Tudo numa transação só, senão uma falha no meio
    deixaria a watchlist vazia e o scraper sem o que coletar."""
    symbols = [s.strip().upper() for s in body.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="Lista de símbolos vazia.")

    with conn.cursor() as cur:
        cur.execute("UPDATE watchlist SET active = false")
        for symbol in symbols:
            cur.execute(
                """
                INSERT INTO watchlist (symbol, active) VALUES (%s, true)
                ON CONFLICT (symbol) DO UPDATE SET active = true
                """,
                (symbol,),
            )
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        active = [row[0] for row in cur.fetchall()]
    return WatchlistResponse(symbols=active)


@app.get(
    "/status",
    response_model=list[SymbolStatus],
    operation_id="acoes_status_dados",
    summary="Frescor dos dados por ação e timeframe",
    description=(
        "Para cada par ação/timeframe, a última vela existente e a última vez que o dado "
        "MUDOU. Use pra responder 'os dados estão atualizados?' antes de confiar numa "
        "análise. Com o mercado fechado esses horários congelam — isso é o esperado, não "
        "sinal de coletor parado. Só aparecem pares com movimento nos últimos 30 dias."
    ),
)
def get_status(conn: Connection = Depends(get_conn)) -> list[SymbolStatus]:
    """Última vela e última ingestão por par symbol/timeframe.

    ATENÇÃO ao significado de `last_ingested_at`: desde que o processor passou
    a só gravar velas que mudaram de verdade (ver `_UPSERT_SQL` em
    processor.py), este campo é "última vez que o dado MUDOU", e não "última
    vez que o scraper falou". Com o mercado fechado ele congela — isso é o
    comportamento correto, não sinal de scraper morto. Um alerta de liveness
    do scraper precisa olhar os logs do processor, não este campo.

    O recorte de 30 dias existe porque sem ele o agregado varre todos os
    chunks da hypertable: em 2026-08-04 eram 213 chunks e 150 ms só de
    planning, num endpoint público e sem autenticação. Pares parados há mais
    de 30 dias somem daqui, que é o que se espera de um painel de liveness."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, timeframe, MAX(time) AS last_candle_time, MAX(ingested_at) AS last_ingested_at
            FROM candles
            WHERE time > now() - INTERVAL '30 days'
            GROUP BY symbol, timeframe
            ORDER BY symbol, timeframe
            """
        )
        rows = cur.fetchall()
    return [
        SymbolStatus(symbol=row[0], timeframe=row[1], last_candle_time=row[2], last_ingested_at=row[3])
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Perfis de análise
#
# Um perfil é um conjunto nomeado de parâmetros do motor
# (`daytrade_smc.AnalysisParams`). Serve pra duas coisas: ajustar a
# calibragem pela interface sem editar código, e — porque cada sinal
# gravado guarda o perfil que o gerou — comparar depois a assertividade
# de uma calibragem contra a outra.
# ---------------------------------------------------------------------------

_PROFILE_COLUMNS = "nome, params, params_hash, descricao, ativo, criado_em, alterado_em"


def _profile_from_row(row: tuple) -> ProfileOut:
    return ProfileOut(
        nome=row[0], params=row[1], params_hash=row[2], descricao=row[3],
        ativo=row[4], criado_em=row[5], alterado_em=row[6],
    )


@app.get(
    "/profiles",
    response_model=ProfilesResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="listar_perfis_analise",
    summary="Calibragens do motor disponíveis",
    description=(
        "Os perfis de calibragem existentes. Um perfil é um conjunto nomeado de "
        "parâmetros do motor; 'padrão' é o motor sem nenhum ajuste. Serve pra saber "
        "quais nomes são aceitos em `analisar_simbolo` e `assertividade_sinais`. "
        "`params` traz só o que difere do padrão."
    ),
)
def get_profiles(conn: Connection = Depends(get_conn)) -> ProfilesResponse:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_PROFILE_COLUMNS} FROM analysis_profiles WHERE ativo ORDER BY nome")
        rows = cur.fetchall()
    return ProfilesResponse(profiles=[_profile_from_row(row) for row in rows])


# As três rotas abaixo ficam fora da spec (não viram tool). Não é receio de
# escrita por escrita — `substituir_watchlist` é tool e escreve. É que estas
# três reescrevem a CALIBRAGEM do motor, e um perfil alterado muda
# retroativamente o significado de toda comparação de assertividade que
# aponta pra ele. Isso é decisão de bancada, feita no Streamlit olhando o
# efeito nos gráficos, não algo pra sair de uma frase solta numa conversa.
@app.get("/profiles/{nome}", response_model=ProfileOut,
         dependencies=[Depends(require_api_key)], include_in_schema=False)
def get_profile(nome: str, conn: Connection = Depends(get_conn)) -> ProfileOut:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_PROFILE_COLUMNS} FROM analysis_profiles WHERE nome = %s", (nome.strip(),))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Perfil '{nome}' não existe.")
    return _profile_from_row(row)


@app.put("/profiles/{nome}", response_model=ProfileOut,
         dependencies=[Depends(require_api_key)], include_in_schema=False)
def replace_profile(nome: str, body: ProfileIn, conn: Connection = Depends(get_conn)) -> ProfileOut:
    """Cria ou substitui um perfil inteiro.

    Mesma semântica de "sobrescreve tudo de uma vez" do PUT /watchlist:
    `params` é o conjunto COMPLETO de ajustes daquele perfil, não um
    patch — mandar `{}` volta o perfil pros defaults do motor.

    Um PUT reativa um perfil que tinha sido desativado, que é o que
    "salvar com um nome que já existiu" deve significar.
    """
    nome = nome.strip()
    if not nome:
        raise HTTPException(status_code=400, detail="Nome de perfil vazio.")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO analysis_profiles (nome, params, params_hash, descricao)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (nome) DO UPDATE SET
                params      = EXCLUDED.params,
                params_hash = EXCLUDED.params_hash,
                descricao   = EXCLUDED.descricao,
                ativo       = true,
                alterado_em = now()
            RETURNING {_PROFILE_COLUMNS}
            """,
            (nome, Jsonb(body.params), body.params_hash, body.descricao),
        )
        row = cur.fetchone()
    return _profile_from_row(row)


@app.delete("/profiles/{nome}", response_model=ProfilesResponse,
            dependencies=[Depends(require_api_key)], include_in_schema=False)
def delete_profile(nome: str, conn: Connection = Depends(get_conn)) -> ProfilesResponse:
    """Desativa um perfil — soft-delete, nunca DELETE.

    Um perfil que já gerou sinais não pode sumir sem levar junto a
    resposta de "com que calibragem esse sinal foi gerado", que é o
    ponto de existir perfil. A FK em `signals.perfil` é RESTRICT e
    bloquearia o DELETE de qualquer forma; o soft-delete é a versão
    honesta disso, mesmo padrão do `watchlist.active`.
    """
    nome = nome.strip()
    if nome == PERFIL_PADRAO:
        raise HTTPException(status_code=400, detail=f"O perfil '{PERFIL_PADRAO}' não pode ser removido.")

    with conn.cursor() as cur:
        cur.execute("UPDATE analysis_profiles SET ativo = false, alterado_em = now() WHERE nome = %s", (nome,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Perfil '{nome}' não existe.")
        cur.execute(f"SELECT {_PROFILE_COLUMNS} FROM analysis_profiles WHERE ativo ORDER BY nome")
        rows = cur.fetchall()
    return ProfilesResponse(profiles=[_profile_from_row(row) for row in rows])


# ---------------------------------------------------------------------------
# Sinais
# ---------------------------------------------------------------------------

_SIGNAL_COLUMNS = (
    "id, symbol, timeframe, modalidade, candle_time, perfil, params_hash, origem, "
    "direcao, score, confianca, setup, mtf_confirmado, mtf_direcao, "
    "entrada, stop, alvo_1, alvo_2, r_alvo_1, r_alvo_2, stop_basis, detalhes, criado_em, "
    "resultado, resultado_detalhe, candles_ate_resultado, avaliado_em"
)


def _signal_from_row(row: tuple) -> SignalOut:
    return SignalOut(
        id=row[0], symbol=row[1], timeframe=row[2], modalidade=row[3], candle_time=row[4],
        perfil=row[5], params_hash=row[6], origem=row[7], direcao=row[8], score=row[9],
        confianca=row[10], setup=row[11], mtf_confirmado=row[12], mtf_direcao=row[13],
        entrada=row[14], stop=row[15], alvo_1=row[16], alvo_2=row[17], r_alvo_1=row[18],
        r_alvo_2=row[19], stop_basis=row[20], detalhes=row[21], criado_em=row[22],
        resultado=row[23], resultado_detalhe=row[24], candles_ate_resultado=row[25],
        avaliado_em=row[26],
    )


# Fora da spec (não vira tool): esta tabela É o dataset de assertividade. Uma
# linha gravada por um modelo, a partir de números que ele montou numa
# conversa, entra nas mesmas médias que os sinais medidos pelo worker e
# corrompe a única medição honesta que este projeto tem. Quem grava sinal é o
# worker (`origem='worker'`) e o botão do Streamlit (`'manual'`).
@app.post("/signals", response_model=SignalSaveResult,
          dependencies=[Depends(require_api_key)], include_in_schema=False)
def save_signal(body: SignalIn, conn: Connection = Depends(get_conn)) -> SignalSaveResult:
    """Grava um sinal, deduplicando pela vela.

    ON CONFLICT DO NOTHING, nunca DO UPDATE: um sinal não muda depois que a
    vela fechou, então um UPDATE incondicional só produziria tupla morta,
    WAL e autovacuum a cada tentativa — que é exatamente o problema medido
    em 2026-08-04 no `POST /candles` (ver `_UPSERT_SQL` no processor.py e
    docs/homelab-pipeline.md).

    Um sinal sem entrada operável (NEUTRO) já nasce com resultado
    'SEM_SINAL': ele nunca vai ter desfecho, e deixar `resultado` nulo o
    deixaria pra sempre na fila de avaliação do worker. Mas é gravado assim
    mesmo — sem ele não dá pra responder "com que frequência este perfil
    sequer produz sinal", e o recorte por faixa de score ficaria enviesado
    pro que o perfil por acaso dispara.
    """
    sem_sinal = body.direcao == "NEUTRO" or body.entrada is None
    dados = body.model_dump()
    dados["detalhes"] = Jsonb(body.detalhes)
    dados["resultado"] = "SEM_SINAL" if sem_sinal else None
    dados["resultado_detalhe"] = "Não havia sinal operável nesta vela." if sem_sinal else None

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO signals (
                symbol, timeframe, modalidade, candle_time, perfil, params_hash, origem,
                direcao, score, confianca, setup, mtf_confirmado, mtf_direcao,
                entrada, stop, alvo_1, alvo_2, r_alvo_1, r_alvo_2, stop_basis, detalhes,
                resultado, resultado_detalhe
            ) VALUES (
                %(symbol)s, %(timeframe)s, %(modalidade)s, %(candle_time)s, %(perfil)s,
                %(params_hash)s, %(origem)s, %(direcao)s, %(score)s, %(confianca)s, %(setup)s,
                %(mtf_confirmado)s, %(mtf_direcao)s, %(entrada)s, %(stop)s, %(alvo_1)s,
                %(alvo_2)s, %(r_alvo_1)s, %(r_alvo_2)s, %(stop_basis)s, %(detalhes)s,
                %(resultado)s, %(resultado_detalhe)s
            )
            ON CONFLICT (symbol, timeframe, modalidade, candle_time, perfil, origem) DO NOTHING
            RETURNING {_SIGNAL_COLUMNS}
            """,
            dados,
        )
        row = cur.fetchone()
        duplicado = row is None
        if duplicado:
            cur.execute(
                f"""
                SELECT {_SIGNAL_COLUMNS} FROM signals
                WHERE symbol = %(symbol)s AND timeframe = %(timeframe)s
                  AND modalidade = %(modalidade)s AND candle_time = %(candle_time)s
                  AND perfil = %(perfil)s AND origem = %(origem)s
                """,
                dados,
            )
            row = cur.fetchone()

    return SignalSaveResult(signal=_signal_from_row(row), duplicado=duplicado)


@app.get(
    "/signals",
    response_model=SignalsResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="listar_sinais",
    summary="Histórico de sinais gerados",
    description=(
        "Sinais que o motor gerou no passado, do mais recente pro mais antigo, com o "
        "desfecho de cada um quando já houve (`resultado`: ALVO_1, ALVO_2, STOP ou "
        "EM_ABERTO). Use pra 'o que apareceu hoje?' ou 'o que deu na VALE3 esta semana?'. "
        "Para a TAXA de acerto agregada use `assertividade_sinais`, que já faz a conta "
        "com o denominador certo. Para a leitura de AGORA use `analisar_simbolo` — o que "
        "está aqui é histórico, e `dias` conta pela data da vela, não pela data em que a "
        "linha foi inserida."
    ),
)
def get_signals(
    symbol: str | None = Query(None),
    timeframe: str | None = Query(None),
    modalidade: str | None = Query(None),
    perfil: str | None = Query(None),
    origem: str | None = Query(None),
    resultado: str | None = Query(None),
    dias: int = Query(90, ge=1, le=3650),
    limite: int = Query(200, ge=1, le=2000),
    conn: Connection = Depends(get_conn),
) -> SignalsResponse:
    """Histórico de sinais, do mais recente pro mais antigo."""
    filtros = {
        "symbol": symbol.strip().upper() if symbol else None,
        "timeframe": timeframe.strip().upper() if timeframe else None,
        "modalidade": modalidade, "perfil": perfil, "origem": origem,
        "resultado": resultado, "dias": dias, "limite": limite,
    }
    # Os ::text não são decoração: num `$1 IS NULL OR col = $1`, o Postgres
    # olha o IS NULL primeiro e desiste de inferir o tipo do parâmetro
    # ("could not determine data type of parameter"). O cast resolve.
    # A janela é por `candle_time`, NÃO por `criado_em`: "últimos 90 dias"
    # significa os sinais das velas desse período, não as linhas inseridas
    # nesse período. Enquanto só o worker escrevia, em tempo real, os dois
    # davam no mesmo; com o backfill (`analyzer.py --backfill`) deixam de
    # dar — um sinal de D1 de 2022 gravado hoje cairia no recorte de 7 dias.
    where = """
        WHERE candle_time > now() - make_interval(days => %(dias)s)
          AND (%(symbol)s::text     IS NULL OR symbol     = %(symbol)s)
          AND (%(timeframe)s::text  IS NULL OR timeframe  = %(timeframe)s)
          AND (%(modalidade)s::text IS NULL OR modalidade = %(modalidade)s)
          AND (%(perfil)s::text     IS NULL OR perfil     = %(perfil)s)
          AND (%(origem)s::text     IS NULL OR origem     = %(origem)s)
          AND (%(resultado)s::text  IS NULL OR resultado  = %(resultado)s)
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM signals {where}", filtros)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {_SIGNAL_COLUMNS} FROM signals {where} ORDER BY candle_time DESC, id DESC LIMIT %(limite)s",
            filtros,
        )
        rows = cur.fetchall()
    return SignalsResponse(signals=[_signal_from_row(row) for row in rows], total=total)


# A CTE que todos os recortes de assertividade compartilham. Ficam de fora:
# 'SEM_SINAL' (não era operável), 'SEM_ENTRADA' (o gap abriu além do stop, a
# operação não chegou a existir) e `resultado IS NULL` (ainda não avaliado).
#
# O R vem de `r_realizado`, gravado pelo motor a partir do preço de execução
# REAL (abertura da vela seguinte) e já líquido de custo — ver
# `evaluate_signal_outcome`. O fallback pelas colunas `r_alvo_*` cobre linhas
# resolvidas pelo modelo antigo, que assumia execução no fechamento anterior
# e ignorava custo; ele infla o resultado justamente nos dias de gap.
# `r_realizado IS NULL` nessas linhas é o sinal de que falta rodar
# `analyzer.py --reavaliar-tudo`, e é por isso que a resposta devolve
# `modelo_atual` por grupo: uma taxa montada em cima de dois modelos de
# execução não descreve nenhum dos dois, e isso tem que ficar visível.
_STATS_BASE = """
WITH base AS (
    SELECT modalidade, timeframe, symbol, direcao, mtf_confirmado, resultado,
           COALESCE(
               r_realizado,
               CASE resultado
                    WHEN 'ALVO_2' THEN  COALESCE(r_alvo_2, 3.0)
                    WHEN 'ALVO_1' THEN  COALESCE(r_alvo_1, 1.5)
                    WHEN 'STOP'   THEN -1.0
               END
           ) AS r,
           (r_realizado IS NOT NULL)::int AS modelo_atual,
           CASE WHEN resultado IN ('ALVO_1', 'ALVO_2') THEN 1
                WHEN resultado  =  'STOP'              THEN 0
           END AS acerto,
           CASE WHEN score >= 80 THEN '80+'
                WHEN score >= 70 THEN '70-80'
                WHEN score >= 60 THEN '60-70'
                WHEN score >= 40 THEN '40-60'
                ELSE '<40'
           END AS faixa_score
    FROM signals
    WHERE resultado IS NOT NULL
      AND resultado NOT IN ('SEM_SINAL', 'SEM_ENTRADA')
      -- por `candle_time`, e não `criado_em` — mesma razão do comentário em
      -- get_signals: o backfill grava sinais antigos com criado_em de hoje
      AND candle_time > now() - make_interval(days => %(dias)s)
      AND (%(perfil)s::text    IS NULL OR perfil    = %(perfil)s)
      AND (%(origem)s::text    IS NULL OR origem    = %(origem)s)
      AND (%(symbol)s::text    IS NULL OR symbol    = %(symbol)s)
      AND (%(timeframe)s::text IS NULL OR timeframe = %(timeframe)s)
)
"""

# Seis agregados = seis varreduras da mesma CTE. Nesse volume (10^4-10^5
# linhas) é seq scan + hash aggregate na casa dos milissegundos. GROUPING
# SETS faria tudo numa varredura só e é a versão elegante — MEÇA antes de
# trocar, na mesma linha do comentário sobre índices em schema.sql.
_STATS_SELECT = """
SELECT modalidade,
       {recorte}                                        AS recorte,
       count(*)                                         AS n,
       count(acerto)                                    AS resolvidos,
       coalesce(sum(acerto), 0)                         AS acertos,
       count(*) FILTER (WHERE resultado = 'EM_ABERTO')  AS em_aberto,
       avg(acerto)::float                               AS taxa_acerto,
       avg(r)::float                                    AS expectativa_r,
       count(*) FILTER (WHERE resultado = 'ALVO_1')     AS alvo_1,
       count(*) FILTER (WHERE resultado = 'ALVO_2')     AS alvo_2,
       count(*) FILTER (WHERE resultado = 'STOP')       AS stop,
       coalesce(sum(modelo_atual), 0)                   AS modelo_atual
FROM base GROUP BY 1, 2 ORDER BY 1, 2
"""


def _stats_rows(cur, recorte: str, filtros: dict) -> list[StatsRow]:
    cur.execute(_STATS_BASE + _STATS_SELECT.format(recorte=recorte), filtros)
    return [
        StatsRow(
            modalidade=row[0], recorte=None if row[1] is None else str(row[1]),
            n=row[2], resolvidos=row[3], acertos=row[4], em_aberto=row[5],
            taxa_acerto=row[6], expectativa_r=row[7],
            alvo_1=row[8], alvo_2=row[9], stop=row[10], modelo_atual=row[11],
        )
        for row in cur.fetchall()
    ]


@app.get(
    "/signals/stats",
    response_model=StatsResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="assertividade_sinais",
    summary="Taxa de acerto histórica do motor",
    description=(
        "Quanto o motor acertou de verdade, quebrado por modalidade e ainda por "
        "timeframe, ação, direção, faixa de score e confirmação multi-timeframe. "
        "É a tool pra 'vale a pena confiar neste sinal?'.\n\n"
        "Como ler sem mentir:\n"
        "- `taxa_acerto` e `expectativa_r` têm como denominador `resolvidos`, NUNCA `n` "
        "— os EM_ABERTO ainda não têm desfecho. Cite sempre os dois: '61% em 47 "
        "resolvidos'.\n"
        "- Com `resolvidos` baixo (menos de ~10) o percentual não significa nada; diga "
        "que a amostra é insuficiente em vez de citar o número.\n"
        "- `expectativa_r` é o retorno médio em múltiplos do risco, já líquido de custo. "
        "Negativo quer dizer que o recorte perde dinheiro mesmo acertando às vezes.\n"
        "- Se `modelo_atual` for menor que `resolvidos`, parte das linhas foi medida por "
        "um modelo de execução antigo e mais otimista: a taxa mistura dois critérios e "
        "isso precisa ser dito junto do número."
    ),
)
def get_signal_stats(
    perfil: str | None = Query(None),
    origem: str | None = Query(None),
    symbol: str | None = Query(None),
    timeframe: str | None = Query(None),
    dias: int = Query(90, ge=1, le=3650),
    conn: Connection = Depends(get_conn),
) -> StatsResponse:
    """Assertividade por modalidade, quebrada nos recortes pedidos.

    Duas coisas que a interface PRECISA respeitar pra não mentir:
      - `taxa_acerto` e `expectativa_r` ignoram os EM_ABERTO (avg() pula
        NULL), então o denominador é `resolvidos`, nunca `n`;
      - `expectativa_r` está em múltiplos do R REALIZADO gravado por linha,
        não de um 1.5/3.0 fixo.
    """
    filtros = {
        "perfil": perfil, "origem": origem,
        "symbol": symbol.strip().upper() if symbol else None,
        "timeframe": timeframe.strip().upper() if timeframe else None,
        "dias": dias,
    }
    with conn.cursor() as cur:
        geral = _stats_rows(cur, "NULL", filtros)
        por_timeframe = _stats_rows(cur, "timeframe", filtros)
        por_symbol = _stats_rows(cur, "symbol", filtros)
        por_direcao = _stats_rows(cur, "direcao", filtros)
        por_faixa_score = _stats_rows(cur, "faixa_score", filtros)
        por_mtf = _stats_rows(cur, "mtf_confirmado::text", filtros)

    return StatsResponse(
        filtros=filtros, total=sum(linha.n for linha in geral),
        geral=geral, por_timeframe=por_timeframe, por_symbol=por_symbol,
        por_direcao=por_direcao, por_faixa_score=por_faixa_score, por_mtf=por_mtf,
    )


# ---------------------------------------------------------------------------
# Análise sob demanda
#
# Até 2026-08-10 o motor só rodava dentro do loop do `analyzer`: não havia
# como pedir "analisa VALE3 agora", só esperar a próxima varredura e ler o que
# ela gravou. Isso bastava pro Streamlit (que roda o motor no próprio
# processo), mas não pro agente, que só fala HTTP.
#
# Mesma relação que `POST /rotas/consultar` tem com o worker de voos na
# plataforma: consulta síncrona sob demanda ao lado do worker em background,
# compartilhando o mesmo motor e os mesmos dados.
# ---------------------------------------------------------------------------

# Espelham os defaults do `analyzer` (ANALYZER_TIMEFRAMES/ANALYZER_COUNTS) pra
# que uma consulta avulsa e a varredura periódica leiam a MESMA janela de
# velas. Divergir aqui produziria duas respostas diferentes pra mesma vela,
# sem nada na resposta explicando a diferença.
_ANALISE_TIMEFRAMES_PADRAO = ("M15", "H1", "H4", "D1")
_ANALISE_COUNTS = {"M15": 250, "H1": 250, "H4": 150, "D1": 250, "W1": 250}

# `origem` que NUNCA existe na tabela `signals` — esta rota não grava nada. O
# valor viaja no payload porque `signal_payload` monta o corpo inteiro de uma
# vez, e um valor próprio é justamente o que impede uma leitura de consulta de
# se passar por 'manual' caso alguém encaminhe esta resposta pro POST /signals.
_ORIGEM_CONSULTA = "consulta"


def _params_do_perfil(conn, nome: str | None) -> tuple[str, AnalysisParams]:
    """Resolve o nome do perfil nos parâmetros do motor.

    Perfil pedido e inexistente é 404, não fallback silencioso: responder com
    a calibragem padrão a quem pediu 'agressivo' devolveria números que não
    são os daquele perfil, sem nada na resposta dizendo isso. O fallback só
    vale pro caso de ninguém ter pedido nada e o 'padrão' ainda não ter sido
    seedado — mesma tolerância que o worker tem.
    """
    pedido = (nome or "").strip()
    alvo = pedido or PERFIL_PADRAO
    with conn.cursor() as cur:
        cur.execute("SELECT nome, params FROM analysis_profiles WHERE ativo AND nome = %s", (alvo,))
        row = cur.fetchone()
    if row is None:
        if pedido:
            raise HTTPException(status_code=404, detail=f"Perfil '{pedido}' não existe ou está inativo.")
        return DEFAULT_PROFILE_NAME, AnalysisParams()
    return row[0], AnalysisParams.from_dict(row[1])


@app.post(
    "/analisar",
    response_model=AnaliseResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="analisar_simbolo",
    summary="Rodar a análise técnica de uma ação agora",
    description=(
        "Roda o motor AGORA sobre os dados já coletados e devolve, por timeframe, as "
        "cinco leituras (Confluência, SMC, Price Action, Médias Móveis, VWAP) com "
        "direção, score, entrada, stop e alvos. É a tool para 'como está a VALE3?' ou "
        "'tem entrada em PETR4?'.\n\n"
        "O que a resposta significa:\n"
        "- `mtf_confirmado` é o que separa um sinal sério de um ruído: só é true quando "
        "os dois timeframes de confirmação concordam na mesma direção naquela "
        "modalidade. Sem ele, trate a leitura como fraca mesmo com score alto.\n"
        "- `direcao` NEUTRO com `entrada` nula é resposta legítima e comum: quer dizer "
        "que não há entrada, não que faltou dado.\n"
        "- `erros` lista os timeframes que não puderam ser analisados e por quê.\n\n"
        "Analisa apenas ações que estão em `listar_watchlist`. NÃO grava nada e NÃO "
        "envia ordem: é leitura de mercado, não recomendação de investimento nem "
        "execução. Para o desempenho histórico desse tipo de sinal, combine com "
        "`assertividade_sinais`."
    ),
)
def analisar(body: AnaliseIn, conn: Connection = Depends(get_conn)) -> AnaliseResponse:
    """Análise sob demanda. Não persiste: consulta não é medição.

    Gravar aqui contaminaria `signals` com leituras que ninguém operou e que
    nem sequer são de vela nova — a assertividade passaria a medir "o que
    alguém perguntou" junto com "o que o motor produziu".
    """
    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Símbolo vazio.")

    pedidos = [tf.strip().upper() for tf in (body.timeframes or _ANALISE_TIMEFRAMES_PADRAO)]
    desconhecidos = [tf for tf in pedidos if tf not in _ANALISE_COUNTS]
    if desconhecidos:
        raise HTTPException(
            status_code=400,
            detail=f"Timeframe(s) {desconhecidos} não reconhecido(s). Use: {sorted(_ANALISE_COUNTS)}.",
        )

    modalidade = (body.modalidade or "").strip() or None
    if modalidade is not None and modalidade not in MODALITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Modalidade '{modalidade}' não existe. Use uma de: {list(MODALITIES)}.",
        )

    nome_perfil, params = _params_do_perfil(conn, body.perfil)

    # Os timeframes de confirmação entram na análise mesmo quando não foram
    # pedidos, e só não aparecem nas leituras devolvidas. Sem isso, pedir só
    # D1 devolveria `mtf_confirmado=false` em tudo — não porque os
    # timeframes discordam, mas porque ninguém olhou —, e um false que
    # significa "não sei" no mesmo campo que um false que significa "não
    # concordam" é pior que não ter o campo.
    a_analisar = list(dict.fromkeys([*pedidos, *DAYTRADE_CONFIRMATION_TIMEFRAMES]))

    analisado: dict[str, tuple] = {}
    erros: dict[str, str] = {}
    for timeframe in a_analisar:
        try:
            df = ler_candles(conn, symbol, timeframe, _ANALISE_COUNTS[timeframe])
            analisado[timeframe] = analyze(df, params)
        except Exception as exc:  # noqa: BLE001 — um timeframe sem dado não derruba os outros
            erros[timeframe] = str(exc)

    if not any(tf in analisado for tf in pedidos):
        raise HTTPException(
            status_code=404,
            detail=f"Sem dados analisáveis para {symbol} em {pedidos}: {erros}",
        )

    sinais_por_tf = {tf: sinais for tf, (_, sinais) in analisado.items()}
    # Uma confirmação POR MODALIDADE — carimbar a da Confluência numa linha de
    # SMC diria que o SMC foi confirmado quando quem concordou foi outra
    # leitura. Ver o docstring de `mtf_confirmation`.
    confirmacao = {
        nome: mtf_confirmation(sinais_por_tf, DAYTRADE_CONFIRMATION_TIMEFRAMES, nome)
        for nome in MODALITIES
    }

    leituras: list[SignalIn] = []
    for timeframe in pedidos:
        if timeframe not in analisado:
            continue
        contexto, sinais = analisado[timeframe]
        for sinal in sinais:
            if sinal.name not in MODALITIES:
                continue
            if modalidade is not None and sinal.name != modalidade:
                continue
            confirmado, direcao_mtf = confirmacao[sinal.name]
            leituras.append(SignalIn(**signal_payload(
                symbol, timeframe, sinal, contexto, nome_perfil, params,
                _ORIGEM_CONSULTA, confirmado, direcao_mtf,
            )))

    return AnaliseResponse(
        symbol=symbol,
        perfil=nome_perfil,
        confirmacao=list(DAYTRADE_CONFIRMATION_TIMEFRAMES),
        analisado_em=datetime.now(UTC),
        leituras=leituras,
        # Só os erros dos timeframes que o usuário pediu: um erro de M15 puxado
        # pra dentro só por causa da confirmação viraria ruído numa consulta
        # que perguntou sobre D1.
        erros={tf: msg for tf, msg in erros.items() if tf in pedidos},
    )
