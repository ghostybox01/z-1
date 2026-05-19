"""
Regression tests for E8: surface v1 fallback in copy_rules.wallet_score.

Previously a bare `except Exception: pass` silently swallowed
wallet_score_v2 failures and silently switched scoring to v1, so the
operator never knew. wallet_score must now:
  - emit a WARNING log line on fallback, and
  - increment state.scoring_fallback_v1 when state is passed in.
"""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot.copy_rules as copy_rules


def _settings():
    return SimpleNamespace(
        copy_wallet_score_overrides={},
        wallet_score_decay_half_life_hours=168.0,
    )


def _rows(n: int = 5):
    return [
        {
            "type": "TRADE",
            "side": "BUY",
            "token_id": "tok" + "x" * 38,
            "question": f"Q{i}",
            "price": 0.4,
            "amount": 10,
            "outcome": "Yes",
            "timestamp": "1700000000",
        }
        for i in range(n)
    ]


def _boom(*_a, **_kw):
    raise RuntimeError("v2 exploded")


class TestFallbackLogsWarning(unittest.TestCase):
    def test_v2_failure_logs_warning_and_runs_v1(self) -> None:
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            with self.assertLogs("polymarket.copy_rules", level="WARNING") as cm:
                score, parts = copy_rules.wallet_score(
                    _rows(5),
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                )
        # v1 fallback ran and returned a real composite score.
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)
        # v1 components shape (n, outcome, price, size).
        self.assertIn("n", parts)
        self.assertIn("outcome", parts)
        self.assertIn("price", parts)
        self.assertIn("size", parts)
        # At least one warning carrying the fallback prefix.
        self.assertTrue(any("scoring fallback" in m for m in cm.output))


class TestFallbackIncrementsCounter(unittest.TestCase):
    def test_counter_increments_when_state_passed(self) -> None:
        state = SimpleNamespace(scoring_fallback_v1=0)
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            # Silence the warning log to keep test output clean.
            with self.assertLogs("polymarket.copy_rules", level="WARNING"):
                copy_rules.wallet_score(
                    _rows(5),
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                    state=state,
                )
        self.assertEqual(state.scoring_fallback_v1, 1)

    def test_counter_initialises_when_field_missing(self) -> None:
        # If Task 1 hadn't landed yet, state may lack the field. The
        # getattr/setattr pattern must create it cleanly at 1.
        state = SimpleNamespace()
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            with self.assertLogs("polymarket.copy_rules", level="WARNING"):
                copy_rules.wallet_score(
                    _rows(5),
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                    state=state,
                )
        self.assertEqual(getattr(state, "scoring_fallback_v1", None), 1)

    def test_no_state_is_a_no_op(self) -> None:
        # No state passed => no AttributeError, just the warning.
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            with self.assertLogs("polymarket.copy_rules", level="WARNING"):
                copy_rules.wallet_score(
                    _rows(5),
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                )


class TestNoFallbackOnSuccess(unittest.TestCase):
    def test_successful_v2_does_not_increment_counter(self) -> None:
        state = SimpleNamespace(scoring_fallback_v1=0)
        # Real wallet_score_v2 runs; with 5 valid rows it should succeed.
        # Suppress propagation of any unrelated logger to keep the test clean.
        prior = logging.getLogger("polymarket.copy_rules").level
        try:
            copy_rules.wallet_score(
                _rows(5),
                wallet="0xabc",
                default_bet_usd=5.0,
                settings=_settings(),
                state=state,
            )
        finally:
            logging.getLogger("polymarket.copy_rules").setLevel(prior)
        self.assertEqual(state.scoring_fallback_v1, 0)


if __name__ == "__main__":
    unittest.main()
