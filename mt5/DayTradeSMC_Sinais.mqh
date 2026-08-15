//+------------------------------------------------------------------+
//| DayTradeSMC_Sinais.mqh — os 5 sinais isolados + confluência,    |
//| filtro de mercado, stops e gestão de risco. Porta smc_signal,   |
//| price_action_signal, moving_average_signal, vwap_signal,        |
//| rsi_signal, confluence_signal, apply_market_filter, attach_risk, |
//| structural_stop, stop_for_signal e analyze do motor Python.     |
//+------------------------------------------------------------------+
#ifndef DAYTRADE_SMC_SINAIS_MQH
#define DAYTRADE_SMC_SINAIS_MQH

#include "DayTradeSMC_Estrutura.mqh"
#include "DayTradeSMC_VWAP.mqh"

// =====================================================================
//  SParams — mapeado 1:1 com AnalysisParams do Python
// =====================================================================
struct SParams
{
   // Contexto
   double volBaixaMaxPct;       // 0.30
   double volExcessivaMinPct;   // 6.0

   // Estrutura
   double estruturaVolumeMin;   // 1.2
   double estruturaRangeMin;    // 0.8
   int    eventoMaxIdade;       // 20
   int    rompimentoLookback;   // 20
   double rompimentoTolPct;     // 0.3
   int    fvgMaxIdade;          // 20

   // VWAP
   double vwapDistMinPct;       // 0.10
   double vwapDistMaxPct;       // 1.5

   // IFR
   double rsiSobrecompra;      // 90.0
   double rsiSobrevenda;       // 10.0
   bool   rsiFiltro;           // true
   double ifrConfirma;         // 1.30
   double ifrContra;           // 0.45
   double ifrPisoScore;        // 62.0

   // Confluência
   double pesoSMC;             // 30.0
   double pesoPA;              // 20.0
   double pesoMA;              // 20.0
   double pesoVWAP;            // 20.0
   double normScore;           // 79.0
   double bandaEmpate;         // 3.0
   double multProp[4];         // {0.60, 0.92, 1.05, 1.15}

   // Filtro de mercado
   double filtIsoScoreMax;     // 79.0
   double filtIsoConfMax;      // 70.0
   double filtBloqScoreMax;    // 39.0
   double filtBloqConfMax;     // 35.0
   double filtExcScoreMax;     // 59.0
   double filtExcConfMax;      // 50.0
   double scoreMinOperavel;    // 40.0

   // Risco
   double rrAlvo1;             // 1.5
   double stopMinimoATR;       // 1.0
   double encolherAlvos;       // 0.85
};

// =====================================================================
//  Volatilidade
// =====================================================================
enum ENUM_VOLATILITY { VOL_BAIXA = 0, VOL_ADEQUADA = 1, VOL_EXCESSIVA = 2 };

// =====================================================================
//  Signal names
// =====================================================================
enum ENUM_SIGNAL_NAME
{
   SIG_CONFLUENCIA  = 0,
   SIG_SMC          = 1,
   SIG_PRICE_ACTION = 2,
   SIG_MEDIAS       = 3,
   SIG_VWAP         = 4,
   SIG_IFR          = 5
};

// =====================================================================
//  SRiskPlan / SSignal
// =====================================================================
struct SRiskPlan
{
   double entry;
   double stop;
   double target1;
   double target2;
   double rr;
   bool   valid;
};

struct SSignal
{
   ENUM_SIGNAL_NAME name;
   ENUM_DIRECTION   direction;
   double           score;
   double           confidence;
   SRiskPlan        risk;
};

// =====================================================================
//  SMarketContext
// =====================================================================
struct SMarketContext
{
   int              count;
   double           price;

   // ATR
   double           atr;
   double           atrPct;

   // Volume
   double           rvol;

   // Volatilidade
   ENUM_VOLATILITY  volatility;

   // EMAs (último valor)
   double           ema9, ema21, ema50, ema200;
   double           ema9Slope, ema21Slope;
   bool             emaReliable;

   // VWAP
   double           vwap;
   double           vwapSlopePct;
   double           vwapDistPct;
   bool             vwapRejection;

   // Swings
   SSwing           swings[];
   int              swingCount;

   // Eventos (BOS/CHoCH)
   SStructureEvent  events[];
   int              eventCount;

   // Padrões de candle (bitmask)
   int              patterns;

   // Breakout/retest
   bool             brokeHigh, brokeLow;
   bool             bullRetest, bearRetest;

   // FVG
   ENUM_FVG         fvg;

   // RSI
   double           rsi;
   double           rsiPrev;
   double           higherRSI;
   bool             hasHigherRSI;

   // Stop auxiliar: min/max dos últimos 10 candles
   double           last10Low;
   double           last10High;
};

// =====================================================================
//  BuildContext
// =====================================================================
void BuildContext(const MqlRates &rates[], int count,
                  const double &atrBuf[], const double &rsiBuf[],
                  const double &ema9Buf[], const double &ema21Buf[],
                  const double &ema50Buf[], const double &ema200Buf[],
                  const double &vwapBuf[], int vwapSessionStart,
                  double higherRSI, bool hasHigherRSI,
                  const SParams &p,
                  SMarketContext &ctx)
{
   int last = count - 1;
   ctx.count = count;
   ctx.price = rates[last].close;

   // ATR
   ctx.atr    = atrBuf[last];
   ctx.atrPct = (ctx.price > 0) ? ctx.atr / ctx.price * 100.0 : 0.0;

   // RVOL
   double volMA20 = _VolumeMA(rates, last);
   ctx.rvol = (volMA20 > 0) ? (double)rates[last].tick_volume / volMA20 : 0.0;

   // Volatilidade
   if(ctx.atrPct < p.volBaixaMaxPct)         ctx.volatility = VOL_BAIXA;
   else if(ctx.atrPct > p.volExcessivaMinPct) ctx.volatility = VOL_EXCESSIVA;
   else                                       ctx.volatility = VOL_ADEQUADA;

   // EMAs
   ctx.ema9  = ema9Buf[last];
   ctx.ema21 = ema21Buf[last];
   ctx.ema50 = ema50Buf[last];
   ctx.ema200 = ema200Buf[last];
   ctx.ema9Slope  = SlopePct(ema9Buf, count);
   ctx.ema21Slope = SlopePct(ema21Buf, count);
   ctx.emaReliable = (count >= 200);

   // VWAP
   ctx.vwap          = vwapBuf[last];
   ctx.vwapSlopePct  = SlopePctRange(vwapBuf, vwapSessionStart, last);
   ctx.vwapDistPct   = (ctx.vwap > 0)
                        ? (ctx.price - ctx.vwap) / ctx.vwap * 100.0
                        : 0.0;
   bool touchedVWAP  = (rates[last].low <= ctx.vwap && ctx.vwap <= rates[last].high);
   ctx.vwapRejection = (touchedVWAP && MathAbs(ctx.vwapDistPct) >= 0.15);

   // RSI
   ctx.rsi         = rsiBuf[last];
   ctx.rsiPrev     = (count >= 2) ? rsiBuf[last - 1] : ctx.rsi;
   ctx.higherRSI   = higherRSI;
   ctx.hasHigherRSI = hasHigherRSI;

   // Swings
   ctx.swingCount = DetectSwings(rates, count, 3, 3, ctx.swings);

   // Estrutura
   ctx.eventCount = DetectStructure(rates, count, atrBuf, ctx.swings, ctx.swingCount,
                                    p.estruturaVolumeMin, p.estruturaRangeMin, ctx.events);

   // Padrões de candle
   ctx.patterns = DetectCandlePatterns(rates, count, ctx.atr, volMA20);

   // Breakout e retest
   BreakoutAndRetest(rates, count, ctx.atr,
                     p.rompimentoLookback, p.rompimentoTolPct,
                     ctx.brokeHigh, ctx.brokeLow,
                     ctx.bullRetest, ctx.bearRetest);

   // FVG
   ctx.fvg = DetectFVG(rates, count, p.fvgMaxIdade);

   // Min/max dos últimos 10 candles (para stop de Price Action)
   ctx.last10Low  = DBL_MAX;
   ctx.last10High = -DBL_MAX;
   for(int i = MathMax(0, count - 10); i < count; i++)
   {
      if(rates[i].low  < ctx.last10Low)  ctx.last10Low  = rates[i].low;
      if(rates[i].high > ctx.last10High) ctx.last10High = rates[i].high;
   }
}

// =====================================================================
//  ApplyMarketFilter
// =====================================================================
void ApplyMarketFilter(ENUM_DIRECTION &dir, double &score, double &conf,
                       const SMarketContext &ctx, const SParams &p,
                       bool isolated, bool blockEntry = false)
{
   if(isolated)
   {
      score = fmin(score, p.filtIsoScoreMax);
      conf  = fmin(conf,  p.filtIsoConfMax);
   }
   if(ctx.volatility == VOL_BAIXA || blockEntry)
   {
      dir   = DIR_NEUTRAL;
      score = fmin(score, p.filtBloqScoreMax);
      conf  = fmin(conf,  p.filtBloqConfMax);
      return;
   }
   if(ctx.volatility == VOL_EXCESSIVA)
   {
      score = fmin(score, p.filtExcScoreMax);
      conf  = fmin(conf,  p.filtExcConfMax);
      return;
   }
   if(score < p.scoreMinOperavel)
   {
      dir  = DIR_NEUTRAL;
      conf = fmin(conf, p.filtBloqConfMax);
   }
}

// =====================================================================
//  Último evento recente
// =====================================================================
bool LastRecentEvent(const SMarketContext &ctx, const SParams &p, SStructureEvent &out)
{
   if(ctx.eventCount == 0) return false;
   out = ctx.events[ctx.eventCount - 1];
   return ((ctx.count - 1 - out.index) <= p.eventoMaxIdade);
}

// =====================================================================
//  SMCSignal
// =====================================================================
void SMCSignal(const SMarketContext &ctx, const SParams &p, SSignal &sig)
{
   sig.name = SIG_SMC;
   sig.direction = DIR_NEUTRAL;
   sig.score = 0.0;
   sig.confidence = 0.0;
   sig.risk.valid = false;

   SStructureEvent ev;
   if(LastRecentEvent(ctx, p, ev))
   {
      sig.direction = ev.direction;
      double base   = (ev.kind == EVENT_CHOCH) ? 70.0 : 55.0;
      sig.score     = fmin(90.0, base + (ev.confidence - 0.5) * 40.0);
   }

   if(ctx.fvg == FVG_ALTA && (sig.direction == DIR_BUY || sig.direction == DIR_NEUTRAL))
   {
      sig.direction = DIR_BUY;
      sig.score    += 20.0;
   }
   if(ctx.fvg == FVG_BAIXA && (sig.direction == DIR_SELL || sig.direction == DIR_NEUTRAL))
   {
      sig.direction = DIR_SELL;
      sig.score    += 20.0;
   }

   sig.confidence = sig.score * 0.8;
   ApplyMarketFilter(sig.direction, sig.score, sig.confidence, ctx, p, true);
}

// =====================================================================
//  PriceActionSignal
// =====================================================================
void PriceActionSignal(const SMarketContext &ctx, const SParams &p, SSignal &sig)
{
   sig.name = SIG_PRICE_ACTION;
   sig.risk.valid = false;

   double bullPts = ((ctx.patterns & PAT_ANY_BULLISH) != 0) ? 50.0 : 0.0;
   double bearPts = ((ctx.patterns & PAT_ANY_BEARISH) != 0) ? 50.0 : 0.0;

   if(ctx.brokeHigh)  bullPts += 45.0;
   if(ctx.brokeLow)   bearPts += 45.0;
   if(ctx.bullRetest) bullPts += 20.0;
   if(ctx.bearRetest) bearPts += 20.0;

   if(MathAbs(bullPts - bearPts) < 1.5)
   {
      sig.direction = DIR_NEUTRAL;
      sig.score     = fmax(bullPts, bearPts) * 0.5;
   }
   else if(bullPts > bearPts)
   {
      sig.direction = DIR_BUY;
      sig.score     = bullPts;
   }
   else
   {
      sig.direction = DIR_SELL;
      sig.score     = bearPts;
   }

   sig.confidence = sig.score * 0.7;
   ApplyMarketFilter(sig.direction, sig.score, sig.confidence, ctx, p, true);
}

// =====================================================================
//  MovingAverageSignal
// =====================================================================
void MovingAverageSignal(const SMarketContext &ctx, const SParams &p, SSignal &sig)
{
   sig.name = SIG_MEDIAS;
   sig.risk.valid = false;

   double minGapPct   = 0.05;
   double minSlopePct = 0.02;
   double emaGapPct   = (ctx.price > 0)
                         ? (ctx.ema9 - ctx.ema21) / ctx.price * 100.0
                         : 0.0;
   bool meaningfulGap = (MathAbs(emaGapPct) >= minGapPct);
   bool ema9Above     = (emaGapPct > 0);

   bool bullish = meaningfulGap && ema9Above &&
                  ctx.ema9Slope > minSlopePct && ctx.ema21Slope > minSlopePct;
   bool bearish = meaningfulGap && !ema9Above &&
                  ctx.ema9Slope < -minSlopePct && ctx.ema21Slope < -minSlopePct;

   if(bullish)
   {
      sig.direction = DIR_BUY;
      sig.score     = 100.0;
   }
   else if(bearish)
   {
      sig.direction = DIR_SELL;
      sig.score     = 100.0;
   }
   else if(meaningfulGap && ema9Above)
   {
      sig.direction = DIR_BUY;
      sig.score     = 40.0;
   }
   else if(meaningfulGap)
   {
      sig.direction = DIR_SELL;
      sig.score     = 40.0;
   }
   else
   {
      sig.direction = DIR_NEUTRAL;
      sig.score     = 15.0;
   }

   // EMA200 context
   if(ctx.emaReliable)
   {
      bool matches = (sig.direction == DIR_BUY  && ctx.price > ctx.ema200) ||
                     (sig.direction == DIR_SELL && ctx.price < ctx.ema200);
      if(!matches && sig.direction != DIR_NEUTRAL)
         sig.score *= 0.65;
   }

   sig.confidence = sig.score * 0.7;
   ApplyMarketFilter(sig.direction, sig.score, sig.confidence, ctx, p, true);
}

// =====================================================================
//  VWAPSignal
// =====================================================================
void VWAPSignal(const SMarketContext &ctx, const SParams &p, SSignal &sig)
{
   sig.name = SIG_VWAP;
   sig.risk.valid = false;

   double minSlopePct = 0.02;
   bool meaningfulDist = (MathAbs(ctx.vwapDistPct) >= p.vwapDistMinPct);
   bool above  = (ctx.vwapDistPct > 0);
   bool rising = (ctx.vwapSlopePct > minSlopePct);
   bool falling = (ctx.vwapSlopePct < -minSlopePct);

   if(!meaningfulDist)
   {
      sig.direction = DIR_NEUTRAL;
      sig.score     = 12.0;
   }
   else if(above && rising)
   {
      sig.direction = DIR_BUY;
      sig.score     = ctx.vwapRejection ? 85.0 : 70.0;
   }
   else if(!above && falling)
   {
      sig.direction = DIR_SELL;
      sig.score     = ctx.vwapRejection ? 85.0 : 70.0;
   }
   else if(above)
   {
      sig.direction = DIR_BUY;
      sig.score     = 30.0;
   }
   else
   {
      sig.direction = DIR_SELL;
      sig.score     = 30.0;
   }

   bool tooFar = (MathAbs(ctx.vwapDistPct) > p.vwapDistMaxPct);
   sig.confidence = sig.score * 0.7;
   ApplyMarketFilter(sig.direction, sig.score, sig.confidence, ctx, p, true, tooFar);
}

// =====================================================================
//  RSISignal
// =====================================================================
void RSISignal(const SMarketContext &ctx, const SParams &p, SSignal &sig)
{
   sig.name = SIG_IFR;
   sig.risk.valid = false;

   if(ctx.rsi <= p.rsiSobrevenda)
   {
      sig.direction = DIR_BUY;
      sig.score     = 100.0;
   }
   else if(ctx.rsi >= p.rsiSobrecompra)
   {
      sig.direction = DIR_SELL;
      sig.score     = 100.0;
   }
   else
   {
      sig.direction = DIR_NEUTRAL;
      sig.score     = 0.0;
   }

   // Higher-timeframe RSI discount
   if(ctx.hasHigherRSI && sig.direction != DIR_NEUTRAL)
   {
      if(sig.direction == DIR_BUY && ctx.higherRSI >= p.rsiSobrecompra)
         sig.score *= 0.55;
      if(sig.direction == DIR_SELL && ctx.higherRSI <= p.rsiSobrevenda)
         sig.score *= 0.55;
   }

   sig.confidence = sig.score * 0.7;
   ApplyMarketFilter(sig.direction, sig.score, sig.confidence, ctx, p, true);
}

// =====================================================================
//  ConfluenceSignal
// =====================================================================
void ConfluenceSignal(const SMarketContext &ctx, const SParams &p,
                      const SSignal &isolated[], const SSignal &rsi,
                      SSignal &sig)
{
   sig.name = SIG_CONFLUENCIA;
   sig.risk.valid = false;

   double weights[4];
   weights[0] = p.pesoSMC;            // SIG_SMC
   weights[1] = p.pesoPA;             // SIG_PRICE_ACTION
   weights[2] = p.pesoMA;             // SIG_MEDIAS
   weights[3] = p.pesoVWAP;           // SIG_VWAP

   double buy = 0.0, sell = 0.0;
   int totalDir = 0;

   for(int i = 0; i < 4; i++)
   {
      if(weights[i] <= 0) continue;
      totalDir++;
      double normStr = fmin(isolated[i].score / p.normScore, 1.0);
      double pts     = weights[i] * normStr;
      if(isolated[i].direction == DIR_BUY)       buy  += pts;
      else if(isolated[i].direction == DIR_SELL)  sell += pts;
   }

   if(ctx.volatility == VOL_ADEQUADA) { buy += 10.0; sell += 10.0; }

   if(MathAbs(buy - sell) < p.bandaEmpate)
   {
      sig.direction = DIR_NEUTRAL;
      sig.score     = fmax(buy, sell) * 0.5;
   }
   else if(buy > sell)
   {
      sig.direction = DIR_BUY;
      sig.score     = buy;
   }
   else
   {
      sig.direction = DIR_SELL;
      sig.score     = sell;
   }

   // --- IFR como filtro pós-decisão ---
   if(p.rsiFiltro && rsi.direction != DIR_NEUTRAL)
   {
      if(sig.direction == DIR_NEUTRAL)
      {
         sig.direction = rsi.direction;
         sig.score     = fmax(sig.score, p.ifrPisoScore);
      }
      else if(rsi.direction == sig.direction)
      {
         sig.score = fmin(100.0, sig.score * p.ifrConfirma);
      }
      else
      {
         sig.score *= p.ifrContra;
      }
   }

   // --- Multiplicador proporcional ---
   int agreeing = 0;
   for(int i = 0; i < 4; i++)
   {
      if(weights[i] <= 0) continue;
      if(isolated[i].direction == sig.direction) agreeing++;
   }

   double proporcao = (totalDir > 0) ? (double)agreeing / totalDir : 0.0;
   int banda;
   if(proporcao >= 0.99)      banda = 3;
   else if(proporcao >= 0.74) banda = 2;
   else if(proporcao >= 0.49) banda = 1;
   else                       banda = 0;

   sig.score = fmin(100.0, sig.score * p.multProp[banda]);
   sig.confidence = (totalDir > 0) ? (double)agreeing / totalDir * 100.0 : 0.0;

   ApplyMarketFilter(sig.direction, sig.score, sig.confidence, ctx, p, false);
}

// =====================================================================
//  StructuralStop
// =====================================================================
double StructuralStop(const SMarketContext &ctx, ENUM_DIRECTION dir)
{
   if(dir == DIR_BUY)
   {
      double best = -DBL_MAX;
      for(int i = 0; i < ctx.swingCount; i++)
         if(ctx.swings[i].kind == SWING_LOW && ctx.swings[i].price < ctx.price)
            if(ctx.swings[i].price > best)
               best = ctx.swings[i].price;
      if(best > -DBL_MAX) return best - ctx.atr * 0.2;
      return ctx.price - ctx.atr * 1.2;
   }

   double best = DBL_MAX;
   for(int i = 0; i < ctx.swingCount; i++)
      if(ctx.swings[i].kind == SWING_HIGH && ctx.swings[i].price > ctx.price)
         if(ctx.swings[i].price < best)
            best = ctx.swings[i].price;
   if(best < DBL_MAX) return best + ctx.atr * 0.2;
   return ctx.price + ctx.atr * 1.2;
}

// =====================================================================
//  StopForSignal
// =====================================================================
double StopForSignal(const SSignal &sig, const SMarketContext &ctx)
{
   ENUM_DIRECTION dir = sig.direction;

   // Confluência / SMC: stop estrutural
   if(sig.name == SIG_CONFLUENCIA || sig.name == SIG_SMC)
      return StructuralStop(ctx, dir);

   // Price Action: min/max 10 candles
   if(sig.name == SIG_PRICE_ACTION)
   {
      if(dir == DIR_BUY)
         return ctx.last10Low - ctx.atr * 0.15;
      return ctx.last10High + ctx.atr * 0.15;
   }

   // Médias Móveis: EMA21 (ou EMA50 se EMA21 está do lado errado)
   if(sig.name == SIG_MEDIAS)
   {
      if(dir == DIR_BUY)
      {
         double base = (ctx.ema21 < ctx.price) ? ctx.ema21 : ctx.ema50;
         return base - ctx.atr * 0.3;
      }
      double base = (ctx.ema21 > ctx.price) ? ctx.ema21 : ctx.ema50;
      return base + ctx.atr * 0.3;
   }

   // IFR: ATR puro
   if(sig.name == SIG_IFR)
   {
      if(dir == DIR_BUY) return ctx.price - ctx.atr * 1.2;
      return ctx.price + ctx.atr * 1.2;
   }

   // VWAP (fallback)
   if(dir == DIR_BUY) return ctx.vwap - ctx.atr * 0.5;
   return ctx.vwap + ctx.atr * 0.5;
}

// =====================================================================
//  RoundTick — arredonda ao tick do símbolo
// =====================================================================
double RoundTick(double price, int mode, double tick)
{
   if(tick <= 0) tick = 0.01;
   double scaled = price / tick;
   double units;
   if(mode == 0)      units = MathFloor(scaled + 1e-12);  // floor
   else if(mode == 1) units = MathCeil(scaled - 1e-12);   // ceil
   else               units = MathFloor(scaled + 0.5);    // nearest
   return units * tick;
}

// =====================================================================
//  AttachRisk — calcula entry/stop/target e preenche SRiskPlan
// =====================================================================
void AttachRisk(SSignal &sig, const SMarketContext &ctx, const SParams &p, double tick)
{
   sig.risk.valid = false;
   if(sig.direction == DIR_NEUTRAL) return;

   double entry = RoundTick(ctx.price, 2, tick); // nearest
   double stop  = StopForSignal(sig, ctx);
   double minDist = ctx.atr * p.stopMinimoATR;

   if(sig.direction == DIR_BUY)
   {
      if(entry - stop < minDist) stop = entry - minDist;
      stop = RoundTick(stop, 0, tick); // floor
      double risk = entry - stop;
      if(risk <= 0) { sig.direction = DIR_NEUTRAL; return; }
      sig.risk.entry   = entry;
      sig.risk.stop    = stop;
      sig.risk.target1 = RoundTick(entry + risk * p.rrAlvo1 * p.encolherAlvos, 1, tick);
      sig.risk.target2 = RoundTick(entry + risk * p.rrAlvo1 * 2.0 * p.encolherAlvos, 1, tick);
      sig.risk.rr      = MathAbs(sig.risk.target1 - entry) / risk;
   }
   else
   {
      if(stop - entry < minDist) stop = entry + minDist;
      stop = RoundTick(stop, 1, tick); // ceil
      double risk = stop - entry;
      if(risk <= 0) { sig.direction = DIR_NEUTRAL; return; }
      sig.risk.entry   = entry;
      sig.risk.stop    = stop;
      sig.risk.target1 = RoundTick(entry - risk * p.rrAlvo1 * p.encolherAlvos, 0, tick);
      sig.risk.target2 = RoundTick(entry - risk * p.rrAlvo1 * 2.0 * p.encolherAlvos, 0, tick);
      sig.risk.rr      = MathAbs(entry - sig.risk.target1) / risk;
   }
   sig.risk.valid = true;
}

// =====================================================================
//  Analyze — roda o motor completo sobre um timeframe
// =====================================================================
void Analyze(const SMarketContext &ctx, const SParams &p, double tick, SSignal &signals[])
{
   ArrayResize(signals, 6);

   // 4 sinais isolados (índices 1-4 no array, ordem: SMC, PA, MA, VWAP)
   SSignal isolated[4];
   SMCSignal(ctx, p, isolated[0]);
   PriceActionSignal(ctx, p, isolated[1]);
   MovingAverageSignal(ctx, p, isolated[2]);
   VWAPSignal(ctx, p, isolated[3]);

   // IFR
   SSignal rsiSig;
   RSISignal(ctx, p, rsiSig);

   // Confluência
   SSignal confSig;
   ConfluenceSignal(ctx, p, isolated, rsiSig, confSig);

   // Montar array de saída: confluência primeiro
   signals[0] = confSig;
   signals[1] = isolated[0]; // SMC
   signals[2] = isolated[1]; // Price Action
   signals[3] = isolated[2]; // Médias
   signals[4] = isolated[3]; // VWAP
   signals[5] = rsiSig;      // IFR

   // Anexar risco a todos
   for(int i = 0; i < 6; i++)
      AttachRisk(signals[i], ctx, p, tick);
}

#endif // DAYTRADE_SMC_SINAIS_MQH
