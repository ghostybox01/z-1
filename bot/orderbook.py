"""Order-book depth check (assistant-tool style): bid vs ask notional imbalance for BUY."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("polymarket.orderbook")


def best_bid_ask(clob: Any, token_id: str) -> tuple[float | None, float | None]:
    """
    Best bid (highest) and best ask (lowest) from CLOB book. On failure returns (None, None).
    """
    try:
        book = clob.get_order_book(token_id)
    except Exception as e:
        log.debug("get_order_book %s: %s", token_id[:12], e)
        return None, None
    bids = getattr(book, "bids", None) or []
    asks = getattr(book, "asks", None) or []
    best_b: float | None = None
    best_a: float | None = None
    for lv in bids:
        try:
            p = float(getattr(lv, "price", None) or 0)
            if p <= 0:
                continue
            best_b = p if best_b is None else max(best_b, p)
        except (TypeError, ValueError):
            continue
    for lv in asks:
        try:
            p = float(getattr(lv, "price", None) or 0)
            if p <= 0:
                continue
            best_a = p if best_a is None else min(best_a, p)
        except (TypeError, ValueError):
            continue
    return best_b, best_a


def spread_mid_bps(clob: Any, token_id: str) -> float | None:
    """
    (ask - bid) / mid in bps where mid = (bid+ask)/2. None if book incomplete.
    """
    bid, ask = best_bid_ask(clob, token_id)
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 1e-9:
        return None
    return (ask - bid) / mid * 10000.0


def _sum_notional(levels: list[Any] | None) -> float:
    if not levels:
        return 0.0
    s = 0.0
    for lv in levels:
        try:
            p = float(getattr(lv, "price", None) or 0)
            sz = float(getattr(lv, "size", None) or 0)
            s += abs(p * sz)
        except (TypeError, ValueError):
            continue
    return s


def orderbook_buy_depth_ok(clob: Any, token_id: str, min_bid_share: float) -> bool:
    """
    For BUY support: require bid notional / (bid+ask) >= min_bid_share.
    If the book is empty or the call fails, return True (do not block on API flake).

    NOTE (E10a): this ratio check is preserved for backward compatibility, but
    it does NOT compare available depth against the planned trade notional.
    Small books pass the ratio test even when they cannot absorb the order.
    Prefer `orderbook_depth_ok_for_notional` for new call sites.
    """
    try:
        book = clob.get_order_book(token_id)
    except Exception as e:
        log.debug("get_order_book %s: %s", token_id[:12], e)
        return True
    bids = getattr(book, "bids", None) or []
    asks = getattr(book, "asks", None) or []
    b = _sum_notional(bids)
    a = _sum_notional(asks)
    if b + a < 1e-9:
        return True
    share = b / (b + a)
    return share >= float(min_bid_share)


def orderbook_depth_ok_for_notional(
    clob: Any,
    token_id: str,
    side: str,
    trade_notional_usd: float,
    slippage_band: float = 0.20,
) -> bool:
    """Depth-vs-notional gate (E10a).

    Return True iff the side of the book the trade will sweep contains at
    least `trade_notional_usd * (1 + slippage_band)` of resting notional.
    For BUY we consume the ASK side; for SELL we consume the BID side.

    The previous gate `orderbook_buy_depth_ok` only checks a ratio of bid
    to ask notional, which a thin book can pass even when it cannot
    absorb the order. This function compares actual depth against the
    planned trade size and is the safer gate for sizing decisions.

    Behavior on failure:
      * If the book call raises, return False (conservative — don't pass
        a thin/unknown book just because the API flaked).
      * If the book is empty on the relevant side, return False.
      * If `trade_notional_usd <= 0`, return True (no-op gate).
    """
    if trade_notional_usd <= 0:
        return True
    try:
        book = clob.get_order_book(token_id)
    except Exception as e:
        log.debug(
            "get_order_book %s failed: %s — depth gate False",
            token_id[:12],
            e,
        )
        return False

    side_u = (side or "").upper()
    if side_u == "BUY":
        levels = getattr(book, "asks", None) or []
        relevant = "asks"
    else:
        levels = getattr(book, "bids", None) or []
        relevant = "bids"

    available = _sum_notional(levels)
    required = float(trade_notional_usd) * (1.0 + float(slippage_band))
    ok = available >= required
    if not ok:
        log.debug(
            "depth_gate: token=%s side=%s relevant=%s available=%.4f required=%.4f (notional=%.4f, band=%.2f)",
            token_id[:12],
            side_u,
            relevant,
            available,
            required,
            float(trade_notional_usd),
            float(slippage_band),
        )
    return ok
