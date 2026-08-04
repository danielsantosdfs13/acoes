"""
backend/processor.py

Caminho de ESCRITA do pipeline: gateway HTTP entre o `acoes-scraper` (VM
Windows com o MT5 aberto) e o TimescaleDB compartilhado do homelab.

  - POST /candles    lote de velas, upsert idempotente
  - GET  /watchlist  símbolos ativos, pro scraper saber o que coletar
  - GET  /health     liveness

O scraper nunca fala direto com o Postgres — só conhece este serviço, então
a VM Windows nunca guarda credencial de banco.

`GET /watchlist` também existe em `api.py`. A repetição é deliberada: é uma
função de rota, e a alternativa seria dar dois hostnames e duas entradas de
`hosts` para a VM. Cada serviço fica autossuficiente pro seu cliente.

Deploy: `uvicorn processor:app`, a partir da mesma imagem `acoes-backend`
que roda `api.py` e `migrate.py`.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from psycopg import Connection

from auth import require_api_key
from db import get_conn
from models import CandleBatchIn, IngestResult, WatchlistResponse

app = FastAPI(title="Ações — Processor (escrita)")


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
