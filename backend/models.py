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
    origem: str                # 'worker' | 'manual'
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


class StatsResponse(BaseModel):
    filtros: dict
    total: int
    geral: list[StatsRow]
    por_timeframe: list[StatsRow]
    por_symbol: list[StatsRow]
    por_direcao: list[StatsRow]
    por_faixa_score: list[StatsRow]
    por_mtf: list[StatsRow]
