"""Modelos Pydantic compartilhados pelos dois entrypoints do backend
(`processor.py`, de escrita, e `api.py`, de leitura)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CandleIn(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleBatchIn(BaseModel):
    symbol: str
    timeframe: str
    source: str = "MetaTrader 5"
    candles: list[CandleIn]


class IngestResult(BaseModel):
    symbol: str
    timeframe: str
    upserted: int


class CandleOut(BaseModel):
    """Uma vela na resposta de GET /candles. Os nomes de campo batem com as
    colunas que `daytrade_smc._fetch_ohlcv_api` monta no DataFrame."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandlesResponse(BaseModel):
    symbol: str
    timeframe: str
    candles: list[CandleOut]


class WatchlistResponse(BaseModel):
    symbols: list[str]


class WatchlistAdd(BaseModel):
    symbol: str


class WatchlistReplace(BaseModel):
    """Corpo do PUT /watchlist: a lista COMPLETA de símbolos ativos.

    Existe porque `daytrade_smc.save_symbols` tem semântica de sobrescrever
    tudo de uma vez — um POST por símbolo somado a um DELETE por símbolo
    removido não expressa isso atomicamente."""

    symbols: list[str]


class SymbolStatus(BaseModel):
    symbol: str
    timeframe: str
    last_candle_time: datetime
    last_ingested_at: datetime


class ProfileIn(BaseModel):
    """Corpo do PUT /profiles/{nome}.

    `params` carrega SÓ os campos diferentes do default do motor — é o que
    faz um perfil salvo hoje continuar carregando depois de um parâmetro
    novo entrar em `AnalysisParams`. Um `{}` significa "todos os defaults"."""

    params: dict = {}
    params_hash: str = ""
    descricao: str = ""


class ProfileOut(BaseModel):
    nome: str
    params: dict
    params_hash: str
    descricao: str
    ativo: bool
    criado_em: datetime
    alterado_em: datetime


class ProfilesResponse(BaseModel):
    profiles: list[ProfileOut]


class SignalIn(BaseModel):
    """Um sinal gerado, pronto pra gravar.

    O corpo é montado por `daytrade_smc.signal_payload`, e SÓ por ela: o
    worker e o botão "salvar sinal" do Streamlit usam a mesma função de
    propósito, senão as duas origens gravariam linhas com formatos que
    divergiriam na primeira mudança do motor."""

    symbol: str
    timeframe: str
    modalidade: str
    candle_time: datetime      # UTC, abertura da vela FECHADA analisada
    perfil: str
    params_hash: str
    origem: str                # 'worker' | 'manual' | 'backfill'
    direcao: str
    score: float
    confianca: float
    setup: str
    mtf_confirmado: bool = False
    mtf_direcao: str = "NEUTRO"
    entrada: float | None = None
    stop: float | None = None
    alvo_1: float | None = None
    alvo_2: float | None = None
    r_alvo_1: float | None = None
    r_alvo_2: float | None = None
    stop_basis: str = ""
    detalhes: dict = {}


class SignalOut(SignalIn):
    id: int
    criado_em: datetime
    resultado: str | None = None
    resultado_detalhe: str | None = None
    candles_ate_resultado: int | None = None
    avaliado_em: datetime | None = None


class SignalSaveResult(BaseModel):
    """`duplicado` distingue "gravei agora" de "já estava lá".

    Sem isso a interface diria "salvo!" nas duas situações, e o usuário não
    teria como saber se o clique anterior tinha funcionado."""

    signal: SignalOut
    duplicado: bool


class SignalsResponse(BaseModel):
    signals: list[SignalOut]
    total: int


class StatsRow(BaseModel):
    """Uma linha de assertividade.

    ATENÇÃO ao denominador: `n` conta todos os sinais do recorte, mas
    `taxa_acerto` e `expectativa_r` só olham os RESOLVIDOS (os `EM_ABERTO`
    ainda não têm desfecho). Mostrar "66,7% de n" seria mentira — a
    interface tem que exibir `resolvidos` junto."""

    modalidade: str
    recorte: str | None = None
    n: int
    resolvidos: int
    acertos: int
    em_aberto: int
    taxa_acerto: float | None = None
    expectativa_r: float | None = None
    alvo_1: int = 0
    alvo_2: int = 0
    stop: int = 0
    # Quantos dos `resolvidos` já foram medidos pelo modelo de execução atual
    # (fill na abertura seguinte, R líquido de custo). Abaixo de `resolvidos`,
    # a expectativa está misturando dois modelos e o número infla nos gaps —
    # o conserto é `analyzer.py --reavaliar-tudo`.
    modelo_atual: int = 0


class AnaliseIn(BaseModel):
    """Corpo do POST /analisar — rodar o motor AGORA, sob demanda.

    Todos os campos além de `symbol` são opcionais e caem nos mesmos
    defaults do worker, pra que uma consulta avulsa e a varredura periódica
    descrevam a mesma coisa quando ninguém pede nada diferente."""

    symbol: str
    timeframes: list[str] | None = None   # default: os quatro varridos pelo worker
    perfil: str | None = None             # default: 'padrão'
    modalidade: str | None = None         # default: todas as cinco


class AnaliseResponse(BaseModel):
    """Resultado de uma análise sob demanda.

    `leituras` reusa `SignalIn` de propósito: é exatamente o mesmo shape que
    o worker grava, montado pela mesma `daytrade_smc.signal_payload`. Assim
    "o que o motor está vendo agora" e "o que o motor viu naquela vela" são
    comparáveis campo a campo, sem tradução no meio.

    Ao contrário da varredura do worker, aqui as leituras NEUTRO vêm junto:
    o worker não as grava porque elas não entram em estatística nenhuma, mas
    numa consulta "e a PETR4?" a resposta "está neutro nos quatro
    timeframes" é o dado que se foi buscar.

    `erros` traz, por timeframe, o motivo de não ter dado pra analisar
    (símbolo sem candle, só vela em formação). Um timeframe sem dado não
    derruba os outros — mas some da resposta em silêncio se não for dito.
    """

    symbol: str
    perfil: str
    confirmacao: list[str]        # o par de timeframes que define "confirmado"
    analisado_em: datetime
    leituras: list[SignalIn]
    erros: dict[str, str] = {}


class StatsResponse(BaseModel):
    filtros: dict
    total: int
    geral: list[StatsRow]
    por_timeframe: list[StatsRow]
    por_symbol: list[StatsRow]
    por_direcao: list[StatsRow]
    por_faixa_score: list[StatsRow]
    por_mtf: list[StatsRow]
