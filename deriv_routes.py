"""deriv_routes.py — Deriv module routes for SocialGuard PRO"""

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
    account_type: str = "demo"
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

def _require_auth(request: Request):
    from server_pro import require_auth as _ra
    return _ra(request)

@deriv_router.post("/api/deriv/connect")
async def deriv_connect(req: DerivConnectReq, request: Request):
    _require_auth(request)
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
            if ws.balance > 0:
                break
            await asyncio.sleep(0.2)
        return JSONResponse({"status": "connected", "balance": ws.balance,
                             "currency": ws.currency, "account_type": req.account_type,
                             "ws_running": ws.running})
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[DerivConnect ERROR] {e}\n{tb}")
        return JSONResponse({"error": str(e), "traceback": tb}, status_code=500)

@deriv_router.post("/api/deriv/start")
async def deriv_start(req: DerivStartReq, request: Request):
    _require_auth(request)
    try:
        from deriv_bot import start_deriv_bot
        app_id = req.app_id or DERIV_APP_ID
        token  = req.token or DERIV_TOKEN
        if not token:
            return JSONResponse({"error": "token required"}, status_code=400)
        result = await start_deriv_bot(
            symbol_key   = req.symbol,
            app_id       = app_id,
            token        = token,
            amount_pct   = req.amount_pct,
            duration     = req.duration,
            risk_level   = req.risk_level,
            account_type = req.account_type,
        )
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[DerivStart ERROR] {e}\n{tb}")
        return JSONResponse({"error": str(e), "traceback": tb}, status_code=500)

@deriv_router.post("/api/deriv/stop")
async def deriv_stop(req: DerivStopReq, request: Request):
    _require_auth(request)
    try:
        from deriv_bot import stop_deriv_bot
        return JSONResponse(await stop_deriv_bot(req.symbol))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@deriv_router.post("/api/deriv/stop-all")
async def deriv_stop_all(request: Request):
    _require_auth(request)
    try:
        from deriv_bot import stop_all_deriv
        return JSONResponse(await stop_all_deriv())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@deriv_router.get("/api/deriv/status")
async def deriv_status(request: Request):
    _require_auth(request)
    try:
        from deriv_bot import get_deriv_status
        return JSONResponse(get_deriv_status())
    except Exception as e:
        return JSONResponse({"error": str(e), "balance": 0, "bots": [],
                             "risk": {}, "last_signals": {}})

@deriv_router.get("/api/deriv/trades")
async def deriv_trades(request: Request):
    _require_auth(request)
    try:
        from deriv_bot import get_deriv_trades
        return JSONResponse(get_deriv_trades())
    except Exception as e:
        return JSONResponse({"error": str(e), "trades": [], "total": 0,
                             "wins": 0, "win_rate": 0, "daily_pnl": 0})

@deriv_router.post("/api/deriv/risk/reset")
async def deriv_risk_reset(request: Request):
    _require_auth(request)
    try:
        from deriv_bot import deriv_risk
        if deriv_risk:
            deriv_risk.reset_stop()
            return JSONResponse({"status": "risk reset"})
        return JSONResponse({"status": "no risk manager"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
