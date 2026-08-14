#!/usr/bin/env python3
"""
Varre parâmetros do motor sobre a base histórica e mede o que cada um paga.

Existe porque não há como decidir um limiar olhando para ele. Em 2026-08-06,
com 81 mil sinais reconstruídos, a medição derrubou duas coisas que pareciam
óbvias: a confirmação multi-timeframe mediu PIOR que sua ausência (37,0%
contra 42,1%) e a faixa de score 80+ — a que ganhava estrela na interface —
foi a segunda pior em expectativa. Ferramenta para não repetir isso por
opinião.

Diferente do `analyzer.py --backfill`, este script NÃO grava nada no banco:
lê velas pela rota pública `GET /candles` e escreve um JSON local. É
conferência, não produção — mesmo espírito do `conferir-refactor-params.py`
ao lado.

Uso:
    python scripts/varrer-parametros.py                    # varredura padrão
    python scripts/varrer-parametros.py --velas 1000 --timeframe M15
    python scripts/varrer-parametros.py --stops 0.85,1.0 --rrs 1.0,1.5,2.0

    python scripts/analisar-varredura.py                   # lê o resultado

Sai com código 1 se nenhuma série puder ser varrida.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import requests  # noqa: E402

from daytrade_smc import (  # noqa: E402
    AnalysisParams,
    Direction,
    RiskPlan,
    analyze,
    evaluate_signal_outcome,
)

# `GET /candles` é rota PÚBLICA (sem X-API-Key) — ver a tabela de endpoints em
# docs/homelab-pipeline.md. Por isso este script não precisa de credencial
# nenhuma, e é de propósito que continue assim.
API_PADRAO = "https://acoes-api.dondon.services"

# O mesmo do `analyzer.py --backfill`, e pela mesma razão: o motor calcula
# EMA200, e numa fatia mais curta a leitura nasce degenerada — gravá-la
# envenenaria a medição com sinais que a interface nunca produziria.
WARMUP = 250


def buscar_candles(api: str, symbol: str, timeframe: str, count: int, cache: Path):
    """Velas de um par, com cache em disco.

    O cache não é otimização: é o que garante que duas execuções do script
    comparem exatamente a mesma série. Sem ele, o próprio movimento do
    mercado entre uma varredura e outra apareceria como diferença de
    parâmetro — a mesma armadilha que o `conferir-refactor-params.py` evita
    buscando os candles uma vez só."""
    import pandas as pd

    cache.mkdir(parents=True, exist_ok=True)
    arquivo = cache / f"{symbol}-{timeframe}-{count}.json"
    if arquivo.exists():
        dados = json.loads(arquivo.read_text())
    else:
        # `requests` e não `urllib`: o gateway do homelab devolve 403 para o
        # User-Agent padrão do urllib.
        resposta = requests.get(
            f"{api}/candles",
            params={"symbol": symbol, "timeframe": timeframe, "count": count},
            timeout=60,
        )
        resposta.raise_for_status()
        dados = resposta.json()
        arquivo.write_text(json.dumps(dados))

    df = pd.DataFrame(dados["candles"])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()


def varrer_par(df, params: AnalysisParams, rrs: tuple[float, ...],
               rr2: float | None = None, baseline: bool = False) -> list[dict]:
    """Um par symbol/timeframe, vela a vela, com o desfecho já resolvido.

    DUAS decisões de memória, ambas aprendidas na marra (a primeira versão
    disto foi morta pelo OOM killer com 23Gi):

    1. **O desfecho é avaliado AQUI, não depois.** Guardar a fatia futura de
       cada sinal para avaliar num segundo passe significa manter ~15 mil
       fatias de DataFrame vivas ao mesmo tempo. Avaliando em linha, a fatia
       morre no fim da iteração.
    2. **Só escalares saem daqui.** Nada de Series, DataFrame ou Signal no
       retorno — o que sobe são números e strings.

    E uma decisão de custo: `rr_alvo_1` NÃO muda quais sinais o motor produz
    (direção, score e stop saem de `stop_for_signal` + `stop_minimo_atr` e
    não dependem dele), só onde o alvo fica. Então o motor roda uma vez e o
    MESMO sinal é avaliado sob todos os `rrs`. Uma passada por R/R custaria N
    vezes mais para medir exatamente a mesma coisa. Já `stop_minimo_atr` muda
    o stop — e portanto o risco, o alvo e até se o sinal sobrevive —, então
    esse exige passada própria, feita pelo chamador.

    A fatia histórica (`:i+1`) e a futura (`i+1:`) nunca se tocam: é a mesma
    garantia de não espiar o futuro que o backfill e o `check_signal_as_of`
    já davam.
    """
    linhas: list[dict] = []
    # Semente fixa por configuração: o mesmo par sorteia a mesma sequência em
    # toda execução, então duas varreduras comparam o MESMO acaso — sem ela,
    # a própria mudança de sorte viraria "diferença de parâmetro" (a mesma
    # armadilha que o cache de velas já evita para a série).
    aleatorio = random.Random(20260812)
    for i in range(WARMUP, len(df) - 1):
        historico = df.iloc[: i + 1]
        futuro = df.iloc[i + 1 :]
        if futuro.empty:
            break
        try:
            contexto, sinais = analyze(historico, params)
        except Exception:  # noqa: BLE001 — uma vela ruim não derruba a série
            continue

        if baseline:
            # BASELINE DE ACASO: um trade cara-ou-coroa neste mesmo candle,
            # com o mesmo perfil de risco que o motor usa (entrada no close,
            # stop a 1 ATR, alvo no rr) e direção sorteada. Serve de régua:
            # "quanto um sorteio teria pago neste mercado, nesta janela?".
            # Avaliado no MESMO `futuro` dos sinais reais, por isso compara
            # de igual para igual. Roda por candle — não por sinal — porque
            # a pergunta é sobre o mercado, não sobre onde o motor decidiu.
            fechamento = float(historico["close"].iloc[-1])
            atr_atual = float(contexto.atr or 0.0)
            if atr_atual > 0:
                for rr in rrs:
                    direcao = Direction.BUY if aleatorio.random() < 0.5 else Direction.SELL
                    risco = atr_atual
                    if direcao == Direction.BUY:
                        stop = fechamento - risco
                        alvo = fechamento + rr * risco
                    else:
                        stop = fechamento + risco
                        alvo = fechamento - rr * risco
                    plano = RiskPlan(entry=fechamento, stop=stop,
                                     target_1=alvo, target_2=None)
                    desfecho = evaluate_signal_outcome(plano, direcao, futuro)
                    linhas.append({
                        "candle_time": historico.index[-1].isoformat(),
                        "modalidade": "BASELINE",
                        "direcao": str(direcao),
                        "score": 0.0,
                        "confianca": 0.0,
                        "rvol": round(float(contexto.rvol), 4),
                        "atr_pct": round(float(contexto.atr_pct), 4),
                        "volatilidade": str(contexto.volatility),
                        "rr": rr,
                        "resultado": desfecho.resultado,
                        "r": (round(float(desfecho.r_realizado), 4)
                              if desfecho.r_realizado is not None else None),
                        "velas": desfecho.candles_ate_resultado,
                    })

        for sinal in sinais:
            if sinal.direction == Direction.NEUTRAL:
                continue
            if sinal.risk.entry is None or sinal.risk.stop is None:
                continue

            risco = abs(sinal.risk.entry - sinal.risk.stop)
            if risco <= 0:
                continue

            # As features de CONTEXTO viajam junto do desfecho. É isto que
            # permite responder "o RVOL separa ganho de perda?" sem tocar no
            # motor: hoje `signal_payload` guarda rvol e atr_pct, mas nada os
            # cruza com o resultado num recorte próprio.
            base = {
                # A ABERTURA da vela analisada, no mesmo formato que a coluna
                # `candle_time` de `signals` — é o que permite recortar a
                # varredura na mesma janela que a produção e comparar as duas
                # de igual para igual.
                "candle_time": historico.index[-1].isoformat(),
                "modalidade": sinal.name,
                "direcao": str(sinal.direction),
                "score": round(float(sinal.score), 2),
                "confianca": round(float(sinal.confidence), 2),
                "rvol": round(float(contexto.rvol), 4),
                "atr_pct": round(float(contexto.atr_pct), 4),
                "volatilidade": str(contexto.volatility),
            }

            for rr in rrs:
                alvo = (sinal.risk.entry + rr * risco
                        if sinal.direction == Direction.BUY
                        else sinal.risk.entry - rr * risco)
                # `target_2=None` no padrão, e de propósito: o executor manda
                # UM alvo só (`alvo_1`), então medir com dois alvos descreveria
                # uma operação que a máquina nunca envia. `--rr2` existe só
                # para reproduzir a avaliação da PRODUÇÃO (que grava alvo_2 e
                # o confere antes do alvo_1) quando se quer conferir o harness
                # contra `/signals/stats` — não para decidir calibragem.
                alvo2 = None
                if rr2 is not None:
                    alvo2 = (sinal.risk.entry + rr2 * risco
                             if sinal.direction == Direction.BUY
                             else sinal.risk.entry - rr2 * risco)
                plano = RiskPlan(entry=sinal.risk.entry, stop=sinal.risk.stop,
                                 target_1=alvo, target_2=alvo2)
                desfecho = evaluate_signal_outcome(plano, sinal.direction, futuro)
                linhas.append({
                    **base,
                    "rr": rr,
                    "resultado": desfecho.resultado,
                    "r": (round(float(desfecho.r_realizado), 4)
                          if desfecho.r_realizado is not None else None),
                    "velas": desfecho.candles_ate_resultado,
                })
    return linhas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=API_PADRAO)
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--velas", type=int, default=1000)
    parser.add_argument("--symbols", default=None,
                        help="lista separada por vírgula (padrão: a watchlist)")
    parser.add_argument("--stops", default="0.85,1.0,1.25",
                        help="valores de stop_minimo_atr (uma passada do motor cada)")
    parser.add_argument("--rrs", default="0.8,1.0,1.5,2.0,2.5,3.0",
                        help="valores de rr_alvo_1 (todos na MESMA passada)")
    parser.add_argument("--score-minimo", type=float, default=60.0)
    parser.add_argument("--rr2", type=float, default=None,
                        help="liga o alvo 2 (ex.: 1.8) — só para conferir o "
                             "harness contra /signals/stats, não para calibrar")
    parser.add_argument("--baseline", action="store_true",
                        help="avalia, em cada candle, um trade cara-ou-coroa "
                             "com o mesmo perfil de risco (entrada no close, "
                             "stop a 1 ATR, alvo no rr) e direção sorteada. "
                             "São as linhas modalidade=BASELINE que o "
                             "analisar-varredura usa como régua do acaso")
    parser.add_argument("--saida", default=str(RAIZ / "varredura-resultado.json"))
    parser.add_argument("--cache", default=str(RAIZ / ".cache-velas"))
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        resposta = requests.get(f"{args.api}/watchlist", timeout=30)
        resposta.raise_for_status()
        # WINFUT fora: o executor não opera nele (o analyzer varre só os
        # timeframes de Day Trade) e a escala em pontos distorceria o R médio
        # junto das ações.
        symbols = [s for s in resposta.json()["symbols"] if s != "WINFUT"]

    stops = tuple(float(x) for x in args.stops.split(","))
    rrs = tuple(float(x) for x in args.rrs.split(","))
    saida, cache = Path(args.saida), Path(args.cache)

    print(f"API        : {args.api}")
    print(f"Ativos     : {', '.join(symbols)}")
    print(f"Timeframe  : {args.timeframe} · {args.velas} velas · warm-up {WARMUP}")
    print(f"stop_min   : {stops}")
    print(f"rr_alvo_1  : {rrs}\n")

    # Persistência PARCIAL, por configuração. A primeira versão deste script
    # gravava só no fim e perdeu duas passadas completas quando foi morta.
    resultado: dict[str, list[dict]] = {}
    if saida.exists():
        resultado = json.loads(saida.read_text())
        print(f"retomando {saida} com {len(resultado)} configuração(ões) já feita(s)\n")

    varridas = 0
    for stop_min in stops:
        chave = f"stop_minimo_atr={stop_min}"
        if chave in resultado:
            print(f"{chave}: já estava no arquivo, pulando")
            continue

        params = AnalysisParams(stop_minimo_atr=stop_min,
                                score_minimo_operavel=args.score_minimo)
        linhas: list[dict] = []
        for symbol in symbols:
            try:
                df = buscar_candles(args.api, symbol, args.timeframe, args.velas, cache)
            except Exception as exc:  # noqa: BLE001 — um ativo sem dado não invalida os outros
                print(f"  {symbol}: ignorado ({exc})")
                continue
            if len(df) <= WARMUP + 10:
                print(f"  {symbol}: ignorado (só {len(df)} velas, warm-up exige {WARMUP})")
                continue
            achados = varrer_par(df, params, rrs, args.rr2, baseline=args.baseline)
            for linha in achados:
                linha["symbol"] = symbol
            linhas.extend(achados)
            varridas += 1
            print(f"  {chave} {symbol}: {len(achados)} avaliações")

        resultado[chave] = linhas
        saida.write_text(json.dumps(resultado))
        print(f"{chave}: {len(linhas)} avaliações · gravado em {saida}\n")

    if not varridas and not resultado:
        print("Nenhuma série varrida — nada foi medido.")
        return 1
    print(f"pronto: {saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
