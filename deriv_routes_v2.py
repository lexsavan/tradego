"""deriv_routes_v2.py — Upgraded routes with debug mode + execution endpoints"""

import os, asyncio, traceback
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

deriv_router = APIRouter()

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "36544")
DERIV_TOKEN  = os.getenv("DERIV_TOKEN", "")

class DerivConnectReq(BaseModel):
    token:        str
    account_type: str   = "demo"
    app_id:       Optional[str] = None

class DerivStartReq(BaseModel):
    symbol:       str   = "V75"
    amount_pct:   float = 2.0
    duration:     int   = 1
    risk_level:   str   = "medium"
    app_id:       Optional[str] = None
    token:        Optional[str] = None
    account_type: str   = "demo"

class DerivStopReq(BaseModel):
    symbol: str = "V75"

class DebugModeReq(BaseModel):
    enabled:    bool = True
    confidence: int  = 50   # lower threshold for test trades

def _auth(request: Request):
    from server_pro import require_auth as _ra
    return _ra(request)

# ── Connect ──────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/connect")
async def deriv_connect(req: DerivConnectReq, request: Request):
    _auth(request)
    try:
        import deriv_bot as db
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
        return JSONResponse({"status":"connected","balance":ws.balance,
                             "currency":ws.currency,"account_type":req.account_type,
                             "ws_running":ws.running})
    except Exception as e:
        return JSONResponse({"error":str(e),"traceback":traceback.format_exc()},status_code=500)

# ── Start ─────────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/start")
async def deriv_start(req: DerivStartReq, request: Request):
    _auth(request)
    try:
        from deriv_bot import start_deriv_bot
        token = req.token or DERIV_TOKEN
        if not token:
            return JSONResponse({"error":"token required"},status_code=400)
        result = await start_deriv_bot(
            symbol_key=req.symbol, app_id=req.app_id or DERIV_APP_ID,
            token=token, amount_pct=req.amount_pct, duration=req.duration,
            risk_level=req.risk_level, account_type=req.account_type,
        )
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error":str(e),"traceback":traceback.format_exc()},status_code=500)

# ── Stop ──────────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/stop")
async def deriv_stop(req: DerivStopReq, request: Request):
    _auth(request)
    try:
        from deriv_bot import stop_deriv_bot
        return JSONResponse(await stop_deriv_bot(req.symbol))
    except Exception as e:
        return JSONResponse({"error":str(e)},status_code=500)

@deriv_router.post("/api/deriv/stop-all")
async def deriv_stop_all(request: Request):
    _auth(request)
    try:
        from deriv_bot import stop_all_deriv
        return JSONResponse(await stop_all_deriv())
    except Exception as e:
        return JSONResponse({"error":str(e)},status_code=500)

# ── Status ────────────────────────────────────────────────────────────────────
@deriv_router.get("/api/deriv/status")
async def deriv_status(request: Request):
    _auth(request)
    try:
        from deriv_bot import get_deriv_status
        return JSONResponse(get_deriv_status())
    except Exception as e:
        return JSONResponse({"error":str(e),"balance":0,"bots":[],"risk":{},"last_signals":{},"execution":{}})

# ── Trades + Execution ────────────────────────────────────────────────────────
@deriv_router.get("/api/deriv/trades")
async def deriv_trades(request: Request):
    _auth(request)
    try:
        from deriv_bot import get_deriv_trades
        return JSONResponse(get_deriv_trades())
    except Exception as e:
        return JSONResponse({"error":str(e),"trades":[],"total":0,"wins":0,"win_rate":0,"daily_pnl":0})

@deriv_router.get("/api/deriv/execution")
async def deriv_execution(request: Request):
    _auth(request)
    try:
        from deriv_execution import exec_state
        return JSONResponse(exec_state.status_dict())
    except Exception as e:
        return JSONResponse({"error":str(e)},status_code=500)

@deriv_router.get("/api/deriv/positions")
async def deriv_positions(request: Request):
    _auth(request)
    try:
        from deriv_execution import exec_state
        return JSONResponse({
            "open_positions": list(exec_state.open_positions.values()),
            "count":          len(exec_state.open_positions),
        })
    except Exception as e:
        return JSONResponse({"error":str(e)},status_code=500)

# ── Debug Mode ────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/debug")
async def deriv_debug(req: DebugModeReq, request: Request):
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
        return JSONResponse({"error":str(e)},status_code=500)

# ── Risk Reset ────────────────────────────────────────────────────────────────
@deriv_router.post("/api/deriv/risk/reset")
async def deriv_risk_reset(request: Request):
    _auth(request)
    try:
        from deriv_bot import deriv_risk
        from deriv_execution import exec_state
        if deriv_risk: deriv_risk.reset_stop()
        exec_state.stopped_today = False
        exec_state.stop_reason   = ""
        exec_state.loss_streak   = 0
        exec_state.cooldown_until = 0
        return JSONResponse({"status":"risk reset — bot can resume"})
    except Exception as e:
        return JSONResponse({"error":str(e)},status_code=500)

# ── Strategy ──────────────────────────────────────────────────────────────────
@deriv_router.get("/api/deriv/strategy")
async def deriv_strategy(request: Request):
    _auth(request)
    try:
        from deriv_execution import exec_state
        return JSONResponse({
            "best_strategy":   exec_state.best_strategy,
            "strategy_scores": exec_state.strategy_scores,
            "strategy_stats":  exec_state.strategy_stats,
            "winrate_10":      exec_state.winrate_10,
            "last_10_trades":  list(exec_state.last_10_trades),
        })
    except Exception as e:
        return JSONResponse({"error":str(e)},status_code=500)
