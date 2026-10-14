"""
executor/parametros_aprendidos.py

Veto de sinais baseado em parâmetros aprendidos pelo Laboratório.

Arquivo `parametros_aprendizado_vN.json` — gerado offline pelo Laboratório,
lido pelo executor a cada ciclo. Aplica-se SOMENTE como veto, DEPOIS que o
setup aprovou o sinal. Nunca cria entradas, nunca altera quantidade, nunca
afrouxa stop.

Formato do JSON:
{
    "versao": 1,
    "gerado_em": "2026-10-09T10:00:00-03:00",
    "modos": {"VWAP": "LIVE", "SMC": "PAPER", "CONFLUENCIA": "SHADOW"},
    "regras": {
        "vol_percentil_min": 20,
        "alvo_custo_min": 1.5,
        "bloqueios": [
            {"setup": "SMC", "regime": "LATERAL", "motivo": "SMC lateral ruim"},
            {"setup": "CONFLUENCIA", "regime": "LATERAL"}
        ]
    },
    "modelo": {
        "corte": 0.35,
        "imput": 0.0,
        "features": ["er", "adx", "atr_pct", "vol_percentil", "vwap_dist_atr"],
        "pesos": [0.2, -0.15, 0.1, 0.25, 0.3],
        "mu": [1.0, 25.0, 1.5, 50.0, 0.5],
        "sd": [0.5, 10.0, 0.5, 25.0, 1.0]
    }
}

Regras de veto:
- `vol_percentil_min`: veto se vol_percentil < mínimo
- `alvo_custo_min`: veto se (alvo - entrada) / (entrada * custo_pct) < mínimo
- `bloqueios`: veto se (setup, regime) casar
- `modelo`: z = w0 + soma(w_i * (x_i - mu_i) / sd_i), veto se z < ln(corte/(1-corte))
  - Variável ausente usa valor de "imput"

Se o arquivo estiver ausente ou inválido: operar sem veto + alerta.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("executor.parametros_aprendidos")

# Caminho do arquivo de parâmetros
PARAMS_PATH = Path(os.environ.get(
    "EXECUTOR_PARAMS_PATH",
    str(Path(__file__).resolve().parent / "parametros_aprendizado.json")
))

# Cache do arquivo (recarregado quando o mtime muda)
_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None
_alerta_emitido: bool = False


def _carregar() -> dict[str, Any] | None:
    """Carrega o JSON de parâmetros. Retorna None se ausente/inválido."""
    global _cache, _cache_mtime, _alerta_emitido

    try:
        if not PARAMS_PATH.exists():
            if not _alerta_emitido:
                log.warning(
                    "parâmetros aprendidos ausentes (%s) — operando sem veto",
                    PARAMS_PATH,
                )
                _alerta_emitido = True
            return None

        mtime = PARAMS_PATH.stat().st_mtime
        if _cache is not None and mtime == _cache_mtime:
            return _cache

        with open(PARAMS_PATH, encoding="utf-8") as f:
            data = json.load(f)

        # Validação básica
        if not isinstance(data, dict) or "versao" not in data:
            raise ValueError("JSON inválido: precisa ser objeto com 'versao'")

        _cache = data
        _cache_mtime = mtime
        _alerta_emitido = False
        log.info("parâmetros aprendidos carregados (v%s)", data.get("versao"))
        return data

    except json.JSONDecodeError as exc:
        if not _alerta_emitido:
            log.error("parâmetros aprendidos com JSON inválido: %s — sem veto", exc)
            _alerta_emitido = True
        return None
    except Exception as exc:  # noqa: BLE001
        if not _alerta_emitido:
            log.error("falha ao ler parâmetros aprendidos: %s — sem veto", exc)
            _alerta_emitido = True
        return None


def modo_do_setup(setup: str) -> str:
    """Retorna o modo (LIVE/PAPER/SHADOW) para um setup.

    Default: LIVE se não houver configuração.
    """
    params = _carregar()
    if params is None:
        return "LIVE"

    modos = params.get("modos", {})
    return modos.get(setup, "LIVE")


def verificar_veto(
    *,
    setup: str,
    regime: str,
    direcao: str,
    er: float | None = None,
    adx: float | None = None,
    atr_pct: float | None = None,
    vol_percentil: float | None = None,
    vwap_dist_atr: float | None = None,
    vwap_slope_atr: float | None = None,
    volume_rel: float | None = None,
    score: float | None = None,
    entrada: float | None = None,
    alvo: float | None = None,
    custo_pct: float = 0.0005,
) -> tuple[bool, str]:
    """Verifica se o sinal deve ser vetado.

    Returns:
        (veto, motivo)
        veto=True: sinal vetado, não enviar ordem
        veto=False: sinal aprovado
    """
    params = _carregar()
    if params is None:
        return (False, "")  # Sem parâmetros = sem veto

    regras = params.get("regras", {})
    motivos: list[str] = []

    # 1. vol_percentil_min
    vol_min = regras.get("vol_percentil_min")
    if vol_min is not None and vol_percentil is not None:
        if vol_percentil < vol_min:
            motivos.append(f"VOL_PERCENTIL<{vol_min}")

    # 2. alvo_custo_min
    alvo_min = regras.get("alvo_custo_min")
    if alvo_min is not None and entrada and alvo and custo_pct:
        custo_reais = entrada * custo_pct
        risco = abs(alvo - entrada)
        if custo_reais > 0:
            razao = risco / custo_reais
            if razao < alvo_min:
                motivos.append(f"ALVO_CUSTO<{alvo_min}")

    # 3. bloqueios setup × regime
    bloqueios = regras.get("bloqueios", [])
    for b in bloqueios:
        if b.get("setup") == setup and b.get("regime") == regime:
            motivos.append(f"BLOQUEIO_{setup}_{regime}")

    # 4. Modelo logístico
    modelo = params.get("modelo")
    if modelo is not None:
        features_valores = {
            "er": er,
            "adx": adx,
            "atr_pct": atr_pct,
            "vol_percentil": vol_percentil,
            "vwap_dist_atr": vwap_dist_atr,
            "vwap_slope_atr": vwap_slope_atr,
            "volume_rel": volume_rel,
            "score": score,
        }
        z = _calcular_z(modelo, features_valores)
        if z is not None:
            corte = modelo.get("corte", 0.5)
            if 0 < corte < 1:
                limiar = math.log(corte / (1 - corte))
                if z < limiar:
                    motivos.append(f"MODELO_Z<{limiar:.2f}")

    if motivos:
        return (True, "|".join(motivos))
    return (False, "")


def _calcular_z(modelo: dict, features: dict[str, float | None]) -> float | None:
    """Calcula z = w0 + soma(w_i * (x_i - mu_i) / sd_i).

    Retorna None se não for possível calcular (sem pesos, etc.).
    """
    pesos = modelo.get("pesos")
    mus = modelo.get("mu")
    sds = modelo.get("sd")
    feature_names = modelo.get("features")
    imput = modelo.get("imput", 0.0)

    if not pesos or not mus or not sds or not feature_names:
        return None

    # pesos tem w0 (intercepto) + w para cada feature
    # mus e sd têm um valor para cada feature (sem intercepto)
    if len(pesos) != len(feature_names) + 1 or len(mus) != len(feature_names) or len(sds) != len(feature_names):
        log.warning("modelo com dimensões inconsistentes")
        return None

    # w0 é o primeiro peso (intercepto)
    z = pesos[0]

    # Demais pesos são para as features
    for i, fname in enumerate(feature_names):
        w = pesos[i + 1]
        mu = mus[i]
        sd = sds[i]

        x = features.get(fname)
        if x is None:
            x = imput

        if sd != 0:
            z += w * ((x - mu) / sd)

    return z


def modo_livre(setup: str, modo_atual: str) -> bool:
    """Verifica se a transição de modo é permitida.

    LIVE -> PAPER: automático
    PAPER -> LIVE: NUNCA automático, exige confirmação manual
    """
    params = _carregar()
    if params is None:
        return True

    modos = params.get("modos", {})
    modo_configurado = modos.get(setup, "LIVE")

    # Se o modo configurado é mais restrito que o atual, aplica automaticamente
    ordem = {"LIVE": 0, "PAPER": 1, "SHADOW": 2}
    if ordem.get(modo_configurado, 0) > ordem.get(modo_atual, 0):
        return True  # LIVE -> PAPER -> SHADOW: automático

    return False  # PAPER -> LIVE ou SHADOW -> PAPER: precisa confirmação


def verificar_rebaixamento(
    setup: str,
    modo_atual: str,
    resultados_recentes: list[float],
    drawdown_r: float = 0.0,
) -> tuple[str, str]:
    """Fase 5.5: rebaixamento automático LIVE → PAPER.

    Critérios (qualquer um dispara):
    - Média das últimas 20 ordens < 0R
    - Drawdown > 10R

    Promoção (PAPER → LIVE) NUNCA é automática.

    Returns:
        (novo_modo, motivo)
    """
    if modo_atual != "LIVE":
        return (modo_atual, "")  # Só rebaixa LIVE

    # Critério 1: média das últimas 20 ordens
    if len(resultados_recentes) >= 5:  # mínimo 5 ordens para avaliar
        media = sum(resultados_recentes[-20:]) / min(len(resultados_recentes), 20)
        if media < 0:
            return ("PAPER", f"REBAIXADO: média {media:.2f}R < 0 nas últimas {min(len(resultados_recentes), 20)} ordens")

    # Critério 2: drawdown
    if drawdown_r > 10:
        return ("PAPER", f"REBAIXADO: drawdown {drawdown_r:.1f}R > 10R")

    return (modo_atual, "")
