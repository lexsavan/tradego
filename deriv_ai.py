"""deriv_ai.py — AI signal engine for Deriv synthetic trading"""

import json, math, os, httpx
from datetime import datetime
from typing import Optional
import anthropic

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")

ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# ── Technical indicators ─────────────────────────────────────────────────────

def _ema(prices: list[float], n: int) -> float:
    if len(prices) < n:
        return prices[-1] if prices else 0.0
    k = 2 / (n + 1)
    e = sum(prices[:n]) / n
    for p in prices[n:]:
        e = p * k + e * (1 - k)
    return e

def _rsi(prices: list[float], n: int = 7) -> float:
    if len(prices) < n + 1:
        return 50.0
    gains = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses= [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    return round(100 - 100 / (1 + (ag / al if al else 9999)), 2)

def _tick_momentum(prices: list[float], n: int = 10) -> float:
    """% of last n ticks that went up"""
    if len(prices) < 2:
        return 0.5
    last = prices[-n:]
    ups = sum(1 for i in range(1, len(last)) if last[i] > last[i-1])
    return round(ups / (len(last) - 1), 3)

def _volatility_spike(prices: list[float]) -> bool:
    """True if last 5 ticks have unusually high variance"""
    if len(prices) < 10:
        return False
    recent = prices[-5:]
    baseline= prices[-20:-5]
    r_std = math.sqrt(sum((x - sum(recent)/len(recent))**2 for x in recent) / len(recent))
    b_std = math.sqrt(sum((x - sum(baseline)/len(baseline))**2 for x in baseline) / len(baseline))
    return r_std > b_std * 2.2 if b_std > 0 else False

def _noise_level(prices: list[float], n: int = 20) -> float:
    """0=calm, 1=extreme noise — direction changes / total ticks"""
    if len(prices) < 3:
        return 0.5
    last = prices[-n:]
    changes = sum(1 for i in range(1, len(last)-1)
                  if (last[i]-last[i-1]) * (last[i+1]-last[i]) < 0)
    return round(changes / max(len(last)-2, 1), 3)

def compute_indicators(prices: list[float]) -> dict:
    if len(prices) < 2:
        return {"rsi": 50, "ema_fast": 0, "ema_slow": 0, "momentum": 0.5,
                "volatility_spike": False, "noise": 0.5, "trend": "sideways",
                "last_20_dirs": []}
    rsi       = _rsi(prices, 7)
    ema_fast  = _ema(prices, 5)
    ema_slow  = _ema(prices, 20)
    momentum  = _tick_momentum(prices, 10)
    v_spike   = _volatility_spike(prices)
    noise     = _noise_level(prices, 20)
    trend     = "up" if ema_fast > ema_slow * 1.0001 else \
                "down" if ema_fast < ema_slow * 0.9999 else "sideways"
    last_20   = prices[-20:] if len(prices) >= 20 else prices
    dirs      = [1 if last_20[i] > last_20[i-1] else -1 if last_20[i] < last_20[i-1] else 0
                 for i in range(1, len(last_20))]
    return {
        "rsi": rsi,
        "ema_fast": round(ema_fast, 5),
        "ema_slow": round(ema_slow, 5),
        "momentum": momentum,
        "volatility_spike": v_spike,
        "noise": noise,
        "trend": trend,
        "last_20_dirs": dirs,
    }

# ── Claude AI signal ─────────────────────────────────────────────────────────

async def ask_claude(symbol: str, ind: dict, balance: float) -> dict:
    if not ai_client:
        return _local_signal(ind)
    dirs_str = "".join(["↑" if d == 1 else "↓" if d == -1 else "→" for d in ind["last_20_dirs"]])
    prompt = (
        f"You are a Deriv synthetic index trading AI.\n"
        f"Symbol: {symbol}\n"
        f"RSI(7): {ind['rsi']}\n"
        f"EMA fast(5): {ind['ema_fast']}  slow(20): {ind['ema_slow']}\n"
        f"Tick momentum (% up): {ind['momentum']}\n"
        f"Noise level 0-1: {ind['noise']}\n"
        f"Volatility spike: {ind['volatility_spike']}\n"
        f"Last 20 tick directions: {dirs_str}\n"
        f"Account balance: {balance} USD\n\n"
        f"Rules:\n"
        f"- RSI < 30 → BUY bias\n"
        f"- RSI > 70 → SELL bias\n"
        f"- noise > 0.6 → WAIT\n"
        f"- volatility spike → WAIT\n"
        f"- confidence must be genuine (don't force high confidence)\n\n"
        f"Reply ONLY JSON (no markdown):\n"
        f'{{ "signal": "BUY|SELL|WAIT", "confidence": 0-100, "reason": "1 sentence" }}'
    )
    try:
        msg = ai_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        result = json.loads(raw)
        result["source"] = "claude"
        result["timestamp"] = datetime.now().isoformat()
        return result
    except Exception as e:
        print(f"[DerivAI] Claude error: {e}")
        return _local_signal(ind)

async def ask_gpt(symbol: str, ind: dict) -> dict:
    if not OPENAI_KEY:
        return {"signal": "WAIT", "confidence": 0, "source": "gpt-unavailable"}
    dirs_str = "".join(["↑" if d==1 else "↓" if d==-1 else "→" for d in ind["last_20_dirs"]])
    prompt = (
        f"Deriv synthetic trading. Symbol={symbol} RSI={ind['rsi']} "
        f"EMA fast={ind['ema_fast']} slow={ind['ema_slow']} "
        f"noise={ind['noise']} spike={ind['volatility_spike']} "
        f"ticks={dirs_str}\n"
        f"Reply ONLY JSON: {{\"signal\":\"BUY|SELL|WAIT\",\"confidence\":0-100,\"reason\":\"...\"}}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                json={"model": "gpt-4o-mini", "max_tokens": 150,
                      "messages": [{"role": "user", "content": prompt}]}
            )
            raw = r.json()["choices"][0]["message"]["content"].strip()
            res = json.loads(raw.replace("```json","").replace("```",""))
            res["source"] = "gpt"
            return res
    except Exception as e:
        return {"signal": "WAIT", "confidence": 0, "source": "gpt", "error": str(e)}

async def ask_gemini(symbol: str, ind: dict) -> dict:
    if not GEMINI_KEY:
        return {"signal": "WAIT", "confidence": 0, "source": "gemini-unavailable"}
    dirs_str = "".join(["↑" if d==1 else "↓" if d==-1 else "→" for d in ind["last_20_dirs"]])
    prompt = (
        f"Deriv synthetic trading. Symbol={symbol} RSI={ind['rsi']} "
        f"EMA fast={ind['ema_fast']} slow={ind['ema_slow']} "
        f"noise={ind['noise']} spike={ind['volatility_spike']} ticks={dirs_str}\n"
        f"Reply ONLY JSON: {{\"signal\":\"BUY|SELL|WAIT\",\"confidence\":0-100,\"reason\":\"...\"}}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]}
            )
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            res = json.loads(raw.replace("```json","").replace("```","").strip())
            res["source"] = "gemini"
            return res
    except Exception as e:
        return {"signal": "WAIT", "confidence": 0, "source": "gemini", "error": str(e)}

def _vote(results: list[dict]) -> dict:
    """Consensus: if 2+ agree on BUY/SELL, use that; else WAIT"""
    valid = [r for r in results if isinstance(r, dict) and "signal" in r]
    if not valid:
        return {"signal": "WAIT", "confidence": 0, "reason": "No AI response", "source": "fallback"}
    counts: dict[str, int] = {}
    conf_sum: dict[str, float] = {}
    for r in valid:
        s = r.get("signal", "WAIT")
        counts[s] = counts.get(s, 0) + 1
        conf_sum[s] = conf_sum.get(s, 0) + r.get("confidence", 50)
    best = max(counts, key=lambda s: (counts[s], conf_sum.get(s, 0)))
    votes = counts[best]
    avg_conf = round(conf_sum[best] / votes)
    reasons = [r.get("reason","") for r in valid if r.get("signal") == best]
    if votes < 2:
        return {"signal": "WAIT", "confidence": avg_conf,
                "reason": "No consensus — " + (reasons[0] if reasons else ""), "source": "consensus-1"}
    return {"signal": best, "confidence": avg_conf,
            "reason": reasons[0] if reasons else f"{votes}/3 AIs agreed",
            "votes": f"{votes}/3", "source": "consensus"}

def _local_signal(ind: dict) -> dict:
    """Pure rule-based fallback"""
    rsi   = ind.get("rsi", 50)
    noise = ind.get("noise", 0.5)
    spike = ind.get("volatility_spike", False)
    mom   = ind.get("momentum", 0.5)
    trend = ind.get("trend", "sideways")

    if noise > 0.6 or spike:
        return {"signal": "WAIT", "confidence": 90,
                "reason": "High noise / volatility spike — skip trade", "source": "local"}
    if rsi < 30 and mom > 0.55 and trend != "down":
        return {"signal": "BUY", "confidence": 75,
                "reason": f"RSI={rsi} oversold, momentum={mom}", "source": "local"}
    if rsi > 70 and mom < 0.45 and trend != "up":
        return {"signal": "SELL", "confidence": 75,
                "reason": f"RSI={rsi} overbought, momentum={mom}", "source": "local"}
    return {"signal": "WAIT", "confidence": 70,
            "reason": f"RSI={rsi} neutral — no clear signal", "source": "local"}

async def get_signal(symbol: str, prices: list[float], balance: float,
                     use_consensus: bool = True) -> dict:
    """Main entry point — returns AI trading signal"""
    ind = compute_indicators(prices)
    # Always check noise first (fast local check)
    if ind["noise"] > 0.65 or ind["volatility_spike"]:
        return {**_local_signal(ind), "indicators": ind}

    if use_consensus and (OPENAI_KEY or GEMINI_KEY):
        import asyncio
        tasks = [ask_claude(symbol, ind, balance)]
        if OPENAI_KEY:   tasks.append(ask_gpt(symbol, ind))
        if GEMINI_KEY:   tasks.append(ask_gemini(symbol, ind))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid   = [r for r in results if isinstance(r, dict)]
        result  = _vote(valid)
    else:
        result = await ask_claude(symbol, ind, balance)

    result["indicators"] = ind
    result["timestamp"]  = datetime.now().isoformat()
    return result
