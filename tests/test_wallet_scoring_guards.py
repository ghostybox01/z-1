"""
Regression tests for E8: stdev() guards in wallet_scoring.

Previously _timing_quality_score and _consistency_score could call
statistics.stdev on lists with <2 elements and raise StatisticsError.
These tests pin the guarded behaviour so the bot never crashes on
sparse wallet samples.
"""

from __future__ import annotations

import unittest

from bot.copy_rules import CopyCandidate
from bot.wallet_scoring import _consistency_score, _timing_quality_score


def _cand(price: float = 0.5, category: str = "other", title: str = "q") -> CopyCandidate:
    return CopyCandidate(
        wallet="w",
        token_id="t",
        tx_key="k",
        title=title,
        slug="",
        tags_text="",
        category=category,
        outcome="yes",
        price=price,
        usdc=10.0,
    )


class TestTimingQualityGuards(unittest.TestCase):
    def test_empty_does_not_raise(self) -> None:
        # No StatisticsError; returns documented 0.0 sentinel.
        self.assertEqual(_timing_quality_score([]), 0.0)

    def test_single_price_does_not_raise(self) -> None:
        # One sample: stdev would raise; guard returns sweet_ratio only.
        score = _timing_quality_score([_cand(price=0.5)])
        self.assertIsInstance(score, float)
        # 0.5 is inside the [0.10, 0.70] sweet spot -> ratio 1.0
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_two_prices_does_not_raise(self) -> None:
        # Two samples: stdev is defined but len<3 path still returns sweet_ratio.
        score = _timing_quality_score([_cand(price=0.3), _cand(price=0.4)])
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


class TestConsistencyGuards(unittest.TestCase):
    def test_all_trades_one_category_does_not_raise(self) -> None:
        # All in one bucket -> cat_counts has length 1 -> stdev would raise.
        cands = [
            _cand(category="sports", title=f"M{i}") for i in range(5)
        ]
        score = _consistency_score(cands)
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_empty_does_not_raise(self) -> None:
        self.assertEqual(_consistency_score([]), 0.0)

    def test_single_trade_does_not_raise(self) -> None:
        self.assertEqual(_consistency_score([_cand()]), 0.0)


if __name__ == "__main__":
    unittest.main()
