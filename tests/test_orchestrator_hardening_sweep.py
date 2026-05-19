"""E14 (T15) hardening sweep — one focused test per sub-item.

Bundled into a single file by captain's discretion (Q2). Each sub-item has
its own TestCase so failures localize cleanly.

Sub-items covered:
  a. Dry-run does NOT reset the live circuit breaker.
  b. Same-cycle dedupe by token in _execute_intent.
  c. CopyManager cache TTL on read.
  d. Case-insensitive category cap lookup in _advanced_gates_ok.
  e. Reconcile depth bounded at 200.
  f. Paper-portfolio + trade-record atomic with rollback.
  g. Same-cycle balance reservation (effective_balance = balance - reserved).
  h. market_intel.py ms/sec threshold configurable + bounded.
"""

from __future__ import annotations

import importlib
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.categories import MarketCategory
from bot.copy_manager import CopyManager, WalletStats
from bot.models import TradeIntent
from bot.orchestrator import TradingBot


def _intent(token: str = "0xTOK", size: float = 5.0) -> TradeIntent:
    return TradeIntent(
        agent="value_edge",
        priority=1,
        token_id=token,
        condition_id="0xcid",
        question="Will X happen?",
        outcome="Yes",
        side="BUY",
        max_price=0.50,
        size_usd=size,
        category=MarketCategory.OTHER,
        strategy="value:yes",
        reason="test",
    )


def _make_bot(*, dry_run: bool = True) -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.settings = SimpleNamespace(
        dry_run=dry_run,
        structured_log=False,
        order_ttl_seconds=45,
        order_poll_seconds=2.0,
        paper_realism_enabled=False,
        paper_slippage_model_bps=0.0,
        follower_latency_ms=0.0,
        allow_market_fallback=False,
        strict_execution=True,
        reconcile_history_depth=15,
        max_category_exposure_usd=0.0,
        category_exposure_caps={},
        max_condition_exposure_usd=0.0,
        max_daily_notional_usd=0.0,
        daily_notional_window_hours=24.0,
        spread_gate_enabled=False,
        resolution_gate_enabled=False,
        min_hours_to_resolution=0.0,
    )
    bot.state = SimpleNamespace(
        errors=[],
        trade_history=[],
        trades_placed=0,
        trades_filled=0,
        last_trade=None,
        usdc_balance=1000.0,
        reserved_usdc=0.0,
        positions=[],
        open_orders=[],
        consecutive_exec_failures=0,
        persist_failures=0,
    )
    bot.clob = None
    bot._paper_portfolio = MagicMock()
    bot._rate_limit = AsyncMock()
    bot._last_api = 0.0
    bot._cycle_inflight_tokens = set()
    return bot


class TestSubItemA_DryRunDoesNotResetBreaker(unittest.TestCase):
    def test_dry_run_success_does_not_reset_failures(self) -> None:
        bot = _make_bot()
        bot.state.consecutive_exec_failures = 5
        bot._note_exec_result(True, is_dry_run=True)
        self.assertEqual(bot.state.consecutive_exec_failures, 5)

    def test_live_success_resets_failures(self) -> None:
        bot = _make_bot()
        bot.state.consecutive_exec_failures = 5
        bot._note_exec_result(True, is_dry_run=False)
        self.assertEqual(bot.state.consecutive_exec_failures, 0)

    def test_failure_increments_regardless(self) -> None:
        bot = _make_bot()
        bot.state.consecutive_exec_failures = 5
        bot._note_exec_result(False, is_dry_run=True)
        self.assertEqual(bot.state.consecutive_exec_failures, 6)


class TestSubItemB_SameCycleTokenDedupe(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_token_skipped_with_warning(self) -> None:
        bot = _make_bot()
        intent = _intent(token="0xDUP")
        bot._cycle_inflight_tokens.add("0xDUP")

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid", "dry_run")),
        ) as m_submit, patch("bot.orchestrator.log") as mock_log:
            ok = await bot._execute_intent(intent)

        self.assertFalse(ok)
        m_submit.assert_not_called()
        msgs = " ".join(str(a) for c in mock_log.warning.call_args_list for a in c.args)
        self.assertIn("same_cycle_duplicate_token_skipped", msgs)

    async def test_first_intent_adds_token_to_set(self) -> None:
        bot = _make_bot()
        intent = _intent(token="0xFIRST")

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-x", "dry_run")),
        ), patch("bot.orchestrator.append_trade_log"), patch(
            "bot.orchestrator.append_paper_trade_log"
        ):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        self.assertIn("0xFIRST", bot._cycle_inflight_tokens)


class TestSubItemC_CopyManagerCacheTTL(unittest.IsolatedAsyncioTestCase):
    async def test_stale_cache_forces_recheck(self) -> None:
        """A wallet whose last_checked is well beyond TTL must NOT reuse cache."""
        from bot import copy_manager as cm_mod

        settings = SimpleNamespace(
            copy_watch_wallets=[],
            copy_refresh_interval_hours=6.0,
            copy_min_win_rate=0.60,
            copy_min_win_streak=3,
            copy_min_total_trades=5,
            copy_max_watched_wallets=50,
            copy_auto_manage=True,
            copy_discover_categories=["OVERALL"],
        )
        cm = CopyManager(settings)
        # Insert a wallet with a very stale last_checked.
        cm.state.wallet_stats["0xstale"] = WalletStats(
            wallet="0xstale",
            added_at=time.time() - 10_000,
            last_checked=time.time() - (cm_mod._CACHE_TTL_SECONDS * 2),
            win_rate=0.7,
            wins=10,
            losses=3,
            status="active",
        )

        http = MagicMock()
        # Simulate the wallet now sub-threshold so prune fires.
        with patch(
            "bot.copy_manager.analyze_wallet_quality",
            new=AsyncMock(return_value={
                "win_rate": 0.20, "wins": 2, "losses": 8,
                "max_streak": 0, "current_streak": 0, "total_pnl": -50.0,
                "total": 10,
            }),
        ) as m_q:
            await cm._check_and_prune(http)

        m_q.assert_called_once()  # NOT short-circuited by cache
        self.assertEqual(cm.state.wallet_stats["0xstale"].status, "pruned")

    async def test_fresh_cache_uses_cached_values(self) -> None:
        from bot import copy_manager as cm_mod

        settings = SimpleNamespace(
            copy_watch_wallets=[],
            copy_refresh_interval_hours=6.0,
            copy_min_win_rate=0.60,
            copy_min_win_streak=3,
            copy_min_total_trades=5,
            copy_max_watched_wallets=50,
            copy_auto_manage=True,
            copy_discover_categories=["OVERALL"],
        )
        cm = CopyManager(settings)
        # Fresh last_checked — well inside TTL.
        cm.state.wallet_stats["0xfresh"] = WalletStats(
            wallet="0xfresh",
            added_at=time.time(),
            last_checked=time.time() - 10,  # 10s ago, < TTL
            win_rate=0.7,
            wins=10,
            losses=3,
            status="active",
        )

        http = MagicMock()
        with patch(
            "bot.copy_manager.analyze_wallet_quality",
            new=AsyncMock(),
        ) as m_q:
            await cm._check_and_prune(http)
        m_q.assert_not_called()


class TestSubItemD_CaseInsensitiveCategoryCap(unittest.IsolatedAsyncioTestCase):
    async def test_uppercase_cap_key_matches_lowercase_category(self) -> None:
        """An operator who writes a cap as 'SPORTS' should still bind to a
        sports intent (categories are normalised to lower-case on lookup)."""
        bot = TradingBot.__new__(TradingBot)
        bot.settings = SimpleNamespace(
            max_daily_notional_usd=0.0,
            max_condition_exposure_usd=0.0,
            max_category_exposure_usd=0.0,
            # Cap key is upper-case — would silently no-op without E14d.
            category_exposure_caps={"SPORTS": 10.0},
            spread_gate_enabled=False,
            resolution_gate_enabled=False,
            min_hours_to_resolution=0.0,
        )
        bot.state = SimpleNamespace(positions=[], open_orders=[])
        bot.clob = None

        intent = TradeIntent(
            agent="x", priority=1, token_id="0xT", condition_id="0xC",
            question="q", outcome="Yes", side="BUY", max_price=0.5,
            size_usd=100.0, category=MarketCategory.SPORTS,
            strategy="s", reason="r",
        )
        ok, reason = await bot._advanced_gates_ok(
            [intent], markets_by_cid={}, rolling_notional=0.0,
        )
        self.assertFalse(ok, f"expected sports cap to bind, got ok=True reason={reason}")
        self.assertIn("category_exposure_sports", reason)


class TestSubItemE_ReconcileDepthBounded(unittest.TestCase):
    def test_depth_capped_at_200(self) -> None:
        bot = _make_bot()
        bot.settings.reconcile_history_depth = 10_000
        self.assertEqual(bot._bounded_reconcile_depth(), 200)

    def test_depth_below_cap_unchanged(self) -> None:
        bot = _make_bot()
        bot.settings.reconcile_history_depth = 50
        self.assertEqual(bot._bounded_reconcile_depth(), 50)


class TestSubItemF_AtomicPaperAndTrade(unittest.IsolatedAsyncioTestCase):
    async def test_paper_failure_rolls_back_trade_history(self) -> None:
        bot = _make_bot(dry_run=True)
        intent = _intent(token="0xATOM")
        bot._paper_portfolio.record_fill.side_effect = RuntimeError("paper boom")

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(return_value=("oid-atom", "dry_run")),
        ), patch("bot.orchestrator.append_trade_log"), patch(
            "bot.orchestrator.append_paper_trade_log"
        ):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)  # cycle survives
        # trade_history rolled back; trades_placed not double-counted.
        self.assertEqual(len(bot.state.trade_history), 0)
        self.assertEqual(bot.state.trades_placed, 0)


class TestSubItemG_BalanceReservation(unittest.IsolatedAsyncioTestCase):
    async def test_reserved_usdc_increments_on_execute_and_releases_after(self) -> None:
        bot = _make_bot(dry_run=True)
        intent = _intent(token="0xRES", size=7.5)

        # Observe reserved_usdc mid-execution by intercepting place_limit.
        observed = {}

        async def fake_submit(*a, **k):
            observed["reserved_during"] = bot.state.reserved_usdc
            return ("oid-res", "dry_run")

        with patch(
            "bot.orchestrator.place_limit_gtd_then_wait",
            new=AsyncMock(side_effect=fake_submit),
        ), patch("bot.orchestrator.append_trade_log"), patch(
            "bot.orchestrator.append_paper_trade_log"
        ):
            ok = await bot._execute_intent(intent)

        self.assertTrue(ok)
        self.assertAlmostEqual(observed["reserved_during"], 7.5)
        # Released after successful execution.
        self.assertAlmostEqual(bot.state.reserved_usdc, 0.0)


class TestSubItemH_MarketIntelMsThreshold(unittest.TestCase):
    def test_env_override_lowers_threshold(self) -> None:
        # Pick a numeric value above an alternative threshold but below 1e12.
        os.environ["MARKET_INTEL_MS_THRESHOLD"] = "1000"
        try:
            import bot.market_intel as mi
            importlib.reload(mi)
            # Value of 5000 (milliseconds since epoch interpretation) should
            # now be treated as ms because 5000 > 1000.
            m = {"endDate": 5000}
            hours = mi.hours_until_resolution_end(m)
            # Expect a NEGATIVE-ish number (epoch+5s is far in the past).
            self.assertIsNotNone(hours)
            self.assertLess(hours, 0)
        finally:
            os.environ.pop("MARKET_INTEL_MS_THRESHOLD", None)
            import bot.market_intel as mi
            importlib.reload(mi)

    def test_default_threshold_is_1e12(self) -> None:
        os.environ.pop("MARKET_INTEL_MS_THRESHOLD", None)
        import bot.market_intel as mi
        importlib.reload(mi)
        self.assertEqual(mi._MS_THRESHOLD, 1e12)


if __name__ == "__main__":
    unittest.main()
