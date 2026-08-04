"""Modelos Pydantic do processor — validação do payload de ingest e das respostas."""

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


class WatchlistResponse(BaseModel):
    symbols: list[str]


class WatchlistAdd(BaseModel):
    symbol: str


class SymbolStatus(BaseModel):
    symbol: str
    timeframe: str
    last_candle_time: datetime
    last_ingested_at: datetime
