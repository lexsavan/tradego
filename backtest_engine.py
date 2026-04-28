# ══════════════════════════════════════════════════════
#  BACKTESTING ENGINE — ເພີ່ມໃສ່ server_pro.py
#  ວາງຫຼັງ class RebalanceReq ແລະ ກ່ອນ fetch_market()
# ══════════════════════════════════════════════════════

# pip install pandas numpy  (ເພີ່ມໃສ່ requirements.txt)

class BacktestReq(BaseModel):
    symbol:      str   = "DGBUSDT"
    strategy:    str   = "grid"      # grid / dca / combo
    capitalUSDT: float = 200.0
    gridLines:   int   = 12
    rangeDown:   float = 6.0
    rangeUp:     float = 6.0
    tpPct:       float = 0.6
    slPct:       float = 9.0
    dcaDropPct:  float = 2.0
    dcaMulti:    float = 1.5
    dcaLevels:   int   = 3
    interval:    str   = "1h"        # 1h / 4h / 1d
    days:        int   = 30          # 7 / 14 / 30 / 90

# ── Fetch historical klines ──
async def fetch_klines(symbol: str, interval: str, days: int) -> list:
    limit = min(1000, days * {"1h":24,"4h":6,"1d":1}.get(interval,24))
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit}
        )
        klines = r.json()
        return [{
            "ts":    k[0],
            "open":  float(k[1]),
            "high":  float(k[2]),
            "low":   float(k[3]),
            "close": float(k[4]),
            "vol":   float(k[5]),
        } for k in klines]

# ── Grid Backtest Engine ──
def backtest_grid(klines, capital, grid_lines, range_down, range_up, tp_pct, sl_pct):
    if not klines: return None

    start_price = klines[0]["close"]
    low_price   = start_price * (1 - range_down/100)
    high_price  = start_price * (1 + range_up/100)
    step        = (high_price - low_price) / grid_lines
    qty_per     = (capital / grid_lines) / start_price

    balance     = capital
    holdings    = 0.0
    trades      = []
    equity_curve= []
    wins = losses = 0
    max_dd = 0; peak = capital

    # Build grid levels
    grid_levels = [low_price + step*i for i in range(grid_lines)]
    filled_buys = {}  # price -> qty

    for i, bar in enumerate(klines):
        hi = bar["high"]; lo = bar["low"]; close = bar["close"]

        # Stop loss check
        if holdings > 0:
            entry_avg = sum(p*q for p,q in filled_buys.items()) / (sum(filled_buys.values()) or 1)
            if close < entry_avg * (1 - sl_pct/100):
                pnl = (close - entry_avg) * holdings
                balance += holdings * close
                trades.append({"type":"SL","price":close,"pnl":round(pnl,4),"bar":i})
                losses += 1
                holdings = 0; filled_buys = {}

        # Check grid orders
        for lvl in grid_levels:
            # BUY: price dips to grid level
            if lo <= lvl and lvl not in filled_buys and balance >= lvl * qty_per:
                filled_buys[lvl] = qty_per
                balance -= lvl * qty_per
                holdings += qty_per

            # SELL: price rises to TP above grid level
            tp_price = lvl * (1 + tp_pct/100)
            if lvl in filled_buys and hi >= tp_price:
                qty  = filled_buys.pop(lvl)
                pnl  = (tp_price - lvl) * qty
                balance += tp_price * qty
                holdings -= qty
                trades.append({"type":"TP","price":tp_price,"pnl":round(pnl,4),"bar":i})
                wins += 1

        # Equity curve
        total = balance + holdings * close
        equity_curve.append(round(total, 4))
        if total > peak: peak = total
        dd = (peak - total) / peak * 100
        if dd > max_dd: max_dd = dd

    final_equity = balance + holdings * klines[-1]["close"]
    total_pnl    = final_equity - capital
    total_pnl_pct= total_pnl / capital * 100
    win_rate     = wins / (wins+losses) * 100 if (wins+losses) > 0 else 0
    days_count   = len(klines) / 24 if len(klines) > 24 else 1
    daily_return = total_pnl_pct / days_count

    return {
        "strategy":     "Grid",
        "symbol":       "",
        "startPrice":   round(start_price, 6),
        "endPrice":     round(klines[-1]["close"], 6),
        "priceChange":  round((klines[-1]["close"]-start_price)/start_price*100, 2),
        "capital":      capital,
        "finalEquity":  round(final_equity, 4),
        "totalPnl":     round(total_pnl, 4),
        "totalPnlPct":  round(total_pnl_pct, 2),
        "dailyReturn":  round(daily_return, 3),
        "totalTrades":  len(trades),
        "wins":         wins,
        "losses":       losses,
        "winRate":      round(win_rate, 1),
        "maxDrawdown":  round(max_dd, 2),
        "equityCurve":  equity_curve[::max(1,len(equity_curve)//100)],  # downsample
        "recentTrades": trades[-20:],
        "bars":         len(klines),
    }

# ── DCA Backtest Engine ──
def backtest_dca(klines, capital, dca_drop, dca_multi, dca_levels, tp_pct):
    if not klines: return None

    balance  = capital
    position = []  # list of {price, qty}
    trades   = []
    equity_curve = []
    wins = losses = 0
    max_dd = 0; peak = capital
    base_qty = (capital * 0.2) / klines[0]["close"]  # 20% per entry

    ref_price = klines[0]["close"]

    for i, bar in enumerate(klines):
        close = bar["close"]; lo = bar["low"]; hi = bar["high"]

        # Check DCA buy triggers
        if len(position) < dca_levels:
            drop_needed = ref_price * (1 - dca_drop/100 * (len(position)+1))
            qty = base_qty * (dca_multi ** len(position))
            if lo <= drop_needed and balance >= drop_needed * qty:
                position.append({"price": drop_needed, "qty": qty})
                balance -= drop_needed * qty
                trades.append({"type":"DCA BUY","price":round(drop_needed,6),"bar":i,"pnl":0})

        # Check TP
        if position:
            avg = sum(p["price"]*p["qty"] for p in position) / sum(p["qty"] for p in position)
            tp  = avg * (1 + tp_pct/100)
            if hi >= tp:
                total_qty = sum(p["qty"] for p in position)
                pnl = (tp - avg) * total_qty
                balance += tp * total_qty
                trades.append({"type":"TP","price":round(tp,6),"pnl":round(pnl,4),"bar":i})
                wins += 1
                position = []
                ref_price = close  # reset reference

        holdings = sum(p["qty"] for p in position)
        total    = balance + holdings * close
        equity_curve.append(round(total, 4))
        if total > peak: peak = total
        dd = (peak-total)/peak*100
        if dd > max_dd: max_dd = dd

    holdings     = sum(p["qty"] for p in position)
    final_equity = balance + holdings * klines[-1]["close"]
    total_pnl    = final_equity - capital
    days_count   = len(klines)/24 if len(klines)>24 else 1

    return {
        "strategy":    "DCA",
        "capital":     capital,
        "finalEquity": round(final_equity,4),
        "totalPnl":    round(total_pnl,4),
        "totalPnlPct": round(total_pnl/capital*100,2),
        "dailyReturn": round(total_pnl/capital*100/days_count,3),
        "totalTrades": len(trades),
        "wins":        wins,
        "winRate":     round(wins/max(len(trades),1)*100,1),
        "maxDrawdown": round(max_dd,2),
        "equityCurve": equity_curve[::max(1,len(equity_curve)//100)],
        "recentTrades":trades[-20:],
        "bars":        len(klines),
    }

# ══════════════════════════════════════════════════════
#  ADD THIS ROUTE TO server_pro.py
# ══════════════════════════════════════════════════════

"""
@app.post("/api/backtest")
async def backtest(req: BacktestReq, request: Request):
    require_auth(request)
    try:
        klines = await fetch_klines(req.symbol, req.interval, req.days)
        if not klines:
            raise HTTPException(400, "No data from Binance")

        if req.strategy == "dca":
            result = backtest_dca(
                klines, req.capitalUSDT,
                req.dcaDropPct, req.dcaMulti, req.dcaLevels, req.tpPct
            )
        elif req.strategy == "combo":
            r1 = backtest_grid(klines, req.capitalUSDT*0.7, req.gridLines,
                               req.rangeDown, req.rangeUp, req.tpPct, req.slPct)
            r2 = backtest_dca(klines, req.capitalUSDT*0.3,
                              req.dcaDropPct, req.dcaMulti, req.dcaLevels, req.tpPct)
            # Merge results
            result = {
                "strategy": "Combo (Grid+DCA)",
                "capital":  req.capitalUSDT,
                "finalEquity": round((r1["finalEquity"] + r2["finalEquity"]), 4),
                "totalPnl":    round((r1["totalPnl"] + r2["totalPnl"]), 4),
                "totalPnlPct": round(((r1["totalPnl"]+r2["totalPnl"])/req.capitalUSDT*100), 2),
                "dailyReturn": round(((r1["dailyReturn"]+r2["dailyReturn"])/2), 3),
                "totalTrades": r1["totalTrades"] + r2["totalTrades"],
                "wins":        r1["wins"] + r2["wins"],
                "winRate":     round((r1["winRate"]+r2["winRate"])/2, 1),
                "maxDrawdown": round(max(r1["maxDrawdown"],r2["maxDrawdown"]), 2),
                "equityCurve": [a+b-req.capitalUSDT
                                for a,b in zip(r1["equityCurve"],r2["equityCurve"])],
                "recentTrades":sorted(r1["recentTrades"]+r2["recentTrades"],
                                     key=lambda x:x["bar"])[-20:],
                "bars":        r1["bars"],
            }
        else:
            result = backtest_grid(
                klines, req.capitalUSDT, req.gridLines,
                req.rangeDown, req.rangeUp, req.tpPct, req.slPct
            )

        result["symbol"]   = req.symbol
        result["interval"] = req.interval
        result["days"]     = req.days
        result["timestamp"]= datetime.now().isoformat()
        return result

    except Exception as e:
        raise HTTPException(500, str(e))
"""

print("Backtest engine ready — add route to server_pro.py")
