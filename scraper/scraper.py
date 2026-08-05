"""
scraper/scraper.py

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
symbol/timeframe e deixamos o upsert do processor deduplicar — correto
contra reinícios, loops perdidos e o problema da vela em formação, sem
nenhum estado local pra persistir ou corromper.

O que este comentário afirmava até 2026-08, e estava ERRADO: que o
ON CONFLICT DO UPDATE absorvia as repetidas "como no-op". Ele é no-op na
contagem de linhas, não no armazenamento — todo UPDATE em Postgres é uma
tupla nova mais uma tupla morta, com WAL e autovacuum atrás. Medido no
homelab: 160,7 escritas/s e 2,1 GB de WAL por dia pra manter 40 KB/dia de
dados novos. O que tornou a afirmação verdadeira foi a guarda
`IS DISTINCT FROM` em `_UPSERT_SQL` (backend/processor.py); o barateamento
deste lado veio de TRAILING_WINDOW menor e do gate de pregão abaixo.

Uso:
    python scraper.py

Ver README.md deste diretório para rodar como serviço Windows via NSSM.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daytrade_smc import DEFAULT_SYMBOLS, fetch_ohlcv  # noqa: E402

from config import (  # noqa: E402
    ACOES_API_KEY,
    MERCADO_ABERTURA_HORA,
    MERCADO_FECHADO_SLEEP_SECONDS,
    MERCADO_FECHAMENTO_HORA,
    MERCADO_TIMEZONE,
    POLL_INTERVAL_SECONDS,
    PROCESSOR_URL,
    REQUEST_TIMEOUT_SECONDS,
    SCRAPER_TIMEFRAMES,
    TRAILING_WINDOW,
    WATCHLIST_REFRESH_SECONDS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scraper")


def _deployed_version() -> str:
    """Lê o VERSION do DEPLOY-INFO que o `make release-scraper` grava na raiz
    do diretório de deploy. Serve pro log do serviço dizer sozinho qual versão
    está rodando — é o equivalente da tag de imagem do lado do k3s.

    Ausente quando se roda direto do checkout, o que é normal e não é erro."""
    info = Path(__file__).resolve().parent.parent / "DEPLOY-INFO"
    try:
        for line in info.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VERSION="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "desconhecida (sem DEPLOY-INFO)"


_TZ_MERCADO = ZoneInfo(MERCADO_TIMEZONE)


def _mercado_aberto(agora: datetime | None = None) -> bool:
    """True se a B3 pode estar negociando agora (seg–sex, dentro da janela).

    Não trata feriado: num feriado o loop roda à toa, que é bem mais barato do
    que manter um calendário da B3 correto. Ver config.py para a janela."""
    agora = agora or datetime.now(_TZ_MERCADO)
    if agora.weekday() >= 5:  # 5 = sábado, 6 = domingo
        return False
    return MERCADO_ABERTURA_HORA <= agora.hour < MERCADO_FECHAMENTO_HORA


def _headers() -> dict[str, str]:
    headers = {}
    if ACOES_API_KEY:
        headers["X-API-Key"] = ACOES_API_KEY
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
        "Iniciando scraper MT5 — versao=%s, processor=%s, intervalo=%ss, timeframes=%s",
        _deployed_version(), PROCESSOR_URL, POLL_INTERVAL_SECONDS, SCRAPER_TIMEFRAMES,
    )
    watchlist = _refresh_watchlist(fallback=DEFAULT_SYMBOLS.copy())
    last_refresh = time.monotonic()
    estava_aberto: bool | None = None

    while True:
        if time.monotonic() - last_refresh > WATCHLIST_REFRESH_SECONDS:
            watchlist = _refresh_watchlist(fallback=watchlist)
            last_refresh = time.monotonic()

        # O gate fica DEPOIS do refresh da watchlist, pra que ela continue
        # sendo atualizada com o mercado fechado — assim o scraper já abre o
        # pregão com a lista certa.
        aberto = _mercado_aberto()
        if aberto != estava_aberto:
            log.info(
                "Mercado %s — intervalo de %ss.",
                "ABERTO" if aberto else "FECHADO",
                POLL_INTERVAL_SECONDS if aberto else MERCADO_FECHADO_SLEEP_SECONDS,
            )
            estava_aberto = aberto

        if not aberto:
            time.sleep(MERCADO_FECHADO_SLEEP_SECONDS)
            continue

        for symbol in watchlist:
            for timeframe in SCRAPER_TIMEFRAMES:
                _post_candles(symbol, timeframe)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
