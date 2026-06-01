"""Tests for the pure core of bot.backtest.edge_probe (no network required)."""
from __future__ import annotations

import pytest

from bot.backtest.edge_probe import replay_passive_bid, ProbeResult


# ---------------------------------------------------------------------------
# test_downtrend_loses
# ---------------------------------------------------------------------------

def test_downtrend_loses():
    """Monotone downtrend: signals fire in the value range, bids fill on the way
    down, and the forward price is lower at hold_horizon → mean_pnl_per_fill < 0
    and win_rate < 0.5."""
    # Prices [0.40, 0.39, 0.38, ..., 0.30] — 11 points, all in [0.20, 0.45]
    prices = [round(0.40 - 0.01 * i, 4) for i in range(11)]
    # Sanity: first and last in range
    assert 0.20 <= prices[0] <= 0.45
    assert 0.20 <= prices[-1] <= 0.45

    result = replay_passive_bid(
        prices,
        entry_discount=0.01,
        value_lo=0.20,
        value_hi=0.45,
        fill_window=5,
        hold_horizon=5,
        fee_bps=0.0,
    )

    assert result.n_signals >= 1, "Expected at least one signal in downtrend"
    assert result.n_fills >= 1, "Expected at least one fill in monotone downtrend"
    assert result.mean_pnl_per_fill < 0, (
        f"Expected negative PnL in downtrend, got {result.mean_pnl_per_fill}"
    )
    assert result.win_rate < 0.5, (
        f"Expected low win rate in downtrend, got {result.win_rate}"
    )


# ---------------------------------------------------------------------------
# test_meanrevert_wins
# ---------------------------------------------------------------------------

def test_meanrevert_wins():
    """Dip-then-recover: a signal fires at 0.40, the price dips to 0.34 (filling
    the passive bid placed at entry ~0.396), then recovers to 0.40 → pnl > 0."""
    # Stable at 0.40, dip to 0.36 / 0.34, then recover to 0.40
    prices = [0.40] * 3 + [0.36, 0.34] + [0.40] * 30
    # Total 35 points; hold_horizon=30 means we fully recover

    result = replay_passive_bid(
        prices,
        entry_discount=0.01,     # entry bid at prices[i] * 0.99 ≈ 0.396
        value_lo=0.20,
        value_hi=0.45,
        fill_window=10,
        hold_horizon=30,
        fee_bps=0.0,
    )

    assert result.n_signals >= 1, "Expected signals in value range [0.20, 0.45]"
    assert result.n_fills >= 1, "Expected at least one fill on the dip"
    assert result.mean_pnl_per_fill > 0, (
        f"Expected positive PnL after mean reversion, got {result.mean_pnl_per_fill}"
    )


# ---------------------------------------------------------------------------
# test_flat_no_fill
# ---------------------------------------------------------------------------

def test_flat_no_fill():
    """Flat price at 0.40: the passive bid is set at 0.40 * 0.99 = 0.396.
    Since the price never drops to 0.396, no fills occur.
    n_fills == 0 and all aggregates are 0.0 (no divide-by-zero crash)."""
    prices = [0.40] * 50

    result = replay_passive_bid(
        prices,
        entry_discount=0.01,
        value_lo=0.20,
        value_hi=0.45,
        fill_window=10,
        hold_horizon=30,
        fee_bps=0.0,
    )

    assert result.n_signals > 0, "Expected signals (0.40 is in [0.20, 0.45])"
    assert result.n_fills == 0, f"Expected 0 fills on flat price, got {result.n_fills}"
    assert result.fill_rate == 0.0
    assert result.total_pnl_per_share == 0.0
    assert result.mean_pnl_per_fill == 0.0
    assert result.mean_pnl_bps == 0.0
    assert result.win_rate == 0.0


# ---------------------------------------------------------------------------
# test_out_of_range_no_signal
# ---------------------------------------------------------------------------

def test_out_of_range_no_signal():
    """Prices all at 0.05, which is below value_lo=0.20.
    No signals should fire at all."""
    prices = [0.05] * 50

    result = replay_passive_bid(
        prices,
        entry_discount=0.01,
        value_lo=0.20,
        value_hi=0.45,
        fill_window=10,
        hold_horizon=30,
        fee_bps=0.0,
    )

    assert result.n_signals == 0, f"Expected 0 signals below value_lo, got {result.n_signals}"
    assert result.n_fills == 0
    assert result.fill_rate == 0.0
    assert result.mean_pnl_bps == 0.0


# ---------------------------------------------------------------------------
# Additional edge-case sanity tests
# ---------------------------------------------------------------------------

def test_fill_rate_between_zero_and_one():
    """fill_rate must always be in [0, 1]."""
    prices = [0.30 + 0.01 * (i % 5) for i in range(50)]
    result = replay_passive_bid(prices, fill_window=5, hold_horizon=10)
    assert 0.0 <= result.fill_rate <= 1.0


def test_win_rate_between_zero_and_one():
    """win_rate must always be in [0, 1] when fills exist."""
    # Mix: dip then flat recovery
    prices = [0.40, 0.38, 0.37, 0.36] + [0.39] * 40
    result = replay_passive_bid(prices, fill_window=5, hold_horizon=10)
    if result.n_fills > 0:
        assert 0.0 <= result.win_rate <= 1.0


def test_fee_reduces_pnl():
    """Higher fee_bps should produce lower (or equal) pnl than no fee."""
    prices = [0.40] * 3 + [0.35] + [0.40] * 30
    r_no_fee = replay_passive_bid(prices, fee_bps=0.0)
    r_fee = replay_passive_bid(prices, fee_bps=200.0)
    if r_no_fee.n_fills > 0 and r_fee.n_fills > 0:
        assert r_fee.mean_pnl_per_fill <= r_no_fee.mean_pnl_per_fill


def test_probe_result_is_dataclass():
    """ProbeResult can be constructed with defaults and fields are accessible."""
    r = ProbeResult()
    assert r.n_signals == 0
    assert r.n_fills == 0
    assert r.fill_rate == 0.0
    assert r.mean_pnl_bps == 0.0
    assert r.win_rate == 0.0
