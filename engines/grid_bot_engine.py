import asyncio
import time
import sqlite3
import requests
import urllib3
import pandas as pd
import pandas_ta as ta
import ccxt
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import GridBotConfig, EXCHANGE, TRADING_MODE
from market_data import MarketData
from vault import TELEGRAM_GRID_BOT
from analytics_engine import get_analytics
from pair_selector import select_grid_pairs
from paper_execution import PaperExecution


def send_telegram(msg, token, chat_id):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": int(chat_id), "text": msg},
            timeout=10,
            verify=False,
        )
        result = r.json()
        if not result.get("ok"):
            print(f"[TELEGRAM ERROR] API: {result.get('description')}", flush=True)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}", flush=True)


class GridBotEngine:
    def __init__(self, cfg: GridBotConfig, log_callback=None,
                 market_data: MarketData | None = None, tg=None, portfolio=None):
        self.cfg = cfg
        self.analytics = get_analytics('grid_bot')
        self._trade_ids: dict = {}
        self.log_callback = log_callback
        self.market_data = market_data
        self.tg = tg
        self.portfolio = portfolio

        self.exchange = ccxt.gateio({
            "apiKey": EXCHANGE.api_key,
            "secret": EXCHANGE.secret_key,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"}
        })
        if TRADING_MODE != "paper":
            raise RuntimeError("GridBotEngine blocks real trading. Set TRADING_MODE='paper'.")
        self.paper_execution = PaperExecution(self.portfolio, self.market_data) if self.portfolio else None

        # {pos_key: {"stake": x, "original_stake": x}} â€” consumed by SharedPortfolio
        self.active_positions: dict = {}

        # {symbol: {center, levels, buy_orders, sell_orders, built_at, atr_val}}
        self._grid: dict = {}

        self.balance_in_wallet = cfg.start_balance
        self.wins = 0
        self.losses = 0
        self._daily_loss = 0.0
        self._day_start = datetime.now().date()

        self._current_symbol: str | None = None      # compat: first active symbol
        self._active_symbols: set[str] = set()        # all symbols with running grids
        self._grid_tasks: dict[str, asyncio.Task] = {}
        self._grid_pairs: list[str] = []
        self._last_pairs_refresh: float = 0.0
        self._pair_log_emitted = False
        self._pending_grid_update = False
        self._last_grid_update: float = 0.0
        self._last_slots_update: float = 0.0
        self._grid_update_lock = asyncio.Lock()
        self.running = False
        self.main_task = None
        self._poll_task = None

    @staticmethod
    def _is_excluded_symbol(symbol: str) -> bool:
        s = str(symbol or "").upper().replace("_", "/").replace("-", "")
        return s in {"PAXG/USDT", "PAXGUSDT"}

    # ============================
    #   STATUS / LOGGING
    # ============================
    def set_status(self, text: str):
        if self.log_callback:
            self.log_callback(f"STATUS_UPDATE::{text}")

    def log(self, msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def gui_log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)

    def log_both(self, msg: str):
        self.log(msg)
        self.gui_log(msg)

    def update_stake_per_grid(self, stake_usd: float):
        self.cfg.position_size_usdt = float(stake_usd)
        self.cfg.max_stake_per_trade = float(stake_usd)
        self._pending_grid_update = True
        self._last_grid_update = time.monotonic()

    def update_max_positions(self, new_value: int):
        self.cfg.max_positions = int(new_value)
        self._pending_grid_update = True
        self._last_grid_update = time.monotonic()
        self._last_slots_update = self._last_grid_update

    def update_runtime_settings(self, updates: dict):
        for key, value in updates.items():
            if hasattr(self.cfg, key):
                setattr(self.cfg, key, value)
        self._pending_grid_update = True
        self._last_grid_update = time.monotonic()

    async def _tg(self, msg: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, send_telegram, msg, TELEGRAM_GRID_BOT.token, TELEGRAM_GRID_BOT.chat_id
        )

    def _wr_text(self) -> str:
        total = self.wins + self.losses
        if total == 0:
            return "W:0 L:0"
        wr = self.wins / total * 100
        return f"W:{self.wins} L:{self.losses} WR:{wr:.0f}%"

    def get_status_text(self) -> str:
        bal = self.portfolio.get_balance("USDT") if self.portfolio else self.balance_in_wallet
        total = self.wins + self.losses
        win_pct = f"{self.wins / total * 100:.0f}" if total > 0 else "0"
        lines = [
            "GRID BOT",
            "-----------------",
            f"Balance: {bal:.2f}$",
            f"Win Rate: W:{self.wins}/L:{self.losses} ({win_pct}%)",
            f"Dzienna strata: {self._daily_loss:.2f}$",
            f"Symbol: {self._current_symbol or 'brak'}",
        ]
        sym = self._current_symbol
        if sym and sym in self._grid:
            g = self._grid[sym]
            lines.append(
                f"Grid: {len(g.get('buy_orders', {}))} buy / "
                f"{len(g.get('sell_orders', {}))} sell"
            )
        for pk, pos in self.active_positions.items():
            lines.append(f"- {pk} | Stake: {pos['stake']:.2f}$")
        return "\n".join(lines)

    def _handle_command(self, text: str) -> str:
        if text.startswith("/status"):
            return self.get_status_text()
        return ""

    # ============================
    #   DAILY RESET
    # ============================
    def _check_daily_reset(self):
        today = datetime.now().date()
        if today != self._day_start:
            self._day_start = today
            self._daily_loss = 0.0

    def _daily_limit_hit(self) -> bool:
        return self._daily_loss >= self.cfg.start_balance * self.cfg.daily_loss_limit_pct

    # ============================
    #   DATABASE
    # ============================
    def init_db(self):
        conn = sqlite3.connect(self.cfg.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS grid_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                symbol TEXT,
                buy_price REAL,
                sell_price REAL,
                qty REAL,
                stake REAL,
                pnl REAL,
                reason TEXT
            )
        """)
        conn.commit()
        conn.close()

    def log_trade_to_db(self, symbol, buy_price, sell_price, qty, stake, pnl, reason):
        try:
            conn = sqlite3.connect(self.cfg.db_path)
            c = conn.cursor()
            c.execute(
                "INSERT INTO grid_trades (ts, symbol, buy_price, sell_price, qty, stake, pnl, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), symbol, buy_price, sell_price, qty, stake, pnl, reason)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.log_both(f"DB write error: {e}")

    # ============================
    #   SYMBOL SELECTION
    # ============================
    async def _select_symbol(self) -> str | None:
        """Pick best symbol for grid: moderate volume, low recent volatility."""
        if not self.market_data:
            return None
        tickers = self.market_data.get_all_tickers() or {}
        if len(tickers) < 100:
            return None
        loop = asyncio.get_running_loop()

        candidates = []
        for sym in self._grid_pairs:
            if sym in self._active_symbols:
                continue
            if self._is_excluded_symbol(sym):
                continue
            t = tickers.get(sym, {})
            if self.cfg.symbol_filter not in sym or ':' in sym or '-' in sym:
                continue
            if any(bl in sym for bl in self.cfg.blacklist):
                continue
            vol = t.get('quoteVolume') or 0
            if not (self.cfg.vol_min <= vol <= self.cfg.vol_max):
                continue
            if (t.get('last') or 0) <= 0:
                continue
            candidates.append(sym)

        if not candidates:
            return None

        # Choose symbol with lowest coefficient of variation (calmest price action)
        best: str | None = None
        best_cv = float('inf')
        for sym in candidates[:self.cfg.max_symbols_to_scan]:
            try:
                ohlcv = await loop.run_in_executor(
                    None, lambda s=sym: self.exchange.fetch_ohlcv(s, '1h', limit=24)
                )
                if not ohlcv or len(ohlcv) < 5:
                    continue
                closes = [c[4] for c in ohlcv]
                mean = sum(closes) / len(closes)
                variance = sum((c - mean) ** 2 for c in closes) / len(closes)
                cv = (variance ** 0.5) / mean if mean > 0 else float('inf')
                if cv < best_cv:
                    best_cv = cv
                    best = sym
            except Exception:
                continue

        return best

    def _refresh_grid_pairs(self, force: bool = False) -> bool:
        if not self.market_data:
            return False
        now = time.monotonic()
        if not force and (now - self._last_pairs_refresh) < 300.0:
            return False
        tickers = self.market_data.get_all_tickers() or {}
        if len(tickers) < 100:
            return False
        selected = select_grid_pairs(tickers, limit=200)
        if not selected:
            return False
        selected = [s for s in selected if not self._is_excluded_symbol(s)]
        if not selected:
            return False
        self._last_pairs_refresh = now
        if selected == self._grid_pairs:
            return False
        self._grid_pairs = selected
        if not self._pair_log_emitted:
            self.log_both(f"[PAIR] Grid pairs updated: {len(self._grid_pairs)}")
            self._pair_log_emitted = True
        return True

    # ============================
    #   ATR
    # ============================
    async def _fetch_atr(self, symbol: str) -> float | None:
        loop = asyncio.get_running_loop()
        try:
            ohlcv = await loop.run_in_executor(
                None,
                lambda: self.exchange.fetch_ohlcv(
                    symbol, self.cfg.atr_timeframe, limit=self.cfg.atr_period + 5
                )
            )
            if not ohlcv or len(ohlcv) < self.cfg.atr_period:
                return None
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
            atr_series = ta.atr(df["high"], df["low"], df["close"], length=self.cfg.atr_period)
            val = float(atr_series.iloc[-1])
            return val if val > 0 else None
        except Exception as e:
            self.log_both(f"_fetch_atr {symbol} error: {e}")
            return None

    # ============================
    #   GRID CALCULATION
    # ============================
    def _compute_levels(self, mid_price: float, atr_val: float) -> list[float]:
        """Return N price levels below mid for buy orders."""
        spacing = max(
            atr_val * self.cfg.atr_spacing_mult,
            mid_price * self.cfg.grid_spacing_pct
        )
        return [
            round(mid_price - i * spacing, 8)
            for i in range(1, self.cfg.grid_levels + 1)
            if mid_price - i * spacing > 0
        ]

    async def _place_buy_orders(self, symbol: str, levels: list[float],
                                stake_per_grid: float) -> dict:
        """Place post-only limit buys at each level. Returns {order_id: meta}."""
        loop = asyncio.get_running_loop()
        params = {"postOnly": True, "timeInForce": "PO"}
        cfg = self.cfg
        placed = {}
        for level_price in levels:
            if level_price <= 0:
                continue
            qty = round(stake_per_grid / level_price, 6)
            if qty <= 0:
                continue
            stake = cfg.position_size_usdt
            if self.portfolio and not self.portfolio.reserve_balance("USDT", stake):
                continue
            try:
                order = await loop.run_in_executor(
                    None,
                    lambda lp=level_price: self.paper_execution.create_limit_buy_order(
                        symbol, qty, lp, params
                    )
                )
                placed[order['id']] = {
                    "level_price": level_price,
                    "qty": qty,
                    "stake": stake,
                }
                self.log_both(f"Grid BUY placed {symbol} @ {level_price:.8f} | Stake: {stake:.2f}$")
            except Exception as e:
                if self.portfolio:
                    self.portfolio.release_balance("USDT", stake)
                self.log_both(f"Grid BUY failed {symbol} @ {level_price:.8f}: {e}")
        return placed

    async def _cancel_all_grid_orders(self, symbol: str):
        """Cancel all open buy and sell orders for the grid."""
        loop = asyncio.get_running_loop()
        g = self._grid.get(symbol, {})
        for oid in list(g.get("buy_orders", {}).keys()):
            try:
                await loop.run_in_executor(
                    None, lambda o=oid: self.paper_execution.cancel_order(o, symbol)
                )
            except Exception:
                pass
        for oid in list(g.get("sell_orders", {}).keys()):
            try:
                await loop.run_in_executor(
                    None, lambda o=oid: self.paper_execution.cancel_order(o, symbol)
                )
            except Exception:
                pass

    async def _apply_incremental_slot_update(self, symbol: str, g: dict, cur_price: float):
        async with self._grid_update_lock:
            cfg = self.cfg
            center = g.get("center") or cur_price
            atr_val = g.get("atr_val") or (cur_price * cfg.grid_spacing_pct * cfg.grid_levels)
            now = time.monotonic()

            target_levels = self._compute_levels(center, atr_val)
            if cfg.max_positions > 0:
                target_levels = target_levels[:cfg.max_positions]

            current_levels = list(g.get("levels", []))
            current_set = set(current_levels)
            to_add = [lvl for lvl in target_levels if lvl not in current_set]
            to_remove = [lvl for lvl in current_levels if lvl not in set(target_levels)]

            if to_remove:
                remove_set = set(to_remove)
                loop = asyncio.get_running_loop()
                for oid, meta in list(g.get("buy_orders", {}).items()):
                    if meta.get("level_price") not in remove_set:
                        continue
                    try:
                        await loop.run_in_executor(
                            None, lambda o=oid: self.paper_execution.cancel_order(o, symbol)
                        )
                    except Exception:
                        pass
                    if self.portfolio:
                        self.portfolio.release_balance("USDT", meta.get("stake", 0.0))
                    g["buy_orders"].pop(oid, None)

            existing_buy_levels = {
                meta.get("level_price")
                for meta in g.get("buy_orders", {}).values()
                if isinstance(meta, dict)
            }
            to_add = [lvl for lvl in to_add if lvl not in existing_buy_levels]
            if to_add and cfg.position_size_usdt > 0:
                placed = await self._place_buy_orders(symbol, to_add, cfg.position_size_usdt)
                g["buy_orders"].update(placed)

            g["levels"] = target_levels
            g["center"] = center
            g["atr_val"] = atr_val
            g["built_at"] = now
            self._last_grid_update = now
            self._last_slots_update = self._last_grid_update
            if to_add or to_remove:
                self.log_both(
                    f"[GRID] Slots update | added: {len(to_add)} removed: {len(to_remove)} target: {len(target_levels)}"
                )

    # ============================
    #   FILL MONITORING
    # ============================
    async def _check_fills(self, symbol: str):
        """Poll buy and sell orders; handle fills."""
        loop = asyncio.get_running_loop()
        g = self._grid[symbol]
        cfg = self.cfg

        # --- Buy fills ---
        for oid, meta in list(g["buy_orders"].items()):
            try:
                order = await loop.run_in_executor(
                    None, lambda o=oid: self.paper_execution.fetch_order(o, symbol)
                )
            except Exception:
                continue

            status = order.get('status', '')
            if status == 'closed':
                fill_price = float(
                    order.get('average') or order.get('price') or meta['level_price']
                )
                qty = meta['qty']
                stake = qty * fill_price * (1 + cfg.maker_fee)
                del g["buy_orders"][oid]

                pos_key = f"{symbol}:{oid}"
                self.active_positions[pos_key] = {
                    "stake": stake,
                    "original_stake": stake,
                }

                _ml = self.analytics.on_entry(
                    symbol=symbol, entry_price=fill_price, stake=stake,
                    sl_pct=cfg.grid_spacing_pct * cfg.grid_levels,
                    tp1_pct=cfg.sell_above_buy_pct,
                    features={"grid_level": fill_price, "maker_fee": cfg.maker_fee},
                )
                self._trade_ids[pos_key] = _ml['trade_id']

                buy_msg = (
                    f"BUY {symbol} | Entry Price: {fill_price:.8f} "
                    f"| Stawka: {stake:.2f}$"
                )
                self.log_both(buy_msg)
                await self._tg(buy_msg)

                # Place corresponding sell order
                sell_price = round(
                    fill_price * (1 + 2 * cfg.sell_above_buy_pct + cfg.maker_fee), 8
                )
                try:
                    params = {"postOnly": True, "timeInForce": "PO"}
                    sell_order = await loop.run_in_executor(
                        None,
                        lambda sp=sell_price: self.paper_execution.create_limit_sell_order(
                            symbol, qty, sp, params
                        )
                    )
                    g["sell_orders"][sell_order['id']] = {
                        "buy_price":  fill_price,
                        "sell_price": sell_price,
                        "qty":        qty,
                        "stake":      stake,
                        "pos_key":    pos_key,
                    }
                    self.log_both(f"Grid SELL placed {symbol} @ {sell_price:.8f}")
                except Exception as e:
                    self.log_both(f"Grid SELL order failed {symbol}: {e}")

            elif status in ('canceled', 'expired', 'rejected'):
                if self.portfolio:
                    self.portfolio.release_balance("USDT", meta.get("stake", 0.0))
                del g["buy_orders"][oid]

        # --- Sell fills ---
        for oid, meta in list(g["sell_orders"].items()):
            try:
                order = await loop.run_in_executor(
                    None, lambda o=oid: self.paper_execution.fetch_order(o, symbol)
                )
            except Exception:
                continue

            status = order.get('status', '')
            if status == 'closed':
                sell_price = float(
                    order.get('average') or order.get('price') or meta['sell_price']
                )
                buy_price = meta['buy_price']
                qty = meta['qty']
                stake = meta['stake']
                pos_key = meta['pos_key']

                proceeds = qty * sell_price * (1 - cfg.maker_fee)
                pnl = proceeds - stake
                win = pnl > 0
                icon = "âœ…" if win else "âŒ"
                if win:
                    self.wins += 1
                else:
                    self.losses += 1
                    self._daily_loss += abs(pnl)

                if self.portfolio:
                    self.portfolio.release_balance("USDT", stake)
                else:
                    self.balance_in_wallet += proceeds

                wr = self._wr_text()
                sell_msg = (
                    f"{icon} SELL {symbol} | GRID_TP | Entry: {buy_price:.8f} | "
                    f"Sell: {sell_price:.8f} | PnL: {pnl:.2f}$ | {wr}"
                )
                self.log_both(sell_msg)
                await self._tg(sell_msg)

                self.log_trade_to_db(symbol, buy_price, sell_price, qty, stake, pnl, 'GRID_TP')

                _tid = self._trade_ids.pop(pos_key, None)
                if _tid:
                    self.analytics.on_exit(
                        trade_id=_tid, exit_reason='GRID_TP',
                        pnl=pnl, pnl_pct=pnl / stake if stake > 0 else 0,
                    )

                self.active_positions.pop(pos_key, None)
                del g["sell_orders"][oid]

            elif status in ('canceled', 'expired', 'rejected'):
                if self.portfolio:
                    self.portfolio.release_balance("USDT", meta.get("stake", 0.0))
                self.active_positions.pop(meta.get('pos_key', ''), None)
                del g["sell_orders"][oid]

    # ============================
    #   GRID LOOP
    # ============================
    async def _run_grid(self, symbol: str):
        """Grid trading loop for one symbol."""
        cfg = self.cfg
        loop = asyncio.get_running_loop()
        self.log_both(f"Grid bot starting on {symbol}")
        await self._tg(f"*GRID BOT START* | {symbol}")

        self._grid[symbol] = {
            "center":      0.0,
            "levels":      [],
            "buy_orders":  {},
            "sell_orders": {},
            "built_at":    0.0,
            "atr_val":     0.0,
        }

        last_fill_check = 0.0

        while self.running:
            try:
                self._check_daily_reset()
                if self._daily_limit_hit():
                    msg = f"GRID: Dzienny limit strat ({self._daily_loss:.2f}$)"
                    self.set_status(msg)
                    self.log_both(msg)
                    await self._tg(msg)
                    break

                g = self._grid[symbol]
                now = time.monotonic()

                # Get current price
                cur_price = None
                if self.market_data:
                    cur_price = self.market_data.get_last_price(symbol)
                if cur_price is None:
                    try:
                        ticker = await loop.run_in_executor(
                            None, lambda: self.exchange.fetch_ticker(symbol)
                        )
                        cur_price = ticker.get('last')
                    except Exception:
                        pass
                if cur_price is None:
                    await asyncio.sleep(2)
                    continue

                did_incremental_update = False
                if self._pending_grid_update:
                    await self._apply_incremental_slot_update(symbol, g, cur_price)
                    self._pending_grid_update = False
                    did_incremental_update = True

                # Decide if grid needs rebuild
                needs_rebuild = (
                    (not did_incremental_update)
                    and (now - self._last_grid_update >= 5.0)
                    and (now - self._last_slots_update >= 5.0)
                    and (not g["levels"]
                    or (
                        g["center"] > 0
                        and abs(cur_price - g["center"]) / g["center"] >= cfg.grid_reset_pct
                    ))
                )

                if needs_rebuild:
                    self.set_status(f"Rebuilding grid {symbol}...")
                    # Cancel open buys only; let active sells manage their own fills
                    cancelled_buy_ids: list[str] = []
                    for oid in list(g["buy_orders"].keys()):
                        try:
                            await loop.run_in_executor(
                                None, lambda o=oid: self.paper_execution.cancel_order(o, symbol)
                            )
                            cancelled_buy_ids.append(oid)
                        except Exception:
                            pass
                    for oid in cancelled_buy_ids:
                        g["buy_orders"].pop(oid, None)

                    atr_val = await self._fetch_atr(symbol)
                    if atr_val is None:
                        atr_val = cur_price * cfg.grid_spacing_pct * cfg.grid_levels
                    g["atr_val"] = atr_val

                    levels = self._compute_levels(cur_price, atr_val)
                    if cfg.max_positions > 0:
                        levels = levels[:cfg.max_positions]
                    g["levels"] = levels
                    g["center"] = cur_price
                    g["built_at"] = now

                    stake_per_grid = cfg.position_size_usdt
                    existing_buy_levels = {
                        meta.get("level_price")
                        for meta in g["buy_orders"].values()
                        if isinstance(meta, dict)
                    }
                    levels_to_place = [lvl for lvl in levels if lvl not in existing_buy_levels]

                    if stake_per_grid > 0 and levels_to_place:
                        placed = await self._place_buy_orders(symbol, levels_to_place, stake_per_grid)
                        g["buy_orders"].update(placed)
                        self.log_both(f"Grid rebuilt: {len(placed)}/{len(levels)} orders on {symbol}")
                        self.set_status(
                            f"Grid aktywny: {len(placed)} leveli | {symbol}"
                        )
                    else:
                        self.log_both(f"Insufficient balance for grid on {symbol}")
                        await asyncio.sleep(30)
                        continue

                # Poll fills
                if now - last_fill_check >= cfg.order_check_interval_s:
                    await self._check_fills(symbol)
                    last_fill_check = now

                buy_cnt = len(g["buy_orders"])
                sell_cnt = len(g["sell_orders"])
                if self._daily_loss > 0:
                    pnl_label = "Strata"
                    pnl_value = self._daily_loss
                else:
                    pnl_label = "Zysk"
                    pnl_value = 0.0
                self.set_status(
                    f"Grid {symbol} | Buy: {buy_cnt} | Sell: {sell_cnt} | "
                    f"{pnl_label}: {pnl_value:.2f}$ | Aktualna Cena: {cur_price:.8f}"
                )
                sleep_total = max(float(cfg.loop_sleep), 0.0)
                if sleep_total <= 0:
                    await asyncio.sleep(0)
                    continue

                elapsed = 0.0
                live_tick = 0.25
                while self.running and elapsed < sleep_total:
                    step = min(live_tick, sleep_total - elapsed)
                    await asyncio.sleep(step)
                    elapsed += step

                    live_price = self.market_data.get_last_price(symbol) if self.market_data else None
                    if live_price is None:
                        continue
                    self.set_status(
                        f"Grid {symbol} | Buy: {buy_cnt} | Sell: {sell_cnt} | "
                        f"{pnl_label}: {pnl_value:.2f}$ | Aktualna Cena: {live_price:.8f}"
                    )

            except Exception as e:
                self.log_both(f"_run_grid {symbol} error: {e}")
                await asyncio.sleep(5)

        await self._cancel_all_grid_orders(symbol)
        self._grid.pop(symbol, None)
        self._active_symbols.discard(symbol)
        self._grid_tasks.pop(symbol, None)
        if self._current_symbol == symbol:
            self._current_symbol = next(iter(self._active_symbols), None)
        self.log_both(f"Grid bot stopped on {symbol}")

    # ============================
    #   MAIN RUN LOOP
    # ============================
    async def run(self):
        self.set_status("Grid Bot startuje...")
        start_msg = "GRID BOT START"
        self.log_both(start_msg)
        await self._tg(start_msg)
        self.init_db()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.exchange.load_markets)
        self._refresh_grid_pairs(force=True)

        while self.running:
            try:
                self._check_daily_reset()
                if self.market_data and len(self.market_data.last_prices) < 100:
                    self.set_status("Warmup MarketData (<100 symboli) - trading pause")
                    await asyncio.sleep(2)
                    continue
                self._refresh_grid_pairs()
                if self._daily_limit_hit():
                    self.set_status("Dzienny limit strat")
                    await asyncio.sleep(60)
                    continue

                # Clean up finished tasks
                for sym in list(self._grid_tasks):
                    task = self._grid_tasks.get(sym)
                    if task and task.done():
                        self._grid_tasks.pop(sym, None)
                        self._active_symbols.discard(sym)

                # Start grids up to max_active_symbols
                max_sym = max(1, int(self.cfg.max_active_symbols))
                while len(self._active_symbols) < max_sym:
                    self.set_status(f"Szukam symbolu do gridu... ({len(self._active_symbols)}/{max_sym})")
                    sym = await self._select_symbol()
                    if sym is None or sym in self._active_symbols:
                        break
                    self._active_symbols.add(sym)
                    if self._current_symbol is None:
                        self._current_symbol = sym
                    task = loop.create_task(self._run_grid(sym))
                    self._grid_tasks[sym] = task
                    self.log_both(f"Grid task created for {sym} ({len(self._active_symbols)}/{max_sym})")

                if not self._active_symbols:
                    self.log_both("No suitable symbol found, retrying in 60s")
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(30)

            except Exception as e:
                self.log_both(f"run() error: {e}")
                await asyncio.sleep(10)

    # ============================
    #   START / STOP
    # ============================
    async def start(self):
        if self.running:
            return
        self.running = True
        self._pair_log_emitted = False
        loop = asyncio.get_running_loop()
        self.main_task = loop.create_task(self.run())
        if self.tg:
            self._poll_task = loop.create_task(
                self.tg.start_polling_async(self._handle_command)
            )

    async def stop(self):
        self.running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except Exception:
                pass
            self._poll_task = None

        if self.main_task:
            self.main_task.cancel()
            try:
                await self.main_task
            except Exception:
                pass
            self.main_task = None


