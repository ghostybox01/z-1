"""Strict admin settings validation."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot.settings import Settings
from bot.settings_validation import validate_and_normalize_settings_patch


class TestSettingsValidation(unittest.TestCase):
    def test_valid_patch(self):
        norm, err = validate_and_normalize_settings_patch(
            {
                "agent_copy": True,
                "copy_watch_wallets": ["0x" + "a" * 40],
                "copy_allowed_categories": ["politics", "crypto_short"],
                "copy_allowed_outcomes": ["yes"],
                "copy_min_price": "0.1",
                "copy_max_price": "0.9",
                "port": "5002",
            }
        )
        self.assertFalse(err)
        self.assertEqual(norm["agent_copy"], "true")
        self.assertEqual(norm["port"], "5002")

    def test_invalid_wallet_and_category(self):
        norm, err = validate_and_normalize_settings_patch(
            {
                "copy_watch_wallets": ["bad"],
                "copy_allowed_categories": ["not_a_category"],
            }
        )
        self.assertIn("copy_watch_wallets", err)
        self.assertIn("copy_allowed_categories", err)
        self.assertFalse(norm)

    def test_cross_field_price_bounds(self):
        _norm, err = validate_and_normalize_settings_patch(
            {
                "copy_min_price": "0.9",
                "copy_max_price": "0.2",
            }
        )
        self.assertIn("copy_min_price", err)

    def test_dict_float_caps(self):
        norm, err = validate_and_normalize_settings_patch(
            {
                "category_exposure_caps": {"politics": 50, "crypto_short": 25},
                "copy_wallet_score_overrides": {"0x" + "a" * 40: 0.2},
            }
        )
        self.assertFalse(err)
        self.assertIn("category_exposure_caps", norm)
        self.assertIn("copy_wallet_score_overrides", norm)


class TestRiskCapsStartupValidator(unittest.TestCase):
    """E1: hard-fail boot when risk caps are zero unless explicitly opted out."""

    EXPECTED_MSG = (
        "Risk caps disabled: set explicit non-zero caps or "
        "risk_caps_disabled=True to confirm"
    )

    def test_default_settings_raise(self):
        """Default Settings() has all three caps == 0.0 and must hard-fail."""
        s = Settings()
        # Sanity: the defaults that motivated this guard.
        self.assertEqual(s.max_condition_exposure_usd, 0.0)
        self.assertEqual(s.max_category_exposure_usd, 0.0)
        self.assertEqual(s.max_daily_notional_usd, 0.0)
        self.assertFalse(s.risk_caps_disabled)
        with self.assertRaises(ValueError) as ctx:
            s.validate_risk_caps_at_startup()
        self.assertEqual(str(ctx.exception), self.EXPECTED_MSG)

    def test_opt_in_disables_check(self):
        """risk_caps_disabled=True lets boot proceed even with zero caps."""
        s = Settings(risk_caps_disabled=True)
        # Must not raise.
        s.validate_risk_caps_at_startup()

    def test_nonzero_caps_pass(self):
        """All three caps > 0 lets boot proceed."""
        s = Settings(
            max_condition_exposure_usd=100.0,
            max_category_exposure_usd=500.0,
            max_daily_notional_usd=1000.0,
        )
        s.validate_risk_caps_at_startup()

    def test_partial_zero_caps_raise(self):
        """If any one of the three is <= 0, fail (defense in depth)."""
        s = Settings(
            max_condition_exposure_usd=100.0,
            max_category_exposure_usd=500.0,
            max_daily_notional_usd=0.0,  # still disabled
        )
        with self.assertRaises(ValueError):
            s.validate_risk_caps_at_startup()

    def test_active_risk_caps_snapshot(self):
        """active_risk_caps() returns the three caps actually enforced."""
        s = Settings(
            max_condition_exposure_usd=10.0,
            max_category_exposure_usd=20.0,
            max_daily_notional_usd=30.0,
        )
        caps = s.active_risk_caps()
        self.assertEqual(
            caps,
            {
                "max_condition_exposure_usd": 10.0,
                "max_category_exposure_usd": 20.0,
                "max_daily_notional_usd": 30.0,
            },
        )

    def test_trading_loop_init_path_raises_on_defaults(self):
        """TradingBot.__init__ (the trading-loop init path) must hard-fail
        when Settings.load() returns defaults with zero caps."""
        # Patch Settings.load on the orchestrator's reference to return defaults
        # so we exercise the real init path without touching the DB.
        from bot import orchestrator as orch

        with patch.object(orch.Settings, "load", return_value=Settings()):
            with self.assertRaises(ValueError) as ctx:
                orch.TradingBot()
            self.assertEqual(str(ctx.exception), self.EXPECTED_MSG)

    def test_trading_loop_init_path_passes_with_opt_in(self):
        """Setting risk_caps_disabled=True allows TradingBot() to boot."""
        from bot import orchestrator as orch

        with patch.object(
            orch.Settings, "load", return_value=Settings(risk_caps_disabled=True)
        ):
            bot = orch.TradingBot()
            # active_caps snapshot should be present even when opted out.
            self.assertIn("max_condition_exposure_usd", bot.state.active_caps)

    def test_trading_loop_init_path_passes_with_nonzero_caps(self):
        """Non-zero caps allow boot and surface on state.active_caps."""
        from bot import orchestrator as orch

        cfg = Settings(
            max_condition_exposure_usd=25.0,
            max_category_exposure_usd=50.0,
            max_daily_notional_usd=200.0,
        )
        with patch.object(orch.Settings, "load", return_value=cfg):
            bot = orch.TradingBot()
            self.assertEqual(bot.state.active_caps["max_condition_exposure_usd"], 25.0)
            self.assertEqual(bot.state.active_caps["max_category_exposure_usd"], 50.0)
            self.assertEqual(bot.state.active_caps["max_daily_notional_usd"], 200.0)


class TestBotStatePreaddedFields(unittest.TestCase):
    """E1 pre-adds fields downstream captains will populate."""

    def test_default_fields_present_and_typed(self):
        from bot.models import BotState

        st = BotState()
        self.assertEqual(st.active_caps, {})
        self.assertEqual(st.scoring_fallback_v1, 0)
        self.assertEqual(st.scoring_mode, "v2")
        self.assertEqual(st.balance_refreshed_at, 0.0)
        self.assertEqual(st.reserved_usdc, 0.0)


if __name__ == "__main__":
    unittest.main()
