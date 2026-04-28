"""deriv_ws.py — Deriv WebSocket client for SocialGuard PRO"""

import asyncio, json, time, websockets
from datetime import datetime
from typing import Callable, Optional

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3"

class DerivWS:
    def __init__(self, app_id: str, token: str):
        self.app_id   = app_id
        self.token    = token
        self.ws       = None
        self.running  = False
        self.tick_cbs : list[Callable] = []
        self.bal_cbs  : list[Callable] = []
        self.trade_cbs: list[Callable] = []
        self.ticks: dict[str, list]    = {}   # symbol → [tick, ...]
        self.balance: float            = 0.0
        self.currency: str             = "USD"
        self._pending: dict            = {}
        self._req_id: int              = 1

    # ── public ──────────────────────────────────────────
    def on_tick(self, cb: Callable):    self.tick_cbs.append(cb)
    def on_balance(self, cb: Callable): self.bal_cbs.append(cb)
    def on_trade(self, cb: Callable):   self.trade_cbs.append(cb)

    async def connect(self):
        url = f"{DERIV_WS_URL}?app_id={self.app_id}"
        self.ws      = await websockets.connect(url, ping_interval=20, ping_timeout=10)
        self.running = True
        await self._authorize()
        asyncio.create_task(self._listen())
        print(f"[DerivWS] Connected — app_id={self.app_id}")

    async def disconnect(self):
        self.running = False
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def subscribe_ticks(self, symbol: str):
        await self._send({"ticks": symbol, "subscribe": 1})
        self.ticks.setdefault(symbol, [])

    async def subscribe_balance(self):
        await self._send({"balance": 1, "subscribe": 1})

    async def buy_contract(self, symbol: str, contract_type: str,
                           duration: int, amount: float) -> dict:
        req = {
            "buy": 1,
            "price": amount,
            "parameters": {
                "contract_type": contract_type,   # CALL / PUT
                "symbol": symbol,
                "duration": duration,
                "duration_unit": "m",
                "basis": "stake",
                "currency": self.currency,
            }
        }
        rid = self._next_rid()
        req["req_id"] = rid
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send_raw(req)
        try:
            result = await asyncio.wait_for(fut, timeout=15)
            return result
        except asyncio.TimeoutError:
            return {"error": "timeout"}

    # ── private ─────────────────────────────────────────
    async def _authorize(self):
        await self._send({"authorize": self.token})

    async def _send(self, payload: dict):
        if self.ws:
            await self.ws.send(json.dumps(payload))

    async def _send_raw(self, payload: dict):
        if self.ws:
            await self.ws.send(json.dumps(payload))

    def _next_rid(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _listen(self):
        try:
            async for raw in self.ws:
                if not self.running:
                    break
                try:
                    msg = json.loads(raw)
                    await self._handle(msg)
                except Exception as e:
                    print(f"[DerivWS] Parse error: {e}")
        except websockets.ConnectionClosed:
            print("[DerivWS] Connection closed")
            self.running = False
        except Exception as e:
            print(f"[DerivWS] Listen error: {e}")
            self.running = False

    async def _handle(self, msg: dict):
        rid = msg.get("req_id")
        if rid and rid in self._pending:
            self._pending.pop(rid).set_result(msg)
            return

        mtype = msg.get("msg_type", "")

        if mtype == "authorize":
            d = msg.get("authorize", {})
            self.balance  = float(d.get("balance", 0))
            self.currency = d.get("currency", "USD")
            print(f"[DerivWS] Authorized — balance={self.balance} {self.currency}")

        elif mtype == "tick":
            t = msg["tick"]
            sym   = t["symbol"]
            price = float(t["quote"])
            epoch = t["epoch"]
            tick_obj = {"price": price, "epoch": epoch, "symbol": sym,
                        "time": datetime.fromtimestamp(epoch).strftime("%H:%M:%S")}
            lst = self.ticks.setdefault(sym, [])
            lst.append(price)
            if len(lst) > 200:
                lst.pop(0)
            for cb in self.tick_cbs:
                asyncio.create_task(cb(tick_obj))

        elif mtype == "balance":
            b = msg["balance"]
            self.balance = float(b.get("balance", self.balance))
            for cb in self.bal_cbs:
                asyncio.create_task(cb({"balance": self.balance, "currency": self.currency}))

        elif mtype == "buy":
            result = msg.get("buy", {})
            for cb in self.trade_cbs:
                asyncio.create_task(cb({"type": "buy", "data": result}))

        elif mtype == "proposal_open_contract":
            poc = msg.get("proposal_open_contract", {})
            if poc.get("is_sold"):
                for cb in self.trade_cbs:
                    asyncio.create_task(cb({"type": "settled", "data": poc}))
