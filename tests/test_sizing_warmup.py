"""E12: warmup multiplier must shrink size until we have a P&L signal."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot import sizing
from bot.sizing import pnl_aware_size_multiplier


class TestSizingWarmup(unittest.TestCase):
    def test_warmup_shrinks_to_half(self):
        # 5 rows -> warmup branch
        with patch.object(sizing, "recent_trade_statuses", return_value=["filled"] * 5):
            m = pnl_aware_size_multiplier()
        self.assertLessEqual(m, 0.5)
        self.assertAlmostEqual(m, 0.5, places=9)

    def test_warmup_never_exceeds_one(self):
        for n in range(0, 6):
            with patch.object(sizing, "recent_trade_statuses", return_value=["filled"] * n):
                m = pnl_aware_size_multiplier()
            self.assertLessEqual(m, 1.0, msg=f"warmup multiplier > 1.0 at n={n}")

    def test_post_warmup_uses_window_behavior(self):
        # 7 rows -> exit warmup. With ~all filled, behavior is the
        # existing win_proxy formula: 1.0 + (1.0 - 0.35) * 0.55, clipped to [0.78, 1.12].
        with patch.object(sizing, "recent_trade_statuses", return_value=["filled"] * 7):
            m_high = pnl_aware_size_multiplier()
        self.assertGreater(m_high, 0.5)
        self.assertLessEqual(m_high, 1.12)
        self.assertGreaterEqual(m_high, 0.78)

        # And with all cancelled, the multiplier should drop toward the floor.
        with patch.object(sizing, "recent_trade_statuses", return_value=["cancelled"] * 7):
            m_low = pnl_aware_size_multiplier()
        self.assertGreaterEqual(m_low, 0.78)
        self.assertLess(m_low, m_high)


if __name__ == "__main__":
    unittest.main()
