"""deriv_ai_v2.py — Multi-symbol + Multi-trade-type AI signal engine

AI decides:
  1. Which symbol to trade (V25/V50/V75/V100)
  2. Which trade type (rise_fall vs higher_lower)
  3. Direction (BUY/SELL or HIGHER/LOWER)
  4. Confidence score
"""

import asyncio, os, json, math
from typing import Optional
from datetime import datetime
import httpx

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")

# ── Indicators ───────────────────────────────────────────────────────────────
def compute_indicators(prices: list) -> dict:
    if len(prices) < 25:
        return {"error": "insufficient data"}
    
    p = prices[-50:] if len(prices) >= 50 else prices
    
    # RSI(7)
    gains, losses = [], []
    for i in range(1, len(p)):
        d = p[i] - p[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-7:]) / 7 if len(gains) >= 7 else 0.001
    avg_l = sum(losses[-7:]) / 7 if len(losses) >= 7 else 0.001
    rs = avg_g / max(avg_l, 0.0001)
    rsi = round(100 - (100 / (1 + rs)), 2)
    
    # EMA fast/slow
    def ema(data, period):
        if not data: return 0
        k = 2 / (period + 1)
        e = data[0]
        for x in data[1:]:
            e = x * k + e * (1 - k)
        return round(e, 5)
    
    ema_fast = ema(p, 5)
    ema_slow = ema(p, 20)
    
    # Momentum (last 10 directions)
    last_10 = []
    for i in range(max(1, len(p)-10), len(p)):
        last_10.append(1 if p[i] > p[i-1] else -1)
    momentum = round(sum(last_10) / max(len(last_10), 1), 3)
    
    # Volatility (std dev of returns)
    rets = [(p[i]-p[i-1])/p[i-1] for i in range(1, len(p))]
    mean_r = sum(rets) / max(len(rets), 1)
    var = sum((r - mean_r)**2 for r in rets) / max(len(rets), 1)
    volatility = math.sqrt(var)
    noise = round(min(volatility * 1000, 1.0), 3)
    
    # Volatility spike
    recent_vol = math.sqrt(sum((r - mean_r)**2 for r in rets[-5:]) / 5) if len(rets) >= 5 else 0
    vol_spike = recent_vol > volatility * 2
    
    # Trend
    if ema_fast > ema_slow * 1.001:    trend = "up"
    elif ema_fast < ema_slow * 0.999:  trend = "down"
    else:                               trend = "sideways"
    
    # Range strength (for higher/lower trades)
    high = max(p[-20:])
    low  = min(p[-20:])
    current = p[-1]
    range_pct = round((current - low) / max(high - low, 0.0001) * 100, 1)
    
    return {
        "rsi": rsi,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "momentum": momentum,
        "noise": noise,
        "trend": trend,
        "volatility_spike": vol_spike,
        "range_pct": range_pct,
        "current": p[-1],
        "high_20": high,
        "low_20": low,
        "last_20_dirs": last_10,
    }

# ── AI Signal Engine (Multi-AI Consensus) ────────────────────────────────────

async def _claude_signal(symbol: str, ind: dict) -> dict:
    if not ANTHROPIC_KEY:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall", "source": "claude_off"}
    
    prompt = f"""You are a trading AI for Deriv synthetic indices.

Symbol: {symbol}
Indicators:
- RSI(7): {ind['rsi']}
- EMA Fast/Slow: {ind['ema_fast']} / {ind['ema_slow']}
- Momentum: {ind['momentum']}
- Noise: {ind['noise']}
- Trend: {ind['trend']}
- Vol Spike: {ind['volatility_spike']}
- Range%: {ind['range_pct']}% (within last 20 ticks)

Decide:
1. signal: "BUY" / "SELL" / "WAIT"
2. trade_type: "rise_fall" (1-3 min binary) or "higher_lower" (longer barrier-based)
3. confidence: 0-100

Rules:
- WAIT if noise > 0.6 OR vol_spike = True
- rise_fall: trend-following, RSI < 30 or > 70
- higher_lower: when current price near range extremes (range_pct < 20 or > 80)

Reply ONLY in JSON: {{"signal": "...", "trade_type": "...", "confidence": N, "reason": "..."}}
"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200,
                      "messages": [{"role": "user", "content": prompt}]}
            )
            data = r.json()
            text = data.get("content", [{}])[0].get("text", "{}")
            text = text.strip().replace("```json","").replace("```","").strip()
            result = json.loads(text)
            result["source"] = "claude"
            return result
    except Exception as e:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall", "source": f"claude_err:{e}"}

async def _gpt_signal(symbol: str, ind: dict) -> dict:
    if not OPENAI_KEY:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall", "source": "gpt_off"}
    
    prompt = f"""Trading AI for Deriv {symbol}. Indicators: RSI={ind['rsi']}, EMA_fast={ind['ema_fast']}, EMA_slow={ind['ema_slow']}, momentum={ind['momentum']}, noise={ind['noise']}, trend={ind['trend']}, vol_spike={ind['volatility_spike']}, range_pct={ind['range_pct']}%. Decide signal (BUY/SELL/WAIT), trade_type (rise_fall/higher_lower), confidence (0-100). Reply ONLY JSON: {{"signal":"","trade_type":"","confidence":N,"reason":""}}"""
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "max_tokens": 200,
                      "messages": [{"role":"user","content":prompt}]}
            )
            data = r.json()
            text = data["choices"][0]["message"]["content"].strip().replace("```json","").replace("```","").strip()
            result = json.loads(text)
            result["source"] = "gpt"
            return result
    except Exception as e:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall", "source": f"gpt_err:{e}"}

async def _gemini_signal(symbol: str, ind: dict) -> dict:
    if not GEMINI_KEY:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall", "source": "gemini_off"}
    
    prompt = f"""Deriv {symbol} indicators: RSI={ind['rsi']}, EMA={ind['ema_fast']}/{ind['ema_slow']}, momentum={ind['momentum']}, noise={ind['noise']}, trend={ind['trend']}, range_pct={ind['range_pct']}%. Output ONLY JSON: {{"signal":"BUY|SELL|WAIT","trade_type":"rise_fall|higher_lower","confidence":0-100,"reason":""}}"""
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}",
                json={"contents":[{"parts":[{"text":prompt}]}]}
            )
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip().replace("```json","").replace("```","").strip()
            result = json.loads(text)
            result["source"] = "gemini"
            return result
    except Exception as e:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall", "source": f"gemini_err:{e}"}

async def get_signal_v2(symbol: str, prices: list, balance: float = 0,
                         use_consensus: bool = True) -> dict:
    """Multi-AI consensus signal with trade_type decision"""
    ind = compute_indicators(prices)
    if "error" in ind:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall",
                "reason": ind["error"], "indicators": ind, "votes": "0/0"}
    
    # Run all 3 AIs in parallel
    results = await asyncio.gather(
        _claude_signal(symbol, ind),
        _gpt_signal(symbol, ind),
        _gemini_signal(symbol, ind),
        return_exceptions=True
    )
    
    valid = [r for r in results if isinstance(r, dict) and not r.get("source","").endswith("_off") and not r.get("source","").startswith("err")]
    
    if not valid:
        return {"signal": "WAIT", "confidence": 0, "trade_type": "rise_fall",
                "reason": "No AI available", "indicators": ind, "votes": "0/3"}
    
    # Vote on signal
    signals = [r.get("signal","WAIT") for r in valid]
    buy_votes  = signals.count("BUY")
    sell_votes = signals.count("SELL")
    wait_votes = signals.count("WAIT")
    
    # Consensus: need 2/3 agreement
    if buy_votes >= 2:    final_signal = "BUY"
    elif sell_votes >= 2: final_signal = "SELL"
    else:                  final_signal = "WAIT"
    
    # Vote on trade_type
    trade_types = [r.get("trade_type","rise_fall") for r in valid]
    rise_fall_votes = trade_types.count("rise_fall")
    higher_lower_votes = trade_types.count("higher_lower")
    final_trade_type = "rise_fall" if rise_fall_votes >= higher_lower_votes else "higher_lower"
    
    # Average confidence (only of agreeing AIs)
    matching = [r for r in valid if r.get("signal") == final_signal]
    avg_conf = round(sum(r.get("confidence",0) for r in matching) / max(len(matching),1))
    
    # Combine reasons
    reasons = [r.get("reason","") for r in valid if r.get("signal") == final_signal]
    main_reason = reasons[0] if reasons else ""
    
    votes_str = f"{max(buy_votes, sell_votes, wait_votes)}/{len(valid)}"
    
    return {
        "signal":          final_signal,
        "trade_type":      final_trade_type,
        "confidence":      avg_conf,
        "reason":          main_reason,
        "votes":           votes_str,
        "source":          "consensus",
        "indicators":      ind,
        "ai_results":      valid,
        "buy_votes":       buy_votes,
        "sell_votes":      sell_votes,
        "wait_votes":      wait_votes,
    }

# Backward compat
get_signal = get_signal_v2
