"""
executor/test_sinais.py

Testes para signal_logger e parametros_aprendidos.

Executar: python -m pytest executor/test_sinais.py -v
Ou simplesmente: python executor/test_sinais.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Adiciona o diretório ao path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signal_logger
import parametros_aprendidos


def test_signal_logger_grava_csv():
    """Testa gravação básica no CSV."""
    with tempfile.TemporaryDirectory() as tmpdir:
        signal_logger.LOG_DIR = Path(tmpdir)
        signal_logger._cache = None
        signal_logger._cache_mtime = None

        # Grava um sinal
        signal_logger.gravar_sinal(
            id="123",
            timestamp_sinal=datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc),
            ativo="VALE3",
            setup="VWAP",
            variante="M15",
            modo="LIVE",
            elegivel=True,
            decisao="TRADE",
            motivo="ENVIADA",
            direcao="LONG",
            regime="TENDENCIA",
            er=1.5,
            adx=30.0,
            atr_pct=2.0,
            vol_percentil=75.0,
            vwap_dist_atr=0.5,
            vwap_slope_atr=0.3,
            volume_rel=1.8,
            score=85.0,
            entrada=68.50,
            stop=67.80,
            alvo=69.90,
            custo_pct=0.0005,
        )

        # Verifica se o arquivo foi criado
        csv_path = Path(tmpdir) / "sinais.csv"
        assert csv_path.exists(), "Arquivo CSV não foi criado"

        # Lê e verifica
        with open(csv_path, encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) == 2, f"Esperava 2 linhas (cabeçalho + dado), got {len(lines)}"
        assert "id" in lines[0], "Cabeçalho não contém 'id'"
        assert "123" in lines[1], "Linha não contém o id"

        # Verifica timestamp com fuso
        assert "-03:00" in lines[1], "Timestamp não tem fuso -03:00"

        print("✅ test_signal_logger_grava_csv passed")


def test_signal_logger_simular_saida():
    """Testa simulação de saída SHADOW."""
    # Caso 1: Alvo atingido (COMPRA)
    velas = [
        {"open": 100, "high": 102, "low": 99, "close": 101},
        {"open": 101, "high": 105, "low": 100, "close": 104},
    ]
    motivo, preco, resultado = signal_logger.simular_saida(
        direcao="COMPRA",
        entrada=100.0,
        stop=98.0,
        alvo=104.0,
        velas=velas,
    )
    assert motivo == "ALVO", f"Esperava ALVO, got {motivo}"
    assert preco == 104.0, f"Esperava preço 104, got {preco}"
    assert resultado > 0, f"Esperava resultado positivo, got {resultado}"
    print(f"  ALVO: resultado={resultado:.2f}R")

    # Caso 2: Stop atingido (COMPRA)
    velas = [
        {"open": 100, "high": 101, "low": 97, "close": 98},
    ]
    motivo, preco, resultado = signal_logger.simular_saida(
        direcao="COMPRA",
        entrada=100.0,
        stop=98.0,
        alvo=104.0,
        velas=velas,
    )
    assert motivo == "STOP", f"Esperava STOP, got {motivo}"
    assert resultado < 0, f"Esperava resultado negativo, got {resultado}"
    print(f"  STOP: resultado={resultado:.2f}R")

    # Caso 3: Saída por tempo
    velas = [
        {"open": 100, "high": 101, "low": 99, "close": 100.5},
        {"open": 100.5, "high": 101, "low": 100, "close": 100.2},
    ]
    motivo, preco, resultado = signal_logger.simular_saida(
        direcao="COMPRA",
        entrada=100.0,
        stop=98.0,
        alvo=104.0,
        velas=velas,
        max_velas=2,
    )
    assert motivo == "TEMPO", f"Esperava TEMPO, got {motivo}"
    print(f"  TEMPO: resultado={resultado:.2f}R")

    print("✅ test_signal_logger_simular_saida passed")


def test_parametros_ausente_sem_veto():
    """Testa que JSON ausente não trava e opera sem veto."""
    with tempfile.TemporaryDirectory() as tmpdir:
        parametros_aprendidos.PARAMS_PATH = Path(tmpdir) / "nao_existe.json"
        parametros_aprendidos._cache = None
        parametros_aprendidos._cache_mtime = None
        parametros_aprendidos._alerta_emitido = False

        # Deve retornar LIVE (default)
        modo = parametros_aprendidos.modo_do_setup("VWAP")
        assert modo == "LIVE", f"Esperava LIVE, got {modo}"

        # Deve retornar sem veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="TENDENCIA",
            direcao="COMPRA",
            vol_percentil=50.0,
        )
        assert veto == False, f"Esperava veto=False, got {veto}"
        assert motivo == "", f"Esperava motivo vazio, got {motivo}"

        print("✅ test_parametros_ausente_sem_veto passed")


def test_parametros_veto_vol_percentil():
    """Testa veto por vol_percentil_min."""
    with tempfile.TemporaryDirectory() as tmpdir:
        params = {
            "versao": 1,
            "modos": {},
            "regras": {
                "vol_percentil_min": 30,
            },
        }
        params_path = Path(tmpdir) / "params.json"
        params_path.write_text(json.dumps(params))

        parametros_aprendidos.PARAMS_PATH = params_path
        parametros_aprendidos._cache = None
        parametros_aprendidos._cache_mtime = None
        parametros_aprendidos._alerta_emitido = False

        # Vol percentil abaixo do mínimo: veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="TENDENCIA",
            direcao="COMPRA",
            vol_percentil=20.0,
        )
        assert veto == True, f"Esperava veto=True, got {veto}"
        assert "VOL_PERCENTIL" in motivo, f"Esperava VOL_PERCENTIL no motivo, got {motivo}"
        print(f"  Veto vol_percentil: {motivo}")

        # Vol percentil acima do mínimo: sem veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="TENDENCIA",
            direcao="COMPRA",
            vol_percentil=50.0,
        )
        assert veto == False, f"Esperava veto=False, got {veto}"

        print("✅ test_parametros_veto_vol_percentil passed")


def test_parametros_veto_bloqueio():
    """Testa veto por bloqueio setup × regime."""
    with tempfile.TemporaryDirectory() as tmpdir:
        params = {
            "versao": 1,
            "modos": {},
            "regras": {
                "bloqueios": [
                    {"setup": "SMC", "regime": "LATERAL", "motivo": "SMC lateral ruim"},
                ],
            },
        }
        params_path = Path(tmpdir) / "params.json"
        params_path.write_text(json.dumps(params))

        parametros_aprendidos.PARAMS_PATH = params_path
        parametros_aprendidos._cache = None
        parametros_aprendidos._cache_mtime = None
        parametros_aprendidos._alerta_emitido = False

        # SMC + LATERAL: veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="SMC",
            regime="LATERAL",
            direcao="COMPRA",
        )
        assert veto == True, f"Esperava veto=True, got {veto}"
        assert "BLOQUEIO" in motivo, f"Esperava BLOQUEIO no motivo, got {motivo}"
        print(f"  Veto bloqueio: {motivo}")

        # SMC + TENDENCIA: sem veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="SMC",
            regime="TENDENCIA",
            direcao="COMPRA",
        )
        assert veto == False, f"Esperava veto=False, got {veto}"

        # VWAP + LATERAL: sem veto (não está no bloqueio)
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="LATERAL",
            direcao="COMPRA",
        )
        assert veto == False, f"Esperava veto=False, got {veto}"

        print("✅ test_parametros_veto_bloqueio passed")


def test_parametros_modos():
    """Testa configuração de modos."""
    with tempfile.TemporaryDirectory() as tmpdir:
        params = {
            "versao": 1,
            "modos": {
                "VWAP": "LIVE",
                "SMC": "PAPER",
                "CONFLUENCIA": "SHADOW",
            },
        }
        params_path = Path(tmpdir) / "params.json"
        params_path.write_text(json.dumps(params))

        parametros_aprendidos.PARAMS_PATH = params_path
        parametros_aprendidos._cache = None
        parametros_aprendidos._cache_mtime = None
        parametros_aprendidos._alerta_emitido = False

        assert parametros_aprendidos.modo_do_setup("VWAP") == "LIVE"
        assert parametros_aprendidos.modo_do_setup("SMC") == "PAPER"
        assert parametros_aprendidos.modo_do_setup("CONFLUENCIA") == "SHADOW"
        assert parametros_aprendidos.modo_do_setup("CHATGPT") == "LIVE"  # default

        print("✅ test_parametros_modos passed")


def test_parametros_json_invalido():
    """Testa que JSON inválido não trava."""
    with tempfile.TemporaryDirectory() as tmpdir:
        params_path = Path(tmpdir) / "params.json"
        params_path.write_text("{{{json inválido")

        parametros_aprendidos.PARAMS_PATH = params_path
        parametros_aprendidos._cache = None
        parametros_aprendidos._cache_mtime = None
        parametros_aprendidos._alerta_emitido = False

        # Deve retornar LIVE (default) mesmo com JSON inválido
        modo = parametros_aprendidos.modo_do_setup("VWAP")
        assert modo == "LIVE", f"Esperava LIVE, got {modo}"

        # Deve retornar sem veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="TENDENCIA",
            direcao="COMPRA",
        )
        assert veto == False, f"Esperava veto=False, got {veto}"

        print("✅ test_parametros_json_invalido passed")


def test_parametros_modelo():
    """Testa veto pelo modelo logístico."""
    with tempfile.TemporaryDirectory() as tmpdir:
        params = {
            "versao": 1,
            "modos": {},
            "regras": {},
            "modelo": {
                "corte": 0.35,
                "imput": 0.0,
                "features": ["vol_percentil", "vwap_dist_atr"],
                "pesos": [0.0, 0.5, 0.5],  # w0=0.0, w1=0.5, w2=0.5
                "mu": [50.0, 0.0],         # mu1=50, mu2=0 (1 por feature)
                "sd": [25.0, 1.0],         # sd1=25, sd2=1 (1 por feature)
            },
        }
        params_path = Path(tmpdir) / "params.json"
        params_path.write_text(json.dumps(params))

        parametros_aprendidos.PARAMS_PATH = params_path
        parametros_aprendidos._cache = None
        parametros_aprendidos._cache_mtime = None
        parametros_aprendidos._alerta_emitido = False

        # Calcular z manualmente:
        # z = 0.0 + 0.5*((75-50)/25) + 0.5*((1.0-0)/1) = 0.0 + 0.5 + 0.5 = 1.0
        # limiar = ln(0.35/0.65) = -0.619
        # 1.0 > -0.619 => sem veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="TENDENCIA",
            direcao="COMPRA",
            vol_percentil=75.0,
            vwap_dist_atr=1.0,
        )
        assert veto == False, f"Esperava veto=False, got {veto}"
        print(f"  Modelo z alto: veto={veto}")

        # z baixo: vol_percentil=25, vwap_dist_atr=-1
        # z = 0.0 + 0.5*((25-50)/25) + 0.5*((-1-0)/1) = 0.0 - 0.5 - 0.5 = -1.0
        # -1.0 < -0.619 => veto
        veto, motivo = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="TENDENCIA",
            direcao="COMPRA",
            vol_percentil=25.0,
            vwap_dist_atr=-1.0,
        )
        assert veto == True, f"Esperava veto=True, got {veto}"
        print(f"  Modelo z baixo: veto={veto}, motivo={motivo}")

        print("✅ test_parametros_modelo passed")


def test_veto_nao_aumenta_ordens():
    """Testa que veto NUNCA aumenta o número de ordens.

    O veto apenas REDUZ: sinal vetado vira SHADOW, não vira ordem.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Configura veto agressivo
        params = {
            "versao": 1,
            "modos": {},  # Todos LIVE
            "regras": {
                "vol_percentil_min": 100,  # Veta tudo
            },
        }
        params_path = Path(tmpdir) / "params.json"
        params_path.write_text(json.dumps(params))

        parametros_aprendidos.PARAMS_PATH = params_path
        parametros_aprendidos._cache = None
        parametros_aprendidos._cache_mtime = None
        parametros_aprendidos._alerta_emitido = False

        # Qualquer sinal deve ser vetado
        veto, _ = parametros_aprendidos.verificar_veto(
            setup="VWAP",
            regime="TENDENCIA",
            direcao="COMPRA",
            vol_percentil=50.0,
        )
        assert veto == True, "Veto agressivo deveria vetar tudo"

        # Verifica que modo é LIVE (não muda)
        modo = parametros_aprendidos.modo_do_setup("VWAP")
        assert modo == "LIVE", "Modo não deve mudar com veto"

        print("✅ test_veto_nao_aumenta_ordens passed")


if __name__ == "__main__":
    print("=" * 60)
    print("TESTES: signal_logger e parametros_aprendidos")
    print("=" * 60)
    print()

    test_signal_logger_grava_csv()
    test_signal_logger_simular_saida()
    test_parametros_ausente_sem_veto()
    test_parametros_veto_vol_percentil()
    test_parametros_veto_bloqueio()
    test_parametros_modos()
    test_parametros_json_invalido()
    test_parametros_modelo()
    test_veto_nao_aumenta_ordens()

    print()
    print("=" * 60)
    print("TODOS OS TESTES PASSARAM ✓")
    print("=" * 60)
