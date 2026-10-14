# ANÁLISE DETALHADA DOS SETUPS DE TRADING — DAY TRADE SMC

## Visão Geral do Motor

O motor analisa 6 modalidades independentes sobre o mesmo contexto de mercado:
- **SMC** (Smart Money Concepts)
- **Price Action**
- **Médias Móveis**
- **VWAP** (Volume Weighted Average Price)
- **IFR** (Índice de Força Relativa) — filtro, não voto
- **Confluência** — combina as 4 primeiras

Cada modalidade retorna um `Signal` com: direção, score (0-100), confiança, setup, reasons e alerts.

---

## 1. SETUP SMC (Smart Money Concepts)

### Conceito
Identifica mudanças de estrutura de mercado via BOS (Break of Structure) e CHoCH (Change of Character), com confirmação por FVG (Fair Value Gap).

### Lógica de Entrada
```
SE existe evento de estrutura recente (BOS ou CHoCH):
    direção = direção do evento
    base_score = 70.0 se CHoCH, senão 55.0
    score = base_score + (confiança_evento - 0.5) * 40.0
    score = min(score, 90.0)
    
SE FVG de alta presente E direção BUY ou NEUTRAL:
    direção = BUY
    score += 20
    
SE FVG de baixa presente E direção SELL ou NEUTRAL:
    direção = SELL
    score += 20
```

### Parâmetros
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `estrutura_range_min` | 1.0 | Amplitude mínima do evento / ATR para validar BOS/CHoCH |
| `evento_max_idade` | 3 | Idade máxima do evento em candles para ser "recente" |
| `fvg_lookback` | 20 | Lookback para detectar FVG |
| `fvg_fill_tolerance` | 0.3 | Tolerância de preenchimento da FVG (30%) |

### Filtro de Mercado Aplicado
- **Leitura isolada**: score máximo 79, confiança máxima 70
- **Volatilidade BAIXA**: bloqueia entrada (score → 39)
- **Volatilidade EXCESSIVA**: score máximo 59
- **Score mínimo operável**: 40

### Setup Identificados
- "Reversão de tendência (CHoCH)"
- "Continuação de tendência (BOS)"
- "FVG + Retorno (alta/baixa)"

---

## 2. SETUP PRICE ACTION

### Conceito
Opera padrões de candle (engolfo, pin bar, candle de força) combinados com rompimentos de máxima/mínima e retestes.

### Lógica de Entrada
```
bull_points = 0
bear_points = 0

SE padrão de candle bullish presente:
    bull_points += 50
SE padrão de candle bearish presente:
    bear_points += 50

SE rompimento de máxima (lookback candles):
    bull_points += 45
SE rompimento de mínima:
    bear_points += 45

SE reteste de alta sustentado:
    bull_points += 20
SE reteste de baixa sustentado:
    bear_points += 20

SE |bull_points - bear_points| < 1.5:
    direção = NEUTRO
    score = max(bull, bear) * 0.5
SENÃO:
    direção = maior pontuador
    score = pontuação do maior
```

### Padrões Reconhecidos
- **Bullish**: ENGOLFO_ALTA, PIN_BAR_ALTA, CANDLE_FORCA_ALTA
- **Bearish**: ENGOLFO_BAIXA, PIN_BAR_BAIXA, CANDLE_FORCA_BAIXA

### Parâmetros
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `rompimento_lookback` | 20 | Número de candles para medir máxima/mínima |
| `reteste_lookback` | 5 | Lookback para confirmar reteste |

### Filtro de Mercado Aplicado
- **Leitura isolada**: score máximo 79, confiança máxima 70

---

## 3. SETUP MÉDIAS MÓVEIS

### Conceito
Opera o alinhamento e inclinação das EMAs 9 e 21, com filtro de contexto pela EMA 200.

### Lógica de Entrada
```
ema_gap_pct = (EMA9 - EMA21) / preço * 100
min_gap_pct = 0.05
min_slope_pct = 0.02

SE gap significativo (|gap| >= 0.05%):
    SE EMA9 > EMA21 E inclinação9 > 0.02 E inclinação21 > 0.0:
        direção = BUY, score = 100
    SE EMA9 < EMA21 E inclinação9 < -0.02 E inclinação21 < 0.0:
        direção = SELL, score = 100
    SENÃO (alinhamento parcial):
        score = 40
SENÃO:
    direção = NEUTRO, score = 15

SE contexto EMA200 disponível:
    SE direção contrária à EMA200:
        score *= 0.65
```

### Parâmetros
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `min_gap_pct` | 0.05 | Distância mínima EMA9/EMA21 (%) |
| `min_slope_pct` | 0.02 | Inclinação mínima das EMAs (%) |

### EMAs Utilizadas
- EMA 9 (curta)
- EMA 21 (média)
- EMA 50 (contexto)
- EMA 200 (longa — filtro de tendência)

---

## 4. SETUP VWAP

### Conceito
Opera a relação preço-VWAP com confirmação de inclinação. A VWAP é o nível institucional mais importante do dia.

### Lógica de Entrada
```
distância = (preço - VWAP) / VWAP * 100
min_distance = params.vwap_distancia_min_pct
min_slope = 0.02

SE |distância| < mínima:
    direção = NEUTRO, score = 12 (preço colado na VWAP)
SENÃO SE preço > VWAP E VWAP subindo (slope > 0.02):
    direção = BUY
    score = 85 se rejeição, senão 70
SENÃO SE preço < VWAP E VWAP descendo (slope < -0.02):
    direção = SELL
    score = 85 se rejeição, senão 70
SENÃO SE preço > VWAP (sem inclinação):
    direção = BUY, score = 30
SENÃO:
    direção = SELL, score = 30

SE |distância| > máxima:
    bloqueia entrada (block_entry=True)
```

### Parâmetros
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `vwap_distancia_min_pct` | 0.1 | Distância mínima pra considerar acima/abaixo |
| `vwap_distancia_max_pct` | 2.0 | Distância máxima (anti-perseguição) |
| `vwap_slope_min_pct` | 0.02 | Inclinação mínima da VWAP |

### Detecção de Rejeição
- `vwap_rejection`: preço tocou a VWAP e rejeitou (score +15)

---

## 5. SETUP IFR (Filtro de Exaustão)

### Conceito
**NÃO vota na Confluência**. Age como filtro pós-decisão: se o IFR está exaustivo e contradiz a direção estrutural, reduz o score.

### Lógica
```
SE IFR <= sobrevenda (10 para Day Trade):
    direção = BUY, score = 100 (exaustão vendedora)
SE IFR >= sobrecompra (90 para Day Trade):
    direção = SELL, score = 100 (exaustão compradora)
SENÃO:
    direção = NEUTRO
```

### Filtro na Confluência (rsi_filtro=True)
```
SE direção estrutural = NEUTRO E IFR exaustivo:
    direção = IFR define direção
    score = max(score, ifr_piso_score=62)
SE IFR confirma direção:
    score *= ifr_filtro_confirma (1.30)
SE IFR contradiz direção:
    score *= ifr_filtro_contra (0.45)
```

### Parâmetros
| Parâmetro | Day Trade | Swing Trade |
|-----------|-----------|-------------|
| `rsi_sobrevenda` | 10 | 20 |
| `rsi_sobrecompra` | 90 | 80 |
| `rsi_filtro` | True | False |
| `ifr_filtro_confirma` | 1.30 | — |
| `ifr_filtro_contra` | 0.45 | — |
| `ifr_piso_score` | 62.0 | — |

---

## 6. SETUP CONFLUÊNCIA

### Conceito
Combina as 4 leituras estruturais (SMC, Price Action, Médias, VWAP) em um score único. O IFR não vota, mas filtra após a decisão.

### Lógica de Cálculo
```
Para cada leitura com peso > 0:
    força_normalizada = min(score_leitura / normalizacao_score, 1.0)
    pontos = peso_leitura * força_normalizada
    SE direção == BUY: buy += pontos
    SE direção == SELL: sell += pontos

SE volatilidade == ADEQUADA:
    buy += 10
    sell += 10

SE |buy - sell| < banda_empate (3.0):
    direção = NEUTRO
    score = max(buy, sell) * 0.5
SENÃO:
    direção = maior acumulador
    score = valor do maior

# Filtro IFR (após direção)
SE rsi_filtro ativo:
    [aplicar regras do IFR acima]

# Multiplicador de concordância
proporção = agreeing / total_leituras
multiplicador = multiplicador_proporcao[banda(proporção)]
score = min(100, score * multiplicador)
```

### Pesos (Day Trade)
| Leitura | Peso |
|---------|------|
| SMC | 30 |
| Price Action | 20 |
| Médias Móveis | 20 |
| VWAP | 20 |

### Multiplicador por Proporção
| Proporção | Multiplicador |
|-----------|---------------|
| < 0.49 | 0.60 |
| 0.49–0.74 | 0.92 |
| 0.74–0.99 | 1.05 |
| ≥ 0.99 | 1.15 |

### Parâmetros
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `normalizacao_score` | 79.0 | Divisor de normalização |
| `confluencia_banda_empate` | 3.0 | Diferença mínima compra/venda |
| `multiplicador_proporcao` | (0.60, 0.92, 1.05, 1.15) | Faixas de concordância |

---

## 7. SETUP CHATGPT (2026-10-06)

### Conceito
Day trade B3 usando EMA 200 + EMA 21 + VWAP + Volume + ATR. Contexto 60min, entrada 15min.

### Fluxo Completo
```
1. Filtro de tendência (EMA 200 + EMA 21)
   - Alta: preço > EMA200 E EMA21 > EMA200
   - Baixa: preço < EMA200 E EMA21 < EMA200
   - Sem tendência → NO_TRADE

2. Filtro de lateralização
   - EMA21 variação < 0.1% em 10 candles → NO_TRADE

3. Filtro de volume
   - volume_atual / volume_média_20 >= 1.2×
   - Abaixo → NO_TRADE

4. Direção pela tendência
   - Alta → BUY
   - Baixa → SELL

5. Filtro de distância VWAP
   - BUY: preço deve estar acima da VWAP
   - SELL: preço deve estar abaixo da VWAP

6. Stop estrutural
   - BUY: fundo dos últimos 10 candles - 0.20 × ATR
   - SELL: topo dos últimos 10 candles + 0.20 × ATR

7. Alvo fixo
   - R/R = 1.25

8. Score composto
```

### Score Composto
```
tendencia_score = 1.0 se EMA alinhada, senão 0.5
vwap_score = 1.0 se |distância| >= 0.2%, senão 0.6
volume_score = min(1.0, volume_ratio / 1.2)
rr_score = min(1.0, R/R_real / 1.25)

score = (tendencia_score × 0.35 + vwap_score × 0.25 + volume_score × 0.25 + rr_score × 0.15) × 100
```

### Parâmetros
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `chatgpt_volume_min_ratio` | 1.2 | Volume mínimo vs média 20 |
| `chatgpt_volume_media_periodo` | 20 | Período da média de volume |
| `chatgpt_atr_stop_buffer` | 0.20 | Folga do stop em ATR |
| `chatgpt_time_stop_candles` | 4 | Candles de 15min pra sair por tempo |
| `chatgpt_rr_alvo` | 1.25 | R/R alvo principal |
| `chatgpt_filtrar_lateral` | True | Evitar lateralização |
| `chatgpt_horario_inicio_manha` | 10:00 | Início da janela manhã |
| `chatgpt_horario_fim_manha` | 12:00 | Fim da janela manhã |
| `chatgpt_horario_inicio_tarde` | 14:00 | Início da janela tarde |
| `chatgpt_horario_fim_tarde` | 16:00 | Fim da janela tarde |

---

## 8. SETUP TPO/VWAP (2026-09-30)

### Conceito
VWAP define a direção, TPO (Time Price Opportunity) define a localização. Opera na abertura do pregão.

### Fluxo Completo
```
1. Análise da 1ª vela (2 minutos após abertura)
   - Corpo mínimo: 30% do range
   - Sombra máxima: 40% do range
   - Range: 0.3–3.0 × ATR

2. VWAP: direção
   - Distância mínima: 0.1%
   - Acima → BUY
   - Abaixo → SELL

3. TPO: localização
   - POC (Point of Control)
   - VAH (Value Area High)
   - VAL (Value Area Low)

4. Anti-perseguição
   - Distância máxima da VWAP: 0.5%
   - Distância máxima do nível de entrada: 0.3%

5. Direção final
   - VWAP + 1ª vela devem concordar
   - Se contradizem → NO_TRADE

6. Stop estrutural
   - Base: TPO_LEVEL, FIRST_CANDLE, ou LOWEST
   - Folga: 0.3 × ATR

7. Alvo
   - FIXED_RR: 1.5 R/R preferido, 1.0 mínimo
   - STRUCTURE: níveis de estrutura

8. Score composto (pesos)
```

### Score Composto (TPO/VWAP)
| Componente | Peso |
|------------|------|
| VWAP | 0.25 |
| 1ª vela | 0.15 |
| TPO | 0.20 |
| Volume | 0.10 |
| Localização entrada | 0.15 |
| R/R | 0.15 |

### Parâmetros
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `market_open_hour` | 10 | Hora de abertura (Brasília) |
| `first_candle_minutes` | 2 | Duração da 1ª vela |
| `vwap_min_distance_pct` | 0.1 | Distância mínima da VWAP |
| `min_body_pct` | 30.0 | Corpo mínimo da 1ª vela (%) |
| `max_wick_pct` | 40.0 | Sombra máxima da 1ª vela (%) |
| `min_range_atr` | 0.3 | Range mínimo (ATR) |
| `max_range_atr` | 3.0 | Range máximo (ATR) |
| `entry_mode` | RETEST | BREAKOUT \| PULLBACK \| RETEST |
| `max_distance_from_vwap_pct` | 0.5 | Anti-perseguição VWAP |
| `max_distance_from_entry_pct` | 0.3 | Anti-perseguição entrada |
| `stop_basis` | TPO_LEVEL | FIRST_CANDLE \| TPO_LEVEL \| LOWEST |
| `stop_buffer_atr` | 0.3 | Folga do stop (ATR) |
| `min_risk_atr` | 0.5 | Risco mínimo (ATR) |
| `preferred_rr` | 1.5 | R/R preferido |
| `minimum_rr` | 1.0 | R/R mínimo |
| `minimum_signal_score` | 60.0 | Score mínimo pra executar |
| `lock_asset_for_day` | True | 1 entrada por ativo por dia |

---

## FILTROS DE MERCADO (apply_market_filter)

Todos os setups passam por este filtro após o cálculo do score:

```python
def apply_market_filter(direction, score, confidence, context, isolated=False, block_entry=False):
    # Leitura isolada: cap em 79
    if isolated:
        score = min(score, 79.0)
        confidence = min(confidence, 70.0)
    
    # Volatilidade BAIXA ou block_entry: bloqueia
    if volatility == "BAIXA" or block_entry:
        return NEUTRAL, min(score, 39.0), min(confidence, 35.0)
    
    # Volatilidade EXCESSIVA: cap em 59
    if volatility == "EXCESSIVA":
        return direction, min(score, 59.0), min(confidence, 50.0)
    
    # Score mínimo operável
    if score < 40.0:
        return NEUTRAL, score, min(confidence, 35.0)
    
    return direction, score, confidence
```

---

## RISCO E EXECUÇÃO

### Parâmetros de Risco
| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `rr_alvo_1` | 1.5 | R/R do alvo 1 |
| `rr_alvo_2` | 3.0 | R/R do alvo 2 |
| `stop_minimo_atr` | 1.0 | Stop mínimo em ATR |
| `encolher_alvos` | 0.85 | Contrai alvos para 85% da distância |
| `stop_maximo_pct` | 0.5 | Stop máximo em % do preço |
| `gain_maximo_pct` | 0.5 | Gain máximo em % do preço |

### Execução no Executor
- **Score mínimo manhã**: 79
- **Score mínimo tarde** (14h+): 91 (apenas score > 90)
- **Frescor máximo**: 15 minutos
- **Risco por operação**: R$ 150 (configurável)
- **Exigir MTF**: Sim (padrão)

---

## PERFORMANCE HISTÓRICA (12 dias)

| Modalidade | Ordens | Acerto | Resultado |
|------------|--------|--------|-----------|
| VWAP | 39 | 64% | +R$ 1.450 |
| CHATGPT | 5 | 60% | +R$ 156 |
| TPO/VWAP | 1 | 0% | -R$ 117 |
| SMC | 27 | 30% | -R$ 1.555 |
| Confluência | 100 | 45% | -R$ 1.818 |

---

## CONFIGURAÇÃO ATUAL (parametros_aprendizado.json)

```json
{
    "modos": {
        "VWAP": "LIVE",
        "SMC": "SHADOW",
        "Confluência": "PAPER",
        "CHATGPT": "LIVE",
        "TPO/VWAP": "SHADOW"
    },
    "regras": {
        "vol_percentil_min": 20,
        "alvo_custo_min": 1.5,
        "bloqueios": [
            {"setup": "SMC", "regime": "LATERAL"},
            {"setup": "Confluência", "regime": "LATERAL"}
        ]
    }
}
```
