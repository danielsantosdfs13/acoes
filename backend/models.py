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
