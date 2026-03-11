"""
analytics_engine.py — GateBot ML Analytics System v3
======================================================
Każdy bot ma OSOBNĄ instancję z własną bazą danych i modelami ML.

Poziomy ML (per bot, niezależnie):
  Poziom 1 (0-199 transakcji):   Rule Engine
  Poziom 2 (200-499):            + Logistic Regression
  Poziom 3 (500-999):            + Random Forest
  Poziom 4 (1000+):              + Meta Model (LightGBM)

Użycie:
    from analytics_engine import get_analytics
    analytics = get_analytics('scalper')   # osobna instancja
    analytics = get_analytics('lowcap')    # osobna instancja
    analytics = get_analytics('grid_bot')  # osobna instancja
"""

import sqlite3
import json
import time
import threading
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#   PER-BOT KONFIGURACJA
# ══════════════════════════════════════════════════════

BOT_PROFILES = {
    'scalper': {
        'vol_ratio_good':         2.5,
        'vol_ratio_bad':          5.0,
        'atr_min':                0.003,
        'atr_max':                0.020,
        'hold_max_ok':            300,
        'min_samples_rule':       8,
        'blacklist_wr_threshold': 0.30,
        'whitelist_wr_threshold': 0.65,
        'bad_hour_wr':            0.35,
        'good_hour_wr':           0.65,
        'score_boost_good_hour':  -5,
        'score_penalty_bad_hour': 12,
        'score_penalty_btc_down': 8,
        'consecutive_loss_block': 4,
        'ml_threshold_l2':        0.52,
        'ml_threshold_l3':        0.55,
        'ml_threshold_l4':        0.58,
        # v3: overtrading control
        'overtrade_window_min':   10,
        'overtrade_max_trades':   15,
        'overtrade_score_penalty': 10,
    },
    'lowcap': {
        'vol_ratio_good':         4.0,
        'vol_ratio_bad':          15.0,
        'atr_min':                0.008,
        'atr_max':                0.060,
        'hold_max_ok':            900,
        'min_samples_rule':       6,
        'blacklist_wr_threshold': 0.25,
        'whitelist_wr_threshold': 0.60,
        'bad_hour_wr':            0.30,
        'good_hour_wr':           0.60,
        'score_boost_good_hour':  -8,
        'score_penalty_bad_hour': 15,
        'score_penalty_btc_down': 12,
        'consecutive_loss_block': 3,
        'ml_threshold_l2':        0.54,
        'ml_threshold_l3':        0.57,
        'ml_threshold_l4':        0.60,
        'overtrade_window_min':   10,
        'overtrade_max_trades':   8,
        'overtrade_score_penalty': 12,
    },
    'grid_bot': {
        'vol_ratio_good':         6.0,
        'vol_ratio_bad':          30.0,
        'atr_min':                0.015,
        'atr_max':                0.120,
        'hold_max_ok':            1800,
        'min_samples_rule':       5,
        'blacklist_wr_threshold': 0.25,
        'whitelist_wr_threshold': 0.55,
        'bad_hour_wr':            0.30,
        'good_hour_wr':           0.55,
        'score_boost_good_hour':  -5,
        'score_penalty_bad_hour': 10,
        'score_penalty_btc_down': 15,
        'consecutive_loss_block': 3,
        'ml_threshold_l2':        0.55,
        'ml_threshold_l3':        0.58,
        'ml_threshold_l4':        0.62,
        'overtrade_window_min':   10,
        'overtrade_max_trades':   6,
        'overtrade_score_penalty': 10,
    },
}

# ── Feature columns for ML models ──
# Original v2 features (backward compatible)
_V2_FEATURE_COLS = [
    'score', 'vol_ratio', 'body_ratio', 'atr_pct',
    'upper_wick_ratio', 'breakout_strength', 'ema9_slope',
    'ema21_slope', 'ema_spread', 'btc_trend', 'btc_atr_pct',
    'btc_vol_ratio', 'hour', 'day_of_week'
]

# v3 extended features
_V3_FEATURE_COLS = [
    # Trend
    'ema_fast', 'ema_slow', 'ema_diff_pct', 'price_vs_vwap', 'rsi', 'macd_hist',
    # Volatility
    'bb_width', 'recent_range_pct',
    # Momentum
    'last_5s_return', 'last_3s_return', 'candle_body_ratio',
    # Volume
    'volume_zscore', 'volume_trend', 'volume_acceleration',
    # Orderbook / microstructure
    'bid_ask_spread', 'bid_volume', 'ask_volume', 'orderbook_imbalance',
    # Trade context
    'time_since_last_trade', 'trades_last_10m', 'market_regime',
]

FEATURE_COLS = _V2_FEATURE_COLS + _V3_FEATURE_COLS

# Market regime encoding: 0=RANGE, 1=TREND, 2=HIGH_VOLATILITY, 3=LOW_VOLATILITY
REGIME_MAP = {'RANGE': 0, 'TREND': 1, 'HIGH_VOLATILITY': 2, 'LOW_VOLATILITY': 3}


# ══════════════════════════════════════════════════════
#   DATA CLASSES
# ══════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    bot: str
    symbol: str
    timestamp: str
    hour: int
    day_of_week: int
    session: str
    entry_price: float
    stake: float
    sl_pct: float
    tp1_pct: float
    # v2 features
    score: float
    vol_ratio: float
    body_ratio: float
    atr_pct: float
    upper_wick_ratio: float
    breakout_strength: float
    ema9_slope: float
    ema21_slope: float
    ema_spread: float
    btc_trend: int
    btc_atr_pct: float
    btc_vol_ratio: float
    ml_level: int
    ml_confidence: float
    ml_approved: int
    exit_reason: str
    pnl: float
    pnl_pct: float
    hold_seconds: float
    win: int
    # v3 extended features (optional, default 0)
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_diff_pct: float = 0.0
    price_vs_vwap: float = 0.0
    rsi: float = 0.0
    macd_hist: float = 0.0
    bb_width: float = 0.0
    recent_range_pct: float = 0.0
    last_5s_return: float = 0.0
    last_3s_return: float = 0.0
    candle_body_ratio: float = 0.0
    volume_zscore: float = 0.0
    volume_trend: float = 0.0
    volume_acceleration: float = 0.0
    bid_ask_spread: float = 0.0
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    orderbook_imbalance: float = 0.5
    time_since_last_trade: float = 0.0
    trades_last_10m: int = 0
    market_regime: int = 0
    # v3 exit analytics
    mfe: float = 0.0  # max favorable excursion (%)
    mae: float = 0.0  # max adverse excursion (%)


@dataclass
class ParameterAdjustment:
    bot: str
    min_score_delta: int
    entry_vol_mult_delta: float
    max_positions_override: Optional[int]
    symbol_blacklist: list
    symbol_whitelist: list
    trading_blocked: bool
    block_reason: str
    ml_level: int
    trade_count: int
    confidence: float
    # v3 dynamic outputs (optional, default-safe)
    position_size_multiplier: float = 1.0  # 0.5 - 2.0
    tp_adjustment: float = 1.0             # multiplier on TP (e.g. 1.2 = +20%)
    sl_adjustment: float = 1.0             # multiplier on SL (e.g. 0.8 = -20%)
    entry_quality_score: float = 0.0       # bonus for entry filter (-1.0 to +1.0)


# ══════════════════════════════════════════════════════
#   DATABASE
# ══════════════════════════════════════════════════════

# v3 columns to add if missing (backward compat migration)
_V3_DB_COLUMNS = [
    ('ema_fast', 'REAL', '0.0'),
    ('ema_slow', 'REAL', '0.0'),
    ('ema_diff_pct', 'REAL', '0.0'),
    ('price_vs_vwap', 'REAL', '0.0'),
    ('rsi', 'REAL', '0.0'),
    ('macd_hist', 'REAL', '0.0'),
    ('bb_width', 'REAL', '0.0'),
    ('recent_range_pct', 'REAL', '0.0'),
    ('last_5s_return', 'REAL', '0.0'),
    ('last_3s_return', 'REAL', '0.0'),
    ('candle_body_ratio', 'REAL', '0.0'),
    ('volume_zscore', 'REAL', '0.0'),
    ('volume_trend', 'REAL', '0.0'),
    ('volume_acceleration', 'REAL', '0.0'),
    ('bid_ask_spread', 'REAL', '0.0'),
    ('bid_volume', 'REAL', '0.0'),
    ('ask_volume', 'REAL', '0.0'),
    ('orderbook_imbalance', 'REAL', '0.5'),
    ('time_since_last_trade', 'REAL', '0.0'),
    ('trades_last_10m', 'INTEGER', '0'),
    ('market_regime', 'INTEGER', '0'),
    ('mfe', 'REAL', '0.0'),
    ('mae', 'REAL', '0.0'),
]


class TradeDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    hour INTEGER,
                    day_of_week INTEGER,
                    session TEXT,
                    entry_price REAL,
                    stake REAL,
                    sl_pct REAL,
                    tp1_pct REAL,
                    score REAL,
                    vol_ratio REAL,
                    body_ratio REAL,
                    atr_pct REAL,
                    upper_wick_ratio REAL,
                    breakout_strength REAL,
                    ema9_slope REAL,
                    ema21_slope REAL,
                    ema_spread REAL,
                    btc_trend INTEGER,
                    btc_atr_pct REAL,
                    btc_vol_ratio REAL,
                    ml_level INTEGER DEFAULT 1,
                    ml_confidence REAL DEFAULT 0.0,
                    ml_approved INTEGER DEFAULT 1,
                    exit_reason TEXT,
                    pnl REAL,
                    pnl_pct REAL,
                    hold_seconds REAL,
                    win INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_win ON trades(win)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hour ON trades(hour)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON trades(symbol)")
            # v3 migration: add new columns if missing
            self._migrate_v3(conn)

    def _migrate_v3(self, conn):
        existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        for col_name, col_type, default in _V3_DB_COLUMNS:
            if col_name not in existing:
                conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {col_name} {col_type} DEFAULT {default}"
                )

    def _get_conn(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def insert_trade(self, record: TradeRecord):
        with self._lock:
            with self._get_conn() as conn:
                d = asdict(record)
                cols = ', '.join(d.keys())
                placeholders = ', '.join(['?' for _ in d])
                conn.execute(
                    f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                    list(d.values())
                )

    def get_trades(self, limit: int = 2000) -> list:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE win IS NOT NULL ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            cols = [d[0] for d in conn.execute("PRAGMA table_info(trades)").fetchall()]
            return [dict(zip(cols, r)) for r in rows]

    def count_trades(self) -> int:
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM trades WHERE win IS NOT NULL"
            ).fetchone()[0]


# ══════════════════════════════════════════════════════
#   RULE ENGINE (Poziom 1)
# ══════════════════════════════════════════════════════

class RuleEngine:
    def __init__(self, bot: str):
        self.bot = bot
        self.profile = BOT_PROFILES[bot]

    def analyze(self, trades: list) -> ParameterAdjustment:
        p = self.profile
        adj = ParameterAdjustment(
            bot=self.bot, min_score_delta=0, entry_vol_mult_delta=0.0,
            max_positions_override=None, symbol_blacklist=[], symbol_whitelist=[],
            trading_blocked=False, block_reason="", ml_level=1,
            trade_count=len(trades), confidence=0.0,
        )

        min_s = p['min_samples_rule']
        if len(trades) < min_s:
            return adj

        # ── 1. Seria strat z rzędu ──
        recent_losses = sum(1 for t in trades[:5] if t.get('win') == 0)
        if recent_losses >= p['consecutive_loss_block']:
            adj.trading_blocked = True
            adj.block_reason = (
                f"⛔ ML [{self.bot}]: {recent_losses}/5 strat z rzędu — pauza ochronna"
            )
            return adj

        # ── 2. Godzinowy WR ──
        hour_stats = {}
        for t in trades:
            h = t.get('hour', 0)
            if h not in hour_stats:
                hour_stats[h] = {'wins': 0, 'total': 0}
            hour_stats[h]['total'] += 1
            hour_stats[h]['wins'] += t.get('win', 0)

        h_now = datetime.now().hour
        if h_now in hour_stats and hour_stats[h_now]['total'] >= min_s:
            wr = hour_stats[h_now]['wins'] / hour_stats[h_now]['total']
            if wr < p['bad_hour_wr']:
                adj.min_score_delta += p['score_penalty_bad_hour']
            elif wr > p['good_hour_wr']:
                adj.min_score_delta += p['score_boost_good_hour']

        # ── 3. Symbol blacklist / whitelist ──
        sym_stats = {}
        for t in trades:
            s = t.get('symbol', '')
            if s not in sym_stats:
                sym_stats[s] = {'wins': 0, 'total': 0}
            sym_stats[s]['total'] += 1
            sym_stats[s]['wins'] += t.get('win', 0)

        for sym, ss in sym_stats.items():
            if ss['total'] >= max(min_s, 8):
                wr = ss['wins'] / ss['total']
                if wr < p['blacklist_wr_threshold']:
                    adj.symbol_blacklist.append(sym)
                elif wr > p['whitelist_wr_threshold']:
                    adj.symbol_whitelist.append(sym)

        # ── 4. BTC kontekst ──
        btc_down = [t for t in trades if t.get('btc_trend') == -1]
        if len(btc_down) >= min_s:
            wr = sum(t.get('win', 0) for t in btc_down) / len(btc_down)
            if wr < 0.35:
                adj.min_score_delta += p['score_penalty_btc_down']

        # ── 5. Vol ratio ──
        high_vol = [t for t in trades if (t.get('vol_ratio') or 0) > p['vol_ratio_good']]
        if len(high_vol) >= min_s:
            wr = sum(t.get('win', 0) for t in high_vol) / len(high_vol)
            if wr < 0.40:
                adj.entry_vol_mult_delta += 0.5

        # ── 6. Overtrading detection (v3) ──
        # If recent trades are too frequent AND losing, penalize
        window_min = p.get('overtrade_window_min', 10)
        max_recent = p.get('overtrade_max_trades', 15)
        if len(trades) >= max_recent:
            recent = trades[:max_recent]
            if recent:
                first_ts = recent[-1].get('timestamp', '')
                last_ts = recent[0].get('timestamp', '')
                try:
                    dt0 = datetime.fromisoformat(first_ts)
                    dt1 = datetime.fromisoformat(last_ts)
                    span_min = (dt1 - dt0).total_seconds() / 60.0
                except Exception:
                    span_min = 999
                if span_min <= window_min and span_min > 0:
                    recent_wr = sum(t.get('win', 0) for t in recent) / len(recent)
                    if recent_wr < 0.45:
                        adj.min_score_delta += p.get('overtrade_score_penalty', 10)

        # ── 7. MFE/MAE-based TP/SL adjustment (v3) ──
        mfe_vals = [t.get('mfe', 0) for t in trades if t.get('mfe')]
        mae_vals = [t.get('mae', 0) for t in trades if t.get('mae')]
        if len(mfe_vals) >= min_s and len(mae_vals) >= min_s:
            avg_mfe = sum(mfe_vals) / len(mfe_vals)
            avg_mae = sum(mae_vals) / len(mae_vals)
            # If average MFE >> current TP, suggest wider TP
            # If average MAE << current SL, suggest tighter SL
            wins_only = [t for t in trades if t.get('win') == 1]
            if len(wins_only) >= min_s:
                win_mfe = [t.get('mfe', 0) for t in wins_only if t.get('mfe')]
                if win_mfe:
                    median_win_mfe = sorted(win_mfe)[len(win_mfe) // 2]
                    avg_tp = sum(t.get('tp1_pct', 0) for t in wins_only) / len(wins_only)
                    if avg_tp > 0 and median_win_mfe > avg_tp * 1.5:
                        adj.tp_adjustment = min(1.3, median_win_mfe / avg_tp)
            losses_only = [t for t in trades if t.get('win') == 0]
            if len(losses_only) >= min_s:
                loss_mae = [t.get('mae', 0) for t in losses_only if t.get('mae')]
                if loss_mae:
                    median_loss_mae = sorted(loss_mae)[len(loss_mae) // 2]
                    avg_sl = sum(t.get('sl_pct', 0) for t in losses_only) / len(losses_only)
                    if avg_sl > 0 and median_loss_mae < avg_sl * 0.7:
                        adj.sl_adjustment = max(0.7, median_loss_mae / avg_sl)

        # ── 8. Market regime analysis (v3) ──
        regime_stats = {}
        for t in trades:
            r = t.get('market_regime', 0)
            regime_stats.setdefault(r, {'wins': 0, 'total': 0})
            regime_stats[r]['total'] += 1
            regime_stats[r]['wins'] += t.get('win', 0)
        # If HIGH_VOLATILITY regime has poor WR, add penalty
        hv = regime_stats.get(REGIME_MAP['HIGH_VOLATILITY'], {'wins': 0, 'total': 0})
        if hv['total'] >= min_s:
            hv_wr = hv['wins'] / hv['total']
            if hv_wr < 0.35:
                adj.min_score_delta += 5

        adj.confidence = min(0.5, len(trades) / 200)
        return adj


# ══════════════════════════════════════════════════════
#   ML PIPELINE (Poziomy 2-4)
# ══════════════════════════════════════════════════════

class MLPipeline:
    def __init__(self, bot: str):
        self.bot = bot
        self.profile = BOT_PROFILES[bot]
        self.model = None
        self.scaler = None
        self.current_level = 1
        self.last_train_time = 0.0
        self.last_auc = 0.0
        self.n_samples_trained = 0
        self._lock = threading.Lock()
        self._use_raw = False

    def get_level(self, trade_count: int) -> int:
        if trade_count >= 1000: return 4
        if trade_count >= 500:  return 3
        if trade_count >= 200:  return 2
        return 1

    def train(self, trades: list, level: int):
        if level < 2:
            return
        try:
            import numpy as np
            from sklearn.preprocessing import StandardScaler
            from sklearn.linear_model import LogisticRegression
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.model_selection import cross_val_score

            X = np.array([[float(t.get(c) or 0) for c in FEATURE_COLS] for t in trades])
            y = np.array([int(t.get('win', 0)) for t in trades])

            if len(X) < 50 or y.sum() < 10 or (1 - y).sum() < 10:
                return

            with self._lock:
                scaler = StandardScaler()
                X_scaled = scaler.fit_transform(X)
                use_raw = False

                if level == 2:
                    model = LogisticRegression(
                        C=0.1, max_iter=1000,
                        class_weight='balanced', random_state=42
                    )
                    model.fit(X_scaled, y)
                    X_fit = X_scaled

                elif level == 3:
                    model = RandomForestClassifier(
                        n_estimators=150, max_depth=6,
                        min_samples_leaf=10, class_weight='balanced',
                        random_state=42, n_jobs=-1
                    )
                    model.fit(X_scaled, y)
                    X_fit = X_scaled

                else:  # level 4
                    try:
                        import lightgbm as lgb
                        model = lgb.LGBMClassifier(
                            n_estimators=300, max_depth=5,
                            learning_rate=0.03, num_leaves=24,
                            min_child_samples=20, subsample=0.8,
                            colsample_bytree=0.8, class_weight='balanced',
                            random_state=42, verbose=-1,
                        )
                        model.fit(X, y)
                        X_fit = X
                        use_raw = True
                    except ImportError:
                        logger.warning(f"[ML:{self.bot}] LightGBM brak — RF fallback")
                        model = RandomForestClassifier(
                            n_estimators=300, max_depth=8,
                            min_samples_leaf=8, class_weight='balanced',
                            random_state=42, n_jobs=-1
                        )
                        model.fit(X_scaled, y)
                        X_fit = X_scaled

                try:
                    cv = cross_val_score(
                        model, X_fit, y,
                        cv=min(5, len(X) // 20), scoring='roc_auc'
                    )
                    auc = float(cv.mean())
                except Exception:
                    auc = 0.5

                self.model = model
                self.scaler = scaler
                self.current_level = level
                self.last_train_time = time.monotonic()
                self.last_auc = auc
                self.n_samples_trained = len(X)
                self._use_raw = use_raw

            logger.info(
                f"[ML:{self.bot}] Poziom {level} | AUC={auc:.3f} | n={len(X)}"
            )

        except ImportError:
            logger.warning(f"[ML:{self.bot}] sklearn brak — pip install scikit-learn")
        except Exception as e:
            logger.error(f"[ML:{self.bot}] Blad trenowania: {e}")

    def predict(self, features: dict, level: int) -> dict:
        """Returns dict with approved, win_prob, position_size_multiplier, entry_quality_score."""
        result = {
            'approved': True,
            'win_prob': 0.5,
            'position_size_multiplier': 1.0,
            'entry_quality_score': 0.0,
        }
        if self.model is None or level < 2:
            # Level 1: compute entry_quality_score from raw features
            result['entry_quality_score'] = self._calc_entry_quality(features)
            return result
        try:
            import numpy as np
            p = self.profile
            thresholds = {
                2: p['ml_threshold_l2'],
                3: p['ml_threshold_l3'],
                4: p['ml_threshold_l4'],
            }
            threshold = thresholds.get(min(level, 4), 0.55)

            row = np.array([[float(features.get(c) or 0) for c in FEATURE_COLS]])
            X_in = row if self._use_raw else self.scaler.transform(row)
            proba = self.model.predict_proba(X_in)[0]
            win_prob = float(proba[1]) if len(proba) > 1 else 0.5

            result['approved'] = win_prob >= threshold
            result['win_prob'] = win_prob
            result['entry_quality_score'] = self._calc_entry_quality(features)

            # Dynamic position sizing based on confidence
            if win_prob >= 0.65:
                result['position_size_multiplier'] = 1.5
            elif win_prob >= 0.60:
                result['position_size_multiplier'] = 1.25
            elif win_prob < 0.50:
                result['position_size_multiplier'] = 0.5
            elif win_prob < 0.52:
                result['position_size_multiplier'] = 0.75
            # else 1.0

            return result

        except Exception as e:
            logger.error(f"[ML:{self.bot}] Blad predykcji: {e}")
            return result

    def _calc_entry_quality(self, features: dict) -> float:
        """
        Compute entry quality score from raw features (-1.0 to +1.0).
        Higher = better conditions for entry.
        Combines: trend, volume, volatility, momentum, orderbook.
        """
        score = 0.0
        n_components = 0

        # Trend: EMA diff positive = uptrend
        ema_diff = float(features.get('ema_diff_pct') or 0)
        if ema_diff != 0:
            score += max(-0.2, min(0.2, ema_diff * 10))
            n_components += 1

        # RSI: 40-60 = neutral, >60 = strong, <40 = weak
        rsi = float(features.get('rsi') or 0)
        if rsi > 0:
            if 45 <= rsi <= 65:
                score += 0.1
            elif rsi > 70 or rsi < 30:
                score -= 0.15
            n_components += 1

        # Volume: high z-score = strong signal
        vol_z = float(features.get('volume_zscore') or 0)
        if vol_z > 0:
            score += min(0.2, vol_z * 0.1)
            n_components += 1

        # Orderbook imbalance: deviation from 0.5 in trade direction
        ob_imb = float(features.get('orderbook_imbalance') or 0.5)
        score += (ob_imb - 0.5) * 0.4
        n_components += 1

        # Volatility: moderate is best; too high or too low is bad
        atr = float(features.get('atr_pct') or 0)
        if atr > 0:
            if 0.003 <= atr <= 0.015:
                score += 0.1
            elif atr > 0.03:
                score -= 0.1
            n_components += 1

        # Bid-ask spread: tight spread = better
        spread = float(features.get('bid_ask_spread') or 0)
        if spread > 0:
            if spread < 0.0003:
                score += 0.1
            elif spread > 0.001:
                score -= 0.1
            n_components += 1

        return max(-1.0, min(1.0, score))

    def get_feature_importance(self) -> dict:
        if self.model is None:
            return {}
        try:
            import numpy as np
            if hasattr(self.model, 'feature_importances_'):
                imp = self.model.feature_importances_
            elif hasattr(self.model, 'coef_'):
                imp = np.abs(self.model.coef_[0])
                imp = imp / imp.sum() if imp.sum() > 0 else imp
            else:
                return {}
            return {col: round(float(imp[i]), 4) for i, col in enumerate(FEATURE_COLS) if i < len(imp)}
        except Exception:
            return {}


# ══════════════════════════════════════════════════════
#   ANALYTICS ENGINE — jedna instancja per bot
# ══════════════════════════════════════════════════════

class AnalyticsEngine:
    def __init__(self, bot: str):
        self.bot = bot
        self.db = TradeDatabase(f"analytics/analytics_{bot}.db")
        self.rule_engine = RuleEngine(bot)
        self.ml_pipeline = MLPipeline(bot)
        self._adjustment = self._empty_adj()
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._retrain_counter = 0
        self._start_background()
        logger.info(f"[Analytics:{bot}] Uruchomiony")

    # ── ENTRY ──

    def on_entry(self, symbol: str, entry_price: float,
                 stake: float, sl_pct: float, tp1_pct: float,
                 features: dict) -> dict:
        now = datetime.now()
        count = self.db.count_trades()
        level = self.ml_pipeline.get_level(count)

        # v3: ML predict returns richer dict
        ml_result = self.ml_pipeline.predict(features, level)
        approved = ml_result['approved']
        confidence = ml_result['win_prob']

        trade_id = f"{self.bot}_{symbol}_{int(time.time()*1000)}"
        with self._lock:
            pending_data = dict(
                bot=self.bot, symbol=symbol,
                timestamp=now.isoformat(),
                hour=now.hour, day_of_week=now.weekday(),
                session=self._session(now.hour),
                entry_price=entry_price, stake=stake,
                sl_pct=sl_pct, tp1_pct=tp1_pct,
                ml_level=level, ml_confidence=confidence,
                ml_approved=1 if approved else 0,
                _entry_time=time.monotonic(),
            )
            # Store all features (v2 + v3)
            for col in FEATURE_COLS:
                pending_data[col] = float(features.get(col) or 0)
            # Keep original v2 keys that map differently
            pending_data['score'] = float(features.get('score') or 0)
            pending_data['vol_ratio'] = float(features.get('vol_ratio') or 0)
            pending_data['body_ratio'] = float(features.get('body_ratio') or 0)
            pending_data['atr_pct'] = float(features.get('atr_pct') or 0)
            pending_data['upper_wick_ratio'] = float(features.get('upper_wick_ratio') or 0)
            pending_data['breakout_strength'] = float(features.get('breakout_strength') or 1.0)
            pending_data['ema9_slope'] = float(features.get('ema9_slope') or 0)
            pending_data['ema21_slope'] = float(features.get('ema21_slope') or 0)
            pending_data['ema_spread'] = float(features.get('ema_spread') or 0)
            pending_data['btc_trend'] = int(features.get('btc_trend') or 0)
            pending_data['btc_atr_pct'] = float(features.get('btc_atr_pct') or 0)
            pending_data['btc_vol_ratio'] = float(features.get('btc_vol_ratio') or 1.0)
            self._pending[trade_id] = pending_data

        return dict(
            trade_id=trade_id,
            approved=approved,
            confidence=confidence,
            ml_level=level,
            trade_count=count,
            position_size_multiplier=ml_result['position_size_multiplier'],
            entry_quality_score=ml_result['entry_quality_score'],
        )

    # ── EXIT ──

    def on_exit(self, trade_id: str, exit_reason: str, pnl: float, pnl_pct: float,
                mfe: float = 0.0, mae: float = 0.0):
        with self._lock:
            p = self._pending.pop(trade_id, None)
        if not p:
            return
        hold = time.monotonic() - p.pop('_entry_time', time.monotonic())
        # Build record from pending data
        record_data = {}
        for k in TradeRecord.__dataclass_fields__:
            if k in p:
                record_data[k] = p[k]
        record = TradeRecord(
            **record_data,
            exit_reason=exit_reason, pnl=pnl, pnl_pct=pnl_pct,
            hold_seconds=hold, win=1 if pnl > 0 else 0,
            mfe=mfe, mae=mae,
        )
        self.db.insert_trade(record)
        self._retrain_counter += 1
        if self._retrain_counter >= 25:
            self._retrain_counter = 0
            self._trigger_retrain()

    # ── INTERFEJS DLA SILNIKOW ──

    def get_adjustment(self) -> ParameterAdjustment:
        """Full adjustment object for engine integration."""
        return self._adjustment

    def is_symbol_blocked(self, symbol: str) -> bool:
        return symbol in self._adjustment.symbol_blacklist

    def is_trading_blocked(self) -> tuple:
        adj = self._adjustment
        return adj.trading_blocked, adj.block_reason

    def get_score_adjustment(self) -> int:
        return self._adjustment.min_score_delta

    def get_vol_mult_adjustment(self) -> float:
        return self._adjustment.entry_vol_mult_delta

    def get_preferred_symbols(self) -> list:
        return self._adjustment.symbol_whitelist

    # ── DASHBOARD ──

    def get_dashboard_data(self) -> dict:
        trades = self.db.get_trades(limit=1000)
        count = len(trades)
        if count == 0:
            return self._empty_dashboard()

        wins = sum(t.get('win', 0) for t in trades)
        total_pnl = sum(t.get('pnl') or 0 for t in trades)

        # Godziny
        h_stats = {}
        for t in trades:
            h = t.get('hour', 0)
            h_stats.setdefault(h, {'wins': 0, 'total': 0})
            h_stats[h]['total'] += 1
            h_stats[h]['wins'] += t.get('win', 0)
        hour_wr = {
            h: {'wr': round(v['wins']/v['total']*100, 1), 'total': v['total']}
            for h, v in h_stats.items() if v['total'] >= 3
        }

        # Symbole
        s_stats = {}
        for t in trades:
            s = t.get('symbol', '')
            s_stats.setdefault(s, {'wins': 0, 'total': 0, 'pnl': 0})
            s_stats[s]['total'] += 1
            s_stats[s]['wins'] += t.get('win', 0)
            s_stats[s]['pnl'] += (t.get('pnl') or 0)
        sym_wr = sorted([
            {'symbol': s, 'wr': round(v['wins']/v['total']*100, 1),
             'total': v['total'], 'pnl': round(v['pnl'], 2)}
            for s, v in s_stats.items() if v['total'] >= 5
        ], key=lambda x: x['wr'], reverse=True)

        # Exit reasons
        ex_stats = {}
        for t in trades:
            er = t.get('exit_reason') or 'unknown'
            ex_stats.setdefault(er, {'count': 0, 'wins': 0, 'pnl': 0})
            ex_stats[er]['count'] += 1
            ex_stats[er]['wins'] += t.get('win', 0)
            ex_stats[er]['pnl'] += (t.get('pnl') or 0)

        ml_level = self.ml_pipeline.get_level(count)
        next_at = {1: 200, 2: 500, 3: 1000, 4: None}.get(ml_level)
        base_at = {1: 0, 2: 200, 3: 500, 4: 1000}.get(ml_level, 0)
        progress = min(100, int((count - base_at) / (next_at - base_at) * 100)) if next_at else 100

        adj = self._adjustment
        return {
            'bot': self.bot,
            'trade_count': count,
            'win_rate': round(wins / count * 100, 1) if count else 0,
            'total_pnl': round(total_pnl, 2),
            'hour_wr': hour_wr,
            'symbol_wr': sym_wr[:10],
            'worst_symbols': list(reversed(sym_wr[-5:])) if len(sym_wr) > 5 else [],
            'exit_stats': ex_stats,
            'feature_importance': self.ml_pipeline.get_feature_importance(),
            'ml_level': ml_level,
            'ml_level_name': {
                1: 'Rule Engine', 2: 'Logistic Regression',
                3: 'Random Forest', 4: 'Meta Model (LightGBM)',
            }.get(ml_level, 'Rule Engine'),
            'ml_auc': round(self.ml_pipeline.last_auc, 3),
            'ml_samples': self.ml_pipeline.n_samples_trained,
            'next_level_at': next_at,
            'ml_progress': progress,
            'active_adjustments': {
                'score_delta':  adj.min_score_delta,
                'vol_delta':    adj.entry_vol_mult_delta,
                'blacklist':    adj.symbol_blacklist,
                'whitelist':    adj.symbol_whitelist,
                'blocked':      adj.trading_blocked,
                'block_reason': adj.block_reason,
                'confidence':   round(adj.confidence, 2),
                'tp_adjustment': adj.tp_adjustment,
                'sl_adjustment': adj.sl_adjustment,
                'position_size_multiplier': adj.position_size_multiplier,
                'entry_quality_score': adj.entry_quality_score,
            },
        }

    # ── WEWNETRZNE ──

    def _refresh(self):
        trades = self.db.get_trades(limit=500)
        adj = self.rule_engine.analyze(trades)
        adj.ml_level = self.ml_pipeline.get_level(len(trades))
        adj.trade_count = len(trades)
        with self._lock:
            self._adjustment = adj

    def _trigger_retrain(self):
        def _go():
            count = self.db.count_trades()
            level = self.ml_pipeline.get_level(count)
            if level >= 2:
                trades = self.db.get_trades(limit=3000)
                self.ml_pipeline.train(trades, level)
            self._refresh()
        threading.Thread(target=_go, daemon=True, name=f"ml-{self.bot}").start()

    def _start_background(self):
        def _loop():
            time.sleep(15)
            while True:
                try:
                    self._refresh()
                except Exception:
                    pass
                time.sleep(300)
        threading.Thread(target=_loop, daemon=True, name=f"analytics-bg-{self.bot}").start()

    def _session(self, hour: int) -> str:
        if hour < 8:   return 'asia'
        if hour < 16:  return 'london'
        if hour < 22:  return 'new_york'
        return 'off'

    def _empty_adj(self) -> ParameterAdjustment:
        return ParameterAdjustment(
            bot=self.bot, min_score_delta=0, entry_vol_mult_delta=0.0,
            max_positions_override=None, symbol_blacklist=[], symbol_whitelist=[],
            trading_blocked=False, block_reason="", ml_level=1,
            trade_count=0, confidence=0.0,
        )

    def _empty_dashboard(self) -> dict:
        return {
            'bot': self.bot, 'trade_count': 0, 'win_rate': 0, 'total_pnl': 0,
            'hour_wr': {}, 'symbol_wr': [], 'worst_symbols': [], 'exit_stats': {},
            'feature_importance': {}, 'ml_level': 1,
            'ml_level_name': 'Rule Engine', 'ml_auc': 0, 'ml_samples': 0,
            'next_level_at': 200, 'ml_progress': 0,
            'active_adjustments': {
                'score_delta': 0, 'vol_delta': 0, 'blacklist': [],
                'whitelist': [], 'blocked': False, 'block_reason': '', 'confidence': 0,
                'tp_adjustment': 1.0, 'sl_adjustment': 1.0,
                'position_size_multiplier': 1.0, 'entry_quality_score': 0.0,
            },
        }


# ══════════════════════════════════════════════════════
#   SINGLETON PER BOT
# ══════════════════════════════════════════════════════

_instances: dict = {}
_instances_lock = threading.Lock()


def get_analytics(bot: str) -> AnalyticsEngine:
    """
    Zwraca osobna instancje AnalyticsEngine dla kazdego bota.
    Kazdy bot ma wlasna baze danych i modele ML.
    """
    with _instances_lock:
        if bot not in _instances:
            if bot not in BOT_PROFILES:
                raise ValueError(f"Nieznany bot: {bot}. Dozwolone: {list(BOT_PROFILES.keys())}")
            _instances[bot] = AnalyticsEngine(bot)
        return _instances[bot]


def get_all_analytics() -> dict:
    """Zwraca slownik wszystkich aktywnych instancji."""
    return dict(_instances)
