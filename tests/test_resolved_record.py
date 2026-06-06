"""Tests for bot.resolved_record — offline (no network calls)."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from bot.resolved_record import compute_resolved_record, resolve_token


def _mock_http(status: int = 200, body: dict | None = None):
    """Build a minimal async httpx-alike that returns the given response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    http = MagicMock()
    http.get = AsyncMock(return_value=resp)
    return http


class TestResolveToken(unittest.IsolatedAsyncioTestCase):
    """Unit tests for resolve_token."""

    async def test_closed_price_high_returns_won(self):
        http = _mock_http(
            body={
                "closed": True,
                "tokens": [{"token_id": "tok1", "price": 1.0}],
            }
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "won")

    async def test_closed_price_zero_returns_lost(self):
        http = _mock_http(
            body={
                "closed": True,
                "tokens": [{"token_id": "tok1", "price": 0.0}],
            }
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "lost")

    async def test_closed_price_below_half_returns_lost(self):
        http = _mock_http(
            body={
                "closed": True,
                "tokens": [{"token_id": "tok1", "price": 0.3}],
            }
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "lost")

    async def test_closed_price_exactly_half_returns_won(self):
        http = _mock_http(
            body={
                "closed": True,
                "tokens": [{"token_id": "tok1", "price": 0.5}],
            }
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "won")

    async def test_not_closed_returns_open(self):
        http = _mock_http(
            body={
                "closed": False,
                "tokens": [{"token_id": "tok1", "price": 0.8}],
            }
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "open")

    async def test_no_closed_field_returns_open(self):
        http = _mock_http(body={"tokens": [{"token_id": "tok1", "price": 0.9}]})
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "open")

    async def test_http_error_returns_none(self):
        http = _mock_http(status=500, body={})
        result = await resolve_token(http, "cid1", "tok1")
        self.assertIsNone(result)

    async def test_winner_flag_true_returns_won(self):
        http = _mock_http(
            body={
                "closed": True,
                "tokens": [{"token_id": "tok1", "winner": True}],
            }
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "won")

    async def test_winner_flag_false_returns_lost(self):
        http = _mock_http(
            body={
                "closed": True,
                "tokens": [{"token_id": "tok1", "winner": False}],
            }
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "lost")

    async def test_network_exception_returns_none(self):
        http = MagicMock()
        http.get = AsyncMock(side_effect=Exception("timeout"))
        result = await resolve_token(http, "cid1", "tok1")
        self.assertIsNone(result)

    # --- value-aware: open market whose price has already decided ---
    async def test_open_price_collapsed_returns_losing(self):
        http = _mock_http(
            body={"closed": False, "tokens": [{"token_id": "tok1", "price": 0.02}]}
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "losing")

    async def test_open_price_spiked_returns_winning(self):
        http = _mock_http(
            body={"closed": False, "tokens": [{"token_id": "tok1", "price": 0.98}]}
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "winning")

    async def test_open_price_midrange_returns_open(self):
        http = _mock_http(
            body={"closed": False, "tokens": [{"token_id": "tok1", "price": 0.5}]}
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "open")

    async def test_open_token_absent_returns_open(self):
        http = _mock_http(
            body={"closed": False, "tokens": [{"token_id": "other", "price": 0.01}]}
        )
        result = await resolve_token(http, "cid1", "tok1")
        self.assertEqual(result, "open")


class TestComputeResolvedRecord(unittest.IsolatedAsyncioTestCase):
    """Unit tests for compute_resolved_record."""

    def _make_trades(self):
        return [
            # strategy A, won
            {"condition_id": "cA", "token_id": "tA1", "cost_usd": 4.0, "price": 0.4, "strategy": "copy_trade"},
            # strategy A, lost
            {"condition_id": "cA2", "token_id": "tA2", "cost_usd": 5.0, "price": 0.5, "strategy": "copy_trade"},
            # strategy B, won
            {"condition_id": "cB", "token_id": "tB1", "cost_usd": 2.0, "price": 0.2, "strategy": "weather_arb"},
            # strategy B, open (unresolved)
            {"condition_id": "cB2", "token_id": "tB2", "cost_usd": 3.0, "price": 0.3, "strategy": "weather_arb"},
        ]

    def _make_http_for_trades(self):
        """HTTP mock that returns known outcomes per condition_id."""

        async def _get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "cA" in url and "cA2" not in url:
                resp.json.return_value = {"closed": True, "tokens": [{"token_id": "tA1", "price": 1.0}]}
            elif "cA2" in url:
                resp.json.return_value = {"closed": True, "tokens": [{"token_id": "tA2", "price": 0.0}]}
            elif "cB" in url and "cB2" not in url:
                resp.json.return_value = {"closed": True, "tokens": [{"token_id": "tB1", "price": 1.0}]}
            else:
                # cB2 — not closed yet
                resp.json.return_value = {"closed": False, "tokens": [{"token_id": "tB2", "price": 0.6}]}
            return resp

        http = MagicMock()
        http.get = _get
        return http

    async def test_correct_overall_stats(self):
        cache: dict = {}
        trades = self._make_trades()
        http = self._make_http_for_trades()
        result = await compute_resolved_record(http, trades, cache)

        overall = result["overall"]
        self.assertEqual(overall["wins"], 2)   # tA1 + tB1
        self.assertEqual(overall["losses"], 1) # tA2
        self.assertEqual(overall["pending"], 1) # tB2

    async def test_correct_per_strategy_stats(self):
        cache: dict = {}
        trades = self._make_trades()
        http = self._make_http_for_trades()
        result = await compute_resolved_record(http, trades, cache)

        copy = result["by_strategy"]["copy_trade"]
        self.assertEqual(copy["wins"], 1)
        self.assertEqual(copy["losses"], 1)
        self.assertEqual(copy["pending"], 0)

        weather = result["by_strategy"]["weather_arb"]
        self.assertEqual(weather["wins"], 1)
        self.assertEqual(weather["losses"], 0)
        self.assertEqual(weather["pending"], 1)

    async def test_win_rate_calculation(self):
        cache: dict = {}
        trades = self._make_trades()
        http = self._make_http_for_trades()
        result = await compute_resolved_record(http, trades, cache)

        overall = result["overall"]
        # 2 wins, 1 loss -> win_rate = 2/3
        self.assertAlmostEqual(overall["win_rate"], 2 / 3, places=5)

        copy = result["by_strategy"]["copy_trade"]
        self.assertAlmostEqual(copy["win_rate"], 0.5, places=5)

    async def test_win_rate_none_when_no_resolved(self):
        http = _mock_http(body={"closed": False})
        cache: dict = {}
        trades = [{"condition_id": "cX", "token_id": "tX", "cost_usd": 5.0, "price": 0.5, "strategy": "copy_trade"}]
        result = await compute_resolved_record(http, trades, cache)
        self.assertIsNone(result["overall"]["win_rate"])

    async def test_realized_pnl_won(self):
        """Won trade: shares = cost/price, pnl = shares - cost."""
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "t1", "cost_usd": 4.0, "price": 0.4, "strategy": "copy_trade"}]
        http = _mock_http(body={"closed": True, "tokens": [{"token_id": "t1", "price": 1.0}]})
        result = await compute_resolved_record(http, trades, cache)
        # shares = 4.0 / 0.4 = 10.0; pnl = 10.0 - 4.0 = 6.0
        self.assertAlmostEqual(result["overall"]["realized_pnl"], 6.0, places=4)

    async def test_realized_pnl_lost(self):
        """Lost trade: pnl = -cost_usd."""
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "t1", "cost_usd": 5.0, "price": 0.5, "strategy": "copy_trade"}]
        http = _mock_http(body={"closed": True, "tokens": [{"token_id": "t1", "price": 0.0}]})
        result = await compute_resolved_record(http, trades, cache)
        self.assertAlmostEqual(result["overall"]["realized_pnl"], -5.0, places=4)

    async def test_cache_prevents_re_resolution(self):
        """Cached tokens must not trigger HTTP calls."""
        cache = {"t1": "won", "t2": "lost"}
        trades = [
            {"condition_id": "c1", "token_id": "t1", "cost_usd": 2.0, "price": 0.2, "strategy": "copy_trade"},
            {"condition_id": "c2", "token_id": "t2", "cost_usd": 3.0, "price": 0.5, "strategy": "copy_trade"},
        ]
        http = MagicMock()
        http.get = AsyncMock(side_effect=AssertionError("HTTP should not be called for cached tokens"))
        result = await compute_resolved_record(http, trades, cache)
        # No exception means no HTTP calls were made.
        self.assertEqual(result["overall"]["wins"], 1)
        self.assertEqual(result["overall"]["losses"], 1)

    async def test_cache_stores_resolved_outcomes(self):
        """Resolved tokens (won/lost) should be stored in cache after compute."""
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "t1", "cost_usd": 2.0, "price": 0.2, "strategy": "copy_trade"}]
        http = _mock_http(body={"closed": True, "tokens": [{"token_id": "t1", "price": 1.0}]})
        await compute_resolved_record(http, trades, cache)
        self.assertEqual(cache.get("t1"), "won")

    async def test_cache_does_not_store_open(self):
        """Open tokens must NOT be cached so they get retried next cycle."""
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "t1", "cost_usd": 2.0, "price": 0.5, "strategy": "copy_trade"}]
        http = _mock_http(body={"closed": False})
        await compute_resolved_record(http, trades, cache)
        self.assertNotIn("t1", cache)

    async def test_deduplication_single_http_call_per_token(self):
        """Same token_id in two rows should only trigger one HTTP GET."""
        call_count = 0

        async def _get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"closed": True, "tokens": [{"token_id": "t1", "price": 1.0}]}
            return resp

        http = MagicMock()
        http.get = _get

        cache: dict = {}
        trades = [
            {"condition_id": "c1", "token_id": "t1", "cost_usd": 2.0, "price": 0.2, "strategy": "copy_trade"},
            {"condition_id": "c1", "token_id": "t1", "cost_usd": 3.0, "price": 0.3, "strategy": "copy_trade"},
        ]
        await compute_resolved_record(http, trades, cache)
        self.assertEqual(call_count, 1)

    async def test_losing_lean_counted_not_pending(self):
        """A still-open position collapsed to ~0 must show as a leaning loss, not pending."""
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "t1", "cost_usd": 2.0, "price": 0.4, "strategy": "weather_arb"}]
        http = _mock_http(body={"closed": False, "tokens": [{"token_id": "t1", "price": 0.001}]})
        result = await compute_resolved_record(http, trades, cache)
        w = result["by_strategy"]["weather_arb"]
        self.assertEqual(w["leaning_losses"], 1)
        self.assertEqual(w["pending"], 0)
        self.assertEqual(w["losses"], 0)            # NOT realized yet
        self.assertNotIn("t1", cache)               # leans are not cached
        self.assertAlmostEqual(w["unrealized_pnl"], -2.0, places=4)
        self.assertAlmostEqual(w["realized_pnl"], 0.0, places=4)

    async def test_winning_lean_counted(self):
        """A still-open position spiked to ~1 must show as a leaning win with unrealized gain."""
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "t1", "cost_usd": 4.0, "price": 0.4, "strategy": "copy_trade"}]
        http = _mock_http(body={"closed": False, "tokens": [{"token_id": "t1", "price": 0.99}]})
        result = await compute_resolved_record(http, trades, cache)
        c = result["by_strategy"]["copy_trade"]
        self.assertEqual(c["leaning_wins"], 1)
        self.assertEqual(c["pending"], 0)
        # shares = 4/0.4 = 10; unrealized = 10 - 4 = 6
        self.assertAlmostEqual(c["unrealized_pnl"], 6.0, places=4)

    async def test_exited_position_not_counted_as_lean(self):
        """An open-market token we no longer hold (sold/redeemed) is 'exited', not a lean.

        Mirrors the manually-closed weather bet: TradeLog still has the buy, the
        token now prices at ~1, but it is gone from the wallet, so it must not
        inflate leaning_wins.
        """
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "tSOLD", "cost_usd": 2.0, "price": 0.5, "strategy": "weather_arb"}]
        http = _mock_http(body={"closed": False, "tokens": [{"token_id": "tSOLD", "price": 0.9995}]})
        result = await compute_resolved_record(http, trades, cache, held_tokens=set())
        w = result["by_strategy"]["weather_arb"]
        self.assertEqual(w["exited"], 1)
        self.assertEqual(w["leaning_wins"], 0)
        self.assertEqual(w["pending"], 0)
        self.assertAlmostEqual(w["unrealized_pnl"], 0.0, places=4)

    async def test_held_losing_position_still_counts_as_lean(self):
        """A losing token we DO still hold remains a leaning loss (not exited)."""
        cache: dict = {}
        trades = [{"condition_id": "c1", "token_id": "tHELD", "cost_usd": 2.0, "price": 0.4, "strategy": "weather_arb"}]
        http = _mock_http(body={"closed": False, "tokens": [{"token_id": "tHELD", "price": 0.001}]})
        result = await compute_resolved_record(http, trades, cache, held_tokens={"tHELD"})
        w = result["by_strategy"]["weather_arb"]
        self.assertEqual(w["leaning_losses"], 1)
        self.assertEqual(w["exited"], 0)
        self.assertAlmostEqual(w["unrealized_pnl"], -2.0, places=4)


if __name__ == "__main__":
    unittest.main()
