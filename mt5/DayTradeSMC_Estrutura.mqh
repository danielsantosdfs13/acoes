//+------------------------------------------------------------------+
//| DayTradeSMC_Estrutura.mqh — swings, BOS/CHoCH, candle patterns, |
//| breakout/retest, FVG. Porta detect_swings, detect_structure,     |
//| candle_patterns, breakout_and_retest, detect_fvg_setup do motor  |
//| Python (daytrade_smc.py).                                        |
//|                                                                  |
//| Arrays: sempre oldest-first. [0] = barra mais antiga,            |
//| [count-1] = barra mais recente (último fechamento).              |
//+------------------------------------------------------------------+
#ifndef DAYTRADE_SMC_ESTRUTURA_MQH
#define DAYTRADE_SMC_ESTRUTURA_MQH

// =====================================================================
//  Enums
// =====================================================================
enum ENUM_DIRECTION   { DIR_NEUTRAL = 0, DIR_BUY = 1, DIR_SELL = -1 };
enum ENUM_SWING_KIND  { SWING_LOW = 0, SWING_HIGH = 1 };
enum ENUM_EVENT_KIND  { EVENT_BOS = 0, EVENT_CHOCH = 1 };
enum ENUM_FVG         { FVG_NONE = 0, FVG_ALTA = 1, FVG_BAIXA = 2 };

// Candle patterns como bitmask
#define PAT_NONE           0x00
#define PAT_ENGOLFO_ALTA   0x01
#define PAT_ENGOLFO_BAIXA  0x02
#define PAT_PIN_BAR_ALTA   0x04
#define PAT_PIN_BAR_BAIXA  0x08
#define PAT_FORCA_ALTA     0x10
#define PAT_FORCA_BAIXA    0x20
#define PAT_INSIDE_BAR     0x40

#define PAT_ANY_BULLISH    (PAT_ENGOLFO_ALTA | PAT_PIN_BAR_ALTA | PAT_FORCA_ALTA)
#define PAT_ANY_BEARISH    (PAT_ENGOLFO_BAIXA | PAT_PIN_BAR_BAIXA | PAT_FORCA_BAIXA)

// =====================================================================
//  Structs
// =====================================================================
struct SSwing
{
   int              index;
   int              confirmed;
   double           price;
   ENUM_SWING_KIND  kind;
};

struct SStructureEvent
{
   int              index;
   ENUM_EVENT_KIND  kind;
   ENUM_DIRECTION   direction;
   double           confidence;
   double           level;
};

// =====================================================================
//  Helpers
// =====================================================================
double _VolumeMA(const MqlRates &rates[], int pos, int period = 20, int minPeriods = 5)
{
   int start = MathMax(0, pos - period + 1);
   int actual = pos - start + 1;
   if(actual < minPeriods) return 0.0;
   double sum = 0.0;
   for(int i = start; i <= pos; i++)
      sum += (double)rates[i].tick_volume;
   return sum / actual;
}

// =====================================================================
//  DetectSwings — pivô left/right, retorna nº de swings
// =====================================================================
int DetectSwings(const MqlRates &rates[], int count, int left, int right, SSwing &swings[])
{
   ArrayResize(swings, 0, 64);
   int n = 0;

   for(int i = left; i < count - right; i++)
   {
      double hi = rates[i].high;
      double lo = rates[i].low;
      int windowStart = i - left;
      int windowEnd   = i + right;

      // --- Swing High ---
      bool isHigh  = true;
      bool unique  = true;
      for(int j = windowStart; j <= windowEnd; j++)
      {
         if(rates[j].high > hi) { isHigh = false; break; }
         if(j != i && rates[j].high == hi) unique = false;
      }
      if(isHigh && unique)
      {
         n++;
         ArrayResize(swings, n, 64);
         swings[n-1].index     = i;
         swings[n-1].confirmed = i + right;
         swings[n-1].price     = hi;
         swings[n-1].kind      = SWING_HIGH;
      }

      // --- Swing Low ---
      bool isLow    = true;
      bool uniqueL  = true;
      for(int j = windowStart; j <= windowEnd; j++)
      {
         if(rates[j].low < lo) { isLow = false; break; }
         if(j != i && rates[j].low == lo) uniqueL = false;
      }
      if(isLow && uniqueL)
      {
         n++;
         ArrayResize(swings, n, 64);
         swings[n-1].index     = i;
         swings[n-1].confirmed = i + right;
         swings[n-1].price     = lo;
         swings[n-1].kind      = SWING_LOW;
      }
   }
   return n;
}

// =====================================================================
//  DetectStructure — BOS/CHoCH com validação de volume e amplitude
// =====================================================================
int DetectStructure(const MqlRates &rates[], int count,
                    const double &atr[], const SSwing &swings[], int swingCount,
                    double volMin, double rangeMin,
                    SStructureEvent &events[])
{
   ArrayResize(events, 0, 32);
   int n = 0;

   SSwing pendingHigh = {};  bool hasPendingHigh = false;
   SSwing pendingLow  = {};  bool hasPendingLow  = false;
   ENUM_DIRECTION trend = DIR_NEUTRAL;

   for(int i = 0; i < count; i++)
   {
      for(int s = 0; s < swingCount; s++)
      {
         if(swings[s].confirmed != i) continue;
         if(swings[s].kind == SWING_HIGH)
           { pendingHigh = swings[s]; hasPendingHigh = true; }
         else
           { pendingLow = swings[s]; hasPendingLow = true; }
      }

      double a = (atr[i] > 0) ? atr[i] : 1e-9;
      double volMA = _VolumeMA(rates, i);
      if(volMA <= 0) continue;

      double volRatio   = (double)rates[i].tick_volume / volMA;
      double rangeRatio = (rates[i].high - rates[i].low) / a;
      bool   valid      = (volRatio >= volMin && rangeRatio >= rangeMin);
      double conf       = fmin(1.0, 0.5 * fmin(1.0, volRatio / 2.4)
                                   + 0.5 * fmin(1.0, rangeRatio / 1.6));

      if(hasPendingHigh && rates[i].close > pendingHigh.price && valid)
      {
         ENUM_EVENT_KIND k = (trend == DIR_SELL) ? EVENT_CHOCH : EVENT_BOS;
         trend = DIR_BUY;
         n++;
         ArrayResize(events, n, 32);
         events[n-1].index      = i;
         events[n-1].kind       = k;
         events[n-1].direction  = DIR_BUY;
         events[n-1].confidence = conf;
         events[n-1].level      = pendingHigh.price;
         hasPendingHigh = false;
      }

      if(hasPendingLow && rates[i].close < pendingLow.price && valid)
      {
         ENUM_EVENT_KIND k = (trend == DIR_BUY) ? EVENT_CHOCH : EVENT_BOS;
         trend = DIR_SELL;
         n++;
         ArrayResize(events, n, 32);
         events[n-1].index      = i;
         events[n-1].kind       = k;
         events[n-1].direction  = DIR_SELL;
         events[n-1].confidence = conf;
         events[n-1].level      = pendingLow.price;
         hasPendingLow = false;
      }
   }
   return n;
}

// =====================================================================
//  DetectCandlePatterns — retorna bitmask dos padrões encontrados
// =====================================================================
int DetectCandlePatterns(const MqlRates &rates[], int count, double atr, double volMA)
{
   if(count < 2 || atr <= 0 || volMA <= 0) return PAT_NONE;

   int last = count - 1;
   int prev = count - 2;
   double candleRange = fmax(rates[last].high - rates[last].low, 1e-9);
   double body        = MathAbs(rates[last].close - rates[last].open);
   double upperWick   = rates[last].high - fmax(rates[last].open, rates[last].close);
   double lowerWick   = fmin(rates[last].open, rates[last].close) - rates[last].low;

   bool sigSize   = candleRange >= atr * 0.8;
   bool sigVolume = (double)rates[last].tick_volume / volMA >= 1.3;
   if(!sigSize || !sigVolume) return PAT_NONE;

   int pat = PAT_NONE;
   bool bullish  = rates[last].close > rates[last].open;
   bool bearish  = rates[last].close < rates[last].open;
   bool prevBull = rates[prev].close > rates[prev].open;
   bool prevBear = rates[prev].close < rates[prev].open;

   if(bullish && prevBear &&
      rates[last].close >= rates[prev].open &&
      rates[last].open  <= rates[prev].close)
      pat |= PAT_ENGOLFO_ALTA;

   if(bearish && prevBull &&
      rates[last].close <= rates[prev].open &&
      rates[last].open  >= rates[prev].close)
      pat |= PAT_ENGOLFO_BAIXA;

   if(bullish && lowerWick / candleRange >= 0.6) pat |= PAT_PIN_BAR_ALTA;
   if(bearish && upperWick / candleRange >= 0.6) pat |= PAT_PIN_BAR_BAIXA;

   if(body / candleRange >= 0.7)
      pat |= (bullish ? PAT_FORCA_ALTA : PAT_FORCA_BAIXA);

   if(rates[last].high <= rates[prev].high &&
      rates[last].low  >= rates[prev].low)
      pat |= PAT_INSIDE_BAR;

   return pat;
}

// =====================================================================
//  ConfirmedBreakout
// =====================================================================
bool ConfirmedBreakout(const MqlRates &candle, double level, bool alta,
                       double atr, double volMA, double marginATR = 0.15)
{
   if(atr <= 0 || volMA <= 0) return false;
   double margin    = atr * marginATR;
   double volRatio  = (double)candle.tick_volume / volMA;
   double rngRatio  = (candle.high - candle.low) / atr;
   bool   confirmed = (volRatio >= 1.1 && rngRatio >= 0.6);
   if(alta) return (candle.close > level + margin && confirmed);
   return (candle.close < level - margin && confirmed);
}

// =====================================================================
//  BreakoutAndRetest — 4 bools (brokeHigh, brokeLow, bullRetest, bearRetest)
// =====================================================================
void BreakoutAndRetest(const MqlRates &rates[], int count, double atr,
                       int lookback, double tolerancePct,
                       bool &brokeHigh, bool &brokeLow,
                       bool &bullRetest, bool &bearRetest)
{
   brokeHigh = brokeLow = bullRetest = bearRetest = false;
   if(count < lookback + 2) return;

   int last = count - 1;

   // Referência: lookback barras antes da atual
   double highLevel = -DBL_MAX, lowLevel = DBL_MAX;
   for(int i = last - lookback; i < last; i++)
   {
      if(rates[i].high > highLevel) highLevel = rates[i].high;
      if(rates[i].low  < lowLevel)  lowLevel  = rates[i].low;
   }

   double curVolMA = _VolumeMA(rates, last);
   brokeHigh = ConfirmedBreakout(rates[last], highLevel, true,  atr, curVolMA);
   brokeLow  = ConfirmedBreakout(rates[last], lowLevel,  false, atr, curVolMA);

   double tol = tolerancePct / 100.0;

   int scanStart = MathMax(1, count - 6);
   for(int idx = scanStart; idx < last; idx++)
   {
      int priorStart = MathMax(0, idx - lookback);
      if(priorStart >= idx) continue;

      double hiRef = -DBL_MAX, loRef = DBL_MAX;
      for(int j = priorStart; j < idx; j++)
      {
         if(rates[j].high > hiRef) hiRef = rates[j].high;
         if(rates[j].low  < loRef) loRef = rates[j].low;
      }

      double idxVolMA = _VolumeMA(rates, idx);

      if(ConfirmedBreakout(rates[idx], hiRef, true, atr, idxVolMA))
      {
         bool near    = MathAbs(rates[last].close - hiRef) / hiRef <= tol;
         bool touched = rates[last].low <= hiRef * (1.0 + tol);
         if(near && touched && rates[last].close >= hiRef)
            bullRetest = true;
      }

      if(ConfirmedBreakout(rates[idx], loRef, false, atr, idxVolMA))
      {
         bool near    = MathAbs(rates[last].close - loRef) / loRef <= tol;
         bool touched = rates[last].high >= loRef * (1.0 - tol);
         if(near && touched && rates[last].close <= loRef)
            bearRetest = true;
      }
   }
}

// =====================================================================
//  DetectFVG — Fair Value Gap próximo do preço
// =====================================================================
ENUM_FVG DetectFVG(const MqlRates &rates[], int count, int maxAge)
{
   if(count < 4) return FVG_NONE;
   double price     = rates[count-1].close;
   double tolerance = price * 0.0015;
   int first        = MathMax(1, count - maxAge);

   for(int mid = count - 2; mid >= first; mid--)
   {
      double c1High = rates[mid-1].high;
      double c3Low  = rates[mid+1].low;

      // FVG de alta: gap entre c1.high e c3.low
      if(c1High < c3Low)
      {
         double bottom = c1High;
         double top    = c3Low;
         bool filled   = false;
         for(int k = mid + 2; k < count; k++)
            if(rates[k].low <= bottom) { filled = true; break; }
         bool near = (bottom - tolerance <= price && price <= top + tolerance);
         if(!filled && near) return FVG_ALTA;
      }

      // FVG de baixa: gap entre c3.high e c1.low
      double c1Low  = rates[mid-1].low;
      double c3High = rates[mid+1].high;
      if(c1Low > c3High)
      {
         double bottom = c3High;
         double top    = c1Low;
         bool filled   = false;
         for(int k = mid + 2; k < count; k++)
            if(rates[k].high >= top) { filled = true; break; }
         bool near = (bottom - tolerance <= price && price <= top + tolerance);
         if(!filled && near) return FVG_BAIXA;
      }
   }
   return FVG_NONE;
}

#endif // DAYTRADE_SMC_ESTRUTURA_MQH
