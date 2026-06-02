"""Tests for the per-niche split logic in bot.backtest.copy_probe (no network)."""
from __future__ import annotations

from collections import Counter

from bot.backtest.copy_probe import (
    NICHE_MIN_PASSED,
    proven_niches,
    _split_stats,
)


# ---------------------------------------------------------------------------
# proven_niches
# ---------------------------------------------------------------------------

def test_niche_threshold_selects_all_qualifying_categories():
    """Every category at/above the threshold is a proven niche."""
    counts = Counter({"sports": 20, "crypto_short": NICHE_MIN_PASSED, "politics": 5})
    assert proven_niches(counts) == {"sports", "crypto_short"}


def test_niche_below_threshold_excluded():
    counts = Counter({"sports": NICHE_MIN_PASSED - 1, "politics": 3})
    # No category clears the bar → fallback to single top category.
    assert proven_niches(counts) == {"sports"}


def test_niche_fallback_to_top_when_none_qualify():
    """Wallet with no >=threshold category falls back to its single top category."""
    counts = Counter({"politics": 8, "sports": 4, "macro": 1})
    assert proven_niches(counts) == {"politics"}


def test_niche_fallback_tiebreak_is_deterministic():
    """Equal counts tie-break by category name (max), so output is stable."""
    counts = Counter({"macro": 5, "crypto_short": 5})
    # max((count, name)) → 'macro' beats 'crypto_short' alphabetically.
    assert proven_niches(counts) == {"macro"}


def test_niche_empty_counts_is_empty_set():
    assert proven_niches(Counter()) == set()


def test_niche_custom_min_passed():
    counts = Counter({"sports": 10, "politics": 12})
    assert proven_niches(counts, min_passed=10) == {"sports", "politics"}


# ---------------------------------------------------------------------------
# _split_stats
# ---------------------------------------------------------------------------

def test_split_stats_basic():
    n, win_rate, mean_bps, median_bps = _split_stats([100.0, -50.0, 200.0], wins=2)
    assert n == 3
    assert win_rate == 2 / 3
    assert mean_bps == (100.0 - 50.0 + 200.0) / 3
    assert median_bps == 100.0


def test_split_stats_median_differs_from_mean_on_longshot_skew():
    """A single jackpot drags the mean positive while the median stays negative —
    the whole reason we lead with median in the verdict."""
    bps = [-100.0, -100.0, -100.0, 900.0]
    n, win_rate, mean_bps, median_bps = _split_stats(bps, wins=1)
    assert n == 4
    assert win_rate == 0.25
    assert mean_bps > 0  # mean misleads
    assert median_bps < 0  # median tells the truth


def test_split_stats_empty():
    assert _split_stats([], wins=0) == (0, 0.0, 0.0, 0.0)
