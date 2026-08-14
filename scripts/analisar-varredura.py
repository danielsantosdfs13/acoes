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
import numpy as np
import random
import statistics
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Sementes acopladas à data da primeira varredura com régua do acaso
# (2026-08-12): fixas para que duas execuções comparem o MESMO sorteio.
SEMENTE = 20260812


def _bootstrap_ic(rs: list[float], n: int = 1000, alfa: float = 0.05,
                  semente: int = SEMENTE) -> tuple[float | None, float | None]:
    """Intervalo de confiança por bootstrap percentil sobre a média dos `r`.

    Responde "a expectativa observada pode ser só sorte?" de forma empírica:
    reamostra o conjunto de `r` com reposição e olha a cauda. Um intervalo que
    cruza o zero é indistinguível de sorte — e foi exatamente o que aconteceu
    com o `proposta_rr05` (expR −0,01 no backtest sobre 794 resolvidos).
    `n` cai para 1000 (e não 5000+): com dezenas de milhares de `r` por grupo,
    o percentile bootstrap já estabilizou, e cada grupo tem MUITAS linhas.

    Vetorizado com numpy: a versão original fazia um loop de n passadas × m
    sorteios em Python puro e, com ~84 mil `r` por banda (o recorte por ATR),
    levava mais de 15 minutos por tabela. O numpy faz as mesmas n×m somas em
    segundos."""
    if len(rs) < 10:
        return None, None
    arr = np.asarray(rs, dtype=float)
    rng = np.random.default_rng(semente)
    m = len(arr)
    # Em blocos de 128 réplicas: com ~108 mil `r` por grupo, uma matriz
    # (1000, m) inteira custaria ~670MB; em blocos o pico fica em ~90MB.
    medias = np.empty(n, dtype=float)
    for base in range(0, n, 128):
        k = min(128, n - base)
        sorteios = rng.integers(0, m, size=(k, m))
        medias[base:base + k] = arr[sorteios].mean(axis=1)
    medias.sort()
    lo = float(medias[int(n * alfa / 2)])
    hi = float(medias[int(n * (1 - alfa / 2))])
    return lo, hi


def _p_acaso(rs: list[float], n_max: int = 10000,
             semente: int = SEMENTE) -> float | None:
    """Teste de permutação de sinal: a baseline do ACASO.

    A pergunta não é "esse número é positivo?", é "esse número seria positivo
    também se a direção das operações fosse sorte?". O nulo dessa permutação é
    a distribuição dos `r` simétrica em torno de zero: se a direção do motor
    não agrega nada além de cara-ou-coroa, trocar o sinal de uma sub-arbitrada
    metade das operações não muda a média esperada — e a média observada cai
    dentro do que o sorteio produz. Se ela cai fora, a direção é informação.

    Por isso a estatística não é `media` mas `|media|`: interessa saber se o
    efeito observado, em qualquer direção, é alcançável por sorte. Um p alto
    com expectativa positiva não significa "está perdendo" — significa que um
    cara-ou-coroa com as mesmas entradas teria tido a mesma cara de positivo.

    `n` é limitado pelo tamanho da amostra para que uma varredura com 100 mil
    linhas não vire um loop de 1e9 — com grupos grandes o teste já satura.

    Vetorizado com numpy (mesma razão do `_bootstrap_ic`): os sorteios de sinal
    viram uma matriz (n, m) de ±1 multiplicada pelo vetor de `r` de uma vez."""
    if len(rs) < 10:
        return None
    arr = np.asarray(rs, dtype=float)
    rng = np.random.default_rng(semente + 1)
    m = len(arr)
    media_obs = float(np.mean(arr))
    abs_obs = abs(media_obs)
    n = min(n_max, max(200, 200_000 // m))
    sinais = rng.choice(np.array([-1.0, 1.0]), size=(n, m))
    medias = np.abs((sinais * arr).mean(axis=1))
    acima = int(np.count_nonzero(medias >= abs_obs)) + 1  # a observação conta
    return acima / (n + 1)


def resumo(linhas: list[dict]) -> dict | None:
    """Taxa E expectativa, sempre juntas, com quantos ficaram em aberto.

    `resolvidos` é o denominador da taxa: os EM_ABERTO não têm desfecho e
    ficam fora de toda estatística (o mesmo `resultado NOT IN (...)` que o
    `_STATS_BASE` da API já aplica). Mostrar a taxa sobre `n` seria mentira.

    `exp_lo`/`exp_hi` (IC95% por bootstrap) e `p_acaso` (significância contra
    a permutação de sinal) são o REMÉDIO para a pergunta "isso pode ser
    sorte?": sem eles, a tabela compara apenas médias, e no mercado de ações
    uma média sobre poucos mélange passado é exatamente o que engana."""
    resolvidos = [x for x in linhas if x["resultado"] in ("ALVO_1", "ALVO_2", "STOP")]
    if not resolvidos:
        return None
    acertos = sum(1 for x in resolvidos if x["resultado"].startswith("ALVO"))
    rs = [x["r"] for x in resolvidos if x["r"] is not None]
    aberto = sum(1 for x in linhas if x["resultado"] == "EM_ABERTO")
    exp_lo, exp_hi = _bootstrap_ic(rs)
    return {
        "n": len(linhas),
        "resolvidos": len(resolvidos),
        "taxa": acertos / len(resolvidos),
        "expectativa": statistics.mean(rs) if rs else 0.0,
        "total_r": sum(rs),
        "aberto": aberto / len(linhas) if linhas else 0.0,
        "exp_lo": exp_lo,
        "exp_hi": exp_hi,
        "p_acaso": _p_acaso(rs),
    }


def tabela(titulo: str, grupos: dict[str, list[dict]], rotulo: str) -> None:
    linhas = {k: resumo(v) for k, v in grupos.items()}
    linhas = {k: v for k, v in linhas.items() if v}
    if not linhas:
        print(f"\n{titulo}\n  (sem dados)")
        return
    melhor = max(linhas.values(), key=lambda r: r["expectativa"])["expectativa"]
    print(f"\n{titulo}")
    print(f"  {rotulo:<26}{'resolv':>8}{'taxa':>8}{'expR':>9}  {'IC95%':>16}   {'p(acaso)':>9}   {'total R':>10}{'%aberto':>9}")
    for chave in sorted(linhas):
        r = linhas[chave]
        best = r["expectativa"] == melhor
        ic = (f"[{r['exp_lo']:+.3f}, {r['exp_hi']:+.3f}]"
              if r["exp_lo"] is not None else "—")
        # `p_acaso` é a coluna que decide: o IC% e a expectativa sozinhos
        # descrevem o tamanho do efeito, mas só a fração contra o acaso diz
        # se ele não é sorte. Sinalizar quando o teste não rejeita o acaso.
        se_para = r["p_acaso"] is None or r["p_acaso"] >= 0.05
        p_acaso = "—" if r["p_acaso"] is None else f"{r['p_acaso']:.3f}"
        alerta = ""
        if se_para:
            alerta = "  (? = acaso)"
        elif r["exp_lo"] is not None and r["exp_hi"] < 0:
            alerta = "  (! negativo)"
        elif r["exp_lo"] is not None and r["exp_lo"] > 0:
            best = False  # já marcado pelo IC%: o `<<<` seria ruído
        marca = "  <<<" if best else ""
        print(f"  {chave:<26}{r['resolvidos']:>8}{r['taxa']:>7.1%}"
              f"{r['expectativa']:>+9.3f}  {ic:>16}   {p_acaso:>9}   "
              f"{r['total_r']:>+10.0f}{r['aberto']:>8.0%}{alerta}{marca}")


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


def faixa_atr(atr_pct: float) -> str:
    """ATR relativo ao preço (%). Os cortes saem da distribuição observada da
    varredura (0,20-1,41), e um ATR alto significa vela movimentada — a
    pergunta é se a calmaria ou o barulho favorecem o motor."""
    if atr_pct < 0.5:
        return "a) < 0,5%"
    if atr_pct < 0.7:
        return "b) 0,5-0,7%"
    if atr_pct < 0.9:
        return "c) 0,7-0,9%"
    if atr_pct < 1.1:
        return "d) 0,9-1,1%"
    return "e) >= 1,1%"


def faixa_velas(velas: int | None) -> str:
    """Quão rápido o desfecho veio. `evaluate_signal_outcome` conta candles até
    o alvo/stop; um desfecho no próprio primeiro candle é cobertura curta, e um
    que demora é a posição sentada torrando tempo."""
    if velas is None:
        return "n/a"
    if velas <= 1:
        return "a) 1 vela"
    if velas <= 3:
        return "b) 2-3 velas"
    if velas <= 8:
        return "c) 4-8 velas"
    if velas <= 20:
        return "d) 9-20 velas"
    return "e) > 20 velas"


def faixa_score(score: float) -> str:
    if score < 70:
        return "a) 60-70"
    if score < 80:
        return "b) 70-80"
    if score < 90:
        return "c) 80-90"
    return "d) 90-100"


def cruza_feature(titulo: str, motor: list[dict], acaso: list[dict],
                  rotulo: str, chave) -> None:
    """Cruza motor × acaso por banda de UMA feature de contexto.

    As linhas BASELINE carregam as mesmas features de contexto que as do motor
    (`rvol`, `atr_pct`, `volatilidade`, `direcao`) — o `varrer_par` grava o
    contexto do candle, não do sinal —, então dá para perguntar "nessa faixa
    de ATR, o motor ainda ganha do sorteio?" por banda, não só no total.
    `chave` é o callable que classifica cada linha numa banda (ex.: faixa_atr,
    ou `lambda x: x["direcao"]`)."""
    grupos_motor: dict[str, list[dict]] = defaultdict(list)
    for x in motor:
        grupos_motor[chave(x)].append(x)
    grupos_acaso: dict[str, list[dict]] = defaultdict(list)
    for x in acaso:
        grupos_acaso[chave(x)].append(x)
    cruza_baseline(titulo, grupos_motor, grupos_acaso, rotulo)


def qual_baseline(dados: dict[str, list[dict]],
                  desde: str | None = None) -> dict[str, list[dict]]:
    """Só as linhas BASELINE de cada configuração, já recortadas pela data.

    A régua do acaso vive na MESMA chave de stop que os sinais do motor
    (o `varrer_par` grava as duas juntas), então basta separar pela
    modalidade. Não respeita `--modalidade` nem `--rr-fixo` de propósito:
    o acaso não tem modalidade e o cruze por rr se faz em cima desta lista."""
    saida: dict[str, list[dict]] = {}
    for chave, linhas in dados.items():
        base = [x for x in linhas if x["modalidade"] == "BASELINE"]
        if desde:
            base = [x for x in base if x.get("candle_time", "") >= desde]
        if base:
            saida[chave] = base
    return saida


def cruza_baseline(titulo: str, motor: dict[str, list[dict]],
                   acaso: dict[str, list[dict]], rotulo: str) -> None:
    """O motor e o cara-ou-coroa, no mesmo recorte, lado a lado.

    A régua não é "expR >= 0" (o acaso também ganha quando o mercado anda
    sozinho) nem "expR do motor > expR do acaso" por um tiquinho (o IC% se
    sobrepõe). É: a expectativa do motor fora do IC95% do acaso, com
    `p_acaso` baixo. O `<<<` marca quem ganha de verdade; o `?` marca quem
    não se distingue do sorteio."""
    chaves = sorted(k for k in motor if k in acaso)
    if not chaves:
        print(f"\n{titulo}\n  (sem cruzamento motor/acaso)")
        return
    print(f"\n{titulo}")
    print(f"  {rotulo:<26}{'motor expR':>12}   {'acaso expR':>12}   {'acaso IC95%':>16}   {'motor p(acaso)':>15}")
    for chave in chaves:
        rm = resumo(motor[chave])
        ra = resumo(acaso[chave])
        if not rm or not ra:
            continue
        fora_ic = (ra["exp_lo"] is not None
                   and (rm["expectativa"] > ra["exp_hi"]
                        or rm["expectativa"] < ra["exp_lo"]))
        marca = "  <<< motor" if fora_ic else "  ? = acaso"
        ic = (f"[{ra['exp_lo']:+.3f}, {ra['exp_hi']:+.3f}]"
              if ra["exp_lo"] is not None else "—")
        p = "—" if rm["p_acaso"] is None else f"{rm['p_acaso']:.3f}"
        print(f"  {chave:<26}{rm['expectativa']:>+12.3f}   "
              f"{ra['expectativa']:>+12.3f}   {ic:>16}   {p:>15}{marca}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arquivo", default=str(RAIZ / "varredura-resultado.json"))
    parser.add_argument("--rr", action="store_true", help="expectativa por rr_alvo_1")
    parser.add_argument("--rvol", action="store_true", help="o RVOL separa ganho de perda?")
    parser.add_argument("--stop", action="store_true", help="expectativa por stop_minimo_atr")
    parser.add_argument("--baseline", action="store_true",
                        help="régua do acaso: o que um cara-ou-coroa com o mesmo "
                             "perfil de risco teria pago em cada rr, lado a lado "
                             "com o motor (exige --baseline na varredura)")
    parser.add_argument("--modalidade", default=None, help="filtra uma leitura")
    parser.add_argument("--rr-fixo", type=float, default=None,
                        help="ao cruzar RVOL/stop, fixa um rr (senão mistura todos)")
    parser.add_argument("--atr", action="store_true",
                        help="o ATR relativo (atr_pct) separa ganho de perda? "
                             "Cruza o motor contra o acaso por faixa de atr_pct")
    parser.add_argument("--score", action="store_true",
                        help="o score do motor discrimina desfecho? Sem acaso "
                             "(baseline tem score 0 de propósito) — só a tabela "
                             "com IC95% e p(acaso)")
    parser.add_argument("--velas", action="store_true",
                        help="a rapidez do desfecho (velas até alvo/stop) "
                             "separara ganho de perda? Tabela própria, sem acaso")
    parser.add_argument("--direcao", action="store_true",
                        help="COMPRA x VENDA: cruza o motor contra o acaso por "
                             "direção sorteada")
    parser.add_argument("--volatilidade", action="store_true",
                        help="a faixa de volatilidade (MarketContext.volatility) "
                             "separa? Cruza contra o acaso por faixa")
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
        # BASELINE é a régua do acaso, não uma leitura: nas tabelas normais
        # ela só poluiria a contagem. Ela aparece no recorte `--baseline`.
        linhas = [x for x in linhas if x["modalidade"] != "BASELINE"]
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

    baseline_por_stop = qual_baseline(dados, desde=args.desde)
    if not baseline_por_stop:
        if args.baseline:
            print("\nNão há linhas BASELINE no arquivo. Rode a varredura com --baseline.")
    else:
        # O stop é onde faz sentido cruzar o motor contra o acaso na MESMA
        # configuração: cada linha tem seu próprio sorteio, do mesmo candle.
        # Aparece no default e com --baseline; quando os dois pedem, uma vez só.
        if args.stop or nenhum or args.baseline:
            grupos_motor = {k: filtrar(v) for k, v in dados.items()}
            cruza_baseline("MOTOR vs ACASO (por stop_minimo_atr)",
                           grupos_motor, baseline_por_stop, "configuração")

    # -- Novos recortes de padrão -------------------------------------------
    # A régua é a mesma do cruze por stop: as linhas BASELINE carregam o
    # CONTEXTO do candle (rvol, atr_pct, volatilidade) e a direção sorteada,
    # então por cada banda de feature dá para perguntar "no candle de ATR/X,
    # o motor ainda bate o acaso?".
    acaso_flat = [x for linhas in baseline_por_stop.values() for x in linhas]
    if args.rr_fixo is not None:
        acaso_flat = [x for x in acaso_flat if x["rr"] == args.rr_fixo]

    if args.atr or nenhum:
        motor_flat = [x for linhas in dados.values() for x in filtrar(linhas)]
        cruza_feature("MOTOR vs ACASO (por ATR relativo)",
                      motor_flat, acaso_flat, "faixa de atr_pct", lambda x: faixa_atr(x["atr_pct"]))

    if args.volatilidade:
        motor_flat = [x for linhas in dados.values() for x in filtrar(linhas)]
        cruza_feature("MOTOR vs ACASO (por faixa de volatilidade)",
                      motor_flat, acaso_flat, "volatilidade",
                      lambda x: x["volatilidade"])

    if args.direcao:
        motor_flat = [x for linhas in dados.values() for x in filtrar(linhas)]
        cruza_feature("MOTOR vs ACASO (por direção)",
                      motor_flat, acaso_flat, "direção",
                      lambda x: "COMPRA" if x["direcao"] == "Direction.BUY" else "VENDA")

    if args.score:
        # Score não cruza com acaso (baseline é score 0 de propósito) — a
        # pergunta é "o próprio score do motor separa os que dão certo dos
        # que dão errado?", que a tabela com IC%/p(acaso) já responde.
        grupos = defaultdict(list)
        for x in todas:
            grupos[faixa_score(x["score"])].append(x)
        tabela("O SCORE DISCRIMINA DESFECHO?", grupos, "faixa de score")

    if args.velas:
        grupos = defaultdict(list)
        for x in todas:
            grupos[faixa_velas(x.get("velas"))].append(x)
        tabela("O DESFECHO RÁPIDO PAGA MAIS?", grupos, "velas até alvo/stop")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
