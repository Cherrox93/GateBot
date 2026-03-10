"""
analytics_engine.py â€” GateBot ML Analytics System v2
======================================================
KaÅ¼dy bot ma OSOBNÄ„ instancjÄ™ z wÅ‚asnÄ… bazÄ… danych i modelami ML.

Poziomy ML (per bot, niezaleÅ¼nie):
  Poziom 1 (0-199 transakcji):   Rule Engine
  Poziom 2 (200-499):            + Logistic Regression
  Poziom 3 (500-999):            + Random Forest
  Poziom 4 (1000+):              + Meta Model (LightGBM)

UÅ¼ycie:
    from analytics_engine import get_analytics
    analytics = get_analytics('scalper')   # osobna instancja
    analytics = get_analytics('lowcap')    # osobna instancja
    analytics = get_analytics('grid_bot')      # osobna instancja
"""

import sqlite3
import json
import time
import threading
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   PER-BOT KONFIGURACJA
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

BOT_PROFILES = {
    'scalper': {
        # Highcap: BTC/ETH/SOL â€” pÅ‚ynne, maÅ‚e spready, szybkie ruchy
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
    },
    'lowcap': {
        # Lowcap: maÅ‚e tokeny â€” duÅ¼e spready, silne ruchy grid, szybkie SL
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
    },
    'grid_bot': {
        # grid_bot: momentum â€” bardzo duÅ¼e spready, krÃ³tkie okna, high risk
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
    },
}

FEATURE_COLS = [
    'score', 'vol_ratio', 'body_ratio', 'atr_pct',
    'upper_wick_ratio', 'breakout_strength', 'ema9_slope',
    'ema21_slope', 'ema_spread', 'btc_trend', 'btc_atr_pct',
    'btc_vol_ratio', 'hour', 'day_of_week'
]


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   DATA CLASSES
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   DATABASE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   RULE ENGINE (Poziom 1)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

        # â”€â”€ 1. Seria strat z rzÄ™du â”€â”€
        recent_losses = sum(1 for t in trades[:5] if t['win'] == 0)
        if recent_losses >= p['consecutive_loss_block']:
            adj.trading_blocked = True
            adj.block_reason = (
                f"â›” ML [{self.bot}]: {recent_losses}/5 strat z rzÄ™du â€” pauza ochronna"
            )
            return adj

        # â”€â”€ 2. Godzinowy WR â”€â”€
        hour_stats = {}
        for t in trades:
            h = t['hour']
            if h not in hour_stats:
                hour_stats[h] = {'wins': 0, 'total': 0}
            hour_stats[h]['total'] += 1
            hour_stats[h]['wins'] += t['win']

        h_now = datetime.now().hour
        if h_now in hour_stats and hour_stats[h_now]['total'] >= min_s:
            wr = hour_stats[h_now]['wins'] / hour_stats[h_now]['total']
            if wr < p['bad_hour_wr']:
                adj.min_score_delta += p['score_penalty_bad_hour']
            elif wr > p['good_hour_wr']:
                adj.min_score_delta += p['score_boost_good_hour']

        # â”€â”€ 3. Symbol blacklist / whitelist â”€â”€
        sym_stats = {}
        for t in trades:
            s = t['symbol']
            if s not in sym_stats:
                sym_stats[s] = {'wins': 0, 'total': 0}
            sym_stats[s]['total'] += 1
            sym_stats[s]['wins'] += t['win']

        for sym, ss in sym_stats.items():
            if ss['total'] >= max(min_s, 8):
                wr = ss['wins'] / ss['total']
                if wr < p['blacklist_wr_threshold']:
                    adj.symbol_blacklist.append(sym)
                elif wr > p['whitelist_wr_threshold']:
                    adj.symbol_whitelist.append(sym)

        # â”€â”€ 4. BTC kontekst â”€â”€
        btc_down = [t for t in trades if t.get('btc_trend') == -1]
        if len(btc_down) >= min_s:
            wr = sum(t['win'] for t in btc_down) / len(btc_down)
            if wr < 0.35:
                adj.min_score_delta += p['score_penalty_btc_down']

        # â”€â”€ 5. Vol ratio â”€â”€
        high_vol = [t for t in trades if (t.get('vol_ratio') or 0) > p['vol_ratio_good']]
        if len(high_vol) >= min_s:
            wr = sum(t['win'] for t in high_vol) / len(high_vol)
            if wr < 0.40:
                adj.entry_vol_mult_delta += 0.5

        adj.confidence = min(0.5, len(trades) / 200)
        return adj


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   ML PIPELINE (Poziomy 2-4)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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
        self._use_raw = False  # True gdy LGB (nie wymaga skalowania)

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
            y = np.array([int(t['win']) for t in trades])

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
                        logger.warning(f"[ML:{self.bot}] LightGBM brak â€” RF fallback")
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
            logger.warning(f"[ML:{self.bot}] sklearn brak â€” pip install scikit-learn")
        except Exception as e:
            logger.error(f"[ML:{self.bot}] BÅ‚Ä…d trenowania: {e}")

    def predict(self, features: dict, level: int) -> tuple:
        if self.model is None or level < 2:
            return True, 0.5
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
            return win_prob >= threshold, win_prob

        except Exception as e:
            logger.error(f"[ML:{self.bot}] BÅ‚Ä…d predykcji: {e}")
            return True, 0.5

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
            return {col: round(float(imp[i]), 4) for i, col in enumerate(FEATURE_COLS)}
        except Exception:
            return {}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   ANALYTICS ENGINE â€” jedna instancja per bot
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

    # â”€â”€ ENTRY â”€â”€

    def on_entry(self, symbol: str, entry_price: float,
                 stake: float, sl_pct: float, tp1_pct: float,
                 features: dict) -> dict:
        now = datetime.now()
        count = self.db.count_trades()
        level = self.ml_pipeline.get_level(count)
        approved, confidence = self.ml_pipeline.predict(features, level)

        trade_id = f"{self.bot}_{symbol}_{int(time.time()*1000)}"
        with self._lock:
            self._pending[trade_id] = dict(
                bot=self.bot, symbol=symbol,
                timestamp=now.isoformat(),
                hour=now.hour, day_of_week=now.weekday(),
                session=self._session(now.hour),
                entry_price=entry_price, stake=stake,
                sl_pct=sl_pct, tp1_pct=tp1_pct,
                score=            float(features.get('score') or 0),
                vol_ratio=        float(features.get('vol_ratio') or 0),
                body_ratio=       float(features.get('body_ratio') or 0),
                atr_pct=          float(features.get('atr_pct') or 0),
                upper_wick_ratio= float(features.get('upper_wick_ratio') or 0),
                breakout_strength=float(features.get('breakout_strength') or 1.0),
                ema9_slope=       float(features.get('ema9_slope') or 0),
                ema21_slope=      float(features.get('ema21_slope') or 0),
                ema_spread=       float(features.get('ema_spread') or 0),
                btc_trend=        int(features.get('btc_trend') or 0),
                btc_atr_pct=      float(features.get('btc_atr_pct') or 0),
                btc_vol_ratio=    float(features.get('btc_vol_ratio') or 1.0),
                ml_level=level, ml_confidence=confidence,
                ml_approved=1 if approved else 0,
                _entry_time=time.monotonic(),
            )
        return dict(
            trade_id=trade_id, approved=approved,
            confidence=confidence, ml_level=level, trade_count=count,
        )

    # â”€â”€ EXIT â”€â”€

    def on_exit(self, trade_id: str, exit_reason: str, pnl: float, pnl_pct: float):
        with self._lock:
            p = self._pending.pop(trade_id, None)
        if not p:
            return
        hold = time.monotonic() - p.pop('_entry_time', time.monotonic())
        record = TradeRecord(
            **{k: p[k] for k in TradeRecord.__dataclass_fields__ if k in p},
            exit_reason=exit_reason, pnl=pnl, pnl_pct=pnl_pct,
            hold_seconds=hold, win=1 if pnl > 0 else 0,
        )
        self.db.insert_trade(record)
        self._retrain_counter += 1
        if self._retrain_counter >= 25:
            self._retrain_counter = 0
            self._trigger_retrain()

    # â”€â”€ INTERFEJS DLA SILNIKÃ“W â”€â”€

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

    # â”€â”€ DASHBOARD â”€â”€

    def get_dashboard_data(self) -> dict:
        trades = self.db.get_trades(limit=1000)
        count = len(trades)
        if count == 0:
            return self._empty_dashboard()

        wins = sum(t['win'] for t in trades)
        total_pnl = sum(t.get('pnl') or 0 for t in trades)

        # Godziny
        h_stats = {}
        for t in trades:
            h = t['hour']
            h_stats.setdefault(h, {'wins': 0, 'total': 0})
            h_stats[h]['total'] += 1
            h_stats[h]['wins'] += t['win']
        hour_wr = {
            h: {'wr': round(v['wins']/v['total']*100, 1), 'total': v['total']}
            for h, v in h_stats.items() if v['total'] >= 3
        }

        # Symbole
        s_stats = {}
        for t in trades:
            s = t['symbol']
            s_stats.setdefault(s, {'wins': 0, 'total': 0, 'pnl': 0})
            s_stats[s]['total'] += 1
            s_stats[s]['wins'] += t['win']
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
            ex_stats[er]['wins'] += t['win']
            ex_stats[er]['pnl'] += (t.get('pnl') or 0)

        ml_level = self.ml_pipeline.get_level(count)
        next_at = {1: 200, 2: 500, 3: 1000, 4: None}.get(ml_level)
        base_at = {1: 0, 2: 200, 3: 500, 4: 1000}.get(ml_level, 0)
        progress = min(100, int((count - base_at) / (next_at - base_at) * 100)) if next_at else 100

        adj = self._adjustment
        return {
            'bot': self.bot,
            'trade_count': count,
            'win_rate': round(wins / count * 100, 1),
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
            },
        }

    # â”€â”€ WEWNÄ˜TRZNE â”€â”€

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
            },
        }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   SINGLETON PER BOT
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_instances: dict = {}
_instances_lock = threading.Lock()


def get_analytics(bot: str) -> AnalyticsEngine:
    """
    Zwraca osobnÄ… instancjÄ™ AnalyticsEngine dla kaÅ¼dego bota.
    KaÅ¼dy bot ma wÅ‚asnÄ… bazÄ™ danych i modele ML.

    UÅ¼ycie:
        analytics = get_analytics('scalper')
        analytics = get_analytics('lowcap')
        analytics = get_analytics('grid_bot')
    """
    with _instances_lock:
        if bot not in _instances:
            if bot not in BOT_PROFILES:
                raise ValueError(f"Nieznany bot: {bot}. Dozwolone: {list(BOT_PROFILES.keys())}")
            _instances[bot] = AnalyticsEngine(bot)
        return _instances[bot]


def get_all_analytics() -> dict:
    """Zwraca sÅ‚ownik wszystkich aktywnych instancji."""
    return dict(_instances)
