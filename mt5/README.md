# DayTradeSMC — Expert Advisor para MetaTrader 5

Port do núcleo do motor Python (`daytrade_smc.py`) para MQL5, focado em
**backtesting e otimização de parâmetros** via Strategy Tester do MT5.

## O que faz

- Implementa os 5 sinais isolados (SMC, Price Action, Médias Móveis, VWAP, IFR)
  e a confluência ponderada, com todos os filtros e thresholds do motor Python
- Cada input do EA mapeia 1:1 com um campo de `AnalysisParams`
- Opera no fechamento da vela anterior (OnBarOpen), uma posição por símbolo
- SL/TP server-side (broker gerencia, sobrevive a queda da VM)
- Volume dimensionado pelo risco em R$ (arredonda para BAIXO ao lot step)
- IFR do Diário como filtro de contexto (higher-timeframe RSI)

## O que NÃO faz (v1)

- **Sem confirmação multi-timeframe M15+H1** — opera no timeframe do gráfico.
  Para comparação exata com o pipeline Python, rodá-lo em M15 e adicionar
  confirmação H1 numa v2
- **Sem scanner multi-ativo** — roda num gráfico por vez
- **Sem persistência de sinais** — quem mede é o Python (analyzer worker)

## Arquivos

```
mt5/
├── DayTradeSMC.mq5              # EA principal (inputs, OnInit, OnTick, trade)
├── DayTradeSMC_Estrutura.mqh    # Swings, BOS/CHoCH, candle patterns, breakout, FVG
├── DayTradeSMC_VWAP.mqh         # VWAP com reset por sessão, slope
├── DayTradeSMC_Sinais.mqh       # Sinais, confluência, filtro, stops, risco
└── README.md
```

## Instalação

1. Copiar a pasta `mt5/` para `MQL5/Experts/DayTradeSMC/` no data folder do MT5:
   ```
   # Achar o data folder: no MT5, File → Open Data Folder
   # Tipicamente: %APPDATA%\MetaQuotes\Terminal\<hash>\MQL5\Experts\
   ```

2. No MetaEditor (F4 no MT5), abrir `DayTradeSMC.mq5` e compilar (F7).
   Deve compilar sem erros nem warnings.

3. O EA aparece em Navigator → Expert Advisors → DayTradeSMC.

## Backtest (Strategy Tester)

1. No MT5: View → Strategy Tester (Ctrl+R)
2. Configurar:
   - **Expert**: DayTradeSMC
   - **Symbol**: VALE3, PETR4, etc.
   - **Period**: M15 (Day Trade) ou D1 (Swing)
   - **Date**: 1-2 anos
   - **Modeling**: "Every tick based on real ticks" (mais preciso) ou
     "1 minute OHLC" (mais rápido, suficiente para M15)
   - **Deposit**: R$ 10.000 (ou o capital simulado)
   - **Leverage**: 1:1 (ações B3, sem alavancagem)
3. Clicar Start para single run, ou na aba Optimization para otimizar.

## Otimização de parâmetros

Os inputs mais importantes para otimizar:

| Input | Range sugerido | Step |
|---|---|---|
| `InpRRAlvo1` | 0.8 — 3.0 | 0.2 |
| `InpStopMinimoATR` | 0.4 — 1.5 | 0.1 |
| `InpPesoSMC` | 10 — 50 | 5 |
| `InpPesoPA` | 10 — 40 | 5 |
| `InpPesoMA` | 10 — 40 | 5 |
| `InpPesoVWAP` | 0 — 30 | 5 |
| `InpBandaEmpate` | 1.0 — 5.0 | 0.5 |
| `InpEncolherAlvos` | 0.70 — 1.00 | 0.05 |
| `InpRSISobrecompra` | 80 — 95 | 5 |
| `InpRSISobrevenda` | 5 — 20 | 5 |

Usar "Fast genetic based algorithm" para a primeira passada, depois refinar
com "Slow complete algorithm" nos 3-5 melhores ranges.

### Trazer parâmetros para o Python

Os valores otimizados mapeiam 1:1 para `AnalysisParams`. Exemplo:

```python
params = AnalysisParams(
    rr_alvo_1=2.0,           # InpRRAlvo1
    stop_minimo_atr=0.8,     # InpStopMinimoATR
    peso_smc=35.0,           # InpPesoSMC
    peso_price_action=15.0,  # InpPesoPA
    peso_medias=25.0,        # InpPesoMA
    peso_vwap=15.0,          # InpPesoVWAP
    # ... etc
)
```

## Mapeamento EA ↔ Python

| EA Input | AnalysisParams field |
|---|---|
| `InpATRPeriodo` | `atr_periodo` |
| `InpRSIPeriodo` | `rsi_periodo` |
| `InpVolBaixaMaxPct` | `vol_baixa_max_pct` |
| `InpVolExcessivaMinPct` | `vol_excessiva_min_pct` |
| `InpEstruturaVolumeMin` | `estrutura_volume_min` |
| `InpEstruturaRangeMin` | `estrutura_range_min` |
| `InpEventoMaxIdade` | `evento_max_idade` |
| `InpRompimentoLookback` | `rompimento_lookback` |
| `InpRompimentoTolPct` | `rompimento_tolerancia_pct` |
| `InpFVGMaxIdade` | `fvg_max_idade` |
| `InpVWAPDistMinPct` | `vwap_distancia_min_pct` |
| `InpVWAPDistMaxPct` | `vwap_distancia_max_pct` |
| `InpRSISobrecompra` | `rsi_sobrecompra` |
| `InpRSISobrevenda` | `rsi_sobrevenda` |
| `InpRSIFiltro` | `rsi_filtro` |
| `InpIFRConfirma` | `ifr_filtro_confirma` |
| `InpIFRContra` | `ifr_filtro_contra` |
| `InpIFRPisoScore` | `ifr_piso_score` |
| `InpPesoSMC` | `peso_smc` |
| `InpPesoPA` | `peso_price_action` |
| `InpPesoMA` | `peso_medias` |
| `InpPesoVWAP` | `peso_vwap` |
| `InpNormScore` | `normalizacao_score` |
| `InpBandaEmpate` | `confluencia_banda_empate` |
| `InpMultProp0..3` | `multiplicador_proporcao[0..3]` |
| `InpFiltIsoScoreMax` | `filtro_isolada_score_max` |
| `InpFiltIsoConfMax` | `filtro_isolada_confianca_max` |
| `InpFiltBloqScoreMax` | `filtro_bloqueio_score_max` |
| `InpFiltBloqConfMax` | `filtro_bloqueio_confianca_max` |
| `InpFiltExcScoreMax` | `filtro_excessiva_score_max` |
| `InpFiltExcConfMax` | `filtro_excessiva_confianca_max` |
| `InpScoreMinOperavel` | `score_minimo_operavel` |
| `InpRRAlvo1` | `rr_alvo_1` |
| `InpStopMinimoATR` | `stop_minimo_atr` |
| `InpEncolherAlvos` | `encolher_alvos` |
| `InpRiscoReais` | *(execucao.py: risco_maximo)* |

## Validação cruzada com o Python

Para confirmar que o EA e o motor Python concordam:

1. Rodar o EA em visual mode no Strategy Tester sobre um ativo/período
2. Rodar `python daytrade_smc.py VALE3 --timeframe M15 --count 250` no mesmo período
3. Comparar: direção, score e stop devem ser iguais (ou muito próximos)
4. Instalar o LuxAlgo no mesmo gráfico para validar visualmente os pontos de
   estrutura (BOS/CHoCH, FVGs) — os três devem marcar os mesmos eventos
