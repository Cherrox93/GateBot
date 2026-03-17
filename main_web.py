"""
main_web.py â€” GateBot Web Server
FastAPI backend z WebSocket, autoryzacjÄ… JWT i peÅ‚nym API.
Uruchomienie: python main_web.py
DostÄ™p: http://localhost:8000 (lokalnie) lub http://IP_VPS:8000 (VPS)
"""

import sys
import asyncio
import threading
import time
import json
import os
import sqlite3
from datetime import datetime, timedelta
from collections import deque
from typing import Optional

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import jwt
import uvicorn

from config import ScalperConfig, LowcapConfig, GridBotConfig
from engines.scalper_engine import ScalperEngine
from engines.lowcap_engine import LowcapEngine
from engines.grid_bot_engine import GridBotEngine
from market_data import MarketData
from telegram_notifier import TelegramNotifier
from vault import TELEGRAM_SCALPER, TELEGRAM_LOWCAP, TELEGRAM_GRID_BOT
from analytics_engine import get_analytics
from wallet import SharedWallet

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   KONFIGURACJA
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "gatebot2024")
JWT_SECRET   = os.environ.get("JWT_SECRET", "gBt_s3cr3t_k3y_2026!xQ9")
JWT_EXPIRE_H = 12  # token wazny 12 godzin

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    init_settings_db()
    init_scalper_settings_file()
    init_lowcap_settings_file()
    init_grid_bot_settings_file()
    init_engines()
    if market_data and not market_data.running:
        await market_data.start()
    apply_settings_to_engines(load_all_settings())
    print(f"[GateBot] Dashboard dostepny na http://0.0.0.0:8000", flush=True)
    print(f"[GateBot] Login: {WEB_USERNAME} / {'*' * len(WEB_PASSWORD)}", flush=True)
    yield
    print("[GateBot] Zatrzymywanie botow...", flush=True)
    if scalper_state.running and scalper_engine:
        try:
            await scalper_engine.stop()
        except Exception:
            pass
    if lowcap_state.running and lowcap_engine:
        try:
            lowcap_engine.stop()
        except Exception:
            pass
    if grid_bot_state.running and grid_bot_engine:
        try:
            await grid_bot_engine.stop()
        except Exception:
            pass
    if market_data and market_data.running:
        try:
            await market_data.stop()
        except Exception:
            pass
    print("[GateBot] Zamknieto.", flush=True)

app = FastAPI(title="GateBot Dashboard", lifespan=lifespan)
security = HTTPBearer()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   JWT AUTH
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
class LoginRequest(BaseModel):
    username: str
    password: str

def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_H)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        return payload["sub"]
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nieprawidlowy token")

@app.post("/api/login")
def login(req: LoginRequest):
    if req.username != WEB_USERNAME or req.password != WEB_PASSWORD:
        raise HTTPException(status_code=401, detail="Nieprawidlowe dane logowania")
    token = create_token(req.username)
    return {"token": token}

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   STATE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
class BotState:
    def __init__(self, name: str):
        self.name = name
        self.running = False
        self.status = "Oczekiwanie..."
        self.logs: deque = deque(maxlen=100)
        self.pnl_history: list = []   # [{"ts": unix, "pnl": float}]
        self.trades: list = []        # historia transakcji
        self.wins = 0
        self.losses = 0
        self.realized_profit = 0.0
        self.open_positions: dict = {}

scalper_state = BotState("scalper")
lowcap_state  = BotState("lowcap")
grid_bot_state    = BotState("grid_bot")

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   SHARED PAPER WALLET
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
wallet = SharedWallet(initial_usdt=200.0)

def get_total_exposure(engines: list) -> float:
    total = 0.0
    for engine in engines:
        for p in getattr(engine, "active_positions", {}).values():
            total += p.get("original_stake", p.get("stake", 0.0))
    return total

def get_total_equity(engines: list) -> float:
    if scalper_engine:
        base = scalper_engine.balance_usdt
    else:
        base = wallet.get_total("USDT")
    return base + get_total_exposure(engines)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   SETTINGS DB
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
SETTINGS_DB = "settings.db"
SCALPER_SETTINGS_FILE = "settings/scalper_settings.json"
LOWCAP_SETTINGS_FILE = "settings/lowcap_settings.json"
GRID_BOT_SETTINGS_FILE = "settings/grid_bot_settings.json"

SETTINGS_DEFAULTS = {
    "scalper": {
        "stake_usd": 25.0, "max_slots": 2,
        "target_profit_pct": 0.0035, "stop_loss_pct": 0.0020, "trailing_stop_pct": 0.0015,
        "maker_fee": 0.001, "taker_fee": 0.001, "runner_trail_pct": 0.0010,
        "reinvest_enabled": False, "reinvest_max_stake": 0.10,
        "stake_max_cap_usdt": 100.0, "base_stake_usdt": 50.0,
        "max_trades_day": 400, "daily_loss_limit_pct": 0.05,
        "min_signal_strength": 0.35,
        "symbol_cooldown_sec": 0.5, "momentum_min_change": 0.002,
        "volume_spike_mult": 1.8, "atr_filter_min": 0.0025,
        "pullback_min_retrace": 0.25, "pullback_max_retrace": 0.40,
        "impulse_ttl_sec": 20.0, "momentum_window_sec": 5.0,
        "volume_baseline_window_sec": 30.0, "trend_ema_period": 20, "trend_window_sec": 60.0,
    },
    "lowcap":  {"stake_usd": 5.0,  "max_slots": 3},
    "grid_bot":    {"stake_usd": 5.0,  "max_slots": 5, "max_active_symbols": 1},
}

def init_settings_db():
    conn = sqlite3.connect(SETTINGS_DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            bot_name TEXT PRIMARY KEY,
            stake_usd REAL NOT NULL DEFAULT 10.0,
            max_slots INTEGER NOT NULL DEFAULT 3
        )
    """)
    for bot, defaults in SETTINGS_DEFAULTS.items():
        c.execute(
            "INSERT OR IGNORE INTO bot_settings (bot_name, stake_usd, max_slots) VALUES (?, ?, ?)",
            (bot, defaults["stake_usd"], defaults["max_slots"])
        )
    conn.commit()
    conn.close()

def _scalper_defaults_dict() -> dict:
    d = SETTINGS_DEFAULTS["scalper"]
    def _cast(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return int(v)
        return float(v)
    return {k: _cast(v) for k, v in d.items()}

def init_scalper_settings_file():
    if os.path.exists(SCALPER_SETTINGS_FILE):
        return
    save_scalper_settings_file(_scalper_defaults_dict())


def _lowcap_defaults_dict() -> dict:
    d = LowcapConfig()
    return {
        "stake_usd": float(SETTINGS_DEFAULTS["lowcap"]["stake_usd"]),
        "max_slots": int(SETTINGS_DEFAULTS["lowcap"]["max_slots"]),
        "buy_drop_min_pct": float(d.buy_drop_min_pct),
        "buy_drop_max_pct": float(d.buy_drop_max_pct),
        "sell_rise_min_pct": float(d.sell_rise_min_pct),
        "sell_rise_max_pct": float(d.sell_rise_max_pct),
        "trailing_enabled": bool(d.trailing_enabled),
        "trailing_pct": float(d.trailing_pct),
        "stop_loss_enabled": bool(d.stop_loss_enabled),
        "stop_loss_pct": float(d.stop_loss_pct),
        "max_orders_per_pair": int(d.max_orders_per_pair),
    }


def init_lowcap_settings_file():
    if os.path.exists(LOWCAP_SETTINGS_FILE):
        return
    save_lowcap_settings_file(_lowcap_defaults_dict())


def load_lowcap_settings_file() -> dict:
    defaults = _lowcap_defaults_dict()
    if not os.path.exists(LOWCAP_SETTINGS_FILE):
        return defaults
    try:
        with open(LOWCAP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:
        return defaults
    for k, v in raw.items():
        if k in defaults:
            defaults[k] = v
    try:
        defaults["stake_usd"] = float(defaults["stake_usd"])
        defaults["max_slots"] = int(defaults["max_slots"])
        defaults["buy_drop_min_pct"] = float(defaults["buy_drop_min_pct"])
        defaults["buy_drop_max_pct"] = float(defaults["buy_drop_max_pct"])
        defaults["sell_rise_min_pct"] = float(defaults["sell_rise_min_pct"])
        defaults["sell_rise_max_pct"] = float(defaults["sell_rise_max_pct"])
        defaults["trailing_enabled"] = bool(defaults["trailing_enabled"])
        defaults["trailing_pct"] = float(defaults["trailing_pct"])
        defaults["stop_loss_enabled"] = bool(defaults["stop_loss_enabled"])
        defaults["stop_loss_pct"] = float(defaults["stop_loss_pct"])
        defaults["max_orders_per_pair"] = int(defaults["max_orders_per_pair"])
    except Exception:
        return _lowcap_defaults_dict()
    return defaults


def save_lowcap_settings_file(values: dict):
    defaults = _lowcap_defaults_dict()
    defaults.update(values or {})
    with open(LOWCAP_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(defaults, f, ensure_ascii=False, indent=2)

def load_scalper_settings_file() -> dict:
    if not os.path.exists(SCALPER_SETTINGS_FILE):
        return _scalper_defaults_dict()
    try:
        with open(SCALPER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:
        return _scalper_defaults_dict()
    defaults = _scalper_defaults_dict()
    for k, v in raw.items():
        if k in defaults:
            defaults[k] = v
    try:
        _int_keys = {"max_slots", "max_trades_day", "trend_ema_period"}
        _bool_keys = {"reinvest_enabled"}
        for k in defaults:
            if k in _bool_keys:
                defaults[k] = bool(defaults[k])
            elif k in _int_keys:
                defaults[k] = int(defaults[k])
            else:
                defaults[k] = float(defaults[k])
    except Exception:
        return _scalper_defaults_dict()
    return defaults

def save_scalper_settings_file(values: dict):
    defaults = _scalper_defaults_dict()
    defaults.update(values or {})
    with open(SCALPER_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(defaults, f, ensure_ascii=False, indent=2)

def _grid_bot_defaults_dict() -> dict:
    defaults = SETTINGS_DEFAULTS["grid_bot"]
    return {
        "stake_usd": float(defaults["stake_usd"]),
        "max_slots": int(defaults["max_slots"]),
        "max_active_symbols": int(defaults["max_active_symbols"]),
    }

def init_grid_bot_settings_file():
    if os.path.exists(GRID_BOT_SETTINGS_FILE):
        return
    save_grid_bot_settings_file(_grid_bot_defaults_dict())

def load_grid_bot_settings_file() -> dict:
    if not os.path.exists(GRID_BOT_SETTINGS_FILE):
        return _grid_bot_defaults_dict()
    try:
        with open(GRID_BOT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:
        return _grid_bot_defaults_dict()
    defaults = _grid_bot_defaults_dict()
    for k, v in raw.items():
        if k in defaults:
            defaults[k] = v
    try:
        defaults["stake_usd"] = float(defaults["stake_usd"])
        defaults["max_slots"] = int(defaults["max_slots"])
        defaults["max_active_symbols"] = int(defaults["max_active_symbols"])
    except Exception:
        return _grid_bot_defaults_dict()
    return defaults

def save_grid_bot_settings_file(values: dict):
    defaults = _grid_bot_defaults_dict()
    defaults.update(values or {})
    with open(GRID_BOT_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(defaults, f, ensure_ascii=False, indent=2)

def load_settings_db() -> dict:
    conn = sqlite3.connect(SETTINGS_DB)
    c = conn.cursor()
    c.execute("SELECT bot_name, stake_usd, max_slots FROM bot_settings")
    rows = c.fetchall()
    conn.close()
    return {row[0]: {"stake_usd": row[1], "max_slots": row[2]} for row in rows}

def load_all_settings() -> dict:
    settings = load_settings_db()
    scalper_json = load_scalper_settings_file()
    settings["scalper"] = dict(scalper_json)
    lowcap_json = load_lowcap_settings_file()
    settings["lowcap"] = {
        "stake_usd": float(lowcap_json["stake_usd"]),
        "max_slots": int(lowcap_json["max_slots"]),
        "buy_drop_min_pct": float(lowcap_json["buy_drop_min_pct"]),
        "buy_drop_max_pct": float(lowcap_json["buy_drop_max_pct"]),
        "sell_rise_min_pct": float(lowcap_json["sell_rise_min_pct"]),
        "sell_rise_max_pct": float(lowcap_json["sell_rise_max_pct"]),
        "trailing_enabled": bool(lowcap_json["trailing_enabled"]),
        "trailing_pct": float(lowcap_json["trailing_pct"]),
        "stop_loss_enabled": bool(lowcap_json["stop_loss_enabled"]),
        "stop_loss_pct": float(lowcap_json["stop_loss_pct"]),
        "max_orders_per_pair": int(lowcap_json["max_orders_per_pair"]),
    }
    grid_json = load_grid_bot_settings_file()
    settings["grid_bot"] = {
        "stake_usd": float(grid_json["stake_usd"]),
        "max_slots": int(grid_json["max_slots"]),
        "max_active_symbols": int(grid_json["max_active_symbols"]),
    }
    return settings

def save_setting_db(bot: str, stake_usd: float, max_slots: int):
    conn = sqlite3.connect(SETTINGS_DB)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO bot_settings (bot_name, stake_usd, max_slots) VALUES (?, ?, ?)",
        (bot, stake_usd, max_slots)
    )
    conn.commit()
    conn.close()

def apply_settings_to_engines(settings: dict):
    if scalper_engine and "scalper" in settings:
        s = settings["scalper"]
        # Capital & slots
        scalper_engine.cfg.max_stake_usd        = float(s.get("stake_usd", scalper_engine.cfg.max_stake_usd))
        scalper_engine.cfg.slot_count            = int(s.get("max_slots", scalper_engine.cfg.slot_count))
        scalper_engine.cfg.max_position_size_usdt = scalper_engine.cfg.max_stake_usd
        scalper_engine.cfg.max_open_positions     = scalper_engine.cfg.slot_count
        # Risk
        _float_keys = [
            "target_profit_pct", "stop_loss_pct", "trailing_stop_pct", "maker_fee",
            "taker_fee", "runner_trail_pct",
            "daily_loss_limit_pct", "min_signal_strength",
            "symbol_cooldown_sec", "momentum_min_change", "volume_spike_mult",
            "atr_filter_min", "pullback_min_retrace", "pullback_max_retrace",
            "impulse_ttl_sec", "momentum_window_sec", "volume_baseline_window_sec",
            "trend_window_sec",
            "reinvest_max_stake", "stake_max_cap_usdt", "base_stake_usdt",
        ]
        for key in _float_keys:
            if key in s:
                setattr(scalper_engine.cfg, key, float(s[key]))
        _int_keys = ["max_trades_day", "trend_ema_period"]
        for key in _int_keys:
            if key in s:
                setattr(scalper_engine.cfg, key, int(s[key]))
        _bool_keys = ["reinvest_enabled"]
        for key in _bool_keys:
            if key in s:
                setattr(scalper_engine.cfg, key, bool(s[key]))
        scalper_engine.apply_cfg_globals()
        print(
            f"[WebConfig] scalper settings applied: stake={scalper_engine.cfg.max_stake_usd}"
            f" slots={scalper_engine.cfg.slot_count}"
            f" TP={scalper_engine.cfg.target_profit_pct}"
            f" SL={scalper_engine.cfg.stop_loss_pct}"
            f" Trail={scalper_engine.cfg.trailing_stop_pct}"
            f" Runner={scalper_engine.cfg.runner_trail_pct}"
            f" Strength={scalper_engine.cfg.min_signal_strength}",
            flush=True,
        )
    if lowcap_engine and "lowcap" in settings:
        s = settings["lowcap"]
        lowcap_engine.cfg.position_size        = s["stake_usd"]
        lowcap_engine.cfg.max_stake_per_trade = s["stake_usd"]
        lowcap_engine.cfg.max_pairs           = s["max_slots"]
        lowcap_engine.cfg.max_positions       = s["max_slots"]
        lowcap_engine.cfg.buy_drop_min_pct    = float(s.get("buy_drop_min_pct", lowcap_engine.cfg.buy_drop_min_pct))
        lowcap_engine.cfg.buy_drop_max_pct    = float(s.get("buy_drop_max_pct", lowcap_engine.cfg.buy_drop_max_pct))
        lowcap_engine.cfg.sell_rise_min_pct   = float(s.get("sell_rise_min_pct", lowcap_engine.cfg.sell_rise_min_pct))
        lowcap_engine.cfg.sell_rise_max_pct   = float(s.get("sell_rise_max_pct", lowcap_engine.cfg.sell_rise_max_pct))
        lowcap_engine.cfg.trailing_enabled    = bool(s.get("trailing_enabled", lowcap_engine.cfg.trailing_enabled))
        lowcap_engine.cfg.trailing_pct        = float(s.get("trailing_pct", lowcap_engine.cfg.trailing_pct))
        lowcap_engine.cfg.stop_loss_enabled   = bool(s.get("stop_loss_enabled", lowcap_engine.cfg.stop_loss_enabled))
        lowcap_engine.cfg.stop_loss_pct       = float(s.get("stop_loss_pct", lowcap_engine.cfg.stop_loss_pct))
        lowcap_engine.cfg.max_orders_per_pair = int(s.get("max_orders_per_pair", lowcap_engine.cfg.max_orders_per_pair))
        print(f"[WebConfig] lowcap position_size updated to {s['stake_usd']}", flush=True)
        print(f"[WebConfig] lowcap max_open_positions updated to {s['max_slots']}", flush=True)
    if grid_bot_engine and "grid_bot" in settings:
        s = settings["grid_bot"]
        new_stake = float(s["stake_usd"])
        new_slots = int(s["max_slots"])
        if "max_active_symbols" in s:
            grid_bot_engine.cfg.max_active_symbols = int(s["max_active_symbols"])
        cur_stake = float(getattr(grid_bot_engine.cfg, "position_size_usdt", new_stake))
        cur_slots = int(getattr(grid_bot_engine.cfg, "max_positions", new_slots))
        stake_changed = cur_stake != new_stake
        slots_changed = cur_slots != new_slots

        if getattr(grid_bot_engine, "running", False):
            if stake_changed:
                grid_bot_engine.update_stake_per_grid(new_stake)
            if slots_changed:
                grid_bot_engine.update_max_positions(new_slots)
            if stake_changed or slots_changed:
                try:
                    grid_bot_engine.log_both("[WebConfig] grid_bot settings updated live")
                except Exception:
                    pass
        else:
            # Startup path: set config directly, do not arm incremental update flags.
            if stake_changed:
                grid_bot_engine.cfg.position_size_usdt = new_stake
            if slots_changed:
                grid_bot_engine.cfg.max_positions = new_slots

        grid_bot_engine.cfg.max_stake_per_trade = float(grid_bot_engine.cfg.position_size_usdt)
        print(
            f"[WebConfig] grid_bot settings: stake={grid_bot_engine.cfg.position_size_usdt} slots={grid_bot_engine.cfg.max_positions}",
            flush=True,
        )

# WebSocket clients
ws_clients: set[WebSocket] = set()

async def broadcast(msg: dict):
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)

def make_log_callback(state: BotState):
    def safe_broadcast(payload: dict):
        if main_loop is None or main_loop.is_closed():
            return
        coro = broadcast(payload)
        try:
            asyncio.run_coroutine_threadsafe(coro, main_loop)
        except Exception:
            # Prevent "coroutine was never awaited" if scheduling fails.
            coro.close()

    def callback(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        if msg.startswith("STATUS_UPDATE::"):
            state.status = msg.replace("STATUS_UPDATE::", "")
            safe_broadcast({"type": "status", "bot": state.name, "text": state.status})
        elif msg.startswith("POSITION_UPDATE::"):
            try:
                pos_data = json.loads(msg.replace("POSITION_UPDATE::", ""))
                pos_data["bot"] = state.name
                safe_broadcast({"type": "position_update", **pos_data})
            except Exception:
                pass
        else:
            entry = {"ts": ts, "msg": msg}
            state.logs.append(entry)

            # Wykryj transakcjÄ™ z logu
            if "SELL" in msg and ("PnL:" in msg):
                try:
                    pnl_part = msg.split("PnL:")[1].split("$")[0].strip()
                    pnl = float(pnl_part)
                    state.realized_profit += pnl
                    state.pnl_history.append({
                        "ts": int(time.time() * 1000),
                        "pnl": round(state.realized_profit, 2)
                    })
                    if len(state.pnl_history) > 200:
                        state.pnl_history = state.pnl_history[-200:]
                    if pnl >= 0:
                        state.wins += 1
                    else:
                        state.losses += 1
                    state.trades.insert(0, {
                        "ts": ts,
                        "msg": msg,
                        "pnl": pnl,
                        "win": pnl >= 0
                    })
                    if len(state.trades) > 50:
                        state.trades = state.trades[:50]
                except Exception:
                    pass

            safe_broadcast({"type": "log", "bot": state.name, "ts": ts, "msg": msg})
    return callback

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   CONTROLLER
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
market_data: Optional[MarketData] = None
scalper_engine: Optional[ScalperEngine] = None
lowcap_engine: Optional[LowcapEngine] = None
grid_bot_engine: Optional[GridBotEngine] = None
main_loop: Optional[asyncio.AbstractEventLoop] = None

def init_engines():
    global market_data, scalper_engine, lowcap_engine, grid_bot_engine

    market_data = MarketData()

    tg_scalper = TelegramNotifier(TELEGRAM_SCALPER.token, TELEGRAM_SCALPER.chat_id)
    tg_lowcap  = TelegramNotifier(TELEGRAM_LOWCAP.token,  TELEGRAM_LOWCAP.chat_id)
    tg_grid_bot    = TelegramNotifier(TELEGRAM_GRID_BOT.token,    TELEGRAM_GRID_BOT.chat_id)

    scalper_cfg = ScalperConfig()
    loaded_scalper = load_scalper_settings_file()
    scalper_cfg.max_stake_usd        = float(loaded_scalper["stake_usd"])
    scalper_cfg.max_position_size_usdt = scalper_cfg.max_stake_usd
    scalper_cfg.slot_count           = int(loaded_scalper["max_slots"])
    scalper_cfg.max_open_positions   = scalper_cfg.slot_count
    _float_cfg_keys = [
        "target_profit_pct", "stop_loss_pct", "trailing_stop_pct", "maker_fee",
        "taker_fee", "runner_trail_pct",
        "daily_loss_limit_pct", "max_position_time_sec", "min_signal_strength",
        "symbol_cooldown_sec", "momentum_min_change", "volume_spike_mult",
        "atr_filter_min", "pullback_min_retrace", "pullback_max_retrace",
        "impulse_ttl_sec", "momentum_window_sec", "volume_baseline_window_sec",
        "trend_window_sec",
        "reinvest_max_stake", "stake_max_cap_usdt", "base_stake_usdt",
    ]
    for key in _float_cfg_keys:
        if key in loaded_scalper:
            setattr(scalper_cfg, key, float(loaded_scalper[key]))
    for key in ["max_trades_day", "trend_ema_period"]:
        if key in loaded_scalper:
            setattr(scalper_cfg, key, int(loaded_scalper[key]))
    if "reinvest_enabled" in loaded_scalper:
        scalper_cfg.reinvest_enabled = bool(loaded_scalper["reinvest_enabled"])

    scalper_engine = ScalperEngine(
        scalper_cfg,
        log_callback=make_log_callback(scalper_state),
        market_data=market_data,
        tg=tg_scalper,
        portfolio=wallet,
    )
    lowcap_cfg = LowcapConfig()
    loaded_lowcap = load_lowcap_settings_file()
    lowcap_cfg.max_stake_per_trade = float(loaded_lowcap["stake_usd"])
    lowcap_cfg.position_size = float(loaded_lowcap["stake_usd"])
    lowcap_cfg.max_pairs = int(loaded_lowcap["max_slots"])
    lowcap_cfg.max_positions = int(loaded_lowcap["max_slots"])
    lowcap_cfg.buy_drop_min_pct = float(loaded_lowcap["buy_drop_min_pct"])
    lowcap_cfg.buy_drop_max_pct = float(loaded_lowcap["buy_drop_max_pct"])
    lowcap_cfg.sell_rise_min_pct = float(loaded_lowcap["sell_rise_min_pct"])
    lowcap_cfg.sell_rise_max_pct = float(loaded_lowcap["sell_rise_max_pct"])
    lowcap_cfg.trailing_enabled = bool(loaded_lowcap["trailing_enabled"])
    lowcap_cfg.trailing_pct = float(loaded_lowcap["trailing_pct"])
    lowcap_cfg.stop_loss_enabled = bool(loaded_lowcap["stop_loss_enabled"])
    lowcap_cfg.stop_loss_pct = float(loaded_lowcap["stop_loss_pct"])
    lowcap_cfg.max_orders_per_pair = int(loaded_lowcap["max_orders_per_pair"])

    lowcap_engine = LowcapEngine(
        lowcap_cfg,
        log_callback=make_log_callback(lowcap_state),
        market_data=market_data,
        tg=tg_lowcap,
        portfolio=wallet,
    )
    grid_bot_cfg = GridBotConfig()
    loaded_grid = load_grid_bot_settings_file()
    grid_bot_cfg.position_size_usdt = float(loaded_grid["stake_usd"])
    grid_bot_cfg.max_positions = int(loaded_grid["max_slots"])
    grid_bot_cfg.max_active_symbols = int(loaded_grid["max_active_symbols"])
    grid_bot_cfg.max_stake_per_trade = float(grid_bot_cfg.position_size_usdt)

    grid_bot_engine = GridBotEngine(
        grid_bot_cfg,
        log_callback=make_log_callback(grid_bot_state),
        market_data=market_data,
        tg=tg_grid_bot,
        portfolio=wallet,
    )

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   API ENDPOINTS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
@app.get("/api/state")
def get_state(user=Depends(verify_token)):
    def bot_info(state: BotState, engine):
        positions = getattr(engine, "active_positions", {}) if engine else {}
        return {
            "running": state.running,
            "status": state.status,
            "wins": state.wins,
            "losses": state.losses,
            "realized_profit": round(state.realized_profit, 2),
            "pnl_history": state.pnl_history[-100:],
            "trades": state.trades[:50],
            "open_positions": list(positions.keys()),
            "position_details": {
                sym.replace("_", "/"): {
                    "entry_price": d.get("entry_price", 0),
                    "sl_price": d.get("sl_price", 0),
                    "tp_price": d.get("tp_price", 0),
                    "stake": d.get("stake", 0),
                }
                for sym, d in positions.items()
            },
        }
    return {
        "scalper": bot_info(scalper_state, scalper_engine),
        "lowcap":  bot_info(lowcap_state,  lowcap_engine),
        "grid_bot":    bot_info(grid_bot_state,     grid_bot_engine),
        "market_data_running": market_data.running if market_data else False,
    }

@app.get("/api/logs/{bot}")
def get_logs(bot: str, user=Depends(verify_token)):
    state = {"scalper": scalper_state, "lowcap": lowcap_state, "grid_bot": grid_bot_state}.get(bot)
    if not state:
        raise HTTPException(404, "Bot nie istnieje")
    return list(state.logs)

@app.post("/api/start/{bot}")
async def start_bot(bot: str, user=Depends(verify_token)):
    global market_data, scalper_engine, lowcap_engine, grid_bot_engine

    if bot == "scalper" and not scalper_state.running:
        # Synchronizuj stan — jeśli engine już nie działa, wymuś reset
        if scalper_engine.running:
            scalper_engine.running = False
        await scalper_engine.start()
        scalper_state.running = True
    elif bot == "lowcap" and not lowcap_state.running:
        lowcap_engine.start()
        lowcap_state.running = True
    elif bot == "grid_bot" and not grid_bot_state.running:
        await grid_bot_engine.start()
        grid_bot_state.running = True
    else:
        raise HTTPException(400, f"Bot '{bot}' nie istnieje lub juz dziala")

    await broadcast({"type": "bot_state", "bot": bot, "running": True})
    return {"ok": True}

@app.post("/api/stop/{bot}")
async def stop_bot(bot: str, user=Depends(verify_token)):
    if bot == "scalper" and scalper_state.running:
        # Ustaw running=False PRZED stop() — stop() może trwać minuty
        # Jeśli HTTP timeout → scalper_state.running musi być False
        # żeby start() mógł ponownie uruchomić bota
        scalper_state.running = False
        await scalper_engine.stop()
    elif bot == "lowcap" and lowcap_state.running:
        lowcap_engine.stop()
        lowcap_state.running = False
    elif bot == "grid_bot" and grid_bot_state.running:
        await grid_bot_engine.stop()
        grid_bot_state.running = False
    else:
        raise HTTPException(400, f"Bot '{bot}' nie istnieje lub nie dziala")

    await broadcast({"type": "bot_state", "bot": bot, "running": False})
    return {"ok": True}

@app.get("/api/portfolio")
async def get_portfolio(user=Depends(verify_token)):
    if scalper_engine:
        try:
            stats = await scalper_engine._fetch_trade_stats()
        except Exception:
            stats = scalper_engine.bot_profit_summary
        _start = scalper_engine._start_equity
        if _start <= 0:
            _start = stats.get("equity", 0.0)
        return {
            "balance_usdt":   stats.get("usdt_free", 0.0),
            "total_exposure": stats.get("engaged", 0.0),
            "total_equity":   stats.get("equity", 0.0),
            "start_balance":  round(_start, 2),
        }
    else:
        available = wallet.get_total("USDT")
        engines = [e for e in [scalper_engine, lowcap_engine, grid_bot_engine] if e]
        exposure = get_total_exposure(engines)
        return {
            "balance_usdt":   round(available, 2),
            "total_exposure": round(exposure, 2),
            "total_equity":   round(available + exposure, 2),
            "start_balance":  round(available, 2),
        }

@app.get("/api/settings")
def get_settings(user=Depends(verify_token)):
    return load_all_settings()

class SettingsRequest(BaseModel):
    stake_usd: Optional[float] = None
    max_slots: Optional[int] = None
    target_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    maker_fee: Optional[float] = None
    taker_fee: Optional[float] = None
    runner_trail_pct: Optional[float] = None
    reinvest_enabled: Optional[bool] = None
    reinvest_max_stake: Optional[float] = None
    stake_max_cap_usdt: Optional[float] = None
    base_stake_usdt: Optional[float] = None
    max_trades_day: Optional[int] = None
    daily_loss_limit_pct: Optional[float] = None
    min_signal_strength: Optional[float] = None
    symbol_cooldown_sec: Optional[float] = None
    momentum_min_change: Optional[float] = None
    volume_spike_mult: Optional[float] = None
    atr_filter_min: Optional[float] = None
    pullback_min_retrace: Optional[float] = None
    pullback_max_retrace: Optional[float] = None
    impulse_ttl_sec: Optional[float] = None
    momentum_window_sec: Optional[float] = None
    volume_baseline_window_sec: Optional[float] = None
    trend_ema_period: Optional[int] = None
    trend_window_sec: Optional[float] = None
    # Lowcap-specific
    buy_drop_min_pct: Optional[float] = None
    buy_drop_max_pct: Optional[float] = None
    sell_rise_min_pct: Optional[float] = None
    sell_rise_max_pct: Optional[float] = None
    trailing_enabled: Optional[bool] = None
    trailing_pct: Optional[float] = None
    stop_loss_enabled: Optional[bool] = None
    max_orders_per_pair: Optional[int] = None
    # Grid bot-specific
    max_active_symbols: Optional[int] = None

@app.post("/api/settings/{bot}")
async def update_settings(bot: str, req: SettingsRequest, user=Depends(verify_token)):
    if bot not in ("scalper", "lowcap", "grid_bot"):
        raise HTTPException(404, "Bot nie istnieje")
    if bot == "scalper":
        if req.stake_usd is None or req.max_slots is None:
            raise HTTPException(400, "stake_usd i max_slots sa wymagane")
        if req.stake_usd <= 0:
            raise HTTPException(400, "stake_usd musi byc > 0")
        if req.max_slots < 1 or req.max_slots > 10:
            raise HTTPException(400, "max_slots musi byc w zakresie 1-10")
        _live_bal = scalper_engine._equity if (scalper_engine and scalper_engine._equity > 0) else wallet.start_balance
        if req.stake_usd * req.max_slots > _live_bal * 1.05:
            raise HTTPException(
                400,
                f"Ekspozycja ({req.stake_usd * req.max_slots:.2f}$) przekracza balance ({_live_bal:.2f}$)",
            )
        payload = _scalper_defaults_dict()  # start from saved/defaults
        payload.update(load_scalper_settings_file())  # merge saved
        payload["stake_usd"] = req.stake_usd
        payload["max_slots"] = req.max_slots
        # Merge all optional scalper fields from request
        _scalper_opt_float = [
            "target_profit_pct", "stop_loss_pct", "trailing_stop_pct", "maker_fee",
            "taker_fee", "runner_trail_pct",
            "daily_loss_limit_pct", "min_signal_strength",
            "symbol_cooldown_sec", "momentum_min_change", "volume_spike_mult",
            "atr_filter_min", "pullback_min_retrace", "pullback_max_retrace",
            "impulse_ttl_sec", "momentum_window_sec", "volume_baseline_window_sec",
            "trend_window_sec",
            "reinvest_max_stake", "stake_max_cap_usdt", "base_stake_usdt",
        ]
        for key in _scalper_opt_float:
            val = getattr(req, key, None)
            if val is not None:
                payload[key] = float(val)
        for key in ["max_trades_day", "trend_ema_period"]:
            val = getattr(req, key, None)
            if val is not None:
                payload[key] = int(val)
        if req.reinvest_enabled is not None:
            payload["reinvest_enabled"] = bool(req.reinvest_enabled)
        # Basic validation
        if payload.get("target_profit_pct", 0) <= 0:
            raise HTTPException(400, "target_profit_pct musi byc > 0")
        if payload.get("stop_loss_pct", 0) <= 0:
            raise HTTPException(400, "stop_loss_pct musi byc > 0")
        if payload.get("trailing_stop_pct", -1) < 0:
            raise HTTPException(400, "trailing_stop_pct musi byc >= 0")
        save_setting_db(bot, req.stake_usd, req.max_slots)
        save_scalper_settings_file(payload)
        apply_settings_to_engines({bot: payload})
        return {"ok": True}

    if bot == "lowcap":
        if req.stake_usd is None or req.max_slots is None:
            raise HTTPException(400, "stake_usd i max_slots sa wymagane")
        if req.stake_usd <= 0:
            raise HTTPException(400, "stake_usd musi byc > 0")
        if req.max_slots < 1 or req.max_slots > 10:
            raise HTTPException(400, "max_slots musi byc w zakresie 1-10")
        if req.stake_usd * req.max_slots > wallet.start_balance:
            raise HTTPException(
                400,
                f"Ekspozycja ({req.stake_usd * req.max_slots:.2f}$) przekracza balance ({wallet.start_balance:.2f}$)",
            )
        payload = _lowcap_defaults_dict()
        payload["stake_usd"] = float(req.stake_usd)
        payload["max_slots"] = int(req.max_slots)
        if req.buy_drop_min_pct is not None:
            payload["buy_drop_min_pct"] = float(req.buy_drop_min_pct)
        if req.buy_drop_max_pct is not None:
            payload["buy_drop_max_pct"] = float(req.buy_drop_max_pct)
        if req.sell_rise_min_pct is not None:
            payload["sell_rise_min_pct"] = float(req.sell_rise_min_pct)
        if req.sell_rise_max_pct is not None:
            payload["sell_rise_max_pct"] = float(req.sell_rise_max_pct)
        if req.trailing_enabled is not None:
            payload["trailing_enabled"] = bool(req.trailing_enabled)
        if req.trailing_pct is not None:
            payload["trailing_pct"] = float(req.trailing_pct)
        if req.stop_loss_enabled is not None:
            payload["stop_loss_enabled"] = bool(req.stop_loss_enabled)
        if req.stop_loss_pct is not None:
            payload["stop_loss_pct"] = float(req.stop_loss_pct)
        if req.max_orders_per_pair is not None:
            payload["max_orders_per_pair"] = int(req.max_orders_per_pair)

        if payload["buy_drop_min_pct"] <= 0 or payload["buy_drop_max_pct"] <= payload["buy_drop_min_pct"]:
            raise HTTPException(400, "buy_drop values invalid")
        if payload["sell_rise_min_pct"] <= 0 or payload["sell_rise_max_pct"] <= payload["sell_rise_min_pct"]:
            raise HTTPException(400, "sell_rise values invalid")
        if payload["trailing_pct"] < 0:
            raise HTTPException(400, "trailing_pct invalid")
        if payload["stop_loss_pct"] <= 0:
            raise HTTPException(400, "stop_loss_pct invalid")
        if payload["max_orders_per_pair"] < 1 or payload["max_orders_per_pair"] > 3:
            raise HTTPException(400, "max_orders_per_pair must be 1-3")

        save_lowcap_settings_file(payload)
        save_setting_db("lowcap", payload["stake_usd"], payload["max_slots"])
        apply_settings_to_engines({"lowcap": payload})
        return {"ok": True}

    if req.stake_usd is None or req.max_slots is None:
        raise HTTPException(400, "stake_usd i max_slots sa wymagane")
    if req.stake_usd <= 0:
        raise HTTPException(400, "stake_usd musi byc > 0")
    if req.max_slots < 1 or req.max_slots > 10:
        raise HTTPException(400, "max_slots musi byc w zakresie 1-10")
    if float(req.stake_usd) * int(req.max_slots) > wallet.start_balance:
        raise HTTPException(
            400,
            f"Ekspozycja ({float(req.stake_usd) * int(req.max_slots):.2f}$) przekracza balance ({wallet.start_balance:.2f}$)",
        )

    payload = {"stake_usd": float(req.stake_usd), "max_slots": int(req.max_slots)}
    if req.max_active_symbols is not None:
        if req.max_active_symbols < 1 or req.max_active_symbols > 10:
            raise HTTPException(400, "max_active_symbols musi byc w zakresie 1-10")
        payload["max_active_symbols"] = int(req.max_active_symbols)
    save_grid_bot_settings_file(payload)
    save_setting_db("grid_bot", payload["stake_usd"], payload["max_slots"])
    apply_settings_to_engines({"grid_bot": payload})
    return {"ok": True}

@app.get("/api/scalper/profit")
async def get_scalper_profit(user=Depends(verify_token)):
    if not scalper_engine:
        return {"bot": "scalper", "usdt_free": 0, "session_pnl": 0,
                "daily_pnl": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "effective_stake": 0, "total_fee_paid": 0, "open_positions": 0}
    try:
        stats = await scalper_engine._fetch_trade_stats()
        return stats
    except Exception:
        return scalper_engine.bot_profit_summary

@app.get("/api/analytics")
def get_analytics_data(bot: str = "scalper", user=Depends(verify_token)):
    if bot not in ("scalper", "lowcap", "grid_bot"):
        raise HTTPException(404, "Bot nie istnieje")
    try:
        analytics = get_analytics(bot)
        return analytics.get_dashboard_data()
    except Exception as e:
        return {"error": str(e), "bot": bot, "trade_count": 0}

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   WEBSOCKET
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Weryfikacja tokenu przez query param
    token = websocket.query_params.get("token", "")
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#   SERVE FRONTEND
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("<h1>Brak pliku web/index.html</h1>", status_code=404)


if __name__ == "__main__":
    uvicorn.run(
        "main_web:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=5,
    )


