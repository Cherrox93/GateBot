import sqlite3
import threading
import time
from datetime import datetime

import ccxt
import requests
import urllib3

from analytics_engine import get_analytics
from config import EXCHANGE, TRADING_MODE, LowcapConfig
from market_data import MarketData
from paper_execution import PaperExecution
from vault import TELEGRAM_LOWCAP

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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


class LowcapEngine:
    """Micro-Reversion Ping-Pong engine for small-capital paper trading."""

    def __init__(self, cfg: LowcapConfig, log_callback=None,
                 market_data: MarketData | None = None, tg=None, portfolio=None):
        self.cfg = cfg
        self.market_data = market_data
        self.tg = tg
        self.portfolio = portfolio
        self.log_callback = log_callback
        self.current_status = self.cfg.status.default_status_text

        if TRADING_MODE != "paper":
            raise RuntimeError("LowcapEngine blocks real trading. Set TRADING_MODE='paper'.")

        self.exchange = ccxt.gateio({
            "apiKey": EXCHANGE.api_key,
            "secret": EXCHANGE.secret_key,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        self.paper_execution = PaperExecution(self.portfolio, self.market_data) if self.portfolio else None

        self.analytics = get_analytics("lowcap")
        self._trade_ids: dict[str, str] = {}

        self.running = False
        self.stop_event = threading.Event()
        self.thread = None

        self.balance = self.cfg.start_balance
        self.total_session_profit = 0.0
        self.wins = 0
        self.losses = 0
        self._daily_loss = 0.0

        self.active_positions: dict = {}
        self._pos_lock = threading.Lock()
        self.active_pairs: dict[str, threading.Thread] = {}
        self._pairs_lock = threading.Lock()
        self._pair_stats: dict[str, dict] = {}
        self._scan_universe: list[str] = []
        self._last_scan_refresh = 0.0

        self.init_db()

    # ----------------------------
    # Logging / status
    # ----------------------------
    def set_status(self, text: str):
        if text != self.current_status:
            self.current_status = text
            if self.log_callback and self.cfg.status.show_status_line:
                try:
                    self.log_callback(f"STATUS_UPDATE::{text}")
                except Exception:
                    pass

    def gui_log(self, msg: str):
        if self.log_callback:
            try:
                self.log_callback(msg)
            except Exception:
                pass

    def log(self, msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def _send_tg(self, msg: str):
        self.log(msg)
        send_telegram(msg, TELEGRAM_LOWCAP.token, TELEGRAM_LOWCAP.chat_id)

    def _wr_text(self) -> str:
        total = self.wins + self.losses
        if total == 0:
            return "W:0 L:0"
        wr = self.wins / total * 100
        return f"W:{self.wins} L:{self.losses} WR:{wr:.0f}%"

    def get_status_text(self) -> str:
        bal = self.portfolio.balance_usdt if self.portfolio else self.balance
        with self._pos_lock:
            pos_count = len(self.active_positions)
            pos_lines = [
                f"- {sym} | Orders: {len(pos.get('orders', []))} | Stake: {pos.get('stake', 0.0):.2f}$"
                for sym, pos in self.active_positions.items()
            ]
        total = self.wins + self.losses
        win_pct = f"{self.wins / total * 100:.0f}" if total > 0 else "0"
        lines = [
            "MICRO-REVERSION PING-PONG",
            f"Balance: {bal:.2f}$",
            f"Win Rate: W:{self.wins}/L:{self.losses} ({win_pct}%)",
            f"Active pairs ({pos_count}/{self.cfg.max_pairs}):",
        ] + pos_lines
        return "\n".join(lines)

    def _handle_command(self, text: str) -> str:
        if text.startswith("/status"):
            return self.get_status_text()
        return ""

    # ----------------------------
    # Database
    # ----------------------------
    def init_db(self):
        try:
            con = sqlite3.connect(self.cfg.db_path)
            cur = con.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    engine TEXT,
                    symbol TEXT,
                    side TEXT,
                    price REAL,
                    qty REAL,
                    stake REAL,
                    pnl REAL,
                    reason TEXT,
                    spread_captured REAL DEFAULT 0
                )
                """
            )
            con.commit()
            con.close()
        except Exception as e:
            self.log(f"DB init error: {e}")

    def log_trade_to_db(self, symbol, side, price, qty, stake, pnl, reason):
        try:
            con = sqlite3.connect(self.cfg.db_path)
            cur = con.cursor()
            cur.execute(
                "INSERT INTO trades (ts, engine, symbol, side, price, qty, stake, pnl, reason, spread_captured) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), "lowcap", symbol, side, price, qty, stake, pnl, reason, 0.0),
            )
            con.commit()
            con.close()
        except Exception as e:
            self.log(f"DB write error: {e}")

    # ----------------------------
    # Pair scanning
    # ----------------------------
    def _refresh_universe(self, force: bool = False):
        if not self.market_data:
            return
        now = time.monotonic()
        if not force and now - self._last_scan_refresh < self.cfg.rescan_interval_s:
            return
        tickers = self.market_data.get_all_tickers() or {}
        if len(tickers) < 50:
            return

        candidates = []
        for sym, t in tickers.items():
            if "/USDT" not in sym or ":" in sym or "-" in sym:
                continue
            if any(b in sym for b in self.cfg.blacklist):
                continue
            vol = float(t.get("quoteVolume") or 0)
            if not (self.cfg.vol_min <= vol <= self.cfg.vol_max):
                continue
            bid = float(t.get("bid") or 0)
            ask = float(t.get("ask") or 0)
            last = float(t.get("last") or 0)
            if bid <= 0 or ask <= 0 or last <= self.cfg.price_min:
                continue
            spread_pct = (ask - bid) / max(bid, 1e-12)
            if not (self.cfg.spread_min_pct <= spread_pct <= self.cfg.spread_max_pct):
                continue
            pct = abs(float(t.get("percentage") or 0)) / 100.0
            if pct < self.cfg.min_daily_change_pct:
                continue
            candidates.append((sym, vol))

        candidates.sort(key=lambda x: x[1], reverse=True)
        self._scan_universe = [s for s, _ in candidates[: self.cfg.max_scan_pairs]]
        self._last_scan_refresh = now
        self.log(f"[PAIR] Micro-Reversion pairs updated: {len(self._scan_universe)}")

    def scan_pairs(self) -> list[str]:
        return list(self._scan_universe)

    # ----------------------------
    # Trading primitives
    # ----------------------------
    def _fetch_bid_ask_last(self, symbol: str) -> tuple[float | None, float | None, float | None]:
        bid = self.market_data.get_bid_price(symbol) if self.market_data else None
        ask = self.market_data.get_ask_price(symbol) if self.market_data else None
        last = self.market_data.get_last_price(symbol) if self.market_data else None
        return bid, ask, last

    def _ensure_pair_state(self, symbol: str, price: float):
        if symbol not in self._pair_stats:
            self._pair_stats[symbol] = {
                "local_high": price,
                "local_low": price,
                "last_entry_ts": 0.0,
            }

    def _sync_position_snapshot(self, symbol: str):
        with self._pos_lock:
            pair = self._pair_stats.get(symbol, {})
            orders = pair.get("orders", [])
            if orders:
                total = sum(o["stake"] for o in orders)
                self.active_positions[symbol] = {
                    "stake": total,
                    "original_stake": total,
                    "orders": list(orders),
                }
            else:
                self.active_positions.pop(symbol, None)

    def _open_buy(self, symbol: str, ask_price: float):
        pair = self._pair_stats[symbol]
        orders = pair.setdefault("orders", [])

        if len(orders) >= int(self.cfg.max_orders_per_pair):
            return

        now = time.monotonic()
        if now - float(pair.get("last_entry_ts", 0.0)) < float(self.cfg.entry_cooldown_s):
            return

        avail = self.portfolio.get_balance("USDT") if self.portfolio else self.balance
        stake = min(float(self.cfg.max_stake_per_trade), avail)
        if stake < 1.0 or ask_price <= 0:
            return

        qty = stake / ask_price

        if self.portfolio and not self.portfolio.reserve_balance("USDT", stake):
            return

        try:
            order = self.paper_execution.create_order(symbol, "market", "buy", qty)
            fill = float(order.get("average") or order.get("price") or ask_price)
        except Exception as e:
            if self.portfolio:
                self.portfolio.release_balance("USDT", stake)
            self.log(f"BUY failed {symbol}: {e}")
            return

        pos = {
            "buy_price": fill,
            "qty": qty,
            "stake": stake,
            "low_since_buy": fill,
            "peak_since_buy": fill,
            "trail_active": False,
            "trail_stop": None,
            "open_ts": time.monotonic(),
        }
        orders.append(pos)
        pair["last_entry_ts"] = now

        _ml = self.analytics.on_entry(
            symbol=symbol,
            entry_price=fill,
            stake=stake,
            sl_pct=float(self.cfg.stop_loss_pct),
            tp1_pct=float(self.cfg.sell_rise_min_pct),
            features={"mode": "micro_reversion", "drop_min": self.cfg.buy_drop_min_pct},
        )
        self._trade_ids[f"{symbol}:{id(pos)}"] = _ml["trade_id"]

        msg = f"BUY {symbol} | Entry: {fill:.8f} | Stake: {stake:.2f}$"
        self.gui_log(msg)
        self.log(msg)

    def _close_sell(self, symbol: str, pos: dict, sell_price: float, reason: str):
        qty = float(pos["qty"])
        stake = float(pos["stake"])
        try:
            order = self.paper_execution.create_order(symbol, "market", "sell", qty)
            sell_fill = float(order.get("average") or order.get("price") or sell_price)
        except Exception as e:
            self.log(f"SELL failed {symbol}: {e}")
            return False

        gross = qty * sell_fill
        fees = stake * float(self.cfg.maker_fee) + gross * float(self.cfg.maker_fee)
        pnl = gross - stake - fees

        if self.portfolio:
            self.portfolio.release_balance("USDT", stake)
        else:
            self.balance += stake + pnl

        self.total_session_profit += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
            self._daily_loss += abs(pnl)

        self.log_trade_to_db(symbol, "sell", sell_fill, qty, stake, pnl, reason)

        tid = self._trade_ids.pop(f"{symbol}:{id(pos)}", None)
        if tid:
            self.analytics.on_exit(
                trade_id=tid,
                exit_reason=reason,
                pnl=pnl,
                pnl_pct=(pnl / stake) if stake > 0 else 0,
            )

        icon = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{icon} SELL {symbol} | {reason} | Entry: {pos['buy_price']:.8f} | "
            f"Sell: {sell_fill:.8f} | PnL: {pnl:+.2f}$ | {self._wr_text()}"
        )
        self.gui_log(msg)
        self.log(msg)
        return True

    def _manage_orders_for_pair(self, symbol: str, bid: float, ask: float, last: float):
        pair = self._pair_stats[symbol]
        orders = pair.setdefault("orders", [])
        if not orders:
            return

        sell_min = float(self.cfg.sell_rise_min_pct)
        sell_max = float(self.cfg.sell_rise_max_pct)
        target_pct = min(max((sell_min + sell_max) / 2.0, sell_min), sell_max)

        to_remove = []
        for pos in orders:
            pos["low_since_buy"] = min(float(pos["low_since_buy"]), bid)
            pos["peak_since_buy"] = max(float(pos["peak_since_buy"]), bid)

            buy_price = float(pos["buy_price"])
            low_ref = max(float(pos["low_since_buy"]), 1e-12)
            rebound = (bid - low_ref) / low_ref
            drawdown = (buy_price - bid) / max(buy_price, 1e-12)

            reason = None

            if bool(self.cfg.stop_loss_enabled) and drawdown >= float(self.cfg.stop_loss_pct):
                reason = "STOP_LOSS"
            else:
                target_price = low_ref * (1.0 + target_pct)
                if bid >= target_price:
                    if bool(self.cfg.trailing_enabled):
                        pos["trail_active"] = True
                        trail_stop = float(pos.get("trail_stop") or 0.0)
                        new_stop = bid * (1.0 - float(self.cfg.trailing_pct))
                        pos["trail_stop"] = max(trail_stop, new_stop)
                    else:
                        reason = "PING_PONG_TP"

                if bool(pos.get("trail_active")) and pos.get("trail_stop"):
                    if bid <= float(pos["trail_stop"]) and bid > buy_price:
                        reason = "TRAIL_EXIT"

            if reason:
                if self._close_sell(symbol, pos, bid, reason):
                    to_remove.append(pos)

        for p in to_remove:
            try:
                orders.remove(p)
            except ValueError:
                pass

    # ----------------------------
    # Per-pair thread
    # ----------------------------
    def _run_pair(self, symbol: str):
        cfg = self.cfg
        self.log(f"Micro-Reversion task started: {symbol}")

        while not self.stop_event.is_set():
            if self._daily_loss >= float(cfg.daily_loss_limit_usdt):
                self.set_status("Daily loss limit reached")
                break

            bid, ask, last = self._fetch_bid_ask_last(symbol)
            if not bid or not ask or not last:
                time.sleep(cfg.loop_sleep)
                continue

            self._ensure_pair_state(symbol, last)
            pair = self._pair_stats[symbol]
            pair["local_high"] = max(float(pair.get("local_high", last)), last)
            pair["local_low"] = min(float(pair.get("local_low", last)), last)

            # Manage open orders first
            self._manage_orders_for_pair(symbol, bid, ask, last)

            # Entry logic: buy dip from local high
            orders = pair.setdefault("orders", [])
            local_high = max(float(pair.get("local_high", last)), 1e-12)
            drop_pct = (local_high - last) / local_high
            if len(orders) < int(cfg.max_orders_per_pair):
                if float(cfg.buy_drop_min_pct) <= drop_pct <= float(cfg.buy_drop_max_pct):
                    self._open_buy(symbol, ask)
                    # reset high anchor after execution to avoid immediate overtrading
                    pair["local_high"] = last
                    pair["local_low"] = min(float(pair.get("local_low", last)), last)

            # If flat for long, refresh anchors around current price slowly
            if not orders:
                pair["local_low"] = last
                if last > pair["local_high"]:
                    pair["local_high"] = last

            self._sync_position_snapshot(symbol)
            time.sleep(cfg.loop_sleep)

        with self._pairs_lock:
            self.active_pairs.pop(symbol, None)
        self._sync_position_snapshot(symbol)
        self.log(f"Micro-Reversion task stopped: {symbol}")

    # ----------------------------
    # Orchestrator
    # ----------------------------
    def loop(self):
        self._send_tg("MICRO-REVERSION START")
        self.set_status("Micro-Reversion ready")

        try:
            self.exchange.load_markets()
        except Exception as e:
            self.log(f"load_markets error: {e}")
            self.running = False
            return

        self._refresh_universe(force=True)

        while not self.stop_event.is_set():
            try:
                if self.market_data and len(self.market_data.last_prices) < 100:
                    self.set_status("Warmup MarketData")
                    time.sleep(2)
                    continue

                self._refresh_universe(force=False)

                with self._pairs_lock:
                    running_syms = set(self.active_pairs.keys())

                candidates = self.scan_pairs()
                target_pairs = int(max(1, min(self.cfg.max_pairs, self.cfg.max_positions)))

                for sym in candidates:
                    if self.stop_event.is_set():
                        break
                    if sym in running_syms:
                        continue
                    with self._pairs_lock:
                        if len(self.active_pairs) >= target_pairs:
                            break
                        t = threading.Thread(target=self._run_pair, args=(sym,), daemon=True)
                        self.active_pairs[sym] = t
                        t.start()
                    self.log(f"Started Micro-Reversion thread for {sym}")

                with self._pairs_lock:
                    active_count = len(self.active_pairs)
                with self._pos_lock:
                    inv_count = len(self.active_positions)

                self.set_status(
                    f"Micro-Reversion active: {active_count} pairs | Open sets: {inv_count} | Loss: {self._daily_loss:.2f}$"
                )
                time.sleep(self.cfg.loop_sleep)

            except Exception as e:
                self.log(f"orchestrator error: {e}")
                time.sleep(2)

        self.running = False
        self.set_status("BOT STOPPED")
        self.log("Micro-Reversion bot stopped.")

    # ----------------------------
    # Start/stop
    # ----------------------------
    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        if self.tg:
            self.tg.start_polling_thread(self._handle_command)

    def stop(self):
        if not self.running:
            return
        self.stop_event.set()
        if self.tg:
            self.tg.stop_polling()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        self.running = False
