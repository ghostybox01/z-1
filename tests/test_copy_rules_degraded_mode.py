"""
Regression tests for E11 / Task 12: degraded-mode visibility and
rate-limit on the v1 fallback path of copy_rules.wallet_score.

When wallet_score_v2 fails and v1 silently runs:
  - ``state.scoring_mode`` must flip to ``"degraded"`` so the dashboard
    can show operators that ranking has changed.
  - The components dict must carry a ``rate_limit_mult`` < 1.0 so the
    copy_signal agent cuts per-trade size while degraded.

On v2 success ``state.scoring_mode`` must be ``"v2"`` and the multiplier
must be 1.0 (i.e. no throttling).

The optional ``state`` kwarg must remain optional — callers that omit it
must not crash; only the WARNING log fires (covered by T8 tests).
"""

from __future__ import annotations

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


class TestScoringModeOnSuccess(unittest.TestCase):
    """When v2 succeeds, scoring_mode must be 'v2' and rate-limit 1.0."""

    def test_v2_success_sets_mode_v2(self) -> None:
        # Pre-set to "degraded" to prove the success path actively
        # reaffirms "v2" (not just relies on default).
        state = SimpleNamespace(scoring_fallback_v1=0, scoring_mode="degraded")
        score, parts = copy_rules.wallet_score(
            _rows(5),
            wallet="0xabc",
            default_bet_usd=5.0,
            settings=_settings(),
            state=state,
        )
        self.assertEqual(state.scoring_mode, "v2")
        self.assertEqual(state.scoring_fallback_v1, 0)
        self.assertIsInstance(score, float)

    def test_v2_success_rate_limit_is_one(self) -> None:
        state = SimpleNamespace(scoring_fallback_v1=0, scoring_mode="v2")
        _, parts = copy_rules.wallet_score(
            _rows(5),
            wallet="0xabc",
            default_bet_usd=5.0,
            settings=_settings(),
            state=state,
        )
        self.assertIn("rate_limit_mult", parts)
        self.assertEqual(float(parts["rate_limit_mult"]), 1.0)


class TestScoringModeOnFallback(unittest.TestCase):
    """When v2 fails, scoring_mode must flip to 'degraded' and rate-limit < 1."""

    def test_v2_failure_sets_mode_degraded(self) -> None:
        state = SimpleNamespace(scoring_fallback_v1=0, scoring_mode="v2")
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            with self.assertLogs("polymarket.copy_rules", level="WARNING"):
                copy_rules.wallet_score(
                    _rows(5),
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                    state=state,
                )
        self.assertEqual(state.scoring_mode, "degraded")
        # T8 counter still increments on the same path.
        self.assertEqual(state.scoring_fallback_v1, 1)

    def test_v2_failure_rate_limit_below_one(self) -> None:
        state = SimpleNamespace(scoring_fallback_v1=0, scoring_mode="v2")
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            with self.assertLogs("polymarket.copy_rules", level="WARNING"):
                _, parts = copy_rules.wallet_score(
                    _rows(5),
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                    state=state,
                )
        self.assertIn("rate_limit_mult", parts)
        mult = float(parts["rate_limit_mult"])
        self.assertLess(mult, 1.0)
        self.assertGreater(mult, 0.0)
        # Wired to the module-level constant — pin to 0.5 per E11 spec.
        self.assertEqual(mult, copy_rules.DEGRADED_MODE_SIZE_MULTIPLIER)
        self.assertEqual(mult, 0.5)

    def test_v2_failure_with_no_rows_still_carries_rate_limit(self) -> None:
        # Empty-row early return path must also carry the hint so the
        # copy_signal agent never sees a missing key in degraded mode.
        state = SimpleNamespace(scoring_fallback_v1=0, scoring_mode="v2")
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            with self.assertLogs("polymarket.copy_rules", level="WARNING"):
                _, parts = copy_rules.wallet_score(
                    [],
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                    state=state,
                )
        self.assertIn("rate_limit_mult", parts)
        self.assertEqual(float(parts["rate_limit_mult"]), 0.5)
        self.assertEqual(state.scoring_mode, "degraded")


class TestStateOptionalNoCrash(unittest.TestCase):
    """Without state passed: no crash, only the T8 warning."""

    def test_fallback_without_state_no_crash(self) -> None:
        with patch("bot.wallet_scoring.wallet_score_v2", side_effect=_boom):
            with self.assertLogs("polymarket.copy_rules", level="WARNING"):
                score, parts = copy_rules.wallet_score(
                    _rows(5),
                    wallet="0xabc",
                    default_bet_usd=5.0,
                    settings=_settings(),
                )
        self.assertIsInstance(score, float)
        self.assertIn("rate_limit_mult", parts)
        self.assertEqual(float(parts["rate_limit_mult"]), 0.5)

    def test_success_without_state_no_crash(self) -> None:
        score, parts = copy_rules.wallet_score(
            _rows(5),
            wallet="0xabc",
            default_bet_usd=5.0,
            settings=_settings(),
        )
        self.assertIsInstance(score, float)
        self.assertEqual(float(parts.get("rate_limit_mult", 1.0)), 1.0)


if __name__ == "__main__":
    unittest.main()
