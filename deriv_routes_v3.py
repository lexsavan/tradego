"""deriv_routes_v3.py — Multi-symbol multi-trade-type routes"""

import os, asyncio, traceback
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List

deriv_router = APIRouter()

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "36544")
DERIV_TOKEN  = os.getenv("DERIV_TOKEN", "")

class DerivConnectReq(BaseModel):
    token:        str
    account_type: str = "demo"
    app_id:       Optional[str] = None

class MultiStartReq(BaseModel):
    symbols:      Optional[List[str]] = None    # None = all (V25, V50, V75, V100)
    stake:        float = 10.0
    duration:     int   = 1
    risk_level:   str   = "medium"
    app_id:       Optional[str] = None
    token:        Optional[str] = None
    account_type: str   = "demo"

class StopBotReq(BaseModel):
    symbol: str = "V75"

class DebugReq(BaseModel):
    enabled:    bool = True
    confidence: int  = 50

def _auth(request: Request):
    from server_pro import require_auth as _ra
    return _ra(request)

# ── Connect ──────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/connect")
async def deriv_connect(req: DerivConnectReq, request: Request):
    _auth(request)
    try:
        import deriv_bot_v3 as db
        from deriv_ws import DerivWS
        app_id = req.app_id or DERIV_APP_ID
        if db._ws_instance is None or not db._ws_instance.running:
            ws = DerivWS(app_id=app_id, token=req.token)
            await ws.connect()
            db._ws_instance = ws
        else:
            ws = db._ws_instance
        for _ in range(40):
            if ws.balance > 0: break
            await asyncio.sleep(0.2)
        return JSONResponse({
            "status":"connected",
            "balance": ws.balance,
            "currency": ws.currency,
            "account_type": req.account_type,
            "ws_running": ws.running,
            "available_symbols": list(db.SYMBOLS.keys()),
        })
    except Exception as e:
        return JSONResponse({"error":str(e),"traceback":traceback.format_exc()}, status_code=500)

# ── Start (Multi-symbol) ─────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/start")
async def deriv_start_multi(req: MultiStartReq, request: Request):
    _auth(request)
    try:
        from deriv_bot_v3 import start_multi_bot
        token = req.token or DERIV_TOKEN
        if not token:
            return JSONResponse({"error":"token required"}, status_code=400)
        result = await start_multi_bot(
            app_id       = req.app_id or DERIV_APP_ID,
            token        = token,
            symbols      = req.symbols,
            stake        = req.stake,
            duration     = req.duration,
            risk_level   = req.risk_level,
            account_type = req.account_type,
        )
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error":str(e),"traceback":traceback.format_exc()}, status_code=500)

# ── Stop ──────────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/stop")
async def deriv_stop(req: StopBotReq, request: Request):
    _auth(request)
    try:
        from deriv_bot_v3 import stop_single_bot
        return JSONResponse(await stop_single_bot(req.symbol))
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)

@deriv_router.post("/api/deriv/stop-all")
async def deriv_stop_all(request: Request):
    _auth(request)
    try:
        from deriv_bot_v3 import stop_all_bots
        return JSONResponse(await stop_all_bots())
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)

# ── Status / Trades ──────────────────────────────────────────────────────────
@deriv_router.get("/api/deriv/status")
async def deriv_status(request: Request):
    _auth(request)
    try:
        from deriv_bot_v3 import get_multi_status
        return JSONResponse(get_multi_status())
    except Exception as e:
        return JSONResponse({"error":str(e),"balance":0,"bots":[],"risk":{},"last_signals":{},"execution":{}})

@deriv_router.get("/api/deriv/trades")
async def deriv_trades(request: Request):
    _auth(request)
    try:
        from deriv_bot_v3 import get_multi_trades
        return JSONResponse(get_multi_trades())
    except Exception as e:
        return JSONResponse({"error":str(e),"trades":[],"total":0,"wins":0,"win_rate":0,"daily_pnl":0})

# ── Debug Mode ────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/debug")
async def deriv_debug(req: DebugReq, request: Request):
    _auth(request)
    try:
        from deriv_execution import exec_state
        exec_state.debug_mode = req.enabled
        exec_state.debug_confidence_threshold = req.confidence
        return JSONResponse({
            "debug_mode": exec_state.debug_mode,
            "confidence_threshold": exec_state.debug_confidence_threshold,
            "message": f"Debug mode {'ON' if req.enabled else 'OFF'} — threshold={req.confidence}%"
        })
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)

@deriv_router.post("/api/deriv/risk/reset")
async def deriv_risk_reset(request: Request):
    _auth(request)
    try:
        from deriv_bot_v3 import deriv_risk
        from deriv_execution import exec_state
        if deriv_risk: deriv_risk.reset_stop()
        exec_state.stopped_today = False
        exec_state.stop_reason   = ""
        exec_state.loss_streak   = 0
        exec_state.cooldown_until = 0
        return JSONResponse({"status":"risk reset"})
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)
