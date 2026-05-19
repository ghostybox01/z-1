"""Regression tests for terminal-absorbing reconcile semantics (E7).

The defect: a stale `PENDING → open → CANCELLED` polling sequence could
downgrade a real fill, corrupting P&L and position state. These tests pin
down the new rules:
  * "filled" is absorbing — no later snapshot overwrites it.
  * "cancelled" / "closed" can only upgrade to "filled".
  * PARTIALLY_FILLED → CANCELLED preserves both the matched fill and the
    cancel-of-remainder via the reconcile_note.
  * The normal PENDING → OPEN → FILLED path still works.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import MagicMock

from bot.reconcile import (
    canonical_status_from_order_payload,
    merge_trade_status,
    reconcile_trade_records_inplace,
)


@dataclass
class FakeRecord:
    order_id: str = "0xabc"
    status: str = ""
    reconcile_note: Optional[str] = None


def _payload(status: str, size_matched: float = 0.0, original_size: float = 100.0,
             oid: str = "0xabc") -> dict:
    return {
        "id": oid,
        "status": status,
        "size_matched": str(size_matched),
        "original_size": str(original_size),
    }


def _replay(records: list, clob_responses: list) -> None:
    """Drive reconcile_trade_records_inplace once per scripted CLOB response."""
    for resp in clob_responses:
        clob = MagicMock()
        clob.get_order.return_value = resp
        reconcile_trade_records_inplace(clob, records, depth=10, sleep_between_s=0)


class TestFilledIsAbsorbing(unittest.TestCase):
    def test_pending_filled_open_cancelled_stays_filled(self) -> None:
        """PENDING → FILLED → OPEN → CANCELLED: end state is FILLED."""
        rec = FakeRecord(status="submitted")
        # First poll: order is open/pending.
        # Second poll: order is FILLED.
        # Third (stale) poll: claims OPEN.
        # Fourth (stale) poll: claims CANCELLED with size_matched=0.
        responses = [
            _payload("PENDING"),
            _payload("FILLED", size_matched=100.0, original_size=100.0),
            _payload("OPEN"),
            _payload("CANCELLED", size_matched=0.0, original_size=100.0),
        ]
        _replay([rec], responses)
        self.assertEqual(rec.status, "filled")

    def test_cancelled_zero_fill_cannot_overwrite_filled(self) -> None:
        rec = FakeRecord(status="filled")
        self.assertIsNone(merge_trade_status("filled", "cancelled"))
        # Drive a reconcile with a CANCELLED zero-fill payload and verify
        # the recorded status stays filled.
        clob = MagicMock()
        clob.get_order.return_value = _payload(
            "CANCELLED", size_matched=0.0, original_size=100.0
        )
        reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)
        self.assertEqual(rec.status, "filled")

    def test_filled_to_open_rejected(self) -> None:
        self.assertIsNone(merge_trade_status("filled", "open"))

    def test_cancelled_to_open_rejected(self) -> None:
        # Terminal cancelled does not fall back to non-terminal open.
        self.assertIsNone(merge_trade_status("cancelled", "open"))

    def test_cancelled_can_upgrade_to_filled(self) -> None:
        # Edge: a later snapshot reveals the order actually had a fill.
        self.assertEqual(merge_trade_status("cancelled", "filled"), "filled")


class TestPartialFillCancel(unittest.TestCase):
    def test_partial_then_cancel_records_both(self) -> None:
        """PARTIALLY_FILLED(50/100) → CANCELLED: effective fill is 50,
        and the reconcile note encodes both the fill and the remainder."""
        rec = FakeRecord(status="open")
        # CLOB returns: partial fill observed (size_matched=50, status PENDING),
        # then a CANCELLED snapshot with the same matched=50 and a cancel of
        # the remaining 50.
        responses = [
            _payload("PENDING", size_matched=50.0, original_size=100.0),
            _payload("CANCELLED", size_matched=50.0, original_size=100.0),
        ]
        _replay([rec], responses)
        # Status promoted to filled (the matched portion settled).
        self.assertEqual(rec.status, "filled")
        # Note encodes both the matched size and the cancelled remainder.
        self.assertIsNotNone(rec.reconcile_note)
        note = rec.reconcile_note or ""
        self.assertIn("partial_fill_cancelled", note)
        self.assertIn("size_matched=50", note)
        self.assertIn("cancelled_remainder=50", note)

    def test_canonical_status_promotes_partial_cancel_to_filled(self) -> None:
        st = canonical_status_from_order_payload(
            _payload("CANCELLED", size_matched=30.0, original_size=100.0)
        )
        self.assertEqual(st, "filled")

    def test_canonical_status_cancel_with_no_fill_is_cancelled(self) -> None:
        st = canonical_status_from_order_payload(
            _payload("CANCELLED", size_matched=0.0, original_size=100.0)
        )
        self.assertEqual(st, "cancelled")


class TestForwardPath(unittest.TestCase):
    def test_pending_open_filled_progresses(self) -> None:
        """Normal forward progression still works."""
        rec = FakeRecord(status="submitted")
        responses = [
            _payload("PENDING"),
            _payload("OPEN"),
            _payload("FILLED", size_matched=100.0, original_size=100.0),
        ]
        _replay([rec], responses)
        self.assertEqual(rec.status, "filled")

    def test_open_to_cancelled_with_zero_fill_is_cancelled(self) -> None:
        rec = FakeRecord(status="open")
        clob = MagicMock()
        clob.get_order.return_value = _payload(
            "CANCELLED", size_matched=0.0, original_size=100.0
        )
        reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)
        self.assertEqual(rec.status, "cancelled")


class TestNoDryRunSideEffects(unittest.TestCase):
    def test_dry_run_record_is_skipped(self) -> None:
        rec = FakeRecord(status="dry_run", order_id="dry_123")
        clob = MagicMock()
        clob.get_order.return_value = _payload("FILLED", 100.0, 100.0)
        reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)
        # Dry-run prefix short-circuits before any get_order call.
        clob.get_order.assert_not_called()
        self.assertEqual(rec.status, "dry_run")


class TestNoneSizeMatchedNormalization(unittest.TestCase):
    """size_matched=None is normalized to 0 (NOT treated as missing data).
    The previous code's `sm is not None and sm > 0` check silently skipped
    partial-fill detection when sm was None, losing fill data."""

    def test_cancelled_with_size_matched_none_no_partial_fill(self) -> None:
        """CANCELLED with sm=None → normalized to 0 → treated as plain
        cancelled, NOT recorded as a partial fill."""
        rec = FakeRecord(status="open")
        clob = MagicMock()
        # size_matched omitted entirely — normalize_order_payload returns None.
        clob.get_order.return_value = {
            "id": "0xabc",
            "status": "CANCELLED",
            "original_size": "100",
            # no size_matched key
        }
        with self.assertLogs("polymarket.reconcile", level="DEBUG") as cm:
            reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)

        # Status: cancelled (no fill).
        self.assertEqual(rec.status, "cancelled")
        # No partial-fill note (because sm normalized to 0).
        note = rec.reconcile_note or ""
        self.assertNotIn("partial_fill_cancelled", note)
        # DEBUG message about the normalization was emitted.
        joined = "\n".join(cm.output)
        self.assertIn("size_matched=None", joined)
        self.assertIn("normalizing to 0", joined)


class TestCancelledAfterFullFill(unittest.TestCase):
    """A CANCELLED snapshot where size_matched == original_size is NOT a
    partial-cancel — the order was fully filled and the cancel only
    cancels the (empty) remainder. Terminal-absorbing logic handles it."""

    def test_cancelled_with_full_fill_no_partial_cancel_note(self) -> None:
        rec = FakeRecord(status="open")
        clob = MagicMock()
        # sm == osz: full fill, then cancel of empty remainder.
        clob.get_order.return_value = _payload(
            "CANCELLED", size_matched=100.0, original_size=100.0
        )
        reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)

        # canonical_status promotes sm>0 cancellation to filled.
        self.assertEqual(rec.status, "filled")
        # But this is NOT a partial-cancel — sm is NOT strictly less than osz,
        # so the note should be the plain filled tag, not partial_fill_cancelled.
        note = rec.reconcile_note or ""
        self.assertNotIn("partial_fill_cancelled", note)
        self.assertIn("clob:filled", note)


class TestStrictPartialFillThreshold(unittest.TestCase):
    """The strict-less-than rule must classify a 99% fill as partial-cancel
    (previously this was governed by the arbitrary `< osz * 0.999` rule).
    The new rule is more conservative: ANY sm < osz with CANCELLED is
    partial-cancel."""

    def test_99_percent_fill_is_partial_cancel(self) -> None:
        rec = FakeRecord(status="open")
        clob = MagicMock()
        clob.get_order.return_value = _payload(
            "CANCELLED", size_matched=99.0, original_size=100.0
        )
        reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)
        self.assertEqual(rec.status, "filled")
        note = rec.reconcile_note or ""
        self.assertIn("partial_fill_cancelled", note)
        self.assertIn("size_matched=99", note)
        self.assertIn("cancelled_remainder=1", note)

    def test_999_per_mil_fill_is_partial_cancel(self) -> None:
        """A 99.9% fill (previously misclassified by the `< 0.999` tolerance)
        is now correctly treated as partial-cancel under the strict rule."""
        rec = FakeRecord(status="open")
        clob = MagicMock()
        clob.get_order.return_value = _payload(
            "CANCELLED", size_matched=99.9, original_size=100.0
        )
        reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)
        self.assertEqual(rec.status, "filled")
        note = rec.reconcile_note or ""
        self.assertIn("partial_fill_cancelled", note)


class TestReconcileLockExists(unittest.TestCase):
    """The module-level lock must exist and be taken by reconcile.
    We verify it's not just decorative by patching it with a sentinel
    and confirming reconcile entered the critical section."""

    def test_lock_is_acquired_during_reconcile(self) -> None:
        from bot import reconcile as _r

        rec = FakeRecord(status="open")
        clob = MagicMock()
        clob.get_order.return_value = _payload("FILLED", 100.0, 100.0)

        original_lock = _r._RECONCILE_LOCK
        acquired = {"n": 0}

        class _SpyLock:
            def __enter__(self):
                acquired["n"] += 1
                return self

            def __exit__(self, *a):
                return False

        _r._RECONCILE_LOCK = _SpyLock()
        try:
            reconcile_trade_records_inplace(clob, [rec], depth=10, sleep_between_s=0)
        finally:
            _r._RECONCILE_LOCK = original_lock

        self.assertEqual(acquired["n"], 1)
        self.assertEqual(rec.status, "filled")


if __name__ == "__main__":
    unittest.main()
