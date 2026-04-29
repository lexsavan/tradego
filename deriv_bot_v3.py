"""deriv_bot_v3.py — Multi-symbol parallel bot with AI trade-type selection
   Trades V25, V50, V75, V100 simultaneously with fixed $10 stake
"""

import asyncio, os, time
from datetime import datetime
from typing import Optional, Dict

from deriv_ws       import DerivWS
from deriv_ai_v2    import get_signal_v2, compute_indicators
from deriv_risk     import DerivRiskManager
from deriv_execution import exec_state, ExecutionState

# All synthetic symbols
SYMBOLS = {
    "V25":  "R_25",
    "V50":  "R_50",
    "V75":  "R_75",
    "V100": "R_100",
}

deriv_bots:    Dict[str, dict]  = {}
deriv_risk:    Optional[DerivRiskManager] = None
_ws_instance:  Optional[DerivWS] = None
_bot_tasks:    Dict[str, asyncio.Task] = {}
_last_signal:  dict = {}
_global_running: bool = False

# Fixed stake
FIXED_STAKE = 10.0

async def start_multi_bot(app_id: str, token: str, 
                           symbols: list = None,
                           stake: float = 10.0,
                           duration: int = 1,
                           risk_level: str = "medium",
                           account_type: str = "demo") -> dict:
    """Start bots for multiple symbols in parallel"""
    global _ws_instance, deriv_risk, _global_running
    
    if symbols is None:
        symbols = list(SYMBOLS.keys())   # All 4 symbols
    
    # Risk manager
    if deriv_risk is None:
        cfg = {
            "low":    {"daily_loss_limit_pct":5,  "max_trades_per_hour":12, "drawdown_stop_pct":10,"min_confidence":82},
            "medium": {"daily_loss_limit_pct":10, "max_trades_per_hour":20, "drawdown_stop_pct":15,"min_confidence":80},
            "high":   {"daily_loss_limit_pct":15, "max_trades_per_hour":30, "drawdown_stop_pct":20,"min_confidence":78},
        }.get(risk_level, {"daily_loss_limit_pct":10,"max_trades_per_hour":20,"drawdown_stop_pct":15,"min_confidence":80})
        deriv_risk = DerivRiskManager(**cfg)
    
    # WebSocket (single instance for all symbols)
    if _ws_instance is None or not _ws_instance.running:
        _ws_instance = DerivWS(app_id=app_id, token=token)
        try:
            await _ws_instance.connect()
        except Exception as e:
            return {"error": f"WS failed: {e}"}
        _ws_instance.on_balance(_on_balance)
        _ws_instance.on_trade(_on_trade_settled)
        _ws_instance.on_tick(_on_tick)
        await _ws_instance.subscribe_balance()
    
    # Subscribe to all symbols
    started = []
    skipped = []
    for sk in symbols:
        sym = SYMBOLS.get(sk)
        if not sym:
            skipped.append(f"{sk}:unknown")
            continue
        if sk in deriv_bots and deriv_bots[sk].get("status") == "running":
            skipped.append(f"{sk}:already_running")
            continue
        
        await _ws_instance.subscribe_ticks(sym)
        
        deriv_bots[sk] = {
            "symbol_key":   sk, "symbol": sym,
            "status":       "running",  "stake": stake,
            "duration":     duration,   "risk_level": risk_level,
            "account_type": account_type,
            "started":      datetime.now().isoformat(),
            "last_signal":  {}, "active_trade": None, "trades_count": 0,
        }
        
        # Start parallel trading loop
        _bot_tasks[sk] = asyncio.create_task(_trading_loop(sk, sym, stake, duration))
        started.append(sk)
    
    _global_running = True
    exec_state.bot_status = "RUNNING"
    
    return {
        "status":       "started",
        "started":      started,
        "skipped":      skipped,
        "symbols":      [SYMBOLS[s] for s in started],
        "stake":        stake,
        "account_type": account_type,
        "total_bots":   len(deriv_bots),
    }

async def stop_all_bots() -> dict:
    """Stop all running bots"""
    global _ws_instance, _global_running
    stopped = []
    for sk in list(deriv_bots.keys()):
        deriv_bots[sk]["status"] = "stopped"
        if sk in _bot_tasks and not _bot_tasks[sk].done():
            _bot_tasks[sk].cancel()
        stopped.append(sk)
    
    _global_running = False
    exec_state.bot_status = "WAITING"
    
    if _ws_instance:
        await _ws_instance.disconnect()
        _ws_instance = None
    
    return {"status": "all stopped", "stopped": stopped}

async def stop_single_bot(symbol_key: str) -> dict:
    """Stop one bot only"""
    if symbol_key not in deriv_bots:
        return {"error": "Bot not found"}
    deriv_bots[symbol_key]["status"] = "stopped"
    if symbol_key in _bot_tasks and not _bot_tasks[symbol_key].done():
        _bot_tasks[symbol_key].cancel()
    return {"status": "stopped", "symbol_key": symbol_key}

async def _trading_loop(symbol_key: str, symbol: str, stake: float, duration: int):
    """Trading loop for ONE symbol — runs in parallel with others"""
    print(f"[BotV3] Loop started — {symbol_key} ({symbol}) stake=${stake}")
    SCAN = 15
    
    try:
        while deriv_bots.get(symbol_key, {}).get("status") == "running":
            await asyncio.sleep(SCAN)
            
            balance = _ws_instance.balance if _ws_instance else 0
            exec_state.init_day(balance)
            if deriv_risk: deriv_risk.set_balance(balance)
            
            # Safety
            if exec_state.stopped_today:
                print(f"[BotV3 {symbol_key}] Stopped: {exec_state.stop_reason}")
                deriv_bots[symbol_key]["status"] = "stopped_safety"
                break
            
            prices = _ws_instance.ticks.get(symbol, []) if _ws_instance else []
            if len(prices) < 25:
                continue
            
            # AI signal with trade_type
            signal_data = await get_signal_v2(symbol_key, prices, balance, use_consensus=True)
            sig         = signal_data.get("signal", "WAIT")
            conf        = signal_data.get("confidence", 0)
            trade_type  = signal_data.get("trade_type", "rise_fall")
            
            exec_state.last_signal = sig
            _last_signal[symbol_key] = signal_data
            deriv_bots[symbol_key]["last_signal"] = signal_data
            
            # AI feedback (if losing, reduce confidence)
            if exec_state.winrate_10 < 40 and len(exec_state.last_10_trades) >= 5:
                conf = max(0, conf - 10)
            
            print(f"[BotV3 {symbol_key}] Signal={sig} Type={trade_type} Conf={conf}% Votes={signal_data.get('votes','-')}")
            
            # Can trade?
            ok, reason = exec_state.can_trade(conf)
            if not ok:
                continue
            
            if deriv_risk:
                risk_ok, risk_reason = deriv_risk.can_trade(conf, sig)
                if not risk_ok:
                    continue
            
            # Determine contract type based on AI's trade_type choice
            if trade_type == "higher_lower":
                # Higher/Lower: barrier-based, longer duration (5min)
                contract_type = "CALL" if sig == "BUY" else "PUT"
                actual_duration = max(duration, 5)
                strategy = "higher_lower"
            else:
                # Rise/Fall: 1-3min binary
                contract_type = "CALL" if sig == "BUY" else "PUT"
                actual_duration = duration
                strategy = "rise_fall"
            
            exec_state.bot_status = "TRADING"
            deriv_bots[symbol_key]["active_trade"] = {
                "signal": sig, "stake": stake, "strategy": strategy,
                "trade_type": trade_type, "duration": actual_duration,
                "time": datetime.now().strftime("%H:%M:%S"),
                "contract_type": contract_type,
            }
            
            print(f"[BotV3 {symbol_key}] PLACING {contract_type} ({trade_type}) | stake=${stake} | {actual_duration}m")
            result = await _ws_instance.buy_contract(symbol, contract_type, actual_duration, stake)
            
            if "error" in result:
                print(f"[BotV3 {symbol_key}] Trade error: {result.get('error')}")
                deriv_bots[symbol_key]["active_trade"] = None
                continue
            
            buy_data    = result.get("buy", {})
            contract_id = str(buy_data.get("contract_id", f"sim_{time.time()}"))
            exec_state.record_open(contract_id, sig, stake, symbol, strategy)
            if deriv_risk: deriv_risk.trade_times.append(time.time())
            
            deriv_bots[symbol_key]["trades_count"] += 1
            
            # Wait for settlement
            await asyncio.sleep(actual_duration * 60 + 10)
            deriv_bots[symbol_key]["active_trade"] = None
    
    except asyncio.CancelledError:
        print(f"[BotV3 {symbol_key}] cancelled")
    except Exception as e:
        import traceback
        print(f"[BotV3 {symbol_key}] Error: {e}\n{traceback.format_exc()}")
        if symbol_key in deriv_bots:
            deriv_bots[symbol_key]["status"] = "error"
            deriv_bots[symbol_key]["error"] = str(e)

async def _on_tick(data: dict):
    exec_state.add_tick(data["price"])

async def _on_balance(data: dict):
    if deriv_risk: deriv_risk.set_balance(data["balance"])

async def _on_trade_settled(data: dict):
    if data.get("type") != "settled": return
    poc = data["data"]
    won   = float(poc.get("profit", 0)) > 0
    pnl   = float(poc.get("profit", 0))
    stake = float(poc.get("buy_price", 0))
    contract_id = str(poc.get("contract_id", ""))
    balance = _ws_instance.balance if _ws_instance else 0
    
    trade = exec_state.record_close(contract_id, won, pnl, balance)
    if deriv_risk: deriv_risk.record_trade(won, pnl, stake, trade.get("symbol",""), trade.get("signal",""))
    print(f"[BotV3] {'✅ WIN' if won else '❌ LOSS'} | PnL={pnl:.2f} | Symbol={trade.get('symbol','')}")

def get_multi_status() -> dict:
    ws_ok = _ws_instance is not None and _ws_instance.running
    bots_list = []
    for sk, b in deriv_bots.items():
        ticks = len(_ws_instance.ticks.get(b["symbol"], [])) if _ws_instance else 0
        last_sig = _last_signal.get(sk, {})
        bots_list.append({
            **b,
            "ticks_collected": ticks,
            "current_signal":  last_sig.get("signal", "WAIT"),
            "current_conf":    last_sig.get("confidence", 0),
            "trade_type":      last_sig.get("trade_type", "rise_fall"),
        })
    
    return {
        "ws_connected":  ws_ok,
        "balance":       _ws_instance.balance if _ws_instance else 0,
        "currency":      _ws_instance.currency if _ws_instance else "USD",
        "bots":          bots_list,
        "active_bots":   sum(1 for b in deriv_bots.values() if b.get("status") == "running"),
        "last_signals":  _last_signal,
        "risk":          deriv_risk.status() if deriv_risk else {},
        "execution":     exec_state.status_dict(),
        "global_running": _global_running,
        "fixed_stake":   FIXED_STAKE,
        "available_symbols": list(SYMBOLS.keys()),
        "timestamp":     datetime.now().isoformat(),
    }

def get_multi_trades() -> dict:
    risk = deriv_risk.status() if deriv_risk else {}
    ex = exec_state
    return {
        "trades":          ex.trade_log[:50],
        "open_positions":  list(ex.open_positions.values()),
        "total":           risk.get("total_trades", len(ex.trade_log)),
        "wins":            risk.get("wins", ex.win_streak),
        "win_rate":        risk.get("win_rate", ex.winrate_10),
        "win_rate_10":     ex.winrate_10,
        "daily_pnl":       ex.daily_pnl,
        "win_streak":      ex.win_streak,
        "loss_streak":     ex.loss_streak,
        "best_strategy":   ex.best_strategy,
        "strategy_stats":  ex.strategy_stats,
    }

# Backward compat aliases
start_deriv_bot = start_multi_bot
stop_deriv_bot  = stop_single_bot
stop_all_deriv  = stop_all_bots
get_deriv_status = get_multi_status
get_deriv_trades = get_multi_trades
