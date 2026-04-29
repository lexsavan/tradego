"""deriv_ai_local.py — FREE local indicator-based signal engine
   No API calls. Uses RSI, EMA, Momentum, Noise, Trend to decide trades.
   Replaces deriv_ai_v2.py — import this instead.
"""

import math
from datetime import datetime

# ── Indicators ───────────────────────────────────────────────────────────────

def compute_indicators(prices: list) -> dict:
    if len(prices) < 25:
        return {"error": "insufficient data"}

    p = prices[-60:] if len(prices) >= 60 else prices

    # RSI(7)
    gains, losses = [], []
    for i in range(1, len(p)):
        d = p[i] - p[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-7:]) / 7 if len(gains) >= 7 else 0.001
    avg_l = sum(losses[-7:]) / 7 if len(losses) >= 7 else 0.001
    rs    = avg_g / max(avg_l, 0.0001)
    rsi   = round(100 - (100 / (1 + rs)), 2)

    # EMA fast(5) / slow(20)
    def ema(data, period):
        if not data: return 0
        k = 2 / (period + 1); e = data[0]
        for x in data[1:]: e = x * k + e * (1 - k)
        return round(e, 5)

    ema_fast = ema(p, 5)
    ema_slow = ema(p, 20)

    # Momentum (last 10 bars)
    last_dirs = [1 if p[i] > p[i-1] else -1 for i in range(max(1, len(p)-10), len(p))]
    momentum  = round(sum(last_dirs) / max(len(last_dirs), 1), 3)

    # Volatility / Noise
    rets    = [(p[i]-p[i-1])/p[i-1] for i in range(1, len(p))]
    mean_r  = sum(rets) / max(len(rets), 1)
    var     = sum((r - mean_r)**2 for r in rets) / max(len(rets), 1)
    vol     = math.sqrt(var)
    noise   = round(min(vol * 1000, 1.0), 3)

    # Vol spike
    recent_vol = math.sqrt(sum((r-mean_r)**2 for r in rets[-5:]) / 5) if len(rets) >= 5 else 0
    vol_spike  = recent_vol > vol * 2.2

    # Trend
    if ema_fast > ema_slow * 1.0008:   trend = "up"
    elif ema_fast < ema_slow * 0.9992: trend = "down"
    else:                               trend = "sideways"

    # Range position (for higher/lower trade decision)
    high20 = max(p[-20:])
    low20  = min(p[-20:])
    rng    = max(high20 - low20, 0.0001)
    range_pct = round((p[-1] - low20) / rng * 100, 1)

    return {
        "rsi":              rsi,
        "ema_fast":         ema_fast,
        "ema_slow":         ema_slow,
        "momentum":         momentum,
        "noise":            noise,
        "volatility_spike": vol_spike,
        "trend":            trend,
        "range_pct":        range_pct,
        "current":          p[-1],
        "high_20":          high20,
        "low_20":           low20,
        "last_20_dirs":     last_dirs,
    }

# ── Signal Engine (Pure Local) ───────────────────────────────────────────────

def _local_signal(symbol_key: str, ind: dict) -> dict:
    rsi       = ind["rsi"]
    noise     = ind["noise"]
    trend     = ind["trend"]
    momentum  = ind["momentum"]
    vol_spike = ind["volatility_spike"]
    range_pct = ind["range_pct"]
    ema_fast  = ind["ema_fast"]
    ema_slow  = ind["ema_slow"]

    # ── Hard WAIT conditions ──
    if vol_spike:
        return {"signal":"WAIT","confidence":0,"trade_type":"rise_fall",
                "reason":"Volatility spike — skip"}
    if noise > 0.65:
        return {"signal":"WAIT","confidence":0,"trade_type":"rise_fall",
                "reason":f"High noise {noise} — skip"}

    # ── Determine trade type ──
    # Higher/Lower: price at range extremes
    if range_pct <= 15:
        trade_type = "higher_lower"
        direction  = "BUY"    # price near bottom → expect higher
        base_conf  = 70
        reason     = f"Price near range low ({range_pct}%) — Higher trade"
    elif range_pct >= 85:
        trade_type = "higher_lower"
        direction  = "SELL"   # price near top → expect lower
        base_conf  = 70
        reason     = f"Price near range high ({range_pct}%) — Lower trade"
    else:
        trade_type = "rise_fall"
        direction  = None
        base_conf  = 0
        reason     = ""

    # ── Rise/Fall signals (trend-following) ──
    if trade_type == "rise_fall":
        # RSI oversold + uptrend
        if rsi < 28 and trend == "up" and momentum > 0.3:
            direction = "BUY"; base_conf = 82
            reason = f"RSI oversold {rsi} + uptrend + positive momentum"
        elif rsi < 32 and trend == "up":
            direction = "BUY"; base_conf = 75
            reason = f"RSI low {rsi} + uptrend"
        # RSI overbought + downtrend
        elif rsi > 72 and trend == "down" and momentum < -0.3:
            direction = "SELL"; base_conf = 82
            reason = f"RSI overbought {rsi} + downtrend + negative momentum"
        elif rsi > 68 and trend == "down":
            direction = "SELL"; base_conf = 75
            reason = f"RSI high {rsi} + downtrend"
        # EMA crossover momentum
        elif ema_fast > ema_slow * 1.001 and momentum > 0.4 and noise < 0.45:
            direction = "BUY"; base_conf = 72
            reason = f"EMA bullish crossover + strong momentum {momentum}"
        elif ema_fast < ema_slow * 0.999 and momentum < -0.4 and noise < 0.45:
            direction = "SELL"; base_conf = 72
            reason = f"EMA bearish crossover + strong momentum {momentum}"
        else:
            return {"signal":"WAIT","confidence":0,"trade_type":"rise_fall",
                    "reason":f"No clear signal — RSI={rsi} trend={trend}"}

    # ── Adjust confidence by noise ──
    noise_penalty = int(noise * 30)
    final_conf    = max(0, min(100, base_conf - noise_penalty))

    if final_conf < 65:
        return {"signal":"WAIT","confidence":final_conf,"trade_type":trade_type,
                "reason":f"Confidence too low after noise penalty: {final_conf}%"}

    return {
        "signal":     direction,
        "trade_type": trade_type,
        "confidence": final_conf,
        "reason":     reason,
        "votes":      "local/1",
        "source":     "local_indicators",
    }

# ── Public API ────────────────────────────────────────────────────────────────

async def get_signal_v2(symbol: str, prices: list, balance: float = 0,
                         use_consensus: bool = True) -> dict:
    """Drop-in replacement for AI signal — uses only local indicators"""
    ind = compute_indicators(prices)
    if "error" in ind:
        return {"signal":"WAIT","confidence":0,"trade_type":"rise_fall",
                "reason":ind["error"],"indicators":ind,"votes":"0/0"}

    result = _local_signal(symbol, ind)
    result["indicators"] = ind
    result["timestamp"]  = datetime.now().isoformat()
    return result

# Backward compat
get_signal = get_signal_v2
