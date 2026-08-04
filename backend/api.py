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

Com este serviço no lugar, nenhum cliente fora do backend precisa de
credencial de banco — o Streamlit deixou de falar SQL.

Deploy: `uvicorn api:app`, a partir da mesma imagem `acoes-backend` que
roda `processor.py` e `migrate.py`.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg import Connection

from auth import require_api_key
from db import get_conn
from models import (
    CandleOut,
    CandlesResponse,
    SymbolStatus,
    WatchlistReplace,
    WatchlistResponse,
)

app = FastAPI(title="Ações — API (leitura)")


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
