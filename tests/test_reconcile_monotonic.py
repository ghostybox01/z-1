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


if __name__ == "__main__":
    unittest.main()
