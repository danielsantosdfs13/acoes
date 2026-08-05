#!/usr/bin/env python3
"""
Compara o motor ANTES e DEPOIS do refactor do AnalysisParams.

Este repo não tem suíte de testes, e o refactor que transformou os
literais do motor em campos de um dataclass é exatamente o tipo de
mudança que falha em silêncio: um score que muda de 72.4 pra 72.3 não
quebra nada visível, mas invalida toda comparação histórica de sinal
depois — e ninguém percebe por semanas. Este script é a rede.

A ideia: buscar os candles UMA VEZ só (senão a variação do próprio
mercado entre uma busca e outra apareceria como diferença), e rodar
`analyze()` dos DOIS motores sobre exatamente o mesmo DataFrame. Com o
perfil padrão, a saída tem que ser idêntica campo a campo.

Uso:
    git worktree add /tmp/base HEAD
    python scripts/conferir-refactor-params.py --base /tmp/base

    # re-rodar sem gastar cota do Yahoo (usa o cache da primeira vez):
    python scripts/conferir-refactor-params.py --base /tmp/base

Sai com código 1 se qualquer diferença aparecer.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import pickle
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
TIMEFRAMES_CONFERIDOS = ("M15", "H1", "H4", "D1", "W1")


def carregar_motor(caminho: Path, apelido: str):
    """Importa um `daytrade_smc.py` específico sob um nome próprio.

    Os dois motores têm o mesmo nome de módulo, então `import` normal
    serviria só pro primeiro — daí o carregamento por caminho.
    """
    arquivo = caminho / "daytrade_smc.py"
    if not arquivo.exists():
        raise SystemExit(f"Não achei {arquivo}")
    spec = importlib.util.spec_from_file_location(apelido, arquivo)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[apelido] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def buscar_candles(motor, simbolos: list[str], cache: Path) -> dict:
    """Baixa (ou lê do cache) os candles usados nas duas execuções."""
    if cache.exists():
        with cache.open("rb") as f:
            dados = pickle.load(f)
        print(f"Cache: {len(dados)} séries lidas de {cache}\n")
        return dados

    dados = {}
    for simbolo in simbolos:
        for tf in TIMEFRAMES_CONFERIDOS:
            count = motor.DEFAULT_TF_COUNTS.get(tf, 200)
            try:
                df = motor.fetch_ohlcv(simbolo, tf, count)
            except Exception as exc:  # noqa: BLE001 — um ativo sem dado não invalida os outros
                print(f"  ignorando {simbolo} {tf}: {exc}")
                continue
            if len(df) < 30:
                print(f"  ignorando {simbolo} {tf}: só {len(df)} candles")
                continue
            dados[(simbolo, tf)] = df
            print(f"  ok {simbolo} {tf}: {len(df)} candles")

    with cache.open("wb") as f:
        pickle.dump(dados, f)
    print(f"\nCache gravado em {cache}\n")
    return dados


def normalizar(sinais: list) -> list[dict]:
    """Reduz os Signal a dicionários puros, comparáveis com ==."""
    return [dataclasses.asdict(sinal) for sinal in sinais]


def diferencas(antes: dict, depois: dict, prefixo: str = "") -> list[str]:
    """Caminha nos dois dicionários e devolve os campos que divergem."""
    achados = []
    for chave in sorted(set(antes) | set(depois)):
        caminho = f"{prefixo}{chave}"
        a, d = antes.get(chave), depois.get(chave)
        if isinstance(a, dict) and isinstance(d, dict):
            achados.extend(diferencas(a, d, f"{caminho}."))
        elif isinstance(a, list) and isinstance(d, list) and len(a) == len(d):
            for i, (ai, di) in enumerate(zip(a, d)):
                if isinstance(ai, dict) and isinstance(di, dict):
                    achados.extend(diferencas(ai, di, f"{caminho}[{i}]."))
                elif ai != di:
                    achados.append(f"{caminho}[{i}]: {ai!r} -> {di!r}")
        elif a != d:
            achados.append(f"{caminho}: {a!r} -> {d!r}")
    return achados


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="worktree com o motor de referência")
    parser.add_argument("--cache", default=None, help="arquivo de cache dos candles")
    parser.add_argument("--simbolos", default=None, help="lista separada por vírgula")
    args = parser.parse_args()

    base = Path(args.base).resolve()
    cache = Path(args.cache) if args.cache else base.parent / "candles-conferencia.pkl"

    antes = carregar_motor(base, "motor_antes")
    depois = carregar_motor(RAIZ, "motor_depois")

    simbolos = args.simbolos.split(",") if args.simbolos else list(depois.DEFAULT_SYMBOLS)
    print(f"Motor antes : {base}")
    print(f"Motor depois: {RAIZ}")
    print(f"Ativos      : {', '.join(simbolos)}\n")

    dados = buscar_candles(antes, simbolos, cache)
    if not dados:
        print("Nenhuma série disponível — nada foi conferido.")
        return 1

    total = 0
    divergentes = 0
    for (simbolo, tf), df in sorted(dados.items()):
        # cópias separadas: se um dos motores mutasse o DataFrame, o outro
        # herdaria a mutação e a conferência daria falso-positivo de "igual"
        _, sinais_antes = antes.analyze(df.copy())
        _, sinais_depois = depois.analyze(df.copy())

        a = normalizar(sinais_antes)
        d = normalizar(sinais_depois)
        total += 1

        if a == d:
            continue

        divergentes += 1
        print(f"DIFERENÇA em {simbolo} {tf}:")
        if len(a) != len(d):
            print(f"  número de sinais: {len(a)} -> {len(d)}")
            continue
        for sinal_antes, sinal_depois in zip(a, d):
            for linha in diferencas(sinal_antes, sinal_depois):
                print(f"  [{sinal_antes['name']}] {linha}")
        print()

    print(f"\n{total} séries conferidas, {divergentes} com diferença.")
    if divergentes:
        print("FALHOU — o perfil padrão NÃO é um no-op.")
        return 1
    print("OK — o perfil padrão reproduz o motor anterior campo a campo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
