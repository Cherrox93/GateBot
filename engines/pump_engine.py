import asyncio
import time
import requests
import urllib3
from collections import deque
from datetime import datetime
import sqlite3
import ccxt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import PumpConfig, EXCHANGE
from market_data import MarketData
from vault import TELEGRAM_PUMP


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


class PumpEngine:
    def __init__(self, cfg: PumpConfig, log_callback=None,
                 market_data: MarketData | None = None, tg=None):
        self.cfg = cfg
        self.log_callback = log_callback
        self.market_data = market_data
        self.tg = tg

        # Sync ccxt — działa na Windows (requests, nie aiohttp)
        self.exchange = ccxt.gateio({
            "apiKey": EXCHANGE.api_key,
            "secret": EXCHANGE.secret_key,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"}
        })

        self.ticker_memory = {}
        self.active_positions = {}
        self.cooldowns = {}
        self.balance_in_wallet = cfg.start_balance
        self.active_tasks = {}
        self.running = False
        self.main_task = None
        self._poll_task = None

        self.realized_profit = 0.0
        self.daily_realized = 0.0
        self._day_start = datetime.now().date()

        self.wins = 0
        self.losses = 0

    # ============================
    #        STATUS SYSTEM
    # ============================
    def set_status(self, text: str):
        if self.log_callback:
            self.log_callback(f"STATUS_UPDATE::{text}")

    # ============================
    #        LOGGING
    # ============================
    def log(self, msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def gui_log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)

    async def _tg(self, msg: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, send_telegram, msg, TELEGRAM_PUMP.token, TELEGRAM_PUMP.chat_id
        )

    # ============================
    #        WIN RATIO
    # ============================
    def _wr_text(self) -> str:
        total = self.wins + self.losses
        if total == 0:
            return "W:0 L:0"
        wr = self.wins / total * 100
        return f"W:{self.wins} L:{self.losses} WR:{wr:.0f}%"

    # ============================
    #        STATUS COMMAND
    # ============================
    def get_status_text(self) -> str:
        equity = self.get_total_equity()
        total = self.wins + self.losses
        win_pct = f"{self.wins / total * 100:.0f}" if total > 0 else "0"
        lines = [
            "📊 *PUMP DETECTOR*",
            "━━━━━━━━━━━━━━━━━",
            f"💰 Balance: *{self.balance_in_wallet:.2f}$*",
            f"📈 Equity: *{equity:.2f}$*",
            f"📊 Win Rate: W:{self.wins}/L:{self.losses} ({win_pct}%)",
            f"🔓 Otwarte pozycje ({len(self.active_positions)}/{self.cfg.max_positions}):",
        ]
        for sym, pos in self.active_positions.items():
            cur_price = self.market_data.get_last_price(sym) if self.market_data else None
            if cur_price:
                cur_pnl = round((cur_price / pos["entry"] - 1) * pos["stake"], 2)
                pnl_sign = "+" if cur_pnl >= 0 else ""
                lines.append(f"• {sym} | Entry: {pos['entry']:.8f} | PnL: {pnl_sign}{cur_pnl}$")
            else:
                lines.append(f"• {sym} | Entry: {pos['entry']:.8f}")
        return "\n".join(lines)

    def _handle_command(self, text: str) -> str:
        if text.startswith("/status"):
            return self.get_status_text()
        return ""

    # ============================
    #        EQUITY
    # ============================
    def get_total_equity(self):
        locked = sum(pos['stake'] for pos in self.active_positions.values())
        return self.balance_in_wallet + locked

    # ============================
    #        DAILY LIMIT
    # ============================
    def _check_daily_reset(self):
        today = datetime.now().date()
        if today != self._day_start:
            self._day_start = today
            self.daily_realized = 0.0

    def _daily_limit_hit(self) -> bool:
        limit = -self.cfg.start_balance * self.cfg.daily_loss_limit_pct
        return self.daily_realized <= limit

    # ============================
    #        DATABASE
    # ============================
    def init_db(self):
        conn = sqlite3.connect(self.cfg.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                entry_time REAL,
                exit_time REAL,
                entry_price REAL,
                exit_price REAL,
                stake REAL,
                qty REAL,
                pnl_percent REAL,
                pnl_usd REAL,
                reason TEXT,
                score INTEGER,
                price_change_3s REAL,
                velocity REAL,
                early_move REAL,
                late_move REAL,
                long_term_change REAL,
                mfe REAL,
                mae REAL,
                duration REAL
            )
        """)
        conn.commit()
        conn.close()

    def log_trade_to_db(self, trade):
        conn = sqlite3.connect(self.cfg.db_path)
        c = conn.cursor()
        try:
            c.execute("""
                INSERT INTO trades (
                    symbol, entry_time, exit_time, entry_price, exit_price,
                    stake, qty, pnl_percent, pnl_usd, reason, score,
                    price_change_3s, velocity, early_move, late_move, long_term_change,
                    mfe, mae, duration
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade["symbol"], trade["entry_time"], trade["exit_time"],
                trade["entry_price"], trade["exit_price"], trade["stake"],
                trade["qty"], trade["pnl_percent"], trade["pnl_usd"],
                trade["reason"], trade["score"], trade["price_change_3s"],
                trade["velocity"], trade["early_move"], trade["late_move"],
                trade["long_term_change"], trade["mfe"], trade["mae"],
                trade["duration"]
            ))
            conn.commit()
        except Exception as e:
            self.log(f"❌ DB Error: {e}")
        finally:
            conn.close()

    # ============================
    #        SCORING
    # ============================
    def compute_score(self, symbol):
        if symbol not in self.ticker_memory:
            return 0
        mem = self.ticker_memory[symbol]["ticks"]
        if len(mem) < self.cfg.min_ticks_in_window:
            return 0

        now, price = mem[-1]
        first_time, first_price = mem[0]
        duration = now - first_time

        if duration < 0.5:
            return 0

        history_price = self.ticker_memory[symbol].get("history_10m")
        long_term_change = (price / history_price) - 1 if history_price else 0

        # Odrzuć jeśli cena nie wzrosła vs historia
        if history_price and price < history_price * 1.005:
            return 0
        # Removed: +30% cap — real pumps do +100-300%, cap was blocking valid entries
        # Instead: if pump is already large, require strong acceleration to still enter
        if history_price and long_term_change > 0.50:
            mid_index = len(mem) // 2
            mid_price_check = mem[mid_index][1]
            early_check = (mid_price_check / first_price) - 1
            late_check = (price / mid_price_check) - 1
            acceleration_check = (late_check / early_check) if early_check > 0 else 0
            if acceleration_check < 2.0:
                return 0

        price_change = (price / first_price) - 1
        velocity = price_change / duration if duration > 0 else 0

        mid_index = len(mem) // 2
        mid_price = mem[mid_index][1]
        early_move = (mid_price / first_price) - 1
        late_move = (price / mid_price) - 1

        # --- Hard gates ---
        if price_change < 0.030:   # raised from 1.5% — consolidation noise filtered out
            return 0
        if velocity < 0.005:       # raised from 0.003
            return 0
        if late_move <= 0:
            return 0

        # --- Position in daily range filter ---
        # Reject entries in top 15% of daily range — pump already exhausted
        daily_high = self.ticker_memory[symbol].get("daily_high")
        daily_low  = self.ticker_memory[symbol].get("daily_low")
        if daily_high and daily_low and daily_high > daily_low:
            position_in_range = (price - daily_low) / (daily_high - daily_low)
            if position_in_range > 0.85:
                return 0

        # --- Recent momentum check ---
        # Reject if last 4 ticks show flat or declining price — pump decelerating
        if len(mem) >= 4:
            recent_prices = [t[1] for t in list(mem)[-4:]]
            recent_trend = (recent_prices[-1] / recent_prices[0]) - 1
            if recent_trend < 0.005:
                return 0

        score = 0

        # Siła ruchu
        if price_change > 0.02:  score += 2
        if price_change > 0.04:  score += 2
        if price_change > 0.07:  score += 3

        # Prędkość
        if velocity > 0.008:  score += 2
        if velocity > 0.015:  score += 2

        # Przyspieszenie
        if early_move > 0:
            acceleration = late_move / early_move
            if acceleration > 1.5:  score += 2
            if acceleration > 3.0:  score += 3

        # Kontekst długoterminowy
        if long_term_change > 0.05:  score += 2
        if long_term_change > 0.10:  score += 2

        # Volume spike — MANDATORY minimum 3x
        avg_vol = self.ticker_memory[symbol].get("avg_vol_10m", 0)
        current_vol = self.ticker_memory[symbol].get("last_vol", 0)

        if avg_vol <= 0 or current_vol <= 0:
            return 0  # no volume data = skip

        vol_spike = current_vol / avg_vol

        if vol_spike < 3.0:
            return 0  # minimum 3x volume spike required — no exceptions

        if vol_spike > 3:   score += 2
        if vol_spike > 8:   score += 3
        if vol_spike > 20:  score += 3

        # Szybka eksplozja
        if duration < 3 and price_change > 0.02:  score += 2

        return score, {
            "price_change_3s": price_change,
            "velocity": velocity,
            "early_move": early_move,
            "late_move": late_move,
            "long_term_change": long_term_change
        }

    def compute_slow_pump_score(self, symbol) -> tuple | int:
        """
        Detects slow pumps: +15%+ over 60 minutes with volume spike.
        Catches coins like ESE/USDT that pump gradually over hours.
        """
        mem = self.ticker_memory.get(symbol)
        if not mem:
            return 0

        history_1h = mem.get("history_1h")
        current_vol = mem.get("last_vol", 0)
        avg_vol = mem.get("avg_vol_10m", 0)
        ticks = mem["ticks"]

        if not ticks or not history_1h:
            return 0

        price = ticks[-1][1]
        change_1h = (price / history_1h) - 1

        # Must have minimum % gain over 60min
        if change_1h < self.cfg.slow_pump_min_change:
            return 0

        # Volume spike required
        if avg_vol <= 0 or current_vol <= 0:
            return 0

        vol_spike = current_vol / avg_vol
        if vol_spike < self.cfg.slow_pump_min_vol_spike:
            return 0

        # Pump must still be accelerating — not topping out
        if len(ticks) >= 4:
            mid_price = ticks[len(ticks) // 2][1]
            late_move = (price / mid_price) - 1
            if late_move <= 0:
                return 0  # pump is decelerating — too late to enter

        # Build score
        score = 8  # base score for confirmed slow pump

        if change_1h > 0.30:  score += 2
        if change_1h > 0.50:  score += 2
        if change_1h > 1.00:  score += 2
        if vol_spike > 10:    score += 2
        if vol_spike > 20:    score += 2

        return score, {
            "price_change_3s": change_1h,
            "velocity": change_1h / 3600,
            "early_move": change_1h * 0.5,
            "late_move": change_1h * 0.5,
            "long_term_change": change_1h,
        }

    # ============================
    #        MONITOR SYMBOL
    # ============================
    async def monitor_symbol(self, symbol):
        self.set_status(f"📡 Monitoring: {symbol}")

        if symbol not in self.ticker_memory:
            self.ticker_memory[symbol] = {
                "ticks": deque(maxlen=30),
                "history_10m": None,
                "history_1h": None,
                "avg_vol_10m": 0.0,
                "last_vol": 0.0,
                "daily_high": None,
                "daily_low": None,
                "entries_today": 0,
                "entries_date": None,
                "last_good_exit_price": None,
            }

        try:
            loop = asyncio.get_running_loop()
            ohlcv = await loop.run_in_executor(
                None, lambda: self.exchange.fetch_ohlcv(
                    symbol, timeframe='1m', limit=self.cfg.long_history_limit
                )
            )
            self.ticker_memory[symbol]["history_10m"] = ohlcv[0][1] if ohlcv else None
            self.ticker_memory[symbol]["history_1h"] = ohlcv[0][1] if ohlcv else None
            if ohlcv and len(ohlcv) >= 2:
                completed = ohlcv[:-1]
                vols = [c[5] for c in completed[-10:]]
                self.ticker_memory[symbol]["avg_vol_10m"] = sum(vols) / len(vols) if vols else 0.0
                self.ticker_memory[symbol]["last_vol"] = ohlcv[-1][5]
                highs = [c[2] for c in ohlcv]
                lows  = [c[3] for c in ohlcv]
                self.ticker_memory[symbol]["daily_high"] = max(highs)
                self.ticker_memory[symbol]["daily_low"]  = min(lows)
        except Exception:
            self.ticker_memory[symbol]["history_10m"] = None

        while self.running and symbol in self.active_tasks:
            try:
                if self.market_data is None:
                    await asyncio.sleep(1)
                    continue

                price = self.market_data.get_last_price(symbol)
                if price is None:
                    await asyncio.sleep(0.2)
                    continue

                now = time.time()
                mem = self.ticker_memory[symbol]["ticks"]
                mem.append((now, price))

                # Keep daily_high updated as price moves
                current_dh = self.ticker_memory[symbol].get("daily_high")
                if current_dh is None or price > current_dh:
                    self.ticker_memory[symbol]["daily_high"] = price

                while mem and now - mem[0][0] > self.cfg.window_seconds:
                    mem.popleft()

                # ENTRY
                # Reset per-coin entry counter at midnight
                mem_entry = self.ticker_memory[symbol]
                today_str = datetime.now().strftime("%Y-%m-%d")
                if mem_entry.get("entries_date") != today_str:
                    mem_entry["entries_today"] = 0
                    mem_entry["entries_date"] = today_str

                if (symbol not in self.active_positions
                        and now > self.cooldowns.get(symbol, 0)
                        and len(self.active_positions) < self.cfg.max_positions
                        and not self._daily_limit_hit()
                        and mem_entry["entries_today"] < self.cfg.max_entries_per_coin_per_day):

                    res = self.compute_score(symbol)

                    # If fast pump not detected, try slow pump detection
                    if res == 0:
                        res = self.compute_slow_pump_score(symbol)
                        slow_pump = True
                    else:
                        slow_pump = False

                    if res != 0:
                        score, feat = res
                        effective_threshold = (
                            self.cfg.slow_pump_score_threshold if slow_pump
                            else self.cfg.entry_score_threshold
                        )
                        if score >= effective_threshold:
                            stake = min(self.cfg.max_stake, self.balance_in_wallet)
                            if stake >= self.cfg.min_stake_usd:
                                # Anti-churn: don't re-enter below last profitable exit price
                                last_good_price = self.ticker_memory[symbol].get("last_good_exit_price")
                                if last_good_price is not None and price < last_good_price * 0.99:
                                    continue

                                # Increment per-coin entry counter
                                self.ticker_memory[symbol]["entries_today"] += 1

                                qty = stake / price
                                self.active_positions[symbol] = {
                                    "entry": price,
                                    "qty": qty,
                                    "stake": stake,
                                    "start": now,
                                    "max_price": price,
                                    "min_price": price,
                                    "mfe": 0.0,
                                    "mae": 0.0,
                                    "score": score,
                                    "features": feat
                                }
                                self.balance_in_wallet -= stake

                                self.set_status(f"🚀 BUY {symbol}")
                                buy_msg = f"💰 BUY {symbol} | Entry Price: {price} | Stawka: {stake:.2f}$"
                                self.gui_log(buy_msg)
                                self.log(buy_msg)
                                await self._tg(buy_msg)

                # EXIT
                if symbol in self.active_positions:
                    pos = self.active_positions[symbol]

                    if price > pos["max_price"]:
                        pos["max_price"] = price
                    if price < pos["min_price"]:
                        pos["min_price"] = price

                    raw_profit = (price / pos["entry"]) - 1
                    pos["mfe"] = max(pos["mfe"], (pos["max_price"] / pos["entry"]) - 1)
                    pos["mae"] = min(pos["mae"], (pos["min_price"] / pos["entry"]) - 1)

                    profit = raw_profit - self.cfg.fee_rate
                    drop_from_max = (price / pos["max_price"]) - 1

                    exit_reason = None
                    if raw_profit <= self.cfg.hard_sl:
                        exit_reason = "🔴 SL"
                    elif profit > self.cfg.trailing_min_profit:
                        if profit > self.cfg.trailing_high_profit:
                            dynamic_trailing = self.cfg.trailing_high_drop
                        elif profit > self.cfg.trailing_mid_profit:
                            dynamic_trailing = self.cfg.trailing_mid_drop
                        else:
                            dynamic_trailing = self.cfg.base_trailing

                        if drop_from_max <= dynamic_trailing:
                            exit_reason = "TS 📉"

                    duration = now - pos["start"]

                    # Quick exit: pump failed within 2 minutes
                    if exit_reason is None and duration > self.cfg.quick_exit_seconds:
                        if raw_profit < self.cfg.quick_exit_threshold:
                            exit_reason = "⏱ QUICK"

                    # Regular time exit
                    if exit_reason is None and duration > self.cfg.time_exit_seconds:
                        exit_reason = "⏱ TIME"

                    if exit_reason:
                        sell_value = pos["qty"] * price * (1 - self.cfg.fee_rate)
                        pnl_usd = sell_value - pos["stake"]
                        pnl_percent = round(profit * 100, 2)

                        self.balance_in_wallet += sell_value
                        self.realized_profit += pnl_usd
                        self.daily_realized += pnl_usd

                        if pnl_usd >= 0:
                            self.wins += 1
                        else:
                            self.losses += 1

                        if exit_reason in ("🔴 SL", "⏱ TIME", "⏱ QUICK") or pnl_usd < 0:
                            icon = "❌"
                        else:
                            icon = "✅"
                        wr = self._wr_text()

                        gui_msg = (
                            f"{icon} SELL {symbol} | {exit_reason} | "
                            f"Entry: {pos['entry']} | Sell: {price} | "
                            f"PnL: {pnl_usd:.2f}$ | {wr}"
                        )
                        tg_msg = gui_msg

                        self.set_status(f"💰 SELL {symbol}")
                        self.gui_log(gui_msg)
                        self.log(tg_msg)
                        await self._tg(tg_msg)

                        trade_record = {
                            "symbol": symbol,
                            "entry_time": pos["start"],
                            "exit_time": now,
                            "entry_price": pos["entry"],
                            "exit_price": price,
                            "stake": pos["stake"],
                            "qty": pos["qty"],
                            "pnl_percent": pnl_percent,
                            "pnl_usd": pnl_usd,
                            "reason": exit_reason,
                            "score": pos["score"],
                            "price_change_3s": pos["features"]["price_change_3s"],
                            "velocity": pos["features"]["velocity"],
                            "early_move": pos["features"]["early_move"],
                            "late_move": pos["features"]["late_move"],
                            "long_term_change": pos["features"]["long_term_change"],
                            "mfe": pos["mfe"],
                            "mae": pos["mae"],
                            "duration": duration
                        }
                        self.log_trade_to_db(trade_record)

                        del self.active_positions[symbol]

                        # Record last profitable exit price for anti-churn guard
                        if exit_reason == "TS 📉" and pnl_usd > 0:
                            self.ticker_memory[symbol]["last_good_exit_price"] = price

                        # Cooldown by exit reason
                        if exit_reason == "🔴 SL":
                            self.cooldowns[symbol] = now + self.cfg.cooldown_after_sl
                        elif exit_reason == "TS 📉" and pnl_usd > 0:
                            self.cooldowns[symbol] = now + self.cfg.cooldown_seconds * 0.5
                        elif exit_reason in ("⏱ QUICK", "⏱ TIME"):
                            self.cooldowns[symbol] = now + self.cfg.cooldown_seconds
                        else:
                            self.cooldowns[symbol] = now + self.cfg.cooldown_seconds

                await asyncio.sleep(0.1)

            except Exception as e:
                self.log(f"⚠️ monitor_symbol error {symbol}: {e}")
                await asyncio.sleep(1)
                if symbol not in self.active_tasks:
                    break

    # ============================
    #        SCANNER LOOP
    # ============================
    async def scanner_loop(self):
        self.set_status("🔍 Skanuję rynek…")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.exchange.load_markets)

        while self.running:
            try:
                self._check_daily_reset()

                if self._daily_limit_hit():
                    msg = f"🛑 PUMP: Dzienny limit strat — bot zatrzymany ({round(self.daily_realized, 2)}$)"
                    self.set_status(msg)
                    self.log(msg)
                    self.gui_log(msg)
                    await self._tg(msg)
                    self.running = False
                    break

                all_tickers = self.market_data.get_all_tickers() if self.market_data else {}
                if not all_tickers:
                    loop2 = asyncio.get_running_loop()
                    all_tickers = await loop2.run_in_executor(
                        None, self.exchange.fetch_tickers
                    )

                candidates = []
                for symbol, data in all_tickers.items():
                    if self.cfg.symbol_filter not in symbol:
                        continue
                    if any(bad in symbol for bad in self.cfg.blacklist):
                        continue

                    vol = data.get('quoteVolume', 0) or 0
                    change = data.get('percentage', 0) or 0

                    if not (self.cfg.vol_min <= vol <= self.cfg.vol_max):
                        continue

                    # Filtr micro-price i spread
                    last_price = data.get('last', 0) or 0
                    if last_price < 0.000001:
                        continue
                    bid = data.get('bid', 0) or 0
                    ask = data.get('ask', 0) or 0
                    if bid > 0 and ask > 0 and (ask - bid) / bid > 0.02:
                        continue

                    candidates.append({'symbol': symbol, 'change': change})

                candidates.sort(key=lambda x: x['change'], reverse=True)
                top_symbols = [c['symbol'] for c in candidates[:self.cfg.max_symbols]]

                self.set_status(f"📡 Monitoring {len(top_symbols)} par…")

                to_remove = [s for s in self.active_tasks if s not in top_symbols and s not in self.active_positions]
                for symbol in to_remove:
                    task = self.active_tasks.pop(symbol)
                    task.cancel()

                for symbol in top_symbols:
                    if symbol not in self.active_tasks:
                        self.active_tasks[symbol] = asyncio.create_task(self.monitor_symbol(symbol))

                await asyncio.sleep(self.cfg.scanner_interval)

            except Exception as e:
                self.log(f"⚠️ Błąd rotatora: {e}")
                await asyncio.sleep(5)

    # ============================
    #        RUN ENGINE
    # ============================
    async def run(self):
        self.set_status("🏁 Pump Detector startuje…")
        start_msg = "🏁 PUMP DETECTOR START"
        self.log(start_msg)
        await self._tg(start_msg)
        self.init_db()
        await self.scanner_loop()

    async def start(self):
        if self.running:
            return
        self.running = True
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

        for task in self.active_tasks.values():
            task.cancel()
        self.active_tasks.clear()
