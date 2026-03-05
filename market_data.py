# market_data.py
import asyncio
import json
import urllib.request
import websockets
from datetime import datetime


class MarketData:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.last_prices = {}   # "BTC_USDT" -> float
        self.ticker_data = {}   # "BTC/USDT" -> {"last": float, "quoteVolume": float, "percentage": float}
        self.running = False
        self.url = "wss://api.gateio.ws/ws/v4/"
        self.ws_task = None
        self._all_ws_symbols = []

    def log(self, msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def get_last_price(self, symbol: str):
        if not symbol:
            return None
        return self.last_prices.get(symbol.replace("/", "_"))

    def get_all_tickers(self) -> dict:
        """Zwraca ticker_data w formacie kompatybilnym z ccxt (klucze BTC/USDT)."""
        return self.ticker_data

    def get_ask_price(self, symbol: str) -> float | None:
        """Returns real buy price (ask). Falls back to last * 1.001 if ask unavailable."""
        data = self.ticker_data.get(symbol, {})
        ask = data.get("ask", 0)
        if ask and ask > 0:
            return ask
        last = self.get_last_price(symbol)
        return last * 1.001 if last else None

    def get_bid_price(self, symbol: str) -> float | None:
        """Returns real sell price (bid). Falls back to last * 0.999 if bid unavailable."""
        data = self.ticker_data.get(symbol, {})
        bid = data.get("bid", 0)
        if bid and bid > 0:
            return bid
        last = self.get_last_price(symbol)
        return last * 0.999 if last else None

    def get_spread_pct(self, symbol: str) -> float:
        """Returns bid/ask spread as fraction. Returns 0.01 (1%) if data unavailable."""
        data = self.ticker_data.get(symbol, {})
        bid = data.get("bid", 0)
        ask = data.get("ask", 0)
        if bid and ask and bid > 0:
            return (ask - bid) / bid
        return 0.01

    async def _fetch_all_usdt_symbols(self) -> list:
        """Pobiera wszystkie aktywne pary USDT z REST API (jednorazowo, w wątku)."""
        def _sync_fetch():
            url = "https://api.gateio.ws/api/v4/spot/currency_pairs"
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read())

        try:
            loop = asyncio.get_running_loop()
            pairs = await loop.run_in_executor(None, _sync_fetch)
            result = [
                p["id"] for p in pairs
                if p.get("quote") == "USDT" and p.get("trade_status") == "tradable"
            ]
            self.log(f"MarketData: znaleziono {len(result)} par USDT")
            return result
        except Exception as e:
            self.log(f"MarketData: błąd pobierania par: {e}")
            return []

    async def _runner(self):
        self.log("MarketData: start...")

        self._all_ws_symbols = await self._fetch_all_usdt_symbols()
        if not self._all_ws_symbols:
            self.log("MarketData: brak par – WebSocket nie startuje")
            return

        while self.running:
            try:
                # ping_interval=None — Gate.io doesn't respond to raw WS pings;
                # we send Gate.io's own channel ping manually every 20s instead
                async with websockets.connect(self.url, ping_interval=None) as ws:

                    batch_size = 100
                    total = len(self._all_ws_symbols)
                    for i in range(0, total, batch_size):
                        batch = self._all_ws_symbols[i:i + batch_size]
                        msg = {
                            "time": int(datetime.now().timestamp()),
                            "channel": "spot.tickers",
                            "event": "subscribe",
                            "payload": batch
                        }
                        await ws.send(json.dumps(msg))

                    self.log(f"MarketData: subskrypcja wysłana ({total} par, {(total + 99) // 100} partii)")

                    async def _keepalive():
                        while True:
                            await asyncio.sleep(20)
                            try:
                                await ws.send(json.dumps({
                                    "time": int(datetime.now().timestamp()),
                                    "channel": "spot.ping"
                                }))
                            except Exception:
                                break

                    keepalive_task = asyncio.create_task(_keepalive())

                    try:
                        first_data = True
                        async for response in ws:
                            if not self.running:
                                break

                            data = json.loads(response)

                            if data.get("error"):
                                self.log(f"MarketData WS błąd: {data['error']}")
                                continue

                            if data.get("channel") != "spot.tickers":
                                continue

                            results = data.get("result")
                            if not results:
                                continue

                            if isinstance(results, dict):
                                results = [results]

                            for ticker in results:
                                ws_sym = ticker.get("currency_pair")
                                price_raw = ticker.get("last")
                                if not ws_sym or not price_raw:
                                    continue
                                try:
                                    price = float(price_raw)
                                    self.last_prices[ws_sym] = price

                                    ccxt_sym = ws_sym.replace("_", "/")

                                    bid_raw = ticker.get("highest_bid")
                                    ask_raw = ticker.get("lowest_ask")
                                    bid = float(bid_raw) if bid_raw else 0.0
                                    ask = float(ask_raw) if ask_raw else 0.0

                                    self.ticker_data[ccxt_sym] = {
                                        "last": price,
                                        "bid": bid,
                                        "ask": ask,
                                        "quoteVolume": float(ticker.get("quote_volume") or 0),
                                        "percentage": float(ticker.get("change_percentage") or 0),
                                    }
                                except Exception:
                                    continue

                            if first_data and self.last_prices:
                                self.log(f"MarketData: pierwsze ceny odebrane ({len(self.last_prices)} par)")
                                first_data = False
                    finally:
                        keepalive_task.cancel()

            except websockets.exceptions.ConnectionClosed:
                # Normal server-side disconnect — reconnect silently
                if self.running:
                    await asyncio.sleep(3)
            except Exception as e:
                self.log(f"MarketData WS błąd: {e} | reconnect za 5s")
                await asyncio.sleep(5)

        self.log("MarketData: zatrzymane.")

    async def start(self):
        if self.running:
            return
        self.running = True
        self.ws_task = asyncio.create_task(self._runner())

    async def stop(self):
        self.running = False
        if self.ws_task:
            self.ws_task.cancel()
            try:
                await self.ws_task
            except Exception:
                pass
            self.ws_task = None
