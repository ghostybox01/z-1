"""Gamma API: list tradeable binary markets with CLOB tokens."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from bot.categories import classify_market
from bot.http_retry import get_json_retry

log = logging.getLogger("polymarket.gamma")

GAMMA = "https://gamma-api.polymarket.com/markets"


async def scan_tradeable_markets(
    http: httpx.AsyncClient,
    rate_limit_cb,
    max_pages: int = 2,
    min_liquidity: float = 500.0,
    min_volume: float = 1000.0,
    volume_supplement_pages: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """
    Returns (normalized_markets, condition_id -> raw_gamma_market cache).

    Fetches max_pages × 100 markets sorted by liquidityClob (most liquid first),
    then supplements with volume_supplement_pages × 100 sorted by volume (most
    recently-active first). This captures both high-liquidity sports/politics
    markets needed for ZScore history AND lower-liquidity crypto/weather markets
    that are actually tradeable from restricted regions.
    """
    raw_markets: list[dict] = []
    seen_cids: set[str] = set()
    cache: dict[str, dict] = {}

    async def _fetch_pages(order: str, n_pages: int, liq_floor: float) -> None:
        for page in range(n_pages):
            await rate_limit_cb()
            offset = page * 100
            try:
                batch = await get_json_retry(
                    http,
                    GAMMA,
                    params={
                        "limit": 100,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                        "order": order,
                        "ascending": "false",
                    },
                )
            except Exception as e:
                log.warning("Gamma %s page %s: %s", order, page, e)
                break
            if not batch or not isinstance(batch, list):
                break
            added = 0
            for m in batch:
                cid = m.get("condition_id") or m.get("conditionId") or ""
                liq = float(m.get("liquidityClob", m.get("liquidity_clob", 0)) or 0)
                if cid and cid not in seen_cids and liq >= liq_floor:
                    seen_cids.add(cid)
                    raw_markets.append(m)
                    added += 1
            if added == 0:
                break  # no new markets on this page, stop

    # Primary: sorted by CLOB liquidity (high-liquidity markets for ZScore history)
    await _fetch_pages("liquidityClob", max_pages, min_liquidity)

    # Supplement: sorted by volume (recently active — picks up crypto/weather)
    # Use a lower liquidity floor ($50) so low-liquidity active markets are included
    supp_liq_floor = max(50.0, min_liquidity * 0.1)
    if volume_supplement_pages > 0:
        await _fetch_pages("volume", volume_supplement_pages, supp_liq_floor)

    markets = raw_markets

    for m in markets:
        cid = m.get("condition_id") or m.get("conditionId") or ""
        if cid:
            cache[cid] = m

    tradeable: list[dict[str, Any]] = []
    for m in markets:
        tokens = m.get("clobTokenIds", m.get("clob_token_ids", ""))
        if not tokens:
            continue
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens) if tokens.startswith("[") else [tokens]
            except json.JSONDecodeError:
                continue
        if len(tokens) < 2:
            continue

        if not m.get("enableOrderBook", m.get("enable_order_book", True)):
            continue

        prices_raw = m.get("outcomePrices", m.get("outcome_prices", ""))
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw) if prices_raw else [0.5, 0.5]
            except json.JSONDecodeError:
                prices = [0.5, 0.5]
        else:
            prices = prices_raw or [0.5, 0.5]
        try:
            prices = [float(p) for p in prices]
        except (ValueError, TypeError):
            continue

        liq = float(m.get("liquidityClob", m.get("liquidity_clob", 0)) or 0)
        vol = float(m.get("volume", 0) or 0)
        if liq < min_liquidity or vol < min_volume:
            continue

        cid = m.get("condition_id", m.get("conditionId", ""))
        outcomes = m.get("outcomes", '["Yes","No"]')
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = ["Yes", "No"]

        nm = {
            "condition_id": cid,
            "question": m.get("question", "Unknown"),
            "tokens": tokens,
            "prices": prices,
            "outcomes": outcomes,
            "liquidity": liq,
            "volume": vol,
            "slug": m.get("slug", ""),
            "category": classify_market(m),
            "raw": m,
        }
        tradeable.append(nm)

    log.info("Gamma: %d raw -> %d tradeable", len(markets), len(tradeable))
    return tradeable, cache
