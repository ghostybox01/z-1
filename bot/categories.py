"""Classify Polymarket Gamma markets into coarse categories for enable/disable toggles."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class MarketCategory(str, Enum):
    CRYPTO_SHORT = "crypto_short"  # short-dated up/down style
    CRYPTO_OTHER = "crypto_other"
    SPORTS = "sports"
    POLITICS = "politics"
    MACRO = "macro"
    WEATHER = "weather"
    SCIENCE_TECH = "science_tech"
    ENTERTAINMENT = "entertainment"
    GEOPOLITICS = "geopolitics"
    OTHER = "other"


_CRYPTO_ASSET = re.compile(
    r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|ripple|doge|dogecoin)\b",
    re.I,
)
_SHORT_TF = re.compile(
    r"\b(5\s*-?\s*min|15\s*-?\s*min|1\s*h(?:our)?|five\s*minute|fifteen\s*minute)\b",
    re.I,
)
_WEATHER = re.compile(
    r"\b(temperature|highest\s+temp|rain|snow|hurricane|nws|forecast|celsius|fahrenheit|°)\b",
    re.I,
)
_SPORTS = re.compile(
    r"\b(nba|nfl|nhl|mlb|ufc|soccer|football|basketball|tennis|golf|spread|total\s+points|vs\.?|"
    r"fifa|world\s+cup|worldcup|champion(?:ship)?|premier\s+league|la\s+liga|bundesliga|"
    r"serie\s+a|ligue\s+1|eredivisie|mls|nwsl|innings|wicket|cricket|"
    r"formula\s+one|f1|grand\s+prix|olympic|medal|nascar|superbowl|super\s+bowl)\b",
    re.I,
)
_POLITICS = re.compile(
    r"\b(president|senate|congress|election|vote|trump|biden|governor|primary|democrat|republican)\b",
    re.I,
)
_MACRO = re.compile(
    r"\b(fed|fomc|interest\s*rate|cpi|inflation|gdp|unemployment|jobs\s*report|treasury|recession)\b",
    re.I,
)
_TECH = re.compile(
    r"\b(ai|openai|google|apple|spacex|tesla|chip|semiconductor|quantum|ipo)\b",
    re.I,
)
_ENT = re.compile(
    r"\b(oscar|grammy|emmy|box\s*office|album|movie|netflix|taylor\s*swift|kanye)\b",
    re.I,
)
_GEO = re.compile(
    r"\b(war|nato|ukraine|russia|china|taiwan|israel|iran|military|invasion|ceasefire)\b",
    re.I,
)


def _text(m: dict[str, Any]) -> str:
    parts = [
        str(m.get("question") or ""),
        str(m.get("slug") or ""),
        str(m.get("description") or ""),
    ]
    tags = m.get("tags") or m.get("categories")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    elif isinstance(tags, str):
        parts.append(tags)
    return " \n".join(parts).lower()


def classify_market(m: dict[str, Any]) -> MarketCategory:
    """Best-effort classification from Gamma market JSON."""
    t = _text(m)

    if _WEATHER.search(t):
        return MarketCategory.WEATHER
    if _SPORTS.search(t):
        return MarketCategory.SPORTS
    if _POLITICS.search(t):
        return MarketCategory.POLITICS
    if _GEO.search(t):
        return MarketCategory.GEOPOLITICS
    if _MACRO.search(t):
        return MarketCategory.MACRO
    if _ENT.search(t):
        return MarketCategory.ENTERTAINMENT
    if _TECH.search(t):
        return MarketCategory.SCIENCE_TECH

    if _CRYPTO_ASSET.search(t):
        if _SHORT_TF.search(t) or "up or down" in t or "up/down" in t:
            return MarketCategory.CRYPTO_SHORT
        return MarketCategory.CRYPTO_OTHER

    if "crypto" in t or "token" in t or "defi" in t:
        return MarketCategory.CRYPTO_OTHER

    return MarketCategory.OTHER


def category_enabled(cat: MarketCategory, flags: dict[str, bool]) -> bool:
    """flags keys: ENABLE_CRYPTO_SHORT, ENABLE_WEATHER, ... (uppercase env-style)."""
    key = f"ENABLE_{cat.value.upper()}"
    if key in flags:
        return bool(flags[key])
    # Aliases
    if cat == MarketCategory.CRYPTO_OTHER and "ENABLE_CRYPTO_OTHER" in flags:
        return bool(flags["ENABLE_CRYPTO_OTHER"])
    return True
