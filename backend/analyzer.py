"""
backend/analyzer.py

Quarto entrypoint da imagem `acoes-backend`. Roda em loop dentro do k3s e
faz duas coisas, em passes separados a cada iteração:

  1. VARREDURA — para cada símbolo da watchlist × timeframe × perfil,
     roda o motor (`daytrade_smc.analyze_symbol_mtf`) sobre os candles já
     gravados no TimescaleDB e grava uma linha em `signals` por leitura
     (Confluência, SMC, Price Action, Médias Móveis, VWAP).

  2. DESFECHO — para cada sinal ainda sem resultado, caminha pelos
     candles que vieram depois e registra o que aconteceu primeiro (alvo
     1, alvo 2 ou stop), reusando `evaluate_signal_outcome` do motor.

É o que torna a assertividade uma medição em vez de uma impressão: sem o
worker, só existiriam os sinais que alguém por acaso olhou e salvou à mão,
e a taxa de acerto sairia enviesada pelo uso da interface.

Existe ainda um terceiro modo, de execução ÚNICA e fora do loop: o
BACKFILL (`--backfill`), que reconstrói os sinais das velas já guardadas
no banco em vez de esperar as próximas. Sem ele a assertividade começa
vazia e leva semanas pra ter número — em D1, um desfecho leva dias.

Por que Deployment em loop, e não CronJob: o gate de pregão já existe (e
duplicá-lo num cron ainda erraria feriado), o pool de conexão fica quente,
o passe de desfecho aproveita as mesmas leituras de vela da varredura, e
`command:` num Deployment é o padrão dos outros três workloads. O retry
grátis do CronJob é substituído por `restartPolicy: Always` + `while True`.

Uso:
    python analyzer.py                    # loop contínuo (é o do manifest)
    python analyzer.py --varrer-uma-vez   # uma varredura e sai
    python analyzer.py --avaliar-uma-vez  # um passe de desfecho e sai
    python analyzer.py --backfill         # reprocessa o histórico e sai

Deploy: `command: ["python", "analyzer.py"]`, mesma imagem do processor,
da api e do migrate.
"""

from __future__ import annotations

import argparse
import bisect
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from psycopg.types.json import Jsonb

# No checkout, `daytrade_smc.py` fica um nível acima (raiz do repo); na
# imagem, tudo é copiado achatado em /app e o CWD já resolve o import. Os
# dois casos funcionam — não troque por um caminho fixo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daytrade_smc import (  # noqa: E402
    DAYTRADE_CONFIRMATION_TIMEFRAMES,
    DEFAULT_PROFILE_NAME,
    DEFAULT_SYMBOLS,
    MODALITIES,
    TIMEFRAMES,
    AnalysisParams,
    Direction,
    RiskPlan,
    analyze,
    evaluate_signal_outcome,
    mtf_confirmation,
    signal_payload,
)

from db import pool  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("analyzer")


def _env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def _env_lista(nome: str, padrao: str) -> list[str]:
    return [item.strip() for item in os.environ.get(nome, padrao).split(",") if item.strip()]


def _env_counts(nome: str, padrao: str) -> dict[str, int]:
    """Lê "M15=250,H1=250" num dicionário."""
    counts: dict[str, int] = {}
    for par in os.environ.get(nome, padrao).split(","):
        if "=" not in par:
            continue
        tf, _, valor = par.partition("=")
        try:
            counts[tf.strip().upper()] = int(valor)
        except ValueError:
            continue
    return counts


# 900s (15min) e não 300s de propósito: casa com a vela mais curta que o
# worker analisa (M15), e um sinal NÃO muda depois que a vela fechou —
# varrer mais rápido só produz tentativas de INSERT que o ON CONFLICT
# descarta. A 300s num pregão de 10h seriam ~26 mil INSERTs tentados por
# dia, ~92% deles conflito, cada um queimando um valor da sequence (o
# IDENTITY não faz rollback) e deixando `max(id)` dez vezes maior que a
# contagem real de sinais. É a lição do TRAILING_WINDOW 10→3 do scraper,
# aplicada antes de repetir o erro.
INTERVAL_SECONDS = _env_int("ANALYZER_INTERVAL_SECONDS", 900)
FECHADO_SLEEP_SECONDS = _env_int("ANALYZER_FECHADO_SLEEP_SECONDS", 900)
TIMEFRAMES_VARRIDOS = _env_lista("ANALYZER_TIMEFRAMES", "M15,H1,H4,D1")
COUNTS = _env_counts("ANALYZER_COUNTS", "M15=250,H1=250,H4=150,D1=250")
PERFIS = _env_lista("ANALYZER_PERFIS", DEFAULT_PROFILE_NAME)
JANELA_DESFECHO_DIAS = _env_int("ANALYZER_JANELA_DESFECHO_DIAS", 30)

# Backfill (modo de execução única, ver `backfill()`). O warm-up existe
# porque o motor calcula EMA200: numa fatia mais curta que isso a leitura
# nasce degenerada, e gravá-la envenenaria a assertividade com sinais que
# a interface nunca produziria.
BACKFILL_WARMUP = _env_int("ANALYZER_BACKFILL_WARMUP", 250)
BACKFILL_VELAS = _env_int("ANALYZER_BACKFILL_VELAS", 750)

# Par de timeframes que define "confirmado no multi-timeframe". O padrão é
# o de Day Trade (M15+H1); o de Swing (D1+W1) exigiria W1, que o scraper
# não coleta — ver SCRAPER_TIMEFRAMES.
CONFIRMACAO = tuple(_env_lista("ANALYZER_CONFIRMACAO", ",".join(DAYTRADE_CONFIRMATION_TIMEFRAMES)))

MERCADO_TIMEZONE = os.environ.get("MERCADO_TIMEZONE", "America/Sao_Paulo")
MERCADO_ABERTURA_HORA = _env_int("MERCADO_ABERTURA_HORA", 9)
MERCADO_FECHAMENTO_HORA = _env_int("MERCADO_FECHAMENTO_HORA", 19)

_TZ_MERCADO = ZoneInfo(MERCADO_TIMEZONE)

ORIGEM = "worker"

# Terceiro valor de `origem`, ao lado de 'worker' e 'manual'. Vale um valor
# próprio, e não reaproveitar 'worker', por três razões: `origem` faz parte
# do índice de dedup, então backfill e varredura nunca colidem nem se o
# backfill for reexecutado com janela maior; a aba Assertividade filtra por
# origem, então dá pra separar o que foi medido em tempo real do que foi
# reconstruído; e uma palavra só passaria a significar dois regimes de
# coleta diferentes.
ORIGEM_BACKFILL = "backfill"


def _mercado_aberto(agora: datetime | None = None) -> bool:
    """Mesma regra do scraper: seg–sex, dentro da janela. Feriado não é
    tratado — rodar à toa num feriado é bem mais barato que manter um
    calendário da B3 correto."""
    agora = agora or datetime.now(_TZ_MERCADO)
    if agora.weekday() >= 5:
        return False
    return MERCADO_ABERTURA_HORA <= agora.hour < MERCADO_FECHAMENTO_HORA


# ---------------------------------------------------------------------------
# Leitura de candles
# ---------------------------------------------------------------------------

def _ler_candles(conn, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    """Últimas `count` velas FECHADAS, direto do banco.

    Espelha `api.get_candles`: ORDER BY time DESC + LIMIT, e devolve ASC,
    que é o contrato do índice em todo o motor.

    ⚠️ O corte da vela em formação é obrigatório e é a armadilha número um
    deste worker. O scraper grava a vela ABERTA a cada ciclo, por design —
    diferente do `_fetch_ohlcv_yahoo`, que descarta a vela corrente antes
    de devolver. Se o analyzer analisasse a vela pela metade, o ON CONFLICT
    DO NOTHING congelaria essa primeira leitura como se fosse o sinal
    definitivo daquela vela, e TODO o dataset de assertividade ficaria
    errado de um jeito que parece certo.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE symbol = %(symbol)s AND timeframe = %(timeframe)s
            ORDER BY time DESC
            LIMIT %(count)s
            """,
            # o clamp é daqui: o `Query(le=5000)` da api não vale in-process
            {"symbol": symbol, "timeframe": timeframe, "count": max(1, min(count, 5000))},
        )
        rows = cur.fetchall()

    if not rows:
        raise RuntimeError(f"Sem candles para {symbol} em {timeframe}.")

    df = pd.DataFrame(
        list(reversed(rows)), columns=["time", "open", "high", "low", "close", "volume"]
    )
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")

    duracao = TIMEFRAMES[timeframe]["duration"]
    fechadas = df[df.index + duracao <= pd.Timestamp.now(tz="UTC")]
    if fechadas.empty:
        raise RuntimeError(f"Só há vela em formação para {symbol} em {timeframe}.")
    return fechadas


def _ler_candles_depois(conn, symbol: str, timeframe: str, desde: datetime) -> pd.DataFrame:
    """Velas fechadas posteriores a `desde` — o material do passe de desfecho."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s AND timeframe = %s AND time > %s
            ORDER BY time ASC
            """,
            (symbol, timeframe, desde),
        )
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")
    duracao = TIMEFRAMES[timeframe]["duration"]
    return df[df.index + duracao <= pd.Timestamp.now(tz="UTC")]


# ---------------------------------------------------------------------------
# Estado lido do banco
# ---------------------------------------------------------------------------

def _watchlist(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol")
        symbols = [row[0] for row in cur.fetchall()]
    return symbols or DEFAULT_SYMBOLS.copy()


def _perfis(conn) -> dict[str, AnalysisParams]:
    with conn.cursor() as cur:
        cur.execute("SELECT nome, params FROM analysis_profiles WHERE ativo AND nome = ANY(%s)", (PERFIS,))
        perfis = {nome: AnalysisParams.from_dict(params) for nome, params in cur.fetchall()}
    if not perfis:
        log.warning("Nenhum dos perfis %s existe/está ativo — usando o padrão do motor.", PERFIS)
        perfis = {DEFAULT_PROFILE_NAME: AnalysisParams()}
    return perfis


# ---------------------------------------------------------------------------
# Passe 1 — varredura
# ---------------------------------------------------------------------------

_INSERT_SQL = """
INSERT INTO signals (
    symbol, timeframe, modalidade, candle_time, perfil, params_hash, origem,
    direcao, score, confianca, setup, mtf_confirmado, mtf_direcao,
    entrada, stop, alvo_1, alvo_2, r_alvo_1, r_alvo_2, stop_basis, detalhes,
    resultado, resultado_detalhe, candles_ate_resultado, avaliado_em, avaliado_ate
) VALUES (
    %(symbol)s, %(timeframe)s, %(modalidade)s, %(candle_time)s, %(perfil)s,
    %(params_hash)s, %(origem)s, %(direcao)s, %(score)s, %(confianca)s, %(setup)s,
    %(mtf_confirmado)s, %(mtf_direcao)s, %(entrada)s, %(stop)s, %(alvo_1)s,
    %(alvo_2)s, %(r_alvo_1)s, %(r_alvo_2)s, %(stop_basis)s, %(detalhes)s,
    %(resultado)s, %(resultado_detalhe)s, %(candles_ate_resultado)s,
    %(avaliado_em)s, %(avaliado_ate)s
)
ON CONFLICT (symbol, timeframe, modalidade, candle_time, perfil, origem) DO NOTHING
"""


def _preparar(
    payload: dict,
    desfecho: tuple[str, str, int | None] | None = None,
    avaliado_ate: datetime | None = None,
) -> dict:
    """Ajusta o payload do motor pro driver, e resolve o SEM_SINAL.

    Sinal sem entrada operável já nasce com desfecho: ele nunca vai ter um,
    e deixar `resultado` nulo o deixaria pra sempre na fila do passe 2. Mas
    é gravado assim mesmo — sem essas linhas não dá pra responder "com que
    frequência este perfil sequer produz sinal".

    `desfecho` só vem do BACKFILL, que já tem as velas seguintes na mão e
    por isso grava o resultado no mesmo INSERT. A varredura normal deixa
    tudo nulo e entrega o trabalho ao passe 2 — que é o certo pra ela, já
    que ali o futuro ainda não aconteceu.
    """
    dados = dict(payload)
    dados["detalhes"] = Jsonb(payload.get("detalhes") or {})

    if payload["direcao"] == Direction.NEUTRAL.value or payload["entrada"] is None:
        dados["resultado"] = "SEM_SINAL"
        dados["resultado_detalhe"] = "Não havia sinal operável nesta vela."
        dados["candles_ate_resultado"] = None
        dados["avaliado_em"] = None
        dados["avaliado_ate"] = None
    elif desfecho is not None:
        dados["resultado"], dados["resultado_detalhe"], dados["candles_ate_resultado"] = desfecho
        dados["avaliado_em"] = datetime.now(UTC)
        dados["avaliado_ate"] = avaliado_ate
    else:
        dados["resultado"] = None
        dados["resultado_detalhe"] = None
        dados["candles_ate_resultado"] = None
        dados["avaliado_em"] = None
        dados["avaliado_ate"] = None

    return dados


def varrer(conn) -> tuple[int, int]:
    """Uma passada por watchlist × timeframe × perfil. Devolve (gravados, tentados).

    Sem watermark, sem cursor, sem arquivo de estado: a deduplicação é o
    índice único em `signals`, e reiniciar o pod no meio de uma varredura
    não produz linha repetida. Mesmo raciocínio do "por que sem watermark"
    do scraper.
    """
    symbols = _watchlist(conn)
    perfis = _perfis(conn)
    gravados = 0
    tentados = 0

    for nome_perfil, params in perfis.items():
        for symbol in symbols:
            # analisa TODOS os timeframes do símbolo antes de gravar: a
            # confirmação multi-timeframe precisa dos dois lados do par na
            # mão, e reler os candles por modalidade seria desperdício
            analisado: dict[str, tuple] = {}
            for timeframe in TIMEFRAMES_VARRIDOS:
                try:
                    df = _ler_candles(conn, symbol, timeframe, COUNTS.get(timeframe, 250))
                    analisado[timeframe] = analyze(df, params)
                except Exception as exc:  # noqa: BLE001 — um par ruim não derruba a varredura
                    log.warning("%s %s (%s): %s", symbol, timeframe, nome_perfil, exc)

            if not analisado:
                continue

            sinais_por_tf = {tf: sinais for tf, (_, sinais) in analisado.items()}
            # uma confirmação POR MODALIDADE — ver mtf_confirmation()
            confirmacao = {
                modalidade: mtf_confirmation(sinais_por_tf, CONFIRMACAO, modalidade)
                for modalidade in MODALITIES
            }

            for timeframe, (contexto, sinais) in analisado.items():
                for sinal in sinais:
                    if sinal.name not in MODALITIES:
                        continue
                    confirmado, direcao_mtf = confirmacao[sinal.name]
                    payload = signal_payload(
                        symbol, timeframe, sinal, contexto, nome_perfil, params, ORIGEM,
                        confirmado, direcao_mtf,
                    )
                    tentados += 1
                    with conn.cursor() as cur:
                        cur.execute(_INSERT_SQL, _preparar(payload))
                        gravados += cur.rowcount
            conn.commit()

    return gravados, tentados


# ---------------------------------------------------------------------------
# Passe 2 — desfecho
# ---------------------------------------------------------------------------

def avaliar(conn) -> int:
    """Preenche o desfecho dos sinais pendentes. Devolve quantos resolveu.

    O recorte de 30 dias é o mesmo, pela mesma razão, do `GET /status`:
    sem ele a consulta cresce pra sempre. Também dispensa inventar um
    desfecho "EXPIRADO" que o `OUTCOME_LABELS` do Streamlit não conhece.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, symbol, timeframe, candle_time, direcao, entrada, stop, alvo_1, alvo_2
            FROM signals
            WHERE (resultado IS NULL OR resultado = 'EM_ABERTO')
              AND candle_time > now() - make_interval(days => %s)
            ORDER BY symbol, timeframe, candle_time
            """,
            (JANELA_DESFECHO_DIAS,),
        )
        pendentes = cur.fetchall()

    if not pendentes:
        return 0

    # uma leitura de candles por (symbol, timeframe), não uma por sinal:
    # busca a partir da vela pendente mais antiga do par e recorta em
    # memória pra cada sinal
    mais_antigo: dict[tuple[str, str], datetime] = {}
    for _, symbol, timeframe, candle_time, *_ in pendentes:
        chave = (symbol, timeframe)
        if chave not in mais_antigo or candle_time < mais_antigo[chave]:
            mais_antigo[chave] = candle_time

    futuros: dict[tuple[str, str], pd.DataFrame | None] = {}
    for chave, desde in mais_antigo.items():
        try:
            futuros[chave] = _ler_candles_depois(conn, chave[0], chave[1], desde)
        except Exception as exc:  # noqa: BLE001
            log.warning("desfecho %s %s: %s", chave[0], chave[1], exc)
            futuros[chave] = None

    atualizacoes = []
    for sinal_id, symbol, timeframe, candle_time, direcao, entrada, stop, alvo_1, alvo_2 in pendentes:
        futuro = futuros.get((symbol, timeframe))
        if futuro is None or futuro.empty:
            continue

        depois = futuro[futuro.index > candle_time]
        if depois.empty:
            continue

        risco = RiskPlan(entry=entrada, stop=stop, target_1=alvo_1, target_2=alvo_2)
        # `evaluate_signal_outcome` do motor, literalmente — incluindo o
        # desempate conservador de vela (stop ganha quando os dois são
        # tocados na mesma). É o que garante que o worker e a "Verificação
        # retroativa" da interface concordem sobre o mesmo sinal.
        resultado, detalhe, candles = evaluate_signal_outcome(risco, Direction(direcao), depois)
        atualizacoes.append((resultado, detalhe, candles, depois.index[-1], sinal_id))

    if not atualizacoes:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE signals
            SET resultado = %s, resultado_detalhe = %s, candles_ate_resultado = %s,
                avaliado_em = now(), avaliado_ate = %s
            WHERE id = %s
            """,
            atualizacoes,
        )
    conn.commit()

    resolvidos = sum(1 for linha in atualizacoes if linha[0] != "EM_ABERTO")
    log.info("desfecho: %d avaliados, %d com resultado final", len(atualizacoes), resolvidos)
    return resolvidos


# ---------------------------------------------------------------------------
# Backfill — execução única, fora do loop
# ---------------------------------------------------------------------------

def _carimbar_mtf(linhas: list[dict], momentos: list[tuple], por_tf: dict[str, list]) -> None:
    """Preenche mtf_confirmado/mtf_direcao com o estado AS-OF de cada vela.

    O `varrer()` calcula uma confirmação por modalidade a partir do M15/H1
    mais recentes e a carimba nas linhas de TODOS os timeframes. Aqui a
    mesma regra é reproduzida no passado — inclusive chamando o
    `mtf_confirmation` do motor, e não uma cópia da regra, pra não haver
    como as duas divergirem.

    ⚠️ A busca as-of é por horário de FECHAMENTO, não de abertura. Um sinal
    de M15 na vela das 10:00 fecha às 10:15; a vela de H1 das 10:00 só
    fecha às 11:00, então usá-la seria espiar 45 minutos de futuro. O
    `varrer()` não tem esse problema porque `_ler_candles` só devolve vela
    fechada — aqui o filtro tem que ser refeito à mão.

    Deixar tudo em `false` seria bem mais barato e deixaria o recorte
    "confirmado no multi-timeframe" da assertividade errado de um jeito
    que parece certo — a mesma armadilha do corte de vela em formação.
    """
    fechamentos = {
        tf: [t + TIMEFRAMES[tf]["duration"] for t, _ in serie] for tf, serie in por_tf.items()
    }

    for linha, (fechamento, modalidade) in zip(linhas, momentos):
        sinais_as_of: dict[str, list | None] = {}
        for tf, serie in por_tf.items():
            pos = bisect.bisect_right(fechamentos[tf], fechamento) - 1
            sinais_as_of[tf] = serie[pos][1] if pos >= 0 else None
        confirmado, direcao = mtf_confirmation(sinais_as_of, CONFIRMACAO, modalidade)
        linha["mtf_confirmado"] = confirmado
        linha["mtf_direcao"] = direcao.value


def _backfill_symbol(conn, symbol: str, nome_perfil: str, params: AnalysisParams,
                     velas: int, warmup: int) -> tuple[int, int]:
    """Reconstrói os sinais de um símbolo × perfil. Devolve (gravados, tentados)."""
    linhas: list[dict] = []
    momentos: list[tuple] = []
    # sinais das velas dos timeframes de confirmação, pro carimbo as-of
    por_tf: dict[str, list] = {tf: [] for tf in CONFIRMACAO}

    for timeframe in TIMEFRAMES_VARRIDOS:
        try:
            df = _ler_candles(conn, symbol, timeframe, warmup + velas)
        except Exception as exc:  # noqa: BLE001 — um par ruim não derruba o backfill
            log.warning("backfill %s %s (%s): %s", symbol, timeframe, nome_perfil, exc)
            continue

        if len(df) <= warmup:
            log.warning(
                "backfill %s %s (%s): só %d velas, warm-up é %d — pulando",
                symbol, timeframe, nome_perfil, len(df), warmup,
            )
            continue

        duracao = TIMEFRAMES[timeframe]["duration"]
        for i in range(warmup, len(df)):
            # A fatia do sinal e a do desfecho NUNCA se tocam: é a mesma
            # garantia de não espiar o futuro que o `check_signal_as_of`
            # (o modo "Verificação retroativa" da interface) já dá.
            historico = df.iloc[: i + 1]
            futuro = df.iloc[i + 1 :]
            contexto, sinais = analyze(historico, params)

            if timeframe in por_tf:
                por_tf[timeframe].append((df.index[i], sinais))

            fechamento = df.index[i] + duracao
            avaliado_ate = futuro.index[-1] if len(futuro) else None
            for sinal in sinais:
                if sinal.name not in MODALITIES:
                    continue
                payload = signal_payload(
                    symbol, timeframe, sinal, contexto, nome_perfil, params, ORIGEM_BACKFILL,
                )
                # O desfecho vai no MESMO insert, e não no passe 2: aquele
                # recorta os pendentes em ANALYZER_JANELA_DESFECHO_DIAS, e
                # um sinal de D1 de 2022 nunca sairia de "aguardando".
                desfecho = evaluate_signal_outcome(sinal.risk, sinal.direction, futuro)
                linhas.append(_preparar(payload, desfecho, avaliado_ate))
                momentos.append((fechamento, sinal.name))

    if not linhas:
        return 0, 0

    _carimbar_mtf(linhas, momentos, por_tf)

    with conn.cursor() as cur:
        cur.executemany(_INSERT_SQL, linhas)
        gravados = cur.rowcount
    conn.commit()
    return gravados, len(linhas)


def backfill(conn, velas: int, warmup: int) -> tuple[int, int]:
    """Reprocessa as velas JÁ guardadas, gerando sinais com desfecho.

    Roda uma vez, à mão, com o mercado fechado — não faz parte do loop. O
    `varrer()` só analisa a última vela fechada, então sozinho ele constrói
    o dataset pra frente e a assertividade fica sem número por semanas.

    Idempotente pelo mesmo motivo do `varrer()`: `ON CONFLICT DO NOTHING`
    sobre o índice único. Rerodar com uma janela maior só acrescenta as
    velas que ainda não estavam lá.
    """
    symbols = _watchlist(conn)
    perfis = _perfis(conn)
    gravados = 0
    tentados = 0

    for nome_perfil, params in perfis.items():
        for symbol in symbols:
            inicio = time.monotonic()
            gr, te = _backfill_symbol(conn, symbol, nome_perfil, params, velas, warmup)
            gravados += gr
            tentados += te
            log.info(
                "backfill %s (%s): %d gravado(s) de %d · %.1fs",
                symbol, nome_perfil, gr, te, time.monotonic() - inicio,
            )

    return gravados, tentados


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def uma_iteracao() -> None:
    with pool.connection() as conn:
        inicio = time.monotonic()
        gravados, tentados = varrer(conn)
        resolvidos = avaliar(conn)
    log.info(
        "varredura: %d sinal(is) novo(s) de %d tentado(s) · %d desfecho(s) · %.1fs",
        gravados, tentados, resolvidos, time.monotonic() - inicio,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker de sinais do pipeline de ações.")
    parser.add_argument("--varrer-uma-vez", action="store_true", help="uma varredura e sai")
    parser.add_argument("--avaliar-uma-vez", action="store_true", help="um passe de desfecho e sai")
    parser.add_argument("--backfill", action="store_true",
                        help="reprocessa as velas já guardadas, com desfecho, e sai")
    parser.add_argument("--backfill-velas", type=int, default=BACKFILL_VELAS,
                        help=f"velas reconstruídas por par symbol/timeframe (padrão {BACKFILL_VELAS})")
    parser.add_argument("--backfill-warmup", type=int, default=BACKFILL_WARMUP,
                        help=f"velas de aquecimento antes do primeiro sinal (padrão {BACKFILL_WARMUP})")
    args = parser.parse_args()

    if args.varrer_uma_vez or args.avaliar_uma_vez or args.backfill:
        try:
            with pool.connection() as conn:
                if args.varrer_uma_vez:
                    gravados, tentados = varrer(conn)
                    log.info("varredura única: %d gravado(s) de %d tentado(s)", gravados, tentados)
                if args.backfill:
                    log.info(
                        "backfill · %d vela(s) por par, warm-up %d · timeframes=%s · perfis=%s",
                        args.backfill_velas, args.backfill_warmup,
                        ",".join(TIMEFRAMES_VARRIDOS), ",".join(PERFIS),
                    )
                    gravados, tentados = backfill(conn, args.backfill_velas, args.backfill_warmup)
                    log.info("backfill: %d gravado(s) de %d tentado(s)", gravados, tentados)
                if args.avaliar_uma_vez:
                    log.info("desfecho único: %d resolvido(s)", avaliar(conn))
        finally:
            # o loop normal nunca sai, mas os modos de uma passada sim — e sem
            # isto o processo fica ~15s pendurado esperando as threads do pool,
            # que é justamente o caminho usado num Job one-shot
            pool.close()
        return 0

    log.info(
        "analyzer iniciado · timeframes=%s · perfis=%s · intervalo=%ds",
        ",".join(TIMEFRAMES_VARRIDOS), ",".join(PERFIS), INTERVAL_SECONDS,
    )

    estava_aberto: bool | None = None
    while True:
        aberto = _mercado_aberto()
        if aberto != estava_aberto:
            log.info("mercado %s", "aberto — varrendo" if aberto else "fechado — aguardando")
            estava_aberto = aberto

        if not aberto:
            time.sleep(FECHADO_SLEEP_SECONDS)
            continue

        try:
            uma_iteracao()
        except Exception as exc:  # noqa: BLE001 — o loop nunca morre por uma iteração ruim
            log.exception("iteração falhou: %s", exc)

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
