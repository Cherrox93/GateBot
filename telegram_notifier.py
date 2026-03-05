# telegram_notifier.py
import asyncio
import threading
import time

import requests
import urllib3

# Suppress InsecureRequestWarning for self-signed corporate proxies
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TG_TIMEOUT = 10
_POLL_TIMEOUT = 30


def _tg_post(base_url: str, method: str, payload: dict) -> dict | None:
    try:
        r = requests.post(
            f"{base_url}/{method}",
            json=payload,
            timeout=_TG_TIMEOUT,
            verify=False,
        )
        return r.json()
    except Exception:
        return None


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self._base = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self._running = False

    # ============================
    #   HTTP helpers
    # ============================
    def _post_sync(self, method: str, payload: dict):
        return _tg_post(self._base, method, payload)

    def get_bot_info(self) -> str:
        """Returns '@username' or error string. Use on startup to confirm bot identity."""
        try:
            r = requests.get(f"{self._base}/getMe", timeout=_TG_TIMEOUT, verify=False)
            data = r.json()
            if data.get("ok"):
                u = data["result"]
                return f"@{u['username']} (id={u['id']})"
            return f"[getMe failed: {data.get('description')}]"
        except Exception as e:
            return f"[getMe error: {e}]"

    def _get_updates_sync(self):
        try:
            r = requests.get(
                f"{self._base}/getUpdates",
                params={
                    "offset": self._offset,
                    "timeout": 25,
                    "allowed_updates": ["message"],
                },
                timeout=_POLL_TIMEOUT,
                verify=False,
            )
            return r.json()
        except Exception:
            return None

    # ============================
    #   Send
    # ============================
    def send_sync(self, text: str):
        """Blocking send — use from sync/thread context."""
        self._post_sync("sendMessage", {
            "chat_id": int(self.chat_id),
            "text": text,
        })

    async def send(self, text: str):
        """Non-blocking send — use from async context."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.send_sync, text)

    # ============================
    #   Async polling (scalper / pump)
    # ============================
    async def start_polling_async(self, command_handler):
        """Long-polling coroutine — run as asyncio.create_task."""
        self._running = True
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                data = await loop.run_in_executor(None, self._get_updates_sync)
                if data and data.get("ok"):
                    for update in data.get("result", []):
                        self._offset = update["update_id"] + 1
                        msg = update.get("message", {})
                        text = msg.get("text", "")
                        from_chat = str(msg.get("chat", {}).get("id", ""))
                        if from_chat == self.chat_id and text.startswith("/"):
                            response = command_handler(text.strip())
                            if response:
                                await loop.run_in_executor(
                                    None, self.send_sync, response
                                )
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

    # ============================
    #   Sync polling (lowcap — in daemon thread)
    # ============================
    def start_polling_thread(self, command_handler):
        """Starts long-polling in a daemon thread. Returns thread."""
        self._running = True
        t = threading.Thread(
            target=self._poll_loop, args=(command_handler,), daemon=True
        )
        t.start()
        return t

    def _poll_loop(self, command_handler):
        while self._running:
            try:
                data = self._get_updates_sync()
                if data and data.get("ok"):
                    for update in data.get("result", []):
                        self._offset = update["update_id"] + 1
                        msg = update.get("message", {})
                        text = msg.get("text", "")
                        from_chat = str(msg.get("chat", {}).get("id", ""))
                        if from_chat == self.chat_id and text.startswith("/"):
                            response = command_handler(text.strip())
                            if response:
                                self.send_sync(response)
            except Exception:
                time.sleep(5)

    def stop_polling(self):
        self._running = False
