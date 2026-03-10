import sys
import asyncio
import threading
from collections import deque

# Wymagane na Windows: aiohttp (ccxt.async_support) nie dziaÅ‚a z domyÅ›lnym ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QTextEdit, QGridLayout, QFrame
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QTextCursor, QColor, QTextCharFormat

from config import ScalperConfig, LowcapConfig, GridBotConfig
from engines.scalper_engine import ScalperEngine
from engines.lowcap_engine import LowcapEngine
from engines.grid_bot_engine import GridBotEngine
from market_data import MarketData
from telegram_notifier import TelegramNotifier
from vault import TELEGRAM_SCALPER, TELEGRAM_LOWCAP, TELEGRAM_GRID_BOT
from wallet import SharedWallet

from datetime import datetime


class Controller:
    def __init__(self):
        # async loop
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        # Jeden wspÃ³lny MarketData â€“ pobiera wszystkie pary USDT automatycznie
        self.market_data = MarketData()
        self.wallet = SharedWallet(initial_usdt=200.0)

        # Telegram notifiers
        tg_scalper = TelegramNotifier(TELEGRAM_SCALPER.token, TELEGRAM_SCALPER.chat_id)
        tg_lowcap  = TelegramNotifier(TELEGRAM_LOWCAP.token,  TELEGRAM_LOWCAP.chat_id)
        tg_grid_bot    = TelegramNotifier(TELEGRAM_GRID_BOT.token,    TELEGRAM_GRID_BOT.chat_id)


        # Boty dostajÄ… referencjÄ™ do MarketData i Telegram
        self.scalper = ScalperEngine(
            ScalperConfig(),
            log_callback=None,
            market_data=self.market_data,
            tg=tg_scalper,
            portfolio=self.wallet,
        )

        self.lowcap = LowcapEngine(
            LowcapConfig(),
            log_callback=None,
            market_data=self.market_data,
            tg=tg_lowcap,
            portfolio=self.wallet,
        )

        self.grid_bot = GridBotEngine(
            GridBotConfig(),
            log_callback=None,
            market_data=self.market_data,
            tg=tg_grid_bot,
            portfolio=self.wallet,
        )

        # Uruchom MarketData od razu po starcie aplikacji
        self.run_async(self.market_data.start())

    def run_async(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ============================
    #        TOGGLE SCALPER
    # ============================
    def toggle_scalper(self):
        if not self.scalper.running:
            if not self.market_data.running:
                self.run_async(self.market_data.start())
            self.run_async(self.scalper.start())
            return True
        else:
            self.run_async(self.scalper.stop())
            return False

    # ============================
    #        TOGGLE LOWCAP
    # ============================
    def toggle_lowcap(self):
        if not self.lowcap.running:
            if not self.market_data.running:
                self.run_async(self.market_data.start())
            self.lowcap.start()
            return True
        else:
            self.lowcap.stop()
            return False

    # ============================
    #        TOGGLE grid_bot
    # ============================
    def toggle_grid_bot(self):
        if not self.grid_bot.running:
            if not self.market_data.running:
                self.run_async(self.market_data.start())
            self.run_async(self.grid_bot.start())
            return True
        else:
            self.run_async(self.grid_bot.stop())
            return False


class MainWindow(QMainWindow):
    def __init__(self, ctrl: Controller):
        super().__init__()
        self.ctrl = ctrl

        # bufory logÃ³w
        self.scalper_log_buffer = deque()
        self.lowcap_log_buffer = deque()
        self.grid_bot_log_buffer = deque()

        # callbacki â€“ tu podpinamy GUI do botÃ³w
        self.ctrl.scalper.log_callback = self.gui_log_scalper
        self.ctrl.lowcap.log_callback = self.gui_log_lowcap
        self.ctrl.grid_bot.log_callback = self.gui_log_grid_bot

        self.setWindowTitle("GATEBOT â€“ Silniki Tradingowe")
        self.resize(1300, 900)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QGridLayout()
        central.setLayout(layout)

        # --- BUTTONS ---
        self.scalper_label = QLabel("Highcap Scalper")
        self.scalper_status = QLabel("not running")
        self.scalper_status.setStyleSheet("color: red;")
        self.scalper_btn = QPushButton("START")
        self.scalper_btn.clicked.connect(self.on_toggle_scalper)

        layout.addWidget(self.scalper_label, 0, 0)
        layout.addWidget(self.scalper_btn, 0, 1)
        layout.addWidget(self.scalper_status, 0, 2)

        self.lowcap_label = QLabel("Micro-Reversion")
        self.lowcap_status = QLabel("not running")
        self.lowcap_status.setStyleSheet("color: red;")
        self.lowcap_btn = QPushButton("START")
        self.lowcap_btn.clicked.connect(self.on_toggle_lowcap)

        layout.addWidget(self.lowcap_label, 1, 0)
        layout.addWidget(self.lowcap_btn, 1, 1)
        layout.addWidget(self.lowcap_status, 1, 2)

        self.grid_bot_label = QLabel("Grid Bot")
        self.grid_bot_status = QLabel("not running")
        self.grid_bot_status.setStyleSheet("color: red;")
        self.grid_bot_btn = QPushButton("START")
        self.grid_bot_btn.clicked.connect(self.on_toggle_grid_bot)

        layout.addWidget(self.grid_bot_label, 2, 0)
        layout.addWidget(self.grid_bot_btn, 2, 1)
        layout.addWidget(self.grid_bot_status, 2, 2)

        # --- FRAME FOR LOGS ---
        self.logs_frame = QFrame()
        self.logs_frame.setFrameShape(QFrame.Shape.Box)
        self.logs_frame.setLineWidth(2)
        frame_layout = QGridLayout()
        self.logs_frame.setLayout(frame_layout)
        layout.addWidget(self.logs_frame, 3, 0, 1, 3)

        # --- PROFIT LABELS ---
        self.scalper_profit_label = QLabel("0.00% | 0.00$")
        self.lowcap_profit_label = QLabel("0.00% | 0.00$")
        self.grid_bot_profit_label = QLabel("0.00% | 0.00$")

        # --- SCALPER LOG ---
        self.scalper_title = QLabel("Highcap Scalper â€“ log transakcji")
        frame_layout.addWidget(self.scalper_title, 0, 0)
        frame_layout.addWidget(self.scalper_profit_label, 0, 2, alignment=Qt.AlignmentFlag.AlignRight)

        self.scalper_status_label = QLabel("ðŸ” Oczekiwanieâ€¦")
        frame_layout.addWidget(self.scalper_status_label, 1, 0, 1, 3)

        self.scalper_box = QTextEdit()
        self.scalper_box.setReadOnly(True)
        frame_layout.addWidget(self.scalper_box, 2, 0, 1, 3)

        # --- LOWCAP LOG ---
        self.lowcap_title = QLabel("Micro-Reversion â€“ log transakcji")
        frame_layout.addWidget(self.lowcap_title, 3, 0)
        frame_layout.addWidget(self.lowcap_profit_label, 3, 2, alignment=Qt.AlignmentFlag.AlignRight)

        self.lowcap_status_label = QLabel("ðŸ” Oczekiwanieâ€¦")
        frame_layout.addWidget(self.lowcap_status_label, 4, 0, 1, 3)

        self.lowcap_box = QTextEdit()
        self.lowcap_box.setReadOnly(True)
        frame_layout.addWidget(self.lowcap_box, 5, 0, 1, 3)

        # --- grid_bot LOG ---
        self.grid_bot_title = QLabel("Grid Bot â€“ log transakcji")
        frame_layout.addWidget(self.grid_bot_title, 6, 0)
        frame_layout.addWidget(self.grid_bot_profit_label, 6, 2, alignment=Qt.AlignmentFlag.AlignRight)

        self.grid_bot_status_label = QLabel("ðŸ” Oczekiwanieâ€¦")
        frame_layout.addWidget(self.grid_bot_status_label, 7, 0, 1, 3)

        self.grid_bot_box = QTextEdit()
        self.grid_bot_box.setReadOnly(True)
        frame_layout.addWidget(self.grid_bot_box, 8, 0, 1, 3)

        layout.setColumnStretch(0, 2)
        layout.setRowStretch(3, 1)

        # --- TIMER ---
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.flush_logs)
        self.timer.timeout.connect(self.update_total_profit)
        self.timer.start(300)

    # --- CALLBACKI ---
    def gui_log_scalper(self, msg: str):
        if msg.startswith("STATUS_UPDATE::"):
            self.scalper_status_label.setText(msg.replace("STATUS_UPDATE::", ""))
        else:
            self.scalper_log_buffer.append(msg)

    def gui_log_lowcap(self, msg: str):
        if msg.startswith("STATUS_UPDATE::"):
            self.lowcap_status_label.setText(msg.replace("STATUS_UPDATE::", ""))
        else:
            self.lowcap_log_buffer.append(msg)

    def gui_log_grid_bot(self, msg: str):
        if msg.startswith("STATUS_UPDATE::"):
            self.grid_bot_status_label.setText(msg.replace("STATUS_UPDATE::", ""))
        else:
            self.grid_bot_log_buffer.append(msg)

    # --- FLUSH LOGÃ“W ---
    def flush_logs(self):
        self._flush_buffer_to_widget(self.scalper_log_buffer, self.scalper_box)
        self._flush_buffer_to_widget(self.lowcap_log_buffer, self.lowcap_box)
        self._flush_buffer_to_widget(self.grid_bot_log_buffer, self.grid_bot_box)

    def _flush_buffer_to_widget(self, buffer: deque, widget: QTextEdit):
        if not buffer:
            return

        scrollbar = widget.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 20

        # Insert via document cursor â€” does not move the widget's visible cursor/scroll
        cursor = QTextCursor(widget.document())

        max_logs = 10
        processed = 0

        widget.blockSignals(True)

        while buffer and processed < max_logs:
            msg = buffer.popleft()
            processed += 1

            if msg.startswith("STATUS_UPDATE::"):
                continue

            cursor.movePosition(QTextCursor.MoveOperation.End)

            fmt = QTextCharFormat()
            if msg.startswith("âœ…"):
                fmt.setForeground(QColor("green"))
            elif msg.startswith("âŒ"):
                fmt.setForeground(QColor("red"))
            else:
                fmt.setForeground(QColor("white"))

            ts = datetime.now().strftime("%H:%M:%S")
            cursor.insertText(f"[{ts}] {msg}\n", fmt)

        widget.blockSignals(False)

        # Only auto-scroll if user was already at the bottom
        if was_at_bottom:
            widget.setTextCursor(cursor)
            widget.ensureCursorVisible()

    # --- SUMARYCZNY PROFIT ---
    def update_total_profit(self):
        total = getattr(self.ctrl.scalper, "realized_profit", 0.0)
        pct = (total / self.ctrl.scalper.cfg.start_balance) * 100 if total != 0 else 0
        self.scalper_profit_label.setText(f"{pct:.2f}% | {total:.2f}$")
        self.scalper_profit_label.setStyleSheet("color: green;" if total >= 0 else "color: red;")

        total = self.ctrl.lowcap.total_session_profit
        pct = (total / self.ctrl.lowcap.cfg.start_balance) * 100 if total != 0 else 0
        self.lowcap_profit_label.setText(f"{pct:.2f}% | {total:.2f}$")
        self.lowcap_profit_label.setStyleSheet("color: green;" if total >= 0 else "color: red;")

        total = getattr(self.ctrl.grid_bot, "realized_profit", 0.0)
        pct = (total / self.ctrl.grid_bot.cfg.start_balance) * 100 if total != 0 else 0
        self.grid_bot_profit_label.setText(f"{pct:.2f}% | {total:.2f}$")
        self.grid_bot_profit_label.setStyleSheet("color: green;" if total >= 0 else "color: red;")

    # --- BUTTONY ---
    def on_toggle_scalper(self):
        running = self.ctrl.toggle_scalper()
        self.scalper_btn.setText("STOP" if running else "START")
        self.scalper_status.setText("running" if running else "not running")
        self.scalper_status.setStyleSheet("color: green;" if running else "color: red;")

    def on_toggle_lowcap(self):
        running = self.ctrl.toggle_lowcap()
        self.lowcap_btn.setText("STOP" if running else "START")
        self.lowcap_status.setText("running" if running else "not running")
        self.lowcap_status.setStyleSheet("color: green;" if running else "color: red;")

    def on_toggle_grid_bot(self):
        running = self.ctrl.toggle_grid_bot()
        self.grid_bot_btn.setText("STOP" if running else "START")
        self.grid_bot_status.setText("running" if running else "not running")
        self.grid_bot_status.setStyleSheet("color: green;" if running else "color: red;")

    # --- ZAMKNIÄ˜CIE ---
    def closeEvent(self, event):
        # Stop timer first â€” prevents flush_logs firing on destroyed widgets
        self.timer.stop()

        # Disconnect log callbacks so daemon threads don't touch dead Qt objects
        self.ctrl.scalper.log_callback = None
        self.ctrl.lowcap.log_callback = None
        self.ctrl.grid_bot.log_callback = None

        # Stop engines (best-effort â€” daemon threads die with the process anyway)
        if self.ctrl.scalper.running:
            self.ctrl.run_async(self.ctrl.scalper.stop())
        if self.ctrl.lowcap.running:
            self.ctrl.lowcap.stop()
        if self.ctrl.grid_bot.running:
            self.ctrl.run_async(self.ctrl.grid_bot.stop())
        if self.ctrl.market_data.running:
            self.ctrl.run_async(self.ctrl.market_data.stop())

        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    ctrl = Controller()
    window = MainWindow(ctrl)
    window.show()
    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        pass


