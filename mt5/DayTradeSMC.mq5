//+------------------------------------------------------------------+
//| DayTradeSMC.mq5 — Expert Advisor que implementa o núcleo do     |
//| motor Day Trade SMC. Todos os inputs são otimizáveis pelo        |
//| Strategy Tester e mapeiam 1:1 com AnalysisParams do Python.     |
//|                                                                  |
//| Opera no fechamento da vela anterior (OnBarOpen). Uma posição    |
//| por símbolo. SL/TP server-side (sobrevivem a queda da VM).      |
//|                                                                  |
//| v1: sem confirmação multi-timeframe (M15+H1). Filtra pelo IFR   |
//| do Diário. Para backtesting e otimização de parâmetros.          |
//+------------------------------------------------------------------+
#property copyright "Daniel Santos"
#property link      "https://github.com/kleverson/acoes"
#property version   "1.00"
#property strict

#include "DayTradeSMC_Sinais.mqh"
#include <Trade\Trade.mqh>

// =====================================================================
//  Inputs — agrupados como no AnalysisParams do Python
// =====================================================================

input group "=== Contexto ==="
input int    InpATRPeriodo          = 14;     // Período do ATR (Wilder)
input int    InpRSIPeriodo          = 14;     // Período do IFR (Wilder)
input double InpVolBaixaMaxPct      = 0.30;   // ATR% abaixo = vol BAIXA
input double InpVolExcessivaMinPct  = 6.0;    // ATR% acima = vol EXCESSIVA

input group "=== Estrutura (BOS/CHoCH) ==="
input double InpEstruturaVolumeMin  = 1.2;    // Volume/média mín p/ validar
input double InpEstruturaRangeMin   = 0.8;    // Amplitude/ATR mín p/ validar
input int    InpEventoMaxIdade      = 20;     // Candles desde o BOS/CHoCH
input int    InpRompimentoLookback  = 20;     // Lookback do breakout
input double InpRompimentoTolPct    = 0.3;    // Tolerância retest (%)
input int    InpFVGMaxIdade         = 20;     // Max candles p/ FVG

input group "=== VWAP ==="
input double InpVWAPDistMinPct      = 0.10;   // Dist mín p/ contar
input double InpVWAPDistMaxPct      = 1.5;    // Dist máx (bloqueia)

input group "=== IFR (RSI) ==="
input double InpRSISobrecompra      = 90.0;   // Limiar sobrecompra
input double InpRSISobrevenda       = 10.0;   // Limiar sobrevenda
input bool   InpRSIFiltro           = true;   // Filtro IFR na confluência
input double InpIFRConfirma         = 1.30;   // Mult se IFR confirma
input double InpIFRContra           = 0.45;   // Mult se IFR contraria
input double InpIFRPisoScore        = 62.0;   // Piso se IFR define direção

input group "=== Confluência ==="
input double InpPesoSMC             = 30.0;   // Peso SMC
input double InpPesoPA              = 20.0;   // Peso Price Action
input double InpPesoMA              = 20.0;   // Peso Médias Móveis
input double InpPesoVWAP            = 20.0;   // Peso VWAP
input double InpNormScore           = 79.0;   // Divisor de normalização
input double InpBandaEmpate         = 3.0;    // Banda de empate
input double InpMultProp0           = 0.60;   // Mult proporção <0.49
input double InpMultProp1           = 0.92;   // Mult proporção 0.49-0.74
input double InpMultProp2           = 1.05;   // Mult proporção 0.74-0.99
input double InpMultProp3           = 1.15;   // Mult proporção >=0.99

input group "=== Filtro de Mercado ==="
input double InpFiltIsoScoreMax     = 79.0;   // Score máx isolada
input double InpFiltIsoConfMax      = 70.0;   // Confiança máx isolada
input double InpFiltBloqScoreMax    = 39.0;   // Score máx bloqueio
input double InpFiltBloqConfMax     = 35.0;   // Confiança máx bloqueio
input double InpFiltExcScoreMax     = 59.0;   // Score máx excessiva
input double InpFiltExcConfMax      = 50.0;   // Confiança máx excessiva
input double InpScoreMinOperavel    = 40.0;   // Score mínimo p/ operar

input group "=== Risco ==="
input double InpRRAlvo1             = 1.5;    // R:R do alvo principal
input double InpStopMinimoATR       = 1.0;    // Stop mín em ATR
input double InpEncolherAlvos       = 0.85;   // Fator de encolhimento
input double InpRiscoReais          = 50.0;   // Risco máximo por trade (R$)

input group "=== Operação ==="
input int    InpBarsAnalise         = 250;    // Barras para análise
input int    InpMagicNumber         = 20260814; // Magic number
input int    InpDesvioMaxPts        = 20;     // Desvio máximo (pontos)

// =====================================================================
//  Globals
// =====================================================================
int      g_hATR, g_hRSI;
int      g_hEMA9, g_hEMA21, g_hEMA50, g_hEMA200;
int      g_hRSI_D1;
CTrade   g_trade;
SParams  g_params;
datetime g_lastBarTime;
double   g_tickSize;
double   g_posSL;
double   g_posTP;
long     g_posType;

// =====================================================================
//  OnInit
// =====================================================================
int OnInit()
{
   // Indicator handles
   g_hATR   = iATR(_Symbol, PERIOD_CURRENT, InpATRPeriodo);
   g_hRSI   = iRSI(_Symbol, PERIOD_CURRENT, InpRSIPeriodo, PRICE_CLOSE);
   g_hEMA9  = iMA(_Symbol, PERIOD_CURRENT,   9, 0, MODE_EMA, PRICE_CLOSE);
   g_hEMA21 = iMA(_Symbol, PERIOD_CURRENT,  21, 0, MODE_EMA, PRICE_CLOSE);
   g_hEMA50 = iMA(_Symbol, PERIOD_CURRENT,  50, 0, MODE_EMA, PRICE_CLOSE);
   g_hEMA200= iMA(_Symbol, PERIOD_CURRENT, 200, 0, MODE_EMA, PRICE_CLOSE);
   g_hRSI_D1= iRSI(_Symbol, PERIOD_D1, InpRSIPeriodo, PRICE_CLOSE);

   if(g_hATR == INVALID_HANDLE || g_hRSI == INVALID_HANDLE ||
      g_hEMA9 == INVALID_HANDLE || g_hEMA21 == INVALID_HANDLE ||
      g_hEMA50 == INVALID_HANDLE || g_hEMA200 == INVALID_HANDLE ||
      g_hRSI_D1 == INVALID_HANDLE)
   {
      Print("Falha ao criar handles de indicadores");
      return INIT_FAILED;
   }

   // Trade setup
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpDesvioMaxPts);
   g_trade.SetTypeFilling(DetectFillingMode());

   // Tick size
   g_tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(g_tickSize <= 0) g_tickSize = 0.01;

   // Populate params
   PopulateParams();

   g_lastBarTime = 0;
   g_posSL = 0;
   g_posTP = 0;
   g_posType = 0;
   Print("DayTradeSMC v1.00 inicializado | ", _Symbol, " ", EnumToString(Period()));
   return INIT_SUCCEEDED;
}

// =====================================================================
//  OnDeinit
// =====================================================================
void OnDeinit(const int reason)
{
   IndicatorRelease(g_hATR);
   IndicatorRelease(g_hRSI);
   IndicatorRelease(g_hEMA9);
   IndicatorRelease(g_hEMA21);
   IndicatorRelease(g_hEMA50);
   IndicatorRelease(g_hEMA200);
   IndicatorRelease(g_hRSI_D1);
}

// =====================================================================
//  OnTick
// =====================================================================
void OnTick()
{
   // --- Gestão manual de SL/TP (a cada tick, não só em nova barra) ---
   // B3 usa execução exchange: SL/TP não são nativos no broker.
   // O tester simula fielmente esse comportamento, então o EA gerencia.
   if(PositionSelect(_Symbol) && g_posSL > 0)
   {
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      bool hit = false;
      if(g_posType == POSITION_TYPE_BUY)
         hit = (bid <= g_posSL || bid >= g_posTP);
      else
         hit = (ask >= g_posSL || ask <= g_posTP);

      if(hit)
      {
         g_trade.PositionClose(_Symbol);
         g_posSL = 0;
         g_posTP = 0;
         return;
      }
   }

   if(!IsNewBar()) return;

   int bars = InpBarsAnalise;

   // --- Copiar dados (posição 1 = última fechada, pula a formando) ---
   MqlRates rates[];
   double atrBuf[], rsiBuf[], ema9Buf[], ema21Buf[], ema50Buf[], ema200Buf[];

   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, bars, rates) < bars) return;
   if(CopyBuffer(g_hATR,   0, 1, bars, atrBuf)   < bars) return;
   if(CopyBuffer(g_hRSI,   0, 1, bars, rsiBuf)   < bars) return;
   if(CopyBuffer(g_hEMA9,  0, 1, bars, ema9Buf)  < bars) return;
   if(CopyBuffer(g_hEMA21, 0, 1, bars, ema21Buf) < bars) return;
   if(CopyBuffer(g_hEMA50, 0, 1, bars, ema50Buf) < bars) return;
   if(CopyBuffer(g_hEMA200,0, 1, bars, ema200Buf)< bars) return;

   // D1 RSI (posição 1 = último D1 fechado)
   double rsiD1[];
   bool hasHigherRSI = (CopyBuffer(g_hRSI_D1, 0, 1, 1, rsiD1) == 1);
   double higherRSI  = hasHigherRSI ? rsiD1[0] : 0.0;

   // VWAP
   double vwapBuf[];
   int sessionStart;
   ComputeVWAP(rates, bars, vwapBuf, sessionStart);

   // --- Construir contexto e analisar ---
   SMarketContext ctx;
   BuildContext(rates, bars, atrBuf, rsiBuf,
                ema9Buf, ema21Buf, ema50Buf, ema200Buf,
                vwapBuf, sessionStart,
                higherRSI, hasHigherRSI,
                g_params, ctx);

   SSignal signals[];
   Analyze(ctx, g_params, g_tickSize, signals);

   // --- Decisão de trade (confluência = signals[0]) ---
   SSignal conf = signals[0];
   if(conf.direction == DIR_NEUTRAL) return;
   if(!conf.risk.valid) return;

   // Já tem posição neste símbolo?
   if(PositionSelect(_Symbol)) return;

   // Enviar ordem
   SendOrder(conf);
}

// =====================================================================
//  SendOrder
// =====================================================================
void SendOrder(const SSignal &sig)
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(ask <= 0 || bid <= 0) return;

   double price, sl, tp;
   if(sig.direction == DIR_BUY)
   {
      price = ask;
      sl    = sig.risk.stop;
      tp    = sig.risk.target1;
      // Conferir coerência: stop abaixo, alvo acima
      if(sl >= price || tp <= price) return;
   }
   else
   {
      price = bid;
      sl    = sig.risk.stop;
      tp    = sig.risk.target1;
      if(sl <= price || tp >= price) return;
   }

   // Volume por risco
   double volume = CalculateVolume(price, sl);
   if(volume <= 0) return;

   // Normalizar SL/TP ao tick
   sl = NormalizeDouble(sl, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
   tp = NormalizeDouble(tp, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));

   string comment = StringFormat("SMC s=%.0f c=%.0f", sig.score, sig.confidence);

   if(sig.direction == DIR_BUY)
      g_trade.Buy(volume, _Symbol, price, sl, tp, comment);
   else
      g_trade.Sell(volume, _Symbol, price, sl, tp, comment);

   if(g_trade.ResultRetcode() == TRADE_RETCODE_DONE ||
      g_trade.ResultRetcode() == TRADE_RETCODE_PLACED)
   {
      g_posSL   = sl;
      g_posTP   = tp;
      g_posType = (sig.direction == DIR_BUY) ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
      Print("Ordem enviada: ", (sig.direction == DIR_BUY ? "BUY" : "SELL"),
            " vol=", volume, " price=", price,
            " sl=", sl, " tp=", tp,
            " score=", sig.score);
   }
   else
      Print("Falha na ordem: ", g_trade.ResultRetcode(),
            " ", g_trade.ResultRetcodeDescription());
}

// =====================================================================
//  CalculateVolume — dimensiona pelo risco, arredonda para BAIXO
// =====================================================================
double CalculateVolume(double price, double sl)
{
   double risk = MathAbs(price - sl);
   if(risk <= 0) return 0;

   double volume  = InpRiscoReais / risk;
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double lotMin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double lotMax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);

   if(lotStep <= 0) lotStep = 1;

   // Arredonda para BAIXO
   volume = MathFloor(volume / lotStep) * lotStep;

   // Se nem o lote mínimo cabe no risco, manda o mínimo
   if(volume < lotMin) volume = lotMin;
   if(volume > lotMax) volume = lotMax;

   return NormalizeDouble(volume, 2);
}

// =====================================================================
//  DetectFillingMode — mesma lógica de execucao._modo_preenchimento
// =====================================================================
ENUM_ORDER_TYPE_FILLING DetectFillingMode()
{
   long filling = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   // SYMBOL_FILLING_FOK = 1, SYMBOL_FILLING_IOC = 2
   if((filling & 1) != 0) return ORDER_FILLING_FOK;
   if((filling & 2) != 0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

// =====================================================================
//  IsNewBar
// =====================================================================
bool IsNewBar()
{
   datetime currentBar = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(currentBar == g_lastBarTime) return false;
   g_lastBarTime = currentBar;
   return true;
}

// =====================================================================
//  PopulateParams — preenche g_params a partir dos inputs
// =====================================================================
void PopulateParams()
{
   g_params.volBaixaMaxPct     = InpVolBaixaMaxPct;
   g_params.volExcessivaMinPct = InpVolExcessivaMinPct;

   g_params.estruturaVolumeMin = InpEstruturaVolumeMin;
   g_params.estruturaRangeMin  = InpEstruturaRangeMin;
   g_params.eventoMaxIdade     = InpEventoMaxIdade;
   g_params.rompimentoLookback = InpRompimentoLookback;
   g_params.rompimentoTolPct   = InpRompimentoTolPct;
   g_params.fvgMaxIdade        = InpFVGMaxIdade;

   g_params.vwapDistMinPct     = InpVWAPDistMinPct;
   g_params.vwapDistMaxPct     = InpVWAPDistMaxPct;

   g_params.rsiSobrecompra     = InpRSISobrecompra;
   g_params.rsiSobrevenda      = InpRSISobrevenda;
   g_params.rsiFiltro          = InpRSIFiltro;
   g_params.ifrConfirma        = InpIFRConfirma;
   g_params.ifrContra          = InpIFRContra;
   g_params.ifrPisoScore       = InpIFRPisoScore;

   g_params.pesoSMC            = InpPesoSMC;
   g_params.pesoPA             = InpPesoPA;
   g_params.pesoMA             = InpPesoMA;
   g_params.pesoVWAP           = InpPesoVWAP;
   g_params.normScore          = InpNormScore;
   g_params.bandaEmpate        = InpBandaEmpate;
   g_params.multProp[0]        = InpMultProp0;
   g_params.multProp[1]        = InpMultProp1;
   g_params.multProp[2]        = InpMultProp2;
   g_params.multProp[3]        = InpMultProp3;

   g_params.filtIsoScoreMax    = InpFiltIsoScoreMax;
   g_params.filtIsoConfMax     = InpFiltIsoConfMax;
   g_params.filtBloqScoreMax   = InpFiltBloqScoreMax;
   g_params.filtBloqConfMax    = InpFiltBloqConfMax;
   g_params.filtExcScoreMax    = InpFiltExcScoreMax;
   g_params.filtExcConfMax     = InpFiltExcConfMax;
   g_params.scoreMinOperavel   = InpScoreMinOperavel;

   g_params.rrAlvo1            = InpRRAlvo1;
   g_params.stopMinimoATR      = InpStopMinimoATR;
   g_params.encolherAlvos      = InpEncolherAlvos;
}
