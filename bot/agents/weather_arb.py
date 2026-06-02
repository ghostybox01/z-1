"""
Weather forecast-arbitrage agent.
Compares Open-Meteo GFS forecasts to Polymarket temperature markets and emits
TradeIntents when the forecast disagrees with the market price by more than
the configured edge threshold.

COMPLETELY ISOLATED from copy-trade logic — separate file, separate propose() cycle.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx

from bot.categories import MarketCategory
from bot.models import TradeIntent

log = logging.getLogger("polymarket.agent.weather_arb")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Regex to match temperature market questions.
# Captures: city, degrees, unit (C/F, optional), modifier (or higher/lower/above/below, optional), date string
_TEMP_RE = re.compile(
    r"highest temperature in (.+?) be (\d+(?:\.\d+)?)\s*°?\s*([CF])?\s*"
    r"(or higher|or above|or lower|or below)?\s*on\s+(.+?)[\?\.]",
    re.IGNORECASE,
)

# Major city → (lat, lon) mapping
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.7128, -74.0060),
    "chicago": (41.8781, -87.6298),
    "seattle": (47.6062, -122.3321),
    "atlanta": (33.7490, -84.3880),
    "dallas": (32.7767, -96.7970),
    "miami": (25.7617, -80.1918),
    "london": (51.5074, -0.1278),
    "seoul": (37.5665, 126.9780),
    "shenzhen": (22.5431, 114.0579),
    "hong kong": (22.3193, 114.1694),
    "los angeles": (34.0522, -118.2437),
    "denver": (39.7392, -104.9903),
    "paris": (48.8566, 2.3522),
    "tokyo": (35.6762, 139.6503),
    "moscow": (55.7558, 37.6173),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "boston": (42.3601, -71.0589),
    "san francisco": (37.7749, -122.4194),
    "philadelphia": (39.9526, -75.1652),
    "washington": (38.9072, -77.0369),
    "berlin": (52.5200, 13.4050),
    "madrid": (40.4168, -3.7038),
    "toronto": (43.6532, -79.3832),
    "beijing": (39.9042, 116.4074),
    "shanghai": (31.2304, 121.4737),
    "singapore": (1.3521, 103.8198),
    "dubai": (25.2048, 55.2708),
    "mumbai": (19.0760, 72.8777),
    "amsterdam": (52.3676, 4.9041),
    "istanbul": (41.0082, 28.9784),
    "ankara": (39.9334, 32.8597),
    "buenos aires": (-34.6037, -58.3816),
    "cape town": (-33.9249, 18.4241),
    "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631),
    "helsinki": (60.1699, 24.9384),
    "kuala lumpur": (3.1390, 101.6869),
    "bangkok": (13.7563, 100.5018),
    "taipei": (25.0330, 121.5654),
}

# Month name → month number
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def phi(z: float) -> float:
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def forecast_probability(
    forecast_temp: float,
    target_temp: float,
    sigma: float,
    modifier: Optional[str],
) -> float:
    """
    Convert a point forecast (max temp) to probability for a binary market.

    modifier=None or "exact"  → P(high falls in [target-0.5, target+0.5])
    modifier="or higher"/"or above" → P(high >= target-0.5)
    modifier="or lower"/"or below"  → P(high <= target+0.5)
    """
    if modifier and ("lower" in modifier.lower() or "below" in modifier.lower()):
        return phi((target_temp + 0.5 - forecast_temp) / sigma)
    elif modifier and ("higher" in modifier.lower() or "above" in modifier.lower()):
        return 1.0 - phi((target_temp - 0.5 - forecast_temp) / sigma)
    else:
        # exact bin
        return phi((target_temp + 0.5 - forecast_temp) / sigma) - phi((target_temp - 0.5 - forecast_temp) / sigma)


def parse_resolution_date(date_str: str) -> Optional[str]:
    """
    Parse a date string from a market question into YYYY-MM-DD.
    Handles: "June 3", "June 3, 2026", "2026-06-03", "June 3rd", etc.
    Returns None if unparseable.
    """
    date_str = date_str.strip().rstrip("?.")

    # Try ISO format first
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
    except ValueError:
        pass

    # Try "Month D, YYYY" or "Month D YYYY"
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            pass

    # Handle ordinal suffixes: "June 3rd" → "June 3"
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_str, flags=re.IGNORECASE)

    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            # If no year was parsed, infer the closest upcoming occurrence
            if fmt in ("%B %d", "%b %d"):
                today = date.today()
                candidate = parsed.replace(year=today.year).date()
                if candidate < today:
                    candidate = parsed.replace(year=today.year + 1).date()
                return candidate.isoformat()
            return parsed.date().isoformat()
        except ValueError:
            pass

    return None


class WeatherArbAgent:
    name = "weather_arb"
    priority = 90

    def __init__(self, settings: Any):
        self.settings = settings
        # Cache: (city_key, unit) → {date_str: max_temp}; reset each cycle
        self._forecast_cache: dict[tuple[str, str], dict[str, float]] = {}
        self.last_note: str = ""

    async def propose(self, http: httpx.AsyncClient) -> list[TradeIntent]:
        if not getattr(self.settings, "agent_weather", False):
            self.last_note = "disabled"
            return []

        # Reset per-cycle forecast cache
        self._forecast_cache = {}

        min_edge = float(getattr(self.settings, "weather_min_edge", 0.12))
        sigma = float(getattr(self.settings, "weather_sigma", 1.6))
        min_vol = float(getattr(self.settings, "weather_min_volume_24h", 500.0))
        max_markets = int(getattr(self.settings, "weather_max_markets_per_cycle", 5))

        markets_scanned = 0
        skipped_no_city = 0
        skipped_date = 0
        skipped_low_vol = 0
        skipped_low_edge = 0
        skipped_parse = 0
        skipped_price_band = 0
        forecast_errors = 0
        intents: list[TradeIntent] = []

        # Fetch temperature markets from Gamma — scan up to 1000 markets in batches.
        # Temperature markets cluster in the 800-1200 volume24hr range, so we must
        # look past the high-volume crypto/sports leaders at the top of the list.
        markets_raw: list = []
        page_limit = 100
        max_offsets = 10  # 10 pages × 100 = 1000 markets
        for page_offset in range(0, max_offsets * page_limit, page_limit):
            try:
                resp = await http.get(
                    GAMMA_MARKETS_URL,
                    params={
                        "closed": "false",
                        "active": "true",
                        "limit": str(page_limit),
                        "offset": str(page_offset),
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                    timeout=20.0,
                )
                resp.raise_for_status()
                batch = resp.json()
            except Exception as exc:
                self.last_note = f"gamma_fetch_error: {exc}"
                log.warning("WeatherArbAgent: Gamma fetch failed at offset=%d: %s", page_offset, exc)
                break
            if not isinstance(batch, list) or len(batch) == 0:
                break
            markets_raw.extend(batch)
            # No early-stop — temperature markets are spread across volume ranks;
            # same-day markets get date-skipped so we need to scan through them.

        if not markets_raw:
            self.last_note = "gamma_unexpected_format"
            return []

        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.date().isoformat()

        for mkt in markets_raw:
            if len(intents) >= max_markets:
                break

            question = str(mkt.get("question") or "")
            m = _TEMP_RE.search(question)
            if not m:
                continue

            markets_scanned += 1

            city_raw, degrees_raw, unit_raw, modifier_raw, date_raw = m.groups()
            city_key = city_raw.strip().lower()
            target_temp = float(degrees_raw)
            unit = (unit_raw or "C").upper()
            modifier = (modifier_raw or "").strip().lower() or None

            # City lookup
            coords = CITY_COORDS.get(city_key)
            if coords is None:
                # Try partial match (e.g. "New York City" → "new york")
                for known_city in CITY_COORDS:
                    if known_city in city_key or city_key in known_city:
                        coords = CITY_COORDS[known_city]
                        break
            if coords is None:
                log.debug("WeatherArbAgent: unknown city %r, skipping", city_raw)
                skipped_no_city += 1
                continue

            lat, lon = coords

            # Parse resolution date
            target_date = parse_resolution_date(date_raw.strip())
            if target_date is None:
                log.debug("WeatherArbAgent: could not parse date %r", date_raw)
                skipped_parse += 1
                continue

            # Skip past dates
            if target_date < today_str:
                skipped_date += 1
                continue

            # CRITICAL: skip same-day markets past noon UTC (the high may already have
            # happened — this is the "settled artifact" trap)
            if target_date == today_str and now_utc.hour >= 12:
                log.debug(
                    "WeatherArbAgent: same-day market past noon UTC, skipping %s", question[:80]
                )
                skipped_date += 1
                continue

            # Volume filter
            try:
                vol24 = float(mkt.get("volume24hr") or mkt.get("volume") or 0)
            except (TypeError, ValueError):
                vol24 = 0.0
            if vol24 < min_vol:
                skipped_low_vol += 1
                continue

            # Fetch forecast (cached per city+unit within this cycle)
            cache_key = (city_key, unit)
            if cache_key not in self._forecast_cache:
                try:
                    temp_unit_param = "fahrenheit" if unit == "F" else "celsius"
                    resp = await http.get(
                        OPEN_METEO_URL,
                        params={
                            "latitude": lat,
                            "longitude": lon,
                            "daily": "temperature_2m_max",
                            "temperature_unit": temp_unit_param,
                            "forecast_days": "7",
                            "timezone": "auto",
                        },
                        timeout=15.0,
                    )
                    resp.raise_for_status()
                    fdata = resp.json()
                    daily = fdata.get("daily", {})
                    dates_list = daily.get("time", [])
                    temps_list = daily.get("temperature_2m_max", [])
                    self._forecast_cache[cache_key] = {
                        d: t for d, t in zip(dates_list, temps_list) if t is not None
                    }
                except Exception as exc:
                    log.warning(
                        "WeatherArbAgent: forecast fetch failed for %s (%s): %s",
                        city_key, unit, exc
                    )
                    forecast_errors += 1
                    continue

            day_forecast = self._forecast_cache[cache_key].get(target_date)
            if day_forecast is None:
                log.debug(
                    "WeatherArbAgent: no forecast for %s on %s", city_key, target_date
                )
                skipped_date += 1
                continue

            # Parse market YES price from outcomePrices
            try:
                outcome_prices_raw = mkt.get("outcomePrices")
                if isinstance(outcome_prices_raw, str):
                    import json
                    prices = json.loads(outcome_prices_raw)
                elif isinstance(outcome_prices_raw, list):
                    prices = outcome_prices_raw
                else:
                    skipped_parse += 1
                    continue
                yes_price = float(prices[0])
                no_price = float(prices[1]) if len(prices) > 1 else (1.0 - yes_price)
            except (IndexError, KeyError, ValueError, TypeError):
                skipped_parse += 1
                continue

            # Compute forecast probability
            forecast_prob = forecast_probability(day_forecast, target_temp, sigma, modifier)
            edge = forecast_prob - yes_price

            if abs(edge) <= min_edge:
                skipped_low_edge += 1
                continue

            # Pick side and check price band (0.30–0.90)
            if edge > 0:
                side = "BUY"
                outcome = "YES"
                bet_price = yes_price
            else:
                side = "BUY"
                outcome = "NO"
                bet_price = no_price

            if not (0.30 <= bet_price <= 0.90):
                skipped_price_band += 1
                continue

            # Extract token_id and condition_id from market
            try:
                clob_token_ids_raw = mkt.get("clobTokenIds")
                if isinstance(clob_token_ids_raw, str):
                    import json
                    clob_tokens = json.loads(clob_token_ids_raw)
                elif isinstance(clob_token_ids_raw, list):
                    clob_tokens = clob_token_ids_raw
                else:
                    skipped_parse += 1
                    continue
                token_id = str(clob_tokens[0] if outcome == "YES" else clob_tokens[1])
            except (IndexError, KeyError, TypeError):
                skipped_parse += 1
                continue

            condition_id = str(mkt.get("conditionId") or mkt.get("condition_id") or "")

            size_usd = max(
                float(getattr(self.settings, "min_bet_usd", 1.0)),
                min(
                    float(getattr(self.settings, "max_bet_usd", 25.0)),
                    float(getattr(self.settings, "default_bet_usd", 5.0)),
                ),
            )

            intents.append(
                TradeIntent(
                    agent=self.name,
                    priority=self.priority,
                    token_id=token_id,
                    condition_id=condition_id,
                    question=question[:500],
                    outcome=outcome,
                    side=side,
                    max_price=round(bet_price * 1.03, 4),  # 3% buffer
                    size_usd=size_usd,
                    category=MarketCategory.WEATHER,
                    strategy="weather_arb",
                    reason=(
                        f"city={city_key} date={target_date} "
                        f"forecast={day_forecast:.1f}{unit} "
                        f"prob={forecast_prob:.2f} mkt={yes_price:.2f} edge={edge:+.2f}"
                    ),
                    reference_price=bet_price,
                )
            )
            log.info(
                "WeatherArbAgent: intent %s %s city=%s date=%s forecast=%.1f prob=%.2f mkt=%.2f edge=%+.2f",
                outcome, side, city_key, target_date, day_forecast, forecast_prob, yes_price, edge,
            )

        parts = [f"scanned={markets_scanned}"]
        if skipped_no_city:
            parts.append(f"no_city={skipped_no_city}")
        if skipped_date:
            parts.append(f"date_skip={skipped_date}")
        if skipped_low_vol:
            parts.append(f"low_vol={skipped_low_vol}")
        if skipped_parse:
            parts.append(f"parse_err={skipped_parse}")
        if forecast_errors:
            parts.append(f"forecast_err={forecast_errors}")
        if skipped_low_edge:
            parts.append(f"low_edge={skipped_low_edge}")
        if skipped_price_band:
            parts.append(f"price_band={skipped_price_band}")
        parts.append(f"new={len(intents)}")
        self.last_note = "; ".join(parts)
        log.info("WeatherArbAgent: %d intents (%s)", len(intents), self.last_note)
        return intents
