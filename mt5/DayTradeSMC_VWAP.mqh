//+------------------------------------------------------------------+
//| DayTradeSMC_VWAP.mqh — VWAP com reset por sessão e slope.       |
//| Porta compute_daily_vwap e slope_pct do motor Python.            |
//|                                                                  |
//| Arrays: oldest-first. [0] = mais antiga, [count-1] = mais nova. |
//+------------------------------------------------------------------+
#ifndef DAYTRADE_SMC_VWAP_MQH
#define DAYTRADE_SMC_VWAP_MQH

// =====================================================================
//  ComputeVWAP — VWAP com reset diário, retorna sessionStart
// =====================================================================
void ComputeVWAP(const MqlRates &rates[], int count,
                 double &vwap[], int &sessionStart)
{
   ArrayResize(vwap, count);
   sessionStart = 0;
   double cumTPVol = 0.0, cumVol = 0.0;
   MqlDateTime dtCur, dtPrev;

   for(int i = 0; i < count; i++)
   {
      TimeToStruct(rates[i].time, dtCur);
      if(i > 0)
      {
         TimeToStruct(rates[i-1].time, dtPrev);
         if(dtCur.day != dtPrev.day || dtCur.mon != dtPrev.mon || dtCur.year != dtPrev.year)
         {
            cumTPVol = 0.0;
            cumVol   = 0.0;
            sessionStart = i;
         }
      }
      double tp  = (rates[i].high + rates[i].low + rates[i].close) / 3.0;
      double vol = (double)rates[i].tick_volume;
      cumTPVol  += tp * vol;
      cumVol    += vol;
      vwap[i]    = (cumVol > 0) ? cumTPVol / cumVol : tp;
   }
}

// =====================================================================
//  SlopePct — variação % entre arr[count-1-lookback] e arr[count-1]
// =====================================================================
double SlopePct(const double &arr[], int count, int lookback = 10)
{
   if(count < 2 || lookback < 1) return 0.0;
   lookback = MathMin(lookback, count - 1);
   double first = arr[count - 1 - lookback];
   double last  = arr[count - 1];
   if(first == 0.0) return 0.0;
   return (last - first) / first * 100.0;
}

// Versão que recebe start/end explícitos (para VWAP da sessão)
double SlopePctRange(const double &arr[], int start, int end, int lookback = 10)
{
   int len = end - start + 1;
   if(len < 2 || lookback < 1) return 0.0;
   lookback = MathMin(lookback, len - 1);
   double first = arr[end - lookback];
   double last  = arr[end];
   if(first == 0.0) return 0.0;
   return (last - first) / first * 100.0;
}

#endif // DAYTRADE_SMC_VWAP_MQH
