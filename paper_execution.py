from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

from config import TRADING_MODE


class PaperExecution:
    def __init__(self, wallet, market_data):
        self.wallet = wallet
        self.market_data = market_data
        self._orders = {}
        self._lock = threading.Lock()

    def _assert_paper(self):
        if TRADING_MODE != "paper":
            raise RuntimeError("Real order execution is disabled. Set TRADING_MODE='paper'.")

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        return symbol.replace("_", "/")

    def _best_prices(self, symbol: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
        symbol = self._norm_symbol(symbol)
        bid = self.market_data.get_bid_price(symbol) if self.market_data else None
        ask = self.market_data.get_ask_price(symbol) if self.market_data else None
        last = self.market_data.get_last_price(symbol) if self.market_data else None
        return bid, ask, last

    def fetch_ticker(self, symbol: str) -> dict:
        bid, ask, last = self._best_prices(symbol)
        return {"bid": bid or 0.0, "ask": ask or 0.0, "last": last or 0.0}

    def _new_order(self, symbol: str, side: str, qty: float, price: float | None, typ: str):
        oid = f"paper_{uuid.uuid4().hex[:12]}"
        return {
            "id": oid,
            "symbol": self._norm_symbol(symbol),
            "side": side.lower(),
            "amount": float(qty),
            "price": float(price) if price is not None else None,
            "average": None,
            "status": "open",
            "type": typ,
            "created": time.time(),
        }

    def _try_fill(self, order: dict):
        bid, ask, last = self._best_prices(order["symbol"])
        side = order["side"]
        price = order["price"]
        fill_price = None

        if order["type"] == "market":
            if side == "buy":
                fill_price = ask or last
            else:
                fill_price = bid or last
        else:
            if side == "buy" and ask and price is not None and price >= ask:
                fill_price = ask
            elif side == "sell" and bid and price is not None and price <= bid:
                fill_price = bid

        if fill_price is None:
            return order

        order["status"] = "closed"
        order["average"] = float(fill_price)
        order["price"] = float(fill_price)
        self.wallet.apply_trade(order["symbol"], side, order["amount"], order["average"], fee_rate=0.0)
        self.wallet.log_trade(
            {
                "symbol": order["symbol"],
                "side": side,
                "qty": order["amount"],
                "price": order["average"],
                "type": order["type"],
                "ts": time.time(),
            }
        )
        print(
            f"[PaperTrade] order filled {order['symbol']} {side} qty={order['amount']:.6f} price={order['average']:.8f}",
            flush=True,
        )
        return order

    def simulate_limit_order(self, symbol: str, side: str, price: float, size: float) -> dict:
        self._assert_paper()
        order = self._new_order(symbol, side, size, price, "limit")
        with self._lock:
            self._orders[order["id"]] = order
            self._try_fill(order)
        print(
            f"[PaperTrade] simulated trade LIMIT {order['symbol']} {side} qty={size:.6f} price={price:.8f}",
            flush=True,
        )
        return dict(order)

    def simulate_market_order(self, symbol: str, side: str, size: float) -> dict:
        self._assert_paper()
        order = self._new_order(symbol, side, size, None, "market")
        with self._lock:
            self._orders[order["id"]] = order
            self._try_fill(order)
        print(f"[PaperTrade] simulated trade MARKET {order['symbol']} {side} qty={size:.6f}", flush=True)
        return dict(order)

    # Compatibility with engine code
    def create_limit_buy_order(self, symbol: str, qty: float, price: float, params=None):
        return self.simulate_limit_order(symbol, "buy", price, qty)

    def create_limit_sell_order(self, symbol: str, qty: float, price: float, params=None):
        return self.simulate_limit_order(symbol, "sell", price, qty)

    def create_market_sell_order(self, symbol: str, qty: float):
        return self.simulate_market_order(symbol, "sell", qty)

    def create_order(self, symbol: str, order_type: str, side: str, qty: float, price: float | None = None, params=None):
        if order_type == "market":
            return self.simulate_market_order(symbol, side, qty)
        return self.simulate_limit_order(symbol, side, float(price or 0.0), qty)

    def fetch_order(self, order_id: str, symbol: str):
        self._assert_paper()
        with self._lock:
            o = self._orders.get(order_id)
            if o is None:
                raise KeyError(order_id)
            if o["status"] == "open":
                self._try_fill(o)
            return dict(o)

    def cancel_order(self, order_id: str, symbol: str):
        self._assert_paper()
        with self._lock:
            o = self._orders.get(order_id)
            if o and o["status"] == "open":
                o["status"] = "canceled"
            return dict(o) if o else {"id": order_id, "status": "canceled"}
