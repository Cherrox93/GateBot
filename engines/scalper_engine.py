# scalper_engine.py
import asyncio
import time
import pandas as pd
import pandas_ta as ta
import ccxt
import requests
import urllib3
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import ScalperConfig, EXCHANGE
from market_data import MarketData
from vault import TELEGRAM_SCALPER
from analytics_engine import get_analytics


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


def _fmt(price: float) -> str:
    """Formatowanie ceny: 2 miejsca dla >1, 8 dla małych tokenów."""
    return f"{price:.2f}" if price >= 1.0 else f"{price:.8f}"


class ScalperEngine:
    def __init__(self, cfg: ScalperConfig, log_callback=None,
                 market_data: MarketData | None = None, tg=None, portfolio=None):
        self.cfg = cfg

        # Sync ccxt — działa na Windows (requests, nie aiohttp)
        self.exchange = ccxt.gateio({
            'apiKey': EXCHANGE.api_key,
            'secret': EXCHANGE.secret_key,
            'enableRateLimit': True
        })

        self.market_data = market_data
        self.tg = tg
        self.portfolio = portfolio
        self.analytics = get_analytics('scalper')
        self._trade_ids: dict = {}
        self.balance_usdt = self.cfg.start_balance

        # {symbol: position_dict}
        self.active_positions = {}

        # {symbol: monotonic_timestamp}
        self.cooldowns = {}

        # {(symbol, timeframe): {"data": list, "ts": float}}
        self._ohlcv_cache = {}

        self.running = False
        self.log_callback = log_callback

        self.realized_profit = 0.0
        self.daily_realized = 0.0
        self._day_start = datetime.now().date()

        # Win/Loss tracking
        self.wins = 0
        self.losses = 0

        # Impulse Hunter v2 state
        self._btc_regime_cache = {"result": True, "ts": 0.0}
        self._alt_breadth_cache = {"result": True, "ts": 0.0}
        self._recent_trade_results: list[int] = []
        self._pause_until: float = 0.0
        self._last_sl_price: dict[str, float] = {}
        self._entries_this_scan: int = 0

        self.stop_event = None
        self.main_task = None
        self._poll_task = None
        self.status_index = 0

    # ============================
    #        STATUS / LOGGING
    # ============================
    def set_status(self, text: str):
        if self.log_callback:
            try:
                self.log_callback(f"STATUS_UPDATE::{text}")
            except Exception:
                pass

    def log(self, msg: str):
        clean = msg.replace("*", "").replace("`", "")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {clean}", flush=True)

    def gui_log(self, msg: str):
        if self.log_callback:
            try:
                self.log_callback(msg)
            except Exception:
                pass

    async def _tg(self, msg: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, send_telegram, msg, TELEGRAM_SCALPER.token, TELEGRAM_SCALPER.chat_id
        )

    # ============================
    #        EQUITY
    # ============================
    def get_total_exposure(self) -> float:
        return sum(p["original_stake"] for p in self.active_positions.values())

    def get_total_equity(self) -> float:
        bal = self.portfolio.balance_usdt if self.portfolio else self.balance_usdt
        return bal + self.get_total_exposure()

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
    #        WIN RATIO
    # ============================
    def _wr_text(self) -> str:
        total = self.wins + self.losses
        if total == 0:
            return "W:0 L:0"
        wr = self.wins / total * 100
        return f"W:{self.wins} L:{self.losses} WR:{wr:.0f}%"

    # ============================
    #        /STATUS COMMAND
    # ============================
    def get_status_text(self) -> str:
        equity = self.get_total_equity()
        total = self.wins + self.losses
        win_pct = f"{self.wins / total * 100:.0f}" if total > 0 else "0"
        lines = [
            "📊 *HIGHCAP SCALPER*",
            "━━━━━━━━━━━━━━━━━",
            f"💰 Balance: *{(self.portfolio.balance_usdt if self.portfolio else self.balance_usdt):.2f}$*",
            f"📈 Equity: *{equity:.2f}$*",
            f"📊 Win Rate: W:{self.wins}/L:{self.losses} ({win_pct}%)",
            f"🔓 Otwarte pozycje ({len(self.active_positions)}/{self.cfg.slot_count}):",
        ]
        for sym, pos in self.active_positions.items():
            ws_sym = sym.replace("/", "_")
            cur_price = self.market_data.get_last_price(ws_sym) if self.market_data else None
            if cur_price:
                cur_pnl = round((cur_price / pos["entry"] - 1) * pos["original_stake"], 2)
                pnl_sign = "+" if cur_pnl >= 0 else ""
                lines.append(f"• {sym} | Entry: {_fmt(pos['entry'])} | PnL: {pnl_sign}{cur_pnl}$")
            else:
                lines.append(f"• {sym} | Entry: {_fmt(pos['entry'])}")
        return "\n".join(lines)

    def _handle_command(self, text: str) -> str:
        if text.startswith("/status"):
            return self.get_status_text()
        return ""

    # ============================
    #        OHLCV CACHE
    # ============================
    async def _get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list:
        key = (symbol, timeframe)
        now = time.monotonic()
        cached = self._ohlcv_cache.get(key)
        if cached and (now - cached["ts"]) < self.cfg.ohlcv_cache_ttl:
            return cached["data"]

        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, lambda: self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )
        self._ohlcv_cache[key] = {"data": data, "ts": now}
        return data

    # ============================
    #        REGIME CHECK
    # ============================
    async def _check_regime(self) -> bool:
        """BTC trend + volatility check. Returns True if market is favorable."""
        now = time.monotonic()
        if now - self._btc_regime_cache["ts"] < self.cfg.btc_regime_cache_ttl:
            return self._btc_regime_cache["result"]

        try:
            btc_ohlcv = await self._get_ohlcv("BTC/USDT", "5m", 60)
            if len(btc_ohlcv) < 51:
                return True  # insufficient data → allow

            btc_df = pd.DataFrame(btc_ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
            btc_ema21 = ta.ema(btc_df["c"], length=21)
            btc_ema50 = ta.ema(btc_df["c"], length=50)
            btc_atr   = ta.atr(btc_df["h"], btc_df["l"], btc_df["c"], length=7)
            btc_price = float(btc_df["c"].iloc[-1])
            btc_atr_pct = float(btc_atr.iloc[-1]) / btc_price if btc_price > 0 else 0.0

            trend_ok = (
                float(btc_ema21.iloc[-1]) > float(btc_ema50.iloc[-1])
                or btc_atr_pct >= 0.003
            )

            # Strong BTC volume impulse overrides borderline trend
            btc_vol_mean = btc_df["v"].rolling(10).mean().iloc[-1]
            btc_vol_ratio = float(btc_df["v"].iloc[-1]) / btc_vol_mean if btc_vol_mean > 0 else 0.0
            if btc_vol_ratio >= self.cfg.btc_breakout_vol_mult:
                trend_ok = True

            result = trend_ok
        except Exception:
            result = True  # error → allow

        self._btc_regime_cache["result"] = result
        self._btc_regime_cache["ts"] = now
        return result

    # ============================
    #        ENTRY CHECK
    # ============================
    async def _check_entry(self, symbol: str, price: float) -> tuple:
        """
        Hard-filter + soft-score entry check.
        Returns (score, features_dict) or (0, None) if any hard filter fails.
        """
        try:
            ohlcv_1m = await self._get_ohlcv(symbol, "1m", 60)
            ohlcv_5m = await self._get_ohlcv(symbol, "5m", 60)

            if len(ohlcv_1m) < 22 or len(ohlcv_5m) < 51:
                return 0, None

            df1 = pd.DataFrame(ohlcv_1m, columns=["ts", "o", "h", "l", "c", "v"])
            df5 = pd.DataFrame(ohlcv_5m, columns=["ts", "o", "h", "l", "c", "v"])

            # Use closed candle (iloc[-2])
            last = df1.iloc[-2]
            candle_range = float(last["h"]) - float(last["l"])
            body = float(last["c"]) - float(last["o"])

            # Hard filter 1: bullish candle only
            if body <= 0:
                return 0, None

            # Hard filter 2: body ratio
            body_ratio = body / candle_range if candle_range > 0 else 0.0
            if body_ratio < self.cfg.entry_body_ratio:
                return 0, None

            # Hard filter 3: breakout above previous high
            prev_high = float(df1["h"].iloc[-3])
            if float(last["c"]) <= prev_high * self.cfg.entry_breakout_factor:
                return 0, None

            # Hard filter 4: volume burst
            vol_mean = df1["v"].rolling(10).mean().iloc[-2]
            vol_ratio = float(df1["v"].iloc[-2]) / vol_mean if vol_mean > 0 else 0.0
            if vol_ratio < self.cfg.entry_vol_mult:
                return 0, None

            # Hard filter 5: M5 EMA9 > EMA21 > EMA50 + rising slope
            ema9_5  = ta.ema(df5["c"], length=9)
            ema21_5 = ta.ema(df5["c"], length=21)
            ema50_5 = ta.ema(df5["c"], length=50)
            if not (float(ema9_5.iloc[-1]) > float(ema21_5.iloc[-1]) > float(ema50_5.iloc[-1])):
                return 0, None
            if float(ema50_5.iloc[-1]) <= float(ema50_5.iloc[-3]):
                return 0, None

            # Hard filter 6: re-entry guard (price must exceed last SL price)
            last_sl = self._last_sl_price.get(symbol, 0.0)
            if price <= last_sl:
                return 0, None

            # --- Soft scoring ---
            score = 0

            # M1 EMA9 > EMA21 alignment
            ema9_1  = ta.ema(df1["c"], length=9)
            ema21_1 = ta.ema(df1["c"], length=21)
            if float(ema9_1.iloc[-1]) > float(ema21_1.iloc[-1]):
                score += 20

            # Extra volume strength
            if vol_ratio >= self.cfg.entry_vol_mult * 1.5:
                score += 15

            # Upper wick clean
            upper_wick = float(last["h"]) - max(float(last["o"]), float(last["c"]))
            uw_ratio = upper_wick / candle_range if candle_range > 0 else 1.0
            if uw_ratio < self.cfg.max_upper_wick_ratio:
                score += 15

            # ATR in optimal range
            atr_ser = ta.atr(df1["h"], df1["l"], df1["c"], length=7)
            atr_val = float(atr_ser.iloc[-1])
            atr_pct = atr_val / price if price > 0 else 0.0
            min_atr_for_profit = 0.005 / self.cfg.atr_sl_multiplier
            if atr_pct < min_atr_for_profit:
                return 0, None
            if 0.003 <= atr_pct <= 0.020:
                score += 15

            # Strong EMA50 slope
            if float(ema50_5.iloc[-1]) > float(ema50_5.iloc[-3]):
                score += 10

            features = {
                "vol_ratio":  vol_ratio,
                "body_ratio": body_ratio,
                "atr_pct":    atr_pct,
                "atr_val":    atr_val,
                "uw_ratio":   uw_ratio,
            }
            return score, features

        except Exception as e:
            self.log(f"_check_entry error {symbol}: {e}")
            return 0, None

    # ============================
    #        RISK CALC
    # ============================
    def _calc_risk(self, price: float, atr_val: float) -> tuple:
        """
        Fixed fractional risk sizing.
        Risk = risk_per_trade_pct * equity
        SL = entry - atr_sl_multiplier * ATR
        Returns (stake, qty, sl_price, sl_distance)
        """
        sl_distance = self.cfg.atr_sl_multiplier * atr_val
        sl_price = price - sl_distance

        avail = self.portfolio.balance_usdt if self.portfolio else self.balance_usdt
        stake = min(self.cfg.max_stake_usd, avail)
        stake = max(stake, 0.0)
        qty = stake / price if price > 0 else 0.0

        return stake, qty, sl_price, sl_distance

    # ============================
    #        EXIT LOGIC
    # ============================
    async def process_exit(self, symbol: str, price: float):
        pos = self.active_positions[symbol]

        if price > pos["high_price"]:
            pos["high_price"] = price

        sell_qty = 0.0
        reason_label = ""
        close_position = False

        # 1. Hard stop loss
        if price <= pos["sl_price"]:
            sell_qty = pos["qty_remaining"]
            reason_label = "🔴 SL"
            close_position = True

        # 2. TP1 — sell tp1_qty_pct (65%), move SL to break-even
        elif not pos["tp1_done"] and price >= pos["tp1_price"]:
            sell_qty = min(pos["qty_original"] * self.cfg.tp1_qty_pct, pos["qty_remaining"])
            pos["tp1_done"] = True
            pos["sl_price"] = pos["entry"]
            reason_label = "TP1"

        # 3 & 4. Runner: activate then trail
        elif pos["tp1_done"]:
            if not pos["runner_active"] and price >= pos["runner_activation"]:
                pos["runner_active"] = True
                pos["trail_price"] = price - self.cfg.atr_trail_multiplier * pos["sl_distance"]

            if pos["runner_active"]:
                new_trail = price - self.cfg.atr_trail_multiplier * pos["sl_distance"]
                if pos["trail_price"] is None or new_trail > pos["trail_price"]:
                    pos["trail_price"] = new_trail

                if pos["trail_price"] is not None and price <= pos["trail_price"]:
                    sell_qty = pos["qty_remaining"]
                    reason_label = "RUNNER"
                    close_position = True

        if sell_qty <= 0:
            return

        received = sell_qty * price * (1 - self.cfg.fee)
        cost_basis = sell_qty * pos["entry"]
        pnl = received - cost_basis

        if self.portfolio:
            self.portfolio.balance_usdt += received
        else:
            self.balance_usdt += received
        self.realized_profit += pnl
        self.daily_realized += pnl
        pos["accumulated_pnl"] += pnl
        pos["qty_remaining"] -= sell_qty

        entry_str = _fmt(pos["entry"])
        sell_str  = _fmt(price)

        icon = "❌" if (reason_label == "🔴 SL" or pnl < 0) else "✅"
        gui_msg = f"{icon} SELL {symbol} | {reason_label} | Entry: {entry_str} | Sell: {sell_str} | PnL: {pnl:.2f}$"
        tg_msg  = gui_msg

        if close_position or pos["qty_remaining"] <= 1e-10:
            trade_won = pos["accumulated_pnl"] >= 0
            if trade_won:
                self.wins += 1
            else:
                self.losses += 1

            # Record for rolling WR circuit breaker
            self._recent_trade_results.append(1 if trade_won else 0)
            if len(self._recent_trade_results) > self.cfg.rolling_wr_window * 2:
                self._recent_trade_results = self._recent_trade_results[-self.cfg.rolling_wr_window * 2:]

            # Re-entry guard: remember SL price so we don't re-enter below it
            if reason_label == "🔴 SL":
                self._last_sl_price[symbol] = pos["sl_price"]

            tg_msg  += f" | {self._wr_text()}"
            gui_msg += f" | {self._wr_text()}"
            del self.active_positions[symbol]
            # ── ML Analytics: rejestracja wyjścia ──
            _tid = self._trade_ids.pop(symbol, None)
            if _tid:
                self.analytics.on_exit(
                    trade_id=_tid,
                    exit_reason=reason_label,
                    pnl=pos['accumulated_pnl'],
                    pnl_pct=pos['accumulated_pnl'] / pos['original_stake'],
                )
            self.cooldowns[symbol] = time.monotonic() + self.cfg.cooldown_seconds

        self.gui_log(gui_msg)
        self.log(gui_msg)
        await self._tg(tg_msg)

    # ============================
    #        ENTRY LOGIC
    # ============================
    async def process_entry(self, symbol: str, price: float):
        now = time.monotonic()

        # Basic gates
        if now < self.cooldowns.get(symbol, 0):
            return
        equity = self.get_total_equity()
        dynamic_slots = 5 if equity >= 600 else 3
        if len(self.active_positions) >= dynamic_slots:
            return
        if self._daily_limit_hit():
            return
        if now < self._pause_until:
            return
        if self._entries_this_scan >= self.cfg.max_entries_per_scan:
            return

        total_equity = self.get_total_equity()
        if total_equity > 0 and self.get_total_exposure() / total_equity >= self.cfg.max_total_exposure:
            return

        # Rolling WR circuit breaker
        if len(self._recent_trade_results) >= self.cfg.rolling_wr_window:
            window = self._recent_trade_results[-self.cfg.rolling_wr_window:]
            wr = sum(window) / len(window)
            if wr < self.cfg.rolling_wr_min:
                if self._pause_until <= now:
                    self._pause_until = now + self.cfg.rolling_wr_pause
                    self._recent_trade_results = []
                    msg = (f"⛔ Rolling WR {wr*100:.0f}% < "
                           f"{self.cfg.rolling_wr_min*100:.0f}% — pauza 1h")
                    self.log(msg)
                    self.gui_log(msg)
                    await self._tg(msg)
                return

        # BTC regime filter (skip for BTC itself)
        if symbol != "BTC/USDT":
            regime_ok = await self._check_regime()
            if not regime_ok:
                return

        # Entry filter + scoring
        score, features = await self._check_entry(symbol, price)
        if features is None or score < self.cfg.min_score:
            return

        atr_val = features.get("atr_val", price * 0.01)
        stake, qty, sl_price, sl_distance = self._calc_risk(price, atr_val)

        if stake < 1.0 or qty <= 0:
            return

        tp1_price        = price + self.cfg.tp1_r_multiple * sl_distance
        runner_activation = price + self.cfg.runner_activation_r * sl_distance

        # ── ML Analytics: rejestracja wejścia ──
        _features = {
            'score':             score,
            'vol_ratio':         features.get('vol_ratio', 0),
            'body_ratio':        features.get('body_ratio', 0),
            'atr_pct':           features.get('atr_pct', 0),
            'upper_wick_ratio':  features.get('uw_ratio', 0),
            'breakout_strength': features.get('breakout_strength', 1.0),
            'ema9_slope':        0.0,
            'ema21_slope':       0.0,
            'ema_spread':        0.0,
            'btc_trend':         1 if self._btc_regime_cache.get('result', True) else -1,
            'btc_atr_pct':       0.0,
            'btc_vol_ratio':     1.0,
        }
        _ml = self.analytics.on_entry(
            symbol=symbol, entry_price=price, stake=stake,
            sl_pct=sl_distance / price,
            tp1_pct=(tp1_price - price) / price,
            features=_features,
        )
        self._trade_ids[symbol] = _ml['trade_id']

        # Blokady ML (sprawdź przed otwarciem pozycji)
        _ml_blocked, _ml_reason = self.analytics.is_trading_blocked()
        if _ml_blocked:
            self.gui_log(f"🤖 {_ml_reason}")
            return
        if self.analytics.is_symbol_blocked(symbol):
            return

        self.active_positions[symbol] = {
            "entry":             price,
            "qty_original":      qty,
            "qty_remaining":     qty,
            "original_stake":    stake,
            "sl_price":          sl_price,
            "sl_distance":       sl_distance,
            "high_price":        price,
            "tp1_price":         tp1_price,
            "tp1_done":          False,
            "runner_active":     False,
            "runner_activation": runner_activation,
            "trail_price":       None,
            "accumulated_pnl":   0.0,
            "score":             score,
            "entry_time":        now,
        }
        if self.portfolio:
            self.portfolio.balance_usdt -= stake
        else:
            self.balance_usdt -= stake
        self._entries_this_scan += 1

        sl_pct  = sl_distance / price * 100
        tp1_pct = (tp1_price / price - 1) * 100
        entry_str = _fmt(price)
        buy_msg = (
            f"💰 BUY {symbol} | Entry: {entry_str} | Stake: {stake:.2f}$ "
            f"| SL: {_fmt(sl_price)} (-{sl_pct:.2f}%) | TP1: {_fmt(tp1_price)} (+{tp1_pct:.2f}%)"
        )

        self.set_status(f"🚀 BUY {symbol} (score={score})")
        self.gui_log(buy_msg)
        self.log(buy_msg)
        await self._tg(buy_msg)

    # ============================
    #        MAIN LOOP
    # ============================
    async def run(self):
        self.set_status("🏁 Scalper startuje…")
        start_msg = "🏁 HIGHCAP SCALPER START"
        self.log(start_msg)
        await self._tg(start_msg)

        symbols_ccxt = list(self.cfg.gigants)
        symbols_ws   = [s.replace("/", "_") for s in symbols_ccxt]

        try:
            while self.running and not self.stop_event.is_set():

                self._check_daily_reset()

                if self._daily_limit_hit():
                    msg = f"🛑 DZIENNY LIMIT STRAT: {round(self.daily_realized, 2)}$ — bot zatrzymany"
                    self.set_status(msg)
                    self.log(msg)
                    self.gui_log(msg)
                    await self._tg(msg)
                    self.running = False
                    break

                self._entries_this_scan = 0

                for symbol, ws_symbol in zip(symbols_ccxt, symbols_ws):

                    self.status_index = (self.status_index + 1) % len(symbols_ccxt)
                    if self.status_index % 5 == 0:
                        self.set_status(f"🔍 Skanuję… ({symbol})")

                    price = self.market_data.get_last_price(ws_symbol)
                    if price is None:
                        continue

                    if symbol in self.active_positions:
                        await self.process_exit(symbol, price)

                    if symbol not in self.active_positions:
                        await self.process_entry(symbol, price)

                    await asyncio.sleep(0.01)

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log(f"ERR main loop: {e}")
        finally:
            self.set_status("🛑 BOT ZATRZYMANY")
            self.log("Bot stopped cleanly.")

    # ============================
    #        START / STOP
    # ============================
    async def start(self):
        if self.running:
            return

        self.running = True
        self.stop_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.exchange.load_markets)
        except Exception as e:
            self.log(f"load_markets error: {e}")

        self.main_task = loop.create_task(self.run())

        if self.tg:
            self._poll_task = loop.create_task(
                self.tg.start_polling_async(self._handle_command)
            )

    async def stop(self):
        if not self.running:
            return

        self.running = False
        self.stop_event.set()

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
