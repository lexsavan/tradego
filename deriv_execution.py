"""deriv_execution.py — Real execution engine for SocialGuard PRO
   Handles: open positions, PnL tracking, win/loss streak,
   AI feedback loop, self-learning, debug mode, safety rules
"""

import asyncio, json, time, os
from datetime import datetime, date
from typing import Optional
from collections import deque

# ── State ────────────────────────────────────────────────────────────────────

class ExecutionState:
    def __init__(self):
        # Open positions
        self.open_positions: dict = {}          # contract_id → position
        # Trade log
        self.trade_log: list = []               # last 200 trades
        # Streak tracking
        self.win_streak:  int = 0
        self.loss_streak: int = 0
        # Daily safety
        self.daily_start_balance: float = 0
        self.daily_date: str = ""
        self.daily_trades: int = 0
        self.daily_losses: int = 0
        self.daily_pnl: float = 0
        # Strategy performance (last 10 trades per strategy)
        self.strategy_stats: dict = {
            "rise_fall":   {"wins": 0, "total": 0, "pnl": 0},
            "momentum":    {"wins": 0, "total": 0, "pnl": 0},
            "reversal":    {"wins": 0, "total": 0, "pnl": 0},
        }
        # AI feedback loop
        self.last_10_trades: deque = deque(maxlen=10)
        self.winrate_10: float = 0.0
        # Self-learning
        self.strategy_scores: dict = {}
        self.best_strategy: str = "rise_fall"
        self.last_backtest: float = 0
        # Debug mode
        self.debug_mode: bool = False
        self.debug_confidence_threshold: int = 50
        # Tick stream
        self.tick_stream: deque = deque(maxlen=20)
        # Bot status
        self.bot_status: str = "WAITING"   # WAITING / RUNNING / TRADING
        self.last_trade_time: Optional[str] = None
        self.last_signal: str = "WAIT"
        self.cooldown_until: float = 0
        # Safety
        self.stopped_today: bool = False
        self.stop_reason: str = ""

    def init_day(self, balance: float):
        today = date.today().isoformat()
        if today != self.daily_date:
            self.daily_date          = today
            self.daily_start_balance = balance
            self.daily_trades        = 0
            self.daily_losses        = 0
            self.daily_pnl           = 0
            self.stopped_today       = False
            self.stop_reason         = ""

    def add_tick(self, price: float):
        self.tick_stream.append({"price": price, "time": datetime.now().strftime("%H:%M:%S")})

    def tick_momentum(self) -> float:
        ticks = list(self.tick_stream)
        if len(ticks) < 5: return 0.5
        ups = sum(1 for i in range(1, len(ticks)) if ticks[i]["price"] > ticks[i-1]["price"])
        return round(ups / (len(ticks) - 1), 3)

    def record_open(self, contract_id: str, signal: str, stake: float,
                    symbol: str, strategy: str):
        self.open_positions[contract_id] = {
            "contract_id": contract_id,
            "signal":      signal,
            "stake":       stake,
            "symbol":      symbol,
            "strategy":    strategy,
            "opened_at":   datetime.now().isoformat(),
            "status":      "open",
        }
        self.bot_status = "TRADING"
        self.daily_trades += 1

    def record_close(self, contract_id: str, won: bool, pnl: float, balance: float):
        pos = self.open_positions.pop(contract_id, {})
        strategy = pos.get("strategy", "rise_fall")
        trade = {
            **pos,
            "won":       won,
            "pnl":       round(pnl, 4),
            "balance":   round(balance, 2),
            "closed_at": datetime.now().isoformat(),
            "status":    "closed",
        }
        self.trade_log.insert(0, trade)
        if len(self.trade_log) > 200: self.trade_log.pop()

        # Streak
        if won:
            self.win_streak  += 1
            self.loss_streak  = 0
        else:
            self.loss_streak += 1
            self.win_streak   = 0
            self.daily_losses += 1

        # Daily PnL
        self.daily_pnl = round(self.daily_pnl + pnl, 4)

        # Strategy stats
        if strategy in self.strategy_stats:
            s = self.strategy_stats[strategy]
            s["total"] += 1
            if won: s["wins"] += 1
            s["pnl"] = round(s.get("pnl", 0) + pnl, 4)

        # AI feedback loop
        self.last_10_trades.append({"won": won, "pnl": pnl, "strategy": strategy})
        wins10 = sum(1 for t in self.last_10_trades if t["won"])
        self.winrate_10 = round(wins10 / len(self.last_10_trades) * 100, 1)

        # Update best strategy
        self._update_best_strategy()

        # Bot status
        self.bot_status = "RUNNING" if not self.open_positions else "TRADING"
        self.last_trade_time = datetime.now().strftime("%H:%M:%S")
        self.cooldown_until  = time.time() + 120   # 120s cooldown

        return trade

    def _update_best_strategy(self):
        scores = {}
        for name, s in self.strategy_stats.items():
            if s["total"] == 0: continue
            wr    = s["wins"] / s["total"]
            score = wr * 0.6 + (s["pnl"] / max(s["total"], 1)) * 0.4
            scores[name] = round(score, 4)
        self.strategy_scores = scores
        if scores:
            self.best_strategy = max(scores, key=scores.get)

    def can_trade(self, confidence: int) -> tuple[bool, str]:
        if self.stopped_today:
            return False, f"Stopped: {self.stop_reason}"
        if time.time() < self.cooldown_until:
            rem = int(self.cooldown_until - time.time())
            return False, f"Cooldown {rem}s"
        if self.loss_streak >= 3:
            self.stopped_today = True
            self.stop_reason   = "3 consecutive losses"
            return False, self.stop_reason
        if self.daily_start_balance > 0:
            loss_pct = abs(self.daily_pnl) / self.daily_start_balance * 100
            if self.daily_pnl < 0 and loss_pct >= 10:
                self.stopped_today = True
                self.stop_reason   = f"Daily loss limit 10% ({loss_pct:.1f}%)"
                return False, self.stop_reason
        threshold = self.debug_confidence_threshold if self.debug_mode else 80
        if confidence < threshold:
            return False, f"Confidence {confidence}% < {threshold}%"
        return True, "OK"

    def status_dict(self) -> dict:
        return {
            "bot_status":      self.bot_status,
            "last_signal":     self.last_signal,
            "last_trade_time": self.last_trade_time,
            "open_positions":  list(self.open_positions.values()),
            "win_streak":      self.win_streak,
            "loss_streak":     self.loss_streak,
            "winrate_10":      self.winrate_10,
            "daily_trades":    self.daily_trades,
            "daily_losses":    self.daily_losses,
            "daily_pnl":       round(self.daily_pnl, 4),
            "stopped_today":   self.stopped_today,
            "stop_reason":     self.stop_reason,
            "debug_mode":      self.debug_mode,
            "best_strategy":   self.best_strategy,
            "strategy_scores": self.strategy_scores,
            "strategy_stats":  self.strategy_stats,
            "tick_momentum":   self.tick_momentum(),
            "last_ticks":      list(self.tick_stream)[-20:],
            "recent_trades":   self.trade_log[:20],
            "cooldown_remaining": max(0, int(self.cooldown_until - time.time())),
        }

# Singleton
exec_state = ExecutionState()
