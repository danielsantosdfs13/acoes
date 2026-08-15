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
  - PUT    /profiles/{nome}/ativo            liga/desliga a geração de alertas
                                            de um perfil no analyzer (tool)

  - GET    /auto-acompanhamento              lista as regras de auto-acompanhamento
                                            ativas (tool)
  - PUT    /auto-acompanhamento              cria ou atualiza uma regra de
                                            auto-acompanhamento (tool)

  - POST   /signals                          grava um sinal (dedup por vela)
  - GET    /signals                          histórico com filtros
  - GET    /signals/stats                    assertividade por recorte

  - GET    /auto-ordem                       regras de ordem automática (tool)
  - PUT    /auto-ordem                       cria ou atualiza uma regra (tool)
  - DELETE /auto-ordem                       apaga uma regra (tool)
  - GET    /ordens                           ordens enviadas, com a regra que as
                                            produziu e o desfecho (tool)
  - GET    /ordens/stats                     desempenho financeiro por recorte (tool)
  - POST   /ordens                           reserva antes do envio
  - PUT    /ordens/{signal_id}               registra a resposta da corretora
  - PUT    /ordens/{signal_id}/fechamento    registra o desfecho lido do MT5
  - DELETE /ordens                           reset de desenvolvimento: apaga
                                            TODAS as ordens (nunca tool)

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
    AutoAcompanhamentoIn,
    AutoAcompanhamentoOut,
    AutoAcompanhamentoResponse,
    AutoOrdemIn,
    AutoOrdemOut,
    AutoOrdemResponse,
    CandleOut,
    CandlesResponse,
    FeedbackIn,
    FeedbackOut,
    FeedbackResponse,
    OrdemFechamento,
    OrdemIn,
    OrdemOut,
    OrdemReserva,
    OrdemResultado,
    OrdemStatsResponse,
    OrdemStatsRow,
    OrdensLimpeza,
    OrdensResponse,
    ProfileAtivoIn,
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
    WebhookPayload,
)

# `candles.py` já fez o `sys.path.insert` que põe a raiz do repo no caminho —
# é de lá que este import se resolve no checkout. Na imagem tudo está achatado
# em /app e resolveria de qualquer jeito.
from daytrade_smc import (  # noqa: E402
    DAYTRADE_CONFIRMATION_TIMEFRAMES,
    DEFAULT_PROFILE_NAME,
    LOCAL_TZ,
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
    operation_id="health",
    summary="Liveness da API de ações",
    description="Responde `{'status':'ok'}` se o serviço está de pé. Não diz nada sobre o "
                "frescor dos dados — pra isso use `status_dados`.",
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
    operation_id="status_dados",
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


# Este é o único caminho de ESCRITA de perfil que vira tool, e de propósito:
# as três rotas de calibragem acima ficam fora da spec porque reescrever a
# calibragem é decisão de bancada. Desligar/religar a GERAÇÃO de alertas não
# reescreve nada — é só a flag `ativo`, o analyzer já a respeita na varredura,
# e é exatamente o tipo de ação que um agente pode ser incumbido de tomar.
@app.put(
    "/profiles/{nome}/ativo",
    response_model=ProfileOut,
    dependencies=[Depends(require_api_key)],
    operation_id="alterar_perfil_ativo",
    summary="Liga ou desliga a geração de alertas de um perfil",
    description=(
        "Ativa ou desativa um perfil de calibragem na geração automática de "
        "alertas do analyzer. Com `ativo=false` o analyzer pula esse perfil na "
        "próxima varredura e nenhum sinal novo dele é gravado; com `ativo=true` "
        "ele volta na varredura seguinte. Só mexe na flag `ativo`: não altera "
        "`params`, não apaga histórico — sinais antigos continuam na base para "
        "a assertividade. Para ver os perfis existentes use "
        "`listar_perfis_analise`; um perfil desativado deixa de "
        "aparecer nessa lista. ATENÇÃO: desativar o último perfil ativo "
        "reintroduz o fallback do motor (padrão), porque o analyzer precisa "
        "de pelo menos um perfil para funcionar."
    ),
)
def set_profile_ativo(nome: str, body: ProfileAtivoIn,
                      conn: Connection = Depends(get_conn)) -> ProfileOut:
    """Liga/desliga a flag `ativo` de um perfil.

    Desativar e depois reativar devolve o perfil exatamente como estava —
    params, descricao e histórico não mudam, e a FK em `signals.perfil`
    continua apontando pra ele sem quebrar.
    """
    nome = nome.strip()
    if not nome:
        raise HTTPException(status_code=400, detail="Nome de perfil vazio.")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE analysis_profiles
               SET ativo = %s, alterado_em = now()
             WHERE nome = %s
            RETURNING {_PROFILE_COLUMNS}
            """,
            (body.ativo, nome),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Perfil '{nome}' não existe.")
    return _profile_from_row(row)


# ---------------------------------------------------------------------------
# Sinais
# ---------------------------------------------------------------------------

_SIGNAL_FIELDS = (
    "id", "symbol", "timeframe", "modalidade", "candle_time", "perfil", "params_hash",
    "origem", "direcao", "score", "confianca", "setup", "mtf_confirmado", "mtf_direcao",
    "entrada", "stop", "alvo_1", "alvo_2", "r_alvo_1", "r_alvo_2", "stop_basis",
    "detalhes", "criado_em", "resultado", "resultado_detalhe", "candles_ate_resultado",
    "avaliado_em",
)
_SIGNAL_COLUMNS = ", ".join(_SIGNAL_FIELDS)
# Mesma lista qualificada, pra quando `signals` entra numa consulta com JOIN
# e `id`/`origem` viram ambíguos. O RETURNING do POST /signals continua usando
# a versão sem prefixo — lá não há junção, e `feedback` (que vem de outra
# tabela) não pode aparecer num RETURNING.
_SIGNAL_COLUMNS_S = ", ".join(f"s.{campo}" for campo in _SIGNAL_FIELDS)

# A decisão mais recente de cada sinal. LATERAL e não um GROUP BY porque a
# `signal_feedback` guarda o histórico: um sinal pode ter sido marcado
# ACOMPANHAR e depois OPEREI, e o que descreve o estado atual é a última.
_FEEDBACK_LATERAL = """
LEFT JOIN LATERAL (
    SELECT f.acao, f.origem
    FROM signal_feedback f
    WHERE f.signal_id = s.id
    ORDER BY f.criado_em DESC, f.id DESC
    LIMIT 1
) fb ON true
"""

# Valor de consulta, NÃO de armazenamento: nenhuma linha de `signal_feedback`
# tem acao='PENDENTE'. É como se pergunta por "sinal sobre o qual ninguém
# decidiu nada" sem inventar um segundo parâmetro booleano.
_ACAO_PENDENTE = "PENDENTE"

# Os ::text em TODAS as ocorrências, não só na primeira: cada `%(acao)s` é um
# placeholder independente pro driver, e o mesmo problema de inferência de
# tipo descrito no comentário de `get_signals` vale para cada um deles.
_FEEDBACK_WHERE = f"""
  AND (%(acao)s::text IS NULL
       OR (%(acao)s::text = '{_ACAO_PENDENTE}' AND fb.acao IS NULL)
       OR fb.acao = %(acao)s::text)
"""


def _signal_from_row(row: tuple) -> SignalOut:
    """Constrói o SignalOut. As duas últimas posições só existem quando a
    consulta trouxe o LATERAL de feedback junto — o RETURNING do POST não
    traz, e aí os campos ficam None."""
    return SignalOut(
        id=row[0], symbol=row[1], timeframe=row[2], modalidade=row[3], candle_time=row[4],
        perfil=row[5], params_hash=row[6], origem=row[7], direcao=row[8], score=row[9],
        confianca=row[10], setup=row[11], mtf_confirmado=row[12], mtf_direcao=row[13],
        entrada=row[14], stop=row[15], alvo_1=row[16], alvo_2=row[17], r_alvo_1=row[18],
        r_alvo_2=row[19], stop_basis=row[20], detalhes=row[21], criado_em=row[22],
        resultado=row[23], resultado_detalhe=row[24], candles_ate_resultado=row[25],
        avaliado_em=row[26],
        feedback=row[27] if len(row) > 27 else None,
        feedback_origem=row[28] if len(row) > 28 else None,
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
    acao: str | None = Query(
        None,
        description=(
            "Decisão mais recente do operador sobre o sinal: ACOMPANHAR, OPERAR, "
            "IGNORAR, OPEREI, CANCELEI — ou PENDENTE para os que ainda não têm "
            "decisão nenhuma. Não confundir com `resultado`, que é o desfecho do "
            "PREÇO (bateu alvo ou stop); este é o que a pessoa decidiu fazer."
        ),
    ),
    dias: int = Query(90, ge=1, le=3650),
    limite: int = Query(200, ge=1, le=2000),
    conn: Connection = Depends(get_conn),
) -> SignalsResponse:
    """Histórico de sinais, do mais recente pro mais antigo."""
    filtros = {
        "symbol": symbol.strip().upper() if symbol else None,
        "timeframe": timeframe.strip().upper() if timeframe else None,
        "modalidade": modalidade, "perfil": perfil, "origem": origem,
        "resultado": resultado, "acao": acao.strip().upper() if acao else None,
        "dias": dias, "limite": limite,
    }
    # Os ::text não são decoração: num `$1 IS NULL OR col = $1`, o Postgres
    # olha o IS NULL primeiro e desiste de inferir o tipo do parâmetro
    # ("could not determine data type of parameter"). O cast resolve.
    # A janela é por `candle_time`, NÃO por `criado_em`: "últimos 90 dias"
    # significa os sinais das velas desse período, não as linhas inseridas
    # nesse período. Enquanto só o worker escrevia, em tempo real, os dois
    # davam no mesmo; com o backfill (`analyzer.py --backfill`) deixam de
    # dar — um sinal de D1 de 2022 gravado hoje cairia no recorte de 7 dias.
    where = f"""
        WHERE s.candle_time > now() - make_interval(days => %(dias)s)
          AND (%(symbol)s::text     IS NULL OR s.symbol     = %(symbol)s)
          AND (%(timeframe)s::text  IS NULL OR s.timeframe  = %(timeframe)s)
          AND (%(modalidade)s::text IS NULL OR s.modalidade = %(modalidade)s)
          AND (%(perfil)s::text     IS NULL OR s.perfil     = %(perfil)s)
          AND (%(origem)s::text     IS NULL OR s.origem     = %(origem)s)
          AND (%(resultado)s::text  IS NULL OR s.resultado  = %(resultado)s)
          {_FEEDBACK_WHERE}
    """
    de = f"FROM signals s {_FEEDBACK_LATERAL} {where}"
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) {de}", filtros)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {_SIGNAL_COLUMNS_S}, fb.acao, fb.origem {de} "
            "ORDER BY s.candle_time DESC, s.id DESC LIMIT %(limite)s",
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
_STATS_BASE = f"""
WITH base AS (
    SELECT s.modalidade, s.timeframe, s.symbol, s.direcao, s.mtf_confirmado, s.resultado,
           -- 'PENDENTE' e não NULL: como recorte, "ninguém decidiu" é um
           -- grupo tão legítimo quanto os outros, e um rótulo nulo apareceria
           -- como linha em branco na tabela.
           COALESCE(fb.acao, '{_ACAO_PENDENTE}') AS feedback,
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
           -- Separa 90-100 do resto do antigo "80+": o harness de 2026-08-12
           -- mediu o topo do score como a PIOR faixa (expR −0,25, e 100%
           -- Confluência), enquanto 70-80 é a melhor. Um recorte "80+"
           -- agrupando as duas esconderia exatamente esse sinal.
           CASE WHEN score >= 90 THEN '90-100'
                WHEN score >= 80 THEN '80-90'
                WHEN score >= 70 THEN '70-80'
                WHEN score >= 60 THEN '60-70'
                WHEN score >= 40 THEN '40-60'
                ELSE '<40'
           END AS faixa_score,
           -- RVOL lido do JSONB `detalhes`: o motor grava `context.rvol` ali
           -- desde sempre, mas nenhum recorte o lia. As faixas são as MESMAS
           -- do harness (`scripts/analisar-varredura.py`), ancoradas no gate
           -- de volume que `candle_patterns` e `_confirmed_breakout` já
           -- aplicam — e que a varredura de 2026-08-12 mediu como a pior
           -- faixa. Recorte novo para responder isso em produção.
           CASE WHEN NOT (detalhes ? 'rvol') THEN 'sem dado'
                WHEN (detalhes->>'rvol')::float < 0.8 THEN 'a) < 0,8'
                WHEN (detalhes->>'rvol')::float < 1.0 THEN 'b) 0,8-1,0'
                WHEN (detalhes->>'rvol')::float < 1.3 THEN 'c) 1,0-1,3'
                WHEN (detalhes->>'rvol')::float < 2.0 THEN 'd) 1,3-2,0'
                ELSE 'e) >= 2,0'
           END AS faixa_rvol
    FROM signals s
    {_FEEDBACK_LATERAL}
    WHERE s.resultado IS NOT NULL
      AND s.resultado NOT IN ('SEM_SINAL', 'SEM_ENTRADA')
      -- por `candle_time`, e não `criado_em` — mesma razão do comentário em
      -- get_signals: o backfill grava sinais antigos com criado_em de hoje
      AND s.candle_time > now() - make_interval(days => %(dias)s)
      AND (%(perfil)s::text    IS NULL OR s.perfil    = %(perfil)s)
      -- `s.origem` qualificado não é estilo: o LATERAL expõe `fb.origem`
      -- (web/whatsapp/telegram/auto) e sem o prefixo o Postgres recusaria a
      -- consulta por ambiguidade. São dois "origem" que falam de coisas
      -- diferentes — quem gerou o SINAL, e por onde veio a DECISÃO.
      AND (%(origem)s::text    IS NULL OR s.origem    = %(origem)s)
      AND (%(symbol)s::text    IS NULL OR s.symbol    = %(symbol)s)
      AND (%(timeframe)s::text IS NULL OR s.timeframe = %(timeframe)s)
      {_FEEDBACK_WHERE}
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
        "timeframe, ação, direção, faixa de score, RVOL, confirmação "
        "multi-timeframe e decisão do operador. É a tool pra 'vale a pena "
        "confiar neste sinal?'.\n\n"
        "Dois recortes novos no 2026-08-14, vindos de uma varredura com régua do "
        "acaso (~422 mil avaliações, cada candle com um trade cara-ou-coroa de "
        "referência): a faixa de score 90-100 mediu a PIOR expectativa (−0,25R, "
        "e 100% Confluência) com 70-80 como a melhor — um recorte '80+' unificado "
        "escondia isso; e `por_rvol` lê `detalhes->>'rvol'` (faixas do harness: "
        "o gate de volume atual 1,3-2,0× mediu a pior faixa). São medições, não "
        "decisões de motor — existem para acompanhar o comportamento em produção.\n\n"
        "`por_feedback` recorta pelo que a PESSOA decidiu (ACOMPANHAR, OPERAR, "
        "IGNORAR, OPEREI, CANCELEI, ou PENDENTE quando não decidiu nada), e o filtro "
        "`acao` restringe a conta a um desses grupos — é assim que se responde "
        "'acertei mais no que eu escolhi operar do que na média?'. Cuidado ao ler: "
        "feedback com `feedback_origem='auto'` foi gerado por regra do worker, não "
        "escolhido por ninguém; se a pergunta é sobre a escolha humana, esses não "
        "contam.\n\n"
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
    acao: str | None = Query(
        None,
        description=(
            "Restringe a conta aos sinais com esta decisão do operador: ACOMPANHAR, "
            "OPERAR, IGNORAR, OPEREI, CANCELEI, ou PENDENTE para os sem decisão."
        ),
    ),
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
        "acao": acao.strip().upper() if acao else None,
        "dias": dias,
    }
    with conn.cursor() as cur:
        geral = _stats_rows(cur, "NULL", filtros)
        por_timeframe = _stats_rows(cur, "timeframe", filtros)
        por_symbol = _stats_rows(cur, "symbol", filtros)
        por_direcao = _stats_rows(cur, "direcao", filtros)
        por_faixa_score = _stats_rows(cur, "faixa_score", filtros)
        por_rvol = _stats_rows(cur, "faixa_rvol", filtros)
        por_mtf = _stats_rows(cur, "mtf_confirmado::text", filtros)
        por_feedback = _stats_rows(cur, "feedback", filtros)

    return StatsResponse(
        filtros=filtros, total=sum(linha.n for linha in geral),
        geral=geral, por_timeframe=por_timeframe, por_symbol=por_symbol,
        por_direcao=por_direcao, por_faixa_score=por_faixa_score,
        por_rvol=por_rvol, por_mtf=por_mtf, por_feedback=por_feedback,
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
_ANALISE_COUNTS = {"M2": 300, "M5": 300, "M15": 250, "H1": 250, "H4": 150, "D1": 250, "W1": 250}

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
        "seis leituras (Confluência, SMC, Price Action, Médias Móveis, VWAP, IFR) com "
        "direção, score, entrada, stop e alvos. É a tool para 'como está a VALE3?' ou "
        "'tem entrada em PETR4?'.\n\n"
        "O que a resposta significa:\n"
        "- `mtf_confirmado` é o que separa um sinal sério de um ruído: só é true quando "
        "os dois timeframes de confirmação concordam na mesma direção naquela "
        "modalidade. Sem ele, trate a leitura como fraca mesmo com score alto.\n"
        "- `IFR` só opera em EXAUSTÃO (≤10 ou ≥90 por padrão), então NEUTRO é a resposta "
        "quase sempre — isso é o desenho, não falta de dado. Quando ele dispara, é raro "
        "e vale mais que as outras leituras isoladas.\n"
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
    #
    # O Diário entra pela MESMA razão, agora que o IFR dele filtra as
    # leituras dos outros prazos (ver `rsi_signal`): sem ele, esta rota
    # devolveria pro agente uma leitura de IFR diferente da que a
    # interface mostra pro mesmo candle. Ele vem PRIMEIRO na lista porque
    # o valor precisa existir antes de ser injetado nos demais.
    a_analisar = list(dict.fromkeys(["D1", *pedidos, *DAYTRADE_CONFIRMATION_TIMEFRAMES]))

    analisado: dict[str, tuple] = {}
    erros: dict[str, str] = {}
    daily_rsi: float | None = None
    for timeframe in a_analisar:
        try:
            df = ler_candles(conn, symbol, timeframe, _ANALISE_COUNTS[timeframe])
            contexto, sinais = analyze(
                df, params, higher_rsi=None if timeframe == "D1" else daily_rsi
            )
            analisado[timeframe] = (contexto, sinais)
            if timeframe == "D1":
                daily_rsi = contexto.rsi
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

# ========================================================================
# Auto-acompanhamento
# ========================================================================


@app.get(
    "/auto-acompanhamento",
    response_model=AutoAcompanhamentoResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="listar_auto_acompanhamento",
    summary="Regras de auto-acompanhamento ativas",
    description=(
        "Lista as regras de auto-acompanhamento ativas: cada regra diz "
        "'para o perfil X e modalidade Y, marque automaticamente como "
        "ACOMPANHAR'. Use `configurar_auto_acompanhamento` para criar "
        "ou atualizar uma regra. O worker-acoes aplica essas regras a "
        "cada ciclo: sinais que batem ganham feedback automático."
    ),
)
def list_auto_acompanhamento(
    conn: Connection = Depends(get_conn),
) -> AutoAcompanhamentoResponse:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT perfil, modalidade, ativo, criado_em "
            "FROM auto_acompanhamento WHERE ativo ORDER BY criado_em DESC"
        )
        rows = cur.fetchall()
    return AutoAcompanhamentoResponse(regras=[
        AutoAcompanhamentoOut(perfil=r[0], modalidade=r[1], ativo=r[2], criado_em=r[3])
        for r in rows
    ])


@app.put(
    "/auto-acompanhamento",
    response_model=AutoAcompanhamentoOut,
    dependencies=[Depends(require_api_key)],
    operation_id="configurar_auto_acompanhamento",
    summary="Cria ou atualiza uma regra de auto-acompanhamento",
    description=(
        "Configura uma regra de auto-acompanhamento: todo sinal do "
        "`perfil` + `modalidade` informados será automaticamente marcado "
        "como ACOMPANHAR pelo worker. Passe `ativo=false` para desativar "
        "a regra sem removê-la. O worker lê as regras a cada ciclo "
        "(~15 min); o efeito é visível a partir do próximo sinal "
        "capturado, não retroativo. As modalidades válidas são: "
        "Confluência, SMC, Price Action, Médias Móveis, VWAP. Os perfis "
        "válidos estão em `listar_perfis_analise`."
    ),
)
def configurar_auto_acompanhamento(
    body: AutoAcompanhamentoIn,
    conn: Connection = Depends(get_conn),
) -> AutoAcompanhamentoOut:
    perfil = body.perfil.strip()
    modalidade = body.modalidade.strip()
    if not perfil or not modalidade:
        raise HTTPException(status_code=400, detail="perfil e modalidade são obrigatórios.")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO auto_acompanhamento (perfil, modalidade, ativo)
            VALUES (%s, %s, %s)
            ON CONFLICT (perfil, modalidade) DO UPDATE
              SET ativo = EXCLUDED.ativo, criado_em = now()
            RETURNING perfil, modalidade, ativo, criado_em
            """,
            (perfil, modalidade, body.ativo),
        )
        row = cur.fetchone()
    return AutoAcompanhamentoOut(perfil=row[0], modalidade=row[1], ativo=row[2], criado_em=row[3])


# ========================================================================
# Feedback e webhooks
# ========================================================================

@app.post(
    "/signals/{signal_id}/feedback",
    response_model=FeedbackOut,
    dependencies=[Depends(require_api_key)],
    include_in_schema=False,
)
def save_feedback(signal_id: int, body: FeedbackIn, conn: Connection = Depends(get_conn)) -> FeedbackOut:
    """Registra feedback do usuario sobre um sinal."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signal_feedback (signal_id, acao, origem, nota)
            VALUES (%s, %s, %s, %s)
            RETURNING id, signal_id, acao, origem, nota, criado_em
            """,
            (signal_id, body.acao, body.origem, body.nota),
        )
        row = cur.fetchone()
    return FeedbackOut(id=row[0], signal_id=row[1], acao=row[2], origem=row[3],
                       nota=row[4], criado_em=row[5])


@app.get(
    "/signals/feedback",
    response_model=FeedbackResponse,
    dependencies=[Depends(require_api_key)],
    include_in_schema=False,
)
def list_feedback(
    acao: str | None = Query(None),
    origem: str | None = Query(None),
    signal_id: int | None = Query(None),
    limite: int = Query(200, ge=1, le=2000),
    dias: int = Query(30, ge=1, le=3650),
    conn: Connection = Depends(get_conn),
) -> FeedbackResponse:
    """Lista feedbacks com filtros."""
    where = ["criado_em > now() - make_interval(days => %(dias)s)"]
    params = {"dias": dias, "limite": limite}

    if acao:
        where.append("acao = %(acao)s")
        params["acao"] = acao
    if origem:
        where.append("origem = %(origem)s")
        params["origem"] = origem
    if signal_id:
        where.append("signal_id = %(signal_id)s")
        params["signal_id"] = signal_id

    clause = " AND ".join(where)

    with conn.cursor() as cur:
        # Contagem
        cur.execute(
            f"SELECT COALESCE(jsonb_object_agg(acao, cnt), '{{}}'::jsonb) FROM "
            f"(SELECT acao, COUNT(*) AS cnt FROM signal_feedback "
            f"WHERE {clause} GROUP BY acao) sub",
            params,
        )
        por_acao = cur.fetchone()[0] or {}

        cur.execute(
            f"""
            SELECT sf.id, sf.signal_id, sf.acao, sf.origem, sf.nota, sf.criado_em,
                   s.symbol, s.timeframe, s.direcao, s.entrada, s.stop, s.alvo_1, s.resultado, s.perfil
            FROM signal_feedback sf
            JOIN signals s ON s.id = sf.signal_id
            WHERE {clause}
            ORDER BY sf.criado_em DESC
            LIMIT %(limite)s
            """,
            params,
        )
        rows = cur.fetchall()

    feedbacks = [
        FeedbackOut(id=r[0], signal_id=r[1], acao=r[2], origem=r[3],
                    nota=r[4], criado_em=r[5])
        for r in rows
    ]
    # Inject signal info as extra context via nota
    for fb, r in zip(feedbacks, rows):
        fb.__dict__['_signal_symbol'] = r[6]
        fb.__dict__['_signal_tf'] = r[7]
        fb.__dict__['_signal_direcao'] = r[8]
        fb.__dict__['_signal_entrada'] = r[9]

    return FeedbackResponse(feedbacks=feedbacks, total=len(feedbacks), por_acao=por_acao)


@app.post(
    "/webhooks/acoes",
    response_model=FeedbackOut,
    include_in_schema=False,
)
def webhook_acoes(body: WebhookPayload, conn: Connection = Depends(get_conn)) -> FeedbackOut:
    """Webhook generico pra WhatsApp/Telegram/n8n. Sem autenticacao extra
    porque o unico path de entrada e via gateway interno do cluster."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signal_feedback (signal_id, acao, origem, nota)
            VALUES (%s, %s, %s, %s)
            RETURNING id, signal_id, acao, origem, nota, criado_em
            """,
            (body.signal_id, body.acao, body.origem, body.nota),
        )
        row = cur.fetchone()
    return FeedbackOut(id=row[0], signal_id=row[1], acao=row[2], origem=row[3],
                       nota=row[4], criado_em=row[5])


# ========================================================================
# Ordens
#
# ⚠️ As rotas de ESCRITA daqui (`POST /ordens`, `PUT /ordens/{id}`) são
# `include_in_schema=False`, e isso NÃO é o mesmo motivo dos outros casos do
# arquivo. Nas outras rotas ocultas o argumento é "um modelo escrevendo isso
# corromperia a medição". Aqui é mais duro: uma tool de mandar ordem põe
# dinheiro ao alcance de um texto gerado. Quem manda ordem é o `executor/`
# rodando na VM, com as travas de `execucao.py` — nunca o agentgateway.
#
# As regras (`/auto-ordem`) e a leitura (`GET /ordens`) SÃO tools: decidir
# que perfil opera e conferir o que foi enviado é exatamente o tipo de
# pergunta que se quer poder fazer em linguagem natural. A diferença é que
# nenhuma delas dispara ordem por si.
# ========================================================================


@app.get(
    "/auto-ordem",
    response_model=AutoOrdemResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="listar_auto_ordem",
    summary="Regras de ordem automática ativas",
    description=(
        "Lista as regras de ordem automática: cada uma diz 'para este perfil, "
        "modalidade e timeframe, envie ordem, arriscando no máximo R$ X'. "
        "`risco_maximo` é em REAIS — a quantidade é calculada na hora, a partir "
        "da distância entre entrada e stop do sinal, então toda operação arrisca "
        "o mesmo valor. Quem executa é o serviço na VM Windows (o MetaTrader 5 é "
        "DLL de Windows e não roda no cluster); esta rota só descreve as regras.\n\n"
        "Por padrão traz só as LIGADAS — que são as que mandam ordem. Use "
        "`incluir_inativas=true` para ver também as desligadas, por exemplo pra "
        "responder 'o que eu já tentei e desliguei?'."
    ),
)
def list_auto_ordem(
    incluir_inativas: bool = Query(
        False,
        description="Inclui as regras desligadas (ativo=false) além das ligadas.",
    ),
    conn: Connection = Depends(get_conn),
) -> AutoOrdemResponse:
    # O default é FALSE e tem que continuar sendo: o executor consome esta
    # mesma rota e percorre a lista inteira sem olhar `ativo`. Inverter o
    # default aqui faria ele voltar a mandar ordem por regra desligada — o
    # tipo de mudança que parece cosmética e volta a operar sozinha.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT perfil, modalidade, timeframe, risco_maximo, ativo, "
            "exigir_mtf, criado_em "
            "FROM auto_ordem WHERE (ativo OR %s) ORDER BY ativo DESC, criado_em DESC",
            (incluir_inativas,),
        )
        rows = cur.fetchall()
    return AutoOrdemResponse(regras=[
        AutoOrdemOut(perfil=r[0], modalidade=r[1], timeframe=r[2],
                     risco_maximo=float(r[3]), ativo=r[4], exigir_mtf=r[5],
                     criado_em=r[6])
        for r in rows
    ])


@app.put(
    "/auto-ordem",
    response_model=AutoOrdemOut,
    dependencies=[Depends(require_api_key)],
    operation_id="configurar_auto_ordem",
    summary="Cria ou atualiza uma regra de ordem automática",
    description=(
        "Cria ou atualiza a regra de (perfil, modalidade, timeframe). Use "
        "`ativo=false` para desligar sem apagar o histórico.\n\n"
        "Antes de ligar uma regra, confira com `assertividade_sinais` se aquele "
        "recorte tem amostra suficiente — `resolvidos` baixo (menos de ~10) não "
        "sustenta decisão de operar. E lembre que `timeframe` é obrigatório de "
        "propósito: sem ele a mesma leitura em M15, H1, H4 e D1 abriria quatro "
        "posições no mesmo ativo."
    ),
)
def configurar_auto_ordem(
    body: AutoOrdemIn, conn: Connection = Depends(get_conn)
) -> AutoOrdemOut:
    if body.risco_maximo <= 0:
        raise HTTPException(status_code=400, detail="risco_maximo tem que ser maior que zero.")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO auto_ordem (perfil, modalidade, timeframe, risco_maximo,
                                    ativo, exigir_mtf)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (perfil, modalidade, timeframe) DO UPDATE
               SET risco_maximo = EXCLUDED.risco_maximo, ativo = EXCLUDED.ativo,
                   exigir_mtf = EXCLUDED.exigir_mtf
            RETURNING perfil, modalidade, timeframe, risco_maximo, ativo,
                      exigir_mtf, criado_em
            """,
            (body.perfil, body.modalidade, body.timeframe.upper(),
             body.risco_maximo, body.ativo, body.exigir_mtf),
        )
        r = cur.fetchone()
    conn.commit()
    return AutoOrdemOut(perfil=r[0], modalidade=r[1], timeframe=r[2],
                        risco_maximo=float(r[3]), ativo=r[4], exigir_mtf=r[5],
                        criado_em=r[6])


@app.delete(
    "/auto-ordem",
    response_model=AutoOrdemResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="remover_auto_ordem",
    summary="Apaga uma regra de ordem automática",
    description=(
        "Apaga DE VEZ a regra de (perfil, modalidade, timeframe). É diferente de "
        "desligar (`configurar_auto_ordem` com `ativo=false`): desligar preserva a "
        "regra e o histórico de configuração, apagar remove a linha. As ordens que "
        "a regra já gerou CONTINUAM na base — vêm do sinal, não da regra — então "
        "apagar a regra não apaga o que ela mediu.\n\n"
        "Devolve a lista de regras restantes, como `listar_auto_ordem` com "
        "`incluir_inativas=true`."
    ),
)
def delete_auto_ordem(
    perfil: str = Query(..., description="Perfil da regra."),
    modalidade: str = Query(..., description="Modalidade da regra."),
    timeframe: str = Query(..., description="Timeframe da regra."),
    conn: Connection = Depends(get_conn),
) -> AutoOrdemResponse:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM auto_ordem "
            "WHERE perfil = %s AND modalidade = %s AND timeframe = %s",
            (perfil, modalidade, timeframe.upper()),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail=f"Nenhuma regra ({perfil}, {modalidade}, {timeframe}) encontrada.",
            )
        cur.execute(
            "SELECT perfil, modalidade, timeframe, risco_maximo, ativo, "
            "exigir_mtf, criado_em "
            "FROM auto_ordem ORDER BY ativo DESC, criado_em DESC"
        )
        rows = cur.fetchall()
    conn.commit()
    return AutoOrdemResponse(regras=[
        AutoOrdemOut(perfil=r[0], modalidade=r[1], timeframe=r[2],
                     risco_maximo=float(r[3]), ativo=r[4], exigir_mtf=r[5],
                     criado_em=r[6])
        for r in rows
    ])


_ORDEM_CAMPOS_ORDEM = (
    "id", "signal_id", "symbol", "direcao", "volume", "risco_maximo", "preco_pedido",
    "preco_executado", "stop", "alvo", "conta", "servidor", "tipo_conta", "ticket",
    "status", "retcode", "mensagem", "criado_em", "enviado_em", "teste",
    "fechado_em", "preco_saida", "volume_saida", "resultado_reais", "motivo_saida",
    "conciliado_em", "desvio_entrada_r", "stop_atual", "stop_movido_em",
)
# Vêm do SINAL, por join. É o que diz QUAL REGRA produziu a ordem: as regras
# de `auto_ordem` têm chave (perfil, modalidade, timeframe), e nenhum desses
# três está na tabela `ordens`. Sem eles, "qual das minhas regras está dando
# dinheiro?" não tem resposta possível do lado do cliente.
_ORDEM_CAMPOS_SINAL = ("perfil", "modalidade", "timeframe", "candle_time", "score",
                       "resultado", "r_realizado")
_ORDEM_CAMPOS = _ORDEM_CAMPOS_ORDEM + _ORDEM_CAMPOS_SINAL

# Nomes em vez de índices: com 32 colunas, um `r[26]` a mais ou a menos passa
# em revisão e troca dois campos de lugar em silêncio.
_ORDEM_COLUNAS = ", ".join(
    [f"o.{c}" for c in _ORDEM_CAMPOS_ORDEM] + [f"s.{c}" for c in _ORDEM_CAMPOS_SINAL]
)
_ORDEM_FROM = "FROM ordens o LEFT JOIN signals s ON s.id = o.signal_id"

# NUMERIC chega como Decimal, que o Pydantic aceita mas o JSON serializa como
# string em alguns caminhos.
_ORDEM_DECIMAIS = ("volume", "risco_maximo", "volume_saida", "resultado_reais")


def _ordem_from_row(r: tuple) -> OrdemOut:
    d = dict(zip(_ORDEM_CAMPOS, r, strict=True))
    for campo in _ORDEM_DECIMAIS:
        if d[campo] is not None:
            d[campo] = float(d[campo])

    # Risco EFETIVO: o dinheiro que ficou de fato exposto, depois de a
    # quantidade ser arredondada ao lote do papel. Diverge do `risco_maximo`
    # que a regra pediu, e é essa diferença que se quer enxergar.
    volume, preco, stop = d["volume"], d["preco_executado"], d["stop"]
    if volume and preco is not None and stop is not None and abs(preco - stop) > 0:
        d["risco_efetivo"] = volume * abs(preco - stop)
        if d["resultado_reais"] is not None:
            d["resultado_r"] = d["resultado_reais"] / d["risco_efetivo"]
    return OrdemOut(**d)


def _uma_ordem(cur, signal_id: int) -> tuple | None:
    """Relê a linha já com o join. As rotas de escrita usam RETURNING, que não
    enxerga o join — reler é o que mantém uma forma só de ordem na API."""
    cur.execute(f"SELECT {_ORDEM_COLUNAS} {_ORDEM_FROM} WHERE o.signal_id = %s", (signal_id,))
    return cur.fetchone()


@app.post("/ordens", response_model=OrdemReserva,
          dependencies=[Depends(require_api_key)], include_in_schema=False)
def reservar_ordem(body: OrdemIn, conn: Connection = Depends(get_conn)) -> OrdemReserva:
    """RESERVA o sinal antes de a ordem sair. Ver o comentário do bloco acima.

    `duplicado=True` significa "esse sinal já tem ordem" — para o executor é
    ordem de PARAR, não erro. É o que impede posição dobrada depois de um
    reinício no meio do envio."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO ordens (signal_id, symbol, direcao, risco_maximo, stop, alvo,
                                status, teste)
            VALUES (%s, %s, %s, %s, %s, %s, 'ENVIANDO', %s)
            ON CONFLICT (signal_id) DO NOTHING
            RETURNING signal_id
            """,
            (body.signal_id, body.symbol.upper(), body.direcao,
             body.risco_maximo, body.stop, body.alvo, body.teste),
        )
        duplicado = cur.fetchone() is None
        row = _uma_ordem(cur, body.signal_id)
    conn.commit()
    return OrdemReserva(ordem=_ordem_from_row(row), duplicado=duplicado)


@app.put("/ordens/{signal_id}", response_model=OrdemOut,
         dependencies=[Depends(require_api_key)], include_in_schema=False)
def registrar_resultado_ordem(
    signal_id: int, body: OrdemResultado, conn: Connection = Depends(get_conn)
) -> OrdemOut:
    """Fecha a reserva com o que a corretora respondeu."""
    if body.status not in ("ENVIADA", "FALHOU", "RECUSADA"):
        raise HTTPException(
            status_code=400,
            detail="status tem que ser ENVIADA, FALHOU ou RECUSADA.",
        )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE ordens SET status = %s, volume = %s, preco_pedido = %s,
                   preco_executado = %s, conta = %s, servidor = %s, tipo_conta = %s,
                   ticket = %s, retcode = %s, mensagem = %s, enviado_em = now(),
                   -- COALESCE, e não atribuição direta, porque estes dois vêm
                   -- da RESERVA e não da resposta da corretora: o caminho
                   -- RECUSADA/FALHOU manda só status e mensagem, e sobrescrever
                   -- apagaria o alvo que a reserva já tinha gravado.
                   alvo = COALESCE(%s, alvo),
                   desvio_entrada_r = COALESCE(%s, desvio_entrada_r)
             WHERE signal_id = %s
            RETURNING signal_id
            """,
            (body.status, body.volume, body.preco_pedido, body.preco_executado,
             body.conta, body.servidor, body.tipo_conta, body.ticket,
             body.retcode, body.mensagem, body.alvo, body.desvio_entrada_r,
             signal_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=404, detail=f"Sem ordem reservada para o sinal {signal_id}.")
        row = _uma_ordem(cur, signal_id)
    conn.commit()
    return _ordem_from_row(row)


@app.put("/ordens/{signal_id}/fechamento", response_model=OrdemOut,
         dependencies=[Depends(require_api_key)], include_in_schema=False)
def registrar_fechamento_ordem(
    signal_id: int, body: OrdemFechamento, conn: Connection = Depends(get_conn)
) -> OrdemOut:
    """Grava o desfecho lido do MetaTrader 5 pela reconciliação do executor.

    Oculta do schema pela mesma razão das outras escritas de ordem, esticada
    um passo: quem escreve o resultado financeiro pode fabricar a medição. O
    agente lê o desempenho; quem o produz é a corretora, por intermédio do
    executor.

    Chamada REPETIDAMENTE enquanto a posição está aberta, com `fechado_em`
    nulo e o resultado não realizado do momento — é isso que faz a tela
    mostrar "3 abertas, +R$ 48 no papel". A gravação é idempotente por
    construção: sobrescreve os mesmos campos.

    `stop_atual` é a exceção: entra por COALESCE, e não por sobrescrita. A
    passada em que a posição aparece FECHADA não tem mais stop vivo pra ler, e
    sobrescrever com nulo apagaria justamente o nível em que ela morreu — o
    único dado que depois distingue uma saída protegida de um -1,00R.
    `stop_movido_em` só anda quando o valor muda de fato; no Postgres todo
    `SET` enxerga a linha ANTIGA, então os dois se leem coerentes."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ordens SET resultado_reais = %s, fechado_em = %s, preco_saida = %s,
                   volume_saida = %s, motivo_saida = %s, conciliado_em = now(),
                   -- Os casts não são decoração: num parâmetro solto dentro de
                   -- `IS NOT NULL` o Postgres não tem de onde inferir o tipo e
                   -- recusa a consulta inteira ("could not determine data type
                   -- of parameter") — o que só aparece quando o valor chega
                   -- NULO, que é justamente toda passada de posição fechada. No
                   -- COALESCE ele infere da coluna.
                   -- (E nada de escrever um marcador de parâmetro aqui dentro:
                   --  o psycopg conta os que estão em comentário também.)
                   stop_movido_em = CASE
                       WHEN %s::double precision IS NOT NULL
                        AND %s::double precision IS DISTINCT FROM stop_atual
                       THEN now() ELSE stop_movido_em END,
                   stop_atual = COALESCE(%s, stop_atual)
             WHERE signal_id = %s
            RETURNING signal_id
            """,
            (body.resultado_reais, body.fechado_em, body.preco_saida,
             body.volume_saida, body.motivo_saida,
             body.stop_atual, body.stop_atual, body.stop_atual, signal_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=404, detail=f"Sem ordem reservada para o sinal {signal_id}.")
        row = _uma_ordem(cur, signal_id)
    conn.commit()
    return _ordem_from_row(row)


@app.delete("/ordens", response_model=OrdensLimpeza,
            dependencies=[Depends(require_api_key)], include_in_schema=False)
def limpar_ordens(
    confirmar: bool = Query(
        False, description="Obrigatório `true`. Sem isso a rota recusa e não apaga nada."),
    incluir_abertas: bool = Query(
        False, description=(
            "Apaga também as posições ainda abertas na corretora. Por padrão elas "
            "ficam — ver o docstring.")),
    conn: Connection = Depends(get_conn),
) -> OrdensLimpeza:
    """RESET de desenvolvimento: apaga a tabela `ordens` inteira.

    Abre exceção — deliberada e de fase — à regra escrita em `schema.sql`
    (bloco da coluna `ordens.teste`): este repositório ROTULA o que não deve
    entrar na conta em vez de apagar, porque apagar de uma tabela de
    auditoria some com um evento que aconteceu. A exceção é que zerar a base
    inteira num ciclo de ajuste não é esconder uma ordem, é começar a medir
    de novo — e enquanto o histórico mistura ordens de teste com regras já
    descartadas, `desempenho_ordens` não responde "esta regra, do jeito que
    ela está agora, dá dinheiro?". O `teste` continua sendo a resposta certa
    pra ordem de validação avulsa; isto aqui não substitui aquilo.

    Três propriedades que sustentam a exceção:

    - **`confirmar` é obrigatório.** Um `DELETE /ordens` sem querer — cliente
      com bug, curl na URL errada — não pode zerar a base. A confirmação
      viaja no parâmetro, não só no navegador.
    - **Posição ABERTA fica**, salvo `incluir_abertas=true`. A conciliação do
      executor descobre o que fechar por `GET /ordens?aberta=true`; apagar a
      linha de uma posição viva faz o desfecho dela nunca ser lido, e a
      posição segue aberta no MT5 sem ninguém olhando.
    - **`signals` não é tocado.** A cascata da FK desce de sinal pra ordem, e
      não o contrário. Some o que a corretora pagou, fica o que o motor
      previu — `assertividade_sinais` sobrevive intacta, e é justamente isso
      que torna a limpeza aceitável.

    Fora do schema pelo motivo mais forte do bloco de comentário acima: uma
    tool de apagar auditoria na mão de um texto gerado é pior que uma de
    mandar ordem."""
    if not confirmar:
        raise HTTPException(
            status_code=400,
            detail=("Limpeza recusada: passe `confirmar=true`. Esta rota apaga TODAS "
                    "as ordens, sem filtro."),
        )

    # Mesmo recorte do índice parcial `ordens_abertas_idx`.
    aberta = "status = 'ENVIADA' AND fechado_em IS NULL"
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM ordens WHERE {aberta}")
        abertas = cur.fetchone()[0]
        cur.execute(
            "DELETE FROM ordens" if incluir_abertas
            else f"DELETE FROM ordens WHERE NOT ({aberta})"
        )
        apagadas = cur.rowcount
        cur.execute("SELECT count(*) FROM ordens")
        restantes = cur.fetchone()[0]
    conn.commit()
    log.warning("Ordens apagadas: %s (abertas preservadas: %s, restantes: %s).",
                apagadas, 0 if incluir_abertas else abertas, restantes)
    return OrdensLimpeza(
        apagadas=apagadas,
        abertas_preservadas=0 if incluir_abertas else abertas,
        restantes=restantes,
    )


# `/ordens/stats` precisa ser declarada ANTES de qualquer `GET /ordens/{…}`
# que venha a existir, senão o `{signal_id}` engole "stats". Hoje não há
# conflito (só há PUT nesse caminho) — o cuidado é pro dia em que houver,
# mesmo arranjo de `/signals/stats` com `/signals/feedback`.
_ORDEM_STATS_BASE = """
WITH base AS (
    SELECT o.tipo_conta, o.symbol, o.direcao, o.motivo_saida, o.fechado_em,
           o.resultado_reais, o.status,
           coalesce(s.perfil, '?') || ' · ' || coalesce(s.modalidade, '?')
               || ' · ' || coalesce(s.timeframe, '?')            AS regra,
           -- Mesmo risco efetivo do `_ordem_from_row`: o exposto DE FATO,
           -- depois do arredondamento ao lote. É o denominador que põe
           -- ativos de preços diferentes na mesma escala.
           CASE WHEN o.volume IS NOT NULL AND o.preco_executado IS NOT NULL
                     AND o.stop IS NOT NULL AND abs(o.preco_executado - o.stop) > 0
                THEN o.volume * abs(o.preco_executado - o.stop)
           END                                                   AS risco_efetivo,
           -- O R/R com que a ordem REALMENTE saiu: alvo e stop medidos contra
           -- o preço executado, não contra a entrada modelada do sinal. Foi a
           -- variável que explicou o prejuízo das 39 primeiras ordens (mediana
           -- 1,00 mas faixa de 0,02 a 7,00, contra um projeto de 0,8 a 1,2) e
           -- não existia em consulta nenhuma — foi preciso cruzar `ordens` com
           -- `signals` à mão para vê-la.
           CASE WHEN o.preco_executado IS NOT NULL AND o.stop IS NOT NULL
                     AND o.alvo IS NOT NULL
                     AND abs(o.preco_executado - o.stop) > 0
                THEN abs(o.alvo - o.preco_executado)
                     / abs(o.preco_executado - o.stop)
           END                                                   AS rr_envio,
           o.desvio_entrada_r,
           -- Data/hora do ENVIO no fuso de Brasília, e não em UTC. O pregão
           -- inteiro (10h-18h) cai no mesmo dia UTC, então hoje as duas
           -- convenções empatam — o que não se pode é depender disso: um
           -- leilão que atravesse as 21h UTC jogaria a ordem no dia seguinte
           -- em todo relatório diário, silenciosamente. Mesmo cuidado do
           -- `_fetch_ohlcv_mt5` e do `execucao.hora_do_mt5`.
           o.criado_em AT TIME ZONE %(tz)s                       AS enviada_local
      FROM ordens o
      LEFT JOIN signals s ON s.id = o.signal_id
     WHERE o.criado_em > now() - make_interval(days => %(dias)s)
       AND (%(symbol)s::text     IS NULL OR o.symbol     = %(symbol)s)
       AND (%(tipo_conta)s::text IS NULL OR o.tipo_conta = %(tipo_conta)s)
       AND (%(perfil)s::text     IS NULL OR s.perfil     = %(perfil)s)
       AND (%(modalidade)s::text IS NULL OR s.modalidade = %(modalidade)s)
       AND (%(timeframe)s::text  IS NULL OR s.timeframe  = %(timeframe)s)
       -- Ordem de validação fica FORA por padrão. Ela saiu de verdade, mas
       -- não mede regra nenhuma — quem a disparou foi uma pessoa conferindo
       -- o encanamento, não o filtro da regra.
       AND (%(incluir_testes)s::bool OR NOT o.teste)
),
-- Só ENVIADA entra nas taxas: RECUSADA e FALHOU não chegaram ao mercado e
-- não têm desempenho nenhum pra medir. Elas voltam contadas à parte.
enviadas AS (
    SELECT *,
           (fechado_em IS NOT NULL)                      AS fechada,
           -- NULL pras abertas E pras zeradas: `avg()` pula NULL, então o
           -- denominador da taxa vira ganhos+perdas. Uma posição que ainda
           -- pode virar prejuízo não pode contar como acerto.
           CASE WHEN fechado_em IS NOT NULL AND resultado_reais > 0 THEN 1
                WHEN fechado_em IS NOT NULL AND resultado_reais < 0 THEN 0
           END                                           AS acerto,
           CASE WHEN fechado_em IS NOT NULL AND risco_efetivo > 0
                THEN resultado_reais / risco_efetivo
           END                                           AS r
      FROM base WHERE status = 'ENVIADA'
)
"""

_ORDEM_STATS_SELECT = """
SELECT {recorte}                                                     AS recorte,
       count(*)                                                      AS n,
       count(*) FILTER (WHERE fechada)                               AS fechadas,
       count(*) FILTER (WHERE NOT fechada)                           AS abertas,
       count(*) FILTER (WHERE fechada AND resultado_reais > 0)       AS ganhos,
       count(*) FILTER (WHERE fechada AND resultado_reais < 0)       AS perdas,
       count(*) FILTER (WHERE fechada AND resultado_reais = 0)       AS zeradas,
       avg(acerto)::float                                            AS taxa_acerto,
       coalesce(sum(resultado_reais) FILTER (WHERE fechada), 0)::float
                                                                     AS resultado_reais,
       avg(r)::float                                                 AS resultado_r_medio,
       coalesce(sum(resultado_reais) FILTER (WHERE NOT fechada), 0)::float
                                                                     AS aberto_reais
FROM enviadas GROUP BY 1 ORDER BY 1
"""


# Faixas de execução. Os cortes não são redondos por estética: 0,7-1,3 é a
# vizinhança dos R/R que os perfis pedem (0,8 a 1,2), e o que cai fora dela
# saiu com uma geometria que ninguém escolheu.
_FAIXA_RR_ENVIO = """
    CASE WHEN rr_envio IS NULL   THEN 'sem dados'
         WHEN rr_envio <  0.5    THEN 'a) < 0,5'
         WHEN rr_envio <  0.7    THEN 'b) 0,5-0,7'
         WHEN rr_envio <= 1.3    THEN 'c) 0,7-1,3 (contratado)'
         WHEN rr_envio <= 2.0    THEN 'd) 1,3-2,0'
         ELSE                         'e) > 2,0'
    END
"""

# Os cortes TEMPORAIS agrupam pelo dia/hora do ENVIO, nunca do fechamento.
# Duas razões: a janela do recorte já filtra por `criado_em` (agrupar por
# outra coluna faria a soma dos dias não bater com o total), e uma posição
# aberta não tem data de fechamento nenhuma — ela sumiria do dia em que foi
# mandada. A consequência a documentar é que a posição que atravessa a
# meia-noite é creditada ao dia em que SAIU, não ao dia em que fechou; em day
# trade os dois coincidem, e misturar as duas convenções numa mesma tabela
# seria pior que escolher uma.
_DIA_ENVIO = "enviada_local::date"
_HORA_ENVIO = "to_char(enviada_local, 'HH24') || 'h'"
# Prefixo numérico porque o `ORDER BY 1` do SELECT é alfabético: sem ele a
# semana sai em ordem de dicionário (dom, qua, qui, sáb, seg…).
_DIA_SEMANA_ENVIO = """
    CASE extract(isodow FROM enviada_local)
         WHEN 1 THEN '1 seg' WHEN 2 THEN '2 ter' WHEN 3 THEN '3 qua'
         WHEN 4 THEN '4 qui' WHEN 5 THEN '5 sex' WHEN 6 THEN '6 sáb'
         ELSE '7 dom'
    END
"""

# O desvio é assinado: positivo = preencheu PIOR (mais perto do alvo, mais
# longe do stop). As duas caudas matam de jeitos opostos, então elas NÃO
# podem cair no mesmo balde — foi exatamente por olhar o módulo que o defeito
# passou despercebido: a média do desvio é ~0,00R, e mesmo assim 11 das 39
# ordens andaram mais de 0,5R.
_FAIXA_DESVIO_ENTRADA = """
    CASE WHEN desvio_entrada_r IS NULL  THEN 'sem dados'
         WHEN desvio_entrada_r < -0.5   THEN 'a) < -0,5R (stop apertou)'
         WHEN desvio_entrada_r < -0.2   THEN 'b) -0,5 a -0,2R'
         WHEN desvio_entrada_r <= 0.2   THEN 'c) -0,2 a +0,2R (fiel)'
         WHEN desvio_entrada_r <= 0.5   THEN 'd) +0,2 a +0,5R'
         ELSE                                'e) > +0,5R (alvo encolheu)'
    END
"""


def _ordem_stats_rows(cur, recorte: str, params: dict) -> list[OrdemStatsRow]:
    """`params` são os filtros MAIS o `tz` dos cortes temporais — não é o
    dicionário que volta na resposta."""
    cur.execute(_ORDEM_STATS_BASE + _ORDEM_STATS_SELECT.format(recorte=recorte), params)
    return [
        OrdemStatsRow(
            recorte=str(row[0]), n=row[1], fechadas=row[2], abertas=row[3],
            ganhos=row[4], perdas=row[5], zeradas=row[6], taxa_acerto=row[7],
            resultado_reais=row[8], resultado_r_medio=row[9], aberto_reais=row[10],
        )
        for row in cur.fetchall()
    ]


@app.get(
    "/ordens/stats",
    response_model=OrdemStatsResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="desempenho_ordens",
    summary="Desempenho financeiro das ordens enviadas",
    description=(
        "Quanto as ordens realmente renderam, quebrado por regra (perfil · "
        "modalidade · timeframe), ativo, motivo de saída, direção e conta. É a "
        "tool pra 'quais das minhas regras de ordem automática estão dando "
        "dinheiro?'.\n\n"
        "O resultado NÃO é modelado: vem do MetaTrader 5, líquido de corretagem "
        "e swap, então já inclui slippage, fechamento parcial e fechamento "
        "manual. Não confunda com `assertividade_sinais`, que mede o MOTOR sobre "
        "candles, com entrada modelada — as duas divergem, e a diferença entre "
        "elas é justamente o custo de executar.\n\n"
        "Como ler sem mentir:\n"
        "- **Nunca some DEMO com REAL, e sempre cite `tipo_conta`**: resultado em "
        "conta demo não é dinheiro. Use `por_conta` quando houver as duas.\n"
        "- `taxa_acerto` tem como denominador `ganhos + perdas`, nunca `n`: as "
        "abertas ainda podem virar prejuízo. Cite os dois: '4 de 7 fechadas'.\n"
        "- `resultado_reais` soma só as FECHADAS. O não realizado das abertas vem "
        "à parte em `aberto_reais`, e somar os dois é anunciar lucro que ainda "
        "pode sumir.\n"
        "- Com menos de ~10 fechadas o percentual não significa nada; diga que a "
        "amostra é insuficiente em vez de citar o número.\n"
        "- `motivo_saida='MANUAL'` é posição fechada à mão: aquela regra não foi "
        "medida, foi pilotada, e não sustenta conclusão sobre a regra.\n"
        "- `recusadas` e `falhadas` ficam fora das taxas (nada delas chegou ao "
        "mercado), mas um número alto ali é problema de configuração, não de "
        "estratégia — vale mencionar.\n"
        "- `por_rr_envio` e `por_desvio_entrada` medem a EXECUÇÃO, não a "
        "estratégia: dizem se a ordem saiu com a geometria que a regra pediu. "
        "Volume fora da faixa 'contratado' ou longe de 'fiel' indica que o "
        "prejuízo é de execução e não do motor — nesse caso não culpe a regra.\n"
        "- `por_dia` vem em ordem cronológica e é o recorte pra 'como foi a "
        "semana?' ou 'que dia estragou o mês?'. É também o único que mostra "
        "TRAJETÓRIA: some `resultado_reais` em ordem pra ter a curva de capital, "
        "porque um mesmo saldo pode ser uma escada subindo ou um lucro antigo "
        "sendo devolvido dia após dia. `por_hora` e `por_dia_semana` são o mesmo "
        "corte agregado — úteis pra 'a primeira hora do pregão me custa "
        "dinheiro?'.\n"
        "- Os três agrupam pelo dia/hora do ENVIO (fuso de Brasília), não do "
        "fechamento: a posição que atravessa a meia-noite conta no dia em que "
        "saiu. Ao citar uma hora, diga que é a do envio."
    ),
)
def get_ordem_stats(
    symbol: str | None = Query(None),
    perfil: str | None = Query(None),
    modalidade: str | None = Query(None),
    timeframe: str | None = Query(None),
    tipo_conta: str | None = Query(None, description="DEMO ou REAL."),
    incluir_testes: bool = Query(
        False,
        description=(
            "Inclui as ordens de validação (`teste=true`), que ficam de fora por "
            "padrão porque não medem regra nenhuma."
        ),
    ),
    dias: int = Query(90, ge=1, le=3650),
    conn: Connection = Depends(get_conn),
) -> OrdemStatsResponse:
    filtros = {
        "symbol": symbol.strip().upper() if symbol else None,
        "perfil": perfil,
        "modalidade": modalidade,
        "timeframe": timeframe.strip().upper() if timeframe else None,
        "tipo_conta": tipo_conta.strip().upper() if tipo_conta else None,
        "incluir_testes": incluir_testes,
        "dias": dias,
    }
    # O fuso NÃO entra em `filtros`: aquele dicionário volta na resposta como
    # o eco do que foi pedido, e o fuso não é um recorte — é a convenção com
    # que os cortes temporais são agrupados.
    params = {**filtros, "tz": LOCAL_TZ}
    with conn.cursor() as cur:
        geral = _ordem_stats_rows(cur, "'geral'", params)
        por_conta = _ordem_stats_rows(cur, "coalesce(tipo_conta, '?')", params)
        por_regra = _ordem_stats_rows(cur, "regra", params)
        por_symbol = _ordem_stats_rows(cur, "symbol", params)
        # Rótulo em vez de NULL: "ainda aberta" é um grupo legítimo, e um
        # rótulo nulo viraria linha em branco na tabela.
        por_motivo = _ordem_stats_rows(cur, "coalesce(motivo_saida, 'EM ABERTO')", params)
        por_direcao = _ordem_stats_rows(cur, "direcao", params)
        # Os dois recortes da EXECUÇÃO, ao lado dos da estratégia. Eles não
        # respondem "qual regra presta", e sim "a ordem saiu com a geometria
        # que a regra pediu?" — pergunta que ficou sem resposta possível até
        # 2026-08-12 e que, quando finalmente foi feita à mão, encontrou 40%
        # do prejuízo concentrado numa única faixa.
        por_rr_envio = _ordem_stats_rows(cur, _FAIXA_RR_ENVIO, params)
        por_desvio = _ordem_stats_rows(cur, _FAIXA_DESVIO_ENTRADA, params)
        # Os cortes TEMPORAIS. `por_dia` é o que sustenta a curva de capital
        # da tela — a soma corrida de `resultado_reais` em ordem de data —, e
        # é a pergunta que nenhum dos outros recortes responde: "estou
        # ganhando ou perdendo AO LONGO do tempo?". Uma taxa de acerto igual
        # pode ser um platô ou uma escada descendo, e só a série mostra qual.
        por_dia = _ordem_stats_rows(cur, _DIA_ENVIO, params)
        por_hora = _ordem_stats_rows(cur, _HORA_ENVIO, params)
        por_dia_semana = _ordem_stats_rows(cur, _DIA_SEMANA_ENVIO, params)

        cur.execute(
            _ORDEM_STATS_BASE
            + "SELECT count(*) FILTER (WHERE status = 'RECUSADA'), "
              "count(*) FILTER (WHERE status = 'FALHOU') FROM base",
            params,
        )
        recusadas, falhadas = cur.fetchone()

    return OrdemStatsResponse(
        filtros=filtros, total=sum(linha.n for linha in geral),
        recusadas=recusadas, falhadas=falhadas,
        geral=geral, por_conta=por_conta, por_regra=por_regra,
        por_symbol=por_symbol, por_motivo_saida=por_motivo, por_direcao=por_direcao,
        por_rr_envio=por_rr_envio, por_desvio_entrada=por_desvio,
        por_dia=por_dia, por_hora=por_hora, por_dia_semana=por_dia_semana,
    )


@app.get(
    "/ordens",
    response_model=OrdensResponse,
    dependencies=[Depends(require_api_key)],
    operation_id="listar_ordens",
    summary="Ordens enviadas pelo executor",
    description=(
        "O que foi efetivamente enviado à corretora, do mais recente pro mais "
        "antigo, com o ticket, o preço executado e o desfecho da posição. "
        "`status`: ENVIANDO (reservada, ainda sem resposta), ENVIADA, FALHOU (a "
        "corretora recusou) ou RECUSADA (as travas locais barraram antes de "
        "sair).\n\n"
        "Cada ordem já vem com a REGRA que a produziu (`perfil`, `modalidade`, "
        "`timeframe`) — é por ela que se compara uma regra com outra.\n\n"
        "O desfecho vem do MetaTrader 5: `resultado_reais` é o lucro em reais "
        "líquido de corretagem, `motivo_saida` diz se saiu no STOP, no ALVO ou "
        "à mão (MANUAL), e `resultado_r` põe isso em múltiplos do risco. "
        "⚠️ Enquanto `fechado_em` for nulo a posição está ABERTA e "
        "`resultado_reais` é o não realizado do momento — não é dinheiro ainda. "
        "Use `aberta=true` pra ver só as em curso.\n\n"
        "Não confunda `resultado` (desfecho do SINAL, calculado sobre candles) "
        "com `resultado_reais` (o que a corretora pagou): a diferença entre os "
        "dois é o custo de executar.\n\n"
        "`tipo_conta` diz se foi DEMO ou REAL — cite sempre, porque um resultado "
        "em conta demo não é dinheiro."
    ),
)
def listar_ordens(
    symbol: str | None = Query(None),
    status: str | None = Query(None),
    perfil: str | None = Query(None),
    modalidade: str | None = Query(None),
    timeframe: str | None = Query(None),
    tipo_conta: str | None = Query(None, description="DEMO ou REAL."),
    aberta: bool | None = Query(
        None,
        description=(
            "true = posições em curso (enviadas e ainda sem fechamento); "
            "false = só as já fechadas. Omitido, traz as duas."
        ),
    ),
    incluir_testes: bool = Query(
        False,
        description=(
            "Inclui as ordens de validação (`teste=true`), que ficam de fora por "
            "padrão. Elas saíram de verdade, mas foram disparadas à mão pra "
            "conferir o encanamento — não medem regra nenhuma."
        ),
    ),
    dias: int = Query(30, ge=1, le=3650),
    limite: int = Query(200, ge=1, le=2000),
    conn: Connection = Depends(get_conn),
) -> OrdensResponse:
    filtros = {
        "symbol": symbol.strip().upper() if symbol else None,
        "status": status.strip().upper() if status else None,
        "perfil": perfil,
        "modalidade": modalidade,
        "timeframe": timeframe.strip().upper() if timeframe else None,
        "tipo_conta": tipo_conta.strip().upper() if tipo_conta else None,
        "aberta": aberta,
        "incluir_testes": incluir_testes,
        "dias": dias, "limite": limite,
    }
    where = """
        WHERE o.criado_em > now() - make_interval(days => %(dias)s)
          AND (%(symbol)s::text     IS NULL OR o.symbol     = %(symbol)s)
          AND (%(status)s::text     IS NULL OR o.status     = %(status)s)
          AND (%(tipo_conta)s::text IS NULL OR o.tipo_conta = %(tipo_conta)s)
          AND (%(perfil)s::text     IS NULL OR s.perfil     = %(perfil)s)
          AND (%(modalidade)s::text IS NULL OR s.modalidade = %(modalidade)s)
          AND (%(timeframe)s::text  IS NULL OR s.timeframe  = %(timeframe)s)
          -- "aberta" é ENVIADA e ainda sem fechamento. Cobra o status de
          -- propósito: uma RECUSADA também tem `fechado_em` nulo, e nunca
          -- esteve aberta coisa nenhuma.
          AND (%(aberta)s::bool IS NULL
               OR (%(aberta)s::bool AND o.status = 'ENVIADA' AND o.fechado_em IS NULL)
               OR (NOT %(aberta)s::bool AND o.fechado_em IS NOT NULL))
          -- Ordem de validação fica FORA por padrão, mesma regra do /stats.
          AND (%(incluir_testes)s::bool OR NOT o.teste)
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) {_ORDEM_FROM} {where}", filtros)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {_ORDEM_COLUNAS} {_ORDEM_FROM} {where} "
            "ORDER BY o.criado_em DESC LIMIT %(limite)s",
            filtros,
        )
        rows = cur.fetchall()
    return OrdensResponse(ordens=[_ordem_from_row(r) for r in rows], total=total)
