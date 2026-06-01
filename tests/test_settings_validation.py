"""Strict admin settings validation."""

from __future__ import annotations

import unittest

from bot.settings import Settings
from bot.settings_validation import (
    live_risk_caps_ok,
    validate_and_normalize_settings_patch,
)


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


class TestLiveRiskCapsOk(unittest.TestCase):
    def test_dry_run_always_ok(self):
        ok, reason = live_risk_caps_ok(Settings(dry_run=True))
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_live_all_caps_zero_blocked(self):
        ok, reason = live_risk_caps_ok(Settings(dry_run=False))
        self.assertFalse(ok)
        self.assertIn("live trading blocked", reason)

    def test_live_one_cap_nonzero_ok(self):
        ok, _ = live_risk_caps_ok(Settings(dry_run=False, max_daily_notional_usd=100.0))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
