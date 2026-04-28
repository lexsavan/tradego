"""deriv_routes.py — Deriv module routes for SocialGuard PRO"""

import os, asyncio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from deriv_bot import (
    start_deriv_bot, stop_deriv_bot, stop_all_deriv,
    get_deriv_status, get_deriv_trades,
)

deriv_router = APIRouter()

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DERIV_TOKEN  = os.getenv("DERIV_TOKEN",  "")

# ── Request models ────────────────────────────────────────────────────────────

class DerivConnectReq(BaseModel):
    token:        str
    account_type: str   = "demo"
    app_id:       Optional[str] = None

class DerivStartReq(BaseModel):
    symbol:       str   = "V75"
    amount_pct:   float = 2.0
    duration:     int   = 1
    risk_level:   str   = "low"
    app_id:       Optional[str] = None
    token:        Optional[str] = None
    account_type: str   = "demo"

class DerivStopReq(BaseModel):
    symbol: str = "V75"

# ── Auth helper ───────────────────────────────────────────────────────────────

def _require_auth(request: Request):
    from server_pro import require_auth as _ra
    return _ra(request)

# ── Routes ────────────────────────────────────────────────────────────────────

@deriv_router.post("/api/deriv/connect")
async def deriv_connect(req: DerivConnectReq, request: Request):
    """Connect WebSocket + fetch balance — no trading started"""
    _require_auth(request)
    import deriv_bot as db
    from deriv_ws import DerivWS

    app_id = req.app_id or DERIV_APP_ID
    token  = req.token
    if not token:
        raise HTTPException(400, "token required")

    # Connect WS if not already running
    if db._ws_instance is None or not db._ws_instance.running:
        ws = DerivWS(app_id=app_id, token=token)
        try:
            await ws.connect()
            db._ws_instance = ws
        except Exception as e:
            return JSONResponse({"error": f"WebSocket connection failed: {e}"}, status_code=500)
    else:
        ws = db._ws_instance

    # Wait up to 8s for authorize + balance
    for _ in range(40):
        if ws.balance > 0:
            break
        await asyncio.sleep(0.2)

    return JSONResponse({
        "status":       "connected",
        "balance":      ws.balance,
        "currency":     ws.currency,
        "account_type": req.account_type,
        "ws_running":   ws.running,
    })

@deriv_router.post("/api/deriv/start")
async def deriv_start(req: DerivStartReq, request: Request):
    _require_auth(request)
    app_id = req.app_id or DERIV_APP_ID
    token  = req.token  or DERIV_TOKEN
    if not token:
        raise HTTPException(400, "DERIV_TOKEN not set — provide token in request or set env var")
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
        raise HTTPException(400, result["error"])
    return result

@deriv_router.post("/api/deriv/stop")
async def deriv_stop(req: DerivStopReq, request: Request):
    _require_auth(request)
    return await stop_deriv_bot(req.symbol)

@deriv_router.post("/api/deriv/stop-all")
async def deriv_stop_all(request: Request):
    _require_auth(request)
    return await stop_all_deriv()

@deriv_router.get("/api/deriv/status")
async def deriv_status(request: Request):
    _require_auth(request)
    return JSONResponse(get_deriv_status())

@deriv_router.get("/api/deriv/trades")
async def deriv_trades(request: Request):
    _require_auth(request)
    return JSONResponse(get_deriv_trades())

@deriv_router.post("/api/deriv/risk/reset")
async def deriv_risk_reset(request: Request):
    _require_auth(request)
    from deriv_bot import deriv_risk
    if deriv_risk:
        deriv_risk.reset_stop()
        return {"status": "risk reset — bot can resume"}
    return {"status": "no risk manager active"}
