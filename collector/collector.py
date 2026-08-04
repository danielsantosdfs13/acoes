"""
collector/collector.py

Roda continuamente na VM Windows onde o terminal MetaTrader 5 está aberto
e logado. A cada `POLL_INTERVAL_SECONDS`, busca as últimas velas de cada
símbolo/timeframe da watchlist via `daytrade_smc.fetch_ohlcv(...,
source="MetaTrader 5")` — a mesma função já usada pelo modo "MetaTrader 5"
do app, sem reimplementar nada da conexão com o MT5 — e envia pro
processor via HTTP POST /candles.

Sem watermark de "o que já foi enviado": `copy_rates_from_pos(..., 0,
count)` sempre inclui a vela ainda em formação, cujo OHLC muda a cada tick
até fechar. Se só reenviássemos linhas mais novas que uma marca lembrada,
perderíamos toda atualização intra-vela da barra em formação. Em vez
disso, a cada loop reenviamos as últimas TRAILING_WINDOW velas de cada
symbol/timeframe, e deixamos o upsert (ON CONFLICT DO UPDATE) do processor
absorver as repetidas como no-op — pouco tráfego numa LAN, e correto
contra reinícios, loops perdidos e o problema da vela em formação, sem
nenhum estado local pra persistir ou corromper.

Uso:
    python collector.py

Ver README.md deste diretório para rodar como serviço Windows via NSSM.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daytrade_smc import DEFAULT_SYMBOLS, fetch_ohlcv  # noqa: E402

from config import (  # noqa: E402
    COLLECTOR_TIMEFRAMES,
    POLL_INTERVAL_SECONDS,
    PROCESSOR_API_KEY,
    PROCESSOR_URL,
    REQUEST_TIMEOUT_SECONDS,
    TRAILING_WINDOW,
    WATCHLIST_REFRESH_SECONDS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("collector")


def _headers() -> dict[str, str]:
    headers = {}
    if PROCESSOR_API_KEY:
        headers["X-API-Key"] = PROCESSOR_API_KEY
    return headers


def _refresh_watchlist(fallback: list[str]) -> list[str]:
    try:
        response = requests.get(
            f"{PROCESSOR_URL}/watchlist", headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        symbols = response.json().get("symbols", [])
        if symbols:
            return symbols
    except Exception as exc:
        log.warning("Não foi possível atualizar a watchlist (%s) — mantendo a lista anterior.", exc)
    return fallback


def _post_candles(symbol: str, timeframe: str) -> None:
    try:
        df = fetch_ohlcv(symbol, timeframe, TRAILING_WINDOW, source="MetaTrader 5")
    except Exception as exc:
        log.warning("Falha ao buscar %s/%s no MT5: %s", symbol, timeframe, exc)
        return

    candles = [
        {
            "time": index.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for index, row in df.iterrows()
    ]
    payload = {"symbol": symbol, "timeframe": timeframe, "source": "MetaTrader 5", "candles": candles}

    try:
        response = requests.post(
            f"{PROCESSOR_URL}/candles", json=payload, headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("Falha ao enviar %s/%s pro processor: %s", symbol, timeframe, exc)


def main() -> None:
    log.info(
        "Iniciando coletor MT5 — processor=%s, intervalo=%ss, timeframes=%s",
        PROCESSOR_URL, POLL_INTERVAL_SECONDS, COLLECTOR_TIMEFRAMES,
    )
    watchlist = _refresh_watchlist(fallback=DEFAULT_SYMBOLS.copy())
    last_refresh = time.monotonic()

    while True:
        if time.monotonic() - last_refresh > WATCHLIST_REFRESH_SECONDS:
            watchlist = _refresh_watchlist(fallback=watchlist)
            last_refresh = time.monotonic()

        for symbol in watchlist:
            for timeframe in COLLECTOR_TIMEFRAMES:
                _post_candles(symbol, timeframe)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
