import ccxt
import pandas as pd
import pandas_ta as ta
import time
import random
import json
import re
import threading
import requests
import urllib3
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import LowcapConfig, EXCHANGE
from market_data import MarketData
from vault import TELEGRAM_LOWCAP
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


class LowcapEngine:
    def __init__(self, cfg: LowcapConfig, log_callback=None,
                 market_data: MarketData | None = None, tg=None, portfolio=None):
        self.cfg = cfg
        self.analytics = get_analytics('lowcap')
        self._trade_ids: dict = {}
        self.market_data = market_data
        self.tg = tg
        self.portfolio = portfolio

        self.exchange = ccxt.gateio({
            'apiKey': EXCHANGE.api_key,
            'secret': EXCHANGE.secret_key,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })

        self.active_positions = {}
        self.cooldowns = {}
        self.total_session_profit = 0.0
        self.consecutive_losses = 0
        self._pause_until: float = 0.0
        self._last_sl_symbols: dict = {}
        self.balance = self.cfg.start_balance
        self.wins = 0
        self.losses = 0
        self.running = False

        self.stop_event = threading.Event()
        self.thread = None

        self._ohlcv_cache = {}   # {(symbol, timeframe): {"data": list, "ts": float}}
        self._ohlcv_cache_ttl = 20.0  # sekundy

        self.ai_db = self.load_ai_db()
        self.last_prices = {}
        self.log_callback = log_callback

        self.current_status = self.cfg.status.default_status_text
        self.all_symbols = None

    # ============================
    #         STATUS SYSTEM
    # ============================
    def set_status(self, text: str):
        if text != self.current_status:
            self.current_status = text
            if self.log_callback and self.cfg.status.show_status_line:
                try:
                    self.log_callback(f"STATUS_UPDATE::{text}")
                except:
                    pass

    # ============================
    #         LOGGING
    # ============================
    def gui_log(self, msg: str):
        if self.log_callback:
            try:
                self.log_callback(msg)
            except:
                pass

    def log(self, msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    # ============================
    #         TELEGRAM
    # ============================
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
        equity = bal + sum(p['stake'] for p in self.active_positions.values())
        total = self.wins + self.losses
        win_pct = f"{self.wins / total * 100:.0f}" if total > 0 else "0"
        lines = [
            "📊 *LOWCAP SCALPER*",
            "━━━━━━━━━━━━━━━━━",
            f"💰 Balance: *{bal:.2f}$*",
            f"📈 Equity: *{equity:.2f}$*",
            f"📊 Win Rate: W:{self.wins}/L:{self.losses} ({win_pct}%)",
            f"🔓 Otwarte pozycje ({len(self.active_positions)}/{self.cfg.max_positions}):",
        ]
        for sym, pos in self.active_positions.items():
            cur_price = self.last_prices.get(sym)
            if cur_price:
                cur_pnl = round((cur_price / pos['buy'] - 1) * pos['stake'], 2)
                pnl_sign = "+" if cur_pnl >= 0 else ""
                lines.append(f"• {sym} | Entry: {pos['buy']:.8f} | PnL: {pnl_sign}{cur_pnl}$")
            else:
                lines.append(f"• {sym} | Entry: {pos['buy']:.8f}")
        return "\n".join(lines)

    def _handle_command(self, text: str) -> str:
        if text.startswith("/status"):
            return self.get_status_text()
        return ""

    # ============================
    #         AI DB
    # ============================
    def load_ai_db(self):
        try:
            with open(self.cfg.ai_db_file, "r") as f:
                return json.load(f)
        except:
            return []

    def save_ai_db(self):
        try:
            with open(self.cfg.ai_db_file, "w") as f:
                json.dump(self.ai_db, f)
        except:
            pass

    def is_blacklisted(self, symbol: str) -> bool:
        return bool(re.search(r'\d[LS]', symbol)) or "3L" in symbol or "3S" in symbol

    # ============================
    #         AI / SCORING
    # ============================
    def ai_score_boost(self, score, features):
        if len(self.ai_db) < self.cfg.min_ai_samples:
            return score

        similar = [
            t for t in self.ai_db
            if abs(t['vol_ratio'] - features['vol_ratio']) < 0.8
            and abs(t['atr'] - features['atr']) < 0.01
        ]

        if len(similar) < 8:
            return score

        wr = sum(t['win'] for t in similar) / len(similar)

        if wr > 0.6:
            score += self.cfg.ai_boost
        elif wr < 0.4:
            score -= self.cfg.ai_boost

        return score

    def dynamic_score_threshold(self):
        total = self.wins + self.losses
        if total < 20:
            return self.cfg.dyn_threshold_base

        wr = self.wins / total

        if wr > self.cfg.dyn_wr_top:
            return self.cfg.dyn_threshold_top
        elif wr > self.cfg.dyn_wr_high:
            return self.cfg.dyn_threshold_high
        elif wr > self.cfg.dyn_wr_mid:
            return self.cfg.dyn_threshold_mid
        else:
            return self.cfg.dyn_threshold_base

    # ============================
    #         TREND / ŚWIECE
    # ============================
    def micro_trend_ok(self, df: pd.DataFrame) -> bool:
        df['EMA_fast'] = ta.ema(df['close'], self.cfg.m1_ema_fast)
        df['EMA_slow'] = ta.ema(df['close'], self.cfg.m1_ema_slow)
        if df[['EMA_fast', 'EMA_slow']].isna().iloc[-1].any():
            return False
        return df['EMA_fast'].iloc[-1] > df['EMA_slow'].iloc[-1]

    def score_symbol(self, df: pd.DataFrame, prev_high: float):
        last = df.iloc[-1]
        vol_mean = df['vol'].rolling(10).mean().iloc[-1]
        vol_ratio = last['vol'] / vol_mean if vol_mean > 0 else 1
        candle_range = last['high'] - last['low']
        body = abs(last['close'] - last['open'])

        score = 0

        # Require real breakout: CLOSE must be above prev_high (not just wick)
        cond_break = last['close'] > prev_high * self.cfg.break_high_factor
        cond_close = last['close'] > prev_high * self.cfg.break_close_factor

        # Candle must be bullish with strong body
        cond_body = (
            candle_range > 0
            and (body / candle_range) >= self.cfg.candle_body_ratio
            and last['close'] > last['open']  # bullish candle required
        )

        if cond_break and cond_close and cond_body:
            score += 35

        # Rolling mean check: current volume must exceed 2x 10-candle average
        rolling_mean_10 = df['vol'].rolling(10).mean().iloc[-1]
        vol_vs_rolling = last['vol'] / rolling_mean_10 if rolling_mean_10 > 0 else 0

        # Volume must also be greater than previous candle (momentum building)
        vol_prev = df['vol'].iloc[-2] if len(df) >= 2 else 0
        vol_increasing = last['vol'] > vol_prev

        vol_burst = vol_vs_rolling >= self.cfg.vol_rolling_multiplier and vol_increasing

        if not vol_burst:
            # Volume too weak — no real momentum
            return 0, vol_ratio

        if vol_ratio > 1.05:
            score += 25
        if vol_ratio > 1.3:
            score += 35
        if vol_burst:
            score += 20

        upper_wick = last['high'] - max(last['open'], last['close'])
        if candle_range > 0:
            upper_wick_ratio = upper_wick / candle_range
            if upper_wick_ratio < self.cfg.max_upper_wick_ratio_score:
                score += 10

        return score, vol_ratio

    # ============================
    #         FILTRY WEJŚCIA
    # ============================
    def passes_market_filters(self, symbol: str, ticker: dict) -> bool:
        if '/USDT' not in symbol:
            return False

        if any(x in symbol for x in [":", "-", "3L", "3S", "5L", "5S"]):
            return False

        if any(bl in symbol for bl in self.cfg.blacklist):
            return False

        vol = ticker.get('quoteVolume') or 0
        if not (self.cfg.vol_min <= vol <= self.cfg.vol_max):
            return False

        if self.is_blacklisted(symbol):
            return False

        if symbol in self.active_positions or time.time() <= self.cooldowns.get(symbol, 0):
            return False

        price = ticker.get('last')
        if price is None or price < self.cfg.price_min:
            return False

        # Reject if spread is too wide (no liquidity)
        if self.market_data:
            spread = self.market_data.get_spread_pct(symbol)
            if spread > self.cfg.max_spread:
                return False

        # Reject if ask price unavailable
        if self.market_data:
            ask = self.market_data.get_ask_price(symbol)
            if ask is None or ask <= 0:
                return False

        return True

    # ============================
    #         OHLCV CACHE
    # ============================
    def _get_ohlcv_cached(self, symbol: str, timeframe: str, limit: int) -> list:
        key = (symbol, timeframe)
        now = time.monotonic()
        cached = self._ohlcv_cache.get(key)
        if cached and (now - cached["ts"]) < self._ohlcv_cache_ttl:
            return cached["data"]
        data = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        self._ohlcv_cache[key] = {"data": data, "ts": now}
        return data

    # ============================
    #         GŁÓWNA PĘTLA
    # ============================
    def loop(self):

        scan_counter = 0

        self._send_tg(
            f"*LOWCAP SCALPER START* | Vol: {self.cfg.vol_min}-{self.cfg.vol_max}"
        )
        self.set_status("🏁 Lowcap startuje…")

        try:
            self.log("Inicjalizacja rynków Gate.io...")
            self.exchange.load_markets()
            self.all_symbols = [
                s for s in self.exchange.markets
                if "/USDT" in s and ":" not in s and "-" not in s and "3L" not in s and "3S" not in s
            ]
            self.log(f"Załadowano {len(self.all_symbols)} czystych par USDT.")
        except Exception as e:
            self.log(f"BŁĄD inicjalizacji giełdy: {e}")
            self.set_status("❌ Błąd inicjalizacji giełdy")
            self.running = False
            return

        while not self.stop_event.is_set():
            try:

                scan_counter += 1

                if self.market_data is None:
                    self.log("ERR: MarketData is None – lowcap nie ma ticków.")
                    self.set_status("❌ Brak MarketData – czekam…")
                    time.sleep(1)
                    continue

                all_tickers = self.market_data.get_all_tickers()
                if not all_tickers:
                    all_tickers = self.exchange.fetch_tickers()

                # ---------- SELL LOGIC ----------
                to_remove = []

                for s, pos in list(self.active_positions.items()):
                    price = self.market_data.get_last_price(s) or (all_tickers.get(s, {}).get('last'))
                    if price is None:
                        continue

                    # Safety: backfill fields for legacy positions
                    if 'entry_time' not in pos:
                        pos['entry_time'] = time.time() - 60
                    if 'atr_val' not in pos:
                        pos['atr_val'] = 0  # legacy positions — no longer used for SL
                    if 'sl_locked' not in pos:
                        pos['sl_locked'] = False

                    self.last_prices[s] = price

                    if price > pos['max']:
                        pos['max'] = price

                    # Use bid price for realistic exit valuation
                    bid_price = self.market_data.get_bid_price(s) if self.market_data else price
                    eval_price = bid_price if bid_price and bid_price > 0 else price

                    profit_pct = (eval_price / pos['buy']) - 1

                    # Fixed percentage SL — ATR on quiet low-caps was ≈0, causing instant triggers
                    sl_price = pos['buy'] * (1 - self.cfg.hard_sl_pct)

                    # Minimum hold time before SL/TS can trigger
                    hold_duration = time.time() - pos['entry_time']
                    held_long_enough = hold_duration >= self.cfg.min_hold_seconds

                    should_sell = False
                    reason = ""

                    if profit_pct >= self.cfg.target_profit:
                        should_sell = True
                        reason = "🟢 TP"
                    elif held_long_enough:
                        if (pos['max'] / pos['buy']) - 1 >= self.cfg.micro_tp:
                            # Lock profit: move SL to BE+0.2% after micro TP is reached
                            be_lock = pos['buy'] * 1.002
                            if sl_price < be_lock:
                                sl_price = be_lock
                                pos['sl_locked'] = True
                            if eval_price < pos['max'] * (1 - self.cfg.trailing_stop):
                                should_sell = True
                                reason = "TS 📉"
                        if not should_sell and eval_price < sl_price:
                            should_sell = True
                            reason = "🔴 SL"

                    if should_sell:
                        self.set_status(f"💰 SELL {s}")

                        sell_value = pos['qty'] * price
                        net = sell_value - (sell_value * self.cfg.fee)
                        gain = net - pos['stake']

                        if self.portfolio:
                            self.portfolio.balance_usdt += net
                        else:
                            self.balance += net
                        self.total_session_profit += gain

                        win = gain > 0
                        icon = "✅" if win else "❌"
                        if win:
                            self.wins += 1
                            self.consecutive_losses = 0  # reset on any win
                        else:
                            self.losses += 1
                            if reason == "🔴 SL":
                                self.consecutive_losses += 1
                                if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                                    self._pause_until = (
                                        time.monotonic()
                                        + self.cfg.consecutive_loss_pause
                                    )
                                    pause_msg = f"⏸️ {self.cfg.max_consecutive_losses} SL z rzędu — pauza {int(self.cfg.consecutive_loss_pause // 60)} minut"
                                    self.log(pause_msg)
                                    self.gui_log(pause_msg)
                            else:
                                self.consecutive_losses = 0

                        entry_price = pos['buy']
                        sell_price = price

                        wr = self._wr_text()
                        sell_msg = f"{icon} SELL {s} | {reason} | Entry: {entry_price:.8f} | Sell: {sell_price:.8f} | PnL: {gain:.2f}$ | {wr}"
                        self.gui_log(sell_msg)
                        self._send_tg(sell_msg)

                        # ── ML Analytics: rejestracja wyjścia ──
                        _tid_lc = self._trade_ids.pop(s, None)
                        if _tid_lc:
                            self.analytics.on_exit(
                                trade_id=_tid_lc,
                                exit_reason=reason,
                                pnl=gain,
                                pnl_pct=gain / pos['stake'] if pos['stake'] > 0 else 0,
                            )

                        if reason == "🔴 SL":
                            self._last_sl_symbols[s] = (
                                time.monotonic()
                                + self.cfg.sl_reentry_block_seconds
                            )

                        to_remove.append(s)
                        if reason == "🔴 SL":
                            self.cooldowns[s] = time.time() + self.cfg.cooldown_after_sl
                        else:
                            self.cooldowns[s] = time.time() + self.cfg.cooldown_seconds

                for s in to_remove:
                    del self.active_positions[s]

                # ---------- BUY LOGIC ----------
                max_pos = self.cfg.max_positions

                avail_bal = self.portfolio.balance_usdt if self.portfolio else self.balance
                if avail_bal >= self.cfg.min_stake_usd and len(self.active_positions) < max_pos:

                    if time.monotonic() < self._pause_until:
                        remaining = int(self._pause_until - time.monotonic())
                        self.set_status(f"⏸️ Pauza ochronna — {remaining}s")
                        time.sleep(5)
                        continue

                    # 🔥 OPTYMALIZACJA: Ogólny status zamiast wszystkich par w kółko
                    self.set_status(f"Analiza M1 - Skanowanie ({len(self.all_symbols)} par)")

                    candidates = []
                    for s in self.all_symbols:
                        ticker_data = all_tickers.get(s)
                        if ticker_data and self.passes_market_filters(s, ticker_data):
                            candidates.append(s)

                    _EXCLUDED = [
                        'USD1', 'USDC', 'BUSD', 'DAI', 'TUSD',
                        'FDUSD', 'PAXG', 'XAUT', 'USDD', 'FRAX',
                        'USDT', 'USDP',
                    ]
                    candidates = [
                        s for s in candidates
                        if not any(p in s.replace('/USDT', '') for p in _EXCLUDED)
                    ]

                    random.shuffle(candidates)

                    btc_regime_ok = True
                    try:
                        btc_ohlcv = self.exchange.fetch_ohlcv("BTC/USDT", "5m", 20)
                        if btc_ohlcv and len(btc_ohlcv) >= 15:
                            btc_df = pd.DataFrame(btc_ohlcv,
                                columns=["ts", "open", "high", "low", "close", "vol"])
                            btc_atr = ta.atr(btc_df["high"], btc_df["low"],
                                btc_df["close"], length=14)
                            btc_price = float(btc_df["close"].iloc[-1])
                            btc_atr_pct = float(btc_atr.iloc[-1]) / btc_price
                            if btc_atr_pct < self.cfg.btc_min_atr_pct:
                                btc_regime_ok = False
                    except Exception:
                        pass

                    for s in candidates[:self.cfg.scan_limit]:
                        if self.stop_event.is_set():
                            break

                        if time.monotonic() < self._last_sl_symbols.get(s, 0):
                            continue

                        if not btc_regime_ok:
                            self.set_status("💤 BTC martwy — pomijam skanowanie")
                            break

                        try:
                            # Wyświetla tylko parę, która faktycznie wchodzi w analizę techniczną
                            self.set_status(f"Analiza M1: {s}")
                            ohlcv = self._get_ohlcv_cached(s, '1m', 50)
                            if not ohlcv:
                                continue

                            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
                            if not self.micro_trend_ok(df):
                                continue

                            # M5 trend confirmation: EMA9 > EMA21 > EMA50, slope EMA21 positive
                            try:
                                ohlcv_5m = self._get_ohlcv_cached(s, '5m', 60)
                                if ohlcv_5m and len(ohlcv_5m) >= 51:
                                    df5 = pd.DataFrame(ohlcv_5m, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
                                    ema9_5  = ta.ema(df5['close'], length=9)
                                    ema21_5 = ta.ema(df5['close'], length=21)
                                    ema50_5 = ta.ema(df5['close'], length=50)
                                    m5_trend_ok = (
                                        float(ema9_5.iloc[-1])  > float(ema21_5.iloc[-1])
                                        and float(ema21_5.iloc[-1]) > float(ema50_5.iloc[-1])
                                    )
                                    slope_ok = (
                                        len(ema21_5) >= 3
                                        and float(ema21_5.iloc[-1]) > float(ema21_5.iloc[-3])
                                    )
                                    if not (m5_trend_ok and slope_ok):
                                        continue
                            except Exception:
                                pass  # if M5 data unavailable, allow entry

                            price = all_tickers[s]['last']
                            prev_high = df['high'].iloc[-2]
                            base_score, v_ratio = self.score_symbol(df, prev_high)

                            atr_val = ta.atr(df['high'], df['low'], df['close'], self.cfg.atr_length).iloc[-1]
                            atr_pct = atr_val / price if price > 0 else 0

                            # Reject dump&pump patterns — too volatile
                            if atr_pct > self.cfg.max_atr_pct:
                                continue

                            final_score = self.ai_score_boost(base_score, {"vol_ratio": v_ratio, "atr": atr_pct})
                            threshold = self.dynamic_score_threshold() + self.cfg.score_threshold_boost

                            if final_score >= threshold:
                                # Get real entry price (ask) not last
                                ask_price = self.market_data.get_ask_price(s) if self.market_data else None
                                if ask_price is None:
                                    continue

                                # Final spread check at moment of entry
                                spread_at_entry = self.market_data.get_spread_pct(s) if self.market_data else 0
                                if spread_at_entry > self.cfg.max_spread_pct:
                                    self.log(f"⏭ SKIP {s} — spread at entry: {spread_at_entry*100:.2f}%")
                                    continue

                                self.set_status(f"💰 BUY {s}")

                                avail = self.portfolio.balance_usdt if self.portfolio else self.balance
                                stake = min(self.cfg.max_stake_per_trade, avail)

                                if stake >= self.cfg.min_stake_usd:
                                    # ── ML Analytics: rejestracja wejścia ──
                                    _features_lc = {
                                        'score':             final_score,
                                        'vol_ratio':         v_ratio,
                                        'body_ratio':        0.5,
                                        'atr_pct':           atr_pct,
                                        'upper_wick_ratio':  0.2,
                                        'breakout_strength': 1.002,
                                        'ema9_slope':        0.0,
                                        'ema21_slope':       0.0,
                                        'ema_spread':        0.0,
                                        'btc_trend':         1 if btc_regime_ok else -1,
                                        'btc_atr_pct':       0.0,
                                        'btc_vol_ratio':     1.0,
                                    }
                                    _ml_lc = self.analytics.on_entry(
                                        symbol=s, entry_price=ask_price, stake=stake,
                                        sl_pct=self.cfg.hard_sl_pct,
                                        tp1_pct=self.cfg.target_profit,
                                        features=_features_lc,
                                    )
                                    self._trade_ids[s] = _ml_lc['trade_id']

                                    if self.analytics.is_symbol_blocked(s):
                                        continue
                                    _ml_blocked_lc, _ml_reason_lc = self.analytics.is_trading_blocked()
                                    if _ml_blocked_lc:
                                        self.set_status(f"🤖 {_ml_reason_lc}")
                                        continue

                                    self.active_positions[s] = {
                                        "buy": ask_price,
                                        "max": ask_price,
                                        "qty": stake / ask_price,
                                        "stake": stake,
                                        "vol_ratio": v_ratio,
                                        "entry_time": time.time(),
                                        "sl_locked": False,
                                    }

                                    if self.portfolio:
                                        self.portfolio.balance_usdt -= stake
                                    else:
                                        self.balance -= stake

                                    buy_msg = f"💰 BUY {s} | Entry Price: {ask_price:.8f} | Stawka: {stake:.2f}$"
                                    self.gui_log(buy_msg)
                                    self._send_tg(buy_msg)
                                    break
                        except Exception as e:
                            continue

                # Po skończeniu pętli analizy, ustawiamy status bezczynności/oczekiwania
                self.set_status(f"Czekam... | Aktywne: {len(self.active_positions)}")
                time.sleep(self.cfg.loop_sleep)

            except Exception as e:
                self.log(f"ERR: {e}")
                self.set_status("❌ Błąd pętli – pauza 1s")
                time.sleep(1)

        self.running = False
        self.set_status("🛑 BOT ZATRZYMANY")
        self.log("Bot stopped cleanly.")

    # ============================
    #         START / STOP
    # ============================
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