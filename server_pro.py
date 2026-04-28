#!/usr/bin/env python3
"""SocialGuard PRO v3 — tradego.sbs — Stable Clean Build"""

import json, os, math, time, hashlib, secrets, asyncio, hmac
from urllib.parse import urlencode
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, Dict, List
import anthropic, httpx
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="SocialGuard PRO")
from deriv_routes import deriv_router
app.include_router(deriv_router)

# ── Deriv Module ──────────────────────────────────────────────────────────────
try:
    from deriv_routes import deriv_router
    app.include_router(deriv_router)
    print("[Startup] Deriv module loaded ✅")
except ImportError as _e:
    print(f"[Startup] Deriv module not found — skipping ({_e})")
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(CORSMiddleware,
    allow_origins=["https://tradego.sbs","https://www.tradego.sbs","http://localhost:8000"],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY","")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY","")
GEMINI_KEY     = os.getenv("GEMINI_API_KEY","")
BINANCE_KEY    = os.getenv("BINANCE_API_KEY","")
BINANCE_SECRET = os.getenv("BINANCE_SECRET","")
PORT           = int(os.getenv("PORT",8000))
ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

PLAIN_USERS = {
    "admin":       os.getenv("ADMIN_PASSWORD","SocialGuard2024!"),
    "socialguard": os.getenv("USER_PASSWORD","Trading@Pro2024"),
}
sessions: Dict[str,dict] = {}

def get_session(r: Request):
    t = r.cookies.get("sg_token")
    if not t or t not in sessions: return None
    s = sessions[t]
    if s["expires"] < time.time(): del sessions[t]; return None
    return s["username"]

def require_auth(r: Request):
    u = get_session(r)
    if not u: raise HTTPException(401,"Unauthorized")
    return u

DATA_FILE = "/tmp/sg_data.json"
bots: Dict[str,dict] = {}
rebalance_log: list  = []
market_cache: dict   = {"coins":[],"updated":0}
signals_cache: dict  = {}

def save_data():
    try:
        clean = {k:v for k,v in bots.items() if isinstance(v,dict)}
        with open(DATA_FILE,"w") as f:
            json.dump({"bots":clean,"rl":rebalance_log[:50]},f)
    except Exception as e: print(f"[Save] {e}")

def load_data():
    global bots, rebalance_log
    try:
        if os.path.exists(DATA_FILE):
            d = json.load(open(DATA_FILE))
            bots = {k:v for k,v in d.get("bots",{}).items() if isinstance(v,dict)}
            rl   = d.get("rl",[])
            rebalance_log = rl if isinstance(rl,list) else []
            print(f"[Load] {len(bots)} bots")
    except Exception as e:
        print(f"[Load] {e}"); bots={}; rebalance_log=[]

load_data()

# ══════════════════════════════════════════════
#  P/L TRACKER — Background job every 5 min
# ══════════════════════════════════════════════
async def update_pnl():
    """Update P/L for all running bots every 5 minutes"""
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes
            for sym, bot in list(bots.items()):
                if not isinstance(bot, dict): continue
                if bot.get("status") != "running": continue

                # Get current price
                binance_sym = sym.replace("/", "")
                try:
                    async with httpx.AsyncClient(timeout=5) as h:
                        r = await h.get("https://api.binance.com/api/v3/ticker/price",
                                        params={"symbol": binance_sym})
                        current_price = float(r.json()["price"])
                except:
                    continue

                capital = bot.get("capital", 50)
                entry   = bot.get("entry_price", 0)
                mode    = bot.get("mode", "sim")

                if mode == "live":
                    # Live: check actual Binance balance
                    try:
                        bal = await get_balance()
                        asset = binance_sym.replace("USDT", "")
                        asset_qty = bal.get("balances", {}).get(asset, 0)
                        usdt_bal  = bal.get("usdt", 0)
                        if entry > 0 and asset_qty > 0:
                            current_value = asset_qty * current_price + usdt_bal
                            bot["pnl"] = round(current_value - capital, 4)
                        bot["currentPrice"] = current_price
                        bot["lastUpdate"]   = datetime.now().isoformat()
                    except Exception as e:
                        print(f"[PnL Live] {sym}: {e}")

                else:
                    # Simulation: estimate P/L based on price movement
                    if entry <= 0:
                        bot["entry_price"] = current_price
                        entry = current_price

                    price_change_pct = (current_price - entry) / entry * 100
                    grid_cfg = bot.get("grid", {})
                    grid_n   = grid_cfg.get("lines", 12)
                    tp_pct   = grid_cfg.get("tp", 0.6)
                    range_d  = grid_cfg.get("rangeDown", 6)
                    range_u  = grid_cfg.get("rangeUp", 6)

                    # Estimate: grid captures ~0.4-0.8% per completed cycle
                    # Time-based estimate: trades_per_day * tp_pct * capital
                    started = bot.get("started", datetime.now().isoformat())
                    try:
                        start_dt   = datetime.fromisoformat(started)
                        hours_run  = (datetime.now() - start_dt).total_seconds() / 3600
                        days_run   = hours_run / 24

                        # Estimate grid performance
                        volatility = abs(price_change_pct)
                        if volatility > 0 and range_d > 0:
                            # How many grid levels hit
                            levels_hit = min(grid_n, int(volatility / (range_d / grid_n)))
                            # Each level earns tp_pct of capital/grid_n
                            estimated_pnl = levels_hit * (capital / grid_n) * (tp_pct / 100)
                            # Factor in if price went down (DCA kicks in)
                            if price_change_pct < -2:
                                estimated_pnl *= 0.7  # DCA offsetting loss
                        else:
                            estimated_pnl = days_run * capital * 0.004  # ~0.4%/day estimate

                        bot["pnl"]          = round(estimated_pnl, 4)
                        bot["currentPrice"] = current_price
                        bot["priceChange"]  = round(price_change_pct, 2)
                        bot["hoursRun"]     = round(hours_run, 1)
                        bot["lastUpdate"]   = datetime.now().isoformat()
                    except Exception as e:
                        print(f"[PnL Sim] {sym}: {e}")

            save_data()
            print(f"[PnL] Updated {len([b for b in bots.values() if isinstance(b,dict) and b.get('status')=='running'])} bots")

        except Exception as e:
            print(f"[PnL Tracker] Error: {e}")
            await asyncio.sleep(60)

@app.on_event("startup")
async def startup():
    asyncio.create_task(update_pnl())
    print("[Startup] P/L Tracker started")

SYMBOLS=["DGBUSDT","BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","ADAUSDT",
         "DOGEUSDT","LTCUSDT","TRXUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT",
         "ATOMUSDT","XLMUSDT","UNIUSDT","CAKEUSDT","NEARUSDT","FTMUSDT"]

class LoginReq(BaseModel): username:str; password:str
class AnalyzeReq(BaseModel):
    capital:float=200; coinCount:int=4; risk:str="low"
    timeframe:str="medium"; strategy:str="combo"; note:Optional[str]=""
class BotStartReq(BaseModel):
    symbol:str; strategy:str="combo"; capitalUSDT:float=50; gridLines:int=12
    rangeDown:float=6.0; rangeUp:float=6.0; tpPct:float=0.6; slPct:float=9.0
    dcaBuyPct:float=2.0; dcaMulti:float=1.5; dcaLevels:int=3; mode:str="sim"
class RebalanceReq(BaseModel): capital:float=200; risk:str="low"; coinCount:int=4
class BacktestReq(BaseModel):
    symbol:str="DGBUSDT"; strategy:str="grid"; capitalUSDT:float=200.0
    gridLines:int=12; rangeDown:float=6.0; rangeUp:float=6.0; tpPct:float=0.6
    slPct:float=9.0; dcaDropPct:float=2.0; dcaMulti:float=1.5; dcaLevels:int=3
    interval:str="1h"; days:int=30

def sma(p,n): return sum(p[-n:])/n if len(p)>=n else (p[-1] if p else 0)
def ema(p,n):
    if len(p)<n: return p[-1] if p else 0
    k=2/(n+1); e=sum(p[:n])/n
    for x in p[n:]: e=x*k+e*(1-k)
    return e
def rsi(p,n=14):
    if len(p)<n+1: return 50.0
    g=[max(p[i]-p[i-1],0) for i in range(1,len(p))]
    l=[max(p[i-1]-p[i],0) for i in range(1,len(p))]
    ag=sum(g[-n:])/n; al=sum(l[-n:])/n
    return round(100-100/(1+(ag/al if al else 9999)),2)
def macd_calc(p):
    if len(p)<26: return {"cross":"none","hist":0,"macd":0,"signal":0}
    m=ema(p,12)-ema(p,26); s=m*0.9; h=m-s
    return {"macd":round(m,8),"signal":round(s,8),"hist":round(h,8),"cross":"bullish" if h>0 else "bearish"}
def bollinger(p,n=20):
    if len(p)<n: return {"pct":0.5,"width":4,"upper":p[-1]*1.02,"mid":p[-1],"lower":p[-1]*0.98}
    s=p[-n:]; mid=sum(s)/n; std=math.sqrt(sum((x-mid)**2 for x in s)/n)
    up=mid+2*std; lo=mid-2*std
    pct=(p[-1]-lo)/(up-lo) if (up-lo)>0 else 0.5
    return {"upper":round(up,8),"mid":round(mid,8),"lower":round(lo,8),"pct":round(pct,4),"width":round((up-lo)/mid*100,2)}
def stoch(hi,lo,cl,k=14):
    if len(cl)<k: return {"k":50,"d":50}
    hh=max(hi[-k:]); ll=min(lo[-k:])
    if hh==ll: return {"k":50,"d":50}
    kv=(cl[-1]-ll)/(hh-ll)*100
    return {"k":round(kv,2),"d":round(kv*0.9,2)}
def atr_calc(hi,lo,cl,n=14):
    if len(cl)<2: return 0
    trs=[max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])) for i in range(1,len(cl))]
    return sum(trs[-n:])/min(n,len(trs))
def score_coin(c):
    sc=50; sigs=[]; warns=[]
    r=c.get("rsi",50); m=c.get("macd",{}); b=c.get("bb",{})
    st=c.get("stoch",{"k":50}); vol=c.get("volatility",5)
    sw=c.get("sidewaysScore",50); trend=c.get("trend","sideways"); volM=c.get("vol24hM",0)
    if r<30:   sc+=15; sigs.append(f"RSI {r:.0f} Oversold 🟢")
    elif r<40: sc+=8;  sigs.append(f"RSI {r:.0f} Near oversold 🟡")
    elif r>70: sc-=15; warns.append(f"RSI {r:.0f} Overbought 🔴")
    elif r>60: sc-=5
    else:      sc+=3;  sigs.append(f"RSI {r:.0f} Neutral ✅")
    if m.get("cross")=="bullish": sc+=10; sigs.append("MACD Bullish 🟢")
    else: sc-=5
    bp=b.get("pct",0.5)
    if bp<0.2: sc+=12; sigs.append("BB Lower 🟢")
    elif bp>0.8: sc-=10
    if b.get("width",4)<2: sc+=5; sigs.append("BB Squeeze 🟡")
    sk=st.get("k",50)
    if sk<25: sc+=8; sigs.append(f"Stoch {sk:.0f} Oversold 🟢")
    elif sk>75: sc-=8
    if trend=="sideways": sc+=10; sigs.append("Sideways ✅")
    elif trend=="down": sc-=20
    if 5<=vol<=9: sc+=10; sigs.append(f"Vol {vol:.1f}% Ideal ✅")
    elif vol<3: sc-=5
    elif vol>12: sc-=8
    if volM>500: sc+=8
    elif volM<50: sc-=5
    sc+=int((sw-50)/5)
    final=max(5,min(98,sc))
    grade="A" if final>=80 else "B" if final>=65 else "C" if final>=50 else "D"
    return {"aiScore":final,"grade":grade,"signals":sigs[:5],"warnings":warns[:3]}

def binance_sign(params):
    params["timestamp"]=int(time.time()*1000)
    q=urlencode(params)
    params["signature"]=hmac.new(BINANCE_SECRET.encode(),q.encode(),hashlib.sha256).hexdigest()
    return params

async def bget(path,params={}):
    p=binance_sign(dict(params))
    async with httpx.AsyncClient(timeout=10) as h:
        r=await h.get(f"https://api.binance.com{path}",params=p,headers={"X-MBX-APIKEY":BINANCE_KEY})
        return r.json()

async def bpost(path,params={}):
    p=binance_sign(dict(params))
    async with httpx.AsyncClient(timeout=10) as h:
        r=await h.post(f"https://api.binance.com{path}",data=p,headers={"X-MBX-APIKEY":BINANCE_KEY})
        return r.json()

async def bdel(path,params={}):
    p=binance_sign(dict(params))
    async with httpx.AsyncClient(timeout=10) as h:
        r=await h.delete(f"https://api.binance.com{path}",params=p,headers={"X-MBX-APIKEY":BINANCE_KEY})
        return r.json()

async def get_balance():
    try:
        d=await bget("/api/v3/account")
        bal={b["asset"]:float(b["free"]) for b in d.get("balances",[]) if float(b["free"])>0}
        return {"ok":True,"balances":bal,"usdt":bal.get("USDT",0)}
    except Exception as e: return {"ok":False,"error":str(e),"balances":{},"usdt":0}

async def get_price(sym):
    try:
        async with httpx.AsyncClient(timeout=5) as h:
            r=await h.get("https://api.binance.com/api/v3/ticker/price",params={"symbol":sym})
            return float(r.json()["price"])
    except: return 0.0

def round_step(qty,step):
    if step<=0: return qty
    prec=len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return round(round(qty/step)*step,prec)

async def run_live_grid(sym_key):
    bot=bots.get(sym_key)
    if not bot or bot.get("mode")!="live": return
    sym=sym_key.replace("/","")
    g=bot.get("grid",{})
    try:
        price=await get_price(sym)
        if price<=0: raise Exception("Cannot get price")
        bot["entry_price"]=price
        lo=price*(1-g.get("rangeDown",6)/100); hi=price*(1+g.get("rangeUp",6)/100)
        n=g.get("lines",12); step=(hi-lo)/n; qpp=(bot["capital"]/n)/price
        orders=[]
        for i in range(n):
            lvl=lo+step*i
            if lvl>=price: continue
            qty=round(qpp,6)
            if qty*lvl<10: continue
            r=await bpost("/api/v3/order",{"symbol":sym,"side":"BUY","type":"LIMIT",
                                            "quantity":qty,"price":round(lvl,8),"timeInForce":"GTC"})
            if "orderId" in r: orders.append({"orderId":r["orderId"],"price":lvl,"qty":qty})
            await asyncio.sleep(0.2)
            if len(orders)>=6: break
        bot["live_orders"]=orders; bot["live_running"]=True; save_data()
        print(f"[Live] {sym} {len(orders)} orders placed")
    except Exception as e:
        bot["status"]="error"; bot["error"]=str(e); save_data()

async def cancel_live(sym_slash):
    sym=sym_slash.replace("/","")
    try:
        orders=await bget("/api/v3/openOrders",{"symbol":sym})
        for o in orders:
            await bdel("/api/v3/order",{"symbol":sym,"orderId":o["orderId"]})
            await asyncio.sleep(0.1)
    except Exception as e: print(f"[Cancel] {e}")

def get_static():
    import random
    base=[
        {"symbol":"DGB/USDT","price":0.0089,"change24h":-0.5,"vol24hM":45,"volatility":8.2,"sidewaysScore":78,"trend":"sideways"},
        {"symbol":"XRP/USDT","price":0.52,"change24h":-0.8,"vol24hM":980,"volatility":7.1,"sidewaysScore":72,"trend":"sideways"},
        {"symbol":"TRX/USDT","price":0.11,"change24h":-0.2,"vol24hM":380,"volatility":4.9,"sidewaysScore":74,"trend":"sideways"},
        {"symbol":"ADA/USDT","price":0.45,"change24h":-0.5,"vol24hM":420,"volatility":6.8,"sidewaysScore":70,"trend":"sideways"},
        {"symbol":"BNB/USDT","price":580,"change24h":-0.3,"vol24hM":1200,"volatility":5.8,"sidewaysScore":65,"trend":"sideways"},
        {"symbol":"BTC/USDT","price":67000,"change24h":1.2,"vol24hM":9200,"volatility":3.1,"sidewaysScore":45,"trend":"up"},
        {"symbol":"ETH/USDT","price":3500,"change24h":0.9,"vol24hM":5100,"volatility":4.2,"sidewaysScore":52,"trend":"up"},
        {"symbol":"SOL/USDT","price":170,"change24h":2.1,"vol24hM":2100,"volatility":9.5,"sidewaysScore":58,"trend":"up"},
        {"symbol":"ATOM/USDT","price":8.5,"change24h":-0.3,"vol24hM":180,"volatility":6.1,"sidewaysScore":73,"trend":"sideways"},
        {"symbol":"XLM/USDT","price":0.11,"change24h":-0.6,"vol24hM":120,"volatility":5.2,"sidewaysScore":76,"trend":"sideways"},
        {"symbol":"DOT/USDT","price":7.2,"change24h":-0.4,"vol24hM":210,"volatility":6.5,"sidewaysScore":71,"trend":"sideways"},
        {"symbol":"LTC/USDT","price":82,"change24h":-0.7,"vol24hM":310,"volatility":5.5,"sidewaysScore":68,"trend":"sideways"},
    ]
    for c in base:
        rv=random.uniform(35,65)
        c.update({"rsi":round(rv,2),"macd":{"cross":"bullish" if random.random()>0.5 else "bearish","hist":0,"macd":0,"signal":0},
                  "bb":{"pct":round(random.uniform(0.2,0.8),4),"width":4,"upper":c["price"]*1.05,"mid":c["price"],"lower":c["price"]*0.95},
                  "stoch":{"k":round(random.uniform(25,75),2),"d":50},"atr":0,"atrPct":round(c["volatility"]*0.4,3),
                  "sma20":c["price"],"sma50":c["price"]*0.99,"maTrend":"sideways","volTrend":"unknown","dcaSignal":rv<40,
                  "binanceSym":c["symbol"].replace("/USDT","USDT")})
    return base

async def fetch_market(force=False):
    global market_cache
    now=time.time()
    if not force and market_cache["coins"] and (now-market_cache["updated"])<30:
        return market_cache["coins"]
    async with httpx.AsyncClient(timeout=15) as h:
        try:
            r=await h.get("https://api.binance.com/api/v3/ticker/24hr")
            tickers={t["symbol"]:t for t in r.json()}; coins=[]
            for sym in SYMBOLS:
                t=tickers.get(sym)
                if not t: continue
                price=float(t["lastPrice"]); chg=float(t["priceChangePercent"])
                volM=float(t["quoteVolume"])/1e6; hi24=float(t["highPrice"]); lo24=float(t["lowPrice"])
                vlt=round((hi24-lo24)/lo24*100,2) if lo24>0 else 0
                try:
                    kr=await h.get("https://api.binance.com/api/v3/klines",
                                   params={"symbol":sym,"interval":"1h","limit":60})
                    kl=kr.json()
                    closes=[float(k[4]) for k in kl]; highs=[float(k[2]) for k in kl]
                    lows=[float(k[3]) for k in kl]
                except: closes=[price]; highs=[hi24]; lows=[lo24]
                r14=rsi(closes); mc=macd_calc(closes); bb=bollinger(closes)
                stv=stoch(highs,lows,closes); at=atr_calc(highs,lows,closes)
                atrp=round(at/price*100,3) if price>0 else 0
                s20=sma(closes,20); s50=sma(closes,50)
                sw=max(0,min(100,int(88-abs(chg)*3.5+vlt*0.4)))
                trend="up" if chg>3 else "down" if chg<-3 else "sideways"
                coins.append({"symbol":sym.replace("USDT","/USDT"),"binanceSym":sym,"price":price,
                              "change24h":round(chg,2),"vol24hM":round(volM,1),"volatility":vlt,
                              "sidewaysScore":sw,"trend":trend,"maTrend":"up" if s20>s50 else "down",
                              "rsi":r14,"macd":mc,"bb":bb,"stoch":stv,"atr":round(at,8),"atrPct":atrp,
                              "sma20":round(s20,8),"sma50":round(s50,8),"dcaSignal":r14<40})
            market_cache={"coins":coins,"updated":time.time()}
            return coins
        except Exception as e:
            print(f"[Market] {e}")
            return market_cache.get("coins",[]) or get_static()

async def ask_gpt(mt,rd):
    if not OPENAI_KEY: return {"picks":[],"source":"chatgpt"}
    try:
        p=(f"Crypto expert. strategy={rd['strategy']} risk={rd['risk']} capital={rd['capital']} count={rd['coinCount']}\n"
           f"DATA:\n{mt}\nReply ONLY JSON: {{\"marketMood\":\"Mixed\",\"picks\":[{{\"symbol\":\"XRP/USDT\","
           f"\"signal\":\"BUY\",\"confidence\":85,\"reason\":\"reason\",\"score\":82}}]}}")
        async with httpx.AsyncClient(timeout=30) as h:
            r=await h.post("https://api.openai.com/v1/chat/completions",
                           headers={"Authorization":f"Bearer {OPENAI_KEY}"},
                           json={"model":"gpt-4o-mini","max_tokens":600,"messages":[{"role":"user","content":p}]})
            raw=r.json()["choices"][0]["message"]["content"].replace("```json","").replace("```","").strip()
            res=json.loads(raw); res["source"]="chatgpt"; return res
    except Exception as e: return {"picks":[],"source":"chatgpt","error":str(e)}

async def ask_gem(mt,rd):
    if not GEMINI_KEY: return {"picks":[],"source":"gemini"}
    try:
        p=(f"Crypto expert. strategy={rd['strategy']} risk={rd['risk']} capital={rd['capital']} count={rd['coinCount']}\n"
           f"DATA:\n{mt}\nReply ONLY JSON: {{\"marketMood\":\"Mixed\",\"picks\":[{{\"symbol\":\"XRP/USDT\","
           f"\"signal\":\"BUY\",\"confidence\":85,\"reason\":\"reason\",\"score\":82}}]}}")
        async with httpx.AsyncClient(timeout=30) as h:
            r=await h.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
                           json={"contents":[{"parts":[{"text":p}]}]})
            raw=r.json()["candidates"][0]["content"]["parts"][0]["text"].replace("```json","").replace("```","").strip()
            res=json.loads(raw); res["source"]="gemini"; return res
    except Exception as e: return {"picks":[],"source":"gemini","error":str(e)}

def vote(cr,gr,mr,n):
    all_s={}
    for name,res in [("claude",cr),("chatgpt",gr),("gemini",mr)]:
        for p in res.get("picks",[]):
            sym=p.get("symbol","")
            if not sym: continue
            if sym not in all_s:
                all_s[sym]={"symbol":sym,"votes":0,"buy":0,"sell":0,"conf":0,"score":0,"ops":[],"reasons":[]}
            s=all_s[sym]; s["votes"]+=1
            sig=p.get("signal","BUY").upper()
            if sig=="BUY": s["buy"]+=1
            elif sig=="SELL": s["sell"]+=1
            s["conf"]+=p.get("confidence",70); s["score"]+=p.get("score",70)
            s["ops"].append(f"{name.upper()}: {sig}({p.get('confidence',70)}%)")
            if p.get("reason"): s["reasons"].append(p["reason"])
    con=[s for s in all_s.values() if s["votes"]>=2]
    con.sort(key=lambda x:(x["votes"],x["score"]),reverse=True)
    picks=[]
    for i,c in enumerate(con[:n]):
        ac=round(c["conf"]/c["votes"]); asc=round(c["score"]/c["votes"])
        sig="BUY" if c["buy"]>=2 else "SELL" if c["sell"]>=2 else "HOLD"
        picks.append({"rank":i+1,"symbol":c["symbol"],"name":c["symbol"].replace("/USDT",""),
                      "signal":sig,"votes":f"{c['votes']}/3 AI agreed","aiOpinions":c["ops"],
                      "aiScore":asc,"confidence":ac,"grade":"A" if asc>=80 else "B" if asc>=65 else "C",
                      "riskLevel":"Low","strategyMode":"Combo",
                      "whyPick":" | ".join(c["reasons"][:2]) or f"Consensus {c['votes']}/3 AIs",
                      "tags":[f"✅ {c['votes']}/3 AI Voted",f"{sig} Signal",f"Score {asc}/100"],
                      "grid":{"capitalUSDT":50,"gridLines":12,"rangeDown":6.0,"rangeUp":6.0,
                              "takeProfitPct":0.6,"stopLossPct":9.0,"expectedDailyPct":"0.4-0.9","gridSpacingPct":1.0},
                      "dca":{"dcaCapital":15,"buyDropPct":2.0,"multiplier":1.5,"maxLevels":3,"triggerRSI":38}})
    moods=[cr.get("marketMood","Mixed"),gr.get("marketMood","Mixed"),mr.get("marketMood","Mixed")]
    mc={}
    for m in moods: mc[m]=mc.get(m,0)+1
    fm=max(mc,key=mc.get)
    nc=len([s for s in all_s.values() if s["votes"]>=2])
    return {"btcTrend":"Sideways","marketMood":fm,"bestStrategy":"Combo","riskLevel":"Low",
            "marketSummary":f"Consensus: Claude+ChatGPT+Gemini ເລືອກ {nc} coins.",
            "picks":picks,"source":"consensus-3ai","timestamp":datetime.now().isoformat()}

async def run_ai(req):
    coins=await fetch_market()
    for c in coins: c["_risk"]=req.risk; c.update(score_coin(c))
    coins.sort(key=lambda x:x.get("aiScore",0),reverse=True)
    top=coins[:12]; cap_each=round(req.capital/req.coinCount,2)
    lines=[]
    for c in top:
        lines.append("{} RSI={:.1f} MACD={} BB={:.2f} vol%={:.1f} sideways={}/100 trend={} score={}/100".format(
            c["symbol"],c.get("rsi",50),c.get("macd",{}).get("cross","?"),
            c.get("bb",{}).get("pct",0.5),c["volatility"],c.get("sidewaysScore",50),c["trend"],c.get("aiScore",50)))
    prompt=(f"ທ່ານເປັນ world-class quantitative trader.\n"
            f"USER: strategy={req.strategy} risk={req.risk} capital={req.capital} count={req.coinCount} tf={req.timeframe}\n"
            f"NOTE: {req.note or 'none'}\nDATA:\n{chr(10).join(lines)}\n"
            f'ຕອບ JSON ເທົ່ານັ້ນ: {{"btcTrend":"Up|Down|Sideways","marketMood":"Bullish|Bearish|Sideways|Mixed",'
            f'"bestStrategy":"Grid|DCA|Combo","riskLevel":"Low|Medium|High","marketSummary":"4-5 ປະໂຫຍກ ພາສາລາວ",'
            f'"picks":[{{"rank":1,"symbol":"XRP/USDT","name":"Ripple","currentPrice":0.52,"aiScore":88,"grade":"A",'
            f'"confidence":85,"riskLevel":"Low","strategyMode":"Combo","whyPick":"ພາສາລາວ",'
            f'"technicalSummary":{{"rsiAnalysis":"...","macdAnalysis":"...","bbAnalysis":"...","trendAnalysis":"...","volumeAnalysis":"...","dcaAnalysis":"..."}},'
            f'"entryStrategy":"ພາສາລາວ","exitStrategy":"ພາສາລາວ","dcaTrigger":"ພາສາລາວ","riskWarning":"ພາສາລາວ",'
            f'"tags":["Sideways ✓","MACD Bullish"],'
            f'"grid":{{"capitalUSDT":{cap_each},"gridLines":12,"rangeDown":6.0,"rangeUp":6.0,"takeProfitPct":0.6,"stopLossPct":9.0,"expectedDailyPct":"0.4-0.9","gridSpacingPct":1.0}},'
            f'"dca":{{"dcaCapital":{round(cap_each*0.3,2)},"buyDropPct":2.0,"multiplier":1.5,"maxLevels":3,"triggerRSI":38}}}}]}}')
    if not ai: raise Exception("No AI key")
    msg=ai.messages.create(model="claude-sonnet-4-20250514",max_tokens=3000,
                            messages=[{"role":"user","content":prompt}])
    raw=msg.content[0].text.replace("```json","").replace("```","").strip()
    result=json.loads(raw); result["source"]="claude-ai-pro"; result["timestamp"]=datetime.now().isoformat()
    signals_cache.update(result); return result

def local_fallback(coins,req,cap_each):
    top=sorted(coins,key=lambda x:x.get("aiScore",0),reverse=True)[:req.coinCount]
    picks=[]
    for i,c in enumerate(top):
        v=c.get("volatility",5); atrp=c.get("atrPct",v*0.5)
        mult={"low":1.2,"med":1.5,"high":2.0}.get(req.risk,1.2)
        rng=round(max(3.0,min(15.0,atrp*mult*2.5)),1)
        grids=8 if v<5 else 12 if v<8 else 15
        tp=round(max(0.3,rng/grids*0.8),2); sl=round(rng*1.5,1)
        sw=c.get("sidewaysScore",60); base=sw/100*v*0.06
        picks.append({"rank":i+1,"symbol":c["symbol"],"name":c["symbol"].replace("/USDT",""),
                      "currentPrice":c["price"],"aiScore":c.get("aiScore",50),"grade":c.get("grade","C"),
                      "confidence":c.get("aiScore",50),"riskLevel":"Low" if v<6 else "Medium" if v<9 else "High",
                      "strategyMode":"Combo","whyPick":f"RSI={c.get('rsi',50):.1f}, MACD={c.get('macd',{}).get('cross','?')}",
                      "technicalSummary":{"rsiAnalysis":f"RSI={c.get('rsi',50):.1f}","macdAnalysis":c.get("macd",{}).get("cross","?"),
                                          "bbAnalysis":f"BB={c.get('bb',{}).get('pct',0.5):.2f}","trendAnalysis":c.get("trend","?"),
                                          "volumeAnalysis":f"{c.get('vol24hM',0):.0f}M","dcaAnalysis":"DCA ready" if c.get("dcaSignal") else "Wait"},
                      "entryStrategy":"ຊື້ຢູ່ lower BB + RSI<45","exitStrategy":"ຂາຍຢູ່ upper BB",
                      "dcaTrigger":"DCA ຕອນ RSI<38","riskWarning":"ຕ້ອງຕັ້ງ SL",
                      "tags":[s[:25] for s in c.get("signals",["Grid ready"])[:4]],
                      "grid":{"capitalUSDT":round(cap_each*0.7,2),"gridLines":grids,"rangeDown":round(rng/2,1),
                              "rangeUp":round(rng/2,1),"takeProfitPct":tp,"stopLossPct":sl,
                              "expectedDailyPct":f"{round(base*0.7,2)}-{round(base*1.4,2)}","gridSpacingPct":round(rng/grids,3)},
                      "dca":{"dcaCapital":round(cap_each*0.3,2),"buyDropPct":2.0,"multiplier":1.5,"maxLevels":3,"triggerRSI":38}})
    return {"btcTrend":"Sideways","marketMood":"Mixed","bestStrategy":"Combo","riskLevel":req.risk.capitalize(),
            "marketSummary":f"Local AI ວິເຄາະ {len(coins)} coins — ເລືອກ {req.coinCount} coins ທີ່ດີ.",
            "picks":picks,"source":"local-engine","timestamp":datetime.now().isoformat()}

async def fetch_klines(symbol,interval,days):
    limit=min(1000,days*{"1h":24,"4h":6,"1d":1}.get(interval,24))
    async with httpx.AsyncClient(timeout=20) as h:
        r=await h.get("https://api.binance.com/api/v3/klines",params={"symbol":symbol,"interval":interval,"limit":limit})
        return [{"ts":k[0],"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4]),"vol":float(k[5])} for k in r.json()]

def bt_grid(klines,capital,gn,rd,ru,tp,sl):
    if not klines: return None
    sp=klines[0]["close"]; lo=sp*(1-rd/100); hi=sp*(1+ru/100)
    step=(hi-lo)/gn; qpp=(capital/gn)/sp
    bal=capital; hld=0.0; trades=[]; eq=[]; wins=losses=0; maxdd=0; peak=capital; filled={}
    lvls=[lo+step*i for i in range(gn)]
    for i,bar in enumerate(klines):
        H=bar["high"]; L=bar["low"]; C=bar["close"]
        if hld>0 and filled:
            tc=sum(p*q for p,q in filled.items()); tq=sum(filled.values())
            avg=tc/tq if tq else sp
            if C<avg*(1-sl/100):
                pnl=(C-avg)*hld; bal+=hld*C
                trades.append({"type":"SL","price":round(C,6),"pnl":round(pnl,4),"bar":i})
                losses+=1; hld=0; filled={}
        for lvl in lvls:
            if L<=lvl and lvl not in filled and bal>=lvl*qpp:
                filled[lvl]=qpp; bal-=lvl*qpp; hld+=qpp
            tpp=lvl*(1+tp/100)
            if lvl in filled and H>=tpp:
                qty=filled.pop(lvl); pnl=(tpp-lvl)*qty; bal+=tpp*qty; hld-=qty
                trades.append({"type":"TP","price":round(tpp,6),"pnl":round(pnl,4),"bar":i}); wins+=1
        tot=bal+hld*C; eq.append(round(tot,4))
        if tot>peak: peak=tot
        dd=(peak-tot)/peak*100 if peak>0 else 0
        if dd>maxdd: maxdd=dd
    fe=bal+hld*klines[-1]["close"]; pnl=fe-capital; pct=pnl/capital*100; dn=max(1,len(klines)/24)
    return {"strategy":"Grid","startPrice":round(sp,6),"endPrice":round(klines[-1]["close"],6),
            "priceChange":round((klines[-1]["close"]-sp)/sp*100,2),"capital":capital,
            "finalEquity":round(fe,4),"totalPnl":round(pnl,4),"totalPnlPct":round(pct,2),
            "dailyReturn":round(pct/dn,3),"totalTrades":len(trades),"wins":wins,"losses":losses,
            "winRate":round(wins/max(wins+losses,1)*100,1),"maxDrawdown":round(maxdd,2),
            "equityCurve":eq[::max(1,len(eq)//100)],"recentTrades":trades[-20:],"bars":len(klines)}

def bt_dca(klines,capital,dd,dm,dl,tp):
    if not klines: return None
    bal=capital; pos=[]; trades=[]; eq=[]; wins=0; maxdd=0; peak=capital
    bq=(capital*0.2)/klines[0]["close"]; ref=klines[0]["close"]
    for i,bar in enumerate(klines):
        C=bar["close"]; L=bar["low"]; H=bar["high"]
        if len(pos)<dl:
            dn=ref*(1-dd/100*(len(pos)+1)); qty=bq*(dm**len(pos))
            if L<=dn and bal>=dn*qty:
                pos.append({"price":dn,"qty":qty}); bal-=dn*qty
                trades.append({"type":"DCA BUY","price":round(dn,6),"pnl":0,"bar":i})
        if pos:
            avg=sum(p["price"]*p["qty"] for p in pos)/sum(p["qty"] for p in pos)
            tpp=avg*(1+tp/100)
            if H>=tpp:
                tq=sum(p["qty"] for p in pos); pnl=(tpp-avg)*tq; bal+=tpp*tq
                trades.append({"type":"TP","price":round(tpp,6),"pnl":round(pnl,4),"bar":i})
                wins+=1; pos=[]; ref=C
        h=sum(p["qty"] for p in pos); tot=bal+h*C; eq.append(round(tot,4))
        if tot>peak: peak=tot
        dd2=(peak-tot)/peak*100 if peak>0 else 0
        if dd2>maxdd: maxdd=dd2
    h=sum(p["qty"] for p in pos); fe=bal+h*klines[-1]["close"]
    pnl=fe-capital; pct=pnl/capital*100; dn=max(1,len(klines)/24)
    return {"strategy":"DCA","startPrice":round(klines[0]["close"],6),"endPrice":round(klines[-1]["close"],6),
            "priceChange":round((klines[-1]["close"]-klines[0]["close"])/klines[0]["close"]*100,2),
            "capital":capital,"finalEquity":round(fe,4),"totalPnl":round(pnl,4),"totalPnlPct":round(pct,2),
            "dailyReturn":round(pct/dn,3),"totalTrades":len(trades),"wins":wins,"losses":0,
            "winRate":round(wins/max(len(trades),1)*100,1),"maxDrawdown":round(maxdd,2),
            "equityCurve":eq[::max(1,len(eq)//100)],"recentTrades":trades[-20:],"bars":len(klines)}

@app.get("/")
async def root(request: Request): return RedirectResponse("/login" if not get_session(request) else "/dashboard")
@app.get("/login")
async def lp():
    try:
        with open("login.html","r",encoding="utf-8") as f: return HTMLResponse(f.read())
    except: return HTMLResponse("<h2>login.html not found</h2>")
@app.get("/dashboard")
async def dp(request: Request):
    if not get_session(request): return RedirectResponse("/login")
    try:
        with open("dashboard.html","r",encoding="utf-8") as f: return HTMLResponse(f.read())
    except: return HTMLResponse("<h2>dashboard.html not found</h2>")
@app.get("/backtest")
async def bp(request: Request):
    if not get_session(request): return RedirectResponse("/login")
    try:
        with open("backtest.html","r",encoding="utf-8") as f: return HTMLResponse(f.read())
    except: return HTMLResponse("<h2>backtest.html not found</h2>")

@app.post("/api/login")
async def do_login(req: LoginReq, response: Response):
    stored=PLAIN_USERS.get(req.username.lower())
    if not stored or stored!=req.password: raise HTTPException(401,"Username ຫຼື Password ຜິດ")
    token=secrets.token_hex(32)
    sessions[token]={"username":req.username,"expires":time.time()+7*24*3600}
    response.set_cookie("sg_token",token,httponly=True,max_age=7*24*3600,samesite="lax")
    return {"status":"ok","username":req.username}

@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token=request.cookies.get("sg_token")
    if token in sessions: del sessions[token]
    response.delete_cookie("sg_token"); return {"status":"logged_out"}

@app.get("/api/health")
async def health():
    from fastapi.responses import JSONResponse
    hc=bool(ANTHROPIC_KEY); hg=bool(OPENAI_KEY); hm=bool(GEMINI_KEY)
    cnt=sum([hc,hg,hm]); con=cnt>=2
    active=len([1 for b in bots.values() if isinstance(b,dict) and b.get("status")=="running"])
    return JSONResponse(
        content={"status":"running","domain":"tradego.sbs",
                 "ai_mode":"consensus-3ai" if con else "claude-only",
                 "ai_count":f"{cnt}/3 AIs active","claude_ok":hc,"openai_ok":hg,"gemini_ok":hm,
                 "consensus":con,"activeBots":active,"timestamp":datetime.now().isoformat()},
        headers={"Content-Type":"application/json"})

@app.get("/api/market")
async def market(request: Request):
    require_auth(request)
    coins=await fetch_market()
    for c in coins: c.update(score_coin(c))
    coins.sort(key=lambda x:x.get("aiScore",0),reverse=True)
    return {"status":"ok","count":len(coins),"coins":coins,"timestamp":datetime.now().isoformat()}

@app.post("/api/analyze")
async def analyze(req: AnalyzeReq, request: Request):
    require_auth(request)
    coins=await fetch_market()
    for c in coins: c["_risk"]=req.risk; c.update(score_coin(c))
    cap_each=round(req.capital/req.coinCount,2)
    scored=sorted(coins,key=lambda x:x.get("aiScore",0),reverse=True)[:12]
    mt="\n".join(["{} RSI={:.1f} MACD={} BB={:.2f} vol%={:.1f} sideways={}/100 trend={} score={}/100".format(
        c["symbol"],c.get("rsi",50),c.get("macd",{}).get("cross","?"),c.get("bb",{}).get("pct",0.5),
        c["volatility"],c.get("sidewaysScore",50),c["trend"],c.get("aiScore",50)) for c in scored])
    hc=bool(ANTHROPIC_KEY); hg=bool(OPENAI_KEY); hm=bool(GEMINI_KEY)
    print(f"[AI] Claude={hc} GPT={hg} Gemini={hm}")
    if hc and (hg or hm):
        try:
            tasks=[run_ai(req)]
            if hg: tasks.append(ask_gpt(mt,req.dict()))
            if hm: tasks.append(ask_gem(mt,req.dict()))
            results=await asyncio.gather(*tasks,return_exceptions=True)
            cr=results[0] if not isinstance(results[0],Exception) else {"picks":[]}
            gr=results[1] if len(results)>1 and not isinstance(results[1],Exception) else {"picks":[]}
            mr=results[2] if len(results)>2 and not isinstance(results[2],Exception) else {"picks":[]}
            final=vote(cr,gr,mr,req.coinCount)
            if not final.get("picks"): return local_fallback(coins,req,cap_each)
            lf=local_fallback(coins,req,cap_each); lm={p["symbol"]:p for p in lf.get("picks",[])}
            for p in final["picks"]:
                if p["symbol"] in lm:
                    p["grid"]=lm[p["symbol"]]["grid"]; p["dca"]=lm[p["symbol"]]["dca"]
                    p["currentPrice"]=lm[p["symbol"]]["currentPrice"]
            return final
        except Exception as e: print(f"[AI] {e}"); return local_fallback(coins,req,cap_each)
    elif hc:
        try:
            r=await run_ai(req); return r if r.get("picks") else local_fallback(coins,req,cap_each)
        except: return local_fallback(coins,req,cap_each)
    return local_fallback(coins,req,cap_each)

@app.post("/api/rebalance")
async def rebalance(req: RebalanceReq, request: Request):
    require_auth(request)
    coins=await fetch_market(force=True)
    for c in coins: c["_risk"]=req.risk; c.update(score_coin(c))
    coins.sort(key=lambda x:x.get("aiScore",0),reverse=True)
    top=coins[:req.coinCount]; ce=round(req.capital/req.coinCount,2)
    entry={"timestamp":datetime.now().isoformat(),"coins":[c["symbol"] for c in top],"scores":[c.get("aiScore",50) for c in top]}
    rebalance_log.insert(0,entry); save_data()
    return {"status":"rebalanced","timestamp":entry["timestamp"],
            "newPortfolio":[{"symbol":c["symbol"],"score":c.get("aiScore",50),"grade":c.get("grade","C"),
                             "capital":ce,"rsi":c.get("rsi",50),"trend":c.get("trend","?")} for c in top]}

@app.get("/api/rebalance/history")
async def rbh(request: Request):
    require_auth(request); return {"history":rebalance_log[:20]}

@app.post("/api/bot/start")
async def bot_start(req: BotStartReq, request: Request):
    require_auth(request)
    bid=f"{req.symbol}_{req.strategy}_{int(time.time())}"
    bots[req.symbol]={"id":bid,"symbol":req.symbol,"strategy":req.strategy,"capital":req.capitalUSDT,
                      "mode":req.mode,"status":"running","pnl":0.0,"trades":0,
                      "started":datetime.now().isoformat(),
                      "grid":{"lines":req.gridLines,"rangeDown":req.rangeDown,"rangeUp":req.rangeUp,"tp":req.tpPct,"sl":req.slPct},
                      "dca":{"buyDropPct":req.dcaBuyPct,"multi":req.dcaMulti,"levels":req.dcaLevels},
                      "live_orders":[],"live_running":False}
    save_data()
    if req.mode=="live":
        if not BINANCE_KEY or not BINANCE_SECRET:
            bots[req.symbol]["status"]="error"; bots[req.symbol]["error"]="No Binance API key"
            return {"status":"error","error":"No Binance API key"}
        asyncio.create_task(run_live_grid(req.symbol))
    return {"status":"started","botId":bid,"symbol":req.symbol,"mode":req.mode}

@app.post("/api/bot/stop/{symbol}")
async def bot_stop(symbol: str, request: Request):
    require_auth(request)
    sym=symbol.replace("_","/")
    if sym not in bots: raise HTTPException(404,f"Bot {sym} not found")
    if bots[sym].get("mode")=="live" and bots[sym].get("live_running"):
        asyncio.create_task(cancel_live(sym))
    bots[sym]["status"]="stopped"; bots[sym]["live_running"]=False; save_data()
    return {"status":"stopped","symbol":sym,"finalPnl":bots[sym]["pnl"]}

@app.post("/api/bot/{symbol}/switch")
async def switch_mode(symbol: str, request: Request):
    require_auth(request)
    sym=symbol.replace("_","/")
    if sym not in bots: raise HTTPException(404,f"Bot {sym} not found")
    data=await request.json(); nm=data.get("mode","sim")
    bot=bots[sym]
    if nm=="sim" and bot.get("mode")=="live":
        if bot.get("live_running"): asyncio.create_task(cancel_live(sym))
        bot["mode"]="sim"; bot["live_running"]=False; save_data()
        return {"status":"switched","mode":"sim"}
    if nm=="live":
        if not BINANCE_KEY or not BINANCE_SECRET: raise HTTPException(400,"No Binance API key")
        bot["mode"]="live"; asyncio.create_task(run_live_grid(sym)); save_data()
        return {"status":"switched","mode":"live"}
    return {"status":"no change","mode":bot.get("mode")}

@app.get("/api/bots")
async def get_bots(request: Request):
    require_auth(request)
    v={k:b for k,b in bots.items() if isinstance(b,dict)}
    running=len([1 for b in v.values() if b.get("status")=="running"])
    return {"bots":list(v.values()),"total":len(v),"running":running}

@app.get("/api/balance")
async def api_bal(request: Request):
    require_auth(request); return await get_balance()

@app.get("/api/signals")
async def get_sigs(request: Request):
    require_auth(request); return {"signals":signals_cache,"timestamp":datetime.now().isoformat()}

@app.get("/api/pnl")
async def get_pnl(request: Request):
    require_auth(request)
    v={k:b for k,b in bots.items() if isinstance(b,dict)}
    total=sum(b.get("pnl",0) for b in v.values())
    return {"totalPnl":round(total,4),"bots":{k:b.get("pnl",0) for k,b in v.items()}}

@app.post("/api/backtest")
async def run_bt(req: BacktestReq, request: Request):
    require_auth(request)
    try:
        klines=await fetch_klines(req.symbol,req.interval,req.days)
        if not klines: raise HTTPException(400,"No data")
        if req.strategy=="dca":
            result=bt_dca(klines,req.capitalUSDT,req.dcaDropPct,req.dcaMulti,req.dcaLevels,req.tpPct)
        elif req.strategy=="combo":
            r1=bt_grid(klines,req.capitalUSDT*0.7,req.gridLines,req.rangeDown,req.rangeUp,req.tpPct,req.slPct)
            r2=bt_dca(klines,req.capitalUSDT*0.3,req.dcaDropPct,req.dcaMulti,req.dcaLevels,req.tpPct)
            tp2=r1["totalPnl"]+r2["totalPnl"]
            result={"strategy":"Combo","startPrice":r1["startPrice"],"endPrice":r1["endPrice"],
                    "priceChange":r1["priceChange"],"capital":req.capitalUSDT,
                    "finalEquity":round(r1["finalEquity"]+r2["finalEquity"],4),"totalPnl":round(tp2,4),
                    "totalPnlPct":round(tp2/req.capitalUSDT*100,2),"dailyReturn":round((r1["dailyReturn"]+r2["dailyReturn"])/2,3),
                    "totalTrades":r1["totalTrades"]+r2["totalTrades"],"wins":r1["wins"]+r2["wins"],
                    "losses":r1.get("losses",0)+r2.get("losses",0),"winRate":round((r1["winRate"]+r2["winRate"])/2,1),
                    "maxDrawdown":round(max(r1["maxDrawdown"],r2["maxDrawdown"]),2),"equityCurve":r1["equityCurve"],
                    "recentTrades":sorted(r1["recentTrades"]+r2["recentTrades"],key=lambda x:x["bar"])[-20:],"bars":r1["bars"]}
        else:
            result=bt_grid(klines,req.capitalUSDT,req.gridLines,req.rangeDown,req.rangeUp,req.tpPct,req.slPct)
        result["symbol"]=req.symbol; result["interval"]=req.interval; result["days"]=req.days
        result["timestamp"]=datetime.now().isoformat(); return result
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))

if __name__=="__main__":
    import uvicorn
    print("SocialGuard PRO v3 — tradego.sbs — Starting...")
    uvicorn.run("server_pro:app",host="0.0.0.0",port=PORT,reload=False)
