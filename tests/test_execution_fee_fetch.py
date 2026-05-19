"""Fee-fetch hardening (E10a).

The previous implementation silently defaulted to fee_bps=0 on any
exception, which produced wrong cost basis and risked CLOB signature
rejection. The hardened helper retries once, then falls back to a
configurable default with a WARNING and an optional counter on `state`.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from bot.execution import _fetch_fee_bps_with_retry


class TestFeeFetchSuccess(unittest.TestCase):
    def test_first_call_succeeds_returns_value(self):
        client = MagicMock()
        client.get_fee_rate_bps.return_value = 7
        v = _fetch_fee_bps_with_retry(
            client, token_id="tok-abc", default_bps=2
        )
        self.assertEqual(v, 7)
        client.get_fee_rate_bps.assert_called_once_with("tok-abc")


class TestFeeFetchRetry(unittest.TestCase):
    def test_first_call_raises_second_succeeds(self):
        client = MagicMock()
        # First call raises; second returns 3.
        client.get_fee_rate_bps.side_effect = [RuntimeError("flake"), 3]
        v = _fetch_fee_bps_with_retry(
            client, token_id="tok-retry", default_bps=2
        )
        self.assertEqual(v, 3)
        self.assertEqual(client.get_fee_rate_bps.call_count, 2)


class TestFeeFetchAllFail(unittest.TestCase):
    def test_both_calls_raise_default_used_warning_emitted(self):
        client = MagicMock()
        client.get_fee_rate_bps.side_effect = RuntimeError("down")
        with self.assertLogs("polymarket.execution", level="WARNING") as cm:
            v = _fetch_fee_bps_with_retry(
                client, token_id="tok-warn-12345", default_bps=2
            )
        self.assertEqual(v, 2)
        self.assertEqual(client.get_fee_rate_bps.call_count, 2)
        joined = "\n".join(cm.output)
        self.assertIn("fee_fetch_failed", joined)
        # token sliced to 12 chars
        self.assertIn("tok-warn-123", joined)
        self.assertIn("using_default=2", joined)

    def test_state_counter_increments_on_failure(self):
        client = MagicMock()
        client.get_fee_rate_bps.side_effect = RuntimeError("down")
        state = SimpleNamespace()  # no counter yet
        v = _fetch_fee_bps_with_retry(
            client, token_id="tok-cnt", default_bps=2, state=state
        )
        self.assertEqual(v, 2)
        self.assertEqual(getattr(state, "fee_fetch_failures", 0), 1)
        # second failure increments to 2
        _fetch_fee_bps_with_retry(
            client, token_id="tok-cnt", default_bps=2, state=state
        )
        self.assertEqual(getattr(state, "fee_fetch_failures", 0), 2)

    def test_state_none_does_not_raise(self):
        client = MagicMock()
        client.get_fee_rate_bps.side_effect = RuntimeError("down")
        # state=None path must not raise.
        v = _fetch_fee_bps_with_retry(
            client, token_id="tok-none", default_bps=5, state=None
        )
        self.assertEqual(v, 5)


if __name__ == "__main__":
    unittest.main()
