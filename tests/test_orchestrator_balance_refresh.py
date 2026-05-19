"""E2 regression: dual-RPC balance refresh failure aborts the trading cycle.

Verifies:
  * refresh_balance() raises BalanceRefreshError when every configured RPC
    fails or returns an empty result — never leaves stale usdc_balance.
  * A single RPC failure with the fallback succeeding still succeeds and
    stamps state.balance_refreshed_at.
  * The cycle wrapper (run_cycle) catches BalanceRefreshError, records
    "balance_refresh_failed" in state.errors, logs a warning, and returns
    early WITHOUT touching downstream trading paths (_gamma_scan, etc).
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.orchestrator import BalanceRefreshError, TradingBot


def _make_bot_skeleton(*, wallet: str = "0x" + "a" * 40) -> TradingBot:
    """Build a TradingBot instance without running __init__ (which touches
    DB-backed Settings.load and validate_risk_caps_at_startup). We only
    need the attributes refresh_balance / run_cycle read."""
    bot = TradingBot.__new__(TradingBot)
    bot.settings = SimpleNamespace(
        wallet_address=wallet,
        dry_run=False,
        default_bet_usd=10.0,
        structured_log=False,
        trading_paused=False,
        balance_buffer_usd=0.0,
        min_bet_usd=1.0,
    )
    # Minimal state surface used by refresh_balance / run_cycle aborts
    bot.state = SimpleNamespace(
        usdc_balance=42.0,
        balance_refreshed_at=0.0,
        errors=[],
        mode="live",
    )
    bot._http = None  # set per-test
    bot.clob = None
    bot._copy_manager = SimpleNamespace(needs_refresh=lambda: False)
    return bot


class TestRefreshBalanceDualRpc(unittest.IsolatedAsyncioTestCase):
    async def test_both_rpcs_raise_raises_balance_refresh_error(self) -> None:
        bot = _make_bot_skeleton()
        # Async HTTP client whose .post raises for every call.
        http = MagicMock()
        http.post = AsyncMock(side_effect=RuntimeError("rpc dead"))
        bot._http = http

        stale = bot.state.usdc_balance
        with self.assertRaises(BalanceRefreshError) as cm:
            await bot.refresh_balance()
        # Stale balance must be untouched and timestamp must NOT be stamped.
        self.assertEqual(bot.state.usdc_balance, stale)
        self.assertEqual(bot.state.balance_refreshed_at, 0.0)
        # Both RPC URLs attempted.
        self.assertEqual(http.post.call_count, 2)
        self.assertIn("dual_rpc_failure", str(cm.exception))

    async def test_both_rpcs_empty_result_raises(self) -> None:
        """Even if the RPC responds 200 but returns '0x', that's not a valid
        balance and we should escalate rather than silently accept 0."""
        bot = _make_bot_skeleton()
        resp = MagicMock()
        resp.json.return_value = {"result": "0x"}
        http = MagicMock()
        http.post = AsyncMock(return_value=resp)
        bot._http = http

        with self.assertRaises(BalanceRefreshError):
            await bot.refresh_balance()
        self.assertEqual(bot.state.balance_refreshed_at, 0.0)
        self.assertEqual(http.post.call_count, 2)

    async def test_single_failure_fallback_succeeds(self) -> None:
        bot = _make_bot_skeleton()
        good_resp = MagicMock()
        # 100 USDC (6 decimals) = 100_000_000 = 0x5f5e100
        good_resp.json.return_value = {"result": hex(100_000_000)}
        http = MagicMock()
        http.post = AsyncMock(side_effect=[RuntimeError("first rpc down"), good_resp])
        bot._http = http

        before = time.time()
        await bot.refresh_balance()
        # Balance updated from the fallback RPC.
        self.assertAlmostEqual(bot.state.usdc_balance, 100.0, places=4)
        # Timestamp was stamped on success.
        self.assertGreaterEqual(bot.state.balance_refreshed_at, before)
        # Both URLs were attempted (first raised, second succeeded).
        self.assertEqual(http.post.call_count, 2)


class TestRunCycleAbortsOnBalanceFailure(unittest.IsolatedAsyncioTestCase):
    async def test_run_cycle_aborts_when_balance_refresh_raises(self) -> None:
        """Cycle wrapper must catch BalanceRefreshError, mark the error,
        and return BEFORE touching positions/scans/agents/execution."""
        bot = _make_bot_skeleton()
        bot._http = MagicMock()  # truthy so cycle proceeds past initial guard
        bot.clob = MagicMock()   # truthy so cycle does not early-return
        # Force the dual-RPC failure path.
        bot.refresh_balance = AsyncMock(side_effect=BalanceRefreshError("dual_rpc_failure:test"))

        # Sentinels for downstream paths that must NOT run.
        bot.refresh_positions = AsyncMock()
        bot._gamma_scan = AsyncMock()
        bot.refresh_open_orders = AsyncMock()
        bot._execute_intent = AsyncMock()
        bot._reload_settings_async = AsyncMock()

        await bot.run_cycle()

        # Error recorded with the contract string the dashboard checks.
        self.assertIn("balance_refresh_failed", bot.state.errors)
        # NONE of the trading-path coroutines were invoked.
        bot.refresh_positions.assert_not_called()
        bot._gamma_scan.assert_not_called()
        bot.refresh_open_orders.assert_not_called()
        bot._execute_intent.assert_not_called()

    async def test_run_cycle_warning_logged(self) -> None:
        bot = _make_bot_skeleton()
        bot._http = MagicMock()
        bot.clob = MagicMock()
        bot.refresh_balance = AsyncMock(side_effect=BalanceRefreshError("dual_rpc_failure:test"))
        bot.refresh_positions = AsyncMock()
        bot._gamma_scan = AsyncMock()
        bot._reload_settings_async = AsyncMock()

        with patch("bot.orchestrator.log") as mock_log:
            await bot.run_cycle()
            # A WARNING-level log was emitted explaining the abort.
            self.assertTrue(mock_log.warning.called)
            msg = " ".join(
                str(a) for call in mock_log.warning.call_args_list for a in call.args
            )
            self.assertIn("balance_refresh_failed", msg)


if __name__ == "__main__":
    unittest.main()
