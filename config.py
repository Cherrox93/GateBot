from dataclasses import dataclass, field

# ============================================================
#   EXCHANGE CONFIG
# ============================================================

@dataclass
class ExchangeConfig:
    api_key: str
    secret_key: str


# ============================================================
#   TELEGRAM CONFIG
# ============================================================

@dataclass
class TelegramConfig:
    token: str = ""
    chat_id: str = ""


# ============================================================
#   STATUS CONFIG – wspólne dla wszystkich botów
# ============================================================

@dataclass
class StatusConfig:
    show_status_line: bool = True
    default_status_text: str = "🔍 Skanuję rynek…"
    status_prefix: str = "STATUS_UPDATE::"


# ============================================================
#   SCALPER (Highcap)
# ============================================================

@dataclass
class ScalperConfig:
    status: StatusConfig = field(default_factory=StatusConfig)

    start_balance: float = 50.0
    fee: float = 0.002

    # Position management
    slot_count: int = 3
    first_stake_pct: float = 0.20
    max_stake_usd: float = 50.0          # górny limit na jedną pozycję
    max_total_exposure: float = 0.70     # max 70% kapitału w otwartych pozycjach

    # EMA parameters
    ema_fast: int = 9
    ema_slow: int = 21
    ema_macro: int = 50

    # Entry scoring
    min_score: int = 55
    min_body_ratio: float = 0.45
    max_upper_wick_ratio: float = 0.30
    min_atr_pct: float = 0.002

    # Risk management (ATR-based)
    risk_per_trade_pct: float = 0.005    # 0.5% equity risked per trade
    atr_sl_multiplier: float = 1.5       # SL = entry - 1.5 * ATR
    atr_trail_multiplier: float = 1.5    # trailing stop distance = 1.5 * ATR
    tp1_r_multiple: float = 2.0          # TP1 = entry + 2.0 * SL_distance
    runner_activation_r: float = 2.5     # runner trail activates at +2.5R
    tp1_qty_pct: float = 0.60            # sell 60% at TP1, run 40%

    # Regime / alt breadth
    btc_breakout_vol_mult: float = 1.5   # BTC vol spike counts as green light
    btc_regime_cache_ttl: float = 60.0   # seconds to cache BTC regime result
    alt_breadth_min_pct: float = 0.60    # min fraction of alts in uptrend
    alt_breadth_cache_ttl: float = 300.0 # seconds to cache alt breadth result

    # Entry filters
    entry_body_ratio: float = 0.45       # min body/range ratio for entry candle
    entry_vol_mult: float = 1.8          # volume must be > 1.8x rolling mean
    entry_breakout_factor: float = 1.001 # close must be > prev_high * 1.001

    # Circuit breaker (rolling WR)
    rolling_wr_window: int = 10          # look at last N closed trades
    rolling_wr_min: float = 0.20         # pause if WR drops below 20%
    rolling_wr_pause: float = 3600.0     # 1h pause when WR circuit fires

    # Per-scan throttle
    max_entries_per_scan: int = 2        # max new entries per main loop iteration

    # Timing
    cooldown_seconds: float = 60.0
    ohlcv_cache_ttl: float = 15.0    # odświeżaj OHLCV co 15 sekund

    # Daily loss limit
    daily_loss_limit_pct: float = 0.05  # stop gdy dzienna strata > 5% start_balance

    # 30 coinów do skalpowania
    gigants: tuple = (
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
        "XRP/USDT",
        "DOGE/USDT",
        "ADA/USDT",
        "AVAX/USDT",
        "LINK/USDT",
        "MATIC/USDT",
        "LTC/USDT",
        "DOT/USDT",
        "NEAR/USDT",
        "UNI/USDT",
        "ATOM/USDT",
        "FIL/USDT",
        "ICP/USDT",
        "APT/USDT",
        "ARB/USDT",
        "OP/USDT",
        "INJ/USDT",
        "SUI/USDT",
        "TIA/USDT",
        "SEI/USDT",
        "WLD/USDT",
        "PEPE/USDT",
        "SHIB/USDT",
        "FLOKI/USDT",
        "BONK/USDT",
        "WIF/USDT",
    )


# ============================================================
#   LOWCAP CONFIG
# ============================================================

@dataclass
class LowcapConfig:
    status: StatusConfig = field(default_factory=StatusConfig)

    start_balance: float = 50.0
    fee: float = 0.002
    min_stake_usd: float = 1.0
    max_stake_per_trade: float = 50.0
    stake_pct: float = 0.10

    vol_min: int = 600_000
    vol_max: int = 5_000_000
    price_min: float = 0.001

    max_spread: float = 0.004
    min_orderbook_liquidity: float = 400.0
    min_hold_seconds: float = 15.0
    hard_sl_pct: float = 0.011          # fixed -1.1% SL instead of broken ATR-based
    cooldown_after_sl: float = 180.0

    m1_ema_fast: int = 5
    m1_ema_slow: int = 13

    m5_ema_fast: int = 20
    m5_ema_slow: int = 50

    atr_length: int = 5
    min_atr_pct: float = 0.003

    min_body_ratio: float = 0.55        # 0.45 → 0.55
    max_upper_wick_ratio: float = 0.30

    min_body_ratio_score: float = 0.30
    max_upper_wick_ratio_score: float = 0.40

    min_vol_ratio: float = 2.5          # 1.8 → 2.5
    min_candle_notional: float = 500.0  # 200 → 500
    vol_burst_multiplier: float = 1.8

    break_high_factor: float = 0.9985
    break_close_factor: float = 0.996

    target_profit: float = 0.018        # 1.5% → 1.8%
    micro_tp: float = 0.010             # 0.8% → 1.0%
    trailing_stop: float = 0.005        # 0.8% → 0.5%
    break_even: float = 0.006           # 0.35% → 0.6%

    min_reentry_price: float = 0.005
    reentry_breakout: float = 1.003
    reentry_min_vol_ratio: float = 2.0
    reentry_stake_pct: float = 0.07

    dyn_threshold_base: int = 46        # 40 → 46
    dyn_threshold_mid: int = 50         # 44 → 50
    dyn_threshold_high: int = 48
    dyn_threshold_top: int = 52
    dyn_wr_mid: float = 0.58
    dyn_wr_high: float = 0.63
    dyn_wr_top: float = 0.68

    scan_limit: int = 20
    loop_sleep: float = 0.10
    cooldown_seconds: float = 60.0      # re-enter faster after wins

    ai_db_file: str = "ai_trades_lowcap.json"
    min_ai_samples: int = 100
    ai_boost: int = 12

    max_positions: int = 5
    score_threshold_boost: int = 12

    # Circuit breaker
    max_consecutive_losses: int = 3
    consecutive_loss_pause: float = 5400.0   # 90 minutes pause after 3 SL in a row
    sl_reentry_block_seconds: float = 3600.0

    # Entry filters
    max_spread_pct: float = 0.0025          # max 0.25% spread at entry
    candle_body_ratio: float = 0.60         # minimum body/range ratio
    max_atr_pct: float = 0.025              # reject if ATR > 2.5% (dump&pump)
    btc_min_atr_pct: float = 0.003          # BTC M5 ATR must be > 0.3% (market alive)
    vol_rolling_multiplier: float = 2.0     # volume must be > 2x rolling mean

    # Blacklista stablecoinów i tokenów dźwigniowych
    blacklist: tuple = (
        "DAI/", "TUSD/", "FRAX/", "USDC/", "USDD/", "FDUSD/",
        "BUSD/", "USDP/", "GUSD/", "LUSD/", "EUR/", "GBP/",
        "3L/", "3S/", "5L/", "5S/", "2L/", "2S/",
    )


# ============================================================
#   PUMP CONFIG
# ============================================================

@dataclass
class PumpConfig:
    status: StatusConfig = field(default_factory=StatusConfig)

    symbol_filter: str = "/USDT"
    max_symbols: int = 100
    vol_min: int = 10_000
    vol_max: int = 5_000_000

    # Timing
    window_seconds: float = 4.0
    min_ticks_in_window: int = 6
    scanner_interval: float = 1.0
    long_history_limit: int = 62       # fetch 60min of 1m candles for slow pump baseline
    slow_pump_min_change: float = 0.10 # minimum +15% in 60min to trigger slow pump
    slow_pump_min_vol_spike: float = 5.0  # minimum 5x volume spike for slow pump
    slow_pump_score_threshold: int = 10   # minimum score to enter on slow pump signal

    # Entry
    entry_score_threshold: int = 15
    max_positions: int = 2
    max_stake: float = 10.0
    min_stake_usd: float = 5.0

    # Exit — Moon Hunter: daj moonie żyć
    hard_sl: float = -0.07
    max_sl_pct: float = 0.035            # twardy cap SL: max 3.5% od entry
    base_trailing: float = -0.020
    trailing_min_profit: float = 0.03
    trailing_mid_profit: float = 0.08
    trailing_mid_drop: float = -0.05
    trailing_high_profit: float = 0.20
    trailing_high_drop: float = -0.10
    time_exit_seconds: float = 360.0

    # Dynamiczny trailing stop — progi zysku
    trail_tier1_pct: float = 0.05         # do 5% zysku
    trail_tier2_pct: float = 0.15         # do 15% zysku
    trail_tier3_pct: float = 0.40         # do 40% zysku

    # Trailing distance per tier
    trail_distance_tier1: float = 0.015   # 1.5%
    trail_distance_tier2: float = 0.030   # 3.0%
    trail_distance_tier3: float = 0.050   # 5.0%
    trail_distance_tier4: float = 0.080   # 8.0% dla 40%+

    # Minimalna odległość od high żeby wyjść
    hard_exit_from_high: float = 0.15     # 15% od szczytu

    quick_exit_seconds: float = 120.0      # check for failed pump after 2 min
    quick_exit_threshold: float = -0.005   # exit if profit < -0.5% after 2 min
    quick_exit_min_loss: float = 0.015     # min 1.5% straty żeby QUICK exit zadziałał
    max_entries_per_coin_per_day: int = 2  # max 2 entries per coin per session
    cooldown_after_sl: float = 1800.0      # 30 min cooldown after SL on that coin
    sl_reentry_block_seconds: float = 7200.0  # 2h blokada re-entry po SL

    fee_rate: float = 0.002
    start_balance: float = 50.0
    db_path: str = "trades.db"

    cooldown_seconds: float = 900.0

    # Dzienny limit strat
    daily_loss_limit_pct: float = 0.10

    blacklist: tuple = (
        '3L/', '3S/', '5L/', '5S/', '2L/', '2S/',
        'DAI/', 'USDC/', 'USDD/', 'TUSD/', 'FRAX/', 'FDUSD/', 'BUSD/',
        'EUR/', 'GBP/',
        'USDT/USDT', 'ON/USDT'
    )


# ============================================================
#   INSTANCJA EXCHANGE (dla wszystkich botów)
# ============================================================

EXCHANGE = ExchangeConfig(
    api_key="WSTAW_API_KEY",
    secret_key="WSTAW_SECRET_KEY"
)
