r"""
Analisador de Day Trade — SMC + Price Action + EMAs + VWAP
Versão 2.0 standalone com interface gráfica para Windows/VSCode.

Instalação:
    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install pandas numpy yfinance

Uso:
    python daytrade_smc.py
    python daytrade_smc.py VALE3
    python daytrade_smc.py VALE3 --timeframe M15 --count 250 --risco 500

Sem informar um ativo, o programa abre a interface gráfica. Pelo terminal,
o primeiro argumento continua sendo o ativo a analisar.

Aviso: o Yahoo Finance tem atraso e pode limitar requisições. O programa
serve para leitura técnica e estudo; não envia ordens e não substitui dados
em tempo real nem gestão profissional de risco.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import queue
import re
import statistics
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

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

# Mantidos por compatibilidade — apontam pro conjunto de Day Trade, que
# é o comportamento padrão histórico deste motor.
CONFIRMATION_TIMEFRAMES = DAYTRADE_CONFIRMATION_TIMEFRAMES
CONTEXT_TIMEFRAMES = DAYTRADE_CONTEXT_TIMEFRAMES


class Direction(str, Enum):
    BUY = "COMPRA"
    SELL = "VENDA"
    NEUTRAL = "NEUTRO"


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


DATA_SOURCES = ("Yahoo Finance", "MetaTrader 5", "GitHub (MT5 de casa)", "Homelab (API)")

# Configuração da ponte GitHub — definida pelo app (a partir de
# st.secrets) antes de qualquer chamada com source="GitHub (MT5 de casa)".
# Fica como variável de módulo pra não precisar passar repo/token em
# cada chamada de fetch_ohlcv/analyze_symbol_mtf/check_signal_as_of.
GITHUB_BRIDGE_REPO: str | None = None
GITHUB_BRIDGE_TOKEN: str | None = None

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

_MT5_TIMEFRAME_MAP_NAMES = {"M15": "TIMEFRAME_M15", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1"}


def fetch_ohlcv(symbol: str, timeframe: str, count: int, source: str = "Yahoo Finance") -> pd.DataFrame:
    """
    Busca candles pela fonte escolhida.
      - "Yahoo Finance": funciona em qualquer lugar (nuvem ou local), atraso de 15-20min.
      - "MetaTrader 5": dado real, sem atraso, mas só funciona rodando LOCALMENTE, na
        máquina com o terminal MT5 aberto e logado.
      - "GitHub (MT5 de casa)": lê o snapshot mais recente publicado no GitHub por
        `mt5_bridge/update_data.py` — dado real do MT5, mas atualizado só quando você
        pedir (botão "Atualizar via MT5" no app), não em tempo real contínuo.
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

    if source == "GitHub (MT5 de casa)":
        return _fetch_ohlcv_github(symbol, timeframe, count)

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
    import requests  # import tardio — mesma convenção de _fetch_ohlcv_mt5/_fetch_ohlcv_github

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


def _fetch_ohlcv_github(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    """Lê o snapshot de candles publicado no GitHub pelo script que roda no PC de casa via MT5."""
    import requests

    if not GITHUB_BRIDGE_REPO:
        raise RuntimeError(
            "Repositório do GitHub não configurado (GITHUB_BRIDGE_REPO). Configure em "
            "st.secrets['github_repo'] no formato 'usuario/nome-do-repositorio'."
        )

    url = f"https://raw.githubusercontent.com/{GITHUB_BRIDGE_REPO}/main/data/mt5_snapshot.json"
    headers = {"Authorization": f"token {GITHUB_BRIDGE_TOKEN}"} if GITHUB_BRIDGE_TOKEN else {}

    try:
        response = requests.get(url, headers=headers, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"Falha ao buscar o snapshot no GitHub: {exc}") from exc

    if response.status_code == 404:
        raise RuntimeError(
            "Ainda não existe nenhum snapshot publicado. Clique em \"Atualizar via MT5\" "
            "primeiro, com o PC de casa ligado e o runner do GitHub Actions ativo."
        )
    if response.status_code != 200:
        raise RuntimeError(f"Não foi possível buscar o snapshot do GitHub (HTTP {response.status_code}).")

    snapshot = response.json()
    symbol_data = snapshot.get("symbols", {}).get(symbol)
    if symbol_data is None:
        raise RuntimeError(f"'{symbol}' não está no snapshot mais recente — confirme se está na watchlist usada pelo PC de casa.")

    tf_data = symbol_data.get(timeframe)
    if tf_data is None:
        raise RuntimeError(f"Timeframe {timeframe} não está no snapshot para {symbol}.")
    if not tf_data.get("ok"):
        raise RuntimeError(f"O PC de casa não conseguiu buscar {symbol} em {timeframe} via MT5: {tf_data.get('error')}")

    candles = tf_data.get("candles", [])
    if not candles:
        raise RuntimeError(f"Snapshot sem candles para {symbol} em {timeframe}.")

    df = pd.DataFrame(candles)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]].tail(count)


def fetch_snapshot_timestamp() -> str | None:
    """Devolve o horário (ISO, UTC) do snapshot mais recente publicado no GitHub, ou None se não houver nenhum ainda."""
    import requests

    if not GITHUB_BRIDGE_REPO:
        return None
    url = f"https://raw.githubusercontent.com/{GITHUB_BRIDGE_REPO}/main/data/mt5_snapshot.json"
    headers = {"Authorization": f"token {GITHUB_BRIDGE_TOKEN}"} if GITHUB_BRIDGE_TOKEN else {}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        return response.json().get("generated_at")
    except Exception:
        return None


def trigger_github_update() -> tuple[bool, str]:
    """
    Dispara o workflow do GitHub Actions (workflow_dispatch) que aciona
    o runner no PC de casa. Devolve (sucesso, mensagem).
    """
    import requests

    if not GITHUB_BRIDGE_REPO or not GITHUB_BRIDGE_TOKEN:
        return False, "Repositório ou token do GitHub não configurados (veja st.secrets)."

    url = f"https://api.github.com/repos/{GITHUB_BRIDGE_REPO}/actions/workflows/mt5-update.yml/dispatches"
    headers = {
        "Authorization": f"token {GITHUB_BRIDGE_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
    except Exception as exc:
        return False, f"Falha ao chamar a API do GitHub: {exc}"

    if response.status_code == 204:
        return True, "Atualização disparada — aguardando o PC de casa processar."
    if response.status_code == 404:
        return False, (
            "Workflow não encontrado (HTTP 404). Confirme se o arquivo "
            ".github/workflows/mt5-update.yml foi commitado e se o token tem permissão 'workflow'."
        )
    return False, f"Falha ao disparar (HTTP {response.status_code}): {response.text[:200]}"


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
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
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


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean().bfill()


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
    left: int = 3,
    right: int = 3,
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
        valid = volume_ratio >= 1.2 and range_ratio >= 0.8
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
    lookback: int = 20,
    tolerance_pct: float = 0.3,
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


def detect_fvg_setup(df: pd.DataFrame, max_age: int = 20) -> str | None:
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


def build_context(df: pd.DataFrame) -> MarketContext:
    if len(df) < 30:
        raise ValueError("São necessários pelo menos 30 candles fechados.")

    atr_series = compute_atr(df)
    atr = float(atr_series.iloc[-1])
    price = float(df["close"].iloc[-1])
    atr_pct = atr / price * 100 if price else 0.0
    rvol_series = df["volume"] / df["volume"].rolling(20, min_periods=5).mean()
    rvol = float(rvol_series.iloc[-1]) if pd.notna(rvol_series.iloc[-1]) else 0.0

    if atr_pct < 0.30:
        volatility = "BAIXA"
    elif atr_pct > 6.0:
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

    volume_ma_current = float(df["volume"].rolling(20, min_periods=5).mean().iloc[-1])
    swings = detect_swings(df)
    events = detect_structure(df, swings, atr_series)
    broke_high, broke_low, bullish_retest, bearish_retest = breakout_and_retest(df, atr)

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
        fvg_setup=detect_fvg_setup(df),
    )


def quality(score: float) -> str:
    if score < 40:
        return "EVITAR"
    if score < 60:
        return "BAIXA QUALIDADE"
    if score < 70:
        return "MONITORAR"
    if score < 80:
        return "BOA OPORTUNIDADE"
    if score < 90:
        return "FORTE OPORTUNIDADE"
    return "OPORTUNIDADE EXCEPCIONAL"


def market_alerts(context: MarketContext) -> list[str]:
    alerts = []
    if context.volatility == "BAIXA":
        alerts.append("VOLATILIDADE INSUFICIENTE — entrada bloqueada")
    if context.volatility == "EXCESSIVA":
        alerts.append("VOLATILIDADE EXCESSIVA — risco elevado")
    if abs(context.vwap_distance_pct) > 1.5:
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
    if isolated:
        score = min(score, 79.0)
        confidence = min(confidence, 70.0)
    if context.volatility == "BAIXA" or block_entry:
        return Direction.NEUTRAL, min(score, 39.0), min(confidence, 35.0)
    if context.volatility == "EXCESSIVA":
        return direction, min(score, 59.0), min(confidence, 50.0)
    if score < 40:
        return Direction.NEUTRAL, score, min(confidence, 35.0)
    return direction, score, confidence


def last_recent_event(
    context: MarketContext,
    max_age: int = 20,
) -> StructureEvent | None:
    if not context.events:
        return None
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
        reasons.append("Rompimento da máxima dos últimos 20 candles")
    if context.broke_low:
        bear_points += 45
        reasons.append("Rompimento da mínima dos últimos 20 candles")
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
    min_distance_pct = 0.10
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

    too_far = abs(context.vwap_distance_pct) > 1.5
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


def confluence_signal(
    context: MarketContext,
    isolated: list[Signal],
) -> Signal:
    weights = {
        "SMC": 30.0,
        "Price Action": 20.0,
        "Médias Móveis": 20.0,
        "VWAP": 20.0,
    }
    buy = 0.0
    sell = 0.0
    agreeing = 0
    reasons: list[str] = []

    for signal in isolated:
        normalized_strength = min(signal.score / 79.0, 1.0)
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

    if abs(buy - sell) < 3:
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
    multiplier = {0: 0.60, 1: 0.60, 2: 0.95, 3: 1.0, 4: 1.10}[agreeing]
    score = min(100.0, score * multiplier)
    confidence = agreeing / 4 * 100

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
    targets = [
        {
            "method": "Risco/Retorno 1:1.5",
            "price": project(risk * 1.5),
            "rr": 1.5,
            "viable": True,
        },
        {
            "method": "Risco/Retorno 1:3.0",
            "price": project(risk * 3.0),
            "rr": 3.0,
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
                    "viable": rr >= 1.5,
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
                "viable": rr >= 1.5,
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
                "viable": rr >= 1.5,
            }
        )

    return targets


def attach_risk(signal: Signal, context: MarketContext) -> None:
    if signal.direction == Direction.NEUTRAL:
        return

    entry = round_tick(float(context.df["close"].iloc[-1]), "nearest")
    stop, basis = stop_for_signal(signal, context)
    minimum_distance = context.atr * 0.75

    if signal.direction == Direction.BUY:
        if entry - stop < minimum_distance:
            stop = entry - minimum_distance
            basis += "+mínimo_0.75ATR"
        stop = round_tick(stop, "floor")
        risk = entry - stop
        if risk <= 0:
            signal.direction = Direction.NEUTRAL
            return
        target_1 = round_tick(entry + risk * 1.5, "ceil")
        target_2 = round_tick(entry + risk * 3.0, "ceil")
    else:
        if stop - entry < minimum_distance:
            stop = entry + minimum_distance
            basis += "+mínimo_0.75ATR"
        stop = round_tick(stop, "ceil")
        risk = stop - entry
        if risk <= 0:
            signal.direction = Direction.NEUTRAL
            return
        target_1 = round_tick(entry - risk * 1.5, "floor")
        target_2 = round_tick(entry - risk * 3.0, "floor")

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


def analyze(df: pd.DataFrame) -> tuple[MarketContext, list[Signal]]:
    context = build_context(df)
    isolated = [
        smc_signal(context),
        price_action_signal(context),
        moving_average_signal(context),
        vwap_signal(context),
    ]
    confluence = confluence_signal(context, isolated)
    signals = [confluence, *isolated]

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


MODALITIES = ("Confluência", "SMC", "Price Action", "Médias Móveis", "VWAP")
ALL_MODALITIES_OPTION = "Todas as modalidades"
MODALITY_CHOICES = (ALL_MODALITIES_OPTION, *MODALITIES)


def overall_score(signals: list[Signal]) -> float:
    """Score geral: média do score das 5 leituras (Confluência, SMC, Price Action, Médias Móveis, VWAP) neste timeframe."""
    return sum(s.score for s in signals) / len(signals)


def overall_direction(signals: list[Signal]) -> Direction:
    """
    Direção geral: exige MAIORIA CLARA entre as 5 leituras (pelo menos
    3 de 5 apontando pra mesma direção), não só "mais compra que
    venda" entre poucas leituras não-neutras. Isso evita que 1 leitura
    isolada decida a direção geral enquanto as outras 4 estão caladas
    (NEUTRO) — nesse caso o correto é permanecer NEUTRO, não declarar
    vencedor por W.O.
    """
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
    """Quantas das leituras concordam com a direção geral, e o total avaliado — pra exibir tipo '4 de 5 leituras concordam'."""
    direction = overall_direction(signals)
    total = len(signals)
    if direction == Direction.NEUTRAL:
        buy = sum(1 for s in signals if s.direction == Direction.BUY)
        sell = sum(1 for s in signals if s.direction == Direction.SELL)
        return max(buy, sell), total
    agreeing = sum(1 for s in signals if s.direction == direction)
    return agreeing, total


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


DEFAULT_TF_COUNTS = {"M15": 250, "H1": 250, "H4": 150, "D1": 250, "W1": 150}


@dataclass
class RetroSignalCheck:
    timeframe: str
    as_of: pd.Timestamp
    direction: Direction
    setup: str
    score: float
    risk: RiskPlan
    outcome: str          # "ALVO_1", "ALVO_2", "STOP", "EM_ABERTO", "SEM_SINAL", "SEM_DADO_FUTURO"
    outcome_detail: str
    candles_ate_resultado: int | None
    candles_futuros_disponiveis: int


def evaluate_signal_outcome(risk: RiskPlan, direction: Direction, future_df: pd.DataFrame) -> tuple[str, str, int | None]:
    """
    Caminha candle a candle pelos dados REAIS que vieram depois do sinal
    e verifica o que aconteceu primeiro: bateu o stop, o alvo 1, o alvo
    2, ou nenhum dos dois ainda (em aberto). Quando stop e alvo são
    tocados no mesmo candle, assume o cenário PIOR (stop primeiro) —
    mesma convenção conservadora usada em qualquer backtest deste
    projeto.
    """
    if risk.entry is None or risk.stop is None:
        return "SEM_SINAL", "Não havia sinal operável nesta data.", None

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
            return "STOP", f"Stop batido {i} candle(s) depois, em R$ {risk.stop:.2f}.", i
        if hit_t2:
            return "ALVO_2", f"Alvo 2 batido {i} candle(s) depois, em R$ {risk.target_2:.2f}.", i
        if hit_t1:
            return "ALVO_1", f"Alvo 1 batido {i} candle(s) depois, em R$ {risk.target_1:.2f}.", i

    return "EM_ABERTO", f"Nenhum nível tocado nos {len(future_df)} candle(s) seguintes disponíveis até agora.", None


def check_signal_as_of(
    symbol: str,
    timeframe: str,
    as_of: pd.Timestamp,
    count: int = 250,
    modality: str = "Confluência",
    source: str = "Yahoo Finance",
) -> RetroSignalCheck:
    """
    Busca os dados normalmente (que vêm até "agora"), separa em duas
    partes: o que já era conhecido ATÉ `as_of` (usado pra gerar o
    sinal, sem espiar o futuro) e o que veio DEPOIS (usado só pra
    conferir o resultado, nunca pra gerar o sinal).

    `modality` escolhe qual das 5 leituras é avaliada: "Confluência"
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

    context, signals = analyze(historical)

    if modality == ALL_MODALITIES_OPTION:
        direction = overall_direction(signals)
        score = overall_score(signals)
        confluence = next(s for s in signals if s.name == "Confluência")
        # só usa o plano de risco da Confluência se ela concordar com a
        # maioria — senão não há um único conjunto de entrada/stop/alvo
        # coerente pra representar "as 5 leituras", só a votação em si
        risk = confluence.risk if confluence.direction == direction else RiskPlan()
        setup = f"Votação das 5 leituras ({confluence.setup} é a leitura combinada)"
        chosen = Signal("Todas as modalidades", direction, score, score, setup, risk=risk)
    else:
        chosen = next(s for s in signals if s.name == modality)

    outcome, detail, candles_to_result = evaluate_signal_outcome(chosen.risk, chosen.direction, future)
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
    )


def analyze_symbol_mtf(
    symbol: str,
    confirmation: tuple[str, str] = CONFIRMATION_TIMEFRAMES,
    context: tuple[str, ...] = CONTEXT_TIMEFRAMES,
    counts: dict[str, int] | None = None,
    modality: str = "Confluência",
    source: str = "Yahoo Finance",
) -> MultiTimeframeResult:
    """
    Roda a análise nos timeframes de CONFIRMAÇÃO (obrigatórios — a
    recomendação só é considerada confirmada se os dois concordarem na
    mesma direção) e de CONTEXTO (informativos, não bloqueiam nem
    confirmam nada sozinhos).

    `modality` escolhe QUAL das 5 leituras decide a confirmação:
    "Confluência" (padrão, combina tudo), "SMC", "Price Action",
    "Médias Móveis" ou "VWAP". Serve tanto pra Day Trade (confirmação
    M15+H1, contexto H4+D1) quanto pra Swing Trade (confirmação D1+W1,
    contexto H4) — e qualquer combinação de timeframes/modalidade.

    H4, quando pedido (confirmação ou contexto), é sempre construído a
    partir do H1 já baixado — se H1 não estiver entre os timeframes
    pedidos, ele é buscado só como dependência interna, sem aparecer
    no resultado final.
    """
    counts = counts or {}
    requested = list(dict.fromkeys([*confirmation, *context]))  # únicos, preserva ordem

    needs_h1_only_for_h4 = "H4" in requested and "H1" not in requested
    fetch_list = [tf for tf in requested if tf != "H4"]
    if needs_h1_only_for_h4:
        fetch_list.insert(0, "H1")

    results: dict[str, TimeframeResult] = {}
    h1_df: pd.DataFrame | None = None

    for tf in fetch_list:
        count = counts.get(tf, DEFAULT_TF_COUNTS.get(tf, 200))
        try:
            df = fetch_ohlcv(symbol, tf, count, source=source)
            ctx, signals = analyze(df)
            results[tf] = TimeframeResult(tf, ctx, signals, None)
            if tf == "H1":
                h1_df = ctx.df
        except Exception as exc:  # noqa: BLE001 — mostra a falha, não derruba os outros timeframes
            results[tf] = TimeframeResult(tf, None, None, str(exc))

    if "H4" in requested:
        if h1_df is not None:
            try:
                h4_df = _resample_to_h4(h1_df)
                if len(h4_df) >= 30:
                    ctx4, sig4 = analyze(h4_df)
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


def print_signal(signal: Signal, symbol: str, risk_budget: float | None) -> None:
    line = "-" * 72
    print(line)
    print(f" {signal.name.upper()}")
    print(line)
    print(
        f"\n>>> DIREÇÃO: {signal.direction.value}"
        f"  |  SCORE: {signal.score:.1f}/100 ({quality(signal.score)})"
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
        context, signals = analyze(df)

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
            print_signal(signal, symbol, risk_budget)
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
    com fallback pro arquivo local `daytrade_symbols.json` — usado pelo
    Tkinter, pelo CLI, e por qualquer deploy que não use o homelab (ex:
    Streamlit Cloud com Yahoo Finance)."""
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


def launch_gui() -> None:
    """Abre a interface gráfica. Tkinter acompanha o Python oficial no Windows."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:
        raise RuntimeError(
            "A interface gráfica Tkinter não está disponível nesta instalação "
            "do Python. Reinstale o Python marcando o componente Tcl/Tk."
        ) from exc

    class DayTradeApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title("Day Trade SMC · Analisador Técnico")
            self.root.geometry("1160x780")
            self.root.minsize(900, 620)

            self.symbols = load_symbols()
            self.result_queue: queue.Queue[tuple[bool, str]] = queue.Queue()
            self.running = False

            self.symbol_var = tk.StringVar(
                value=self.symbols[0] if self.symbols else "VALE3"
            )
            self.timeframe_var = tk.StringVar(value="M15")
            self.count_var = tk.StringVar(value="250")
            self.risk_var = tk.StringVar(value="500")
            self.new_symbol_var = tk.StringVar()
            self.status_var = tk.StringVar(value="Pronto para analisar.")

            self._configure_style()
            self._build_interface()
            self._refresh_symbol_widgets()
            self.root.after(150, self._poll_results)

        def _configure_style(self) -> None:
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

            self.root.configure(background="#edf1f5")
            style.configure("TFrame", background="#edf1f5")
            style.configure("TLabel", background="#edf1f5", foreground="#17202a")
            style.configure(
                "Title.TLabel",
                font=("Segoe UI", 18, "bold"),
                foreground="#123b2c",
            )
            style.configure(
                "Subtitle.TLabel",
                font=("Segoe UI", 10),
                foreground="#52606d",
            )
            style.configure(
                "Analyze.TButton",
                font=("Segoe UI", 10, "bold"),
                foreground="#ffffff",
                background="#18794e",
                padding=(18, 8),
            )
            style.map(
                "Analyze.TButton",
                background=[
                    ("disabled", "#9aa5b1"),
                    ("active", "#0f6841"),
                ],
            )
            style.configure("TNotebook", background="#edf1f5", borderwidth=0)
            style.configure("TNotebook.Tab", padding=(16, 8))

        def _build_interface(self) -> None:
            header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
            header.pack(fill="x")
            ttk.Label(
                header,
                text="Analisador Day Trade · SMC",
                style="Title.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                header,
                text=(
                    "SMC, Price Action, EMA9/21/50/200 e VWAP · "
                    "dados do Yahoo Finance com atraso"
                ),
                style="Subtitle.TLabel",
            ).pack(anchor="w", pady=(3, 0))

            self.notebook = ttk.Notebook(self.root)
            self.notebook.pack(fill="both", expand=True, padx=18, pady=(4, 16))

            self.analysis_tab = ttk.Frame(self.notebook, padding=12)
            self.assets_tab = ttk.Frame(self.notebook, padding=16)
            self.notebook.add(self.analysis_tab, text="Análise")
            self.notebook.add(self.assets_tab, text="Meus ativos")

            self._build_analysis_tab()
            self._build_assets_tab()

        def _build_analysis_tab(self) -> None:
            controls = ttk.Frame(self.analysis_tab)
            controls.pack(fill="x", pady=(0, 10))

            ttk.Label(controls, text="Ativo").grid(
                row=0, column=0, sticky="w", padx=(0, 6)
            )
            self.symbol_combo = ttk.Combobox(
                controls,
                textvariable=self.symbol_var,
                width=15,
                state="normal",
            )
            self.symbol_combo.grid(row=1, column=0, padx=(0, 12), sticky="ew")
            self.symbol_combo.bind("<Return>", lambda _event: self.start_analysis())

            ttk.Label(controls, text="Período").grid(
                row=0, column=1, sticky="w", padx=(0, 6)
            )
            ttk.Combobox(
                controls,
                textvariable=self.timeframe_var,
                values=tuple(TIMEFRAMES),
                state="readonly",
                width=9,
            ).grid(row=1, column=1, padx=(0, 12), sticky="ew")

            ttk.Label(controls, text="Candles fechados").grid(
                row=0, column=2, sticky="w", padx=(0, 6)
            )
            ttk.Spinbox(
                controls,
                from_=30,
                to=1000,
                increment=10,
                textvariable=self.count_var,
                width=11,
            ).grid(row=1, column=2, padx=(0, 12), sticky="ew")

            ttk.Label(controls, text="Risco máximo (R$)").grid(
                row=0, column=3, sticky="w", padx=(0, 6)
            )
            ttk.Entry(
                controls,
                textvariable=self.risk_var,
                width=14,
            ).grid(row=1, column=3, padx=(0, 12), sticky="ew")

            self.analyze_button = ttk.Button(
                controls,
                text="Analisar agora",
                style="Analyze.TButton",
                command=self.start_analysis,
            )
            self.analyze_button.grid(row=1, column=4, padx=(4, 8))

            ttk.Button(
                controls,
                text="Limpar resultado",
                command=self.clear_output,
            ).grid(row=1, column=5)

            controls.columnconfigure(0, weight=1)

            status_frame = ttk.Frame(self.analysis_tab)
            status_frame.pack(fill="x", pady=(0, 7))
            ttk.Label(
                status_frame,
                textvariable=self.status_var,
            ).pack(side="left")
            ttk.Label(
                status_frame,
                text="BRA50 usa o Ibovespa (^BVSP) como referência no Yahoo.",
                style="Subtitle.TLabel",
            ).pack(side="right")

            result_frame = ttk.Frame(self.analysis_tab)
            result_frame.pack(fill="both", expand=True)
            result_frame.rowconfigure(0, weight=1)
            result_frame.columnconfigure(0, weight=1)

            self.output = tk.Text(
                result_frame,
                wrap="none",
                font=("Consolas", 10),
                background="#101820",
                foreground="#e8f1ed",
                insertbackground="#ffffff",
                selectbackground="#2b6f55",
                padx=12,
                pady=12,
                relief="flat",
            )
            vertical = ttk.Scrollbar(
                result_frame,
                orient="vertical",
                command=self.output.yview,
            )
            horizontal = ttk.Scrollbar(
                result_frame,
                orient="horizontal",
                command=self.output.xview,
            )
            self.output.configure(
                yscrollcommand=vertical.set,
                xscrollcommand=horizontal.set,
            )
            self.output.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            self.output.insert(
                "1.0",
                "Selecione ou digite um ativo e clique em “Analisar agora”.\n"
                "Exemplos: VALE3, PETR4, PRIO3, BRA50, AAPL, BTC-USD.\n",
            )

        def _build_assets_tab(self) -> None:
            ttk.Label(
                self.assets_tab,
                text="Lista personalizada de ativos",
                style="Title.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                self.assets_tab,
                text=(
                    "Adicione os códigos que acompanha. A lista fica salva ao "
                    "lado do programa e reaparece nas próximas execuções."
                ),
                style="Subtitle.TLabel",
            ).pack(anchor="w", pady=(3, 14))

            add_frame = ttk.Frame(self.assets_tab)
            add_frame.pack(fill="x", pady=(0, 10))
            self.new_symbol_entry = ttk.Entry(
                add_frame,
                textvariable=self.new_symbol_var,
                width=24,
            )
            self.new_symbol_entry.pack(side="left", padx=(0, 8))
            self.new_symbol_entry.bind(
                "<Return>",
                lambda _event: self.add_symbol(),
            )
            ttk.Button(
                add_frame,
                text="Adicionar ativo",
                command=self.add_symbol,
            ).pack(side="left", padx=(0, 8))
            ttk.Button(
                add_frame,
                text="Restaurar lista padrão",
                command=self.restore_default_symbols,
            ).pack(side="left")

            list_frame = ttk.Frame(self.assets_tab)
            list_frame.pack(fill="both", expand=True)
            list_frame.rowconfigure(0, weight=1)
            list_frame.columnconfigure(0, weight=1)

            self.symbol_list = tk.Listbox(
                list_frame,
                font=("Consolas", 12),
                selectmode="browse",
                background="#ffffff",
                foreground="#17202a",
                selectbackground="#18794e",
                selectforeground="#ffffff",
                activestyle="none",
                relief="solid",
                borderwidth=1,
            )
            symbol_scroll = ttk.Scrollbar(
                list_frame,
                orient="vertical",
                command=self.symbol_list.yview,
            )
            self.symbol_list.configure(yscrollcommand=symbol_scroll.set)
            self.symbol_list.grid(row=0, column=0, sticky="nsew")
            symbol_scroll.grid(row=0, column=1, sticky="ns")
            self.symbol_list.bind(
                "<Double-Button-1>",
                lambda _event: self.use_selected_symbol(),
            )

            actions = ttk.Frame(list_frame, padding=(12, 0, 0, 0))
            actions.grid(row=0, column=2, sticky="n")
            ttk.Button(
                actions,
                text="Usar na análise",
                command=self.use_selected_symbol,
            ).pack(fill="x", pady=(0, 8))
            ttk.Button(
                actions,
                text="Analisar selecionado",
                style="Analyze.TButton",
                command=lambda: self.use_selected_symbol(analyze=True),
            ).pack(fill="x", pady=(0, 8))
            ttk.Button(
                actions,
                text="Remover da lista",
                command=self.remove_selected_symbol,
            ).pack(fill="x")

            alias_text = (
                "Atalhos reconhecidos:\n\n"
                "BRA50 / IBOV / IBOVESPA  →  ^BVSP\n"
                "DOLAR / USDBRL           →  BRL=X\n"
                "SP500                    →  ^GSPC\n"
                "NASDAQ                   →  ^IXIC\n"
                "BTC / BITCOIN            →  BTC-USD\n\n"
                "Você também pode informar diretamente um ticker do Yahoo."
            )
            ttk.Label(
                self.assets_tab,
                text=alias_text,
                justify="left",
                style="Subtitle.TLabel",
            ).pack(anchor="w", pady=(14, 0))

        def _refresh_symbol_widgets(self) -> None:
            self.symbol_combo.configure(values=self.symbols)
            self.symbol_list.delete(0, tk.END)
            for symbol in self.symbols:
                self.symbol_list.insert(tk.END, symbol)

        def _persist_symbols(self) -> None:
            try:
                save_symbols(self.symbols)
            except OSError as exc:
                messagebox.showwarning(
                    "Lista não salva",
                    f"Não foi possível salvar a lista de ativos:\n{exc}",
                )

        def add_symbol(self, symbol: str | None = None) -> None:
            value = (symbol or self.new_symbol_var.get()).strip().upper()
            value = value.replace(" ", "")
            if not value:
                messagebox.showinfo("Informe o ativo", "Digite um símbolo.")
                return
            if value not in self.symbols:
                self.symbols.append(value)
                self._refresh_symbol_widgets()
                self._persist_symbols()
            self.new_symbol_var.set("")
            self.symbol_var.set(value)

        def remove_selected_symbol(self) -> None:
            selection = self.symbol_list.curselection()
            if not selection:
                messagebox.showinfo(
                    "Selecione o ativo",
                    "Selecione um ativo da lista para remover.",
                )
                return
            symbol = self.symbol_list.get(selection[0])
            self.symbols = [item for item in self.symbols if item != symbol]
            self._refresh_symbol_widgets()
            self._persist_symbols()

        def restore_default_symbols(self) -> None:
            self.symbols = DEFAULT_SYMBOLS.copy()
            self._refresh_symbol_widgets()
            self._persist_symbols()

        def use_selected_symbol(self, analyze: bool = False) -> None:
            selection = self.symbol_list.curselection()
            if not selection:
                messagebox.showinfo(
                    "Selecione o ativo",
                    "Selecione um ativo da lista.",
                )
                return
            self.symbol_var.set(self.symbol_list.get(selection[0]))
            self.notebook.select(self.analysis_tab)
            if analyze:
                self.start_analysis()

        def clear_output(self) -> None:
            self.output.delete("1.0", tk.END)

        def start_analysis(self) -> None:
            if self.running:
                return

            symbol = self.symbol_var.get().strip().upper().replace(" ", "")
            if not symbol:
                messagebox.showerror("Ativo obrigatório", "Informe um ativo.")
                return

            try:
                count = int(self.count_var.get())
                if count < 30:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Candles inválidos",
                    "Informe um número inteiro igual ou superior a 30.",
                )
                return

            risk_text = self.risk_var.get().strip().replace(",", ".")
            try:
                risk_budget = float(risk_text) if risk_text else None
                if risk_budget is not None and risk_budget <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Risco inválido",
                    "Informe um valor de risco maior que zero ou deixe em branco.",
                )
                return

            if symbol not in self.symbols:
                self.add_symbol(symbol)

            timeframe = self.timeframe_var.get()
            self.running = True
            self.analyze_button.state(["disabled"])
            self.status_var.set(
                f"Buscando candles e analisando {symbol}. Aguarde..."
            )
            self.output.delete("1.0", tk.END)
            self.output.insert(
                "1.0",
                f"Processando {symbol} em {timeframe}...\n",
            )

            worker = threading.Thread(
                target=self._analysis_worker,
                args=(symbol, timeframe, count, risk_budget),
                daemon=True,
            )
            worker.start()

        def _analysis_worker(
            self,
            symbol: str,
            timeframe: str,
            count: int,
            risk_budget: float | None,
        ) -> None:
            try:
                report = build_report(
                    symbol,
                    timeframe,
                    count,
                    risk_budget,
                )
                self.result_queue.put((True, report))
            except Exception as exc:
                self.result_queue.put(
                    (
                        False,
                        f"[ERRO] {exc}\n\n"
                        "Se o Yahoo estiver limitando consultas, aguarde alguns "
                        "minutos e tente novamente.",
                    )
                )

        def _poll_results(self) -> None:
            try:
                while True:
                    success, text = self.result_queue.get_nowait()
                    self.running = False
                    self.analyze_button.state(["!disabled"])
                    self.output.delete("1.0", tk.END)
                    self.output.insert("1.0", text)
                    self.output.see("1.0")
                    self.status_var.set(
                        "Análise concluída."
                        if success
                        else "Não foi possível concluir a análise."
                    )
                    if not success:
                        messagebox.showerror("Falha na análise", text)
            except queue.Empty:
                pass
            finally:
                self.root.after(150, self._poll_results)

    root = tk.Tk()
    DayTradeApp(root)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Analisador standalone de SMC, Price Action, EMAs e VWAP. "
            "Sem ativo, abre a interface gráfica."
        )
    )
    parser.add_argument(
        "symbol",
        nargs="?",
        help="Ticker, por exemplo VALE3 ou PETR4; omita para abrir a interface.",
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
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Abre a interface gráfica.",
    )
    args = parser.parse_args()

    if args.gui or args.symbol is None:
        try:
            launch_gui()
        except RuntimeError as exc:
            parser.exit(1, f"[ERRO] {exc}\n")
        return

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
