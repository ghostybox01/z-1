"""Tests for state propagation, mode syncing, dry-run execution settings wiring."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from bot.agents.registry import agents_status
from bot.execution import _simulate_paper_fill
from bot.models import BotState
from bot.settings import Settings, default_kv_seed
from bot.settings_validation import validate_and_normalize_settings_patch
from bot.copy_manager import CopyManager, WalletStats
from bot.paper_portfolio import _best_price_for_outcome


class TestModeSyncsWithDryRun(unittest.TestCase):
    """BotState.mode must stay in sync with settings.dry_run after reloads."""

    def test_initial_mode_dry(self):
        s = Settings(dry_run=True)
        state = BotState(mode="dry_run" if s.dry_run else "live")
        self.assertEqual(state.mode, "dry_run")

    def test_initial_mode_live(self):
        s = Settings(dry_run=False)
        state = BotState(mode="dry_run" if s.dry_run else "live")
        self.assertEqual(state.mode, "live")

    def test_mode_update_on_toggle(self):
        """Simulate what _reload_settings_async now does: sync state.mode."""
        state = BotState(mode="dry_run")
        s = Settings(dry_run=False)
        state.mode = "dry_run" if s.dry_run else "live"
        self.assertEqual(state.mode, "live")

        s2 = Settings(dry_run=True)
        state.mode = "dry_run" if s2.dry_run else "live"
        self.assertEqual(state.mode, "dry_run")


class TestCopyAgentStatus(unittest.TestCase):
    """Copy signal card shows ON/OFF based on agent_copy AND copy_watch_wallets."""

    def test_copy_off_when_disabled(self):
        s = Settings(agent_copy=False, copy_watch_wallets=[])
        statuses = agents_status(s)
        copy = next(a for a in statuses if a["id"] == "copy_signal")
        self.assertFalse(copy["enabled"])

    def test_copy_enabled_when_toggled_on_but_no_wallets(self):
        s = Settings(agent_copy=True, copy_watch_wallets=[])
        statuses = agents_status(s)
        copy = next(a for a in statuses if a["id"] == "copy_signal")
        self.assertTrue(copy["enabled"])

    def test_copy_on_when_enabled_and_wallets(self):
        s = Settings(agent_copy=True, copy_watch_wallets=["0x" + "a" * 40])
        statuses = agents_status(s)
        copy = next(a for a in statuses if a["id"] == "copy_signal")
        self.assertTrue(copy["enabled"])


class TestPaperFillSettings(unittest.TestCase):
    """_simulate_paper_fill respects paper_realism_enabled and settings params."""

    def test_realism_disabled_returns_plain_dry_run(self):
        result = _simulate_paper_fill(
            token_id="t" * 42,
            side="BUY",
            price=0.5,
            size=10.0,
            paper_realism_enabled=False,
            slippage_model_bps=100.0,
            latency_ms=1000.0,
        )
        self.assertEqual(result, "dry_run")

    def test_realism_enabled_returns_paper_result(self):
        result = _simulate_paper_fill(
            token_id="t" * 42,
            side="BUY",
            price=0.5,
            size=10.0,
            paper_realism_enabled=True,
            slippage_model_bps=50.0,
            latency_ms=500.0,
        )
        self.assertTrue(
            result.startswith("dry_run"),
            f"Expected dry_run prefix, got: {result}",
        )
        self.assertNotEqual(result, "dry_run")

    def test_default_realism_enabled(self):
        result = _simulate_paper_fill(
            token_id="t" * 42,
            side="BUY",
            price=0.5,
            size=10.0,
        )
        self.assertTrue(result.startswith("dry_run"))


class TestSettingsFromKv(unittest.TestCase):
    """Settings.from_kv correctly round-trips copy_watch_wallets and agent flags."""

    def test_wallets_from_kv(self):
        addr = "0x" + "ab" * 20
        kv = {"copy_watch_wallets": f'["{addr}"]', "agent_copy": "true"}
        s = Settings.from_kv(kv)
        self.assertTrue(s.agent_copy)
        self.assertEqual(s.copy_watch_wallets, [addr])

    def test_empty_wallets_from_kv(self):
        kv = {"copy_watch_wallets": "[]", "agent_copy": "true"}
        s = Settings.from_kv(kv)
        self.assertTrue(s.agent_copy)
        self.assertEqual(s.copy_watch_wallets, [])

    def test_invalid_wallet_filtered(self):
        kv = {"copy_watch_wallets": '["not_valid", "0x' + "ab" * 20 + '"]'}
        s = Settings.from_kv(kv)
        self.assertEqual(len(s.copy_watch_wallets), 1)

    def test_dry_run_defaults_true(self):
        s = Settings.from_kv({})
        self.assertTrue(s.dry_run)

    def test_dry_run_false(self):
        s = Settings.from_kv({"dry_run": "false"})
        self.assertFalse(s.dry_run)


class TestPaperRealismSettingsWiring(unittest.TestCase):
    """Settings correctly loads paper realism fields from KV."""

    def test_paper_realism_fields_from_kv(self):
        kv = {
            "paper_realism_enabled": "true",
            "paper_slippage_model_bps": "75",
            "follower_latency_ms": "300",
        }
        s = Settings.from_kv(kv)
        self.assertTrue(s.paper_realism_enabled)
        self.assertAlmostEqual(s.paper_slippage_model_bps, 75.0)
        self.assertAlmostEqual(s.follower_latency_ms, 300.0)

    def test_paper_realism_disabled(self):
        kv = {"paper_realism_enabled": "false"}
        s = Settings.from_kv(kv)
        self.assertFalse(s.paper_realism_enabled)


class TestCopySignalAdminRoundTrip(unittest.TestCase):
    """Full admin save→load→agents_status round-trip for copy signal card."""

    VALID_WALLET = "0x" + "ab" * 20

    def _simulate_admin_save_and_reload(self, admin_payload: dict) -> Settings:
        """Simulate: admin saves settings, then reloads via Settings.from_kv."""
        normalized, errors = validate_and_normalize_settings_patch(admin_payload)
        self.assertEqual(errors, {}, f"Unexpected validation errors: {errors}")
        kv = dict(default_kv_seed())
        kv.update(normalized)
        return Settings.from_kv(kv)

    def test_enable_copy_with_wallets_shows_on(self):
        """Admin enables agent_copy and adds wallets → card must show ON."""
        s = self._simulate_admin_save_and_reload({
            "agent_copy": "true",
            "copy_watch_wallets": json.dumps([self.VALID_WALLET]),
        })
        self.assertTrue(s.agent_copy)
        self.assertEqual(s.copy_watch_wallets, [self.VALID_WALLET])
        copy = next(a for a in agents_status(s) if a["id"] == "copy_signal")
        self.assertTrue(copy["enabled"], "Copy signal card should be ON")

    def test_enable_copy_without_wallets_shows_enabled(self):
        """Admin enables agent_copy but no wallets → card shows enabled (toggle on)."""
        s = self._simulate_admin_save_and_reload({
            "agent_copy": "true",
            "copy_watch_wallets": "[]",
        })
        self.assertTrue(s.agent_copy)
        self.assertEqual(s.copy_watch_wallets, [])
        copy = next(a for a in agents_status(s) if a["id"] == "copy_signal")
        self.assertTrue(copy["enabled"], "Copy signal card should reflect toggle state")

    def test_disable_copy_with_wallets_shows_off(self):
        """Admin disables agent_copy but has wallets → card must show OFF."""
        s = self._simulate_admin_save_and_reload({
            "agent_copy": "false",
            "copy_watch_wallets": json.dumps([self.VALID_WALLET]),
        })
        self.assertFalse(s.agent_copy)
        copy = next(a for a in agents_status(s) if a["id"] == "copy_signal")
        self.assertFalse(copy["enabled"], "Copy signal card should be OFF when agent disabled")

    def test_structured_form_round_trip(self):
        """Simulate the exact JS collectSettingsFromForm → Python validate → load path."""
        wallet = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        js_items = [wallet.lower()]
        js_payload = json.dumps(js_items)

        all_defaults = dict(default_kv_seed())
        all_defaults["agent_copy"] = "true"
        all_defaults["copy_watch_wallets"] = js_payload

        normalized, errors = validate_and_normalize_settings_patch(all_defaults)
        self.assertEqual(errors, {})
        self.assertEqual(normalized["agent_copy"], "true")

        kv = dict(default_kv_seed())
        kv.update(normalized)
        s = Settings.from_kv(kv)
        self.assertTrue(s.agent_copy)
        self.assertEqual(len(s.copy_watch_wallets), 1)
        self.assertEqual(s.copy_watch_wallets[0], wallet.lower())

        copy = next(a for a in agents_status(s) if a["id"] == "copy_signal")
        self.assertTrue(copy["enabled"])

    def test_multiple_wallets_all_valid(self):
        w1 = "0x" + "ab" * 20
        w2 = "0x" + "cd" * 20
        s = self._simulate_admin_save_and_reload({
            "agent_copy": "true",
            "copy_watch_wallets": json.dumps([w1, w2]),
        })
        self.assertEqual(len(s.copy_watch_wallets), 2)
        copy = next(a for a in agents_status(s) if a["id"] == "copy_signal")
        self.assertTrue(copy["enabled"])

    def test_wallet_validation_rejects_bad_address(self):
        """If wallets fail validation, the save should error, not silently drop them."""
        _, errors = validate_and_normalize_settings_patch({
            "copy_watch_wallets": json.dumps(["not_an_address"]),
        })
        self.assertIn("copy_watch_wallets", errors)

    def test_mixed_valid_invalid_wallets_rejected(self):
        """Validation rejects the entire field if any wallet is invalid."""
        _, errors = validate_and_normalize_settings_patch({
            "copy_watch_wallets": json.dumps(["bad", self.VALID_WALLET]),
        })
        self.assertIn("copy_watch_wallets", errors)


class TestAgentsStatusRuntime(unittest.TestCase):
    """agents_status returns enriched fields when cycle_runtime is provided."""

    VALID_WALLET = "0x" + "ab" * 20

    def test_without_runtime_defaults_to_false(self):
        """Without cycle_runtime, new fields default to False/0/empty."""
        s = Settings(agent_value=True)
        statuses = agents_status(s)
        ve = next(a for a in statuses if a["id"] == "value_edge")
        self.assertTrue(ve["enabled"])
        self.assertFalse(ve["scheduled"])
        self.assertFalse(ve["ran"])
        self.assertEqual(ve["intents"], 0)
        self.assertEqual(ve["note"], "")

    def test_with_runtime_reflects_agent_state(self):
        """cycle_runtime is surfaced in agent status output."""
        s = Settings(agent_value=True, agent_copy=True, copy_watch_wallets=[self.VALID_WALLET])
        rt = {
            "value_edge": {"scheduled": True, "ran": True, "intents": 3, "note": ""},
            "copy_signal": {"scheduled": True, "ran": True, "intents": 0, "note": "cold_start_seeded=15; polled=1/1; new=0"},
        }
        statuses = agents_status(s, cycle_runtime=rt)
        ve = next(a for a in statuses if a["id"] == "value_edge")
        self.assertTrue(ve["enabled"])
        self.assertTrue(ve["scheduled"])
        self.assertTrue(ve["ran"])
        self.assertEqual(ve["intents"], 3)

        cs = next(a for a in statuses if a["id"] == "copy_signal")
        self.assertTrue(cs["enabled"])
        self.assertTrue(cs["scheduled"])
        self.assertTrue(cs["ran"])
        self.assertEqual(cs["intents"], 0)
        self.assertIn("cold_start", cs["note"])

    def test_disabled_agent_not_scheduled(self):
        """Disabled agent: enabled=False, scheduled/ran/intents all False/0."""
        s = Settings(agent_copy=False, copy_watch_wallets=[])
        rt = {}
        statuses = agents_status(s, cycle_runtime=rt)
        cs = next(a for a in statuses if a["id"] == "copy_signal")
        self.assertFalse(cs["enabled"])
        self.assertFalse(cs["scheduled"])
        self.assertFalse(cs["ran"])
        self.assertEqual(cs["intents"], 0)

    def test_error_agent_ran_false(self):
        """Agent that raised an exception: ran=False, note has error."""
        s = Settings(agent_latency=True)
        rt = {
            "latency_arb": {"scheduled": True, "ran": False, "intents": 0, "note": "error: timeout"},
        }
        statuses = agents_status(s, cycle_runtime=rt)
        la = next(a for a in statuses if a["id"] == "latency_arb")
        self.assertTrue(la["enabled"])
        self.assertTrue(la["scheduled"])
        self.assertFalse(la["ran"])
        self.assertIn("error", la["note"])

    def test_triggered_state_semantics(self):
        """Dashboard: enabled + ran + intents > 0 = TRIGGERED."""
        s = Settings(agent_value=True)
        rt = {"value_edge": {"scheduled": True, "ran": True, "intents": 5, "note": ""}}
        statuses = agents_status(s, cycle_runtime=rt)
        ve = next(a for a in statuses if a["id"] == "value_edge")
        self.assertTrue(ve["enabled"])
        self.assertTrue(ve["ran"])
        self.assertGreater(ve["intents"], 0)

    def test_armed_state_semantics(self):
        """Dashboard: enabled + ran + intents == 0 = ARMED (no opportunities)."""
        s = Settings(agent_value=True)
        rt = {"value_edge": {"scheduled": True, "ran": True, "intents": 0, "note": ""}}
        statuses = agents_status(s, cycle_runtime=rt)
        ve = next(a for a in statuses if a["id"] == "value_edge")
        self.assertTrue(ve["enabled"])
        self.assertTrue(ve["ran"])
        self.assertEqual(ve["intents"], 0)


class TestCopySignalAgentDiagnostics(unittest.TestCase):
    """CopySignalAgent exposes cold_start and diagnostic notes."""

    def test_cold_start_flag(self):
        s = Settings(agent_copy=True, copy_watch_wallets=["0x" + "ab" * 20])
        from bot.agents.copy_signal import CopySignalAgent
        agent = CopySignalAgent(s)
        self.assertTrue(agent.is_cold_start)

    def test_last_note_when_disabled(self):
        import asyncio
        import httpx
        s = Settings(agent_copy=False, copy_watch_wallets=[])
        from bot.agents.copy_signal import CopySignalAgent
        agent = CopySignalAgent(s)
        result = asyncio.run(agent.propose(httpx.AsyncClient()))
        self.assertEqual(result, [])
        self.assertIn("disabled", agent.last_note)


class TestBotStateCycleRuntime(unittest.TestCase):
    """BotState.cycle_agent_runtime field exists and defaults to empty dict."""

    def test_default_empty(self):
        state = BotState()
        self.assertEqual(state.cycle_agent_runtime, {})

    def test_can_set_runtime(self):
        state = BotState()
        state.cycle_agent_runtime = {
            "value_edge": {"scheduled": True, "ran": True, "intents": 2, "note": ""},
        }
        self.assertEqual(state.cycle_agent_runtime["value_edge"]["intents"], 2)


class TestCopyManagerPersistence(unittest.TestCase):
    def test_manual_wallets_are_seeded_and_preserved(self):
        wallet = "0x" + "ab" * 20
        s = Settings(copy_watch_wallets=[wallet])
        mgr = CopyManager(s)
        self.assertIn(wallet, mgr.state.wallet_stats)
        self.assertEqual(mgr.state.wallet_stats[wallet].status, "manual")

    def test_persist_keeps_manual_plus_active_wallets(self):
        manual = "0x" + "ab" * 20
        active = "0x" + "cd" * 20
        s = Settings(copy_watch_wallets=[manual])
        mgr = CopyManager(s)
        mgr.state.wallet_stats[active] = WalletStats(wallet=active, status="active")
        with patch("bot.copy_manager.upsert_many_kv") as upsert:
            mgr._persist_wallets()
        payload = upsert.call_args.args[0]
        saved = json.loads(payload["copy_watch_wallets"])
        self.assertIn(manual, saved)
        self.assertIn(active, saved)


class TestPaperPortfolioPriceMapping(unittest.TestCase):
    def test_best_price_uses_token_id_mapping(self):
        market = {
            "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
            "outcomePrices": json.dumps(["0.61", "0.39"]),
            "outcomes": json.dumps(["Yes", "No"]),
        }
        self.assertAlmostEqual(_best_price_for_outcome(market, "tok_no", "Yes"), 0.39)

    def test_best_price_falls_back_to_outcome_name(self):
        market = {
            "outcomePrices": ["0.44", "0.56"],
            "outcomes": ["Yes", "No"],
        }
        self.assertAlmostEqual(_best_price_for_outcome(market, "", "No"), 0.56)


class TestSettingsLoadFallback(unittest.TestCase):
    """Settings.load raises when DB is configured but read fails."""

    def test_load_without_db_returns_defaults(self):
        """Without a DB engine, load falls back to defaults silently."""
        from bot.db import models
        old_engine = models._engine
        models._engine = None
        try:
            s = Settings.load()
            self.assertFalse(s.agent_copy)
            self.assertEqual(s.copy_watch_wallets, [])
        finally:
            models._engine = old_engine

    def test_load_with_db_propagates_errors(self):
        """With a DB engine configured, load must not silently swallow errors."""
        from bot.db import models
        from unittest.mock import patch

        dummy_engine = object()
        old_engine = models._engine
        models._engine = dummy_engine
        try:
            with patch("bot.db.kv.load_all_kv", side_effect=RuntimeError("db broken")):
                with self.assertRaises(RuntimeError):
                    Settings.load()
        finally:
            models._engine = old_engine


class TestReloadKeepsSettingsOnError(unittest.TestCase):
    """_reload_settings_async must keep old settings when load fails."""

    def test_old_settings_preserved_on_error(self):
        """If Settings.load raises, the bot keeps its previous settings."""
        import asyncio
        from unittest.mock import patch, AsyncMock, MagicMock

        old_settings = Settings(agent_copy=True, copy_watch_wallets=["0x" + "ab" * 20])
        bot = MagicMock()
        bot.settings = old_settings
        bot.state = BotState(mode="dry_run")
        bot._value_agent = MagicMock()
        bot._copy_agent = MagicMock()
        bot._latency_agent = MagicMock()
        bot._bundle_agent = MagicMock()
        bot._zscore_agent = MagicMock()

        from bot.orchestrator import TradingBot

        async def run_reload():
            with patch.object(Settings, "load", side_effect=RuntimeError("db error")):
                await TradingBot._reload_settings_async(bot)

        asyncio.run(run_reload())
        self.assertIs(bot.settings, old_settings,
                      "Settings should be unchanged after a failed reload")


class TestStateDictContract(unittest.TestCase):
    """get_state_dict() must always include has_private_key and paper_portfolio."""

    def _make_bot_mock(self, *, private_key="", dry_run=True, paper_summary=None):
        from unittest.mock import MagicMock
        from bot.orchestrator import TradingBot
        bot = MagicMock()
        bot.settings = Settings(
            polymarket_private_key=private_key,
            dry_run=dry_run,
        )
        bot.state = BotState()
        bot._paper_portfolio = MagicMock()
        bot._paper_portfolio.get_summary.return_value = paper_summary or {}
        bot._copy_manager = MagicMock()
        bot._copy_manager.get_summary.return_value = {}
        bot._copy_manager.get_managed_wallets.return_value = []
        return bot

    def test_has_private_key_true_when_set(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(private_key="0x" + "b" * 64)
        d = TradingBot.get_state_dict(bot)
        self.assertTrue(d["has_private_key"])

    def test_has_private_key_false_when_empty(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(private_key="")
        d = TradingBot.get_state_dict(bot)
        self.assertFalse(d["has_private_key"])

    def test_paper_portfolio_included_in_live_mode(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(dry_run=False, paper_summary={"total_invested": 5.0})
        d = TradingBot.get_state_dict(bot)
        self.assertIn("paper_portfolio", d)
        self.assertEqual(d["paper_portfolio"], {"total_invested": 5.0})

    def test_paper_portfolio_included_in_dry_run_mode(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(dry_run=True, paper_summary={"total_invested": 10.0})
        d = TradingBot.get_state_dict(bot)
        self.assertIn("paper_portfolio", d)
        self.assertEqual(d["paper_portfolio"], {"total_invested": 10.0})


if __name__ == "__main__":
    unittest.main()
