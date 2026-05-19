"""Depth-vs-notional gate (E10a).

The legacy `orderbook_buy_depth_ok` checks a ratio of bid to ask notional —
a thin book can pass even when it cannot absorb the planned trade.
`orderbook_depth_ok_for_notional` instead compares the SIDE we'll sweep
against the trade notional plus a configurable slippage band.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from bot.orderbook import orderbook_depth_ok_for_notional


class FakeBook:
    def __init__(self, bids, asks):
        self.bids = bids
        self.asks = asks


class FakeClob:
    def __init__(self, book):
        self._book = book

    def get_order_book(self, token_id):
        return self._book


class ErroringClob:
    def get_order_book(self, token_id):
        raise RuntimeError("api flake")


def _book_with_ask_notional(notional_usd: float) -> FakeBook:
    """Ask side: one level at price=0.50 sized to produce the requested
    notional (price * size). Bid side empty for clarity."""
    price = 0.50
    size = notional_usd / price
    return FakeBook(
        bids=[],
        asks=[SimpleNamespace(price=str(price), size=str(size))],
    )


def _book_with_bid_notional(notional_usd: float) -> FakeBook:
    price = 0.50
    size = notional_usd / price
    return FakeBook(
        bids=[SimpleNamespace(price=str(price), size=str(size))],
        asks=[],
    )


class TestDepthOkForNotional(unittest.TestCase):
    def test_buy_book_has_10_trade_5_passes(self):
        """$10 ask depth, $5 trade with default 20% band → required $6 → OK."""
        clob = FakeClob(_book_with_ask_notional(10.0))
        self.assertTrue(
            orderbook_depth_ok_for_notional(
                clob, "tok", "BUY", trade_notional_usd=5.0
            )
        )

    def test_buy_book_has_10_trade_9_fails(self):
        """$10 ask depth, $9 trade with 20% band → required $10.80 → FAIL."""
        clob = FakeClob(_book_with_ask_notional(10.0))
        self.assertFalse(
            orderbook_depth_ok_for_notional(
                clob, "tok", "BUY", trade_notional_usd=9.0
            )
        )

    def test_empty_book_fails(self):
        """Empty asks → cannot absorb a BUY → FAIL (conservative)."""
        clob = FakeClob(FakeBook(bids=[], asks=[]))
        self.assertFalse(
            orderbook_depth_ok_for_notional(
                clob, "tok", "BUY", trade_notional_usd=1.0
            )
        )

    def test_sell_uses_bid_side(self):
        """SELL consumes BIDS — verify the side selector wires correctly."""
        clob = FakeClob(_book_with_bid_notional(10.0))
        # Trade $5 SELL → required $6 (20% band) → bids have $10 → OK.
        self.assertTrue(
            orderbook_depth_ok_for_notional(
                clob, "tok", "SELL", trade_notional_usd=5.0
            )
        )
        # And confirm BUY against the same book (which has empty asks) FAILS.
        self.assertFalse(
            orderbook_depth_ok_for_notional(
                clob, "tok", "BUY", trade_notional_usd=5.0
            )
        )

    def test_api_flake_returns_false(self):
        """A raising get_order_book is treated as 'unknown book' →
        conservative FALSE. Unlike the legacy ratio gate which returned True
        on API failure, we don't trust an unknown book for a sizing
        decision."""
        self.assertFalse(
            orderbook_depth_ok_for_notional(
                ErroringClob(), "tok", "BUY", trade_notional_usd=1.0
            )
        )

    def test_zero_notional_is_noop(self):
        """trade_notional_usd <= 0 is treated as no-op (pass) — the caller
        is not actually placing an order."""
        clob = FakeClob(FakeBook(bids=[], asks=[]))
        self.assertTrue(
            orderbook_depth_ok_for_notional(
                clob, "tok", "BUY", trade_notional_usd=0.0
            )
        )

    def test_band_zero_uses_exact_notional(self):
        """slippage_band=0 → required == notional exactly."""
        clob = FakeClob(_book_with_ask_notional(10.0))
        self.assertTrue(
            orderbook_depth_ok_for_notional(
                clob, "tok", "BUY", trade_notional_usd=10.0, slippage_band=0.0
            )
        )
        self.assertFalse(
            orderbook_depth_ok_for_notional(
                clob, "tok", "BUY", trade_notional_usd=10.01, slippage_band=0.0
            )
        )


class TestLegacyGateUnchanged(unittest.TestCase):
    """The legacy ratio gate is preserved for backward compatibility. We
    don't rewire existing call sites in this commit."""

    def test_legacy_function_still_importable(self):
        from bot.orderbook import orderbook_buy_depth_ok  # noqa: F401


if __name__ == "__main__":
    unittest.main()
