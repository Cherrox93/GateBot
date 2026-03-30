# 🤖 GateBot — Automated Cryptocurrency Scalping Bot

> **Automated spot trading bot for Gate.io exchange, built in Python with async architecture.**
> Developed with the assistance of [Claude Code](https://claude.ai/code) by Anthropic.

---

## 📌 Overview

GateBot is a personal project — a fully automated cryptocurrency trading system deployed on a VPS (Ubuntu/Hetzner), targeting the Gate.io spot market. The bot operates 24/7, analyzes real-time market data via WebSocket feeds, generates trading signals, executes orders, and manages risk autonomously.

The project was built iteratively over several months, with continuous improvements to signal quality, risk management, and execution reliability.

---

## ⚙️ Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Architecture | Async (asyncio) |
| Exchange connectivity | ccxt + Gate.io WebSocket API |
| Web dashboard | FastAPI + WebSocket |
| ML pipeline | scikit-learn, LightGBM |
| Database | SQLite (per-engine analytics) |
| Deployment | Ubuntu VPS (Hetzner) |
| Notifications | Telegram Bot API |
| AI-assisted development | Claude Code (Anthropic) |

---

## 🏗️ Architecture

GateBot consists of three independent trading engines sharing a common MarketData WebSocket feed:

```
┌─────────────────────────────────────────┐
│            main_web.py (FastAPI)         │
│         Web Dashboard + REST API         │
└────────────┬────────────────────────────┘
             │
    ┌────────▼────────┐
    │   MarketData    │  ← Shared WebSocket feed
    │   WebSocket     │     (orderbook + trades)
    └────────┬────────┘
             │
    ┌────────┴──────────────┐
    │                       │
┌───▼──────────┐   ┌───────▼──────────┐
│   Highcap    │   │   Pump Detector  │
│   Scalper    │   │   / Grid Bot     │
│ BTC ETH SOL  │   │  (lowcap pairs)  │
│ XRP BNB ...  │   │                  │
└──────────────┘   └──────────────────┘
```

---

## 🚀 Scalper Engine — Core Features

### Smart Order Router (SOR)
Three-tier entry execution for optimal fill price:
1. **Post-only maker** — attempts cheapest entry inside spread
2. **Aggressive limit** — crosses spread for faster fill
3. **Market fallback** — guarantees execution

### Entry Signal Pipeline
- **Momentum breakout** — detects 5s price impulse + volume spike
- **Momentum pullback** — enters on retracement after impulse
- **Micro breakout** — FeatureEngine orderbook imbalance confirmation

### Multi-Timeframe Trend Filter
```
LTF (5min ticks): price > EMA20 AND EMA20 > EMA50
BTC context:      BTC 5min change as market regime filter
→ LONG only when both levels aligned
```

### Risk Management
- **Dynamic SL** — ATR-based stop loss (1.3× ATR, floored at 0.30%)
- **TP + Runner system** — position runs freely above TP, trailing exit at 0.20% from peak
- **Break-even protection** — SL floor at TP price once runner activates
- **Circuit breaker** — 20min pause after 3 consecutive losses
- **Daily loss limit** — auto-stop at 5% daily drawdown

### Execution Quality
- Real-time WebSocket orderbook cache (sub-100ms data)
- Balance verification before every trade
- Full dust cleanup after sell (sells entire available balance)
- Position recovery on restart — reads saved JSON state

---

## 📊 ML Analytics Pipeline

Four-level ML system activates progressively as trade history grows:

```
Trades 0–20:    Rule Engine only
Trades 20–50:   + Logistic Regression
Trades 50–100:  + Random Forest
Trades 100+:    + LightGBM (full pipeline)
```

**Features collected per trade (30+):**
- EMA slopes, RSI, MACD histogram, Bollinger Band width
- Volume Z-score, volume acceleration, trade flow imbalance
- Orderbook bid/ask ratio, spread
- BTC trend, market regime classification
- MFE/MAE (max favorable/adverse excursion)

**ML outputs:**
- Trade approval/rejection
- Position size multiplier (0.5× – 2.0×)
- Dynamic TP/SL adjustment
- Symbol-level blocking after consecutive losses

---

## 🖥️ Web Dashboard

Real-time FastAPI dashboard with WebSocket updates:

- **Portfolio overview** — live balance, equity, engaged capital (from Gate.io API)
- **Session PnL** — calculated from real exchange trade history
- **Win Rate / W / L** — updated immediately after each trade
- **Open positions panel** — live price, entry, SL, TP, unrealized PnL %
- **Trade log** — timestamped BUY/SELL with full details
- **Per-engine controls** — START / STOP with graceful shutdown

---

## 📱 Telegram Notifications

Real-time alerts for every trade event:

```
💰 BUY ETH/USDT | Entry Price: 2091.38 | Stawka: 50.00$
🚀 SELL ETH/USDT | RUNNER | Entry: 2091.38 | Sell: 2101.84 | PnL: +0.23$
🔴 SELL SOL/USDT | SL | Entry: 91.96 | Sell: 91.40 | PnL: -0.18$
⛔ CIRCUIT BREAKER: 3 SL z rzędu — pauza 20 minut
♻️ RECOVERY: ETH/USDT | entry=2075.80 | TP=2086.18 | SL=2069.57
```

---

## 🔄 Resilience Features

- **Crash recovery** — on restart, reads `position_*.json` files, verifies live balance on Gate.io, resumes monitoring with original entry/TP/SL
- **Manual close detection** — `_monitor()` checks exchange balance every 60s, detects positions closed manually on the exchange
- **Graceful STOP** — stops accepting new signals immediately, waits for all open positions to close naturally (TP/Runner/SL) before shutting down
- **WebSocket reconnect** — exponential backoff, max 10 retries
- **WS staleness guard** — skips processing if data > 500ms old

---

## 📈 Trading Pairs

```python
gigants = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "BNB/USDT", "DOGE/USDT", "ADA/USDT", "AVAX/USDT",
    "SUI/USDT", "LINK/USDT", "LTC/USDT", "ARB/USDT",
    "DOT/USDT", "NEAR/USDT",
]
```

---

## 📸 Screenshots

https://drive.google.com/file/d/1JtJiEDzSIWlIOZfN7XbZjR2EHRGrOeHV/view?usp=sharing
https://drive.google.com/file/d/1qQaBZKPgsTnuplytJ9Rfp188DHLqT74R/view?usp=sharing
https://drive.google.com/file/d/1OZ0muCHuLF_L0yRzzKcA0YMH_lOg2aeR/view?usp=sharing

---

## 🧠 Development Approach

GateBot was built iteratively using **Claude Code** (Anthropic) as an AI development assistant. The workflow involved:

- Designing architecture and data flow collaboratively
- Debugging live trading issues from log analysis
- Iterative parameter tuning based on real trade results
- Prompt-driven code changes with explicit constraints to avoid regressions

This approach allowed rapid development of a production-grade trading system while maintaining code quality and auditability.

---

## ⚠️ Disclaimer

This project is for **educational and personal use only**. Cryptocurrency trading carries significant financial risk. Past performance does not guarantee future results. Use at your own risk.

---

## 👤 Author

**Kamil** — [@Cherrox93](https://github.com/Cherrox93)

*Built with Python + asyncio + Gate.io API + Claude Code*
