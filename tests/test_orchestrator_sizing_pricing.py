"""E9 regression suite: EV clamp, sizing budget guard, fill-price recording.

Covers four invariants asserted by Task 9:

  1. EV math at a valid-but-small entry price (0.005) does NOT inflate the
     share count by clamping to 0.01 — the clamp now matches the gate
     threshold at line 58 of ev_math.py.

  2. When intent.size_usd cannot afford even one share at the executable
     price (1-share cost > 1.05 * intent_usd), _execute_intent rejects the
     order, logs a WARNING, and records an "intent_too_small:..." marker in
     state.errors so the dashboard / operator sees the abort.

  3. After a live market_fok fill, TradeRecord.price stores the actual fill
     price returned by client.get_order — not the limit. The strategy field
     is tagged with price_source=fok_fill.

  4. After a limit-GTD that's still pending (or returns no extractable fill
     price), TradeRecord.price falls back to the limit price BUT the strategy
     field is tagged with price_source=limit_pending so downstream P&L can
     mark the entry as provisional.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.categories import MarketCategory
from bot.ev_math import compute_ev
from bot.models import TradeIntent
from bot.orchestrator import TradingBot


def _make_bot_skeleton(*, dry_run: bool = False) -> TradingBot:
    """Build a TradingBot without invoking __init__ — same trick used by
    test_orchestrator_balance_refresh: avoid touching the DB-backed
    Settings.load path and risk-cap validation."""
    bot = TradingBot.__new__(TradingBot)
    bot.settings = SimpleNamespace(
        dry_run=dry_run,
        structured_log=False,
        order_ttl_seconds=10,
        order_poll_seconds=0.1,
        paper_realism_enabled=False,
        paper_slippage_model_bps=0.0,
        follower_latency_ms=0.0,
        allow_market_fallback=True,
        strict_execution=False,
    )
    bot.state = SimpleNamespace(
        errors=[],
        trades_placed=0,
        trades_filled=0,
        trade_history=[],
        last_trade=None,
    )
    bot.clob = None
    bot._http = None
    # _execute_intent calls self._rate_limit() at the top — stub it.
    bot._rate_limit = AsyncMock()
    # _paper_portfolio is only touched on the dry-run branch; provide a
    # minimal stub anyway so test #3 (live path) doesn't need to special-case.
    bot._paper_portfolio = MagicMock()
    return bot


def _make_intent(*, size_usd: float = 5.0, max_price: float = 0.50) -> TradeIntent:
    return TradeIntent(
        agent="test_agent",
        priority=1,
        token_id="0x" + "a" * 60,
        condition_id="0x" + "b" * 60,
        question="Will it test?",
        outcome="YES",
        side="BUY",
        max_price=max_price,
        size_usd=size_usd,
        category=MarketCategory.OTHER,
        strategy="t9_test",
        reason="unit test",
    )


class TestEVClampAlignsWithGate(unittest.TestCase):
    """Defect #9: at entry_price=0.005 the rejection gate at ev_math.py:58
    passes (>0.001), but the share-count clamp used to floor entry to 0.01,
    inflating shares by 2x. After the fix the clamp matches the gate so
    shares = size_usd / entry_price exactly."""

    def test_small_price_no_share_inflation(self) -> None:
        size_usd = 10.0
        entry = 0.005
        # Use fair_price that yields a positive EV without tripping other gates.
        r = compute_ev(
            entry_price=entry,
            fair_price=0.10,
            size_usd=size_usd,
        )
        # The gate must allow this market through (entry > 0.001).
        self.assertNotEqual(r.reason, "extreme_entry_price")
        self.assertTrue(r.passes, f"expected pass, got reason={r.reason}")
        # Derive shares the way the math now does it: clamp is 0.001 so a
        # 0.005 entry yields exactly 10/0.005 = 2000 shares. With the old
        # 0.01 clamp it would have been 10/0.01 = 1000 (inflation by 2x —
        # actually a deflation of profit, but the per-share economics ended
        # up doubled in the *_per_share fields). The expected_profit number
        # has to reflect 2000 shares, not 1000.
        expected_shares = size_usd / entry
        # absolute_expected_profit = (fair - entry) * shares = 0.095 * 2000
        # = 190. With the buggy 0.01 clamp it would be 0.095 * 1000 = 95.
        self.assertAlmostEqual(r.absolute_expected_profit_usd, 0.095 * expected_shares, places=4)

    def test_extreme_low_still_rejected(self) -> None:
        """Sanity: the gate itself is untouched — 0.001 and below still reject."""
        r = compute_ev(entry_price=0.001, fair_price=0.5, size_usd=10.0)
        self.assertFalse(r.passes)
        self.assertEqual(r.reason, "extreme_entry_price")


class TestSizingBudgetGuard(unittest.IsolatedAsyncioTestCase):
    """Defect #10: forcing size_shares to 1.0 silently over-spent when
    price * 1 > intent.size_usd. With the guard, those intents reject."""

    async def test_one_share_cost_over_budget_rejects(self) -> None:
        bot = _make_bot_skeleton(dry_run=False)
        # Mock clob with a tick size that does not perturb the price.
        clob = MagicMock()
        clob.get_tick_size.return_value = 0.01
        bot.clob = clob

        # price=0.80, intent_usd=0.50 → size_shares = 0.62, snaps to 1.0,
        # cost would be 0.80 > 0.50 * 1.05 = 0.525 → must reject.
        intent = _make_intent(size_usd=0.50, max_price=0.80)

        with patch("bot.orchestrator.place_limit_gtd_then_wait", new=AsyncMock()) as posted:
            ok = await bot._execute_intent(intent)

        self.assertFalse(ok, "execute should reject when 1-share cost exceeds budget")
        posted.assert_not_called()
        # state.errors must carry the structured marker so the dashboard sees it.
        self.assertTrue(
            any(e.startswith("intent_too_small:") for e in bot.state.errors),
            f"expected intent_too_small marker in errors, got: {bot.state.errors}",
        )

    async def test_one_share_cost_within_tolerance_allowed(self) -> None:
        """Within 5% tolerance the 1-share floor is still permitted."""
        bot = _make_bot_skeleton(dry_run=False)
        clob = MagicMock()
        clob.get_tick_size.return_value = 0.01
        # get_order must succeed for the post-fill path; return a minimal dict.
        clob.get_order.return_value = {"status": "FILLED", "size_matched": 1.0, "original_size": 1.0}
        bot.clob = clob

        # price=0.52, intent_usd=0.50 → cost_if_one_share=0.52 < 0.525 → allowed.
        intent = _make_intent(size_usd=0.50, max_price=0.52)

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-allowed", "filled:FILLED")),
        ), patch("bot.orchestrator.append_trade_log"):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        self.assertFalse(
            any(e.startswith("intent_too_small:") for e in bot.state.errors),
            f"should NOT have intent_too_small in errors: {bot.state.errors}",
        )
        # 1-share floor was applied.
        self.assertEqual(len(bot.state.trade_history), 1)
        self.assertEqual(bot.state.trade_history[0].size, 1.0)


class TestFillPriceRecording(unittest.IsolatedAsyncioTestCase):
    """Defect #11: TradeRecord.price must be the ACTUAL fill price (or VWAP)
    when available, not the limit. When fill data isn't reachable, we fall
    back to the limit but tag price_source=limit_pending in the strategy."""

    async def test_fok_fill_records_fill_price_not_limit(self) -> None:
        bot = _make_bot_skeleton(dry_run=False)
        clob = MagicMock()
        clob.get_tick_size.return_value = 0.01
        # The fill came in cheaper than our limit: limit 0.50, fill 0.46.
        clob.get_order.return_value = {
            "status": "FILLED",
            "average_price": "0.46",
            "size_matched": 20.0,
            "original_size": 20.0,
        }
        bot.clob = clob

        intent = _make_intent(size_usd=10.0, max_price=0.50)

        # Simulate the cancel→FOK fallback path: GTD cancels then market_fok fires.
        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-gtd", "cancelled_ttl")),
        ), patch(
            "bot.orchestrator.place_market_fok_fallback",
            new=AsyncMock(return_value=("oid-fok", "market_fok")),
        ), patch("bot.orchestrator.append_trade_log"):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        self.assertEqual(len(bot.state.trade_history), 1)
        rec = bot.state.trade_history[0]
        # The recorded price MUST be the fill (0.46), not the limit (0.50).
        self.assertAlmostEqual(rec.price, 0.46, places=4)
        # And the strategy field carries the price_source tag for downstream.
        self.assertIn("price_source=fok_fill", rec.strategy)

    async def test_limit_pending_falls_back_to_limit_with_tag(self) -> None:
        """When the order is still pending (cancelled_ttl with no fallback),
        we record the limit price but tag it limit_pending."""
        bot = _make_bot_skeleton(dry_run=False)
        # Disable the FOK fallback so the path stays in cancelled status.
        bot.settings.allow_market_fallback = False
        clob = MagicMock()
        clob.get_tick_size.return_value = 0.01
        bot.clob = clob

        intent = _make_intent(size_usd=10.0, max_price=0.50)

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-pending", "cancelled_ttl")),
        ), patch("bot.orchestrator.append_trade_log"):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        rec = bot.state.trade_history[0]
        # Limit price is the only number we have; record it but tag it.
        self.assertAlmostEqual(rec.price, 0.50, places=4)
        self.assertIn("price_source=limit_pending", rec.strategy)


if __name__ == "__main__":
    unittest.main()
