"""
scraper/scraper.py

Roda continuamente na VM Windows onde o terminal MetaTrader 5 está aberto
e logado. A cada `POLL_INTERVAL_SECONDS`, busca as últimas velas de cada
símbolo/timeframe da watchlist via `daytrade_smc.fetch_ohlcv(...,
source="MetaTrader 5")` — sem reimplementar nada da conexão com o MT5 — e
envia pro processor via HTTP POST /candles.

Este é hoje o ÚNICO chamador de `source="MetaTrader 5"`. A opção saiu do
seletor da interface web (a web nunca roda na máquina com o terminal
aberto), mas o ramo continua em `fetch_ohlcv` justamente por causa daqui —
ver o comentário de `DATA_SOURCES` em daytrade_smc.py.

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
import daytrade_smc  # noqa: E402
from daytrade_smc import DEFAULT_SYMBOLS, fetch_ohlcv, mt5_conta_ativa  # noqa: E402

from config import (  # noqa: E402
    ACOES_API_KEY,
    MT5_LOGIN,
    MT5_PASSWORD,
    MT5_PATH,
    MT5_SERVER,
    MERCADO_ABERTURA_HORA,
    MERCADO_FECHADO_SLEEP_SECONDS,
    MERCADO_FECHAMENTO_HORA,
    MERCADO_TIMEZONE,
    POLL_INTERVAL_SECONDS,
    PROCESSOR_URL,
    REQUEST_TIMEOUT_SECONDS,
    SCRAPER_SYMBOL_MT5,
    SCRAPER_TIMEFRAMES,
    SCRAPER_TIMEFRAMES_POR_SYMBOL,
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
    # Busca pelo ticker do MT5, publica pelo nome lógico. Os dois só
    # diferem no mini índice (ver SCRAPER_SYMBOL_MT5): é o que permite o
    # contrato rolar de vencimento sem partir a série no banco.
    ticker = SCRAPER_SYMBOL_MT5.get(symbol.upper(), symbol)
    try:
        df = fetch_ohlcv(ticker, timeframe, TRAILING_WINDOW, source="MetaTrader 5")
    except Exception as exc:
        alias = f" (MT5: {ticker})" if ticker != symbol else ""
        log.warning("Falha ao buscar %s/%s%s no MT5: %s", symbol, timeframe, alias, exc)
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


def _anunciar_conta() -> None:
    """Registra no log qual conta MT5 vai alimentar o pipeline, e para o
    serviço se não for a esperada.

    Vale a chamada extra na subida porque até 2026-08-11 esta pergunta não
    tinha resposta em lugar nenhum: com duas instâncias do terminal abertas
    (real e demo), `mt5.initialize()` pegava uma delas sem critério e nada
    registrava qual. Falhar aqui é de propósito — o serviço não subir é
    ruído que se vê; coletar da conta errada é dado que não se vê."""
    try:
        conta = mt5_conta_ativa()
    except Exception as exc:  # noqa: BLE001
        if MT5_LOGIN:
            # Com conta declarada, divergência é erro de configuração e o
            # serviço não deve subir fingindo que está tudo bem.
            log.error("Conta MT5 não confere com o configurado: %s", exc)
            raise
        log.warning("Não consegui identificar a conta MT5 agora (%s) — seguindo.", exc)
        return

    log.info(
        "Conta MT5: login=%s servidor=%s (%s) tipo=%s moeda=%s | terminal=%s",
        conta["login"], conta["servidor"], conta["corretora"],
        conta["tipo"], conta["moeda"], conta["path"] or conta["terminal"],
    )
    if not MT5_LOGIN:
        log.warning(
            "MT5_LOGIN não configurado: o scraper aceita QUALQUER conta que o "
            "terminal estiver servindo. Com real e demo abertas ao mesmo tempo, "
            "configure MT5_LOGIN (e MT5_PATH) pra fixar de onde vem o dado."
        )


def main() -> None:
    log.info(
        "Iniciando scraper MT5 — versao=%s, processor=%s, intervalo=%ss, timeframes=%s",
        _deployed_version(), PROCESSOR_URL, POLL_INTERVAL_SECONDS, SCRAPER_TIMEFRAMES,
    )
    # A configuração de conexão vive em `config.py` e é injetada no motor por
    # atribuição, no mesmo padrão de `ACOES_API_URL` — o motor não importa o
    # config do scraper (o Makefile entrega os dois arquivos separados).
    daytrade_smc.MT5_PATH = MT5_PATH
    daytrade_smc.MT5_LOGIN = MT5_LOGIN
    daytrade_smc.MT5_SERVER = MT5_SERVER
    daytrade_smc.MT5_PASSWORD = MT5_PASSWORD
    _anunciar_conta()

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
            for timeframe in SCRAPER_TIMEFRAMES_POR_SYMBOL.get(symbol.upper(), SCRAPER_TIMEFRAMES):
                _post_candles(symbol, timeframe)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
