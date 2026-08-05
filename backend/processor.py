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


# O `WHERE` no fim do DO UPDATE é o que impede a reescrita de velas que não
# mudaram, e é a diferença entre este pipeline gravar ~14 milhões de linhas por
# dia ou algumas centenas.
#
# O scraper reenvia as últimas TRAILING_WINDOW velas a cada volta do loop (ver
# scraper/scraper.py), das quais normalmente só a em formação mudou. Sem esta
# guarda, cada repetição era um UPDATE MVCC de verdade — tupla nova, tupla
# morta, WAL e autovacuum — porque `ingested_at = now()` sempre difere. Medido
# em 2026-08-04: 160,7 escritas/s e 2,1 GB de WAL por dia para manter 40 KB/dia
# de dados novos.
#
# `source` entra na comparação junto com o OHLCV: sem ele, uma troca de fonte
# do mesmo candle seria silenciosamente ignorada.
_UPSERT_SQL = """
INSERT INTO candles (symbol, timeframe, time, open, high, low, close, volume, source)
VALUES (%(symbol)s, %(timeframe)s, %(time)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(source)s)
ON CONFLICT (symbol, timeframe, time) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, volume = EXCLUDED.volume,
    source = EXCLUDED.source, ingested_at = now()
WHERE candles.open   IS DISTINCT FROM EXCLUDED.open
   OR candles.high   IS DISTINCT FROM EXCLUDED.high
   OR candles.low    IS DISTINCT FROM EXCLUDED.low
   OR candles.close  IS DISTINCT FROM EXCLUDED.close
   OR candles.volume IS DISTINCT FROM EXCLUDED.volume
   OR candles.source IS DISTINCT FROM EXCLUDED.source
"""


@app.post("/candles", response_model=IngestResult, dependencies=[Depends(require_api_key)])
def ingest_candles(batch: CandleBatchIn, conn: Connection = Depends(get_conn)) -> IngestResult:
    """Upsert idempotente de um lote de velas de um mesmo symbol/timeframe.

    `upserted` conta as linhas que REALMENTE foram gravadas, não o tamanho do
    lote: com a guarda de `_UPSERT_SQL`, uma vela reenviada sem alteração não
    conta. Fora do pregão o normal é vir zero — é sinal de que está funcionando,
    não de falha."""
    if not batch.candles:
        return IngestResult(symbol=batch.symbol, timeframe=batch.timeframe, upserted=0)

    params = [
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
        }
        for candle in batch.candles
    ]

    # executemany do psycopg3 usa pipeline: o lote inteiro vai numa ida só, em
    # vez de uma round-trip por vela, e `rowcount` já vem somado.
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, params)
        upserted = cur.rowcount

    return IngestResult(symbol=batch.symbol, timeframe=batch.timeframe, upserted=upserted)


@app.get("/watchlist", response_model=WatchlistResponse)
def get_watchlist(conn: Connection = Depends(get_conn)) -> WatchlistResponse:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        symbols = [row[0] for row in cur.fetchall()]
    return WatchlistResponse(symbols=symbols)
