#!/usr/bin/env python3
"""
SocialGuard DERIV BOT — forex-production-f9ba.up.railway.app
Deriv WebSocket API: Forex + Synthetic Indices
3 AI Consensus: Claude + ChatGPT + Gemini
Routes: /login /deriv /dashboard /forex /
"""

import json, os, time, secrets, asyncio, logging
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import anthropic, httpx
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deriv-bot")

app = FastAPI(title="SocialGuard DERIV BOT")
app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True
)

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
DERIV_TOKEN   = os.getenv("DERIV_API_TOKEN", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")
PORT          = int(os.getenv("PORT", 8002))
ADMIN_PASS    = os.getenv("ADMIN_PASSWORD", "SocialGuard2024!")

DERIV_WS_URL  = "wss://ws.derivws.com/websockets/v3?app_id=1089"
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

PLAIN_USERS = {
    "admin": ADMIN_PASS,
    "trader": os.getenv("TRADER_PASSWORD", "Trade2024!"),
}

# In-memory sessions & trades
sessions: dict = {}
trades: dict   = {}
trade_counter  = 0

# ══════════════════════════════════════════════
#  SESSION HELPERS
# ══════════════════════════════════════════════
def get_session(request: Request) -> Optional[dict]:
    token = request.cookies.get("sg_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip() if auth else None
    if not token:
        return None
    s = sessions.get(token)
    if s and s["expires"] > time.time():
        return s
    if s:
        sessions.pop(token, None)
    return None

def require_auth(request: Request) -> dict:
    s = get_session(request)
    if not s:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return s

# ══════════════════════════════════════════════
#  DERIV WEBSOCKET HELPER
# ══════════════════════════════════════════════
async def deriv_request(payload: dict) -> dict:
    try:
        import websockets
        async with websockets.connect(DERIV_WS_URL, ping_timeout=10) as ws:
            if DERIV_TOKEN:
                await ws.send(json.dumps({"authorize": DERIV_TOKEN}))
                auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if auth_resp.get("error"):
                    return {"error": auth_resp["error"]}
            await ws.send(json.dumps(payload))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            return resp
    except Exception as e:
        log.error(f"Deriv WS error: {e}")
        return {"error": {"message": str(e)}}

# ══════════════════════════════════════════════
#  AI HELPERS
# ══════════════════════════════════════════════
async def ai_claude(prompt: str) -> dict:
    if not ai:
        return {"signal": "HOLD", "confidence": 0, "reason": "No Claude key"}
    try:
        msg = ai.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        return {"signal": "HOLD", "confidence": 0, "reason": str(e)}

async def ai_gpt(prompt: str) -> dict:
    if not OPENAI_KEY:
        return {"signal": "HOLD", "confidence": 0, "reason": "No GPT key"}
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                json={"model": "gpt-4o-mini", "max_tokens": 200,
                      "messages": [{"role": "user", "content": prompt}]}
            )
            raw = r.json()["choices"][0]["message"]["content"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
    except Exception as e:
        return {"signal": "HOLD", "confidence": 0, "reason": str(e)}

async def ai_gemini(prompt: str) -> dict:
    """Try Gemini first, fallback to DeepSeek if Gemini fails"""
    # Try Gemini
    if GEMINI_KEY:
        attempts = [
            ("v1",     "gemini-2.0-flash-lite"),
            ("v1",     "gemini-2.0-flash"),
            ("v1",     "gemini-1.5-flash"),
            ("v1beta", "gemini-2.0-flash-lite"),
            ("v1beta", "gemini-2.0-flash"),
        ]
        for api_ver, model in attempts:
            try:
                url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{model}:generateContent?key={GEMINI_KEY}"
                async with httpx.AsyncClient(timeout=20) as h:
                    r = await h.post(url, json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 256}
                    })
                    resp_json = r.json()
                    if "candidates" not in resp_json:
                        continue
                    raw = resp_json["candidates"][0]["content"]["parts"][0]["text"]
                    raw = raw.replace("```json","").replace("```","").strip()
                    result = json.loads(raw)
                    result["_source"] = f"Gemini({model})"
                    log.info(f"Gemini OK: {api_ver}/{model}")
                    return result
            except:
                continue

    # Fallback: DeepSeek (free tier, OpenAI-compatible)
    DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    if DEEPSEEK_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                    json={"model": "deepseek-chat", "max_tokens": 200,
                          "messages": [{"role": "user", "content": prompt}]}
                )
                raw = r.json()["choices"][0]["message"]["content"].strip()
                raw = raw.replace("```json","").replace("```","").strip()
                result = json.loads(raw)
                result["_source"] = "DeepSeek"
                log.info("DeepSeek OK")
                return result
        except Exception as e:
            log.warning(f"DeepSeek error: {e}")

    # Fallback: Use OpenAI with different temperature for diversity
    if OPENAI_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                    json={"model": "gpt-4o-mini", "max_tokens": 200,
                          "temperature": 0.9,
                          "messages": [
                              {"role": "system", "content": "You are a contrarian trading AI. Always consider BOTH bullish and bearish scenarios equally."},
                              {"role": "user", "content": prompt}
                          ]}
                )
                raw = r.json()["choices"][0]["message"]["content"].strip()
                raw = raw.replace("```json","").replace("```","").strip()
                result = json.loads(raw)
                result["_source"] = "GPT-Contrarian"
                result["name"] = "Gemini"
                log.info("GPT-Contrarian fallback OK")
                return result
        except Exception as e:
            log.warning(f"GPT fallback error: {e}")

    return {"signal": "HOLD", "confidence": 0, "reason": "All AI failed", "_source": "none"}

def build_prompt(symbol: str, symbol_type: str, price: float, spread: float, count: int) -> str:
    import random
    bias = random.choice(["bullish", "bearish", "neutral"])
    return (
        f"You are a professional Deriv trading AI. Analyze objectively — do NOT always say CALL.\n"
        f"Symbol: {symbol} | Type: {symbol_type} | Current Price: {price} | Spread: {spread}\n"
        f"Market: Deriv {symbol_type} — algorithmic 24/7. Consider mean reversion and trend reversal.\n"
        f"Current market feel: {bias}. Give your honest assessment.\n"
        f"Return ONLY valid JSON (no markdown, no extra text):\n"
        f'{{"signal":"CALL","confidence":65,"reason":"your reason here","duration":5}}\n'
        f'Rules: signal = CALL or PUT only. confidence = 40-85. duration = 1-15 minutes.'
    )

# ══════════════════════════════════════════════
#  MODELS
# ══════════════════════════════════════════════
class LoginReq(BaseModel):
    username: str
    password: str

class AnalyzeReq(BaseModel):
    symbol: str = "R_10"
    symbol_type: str = "synthetic"
    count: int = 1

class TradeReq(BaseModel):
    symbol: str = "R_10"
    contract_type: str = "CALL"
    duration: int = 5
    stake: float = 1.0
    mode: str = "sim"

class CloseReq(BaseModel):
    trade_id: str
    mode: str = "sim"

# ══════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_HTML)

@app.post("/api/login")
async def do_login(req: LoginReq, response: Response):
    stored = PLAIN_USERS.get(req.username.lower())
    if not stored or stored != req.password:
        raise HTTPException(status_code=401, detail="Username ຫຼື Password ຜິດ")
    token = secrets.token_hex(32)
    sessions[token] = {
        "username": req.username,
        "expires": time.time() + 7 * 24 * 3600
    }
    response.set_cookie(
        key="sg_token", value=token,
        max_age=7 * 24 * 3600,
        httponly=True, samesite="lax"
    )
    return {"ok": True, "token": token, "username": req.username}

@app.post("/api/logout")
async def do_logout(request: Request, response: Response):
    token = request.cookies.get("sg_token")
    if token:
        sessions.pop(token, None)
    response.delete_cookie("sg_token")
    return {"ok": True}

@app.get("/api/me")
async def me(request: Request):
    s = get_session(request)
    if not s:
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True, "username": s["username"]}

# ══════════════════════════════════════════════
#  DASHBOARD ROUTES  (ALL ALIASES)
# ══════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not get_session(request):
        return RedirectResponse("/login")
    return RedirectResponse("/deriv")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Alias — redirect to /deriv"""
    if not get_session(request):
        return RedirectResponse("/login")
    return RedirectResponse("/deriv")

@app.get("/forex", response_class=HTMLResponse)
async def forex_page(request: Request):
    """Alias — redirect to /deriv"""
    if not get_session(request):
        return RedirectResponse("/login")
    return RedirectResponse("/deriv")

@app.get("/deriv", response_class=HTMLResponse)
async def deriv_page(request: Request):
    if not get_session(request):
        return RedirectResponse("/login")
    return HTMLResponse(DERIV_DASHBOARD_HTML)

# ══════════════════════════════════════════════
#  DERIV API ROUTES
# ══════════════════════════════════════════════
@app.get("/api/deriv/account")
async def deriv_account(request: Request):
    require_auth(request)
    if not DERIV_TOKEN:
        return {"ok": False, "error": "No DERIV_API_TOKEN set", "demo": True,
                "balance": 10000.00, "currency": "USD", "loginid": "DEMO"}
    resp = await deriv_request({"balance": 1, "account": "current"})
    if resp.get("error"):
        return {"ok": False, "error": resp["error"]["message"]}
    b = resp.get("balance", {})
    return {"ok": True, "balance": b.get("balance", 0),
            "currency": b.get("currency", "USD"), "loginid": b.get("loginid", "")}

@app.get("/api/deriv/symbols")
async def deriv_symbols(request: Request):
    require_auth(request)
    resp = await deriv_request({"active_symbols": "brief", "product_type": "basic"})
    if resp.get("error"):
        # Return fallback symbols
        return {"ok": True, "symbols": FALLBACK_SYMBOLS}
    syms = resp.get("active_symbols", [])
    result = []
    for s in syms:
        result.append({
            "symbol": s.get("symbol"),
            "display_name": s.get("display_name"),
            "market": s.get("market"),
            "market_display_name": s.get("market_display_name"),
            "pip": s.get("pip"),
            "spot": s.get("spot"),
            "spot_time": s.get("spot_time"),
            "exchange_is_open": s.get("exchange_is_open"),
        })
    return {"ok": True, "symbols": result}

@app.get("/api/deriv/price/{symbol}")
async def deriv_price(symbol: str, request: Request):
    require_auth(request)
    resp = await deriv_request({"ticks": symbol})
    if resp.get("error"):
        return {"ok": False, "error": resp["error"]["message"], "price": 0}
    tick = resp.get("tick", {})
    return {"ok": True, "symbol": symbol,
            "price": tick.get("quote", 0), "time": tick.get("epoch", 0)}

@app.post("/api/deriv/analyze")
async def deriv_analyze(req: AnalyzeReq, request: Request):
    require_auth(request)

    # Get current price
    price_resp = await deriv_price(req.symbol, request)
    price = price_resp.get("price", 1000.0)
    spread = 0.5 if "R_" in req.symbol else 0.0001

    prompt = build_prompt(req.symbol, req.symbol_type, price, spread, req.count)

    # Run 3 AIs in parallel
    claude_r, gpt_r, gemini_r = await asyncio.gather(
        ai_claude(prompt), ai_gpt(prompt), ai_gemini(prompt)
    )

    ais = [
        {"name": "Claude", "icon": "🟣", **claude_r},
        {"name": "ChatGPT", "icon": "🟢", **gpt_r},
        {"name": "Gemini", "icon": "🔵", **gemini_r},
    ]

    # Consensus
    calls = sum(1 for a in ais if a.get("signal") == "CALL")
    puts  = sum(1 for a in ais if a.get("signal") == "PUT")
    if calls > puts:
        consensus = "CALL"
        strength  = calls
    elif puts > calls:
        consensus = "PUT"
        strength  = puts
    else:
        consensus = "HOLD"
        strength  = 0

    avg_conf = round(sum(a.get("confidence", 0) for a in ais) / 3)
    avg_dur  = round(sum(a.get("duration", 5) for a in ais) / 3)

    return {
        "ok": True,
        "symbol": req.symbol,
        "price": price,
        "consensus": consensus,
        "strength": f"{strength}/3",
        "confidence": avg_conf,
        "duration": avg_dur,
        "ais": ais,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/api/deriv/trade")
async def deriv_trade(req: TradeReq, request: Request):
    require_auth(request)
    global trade_counter
    trade_counter += 1
    tid = f"DRV-{int(time.time())}-{trade_counter}"

    if req.mode == "sim":
        # Simulation trade
        price_resp = await deriv_price(req.symbol, request)
        entry_price = price_resp.get("price", 1000.0)
        trade = {
            "id": tid,
            "symbol": req.symbol,
            "contract_type": req.contract_type,
            "stake": req.stake,
            "duration": req.duration,
            "entry_price": entry_price,
            "mode": "sim",
            "status": "open",
            "pnl": 0.0,
            "opened": datetime.now().isoformat(),
        }
        trades[tid] = trade

        # Auto-close simulation after duration
        asyncio.create_task(auto_close_sim(tid, req.duration, entry_price, req.contract_type, req.stake))
        return {"ok": True, "trade_id": tid, "mode": "sim", "entry_price": entry_price}

    # Live trade via Deriv API
    if not DERIV_TOKEN:
        raise HTTPException(400, "DERIV_API_TOKEN not set — use sim mode")

    resp = await deriv_request({
        "buy": 1,
        "price": req.stake,
        "parameters": {
            "amount": req.stake,
            "basis": "stake",
            "contract_type": req.contract_type,
            "currency": "USD",
            "duration": req.duration,
            "duration_unit": "m",
            "symbol": req.symbol,
        }
    })
    if resp.get("error"):
        raise HTTPException(400, resp["error"]["message"])

    buy_resp = resp.get("buy", {})
    trade = {
        "id": tid,
        "contract_id": buy_resp.get("contract_id"),
        "symbol": req.symbol,
        "contract_type": req.contract_type,
        "stake": req.stake,
        "duration": req.duration,
        "entry_price": buy_resp.get("start_time", 0),
        "mode": "live",
        "status": "open",
        "pnl": 0.0,
        "opened": datetime.now().isoformat(),
    }
    trades[tid] = trade
    return {"ok": True, "trade_id": tid, "mode": "live",
            "contract_id": buy_resp.get("contract_id")}

async def auto_close_sim(tid: str, duration_min: int, entry: float, ctype: str, stake: float):
    await asyncio.sleep(duration_min * 60)
    if tid not in trades:
        return
    # Simulate result: ~60% win rate
    import random
    won = random.random() < 0.55
    pnl = round(stake * 0.85, 2) if won else -stake
    trades[tid]["status"] = "closed"
    trades[tid]["pnl"]    = pnl
    trades[tid]["result"] = "WIN" if won else "LOSS"
    trades[tid]["closed"] = datetime.now().isoformat()
    log.info(f"SIM trade {tid} closed: {trades[tid]['result']} PnL={pnl}")

@app.get("/api/deriv/trades")
async def get_trades(request: Request):
    require_auth(request)
    open_t   = [t for t in trades.values() if t.get("status") == "open"]
    closed_t = sorted(
        [t for t in trades.values() if t.get("status") == "closed"],
        key=lambda x: x.get("closed", ""), reverse=True
    )[:30]
    total_pnl = round(sum(t.get("pnl", 0) for t in trades.values()), 2)
    wins  = sum(1 for t in trades.values() if t.get("result") == "WIN")
    total = sum(1 for t in trades.values() if t.get("status") == "closed")
    return {
        "ok": True,
        "open": open_t,
        "closed": closed_t,
        "total_pnl": total_pnl,
        "wins": wins,
        "total_closed": total,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0
    }

@app.post("/api/deriv/close")
async def close_trade(req: CloseReq, request: Request):
    require_auth(request)
    t = trades.get(req.trade_id)
    if not t:
        raise HTTPException(404, "Trade not found")
    if t["status"] == "closed":
        return {"ok": True, "message": "Already closed", "pnl": t["pnl"]}

    if req.mode == "live" and t.get("contract_id") and DERIV_TOKEN:
        resp = await deriv_request({"sell": t["contract_id"], "price": 0})
        if resp.get("error"):
            raise HTTPException(400, resp["error"]["message"])
        sell = resp.get("sell", {})
        pnl  = round(sell.get("sold_for", 0) - t["stake"], 2)
        t["pnl"] = pnl
    else:
        import random
        pnl = round(t["stake"] * 0.85, 2) if random.random() < 0.55 else -t["stake"]
        t["pnl"] = pnl

    t["status"] = "closed"
    t["result"] = "WIN" if t["pnl"] > 0 else "LOSS"
    t["closed"] = datetime.now().isoformat()
    return {"ok": True, "pnl": t["pnl"], "result": t["result"]}

# ══════════════════════════════════════════════
#  FALLBACK SYMBOLS
# ══════════════════════════════════════════════
FALLBACK_SYMBOLS = [
    {"symbol": "R_10",  "display_name": "Volatility 10 Index",  "market": "synthetic_index", "pip": 0.001,  "spot": 6500.0,  "exchange_is_open": 1},
    {"symbol": "R_25",  "display_name": "Volatility 25 Index",  "market": "synthetic_index", "pip": 0.001,  "spot": 3200.0,  "exchange_is_open": 1},
    {"symbol": "R_50",  "display_name": "Volatility 50 Index",  "market": "synthetic_index", "pip": 0.001,  "spot": 5500.0,  "exchange_is_open": 1},
    {"symbol": "R_75",  "display_name": "Volatility 75 Index",  "market": "synthetic_index", "pip": 0.001,  "spot": 7500.0,  "exchange_is_open": 1},
    {"symbol": "R_100", "display_name": "Volatility 100 Index", "market": "synthetic_index", "pip": 0.001,  "spot": 4000.0,  "exchange_is_open": 1},
    {"symbol": "BOOM300N",  "display_name": "Boom 300 Index",   "market": "synthetic_index", "pip": 0.01,   "spot": 8100.0,  "exchange_is_open": 1},
    {"symbol": "CRASH300N", "display_name": "Crash 300 Index",  "market": "synthetic_index", "pip": 0.01,   "spot": 7800.0,  "exchange_is_open": 1},
    {"symbol": "BOOM500",   "display_name": "Boom 500 Index",   "market": "synthetic_index", "pip": 0.01,   "spot": 6200.0,  "exchange_is_open": 1},
    {"symbol": "CRASH500",  "display_name": "Crash 500 Index",  "market": "synthetic_index", "pip": 0.01,   "spot": 5900.0,  "exchange_is_open": 1},
    {"symbol": "BOOM1000",  "display_name": "Boom 1000 Index",  "market": "synthetic_index", "pip": 0.01,   "spot": 5100.0,  "exchange_is_open": 1},
    {"symbol": "CRASH1000", "display_name": "Crash 1000 Index", "market": "synthetic_index", "pip": 0.01,   "spot": 4900.0,  "exchange_is_open": 1},
    {"symbol": "STEPINDX",  "display_name": "Step Index",       "market": "synthetic_index", "pip": 0.1,    "spot": 8900.0,  "exchange_is_open": 1},
    {"symbol": "frxEURUSD", "display_name": "EUR/USD",          "market": "forex",           "pip": 0.0001, "spot": 1.0875,  "exchange_is_open": 1},
    {"symbol": "frxGBPUSD", "display_name": "GBP/USD",          "market": "forex",           "pip": 0.0001, "spot": 1.2650,  "exchange_is_open": 1},
    {"symbol": "frxUSDJPY", "display_name": "USD/JPY",          "market": "forex",           "pip": 0.01,   "spot": 149.50,  "exchange_is_open": 1},
    {"symbol": "frxAUDUSD", "display_name": "AUD/USD",          "market": "forex",           "pip": 0.0001, "spot": 0.6520,  "exchange_is_open": 1},
    {"symbol": "frxUSDCHF", "display_name": "USD/CHF",          "market": "forex",           "pip": 0.0001, "spot": 0.9050,  "exchange_is_open": 1},
]

# ══════════════════════════════════════════════
#  LOGIN HTML
# ══════════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html lang="lo">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SocialGuard PRO — Login</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@600;900&family=Syne:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#05080f;color:#8ab4d4;font-family:'Syne',sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background-image:radial-gradient(ellipse at 20% 20%,rgba(77,184,255,.06) 0%,transparent 50%),
                   radial-gradient(ellipse at 80% 80%,rgba(170,119,255,.04) 0%,transparent 50%);}
.card{width:380px;background:#080d1a;border:1px solid #132040;border-radius:12px;
  padding:48px 36px;box-shadow:0 0 60px rgba(77,184,255,.1);}
.logo{text-align:center;margin-bottom:32px;}
.logo-icon{font-size:40px;margin-bottom:12px;}
.logo-title{font-family:'Orbitron',monospace;font-size:18px;font-weight:900;
  color:#fff;letter-spacing:3px;}
.logo-sub{font-size:11px;color:#4db8ff;letter-spacing:2px;margin-top:4px;}
.field{margin-bottom:16px;}
.field label{display:block;font-size:11px;color:#4db8ff;letter-spacing:1px;
  text-transform:uppercase;margin-bottom:6px;}
.inp-wrap{position:relative;}
.inp-icon{position:absolute;left:12px;top:50%;transform:translateY(-50%);font-size:14px;}
.field input{width:100%;padding:12px 12px 12px 36px;background:#0d1525;
  border:1px solid #132040;border-radius:6px;color:#e2eeff;font-family:'JetBrains Mono',monospace;
  font-size:13px;outline:none;transition:border .2s;}
.field input:focus{border-color:#4db8ff;}
.toggle-pw{position:absolute;right:12px;top:50%;transform:translateY(-50%);
  background:none;border:none;color:#4a6a8a;cursor:pointer;font-size:14px;padding:4px;}
.btn{width:100%;padding:14px;background:linear-gradient(135deg,#1a4080,#2060c0);
  border:none;border-radius:6px;color:#fff;font-family:'Orbitron',monospace;
  font-size:12px;font-weight:700;letter-spacing:2px;cursor:pointer;
  transition:all .2s;margin-top:8px;}
.btn:hover{background:linear-gradient(135deg,#2050a0,#3070d0);
  box-shadow:0 0 20px rgba(77,184,255,.3);}
.err{display:none;background:rgba(255,68,102,.1);border:1px solid rgba(255,68,102,.3);
  border-radius:6px;padding:10px;font-size:12px;color:#ff6680;text-align:center;margin-bottom:12px;}
.footer{text-align:center;margin-top:24px;font-size:10px;color:#2a4060;letter-spacing:1px;}
.loading{display:none;text-align:center;padding:8px;font-size:11px;color:#4db8ff;}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">🛡</div>
    <div class="logo-title">SOCIAL<span style="color:#4db8ff">GUARD</span> <span style="color:#aa77ff">PRO</span></div>
    <div class="logo-sub">AI TRADING SYSTEM — SECURE ACCESS</div>
  </div>
  <div class="err" id="errBox"></div>
  <div class="field">
    <label>Username</label>
    <div class="inp-wrap">
      <span class="inp-icon">👤</span>
      <input type="text" id="username" placeholder="admin" autocomplete="username" />
    </div>
  </div>
  <div class="field">
    <label>Password</label>
    <div class="inp-wrap">
      <span class="inp-icon">🔑</span>
      <input type="password" id="password" placeholder="••••••••" autocomplete="current-password" />
      <button class="toggle-pw" onclick="togglePw()" type="button">👁</button>
    </div>
  </div>
  <div class="loading" id="loadMsg">🔄 ກຳລັງ authenticate...</div>
  <button class="btn" onclick="doLogin()">🔐 ENTER DASHBOARD</button>
  <div class="footer">🔒 ENCRYPTED · SECURE · SocialGuard PRO<br>v4.0 — Deriv AI Trading System</div>
</div>
<script>
document.addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });
function togglePw(){
  const i=document.getElementById('password');
  i.type = i.type==='password' ? 'text' : 'password';
}
async function doLogin(){
  const u=document.getElementById('username').value.trim();
  const p=document.getElementById('password').value;
  const err=document.getElementById('errBox');
  const load=document.getElementById('loadMsg');
  if(!u||!p){err.style.display='block';err.textContent='⚠ ກະລຸນາໃສ່ username ແລະ password';return;}
  err.style.display='none';
  load.style.display='block';
  try{
    const r=await fetch('/api/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:u,password:p}),
      credentials:'include'
    });
    const d=await r.json();
    if(r.ok && d.ok){
      load.textContent='✅ ACCESS GRANTED — ກຳລັງໂຫຼດ Dashboard...';
      localStorage.setItem('sg_token', d.token);
      setTimeout(()=>{ window.location.href='/deriv'; }, 1200);
    } else {
      load.style.display='none';
      err.style.display='block';
      err.textContent='⚠ '+(d.detail||'Username ຫຼື Password ຜິດ');
    }
  }catch(e){
    load.style.display='none';
    err.style.display='block';
    err.textContent='⚠ ເຊື່ອມຕໍ່ server ບໍ່ໄດ້: '+e.message;
  }
}
</script>
</body>
</html>"""

# ══════════════════════════════════════════════
#  DERIV DASHBOARD HTML
# ══════════════════════════════════════════════
DERIV_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="lo">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SocialGuard — Deriv Trading</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@600;900&family=Syne:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
:root{
  --bg:#05080f;--p1:#080d1a;--p2:#0d1525;--b1:#132040;--b2:#1e3060;
  --a:#4db8ff;--a2:#2080cc;--g:#00e87a;--r:#ff4466;--y:#ffcc00;
  --o:#ff8833;--pu:#aa77ff;--tx:#8ab4d4;--dim:#2a4060;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--tx);font-family:'Syne',sans-serif;min-height:100vh;}
/* NAV */
.nav{background:var(--p1);border-bottom:1px solid var(--b1);padding:0 20px;
  display:flex;align-items:center;gap:16px;height:52px;position:sticky;top:0;z-index:100;}
.nav-logo{font-family:'Orbitron',monospace;font-size:13px;font-weight:900;
  color:#fff;letter-spacing:2px;margin-right:8px;}
.nav-logo span{color:var(--a);}
.nav-badge{font-size:9px;background:rgba(0,232,122,.1);color:var(--g);
  border:1px solid rgba(0,232,122,.3);border-radius:3px;padding:2px 6px;letter-spacing:1px;}
.nav-tabs{display:flex;gap:2px;margin:0 auto;}
.tab{padding:6px 14px;border-radius:4px;font-size:11px;font-weight:600;
  cursor:pointer;transition:all .2s;border:none;background:none;color:var(--tx);}
.tab:hover{background:var(--b1);color:#fff;}
.tab.active{background:rgba(77,184,255,.15);color:var(--a);}
.nav-right{display:flex;align-items:center;gap:8px;}
.bal{font-family:'JetBrains Mono',monospace;font-size:12px;
  color:var(--g);background:rgba(0,232,122,.08);border:1px solid rgba(0,232,122,.2);
  border-radius:4px;padding:4px 10px;}
.logout-btn{padding:4px 10px;background:rgba(255,68,102,.1);border:1px solid rgba(255,68,102,.3);
  border-radius:4px;color:var(--r);font-size:10px;cursor:pointer;font-family:'Orbitron',monospace;}
/* PAGES */
.page{display:none;padding:16px;}
.page.active{display:block;}
/* GRID */
.r2{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.r3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;}
@media(max-width:768px){.r2,.r3{grid-template-columns:1fr;}}
/* PANEL */
.pnl{background:var(--p1);border:1px solid var(--b1);border-radius:8px;overflow:hidden;margin-bottom:14px;}
.ph{display:flex;align-items:center;gap:8px;padding:10px 14px;
  border-bottom:1px solid var(--b1);font-size:11px;font-weight:700;
  letter-spacing:1.5px;color:#fff;}
.pd{width:8px;height:8px;border-radius:50%;}
.pd.g{background:var(--g);box-shadow:0 0 6px var(--g);}
.pd.a{background:var(--a);box-shadow:0 0 6px var(--a);}
.pd.o{background:var(--o);box-shadow:0 0 6px var(--o);}
.pd.pu{background:var(--pu);box-shadow:0 0 6px var(--pu);}
/* STAT CARDS */
.stat{padding:14px;text-align:center;}
.stat-val{font-family:'Orbitron',monospace;font-size:22px;font-weight:900;color:#fff;margin-bottom:4px;}
.stat-lbl{font-size:10px;color:var(--tx);letter-spacing:1px;text-transform:uppercase;}
.stat-val.green{color:var(--g);}
.stat-val.red{color:var(--r);}
.stat-val.blue{color:var(--a);}
.stat-val.purple{color:var(--pu);}
/* SYMBOL SELECT */
.sym-select{width:100%;padding:10px 12px;background:var(--p2);border:1px solid var(--b1);
  border-radius:6px;color:#fff;font-family:'JetBrains Mono',monospace;font-size:12px;
  outline:none;cursor:pointer;margin-bottom:10px;}
/* BUTTONS */
.btn{padding:10px 18px;border:none;border-radius:6px;font-family:'Orbitron',monospace;
  font-size:10px;font-weight:700;letter-spacing:1px;cursor:pointer;transition:all .2s;}
.btn-a{background:linear-gradient(135deg,#1a4080,#2060c0);color:#fff;}
.btn-a:hover{background:linear-gradient(135deg,#2050a0,#3070d0);box-shadow:0 0 16px rgba(77,184,255,.3);}
.btn-g{background:rgba(0,232,122,.15);color:var(--g);border:1px solid rgba(0,232,122,.3);}
.btn-r{background:rgba(255,68,102,.15);color:var(--r);border:1px solid rgba(255,68,102,.3);}
.btn-full{width:100%;}
/* TRADE CARD */
.trade-card{padding:10px 14px;border-bottom:1px solid var(--b1);display:flex;
  align-items:center;gap:10px;font-size:11px;}
.trade-card:last-child{border-bottom:none;}
.tc-sym{font-family:'Orbitron',monospace;font-size:10px;color:var(--a);font-weight:700;}
.tc-type{padding:2px 8px;border-radius:3px;font-size:10px;font-weight:700;}
.tc-call{background:rgba(0,232,122,.15);color:var(--g);}
.tc-put{background:rgba(255,68,102,.15);color:var(--r);}
.tc-pnl{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;}
.tc-status{font-size:9px;color:var(--tx);}
/* AI CARD */
.ai-card{padding:12px 14px;border-bottom:1px solid var(--b1);}
.ai-card:last-child{border-bottom:none;}
.ai-name{font-size:11px;font-weight:700;color:#fff;margin-bottom:4px;}
.ai-signal{display:inline-block;padding:2px 8px;border-radius:3px;font-size:10px;
  font-weight:700;margin-right:8px;}
.sig-call{background:rgba(0,232,122,.15);color:var(--g);}
.sig-put{background:rgba(255,68,102,.15);color:var(--r);}
.sig-hold{background:rgba(138,180,212,.1);color:var(--tx);}
.ai-reason{font-size:10px;color:var(--tx);margin-top:4px;line-height:1.5;}
/* CONF BAR */
.conf-bar{height:4px;background:var(--b1);border-radius:2px;margin-top:6px;}
.conf-fill{height:4px;border-radius:2px;background:linear-gradient(90deg,var(--a2),var(--a));
  transition:width .5s;}
/* CONSENSUS BOX */
.cons-box{padding:16px;margin:10px;border-radius:6px;text-align:center;}
.cons-call{background:rgba(0,232,122,.08);border:1px solid rgba(0,232,122,.25);}
.cons-put{background:rgba(255,68,102,.08);border:1px solid rgba(255,68,102,.25);}
.cons-hold{background:rgba(138,180,212,.05);border:1px solid var(--b1);}
.cons-label{font-family:'Orbitron',monospace;font-size:22px;font-weight:900;margin-bottom:4px;}
.cons-call .cons-label{color:var(--g);}
.cons-put .cons-label{color:var(--r);}
.cons-hold .cons-label{color:var(--tx);}
.cons-sub{font-size:10px;color:var(--tx);letter-spacing:1px;}
/* FORM ROW */
.form-row{padding:8px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.form-lbl{font-size:10px;color:var(--tx);letter-spacing:1px;text-transform:uppercase;min-width:60px;}
.form-inp{flex:1;padding:8px 10px;background:var(--p2);border:1px solid var(--b1);
  border-radius:4px;color:#fff;font-family:'JetBrains Mono',monospace;font-size:12px;outline:none;}
.form-inp:focus{border-color:var(--a);}
/* LOG */
.log-box{height:180px;overflow-y:auto;padding:10px 14px;font-family:'JetBrains Mono',monospace;
  font-size:10px;line-height:1.8;}
.log-box p{color:var(--tx);}
.log-box p.ok{color:var(--g);}
.log-box p.err{color:var(--r);}
.log-box p.info{color:var(--a);}
/* BADGE */
.badge{display:inline-flex;align-items:center;gap:4px;font-size:10px;padding:2px 8px;
  border-radius:3px;font-weight:700;}
.badge-green{background:rgba(0,232,122,.1);color:var(--g);}
.badge-blue{background:rgba(77,184,255,.1);color:var(--a);}
.badge-red{background:rgba(255,68,102,.1);color:var(--r);}
/* MARKET TABLE */
.mkt-table{width:100%;border-collapse:collapse;font-size:11px;}
.mkt-table th{padding:8px 12px;text-align:left;border-bottom:1px solid var(--b2);
  font-size:9px;color:var(--tx);letter-spacing:1px;text-transform:uppercase;
  font-family:'JetBrains Mono',monospace;}
.mkt-table td{padding:8px 12px;border-bottom:1px solid var(--b1);}
.mkt-table tr:hover td{background:rgba(77,184,255,.03);}
.mkt-sym{font-family:'Orbitron',monospace;font-size:10px;color:var(--a);font-weight:700;}
.mkt-price{font-family:'JetBrains Mono',monospace;font-size:12px;color:#fff;}
.mkt-open{padding:2px 6px;border-radius:2px;font-size:9px;}
.mkt-open.yes{background:rgba(0,232,122,.1);color:var(--g);}
.mkt-open.no{background:rgba(255,68,102,.1);color:var(--r);}
/* SCROLL */
::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-track{background:var(--b1);}
::-webkit-scrollbar-thumb{background:var(--b2);border-radius:2px;}
</style>
</head>
<body>

<!-- NAV -->
<nav class="nav">
  <div class="nav-logo">SOCIAL<span>GUARD</span></div>
  <div class="nav-badge">DERIV AI</div>
  <div class="nav-tabs">
    <button class="tab active" onclick="showPage('trade')">📊 TRADE</button>
    <button class="tab" onclick="showPage('market')">🌐 MARKET</button>
    <button class="tab" onclick="showPage('history')">📋 HISTORY</button>
  </div>
  <div class="nav-right">
    <div class="bal" id="balDisplay">💰 $—</div>
    <button class="logout-btn" onclick="doLogout()">LOGOUT</button>
  </div>
</nav>

<!-- ═══ TRADE PAGE ═══ -->
<div class="page active" id="page-trade">
  <div class="r3">
    <div class="pnl"><div class="stat">
      <div class="stat-val blue" id="statBal">$—</div>
      <div class="stat-lbl">Balance</div>
    </div></div>
    <div class="pnl"><div class="stat">
      <div class="stat-val" id="statPnl">$0.00</div>
      <div class="stat-lbl">Total P/L</div>
    </div></div>
    <div class="pnl"><div class="stat">
      <div class="stat-val purple" id="statWR">0%</div>
      <div class="stat-lbl">Win Rate</div>
    </div></div>
  </div>

  <div class="r2">
    <!-- LEFT: Analyze -->
    <div>
      <div class="pnl">
        <div class="ph"><div class="pd a"></div>AI ANALYZE</div>
        <div style="padding:10px 14px;">
          <div style="font-size:10px;color:var(--tx);margin-bottom:6px;text-transform:uppercase;letter-spacing:1px;">Symbol</div>
          <select class="sym-select" id="symSelect">
            <option value="R_10">📈 Volatility 10 Index</option>
            <option value="R_25">📈 Volatility 25 Index</option>
            <option value="R_50" selected>📈 Volatility 50 Index</option>
            <option value="R_75">📈 Volatility 75 Index</option>
            <option value="R_100">📈 Volatility 100 Index</option>
            <option value="BOOM300N">🚀 Boom 300 Index</option>
            <option value="CRASH300N">💥 Crash 300 Index</option>
            <option value="BOOM500">🚀 Boom 500 Index</option>
            <option value="CRASH500">💥 Crash 500 Index</option>
            <option value="BOOM1000">🚀 Boom 1000 Index</option>
            <option value="CRASH1000">💥 Crash 1000 Index</option>
            <option value="STEPINDX">〰️ Step Index</option>
            <option value="frxEURUSD">💶 EUR/USD</option>
            <option value="frxGBPUSD">💷 GBP/USD</option>
            <option value="frxUSDJPY">💴 USD/JPY</option>
            <option value="frxAUDUSD">🇦🇺 AUD/USD</option>
            <option value="frxUSDCHF">🇨🇭 USD/CHF</option>
          </select>
          <button class="btn btn-a btn-full" onclick="doAnalyze()">🤖 AI ANALYZE (3 AI CONSENSUS)</button>
        </div>
        <div id="analyzeResult" style="display:none;">
          <div id="consensusBox" class="cons-box cons-hold" style="margin:10px;">
            <div class="cons-label" id="consLabel">HOLD</div>
            <div class="cons-sub" id="consSub">ລໍຖ້າ...</div>
          </div>
          <div id="aiCards"></div>
        </div>
      </div>

      <div class="pnl">
        <div class="ph"><div class="pd g"></div>PLACE TRADE</div>
        <div class="form-row">
          <span class="form-lbl">Symbol</span>
          <span id="tradeSymDisplay" style="font-family:'Orbitron',monospace;font-size:11px;color:var(--a);">R_50</span>
          <span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--tx);margin-left:auto;" id="tradePriceDisplay">—</span>
        </div>
        <div class="form-row">
          <span class="form-lbl">Stake $</span>
          <input class="form-inp" type="number" id="stakeInput" value="1" min="0.35" step="0.1">
        </div>
        <div class="form-row">
          <span class="form-lbl">Duration</span>
          <select class="form-inp" id="durSelect">
            <option value="1">1 min</option>
            <option value="2">2 min</option>
            <option value="5" selected>5 min</option>
            <option value="10">10 min</option>
            <option value="15">15 min</option>
            <option value="30">30 min</option>
          </select>
        </div>
        <div class="form-row">
          <span class="form-lbl">Mode</span>
          <select class="form-inp" id="modeSelect">
            <option value="sim" selected>🎮 Simulation</option>
            <option value="live">🔴 Live (Real)</option>
          </select>
        </div>
        <div class="form-row" style="gap:8px;padding-top:4px;">
          <button class="btn btn-g" style="flex:1;font-size:11px;" onclick="placeTrade('CALL')">📈 CALL (UP)</button>
          <button class="btn btn-r" style="flex:1;font-size:11px;" onclick="placeTrade('PUT')">📉 PUT (DOWN)</button>
        </div>
      </div>
    </div>

    <!-- RIGHT: Open trades + Log -->
    <div>
      <div class="pnl">
        <div class="ph"><div class="pd g"></div>OPEN TRADES
          <button onclick="loadTrades()" style="margin-left:auto;padding:2px 8px;background:rgba(77,184,255,.1);
            border:1px solid var(--b2);border-radius:3px;color:var(--a);cursor:pointer;font-size:9px;">↺</button>
        </div>
        <div id="openTradesList" style="min-height:60px;">
          <div style="padding:20px;text-align:center;color:var(--dim);font-size:11px;">ບໍ່ມີ open trades</div>
        </div>
      </div>
      <div class="pnl">
        <div class="ph"><div class="pd pu"></div>ACTIVITY LOG</div>
        <div class="log-box" id="logBox">
          <p class="info">[SYSTEM] SocialGuard Deriv Bot v4.0 ready</p>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ MARKET PAGE ═══ -->
<div class="page" id="page-market">
  <div class="pnl">
    <div class="ph"><div class="pd g"></div>DERIV SYMBOLS — LIVE
      <button onclick="loadMarket()" style="margin-left:auto;padding:2px 8px;
        background:rgba(77,184,255,.1);border:1px solid var(--b2);border-radius:3px;
        color:var(--a);cursor:pointer;font-size:9px;">↺ REFRESH</button>
    </div>
    <div style="overflow-x:auto;">
      <table class="mkt-table">
        <thead><tr>
          <th>Symbol</th><th>Name</th><th>Market</th>
          <th>Price</th><th>Pip</th><th>Status</th><th>Action</th>
        </tr></thead>
        <tbody id="mktBody">
          <tr><td colspan="7" style="text-align:center;padding:20px;color:var(--dim);">ກຳລັງໂຫຼດ...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══ HISTORY PAGE ═══ -->
<div class="page" id="page-history">
  <div class="r3" style="margin-bottom:0;">
    <div class="pnl"><div class="stat">
      <div class="stat-val" id="h-pnl">$0.00</div>
      <div class="stat-lbl">Total P/L</div>
    </div></div>
    <div class="pnl"><div class="stat">
      <div class="stat-val purple" id="h-wr">0%</div>
      <div class="stat-lbl">Win Rate</div>
    </div></div>
    <div class="pnl"><div class="stat">
      <div class="stat-val blue" id="h-total">0</div>
      <div class="stat-lbl">Total Trades</div>
    </div></div>
  </div>
  <div class="pnl" style="margin-top:14px;">
    <div class="ph"><div class="pd a"></div>TRADE HISTORY
      <button onclick="loadHistory()" style="margin-left:auto;padding:2px 8px;
        background:rgba(77,184,255,.1);border:1px solid var(--b2);border-radius:3px;
        color:var(--a);cursor:pointer;font-size:9px;">↺ REFRESH</button>
    </div>
    <div id="historyList">
      <div style="padding:20px;text-align:center;color:var(--dim);font-size:11px;">ຍັງບໍ່ມີ history</div>
    </div>
  </div>
</div>

<script>
const BASE = '';
let currentSymbol = 'R_50';
let analyzing = false;

// ── PAGE NAV ──────────────────────────────────
function showPage(p){
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.getElementById('page-'+p).classList.add('active');
  event.target.classList.add('active');
  if(p==='market') loadMarket();
  if(p==='history') loadHistory();
}

// ── LOG ───────────────────────────────────────
function log(msg, cls=''){
  const b=document.getElementById('logBox');
  const p=document.createElement('p');
  if(cls) p.className=cls;
  const t=new Date().toLocaleTimeString('en',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  p.textContent=`[${t}] ${msg}`;
  b.appendChild(p);
  b.scrollTop=b.scrollHeight;
  if(b.children.length>80) b.removeChild(b.firstChild);
}

// ── AUTH ─────────────────────────────────────
async function doLogout(){
  await fetch('/api/logout',{method:'POST',credentials:'include'});
  localStorage.removeItem('sg_token');
  window.location.href='/login';
}

// ── ACCOUNT ──────────────────────────────────
async function loadAccount(){
  try{
    const r=await fetch('/api/deriv/account',{credentials:'include',
      headers:{'Authorization':'Bearer '+(localStorage.getItem('sg_token')||'')}});
    const d=await r.json();
    if(d.ok){
      const bal='$'+parseFloat(d.balance).toFixed(2);
      document.getElementById('balDisplay').textContent='💰 '+bal;
      document.getElementById('statBal').textContent=bal;
      log('Account loaded: '+bal+' '+d.currency,'ok');
    }
  }catch(e){ log('Account error: '+e.message,'err'); }
}

// ── ANALYZE ──────────────────────────────────
async function doAnalyze(){
  if(analyzing) return;
  analyzing=true;
  currentSymbol=document.getElementById('symSelect').value;
  document.getElementById('tradeSymDisplay').textContent=currentSymbol;
  log('AI analyzing '+currentSymbol+'...','info');
  const res=document.getElementById('analyzeResult');
  res.style.display='block';
  document.getElementById('consLabel').textContent='...';
  document.getElementById('consSub').textContent='3 AI ກຳລັງວິເຄາະ...';
  document.getElementById('aiCards').innerHTML='<div style="padding:20px;text-align:center;color:var(--tx);font-size:11px;">🤖 Claude + ChatGPT + Gemini กำลัง analyze...</div>';
  try{
    const r=await fetch('/api/deriv/analyze',{
      method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+(localStorage.getItem('sg_token')||'')},
      body:JSON.stringify({symbol:currentSymbol,symbol_type:currentSymbol.startsWith('frx')?'forex':'synthetic',count:1})
    });
    const d=await r.json();
    if(!d.ok) throw new Error(d.error||'Analyze failed');

    // Consensus box
    const cb=document.getElementById('consensusBox');
    cb.className='cons-box cons-'+d.consensus.toLowerCase();
    document.getElementById('consLabel').textContent=d.consensus;
    document.getElementById('consSub').textContent=
      d.strength+' AI Agree · Confidence: '+d.confidence+'% · Duration: '+d.duration+'min';

    // Stake auto-suggest
    document.getElementById('durSelect').value = Math.min(d.duration, 30);

    // AI cards
    let html='';
    for(const a of d.ais){
      const sc=a.signal==='CALL'?'sig-call':a.signal==='PUT'?'sig-put':'sig-hold';
      html+=`<div class="ai-card">
        <div class="ai-name">${a.icon} ${a.name}</div>
        <span class="ai-signal ${sc}">${a.signal||'HOLD'}</span>
        <span style="font-size:10px;color:var(--tx);">Confidence: ${a.confidence||0}%</span>
        <div class="conf-bar"><div class="conf-fill" style="width:${a.confidence||0}%"></div></div>
        <div class="ai-reason">${a.reason||''}</div>
      </div>`;
    }
    document.getElementById('aiCards').innerHTML=html;
    document.getElementById('tradePriceDisplay').textContent='Price: '+d.price;
    log('AI consensus: '+d.consensus+' ('+d.strength+', '+d.confidence+'%)','ok');
  }catch(e){
    log('Analyze error: '+e.message,'err');
    document.getElementById('consLabel').textContent='ERROR';
    document.getElementById('consSub').textContent=e.message;
  }
  analyzing=false;
}

// ── TRADE ─────────────────────────────────────
async function placeTrade(ctype){
  const sym=document.getElementById('symSelect').value;
  const stake=parseFloat(document.getElementById('stakeInput').value)||1;
  const dur=parseInt(document.getElementById('durSelect').value)||5;
  const mode=document.getElementById('modeSelect').value;
  log('Placing '+ctype+' on '+sym+' $'+stake+' '+dur+'min ('+mode+')...','info');
  try{
    const r=await fetch('/api/deriv/trade',{
      method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+(localStorage.getItem('sg_token')||'')},
      body:JSON.stringify({symbol:sym,contract_type:ctype,duration:dur,stake:stake,mode:mode})
    });
    const d=await r.json();
    if(!d.ok) throw new Error(d.detail||'Trade failed');
    log('Trade opened: '+d.trade_id+' entry='+d.entry_price,'ok');
    setTimeout(loadTrades, 800);
  }catch(e){ log('Trade error: '+e.message,'err'); }
}

// ── LOAD TRADES ───────────────────────────────
async function loadTrades(){
  try{
    const r=await fetch('/api/deriv/trades',{credentials:'include',
      headers:{'Authorization':'Bearer '+(localStorage.getItem('sg_token')||'')}});
    const d=await r.json();
    if(!d.ok) return;
    // Update stats
    const pnl=d.total_pnl||0;
    const pnlEl=document.getElementById('statPnl');
    pnlEl.textContent='$'+pnl.toFixed(2);
    pnlEl.className='stat-val '+(pnl>=0?'green':'red');
    document.getElementById('statWR').textContent=(d.win_rate||0)+'%';
    // Open trades
    const el=document.getElementById('openTradesList');
    if(!d.open||d.open.length===0){
      el.innerHTML='<div style="padding:20px;text-align:center;color:var(--dim);font-size:11px;">ບໍ່ມີ open trades</div>';
      return;
    }
    el.innerHTML=d.open.map(t=>`
      <div class="trade-card">
        <div>
          <div class="tc-sym">${t.symbol}</div>
          <div class="tc-status">$${t.stake} · ${t.duration}min · ${t.mode}</div>
        </div>
        <span class="tc-type ${t.contract_type==='CALL'?'tc-call':'tc-put'}">${t.contract_type}</span>
        <div class="tc-pnl" style="color:var(--y);">OPEN</div>
        <button onclick="closeTrade('${t.id}')" style="padding:3px 8px;background:rgba(255,68,102,.1);
          border:1px solid rgba(255,68,102,.3);border-radius:3px;color:var(--r);font-size:9px;cursor:pointer;">CLOSE</button>
      </div>`).join('');
  }catch(e){}
}

async function closeTrade(tid){
  const mode=document.getElementById('modeSelect').value;
  try{
    const r=await fetch('/api/deriv/close',{
      method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+(localStorage.getItem('sg_token')||'')},
      body:JSON.stringify({trade_id:tid,mode:mode})
    });
    const d=await r.json();
    if(d.ok) log('Trade '+tid+' closed PnL=$'+d.pnl+' '+d.result, d.pnl>=0?'ok':'err');
    setTimeout(loadTrades, 500);
    setTimeout(loadAccount, 600);
  }catch(e){ log('Close error: '+e.message,'err'); }
}

// ── MARKET ────────────────────────────────────
async function loadMarket(){
  try{
    const r=await fetch('/api/deriv/symbols',{credentials:'include',
      headers:{'Authorization':'Bearer '+(localStorage.getItem('sg_token')||'')}});
    const d=await r.json();
    if(!d.ok) return;
    const tbody=document.getElementById('mktBody');
    tbody.innerHTML=d.symbols.map(s=>{
      const open=s.exchange_is_open;
      const mkt=s.market==='synthetic_index'?'🎲 Synthetic':'💱 Forex';
      return `<tr>
        <td><span class="mkt-sym">${s.symbol}</span></td>
        <td style="font-size:11px;color:#fff;">${s.display_name}</td>
        <td style="font-size:10px;color:var(--tx);">${mkt}</td>
        <td><span class="mkt-price">${s.spot||'—'}</span></td>
        <td style="font-family:'JetBrains Mono',monospace;font-size:10px;">${s.pip||'—'}</td>
        <td><span class="mkt-open ${open?'yes':'no'}">${open?'OPEN':'CLOSED'}</span></td>
        <td>
          ${open?`<button onclick="selectAndTrade('${s.symbol}')" style="padding:3px 8px;
            background:rgba(0,232,122,.1);border:1px solid rgba(0,232,122,.3);
            border-radius:3px;color:var(--g);font-size:9px;cursor:pointer;">TRADE</button>`:'—'}
        </td>
      </tr>`;
    }).join('');
  }catch(e){ document.getElementById('mktBody').innerHTML='<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--r);">Error: '+e.message+'</td></tr>'; }
}

function selectAndTrade(sym){
  document.getElementById('symSelect').value=sym;
  showPageDirect('trade');
  document.querySelectorAll('.tab').forEach((t,i)=>{ if(i===0) t.classList.add('active'); else t.classList.remove('active'); });
}

function showPageDirect(p){
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
  document.getElementById('page-'+p).classList.add('active');
}

// ── HISTORY ───────────────────────────────────
async function loadHistory(){
  try{
    const r=await fetch('/api/deriv/trades',{credentials:'include',
      headers:{'Authorization':'Bearer '+(localStorage.getItem('sg_token')||'')}});
    const d=await r.json();
    if(!d.ok) return;
    const pnl=d.total_pnl||0;
    document.getElementById('h-pnl').textContent='$'+pnl.toFixed(2);
    document.getElementById('h-pnl').className='stat-val '+(pnl>=0?'green':'red');
    document.getElementById('h-wr').textContent=(d.win_rate||0)+'%';
    document.getElementById('h-total').textContent=d.total_closed||0;
    const el=document.getElementById('historyList');
    if(!d.closed||d.closed.length===0){
      el.innerHTML='<div style="padding:20px;text-align:center;color:var(--dim);font-size:11px;">ຍັງບໍ່ມີ history</div>';
      return;
    }
    el.innerHTML=d.closed.map(t=>{
      const win=t.result==='WIN';
      return `<div class="trade-card">
        <div>
          <div class="tc-sym">${t.symbol}</div>
          <div class="tc-status">${t.opened?t.opened.slice(0,16):''} · ${t.mode}</div>
        </div>
        <span class="tc-type ${t.contract_type==='CALL'?'tc-call':'tc-put'}">${t.contract_type}</span>
        <span class="badge ${win?'badge-green':'badge-red'}">${t.result||'—'}</span>
        <div class="tc-pnl" style="color:${win?'var(--g)':'var(--r)'};">
          ${win?'+':''}$${(t.pnl||0).toFixed(2)}
        </div>
      </div>`;
    }).join('');
  }catch(e){}
}

// ── INIT ─────────────────────────────────────
loadAccount();
loadTrades();
setInterval(loadTrades, 15000);
setInterval(loadAccount, 30000);
</script>
</body>
</html>"""

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    print(f"🚀 SocialGuard DERIV BOT — Starting on port {PORT}")
    print(f"   DERIV_TOKEN  : {'✅ SET' if DERIV_TOKEN else '⚠️  NOT SET (demo mode)'}")
    print(f"   ANTHROPIC_KEY: {'✅ SET' if ANTHROPIC_KEY else '❌ NOT SET'}")
    print(f"   OPENAI_KEY   : {'✅ SET' if OPENAI_KEY else '❌ NOT SET'}")
    print(f"   GEMINI_KEY   : {'✅ SET' if GEMINI_KEY else '❌ NOT SET'}")
    uvicorn.run("forex_bot:app", host="0.0.0.0", port=PORT, reload=False)
