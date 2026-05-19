"""E12: sentiment factor must be symmetric (no systematic long bias)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from bot import signals
from bot.signals import SENTIMENT_FACTOR, SignalView, intent_signal_boost


def _sig(sentiment: float, weight: float = 1.0) -> SignalView:
    return SignalView(
        title="t",
        keywords=json.dumps(["election"]),
        sentiment=sentiment,
        weight=weight,
    )


class TestSentimentSymmetry(unittest.TestCase):
    def test_symmetric_factor_constant(self):
        # Sanity: there is a single factor constant.
        self.assertGreater(SENTIMENT_FACTOR, 0.0)
        self.assertLess(SENTIMENT_FACTOR, 1.0)

    def test_equal_magnitude_for_matching_sentiment(self):
        question = "Will the election outcome be decisive?"

        with patch.object(signals, "active_signals", return_value=[_sig(0.5)]):
            m_pos, _ = intent_signal_boost(question)
        with patch.object(signals, "active_signals", return_value=[_sig(-0.5)]):
            m_neg, _ = intent_signal_boost(question)

        # Adjustments equal in magnitude around 1.0
        self.assertAlmostEqual(m_pos - 1.0, -(m_neg - 1.0), places=9)

    def test_symmetry_across_multiple_strengths(self):
        question = "Will the election outcome be decisive?"
        for sent in (0.2, 0.4, 0.75, 1.0):
            with patch.object(signals, "active_signals", return_value=[_sig(sent)]):
                m_pos, _ = intent_signal_boost(question)
            with patch.object(signals, "active_signals", return_value=[_sig(-sent)]):
                m_neg, _ = intent_signal_boost(question)
            self.assertAlmostEqual(
                m_pos - 1.0, -(m_neg - 1.0), places=9,
                msg=f"asymmetry at sent={sent}: +{m_pos} vs -{m_neg}",
            )


if __name__ == "__main__":
    unittest.main()
