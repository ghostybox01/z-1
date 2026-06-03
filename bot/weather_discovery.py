"""Discover weather/temperature-market specialist wallets for copy-trading.

The goal is to find wallets that *specialize* in weather/temperature markets and
have a high verified win-rate, then feed them into the copy watch-list so the
existing copy agent follows them.

Strategy (distinct from the leaderboard-based ``discover_qualified_wallets``):
  1. Enumerate every active temperature market on Gamma (paginated) and keep its
     ``conditionId``. Temperature markets are recognised by the same regex the
     weather-arb agent uses (``bot.agents.weather_arb._TEMP_RE``).
  2. For each temp market, pull its recent trades from the Polymarket data-API
     and tally, per wallet, the number of DISTINCT weather markets it has traded.
     A wallet that shows up across many temperature markets is a specialist.
  3. The wallets with the most distinct weather markets become candidates.
  4. Each candidate is vetted with ``analyze_wallet_quality`` (the same
     multi-source, loss-visibility-aware analyzer used for the leaderboard). A
     wallet only survives if it has enough trades, a verifiable win-rate at or
     above the bar, AND its losses are actually visible to us (``loss_visibility
     == "verified"``) — no loss-blind 100%-win illusions.

Everything is defensive: per-market and per-wallet fetches are wrapped in
try/except, skips are counted, and the function never raises. When there are no
active temperature markets (common at night between daily market batches) it
returns ``[]`` immediately.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from bot.agents.weather_arb import _TEMP_RE
from bot.leaderboard import analyze_wallet_quality

log = logging.getLogger("polymarket.weather_discovery")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
TRADES_URL = "https://data-api.polymarket.com/trades"

# Politeness sleep between successive market/wallet fetches (seconds).
_FETCH_SLEEP_S = 0.25
# How many gamma pages of 100 markets to scan (0,100,...,900 → 1000 markets).
_GAMMA_PAGES = 10
_GAMMA_PAGE_LIMIT = 100


def _is_temp_market(market: dict[str, Any]) -> bool:
    """True iff the market question parses as a temperature market."""
    question = str(market.get("question") or "")
    return bool(_TEMP_RE.search(question))


async def _scan_temp_market_ids(http: httpx.AsyncClient) -> list[str]:
    """Paginate active gamma markets and return distinct temperature conditionIds."""
    condition_ids: list[str] = []
    seen: set[str] = set()
    for offset in range(0, _GAMMA_PAGES * _GAMMA_PAGE_LIMIT, _GAMMA_PAGE_LIMIT):
        try:
            resp = await http.get(
                GAMMA_MARKETS_URL,
                params={
                    "closed": "false",
                    "active": "true",
                    "limit": str(_GAMMA_PAGE_LIMIT),
                    "offset": str(offset),
                },
                timeout=20.0,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception as exc:
            log.warning("weather_discovery: gamma fetch failed at offset=%d: %s", offset, exc)
            break
        if not isinstance(batch, list) or not batch:
            break
        for mkt in batch:
            if not _is_temp_market(mkt):
                continue
            cid = str(mkt.get("conditionId") or mkt.get("condition_id") or "").strip()
            if cid and cid not in seen:
                seen.add(cid)
                condition_ids.append(cid)
    return condition_ids


async def discover_weather_specialists(
    http: httpx.AsyncClient,
    *,
    min_weather_markets: int = 4,
    min_total_trades: int = 100,
    min_win_rate: float = 0.80,
    max_candidates: int = 40,
    max_results: int = 15,
) -> list[dict[str, Any]]:
    """Find wallets that specialize in weather markets with a high win-rate.

    Args:
        http: an ``httpx.AsyncClient`` (or compatible stub with ``.get``).
        min_weather_markets: a wallet must have traded at least this many
            DISTINCT weather markets to be considered a specialist.
        min_total_trades: minimum total trades (sample-size gate) from
            ``analyze_wallet_quality``.
        min_win_rate: minimum verified win-rate to qualify.
        max_candidates: cap on how many wallets we vet (cost control).
        max_results: cap on how many qualifying wallets we return.

    Returns:
        A list of dicts (sorted by ``win_rate`` desc, capped at ``max_results``):
        ``{"wallet", "weather_markets_traded", "win_rate", "total", "wins",
        "losses", "loss_visibility", "account_age_days", "profit_factor"}``.
        Returns ``[]`` when there are no active temperature markets.
    """
    # 1. Scan active temperature markets.
    condition_ids = await _scan_temp_market_ids(http)
    if not condition_ids:
        log.info("weather_discovery: no active temperature markets found — returning []")
        return []
    log.info("weather_discovery: found %d active temperature markets", len(condition_ids))

    # 2. Accumulate, per wallet, the set of distinct weather markets it traded.
    wallet_markets: dict[str, set[str]] = {}
    market_skips = 0
    for cid in condition_ids:
        await asyncio.sleep(_FETCH_SLEEP_S)
        try:
            resp = await http.get(
                TRADES_URL,
                params={"market": cid, "limit": "500"},
                timeout=20.0,
            )
            resp.raise_for_status()
            trades = resp.json()
        except Exception as exc:
            market_skips += 1
            log.debug("weather_discovery: trades fetch failed for %s: %s", cid[:16], exc)
            continue
        if not isinstance(trades, list):
            market_skips += 1
            continue
        for trade in trades:
            wallet = str((trade or {}).get("proxyWallet") or "").strip().lower()
            if not wallet:
                continue
            wallet_markets.setdefault(wallet, set()).add(cid)

    # 3. Candidate wallets: >= min_weather_markets distinct weather markets,
    #    sorted by that count desc, capped at max_candidates.
    candidates = [
        (wallet, len(markets))
        for wallet, markets in wallet_markets.items()
        if len(markets) >= min_weather_markets
    ]
    candidates.sort(key=lambda x: -x[1])
    candidates = candidates[:max_candidates]
    log.info(
        "weather_discovery: %d distinct wallets traded temp markets; "
        "%d are specialists (>=%d markets); %d market-fetch skips",
        len(wallet_markets), len(candidates), min_weather_markets, market_skips,
    )

    # 4. Vet each candidate with the multi-source quality analyzer.
    results: list[dict[str, Any]] = []
    wallet_skips = 0
    for wallet, markets_traded in candidates:
        await asyncio.sleep(_FETCH_SLEEP_S)
        try:
            quality = await analyze_wallet_quality(http, wallet)
        except Exception as exc:
            wallet_skips += 1
            log.debug("weather_discovery: quality check failed for %s: %s", wallet[:12], exc)
            continue

        total = quality.get("total") or 0
        win_rate = quality.get("win_rate")
        loss_visibility = quality.get("loss_visibility")

        if total < min_total_trades:
            log.debug(
                "weather_discovery skip %s: too_few_trades (%s < %s)",
                wallet[:12], total, min_total_trades,
            )
            continue
        if win_rate is None:
            log.debug(
                "weather_discovery skip %s: win_rate unverifiable (loss_visibility=%s)",
                wallet[:12], loss_visibility,
            )
            continue
        if loss_visibility != "verified":
            log.debug(
                "weather_discovery skip %s: loss_visibility=%s (need 'verified')",
                wallet[:12], loss_visibility,
            )
            continue
        if win_rate < min_win_rate:
            log.debug(
                "weather_discovery skip %s: low_win_rate (%.0f%% < %.0f%%)",
                wallet[:12], win_rate * 100, min_win_rate * 100,
            )
            continue

        results.append({
            "wallet": wallet,
            "weather_markets_traded": markets_traded,
            "win_rate": win_rate,
            "total": total,
            "wins": quality.get("wins", 0),
            "losses": quality.get("losses", 0),
            "loss_visibility": loss_visibility,
            "account_age_days": quality.get("account_age_days", 0.0),
            "profit_factor": quality.get("profit_factor"),
        })
        log.info(
            "weather_discovery QUALIFIED %s: WR=%.0f%% total=%d markets=%d loss_vis=%s",
            wallet[:12], win_rate * 100, total, markets_traded, loss_visibility,
        )

    # 5. Sort by win_rate desc, cap at max_results.
    results.sort(key=lambda x: -(x["win_rate"] or 0.0))
    results = results[:max_results]
    log.info(
        "weather_discovery: %d weather specialists qualified (%d wallet-vet skips)",
        len(results), wallet_skips,
    )
    return results
