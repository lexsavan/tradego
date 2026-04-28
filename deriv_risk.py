"""deriv_risk.py — Risk management for Deriv synthetic trading"""

import time
from datetime import datetime, date

class DerivRiskManager:
    def __init__(self,
                 daily_loss_limit_pct: float = 10.0,
                 max_trades_per_hour: int = 8,
                 drawdown_stop_pct: float = 15.0,
                 max_consecutive_losses: int = 3,
                 cooldown_seconds: int = 120,
                 min_confidence: int = 80):
        self.daily_loss_limit_pct    = daily_loss_limit_pct
        self.max_trades_per_hour     = max_trades_per_hour
        self.drawdown_stop_pct       = drawdown_stop_pct
        self.max_consecutive_losses  = max_consecutive_losses
        self.cooldown_seconds        = cooldown_seconds
        self.min_confidence          = min_confidence

        # Runtime state
        self.start_balance: float    = 0.0
        self.peak_balance: float     = 0.0
        self.current_balance: float  = 0.0
        self.daily_start_balance: float = 0.0
        self.last_trade_date: str    = ""

        self.consecutive_losses: int = 0
        self.last_loss_time: float   = 0.0
        self.trade_times: list       = []   # epoch list for this hour
        self.trade_history: list     = []   # all trades

        self.stopped: bool           = False
        self.stop_reason: str        = ""

    # ── Initialise ─────────────────────────────────────────────────
    def set_balance(self, balance: float):
        if self.start_balance == 0:
            self.start_balance       = balance
            self.peak_balance        = balance
            self.daily_start_balance = balance
        self.current_balance = balance
        if balance > self.peak_balance:
            self.peak_balance = balance
        today = date.today().isoformat()
        if today != self.last_trade_date:
            self.daily_start_balance = balance
            self.last_trade_date     = today

    # ── Checks ─────────────────────────────────────────────────────
    def can_trade(self, confidence: int, signal: str) -> tuple[bool, str]:
        if self.stopped:
            return False, f"Bot stopped: {self.stop_reason}"

        if signal == "WAIT":
            return False, "Signal is WAIT"

        if confidence < self.min_confidence:
            return False, f"Confidence {confidence}% < {self.min_confidence}% threshold"

        # Cooldown after loss
        if self.last_loss_time > 0:
            elapsed = time.time() - self.last_loss_time
            if elapsed < self.cooldown_seconds:
                remaining = int(self.cooldown_seconds - elapsed)
                return False, f"Cooldown {remaining}s remaining after loss"

        # Consecutive loss limit
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.stopped     = True
            self.stop_reason = f"Hit {self.max_consecutive_losses} consecutive losses"
            return False, self.stop_reason

        # Daily loss limit
        if self.daily_start_balance > 0:
            daily_loss_pct = (self.daily_start_balance - self.current_balance) / self.daily_start_balance * 100
            if daily_loss_pct >= self.daily_loss_limit_pct:
                self.stopped     = True
                self.stop_reason = f"Daily loss limit {self.daily_loss_limit_pct}% reached ({daily_loss_pct:.1f}%)"
                return False, self.stop_reason

        # Drawdown limit
        if self.peak_balance > 0:
            drawdown_pct = (self.peak_balance - self.current_balance) / self.peak_balance * 100
            if drawdown_pct >= self.drawdown_stop_pct:
                self.stopped     = True
                self.stop_reason = f"Drawdown {drawdown_pct:.1f}% >= {self.drawdown_stop_pct}% limit"
                return False, self.stop_reason

        # Trades per hour
        now = time.time()
        self.trade_times = [t for t in self.trade_times if now - t < 3600]
        if len(self.trade_times) >= self.max_trades_per_hour:
            return False, f"Max {self.max_trades_per_hour} trades/hour reached"

        return True, "OK"

    def stake_amount(self, pct: float = 2.0) -> float:
        """Calculate stake — reduce after losses, never martingale"""
        base = round(self.current_balance * pct / 100, 2)
        # Reduce by 25% for each consecutive loss (anti-martingale)
        factor = max(0.25, 1.0 - self.consecutive_losses * 0.25)
        stake  = round(base * factor, 2)
        return max(stake, 1.0)   # minimum $1

    # ── Record result ───────────────────────────────────────────────
    def record_trade(self, won: bool, pnl: float, stake: float, symbol: str, signal: str):
        now = time.time()
        self.trade_times.append(now)
        entry = {
            "time":      datetime.now().strftime("%H:%M:%S"),
            "symbol":    symbol,
            "signal":    signal,
            "stake":     stake,
            "pnl":       round(pnl, 4),
            "won":       won,
            "balance":   self.current_balance,
        }
        self.trade_history.insert(0, entry)
        if len(self.trade_history) > 200:
            self.trade_history.pop()

        if won:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.last_loss_time      = now

    # ── Status ─────────────────────────────────────────────────────
    def status(self) -> dict:
        now = time.time()
        self.trade_times = [t for t in self.trade_times if now - t < 3600]

        daily_pnl = round(self.current_balance - self.daily_start_balance, 4) if self.daily_start_balance else 0
        daily_pct = round(daily_pnl / self.daily_start_balance * 100, 2) if self.daily_start_balance else 0
        dd_pct    = round((self.peak_balance - self.current_balance) / self.peak_balance * 100, 2) if self.peak_balance else 0

        wins  = sum(1 for t in self.trade_history if t["won"])
        total = len(self.trade_history)
        win_rate = round(wins / total * 100, 1) if total > 0 else 0

        cooldown_rem = 0
        if self.last_loss_time > 0:
            elapsed      = time.time() - self.last_loss_time
            cooldown_rem = max(0, int(self.cooldown_seconds - elapsed))

        return {
            "balance":            round(self.current_balance, 2),
            "peak_balance":       round(self.peak_balance, 2),
            "daily_pnl":          daily_pnl,
            "daily_pnl_pct":      daily_pct,
            "drawdown_pct":       dd_pct,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_remaining": cooldown_rem,
            "trades_this_hour":   len(self.trade_times),
            "total_trades":       total,
            "wins":               wins,
            "win_rate":           win_rate,
            "stopped":            self.stopped,
            "stop_reason":        self.stop_reason,
            "recent_trades":      self.trade_history[:20],
        }

    def reset_stop(self):
        """Manual override to resume after review"""
        self.stopped          = False
        self.stop_reason      = ""
        self.consecutive_losses = 0
