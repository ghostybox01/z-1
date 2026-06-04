"""Tests for the pure calibration / strategy logic in
bot.backtest.calibration_probe (no network)."""
from __future__ import annotations

from bot.backtest.calibration_probe import (
    BUCKETS,
    COST,
    FAVORITE_MIN,
    LONGSHOT_MAX,
    CalibrationResult,
    bucket_index,
    build_buckets,
    score_favorite,
    score_longshot_no,
    tally_trade,
)


# ---------------------------------------------------------------------------
# bucket_index
# ---------------------------------------------------------------------------

def test_bucket_index_lower_edge_inclusive():
    # 0.00 falls in the first bucket [0.00, 0.05).
    assert bucket_index(0.0) == 0
    assert BUCKETS[0] == (0.00, 0.05)


def test_bucket_index_half_open_boundary():
    # 0.05 is the upper edge of bucket 0 and the lower edge of bucket 1 → bucket 1.
    assert bucket_index(0.05) == 1


def test_bucket_index_final_bucket_includes_one():
    last = len(BUCKETS) - 1
    assert bucket_index(1.0) == last
    assert bucket_index(0.97) == last


def test_bucket_index_out_of_range_is_none():
    assert bucket_index(-0.01) is None
    assert bucket_index(1.01) is None


# ---------------------------------------------------------------------------
# score_favorite  (BUY the contract; pays $1 if token resolves 1)
# ---------------------------------------------------------------------------

def test_favorite_win_pnl():
    # entry 0.90, token wins (1.0): pnl = (1 - 0.9)/0.9.
    pnl, win = score_favorite(0.90, 1.0)
    assert win is True
    assert pnl == (1.0 - 0.90) / 0.90


def test_favorite_loss_is_total():
    # entry 0.90, token loses (0.0): lose the whole stake → pnl = -1.
    pnl, win = score_favorite(0.90, 0.0)
    assert win is False
    assert pnl == -1.0


# ---------------------------------------------------------------------------
# score_longshot_no  (BUY the OPPOSITE; pays $1 if the cheap token loses)
# ---------------------------------------------------------------------------

def test_longshot_no_wins_when_token_loses():
    # Cheap token at p=0.10 → NO entry = 0.90 + COST. Token resolves 0 (lost),
    # so NO pays 1: pnl = (1 - entry)/entry, and it's a win.
    entry = (1.0 - 0.10) + COST
    pnl, win = score_longshot_no(entry, 0.0)
    assert win is True
    assert pnl == (1.0 - entry) / entry


def test_longshot_no_loses_when_token_wins():
    # The rare longshot that hits: our NO loses the whole stake.
    entry = (1.0 - 0.10) + COST
    pnl, win = score_longshot_no(entry, 1.0)
    assert win is False
    assert pnl == -1.0


# ---------------------------------------------------------------------------
# tally_trade — routing + calibration bucketing
# ---------------------------------------------------------------------------

def test_tally_routes_longshot_to_no_side_only():
    r = CalibrationResult()
    r.buckets = build_buckets()
    tally_trade(r, price=0.08, token_resolved=0.0)  # cheap token that lost
    assert r.longshot.n == 1
    assert r.favorite.n == 0
    assert r.combined.n == 1
    # Calibration bucket [0.05,0.10) recorded a loss (token resolved 0).
    bi = bucket_index(0.08)
    assert r.buckets[bi].n == 1
    assert r.buckets[bi].wins == 0
    assert r.buckets[bi].gap == r.buckets[bi].win_rate - r.buckets[bi].mean_price


def test_tally_routes_favorite_to_yes_side_only():
    r = CalibrationResult()
    r.buckets = build_buckets()
    tally_trade(r, price=0.92, token_resolved=1.0)  # expensive token that won
    assert r.favorite.n == 1
    assert r.longshot.n == 0
    assert r.combined.n == 1
    bi = bucket_index(0.92)
    assert r.buckets[bi].wins == 1
    assert r.buckets[bi].win_rate == 1.0


def test_tally_middle_price_makes_no_bet_but_still_buckets():
    r = CalibrationResult()
    r.buckets = build_buckets()
    tally_trade(r, price=0.50, token_resolved=1.0)
    # Middle of the book: no strategy bet on either tail side...
    assert r.longshot.n == 0
    assert r.favorite.n == 0
    assert r.combined.n == 0
    # ...but it still contributes to the calibration curve.
    bi = bucket_index(0.50)
    assert r.buckets[bi].n == 1


def test_boundaries_are_strict_no_bet_at_exact_thresholds():
    r = CalibrationResult()
    r.buckets = build_buckets()
    # p == LONGSHOT_MAX and p == FAVORITE_MIN are NOT in range (strict < / >).
    tally_trade(r, price=LONGSHOT_MAX, token_resolved=0.0)
    tally_trade(r, price=FAVORITE_MIN, token_resolved=1.0)
    assert r.longshot.n == 0
    assert r.favorite.n == 0


def test_gap_sign_reflects_flb_signature():
    """A bucket where cheap tokens win LESS than their price → negative gap
    (the overpriced-longshot signature)."""
    r = CalibrationResult()
    r.buckets = build_buckets()
    # Ten tokens priced ~0.10; only 1 wins → actual 10% vs implied ~10%... make
    # it clearly under: 0 wins out of 10 at price 0.10 → gap = -0.10.
    for _ in range(10):
        tally_trade(r, price=0.10, token_resolved=0.0)
    bi = bucket_index(0.10)
    b = r.buckets[bi]
    assert b.n == 10
    assert b.win_rate == 0.0
    assert abs(b.mean_price - 0.10) < 1e-9
    assert b.gap < 0  # overpriced longshot
