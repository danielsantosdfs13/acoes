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


def _mapa_por_symbol(bruto: str) -> dict[str, str]:
    """Lê "CHAVE=valor;CHAVE2=valor2" num dicionário. Formato pequeno de
    propósito: são duas variáveis de ambiente, não um arquivo de config."""
    mapa: dict[str, str] = {}
    for parte in bruto.split(";"):
        parte = parte.strip()
        if not parte or "=" not in parte:
            continue
        chave, valor = parte.split("=", 1)
        if chave.strip() and valor.strip():
            mapa[chave.strip().upper()] = valor.strip()
    return mapa


# Timeframes DIFERENTES pra símbolos específicos. O loop é um produto
# cartesiano (símbolo × timeframe), então pôr M2/M5 no SCRAPER_TIMEFRAMES
# global faria o scraper coletar 2 e 5 minutos das ONZE ações também —
# duas requisições a mais por ativo por loop, pra um dado que nenhuma
# tela de ação usa. O Mini Índice é o único que precisa desses prazos.
SCRAPER_TIMEFRAMES_POR_SYMBOL = {
    symbol: tuple(tf.strip() for tf in tfs.split(",") if tf.strip())
    for symbol, tfs in _mapa_por_symbol(
        os.environ.get("SCRAPER_TIMEFRAMES_POR_SYMBOL", "WINFUT=M2,M5,M15,H1")
    ).items()
}

# Tradução do nome LÓGICO (o que trafega na API e no banco) pro ticker do
# MT5. Existe porque o mini índice não tem nome estável: dependendo da
# corretora é o contínuo ("WIN$", "WIN$N") ou o vencimento vigente
# ("WINZ25"), que muda a cada trimestre. Fixar o vencimento no banco
# quebraria a série histórica na virada; o nome lógico "WINFUT" não muda
# nunca, e só esta tradução acompanha o rolo do contrato.
#
# Ajuste `WINFUT=` pro que aparece no Observador de Mercado do SEU MT5.
SCRAPER_SYMBOL_MT5 = _mapa_por_symbol(
    os.environ.get("SCRAPER_SYMBOL_MT5", "WINFUT=WIN$")
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

# --- Qual terminal / qual conta do MetaTrader 5 ---
#
# Só `MT5_PATH` e `MT5_LOGIN` bastam pro caso normal, e os dois são
# OPCIONAIS: sem nada configurado o comportamento é o de antes (anexa no
# terminal que estiver rodando). O que eles compram é determinismo.
#
# `mt5.initialize()` sem argumento pega o terminal que encontrar, com a conta
# que estiver logada. Com duas instâncias abertas — o caso de quem mantém a
# real e uma demo ao mesmo tempo — nada decide qual delas alimenta o
# pipeline, e o dado de uma entra no banco com o mesmo nome de símbolo da
# outra, indistinguível depois de gravado.
#
# Como separar de verdade REAL e DEMO na mesma máquina:
#
#   Instâncias lançadas da MESMA pasta compartilham o diretório de dados
#   (%APPDATA%\MetaQuotes\Terminal\<hash>, derivado do caminho de
#   instalação), então elas brigam pela mesma configuração. Para duas contas
#   simultâneas e estáveis, use DUAS instalações — ou copie a pasta do
#   terminal e rode a cópia com `/portable`, que põe os dados ao lado do
#   .exe — e aponte `MT5_PATH` para o .exe de cada uma:
#
#     real  MT5_PATH=C:\Program Files\Clear Investimentos MT5 Terminal\terminal64.exe
#     demo  MT5_PATH=C:\MT5-Clear-Demo\terminal64.exe
#
# `MT5_LOGIN` sozinho NÃO troca de conta: ele afirma qual conta se espera, e
# a coleta falha alto se o terminal estiver servindo outra. É a guarda barata
# — vale configurar mesmo sem `MT5_PATH`.
#
# `MT5_PASSWORD` + `MT5_SERVER` (junto de `MT5_LOGIN`) fazem o terminal
# LOGAR naquela conta, derrubando a sessão que estiver aberta nele. Útil pra
# um processo dedicado, ruim se você estiver olhando aquele terminal.
MT5_PATH = os.environ.get("MT5_PATH") or None
MT5_LOGIN = os.environ.get("MT5_LOGIN") or None
MT5_SERVER = os.environ.get("MT5_SERVER") or None
MT5_PASSWORD = os.environ.get("MT5_PASSWORD") or None
