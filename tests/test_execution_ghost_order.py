"""Regression tests for ghost-order detection on submit failure (E6).

When `post_order` returns a malformed response with no order ID, the order
may still be live on the exchange. `place_limit_gtd_then_wait` must do
exactly one CLOB open-orders query and attribute by intent fingerprint
(token_id + side + price + size). See bot/execution.py."""

from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock

from bot import execution
from bot.execution import (
    _INFLIGHT_KEYS,
    _find_matching_open_order,
    place_limit_gtd_then_wait,
)


def _open_order_row(
    *,
    oid: str,
    token_id: str = "tok-A",
    side: str = "BUY",
    price: float = 0.55,
    size: float = 10.0,
    status: str = "LIVE",
) -> dict:
    """Shape of an open-order row as returned by py_clob_client.get_orders."""
    return {
        "id": oid,
        "asset_id": token_id,
        "side": side,
        "price": price,
        "original_size": size,
        "size_matched": 0.0,
        "status": status,
    }


class TestFindMatchingOpenOrder(unittest.TestCase):
    """Direct unit tests for the helper — independent of the async wrapper."""

    def test_exact_single_match_returns_id(self) -> None:
        client = MagicMock()
        client.get_orders.return_value = [
            _open_order_row(oid="oid-target"),
        ]
        oid, kind = _find_matching_open_order(
            client, token_id="tok-A", side="BUY", price=0.55, size=10.0,
        )
        self.assertEqual(kind, "exact")
        self.assertEqual(oid, "oid-target")

    def test_two_exact_matches_are_ambiguous(self) -> None:
        client = MagicMock()
        client.get_orders.return_value = [
            _open_order_row(oid="oid-1"),
            _open_order_row(oid="oid-2"),
        ]
        oid, kind = _find_matching_open_order(
            client, token_id="tok-A", side="BUY", price=0.55, size=10.0,
        )
        self.assertEqual(kind, "ambiguous")
        self.assertIsNone(oid)

    def test_no_rows_returns_none(self) -> None:
        client = MagicMock()
        client.get_orders.return_value = []
        oid, kind = _find_matching_open_order(
            client, token_id="tok-A", side="BUY", price=0.55, size=10.0,
        )
        self.assertEqual(kind, "none")
        self.assertIsNone(oid)

    def test_unrelated_token_is_filtered(self) -> None:
        client = MagicMock()
        client.get_orders.return_value = [
            _open_order_row(oid="oid-other", token_id="tok-OTHER"),
        ]
        oid, kind = _find_matching_open_order(
            client, token_id="tok-A", side="BUY", price=0.55, size=10.0,
        )
        # Different token entirely — not a near-miss, just irrelevant.
        self.assertEqual(kind, "none")
        self.assertIsNone(oid)

    def test_same_token_different_price_is_ambiguous_near_miss(self) -> None:
        client = MagicMock()
        client.get_orders.return_value = [
            _open_order_row(oid="oid-different-price", price=0.60),
        ]
        oid, kind = _find_matching_open_order(
            client, token_id="tok-A", side="BUY", price=0.55, size=10.0,
        )
        # Same token+side, very different price — we refuse to attribute.
        self.assertEqual(kind, "ambiguous")
        self.assertIsNone(oid)

    def test_get_orders_exception_returns_none(self) -> None:
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("network down")
        oid, kind = _find_matching_open_order(
            client, token_id="tok-A", side="BUY", price=0.55, size=10.0,
        )
        self.assertEqual(kind, "none")
        self.assertIsNone(oid)


class TestGhostOrderRecoveryOnSubmit(unittest.IsolatedAsyncioTestCase):
    """End-to-end async tests for the recovery path in place_limit_gtd_then_wait."""

    def setUp(self) -> None:
        _INFLIGHT_KEYS.clear()

    def tearDown(self) -> None:
        _INFLIGHT_KEYS.clear()

    async def test_recovers_via_query_when_post_returns_no_id(self) -> None:
        """post_order returns no id; get_orders returns exactly one match → recovered."""
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        # Malformed response — no orderID field anywhere.
        client.post_order.return_value = {"some_other_field": "noise"}
        client.get_orders.return_value = [
            _open_order_row(oid="oid-ghost", price=0.55, size=10.0),
        ]

        oid, note = await place_limit_gtd_then_wait(
            client,
            token_id="tok-A",
            side="BUY",
            price=0.55,
            size=10.0,
            ttl_seconds=1,
            poll_seconds=0.1,
            dry_run=False,
        )
        self.assertEqual(oid, "oid-ghost")
        self.assertEqual(note, "post_recovered_via_query")
        # We queried open orders exactly once — no retry storm.
        self.assertEqual(client.get_orders.call_count, 1)
        # We did NOT enter the local polling loop; reconcile owns the lifecycle.
        client.get_order.assert_not_called()
        client.cancel.assert_not_called()

    async def test_ambiguous_when_two_matches(self) -> None:
        """post_order returns no id; get_orders returns two exact matches → ambiguous."""
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.return_value = {}
        client.get_orders.return_value = [
            _open_order_row(oid="oid-1"),
            _open_order_row(oid="oid-2"),
        ]

        with self.assertLogs("polymarket.execution", level="WARNING") as cm:
            oid, note = await place_limit_gtd_then_wait(
                client,
                token_id="tok-A",
                side="BUY",
                price=0.55,
                size=10.0,
                ttl_seconds=1,
                poll_seconds=0.1,
                dry_run=False,
            )
        self.assertIsNone(oid)
        self.assertEqual(note, "post_ambiguous")
        # WARNING was logged for ops visibility.
        self.assertTrue(
            any("ambiguous" in msg.lower() for msg in cm.output),
            f"expected an 'ambiguous' WARNING; got {cm.output!r}",
        )
        # Lifecycle not touched.
        client.get_order.assert_not_called()
        client.cancel.assert_not_called()

    async def test_no_match_preserves_today_behavior(self) -> None:
        """post_order returns no id; get_orders returns nothing → today's no_order_id behavior."""
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.return_value = {}
        client.get_orders.return_value = []

        oid, note = await place_limit_gtd_then_wait(
            client,
            token_id="tok-A",
            side="BUY",
            price=0.55,
            size=10.0,
            ttl_seconds=1,
            poll_seconds=0.1,
            dry_run=False,
        )
        self.assertIsNone(oid)
        self.assertEqual(note, "post_failed:no_order_id")
        # No polling, no cancel — exactly today's behavior.
        client.get_order.assert_not_called()
        client.cancel.assert_not_called()

    async def test_recovery_succeeds_when_post_returns_unrelated_fields(self) -> None:
        """A nested 'order' object missing the id should still trigger recovery."""
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.return_value = {"order": {"status": "LIVE"}}
        client.get_orders.return_value = [
            _open_order_row(oid="oid-recovered-from-nested"),
        ]

        oid, note = await place_limit_gtd_then_wait(
            client,
            token_id="tok-A",
            side="BUY",
            price=0.55,
            size=10.0,
            ttl_seconds=1,
            poll_seconds=0.1,
            dry_run=False,
        )
        self.assertEqual(oid, "oid-recovered-from-nested")
        self.assertEqual(note, "post_recovered_via_query")

    async def test_inflight_key_cleared_after_recovery(self) -> None:
        """The idempotency in-flight key is removed even on the recovery path."""
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.return_value = {}
        client.get_orders.return_value = [
            _open_order_row(oid="oid-recovered"),
        ]
        intent = {
            "intent_id": "intent-recover",
            "token_id": "tok-A",
            "side": "BUY",
            "price": 0.55,
            "size_usd": 5.5,
        }
        oid, note = await place_limit_gtd_then_wait(
            client,
            token_id="tok-A",
            side="BUY",
            price=0.55,
            size=10.0,
            ttl_seconds=1,
            poll_seconds=0.1,
            dry_run=False,
            intent=intent,
            idempotency_time_bucket=10_000_000,
        )
        self.assertEqual(oid, "oid-recovered")
        self.assertEqual(note, "post_recovered_via_query")
        self.assertEqual(_INFLIGHT_KEYS, set())


if __name__ == "__main__":
    unittest.main()
