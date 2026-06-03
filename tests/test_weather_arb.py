"""Tests for WeatherArbAgent — probability math, date gating, regex parsing, edge threshold."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.agents.weather_arb import (
    CITY_COORDS,
    WeatherArbAgent,
    forecast_probability,
    parse_resolution_date,
    phi,
    _TEMP_RE,
)


# ---------------------------------------------------------------------------
# phi / normal CDF sanity
# ---------------------------------------------------------------------------

def test_phi_midpoint():
    """phi(0) == 0.5"""
    assert abs(phi(0.0) - 0.5) < 1e-9


def test_phi_positive():
    assert phi(1.96) > 0.97


def test_phi_negative():
    assert phi(-1.96) < 0.03


# ---------------------------------------------------------------------------
# Probability calculations
# ---------------------------------------------------------------------------

def test_forecast_prob_exact_dead_on():
    """If forecast == target, exact-bin probability should be ~30% for sigma=1.6."""
    p = forecast_probability(25.0, 25.0, 1.6, None)
    # P(24.5 < X < 25.5) for N(25, 1.6) ≈ 0.24
    assert 0.20 < p < 0.35


def test_forecast_prob_exact_far_away():
    """If forecast is far from target, exact-bin probability should be near 0."""
    p = forecast_probability(30.0, 20.0, 1.6, None)
    assert p < 0.001


def test_forecast_prob_or_higher_above_forecast():
    """
    or-higher with target below forecast → probability near 1.
    forecast=30, target=20, so P(high >= 19.5) ≈ 1.
    """
    p = forecast_probability(30.0, 20.0, 1.6, "or higher")
    assert p > 0.999


def test_forecast_prob_or_higher_above_target():
    """
    or-higher with target above forecast → probability near 0.
    forecast=20, target=30, so P(high >= 29.5) is tiny.
    """
    p = forecast_probability(20.0, 30.0, 1.6, "or higher")
    assert p < 0.001


def test_forecast_prob_or_lower_below_forecast():
    """
    or-lower with target above forecast → probability near 1.
    forecast=20, target=30, P(high <= 30.5) ≈ 1.
    """
    p = forecast_probability(20.0, 30.0, 1.6, "or lower")
    assert p > 0.999


def test_forecast_prob_or_lower_above_forecast():
    """
    or-lower with target below forecast → near 0.
    forecast=30, target=20, P(high <= 20.5) is tiny.
    """
    p = forecast_probability(30.0, 20.0, 1.6, "or lower")
    assert p < 0.001


def test_forecast_prob_or_above_alias():
    """'or above' should behave the same as 'or higher'."""
    p_higher = forecast_probability(28.0, 25.0, 1.6, "or higher")
    p_above = forecast_probability(28.0, 25.0, 1.6, "or above")
    assert abs(p_higher - p_above) < 1e-12


def test_forecast_prob_or_below_alias():
    """'or below' should behave the same as 'or lower'."""
    p_lower = forecast_probability(22.0, 25.0, 1.6, "or lower")
    p_below = forecast_probability(22.0, 25.0, 1.6, "or below")
    assert abs(p_lower - p_below) < 1e-12


def test_forecast_prob_sigma_sensitivity():
    """
    For an off-center case (forecast far from target), wider sigma → higher
    exact-bin prob because more probability mass reaches the target bin.
    """
    # forecast=28, target=25 — with narrow sigma the target is in the far tail
    p_narrow = forecast_probability(28.0, 25.0, 0.5, None)
    p_wide = forecast_probability(28.0, 25.0, 3.0, None)
    assert p_wide > p_narrow


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def test_parse_iso_date():
    assert parse_resolution_date("2026-06-03") == "2026-06-03"


def test_parse_month_day_year():
    assert parse_resolution_date("June 3, 2026") == "2026-06-03"


def test_parse_month_day_no_year():
    result = parse_resolution_date("June 3")
    assert result is not None
    # Should return a future date in YYYY-MM-DD format
    assert result.endswith("-06-03")


def test_parse_ordinal():
    result = parse_resolution_date("June 3rd, 2026")
    assert result == "2026-06-03"


def test_parse_invalid_returns_none():
    assert parse_resolution_date("not-a-date") is None


def test_parse_short_month():
    assert parse_resolution_date("Jun 15, 2026") == "2026-06-15"


# ---------------------------------------------------------------------------
# Regex parsing
# ---------------------------------------------------------------------------

def test_regex_basic_celsius():
    q = "Will the highest temperature in New York be 30°C on June 3?"
    m = _TEMP_RE.search(q)
    assert m is not None
    city, degrees, unit, modifier, date_str = m.groups()
    assert city.strip().lower() == "new york"
    assert float(degrees) == 30.0
    assert unit.upper() == "C"
    assert modifier is None
    assert "june 3" in date_str.lower()


def test_regex_or_higher():
    q = "Will the highest temperature in Chicago be 95°F or higher on July 4, 2026?"
    m = _TEMP_RE.search(q)
    assert m is not None
    city, degrees, unit, modifier, date_str = m.groups()
    assert "chicago" in city.lower()
    assert float(degrees) == 95.0
    assert unit.upper() == "F"
    assert modifier is not None
    assert "higher" in modifier.lower()


def test_regex_or_lower():
    q = "Will the highest temperature in London be 15°C or lower on January 10?"
    m = _TEMP_RE.search(q)
    assert m is not None
    _, _, _, modifier, _ = m.groups()
    assert modifier is not None
    assert "lower" in modifier.lower()


def test_regex_no_match_on_non_temp():
    q = "Will Bitcoin reach $100,000 by end of 2026?"
    m = _TEMP_RE.search(q)
    assert m is None


def test_regex_no_unit_defaults():
    """Markets sometimes omit the unit symbol."""
    q = "Will the highest temperature in Dallas be 25 on June 5?"
    m = _TEMP_RE.search(q)
    assert m is not None
    _, _, unit, _, _ = m.groups()
    # unit may be None or empty — the agent defaults it to C
    assert unit is None or unit == ""


# ---------------------------------------------------------------------------
# Settings / disabled
# ---------------------------------------------------------------------------

def _make_settings(**kwargs: Any) -> Any:
    s = MagicMock()
    s.agent_weather = kwargs.get("agent_weather", False)
    s.weather_min_edge = kwargs.get("weather_min_edge", 0.12)
    s.weather_sigma = kwargs.get("weather_sigma", 1.6)
    s.weather_min_volume_24h = kwargs.get("weather_min_volume_24h", 500.0)
    s.weather_max_markets_per_cycle = kwargs.get("weather_max_markets_per_cycle", 5)
    s.min_bet_usd = kwargs.get("min_bet_usd", 1.0)
    s.max_bet_usd = kwargs.get("max_bet_usd", 25.0)
    s.default_bet_usd = kwargs.get("default_bet_usd", 5.0)
    return s


def test_propose_disabled_returns_empty():
    agent = WeatherArbAgent(_make_settings(agent_weather=False))
    result = asyncio.run(agent.propose(AsyncMock()))
    assert result == []
    assert agent.last_note == "disabled"


# ---------------------------------------------------------------------------
# Same-day past-noon gate
# ---------------------------------------------------------------------------

def _make_market(question: str, volume: float = 5000.0) -> dict:
    """Build a minimal Gamma market dict for testing."""
    return {
        "question": question,
        "volume24hr": volume,
        "outcomePrices": json.dumps(["0.45", "0.55"]),
        "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
        "conditionId": "cond123",
        "active": True,
        "closed": False,
    }


def test_same_day_past_noon_skipped():
    """
    A market resolving today in a city whose local time is past 2pm
    (i.e. the daily high has likely already set) should be skipped.
    Use Seoul (lon ~127°): at UTC 14:00, local ≈ 22:30 → well past 14:00 local.
    """
    async def _run():
        today = datetime.now(timezone.utc)
        today_str = today.strftime("%B %-d, %Y")  # e.g. "June 3, 2026"
        question = f"Will the highest temperature in Seoul be 28°C on {today_str}?"

        market = _make_market(question, volume=10000.0)

        agent = WeatherArbAgent(_make_settings(agent_weather=True))

        # Page 1: returns the market; page 2: empty (ends pagination)
        gamma_resp1 = MagicMock()
        gamma_resp1.raise_for_status = MagicMock()
        gamma_resp1.json = MagicMock(return_value=[market])

        gamma_empty = MagicMock()
        gamma_empty.raise_for_status = MagicMock()
        gamma_empty.json = MagicMock(return_value=[])

        http = AsyncMock()
        # The date-skip happens BEFORE any Open-Meteo fetch, so only 2 HTTP calls
        http.get = AsyncMock(side_effect=[gamma_resp1, gamma_empty])

        fixed_now = today.replace(hour=14, minute=0, second=0, microsecond=0)
        with patch("bot.agents.weather_arb.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.now.side_effect = None
            mock_dt.strptime = datetime.strptime
            result = await agent.propose(http)

        assert result == [], f"Expected empty result, got {result}"
        assert "date_skip" in agent.last_note

    asyncio.run(_run())


def test_edge_below_threshold_skipped():
    """
    When forecast probability is close to market price (edge < min_edge), no intent emitted.
    """
    from datetime import timedelta

    async def _run():
        tomorrow = datetime.now(timezone.utc)
        tomorrow_date = (tomorrow + timedelta(days=1)).date()
        tomorrow_str = tomorrow_date.strftime("%B %-d, %Y")
        question = f"Will the highest temperature in Chicago be 25°C on {tomorrow_str}?"

        market = _make_market(question, volume=5000.0)

        # Set min_edge very high so nothing passes
        agent = WeatherArbAgent(_make_settings(agent_weather=True, weather_min_edge=0.99))

        # Gamma returns market on first page, empty on subsequent pages (ends pagination)
        gamma_resp1 = MagicMock()
        gamma_resp1.raise_for_status = MagicMock()
        gamma_resp1.json = MagicMock(return_value=[market])

        gamma_empty = MagicMock()
        gamma_empty.raise_for_status = MagicMock()
        gamma_empty.json = MagicMock(return_value=[])

        meteo_resp = MagicMock()
        meteo_resp.raise_for_status = MagicMock()
        meteo_resp.json = MagicMock(return_value={
            "daily": {
                "time": [tomorrow_date.isoformat()],
                "temperature_2m_max": [25.5],
            }
        })

        http = AsyncMock()
        # gamma page 1, gamma page 2 (empty=stop), meteo
        http.get = AsyncMock(side_effect=[gamma_resp1, gamma_empty, meteo_resp])

        result = await agent.propose(http)
        assert result == []
        assert "low_edge" in agent.last_note

    asyncio.run(_run())


def test_intent_emitted_on_clear_edge():
    """
    When forecast gives strong edge (e.g. forecast very high vs market saying unlikely),
    a TradeIntent should be emitted.
    """
    from datetime import timedelta

    async def _run():
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        tomorrow_str = tomorrow.strftime("%B %-d, %Y")
        question = f"Will the highest temperature in Miami be 35°C or higher on {tomorrow_str}?"

        # YES price 0.40 (market has moderate doubt), but our forecast is 40°C → P(>34.5) ≈ 1.0
        # edge ≈ 1.0 - 0.40 = +0.60 >> min_edge=0.12; YES price 0.40 is within 0.30-0.90 band
        market = {
            "question": question,
            "volume24hr": 50000.0,
            "outcomePrices": json.dumps(["0.40", "0.60"]),
            "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
            "conditionId": "cond456",
            "active": True,
            "closed": False,
        }

        agent = WeatherArbAgent(_make_settings(agent_weather=True, weather_min_edge=0.12))

        # Gamma page 1 returns the market, page 2 returns empty (ends pagination)
        gamma_resp1 = MagicMock()
        gamma_resp1.raise_for_status = MagicMock()
        gamma_resp1.json = MagicMock(return_value=[market])

        gamma_empty = MagicMock()
        gamma_empty.raise_for_status = MagicMock()
        gamma_empty.json = MagicMock(return_value=[])

        meteo_resp = MagicMock()
        meteo_resp.raise_for_status = MagicMock()
        meteo_resp.json = MagicMock(return_value={
            "daily": {
                "time": [tomorrow.isoformat()],
                "temperature_2m_max": [40.0],
            }
        })

        http = AsyncMock()
        # gamma page 1, gamma page 2 (empty=stop), meteo
        http.get = AsyncMock(side_effect=[gamma_resp1, gamma_empty, meteo_resp])

        result = await agent.propose(http)
        assert len(result) == 1
        intent = result[0]
        assert intent.agent == "weather_arb"
        assert intent.outcome == "YES"
        assert intent.side == "BUY"
        assert intent.strategy == "weather_arb"
        assert "forecast" in intent.reason
        assert "edge" in intent.reason

    asyncio.run(_run())


def test_low_volume_market_skipped():
    """Markets below min volume threshold should be skipped."""
    from datetime import timedelta

    async def _run():
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        tomorrow_str = tomorrow.strftime("%B %-d, %Y")
        question = f"Will the highest temperature in London be 20°C on {tomorrow_str}?"

        market = _make_market(question, volume=100.0)  # below 500 threshold

        agent = WeatherArbAgent(_make_settings(agent_weather=True, weather_min_volume_24h=500.0))

        # Page 1: one low-volume market; page 2+: empty (ends pagination)
        gamma_resp1 = MagicMock()
        gamma_resp1.raise_for_status = MagicMock()
        gamma_resp1.json = MagicMock(return_value=[market])

        gamma_empty = MagicMock()
        gamma_empty.raise_for_status = MagicMock()
        gamma_empty.json = MagicMock(return_value=[])

        http = AsyncMock()
        # Low-vol skip happens before Open-Meteo; pagination stops on empty page
        http.get = AsyncMock(side_effect=[gamma_resp1, gamma_empty])

        result = await agent.propose(http)
        assert result == []
        assert "low_vol" in agent.last_note

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# City coords lookup
# ---------------------------------------------------------------------------

def test_major_cities_in_dict():
    """Spot-check that key cities are present."""
    for city in ("new york", "london", "tokyo", "dubai", "sydney"):
        assert city in CITY_COORDS, f"{city} missing from CITY_COORDS"


def test_coords_are_valid_range():
    """All coords should be within valid lat/lon bounds."""
    for city, (lat, lon) in CITY_COORDS.items():
        assert -90 <= lat <= 90, f"{city} lat out of range"
        assert -180 <= lon <= 180, f"{city} lon out of range"
