"""
executor/signal_logger.py

Log CSV de sinais elegíveis — uma linha por sinal que o setup aprovou,
operado ou não.

Por que CSV e não banco: o log é matéria-prima do Laboratório, que roda
offline e precisa de algo que o pandas leria direto. CSV é o formato mais
barato de consumir nesse contexto, e o volume (centenas de linhas/dia)
não justifica uma tabela nova no Postgres.

Colunas (ordem fixa):
    id, timestamp_sinal, ativo, setup, variante, modo, elegivel, decisao,
    motivo, direcao, regime, er, adx, atr_pct, vol_percentil, vwap_dist_atr,
    vwap_slope_atr, volume_rel, score, entrada, stop, alvo, custo_pct,
    saida_timestamp, saida_preco, saida_motivo, resultado_r, resultado_brl

Convenções:
    - `timestamp_sinal`: ISO com fuso -03:00 (Brasília), NÃO UTC.
    - `vol_percentil`: percentil (0-100) do ATR% atual vs ATR% do mesmo
      horário nas 20 sessões anteriores do mesmo ativo.
    - `vwap_dist_atr` e `vwap_slope_atr`: sinal A FAVOR da direção
      (positivo = a favor).
    - `resultado_r`: líquido de custos, em múltiplos do risco inicial.
    - Sinal não operado = modo SHADOW, decisao NO_TRADE, resultado SIMULADO.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("executor.signal_logger")

# Fuso de Brasília para o timestamp do sinal
TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

# Ordem fixa das colunas (Fase 6: colunas novas adicionadas)
COLUNAS = [
    "id", "timestamp_sinal", "ativo", "setup", "variante", "modo",
    "elegivel", "decisao", "motivo", "direcao", "regime",
    "er", "adx", "atr_pct", "vol_percentil", "vwap_dist_atr",
    "vwap_slope_atr", "volume_rel", "score", "entrada", "stop", "alvo",
    "custo_pct", "saida_timestamp", "saida_preco", "saida_motivo",
    "resultado_r", "resultado_brl",
    # Fase 6: colunas novas
    "entrada_modo", "recuo_atr", "rr_real_liquido", "fonte_dados", "volume_tipo",
]

# Diretório padrão do log (relativo ao executor)
LOG_DIR = Path(os.environ.get(
    "EXECUTOR_LOG_DIR",
    str(Path(__file__).resolve().parent / "logs")
))
LOG_FILENAME = "sinais.csv"


def _garantir_diretorio() -> Path:
    """Cria o diretório do log se não existir."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / LOG_FILENAME


def _cabecalho_necessario(caminho: Path) -> bool:
    """True se o arquivo não existe ou está vazio (precisa de cabeçalho)."""
    if not caminho.exists():
        return True
    return caminho.stat().st_size == 0


def gravar_sinal(
    *,
    id: str,
    timestamp_sinal: datetime,
    ativo: str,
    setup: str,
    variante: str,
    modo: str,  # LIVE | PAPER | SHADOW
    elegivel: bool,
    decisao: str,  # TRADE | NO_TRADE
    motivo: str,
    direcao: str,  # LONG | SHORT
    regime: str,  # TENDENCIA | LATERAL | INDEFINIDO
    er: float | None = None,
    adx: float | None = None,
    atr_pct: float | None = None,
    vol_percentil: float | None = None,
    vwap_dist_atr: float | None = None,
    vwap_slope_atr: float | None = None,
    volume_rel: float | None = None,
    score: float | None = None,
    entrada: float | None = None,
    stop: float | None = None,
    alvo: float | None = None,
    custo_pct: float | None = None,
    saida_timestamp: datetime | None = None,
    saida_preco: float | None = None,
    saida_motivo: str | None = None,
    resultado_r: float | None = None,
    resultado_brl: float | None = None,
    # Fase 6: colunas novas
    entrada_modo: str | None = None,
    recuo_atr: float | None = None,
    rr_real_liquido: float | None = None,
    fonte_dados: str | None = None,
    volume_tipo: str | None = None,
) -> None:
    """Grava uma linha no log CSV.

    Não levanta exceção: um erro de I/O não pode derrubar o executor.
    """
    try:
        caminho = _garantir_diretorio()
        precisar_cabecalho = _cabecalho_necessario(caminho)

        # Timestamp com fuso -03:00
        ts = timestamp_sinal.astimezone(TZ_BRASILIA).isoformat()
        ts_saida = (
            saida_timestamp.astimezone(TZ_BRASILIA).isoformat()
            if saida_timestamp else ""
        )

        linha = {
            "id": id,
            "timestamp_sinal": ts,
            "ativo": ativo,
            "setup": setup,
            "variante": variante,
            "modo": modo,
            "elegivel": str(elegivel).upper(),
            "decisao": decisao,
            "motivo": motivo,
            "direcao": direcao,
            "regime": regime,
            "er": _fmt(er),
            "adx": _fmt(adx),
            "atr_pct": _fmt(atr_pct),
            "vol_percentil": _fmt(vol_percentil),
            "vwap_dist_atr": _fmt(vwap_dist_atr),
            "vwap_slope_atr": _fmt(vwap_slope_atr),
            "volume_rel": _fmt(volume_rel),
            "score": _fmt(score),
            "entrada": _fmt(entrada),
            "stop": _fmt(stop),
            "alvo": _fmt(alvo),
            "custo_pct": _fmt(custo_pct),
            "saida_timestamp": ts_saida,
            "saida_preco": _fmt(saida_preco),
            "saida_motivo": saida_motivo or "",
            "resultado_r": _fmt(resultado_r),
            "resultado_brl": _fmt(resultado_brl),
            # Fase 6: colunas novas
            "entrada_modo": entrada_modo or "",
            "recuo_atr": _fmt(recuo_atr),
            "rr_real_liquido": _fmt(rr_real_liquido),
            "fonte_dados": fonte_dados or "",
            "volume_tipo": volume_tipo or "",
        }

        with open(caminho, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUNAS)
            if precisar_cabecalho:
                writer.writeheader()
            writer.writerow(linha)

        log.debug("sinal %s gravado no log CSV (modo=%s, decisao=%s)",
                  id, modo, decisao)

    except Exception as exc:  # noqa: BLE001 — log de I/O não pode derrubar
        log.warning("falha ao gravar sinal %s no CSV: %s", id, exc)


def _fmt(valor: float | None) -> str:
    """Formata float para CSV: vazio se None, senão 6 casas decimais."""
    if valor is None:
        return ""
    return f"{valor:.6f}"


def simular_saida(
    *,
    direcao: str,
    entrada: float,
    stop: float,
    alvo: float,
    velas: list[dict],
    custo_pct: float = 0.0005,  # 0.05% round-trip (corretagem + emolumentos)
    max_velas: int = 8,  # saída por tempo após 8 velas
) -> tuple[str, float, float]:
    """Simula a saída de um sinal SHADOW usando as mesmas regras de stop/alvo.

    Args:
        direcao: COMPRA ou VENDA
        entrada: preço de entrada
        stop: preço de stop
        alvo: preço de alvo
        velas: lista de dicts com 'open', 'high', 'low', 'close' (cronológicas)
        custo_pct: custo percentual round-trip
        max_velas: número máximo de velas antes de saída por tempo

    Returns:
        (motivo, preco_saida, resultado_r)
        motivo: STOP, ALVO, TEMPO
        resultado_r: resultado líquido de custos em múltiplos do risco
    """
    risco = abs(entrada - stop)
    if risco <= 0:
        return ("INVALIDO", entrada, 0.0)

    sentido = 1 if direcao == "COMPRA" else -1

    for i, vela in enumerate(velas[:max_velas]):
        low = vela.get("low", 0)
        high = vela.get("high", 0)

        # Verifica stop e alvo na mesma vela (stop tem prioridade se ambos)
        if direcao == "COMPRA":
            if low <= stop:
                # Stop atingido
                resultado_bruto = (stop - entrada) * sentido
                custo = entrada * custo_pct
                resultado_liquido = resultado_bruto - custo
                return ("STOP", stop, resultado_liquido / risco)
            if high >= alvo:
                resultado_bruto = (alvo - entrada) * sentido
                custo = entrada * custo_pct
                resultado_liquido = resultado_bruto - custo
                return ("ALVO", alvo, resultado_liquido / risco)
        else:  # VENDA
            if high >= stop:
                resultado_bruto = (stop - entrada) * sentido
                custo = entrada * custo_pct
                resultado_liquido = resultado_bruto - custo
                return ("STOP", stop, resultado_liquido / risco)
            if low <= alvo:
                resultado_bruto = (alvo - entrada) * sentido
                custo = entrada * custo_pct
                resultado_liquido = resultado_bruto - custo
                return ("ALVO", alvo, resultado_liquido / risco)

    # Saída por tempo: fecha no close da última vela
    if velas:
        ultimo_close = velas[min(max_velas, len(velas)) - 1].get("close", entrada)
        resultado_bruto = (ultimo_close - entrada) * sentido
        custo = entrada * custo_pct
        resultado_liquido = resultado_bruto - custo
        return ("TEMPO", ultimo_close, resultado_liquido / risco)

    return ("INDEFINIDO", entrada, 0.0)
