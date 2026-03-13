"""
engines/scalper_engine.py — Scalping Engine for Gate.io Spot.

Symbols : BTC_USDT, ETH_USDT, SOL_USDT
Strategy: WebSocket orderbook + trades feed → features → signals → maker-first execution
Interface: matches project pattern (cfg, log_callback, market_data, tg)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import requests
import urllib3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import ccxt
import websockets

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import ScalperConfig
from market_data import MarketData
from vault import TELEGRAM_SCALPER, GATEIO_LIVE
from analytics_engine import get_analytics


# ============================================================
#   CONSTANTS (initialized from ScalperConfig defaults)
# ============================================================

_DEFAULT_SCALPER_CFG = ScalperConfig()

MAKER_FEE: float = _DEFAULT_SCALPER_CFG.maker_fee
TAKER_FEE: float = _DEFAULT_SCALPER_CFG.taker_fee
MAX_TRADES_DAY: int = _DEFAULT_SCALPER_CFG.max_trades_day
SYMBOLS: list[str] = [s.replace("/", "_") for s in _DEFAULT_SCALPER_CFG.gigants]
PAIR_REFRESH_SEC: float = _DEFAULT_SCALPER_CFG.pair_refresh_sec

WS_ENDPOINT: str         = "wss://api.gateio.ws/ws/v4/"
WS_RECONNECT_MAX: int = _DEFAULT_SCALPER_CFG.ws_reconnect_max
WS_RECONNECT_BASE: float = _DEFAULT_SCALPER_CFG.ws_reconnect_base
EXCHANGE_TIMEOUT_SEC: float = _DEFAULT_SCALPER_CFG.exchange_timeout_sec
MONITOR_POLL_SEC: float = _DEFAULT_SCALPER_CFG.monitor_poll_sec
MISSED_RETRY_COOLDOWN_SEC: float = _DEFAULT_SCALPER_CFG.missed_retry_cooldown_sec
TARGET_PROFIT_PCT: float = _DEFAULT_SCALPER_CFG.target_profit_pct
STOP_LOSS_PCT: float = _DEFAULT_SCALPER_CFG.stop_loss_pct
TRAILING_STOP_PCT: float = _DEFAULT_SCALPER_CFG.trailing_stop_pct
SOR_MAKER_WAIT_MS: int = _DEFAULT_SCALPER_CFG.sor_maker_wait_ms
SOR_AGGR_WAIT_MS: int = _DEFAULT_SCALPER_CFG.sor_aggressive_wait_ms


def send_telegram(msg: str, token: str, chat_id: str) -> None:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": int(chat_id), "text": msg},
            timeout=10,
            verify=False,
        )
        result = r.json()
        if not result.get("ok"):
            print(f"[TELEGRAM ERROR] {result.get('description')}", flush=True)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}", flush=True)


# ============================================================
#   DATA STRUCTURES
# ============================================================

@dataclass
class MarketSnapshot:
    symbol: str
    timestamp: float
    bid: float
    ask: float
    spread_pct: float
    bid_volume: float
    ask_volume: float
    last_price: float
    volume_1m: float
    volume_5m_avg: float
    price_range_1m: float


@dataclass
class Features:
    symbol: str
    timestamp: float
    bid: float
    ask: float
    ob_imbalance: float
    ob_imbalance_signal: int        # +1 long | -1 short | 0 neutral
    volume_spike_ratio: float
    volume_spike: bool
    spread_pct: float
    spread_ok: bool
    price_range_1m: float
    volatility_ok: bool
    micro_range_pct: float
    breakout_detected: bool
    breakout_direction: int         # +1 / -1
    signal_strength: float          # 0.0 – 1.0


@dataclass
class Signal:
    symbol: str
    timestamp: float
    direction: int                  # +1 long | -1 short
    strategy: str
    strength: float
    entry_price: float
    tp_price: float
    sl_price: float
    tp_pct: float
    sl_pct: float
    reason: str


@dataclass
class OpenPosition:
    symbol: str
    direction: int
    entry_price: float
    tp_price: float
    sl_price: float
    qty: float
    stake: float
    entry_time: float
    entry_order_id: str
    tp_order_id: Optional[str]
    strategy: str
    sl_pct: float = 0.0
    entry_fee_cost: float = 0.0
    entry_fee_currency: str = "USDT"


@dataclass
class TradeResult:
    symbol: str
    direction: int
    strategy: str
    entry_price: float
    exit_price: float
    stake: float
    pnl_usd: float          # gross P&L before fees
    fee_paid: float
    exit_reason: str        # "TP_MAKER" | "TP" | "SL" | "MISSED"
    fill_latency_sec: float
    duration: float
    timestamp: float
    mfe: float = 0.0        # max favorable excursion (%)
    mae: float = 0.0        # max adverse excursion (%)


# ============================================================
#   BOT STATE  (positions only — balance from exchange)
# ============================================================

class _BotState:
    """Thread-safe open position tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._positions: dict[str, OpenPosition] = {}
        self.trades_today: int = 0

    def add(self, pos: OpenPosition) -> None:
        with self._lock:
            self._positions[pos.symbol] = pos

    def reserve(self, pos: OpenPosition, max_positions: int) -> tuple[bool, str]:
        """
        Atomically reserve a position slot for a symbol.
        Prevents race conditions where multiple tasks pass pre-checks concurrently.
        """
        with self._lock:
            if pos.symbol in self._positions:
                return False, f"position_open ({pos.symbol})"
            if len(self._positions) >= max_positions:
                return False, f"max_positions ({max_positions})"
            self._positions[pos.symbol] = pos
            return True, "ok"

    def remove(self, symbol: str) -> Optional[OpenPosition]:
        with self._lock:
            return self._positions.pop(symbol, None)

    def has(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._positions

    def count(self) -> int:
        with self._lock:
            return len(self._positions)

    def as_dict(self) -> dict:
        """Return snapshot dict compatible with main_web.py exposure calc."""
        with self._lock:
            return {
                sym: {
                    "stake": pos.stake,
                    "original_stake": pos.stake,
                    "entry_price": pos.entry_price,
                    "sl_price": pos.sl_price,
                    "tp_price": pos.tp_price,
                    "direction": pos.direction,
                    "strategy": pos.strategy,
                    "entry_time": pos.entry_time,
                }
                for sym, pos in self._positions.items()
            }


# ============================================================
#   MARKET DATA CACHE
# ============================================================

class _SymbolCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.last_price: float = 0.0
        self.last_update: float = 0.0
        self.price_ticks: deque[tuple[float, float]] = deque()
        self.volume_ticks: deque[tuple[float, float]] = deque()
        self.trade_flow_ticks: deque[tuple[float, float, str]] = deque()
        self.trade_notional_ticks: deque[tuple[float, float]] = deque()


class MarketDataCache:
    """Thread-safe in-memory store updated by WebSocket callbacks."""

    def __init__(self) -> None:
        self._data: dict[str, _SymbolCache] = {}
        self._init_lock = threading.Lock()

    def _get(self, symbol: str) -> _SymbolCache:
        with self._init_lock:
            if symbol not in self._data:
                self._data[symbol] = _SymbolCache()
            return self._data[symbol]

    def update_orderbook(self, symbol: str, data: dict) -> None:
        """Process top-5 orderbook update from WebSocket."""
        cache = self._get(symbol)
        now = time.time()
        with cache.lock:
            cache.bids = [(float(p), float(q)) for p, q in data.get("bids", [])][:5]
            cache.asks = [(float(p), float(q)) for p, q in data.get("asks", [])][:5]
            if cache.bids:
                cache.last_price = cache.bids[0][0]
            cache.last_update = now

    def update_trades(self, symbol: str, trades: list[dict]) -> None:
        """Process trade events from WebSocket."""
        cache = self._get(symbol)
        now = time.time()
        with cache.lock:
            for trade in trades:
                ts = float(trade.get("create_time_ms", now * 1000)) / 1000.0
                price = float(trade.get("price", 0))
                amount = float(trade.get("amount", 0))
                vol_usdt = price * amount
                side_raw = str(trade.get("side", "")).lower()
                if side_raw not in ("buy", "sell"):
                    side_raw = ""
                if price > 0:
                    cache.last_price = price
                    cache.price_ticks.append((ts, price))
                    cache.volume_ticks.append((ts, vol_usdt))
                    if amount > 0:
                        cache.trade_flow_ticks.append((ts, amount, side_raw))
                    if vol_usdt > 0:
                        cache.trade_notional_ticks.append((ts, vol_usdt))
                    cache.last_update = now
            cutoff = now - 300.0
            while cache.price_ticks and cache.price_ticks[0][0] < cutoff:
                cache.price_ticks.popleft()
            while cache.volume_ticks and cache.volume_ticks[0][0] < cutoff:
                cache.volume_ticks.popleft()
            while cache.trade_flow_ticks and cache.trade_flow_ticks[0][0] < cutoff:
                cache.trade_flow_ticks.popleft()
            while cache.trade_notional_ticks and cache.trade_notional_ticks[0][0] < cutoff:
                cache.trade_notional_ticks.popleft()

    def get_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        """Build MarketSnapshot from cached data. Returns None if data missing."""
        cache = self._get(symbol)
        now = time.time()
        with cache.lock:
            if not cache.bids or not cache.asks:
                return None
            bid = cache.bids[0][0]
            ask = cache.asks[0][0]
            if bid <= 0 or ask <= 0:
                return None

            spread_pct = (ask - bid) / bid * 100.0
            bid_volume = sum(q for _, q in cache.bids)
            ask_volume = sum(q for _, q in cache.asks)
            last_price = cache.last_price or bid

            cutoff_1m = now - 60.0
            vol_1m = sum(v for t, v in cache.volume_ticks if t >= cutoff_1m)
            vol_5m_avg = sum(v for _, v in cache.volume_ticks) / 5.0 if cache.volume_ticks else 0.0

            prices_1m = [p for t, p in cache.price_ticks if t >= cutoff_1m]
            if len(prices_1m) >= 2:
                lo = min(prices_1m)
                price_range_1m = (max(prices_1m) - lo) / lo * 100.0 if lo > 0 else 0.0
            else:
                price_range_1m = 0.0

        return MarketSnapshot(
            symbol=symbol, timestamp=now,
            bid=bid, ask=ask, spread_pct=spread_pct,
            bid_volume=bid_volume, ask_volume=ask_volume,
            last_price=last_price, volume_1m=vol_1m,
            volume_5m_avg=vol_5m_avg, price_range_1m=price_range_1m,
        )

    def is_stale(self, symbol: str, max_age_ms: float = 500) -> bool:
        cache = self._get(symbol)
        with cache.lock:
            return (time.time() - cache.last_update) * 1000 > max_age_ms

    def get_price_ticks_30s(self, symbol: str) -> list[tuple[float, float]]:
        cache = self._get(symbol)
        cutoff = time.time() - 30.0
        with cache.lock:
            return [(t, p) for t, p in cache.price_ticks if t >= cutoff]

    def get_price_ticks(self, symbol: str, window_sec: float) -> list[tuple[float, float]]:
        cache = self._get(symbol)
        cutoff = time.time() - window_sec
        with cache.lock:
            return [(t, p) for t, p in cache.price_ticks if t >= cutoff]

    def get_volume_sum(self, symbol: str, window_sec: float) -> float:
        cache = self._get(symbol)
        cutoff = time.time() - window_sec
        with cache.lock:
            return sum(v for t, v in cache.volume_ticks if t >= cutoff)

    def get_ema(self, symbol: str, period: int = 20, window_sec: float = 60.0) -> Optional[float]:
        ticks = self.get_price_ticks(symbol, window_sec)
        if len(ticks) < period:
            return None
        prices = [p for _, p in ticks][-period:]
        alpha = 2.0 / (period + 1.0)
        ema = prices[0]
        for price in prices[1:]:
            ema = alpha * price + (1.0 - alpha) * ema
        return ema

    def get_atr_1m_pct(self, symbol: str) -> float:
        ticks = self.get_price_ticks(symbol, 60.0)
        if len(ticks) < 5:
            return 0.0
        prices = [p for _, p in ticks]
        last = prices[-1]
        if last <= 0:
            return 0.0
        return (max(prices) - min(prices)) / last

    def get_top_of_book(self, symbol: str) -> Optional[tuple[float, float, float, float]]:
        cache = self._get(symbol)
        with cache.lock:
            if not cache.bids or not cache.asks:
                return None
            bid_p, bid_q = cache.bids[0]
            ask_p, ask_q = cache.asks[0]
            return bid_p, ask_p, bid_q, ask_q

    def get_recent_trade_rate(self, symbol: str, side: str, window_sec: float = 3.0) -> float:
        """
        Returns estimated traded base-amount per second for aggressor side.
        side='sell' estimates flow hitting bids, side='buy' estimates lifting asks.
        """
        cache = self._get(symbol)
        cutoff = time.time() - window_sec
        side = side.lower()
        if side not in ("buy", "sell"):
            return 0.0
        with cache.lock:
            recent = [(amt, s) for t, amt, s in cache.trade_flow_ticks if t >= cutoff]
        if not recent:
            return 0.0
        known_side_total = sum(amt for amt, s in recent if s == side)
        unknown_total = sum(amt for amt, s in recent if s not in ("buy", "sell"))
        return (known_side_total + 0.5 * unknown_total) / max(window_sec, 1e-6)

    def get_last_n_trade_notional(self, symbol: str, n: int = 20) -> float:
        cache = self._get(symbol)
        with cache.lock:
            if not cache.trade_notional_ticks:
                return 0.0
            vals = list(cache.trade_notional_ticks)[-n:]
        return sum(v for _, v in vals)


# ============================================================
#   FEATURE ENGINE
# ============================================================

class FeatureEngine:
    OB_LONG_THRESHOLD: float  = 1.8
    OB_SHORT_THRESHOLD: float = 0.55
    VOLUME_SPIKE_MIN: float   = 2.0
    SPREAD_MAX_PCT: float     = 0.05
    VOLATILITY_MAX_PCT: float = 0.80
    MICRO_RANGE_MAX: float    = 0.15

    def __init__(self, cache: MarketDataCache) -> None:
        self._cache = cache

    def compute(self, snapshot: MarketSnapshot) -> Features:
        """Compute all features from snapshot + cached price history."""
        sym = snapshot.symbol

        ob_imbalance = (
            snapshot.bid_volume / snapshot.ask_volume
            if snapshot.ask_volume > 0 else 1.0
        )
        if ob_imbalance >= self.OB_LONG_THRESHOLD:
            ob_imbalance_signal = 1
        elif ob_imbalance <= self.OB_SHORT_THRESHOLD:
            ob_imbalance_signal = -1
        else:
            ob_imbalance_signal = 0

        vol_ratio = (
            snapshot.volume_1m / snapshot.volume_5m_avg
            if snapshot.volume_5m_avg > 0 else 0.0
        )
        volume_spike = vol_ratio >= self.VOLUME_SPIKE_MIN
        spread_ok = snapshot.spread_pct < self.SPREAD_MAX_PCT
        volatility_ok = snapshot.price_range_1m < self.VOLATILITY_MAX_PCT

        ticks_30s = self._cache.get_price_ticks_30s(sym)
        if len(ticks_30s) >= 4:
            prices = [p for _, p in ticks_30s]
            lo, hi = min(prices), max(prices)
            micro_range_pct = (hi - lo) / lo * 100.0 if lo > 0 else 0.0
            breakout_detected = (
                micro_range_pct <= self.MICRO_RANGE_MAX
                and ob_imbalance_signal != 0
            )
            if breakout_detected:
                mid = (hi + lo) / 2.0
                breakout_direction = 1 if prices[-1] > mid else -1
            else:
                breakout_direction = 0
        else:
            micro_range_pct = 0.0
            breakout_detected = False
            breakout_direction = 0

        strength = self._calc_strength(
            ob_imbalance, ob_imbalance_signal, vol_ratio, volume_spike, breakout_detected
        )

        return Features(
            symbol=sym, timestamp=snapshot.timestamp,
            bid=snapshot.bid, ask=snapshot.ask,
            ob_imbalance=ob_imbalance, ob_imbalance_signal=ob_imbalance_signal,
            volume_spike_ratio=vol_ratio, volume_spike=volume_spike,
            spread_pct=snapshot.spread_pct, spread_ok=spread_ok,
            price_range_1m=snapshot.price_range_1m, volatility_ok=volatility_ok,
            micro_range_pct=micro_range_pct,
            breakout_detected=breakout_detected, breakout_direction=breakout_direction,
            signal_strength=strength,
        )

    def _calc_strength(
        self,
        ob_imbalance: float, ob_imbalance_signal: int,
        vol_ratio: float, volume_spike: bool, breakout_detected: bool,
    ) -> float:
        score = 0.0
        if ob_imbalance_signal != 0:
            score += min(abs(ob_imbalance - 1.0) / 2.0, 0.40)
        if volume_spike and vol_ratio > 0:
            score += min((vol_ratio - self.VOLUME_SPIKE_MIN) / 10.0 + 0.20, 0.35)
        if breakout_detected:
            score += 0.25
        return min(score, 1.0)


# ============================================================
#   TRADE FILTER
# ============================================================

class TradeFilter:
    """Pre-execution checks against current bot state."""

    MIN_STRENGTH: float = 0.35

    def should_trade(
        self,
        signal: Signal,
        state: _BotState,
        available_balance: float,
        max_positions: int,
        trades_today: int,
        daily_limit_hit: bool,
        stake: float,
    ) -> tuple[bool, str]:
        """
        Returns (True, 'ok') or (False, reason).
        Checks: daily trade limit, open position, strength, daily loss, balance, max slots.
        """
        if trades_today >= MAX_TRADES_DAY:
            return False, f"daily_trade_limit ({MAX_TRADES_DAY})"
        if state.has(signal.symbol):
            return False, f"position_open ({signal.symbol})"
        if signal.strength < self.MIN_STRENGTH:
            return False, f"weak_signal ({signal.strength:.2f})"
        if daily_limit_hit:
            return False, "daily_loss_limit"
        if available_balance < stake:
            return False, f"low_balance ({available_balance:.2f} < {stake:.2f})"
        if state.count() >= max_positions:
            return False, f"max_positions ({max_positions})"
        return True, "ok"


# ============================================================
#   EXECUTION ENGINE
# ============================================================

class ExecutionEngine:
    """
    Order lifecycle: post-only entry → TP limit + SL monitor → exit.
    Uses sync ccxt via run_in_executor (Windows compatible).
    """

    RUNNER_TRAIL_PCT: float = 0.0010
    BE_TRIGGER_PCT:  float = 0.0020   # +0.20% gross → activate break-even
    BE_BUFFER_PCT:   float = 0.0021   # BE SL at entry*(1+0.21%) — covers round-trip fee + buffer

    def __init__(
        self,
        exchange: ccxt.Exchange,
        maker_wait_ms: int = SOR_MAKER_WAIT_MS,
        aggressive_wait_ms: int = SOR_AGGR_WAIT_MS,
        position_callback=None,
        cache: Optional[MarketDataCache] = None,
    ) -> None:
        self.exchange = exchange
        self.maker_wait_ms = maker_wait_ms
        self.aggressive_wait_ms = aggressive_wait_ms
        self.position_callback = position_callback
        self._cache = cache

    @staticmethod
    def _ccxt(symbol: str) -> str:
        return symbol.replace("_", "/")

    async def _call(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def _post_only(
        self, ccxt_sym: str, side: str, qty: float, price: float
    ) -> Optional[dict]:
        try:
            return await self._call(
                self.exchange.create_order,
                ccxt_sym, "limit", side, qty, price,
                {"postOnly": True, "timeInForce": "PO"},
            )
        except Exception as e:
            print(f"[ORDER] post_only failed {ccxt_sym} {side} qty={qty} "
                  f"price={price}: {e}", flush=True)
            return None

    async def _limit_order(
        self, ccxt_sym: str, side: str, qty: float, price: float
    ) -> Optional[dict]:
        try:
            return await self._call(
                self.exchange.create_order,
                ccxt_sym, "limit", side, qty, price,
                {"timeInForce": "GTC"},
            )
        except Exception as e:
            print(f"[ORDER] limit_order failed {ccxt_sym} {side} qty={qty} "
                  f"price={price}: {e}", flush=True)
            return None

    async def _fetch_order_safe(self, ccxt_sym: str, order_id: str) -> Optional[dict]:
        try:
            return await self._call(self.exchange.fetch_order, order_id, ccxt_sym)
        except Exception:
            return None

    async def _cancel_if_open(self, ccxt_sym: str, order_id: Optional[str]) -> None:
        if not order_id:
            return
        try:
            await self._call(self.exchange.cancel_order, order_id, ccxt_sym)
        except Exception:
            pass

    async def _market_sell(self, pos: OpenPosition, ccxt_sym: str) -> dict:
        side = "sell" if pos.direction == 1 else "buy"

        # Fetch actual available balance of base currency to avoid BALANCE_NOT_ENOUGH.
        # Gate.io deducts fees from base currency on buy, so held qty < pos.qty.
        base_currency = ccxt_sym.split("/")[0]
        sell_qty = pos.qty
        try:
            loop = asyncio.get_running_loop()
            bal = await loop.run_in_executor(
                None, lambda: self.exchange.fetch_balance()
            )
            available = float(bal.get(base_currency, {}).get("free", 0.0))
            if available <= 0:
                raise ValueError(f"zero_available_balance for {base_currency}")
            if available < sell_qty:
                print(
                    f"[{pos.symbol}] qty adjusted {sell_qty:.6f}→{available:.6f} "
                    f"(fee deducted from base)",
                    flush=True,
                )
                sell_qty = available
        except Exception as e:
            print(f"[{pos.symbol}] balance fetch failed, using pos.qty: {e}", flush=True)

        last_exc: Exception = Exception("no_attempts")
        for attempt in range(3):
            try:
                if attempt > 0:
                    await asyncio.sleep(0.5 * attempt)
                o = await self._call(
                    self.exchange.create_order, ccxt_sym, "market", side, sell_qty
                )
                order_id = o.get("id")
                if order_id:
                    await asyncio.sleep(0.5)
                    o = await self._call(
                        self.exchange.fetch_order, order_id, ccxt_sym
                    ) or o
                filled = float(o.get("average") or o.get("price") or 0.0)
                if filled <= 0:
                    raise ValueError(f"zero_fill attempt={attempt}")
                return {
                    "price": filled,
                    "fee_cost": float((o.get("fee") or {}).get("cost") or 0.0),
                    "fee_currency": (o.get("fee") or {}).get("currency") or "USDT",
                }
            except Exception as e:
                last_exc = e
                print(
                    f"[ORDER] _market_sell FAILED attempt={attempt+1}/3 "
                    f"{ccxt_sym} qty={sell_qty:.6f}: {e}",
                    flush=True,
                )
        raise RuntimeError(
            f"_market_sell: 3 retries exhausted for {ccxt_sym} "
            f"qty={sell_qty:.6f}: {last_exc}"
        )

    def _get_cached_price(self, symbol: str, fallback: float) -> float:
        """Get latest price from WebSocket cache — zero latency vs REST."""
        if self._cache is None:
            return fallback
        try:
            top = self._cache.get_top_of_book(symbol)
            if top:
                bid_p, ask_p, _, _ = top
                if bid_p > 0 and ask_p > 0:
                    return (bid_p + ask_p) / 2.0
        except Exception:
            pass
        return fallback

    async def _last_price(self, ccxt_sym: str, fallback: float) -> float:
        try:
            t = await self._call(self.exchange.fetch_ticker, ccxt_sym)
            return float(t.get("last") or t.get("bid") or fallback)
        except Exception:
            return fallback

    async def _best_prices(self, ccxt_sym: str, fallback: float) -> tuple[float, float]:
        try:
            t = await self._call(self.exchange.fetch_ticker, ccxt_sym)
            bid = float(t.get("bid") or fallback)
            ask = float(t.get("ask") or fallback)
            return bid, ask
        except Exception:
            return fallback, fallback

    async def _taker_order(self, ccxt_sym: str, side: str, qty: float,
                           price: float = 0.0) -> Optional[dict]:
        try:
            if side == "buy" and price > 0:
                # Gate.io market buy requires price to calculate cost
                o = await self._call(
                    self.exchange.create_order,
                    ccxt_sym, "market", side, qty, price,
                )
            else:
                # Market sell — no price needed
                o = await self._call(
                    self.exchange.create_order,
                    ccxt_sym, "market", side, qty,
                )
            filled = float(o.get("average") or o.get("price") or price or 0.0)
            if filled <= 0:
                print(f"[ORDER] taker_order zero fill {ccxt_sym} {side} "
                      f"qty={qty}: {o}", flush=True)
                return None
            return o
        except Exception as e:
            print(f"[ORDER] taker_order failed {ccxt_sym} {side} "
                  f"qty={qty}: {e}", flush=True)
            return None

    async def execute_signal(self, signal: Signal, stake: float) -> TradeResult:
        """
        Smart Order Router:
        1) post-only maker
        2) aggressive limit
        3) market fallback
        """
        ccxt_sym = self._ccxt(signal.symbol)
        side = "buy" if signal.direction == 1 else "sell"
        qty = round(stake / signal.entry_price, 6)  # Gate.io max 6 decimals
        if qty <= 0:
            return self._missed(signal, stake, "qty_zero")
        print(f"[ORDER] Attempting {signal.symbol} {side} qty={qty} "
              f"stake={stake} price={signal.entry_price}", flush=True)
        entry_start = time.time()

        # 1) Maker slightly inside spread (retail-safe, still post-only)
        bid, ask = await self._best_prices(ccxt_sym, signal.entry_price)
        spread = max(ask - bid, 0.0)
        if side == "buy":
            entry_price = bid + 0.15 * spread
            if ask > 0:
                entry_price = min(entry_price, ask * 0.9999)
        else:
            entry_price = ask - 0.15 * spread
            if bid > 0:
                entry_price = max(entry_price, bid * 1.0001)
        if entry_price <= 0:
            entry_price = signal.entry_price

        order = await self._post_only(ccxt_sym, side, qty, entry_price)
        order_id = order["id"] if order else None
        filled_price: Optional[float] = None
        if order_id:
            await asyncio.sleep(self.maker_wait_ms / 1000.0)
            st = await self._fetch_order_safe(ccxt_sym, order_id)
            if st and st.get("status") in ("closed", "filled"):
                filled_price = float(st.get("average") or st.get("price") or entry_price)
            else:
                await self._cancel_if_open(ccxt_sym, order_id)

        # 2) Aggressive limit crossing spread
        if filled_price is None:
            bid, ask = await self._best_prices(ccxt_sym, signal.entry_price)
            aggressive_price = ask if side == "buy" else bid
            if aggressive_price > 0:
                aggr_order = await self._limit_order(ccxt_sym, side, qty, aggressive_price)
                if aggr_order:
                    order_id = aggr_order["id"]
                    await asyncio.sleep(self.aggressive_wait_ms / 1000.0)
                    st = await self._fetch_order_safe(ccxt_sym, order_id)
                    if st and st.get("status") in ("closed", "filled"):
                        filled_price = float(st.get("average") or st.get("price") or aggressive_price)
                    else:
                        await self._cancel_if_open(ccxt_sym, order_id)

        # 3) Market fallback
        _taker_raw: Optional[dict] = None
        if filled_price is None:
            _taker_raw = await self._taker_order(ccxt_sym, side, qty, price=signal.entry_price)
            if _taker_raw is None:
                return self._missed(signal, stake, "sor_no_fill")
            filled_price = float(_taker_raw.get("average") or _taker_raw.get("price"))
            order_id = _taker_raw.get("id") or "market_fallback"

        # Fetch verified entry fill from exchange
        try:
            if order_id and order_id != "market_fallback":
                entry_order_data = await self._call(
                    self.exchange.fetch_order, order_id, ccxt_sym
                )
            elif _taker_raw:
                entry_order_data = _taker_raw
            else:
                entry_order_data = {}
            real_entry_price = float(
                entry_order_data.get("average")
                or entry_order_data.get("price")
                or filled_price
            )
            real_entry_qty = float(
                entry_order_data.get("filled") or qty
            )
            real_entry_fee_cost = float(
                (entry_order_data.get("fee") or {}).get("cost") or 0.0
            )
            real_entry_fee_currency = (
                (entry_order_data.get("fee") or {}).get("currency") or "USDT"
            )
        except Exception:
            real_entry_price = filled_price
            real_entry_qty = qty
            real_entry_fee_cost = 0.0
            real_entry_fee_currency = "USDT"

        # Dynamic SL: at least 1.3× the 1-minute ATR, floored at STOP_LOSS_PCT
        cache_ref = self._cache
        _atr = cache_ref.get_atr_1m_pct(signal.symbol) if cache_ref else 0.0
        _dynamic_sl = max(_atr * 1.3, STOP_LOSS_PCT)
        _dynamic_sl = min(_dynamic_sl, STOP_LOSS_PCT * 4.0)   # cap at 4× to avoid runaway SL

        # TP/SL prices — exact user settings, no fee adjustment
        # Fees are only accounted for in PnL display/logging, not in trigger prices
        pos = OpenPosition(
            symbol=signal.symbol, direction=signal.direction,
            entry_price=real_entry_price,
            tp_price=real_entry_price * (1 + signal.direction * TARGET_PROFIT_PCT),
            sl_price=real_entry_price * (1 - signal.direction * _dynamic_sl),
            qty=real_entry_qty, stake=stake,
            entry_time=time.time(), entry_order_id=order_id,
            tp_order_id=None, strategy=signal.strategy,
            sl_pct=_dynamic_sl,
            entry_fee_cost=real_entry_fee_cost,
            entry_fee_currency=real_entry_fee_currency,
        )
        print(
            f"[{signal.symbol}] OPEN entry={real_entry_price:.6f}"
            f" qty={real_entry_qty:.6f} fee={real_entry_fee_cost:.6f}{real_entry_fee_currency}"
            f" ATR={_atr:.5f} dynamic_SL={_dynamic_sl:.5f}"
            f" TP={TARGET_PROFIT_PCT:.5f}"
            f" tp_price={pos.tp_price:.6f} sl_price={pos.sl_price:.6f}",
            flush=True,
        )

        # Retail exit model: take-profit is executed with taker (market) for fill certainty.
        fill_latency = max(time.time() - entry_start, 0.0)
        return await self._monitor(pos, ccxt_sym, fill_latency)

    async def _monitor(self, pos: OpenPosition, ccxt_sym: str, fill_latency_sec: float) -> TradeResult:
        exit_reason = "SL"
        exit_price = pos.entry_price
        exit_fee_cost = 0.0
        exit_fee_currency = "USDT"
        _last_pos_broadcast = 0.0
        # MFE/MAE tracking (gross pnl extremes)
        _max_favorable = 0.0
        _max_adverse = 0.0
        # Runner state (Phase 3)
        runner_active: bool = False
        runner_sl: float = 0.0
        runner_peak: float = 0.0
        # Break-even state (Phase 2)
        be_active: bool = False
        be_sl_price: float = 0.0

        BE_TRIGGER = self.BE_TRIGGER_PCT
        BE_BUFFER  = self.BE_BUFFER_PCT

        while True:
            # a) sleep
            await asyncio.sleep(MONITOR_POLL_SEC)
            now = time.time()

            # b) current price — WebSocket cache first, REST fallback
            current = self._get_cached_price(pos.symbol, 0.0)
            if current <= 0:
                current = await self._last_price(ccxt_sym, pos.entry_price)

            # c) broadcast position update every ~1s
            if self.position_callback and now - _last_pos_broadcast >= 1.0:
                _last_pos_broadcast = now
                self.position_callback(pos.symbol, pos.entry_price, current, pos.sl_price, pos.tp_price)

            # d) check TP_MAKER order fill
            if pos.tp_order_id:
                try:
                    tp = await self._call(self.exchange.fetch_order, pos.tp_order_id, ccxt_sym)
                    if tp["status"] in ("closed", "filled"):
                        exit_price = float(tp.get("average") or tp.get("price") or pos.tp_price)
                        exit_fee_cost = float((tp.get("fee") or {}).get("cost") or 0.0)
                        exit_fee_currency = (tp.get("fee") or {}).get("currency") or "USDT"
                        exit_reason = "TP_MAKER"
                        break
                except Exception:
                    pass

            # e) calc gross_pnl
            gross_pnl = (current / pos.entry_price - 1.0) * pos.direction

            # Track MFE/MAE (gross)
            if gross_pnl > _max_favorable:
                _max_favorable = gross_pnl
            if gross_pnl < 0 and abs(gross_pnl) > _max_adverse:
                _max_adverse = abs(gross_pnl)

            # f) RUNNER block — Phase 3 active
            if runner_active:
                runner_peak = max(runner_peak, current)
                runner_trail_sl = runner_peak * (1.0 - self.RUNNER_TRAIL_PCT)
                effective_sl = max(runner_sl, runner_trail_sl)
                if current <= effective_sl:
                    if pos.tp_order_id:
                        await self._cancel_if_open(ccxt_sym, pos.tp_order_id)
                    try:
                        _exit_order = await self._market_sell(pos, ccxt_sym)
                        exit_price = _exit_order["price"]
                        exit_fee_cost = _exit_order["fee_cost"]
                        exit_fee_currency = _exit_order["fee_currency"]
                        exit_reason = "RUNNER"
                        break
                    except RuntimeError as _sell_err:
                        print(f"[{pos.symbol}] SELL_FAILED — retrying monitor loop: {_sell_err}",
                              flush=True)
                        await asyncio.sleep(1.0)
                        continue
                continue  # runner active — skip other checks

            # g) NEAR TP logging
            if gross_pnl >= TARGET_PROFIT_PCT * 0.9:
                print(
                    f"[{pos.symbol}] NEAR_TP gross={gross_pnl:.5f} "
                    f"target={TARGET_PROFIT_PCT:.5f} "
                    f"current={current:.6f} tp={pos.tp_price:.6f}",
                    flush=True,
                )

            # h) RUNNER activation — Phase 2→3 transition
            if gross_pnl >= TARGET_PROFIT_PCT:
                # Cancel any open order so it can't fill as TP_MAKER behind our back
                if pos.tp_order_id:
                    await self._cancel_if_open(ccxt_sym, pos.tp_order_id)
                    pos.tp_order_id = None
                runner_active = True
                runner_sl = pos.tp_price
                runner_peak = current
                print(
                    f"[{pos.symbol}] 🚀 RUNNER aktywny "
                    f"peak={current:.6f} "
                    f"sl_lock={pos.tp_price:.6f}",
                    flush=True,
                )
                continue

            # i) BREAK-EVEN block — Phase 2 active
            if be_active:
                if current * pos.direction <= be_sl_price * pos.direction:
                    if pos.tp_order_id:
                        await self._cancel_if_open(ccxt_sym, pos.tp_order_id)
                    try:
                        _exit_order = await self._market_sell(pos, ccxt_sym)
                        exit_price = _exit_order["price"]
                        exit_fee_cost = _exit_order["fee_cost"]
                        exit_fee_currency = _exit_order["fee_currency"]
                        exit_reason = "BE"
                        break
                    except RuntimeError as _sell_err:
                        print(f"[{pos.symbol}] SELL_FAILED — retrying monitor loop: {_sell_err}",
                              flush=True)
                        await asyncio.sleep(1.0)
                        continue
                # BE active but price above BE SL — check backup SL for flash crash
                if gross_pnl <= -pos.sl_pct:
                    if pos.tp_order_id:
                        await self._cancel_if_open(ccxt_sym, pos.tp_order_id)
                    try:
                        _exit_order = await self._market_sell(pos, ccxt_sym)
                        exit_price = _exit_order["price"]
                        exit_fee_cost = _exit_order["fee_cost"]
                        exit_fee_currency = _exit_order["fee_currency"]
                        exit_reason = "SL"
                        break
                    except RuntimeError as _sell_err:
                        print(f"[{pos.symbol}] SELL_FAILED — retrying monitor loop: {_sell_err}",
                              flush=True)
                        await asyncio.sleep(1.0)
                        continue
                continue

            # j) BREAK-EVEN activation — Phase 1→2 transition
            # No limit order on exchange — exit managed by software monitor only.
            # Placing a limit would get filled as TP_MAKER before runner can activate.
            if gross_pnl >= BE_TRIGGER:
                be_active = True
                be_sl_price = pos.entry_price * (1.0 + BE_BUFFER * pos.direction)
                print(
                    f"[{pos.symbol}] 🛡️ BREAK-EVEN aktywny "
                    f"be_sl={be_sl_price:.6f} "
                    f"current={current:.6f}",
                    flush=True,
                )
                continue

            # k) Normal SL — Phase 1 (before BE activation)
            if gross_pnl <= -pos.sl_pct:
                if pos.tp_order_id:
                    await self._cancel_if_open(ccxt_sym, pos.tp_order_id)
                try:
                    _exit_order = await self._market_sell(pos, ccxt_sym)
                    exit_price = _exit_order["price"]
                    exit_fee_cost = _exit_order["fee_cost"]
                    exit_fee_currency = _exit_order["fee_currency"]
                    exit_reason = "SL"
                    break
                except RuntimeError as _sell_err:
                    print(f"[{pos.symbol}] SELL_FAILED — retrying monitor loop: {_sell_err}",
                          flush=True)
                    await asyncio.sleep(1.0)
                    continue

        return self._build_result(pos, exit_price, exit_reason, fill_latency_sec,
                                  exit_fee_cost=exit_fee_cost, exit_fee_currency=exit_fee_currency,
                                  mfe=_max_favorable, mae=_max_adverse)

    def _build_result(
        self,
        pos: OpenPosition,
        exit_price: float,
        exit_reason: str,
        fill_latency_sec: float,
        exit_fee_cost: float = 0.0,
        exit_fee_currency: str = "USDT",
        mfe: float = 0.0,
        mae: float = 0.0,
    ) -> TradeResult:
        # All prices and fees are real exchange data — no local estimation
        real_qty = pos.qty
        raw_pnl = (exit_price - pos.entry_price) * real_qty * pos.direction
        total_fee = pos.entry_fee_cost + exit_fee_cost

        return TradeResult(
            symbol=pos.symbol, direction=pos.direction, strategy=pos.strategy,
            entry_price=pos.entry_price, exit_price=exit_price,
            stake=pos.stake, pnl_usd=raw_pnl,
            fee_paid=total_fee, exit_reason=exit_reason, fill_latency_sec=fill_latency_sec,
            duration=time.time() - pos.entry_time, timestamp=time.time(),
            mfe=mfe, mae=mae,
        )

    def _missed(self, signal: Signal, stake: float, reason: str = "") -> TradeResult:
        return TradeResult(
            symbol=signal.symbol, direction=signal.direction, strategy=signal.strategy,
            entry_price=signal.entry_price, exit_price=signal.entry_price,
            stake=stake, pnl_usd=0.0, fee_paid=0.0, exit_reason="MISSED", fill_latency_sec=0.0,
            duration=0.0, timestamp=time.time(),
        )


# ============================================================
#   SCALPER ENGINE  (main class — matches project interface)
# ============================================================

class ScalperEngine:
    """
    Scalping engine for BTC/ETH/SOL.
    Interface matches other project engines (cfg, log_callback, market_data, tg).
    Live trading only — balance fetched from Gate.io, no paper simulation.
    """

    def __init__(
        self,
        cfg: ScalperConfig,
        log_callback=None,
        market_data: Optional[MarketData] = None,
        tg=None,
        portfolio=None,       # accepted for interface compat, ignored
    ) -> None:
        global MAKER_FEE, TAKER_FEE, MAX_TRADES_DAY, PAIR_REFRESH_SEC
        global WS_RECONNECT_MAX, WS_RECONNECT_BASE, EXCHANGE_TIMEOUT_SEC
        global MONITOR_POLL_SEC, MISSED_RETRY_COOLDOWN_SEC
        global TARGET_PROFIT_PCT, STOP_LOSS_PCT, TRAILING_STOP_PCT
        global SOR_MAKER_WAIT_MS, SOR_AGGR_WAIT_MS

        self.cfg = cfg
        MAKER_FEE = float(self.cfg.maker_fee)
        TAKER_FEE = float(self.cfg.taker_fee)
        MAX_TRADES_DAY = int(self.cfg.max_trades_day)
        PAIR_REFRESH_SEC = float(self.cfg.pair_refresh_sec)
        WS_RECONNECT_MAX = int(self.cfg.ws_reconnect_max)
        WS_RECONNECT_BASE = float(self.cfg.ws_reconnect_base)
        EXCHANGE_TIMEOUT_SEC = float(self.cfg.exchange_timeout_sec)
        MONITOR_POLL_SEC = float(self.cfg.monitor_poll_sec)
        MISSED_RETRY_COOLDOWN_SEC = float(self.cfg.missed_retry_cooldown_sec)
        TARGET_PROFIT_PCT = float(self.cfg.target_profit_pct)
        STOP_LOSS_PCT = float(self.cfg.stop_loss_pct)
        TRAILING_STOP_PCT = float(self.cfg.trailing_stop_pct)
        SOR_MAKER_WAIT_MS = int(self.cfg.sor_maker_wait_ms)
        SOR_AGGR_WAIT_MS = int(self.cfg.sor_aggressive_wait_ms)

        self.log_callback = log_callback
        self.market_data = market_data      # kept for compatibility; engine uses own WS
        self.tg = tg

        # Analytics
        self.analytics = get_analytics('scalper')
        self._trade_ids: dict = {}

        # ccxt — real Gate.io credentials for live spot trading
        self.exchange_client = ccxt.gateio({
            "apiKey": GATEIO_LIVE.api_key,
            "secret": GATEIO_LIVE.secret_key,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        })

        # Components
        self._cache    = MarketDataCache()
        self._features = FeatureEngine(self._cache)
        self._filter   = TradeFilter()
        self._filter.MIN_STRENGTH = float(self.cfg.min_signal_strength)
        self._executor = ExecutionEngine(
            self.exchange_client,
            maker_wait_ms=int(self.cfg.sor_maker_wait_ms),
            aggressive_wait_ms=int(self.cfg.sor_aggressive_wait_ms),
            position_callback=self._broadcast_position,
            cache=self._cache,
        )
        self._state    = _BotState()
        self._positions_lock = threading.Lock()
        self._pending_orders: set[str] = set()
        self._trade_rate_window: deque[float] = deque()
        self._last_trade_ts: dict[str, float] = {}

        self.symbol_cooldown_sec = float(self.cfg.symbol_cooldown_sec)
        self.max_pending_orders = int(self.cfg.max_pending_orders)
        self.max_trade_rate_per_sec = float(self.cfg.max_trade_rate_per_sec)
        self.max_position_size_usdt = float(self.cfg.max_position_size_usdt)
        self.max_open_positions = int(self.cfg.max_open_positions)

        # Runtime flags
        self.running = False
        self.main_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._process_tasks: dict[str, asyncio.Task] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._pair_task: Optional[asyncio.Task] = None
        self._last_ws_ok: float = time.time()
        self._retry_after: dict[str, float] = {}
        self._symbols: list[str] = [s.replace("/", "_") for s in getattr(self.cfg, "gigants", ())] or list(SYMBOLS)
        self._impulses: dict[str, dict] = {}

        # Stats (exposed for /status and main_web.py)
        # Balance from exchange — cached for max 2s to avoid API spam
        self._balance_cache: float = 0.0
        self._balance_cache_ts: float = 0.0
        try:
            _init_bal = self.exchange_client.fetch_balance()
            self._balance_cache = float(_init_bal.get("USDT", {}).get("free", 0.0))
            self._balance_cache_ts = time.time()
        except Exception:
            self._balance_cache = cfg.start_balance
        # Trade stats — fetched from Gate.io API, cached 30s
        self._stats_cache: dict = {}
        self._stats_cache_ts: float = 0.0
        self._stats_cache_ttl: float = 30.0
        self._session_start_time: float = time.time()
        self._day_start = datetime.now().date()
        self._debug_last_print: dict[str, float] = {}
        self._exec_attempts = 0
        self._exec_fills = 0
        self._exec_missed = 0
        self._exec_fill_latencies: deque[float] = deque(maxlen=500)

    # ── Balance — always from exchange, cached 2s ─────────────────────────────

    @property
    def balance_usdt(self) -> float:
        """Read-only for dashboard / main_web.py — returns cached exchange balance."""
        return self._balance_cache

    @balance_usdt.setter
    def balance_usdt(self, value: float) -> None:
        """Allow direct assignment for backward compat (e.g. _run startup)."""
        self._balance_cache = value
        self._balance_cache_ts = time.time()

    async def _get_available_balance(self) -> float:
        """Fetch available USDT from Gate.io with 2s cache."""
        now = time.time()
        if now - self._balance_cache_ts < 2.0:
            return self._balance_cache
        try:
            loop = asyncio.get_running_loop()
            bal = await loop.run_in_executor(
                None, lambda: self.exchange_client.fetch_balance()
            )
            result = float(bal.get("USDT", {}).get("free", 0.0))
            self._balance_cache = result
            self._balance_cache_ts = now
            return result
        except Exception as e:
            self.log(f"[BALANCE] Błąd fetch_balance: {e}")
            return self._balance_cache

    def _invalidate_balance_cache(self) -> None:
        """Force next _get_available_balance() to fetch fresh from exchange."""
        self._balance_cache_ts = 0.0

    # ── Trade stats from Gate.io API ──────────────────────────────────────────

    async def _fetch_trade_stats(self) -> dict:
        """Fetch real trading stats from Gate.io. Cached for 30s."""
        now = time.time()
        if self._stats_cache and now - self._stats_cache_ts < self._stats_cache_ttl:
            return self._stats_cache

        try:
            loop = asyncio.get_running_loop()

            # 1) Real balance
            balance_data = await loop.run_in_executor(
                None, lambda: self.exchange_client.fetch_balance()
            )
            usdt_free = float(balance_data.get("USDT", {}).get("free", 0.0))
            usdt_total = float(balance_data.get("USDT", {}).get("total", 0.0))

            # 2) Trade history from exchange (since session start)
            since_ms = int(self._session_start_time * 1000)
            all_trades = []
            for symbol in self._symbols:
                try:
                    ccxt_sym = symbol.replace("_", "/")
                    trades = await loop.run_in_executor(
                        None,
                        lambda s=ccxt_sym: self.exchange_client.fetch_my_trades(
                            s, since=since_ms, limit=100
                        )
                    )
                    all_trades.extend(trades)
                except Exception:
                    pass

            # 3) Compute stats from real data
            buys = [t for t in all_trades if t.get("side") == "buy"]
            sells = [t for t in all_trades if t.get("side") == "sell"]

            # Only count matched pairs — an unmatched BUY is an open position, not a loss.
            matched_buy_ids: set[str] = set()
            matched_pnl: float = 0.0
            matched_fee: float = 0.0
            for sell in sells:
                sym = sell.get("symbol")
                sell_ts = float(sell.get("timestamp", 0))
                candidates = [
                    b for b in buys
                    if b.get("symbol") == sym
                    and float(b.get("timestamp", 0)) < sell_ts
                    and b.get("id") not in matched_buy_ids
                ]
                if not candidates:
                    continue
                matching_buy = max(candidates, key=lambda b: float(b.get("timestamp", 0)))
                matched_buy_ids.add(matching_buy.get("id"))
                sell_cost = float(sell.get("cost", 0.0))
                buy_cost = float(matching_buy.get("cost", 0.0))
                sell_fee = float(sell.get("fee", {}).get("cost", 0.0))
                buy_fee = float(matching_buy.get("fee", {}).get("cost", 0.0))
                matched_pnl += sell_cost - buy_cost
                matched_fee += sell_fee + buy_fee

            total_fee = matched_fee
            session_pnl = matched_pnl - matched_fee

            # Daily PnL (trades from today only)
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp() * 1000
            today_trades = [
                t for t in all_trades
                if float(t.get("timestamp", 0)) >= today_start
            ]
            today_buys = [t for t in today_trades if t.get("side") == "buy"]
            today_sells = [t for t in today_trades if t.get("side") == "sell"]
            today_matched_buy_ids: set[str] = set()
            daily_pnl: float = 0.0
            today_fee: float = 0.0
            for sell in today_sells:
                sym = sell.get("symbol")
                sell_ts = float(sell.get("timestamp", 0))
                candidates = [
                    b for b in today_buys
                    if b.get("symbol") == sym
                    and float(b.get("timestamp", 0)) < sell_ts
                    and b.get("id") not in today_matched_buy_ids
                ]
                if not candidates:
                    continue
                matching_buy = max(candidates, key=lambda b: float(b.get("timestamp", 0)))
                today_matched_buy_ids.add(matching_buy.get("id"))
                sell_fee = float(sell.get("fee", {}).get("cost", 0.0))
                buy_fee = float(matching_buy.get("fee", {}).get("cost", 0.0))
                daily_pnl += float(sell.get("cost", 0.0)) - float(matching_buy.get("cost", 0.0))
                today_fee += sell_fee + buy_fee
            daily_pnl -= today_fee

            # Wins/losses: compare sell price vs last buy of same symbol
            wins = 0
            losses = 0
            for sell in sells:
                sym = sell.get("symbol")
                matching_buys = [
                    b for b in buys
                    if b.get("symbol") == sym
                    and float(b.get("timestamp", 0)) < float(sell.get("timestamp", 0))
                ]
                if matching_buys:
                    last_buy = max(matching_buys, key=lambda b: float(b.get("timestamp", 0)))
                    if float(sell.get("price", 0)) > float(last_buy.get("price", 0)):
                        wins += 1
                    else:
                        losses += 1

            total_trades = wins + losses
            win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0.0

            stats = {
                "bot": "scalper",
                "usdt_free": round(usdt_free, 2),
                "usdt_total": round(usdt_total, 2),
                "session_pnl": round(session_pnl, 4),
                "daily_pnl": round(daily_pnl, 4),
                "total_fee_paid": round(total_fee, 4),
                "wins": wins,
                "losses": losses,
                "total_trades": total_trades,
                "win_rate": win_rate,
                "effective_stake": self._calc_effective_stake(),
                "open_positions": self._state.count(),
                "last_updated": datetime.now().strftime("%H:%M:%S"),
            }

            self._stats_cache = stats
            self._stats_cache_ts = now
            return stats

        except Exception as e:
            self.log(f"[STATS] Błąd pobierania statystyk: {e}")
            return self._stats_cache or {
                "bot": "scalper",
                "usdt_free": 0.0, "usdt_total": 0.0,
                "session_pnl": 0.0, "daily_pnl": 0.0, "total_fee_paid": 0.0,
                "wins": 0, "losses": 0, "total_trades": 0, "win_rate": 0.0,
                "effective_stake": self._calc_effective_stake(),
                "open_positions": self._state.count(),
                "last_updated": "błąd",
            }

    async def _fetch_real_pnl(
        self, symbol: str, entry_price: float, qty: float, after_ts: float,
    ) -> float | None:
        """Fetch real net PnL from last SELL trade on Gate.io.
        Returns float if successful, None on API error (caller uses fallback).
        """
        try:
            loop = asyncio.get_running_loop()
            ccxt_sym = symbol.replace("_", "/")
            since_ms = int(after_ts * 1000)

            trades = await loop.run_in_executor(
                None,
                lambda: self.exchange_client.fetch_my_trades(
                    ccxt_sym, since=since_ms, limit=10,
                )
            )
            if not trades:
                return None

            sell_trades = [t for t in trades if t.get("side") == "sell"]
            if not sell_trades:
                return None

            last_sell = max(sell_trades, key=lambda t: float(t.get("timestamp", 0)))
            real_sell_price = float(last_sell.get("price", 0.0))
            real_fee = float(last_sell.get("fee", {}).get("cost", 0.0))
            real_pnl = (real_sell_price - entry_price) * qty - real_fee
            return round(real_pnl, 2)

        except Exception as e:
            self.log(f"[REAL PnL] Błąd dla {symbol}: {e}")
            return None

    def apply_cfg_globals(self) -> None:
        """Propagate cfg values to module-level globals and instance vars."""
        global TARGET_PROFIT_PCT, STOP_LOSS_PCT, TRAILING_STOP_PCT
        global MAKER_FEE, TAKER_FEE, MAX_TRADES_DAY
        _old_tp, _old_sl = TARGET_PROFIT_PCT, STOP_LOSS_PCT
        TARGET_PROFIT_PCT = float(self.cfg.target_profit_pct)
        STOP_LOSS_PCT = float(self.cfg.stop_loss_pct)
        TRAILING_STOP_PCT = float(self.cfg.trailing_stop_pct)
        MAKER_FEE = float(self.cfg.maker_fee)
        TAKER_FEE = float(self.cfg.taker_fee)
        MAX_TRADES_DAY = int(self.cfg.max_trades_day)
        # Sync Runner/BE to executor
        self._executor.RUNNER_TRAIL_PCT = float(self.cfg.runner_trail_pct)
        self._executor.BE_TRIGGER_PCT   = float(self.cfg.be_trigger_pct)
        self._executor.BE_BUFFER_PCT    = float(self.cfg.be_buffer_pct)
        # Sync instance vars set from cfg in __init__
        self.symbol_cooldown_sec = float(self.cfg.symbol_cooldown_sec)
        self.max_position_size_usdt = float(self.cfg.max_position_size_usdt)
        self.max_open_positions = int(self.cfg.max_open_positions)
        self._filter.MIN_STRENGTH = float(self.cfg.min_signal_strength)
        if _old_tp != TARGET_PROFIT_PCT or _old_sl != STOP_LOSS_PCT:
            print(
                f"[ScalperCfg] globals synced: TP {_old_tp}->{TARGET_PROFIT_PCT}"
                f" SL {_old_sl}->{STOP_LOSS_PCT}"
                f" Trail={TRAILING_STOP_PCT} Runner={self._executor.RUNNER_TRAIL_PCT}",
                flush=True,
            )

    # ── Compatibility property for main_web.py ───────────────────────────────

    @property
    def active_positions(self) -> dict:
        """Dict of {symbol: {"stake": x, "original_stake": x}} for portfolio calc."""
        return self._state.as_dict()

    @property
    def bot_profit_summary(self) -> dict:
        """Return last cached stats (synchronous)."""
        return self._stats_cache or {
            "bot": "scalper",
            "usdt_free": 0.0, "session_pnl": 0.0, "daily_pnl": 0.0,
            "wins": 0, "losses": 0, "win_rate": 0.0,
            "effective_stake": self._calc_effective_stake(),
            "open_positions": self._state.count(),
            "last_updated": "ładowanie...",
        }

    def _calc_effective_stake(self) -> float:
        """Effective stake with optional reinvest from session profit."""
        if not self.cfg.reinvest_enabled:
            return float(self.cfg.base_stake_usdt)
        session_pnl = self._stats_cache.get("session_pnl", 0.0)
        reinvest_pool = max(session_pnl, 0.0) * float(self.cfg.reinvest_max_stake)
        open_slots = max(self._state.count(), 1)
        reinvest_per_slot = reinvest_pool / open_slots
        effective = float(self.cfg.base_stake_usdt) + reinvest_per_slot
        effective = min(effective, float(self.cfg.stake_max_cap_usdt))
        return round(effective, 2)

    # ── Logging (same pattern as other engines) ──────────────────────────────

    def log(self, msg: str) -> None:
        clean = msg.replace("*", "").replace("`", "")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {clean}", flush=True)

    def gui_log(self, msg: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(msg)
            except Exception:
                pass

    def set_status(self, text: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(f"STATUS_UPDATE::{text}")
            except Exception:
                pass

    def _broadcast_position(self, symbol: str, entry_price: float, current_price: float, sl_price: float, tp_price: float) -> None:
        """Send live position data to web frontend via log_callback."""
        if self.log_callback:
            try:
                data = json.dumps({
                    "symbol": symbol.replace("_", "/"),
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                })
                self.log_callback(f"POSITION_UPDATE::{data}")
            except Exception:
                pass

    def _broadcast_position_closed(self, symbol: str) -> None:
        """Notify web frontend that a position was closed."""
        if self.log_callback:
            try:
                data = json.dumps({"symbol": symbol.replace("_", "/"), "closed": True})
                self.log_callback(f"POSITION_UPDATE::{data}")
            except Exception:
                pass

    async def _tg(self, msg: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, send_telegram, msg, TELEGRAM_SCALPER.token, TELEGRAM_SCALPER.chat_id
        )

    # ── /status command ──────────────────────────────────────────────────────

    def _handle_command(self, text: str) -> str:
        if text.startswith("/status"):
            return self._get_status_text()
        return ""

    def _get_status_text(self) -> str:
        stats = self._stats_cache or {}
        bal = stats.get("usdt_free", self.balance_usdt)
        lines = [
            "📊 *SCALPER*",
            "━━━━━━━━━━━━━━━━━",
            f"💰 Saldo: *{bal:.2f}$*",
            f"📈 Sesja: *{stats.get('session_pnl', 0.0):+.2f}$*",
            f"📊 {self._wr_text()}",
            f"🔓 Otwarte pozycje ({self._state.count()}):",
        ]
        for sym, pos_dict in self.active_positions.items():
            lines.append(f"• {sym} | Stake: {pos_dict['stake']:.2f}$")
        return "\n".join(lines)

    # ── Daily tracking ───────────────────────────────────────────────────────

    def _check_daily_reset(self) -> None:
        today = datetime.now().date()
        if today != self._day_start:
            self._day_start = today
            self._state.trades_today = 0

    def is_market_volatile(self, symbol: str) -> bool:
        return self._cache.get_atr_1m_pct(symbol) > float(self.cfg.atr_filter_min)

    def _trend_direction(self, symbol: str) -> int:
        trend_window = float(self.cfg.trend_window_sec)
        ema_period = int(self.cfg.trend_ema_period)
        ticks = self._cache.get_price_ticks(symbol, trend_window)
        if len(ticks) < ema_period:
            return 0
        last = ticks[-1][1]
        ema = self._cache.get_ema(symbol, period=ema_period, window_sec=trend_window)
        if ema is None:
            return 0
        if last > ema:
            return 1
        if last < ema:
            return -1
        return 0

    def _detect_impulse(self, symbol: str) -> Optional[dict]:
        momentum_window = float(self.cfg.momentum_window_sec)
        vol_window = float(self.cfg.volume_baseline_window_sec)
        ticks_m = self._cache.get_price_ticks(symbol, momentum_window)
        if len(ticks_m) < 3:
            return None
        price_start = ticks_m[0][1]
        last_price = ticks_m[-1][1]
        if price_start <= 0:
            return None
        price_change = (last_price - price_start) / price_start

        vol_last_m = self._cache.get_volume_sum(symbol, momentum_window)
        vol_last_base = self._cache.get_volume_sum(symbol, vol_window)
        baseline_slices = max(vol_window / max(momentum_window, 1e-9), 1.0)
        avg_vol_m_from_base = vol_last_base / baseline_slices if vol_last_base > 0 else 0.0
        if avg_vol_m_from_base <= 0:
            return None

        if price_change <= float(self.cfg.momentum_min_change):
            return None
        if vol_last_m <= float(self.cfg.volume_spike_mult) * avg_vol_m_from_base:
            return None

        prices = [p for _, p in ticks_m]
        return {
            "direction": 1,
            "impulse_low": min(prices),
            "impulse_high": max(prices),
            "ts": time.time(),
            "price_change_5s": price_change,
        }

    def detect_pullback_entry(self, symbol: str, impulse_high: float, impulse_low: float, current_price: float) -> bool:
        impulse_range = impulse_high - impulse_low
        if impulse_range <= 0:
            return False
        zone_high = impulse_high - float(self.cfg.pullback_min_retrace) * impulse_range
        zone_low = impulse_high - float(self.cfg.pullback_max_retrace) * impulse_range
        return zone_low <= current_price <= zone_high

    def _build_momentum_pullback_signal(self, symbol: str, current_price: float, reason: str) -> Signal:
        entry = current_price
        return Signal(
            symbol=symbol,
            timestamp=time.time(),
            direction=1,
            strategy="momentum_pullback",
            strength=0.8,
            entry_price=entry,
            tp_price=entry * (1 + TARGET_PROFIT_PCT),
            sl_price=entry * (1 - STOP_LOSS_PCT),
            tp_pct=TARGET_PROFIT_PCT,
            sl_pct=STOP_LOSS_PCT,
            reason=reason,
        )

    def _record_execution_metrics(self, result: TradeResult) -> None:
        self._exec_attempts += 1
        if result.exit_reason == "MISSED":
            self._exec_missed += 1
        else:
            self._exec_fills += 1
            if result.fill_latency_sec > 0:
                self._exec_fill_latencies.append(result.fill_latency_sec)

        if self._exec_attempts % 20 != 0:
            return
        fill_rate = (self._exec_fills / self._exec_attempts) * 100.0 if self._exec_attempts else 0.0
        missed_ratio = (self._exec_missed / self._exec_attempts) * 100.0 if self._exec_attempts else 0.0
        avg_fill = (
            sum(self._exec_fill_latencies) / len(self._exec_fill_latencies)
            if self._exec_fill_latencies else 0.0
        )
        self.log(
            f"[EXEC] fill_rate={fill_rate:.1f}% avg_fill_time={avg_fill:.3f}s "
            f"missed_ratio={missed_ratio:.1f}% attempts={self._exec_attempts}"
        )

    def _daily_limit_hit(self) -> bool:
        limit = -self.cfg.start_balance * self.cfg.daily_loss_limit_pct
        daily_pnl = self._stats_cache.get("daily_pnl", 0.0)
        return daily_pnl <= limit

    # ── ML Feature Collection (v3) ──────────────────────────────────────────

    def _collect_ml_features(self, symbol: str, signal: Signal,
                             features: Features, snapshot: MarketSnapshot) -> dict:
        """Collect full feature set for ML analytics from live market data."""
        now = time.time()
        cache = self._cache

        # ── v2 base features (from signal + features) ──
        f = {
            'score': round(signal.strength * 100),
            'vol_ratio': features.volume_spike_ratio,
            'body_ratio': features.micro_range_pct / 100.0 if features.micro_range_pct > 0 else 0.5,
            'atr_pct': cache.get_atr_1m_pct(symbol),
            'upper_wick_ratio': 0.0,
            'breakout_strength': 1.0 + (features.signal_strength - 0.5) * 0.1,
            'hour': datetime.now().hour,
            'day_of_week': datetime.now().weekday(),
        }

        # EMA slopes
        ema9 = cache.get_ema(symbol, period=9, window_sec=30.0)
        ema21 = cache.get_ema(symbol, period=21, window_sec=60.0)
        price = snapshot.last_price
        if ema9 and ema21 and price > 0:
            f['ema9_slope'] = (price - ema9) / price
            f['ema21_slope'] = (price - ema21) / price
            f['ema_spread'] = (ema9 - ema21) / price
        else:
            f['ema9_slope'] = 0.0
            f['ema21_slope'] = 0.0
            f['ema_spread'] = 0.0

        # BTC context
        btc_ticks = cache.get_price_ticks("BTC_USDT", 60.0)
        if len(btc_ticks) >= 5:
            btc_first, btc_last = btc_ticks[0][1], btc_ticks[-1][1]
            btc_change = (btc_last - btc_first) / btc_first if btc_first > 0 else 0
            f['btc_trend'] = 1 if btc_change > 0.0005 else (-1 if btc_change < -0.0005 else 0)
            btc_prices = [p for _, p in btc_ticks]
            btc_lo = min(btc_prices)
            f['btc_atr_pct'] = (max(btc_prices) - btc_lo) / btc_lo if btc_lo > 0 else 0
            btc_vol = cache.get_volume_sum("BTC_USDT", 60.0)
            btc_vol_base = cache.get_volume_sum("BTC_USDT", 300.0) / 5.0
            f['btc_vol_ratio'] = btc_vol / btc_vol_base if btc_vol_base > 0 else 1.0
        else:
            f['btc_trend'] = 0
            f['btc_atr_pct'] = 0.0
            f['btc_vol_ratio'] = 1.0

        # ── v3 extended features ──

        # Trend features
        ema_fast = cache.get_ema(symbol, period=9, window_sec=30.0) or 0.0
        ema_slow = cache.get_ema(symbol, period=21, window_sec=60.0) or 0.0
        f['ema_fast'] = ema_fast
        f['ema_slow'] = ema_slow
        f['ema_diff_pct'] = (ema_fast - ema_slow) / ema_slow if ema_slow > 0 else 0.0

        # VWAP approximation: sum(price*vol) / sum(vol) over 5min
        ticks_5m = cache.get_price_ticks(symbol, 300.0)
        vol_5m = cache.get_volume_sum(symbol, 300.0)
        if ticks_5m and vol_5m > 0:
            approx_vwap = sum(p * 1.0 for _, p in ticks_5m) / len(ticks_5m)
            f['price_vs_vwap'] = (price - approx_vwap) / approx_vwap if approx_vwap > 0 else 0.0
        else:
            f['price_vs_vwap'] = 0.0

        # RSI (14-period on tick data, simplified)
        ticks_60 = cache.get_price_ticks(symbol, 60.0)
        if len(ticks_60) >= 15:
            prices = [p for _, p in ticks_60]
            changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
            gains = [c for c in changes[-14:] if c > 0]
            losses = [-c for c in changes[-14:] if c < 0]
            avg_gain = sum(gains) / 14.0 if gains else 0.0
            avg_loss = sum(losses) / 14.0 if losses else 0.0001
            rs = avg_gain / avg_loss
            f['rsi'] = 100.0 - (100.0 / (1.0 + rs))
        else:
            f['rsi'] = 50.0

        # MACD histogram (EMA12 - EMA26 - signal9)
        ema12 = cache.get_ema(symbol, period=12, window_sec=60.0) or 0.0
        ema26 = cache.get_ema(symbol, period=26, window_sec=120.0) or 0.0
        macd_line = ema12 - ema26
        f['macd_hist'] = macd_line / price if price > 0 else 0.0

        # Bollinger Band width
        if len(ticks_60) >= 20:
            prices_20 = [p for _, p in ticks_60[-20:]]
            mean_p = sum(prices_20) / len(prices_20)
            std_p = (sum((p - mean_p) ** 2 for p in prices_20) / len(prices_20)) ** 0.5
            f['bb_width'] = (4.0 * std_p) / mean_p if mean_p > 0 else 0.0
        else:
            f['bb_width'] = 0.0

        # Recent range
        f['recent_range_pct'] = snapshot.price_range_1m / 100.0

        # Momentum: returns over windows
        ticks_5s = cache.get_price_ticks(symbol, 5.0)
        ticks_3s = cache.get_price_ticks(symbol, 3.0)
        if len(ticks_5s) >= 2:
            f['last_5s_return'] = (ticks_5s[-1][1] - ticks_5s[0][1]) / ticks_5s[0][1] if ticks_5s[0][1] > 0 else 0
        else:
            f['last_5s_return'] = 0.0
        if len(ticks_3s) >= 2:
            f['last_3s_return'] = (ticks_3s[-1][1] - ticks_3s[0][1]) / ticks_3s[0][1] if ticks_3s[0][1] > 0 else 0
        else:
            f['last_3s_return'] = 0.0
        f['candle_body_ratio'] = features.micro_range_pct / max(snapshot.price_range_1m, 0.001)

        # Volume features
        vol_1m = snapshot.volume_1m
        vol_5m_avg = snapshot.volume_5m_avg
        if vol_5m_avg > 0:
            f['volume_zscore'] = (vol_1m - vol_5m_avg) / max(vol_5m_avg, 1e-9)
        else:
            f['volume_zscore'] = 0.0
        vol_30s = cache.get_volume_sum(symbol, 30.0)
        vol_60s = cache.get_volume_sum(symbol, 60.0)
        f['volume_trend'] = vol_30s / max(vol_60s - vol_30s, 1e-9) if vol_60s > vol_30s else 1.0
        vol_10s = cache.get_volume_sum(symbol, 10.0)
        vol_20s = cache.get_volume_sum(symbol, 20.0)
        recent_rate = vol_10s / 10.0 if vol_10s > 0 else 0
        older_rate = (vol_20s - vol_10s) / 10.0 if (vol_20s - vol_10s) > 0 else 0.001
        f['volume_acceleration'] = recent_rate / older_rate if older_rate > 0 else 1.0

        # Orderbook / microstructure
        f['bid_ask_spread'] = snapshot.spread_pct / 100.0
        f['bid_volume'] = snapshot.bid_volume
        f['ask_volume'] = snapshot.ask_volume
        total_ob = snapshot.bid_volume + snapshot.ask_volume
        f['orderbook_imbalance'] = snapshot.bid_volume / total_ob if total_ob > 0 else 0.5

        # Trade context
        last_ts = self._last_trade_ts.get(symbol, 0.0)
        f['time_since_last_trade'] = now - last_ts if last_ts > 0 else 999.0
        f['trades_last_10m'] = self._state.trades_today

        # Market regime classification
        atr = f['atr_pct']
        bb_w = f['bb_width']
        ema_d = abs(f['ema_diff_pct'])
        if atr > 0.015 or bb_w > 0.04:
            regime = 2  # HIGH_VOLATILITY
        elif atr < 0.003 and bb_w < 0.01:
            regime = 3  # LOW_VOLATILITY
        elif ema_d > 0.002:
            regime = 1  # TREND
        else:
            regime = 0  # RANGE
        f['market_regime'] = regime

        return f

    def _wr_text(self) -> str:
        stats = self._stats_cache
        if not stats:
            return "W:0 L:0"
        w = stats.get("wins", 0)
        l = stats.get("losses", 0)
        wr = stats.get("win_rate", 0.0)
        return f"W:{w} L:{l} WR:{wr:.0f}%"

    def _warmup_ready(self) -> bool:
        if not self.market_data:
            return True
        return len(getattr(self.market_data, "last_prices", {})) >= 100

    def _refresh_scalper_pairs(self) -> bool:
        """
        Refresh local scalper universe from shared MarketData cache.
        Returns True if symbol list changed.
        """
        if not self.market_data:
            return False
        tickers = self.market_data.get_all_tickers() or {}
        if len(tickers) < 100:
            return False
        cfg_symbols = [s.replace("/", "_") for s in getattr(self.cfg, "gigants", ())]
        if not cfg_symbols:
            return False
        available = set(tickers.keys())
        new_symbols = [s for s in cfg_symbols if s.replace("_", "/") in available]
        if not new_symbols:
            new_symbols = cfg_symbols
        if new_symbols == self._symbols:
            return False
        self._symbols = new_symbols
        return True

    async def _restart_symbol_tasks(self) -> None:
        for task in self._process_tasks.values():
            task.cancel()
        for task in self._process_tasks.values():
            try:
                await task
            except Exception:
                pass
        self._process_tasks.clear()
        for symbol in self._symbols:
            self._process_tasks[symbol] = asyncio.create_task(self._symbol_loop(symbol))

    async def _pair_refresher(self) -> None:
        while self.running:
            try:
                await asyncio.sleep(float(self.cfg.pair_refresh_sec))
                if not self._warmup_ready():
                    continue
                changed = self._refresh_scalper_pairs()
                if not changed:
                    continue
                await self._restart_symbol_tasks()
                if self._ws_task:
                    self._ws_task.cancel()
                    try:
                        await self._ws_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                self._ws_task = asyncio.create_task(self._ws_main())
                self.log(f"[PAIR] Scalper pairs updated: {len(self._symbols)}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log(f"[PAIR] refresh error: {exc}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Async start — creates WS and processing tasks."""
        if self.running:
            return
        self.running = True
        self._last_ws_ok = time.time()
        loop = asyncio.get_running_loop()
        self.main_task = loop.create_task(self._run())
        if self.tg:
            self._poll_task = loop.create_task(
                self.tg.start_polling_async(self._handle_command)
            )

    async def _recover_open_positions(self) -> None:
        """
        On startup, query Gate.io for any open orders and open balances
        that match our trading symbols. Reconstruct OpenPosition objects
        and resume SL monitoring for each recovered position.
        Called once from _run() before WebSocket and symbol tasks start.
        """
        loop = asyncio.get_running_loop()
        recovered = 0

        try:
            # Fetch all open orders on spot
            open_orders = await loop.run_in_executor(
                None, lambda: self.exchange_client.fetch_open_orders()
            )
            # Fetch current balances to detect held assets
            balances = await loop.run_in_executor(
                None, lambda: self.exchange_client.fetch_balance()
            )
        except Exception as e:
            self.log(f"[RECOVERY] Błąd pobierania danych z giełdy: {e}")
            return

        for symbol in self._symbols:
            base_currency = symbol.split("_")[0]
            held = float(balances.get(base_currency, {}).get("free", 0.0))
            if held <= 0:
                continue

            ccxt_sym = symbol.replace("_", "/")
            sym_orders = [o for o in open_orders if o.get("symbol") == ccxt_sym]

            # KEY GUARD: only recover if there is an open order on exchange.
            # A position without an open order was either manually closed,
            # or is dust — do not reconstruct it.
            if not sym_orders:
                self.log(
                    f"[RECOVERY] {symbol}: held={held:.6f} {base_currency} "
                    f"but no open orders → skipping (manually closed or dust)"
                )
                continue

            tp_order_id = sym_orders[0]["id"]

            try:
                ticker = await loop.run_in_executor(
                    None, lambda: self.exchange_client.fetch_ticker(ccxt_sym)
                )
                current_price = float(ticker.get("last") or ticker.get("bid") or 0.0)
            except Exception:
                current_price = 0.0

            if current_price <= 0:
                continue

            stake = held * current_price
            if stake < 2.0:
                self.log(f"[RECOVERY] {symbol}: stake={stake:.2f}$ < 2$ — dust, skipping")
                continue

            atr = self._cache.get_atr_1m_pct(symbol)
            recovery_sl_pct = max(atr * 1.3, float(self.cfg.stop_loss_pct))

            pos = OpenPosition(
                symbol=symbol,
                direction=1,
                entry_price=current_price,
                tp_price=current_price * (1 + TARGET_PROFIT_PCT),
                sl_price=current_price * (1 - recovery_sl_pct),
                sl_pct=recovery_sl_pct,
                qty=held,
                stake=stake,
                entry_time=time.time(),
                entry_order_id="recovered",
                tp_order_id=tp_order_id,
                strategy="recovered",
            )

            self._state.add(pos)
            recovered += 1

            msg = (
                f"♻️ RECOVERY: {symbol} | held={held:.6f} {base_currency}"
                f" | ~price={current_price:.4f} | SL={pos.sl_price:.4f}"
                f" | TP_order={tp_order_id}"
            )
            self.log(msg)
            await self._tg(msg)

            asyncio.create_task(
                self._executor._monitor(pos, ccxt_sym, fill_latency_sec=0.0)
            )

        if recovered == 0:
            self.log("[RECOVERY] Brak otwartych pozycji — czyste uruchomienie")
        else:
            self.log(f"[RECOVERY] Przywrócono {recovered} pozycji — monitoring aktywny")

    async def _run(self) -> None:
        start_msg = "🏁 SCALPER START"
        self.log(start_msg)
        self.gui_log(start_msg)
        await self._tg(start_msg)

        # Verify API connectivity and fetch real balance before starting
        self._invalidate_balance_cache()
        usdt_free = await self._get_available_balance()
        if usdt_free <= 0:
            msg = "⛔ SCALPER: Brak środków USDT na koncie Gate.io — bot zatrzymany"
            self.log(msg)
            await self._tg(msg)
            self.running = False
            return
        msg = f"✅ SCALPER LIVE: Połączono z Gate.io | USDT free: {usdt_free:.2f}$"
        self.log(msg)
        self.gui_log(msg)
        await self._tg(msg)

        await self._recover_open_positions()

        if self._warmup_ready():
            self._refresh_scalper_pairs()

        self._ws_task = asyncio.create_task(self._ws_main())
        await self._restart_symbol_tasks()
        self._pair_task = asyncio.create_task(self._pair_refresher())

        while self.running:
            await asyncio.sleep(1.0)

    async def stop(self) -> None:
        """Async stop — cancels all tasks gracefully."""
        self.running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._poll_task = None

        if self._pair_task:
            self._pair_task.cancel()
            try:
                await self._pair_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._pair_task = None

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        for task in self._process_tasks.values():
            task.cancel()
        self._process_tasks.clear()

        if self.main_task:
            self.main_task.cancel()
            try:
                await self.main_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self.main_task = None

    # ── WebSocket ────────────────────────────────────────────────────────────

    async def _ws_main(self) -> None:
        """WebSocket manager with exponential-backoff reconnect."""
        retries = 0
        while self.running:
            try:
                await self._ws_connect()
                retries = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self.running:
                    break
                retries += 1
                if retries > WS_RECONNECT_MAX:
                    msg = f"⛔ SCALPER: WS reconnect nieudany po {WS_RECONNECT_MAX} próbach"
                    self.log(msg)
                    self.gui_log(msg)
                    await self._tg(msg)
                    self.running = False
                    break
                delay = WS_RECONNECT_BASE * (2 ** (retries - 1))
                self.log(f"[WS] Reconnect za {delay:.1f}s (próba {retries}): {exc}")
                await asyncio.sleep(delay)

    async def _ws_connect(self) -> None:
        """Open WS, subscribe to orderbook + trades, dispatch messages."""
        async with websockets.connect(WS_ENDPOINT, ping_interval=20, ping_timeout=30) as ws:
            self.log("[WS] Połączono z Gate.io")
            await self._ws_subscribe(ws)
            async for raw in ws:
                if not self.running:
                    break
                try:
                    msg = json.loads(raw)
                    await self._ws_dispatch(msg)
                    self._last_ws_ok = time.time()
                except Exception:
                    pass

    async def _ws_subscribe(self, ws) -> None:
        now = int(time.time())
        for symbol in self._symbols:
            await ws.send(json.dumps({
                "time": now, "channel": "spot.order_book",
                "event": "subscribe", "payload": [symbol, "5", "100ms"],
            }))
            await ws.send(json.dumps({
                "time": now, "channel": "spot.trades",
                "event": "subscribe", "payload": [symbol],
            }))

    async def _ws_dispatch(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        event   = msg.get("event", "")
        result  = msg.get("result")
        if event in ("subscribe", "unsubscribe") or result is None:
            return
        if channel == "spot.order_book" and event == "update":
            sym = result.get("s")
            if sym in self._symbols:
                self._cache.update_orderbook(sym, result)
        elif channel == "spot.trades" and event == "update":
            sym = result.get("currency_pair")
            if sym in self._symbols:
                self._cache.update_trades(sym, [result])

    # ── Processing loop ──────────────────────────────────────────────────────

    async def _symbol_loop(self, symbol: str) -> None:
        """Per-symbol pipeline every 100 ms."""
        while self.running:
            try:
                await asyncio.sleep(float(self.cfg.symbol_loop_sleep_sec))
                self._check_daily_reset()
                await self._process_symbol(symbol)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log(f"[{symbol}] loop error: {exc}")

    async def _process_symbol(self, symbol: str) -> None:
        """Momentum + pullback retail entry pipeline."""
        # Early exit if max positions already filled — no point scanning
        max_pos = int(getattr(self.cfg, "slot_count", self.cfg.max_open_positions))
        if self._state.count() >= max_pos:
            return

        if not self._warmup_ready():
            return
        if time.time() - self._last_ws_ok > EXCHANGE_TIMEOUT_SEC:
            return
        if self._cache.is_stale(symbol, max_age_ms=float(self.cfg.stale_max_age_ms)):
            return

        snapshot = self._cache.get_snapshot(symbol)
        if snapshot is None:
            return
        # Relaxed ATR filter: allow moderate volatility regimes.
        atr_1m_pct = self._cache.get_atr_1m_pct(symbol)
        _dbg_throttle = time.time() - self._debug_last_print.get(symbol, 0) > 10.0
        if atr_1m_pct < float(self.cfg.atr_filter_min) * 0.4:
            if _dbg_throttle:
                self._debug_last_print[symbol] = time.time()
                print(f"[DEBUG {symbol}] SKIP: atr_too_low atr={atr_1m_pct:.5f} "
                      f"floor={float(self.cfg.atr_filter_min)*0.4:.5f}", flush=True)
            return
        if _dbg_throttle:
            self._debug_last_print[symbol] = time.time()
            print(f"[DEBUG {symbol}] atr={atr_1m_pct:.5f} "
                  f"filter_min={float(self.cfg.atr_filter_min):.5f}", flush=True)

        signals: list[Signal] = []
        features = self._features.compute(snapshot)
        trend_dir = self._trend_direction(symbol)

        momentum_window = float(self.cfg.momentum_window_sec)
        vol_window = float(self.cfg.volume_baseline_window_sec)
        ticks_m = self._cache.get_price_ticks(symbol, momentum_window)
        if len(ticks_m) >= 3:
            price_start = ticks_m[0][1]
            last_price = ticks_m[-1][1]
            if price_start > 0:
                price_change_5s = (last_price - price_start) / price_start
                vol_last_5s = self._cache.get_volume_sum(symbol, momentum_window)
                vol_last_base = self._cache.get_volume_sum(symbol, vol_window)
                baseline_slices = max(vol_window / max(momentum_window, 1e-9), 1.0)
                baseline_volume = vol_last_base / baseline_slices if vol_last_base > 0 else 0.0
            else:
                price_change_5s = 0.0
                vol_last_5s = 0.0
                baseline_volume = 0.0
        else:
            price_change_5s = 0.0
            vol_last_5s = 0.0
            baseline_volume = 0.0

        # Skip symbols with no trade flow data (stale WS or illiquid pair)
        if baseline_volume == 0 and vol_last_5s == 0:
            if _dbg_throttle:
                print(f"[DEBUG {symbol}] SKIP: no_trade_flow (WS stale or illiquid)", flush=True)
            return

        trend_required = trend_dir == 1
        if price_change_5s > float(self.cfg.momentum_min_change) * 1.5:
            trend_required = True  # ignore trend filter on strong impulse

        impulse = self._detect_impulse(symbol)
        if impulse is not None:
            self._impulses[symbol] = impulse

        # 1) Immediate momentum breakout (no pullback wait).
        if (
            price_change_5s > float(self.cfg.momentum_min_change)
            and baseline_volume > 0
            and vol_last_5s > 1.5 * baseline_volume
            and trend_required
        ):
            entry = snapshot.last_price
            signals.append(
                Signal(
                    symbol=symbol,
                    timestamp=time.time(),
                    direction=1,
                    strategy="momentum_breakout",
                    strength=0.75,
                    entry_price=entry,
                    tp_price=entry * (1 + TARGET_PROFIT_PCT),
                    sl_price=entry * (1 - STOP_LOSS_PCT),
                    tp_pct=TARGET_PROFIT_PCT,
                    sl_pct=STOP_LOSS_PCT,
                    reason=f"breakout5s={price_change_5s:.4f} vol_spike",
                )
            )

        state = self._impulses.get(symbol)
        if state:
            if time.time() - state["ts"] > float(self.cfg.impulse_ttl_sec):
                self._impulses.pop(symbol, None)
            else:
                # Expanded pullback zone: 10% - 50%.
                impulse_range = state["impulse_high"] - state["impulse_low"]
                if impulse_range > 0:
                    zone_high = state["impulse_high"] - 0.10 * impulse_range
                    zone_low = state["impulse_high"] - 0.50 * impulse_range
                    in_zone = zone_low <= snapshot.last_price <= zone_high
                else:
                    in_zone = False
                if in_zone and trend_required:
                    signals.append(
                        self._build_momentum_pullback_signal(
                            symbol,
                            snapshot.last_price,
                            f"impulse={state['price_change_5s']:.4f} pullback",
                        )
                    )
                    self._impulses.pop(symbol, None)

        # 2) FeatureEngine breakout confirmation.
        if features.breakout_detected and features.volume_spike and trend_required:
            entry = snapshot.last_price
            signals.append(
                Signal(
                    symbol=symbol,
                    timestamp=time.time(),
                    direction=1,
                    strategy="micro_breakout",
                    strength=0.7,
                    entry_price=entry,
                    tp_price=entry * (1 + TARGET_PROFIT_PCT),
                    sl_price=entry * (1 - STOP_LOSS_PCT),
                    tp_pct=TARGET_PROFIT_PCT,
                    sl_pct=STOP_LOSS_PCT,
                    reason="feature_breakout+volume_spike",
                )
            )

        if _dbg_throttle:
            print(f"[DEBUG {symbol}] signals_generated={len(signals)} "
                  f"price_change_5s={price_change_5s:.5f} "
                  f"baseline_vol={baseline_volume:.2f} "
                  f"vol_last_5s={vol_last_5s:.2f} "
                  f"trend_dir={trend_dir} "
                  f"trend_required={trend_required}", flush=True)

        for signal in signals:
            # 1. Non-async guards (safe — no await, no race)
            now = time.time()
            if now < self._retry_after.get(signal.symbol, 0.0):
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: retry_cooldown", flush=True)
                continue
            if now - self._last_trade_ts.get(signal.symbol, 0.0) < self.symbol_cooldown_sec:
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: symbol_cooldown", flush=True)
                continue
            if signal.symbol in self._pending_orders:
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: pending_order", flush=True)
                continue
            if len(self._pending_orders) >= int(self.cfg.max_pending_orders):
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: max_pending_orders", flush=True)
                continue

            # ML guards (no await)
            adj = self.analytics.get_adjustment()
            if adj.trading_blocked:
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: ml_blocked reason={adj.block_reason}", flush=True)
                continue
            if self.analytics.is_symbol_blocked(signal.symbol):
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: symbol_blocked", flush=True)
                continue

            # Rate limit (no await)
            while self._trade_rate_window and now - self._trade_rate_window[0] > 1.0:
                self._trade_rate_window.popleft()
            if len(self._trade_rate_window) >= int(max(float(self.cfg.max_trade_rate_per_sec), 1.0)):
                continue

            # ML score check (no await)
            effective_strength = signal.strength * 100.0
            score_delta = adj.min_score_delta
            entry_quality = adj.entry_quality_score
            effective_threshold = (self._filter.MIN_STRENGTH * 100.0) + score_delta - (entry_quality * 10.0)
            if effective_strength < effective_threshold:
                continue

            # 2. ATOMIC SLOT CLAIM — must happen before any await
            max_pos = int(getattr(self.cfg, "slot_count", self.cfg.max_open_positions))
            if adj.max_positions_override is not None:
                max_pos = min(max_pos, adj.max_positions_override)

            placeholder = OpenPosition(
                symbol=signal.symbol, direction=signal.direction,
                entry_price=signal.entry_price, tp_price=signal.tp_price,
                sl_price=signal.sl_price, sl_pct=float(self.cfg.stop_loss_pct),
                qty=0.0, stake=0.0,
                entry_time=time.time(), entry_order_id="pending",
                tp_order_id=None, strategy=signal.strategy,
            )
            reserved, reserve_reason = self._state.reserve(placeholder, max_pos)
            if not reserved:
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: reserve {reserve_reason}", flush=True)
                continue

            # 3. Balance fetch (await — now safe, slot already claimed)
            balance_now = await self._get_available_balance()

            stake = self._calc_effective_stake()
            if balance_now < stake:
                stake = balance_now
            min_stake = float(self.cfg.base_stake_usdt) * 0.95
            if stake < min_stake:
                self._state.remove(signal.symbol)
                if _dbg_throttle: print(f"[DEBUG {symbol}] SKIP: stake {stake:.2f}$ < min {min_stake:.2f}$ bal={balance_now:.2f}", flush=True)
                continue

            # 4. Remaining filter checks (balance already known)
            if self._state.trades_today >= MAX_TRADES_DAY:
                self._state.remove(signal.symbol)
                continue
            if signal.strength < self._filter.MIN_STRENGTH:
                self._state.remove(signal.symbol)
                continue
            if self._daily_limit_hit():
                self._state.remove(signal.symbol)
                continue

            # 5. R:R guard — uses expected runner exit, not initial TP trigger
            # signal.tp_pct is only the runner activation threshold (e.g. 0.5%)
            # The real exit is runner trailing, conservatively estimated at 2× TP
            _atr_now = self._cache.get_atr_1m_pct(signal.symbol)
            _eff_sl = max(_atr_now * 1.3, STOP_LOSS_PCT)
            _expected_exit_pct = signal.tp_pct * 2.0   # runner adds at minimum 1× TP beyond trigger
            _net_reward = _expected_exit_pct - (MAKER_FEE + TAKER_FEE)
            _net_risk   = _eff_sl            + (MAKER_FEE + TAKER_FEE)
            if _net_risk <= 0 or (_net_reward / _net_risk) < 0.8:
                self._state.remove(signal.symbol)
                self.log(
                    f"[{signal.symbol}] RR_SKIP ratio="
                    f"{_net_reward/_net_risk if _net_risk>0 else 0:.2f}"
                )
                continue

            # 6. Proceed — slot claimed, balance checked, all filters passed
            self._invalidate_balance_cache()  # force fresh fetch on next check
            self._pending_orders.add(signal.symbol)
            self._trade_rate_window.append(now)
            ml_features = self._collect_ml_features(signal.symbol, signal, features, snapshot)
            self._log_signal(signal, stake)
            asyncio.create_task(self._execute_and_record(signal, stake, ml_features))

    async def _execute_and_record(self, signal: Signal, stake: float,
                                   ml_features: dict | None = None) -> None:
        """Execute signal, log result. Balance managed by exchange."""
        # ── Analytics entry ──
        _features_map = ml_features or {
            "score": round(signal.strength * 100),
            "vol_ratio": signal.strength,
            "body_ratio": 0.5, "atr_pct": float(self.cfg.stop_loss_pct),
            "upper_wick_ratio": 0.1, "breakout_strength": 1.001,
            "ema9_slope": 0.0, "ema21_slope": 0.0, "ema_spread": 0.0,
            "btc_trend": 1, "btc_atr_pct": 0.0, "btc_vol_ratio": 1.0,
        }
        _ml = self.analytics.on_entry(
            symbol=signal.symbol, entry_price=signal.entry_price,
            stake=stake, sl_pct=float(self.cfg.stop_loss_pct), tp1_pct=signal.tp_pct,
            features=_features_map,
        )
        self._trade_ids[signal.symbol] = _ml["trade_id"]

        # ── ML gate: reject trade if ML disapproves (level >= 2) ──
        if not _ml["approved"] and _ml["ml_level"] >= 2:
            self.log(f"[{signal.symbol}] ML REJECTED (level={_ml['ml_level']}, "
                     f"conf={_ml['confidence']:.2f})")
            self._state.remove(signal.symbol)
            self._pending_orders.discard(signal.symbol)
            self._trade_ids.pop(signal.symbol, None)
            return

        # ── ML position sizing ──
        size_mult = max(0.5, min(2.0, _ml.get("position_size_multiplier", 1.0)))
        if size_mult != 1.0:
            old_stake = stake
            stake = round(stake * size_mult, 2)
            if stake != old_stake:
                self.log(f"[{signal.symbol}] ML size adjust: ${old_stake:.2f} → ${stake:.2f} "
                         f"(x{size_mult:.2f})")

        # ── ML TP/SL adjustment ──
        adj = self.analytics.get_adjustment()
        if adj.tp_adjustment != 1.0 or adj.sl_adjustment != 1.0:
            signal.tp_pct = signal.tp_pct * max(0.5, min(2.0, adj.tp_adjustment))
            signal.sl_pct = signal.sl_pct * max(0.5, min(2.0, adj.sl_adjustment))
            signal.tp_price = signal.entry_price * (1 + signal.direction * signal.tp_pct)
            signal.sl_price = signal.entry_price * (1 - signal.direction * signal.sl_pct)
            self.log(f"[{signal.symbol}] ML TP/SL adjust: TP x{adj.tp_adjustment:.2f}, "
                     f"SL x{adj.sl_adjustment:.2f}")

        try:
            result = await self._executor.execute_signal(signal, stake)
        except Exception as exc:
            self.log(f"[{signal.symbol}] execution error: {exc}")
            self._state.remove(signal.symbol)
            self._trade_ids.pop(signal.symbol, None)
            self._retry_after[signal.symbol] = time.time() + MISSED_RETRY_COOLDOWN_SEC
            self._invalidate_balance_cache()
            self._pending_orders.discard(signal.symbol)
            self._last_trade_ts[signal.symbol] = time.time()
            return

        self._state.remove(signal.symbol)
        self._broadcast_position_closed(signal.symbol)

        if result.exit_reason == "MISSED":
            self._retry_after[signal.symbol] = time.time() + MISSED_RETRY_COOLDOWN_SEC
            self._trade_ids.pop(signal.symbol, None)
            self.log(f"[{signal.symbol}] [{signal.strategy}] MISSED")
            self._record_execution_metrics(result)
            self._pending_orders.discard(signal.symbol)
            self._last_trade_ts[signal.symbol] = time.time()
            return

        self._state.trades_today += 1

        # Real PnL from exchange (fallback to local calculation)
        _real_pnl = await self._fetch_real_pnl(
            symbol=signal.symbol,
            entry_price=result.entry_price,
            qty=result.stake / result.entry_price if result.entry_price > 0 else 0,
            after_ts=result.timestamp - result.duration,
        )
        net_pnl = _real_pnl if _real_pnl is not None \
            else round(result.pnl_usd - result.fee_paid, 2)

        self._invalidate_balance_cache()  # force fresh balance on next check
        self._stats_cache_ts = 0.0        # invalidate stats cache — force refresh

        # ── Analytics exit (with MFE/MAE) — uses local calculation, not exchange ──
        _local_pnl = result.pnl_usd - result.fee_paid
        _tid = self._trade_ids.pop(signal.symbol, None)
        if _tid:
            self.analytics.on_exit(
                trade_id=_tid,
                exit_reason=result.exit_reason,
                pnl=_local_pnl,
                pnl_pct=_local_pnl / stake if stake > 0 else 0,
                mfe=getattr(result, 'mfe', 0.0),
                mae=getattr(result, 'mae', 0.0),
            )

        # Log in project-standard format (make_log_callback parses "SELL" + "PnL:")
        # Warn if SL exit with ~zero PnL (may indicate failed sell)
        if result.exit_reason == "SL" and abs(net_pnl) < 0.01:
            self.log(
                f"⚠️ UWAGA: SL exit z PnL≈0 dla {signal.symbol.replace('_', '/')} — "
                f"sprawdź czy sprzedaż dotarła na giełdę! "
                f"entry={result.entry_price} exit={result.exit_price}"
            )

        if result.exit_reason == "RUNNER":
            icon = "🚀"
        elif result.exit_reason in ("TP", "TP_MAKER"):
            icon = "✅"
        elif result.exit_reason == "BE":
            icon = "🛡️"
        elif result.exit_reason == "SL":
            icon = "🔴"
        elif result.exit_reason == "TRAIL":
            icon = "📉"
        else:
            icon = "⚠️"
        sign = "+" if net_pnl >= 0 else ""
        ccxt_sym = signal.symbol.replace("_", "/")
        wr = self._wr_text()
        gui_msg = (
            f"{icon} SELL {ccxt_sym} | {result.exit_reason} | "
            f"Entry: {result.entry_price} | Sell: {result.exit_price} | "
            f"PnL: {sign}{net_pnl:.2f}$ | Exec: {result.fill_latency_sec:.3f}s | "
            f"Dur: {result.duration:.2f}s | {wr}"
        )
        self.set_status(f"💰 SELL {ccxt_sym}")
        self.gui_log(gui_msg)
        self.log(gui_msg)
        await self._tg(gui_msg)
        self._record_execution_metrics(result)
        self._pending_orders.discard(signal.symbol)
        self._last_trade_ts[signal.symbol] = time.time()

        if self._daily_limit_hit():
            daily_pnl = self._stats_cache.get("daily_pnl", 0.0)
            msg = f"🛑 SCALPER: Dzienny limit strat — bot zatrzymany ({round(daily_pnl, 2)}$)"
            self.log(msg)
            self.gui_log(msg)
            await self._tg(msg)
            self.running = False

    def _log_signal(self, signal: Signal, stake: float) -> None:
        """Log entry signal in standard format."""
        ts  = datetime.now().strftime("%H:%M:%S")
        ccxt_sym = signal.symbol.replace("_", "/")
        dir_label = "LONG" if signal.direction == 1 else "SHORT"
        buy_msg = (
            f"💰 BUY {ccxt_sym} | Entry Price: {signal.entry_price} | "
            f"Stawka: {stake:.2f}$"
        )
        self.set_status(f"🚀 BUY {ccxt_sym}")
        self.gui_log(buy_msg)
        self.log(buy_msg)
        self.log(
            f"[{ts}] [{signal.symbol}] [{signal.strategy}] [{dir_label}] "
            f"entry={signal.entry_price:.8f} tp={signal.tp_price:.8f} "
            f"sl={signal.sl_price:.8f} strength={signal.strength:.2f}"
        )
        asyncio.create_task(self._tg(buy_msg))
