"""Configuração do scraper, via variáveis de ambiente. Nenhuma delas tem
acesso a credencial de banco — o scraper só fala com o processor via HTTP."""

from __future__ import annotations

import os

# No homelab: https://acoes-processor.dondon.services, resolvido pelo
# arquivo hosts da VM para 192.168.122.1 (a NIC da VM na rede do libvirt).
# Ver scraper/README.md e docs/homelab-pipeline.md.
PROCESSOR_URL = os.environ.get("PROCESSOR_URL", "http://localhost:8000").rstrip("/")
ACOES_API_KEY = os.environ.get("ACOES_API_KEY") or None

POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
WATCHLIST_REFRESH_SECONDS = float(os.environ.get("WATCHLIST_REFRESH_SECONDS", "60"))

# H4 é nativo no MT5 (diferente do Yahoo, que precisa de resample) — sem
# custo extra incluir aqui. W1 fica de fora do loop de tempo real por não
# fazer sentido reenviar a cada poucos segundos; pode ser adicionado se
# algum dia for necessário.
SCRAPER_TIMEFRAMES = tuple(
    tf.strip() for tf in os.environ.get("SCRAPER_TIMEFRAMES", "M15,H1,H4,D1").split(",") if tf.strip()
)

# Quantas velas (mais recentes) reenviar a cada loop, por symbol/timeframe.
# Cobre reinícios, loops perdidos e a vela em formação (que muda de OHLC a
# cada tick até fechar) — ver scraper.py para o raciocínio completo.
#
# Era 10 até 2026-08. Como só a vela em formação muda entre um loop e outro,
# as outras 9 eram reenvio puro; 3 já cobre reinício e loop perdido com folga.
# Backfill de histórico é trabalho de script one-shot, não do loop permanente.
TRAILING_WINDOW = int(os.environ.get("TRAILING_WINDOW", "3"))

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))

# Gate de pregão: fora do horário da B3 nenhuma vela pode mudar, então o loop
# dorme MERCADO_FECHADO_SLEEP_SECONDS em vez de POLL_INTERVAL_SECONDS. Sem
# isso, ~70% das buscas no MT5 e dos POSTs aconteciam de madrugada e no fim de
# semana, sem nada pra coletar.
#
# A janela é folgada de propósito (pregão regular é 10h–17h): pega leilão de
# abertura, after-market e o fechamento do D1 sem depender de horário exato.
# Feriados da B3 não são tratados — nesses dias o gate não economiza, o que é
# aceitável perto da complexidade de manter um calendário.
MERCADO_TIMEZONE = os.environ.get("MERCADO_TIMEZONE", "America/Sao_Paulo")
MERCADO_ABERTURA_HORA = int(os.environ.get("MERCADO_ABERTURA_HORA", "9"))
MERCADO_FECHAMENTO_HORA = int(os.environ.get("MERCADO_FECHAMENTO_HORA", "19"))
MERCADO_FECHADO_SLEEP_SECONDS = float(os.environ.get("MERCADO_FECHADO_SLEEP_SECONDS", "300"))
