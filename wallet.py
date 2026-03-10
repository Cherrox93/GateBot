from __future__ import annotations

import threading
import time
from collections import defaultdict


class SharedWallet:
    def __init__(self, initial_usdt: float = 200.0):
        self._lock = threading.Lock()
        self.start_balance = float(initial_usdt)
        self._balances = {"USDT": float(initial_usdt)}
        self._reserved = defaultdict(float)
        self._positions = defaultdict(list)
        self._trade_log = []

    @property
    def balance_usdt(self) -> float:
        return self.get_balance("USDT")

    @balance_usdt.setter
    def balance_usdt(self, value: float) -> None:
        with self._lock:
            self._balances["USDT"] = float(value)

    def get_balance(self, asset: str) -> float:
        with self._lock:
            total = float(self._balances.get(asset, 0.0))
            reserved = float(self._reserved.get(asset, 0.0))
            return total - reserved

    def get_total(self, asset: str) -> float:
        with self._lock:
            return float(self._balances.get(asset, 0.0))

    def reserve_balance(self, asset: str, amount: float) -> bool:
        if amount <= 0:
            return True
        with self._lock:
            total = float(self._balances.get(asset, 0.0))
            reserved = float(self._reserved.get(asset, 0.0))
            if total - reserved < amount:
                return False
            self._reserved[asset] += amount
        print(f"[Wallet] reserved {amount:.4f} {asset}", flush=True)
        return True

    def release_balance(self, asset: str, amount: float) -> None:
        if amount <= 0:
            return
        with self._lock:
            self._reserved[asset] = max(0.0, float(self._reserved.get(asset, 0.0)) - amount)
        print(f"[Wallet] released {amount:.4f} {asset}", flush=True)

    def apply_trade(self, symbol: str, side: str, size: float, price: float, fee_rate: float = 0.0) -> float:
        """
        Applies a filled trade to wallet balances.
        Returns signed USDT delta (negative for buy, positive for sell).
        """
        notional = max(0.0, float(size) * float(price))
        fee = notional * max(0.0, float(fee_rate))
        side = side.lower()
        base, quote = symbol.replace("_", "/").split("/")

        with self._lock:
            if quote not in self._balances:
                self._balances[quote] = 0.0
            if base not in self._balances:
                self._balances[base] = 0.0

            if side == "buy":
                delta = -(notional + fee)
                self._balances[quote] += delta
                self._balances[base] += float(size)
                self._positions[symbol].append(
                    {"side": "buy", "size": float(size), "price": float(price), "ts": time.time()}
                )
            elif side == "sell":
                delta = notional - fee
                self._balances[quote] += delta
                self._balances[base] = max(0.0, self._balances[base] - float(size))
                self._positions[symbol].append(
                    {"side": "sell", "size": float(size), "price": float(price), "ts": time.time()}
                )
            else:
                raise ValueError(f"Unsupported side: {side}")

        print(f"[Wallet] balance updated {quote}: {self._balances[quote]:.4f}", flush=True)
        return delta

    def update_balance_after_trade(self, asset: str, delta: float) -> None:
        with self._lock:
            self._balances[asset] = float(self._balances.get(asset, 0.0)) + float(delta)
        print(f"[Wallet] balance updated {asset}: {self._balances[asset]:.4f}", flush=True)

    def log_trade(self, trade: dict) -> None:
        with self._lock:
            self._trade_log.append(dict(trade))
        print(f"[Wallet] trade logged: {trade.get('symbol','?')} {trade.get('side','?')}", flush=True)

    def get_positions(self) -> dict:
        with self._lock:
            return {k: list(v) for k, v in self._positions.items()}

