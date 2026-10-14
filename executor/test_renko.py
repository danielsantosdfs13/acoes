"""
executor/test_renko.py

Testes para o setup RENKO_TOURO_URSO.

Executar: python executor/test_renko.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from datetime import datetime, timezone

import daytrade_smc
from daytrade_smc import (
    build_renko_bricks,
    compute_renko_vwap,
    renko_touro_urso_signal,
    AnalysisParams,
    Direction,
)


def test_renko_bricks_basic():
    """Testa construção básica de tijolos Renko."""
    # Dados com tendência clara
    dates = pd.date_range('2026-01-01', periods=100, freq='1min', tz='UTC')
    # Preço subindo gradualmente
    prices = np.linspace(100, 110, 100)
    df = pd.DataFrame({
        'open': prices,
        'high': prices + 0.05,
        'low': prices - 0.05,
        'close': prices,
        'volume': np.full(100, 1000.0),
    }, index=dates)

    n = 12
    tick = 0.01
    body_size = (n - 1) * tick  # 0.11
    continuation = n * tick      # 0.12

    bricks = build_renko_bricks(df, n=n, tick_size=tick)

    print(f"  Tijolos construídos: {len(bricks)}")
    assert len(bricks) > 0, "Deveria construir pelo menos 1 tijolo"

    # Verifica corpo dos tijolos
    for brick in bricks[:5]:
        body = abs(brick.close - brick.open)
        assert abs(body - body_size) < 0.001, \
            f"Corpo do tijolo {body:.4f} != esperado {body_size:.4f}"

    print("✅ test_renko_bricks_basic passed")


def test_renko_bricks_reversal():
    """Testa reversão de tijolos Renko."""
    # Dados com reversão: sobe 10, depois desce 20
    dates = pd.date_range('2026-01-01', periods=200, freq='1min', tz='UTC')
    prices_up = np.linspace(100, 110, 100)
    prices_down = np.linspace(110, 90, 100)
    prices = np.concatenate([prices_up, prices_down])

    df = pd.DataFrame({
        'open': prices,
        'high': prices + 0.05,
        'low': prices - 0.05,
        'close': prices,
        'volume': np.full(200, 1000.0),
    }, index=dates)

    n = 12
    tick = 0.01
    reversal = (2 * n - 1) * tick  # 0.23

    bricks = build_renko_bricks(df, n=n, tick_size=tick)

    print(f"  Tijolos construídos: {len(bricks)}")
    assert len(bricks) > 0

    # Deveria ter tanto tijolos de ALTA quanto de BAIXA
    altas = sum(1 for b in bricks if b.direction == "ALTA")
    baixas = sum(1 for b in bricks if b.direction == "BAIXA")
    print(f"  Altas: {altas}, Baixas: {baixas}")
    assert altas > 0, "Deveria ter tijolos de ALTA"
    assert baixas > 0, "Deveria ter tijolos de BAIXA"

    print("✅ test_renko_bricks_reversal passed")


def test_renko_signal_disabled():
    """Testa que o sinal retorna NEUTRO quando desabilitado."""
    dates = pd.date_range('2026-01-01', periods=100, freq='1min', tz='UTC')
    prices = np.linspace(100, 110, 100)
    df = pd.DataFrame({
        'open': prices,
        'high': prices + 0.05,
        'low': prices - 0.05,
        'close': prices,
        'volume': np.full(100, 1000.0),
    }, index=dates)

    params = AnalysisParams(renko_touro_urso_enabled=False)
    context = daytrade_smc.build_context(df, params)
    signal = renko_touro_urso_signal(context)

    assert signal.direction == Direction.NEUTRAL
    assert "desabilitado" in signal.setup
    print("✅ test_renko_signal_disabled passed")


def test_renko_signal_enabled():
    """Testa que o sinal é gerado quando habilitado."""
    dates = pd.date_range('2026-01-01', periods=200, freq='1min', tz='UTC')
    # Dados com tendência clara
    prices = np.linspace(100, 110, 200)
    df = pd.DataFrame({
        'open': prices,
        'high': prices + 0.05,
        'low': prices - 0.05,
        'close': prices,
        'volume': np.full(200, 1000.0),
    }, index=dates)

    params = AnalysisParams(renko_touro_urso_enabled=True)
    context = daytrade_smc.build_context(df, params)
    signal = renko_touro_urso_signal(context)

    # Mesmo sem sinal válido, não deve lançar exceção
    print(f"  Signal: {signal.direction}, setup: {signal.setup}")
    print("✅ test_renko_signal_enabled passed")


def test_renko_analysis_params():
    """Testa que os parâmetros RENKO existem no AnalysisParams."""
    params = AnalysisParams()

    assert hasattr(params, 'renko_touro_urso_enabled')
    assert hasattr(params, 'renko_n')
    assert hasattr(params, 'renko_tick_size')
    assert hasattr(params, 'renko_media_tijolos')
    assert hasattr(params, 'renko_lookback_tentativa')
    assert hasattr(params, 'renko_alvo_tijolos')
    assert hasattr(params, 'renko_janela_inicio')
    assert hasattr(params, 'renko_janela_fim')
    assert hasattr(params, 'renko_zerar_ate')
    assert hasattr(params, 'renko_liberado_conta_real')

    # Verifica valores padrão
    assert params.renko_touro_urso_enabled == False
    assert params.renko_n == 12
    assert params.renko_alvo_tijolos == 2
    assert params.renko_liberado_conta_real == False

    print("✅ test_renko_analysis_params passed")


def test_renko_in_modalities():
    """Testa que RENKO_TOURO_URSO está em MODALITIES."""
    assert "RENKO_TOURO_URSO" in daytrade_smc.MODALITIES
    print("✅ test_renko_in_modalities passed")


def test_renko_vwap():
    """Testa cálculo do VWAP Renko."""
    dates = pd.date_range('2026-01-01', periods=100, freq='1min', tz='UTC')
    prices = np.linspace(100, 110, 100)
    df = pd.DataFrame({
        'open': prices,
        'high': prices + 0.05,
        'low': prices - 0.05,
        'close': prices,
        'volume': np.full(100, 1000.0),
    }, index=dates)

    # VWAP até o final
    vwap = compute_renko_vwap(df, dates[-1])
    print(f"  VWAP: {vwap:.2f}")
    assert 100 <= vwap <= 110, f"VWAP {vwap} fora do range"

    # VWAP até o meio
    vwap_meio = compute_renko_vwap(df, dates[50])
    print(f"  VWAP até meio: {vwap_meio:.2f}")
    assert 100 <= vwap_meio <= 106, f"VWAP meio {vwap_meio} fora do range"

    print("✅ test_renko_vwap passed")


if __name__ == "__main__":
    print("=" * 60)
    print("TESTES: RENKO_TOURO_URSO")
    print("=" * 60)
    print()

    test_renko_bricks_basic()
    test_renko_bricks_reversal()
    test_renko_signal_disabled()
    test_renko_signal_enabled()
    test_renko_analysis_params()
    test_renko_in_modalities()
    test_renko_vwap()

    print()
    print("=" * 60)
    print("TODOS OS TESTES RENKO PASSARAM ✓")
    print("=" * 60)
