r"""
Motor de análise — SMC + Price Action + EMAs + VWAP + Confluência.

É a única casa da lógica de análise do projeto. Quem importa daqui:

    streamlit_app.py       a interface web (as seis leituras, o Scanner,
                           a verificação retroativa e a assertividade)
    backend/analyzer.py    o worker que varre a watchlist gravando sinais
    scraper/scraper.py     a ingestão na VM Windows, via
                           fetch_ohlcv(..., source="MetaTrader 5")

Este arquivo não tem código de interface: a tela é o Streamlit. O que sobra
de linha de comando é um relatório de um ativo, útil pra conferir o motor
sem subir a web:

    python daytrade_smc.py VALE3
    python daytrade_smc.py VALE3 --timeframe M15 --count 250 --risco 500

Aviso: o Yahoo Finance tem atraso e pode limitar requisições. O programa
serve para leitura técnica e estudo; não envia ordens e não substitui dados
em tempo real nem gestão profissional de risco.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import queue
import re
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError as exc:
    raise SystemExit(
        "Biblioteca ausente. Rode no terminal:\n"
        "python -m pip install pandas numpy yfinance"
    ) from exc


LOCAL_TZ = "America/Sao_Paulo"
DEFAULT_SYMBOLS = [
    "VALE3",
    "PETR4",
    "PRIO3",
    "ITUB4",
    "BBAS3",
    "BBDC4",
    "B3SA3",
    "WEGE3",
    "ABEV3",
    "MGLU3",
    "BRA50",
]
SYMBOL_ALIASES = {
    "BRA50": "^BVSP",
    "IBOV": "^BVSP",
    "IBOVESPA": "^BVSP",
    "DOLAR": "BRL=X",
    "USDBRL": "BRL=X",
    "USD/BRL": "BRL=X",
    "SP500": "^GSPC",
    "S&P500": "^GSPC",
    "NASDAQ": "^IXIC",
    "BTC": "BTC-USD",
    "BITCOIN": "BTC-USD",
}
TIMEFRAMES = {
    # M2 e M5 existem pro Mini Índice (ver WINFUT_* abaixo) e não são
    # usados por nenhum ativo da watchlist de ações. Os limites de
    # `max_days` são os do Yahoo (7 dias em 2m, 60 em 5m), mas na prática
    # o WINFUT não vem do Yahoo — ver o comentário em WINFUT_SYMBOL.
    "M2": {
        "interval": "2m",
        "duration": pd.Timedelta(minutes=2),
        "candles_day": 203,   # ~6h45 de pregão da B3
        "max_days": 7,
    },
    "M5": {
        "interval": "5m",
        "duration": pd.Timedelta(minutes=5),
        "candles_day": 81,
        "max_days": 60,
    },
    "M15": {
        "interval": "15m",
        "duration": pd.Timedelta(minutes=15),
        "candles_day": 26,
        "max_days": 60,
    },
    "H1": {
        "interval": "60m",
        "duration": pd.Timedelta(hours=1),
        "candles_day": 7,
        "max_days": 60,
    },
    "H4": {
        # O Yahoo Finance não tem intervalo nativo de 240min — este
        # timeframe é construído agregando 4 candles de H1 (ver
        # `fetch_ohlcv`). "candles_day" é aproximado (pregão de ~7h ÷ 4h).
        "interval": "240m",
        "duration": pd.Timedelta(hours=4),
        "candles_day": 3,
        "max_days": 60,
    },
    "D1": {
        "interval": "1d",
        "duration": pd.Timedelta(days=1),
        "candles_day": 1,
        "max_days": 730,
    },
    "W1": {
        "interval": "1wk",
        "duration": pd.Timedelta(weeks=1),
        "candles_day": 1 / 7,
        "max_days": 2500,  # o Yahoo permite bastante histórico semanal
    },
}

# Day Trade: confirmação em M15+H1 (posições fechadas no mesmo dia).
# H4 e Diário entram como contexto de tendência mais ampla.
DAYTRADE_CONFIRMATION_TIMEFRAMES = ("M15", "H1")
DAYTRADE_CONTEXT_TIMEFRAMES = ("H4", "D1")

# Swing Trade: confirmação em Diário+Semanal (posições de dias a semanas).
# H4 entra como contexto pra afinar o timing de entrada dentro da
# tendência maior — o inverso do Day Trade, onde H4 é "zoom out".
SWING_CONFIRMATION_TIMEFRAMES = ("D1", "W1")
SWING_CONTEXT_TIMEFRAMES = ("H4",)

# Mini Índice (WINFUT) — modo dedicado, totalmente separado da watchlist
# de ações. Timeframes próprios porque o contrato futuro se opera num
# horizonte mais curto: confirmação em M5+M15, com M2 pra afinar o timing
# de entrada e H1 pro contexto da sessão.
#
# ATENÇÃO à origem do dado: "WINFUT" é um nome LÓGICO, não um ticker do
# Yahoo — `yahoo_symbol("WINFUT")` devolve a string intacta e o Yahoo não
# conhece esse símbolo. O mini índice só chega aqui pelo pipeline do
# homelab, com o scraper coletando do MT5 na VM Windows. É por isso que a
# interface bloqueia o modo quando a fonte não é "Homelab (API)": sem
# isso, o modo daria erro de "símbolo não encontrado" e pareceria bug.
#
# O nome do contrato no MT5 depende da corretora (contínuo "WIN$" ou o
# vencimento vigente, tipo "WINZ25"). Quem faz essa tradução é o scraper,
# via SCRAPER_SYMBOL_MT5 — aqui dentro o símbolo é sempre "WINFUT".
WINFUT_SYMBOL = "WINFUT"
WINFUT_CONFIRMATION_TIMEFRAMES = ("M5", "M15")
WINFUT_CONTEXT_TIMEFRAMES = ("M2", "H1")

# Mantidos por compatibilidade — apontam pro conjunto de Day Trade, que
# é o comportamento padrão histórico deste motor.
CONFIRMATION_TIMEFRAMES = DAYTRADE_CONFIRMATION_TIMEFRAMES
CONTEXT_TIMEFRAMES = DAYTRADE_CONTEXT_TIMEFRAMES

# Limiares de IFR sugeridos por estilo de operação. A diferença existe
# porque o ruído muda de escala com o timeframe: em M15 o IFR bate 90/10
# com alguma regularidade, então só o extremo verdadeiro filtra bem. Já
# no Diário/Semanal, 90/10 é raríssimo — quase nunca dispararia — e
# 80/20 já representa exaustão genuína naquele horizonte.
#
# É SUGESTÃO, não imposição: quem manda são os campos `rsi_sobrevenda` /
# `rsi_sobrecompra` do perfil em uso. A interface só aplica esta tabela
# quando o perfil deixou os dois no default (ver `params_para_estilo`),
# pra não sobrescrever em silêncio uma calibragem que alguém escolheu.
# O worker (`backend/analyzer.py`) não consome isto: ele é Day Trade
# puro, com CONFIRMACAO fixo em M15+H1.
STYLE_RSI_THRESHOLDS = {
    "Day Trade": (10.0, 90.0),
    "Swing Trade": (20.0, 80.0),
    # O Mini Índice opera em M2/M5, prazos ainda mais curtos e ruidosos
    # que o M15 — o extremo verdadeiro é o único que filtra alguma coisa.
    "Mini Índice (WINFUT)": (10.0, 90.0),
}


def params_para_estilo(params: AnalysisParams, style: str) -> AnalysisParams:
    """Aplica os limiares de IFR sugeridos pro estilo, SE o perfil não
    tiver escolhido os seus.

    "Não escolheu" aqui é "os dois limiares estão no default". A
    ambiguidade é conhecida e aceita: quem cravar 10/90 num perfil de
    Swing vai ver 20/80 mesmo assim, porque não há como distinguir o
    valor escolhido do valor herdado. O caminho pra fixar de verdade é
    salvar o perfil com qualquer outro par.

    Devolve uma instância NOVA — `AnalysisParams` é frozen, e o
    `params_hash` acompanha a troca, que é o ponto: um sinal de Swing
    gravado com 20/80 tem que ficar distinguível de um de Day Trade
    gravado com 10/90.
    """
    limiares = STYLE_RSI_THRESHOLDS.get(style)
    if limiares is None:
        return params
    no_default = (
        params.rsi_sobrevenda == DEFAULT_PARAMS.rsi_sobrevenda
        and params.rsi_sobrecompra == DEFAULT_PARAMS.rsi_sobrecompra
    )
    if not no_default:
        return params
    sobrevenda, sobrecompra = limiares
    return replace(params, rsi_sobrevenda=sobrevenda, rsi_sobrecompra=sobrecompra)


class Direction(str, Enum):
    BUY = "COMPRA"
    SELL = "VENDA"
    NEUTRAL = "NEUTRO"


@dataclass(frozen=True)
class AnalysisParams:
    """
    Conjunto CURADO de parâmetros do motor. Todo default aqui é exatamente
    o literal que estava cravado no código antes — um `AnalysisParams()`
    sem argumentos reproduz o comportamento histórico, campo por campo.

    Mora neste arquivo, e não num módulo novo, porque `daytrade_smc.py`
    não importa nenhum módulo de primeira parte: é o fechamento que o
    `make release-scraper` entrega pra VM Windows (ver Makefile). Um
    `params.py` separado quebraria o scraper em silêncio.

    É `frozen` por dois motivos: ganha `__hash__` de graça (o
    `st.cache_data` do Streamlit precisa hashear isso — ver `to_items`) e
    torna seguro compartilhar a instância única `DEFAULT_PARAMS` como
    default de campo do `MarketContext`.

    O que ficou DE FORA, de propósito, pra não procurar em vão:
      - períodos das EMAs (9/21/50/200): os nomes de coluna `ema_9`... são
        lidos por nome em meia dúzia de lugares;
      - as bases 70/55 e o teto 90 do `smc_signal`;
      - os pesos 50/45/20 do `price_action_signal`;
      - `min_gap_pct`/`min_slope_pct` do `moving_average_signal`;
      - os limiares de `_confirmed_breakout` e `candle_patterns`;
      - os buffers de ATR do `stop_for_signal`/`structural_stop`.

    ATENÇÃO ao par `normalizacao_score` / `filtro_isolada_score_max`: os
    dois valem 79.0 e são ACOPLADOS por construção — uma leitura isolada
    é capada em 79 pelo filtro de mercado, e a confluência divide por 79
    justamente pra essa leitura capada normalizar em exatamente 1.0.
    Mexer num sem o outro desregula a escala da confluência.
    """

    # --- Contexto -------------------------------------------------------
    atr_periodo: int = 14                       # compute_atr
    # "wilder" (SMMA/RMA, convenção do MT5 e do TradingView) ou "simples"
    # (média móvel do True Range, o que este motor fazia até 10/08/2026).
    # Existe como CAMPO, e não como troca silenciosa, porque o ATR define
    # a distância mínima do stop: entra no `params_hash` e portanto fica
    # gravado em cada linha de `signals`, dizendo com que convenção
    # aquele sinal foi medido. Sem isso, o histórico anterior e o novo
    # ficariam indistinguíveis na taxa de acerto.
    atr_suavizacao: str = "wilder"              # compute_atr
    rsi_periodo: int = 14                       # compute_rsi
    swing_esquerda: int = 3                     # detect_swings
    swing_direita: int = 3                      # detect_swings
    vol_baixa_max_pct: float = 0.30             # build_context: abaixo disso, volatilidade BAIXA
    vol_excessiva_min_pct: float = 6.0          # build_context: acima disso, EXCESSIVA

    # --- Estrutura (BOS/CHoCH) e Price Action ---------------------------
    estrutura_volume_min: float = 1.2           # detect_structure: volume/média mínimo pra validar
    estrutura_range_min: float = 0.8            # detect_structure: amplitude/ATR mínima
    evento_max_idade: int = 20                  # last_recent_event: candles desde o BOS/CHoCH
    rompimento_lookback: int = 20               # breakout_and_retest
    rompimento_tolerancia_pct: float = 0.3      # breakout_and_retest
    fvg_max_idade: int = 20                     # detect_fvg_setup

    # --- VWAP -----------------------------------------------------------
    vwap_distancia_min_pct: float = 0.10        # abaixo disso o preço está "em cima" da VWAP
    vwap_distancia_max_pct: float = 1.5         # acima disso bloqueia entrada e alerta

    # --- IFR (RSI) ------------------------------------------------------
    # O padrão é 90/10 (extremo verdadeiro), não os convencionais 70/30.
    # A diferença é de natureza, não só de grau: 70/30 é atingido com
    # frequência DENTRO de tendências normais — ali o gatilho confiável
    # seria a SAÍDA da zona, não a permanência nela. Já 90/10 marca
    # exaustão genuína e rara, em que a própria permanência na zona já é
    # o sinal. Muito menos sinais, porém bem mais seletivos.
    rsi_sobrecompra: float = 90.0               # rsi_signal: acima disso, exaustão compradora -> VENDA
    rsi_sobrevenda: float = 10.0                # rsi_signal: abaixo disso, exaustão vendedora -> COMPRA

    # --- Confluência ----------------------------------------------------
    # As QUATRO categorias estruturais, e só elas. O IFR é a sexta leitura
    # mas fica DELIBERADAMENTE fora deste cálculo (ver `analyze`): é uma
    # leitura contrária, de exaustão, enquanto estas quatro são de
    # estrutura e tendência. Misturar as duas naturezas tinha dois
    # efeitos ruins, os dois medidos: como o IFR fica NEUTRO quase
    # sempre (0 disparos em 20 séries reais), ele diluía TODO score de
    # confluência em ~10-12% sem acrescentar informação, e ainda
    # invalidava a comparação com todo o histórico já gravado nesta
    # modalidade. Fora do cálculo, a Confluência segue bit a bit a de
    # antes e o IFR continua visível como leitura própria.
    peso_smc: float = 30.0
    peso_price_action: float = 20.0
    peso_medias: float = 20.0
    peso_vwap: float = 20.0
    normalizacao_score: float = 79.0            # divisor que normaliza a força de cada leitura
    confluencia_banda_empate: float = 3.0       # diferença compra/venda abaixo da qual dá NEUTRO
    multiplicador_concordancia: tuple[float, ...] = (0.60, 0.60, 0.95, 1.0, 1.10)  # indexado por 0..4 leituras concordando

    # --- Filtro de mercado ----------------------------------------------
    filtro_isolada_score_max: float = 79.0
    filtro_isolada_confianca_max: float = 70.0
    filtro_bloqueio_score_max: float = 39.0
    filtro_bloqueio_confianca_max: float = 35.0
    filtro_excessiva_score_max: float = 59.0
    filtro_excessiva_confianca_max: float = 50.0
    score_minimo_operavel: float = 40.0
    bandas_qualidade: tuple[float, ...] = (40.0, 60.0, 70.0, 80.0, 90.0)

    # --- Risco ----------------------------------------------------------
    rr_alvo_1: float = 1.5
    rr_alvo_2: float = 3.0
    stop_minimo_atr: float = 0.75

    def to_items(self) -> tuple[tuple[str, object], ...]:
        """Forma canônica e hasheável, ordenada por nome do campo.

        É ESTA a forma que atravessa o `@st.cache_data` do Streamlit. O
        hasher dele garante tuplas de primitivos; um dataclass ou levanta
        `UnhashableParamError` ou é hasheado por identidade — e a falha
        por identidade é silenciosa (trocar de perfil continuaria
        servindo os scores do perfil anterior pelo TTL inteiro)."""
        return tuple(
            (campo.name, getattr(self, campo.name))
            for campo in sorted(fields(self), key=lambda f: f.name)
        )

    @classmethod
    def from_items(cls, items: tuple[tuple[str, object], ...]) -> AnalysisParams:
        return cls.from_dict(dict(items))

    def to_dict(self) -> dict:
        """Pronto pra JSON: tuplas viram listas."""
        pronto = {}
        for campo in fields(self):
            valor = getattr(self, campo.name)
            pronto[campo.name] = list(valor) if isinstance(valor, tuple) else valor
        return pronto

    @classmethod
    def from_dict(cls, data: dict | None) -> AnalysisParams:
        """Tolerante de propósito: chave desconhecida é ignorada, chave
        ausente cai no default, lista vira tupla, e tupla CURTA demais é
        completada com o default.

        É isso que faz um perfil salvo por um build antigo continuar
        carregando depois de um parâmetro novo entrar no motor, e que faz
        `{}` significar "todos os defaults de hoje" — por isso o perfil
        'padrão' é gravado no banco como um objeto vazio, e não como o
        dicionário completo.

        O completar-com-o-default cobre um caso que já mordeu: quando o
        IFR entrou como quinta leitura isolada, `multiplicador_concordancia`
        passou de 5 pra 6 posições. Um perfil gravado antes disso, que
        tivesse customizado essa tupla, voltaria do banco com 5 posições e
        estouraria `IndexError` na primeira vez que as cinco leituras
        concordassem — meses depois de salvo, num caminho raro, no worker.
        Uma tupla LONGA demais é truncada pelo mesmo motivo simétrico."""
        if not data:
            return cls()
        conhecidos = {campo.name: campo for campo in fields(cls)}
        padrao = cls()
        kwargs = {}
        for nome, valor in data.items():
            campo = conhecidos.get(nome)
            if campo is None:
                continue
            if isinstance(valor, (list, tuple)):
                referencia = getattr(padrao, nome)
                valor = tuple(valor)
                if isinstance(referencia, tuple) and len(valor) != len(referencia):
                    valor = (valor + referencia[len(valor):])[:len(referencia)]
            kwargs[nome] = valor
        return cls(**kwargs)

    def params_hash(self) -> str:
        """Identidade estável do conjunto, gravada junto de cada sinal pra
        depois dar pra saber com que calibragem ele foi gerado."""
        bruto = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:16]


DEFAULT_PARAMS = AnalysisParams()


@dataclass
class Swing:
    index: int
    confirmed_index: int
    price: float
    kind: str  # HIGH ou LOW


@dataclass
class StructureEvent:
    index: int
    kind: str  # BOS ou CHOCH
    direction: Direction
    confidence: float
    broken_level: float


@dataclass
class MarketContext:
    df: pd.DataFrame
    atr: float
    atr_pct: float
    rvol: float
    volatility: str
    emas: pd.DataFrame
    ema_reliable: bool
    vwap_series: pd.Series
    vwap: float
    vwap_slope_pct: float
    vwap_distance_pct: float
    vwap_rejection: bool
    swings: list[Swing]
    events: list[StructureEvent]
    patterns: list[str]
    broke_high: bool
    broke_low: bool
    bullish_retest: bool
    bearish_retest: bool
    fvg_setup: str | None
    # IFR do próprio timeframe. `rsi_series` fica guardada pro gráfico.
    rsi: float = 50.0
    rsi_prev: float = 50.0
    rsi_series: pd.Series | None = None
    # IFR do timeframe superior (Diário), injetado pelo multi-timeframe
    # (ver `analyze_symbol_mtf`). None quando não disponível — é o caso
    # de quem analisa um timeframe só, como a verificação retroativa e o
    # backfill; aí a leitura de IFR opera só com o próprio prazo.
    higher_rsi: float | None = None
    # Último campo, e trailing de propósito: é o que torna os
    # parâmetros alcançáveis por TODA função de score/risco (que recebem
    # só o contexto) sem mexer em nenhuma assinatura.
    params: AnalysisParams = DEFAULT_PARAMS


@dataclass
class RiskPlan:
    entry: float | None = None
    stop: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    rr: float | None = None
    stop_basis: str = ""
    alternatives: list[dict] = field(default_factory=list)


@dataclass
class Signal:
    name: str
    direction: Direction
    score: float
    confidence: float
    setup: str
    reasons: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    risk: RiskPlan = field(default_factory=RiskPlan)


def yahoo_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper().replace(" ", "")
    if not symbol:
        raise ValueError("Informe um ativo para análise.")

    symbol = SYMBOL_ALIASES.get(symbol, symbol)
    if (
        symbol.startswith("^")
        or "." in symbol
        or "=" in symbol
        or "-" in symbol
    ):
        return symbol

    # Ações, units, FIIs e ETFs brasileiros recebem o sufixo do Yahoo.
    if re.fullmatch(r"[A-Z0-9]{4,6}\d{1,2}", symbol):
        return f"{symbol}.SA"

    # Símbolos internacionais, como AAPL, permanecem sem sufixo.
    return symbol


def date_window(
    timeframe: str,
    count: int,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    cfg = TIMEFRAMES[timeframe]
    days = math.ceil(count / cfg["candles_day"] * 2.2)
    days = max(5, min(days, cfg["max_days"]))
    end = pd.Timestamp.now(tz="UTC").tz_localize(None)
    start = end - pd.Timedelta(days=days)
    return start, end


def _resample_to_h4(df_h1: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega candles de H1 em barras de 4 horas. Ancorado à meia-noite
    local (Brasília) — como o pregão da B3 abre às 10h, a primeira barra
    do dia cobre só 10h-12h (2h reais) e as seguintes ficam completas
    (12h-16h, 16h-18h~fechamento). É uma aproximação de "H4 de mercado",
    não um H4 perfeitamente alinhado à abertura — suficiente pra dar
    contexto de tendência mais ampla, mas vale ter isso em mente.
    """
    local = df_h1.tz_convert(LOCAL_TZ)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    resampled = local.resample("4h", origin="start_day").agg(agg)
    resampled = resampled.dropna(subset=["open", "high", "low", "close"])

    # A última barra de 4h só deve ser descartada se ainda estiver em
    # formação AGORA (pregão do dia em andamento e a janela de 4h ainda
    # não terminou) — não se for o fim de um pregão já encerrado (nesse
    # caso a barra está completa mesmo cobrindo menos de 4h reais, já
    # que não virá mais dado depois do fechamento daquele dia).
    if not resampled.empty:
        now_local = pd.Timestamp.now(tz=LOCAL_TZ)
        last_bucket_start = resampled.index[-1]
        last_bucket_end = last_bucket_start + pd.Timedelta(hours=4)
        if now_local < last_bucket_end:
            resampled = resampled.iloc[:-1]

    return resampled.tz_convert("UTC")


# Fontes oferecidas no SELETOR da interface web — não é a lista completa que
# `fetch_ohlcv` aceita. "MetaTrader 5" continua despachando normalmente logo
# abaixo, porque o scraper na VM Windows chama
# `fetch_ohlcv(..., source="MetaTrader 5")` direto (scraper/scraper.py). Tirar
# o ramo do MT5 de `fetch_ohlcv` por ele não estar nesta tupla mataria a
# ingestão do pipeline inteiro; ele só não aparece na web porque a web nunca
# roda na máquina que tem o terminal aberto.
#
# Homelab vem primeiro por ser o caminho recomendado: é o default do seletor
# quando ACOES_API_URL está configurada.
DATA_SOURCES = ("Homelab (API)", "Yahoo Finance")

# Configuração da API do homelab (serviço `api`, que lê o TimescaleDB
# alimentado pelo `acoes-scraper`) — definida pelo app a partir de
# st.secrets/env antes de qualquer chamada com source="Homelab (API)".
# Mesmo padrão de globals de módulo usado pela ponte GitHub acima.
#
# Até 2026-08 este módulo falava SQL direto com o Postgres. Passar por HTTP
# tirou a credencial de banco de todo cliente que não seja o backend, e deu
# um caminho de leitura único pro Streamlit e pra qualquer outro consumidor.
ACOES_API_URL: str | None = None
ACOES_API_KEY: str | None = None

# Timeout de todas as chamadas à API. Curto de propósito: o Streamlit chama
# isso de dentro de um rerun, e travar a UI é pior que dar erro.
_API_TIMEOUT_SECONDS = 10

_MT5_TIMEFRAME_MAP_NAMES = {"M2": "TIMEFRAME_M2", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1"}


def fetch_ohlcv(symbol: str, timeframe: str, count: int, source: str = "Yahoo Finance") -> pd.DataFrame:
    """
    Busca candles pela fonte escolhida.
      - "Yahoo Finance": funciona em qualquer lugar (nuvem ou local), atraso de 15-20min.
      - "MetaTrader 5": dado real, sem atraso, mas só funciona rodando LOCALMENTE, na
        máquina com o terminal MT5 aberto e logado. Não aparece no seletor da web
        (ver DATA_SOURCES) — é por aqui que o scraper na VM ingere as velas.
      - "Homelab (API)": lê candles pela API do homelab (`GET /candles`), que serve
        o que o `acoes-scraper` persistiu no TimescaleDB rodando continuamente numa
        VM — dado real, quase em tempo real (poucos segundos de atraso), sem
        depender do GitHub Actions e sem credencial de banco no cliente.
    """
    if timeframe == "H4" and source == "Yahoo Finance":
        # busca H1 com folga (4x) pra ter candles de H1 suficientes antes de agregar
        h1_df = fetch_ohlcv(symbol, "H1", count * 4 + 20, source=source)
        df = _resample_to_h4(h1_df)
        if df.empty:
            raise RuntimeError(f"Não foi possível construir candles de H4 para {symbol} a partir do H1.")
        return df.iloc[-count:] if len(df) > count else df

    if source == "MetaTrader 5":
        return _fetch_ohlcv_mt5(symbol, timeframe, count)

    if source == "Homelab (API)":
        return _fetch_ohlcv_api(symbol, timeframe, count)

    return _fetch_ohlcv_yahoo(symbol, timeframe, count)


def _api_base_url() -> str:
    """URL da API do homelab, ou erro claro em vez de travar."""
    if not ACOES_API_URL:
        raise RuntimeError(
            "API do homelab não configurada (ACOES_API_URL). Configure em "
            "st.secrets['acoes_api_url'] ou na variável de ambiente ACOES_API_URL."
        )
    return ACOES_API_URL.rstrip("/")


def _api_headers() -> dict[str, str]:
    """A chave só é necessária nas rotas de escrita, mas mandar sempre é
    inofensivo e evita ter dois caminhos de montagem de header."""
    return {"X-API-Key": ACOES_API_KEY} if ACOES_API_KEY else {}


def _fetch_ohlcv_api(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    """Lê candles pela API do homelab (`GET /candles`), que serve o que o
    `acoes-scraper` persistiu no TimescaleDB."""
    import requests  # import tardio — mesma convenção de _fetch_ohlcv_mt5

    try:
        response = requests.get(
            f"{_api_base_url()}/candles",
            params={"symbol": symbol, "timeframe": timeframe, "count": count},
            headers=_api_headers(),
            timeout=_API_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            raise RuntimeError(f"Sem candles na API do homelab para {symbol} em {timeframe}.")
        response.raise_for_status()
        candles = response.json().get("candles", [])
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Falha ao consultar a API do homelab: {exc}") from exc

    if not candles:
        raise RuntimeError(f"Sem candles na API do homelab para {symbol} em {timeframe}.")

    df = pd.DataFrame(candles)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]].tail(count)


def _fetch_ohlcv_mt5(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError(
            "Pacote MetaTrader5 não está instalado neste ambiente. Isso só funciona rodando o "
            "app LOCALMENTE, na mesma máquina onde o terminal MT5 está instalado — não funciona "
            "no Streamlit Cloud. Rode `pip install MetaTrader5` na máquina onde o MT5 está aberto."
        ) from exc

    if timeframe not in _MT5_TIMEFRAME_MAP_NAMES:
        raise ValueError(f"Timeframe {timeframe} não é suportado via MT5.")
    mt5_timeframe = getattr(mt5, _MT5_TIMEFRAME_MAP_NAMES[timeframe])

    if not mt5.initialize():
        error = mt5.last_error()
        raise RuntimeError(
            f"Não foi possível conectar ao terminal MetaTrader 5 ({error}). Confirme que o MT5 "
            "está aberto e logado nesta máquina."
        )

    try:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"Ativo '{symbol}' não foi encontrado no MT5. Confirme o código exato usado pela "
                "sua corretora (às vezes tem sufixo, ex: PETR4F)."
            )

        rates = mt5.copy_rates_from_pos(symbol, mt5_timeframe, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 não devolveu candles para {symbol} em {timeframe} ({mt5.last_error()}).")
    finally:
        mt5.shutdown()

    df = pd.DataFrame(rates)
    # O `time` devolvido pelo MT5 é o relógio LOCAL do servidor em formato
    # epoch — o horário de parede como se fosse UTC, sem o offset. A Clear
    # roda o servidor em horário de Brasília, então sem conversão cada vela
    # nascia 3h atrasada em relação ao UTC verdadeiro. Isso não era só
    # estético: como o `ler_candles` do backend corta "vela fechada"
    # comparando com `now(UTC)`, o worker acabava analisando a vela EM
    # FORMAÇÃO e o `ON CONFLICT DO NOTHING` congelava a leitura pela metade
    # (medido: `criado_em − candle_time` ≈ 3h + 1min nos sinais M15).
    #
    # A conversão reusa o MESMO fuso que o resto da pipeline (LOCAL_TZ, que o
    # scraper espelha em MERCADO_TIMEZONE): interpreta o epoch como hora local
    # do mercado e converte pra UTC. Se a corretora um dia trocar o fuso do
    # servidor, é aqui que se mexe — o restante do pipeline não muda.
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df["time"] = df["time"].dt.tz_localize(LOCAL_TZ).dt.tz_convert("UTC")
    df = df.set_index("time").sort_index()
    df = df.rename(columns={"tick_volume": "volume"})

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Resposta do MT5 sem colunas obrigatórias: {missing}")

    return df[["open", "high", "low", "close", "volume"]].tail(count)


def _fetch_ohlcv_yahoo(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    ticker = yahoo_symbol(symbol)
    cfg = TIMEFRAMES[timeframe]
    start, end = date_window(timeframe, count)
    raw = None
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            raw = yf.download(
                ticker,
                start=start.to_pydatetime(),
                end=end.to_pydatetime(),
                interval=cfg["interval"],
                progress=False,
                auto_adjust=False,
                threads=False,
            )
            if raw is not None and not raw.empty:
                break
        except Exception as exc:  # yfinance usa exceções diferentes por versão
            last_error = exc

        if attempt < 2:
            time.sleep(2**attempt)

    if raw is None or raw.empty:
        detail = f"\nDetalhe: {last_error}" if last_error else ""
        raise RuntimeError(
            f"O Yahoo não retornou dados de {ticker}. Pode haver rate limit. "
            f"Tente novamente em alguns minutos.{detail}"
        )

    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        try:
            df = df.xs(ticker, axis=1, level=-1)
        except KeyError:
            df.columns = df.columns.get_level_values(0)

    df.columns = [str(column).lower() for column in df.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Resposta do Yahoo sem colunas obrigatórias: {missing}")

    df = df[["open", "high", "low", "close", "volume"]].copy()
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    # O índice do Yahoo representa a abertura do candle.
    now = pd.Timestamp.now(tz="UTC")
    if not df.empty and now < df.index[-1] + cfg["duration"]:
        df = df.iloc[:-1]

    if df.empty:
        raise RuntimeError("Não há candle fechado disponível para análise.")

    return df.tail(count)


def compute_atr(
    df: pd.DataFrame,
    period: int = DEFAULT_PARAMS.atr_periodo,
    suavizacao: str = DEFAULT_PARAMS.atr_suavizacao,
) -> pd.Series:
    """
    ATR pelo método de Wilder — a mesma convenção do MetaTrader e do
    TradingView, que chamam esse suavizamento de SMMA/RMA.

    Até 10/08/2026 este motor usava média SIMPLES do True Range, o que
    dava 2-5% de divergência contra o ATR das plataformas. Parece pouco,
    mas o ATR define a distância mínima do stop (ver `stop_for_signal` e
    `alternative_targets`), então a diferença ia direto pro tamanho do
    risco de cada operação.

    `suavizacao="simples"` reproduz o comportamento antigo — serve pra
    comparar o histórico gravado antes da troca, não pra operar.
    """
    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    if suavizacao == "simples" or len(true_range) <= period:
        return true_range.rolling(period, min_periods=period).mean().bfill()

    # Mesma semente do IFR (ver `compute_rsi`): média simples dos
    # `period` primeiros valores posicionada no índice `period`, e daí
    # pra frente o suavizamento de Wilder. O primeiro True Range é NaN
    # (não há close anterior), então a média vai de 1 a `period`.
    seeded = true_range.copy()
    seeded.iloc[:period] = np.nan
    seeded.iloc[period] = true_range.iloc[1:period + 1].mean()
    return seeded.ewm(alpha=1 / period, adjust=False).mean().bfill()


def compute_rsi(df: pd.DataFrame, period: int = DEFAULT_PARAMS.rsi_periodo) -> pd.Series:
    """
    IFR (Índice de Força Relativa / RSI) pelo método de Wilder — o mesmo
    usado por padrão no MetaTrader, TradingView e Profit, pra que o
    número daqui bata com o que aparece no gráfico.

    O detalhe que faz toda a diferença, e é a fonte de erro mais comum
    nas implementações, é a SEMENTE. Wilder inicia com a média SIMPLES
    dos primeiros `period` ganhos/perdas e só a partir daí aplica o
    suavizamento (`avg = (avg_anterior * (n - 1) + atual) / n`).

    Um `ewm(adjust=False)` direto sobre a série inteira semeia o cálculo
    com o primeiro ganho ISOLADO em vez dessa média. O erro chega a ~20
    pontos no início e decai devagar ao longo da série — grande o
    bastante pra inverter uma leitura de exaustão, e discreto o bastante
    pra ninguém notar. É o bug que a auditoria do projeto original
    encontrou; a correção foi validada contra os dados de referência do
    próprio Wilder.
    """
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    if len(delta) <= period:
        return pd.Series(np.nan, index=df.index, dtype=float)

    # A semente vai posicionada no índice `period`; tudo antes vira NaN
    # (período de aquecimento). O `ewm` começa no primeiro valor
    # não-nulo, então daí pra frente ele reproduz exatamente a recursão
    # de Wilder.
    gain_seeded = gain.copy()
    loss_seeded = loss.copy()
    gain_seeded.iloc[:period] = np.nan
    loss_seeded.iloc[:period] = np.nan
    gain_seeded.iloc[period] = gain.iloc[1:period + 1].mean()
    loss_seeded.iloc[period] = loss.iloc[1:period + 1].mean()

    avg_gain = gain_seeded.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss_seeded.ewm(alpha=1 / period, adjust=False).mean()

    rsi = pd.Series(np.nan, index=df.index, dtype=float)
    valid = avg_gain.notna() & avg_loss.notna()

    # Três casos, explícitos pra não dividir por zero:
    #   perda 0 e ganho > 0 -> alta sem nenhuma queda: IFR = 100
    #   perda 0 e ganho 0   -> mercado parado: IFR = 50 (neutro, NÃO 100)
    #   caso normal         -> fórmula padrão
    sem_perda = valid & (avg_loss == 0)
    rsi[sem_perda & (avg_gain > 0)] = 100.0
    rsi[sem_perda & (avg_gain == 0)] = 50.0

    normal = valid & (avg_loss > 0)
    rs = avg_gain[normal] / avg_loss[normal]
    rsi[normal] = 100 - (100 / (1 + rs))

    return rsi


def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    result = pd.DataFrame(index=df.index)
    for period in (9, 21, 50, 200):
        result[f"ema_{period}"] = df["close"].ewm(
            span=period,
            adjust=False,
        ).mean()
    return result


def compute_daily_vwap(df: pd.DataFrame) -> pd.Series:
    local_index = df.index.tz_convert(LOCAL_TZ)
    session = pd.Series(local_index.date, index=df.index)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    price_volume = typical * df["volume"]
    cumulative_pv = price_volume.groupby(session).cumsum()
    cumulative_volume = df["volume"].groupby(session).cumsum()
    return cumulative_pv / cumulative_volume.replace(0, np.nan)


def slope_pct(series: pd.Series, lookback: int = 5) -> float:
    clean = series.dropna()
    if len(clean) < 2:
        return 0.0
    lookback = min(lookback, len(clean) - 1)
    old = float(clean.iloc[-1 - lookback])
    new = float(clean.iloc[-1])
    return (new - old) / abs(old) * 100 if old else 0.0


def detect_swings(
    df: pd.DataFrame,
    left: int = DEFAULT_PARAMS.swing_esquerda,
    right: int = DEFAULT_PARAMS.swing_direita,
) -> list[Swing]:
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    swings: list[Swing] = []

    for index in range(left, len(df) - right):
        high_window = highs[index - left : index + right + 1]
        low_window = lows[index - left : index + right + 1]

        if highs[index] == high_window.max() and (high_window == highs[index]).sum() == 1:
            swings.append(
                Swing(index, index + right, float(highs[index]), "HIGH")
            )
        if lows[index] == low_window.min() and (low_window == lows[index]).sum() == 1:
            swings.append(
                Swing(index, index + right, float(lows[index]), "LOW")
            )

    return sorted(swings, key=lambda swing: swing.index)


def detect_structure(
    df: pd.DataFrame,
    swings: list[Swing],
    atr_series: pd.Series,
    params: AnalysisParams = DEFAULT_PARAMS,
) -> list[StructureEvent]:
    volume_average = df["volume"].rolling(20, min_periods=5).mean()
    by_confirmation: dict[int, list[Swing]] = {}
    for swing in swings:
        by_confirmation.setdefault(swing.confirmed_index, []).append(swing)

    pending_high: Swing | None = None
    pending_low: Swing | None = None
    trend = Direction.NEUTRAL
    events: list[StructureEvent] = []

    for index in range(len(df)):
        for swing in by_confirmation.get(index, []):
            if swing.kind == "HIGH":
                pending_high = swing
            else:
                pending_low = swing

        candle = df.iloc[index]
        atr = float(atr_series.iloc[index]) or 1e-9
        volume_ma = float(volume_average.iloc[index])
        if not math.isfinite(volume_ma) or volume_ma <= 0:
            continue

        volume_ratio = float(candle["volume"]) / volume_ma
        range_ratio = float(candle["high"] - candle["low"]) / atr
        valid = (
            volume_ratio >= params.estrutura_volume_min
            and range_ratio >= params.estrutura_range_min
        )
        # Os divisores 2.4 e 1.6 continuam literais de propósito: são a
        # escala da confiança (0.5-1.0 quando válido), não o critério de
        # validade. Amarrá-los aos mínimos acima mudaria o significado do
        # número de confiança toda vez que alguém ajustasse o filtro.
        confidence = min(
            1.0,
            0.5 * min(1.0, volume_ratio / 2.4)
            + 0.5 * min(1.0, range_ratio / 1.6),
        )

        if pending_high and candle["close"] > pending_high.price and valid:
            kind = "CHOCH" if trend == Direction.SELL else "BOS"
            trend = Direction.BUY
            events.append(
                StructureEvent(
                    index,
                    kind,
                    Direction.BUY,
                    confidence,
                    pending_high.price,
                )
            )
            pending_high = None

        if pending_low and candle["close"] < pending_low.price and valid:
            kind = "CHOCH" if trend == Direction.BUY else "BOS"
            trend = Direction.SELL
            events.append(
                StructureEvent(
                    index,
                    kind,
                    Direction.SELL,
                    confidence,
                    pending_low.price,
                )
            )
            pending_low = None

    return events


def candle_patterns(df: pd.DataFrame, atr: float, volume_ma: float) -> list[str]:
    current = df.iloc[-1]
    previous = df.iloc[-2]
    candle_range = max(float(current["high"] - current["low"]), 1e-9)
    body = abs(float(current["close"] - current["open"]))
    upper_wick = float(current["high"] - max(current["open"], current["close"]))
    lower_wick = float(min(current["open"], current["close"]) - current["low"])
    patterns: list[str] = []

    # Filtro de ruído: um candle pequeno demais ou com volume na média
    # (ou pouco acima) não deveria virar um "padrão" decisivo — do
    # contrário, qualquer candle comum dentro de uma oscilação pequena já
    # conta como engolfo/força, fazendo o setup trocar a cada candle novo
    # sem nenhum movimento real acontecendo. Precisa ser um candle
    # CLARAMENTE fora do padrão recente, não só "na média ou acima".
    significant_size = atr > 0 and candle_range >= atr * 0.8
    significant_volume = volume_ma > 0 and float(current["volume"]) / volume_ma >= 1.3
    if not (significant_size and significant_volume):
        return patterns

    bullish = current["close"] > current["open"]
    bearish = current["close"] < current["open"]
    previous_bullish = previous["close"] > previous["open"]
    previous_bearish = previous["close"] < previous["open"]

    if (
        bullish
        and previous_bearish
        and current["close"] >= previous["open"]
        and current["open"] <= previous["close"]
    ):
        patterns.append("ENGOLFO_ALTA")
    if (
        bearish
        and previous_bullish
        and current["close"] <= previous["open"]
        and current["open"] >= previous["close"]
    ):
        patterns.append("ENGOLFO_BAIXA")
    if bullish and lower_wick / candle_range >= 0.6:
        patterns.append("PIN_BAR_ALTA")
    if bearish and upper_wick / candle_range >= 0.6:
        patterns.append("PIN_BAR_BAIXA")
    if body / candle_range >= 0.7:
        patterns.append("CANDLE_FORCA_ALTA" if bullish else "CANDLE_FORCA_BAIXA")
    if current["high"] <= previous["high"] and current["low"] >= previous["low"]:
        patterns.append("INSIDE_BAR")

    return patterns


def _confirmed_breakout(candle: pd.Series, level: float, direction: str, atr: float, volume_ma: float, margin_atr: float = 0.15) -> bool:
    """
    Um rompimento só conta se: (1) o fechamento passar do nível por uma
    margem mínima (em função do ATR) — não qualquer tick acima/abaixo —
    e (2) o candle tiver volume e amplitude acima do normal, no mesmo
    padrão já usado pra validar BOS/CHoCH no motor SMC. Sem isso, uma
    oscilação pequena perto do nível fica "rompendo e desrompendo" a
    cada candle, fazendo o setup girar sem parar.
    """
    if atr <= 0 or volume_ma <= 0:
        return False

    margin = atr * margin_atr
    volume_ratio = float(candle["volume"]) / volume_ma
    range_ratio = float(candle["high"] - candle["low"]) / atr
    confirmed = volume_ratio >= 1.1 and range_ratio >= 0.6

    if direction == "ALTA":
        return bool(candle["close"] > level + margin and confirmed)
    return bool(candle["close"] < level - margin and confirmed)


def breakout_and_retest(
    df: pd.DataFrame,
    atr: float,
    lookback: int = DEFAULT_PARAMS.rompimento_lookback,
    tolerance_pct: float = DEFAULT_PARAMS.rompimento_tolerancia_pct,
) -> tuple[bool, bool, bool, bool]:
    current = df.iloc[-1]
    reference = df.iloc[-(lookback + 1) : -1]
    high_level = float(reference["high"].max())
    low_level = float(reference["low"].min())

    volume_ma_series = df["volume"].rolling(20, min_periods=5).mean()
    current_volume_ma = float(volume_ma_series.iloc[-1])

    broke_high = _confirmed_breakout(current, high_level, "ALTA", atr, current_volume_ma)
    broke_low = _confirmed_breakout(current, low_level, "BAIXA", atr, current_volume_ma)
    bullish_retest = False
    bearish_retest = False
    tolerance = tolerance_pct / 100

    for index in range(max(1, len(df) - 6), len(df) - 1):
        prior = df.iloc[max(0, index - lookback) : index]
        if prior.empty:
            continue

        breakout = df.iloc[index]
        idx_atr = atr  # aproximação: usa o ATR atual pra todo o lookback recente, suficiente pra esse filtro
        idx_volume_ma = float(volume_ma_series.iloc[index]) if pd.notna(volume_ma_series.iloc[index]) else 0.0
        high_ref = float(prior["high"].max())
        low_ref = float(prior["low"].min())

        if _confirmed_breakout(breakout, high_ref, "ALTA", idx_atr, idx_volume_ma):
            near = abs(float(current["close"]) - high_ref) / high_ref <= tolerance
            touched = current["low"] <= high_ref * (1 + tolerance)
            bullish_retest |= bool(near and touched and current["close"] >= high_ref)

        if _confirmed_breakout(breakout, low_ref, "BAIXA", idx_atr, idx_volume_ma):
            near = abs(float(current["close"]) - low_ref) / low_ref <= tolerance
            touched = current["high"] >= low_ref * (1 - tolerance)
            bearish_retest |= bool(near and touched and current["close"] <= low_ref)

    return broke_high, broke_low, bullish_retest, bearish_retest


def detect_fvg_setup(df: pd.DataFrame, max_age: int = DEFAULT_PARAMS.fvg_max_idade) -> str | None:
    current_price = float(df["close"].iloc[-1])
    tolerance = current_price * 0.0015
    first = max(1, len(df) - max_age)

    for middle in range(len(df) - 2, first - 1, -1):
        candle_1 = df.iloc[middle - 1]
        candle_3 = df.iloc[middle + 1]

        if candle_1["high"] < candle_3["low"]:
            bottom = float(candle_1["high"])
            top = float(candle_3["low"])
            later_lows = df["low"].iloc[middle + 2 :]
            filled = bool((later_lows <= bottom).any())
            near = bottom - tolerance <= current_price <= top + tolerance
            if not filled and near:
                return "FVG_ALTA"

        if candle_1["low"] > candle_3["high"]:
            bottom = float(candle_3["high"])
            top = float(candle_1["low"])
            later_highs = df["high"].iloc[middle + 2 :]
            filled = bool((later_highs >= top).any())
            near = bottom - tolerance <= current_price <= top + tolerance
            if not filled and near:
                return "FVG_BAIXA"

    return None


def build_context(
    df: pd.DataFrame,
    params: AnalysisParams = DEFAULT_PARAMS,
    higher_rsi: float | None = None,
) -> MarketContext:
    if len(df) < 30:
        raise ValueError("São necessários pelo menos 30 candles fechados.")

    atr_series = compute_atr(df, params.atr_periodo, params.atr_suavizacao)
    atr = float(atr_series.iloc[-1])
    price = float(df["close"].iloc[-1])
    atr_pct = atr / price * 100 if price else 0.0
    rvol_series = df["volume"] / df["volume"].rolling(20, min_periods=5).mean()
    rvol = float(rvol_series.iloc[-1]) if pd.notna(rvol_series.iloc[-1]) else 0.0

    if atr_pct < params.vol_baixa_max_pct:
        volatility = "BAIXA"
    elif atr_pct > params.vol_excessiva_min_pct:
        volatility = "EXCESSIVA"
    else:
        volatility = "ADEQUADA"

    emas = compute_emas(df)
    vwap_series = compute_daily_vwap(df)
    local_dates = pd.Series(df.index.tz_convert(LOCAL_TZ).date, index=df.index)
    current_session_vwap = vwap_series[local_dates == local_dates.iloc[-1]]
    vwap = float(vwap_series.iloc[-1])
    vwap_slope = slope_pct(current_session_vwap)
    vwap_distance = (price - vwap) / vwap * 100 if vwap else 0.0
    candle = df.iloc[-1]
    touched_vwap = bool(candle["low"] <= vwap <= candle["high"])
    rejection = touched_vwap and abs(vwap_distance) >= 0.15

    # O IFR tem período de aquecimento (os `rsi_periodo` primeiros
    # candles são NaN por construção da semente de Wilder), então o
    # `dropna` antes de ler os dois últimos valores não é opcional.
    rsi_series = compute_rsi(df, params.rsi_periodo)
    rsi_clean = rsi_series.dropna()
    rsi_now = float(rsi_clean.iloc[-1]) if len(rsi_clean) >= 1 else 50.0
    rsi_before = float(rsi_clean.iloc[-2]) if len(rsi_clean) >= 2 else rsi_now

    volume_ma_current = float(df["volume"].rolling(20, min_periods=5).mean().iloc[-1])
    swings = detect_swings(df, params.swing_esquerda, params.swing_direita)
    events = detect_structure(df, swings, atr_series, params)
    broke_high, broke_low, bullish_retest, bearish_retest = breakout_and_retest(
        df,
        atr,
        params.rompimento_lookback,
        params.rompimento_tolerancia_pct,
    )

    return MarketContext(
        df=df,
        atr=atr,
        atr_pct=atr_pct,
        rvol=rvol,
        volatility=volatility,
        emas=emas,
        ema_reliable=len(df) >= 200,
        vwap_series=vwap_series,
        vwap=vwap,
        vwap_slope_pct=vwap_slope,
        vwap_distance_pct=vwap_distance,
        vwap_rejection=rejection,
        swings=swings,
        events=events,
        patterns=candle_patterns(df, atr, volume_ma_current),
        broke_high=broke_high,
        broke_low=broke_low,
        bullish_retest=bullish_retest,
        bearish_retest=bearish_retest,
        fvg_setup=detect_fvg_setup(df, params.fvg_max_idade),
        rsi=rsi_now,
        rsi_prev=rsi_before,
        rsi_series=rsi_series,
        higher_rsi=higher_rsi,
        params=params,
    )


QUALITY_LABELS = (
    "EVITAR",
    "BAIXA QUALIDADE",
    "MONITORAR",
    "BOA OPORTUNIDADE",
    "FORTE OPORTUNIDADE",
    "OPORTUNIDADE EXCEPCIONAL",
)


def quality(score: float, params: AnalysisParams = DEFAULT_PARAMS) -> str:
    for banda, rotulo in zip(params.bandas_qualidade, QUALITY_LABELS):
        if score < banda:
            return rotulo
    return QUALITY_LABELS[-1]


def market_alerts(context: MarketContext) -> list[str]:
    alerts = []
    if context.volatility == "BAIXA":
        alerts.append("VOLATILIDADE INSUFICIENTE — entrada bloqueada")
    if context.volatility == "EXCESSIVA":
        alerts.append("VOLATILIDADE EXCESSIVA — risco elevado")
    if abs(context.vwap_distance_pct) > context.params.vwap_distancia_max_pct:
        alerts.append("PREÇO MUITO DISTANTE DA VWAP — risco de entrada tardia")
    if not context.ema_reliable:
        alerts.append("EMA200 EM AQUECIMENTO — use 200 ou mais candles")
    return alerts


def apply_market_filter(
    direction: Direction,
    score: float,
    confidence: float,
    context: MarketContext,
    isolated: bool = False,
    block_entry: bool = False,
) -> tuple[Direction, float, float]:
    params = context.params
    if isolated:
        score = min(score, params.filtro_isolada_score_max)
        confidence = min(confidence, params.filtro_isolada_confianca_max)
    if context.volatility == "BAIXA" or block_entry:
        return (
            Direction.NEUTRAL,
            min(score, params.filtro_bloqueio_score_max),
            min(confidence, params.filtro_bloqueio_confianca_max),
        )
    if context.volatility == "EXCESSIVA":
        return (
            direction,
            min(score, params.filtro_excessiva_score_max),
            min(confidence, params.filtro_excessiva_confianca_max),
        )
    if score < params.score_minimo_operavel:
        return Direction.NEUTRAL, score, min(confidence, params.filtro_bloqueio_confianca_max)
    return direction, score, confidence


def last_recent_event(
    context: MarketContext,
    max_age: int | None = None,
) -> StructureEvent | None:
    if not context.events:
        return None
    if max_age is None:
        max_age = context.params.evento_max_idade
    event = context.events[-1]
    return event if len(context.df) - 1 - event.index <= max_age else None


def smc_signal(context: MarketContext) -> Signal:
    event = last_recent_event(context)
    reasons: list[str] = []
    direction = Direction.NEUTRAL
    score = 0.0
    setup = "Sem setup claro (SMC)"

    if event:
        direction = event.direction
        # Base recalibrada: um evento só é criado depois de passar pelo
        # filtro de volume/amplitude (valid=True), ou seja, já é um sinal
        # validado — por isso a confiança (0.5-1.0) modula um INTERVALO
        # acima do corte de neutralização, em vez de multiplicar direto
        # (o que fazia BOS quase sempre nascer abaixo de 40, mesmo
        # validado, e ficar neutralizado sem motivo real).
        base = 70.0 if event.kind == "CHOCH" else 55.0
        score = base + (event.confidence - 0.5) * 40.0
        score = min(score, 90.0)
        reasons.append(
            f"{event.kind} de {direction.value.lower()} confirmado "
            f"(confiança técnica {event.confidence:.0%})"
        )
        setup = (
            "Reversão de tendência (CHoCH)"
            if event.kind == "CHOCH"
            else "Continuação de tendência (BOS)"
        )
    else:
        reasons.append("Sem BOS/CHoCH recente e validado por volume/amplitude")

    if context.fvg_setup == "FVG_ALTA" and direction in (Direction.BUY, Direction.NEUTRAL):
        direction = Direction.BUY
        score += 20
        setup = "FVG + Retorno (alta)"
        reasons.append("Preço retornando a FVG de alta recente e ainda não preenchido")
    if context.fvg_setup == "FVG_BAIXA" and direction in (Direction.SELL, Direction.NEUTRAL):
        direction = Direction.SELL
        score += 20
        setup = "FVG + Retorno (baixa)"
        reasons.append("Preço retornando a FVG de baixa recente e ainda não preenchido")

    direction, score, confidence = apply_market_filter(
        direction,
        score,
        score * 0.8,
        context,
        isolated=True,
    )
    return Signal(
        "SMC",
        direction,
        score,
        confidence,
        setup if direction != Direction.NEUTRAL else "Sem setup operável (SMC)",
        reasons,
        market_alerts(context) + ["LEITURA ISOLADA — confirme com outras categorias"],
    )


def price_action_signal(context: MarketContext) -> Signal:
    bullish = {
        "ENGOLFO_ALTA",
        "PIN_BAR_ALTA",
        "CANDLE_FORCA_ALTA",
    }
    bearish = {
        "ENGOLFO_BAIXA",
        "PIN_BAR_BAIXA",
        "CANDLE_FORCA_BAIXA",
    }
    bull_points = 50.0 if bullish.intersection(context.patterns) else 0.0
    bear_points = 50.0 if bearish.intersection(context.patterns) else 0.0
    reasons: list[str] = []

    if context.broke_high:
        # Recalibrado de 30 para 45: um rompimento de máxima de 20
        # candles é, sozinho, um sinal válido de Price Action — não
        # deveria nascer abaixo do corte de neutralização só por faltar
        # também um padrão de candle no mesmo candle.
        bull_points += 45
        reasons.append(f"Rompimento da máxima dos últimos {context.params.rompimento_lookback} candles")
    if context.broke_low:
        bear_points += 45
        reasons.append(f"Rompimento da mínima dos últimos {context.params.rompimento_lookback} candles")
    if context.bullish_retest:
        bull_points += 20
        reasons.append("Reteste de alta sustentado")
    if context.bearish_retest:
        bear_points += 20
        reasons.append("Reteste de baixa sustentado")
    if context.patterns:
        reasons.append("Padrões: " + ", ".join(context.patterns))
    if not reasons:
        reasons.append("Sem padrão relevante no candle fechado")

    if abs(bull_points - bear_points) < 1.5:
        direction = Direction.NEUTRAL
        score = max(bull_points, bear_points) * 0.5
    elif bull_points > bear_points:
        direction, score = Direction.BUY, bull_points
    else:
        direction, score = Direction.SELL, bear_points

    if direction == Direction.BUY and context.bullish_retest:
        setup = "Rompimento + Reteste (alta)"
    elif direction == Direction.SELL and context.bearish_retest:
        setup = "Rompimento + Reteste (baixa)"
    elif direction == Direction.BUY and context.broke_high:
        setup = "Rompimento de máxima"
    elif direction == Direction.SELL and context.broke_low:
        setup = "Rompimento de mínima"
    else:
        setup = "Padrão de candle"

    direction, score, confidence = apply_market_filter(
        direction,
        score,
        score * 0.7,
        context,
        isolated=True,
    )
    return Signal(
        "Price Action",
        direction,
        score,
        confidence,
        setup if direction != Direction.NEUTRAL else "Sem setup operável (Price Action)",
        reasons,
        market_alerts(context) + ["LEITURA ISOLADA — confirme com outras categorias"],
    )


def moving_average_signal(context: MarketContext) -> Signal:
    ema = context.emas.iloc[-1]
    ema9_slope = slope_pct(context.emas["ema_9"])
    ema21_slope = slope_pct(context.emas["ema_21"])
    price = float(context.df["close"].iloc[-1])
    reasons: list[str] = []

    # Filtro de ruído: a distância entre EMA9/EMA21 e a inclinação de
    # cada uma precisam superar um mínimo relativo ao preço pra contar
    # como alinhamento real. Sem isso, uma diferença de milésimos de %
    # (um empate técnico) já virava COMPRA/VENDA com confiança máxima,
    # fazendo o setup girar a cada candle numa oscilação pequena.
    min_gap_pct = 0.05
    min_slope_pct = 0.02
    ema_gap_pct = (ema["ema_9"] - ema["ema_21"]) / price * 100 if price else 0.0
    meaningful_gap = abs(ema_gap_pct) >= min_gap_pct
    ema9_above = ema_gap_pct > 0

    bullish = meaningful_gap and ema9_above and ema9_slope > min_slope_pct and ema21_slope > min_slope_pct
    bearish = meaningful_gap and not ema9_above and ema9_slope < -min_slope_pct and ema21_slope < -min_slope_pct

    if bullish:
        direction, score = Direction.BUY, 100.0
        setup = "EMA9 > EMA21 com inclinação positiva"
        reasons.append("EMA9 e EMA21 alinhadas para alta")
    elif bearish:
        direction, score = Direction.SELL, 100.0
        setup = "EMA9 < EMA21 com inclinação negativa"
        reasons.append("EMA9 e EMA21 alinhadas para baixa")
    elif meaningful_gap and ema9_above:
        direction, score = Direction.BUY, 40.0
        setup = "Alinhamento parcial de alta"
        reasons.append("EMA9 acima da EMA21, mas sem inclinação completa")
    elif meaningful_gap:
        direction, score = Direction.SELL, 40.0
        setup = "Alinhamento parcial de baixa"
        reasons.append("EMA9 abaixo da EMA21, mas sem inclinação completa")
    else:
        direction, score = Direction.NEUTRAL, 15.0
        setup = "EMA9 e EMA21 praticamente coladas — sem alinhamento claro"
        reasons.append(f"Diferença entre EMA9 e EMA21 é insignificante ({ema_gap_pct:+.3f}%)")

    if context.ema_reliable:
        long_context_matches = (
            direction == Direction.BUY and price > ema["ema_200"]
        ) or (
            direction == Direction.SELL and price < ema["ema_200"]
        )
        if not long_context_matches:
            score *= 0.65
            reasons.append("Direção curta contraria a EMA200")

    direction, score, confidence = apply_market_filter(
        direction,
        score,
        score * 0.7,
        context,
        isolated=True,
    )
    return Signal(
        "Médias Móveis",
        direction,
        score,
        confidence,
        setup if direction != Direction.NEUTRAL else "Sem setup operável (Médias)",
        reasons,
        market_alerts(context) + ["LEITURA ISOLADA — confirme com outras categorias"],
    )


def vwap_signal(context: MarketContext) -> Signal:
    reasons: list[str] = []

    # Mesmo filtro de ruído das Médias: distância e inclinação da VWAP
    # precisam superar um mínimo pra contar como "acima/abaixo" ou
    # "subindo/descendo" de verdade — um preço a 0,007% da VWAP está,
    # na prática, EM CIMA dela, não acima nem abaixo.
    min_distance_pct = context.params.vwap_distancia_min_pct
    min_slope_pct = 0.02
    meaningful_distance = abs(context.vwap_distance_pct) >= min_distance_pct
    above = context.vwap_distance_pct > 0
    rising = context.vwap_slope_pct > min_slope_pct
    falling = context.vwap_slope_pct < -min_slope_pct

    if not meaningful_distance:
        direction, score = Direction.NEUTRAL, 12.0
        setup = "Preço colado na VWAP — sem definição"
        reasons.append(f"Distância até a VWAP é insignificante ({context.vwap_distance_pct:+.3f}%)")
    elif above and rising:
        direction, score = Direction.BUY, 85.0 if context.vwap_rejection else 70.0
        setup = "Pullback/rejeição na VWAP" if context.vwap_rejection else "Acima da VWAP ascendente"
        reasons.append("Preço acima da VWAP, com VWAP inclinada para cima")
    elif not above and falling:
        direction, score = Direction.SELL, 85.0 if context.vwap_rejection else 70.0
        setup = "Pullback/rejeição na VWAP" if context.vwap_rejection else "Abaixo da VWAP descendente"
        reasons.append("Preço abaixo da VWAP, com VWAP inclinada para baixo")
    elif above:
        direction, score = Direction.BUY, 30.0
        setup = "Acima da VWAP sem inclinação"
        reasons.append("Preço acima da VWAP, mas sem inclinação favorável")
    else:
        direction, score = Direction.SELL, 30.0
        setup = "Abaixo da VWAP sem inclinação"
        reasons.append("Preço abaixo da VWAP, mas sem inclinação favorável")

    too_far = abs(context.vwap_distance_pct) > context.params.vwap_distancia_max_pct
    direction, score, confidence = apply_market_filter(
        direction,
        score,
        score * 0.7,
        context,
        isolated=True,
        block_entry=too_far,
    )
    return Signal(
        "VWAP",
        direction,
        score,
        confidence,
        setup if direction != Direction.NEUTRAL else "Sem setup operável (VWAP)",
        reasons,
        market_alerts(context) + ["LEITURA ISOLADA — confirme com outras categorias"],
    )


def rsi_signal(context: MarketContext) -> Signal:
    """
    Leitura de IFR por EXAUSTÃO. A regra é única e binária:

        IFR <= rsi_sobrevenda   (padrão 10)  ->  COMPRA
        IFR >= rsi_sobrecompra  (padrão 90)  ->  VENDA
        no meio                              ->  NEUTRO

    De propósito não existe gradação de "aproximando da zona" nem
    "saindo da zona". Exaustão é o extremo — ou o preço está exaurido,
    ou o IFR não tem nada a dizer. Uma leitura que pontua fora do
    extremo descaracteriza o indicador: vira mais um seguidor de
    tendência, que é justamente o que as outras quatro categorias já
    fazem, e o voto dela na Confluência deixa de acrescentar informação.

    O IFR do timeframe superior (Diário), quando disponível, entra como
    reforço — exaustão simultânea nos dois prazos é a leitura de maior
    convicção que este motor produz. Quando os dois prazos apontam pra
    lados opostos, isso vira alerta, não cancelamento: quem decide o
    peso disso é a Confluência.
    """
    params = context.params
    rsi = context.rsi
    sobrevenda, sobrecompra = params.rsi_sobrevenda, params.rsi_sobrecompra
    reasons: list[str] = []

    # 100.0 e não o teto direto: quem capa a leitura isolada é o
    # `apply_market_filter` lá embaixo, com `filtro_isolada_score_max`.
    # Cravar 79 aqui ignoraria um perfil que tivesse mudado esse teto.
    if rsi <= sobrevenda:
        direction, score = Direction.BUY, 100.0
        setup = f"EXAUSTÃO VENDEDORA — IFR ≤ {sobrevenda:.0f}"
        reasons.append(
            f"IFR em {rsi:.1f}, abaixo de {sobrevenda:.0f} — vendedores exauridos, "
            "entrada a favor da reversão"
        )
    elif rsi >= sobrecompra:
        direction, score = Direction.SELL, 100.0
        setup = f"EXAUSTÃO COMPRADORA — IFR ≥ {sobrecompra:.0f}"
        reasons.append(
            f"IFR em {rsi:.1f}, acima de {sobrecompra:.0f} — compradores exauridos, "
            "entrada a favor da reversão"
        )
    else:
        direction, score = Direction.NEUTRAL, 0.0
        setup = "Sem exaustão"
        reasons.append(
            f"IFR em {rsi:.1f} — fora das zonas de exaustão "
            f"({sobrevenda:.0f}/{sobrecompra:.0f})"
        )

    # --- Reforço (ou desconto) pelo timeframe superior (Diário) ---
    # Prazos opostos não zeram a leitura, descontam: o extremo do
    # próprio timeframe continua existindo, só perde convicção.
    if context.higher_rsi is not None and direction != Direction.NEUTRAL:
        d_rsi = context.higher_rsi
        if direction == Direction.BUY:
            if d_rsi <= sobrevenda:
                reasons.append(f"Diário TAMBÉM exaurido na venda (IFR {d_rsi:.1f}) — convicção máxima")
            elif d_rsi >= sobrecompra:
                score *= 0.55
                reasons.append(f"ATENÇÃO: Diário exaurido na COMPRA (IFR {d_rsi:.1f}) — sinais opostos entre prazos")
        else:  # SELL
            if d_rsi >= sobrecompra:
                reasons.append(f"Diário TAMBÉM exaurido na compra (IFR {d_rsi:.1f}) — convicção máxima")
            elif d_rsi <= sobrevenda:
                score *= 0.55
                reasons.append(f"ATENÇÃO: Diário exaurido na VENDA (IFR {d_rsi:.1f}) — sinais opostos entre prazos")

    direction, score, confidence = apply_market_filter(
        direction,
        score,
        score * 0.7,
        context,
        isolated=True,
    )
    return Signal(
        "IFR",
        direction,
        score,
        confidence,
        setup,
        reasons,
        market_alerts(context) + ["LEITURA ISOLADA — confirme com outras categorias"],
    )


def confluence_signal(
    context: MarketContext,
    isolated: list[Signal],
) -> Signal:
    params = context.params
    weights = {
        "SMC": params.peso_smc,
        "Price Action": params.peso_price_action,
        "Médias Móveis": params.peso_medias,
        "VWAP": params.peso_vwap,
    }
    buy = 0.0
    sell = 0.0
    agreeing = 0
    reasons: list[str] = []

    for signal in isolated:
        normalized_strength = min(signal.score / params.normalizacao_score, 1.0)
        points = weights[signal.name] * normalized_strength
        if signal.direction == Direction.BUY:
            buy += points
        elif signal.direction == Direction.SELL:
            sell += points
        reasons.extend(f"{signal.name}: {reason}" for reason in signal.reasons[:1])

    if context.volatility == "ADEQUADA":
        buy += 10
        sell += 10
        reasons.append("Volatilidade adequada para o ativo/timeframe")

    if abs(buy - sell) < params.confluencia_banda_empate:
        direction = Direction.NEUTRAL
        score = max(buy, sell) * 0.5
    elif buy > sell:
        direction, score = Direction.BUY, buy
    else:
        direction, score = Direction.SELL, sell

    agreeing = sum(signal.direction == direction for signal in isolated)
    # O multiplicador de "2 categorias concordando" foi recalibrado de 0.85
    # para 0.95: com 0.85, o teto matemático desse cenário (quando só
    # Médias+VWAP concordam, por exemplo) ficava a poucos pontos do corte
    # de operabilidade (40) — na prática, quase nenhum sinal real de 2
    # categorias conseguia passar, mesmo com concordância forte. 0.95 dá
    # margem real sem abrir mão do critério (ainda exige concordância
    # genuína e pontuação consistente das 2 categorias).
    multiplier = params.multiplicador_concordancia[agreeing]
    score = min(100.0, score * multiplier)
    # Sobre `len(isolated)`, não sobre um 4 cravado: o número de leituras
    # isoladas já mudou uma vez (quatro até a entrada do IFR) e o
    # denominador fixo teria passado a reportar 125% de confiança na
    # unanimidade, silenciosamente.
    confidence = agreeing / len(isolated) * 100 if isolated else 0.0

    direction, score, confidence = apply_market_filter(
        direction,
        score,
        confidence,
        context,
    )

    event = last_recent_event(context)
    if direction == Direction.NEUTRAL:
        setup = "Sem setup operável"
    elif event and event.kind == "CHOCH" and event.direction == direction:
        setup = "Reversão de tendência (CHoCH)"
    elif (
        direction == Direction.BUY and context.bullish_retest
    ) or (
        direction == Direction.SELL and context.bearish_retest
    ):
        setup = "Rompimento + Reteste"
    elif context.vwap_rejection:
        setup = "Pullback na VWAP"
    elif context.fvg_setup:
        setup = "FVG + Retorno"
    elif event and event.direction == direction:
        setup = "Continuação de tendência (BOS)"
    else:
        setup = "Confluência técnica"

    alerts = market_alerts(context)
    if agreeing < 3:
        alerts.append("SINAIS CONFLITANTES — baixa confluência")

    return Signal(
        "Confluência",
        direction,
        score,
        confidence,
        setup,
        reasons,
        alerts,
    )


def round_tick(price: float, mode: str, tick: float = 0.01) -> float:
    scaled = price / tick
    if mode == "floor":
        units = math.floor(scaled + 1e-12)
    elif mode == "ceil":
        units = math.ceil(scaled - 1e-12)
    else:
        units = math.floor(scaled + 0.5)
    return round(units * tick, 2)


def structural_stop(context: MarketContext, direction: Direction) -> tuple[float, str]:
    price = float(context.df["close"].iloc[-1])
    if direction == Direction.BUY:
        lows = [swing.price for swing in context.swings if swing.kind == "LOW" and swing.price < price]
        if lows:
            return max(lows) - context.atr * 0.2, "swing_low"
        return price - context.atr * 1.2, "ATR"

    highs = [swing.price for swing in context.swings if swing.kind == "HIGH" and swing.price > price]
    if highs:
        return min(highs) + context.atr * 0.2, "swing_high"
    return price + context.atr * 1.2, "ATR"


def stop_for_signal(
    signal: Signal,
    context: MarketContext,
) -> tuple[float, str]:
    price = float(context.df["close"].iloc[-1])
    direction = signal.direction

    if signal.name in ("Confluência", "SMC"):
        return structural_stop(context, direction)

    if signal.name == "Price Action":
        window = context.df.iloc[-10:]
        if direction == Direction.BUY:
            return float(window["low"].min()) - context.atr * 0.15, "mínima_10_candles"
        return float(window["high"].max()) + context.atr * 0.15, "máxima_10_candles"

    if signal.name == "Médias Móveis":
        ema = context.emas.iloc[-1]
        if direction == Direction.BUY:
            base = float(ema["ema_21"] if ema["ema_21"] < price else ema["ema_50"])
            return base - context.atr * 0.3, "EMA21/EMA50"
        base = float(ema["ema_21"] if ema["ema_21"] > price else ema["ema_50"])
        return base + context.atr * 0.3, "EMA21/EMA50"

    if signal.name == "IFR":
        # O IFR não tem nível de preço próprio: é leitura de momentum, não
        # de estrutura. Sem este ramo ele cairia no fallback da VWAP logo
        # abaixo e sairia com um stop ancorado num nível que a leitura
        # nunca consultou — ainda por cima numa entrada contrária, onde o
        # preço costuma estar longe da VWAP justamente por estar exaurido.
        # ATR puro é o honesto aqui: distância por volatilidade, sem fingir
        # estrutura que não existe.
        if direction == Direction.BUY:
            return price - context.atr * 1.2, "ATR"
        return price + context.atr * 1.2, "ATR"

    if direction == Direction.BUY:
        return context.vwap - context.atr * 0.5, "VWAP"
    return context.vwap + context.atr * 0.5, "VWAP"


def alternative_targets(
    context: MarketContext,
    direction: Direction,
    entry: float,
    stop: float,
) -> list[dict]:
    risk = abs(entry - stop)
    if risk <= 0:
        return []

    project = lambda distance: (
        entry + distance if direction == Direction.BUY else entry - distance
    )
    params = context.params
    # O corte de viabilidade dos alvos alternativos é o próprio R/R do
    # alvo 1: um alvo alternativo só é "viável" se render pelo menos
    # tanto quanto o alvo padrão. Antes era um 1.5 literal, idêntico por
    # coincidência — amarrado, continua coerente se o R/R for ajustado.
    minimo_viavel = params.rr_alvo_1
    targets = [
        {
            "method": f"Risco/Retorno 1:{params.rr_alvo_1:.1f}",
            "price": project(risk * params.rr_alvo_1),
            "rr": params.rr_alvo_1,
            "viable": True,
        },
        {
            "method": f"Risco/Retorno 1:{params.rr_alvo_2:.1f}",
            "price": project(risk * params.rr_alvo_2),
            "rr": params.rr_alvo_2,
            "viable": True,
        },
    ]

    if len(context.swings) >= 2:
        leg = abs(context.swings[-1].price - context.swings[-2].price)
        for ratio in (1.272, 1.618):
            price = project(leg * ratio)
            rr = abs(price - entry) / risk
            targets.append(
                {
                    "method": f"Fibonacci {ratio:.3f}",
                    "price": price,
                    "rr": rr,
                    "viable": rr >= minimo_viavel,
                }
            )

    if direction == Direction.BUY:
        structures = [
            swing.price
            for swing in context.swings
            if swing.kind == "HIGH" and swing.price > entry
        ]
        structure = min(structures) - context.atr * 0.1 if structures else None
    else:
        structures = [
            swing.price
            for swing in context.swings
            if swing.kind == "LOW" and swing.price < entry
        ]
        structure = max(structures) + context.atr * 0.1 if structures else None

    if structure is not None:
        rr = abs(structure - entry) / risk
        targets.append(
            {
                "method": "Próxima estrutura (SMC)",
                "price": structure,
                "rr": rr,
                "viable": rr >= minimo_viavel,
            }
        )

    recent_swings = context.swings[-7:]
    legs = [
        abs(recent_swings[index].price - recent_swings[index - 1].price)
        for index in range(1, len(recent_swings))
    ]
    if legs:
        expected = statistics.median(legs)
        price = project(expected)
        rr = expected / risk
        targets.append(
            {
                "method": "Expectativa estatística",
                "price": price,
                "rr": rr,
                "viable": rr >= minimo_viavel,
            }
        )

    return targets


def attach_risk(signal: Signal, context: MarketContext) -> None:
    if signal.direction == Direction.NEUTRAL:
        return

    params = context.params
    entry = round_tick(float(context.df["close"].iloc[-1]), "nearest")
    stop, basis = stop_for_signal(signal, context)
    minimum_distance = context.atr * params.stop_minimo_atr
    minimo_label = f"+mínimo_{params.stop_minimo_atr:g}ATR"

    if signal.direction == Direction.BUY:
        if entry - stop < minimum_distance:
            stop = entry - minimum_distance
            basis += minimo_label
        stop = round_tick(stop, "floor")
        risk = entry - stop
        if risk <= 0:
            signal.direction = Direction.NEUTRAL
            return
        target_1 = round_tick(entry + risk * params.rr_alvo_1, "ceil")
        target_2 = round_tick(entry + risk * params.rr_alvo_2, "ceil")
    else:
        if stop - entry < minimum_distance:
            stop = entry + minimum_distance
            basis += minimo_label
        stop = round_tick(stop, "ceil")
        risk = stop - entry
        if risk <= 0:
            signal.direction = Direction.NEUTRAL
            return
        target_1 = round_tick(entry - risk * params.rr_alvo_1, "floor")
        target_2 = round_tick(entry - risk * params.rr_alvo_2, "floor")

    alternatives = alternative_targets(
        context,
        signal.direction,
        entry,
        stop,
    )
    signal.risk = RiskPlan(
        entry,
        stop,
        target_1,
        target_2,
        abs(target_1 - entry) / risk,
        basis,
        alternatives,
    )

    structure = next(
        (
            target
            for target in alternatives
            if target["method"] == "Próxima estrutura (SMC)"
        ),
        None,
    )
    if structure and not structure["viable"]:
        signal.alerts.append(
            "ESPAÇO INSUFICIENTE ATÉ A PRÓXIMA ESTRUTURA — "
            f"R/R 1:{structure['rr']:.2f}"
        )


def analyze(
    df: pd.DataFrame,
    params: AnalysisParams = DEFAULT_PARAMS,
    higher_rsi: float | None = None,
) -> tuple[MarketContext, list[Signal]]:
    """Roda o motor inteiro sobre UM timeframe.

    `higher_rsi` é o IFR do Diário, usado pelo `rsi_signal` como filtro
    de contexto. Quem analisa vários timeframes (`analyze_symbol_mtf`)
    passa esse valor; quem analisa um só — a verificação retroativa
    (`check_signal_as_of`) e o backfill do worker — deixa em None, e aí
    a leitura de IFR opera apenas com o próprio prazo. É por isso que a
    verificação retroativa pode divergir levemente da leitura ao vivo
    na modalidade IFR: ao vivo ela tem o Diário, retroativa não.

    O IFR entra como leitura própria, mas FORA da confluência — só as
    quatro categorias estruturais alimentam `confluence_signal`. O
    motivo está no comentário dos pesos, em `AnalysisParams`: as quatro
    leem estrutura e tendência, o IFR lê exaustão e é contrário por
    natureza. Como ele fica NEUTRO quase sempre, somá-lo ali diluía
    todo score de confluência sem acrescentar informação — e quebrava
    a comparação com o histórico já gravado nessa modalidade. Por isso
    a ordem aqui importa: a confluência é calculada ANTES de o IFR ser
    anexado à lista.
    """
    context = build_context(df, params, higher_rsi=higher_rsi)
    isolated = [
        smc_signal(context),
        price_action_signal(context),
        moving_average_signal(context),
        vwap_signal(context),
    ]
    confluence = confluence_signal(context, isolated)
    signals = [confluence, *isolated, rsi_signal(context)]

    for signal in signals:
        attach_risk(signal, context)

    return context, signals


@dataclass
class TimeframeResult:
    timeframe: str
    context: MarketContext | None
    signals: list[Signal] | None
    error: str | None


@dataclass
class MultiTimeframeResult:
    symbol: str
    results: dict[str, TimeframeResult]  # chaves: "M15", "H1", "H4", "D1", "W1"
    modality: str                  # qual leitura foi usada pra confirmação: Confluência, SMC, Price Action, Médias Móveis ou VWAP
    confirmed: bool               # True só se os dois timeframes de confirmação concordarem na mesma direção (nessa leitura)
    confirmed_direction: Direction


MODALITIES = ("Confluência", "SMC", "Price Action", "Médias Móveis", "VWAP", "IFR")
ALL_MODALITIES_OPTION = "Todas as modalidades"
MODALITY_CHOICES = (ALL_MODALITIES_OPTION, *MODALITIES)


def mtf_confirmation(
    signals_por_tf: dict[str, list[Signal] | None],
    confirmation: tuple[str, str],
    modality: str,
) -> tuple[bool, Direction]:
    """Confirmação multi-timeframe DE UMA modalidade específica.

    A confirmação é uma propriedade do par (timeframes de confirmação,
    modalidade) — não do símbolo. Carimbar a confirmação da Confluência
    numa linha de SMC diria que o SMC foi confirmado quando quem
    concordou foi outra leitura, e o recorte "confirmado no MTF" da
    assertividade passaria a medir a coisa errada.

    Existe separado de `analyze_symbol_mtf` porque tanto a interface
    quanto o worker precisam calcular isso pras SEIS modalidades a
    partir de um mesmo conjunto de resultados, sem reanalisar nada.
    """
    tf_a, tf_b = confirmation
    dir_a = _signal_direction(signals_por_tf.get(tf_a), modality)
    dir_b = _signal_direction(signals_por_tf.get(tf_b), modality)
    confirmado = bool(dir_a and dir_b and dir_a == dir_b and dir_a != Direction.NEUTRAL)
    return confirmado, dir_a if confirmado and dir_a is not None else Direction.NEUTRAL


# Leituras que NÃO entram no agregado "Todas as modalidades". O IFR está
# aqui pelo mesmo motivo que está fora da confluência (ver `analyze`), e
# aqui o efeito era ainda pior: como `overall_direction` exige maioria
# ABSOLUTA, uma sexta leitura que é NEUTRO por desenho nunca compõe a
# maioria e só levanta a régua — de 3-de-5 pra 4-de-6. Medido nas séries
# reais, isso virava NEUTRO em 9 de 20 casos e derrubava o Score Geral em
# ~6 pontos, sem ninguém ter decidido endurecer o critério. O IFR segue
# como modalidade selecionável e como coluna do Scanner; ele só não vota
# no agregado nem entra na média.
MODALIDADES_FORA_DO_AGREGADO = frozenset({"IFR"})


def _agregaveis(signals: list[Signal]) -> list[Signal]:
    return [s for s in signals if s.name not in MODALIDADES_FORA_DO_AGREGADO]


def overall_score(signals: list[Signal]) -> float:
    """Score geral: média do score das 5 leituras agregáveis (Confluência, SMC, Price Action, Médias Móveis, VWAP) neste timeframe. O IFR fica fora — ver `MODALIDADES_FORA_DO_AGREGADO`."""
    agregaveis = _agregaveis(signals)
    if not agregaveis:
        return 0.0
    return sum(s.score for s in agregaveis) / len(agregaveis)


def overall_direction(signals: list[Signal]) -> Direction:
    """
    Direção geral: exige MAIORIA CLARA entre as 5 leituras agregáveis
    (pelo menos 3 de 5 apontando pra mesma direção), não só "mais compra
    que venda" entre poucas leituras não-neutras. Isso evita que 1
    leitura isolada decida a direção geral enquanto as outras 4 estão
    caladas (NEUTRO) — nesse caso o correto é permanecer NEUTRO, não
    declarar vencedor por W.O.

    O IFR não entra na conta (ver `MODALIDADES_FORA_DO_AGREGADO`): uma
    leitura que é NEUTRO por desenho não pode endurecer o critério das
    outras só por existir.
    """
    signals = _agregaveis(signals)
    total = len(signals)
    if total == 0:
        return Direction.NEUTRAL
    buy = sum(1 for s in signals if s.direction == Direction.BUY)
    sell = sum(1 for s in signals if s.direction == Direction.SELL)
    minimo = (total // 2) + 1  # maioria absoluta: 3 de 5, 3 de 4, etc.
    if buy >= minimo and buy > sell:
        return Direction.BUY
    if sell >= minimo and sell > buy:
        return Direction.SELL
    return Direction.NEUTRAL


def overall_agreement(signals: list[Signal]) -> tuple[int, int]:
    """Quantas das leituras concordam com a direção geral, e o total avaliado — pra exibir tipo '3 de 5 leituras concordam'.

    Filtra pelo mesmo critério de `overall_direction`. Sem isso a tela
    diria "3 de 6" ao lado de uma direção decidida por uma regra de 3
    de 5 — o número exibido não bateria com o número que decidiu."""
    direction = overall_direction(signals)
    signals = _agregaveis(signals)
    total = len(signals)
    if direction == Direction.NEUTRAL:
        buy = sum(1 for s in signals if s.direction == Direction.BUY)
        sell = sum(1 for s in signals if s.direction == Direction.SELL)
        return max(buy, sell), total
    agreeing = sum(1 for s in signals if s.direction == direction)
    return agreeing, total


def rsi_extremes_across_timeframes(
    mtf: "MultiTimeframeResult",
    params: AnalysisParams = DEFAULT_PARAMS,
) -> dict:
    """
    Consolida o IFR de TODOS os timeframes analisados e diz onde há
    extremo. Em Day Trade isso cobre M15 e H1 (mais H4/D1 de contexto);
    em Swing, D1/W1/H4.

    Devolve:
      - `por_tf`: {timeframe: (valor_ifr, "COMPRA"/"VENDA"/None)}
      - `extremos_compra` / `extremos_venda`: listas de timeframes
      - `alinhamento`: quantos timeframes estão em extremo na MESMA
        direção (0 se não houver nenhum)
      - `direcao`: direção do alinhamento, ou None

    O `alinhamento` é o filtro forte: dois ou mais timeframes em
    exaustão simultânea na mesma direção é bem mais raro — e bem mais
    significativo — do que um isolado.

    Recebe `params` em vez de ler limiares de variável de módulo porque
    os limiares são por PERFIL: dois perfis podem discordar sobre o que
    conta como exaustão, e um global faria o perfil que rodou por
    último decidir pelos outros.
    """
    por_tf: dict[str, tuple[float, str | None]] = {}
    compra: list[str] = []
    venda: list[str] = []

    for tf, resultado in mtf.results.items():
        if resultado.context is None:
            continue
        valor = resultado.context.rsi
        if valor <= params.rsi_sobrevenda:
            por_tf[tf] = (valor, Direction.BUY.value)
            compra.append(tf)
        elif valor >= params.rsi_sobrecompra:
            por_tf[tf] = (valor, Direction.SELL.value)
            venda.append(tf)
        else:
            por_tf[tf] = (valor, None)

    if len(compra) > len(venda):
        alinhamento, direcao = len(compra), Direction.BUY.value
    elif len(venda) > len(compra):
        alinhamento, direcao = len(venda), Direction.SELL.value
    else:
        # Empate (inclusive 0 x 0) não configura alinhamento: extremos
        # opostos em timeframes diferentes se anulam, não se somam.
        alinhamento, direcao = 0, None

    return {
        "por_tf": por_tf,
        "extremos_compra": compra,
        "extremos_venda": venda,
        "alinhamento": alinhamento,
        "direcao": direcao,
    }


def _signal_direction(signals: list[Signal] | None, modality: str = "Confluência") -> Direction | None:
    if not signals:
        return None
    if modality == ALL_MODALITIES_OPTION:
        return overall_direction(signals)
    return next((s.direction for s in signals if s.name == modality), None)


def _signal_score(signals: list[Signal] | None, modality: str = "Confluência") -> float | None:
    if not signals:
        return None
    if modality == ALL_MODALITIES_OPTION:
        return overall_score(signals)
    sig = next((s for s in signals if s.name == modality), None)
    return sig.score if sig else None


DEFAULT_TF_COUNTS = {"M2": 300, "M5": 300, "M15": 250, "H1": 250, "H4": 150, "D1": 250, "W1": 150}


@dataclass
class RetroSignalCheck:
    timeframe: str
    as_of: pd.Timestamp
    direction: Direction
    setup: str
    score: float
    risk: RiskPlan
    outcome: str          # "ALVO_1", "ALVO_2", "STOP", "EM_ABERTO", "SEM_SINAL", "SEM_ENTRADA", "SEM_DADO_FUTURO"
    outcome_detail: str
    candles_ate_resultado: int | None
    candles_futuros_disponiveis: int
    preco_fill: float | None = None
    r_realizado: float | None = None


# Custo de ida e volta como fração do valor negociado. 0.05% cobre
# corretagem diluída + emolumentos da B3 + um slippage modesto num ativo
# líquido. É o DEFAULT da medição, não uma verdade: quem opera com
# corretagem fixa alta em lote pequeno paga muito mais, e isso muda o
# resultado — por isso é parâmetro, e por isso o número aparece junto da
# taxa de acerto em vez de ficar embutido em silêncio.
CUSTO_ROUND_TRIP_PADRAO = 0.0005


@dataclass(frozen=True)
class SignalOutcome:
    """Desfecho de um sinal, com o preço que a operação REALMENTE teria tido."""
    resultado: str            # ALVO_1 | ALVO_2 | STOP | EM_ABERTO | SEM_SINAL | SEM_ENTRADA
    detalhe: str
    candles_ate_resultado: int | None
    preco_fill: float | None  # abertura da vela seguinte — o preço de execução
    r_realizado: float | None # R líquido de custo; None enquanto não resolveu


def evaluate_signal_outcome(
    risk: RiskPlan,
    direction: Direction,
    future_df: pd.DataFrame,
    custo_round_trip: float = CUSTO_ROUND_TRIP_PADRAO,
) -> SignalOutcome:
    """
    Caminha candle a candle pelos dados REAIS que vieram depois do sinal e
    verifica o que aconteceu primeiro: stop, alvo 1, alvo 2, ou nada ainda.

    EXECUÇÃO — a parte que mudou em 2026-08-06, e por quê:

    `risk.entry` é o FECHAMENTO da vela que gerou o sinal (ver `attach_risk`).
    Até aqui a avaliação assumia execução exatamente nesse preço, começando a
    testar stop/alvo já na vela seguinte. Isso concede um preenchimento que
    não existe quando o papel abre em gap: o modelo entrava no preço pré-gap,
    que é justamente o preço que ninguém conseguiu. Numa medição usada pra
    decidir calibragem, esse é um viés que se acumula em cima justamente dos
    dias de notícia — os que mais mexem no resultado.

    Agora a execução é na ABERTURA da vela seguinte (`preco_fill`), que é o
    que uma ordem a mercado disparada pelo sinal de fato pegaria.

    O denominador do R continua sendo o risco PLANEJADO (`entry - stop`),
    porque é ele que define o tamanho da posição na hora de entrar. O
    numerador é o resultado real a partir do fill. Quando o gap passa por
    cima do próprio stop, não há operação a fazer: sai `SEM_ENTRADA`, não um
    stop fictício.

    Quando stop e alvo são tocados no mesmo candle, assume o cenário PIOR
    (stop primeiro) — convenção conservadora mantida do modelo anterior.
    """
    if risk.entry is None or risk.stop is None:
        return SignalOutcome("SEM_SINAL", "Não havia sinal operável nesta data.", None, None, None)

    if future_df.empty:
        return SignalOutcome("EM_ABERTO", "Ainda não há velas seguintes para conferir.", None, None, None)

    risco_planejado = abs(risk.entry - risk.stop)
    if risco_planejado <= 0:
        return SignalOutcome("SEM_SINAL", "Entrada e stop coincidem — não há risco definido.", None, None, None)

    fill = float(future_df.iloc[0]["open"])
    custo_r = (fill * custo_round_trip) / risco_planejado

    # gap que abriu além do stop: a operação não chega a existir
    if (direction == Direction.BUY and fill <= risk.stop) or (
        direction == Direction.SELL and fill >= risk.stop
    ):
        return SignalOutcome(
            "SEM_ENTRADA",
            f"A vela seguinte abriu em R$ {fill:.2f}, já além do stop de R$ {risk.stop:.2f} — "
            "o gap passou por cima da operação.",
            None, fill, None,
        )

    def resultado_em(preco_saida: float) -> float:
        bruto = (preco_saida - fill) if direction == Direction.BUY else (fill - preco_saida)
        return bruto / risco_planejado - custo_r

    for i, (_, candle) in enumerate(future_df.iterrows(), start=1):
        if direction == Direction.BUY:
            hit_stop = candle["low"] <= risk.stop
            hit_t1 = risk.target_1 is not None and candle["high"] >= risk.target_1
            hit_t2 = risk.target_2 is not None and candle["high"] >= risk.target_2
        else:
            hit_stop = candle["high"] >= risk.stop
            hit_t1 = risk.target_1 is not None and candle["low"] <= risk.target_1
            hit_t2 = risk.target_2 is not None and candle["low"] <= risk.target_2

        if hit_stop:
            return SignalOutcome(
                "STOP", f"Stop batido {i} candle(s) depois, em R$ {risk.stop:.2f} "
                        f"(entrada real R$ {fill:.2f}).",
                i, fill, resultado_em(risk.stop),
            )
        if hit_t2:
            return SignalOutcome(
                "ALVO_2", f"Alvo 2 batido {i} candle(s) depois, em R$ {risk.target_2:.2f} "
                          f"(entrada real R$ {fill:.2f}).",
                i, fill, resultado_em(risk.target_2),
            )
        if hit_t1:
            return SignalOutcome(
                "ALVO_1", f"Alvo 1 batido {i} candle(s) depois, em R$ {risk.target_1:.2f} "
                          f"(entrada real R$ {fill:.2f}).",
                i, fill, resultado_em(risk.target_1),
            )

    return SignalOutcome(
        "EM_ABERTO",
        f"Nenhum nível tocado nos {len(future_df)} candle(s) seguintes disponíveis até agora.",
        None, fill, None,
    )


def check_signal_as_of(
    symbol: str,
    timeframe: str,
    as_of: pd.Timestamp,
    count: int = 250,
    modality: str = "Confluência",
    source: str = "Yahoo Finance",
    params: AnalysisParams = DEFAULT_PARAMS,
) -> RetroSignalCheck:
    """
    Busca os dados normalmente (que vêm até "agora"), separa em duas
    partes: o que já era conhecido ATÉ `as_of` (usado pra gerar o
    sinal, sem espiar o futuro) e o que veio DEPOIS (usado só pra
    conferir o resultado, nunca pra gerar o sinal).

    `modality` escolhe qual das 6 leituras é avaliada: "Confluência"
    (padrão), "SMC", "Price Action", "Médias Móveis" ou "VWAP".
    """
    as_of_utc = as_of.tz_localize(LOCAL_TZ) if as_of.tzinfo is None else as_of
    as_of_utc = as_of_utc.tz_convert("UTC")

    full_df = fetch_ohlcv(symbol, timeframe, count, source=source)
    historical = full_df[full_df.index <= as_of_utc]
    future = full_df[full_df.index > as_of_utc]

    if len(historical) < 30:
        raise ValueError(
            f"Histórico insuficiente até {as_of.date()} em {timeframe} "
            f"({len(historical)} candles, precisa de 30+). Tente uma data mais recente ou outro timeframe."
        )

    context, signals = analyze(historical, params)

    if modality == ALL_MODALITIES_OPTION:
        direction = overall_direction(signals)
        score = overall_score(signals)
        confluence = next(s for s in signals if s.name == "Confluência")
        # só usa o plano de risco da Confluência se ela concordar com a
        # maioria — senão não há um único conjunto de entrada/stop/alvo
        # coerente pra representar "as 6 leituras", só a votação em si
        risk = confluence.risk if confluence.direction == direction else RiskPlan()
        setup = f"Votação das 6 leituras ({confluence.setup} é a leitura combinada)"
        chosen = Signal("Todas as modalidades", direction, score, score, setup, risk=risk)
    else:
        chosen = next(s for s in signals if s.name == modality)

    desfecho = evaluate_signal_outcome(chosen.risk, chosen.direction, future)
    outcome, detail = desfecho.resultado, desfecho.detalhe
    candles_to_result = desfecho.candles_ate_resultado
    if chosen.direction == Direction.NEUTRAL or chosen.risk.entry is None:
        outcome, detail, candles_to_result = "SEM_SINAL", "Não havia sinal operável nesta data.", None
    elif future.empty:
        outcome, detail, candles_to_result = "SEM_DADO_FUTURO", "Não há candles disponíveis depois desta data ainda.", None

    return RetroSignalCheck(
        timeframe=timeframe,
        as_of=historical.index[-1].tz_convert(LOCAL_TZ),
        direction=chosen.direction,
        setup=chosen.setup,
        score=chosen.score,
        risk=chosen.risk,
        outcome=outcome,
        outcome_detail=detail,
        candles_ate_resultado=candles_to_result,
        candles_futuros_disponiveis=len(future),
        preco_fill=desfecho.preco_fill,
        r_realizado=desfecho.r_realizado,
    )


def analyze_symbol_mtf(
    symbol: str,
    confirmation: tuple[str, str] = CONFIRMATION_TIMEFRAMES,
    context: tuple[str, ...] = CONTEXT_TIMEFRAMES,
    counts: dict[str, int] | None = None,
    modality: str = "Confluência",
    source: str = "Yahoo Finance",
    params: AnalysisParams = DEFAULT_PARAMS,
    fetcher: Callable[[str, str, int], pd.DataFrame] | None = None,
) -> MultiTimeframeResult:
    """
    Roda a análise nos timeframes de CONFIRMAÇÃO (obrigatórios — a
    recomendação só é considerada confirmada se os dois concordarem na
    mesma direção) e de CONTEXTO (informativos, não bloqueiam nem
    confirmam nada sozinhos).

    `modality` escolhe QUAL das 6 leituras decide a confirmação:
    "Confluência" (padrão, combina tudo), "SMC", "Price Action",
    "Médias Móveis" ou "VWAP". Serve tanto pra Day Trade (confirmação
    M15+H1, contexto H4+D1) quanto pra Swing Trade (confirmação D1+W1,
    contexto H4) — e qualquer combinação de timeframes/modalidade.

    H4, quando pedido (confirmação ou contexto), é sempre construído a
    partir do H1 já baixado — se H1 não estiver entre os timeframes
    pedidos, ele é buscado só como dependência interna, sem aparecer
    no resultado final.

    `fetcher` troca a origem das velas sem trocar mais nada: recebe
    (symbol, timeframe, count) e devolve o mesmo DataFrame que
    `fetch_ohlcv` devolveria. Existe pro worker do backend, que lê o
    TimescaleDB in-process — sem isso, a alternativa seria devolver um
    DSN de banco pra dentro deste módulo, que é justamente o que foi
    removido daqui quando a API entrou no lugar. Com `None` (o padrão),
    `source` decide como sempre.
    """
    counts = counts or {}
    if fetcher is None:
        fetcher = lambda sym, tf, n: fetch_ohlcv(sym, tf, n, source=source)
    requested = list(dict.fromkeys([*confirmation, *context]))  # únicos, preserva ordem

    needs_h1_only_for_h4 = "H4" in requested and "H1" not in requested
    fetch_list = [tf for tf in requested if tf != "H4"]
    if needs_h1_only_for_h4:
        fetch_list.insert(0, "H1")

    # O Diário tem que ser processado ANTES dos demais: o IFR dele é
    # injetado como filtro de contexto nas outras leituras (ver
    # `rsi_signal`). Na ordem natural, M15/H1 seriam analisados enquanto
    # `daily_rsi` ainda fosse None e ficariam sem o filtro — sem erro
    # nenhum, só sem o reforço, que é o tipo de perda que não aparece.
    if "D1" in fetch_list:
        fetch_list = ["D1"] + [tf for tf in fetch_list if tf != "D1"]

    results: dict[str, TimeframeResult] = {}
    h1_df: pd.DataFrame | None = None
    daily_rsi: float | None = None

    for tf in fetch_list:
        count = counts.get(tf, DEFAULT_TF_COUNTS.get(tf, 200))
        try:
            df = fetcher(symbol, tf, count)
            # O próprio D1 não recebe filtro de si mesmo; os demais sim.
            ctx, signals = analyze(df, params, higher_rsi=None if tf == "D1" else daily_rsi)
            results[tf] = TimeframeResult(tf, ctx, signals, None)
            if tf == "D1":
                daily_rsi = ctx.rsi
            if tf == "H1":
                h1_df = ctx.df
        except Exception as exc:  # noqa: BLE001 — mostra a falha, não derruba os outros timeframes
            results[tf] = TimeframeResult(tf, None, None, str(exc))

    if "H4" in requested:
        if h1_df is not None:
            try:
                h4_df = _resample_to_h4(h1_df)
                if len(h4_df) >= 30:
                    ctx4, sig4 = analyze(h4_df, params, higher_rsi=daily_rsi)
                    results["H4"] = TimeframeResult("H4", ctx4, sig4, None)
                else:
                    results["H4"] = TimeframeResult(
                        "H4", None, None,
                        f"Histórico de H1 insuficiente para montar H4 ({len(h4_df)} candles, precisa de 30+).",
                    )
            except Exception as exc:  # noqa: BLE001
                results["H4"] = TimeframeResult("H4", None, None, str(exc))
        else:
            results["H4"] = TimeframeResult("H4", None, None, "Depende do H1, que falhou.")

    if needs_h1_only_for_h4:
        results.pop("H1", None)  # H1 só foi buscado como dependência do H4, não foi pedido de verdade

    tf_a, tf_b = confirmation
    dir_a = _signal_direction(results[tf_a].signals, modality) if tf_a in results else None
    dir_b = _signal_direction(results[tf_b].signals, modality) if tf_b in results else None

    confirmed = bool(dir_a and dir_b and dir_a == dir_b and dir_a != Direction.NEUTRAL)
    confirmed_direction = dir_a if confirmed and dir_a is not None else Direction.NEUTRAL

    return MultiTimeframeResult(
        symbol=symbol, results=results, modality=modality,
        confirmed=confirmed, confirmed_direction=confirmed_direction,
    )


def percentage(entry: float, price: float) -> float:
    return (price - entry) / entry * 100 if entry else 0.0


def print_signal(
    signal: Signal,
    symbol: str,
    risk_budget: float | None,
    params: AnalysisParams = DEFAULT_PARAMS,
) -> None:
    line = "-" * 72
    print(line)
    print(f" {signal.name.upper()}")
    print(line)
    print(
        f"\n>>> DIREÇÃO: {signal.direction.value}"
        f"  |  SCORE: {signal.score:.1f}/100 ({quality(signal.score, params)})"
    )
    print(f">>> SETUP: {signal.setup}")
    print(f">>> CONFIANÇA: {signal.confidence:.0f}%\n")

    risk = signal.risk
    if signal.direction == Direction.NEUTRAL or risk.entry is None:
        print("Sem sinal operável — entrada, stop e alvos foram bloqueados.\n")
    else:
        action = "COMPRAR" if signal.direction == Direction.BUY else "VENDER"
        print(
            f"{action} {symbol} perto de R$ {risk.entry:.2f}, "
            f"stop R$ {risk.stop:.2f}, alvo R$ {risk.target_1:.2f}.\n"
        )
        print(f"  {'Nível':<12}{'Preço':>12}{'Distância':>14}")
        print(f"  {'Entrada':<12}{risk.entry:>12.2f}{'—':>14}")
        print(
            f"  {'Stop':<12}{risk.stop:>12.2f}"
            f"{percentage(risk.entry, risk.stop):>13.2f}%"
        )
        print(
            f"  {'Alvo 1':<12}{risk.target_1:>12.2f}"
            f"{percentage(risk.entry, risk.target_1):>13.2f}%"
        )
        print(
            f"  {'Alvo 2':<12}{risk.target_2:>12.2f}"
            f"{percentage(risk.entry, risk.target_2):>13.2f}%"
        )
        risk_per_share = abs(risk.entry - risk.stop)
        print(
            f"\n  Risco por ação: R$ {risk_per_share:.2f}"
            f"  |  Stop: {risk.stop_basis}"
            f"  |  R/R: 1:{risk.rr:.2f}"
        )

        if risk_budget is not None and risk_per_share > 0:
            quantity = int(risk_budget // risk_per_share)
            print(
                f"  Para risco máximo de R$ {risk_budget:.2f}: "
                f"{quantity} ação(ões), risco estimado R$ "
                f"{quantity * risk_per_share:.2f}"
            )

        if risk.alternatives:
            print("\n  Alvos alternativos:")
            print(f"  {'Método':<28}{'Preço':>10}{'R/R':>9}{'Status':>12}")
            for target in risk.alternatives:
                status = "VIÁVEL" if target["viable"] else "FRACO"
                print(
                    f"  {target['method']:<28}"
                    f"{target['price']:>10.2f}"
                    f"{target['rr']:>9.2f}"
                    f"{status:>12}"
                )

    print("\nMotivos:")
    for reason in signal.reasons:
        print(f"  - {reason}")
    if signal.alerts:
        print("\nAlertas:")
        for alert in dict.fromkeys(signal.alerts):
            print(f"  ! {alert}")
    print()


def print_summary(signals: list[Signal]) -> None:
    print("=" * 72)
    print(" RESUMO DAS CINCO LEITURAS")
    print("=" * 72)
    print(
        f"  {'Análise':<18}{'Direção':<10}{'Score':>8}"
        f"{'Entrada':>12}{'Stop':>12}{'Alvo 1':>12}"
    )
    for signal in signals:
        risk = signal.risk
        entry = f"{risk.entry:.2f}" if risk.entry is not None else "—"
        stop = f"{risk.stop:.2f}" if risk.stop is not None else "—"
        target = f"{risk.target_1:.2f}" if risk.target_1 is not None else "—"
        print(
            f"  {signal.name:<18}{signal.direction.value:<10}"
            f"{signal.score:>8.1f}{entry:>12}{stop:>12}{target:>12}"
        )


def build_report(
    symbol: str,
    timeframe: str = "M15",
    count: int = 250,
    risk_budget: float | None = None,
    params: AnalysisParams = DEFAULT_PARAMS,
) -> str:
    """Executa a análise e devolve o relatório completo como texto."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Informe um ativo para análise.")
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Timeframe inválido: {timeframe}.")
    if count < 30:
        raise ValueError("A quantidade de candles deve ser pelo menos 30.")
    if risk_budget is not None and risk_budget <= 0:
        raise ValueError("O risco financeiro deve ser maior que zero.")

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        print(
            f"Buscando {count} candles fechados de {symbol} "
            f"em {timeframe} via Yahoo Finance...\n",
            flush=True,
        )

        df = fetch_ohlcv(symbol, timeframe, count)
        context, signals = analyze(df, params)

        duration = TIMEFRAMES[timeframe]["duration"]
        last_open = df.index[-1].tz_convert(LOCAL_TZ)
        last_close = last_open + duration
        age = pd.Timestamp.now(tz=LOCAL_TZ) - last_close

        print("=" * 72)
        print(
            f" {symbol} · {timeframe} · último candle fechado: "
            f"{last_open:%d/%m/%Y %H:%M}–{last_close:%H:%M} (Brasília)"
        )
        print(
            f" ATR: R$ {context.atr:.2f} ({context.atr_pct:.2f}%)"
            f" · RVOL: {context.rvol:.2f}x"
            f" · Volatilidade: {context.volatility}"
        )
        print("=" * 72)

        if age > duration * 3:
            print(
                f"\n! DADOS DEFASADOS EM {age.total_seconds() / 3600:.1f}H — "
                "não use os preços como gatilho de execução.\n"
            )

        for signal in signals:
            print_signal(signal, symbol, risk_budget, params)
        print_summary(signals)

    return output.getvalue()


def symbols_file() -> Path:
    """Arquivo local usado para lembrar a lista personalizada da interface."""
    return Path(__file__).resolve().with_name("daytrade_symbols.json")


def _load_symbols_file() -> list[str]:
    path = symbols_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        symbols = [
            str(item).strip().upper()
            for item in saved
            if str(item).strip()
        ]
        if symbols:
            return list(dict.fromkeys(symbols))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return DEFAULT_SYMBOLS.copy()


def _save_symbols_file(symbols: list[str]) -> None:
    path = symbols_file()
    path.write_text(
        json.dumps(symbols, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_symbols_api() -> list[str]:
    import requests

    response = requests.get(
        f"{_api_base_url()}/watchlist", headers=_api_headers(), timeout=_API_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.json().get("symbols", [])


def _save_symbols_api(symbols: list[str]) -> None:
    import requests

    # PUT (não POST) porque a semântica aqui é "sobrescreve tudo de uma
    # vez", igual à do arquivo local: o servidor desativa quem saiu e
    # (re)ativa quem está na lista, tudo numa transação só.
    response = requests.put(
        f"{_api_base_url()}/watchlist",
        json={"symbols": symbols},
        headers=_api_headers(),
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def load_symbols() -> list[str]:
    """Lê a watchlist da API do homelab quando configurada (ACOES_API_URL),
    com fallback pro arquivo local `daytrade_symbols.json` — usado por
    qualquer deploy que não tenha o homelab, rodando com a fonte Yahoo."""
    if ACOES_API_URL:
        try:
            symbols = _load_symbols_api()
            if symbols:
                return symbols
        except Exception:
            pass  # API indisponível — cai pro arquivo local
    return _load_symbols_file()


def save_symbols(symbols: list[str]) -> None:
    """Espelho de `load_symbols`: grava pela API do homelab quando
    configurada, com fallback pro arquivo local."""
    if ACOES_API_URL:
        try:
            _save_symbols_api(symbols)
            return
        except Exception:
            pass  # API indisponível — cai pro arquivo local
    _save_symbols_file(symbols)


# ---------------------------------------------------------------------------
# Perfis de análise
#
# Mesmo desenho da watchlist acima: API do homelab quando ACOES_API_URL
# está configurada, arquivo local senão. Um perfil pode ter sido salvo por
# um build mais antigo do motor, e `AnalysisParams.from_dict` é tolerante
# de propósito pra isso — parâmetro que não existe mais é ignorado,
# parâmetro que ainda não existia cai no default.
# ---------------------------------------------------------------------------

DEFAULT_PROFILE_NAME = "padrão"


def profiles_file() -> Path:
    """Espelho de `symbols_file()` para os perfis."""
    return Path(__file__).resolve().with_name("daytrade_profiles.json")


def _load_profiles_file() -> dict[str, AnalysisParams]:
    path = profiles_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        perfis = {
            str(nome): AnalysisParams.from_dict(dados)
            for nome, dados in saved.items()
            if str(nome).strip()
        }
        if perfis:
            perfis.setdefault(DEFAULT_PROFILE_NAME, AnalysisParams())
            return perfis
    except (OSError, AttributeError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return {DEFAULT_PROFILE_NAME: AnalysisParams()}


def _save_profiles_file(perfis: dict[str, AnalysisParams]) -> None:
    path = profiles_file()
    path.write_text(
        json.dumps(
            {nome: params.to_dict() for nome, params in perfis.items()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _load_profiles_api() -> dict[str, AnalysisParams]:
    import requests

    response = requests.get(
        f"{_api_base_url()}/profiles", headers=_api_headers(), timeout=_API_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return {
        perfil["nome"]: AnalysisParams.from_dict(perfil.get("params"))
        for perfil in response.json().get("profiles", [])
    }


def _save_profile_api(nome: str, params: AnalysisParams, descricao: str) -> None:
    import requests

    response = requests.put(
        f"{_api_base_url()}/profiles/{quote(nome, safe='')}",
        json={
            "params": params.to_dict(),
            "params_hash": params.params_hash(),
            "descricao": descricao,
        },
        headers=_api_headers(),
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _delete_profile_api(nome: str) -> None:
    import requests

    response = requests.delete(
        f"{_api_base_url()}/profiles/{quote(nome, safe='')}",
        headers=_api_headers(),
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def load_profiles() -> dict[str, AnalysisParams]:
    """Lê os perfis da API do homelab quando configurada, com fallback
    pro arquivo local. O perfil padrão sempre existe, mesmo sem nenhuma
    das duas fontes disponível."""
    if ACOES_API_URL:
        try:
            perfis = _load_profiles_api()
            if perfis:
                perfis.setdefault(DEFAULT_PROFILE_NAME, AnalysisParams())
                return perfis
        except Exception:
            pass  # API indisponível — cai pro arquivo local
    return _load_profiles_file()


def save_profile(nome: str, params: AnalysisParams, descricao: str = "") -> None:
    """Espelho de `load_profiles`: grava pela API quando configurada,
    com fallback pro arquivo local."""
    nome = nome.strip()
    if not nome:
        raise ValueError("Informe um nome para o perfil.")

    if ACOES_API_URL:
        try:
            _save_profile_api(nome, params, descricao)
            return
        except Exception:
            pass  # API indisponível — cai pro arquivo local
    perfis = _load_profiles_file()
    perfis[nome] = params
    _save_profiles_file(perfis)


def delete_profile(nome: str) -> None:
    """Desativa um perfil. O padrão nunca some — é o fallback de todo o resto."""
    nome = nome.strip()
    if nome == DEFAULT_PROFILE_NAME:
        raise ValueError(f"O perfil '{DEFAULT_PROFILE_NAME}' não pode ser removido.")

    if ACOES_API_URL:
        try:
            _delete_profile_api(nome)
            return
        except Exception:
            pass  # API indisponível — cai pro arquivo local
    perfis = _load_profiles_file()
    perfis.pop(nome, None)
    _save_profiles_file(perfis)


# ---------------------------------------------------------------------------
# Sinais gravados
#
# Diferente da watchlist e dos perfis, aqui NÃO existe fallback pro arquivo
# local, de propósito: um histórico de sinais dividido entre o banco e o
# filesystem efêmero de um container é pior que histórico nenhum, porque a
# taxa de acerto sairia calculada sobre a metade que o processo por acaso
# enxergou. Watchlist e perfil são preferência e toleram sumir; taxa de
# acerto é medição, e uma medição incompleta em silêncio engana.
# ---------------------------------------------------------------------------


def signal_payload(
    symbol: str,
    timeframe: str,
    signal: Signal,
    context: MarketContext,
    perfil: str,
    params: AnalysisParams,
    origem: str,
    mtf_confirmado: bool = False,
    mtf_direcao: Direction = Direction.NEUTRAL,
    modalidade: str | None = None,
) -> dict:
    """Monta o corpo do POST /signals a partir de um sinal analisado.

    Mora AQUI, e não no worker nem no Streamlit, porque os dois produtores
    precisam gravar linhas idênticas — dois construtores de payload
    divergiriam na primeira mudança do motor, e a comparação entre sinais
    de origens diferentes deixaria de valer.

    `candle_time` é a abertura da última vela do DataFrame, em UTC, que é o
    contrato do índice em todo o motor. Montar essa chave a partir do
    horário EXIBIDO (Brasília) criaria em silêncio uma segunda linha
    deslocada 3 horas, escapando do índice de deduplicação.

    `mtf_confirmado`/`mtf_direcao` vêm de `mtf_confirmation` calculado PRA
    ESTA modalidade — ver o docstring de lá sobre por que reaproveitar a
    confirmação de outra leitura falsearia o recorte de assertividade.
    """
    risk = signal.risk
    risco_por_acao = (
        abs(risk.entry - risk.stop)
        if risk.entry is not None and risk.stop is not None
        else None
    )

    def r_de(alvo: float | None) -> float | None:
        if alvo is None or risk.entry is None or not risco_por_acao:
            return None
        return abs(alvo - risk.entry) / risco_por_acao

    return {
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe.strip().upper(),
        "modalidade": modalidade or signal.name,
        "candle_time": context.df.index[-1].isoformat(),
        "perfil": perfil,
        "params_hash": params.params_hash(),
        "origem": origem,
        "direcao": signal.direction.value,
        "score": float(signal.score),
        "confianca": float(signal.confidence),
        "setup": signal.setup,
        "mtf_confirmado": bool(mtf_confirmado),
        "mtf_direcao": mtf_direcao.value if isinstance(mtf_direcao, Direction) else str(mtf_direcao),
        "entrada": risk.entry,
        "stop": risk.stop,
        "alvo_1": risk.target_1,
        "alvo_2": risk.target_2,
        "r_alvo_1": r_de(risk.target_1),
        "r_alvo_2": r_de(risk.target_2),
        "stop_basis": risk.stop_basis,
        "detalhes": {
            "reasons": list(signal.reasons),
            "alerts": list(dict.fromkeys(signal.alerts)),
            "alternatives": list(risk.alternatives),
            "volatilidade": context.volatility,
            "atr": float(context.atr),
            "atr_pct": float(context.atr_pct),
            "rvol": float(context.rvol),
        },
    }


def save_signal(payload: dict) -> dict:
    """Grava um sinal pela API. LEVANTA erro se a API não estiver
    configurada ou não responder — ver o comentário do bloco acima sobre
    por que aqui não existe fallback local.

    A resposta traz `duplicado=True` quando aquela vela já tinha sido
    gravada com a mesma modalidade/perfil/origem."""
    import requests

    response = requests.post(
        f"{_api_base_url()}/signals",
        json=payload,
        headers=_api_headers(),
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def fetch_signals(**filtros) -> dict:
    """Histórico de sinais. Filtros aceitos: symbol, timeframe, modalidade,
    perfil, origem, resultado, dias, limite."""
    import requests

    response = requests.get(
        f"{_api_base_url()}/signals",
        params={k: v for k, v in filtros.items() if v is not None},
        headers=_api_headers(),
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def fetch_signal_stats(**filtros) -> dict:
    """Assertividade agregada. Filtros aceitos: perfil, origem, symbol,
    timeframe, dias."""
    import requests

    response = requests.get(
        f"{_api_base_url()}/signals/stats",
        params={k: v for k, v in filtros.items() if v is not None},
        headers=_api_headers(),
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def fetch_last_candle_time() -> pd.Timestamp | None:
    """Horário (UTC) da vela mais recente que a API tem, de qualquer par —
    o sinal de frescor da barra lateral.

    Lê `last_candle_time` do `GET /status`, e NÃO `last_ingested_at`. Os dois
    parecem servir aqui e só um serve: desde que o processor passou a gravar
    só vela que mudou de verdade, `last_ingested_at` significa "última vez que
    o dado MUDOU" e congela com o mercado fechado (ver o docstring do endpoint
    em backend/api.py). Uma legenda montada em cima dele acusaria scraper
    morto toda noite e todo fim de semana.

    Devolve None em qualquer falha: isto alimenta uma legenda, e uma legenda
    não pode derrubar a barra lateral inteira — sem a API a fonte Yahoo
    continua utilizável."""
    import requests

    try:
        response = requests.get(
            f"{_api_base_url()}/status",
            headers=_api_headers(),
            timeout=_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        horarios = [
            pd.to_datetime(linha["last_candle_time"], utc=True)
            for linha in response.json()
            if linha.get("last_candle_time")
        ]
    except Exception:
        return None

    return max(horarios) if horarios else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Relatório de SMC, Price Action, EMAs e VWAP para um ativo, no "
            "terminal. A interface completa é a web: streamlit run streamlit_app.py"
        )
    )
    parser.add_argument(
        "symbol",
        help="Ticker, por exemplo VALE3 ou PETR4.",
    )
    parser.add_argument(
        "--timeframe",
        choices=tuple(TIMEFRAMES),
        default="M15",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=250,
        help="Candles fechados; recomenda-se 250 ou mais.",
    )
    parser.add_argument(
        "--risco",
        type=float,
        default=None,
        help="Risco financeiro máximo por operação.",
    )
    args = parser.parse_args()

    try:
        report = build_report(
            args.symbol,
            args.timeframe,
            args.count,
            args.risco,
        )
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"[ERRO] {exc}\n")
    print(report, end="")


if __name__ == "__main__":
    main()
