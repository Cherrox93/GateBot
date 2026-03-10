from __future__ import annotations

from typing import Dict, Iterable, Tuple


def _iter_usdt_pairs(market_cache: Dict) -> Iterable[Tuple[str, dict]]:
    for symbol, data in market_cache.items():
        if not isinstance(symbol, str) or not isinstance(data, dict):
            continue
        if not symbol.endswith("/USDT"):
            continue
        if ":" in symbol or "-" in symbol:
            continue
        yield symbol, data


def select_grid_pairs(market_cache: Dict, limit: int = 200) -> list[str]:
    """
    USDT pairs with volume > 500k.
    """
    pairs = []
    for symbol, t in _iter_usdt_pairs(market_cache):
        vol = float(t.get("quoteVolume") or 0.0)
        if vol > 500_000:
            pairs.append((vol, symbol))
    pairs.sort(reverse=True)
    return [sym for _, sym in pairs[:limit]]
