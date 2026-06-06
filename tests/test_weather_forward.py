"""Tests for the weather forward paper-test core math (offline, no network)."""

from __future__ import annotations

import unittest

from bot.backtest.weather_forward import f_to_c, forecast_cdf, parse_threshold_market


class TestForecastCDF(unittest.TestCase):
    def test_threshold_far_above_forecast_is_unlikely(self):
        # forecast 18C, threshold 22C above -> very unlikely
        p = forecast_cdf(18.0, 22.0, above=True)
        self.assertLess(p, 0.05)

    def test_threshold_far_below_forecast_above_is_near_certain(self):
        # forecast 25C, threshold 21C, "or higher" -> near certain
        p = forecast_cdf(25.0, 21.0, above=True)
        self.assertGreater(p, 0.95)

    def test_above_and_below_are_complementary(self):
        a = forecast_cdf(20.0, 22.0, above=True)
        b = forecast_cdf(20.0, 22.0, above=False)
        self.assertAlmostEqual(a + b, 1.0, places=6)

    def test_bias_shifts_distribution_up(self):
        # With positive bias, P(>= forecast) should exceed 0.5 (actual runs hot)
        p = forecast_cdf(20.0, 20.0, above=True)
        self.assertGreater(p, 0.5)


class TestParseThresholdMarket(unittest.TestCase):
    def test_parse_fahrenheit_or_higher(self):
        r = parse_threshold_market("Will the highest temperature in Houston be 92°F or higher on June 5?")
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "houston")
        self.assertTrue(r["above"])
        self.assertAlmostEqual(r["threshold_c"], f_to_c(92), places=2)

    def test_parse_celsius_or_below(self):
        r = parse_threshold_market("Will the highest temperature in London be 18°C or below on June 5?")
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "london")
        self.assertFalse(r["above"])
        self.assertEqual(r["threshold_c"], 18.0)

    def test_exact_degree_market_is_not_a_threshold(self):
        self.assertIsNone(parse_threshold_market("Will the highest temperature in Tokyo be 19°C on June 5?"))

    def test_lowest_temperature_market_is_skipped(self):
        # MIN markets need their own calibration; not parsed here.
        self.assertIsNone(parse_threshold_market("Will the lowest temperature in NYC be 55°F or below on June 5?"))


if __name__ == "__main__":
    unittest.main()
