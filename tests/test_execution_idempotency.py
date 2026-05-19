"""Regression tests for client-side idempotency in bot.execution (E5)."""

from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import MagicMock

from bot import execution
from bot.execution import (
    _INFLIGHT_KEYS,
    _intent_idempotency_key,
    place_limit_gtd_then_wait,
    place_market_fok_fallback,
)


def _make_intent(intent_id: str, token_id: str = "tok-A", side: str = "BUY",
                 price: float = 0.55, size_usd: float = 10.0) -> dict:
    return {
        "intent_id": intent_id,
        "token_id": token_id,
        "side": side,
        "price": price,
        "size_usd": size_usd,
    }


class TestIdempotencyKey(unittest.TestCase):
    def setUp(self) -> None:
        _INFLIGHT_KEYS.clear()

    def test_same_intent_same_bucket_same_key(self) -> None:
        intent = _make_intent("i-1")
        k1 = _intent_idempotency_key(intent, time_bucket=10_000_000)
        k2 = _intent_idempotency_key(intent, time_bucket=10_000_000)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 16)

    def test_different_intents_different_keys(self) -> None:
        k1 = _intent_idempotency_key(_make_intent("i-1"), time_bucket=10_000_000)
        k2 = _intent_idempotency_key(_make_intent("i-2"), time_bucket=10_000_000)
        self.assertNotEqual(k1, k2)

    def test_different_size_different_keys(self) -> None:
        a = _intent_idempotency_key(_make_intent("i-1", size_usd=10.0), time_bucket=10_000_000)
        b = _intent_idempotency_key(_make_intent("i-1", size_usd=20.0), time_bucket=10_000_000)
        self.assertNotEqual(a, b)


class TestPlaceLimitIdempotencyBlocksDuplicate(unittest.IsolatedAsyncioTestCase):
    """Second submit with a same-intent key already in flight is rejected
    before any underlying post_order fires."""

    def setUp(self) -> None:
        _INFLIGHT_KEYS.clear()

    def tearDown(self) -> None:
        _INFLIGHT_KEYS.clear()

    async def test_duplicate_inflight_blocks_post(self) -> None:
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.return_value = {"orderID": "oid-should-not-fire"}
        client.get_order.return_value = {
            "status": "FILLED", "size_matched": "10", "original_size": "10",
        }

        intent = _make_intent("intent-abc")

        # Pre-seed the in-flight set as if a prior call were mid-post.
        # This is exactly the state that exists between
        # `_INFLIGHT_KEYS.add(key)` and the `finally: discard(key)` block.
        # The intent already carries size_usd, so the function will use it
        # verbatim (setdefault is a no-op when key present).
        key = _intent_idempotency_key(
            dict(intent),
            time_bucket=10_000_000,
        )
        _INFLIGHT_KEYS.add(key)
        try:
            oid, note = await place_limit_gtd_then_wait(
                client,
                token_id=intent["token_id"],
                side=intent["side"],
                price=intent["price"],
                size=10.0,
                ttl_seconds=1,
                poll_seconds=0.1,
                dry_run=False,
                intent=intent,
                idempotency_time_bucket=10_000_000,
            )
        finally:
            _INFLIGHT_KEYS.discard(key)

        self.assertIsNone(oid)
        self.assertEqual(note, "idempotency_inflight")
        # No underlying create_order / post_order calls happened.
        client.create_order.assert_not_called()
        client.post_order.assert_not_called()

    async def test_key_is_inflight_during_post_and_cleared_after(self) -> None:
        """Confirms the lifecycle: the idempotency key sits in _INFLIGHT_KEYS
        for the entire duration of post_order, so a concurrent retry would
        see it; the key is removed after the post returns."""
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}

        observed_keys_during_post: list = []
        intent = _make_intent("intent-lifecycle")
        expected_key = _intent_idempotency_key(dict(intent), time_bucket=10_000_000)

        def post_order_side_effect(_signed, _ot):
            # Snapshot membership while we're "in flight".
            observed_keys_during_post.append(expected_key in _INFLIGHT_KEYS)
            return {"orderID": "oid-1"}

        client.post_order.side_effect = post_order_side_effect
        client.get_order.return_value = {
            "status": "FILLED", "size_matched": "10", "original_size": "10",
        }

        oid, note = await place_limit_gtd_then_wait(
            client,
            token_id=intent["token_id"],
            side=intent["side"],
            price=intent["price"],
            size=10.0,
            ttl_seconds=1,
            poll_seconds=0.1,
            dry_run=False,
            intent=intent,
            idempotency_time_bucket=10_000_000,
        )
        self.assertEqual(oid, "oid-1")
        self.assertTrue(note.startswith("filled:"))
        # Key was present during post_order, absent afterwards.
        self.assertEqual(observed_keys_during_post, [True])
        self.assertNotIn(expected_key, _INFLIGHT_KEYS)

    async def test_different_intents_both_submit(self) -> None:
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        call_count = {"n": 0}

        def post_order(_s, _o):
            call_count["n"] += 1
            return {"orderID": f"oid-{call_count['n']}"}

        client.post_order.side_effect = post_order
        client.get_order.return_value = {
            "status": "FILLED", "size_matched": "10", "original_size": "10",
        }

        intent1 = _make_intent("intent-1")
        intent2 = _make_intent("intent-2")

        async def call(intent):
            return await place_limit_gtd_then_wait(
                client,
                token_id=intent["token_id"],
                side=intent["side"],
                price=intent["price"],
                size=10.0,
                ttl_seconds=1,
                poll_seconds=0.1,
                dry_run=False,
                intent=intent,
                idempotency_time_bucket=10_000_000,
            )

        r1 = await call(intent1)
        r2 = await call(intent2)
        self.assertEqual(r1[0], "oid-1")
        self.assertEqual(r2[0], "oid-2")
        self.assertEqual(call_count["n"], 2)
        self.assertEqual(_INFLIGHT_KEYS, set())


class TestInflightClearedOnError(unittest.IsolatedAsyncioTestCase):
    """If post_order raises, the in-flight key is still removed via finally."""

    def setUp(self) -> None:
        _INFLIGHT_KEYS.clear()

    async def test_key_cleared_on_post_failure(self) -> None:
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.side_effect = RuntimeError("boom")

        intent = _make_intent("intent-fail")
        oid, note = await place_limit_gtd_then_wait(
            client,
            token_id=intent["token_id"],
            side=intent["side"],
            price=intent["price"],
            size=10.0,
            ttl_seconds=1,
            poll_seconds=0.1,
            dry_run=False,
            intent=intent,
            idempotency_time_bucket=10_000_000,
        )
        self.assertIsNone(oid)
        self.assertTrue(note.startswith("post_failed:"))
        self.assertEqual(_INFLIGHT_KEYS, set())


class TestKeyIsTimeBucketIndependent(unittest.TestCase):
    """The idempotency key MUST NOT depend on wall-clock time bucketing.

    Previously the key included `int(time.time()) // bucket`, which produced
    a boundary race: two retries straddling the bucket boundary computed
    DIFFERENT keys and both proceeded. The hardened version drops the time
    component entirely so the key is purely a function of intent identity.
    """

    def setUp(self) -> None:
        _INFLIGHT_KEYS.clear()

    def test_key_stable_across_arbitrary_bucket_arg(self) -> None:
        intent = _make_intent("i-stable")
        # The `time_bucket` arg is preserved for source compatibility but
        # ignored. Any value MUST produce the same key.
        k_small = _intent_idempotency_key(intent, time_bucket=1)
        k_med = _intent_idempotency_key(intent, time_bucket=60)
        k_large = _intent_idempotency_key(intent, time_bucket=10_000_000)
        self.assertEqual(k_small, k_med)
        self.assertEqual(k_med, k_large)

    def test_key_stable_across_real_time(self) -> None:
        """A 1-second bucket previously produced a fresh key per second.
        The hardened key is purely intent-derived, so it must not change
        even if we monkey-patch time.time forward by hours."""
        import time as _time

        intent = _make_intent("i-time")
        real_time = _time.time
        try:
            _time.time = lambda: 1_000_000.0  # noqa: E731
            k1 = _intent_idempotency_key(intent, time_bucket=1)
            _time.time = lambda: 1_007_200.0  # +2hr  # noqa: E731
            k2 = _intent_idempotency_key(intent, time_bucket=1)
        finally:
            _time.time = real_time
        self.assertEqual(k1, k2)


class TestSubmitWithoutIdempotencyWarning(unittest.IsolatedAsyncioTestCase):
    """When `intent` is omitted the caller has opted out of idempotency
    protection; existing behavior is preserved (no raise) but a WARNING
    is emitted so operators can see the gap."""

    def setUp(self) -> None:
        _INFLIGHT_KEYS.clear()

    def tearDown(self) -> None:
        _INFLIGHT_KEYS.clear()

    async def test_limit_path_warns_when_intent_omitted(self) -> None:
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.return_value = {"orderID": "oid-no-intent"}
        client.get_order.return_value = {
            "status": "FILLED", "size_matched": "10", "original_size": "10",
        }

        with self.assertLogs("polymarket.execution", level="WARNING") as cm:
            oid, note = await place_limit_gtd_then_wait(
                client,
                token_id="tok-noid-1234567890",
                side="BUY",
                price=0.5,
                size=10.0,
                ttl_seconds=1,
                poll_seconds=0.1,
                dry_run=False,
                intent=None,
            )
        self.assertEqual(oid, "oid-no-intent")
        # WARNING emitted for opt-out.
        joined = "\n".join(cm.output)
        self.assertIn("submit_without_idempotency_protection", joined)
        # token id is sliced to 12 chars in the log.
        self.assertIn("tok-noid-123", joined)

    async def test_market_path_warns_when_intent_omitted(self) -> None:
        client = MagicMock()
        client.create_market_order.return_value = {"signed": True}
        client.post_order.return_value = {"orderID": "mkt-oid-no-intent"}

        with self.assertLogs("polymarket.execution", level="WARNING") as cm:
            oid, note = await place_market_fok_fallback(
                client,
                token_id="tok-noid-mkt-12345",
                side="SELL",
                amount_usd=25.0,
                dry_run=False,
                intent=None,
            )
        self.assertEqual(oid, "mkt-oid-no-intent")
        joined = "\n".join(cm.output)
        self.assertIn("submit_without_idempotency_protection", joined)

    async def test_no_warning_when_intent_present(self) -> None:
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 0
        client.create_order.return_value = {"signed": True}
        client.post_order.return_value = {"orderID": "oid-with-intent"}
        client.get_order.return_value = {
            "status": "FILLED", "size_matched": "10", "original_size": "10",
        }

        intent = _make_intent("intent-present")
        logger = logging.getLogger("polymarket.execution")
        # Capture all records emitted on the execution logger and assert
        # the opt-out warning was NOT among them.
        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Cap(level=logging.DEBUG)
        logger.addHandler(handler)
        try:
            await place_limit_gtd_then_wait(
                client,
                token_id=intent["token_id"],
                side=intent["side"],
                price=intent["price"],
                size=10.0,
                ttl_seconds=1,
                poll_seconds=0.1,
                dry_run=False,
                intent=intent,
            )
        finally:
            logger.removeHandler(handler)
        msgs = [r.getMessage() for r in records]
        self.assertFalse(
            any("submit_without_idempotency_protection" in m for m in msgs),
            f"unexpected opt-out warning when intent was supplied: {msgs}",
        )


class TestMarketFokIdempotency(unittest.IsolatedAsyncioTestCase):
    """place_market_fok_fallback honors the same in-flight gate."""

    def setUp(self) -> None:
        _INFLIGHT_KEYS.clear()

    def tearDown(self) -> None:
        _INFLIGHT_KEYS.clear()

    async def test_inflight_blocks_market_submit(self) -> None:
        client = MagicMock()
        client.create_market_order.return_value = {"signed": True}
        client.post_order.return_value = {"orderID": "should-not-fire"}

        # Build intent with explicit size_usd=25.0 and price=0.0 to match
        # the market-path key derivation (market path sets price=0.0 default
        # but the intent's size_usd wins via setdefault).
        intent = _make_intent("intent-mkt", price=0.0, size_usd=25.0)
        key = _intent_idempotency_key(dict(intent), time_bucket=10_000_000)
        _INFLIGHT_KEYS.add(key)
        try:
            oid, note = await place_market_fok_fallback(
                client,
                token_id=intent["token_id"],
                side=intent["side"],
                amount_usd=25.0,
                dry_run=False,
                intent=intent,
                idempotency_time_bucket=10_000_000,
            )
        finally:
            _INFLIGHT_KEYS.discard(key)

        self.assertIsNone(oid)
        self.assertEqual(note, "idempotency_inflight")
        client.create_market_order.assert_not_called()
        client.post_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
