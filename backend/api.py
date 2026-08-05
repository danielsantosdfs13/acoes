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

from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg import Connection
from psycopg.types.json import Jsonb

import auth
from auth import require_api_key
from db import get_conn
from models import (
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

log = logging.getLogger("api")

app = FastAPI(title="Ações — API (leitura)")

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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/candles", response_model=CandlesResponse)
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


@app.get("/watchlist", response_model=WatchlistResponse)
def get_watchlist(conn: Connection = Depends(get_conn)) -> WatchlistResponse:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        symbols = [row[0] for row in cur.fetchall()]
    return WatchlistResponse(symbols=symbols)


@app.put("/watchlist", response_model=WatchlistResponse, dependencies=[Depends(require_api_key)])
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


@app.get("/status", response_model=list[SymbolStatus])
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


@app.get("/profiles", response_model=ProfilesResponse, dependencies=[Depends(require_api_key)])
def get_profiles(conn: Connection = Depends(get_conn)) -> ProfilesResponse:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_PROFILE_COLUMNS} FROM analysis_profiles WHERE ativo ORDER BY nome")
        rows = cur.fetchall()
    return ProfilesResponse(profiles=[_profile_from_row(row) for row in rows])


@app.get("/profiles/{nome}", response_model=ProfileOut, dependencies=[Depends(require_api_key)])
def get_profile(nome: str, conn: Connection = Depends(get_conn)) -> ProfileOut:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_PROFILE_COLUMNS} FROM analysis_profiles WHERE nome = %s", (nome.strip(),))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Perfil '{nome}' não existe.")
    return _profile_from_row(row)


@app.put("/profiles/{nome}", response_model=ProfileOut, dependencies=[Depends(require_api_key)])
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


@app.delete("/profiles/{nome}", response_model=ProfilesResponse, dependencies=[Depends(require_api_key)])
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


@app.post("/signals", response_model=SignalSaveResult, dependencies=[Depends(require_api_key)])
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


@app.get("/signals", response_model=SignalsResponse, dependencies=[Depends(require_api_key)])
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


# A CTE que todos os recortes de assertividade compartilham. Só entram os
# sinais com desfecho: 'SEM_SINAL' fica de fora porque não era operável, e
# `resultado IS NULL` porque o worker ainda não avaliou.
#
# O R vem da COLUNA, não de um 1.5/3.0 cravado: `rr_alvo_1`/`rr_alvo_2` são
# parâmetros por perfil, então um CASE fixo aqui mentiria pra qualquer
# perfil que os tivesse ajustado. O COALESCE cobre só linhas antigas.
_STATS_BASE = """
WITH base AS (
    SELECT modalidade, timeframe, symbol, direcao, mtf_confirmado, resultado,
           CASE resultado
                WHEN 'ALVO_2' THEN  COALESCE(r_alvo_2, 3.0)
                WHEN 'ALVO_1' THEN  COALESCE(r_alvo_1, 1.5)
                WHEN 'STOP'   THEN -1.0
           END AS r,
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
      AND resultado <> 'SEM_SINAL'
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
       count(*) FILTER (WHERE resultado = 'STOP')       AS stop
FROM base GROUP BY 1, 2 ORDER BY 1, 2
"""


def _stats_rows(cur, recorte: str, filtros: dict) -> list[StatsRow]:
    cur.execute(_STATS_BASE + _STATS_SELECT.format(recorte=recorte), filtros)
    return [
        StatsRow(
            modalidade=row[0], recorte=None if row[1] is None else str(row[1]),
            n=row[2], resolvidos=row[3], acertos=row[4], em_aberto=row[5],
            taxa_acerto=row[6], expectativa_r=row[7],
            alvo_1=row[8], alvo_2=row[9], stop=row[10],
        )
        for row in cur.fetchall()
    ]


@app.get("/signals/stats", response_model=StatsResponse, dependencies=[Depends(require_api_key)])
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
