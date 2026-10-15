"""
executor/test_carteiras.py

Testes para o módulo de carteiras (SOMADOS/RENKO).

Executar: python executor/test_carteiras.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import carteiras


def test_carteira_do_setup():
    """Testa mapeamento setup -> carteira."""
    assert carteiras.carteira_do_setup("VWAP") == "SOMADOS"
    assert carteiras.carteira_do_setup("SMC") == "SOMADOS"
    assert carteiras.carteira_do_setup("Médias Móveis") == "SOMADOS"
    assert carteiras.carteira_do_setup("Confluência") == "SOMADOS"
    assert carteiras.carteira_do_setup("RENKO_TOURO_URSO") == "RENKO"
    # Setup desconhecido vai para SOMADOS
    assert carteiras.carteira_do_setup("DESCONHECIDO") == "SOMADOS"
    print("✅ test_carteira_do_setup passed")


def test_limites_da_carteira():
    """Testa limites das carteiras."""
    limites_somados = carteiras.limites_da_carteira("SOMADOS")
    limites_renko = carteiras.limites_da_carteira("RENKO")

    assert limites_somados.max_posicoes == 5
    assert limites_somados.max_trades_dia == 20
    assert limites_somados.perda_diaria_r == 4.0

    assert limites_renko.max_posicoes == 5
    assert limites_renko.max_trades_dia == 20
    assert limites_renko.perda_diaria_r == 4.0

    print("✅ test_limites_da_carteira passed")


def test_pode_operar_somados():
    """Testa limites da carteira SOMADOS."""
    # Reset estado
    carteiras.resetar_dia()

    # Inicialmente pode operar
    pode, motivo = carteiras.pode_operar("SOMADOS", "VALE3")
    assert pode == True
    assert motivo == ""

    # Abre posição
    carteiras.registrar_abertura("SOMADOS", "VALE3", {"setup": "VWAP"})

    # Não pode operar o mesmo ativo (disputa vaga)
    pode, motivo = carteiras.pode_operar("SOMADOS", "VALE3")
    assert pode == False
    assert motivo == "ATIVO_POSICIONADO"

    # Pode operar outro ativo
    pode, motivo = carteiras.pode_operar("SOMADOS", "PETR4")
    assert pode == True

    print("✅ test_pode_operar_somados passed")


def test_pode_operar_renko_independente():
    """Testa que RENKO não disputa vaga com SOMADOS."""
    carteiras.resetar_dia()

    # Abre posição SOMADOS em VALE3
    carteiras.registrar_abertura("SOMADOS", "VALE3", {"setup": "VWAP"})

    # RENKO pode operar VALE3 ao mesmo tempo
    pode, motivo = carteiras.pode_operar("RENKO", "VALE3")
    assert pode == True, f"RENKO deveria poder operar VALE3, mas foi bloqueado: {motivo}"

    print("✅ test_pode_operar_renko_independente passed")


def test_limite_posicoes():
    """Testa limite de posições por carteira."""
    carteiras.resetar_dia()

    # Abre 5 posições em SOMADOS
    for i in range(5):
        carteiras.registrar_abertura("SOMADOS", f"ATIVO{i}", {"setup": "VWAP"})

    # Sexta posição deve ser bloqueada
    pode, motivo = carteiras.pode_operar("SOMADOS", "ATIVO5")
    assert pode == False
    assert motivo == "MAX_POSICOES"

    # RENKO ainda pode operar (carteira independente)
    pode, motivo = carteiras.pode_operar("RENKO", "ATIVO5")
    assert pode == True

    print("✅ test_limite_posicoes passed")


def test_limite_perda_diaria():
    """Testa limite de perda diária por carteira."""
    carteiras.resetar_dia()

    # Simula perda de 4R em SOMADOS
    carteiras.registrar_abertura("SOMADOS", "VALE3", {"setup": "VWAP"})
    carteiras.registrar_fechamento("SOMADOS", "VALE3", -4.0)

    # Nova operação deve ser bloqueada
    pode, motivo = carteiras.pode_operar("SOMADOS", "PETR4")
    assert pode == False
    assert motivo == "LIMITE_PERDA_DIARIA"

    # RENKO não é afetado
    pode, motivo = carteiras.pode_operar("RENKO", "PETR4")
    assert pode == True

    print("✅ test_limite_perda_diaria passed")


def test_kill_switch():
    """Testa kill switch por carteira."""
    carteiras.resetar_dia()

    # Abre posição
    carteiras.registrar_abertura("SOMADOS", "VALE3", {"setup": "VWAP"})

    # Ativa kill switch
    simbolos = carteiras.zerar_carteira("SOMADOS")
    assert "VALE3" in simbolos

    # Não pode operar
    pode, motivo = carteiras.pode_operar("SOMADOS", "PETR4")
    assert pode == False
    assert motivo == "KILL_SWITCH"

    # Libera
    carteiras.liberar_carteira("SOMADOS")
    pode, motivo = carteiras.pode_operar("SOMADOS", "PETR4")
    assert pode == True

    print("✅ test_kill_switch passed")


def test_rebaixamento():
    """Testa critérios de rebaixamento."""
    # Média negativa
    resultados = [-1.0, -0.5, -1.5, -0.8, -1.2]  # média -1.0
    deve, motivo = carteiras.verificar_rebaixamento("SOMADOS", resultados)
    assert deve == True
    assert "MÉDIA" in motivo

    # Média positiva
    resultados = [1.0, 0.5, 1.5, 0.8, 1.2]  # média +1.0
    deve, motivo = carteiras.verificar_rebaixamento("SOMADOS", resultados)
    assert deve == False

    # Poucos resultados
    resultados = [1.0, 0.5]
    deve, motivo = carteiras.verificar_rebaixamento("SOMADOS", resultados)
    assert deve == False

    print("✅ test_rebaixamento passed")


def test_resumo_carteira():
    """Testa resumo da carteira."""
    carteiras.resetar_dia()

    carteiras.registrar_abertura("SOMADOS", "VALE3", {"setup": "VWAP"})
    carteiras.registrar_abertura("SOMADOS", "PETR4", {"setup": "SMC"})
    carteiras.registrar_fechamento("SOMADOS", "VALE3", 1.5)

    resumo = carteiras.resumo_carteira("SOMADOS")
    assert resumo["carteira"] == "SOMADOS"
    assert resumo["posicoes_abertas"] == 1  # PETR4 ainda aberta
    assert resumo["trades_hoje"] == 2
    assert abs(resumo["resultado_dia_r"] - 1.5) < 0.01

    print("✅ test_resumo_carteira passed")


if __name__ == "__main__":
    print("=" * 60)
    print("TESTES: CARTEIRAS (SOMADOS/RENKO)")
    print("=" * 60)
    print()

    test_carteira_do_setup()
    test_limites_da_carteira()
    test_pode_operar_somados()
    test_pode_operar_renko_independente()
    test_limite_posicoes()
    test_limite_perda_diaria()
    test_kill_switch()
    test_rebaixamento()
    test_resumo_carteira()

    print()
    print("=" * 60)
    print("TODOS OS TESTES DE CARTEIRAS PASSARAM ✓")
    print("=" * 60)
