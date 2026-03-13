from dataclasses import dataclass, field

# Global trading safety mode
TRADING_MODE: str = "paper"

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
#   STATUS CONFIG â€“ wspÃ³lne dla wszystkich botÃ³w
# ============================================================

@dataclass
class StatusConfig:
    show_status_line: bool = True
    default_status_text: str = "Skanuje rynek..."
    status_prefix: str = "STATUS_UPDATE::"


# ============================================================
#   SCALPER
#   Trading params → managed via web app (settings/scalper_settings.json)
#   Infrastructure params → defaults below
# ============================================================

@dataclass
class ScalperConfig:
    status: StatusConfig = field(default_factory=StatusConfig)
    start_balance: float = 200.0

    # ── Trading params (real defaults in main_web.py, set on startup) ──
    slot_count: int = 0
    max_stake_usd: float = 0.0
    maker_fee: float = 0.001
    taker_fee: float = 0.001
    target_profit_pct: float = 0.0
    stop_loss_pct: float = 0.0
    trailing_stop_pct: float = 0.0
    runner_trail_pct: float = 0.0010
    max_trades_day: int = 0
    daily_loss_limit_pct: float = 0.0
    min_signal_strength: float = 0.0
    symbol_cooldown_sec: float = 0.0
    momentum_min_change: float = 0.00012
    volume_spike_mult: float = 0.0
    atr_filter_min: float = 0.0
    pullback_min_retrace: float = 0.0
    pullback_max_retrace: float = 0.0
    impulse_ttl_sec: float = 0.0
    momentum_window_sec: float = 0.0
    volume_baseline_window_sec: float = 0.0
    trend_ema_period: int = 0
    trend_window_sec: float = 0.0

    # ── Reinvest ──
    reinvest_enabled: bool = False
    reinvest_max_stake: float = 0.10
    stake_max_cap_usdt: float = 100.0
    base_stake_usdt: float = 50.0

    # ── Infrastructure (not exposed in web) ──
    max_position_size_usdt: float = 10.0
    max_open_positions: int = 3
    pair_refresh_sec: float = 30.0
    ws_reconnect_max: int = 5
    ws_reconnect_base: float = 1.0
    exchange_timeout_sec: float = 30.0
    monitor_poll_sec: float = 0.05
    missed_retry_cooldown_sec: float = 2.0
    sor_maker_wait_ms: int = 700
    sor_aggressive_wait_ms: int = 500
    max_pending_orders: int = 32
    max_trade_rate_per_sec: float = 12.0
    stale_max_age_ms: float = 500.0
    symbol_loop_sleep_sec: float = 0.1

    # Symbols
    gigants: tuple = (
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
        "XRP/USDT",
        "DOGE/USDT",
    )


# ============================================================
#   LOWCAP CONFIG
# ============================================================

@dataclass
class LowcapConfig:
    status: StatusConfig = field(default_factory=lambda: StatusConfig(
        default_status_text="Micro-Reversion gotowy"
    ))

    start_balance: float = 50.0

    # Capital / concurrency
    position_size: float = 5.0           # USDT per entry
    max_pairs: int = 4                   # active symbols (3-4 recommended)
    max_orders_per_pair: int = 2         # avoid capital lock per symbol

    # Aliases required by main_web.py settings system (apply_settings_to_engines)
    max_stake_per_trade: float = 5.0     # mirrors position_size
    max_positions: int = 4               # mirrors max_pairs

    # Pair selection
    vol_min: float = 200_000
    vol_max: float = 20_000_000
    spread_min_pct: float = 0.0002
    spread_max_pct: float = 0.0150
    min_daily_change_pct: float = 0.01  # prefer pairs with at least 1% daily move
    max_scan_pairs: int = 20
    price_min: float = 0.000001
    blacklist: list = field(default_factory=lambda: [
        "3L", "3S", "5L", "5S", "UP", "DOWN", "BEAR", "BULL",
    ])

    # Micro-Reversion strategy
    buy_drop_min_pct: float = 0.01      # buy on 1%-3% pullback from local high
    buy_drop_max_pct: float = 0.03
    sell_rise_min_pct: float = 0.01     # sell on 1%-3% rebound from local low
    sell_rise_max_pct: float = 0.03
    trailing_enabled: bool = True
    trailing_pct: float = 0.007         # 0.5%-1% trail
    stop_loss_enabled: bool = True
    stop_loss_pct: float = 0.07         # 5%-10% optional stop loss
    entry_cooldown_s: float = 5.0
    maker_fee: float = 0.001

    # Daily circuit breaker
    daily_loss_limit_usdt: float = 3.0

    # Engine timing
    rescan_interval_s: int = 300
    loop_sleep: float = 0.5
    order_refresh_ms: int = 500

    db_path: str = "trades.db"


# ============================================================
#   GRID BOT CONFIG
# ============================================================

@dataclass
class GridBotConfig:
    status: StatusConfig = field(default_factory=StatusConfig)

    start_balance: float = 50.0

    # Symbol selection
    symbol_filter: str = "/USDT"
    vol_min: float = 100_000           # min 24h USDT volume
    vol_max: float = 20_000_000        # max 24h USDT volume
    max_symbols_to_scan: int = 30

    # Grid parameters
    grid_levels: int = 5               # buy levels below mid-price
    grid_spacing_pct: float = 0.005    # 0.5% fallback spacing (if ATR unavailable)
    atr_period: int = 14
    atr_timeframe: str = "1h"
    atr_spacing_mult: float = 0.3      # level spacing = 0.3 * ATR
    atr_range_mult: float = 2.0        # total grid range = 2.0 * ATR below mid

    # Position sizing
    position_size_usdt: float = 10.0   # USDT budget per grid level
    max_stake_per_trade: float = 10.0  # alias required by main_web.py
    max_positions: int = 5             # max concurrent filled levels (orders per pair)
    max_active_symbols: int = 1        # how many symbols to trade simultaneously

    # Exit
    sell_above_buy_pct: float = 0.006  # sell at buy * (1 + 2*this + maker_fee)
    maker_fee: float = 0.001
    taker_fee: float = 0.001

    # Grid management
    rebalance_interval_s: float = 1800.0   # rebuild grid every 30 min
    grid_reset_pct: float = 0.03           # rebuild if price drifts 3% from center

    # Daily loss limit
    daily_loss_limit_pct: float = 0.10

    # Timing
    loop_sleep: float = 2.0
    order_check_interval_s: float = 5.0

    db_path: str = "trades.db"

    blacklist: tuple = (
        # Leveraged / inverse tokens
        '3L/', '3S/', '5L/', '5S/', '2L/', '2S/',
        # Fiat pairs
        'EUR/', 'GBP/', 'USDT/USDT', 'ON/USDT',
        # Stablecoins (USD-pegged)
        'USDC/', 'BUSD/', 'DAI/', 'TUSD/', 'FDUSD/', 'USDD/',
        'FRAX/', 'USDP/', 'GUSD/', 'PYUSD/', 'LUSD/', 'USDE/',
        'CRVUSD/', 'GHO/', 'SUSD/', 'USDJ/', 'USTC/', 'MIM/',
        'DOLA/', 'ALUSD/', 'FEI/', 'TRIBE/', 'RSR/', 'CUSD/',
        'UST/', 'USDX/', 'USDK/', 'HUSD/', 'USDN/', 'USDQ/',
        'ZUSD/', 'OUSD/', 'EUSD/', 'USD0/',
        # EUR / other fiat stables
        'EURS/', 'EURT/', 'AGEUR/', 'EUROC/', 'STEUR/',
        # Gold-backed stables
        'XAUT/', 'PAXG/',
    )


# ============================================================
#   INSTANCJA EXCHANGE (dla wszystkich botÃ³w)
# ============================================================

EXCHANGE = ExchangeConfig(
    api_key="WSTAW_API_KEY",
    secret_key="WSTAW_SECRET_KEY"
)


