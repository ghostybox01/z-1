"""E10b regression: persist-after-success is treated as critical.

After a live order succeeds on the exchange, the DB persist call MUST be
retried once before being declared failed. A persist failure is logged at
ERROR (not WARNING), appended to state.errors as `persist_failed:order_id=…`,
and increments `state.persist_failures` so the operator sees the cycle was
degraded. The trading cycle MUST NOT crash — other orders may have already
succeeded on the exchange.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.categories import MarketCategory
from bot.models import TradeIntent
from bot.orchestrator import TradingBot


def _make_bot() -> TradingBot:
    """Build a TradingBot skeleton that exposes only what _execute_intent reads."""
    bot = TradingBot.__new__(TradingBot)
    bot.settings = SimpleNamespace(
        dry_run=True,
        structured_log=False,
        order_ttl_seconds=45,
        order_poll_seconds=2.0,
        paper_realism_enabled=False,
        paper_slippage_model_bps=0.0,
        follower_latency_ms=0.0,
        allow_market_fallback=False,
        strict_execution=True,
    )
    bot.state = SimpleNamespace(
        errors=[],
        trade_history=[],
        trades_placed=0,
        trades_filled=0,
        last_trade=None,
        persist_failures=0,
    )
    bot.clob = None
    bot._paper_portfolio = MagicMock()
    bot._rate_limit = AsyncMock()
    bot._last_api = 0.0
    return bot


def _make_intent() -> TradeIntent:
    return TradeIntent(
        agent="value_edge",
        priority=1,
        token_id="0x" + "a" * 64,
        condition_id="0xcid",
        question="Will X happen?",
        outcome="Yes",
        side="BUY",
        max_price=0.50,
        size_usd=5.0,
        category=MarketCategory.OTHER,
        strategy="value:yes",
        reason="test",
    )


class TestPersistAfterSuccessCritical(unittest.IsolatedAsyncioTestCase):
    async def test_persist_succeeds_first_try_no_error(self) -> None:
        bot = _make_bot()
        intent = _make_intent()

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-1", "dry_run")),
        ), patch(
            "bot.orchestrator.append_trade_log"
        ) as m_log, patch(
            "bot.orchestrator.append_paper_trade_log"
        ):
            m_log.return_value = None
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        self.assertEqual(bot.state.persist_failures, 0)
        self.assertNotIn("persist_failed:order_id=oid-1", bot.state.errors)
        # exactly one call — no retry needed
        self.assertEqual(m_log.call_count, 1)

    async def test_persist_retries_once_and_recovers(self) -> None:
        bot = _make_bot()
        intent = _make_intent()

        call_log = []

        def flaky(*a, **k):
            call_log.append(1)
            if len(call_log) == 1:
                raise RuntimeError("transient db hiccup")
            return None

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-2", "dry_run")),
        ), patch(
            "bot.orchestrator.append_trade_log", side_effect=flaky
        ), patch(
            "bot.orchestrator.append_paper_trade_log"
        ):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        # Retry recovered — no error, no counter bump.
        self.assertEqual(bot.state.persist_failures, 0)
        self.assertFalse(
            any("persist_failed" in e for e in bot.state.errors),
            f"unexpected persist_failed in errors: {bot.state.errors}",
        )
        self.assertEqual(len(call_log), 2)

    async def test_persist_fails_twice_records_error_and_increments_counter(self) -> None:
        bot = _make_bot()
        intent = _make_intent()

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-3", "dry_run")),
        ), patch(
            "bot.orchestrator.append_trade_log",
            side_effect=RuntimeError("db down"),
        ), patch(
            "bot.orchestrator.append_paper_trade_log"
        ), patch(
            "bot.orchestrator.log"
        ) as mock_log:
            ok = await bot._execute_intent(intent)

        # Cycle must NOT crash.
        self.assertTrue(ok)
        # Structured error appended for operator visibility.
        self.assertIn("persist_failed:order_id=oid-3", bot.state.errors)
        # Counter advanced.
        self.assertEqual(bot.state.persist_failures, 1)
        # ERROR-level log (not WARNING).
        self.assertTrue(mock_log.error.called)
        # Verify exc_info passed so stack traces are emitted.
        _, kwargs = mock_log.error.call_args
        self.assertIn("exc_info", kwargs)

    async def test_persist_failure_counter_starts_from_missing_attr(self) -> None:
        """Even if state.persist_failures is missing, the helper must not raise."""
        bot = _make_bot()
        # Remove the field entirely.
        del bot.state.persist_failures
        intent = _make_intent()

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-4", "dry_run")),
        ), patch(
            "bot.orchestrator.append_trade_log",
            side_effect=RuntimeError("db down"),
        ), patch(
            "bot.orchestrator.append_paper_trade_log"
        ):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        self.assertIn("persist_failed:order_id=oid-4", bot.state.errors)
        # Field added by getattr/setattr fallback path.
        self.assertEqual(getattr(bot.state, "persist_failures", None), 1)


if __name__ == "__main__":
    unittest.main()
