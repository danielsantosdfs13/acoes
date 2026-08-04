"""Configuração do coletor, via variáveis de ambiente. Nenhuma delas tem
acesso a credencial de banco — o coletor só fala com o processor via HTTP."""

from __future__ import annotations

import os

PROCESSOR_URL = os.environ.get("PROCESSOR_URL", "http://localhost:8000").rstrip("/")
PROCESSOR_API_KEY = os.environ.get("PROCESSOR_API_KEY") or None

POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
WATCHLIST_REFRESH_SECONDS = float(os.environ.get("WATCHLIST_REFRESH_SECONDS", "60"))

# H4 é nativo no MT5 (diferente do Yahoo, que precisa de resample) — sem
# custo extra incluir aqui. W1 fica de fora do loop de tempo real por não
# fazer sentido reenviar a cada poucos segundos; pode ser adicionado se
# algum dia for necessário.
COLLECTOR_TIMEFRAMES = tuple(
    tf.strip() for tf in os.environ.get("COLLECTOR_TIMEFRAMES", "M15,H1,H4,D1").split(",") if tf.strip()
)

# Quantas velas (mais recentes) reenviar a cada loop, por symbol/timeframe.
# Cobre reinícios, loops perdidos e a vela em formação (que muda de OHLC a
# cada tick até fechar) — ver collector.py para o raciocínio completo.
TRAILING_WINDOW = int(os.environ.get("TRAILING_WINDOW", "10"))

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))
