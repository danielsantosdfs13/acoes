#!/usr/bin/env python3
"""
Lê o JSON do `varrer-parametros.py` e responde as perguntas de calibragem.

Três recortes, nesta ordem de importância:

  --rr      qual `rr_alvo_1` paga mais (a decisão de 2026-08-12: os perfis
            estavam em 0,8 e 1,0, calibrados para taxa de acerto, o que
            garante expectativa negativa abaixo de 56% e 50%)
  --rvol    o RVOL separa ganho de perda? Ele é calculado pelo motor
            (`context.rvol`) e hoje não pontua NADA — só é impresso no CLI e
            guardado em `detalhes`
  --stop    qual `stop_minimo_atr` paga mais

REGRA DE LEITURA, e é o ponto do script inteiro: decidir por
`expectativa` (R médio), nunca por `taxa` isolada. As duas puxam para lados
opostos — alvo mais curto sobe a taxa e afunda a expectativa —, e
"assertividade" nomeia justamente a que não paga. Por isso as duas aparecem
sempre lado a lado, junto de `%aberto`: alvo mais distante engorda a fatia
que nunca resolve, e uma expectativa alta sobre poucos resolvidos é miragem.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def resumo(linhas: list[dict]) -> dict | None:
    """Taxa E expectativa, sempre juntas, com quantos ficaram em aberto.

    `resolvidos` é o denominador da taxa: os EM_ABERTO não têm desfecho e
    ficam fora de toda estatística (o mesmo `resultado NOT IN (...)` que o
    `_STATS_BASE` da API já aplica). Mostrar a taxa sobre `n` seria mentira."""
    resolvidos = [x for x in linhas if x["resultado"] in ("ALVO_1", "ALVO_2", "STOP")]
    if not resolvidos:
        return None
    acertos = sum(1 for x in resolvidos if x["resultado"].startswith("ALVO"))
    rs = [x["r"] for x in resolvidos if x["r"] is not None]
    aberto = sum(1 for x in linhas if x["resultado"] == "EM_ABERTO")
    return {
        "n": len(linhas),
        "resolvidos": len(resolvidos),
        "taxa": acertos / len(resolvidos),
        "expectativa": statistics.mean(rs) if rs else 0.0,
        "total_r": sum(rs),
        "aberto": aberto / len(linhas) if linhas else 0.0,
    }


def tabela(titulo: str, grupos: dict[str, list[dict]], rotulo: str) -> None:
    linhas = {k: resumo(v) for k, v in grupos.items()}
    linhas = {k: v for k, v in linhas.items() if v}
    if not linhas:
        print(f"\n{titulo}\n  (sem dados)")
        return
    melhor = max(linhas.values(), key=lambda r: r["expectativa"])["expectativa"]
    print(f"\n{titulo}")
    print(f"  {rotulo:<26}{'resolv':>8}{'taxa':>8}{'expR':>9}{'total R':>10}{'%aberto':>9}")
    for chave in sorted(linhas):
        r = linhas[chave]
        marca = "  <<<" if r["expectativa"] == melhor else ""
        print(f"  {chave:<26}{r['resolvidos']:>8}{r['taxa']:>7.1%}"
              f"{r['expectativa']:>+9.3f}{r['total_r']:>+10.0f}{r['aberto']:>8.0%}{marca}")


def faixa_rvol(v: float) -> str:
    """Cortes ancorados no gate que já existe: `candle_patterns` só reconhece
    padrão com volume >= 1,3x a média. A pergunta é se esse mesmo corte
    separa ganho de perda NAS OUTRAS leituras, que hoje ignoram volume."""
    if v < 0.8:
        return "a) < 0,8"
    if v < 1.0:
        return "b) 0,8-1,0"
    if v < 1.3:
        return "c) 1,0-1,3"
    if v < 2.0:
        return "d) 1,3-2,0 (gate atual)"
    return "e) >= 2,0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arquivo", default=str(RAIZ / "varredura-resultado.json"))
    parser.add_argument("--rr", action="store_true", help="expectativa por rr_alvo_1")
    parser.add_argument("--rvol", action="store_true", help="o RVOL separa ganho de perda?")
    parser.add_argument("--stop", action="store_true", help="expectativa por stop_minimo_atr")
    parser.add_argument("--modalidade", default=None, help="filtra uma leitura")
    parser.add_argument("--rr-fixo", type=float, default=None,
                        help="ao cruzar RVOL/stop, fixa um rr (senão mistura todos)")
    parser.add_argument("--desde", default=None,
                        help="só velas a partir desta data (YYYY-MM-DD). Serve "
                             "para recortar a MESMA janela que a produção tem e "
                             "conferir o harness contra /signals/stats — a "
                             "tabela `signals` foi purgada em 2026-08-06 e só "
                             "tem os dias desde então")
    args = parser.parse_args()

    arquivo = Path(args.arquivo)
    if not arquivo.exists():
        print(f"Não achei {arquivo}. Rode scripts/varrer-parametros.py antes.")
        return 1
    dados = json.loads(arquivo.read_text())

    def filtrar(linhas: list[dict]) -> list[dict]:
        if args.modalidade:
            linhas = [x for x in linhas if x["modalidade"] == args.modalidade]
        if args.rr_fixo is not None:
            linhas = [x for x in linhas if x["rr"] == args.rr_fixo]
        if args.desde:
            # Comparação de string funciona porque `candle_time` é ISO-8601
            # com fuso fixo (UTC), então a ordem lexicográfica é a cronológica.
            linhas = [x for x in linhas if x.get("candle_time", "") >= args.desde]
        return linhas

    todas = [x for linhas in dados.values() for x in filtrar(linhas)]
    if not todas:
        print("Nenhuma linha depois dos filtros.")
        return 1
    print(f"{arquivo}: {len(todas)} avaliações"
          + (f" · modalidade={args.modalidade}" if args.modalidade else "")
          + (f" · rr={args.rr_fixo}" if args.rr_fixo is not None else ""))

    nenhum = not (args.rr or args.rvol or args.stop)

    if args.rr or nenhum:
        # O rr mistura todos os stops de propósito quando --stop não é pedido:
        # a pergunta "qual alvo paga mais" é sobre o alvo, e o stop entra como
        # ruído comum a todos os candidatos.
        grupos = defaultdict(list)
        for x in todas:
            grupos[f"rr = {x['rr']}"].append(x)
        tabela("EXPECTATIVA POR R/R", grupos, "rr_alvo_1")

    if args.rvol or nenhum:
        grupos = defaultdict(list)
        for x in todas:
            grupos[faixa_rvol(x["rvol"])].append(x)
        tabela("O RVOL SEPARA GANHO DE PERDA?", grupos, "faixa de RVOL")

        # Por modalidade: o gradiente pode existir nas leituras seguidoras de
        # tendência e não nas contrárias, e uma média só esconderia isso.
        mods = sorted({x["modalidade"] for x in todas})
        for mod in mods:
            sub = [x for x in todas if x["modalidade"] == mod]
            grupos = defaultdict(list)
            for x in sub:
                grupos[faixa_rvol(x["rvol"])].append(x)
            tabela(f"  RVOL — {mod}", grupos, "faixa de RVOL")

    if args.stop or nenhum:
        grupos = {chave: filtrar(linhas) for chave, linhas in dados.items()}
        tabela("EXPECTATIVA POR stop_minimo_atr", grupos, "configuração")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
