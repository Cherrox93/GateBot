"""
main_web.py — GateBot Web Server
FastAPI backend z WebSocket, autoryzacją JWT i pełnym API.
Uruchomienie: python main_web.py
Dostęp: http://localhost:8000 (lokalnie) lub http://IP_VPS:8000 (VPS)
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

from config import ScalperConfig, LowcapConfig, PumpConfig
from engines.scalper_engine import ScalperEngine
from engines.lowcap_engine import LowcapEngine
from engines.pump_engine import PumpEngine
from market_data import MarketData
from telegram_notifier import TelegramNotifier
from vault import TELEGRAM_SCALPER, TELEGRAM_LOWCAP, TELEGRAM_PUMP
from analytics_engine import get_analytics, get_all_analytics

# ═══════════════════════════════════════
#   KONFIGURACJA
# ═══════════════════════════════════════
WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "gatebot2024")
JWT_SECRET   = os.environ.get("JWT_SECRET", "superSecretKey_zmienMnie!")
JWT_EXPIRE_H = 24 * 7  # token ważny 7 dni

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    init_settings_db()
    init_engines()
    apply_settings_to_engines(load_settings_db())
    print(f"[GateBot] Dashboard dostępny na http://0.0.0.0:8000", flush=True)
    print(f"[GateBot] Login: {WEB_USERNAME} / {WEB_PASSWORD}", flush=True)
    yield
    print("[GateBot] Zatrzymywanie botów...", flush=True)
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
    if pump_state.running and pump_engine:
        try:
            await pump_engine.stop()
        except Exception:
            pass
    if market_data and market_data.running:
        try:
            await market_data.stop()
        except Exception:
            pass
    print("[GateBot] Zamknięto.", flush=True)

app = FastAPI(title="GateBot Dashboard", lifespan=lifespan)
security = HTTPBearer()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════
#   JWT AUTH
# ═══════════════════════════════════════
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nieprawidłowy token")

@app.post("/api/login")
def login(req: LoginRequest):
    if req.username != WEB_USERNAME or req.password != WEB_PASSWORD:
        raise HTTPException(status_code=401, detail="Nieprawidłowe dane logowania")
    token = create_token(req.username)
    return {"token": token}

# ═══════════════════════════════════════
#   STATE
# ═══════════════════════════════════════
class BotState:
    def __init__(self, name: str):
        self.name = name
        self.running = False
        self.status = "🔍 Oczekiwanie…"
        self.logs: deque = deque(maxlen=100)
        self.pnl_history: list = []   # [{"ts": unix, "pnl": float}]
        self.trades: list = []        # historia transakcji
        self.wins = 0
        self.losses = 0
        self.realized_profit = 0.0
        self.open_positions: dict = {}

scalper_state = BotState("scalper")
lowcap_state  = BotState("lowcap")
pump_state    = BotState("pump")

# ═══════════════════════════════════════
#   SHARED PORTFOLIO
# ═══════════════════════════════════════
class SharedPortfolio:
    def __init__(self, start_balance: float = 200.0):
        self.start_balance = start_balance
        self.balance_usdt = start_balance

    def get_total_exposure(self, engines: list) -> float:
        total = 0.0
        for engine in engines:
            for p in getattr(engine, "active_positions", {}).values():
                total += p.get("original_stake", p.get("stake", 0.0))
        return total

    def get_total_equity(self, engines: list) -> float:
        return self.balance_usdt + self.get_total_exposure(engines)

portfolio = SharedPortfolio(start_balance=200.0)

# ═══════════════════════════════════════
#   SETTINGS DB
# ═══════════════════════════════════════
SETTINGS_DB = "settings.db"

SETTINGS_DEFAULTS = {
    "scalper": {"stake_usd": 10.0, "max_slots": 3},
    "lowcap":  {"stake_usd": 5.0,  "max_slots": 3},
    "pump":    {"stake_usd": 5.0,  "max_slots": 3},
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

def load_settings_db() -> dict:
    conn = sqlite3.connect(SETTINGS_DB)
    c = conn.cursor()
    c.execute("SELECT bot_name, stake_usd, max_slots FROM bot_settings")
    rows = c.fetchall()
    conn.close()
    return {row[0]: {"stake_usd": row[1], "max_slots": row[2]} for row in rows}

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
        scalper_engine.cfg.max_stake_usd = s["stake_usd"]
        scalper_engine.cfg.slot_count    = s["max_slots"]
    if lowcap_engine and "lowcap" in settings:
        s = settings["lowcap"]
        lowcap_engine.cfg.max_stake_per_trade = s["stake_usd"]
        lowcap_engine.cfg.max_positions       = s["max_slots"]
    if pump_engine and "pump" in settings:
        s = settings["pump"]
        pump_engine.cfg.max_stake    = s["stake_usd"]
        pump_engine.cfg.max_positions = s["max_slots"]

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
    def callback(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        if msg.startswith("STATUS_UPDATE::"):
            state.status = msg.replace("STATUS_UPDATE::", "")
            asyncio.run_coroutine_threadsafe(
                broadcast({"type": "status", "bot": state.name, "text": state.status}),
                main_loop
            )
        else:
            entry = {"ts": ts, "msg": msg}
            state.logs.append(entry)

            # Wykryj transakcję z logu
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

            asyncio.run_coroutine_threadsafe(
                broadcast({"type": "log", "bot": state.name, "ts": ts, "msg": msg}),
                main_loop
            )
    return callback

# ═══════════════════════════════════════
#   CONTROLLER
# ═══════════════════════════════════════
market_data: Optional[MarketData] = None
scalper_engine: Optional[ScalperEngine] = None
lowcap_engine: Optional[LowcapEngine] = None
pump_engine: Optional[PumpEngine] = None
main_loop: Optional[asyncio.AbstractEventLoop] = None

def init_engines():
    global market_data, scalper_engine, lowcap_engine, pump_engine

    market_data = MarketData()

    tg_scalper = TelegramNotifier(TELEGRAM_SCALPER.token, TELEGRAM_SCALPER.chat_id)
    tg_lowcap  = TelegramNotifier(TELEGRAM_LOWCAP.token,  TELEGRAM_LOWCAP.chat_id)
    tg_pump    = TelegramNotifier(TELEGRAM_PUMP.token,    TELEGRAM_PUMP.chat_id)

    scalper_engine = ScalperEngine(
        ScalperConfig(),
        log_callback=make_log_callback(scalper_state),
        market_data=market_data,
        tg=tg_scalper,
        portfolio=portfolio,
    )
    lowcap_engine = LowcapEngine(
        LowcapConfig(),
        log_callback=make_log_callback(lowcap_state),
        market_data=market_data,
        tg=tg_lowcap,
        portfolio=portfolio,
    )
    pump_engine = PumpEngine(
        PumpConfig(),
        log_callback=make_log_callback(pump_state),
        market_data=market_data,
        tg=tg_pump,
        portfolio=portfolio,
    )

# ═══════════════════════════════════════
#   API ENDPOINTS
# ═══════════════════════════════════════
@app.get("/api/state")
def get_state(user=Depends(verify_token)):
    def bot_info(state: BotState, engine):
        return {
            "running": state.running,
            "status": state.status,
            "wins": state.wins,
            "losses": state.losses,
            "realized_profit": round(state.realized_profit, 2),
            "pnl_history": state.pnl_history[-100:],
            "trades": state.trades[:50],
            "open_positions": list(getattr(engine, "active_positions", {}).keys()) if engine else [],
        }
    return {
        "scalper": bot_info(scalper_state, scalper_engine),
        "lowcap":  bot_info(lowcap_state,  lowcap_engine),
        "pump":    bot_info(pump_state,     pump_engine),
        "market_data_running": market_data.running if market_data else False,
    }

@app.get("/api/logs/{bot}")
def get_logs(bot: str, user=Depends(verify_token)):
    state = {"scalper": scalper_state, "lowcap": lowcap_state, "pump": pump_state}.get(bot)
    if not state:
        raise HTTPException(404, "Bot nie istnieje")
    return list(state.logs)

@app.post("/api/start/{bot}")
async def start_bot(bot: str, user=Depends(verify_token)):
    global market_data, scalper_engine, lowcap_engine, pump_engine

    if not market_data.running:
        await market_data.start()
        await asyncio.sleep(3)

    if bot == "scalper" and not scalper_state.running:
        await scalper_engine.start()
        scalper_state.running = True
    elif bot == "lowcap" and not lowcap_state.running:
        lowcap_engine.start()
        lowcap_state.running = True
    elif bot == "pump" and not pump_state.running:
        await pump_engine.start()
        pump_state.running = True
    else:
        raise HTTPException(400, f"Bot '{bot}' nie istnieje lub już działa")

    await broadcast({"type": "bot_state", "bot": bot, "running": True})
    return {"ok": True}

@app.post("/api/stop/{bot}")
async def stop_bot(bot: str, user=Depends(verify_token)):
    if bot == "scalper" and scalper_state.running:
        await scalper_engine.stop()
        scalper_state.running = False
    elif bot == "lowcap" and lowcap_state.running:
        lowcap_engine.stop()
        lowcap_state.running = False
    elif bot == "pump" and pump_state.running:
        await pump_engine.stop()
        pump_state.running = False
    else:
        raise HTTPException(400, f"Bot '{bot}' nie istnieje lub nie działa")

    await broadcast({"type": "bot_state", "bot": bot, "running": False})
    return {"ok": True}

@app.get("/api/portfolio")
def get_portfolio(user=Depends(verify_token)):
    engines = [e for e in [scalper_engine, lowcap_engine, pump_engine] if e]
    exposure = portfolio.get_total_exposure(engines)
    equity   = portfolio.get_total_equity(engines)
    return {
        "balance_usdt":   round(portfolio.balance_usdt, 2),
        "total_exposure": round(exposure, 2),
        "total_equity":   round(equity, 2),
        "start_balance":  round(portfolio.start_balance, 2),
    }

@app.get("/api/settings")
def get_settings(user=Depends(verify_token)):
    return load_settings_db()

class SettingsRequest(BaseModel):
    stake_usd: float
    max_slots: int

@app.post("/api/settings/{bot}")
def update_settings(bot: str, req: SettingsRequest, user=Depends(verify_token)):
    if bot not in ("scalper", "lowcap", "pump"):
        raise HTTPException(404, "Bot nie istnieje")
    save_setting_db(bot, req.stake_usd, req.max_slots)
    apply_settings_to_engines({bot: {"stake_usd": req.stake_usd, "max_slots": req.max_slots}})
    return {"ok": True}

@app.get("/api/analytics")
def get_analytics_data(bot: str = "scalper", user=Depends(verify_token)):
    if bot not in ("scalper", "lowcap", "pump"):
        raise HTTPException(404, "Bot nie istnieje")
    try:
        analytics = get_analytics(bot)
        return analytics.get_dashboard_data()
    except Exception as e:
        return {"error": str(e), "bot": bot, "trade_count": 0}

# ═══════════════════════════════════════
#   WEBSOCKET
# ═══════════════════════════════════════
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

# ═══════════════════════════════════════
#   SERVE FRONTEND
# ═══════════════════════════════════════
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