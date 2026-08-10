"""Leitura de velas do banco pro motor — compartilhada, como `db.py`/`models.py`.

Nasceu em 2026-08-10 extraída de `analyzer.py`, quando `api.py` ganhou o
`POST /analisar` e passou a precisar exatamente da mesma leitura. Ficar num
módulo próprio, e não importado de `analyzer.py`, tem duas razões: importar um
entrypoint de CLI dentro do servidor arrastaria junto o `argparse`, o
`logging.basicConfig` e a dúzia de constantes de env dele; e duplicar o corte
da vela em formação (ver o aviso abaixo) seria repetir a armadilha número um
do worker em outro arquivo, onde ela divergiria em silêncio.

Não confundir com `api.get_candles`: aquele serve JSON pra fora e devolve
velas cruas, inclusive a em formação. Estas aqui alimentam o motor.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# No checkout, `daytrade_smc.py` fica um nível acima (raiz do repo); na
# imagem, tudo é copiado achatado em /app e o CWD já resolve o import. Os
# dois casos funcionam — não troque por um caminho fixo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daytrade_smc import TIMEFRAMES  # noqa: E402


def ler_candles(conn, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    """Últimas `count` velas FECHADAS, direto do banco.

    Espelha `api.get_candles`: ORDER BY time DESC + LIMIT, e devolve ASC,
    que é o contrato do índice em todo o motor.

    ⚠️ O corte da vela em formação é obrigatório e é a armadilha número um
    de quem consome isto. O scraper grava a vela ABERTA a cada ciclo, por
    design — diferente do `_fetch_ohlcv_yahoo`, que descarta a vela corrente
    antes de devolver. Se o analyzer analisasse a vela pela metade, o ON
    CONFLICT DO NOTHING congelaria essa primeira leitura como se fosse o
    sinal definitivo daquela vela, e TODO o dataset de assertividade ficaria
    errado de um jeito que parece certo.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE symbol = %(symbol)s AND timeframe = %(timeframe)s
            ORDER BY time DESC
            LIMIT %(count)s
            """,
            # o clamp é daqui: o `Query(le=5000)` da api não vale in-process
            {"symbol": symbol, "timeframe": timeframe, "count": max(1, min(count, 5000))},
        )
        rows = cur.fetchall()

    if not rows:
        raise RuntimeError(f"Sem candles para {symbol} em {timeframe}.")

    df = pd.DataFrame(
        list(reversed(rows)), columns=["time", "open", "high", "low", "close", "volume"]
    )
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")

    duracao = TIMEFRAMES[timeframe]["duration"]
    fechadas = df[df.index + duracao <= pd.Timestamp.now(tz="UTC")]
    if fechadas.empty:
        raise RuntimeError(f"Só há vela em formação para {symbol} em {timeframe}.")
    return fechadas


def ler_candles_depois(conn, symbol: str, timeframe: str, desde: datetime) -> pd.DataFrame:
    """Velas fechadas posteriores a `desde` — o material do passe de desfecho."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s AND timeframe = %s AND time > %s
            ORDER BY time ASC
            """,
            (symbol, timeframe, desde),
        )
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")
    duracao = TIMEFRAMES[timeframe]["duration"]
    return df[df.index + duracao <= pd.Timestamp.now(tz="UTC")]
