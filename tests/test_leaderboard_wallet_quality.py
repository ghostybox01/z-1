"""Tests for the multi-source wallet quality analyzer.

The legacy implementation trusted /closed-positions as a complete history, which
yielded fake 100% win rates because the endpoint is server-capped at 50 and
server-filters to wins only. The replacement evaluates wallets from multiple
sources and refuses to qualify wallets whose losing trades are invisible.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from bot.leaderboard import (
    CLOSED_POSITIONS_SERVER_CAP,
    _reconstruct_round_trips,
    analyze_wallet_quality,
    discover_qualified_wallets,
)


def _t(ts: float, asset: str, side: str, price: float, size: float) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "asset": asset,
        "side": side,
        "price": price,
        "size": size,
    }


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):  # pragma: no cover - never errors in fake
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """Minimal httpx.AsyncClient stand-in.

    Returns a queued payload per (URL, params['user']) call. Falls back to
    queued ``default_payloads_by_url`` keyed on the URL alone.
    """

    def __init__(self, by_url: dict[str, Any]):
        self._by_url = by_url

    async def get(self, url, params=None, **kwargs):
        return _FakeResp(self._by_url.get(url, []))


class RoundTripReconstruction(unittest.TestCase):
    def test_single_buy_then_full_sell_yields_one_round_trip(self):
        trades = [
            _t(1.0, "A", "BUY", 0.30, 100),
            _t(2.0, "A", "SELL", 0.70, 100),
        ]
        rts = _reconstruct_round_trips(trades)
        self.assertEqual(len(rts), 1)
        self.assertAlmostEqual(rts[0]["pnl"], (0.70 - 0.30) * 100, places=6)
        self.assertEqual(rts[0]["asset"], "A")
        self.assertEqual(rts[0]["size"], 100)
        self.assertEqual(rts[0]["ts"], 2.0)

    def test_partial_sell_consumes_oldest_lot_first_fifo(self):
        trades = [
            _t(1.0, "A", "BUY", 0.30, 100),  # oldest lot
            _t(2.0, "A", "BUY", 0.50, 100),  # newer lot
            _t(3.0, "A", "SELL", 0.80, 150), # consumes all of lot 1 + half of lot 2
        ]
        rts = _reconstruct_round_trips(trades)
        self.assertEqual(len(rts), 2)
        # First round-trip consumes the oldest lot fully
        self.assertAlmostEqual(rts[0]["pnl"], (0.80 - 0.30) * 100, places=6)
        # Second uses the newer lot
        self.assertAlmostEqual(rts[1]["pnl"], (0.80 - 0.50) * 50, places=6)

    def test_sell_with_no_inventory_is_dropped(self):
        trades = [_t(1.0, "A", "SELL", 0.50, 50)]  # nothing was bought
        self.assertEqual(_reconstruct_round_trips(trades), [])

    def test_buy_only_yields_no_round_trip(self):
        trades = [_t(1.0, "A", "BUY", 0.50, 100)]
        self.assertEqual(_reconstruct_round_trips(trades), [])

    def test_losing_round_trip(self):
        trades = [
            _t(1.0, "A", "BUY", 0.60, 100),
            _t(2.0, "A", "SELL", 0.40, 100),
        ]
        rts = _reconstruct_round_trips(trades)
        self.assertEqual(len(rts), 1)
        self.assertAlmostEqual(rts[0]["pnl"], -20.0, places=6)

    def test_out_of_order_timestamps_are_sorted(self):
        trades = [
            _t(5.0, "A", "SELL", 0.70, 100),
            _t(1.0, "A", "BUY", 0.30, 100),
        ]
        rts = _reconstruct_round_trips(trades)
        self.assertEqual(len(rts), 1)
        self.assertAlmostEqual(rts[0]["pnl"], 40.0, places=6)

    def test_zero_or_negative_size_ignored(self):
        trades = [
            _t(1.0, "A", "BUY", 0.50, 0),     # zero size
            _t(2.0, "A", "BUY", 0.50, -10),   # negative size
            _t(3.0, "A", "BUY", 0.50, 100),   # real
            _t(4.0, "A", "SELL", 0.60, 100),
        ]
        rts = _reconstruct_round_trips(trades)
        self.assertEqual(len(rts), 1)
        self.assertAlmostEqual(rts[0]["pnl"], 10.0, places=6)

    def test_isolated_per_asset(self):
        trades = [
            _t(1.0, "A", "BUY", 0.30, 100),
            _t(2.0, "B", "BUY", 0.40, 100),
            _t(3.0, "A", "SELL", 0.50, 100),   # asset A round-trip
            _t(4.0, "B", "SELL", 0.30, 100),   # asset B losing round-trip
        ]
        rts = _reconstruct_round_trips(trades)
        self.assertEqual(len(rts), 2)
        by_asset = {rt["asset"]: rt for rt in rts}
        self.assertAlmostEqual(by_asset["A"]["pnl"], 20.0, places=6)
        self.assertAlmostEqual(by_asset["B"]["pnl"], -10.0, places=6)


class AnalyzeWalletQuality(unittest.TestCase):
    def _run(self, trades, closed, positions=None):
        http = _FakeHttp({
            "https://data-api.polymarket.com/trades": trades,
            "https://data-api.polymarket.com/closed-positions": closed,
            "https://data-api.polymarket.com/positions": positions or [],
        })
        return asyncio.run(analyze_wallet_quality(http, "0xabc"))

    def test_truncation_risk_flag_set_when_closed_returns_cap(self):
        closed = [{"realizedPnl": 1.0, "timestamp": float(i)} for i in range(CLOSED_POSITIONS_SERVER_CAP)]
        q = self._run([], closed)
        self.assertTrue(q["data_truncation_risk"])

    def test_no_truncation_when_closed_under_cap(self):
        closed = [{"realizedPnl": 1.0, "timestamp": 1.0}]
        q = self._run([], closed)
        self.assertFalse(q["data_truncation_risk"])

    def test_no_losses_no_round_trips_returns_none_win_rate(self):
        """Buy-and-hold-to-resolution wallet with no round-trips and a small
        resolved-wins sample: we cannot verify a win rate. Must return None."""
        trades = [_t(1.0, "A", "BUY", 0.5, 100)]
        closed = [{"realizedPnl": 1.0, "timestamp": 1.0}] * 5
        q = self._run(trades, closed)
        self.assertIsNone(q["win_rate"])
        self.assertEqual(q["loss_visibility"], "none")

    def test_resolved_wins_large_sample_grants_partial_visibility_but_still_no_win_rate(self):
        """Without a single observed loss, win_rate is still None even with 20+
        resolved wins. ``loss_visibility`` is downgraded from 'none' to 'partial'."""
        trades = []
        closed = [{"realizedPnl": 5.0, "timestamp": float(i)} for i in range(25)]
        q = self._run(trades, closed)
        self.assertEqual(q["loss_visibility"], "partial")
        self.assertIsNone(q["win_rate"])  # still unverifiable without an observed loss

    def test_observed_loss_yields_concrete_win_rate(self):
        trades = [
            _t(1.0, "A", "BUY", 0.30, 100),
            _t(2.0, "A", "SELL", 0.50, 100),   # WIN 20
            _t(3.0, "B", "BUY", 0.60, 100),
            _t(4.0, "B", "SELL", 0.40, 100),   # LOSS 20
            _t(5.0, "C", "BUY", 0.40, 100),
            _t(6.0, "C", "SELL", 0.60, 100),   # WIN 20
        ]
        q = self._run(trades, [])
        self.assertEqual(q["active_wins"], 2)
        self.assertEqual(q["active_losses"], 1)
        self.assertEqual(q["verified_total"], 3)
        self.assertEqual(q["loss_visibility"], "verified")
        self.assertAlmostEqual(q["win_rate"], 2 / 3, places=4)

    def test_buy_and_hold_positions_reveal_losses_and_win_rate(self):
        """A buy-and-hold wallet with NO SELL round-trips (active loss-blind):
        /trades only has BUYs, /closed-positions is empty, but /positions
        exposes resolved held outcomes including LOSERS. With 3 resolved wins
        (curPrice≈1, redeemable) and 2 resolved losers (curPrice≈0, redeemable)
        the win rate becomes verifiable at 0.6."""
        trades = [_t(float(i), f"hold_{i}", "BUY", 0.50, 100) for i in range(5)]
        positions = [
            # 3 resolved WINNERS (redeemable, curPrice ~1)
            {"redeemable": True, "curPrice": 1.0, "cashPnl": 50.0, "title": "w1"},
            {"redeemable": True, "curPrice": 0.99, "cashPnl": 49.0, "title": "w2"},
            {"redeemable": True, "curPrice": 0.97, "cashPnl": 47.0, "title": "w3"},
            # 2 resolved LOSERS (redeemable, curPrice ~0) — invisible to round-trips
            {"redeemable": True, "curPrice": 0.0, "cashPnl": -50.0, "title": "l1"},
            {"redeemable": True, "curPrice": 0.01, "cashPnl": -49.0, "title": "l2"},
            # an UNRESOLVED open position must be ignored entirely
            {"redeemable": False, "curPrice": 0.45, "cashPnl": -5.0, "title": "open"},
        ]
        q = self._run(trades, [], positions)
        # Active side is loss-blind (no SELLs → no round-trips)
        self.assertEqual(q["active_round_trips"], 0)
        self.assertEqual(q["active_wins"], 0)
        self.assertEqual(q["active_losses"], 0)
        # /positions reveals the resolved outcomes
        self.assertEqual(q["pos_resolved_wins"], 3)
        self.assertEqual(q["pos_resolved_losses"], 2)
        self.assertEqual(q["positions_seen"], 6)
        # Folded into the verified picture
        self.assertEqual(q["wins"], 3)
        self.assertEqual(q["losses"], 2)
        self.assertEqual(q["verified_total"], 5)
        self.assertEqual(q["loss_visibility"], "verified")
        self.assertAlmostEqual(q["win_rate"], 0.6, places=4)

    def test_positions_resolved_loss_can_drag_win_rate_below_active_only(self):
        """Active round-trips alone would read 100% (1 win, 0 losses), but
        /positions surfaces a previously-invisible resolved loser, pulling the
        verified win rate down to 0.5 and flipping visibility to 'verified'."""
        trades = [
            _t(1.0, "A", "BUY", 0.30, 100),
            _t(2.0, "A", "SELL", 0.70, 100),  # one active WIN, no active losses
        ]
        positions = [
            {"redeemable": True, "curPrice": 0.0, "cashPnl": -100.0, "title": "buryme"},
        ]
        q = self._run(trades, [], positions)
        self.assertEqual(q["active_wins"], 1)
        self.assertEqual(q["active_losses"], 0)
        self.assertEqual(q["pos_resolved_losses"], 1)
        self.assertEqual(q["loss_visibility"], "verified")
        self.assertAlmostEqual(q["win_rate"], 0.5, places=4)

    def test_streak_across_mixed_sources_chronological(self):
        trades = [
            _t(10.0, "A", "BUY", 0.30, 100),
            _t(20.0, "A", "SELL", 0.50, 100),  # WIN at ts=20
            _t(30.0, "B", "BUY", 0.60, 100),
            _t(40.0, "B", "SELL", 0.50, 100),  # LOSS at ts=40
        ]
        # Two resolved wins interleaved in time
        closed = [
            {"realizedPnl": 1.0, "timestamp": 5.0},    # win at ts=5  (before any active)
            {"realizedPnl": 1.0, "timestamp": 25.0},   # win at ts=25 (between W and L)
        ]
        q = self._run(trades, closed)
        # Chronological merged outcomes: W(5), W(20), W(25), L(40)
        # max_streak should be 3
        self.assertEqual(q["max_streak"], 3)
        self.assertEqual(q["current_streak"], 0)

    def test_current_loss_streak_counts_trailing_losses(self):
        """Most recent three outcomes are losses → current_loss_streak == 3.

        Round-trips: WIN(ts2), then three LOSSES (ts4, ts6, ts8). Walking back
        from the latest outcome we count 3 leading losses before hitting the win.
        """
        trades = [
            _t(1.0, "A", "BUY", 0.30, 100),
            _t(2.0, "A", "SELL", 0.70, 100),   # WIN  (oldest)
            _t(3.0, "B", "BUY", 0.60, 100),
            _t(4.0, "B", "SELL", 0.40, 100),   # LOSS
            _t(5.0, "C", "BUY", 0.60, 100),
            _t(6.0, "C", "SELL", 0.40, 100),   # LOSS
            _t(7.0, "D", "BUY", 0.60, 100),
            _t(8.0, "D", "SELL", 0.40, 100),   # LOSS (most recent)
        ]
        q = self._run(trades, [])
        self.assertEqual(q["current_loss_streak"], 3)

    def test_current_loss_streak_zero_when_latest_is_win(self):
        trades = [
            _t(1.0, "A", "BUY", 0.60, 100),
            _t(2.0, "A", "SELL", 0.40, 100),   # LOSS (older)
            _t(3.0, "B", "BUY", 0.30, 100),
            _t(4.0, "B", "SELL", 0.70, 100),   # WIN (most recent)
        ]
        q = self._run(trades, [])
        self.assertEqual(q["current_loss_streak"], 0)

    def test_current_loss_streak_zero_when_no_outcomes(self):
        q = self._run([_t(1.0, "A", "BUY", 0.5, 100)], [])
        self.assertEqual(q["current_loss_streak"], 0)

    def test_open_position_count(self):
        trades = [
            _t(1.0, "A", "BUY", 0.30, 100),
            _t(2.0, "A", "SELL", 0.50, 40),    # still holds 60
            _t(3.0, "B", "BUY", 0.20, 50),     # holds 50
            _t(4.0, "C", "BUY", 0.10, 10),
            _t(5.0, "C", "SELL", 0.30, 10),    # closed
        ]
        q = self._run(trades, [])
        self.assertEqual(q["open_position_count"], 2)


class DiscoverQualifiedWallets(unittest.TestCase):
    """End-to-end gating tests through the public discover_qualified_wallets."""

    def _run(self, leaderboard_resp, trades_by_user, closed_by_user, positions_by_user=None):
        # Build a fake http that routes by URL; closed/trades/positions vary per
        # user via the params dict. We intercept ``get`` and switch on the user
        # param.
        positions_by_user = positions_by_user or {}
        class Http:
            async def get(self, url, params=None, **kwargs):
                user = (params or {}).get("user", "")
                if url.endswith("/v1/leaderboard"):
                    return _FakeResp(leaderboard_resp)
                if url.endswith("/trades"):
                    return _FakeResp(trades_by_user.get(user, []))
                if url.endswith("/closed-positions"):
                    return _FakeResp(closed_by_user.get(user, []))
                if url.endswith("/positions"):
                    return _FakeResp(positions_by_user.get(user, []))
                return _FakeResp([])

        return asyncio.run(discover_qualified_wallets(
            Http(),
            categories=["OVERALL"],
            min_pnl=0.0,
            min_win_rate=0.60,
            min_win_streak=2,
            min_total_trades=5,
        ))

    def test_truncated_no_losses_wallet_rejected(self):
        """The exact pattern observed in the live smoke test: closed-positions
        returns the server cap of 50 winning rows, and there are no SELL trades
        to verify losses. This wallet must NOT qualify."""
        w = "0x" + "a" * 40
        lb = [{"proxyWallet": w, "rank": 1, "pnl": 1e6, "vol": 1e7, "userName": "x"}]
        # 489 BUYs / 11 SELLs but on different assets so no round-trip closes
        trades = [_t(float(i), f"asset_{i}", "BUY", 0.30, 100) for i in range(489)]
        closed = [{"realizedPnl": 1.0, "timestamp": float(i)} for i in range(CLOSED_POSITIONS_SERVER_CAP)]
        qualified = self._run(lb, {w: trades}, {w: closed})
        self.assertEqual(qualified, [])

    def test_wallet_with_verified_losses_can_qualify(self):
        w = "0x" + "b" * 40
        lb = [{"proxyWallet": w, "rank": 1, "pnl": 1e3, "vol": 1e4, "userName": "good"}]
        # 8 winning round-trips + 2 losing round-trips → 80% win rate, verified
        trades = []
        ts = 0.0
        for i in range(8):
            trades.append(_t(ts, f"win_{i}", "BUY", 0.30, 100)); ts += 1
            trades.append(_t(ts, f"win_{i}", "SELL", 0.70, 100)); ts += 1
        for i in range(2):
            trades.append(_t(ts, f"loss_{i}", "BUY", 0.60, 100)); ts += 1
            trades.append(_t(ts, f"loss_{i}", "SELL", 0.40, 100)); ts += 1
        qualified = self._run(lb, {w: trades}, {w: []})
        self.assertEqual(len(qualified), 1)
        self.assertAlmostEqual(qualified[0]["win_rate"], 0.8, places=4)
        self.assertEqual(qualified[0]["loss_visibility"], "verified")

    def test_low_win_rate_rejected_even_when_verifiable(self):
        w = "0x" + "c" * 40
        lb = [{"proxyWallet": w, "rank": 1, "pnl": 1e3, "vol": 1e4, "userName": "bad"}]
        trades = []
        ts = 0.0
        # 1 win, 4 losses → 20% verified win rate, below 60% gate
        trades.append(_t(ts, "x", "BUY", 0.30, 100)); ts += 1
        trades.append(_t(ts, "x", "SELL", 0.70, 100)); ts += 1
        for i in range(4):
            trades.append(_t(ts, f"loss_{i}", "BUY", 0.60, 100)); ts += 1
            trades.append(_t(ts, f"loss_{i}", "SELL", 0.40, 100)); ts += 1
        qualified = self._run(lb, {w: trades}, {w: []})
        self.assertEqual(qualified, [])

    def test_positions_losses_qualify_wallet_with_verified_win_rate(self):
        """/positions resolved outcomes fold into the verified win-rate that
        flows through the gate. Active round-trips (4 wins, 1 loss) clear the
        streak gate; /positions adds 1 more resolved win → 5W/1L = ~83%, still
        above the 60% bar, and the wallet qualifies with that combined rate."""
        w = "0x" + "e" * 40
        lb = [{"proxyWallet": w, "rank": 1, "pnl": 5e3, "vol": 5e4, "userName": "holder"}]
        trades = []
        ts = 0.0
        for i in range(4):
            trades.append(_t(ts, f"win_{i}", "BUY", 0.30, 100)); ts += 1
            trades.append(_t(ts, f"win_{i}", "SELL", 0.70, 100)); ts += 1
        trades.append(_t(ts, "loss_0", "BUY", 0.60, 100)); ts += 1
        trades.append(_t(ts, "loss_0", "SELL", 0.40, 100)); ts += 1
        positions = [{"redeemable": True, "curPrice": 1.0, "cashPnl": 60.0}]
        qualified = self._run(lb, {w: trades}, {w: []}, {w: positions})
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["pos_resolved_wins"], 1)
        self.assertEqual(qualified[0]["loss_visibility"], "verified")
        # 5 verified wins / 6 verified total
        self.assertAlmostEqual(qualified[0]["win_rate"], 5 / 6, places=4)

    def test_positions_losses_drag_wallet_below_win_rate_bar(self):
        """The whole point: /positions can DRAG a wallet under the bar. Active
        round-trips alone read 4W/1L (80%, would qualify), but /positions
        surfaces 3 previously-invisible resolved losers → 4W/4L = 50%, below the
        60% gate, so the wallet is now rejected."""
        w = "0x" + "f" * 40
        lb = [{"proxyWallet": w, "rank": 1, "pnl": 5e3, "vol": 5e4, "userName": "hidden_losses"}]
        trades = []
        ts = 0.0
        for i in range(4):
            trades.append(_t(ts, f"win_{i}", "BUY", 0.30, 100)); ts += 1
            trades.append(_t(ts, f"win_{i}", "SELL", 0.70, 100)); ts += 1
        trades.append(_t(ts, "loss_0", "BUY", 0.60, 100)); ts += 1
        trades.append(_t(ts, "loss_0", "SELL", 0.40, 100)); ts += 1
        positions = [{"redeemable": True, "curPrice": 0.0, "cashPnl": -60.0} for _ in range(3)]
        qualified = self._run(lb, {w: trades}, {w: []}, {w: positions})
        self.assertEqual(qualified, [])

    def test_loss_streak_rejects_otherwise_qualified_wallet(self):
        """A wallet with a strong overall record (8W/3L = ~73%, max_streak 8)
        but whose three MOST RECENT outcomes are losses must be rejected when
        max_loss_streak=3, even though it clears win-rate, sample, and streak
        gates. Same fixture WITHOUT the cutoff (max_loss_streak=0) qualifies."""
        w = "0x" + "9" * 40
        lb = [{"proxyWallet": w, "rank": 1, "pnl": 1e3, "vol": 1e4, "userName": "streaky"}]
        trades = []
        ts = 0.0
        for i in range(8):  # 8 winning round-trips first (builds max_streak=8)
            trades.append(_t(ts, f"win_{i}", "BUY", 0.30, 100)); ts += 1
            trades.append(_t(ts, f"win_{i}", "SELL", 0.70, 100)); ts += 1
        for i in range(3):  # then 3 LOSSES as the most recent outcomes
            trades.append(_t(ts, f"loss_{i}", "BUY", 0.60, 100)); ts += 1
            trades.append(_t(ts, f"loss_{i}", "SELL", 0.40, 100)); ts += 1

        class Http:
            async def get(self, url, params=None, **kwargs):
                if url.endswith("/v1/leaderboard"):
                    return _FakeResp(lb)
                if url.endswith("/trades"):
                    return _FakeResp(trades)
                return _FakeResp([])

        rejected = asyncio.run(discover_qualified_wallets(
            Http(), categories=["OVERALL"], min_pnl=0.0,
            min_win_rate=0.60, min_win_streak=2, min_total_trades=5,
            max_loss_streak=3,
        ))
        self.assertEqual(rejected, [])

        # Without the cutoff the same wallet qualifies (sanity that the only
        # thing keeping it out above is the loss-streak gate).
        ok = asyncio.run(discover_qualified_wallets(
            Http(), categories=["OVERALL"], min_pnl=0.0,
            min_win_rate=0.60, min_win_streak=2, min_total_trades=5,
            max_loss_streak=0,
        ))
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["current_loss_streak"], 3)

    def test_truncated_but_large_sample_can_qualify_with_higher_bar(self):
        """When closed-positions is at the cap, the required sample grows to
        max(min_total_trades*4, 20). The wallet still needs verified losses."""
        w = "0x" + "d" * 40
        lb = [{"proxyWallet": w, "rank": 1, "pnl": 1e4, "vol": 1e5, "userName": "lots"}]
        trades = []
        ts = 0.0
        # 30 wins + 5 losses → easily passes inflated sample requirement of 20
        for i in range(30):
            trades.append(_t(ts, f"w_{i}", "BUY", 0.30, 100)); ts += 1
            trades.append(_t(ts, f"w_{i}", "SELL", 0.70, 100)); ts += 1
        for i in range(5):
            trades.append(_t(ts, f"l_{i}", "BUY", 0.60, 100)); ts += 1
            trades.append(_t(ts, f"l_{i}", "SELL", 0.40, 100)); ts += 1
        closed = [{"realizedPnl": 1.0, "timestamp": float(i)} for i in range(CLOSED_POSITIONS_SERVER_CAP)]
        qualified = self._run(lb, {w: trades}, {w: closed})
        self.assertEqual(len(qualified), 1)
        self.assertTrue(qualified[0]["data_truncation_risk"])
        self.assertEqual(qualified[0]["loss_visibility"], "verified")


if __name__ == "__main__":
    unittest.main()
