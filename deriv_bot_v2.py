"""deriv_bot_v2.py — Upgraded bot with real execution + feedback loop"""

import asyncio, os, time
from datetime import datetime
from typing import Optional

from deriv_ws       import DerivWS
from deriv_ai       import get_signal, compute_indicators
from deriv_risk     import DerivRiskManager
from deriv_execution import exec_state, ExecutionState

SYMBOLS = {"V75":"R_75","V100":"R_100","V25":"R_25","V50":"R_50"}

deriv_bots:    dict = {}
deriv_risk:    Optional[DerivRiskManager] = None
_ws_instance:  Optional[DerivWS] = None
_bot_task:     Optional[asyncio.Task] = None
_last_signal:  dict = {}

async def start_deriv_bot(symbol_key: str, app_id: str, token: str,
                          amount_pct: float = 2.0, duration: int = 1,
                          risk_level: str = "medium",
                          account_type: str = "demo") -> dict:
    global _ws_instance, deriv_risk, _bot_task

    if symbol_key in deriv_bots and deriv_bots[symbol_key].get("status") == "running":
        return {"error": f"Bot {symbol_key} already running"}

    sym = SYMBOLS.get(symbol_key)
    if not sym:
        return {"error": f"Unknown symbol {symbol_key}"}

    # Risk manager
    if deriv_risk is None:
        cfg = {
            "low":    {"daily_loss_limit_pct":5,  "max_trades_per_hour":6,  "drawdown_stop_pct":10,"min_confidence":82},
            "medium": {"daily_loss_limit_pct":10, "max_trades_per_hour":8,  "drawdown_stop_pct":15,"min_confidence":80},
            "high":   {"daily_loss_limit_pct":15, "max_trades_per_hour":10, "drawdown_stop_pct":20,"min_confidence":78},
        }.get(risk_level, {"daily_loss_limit_pct":10,"max_trades_per_hour":8,"drawdown_stop_pct":15,"min_confidence":80})
        deriv_risk = DerivRiskManager(**cfg)

    # WebSocket
    if _ws_instance is None or not _ws_instance.running:
        _ws_instance = DerivWS(app_id=app_id, token=token)
        try:
            await _ws_instance.connect()
        except Exception as e:
            return {"error": f"WS failed: {e}"}
        _ws_instance.on_balance(_on_balance)
        _ws_instance.on_trade(_on_trade_settled)
        await _ws_instance.subscribe_balance()

    await _ws_instance.subscribe_ticks(sym)

    # Register tick callback for exec_state
    _ws_instance.on_tick(_on_tick)

    deriv_bots[symbol_key] = {
        "symbol_key":   symbol_key, "symbol": sym,
        "status":       "running",  "amount_pct": amount_pct,
        "duration":     duration,   "risk_level": risk_level,
        "account_type": account_type,
        "started":      datetime.now().isoformat(),
        "last_signal":  {}, "active_trade": None, "trades_count": 0,
    }
    exec_state.bot_status = "RUNNING"

    _bot_task = asyncio.create_task(_trading_loop(symbol_key, sym, amount_pct, duration))
    return {"status": "started", "symbol": sym, "symbol_key": symbol_key, "account_type": account_type}

async def stop_deriv_bot(symbol_key: str) -> dict:
    global _bot_task
    if symbol_key not in deriv_bots:
        return {"error": "Bot not found"}
    deriv_bots[symbol_key]["status"] = "stopped"
    exec_state.bot_status = "WAITING"
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
    return {"status": "stopped", "symbol_key": symbol_key}

async def stop_all_deriv() -> dict:
    global _ws_instance, _bot_task
    for k in list(deriv_bots.keys()):
        deriv_bots[k]["status"] = "stopped"
    exec_state.bot_status = "WAITING"
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
    if _ws_instance:
        await _ws_instance.disconnect()
        _ws_instance = None
    return {"status": "all stopped"}

async def _trading_loop(symbol_key: str, symbol: str, amount_pct: float, duration: int):
    print(f"[BotV2] Loop started — {symbol_key}")
    SCAN = 15   # seconds between AI scans

    try:
        while deriv_bots.get(symbol_key, {}).get("status") == "running":
            await asyncio.sleep(SCAN)

            balance = _ws_instance.balance if _ws_instance else 0
            exec_state.init_day(balance)
            if deriv_risk: deriv_risk.set_balance(balance)

            # Safety check
            if exec_state.stopped_today:
                print(f"[BotV2] Stopped today: {exec_state.stop_reason}")
                deriv_bots[symbol_key]["status"] = "stopped_safety"
                break

            prices = _ws_instance.ticks.get(symbol, []) if _ws_instance else []
            if len(prices) < 25:
                print(f"[BotV2] Waiting ticks ({len(prices)}/25)")
                continue

            # AI signal with best strategy context
            signal_data = await get_signal(symbol, prices, balance, use_consensus=True)
            sig  = signal_data.get("signal", "WAIT")
            conf = signal_data.get("confidence", 0)
            exec_state.last_signal = sig
            _last_signal[symbol_key] = signal_data
            deriv_bots[symbol_key]["last_signal"] = signal_data

            # AI feedback: adjust confidence if winrate low
            if exec_state.winrate_10 < 40 and len(exec_state.last_10_trades) >= 5:
                conf = max(0, conf - 10)   # reduce confidence if losing
                print(f"[BotV2] Winrate low ({exec_state.winrate_10}%) — confidence adjusted to {conf}%")

            print(f"[BotV2] {symbol_key} Signal={sig} Conf={conf}% Winrate10={exec_state.winrate_10}%")

            # Can trade?
            ok, reason = exec_state.can_trade(conf)
            if not ok:
                print(f"[BotV2] Block: {reason}")
                exec_state.bot_status = "WAITING"
                continue

            # Also check risk manager
            if deriv_risk:
                risk_ok, risk_reason = deriv_risk.can_trade(conf, sig)
                if not risk_ok:
                    print(f"[BotV2] Risk block: {risk_reason}")
                    continue

            # Determine strategy
            ind      = signal_data.get("indicators", {})
            strategy = _pick_strategy(ind, exec_state)

            # Stake (reduce after losses)
            if deriv_risk:
                stake = deriv_risk.stake_amount(amount_pct)
            else:
                factor = max(0.5, 1.0 - exec_state.loss_streak * 0.2)
                stake  = round(balance * amount_pct / 100 * factor, 2)
            stake = max(stake, 1.0)

            contract_type = "CALL" if sig == "BUY" else "PUT"
            exec_state.bot_status = "TRADING"
            deriv_bots[symbol_key]["active_trade"] = {
                "signal": sig, "stake": stake, "strategy": strategy,
                "time": datetime.now().strftime("%H:%M:%S"),
                "duration": duration, "contract_type": contract_type,
            }

            print(f"[BotV2] Placing {contract_type} | stake={stake} | strategy={strategy}")
            result = await _ws_instance.buy_contract(symbol, contract_type, duration, stake)

            if "error" in result:
                print(f"[BotV2] Trade error: {result.get('error')}")
                deriv_bots[symbol_key]["active_trade"] = None
                exec_state.bot_status = "RUNNING"
                continue

            # Record open position
            buy_data    = result.get("buy", {})
            contract_id = str(buy_data.get("contract_id", f"sim_{time.time()}"))
            exec_state.record_open(contract_id, sig, stake, symbol, strategy)
            if deriv_risk: deriv_risk.trade_times.append(time.time())

            deriv_bots[symbol_key]["trades_count"] = deriv_bots[symbol_key].get("trades_count", 0) + 1

            # Wait for settlement
            await asyncio.sleep(duration * 60 + 10)
            deriv_bots[symbol_key]["active_trade"] = None
            exec_state.bot_status = "RUNNING"

    except asyncio.CancelledError:
        print(f"[BotV2] {symbol_key} cancelled")
    except Exception as e:
        import traceback
        print(f"[BotV2] Error: {e}\n{traceback.format_exc()}")
        if symbol_key in deriv_bots:
            deriv_bots[symbol_key]["status"] = "error"
            deriv_bots[symbol_key]["error"]   = str(e)
        exec_state.bot_status = "WAITING"

def _pick_strategy(ind: dict, state: ExecutionState) -> str:
    """Pick best strategy based on indicators + performance"""
    rsi   = ind.get("rsi", 50)
    noise = ind.get("noise", 0.5)
    trend = ind.get("trend", "sideways")
    mom   = state.tick_momentum()
    # If we have enough data, use best performing strategy
    if sum(s["total"] for s in state.strategy_stats.values()) >= 10:
        return state.best_strategy
    # Otherwise: rule-based
    if rsi < 35 and mom > 0.6: return "reversal"
    if noise < 0.4 and trend != "sideways": return "momentum"
    return "rise_fall"

async def _on_tick(data: dict):
    exec_state.add_tick(data["price"])

async def _on_balance(data: dict):
    if deriv_risk: deriv_risk.set_balance(data["balance"])

async def _on_trade_settled(data: dict):
    if data.get("type") != "settled": return
    poc         = data["data"]
    won         = float(poc.get("profit", 0)) > 0
    pnl         = float(poc.get("profit", 0))
    stake       = float(poc.get("buy_price", 0))
    contract_id = str(poc.get("contract_id", ""))
    balance     = _ws_instance.balance if _ws_instance else 0

    trade = exec_state.record_close(contract_id, won, pnl, balance)
    if deriv_risk: deriv_risk.record_trade(won, pnl, stake, trade.get("symbol",""), trade.get("signal",""))
    print(f"[BotV2] {'✅ WIN' if won else '❌ LOSS'} | PnL={pnl:.2f} | Streak L={exec_state.loss_streak} W={exec_state.win_streak}")

    # Hourly auto-backtest (score strategies)
    if time.time() - exec_state.last_backtest > 3600:
        exec_state._update_best_strategy()
        exec_state.last_backtest = time.time()
        print(f"[BotV2] Strategy scores: {exec_state.strategy_scores} | Best: {exec_state.best_strategy}")

def get_deriv_status() -> dict:
    ws_ok = _ws_instance is not None and _ws_instance.running
    bots_list = []
    for k, b in deriv_bots.items():
        ticks = len(_ws_instance.ticks.get(b["symbol"], [])) if _ws_instance else 0
        bots_list.append({**b, "ticks_collected": ticks})
    return {
        "ws_connected":  ws_ok,
        "balance":       _ws_instance.balance if _ws_instance else 0,
        "currency":      _ws_instance.currency if _ws_instance else "USD",
        "bots":          bots_list,
        "last_signals":  _last_signal,
        "risk":          deriv_risk.status() if deriv_risk else {},
        "execution":     exec_state.status_dict(),
        "timestamp":     datetime.now().isoformat(),
    }

def get_deriv_trades() -> dict:
    risk = deriv_risk.status() if deriv_risk else {}
    ex   = exec_state
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
