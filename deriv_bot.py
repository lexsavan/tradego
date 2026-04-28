"""deriv_bot.py — Deriv bot orchestrator for SocialGuard PRO"""

import asyncio, os, time
from datetime import datetime
from typing import Optional

from deriv_ws  import DerivWS
from deriv_ai  import get_signal
from deriv_risk import DerivRiskManager

SYMBOLS = {
    "V75":  "R_75",
    "V100": "R_100",
    "V25":  "R_25",
    "V50":  "R_50",
}

# ── Isolated Deriv state ─────────────────────────────────────────────────────
deriv_bots: dict     = {}
deriv_sessions: dict = {}
deriv_risk: Optional[DerivRiskManager] = None
_last_signal: dict   = {}
_ws_instance: Optional[DerivWS] = None
_bot_task: Optional[asyncio.Task] = None

async def start_deriv_bot(symbol_key: str, app_id: str, token: str,
                          amount_pct: float = 2.0, duration: int = 1,
                          risk_level: str = "low",
                          account_type: str = "demo") -> dict:
    global _ws_instance, deriv_risk, _bot_task

    if symbol_key in deriv_bots and deriv_bots[symbol_key].get("status") == "running":
        return {"error": f"Bot {symbol_key} already running"}

    sym = SYMBOLS.get(symbol_key)
    if not sym:
        return {"error": f"Unknown symbol {symbol_key}. Use: {list(SYMBOLS.keys())}"}

    # Risk manager
    if deriv_risk is None:
        cfg = {
            "low":    {"daily_loss_limit_pct": 5,  "max_trades_per_hour": 6,  "drawdown_stop_pct": 10, "min_confidence": 82},
            "medium": {"daily_loss_limit_pct": 10, "max_trades_per_hour": 8,  "drawdown_stop_pct": 15, "min_confidence": 80},
            "high":   {"daily_loss_limit_pct": 15, "max_trades_per_hour": 10, "drawdown_stop_pct": 20, "min_confidence": 78},
        }.get(risk_level, {"daily_loss_limit_pct": 10, "max_trades_per_hour": 8,
                           "drawdown_stop_pct": 15, "min_confidence": 80})
        deriv_risk = DerivRiskManager(**cfg)

    # WebSocket
    if _ws_instance is None or not _ws_instance.running:
        _ws_instance = DerivWS(app_id=app_id, token=token)
        try:
            await _ws_instance.connect()
        except Exception as e:
            return {"error": f"WebSocket connection failed: {e}"}
        _ws_instance.on_balance(_on_balance)
        _ws_instance.on_trade(_on_trade_result)
        await _ws_instance.subscribe_balance()

    await _ws_instance.subscribe_ticks(sym)

    bot_state = {
        "symbol_key":    symbol_key,
        "symbol":        sym,
        "status":        "running",
        "amount_pct":    amount_pct,
        "duration":      duration,
        "risk_level":    risk_level,
        "account_type":  account_type,
        "started":       datetime.now().isoformat(),
        "last_signal":   {},
        "active_trade":  None,
        "trades_count":  0,
    }
    deriv_bots[symbol_key] = bot_state

    _bot_task = asyncio.create_task(_trading_loop(symbol_key, sym, amount_pct, duration))
    return {"status": "started", "symbol": sym, "symbol_key": symbol_key,
            "account_type": account_type}

async def stop_deriv_bot(symbol_key: str) -> dict:
    global _bot_task
    if symbol_key not in deriv_bots:
        return {"error": "Bot not found"}
    deriv_bots[symbol_key]["status"] = "stopped"
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
    return {"status": "stopped", "symbol_key": symbol_key}

async def stop_all_deriv() -> dict:
    global _ws_instance, _bot_task
    for k in list(deriv_bots.keys()):
        deriv_bots[k]["status"] = "stopped"
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
    if _ws_instance:
        await _ws_instance.disconnect()
        _ws_instance = None
    return {"status": "all stopped"}

async def _trading_loop(symbol_key: str, symbol: str, amount_pct: float, duration: int):
    global _last_signal
    print(f"[DerivBot] Loop started — {symbol_key} ({symbol})")
    SCAN_INTERVAL = 15

    try:
        while deriv_bots.get(symbol_key, {}).get("status") == "running":
            await asyncio.sleep(SCAN_INTERVAL)

            if deriv_risk and deriv_risk.stopped:
                deriv_bots[symbol_key]["status"] = "stopped_risk"
                print(f"[DerivBot] Risk stop: {deriv_risk.stop_reason}")
                break

            prices = _ws_instance.ticks.get(symbol, []) if _ws_instance else []
            if len(prices) < 25:
                print(f"[DerivBot] Waiting for ticks ({len(prices)}/25)")
                continue

            balance = _ws_instance.balance if _ws_instance else 0
            if deriv_risk:
                deriv_risk.set_balance(balance)

            signal_data = await get_signal(symbol, prices, balance, use_consensus=True)
            _last_signal[symbol_key]              = signal_data
            deriv_bots[symbol_key]["last_signal"] = signal_data

            sig  = signal_data.get("signal", "WAIT")
            conf = signal_data.get("confidence", 0)
            print(f"[DerivBot] {symbol_key} Signal={sig} Conf={conf}% — {signal_data.get('reason','')}")

            if deriv_risk:
                ok, reason = deriv_risk.can_trade(conf, sig)
                if not ok:
                    print(f"[DerivBot] Risk block: {reason}")
                    continue

            stake = deriv_risk.stake_amount(amount_pct) if deriv_risk else round(balance * amount_pct / 100, 2)
            contract_type = "CALL" if sig == "BUY" else "PUT"

            deriv_bots[symbol_key]["active_trade"] = {
                "signal": sig, "stake": stake,
                "time": datetime.now().strftime("%H:%M:%S"),
                "duration": duration, "contract_type": contract_type
            }

            print(f"[DerivBot] Placing {contract_type} | stake={stake} | {duration}m")
            result = await _ws_instance.buy_contract(symbol, contract_type, duration, stake)
            if "error" in result:
                print(f"[DerivBot] Trade error: {result['error']}")
                deriv_bots[symbol_key]["active_trade"] = None
                continue

            deriv_bots[symbol_key]["trades_count"] = deriv_bots[symbol_key].get("trades_count", 0) + 1
            await asyncio.sleep(duration * 60 + 10)
            deriv_bots[symbol_key]["active_trade"] = None

    except asyncio.CancelledError:
        print(f"[DerivBot] {symbol_key} loop cancelled")
    except Exception as e:
        print(f"[DerivBot] Loop error: {e}")
        if symbol_key in deriv_bots:
            deriv_bots[symbol_key]["status"] = "error"
            deriv_bots[symbol_key]["error"]  = str(e)

async def _on_balance(data: dict):
    if deriv_risk:
        deriv_risk.set_balance(data["balance"])

async def _on_trade_result(data: dict):
    if data.get("type") != "settled":
        return
    poc   = data["data"]
    won   = float(poc.get("profit", 0)) > 0
    pnl   = float(poc.get("profit", 0))
    stake = float(poc.get("buy_price", 0))
    sym   = poc.get("underlying", "")
    sig   = "BUY" if poc.get("contract_type", "").startswith("CALL") else "SELL"
    if deriv_risk:
        deriv_risk.record_trade(won, pnl, stake, sym, sig)
    print(f"[DerivBot] {'✅ WIN' if won else '❌ LOSS'} | PnL={pnl:.2f} | {sym}")

def get_deriv_status() -> dict:
    risk_status  = deriv_risk.status() if deriv_risk else {}
    ws_connected = _ws_instance is not None and _ws_instance.running
    bots_list    = []
    for k, b in deriv_bots.items():
        ticks_count = len(_ws_instance.ticks.get(b["symbol"], [])) if _ws_instance else 0
        bots_list.append({**b, "ticks_collected": ticks_count})
    return {
        "ws_connected": ws_connected,
        "balance":      _ws_instance.balance if _ws_instance else 0,
        "currency":     _ws_instance.currency if _ws_instance else "USD",
        "bots":         bots_list,
        "last_signals": _last_signal,
        "risk":         risk_status,
        "timestamp":    datetime.now().isoformat(),
    }

def get_deriv_trades() -> dict:
    trades = deriv_risk.trade_history if deriv_risk else []
    risk   = deriv_risk.status() if deriv_risk else {}
    return {
        "trades":    trades[:50],
        "total":     risk.get("total_trades", 0),
        "wins":      risk.get("wins", 0),
        "win_rate":  risk.get("win_rate", 0),
        "daily_pnl": risk.get("daily_pnl", 0),
    }
