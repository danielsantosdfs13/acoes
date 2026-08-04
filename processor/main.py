"""
processor/main.py

Gateway HTTP entre o coletor MT5 (VM Windows) e o Postgres/TimescaleDB do
homelab. Responsabilidades:
  - receber lotes de candles do coletor e gravar via upsert idempotente
    (POST /candles);
  - expor a watchlist ativa pro coletor descobrir o que coletar
    (GET /watchlist);
  - status de última atualização por symbol/timeframe (GET /status).

O coletor nunca fala direto com o Postgres — só conhece este serviço, então
a VM Windows nunca guarda credencial de banco. O app Streamlit, por outro
lado, lê/escreve direto no Postgres via `daytrade_smc.HOMELAB_DB_DSN` (ver
daytrade_smc.py) — os endpoints de escrita da watchlist aqui existem só
como conveniência, não são o caminho principal de escrita.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from psycopg import Connection

from db import get_conn
from models import (
    CandleBatchIn,
    IngestResult,
    SymbolStatus,
    WatchlistAdd,
    WatchlistResponse,
)

app = FastAPI(title="Day Trade SMC — Processor")

_API_KEY = os.environ.get("PROCESSOR_API_KEY") or None


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Checa X-API-Key contra PROCESSOR_API_KEY. Sem a env var, a checagem é
    pulada — a proteção real é o processor não estar exposto fora da
    LAN/tailnet do homelab; a chave é defesa em profundidade, não o
    controle principal."""
    if _API_KEY and x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="X-API-Key inválida ou ausente.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/candles", response_model=IngestResult, dependencies=[Depends(require_api_key)])
def ingest_candles(batch: CandleBatchIn, conn: Connection = Depends(get_conn)) -> IngestResult:
    upserted = 0
    with conn.cursor() as cur:
        for candle in batch.candles:
            cur.execute(
                """
                INSERT INTO candles (symbol, timeframe, time, open, high, low, close, volume, source)
                VALUES (%(symbol)s, %(timeframe)s, %(time)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(source)s)
                ON CONFLICT (symbol, timeframe, time) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                    close = EXCLUDED.close, volume = EXCLUDED.volume,
                    source = EXCLUDED.source, ingested_at = now()
                """,
                {
                    "symbol": batch.symbol,
                    "timeframe": batch.timeframe,
                    "time": candle.time,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "source": batch.source,
                },
            )
            upserted += 1
    return IngestResult(symbol=batch.symbol, timeframe=batch.timeframe, upserted=upserted)


@app.get("/watchlist", response_model=WatchlistResponse)
def get_watchlist(conn: Connection = Depends(get_conn)) -> WatchlistResponse:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        symbols = [row[0] for row in cur.fetchall()]
    return WatchlistResponse(symbols=symbols)


@app.post("/watchlist", response_model=WatchlistResponse, dependencies=[Depends(require_api_key)])
def add_to_watchlist(body: WatchlistAdd, conn: Connection = Depends(get_conn)) -> WatchlistResponse:
    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Símbolo vazio.")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist (symbol, active) VALUES (%s, true)
            ON CONFLICT (symbol) DO UPDATE SET active = true
            """,
            (symbol,),
        )
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        symbols = [row[0] for row in cur.fetchall()]
    return WatchlistResponse(symbols=symbols)


@app.delete("/watchlist/{symbol}", response_model=WatchlistResponse, dependencies=[Depends(require_api_key)])
def remove_from_watchlist(symbol: str, conn: Connection = Depends(get_conn)) -> WatchlistResponse:
    with conn.cursor() as cur:
        cur.execute("UPDATE watchlist SET active = false WHERE symbol = %s", (symbol.strip().upper(),))
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        symbols = [row[0] for row in cur.fetchall()]
    return WatchlistResponse(symbols=symbols)


@app.get("/status", response_model=list[SymbolStatus])
def get_status(conn: Connection = Depends(get_conn)) -> list[SymbolStatus]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, timeframe, MAX(time) AS last_candle_time, MAX(ingested_at) AS last_ingested_at
            FROM candles
            GROUP BY symbol, timeframe
            ORDER BY symbol, timeframe
            """
        )
        rows = cur.fetchall()
    return [
        SymbolStatus(symbol=row[0], timeframe=row[1], last_candle_time=row[2], last_ingested_at=row[3])
        for row in rows
    ]
