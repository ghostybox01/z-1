"""Tests for the weather-specialist wallet discovery engine.

The engine enumerates active temperature markets, tallies how many distinct
weather markets each wallet has traded, then vets the specialists with
``analyze_wallet_quality`` (patched here so we control the verdict). Only wallets
with enough trades, a verifiable win-rate at/above the bar, AND fully visible
losses (``loss_visibility == "verified"``) survive.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from bot import weather_discovery
from bot.weather_discovery import discover_weather_specialists


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResp:
    status_code = 200

    def __init__(self, payload: Any):
        self._payload = payload

    def raise_for_status(self) -> None:  # pragma: no cover - never errors in fake
        return None

    def json(self) -> Any:
        return self._payload


class _FakeHttp:
    """Routes gamma market pages and per-market trade fetches.

    ``gamma_pages`` is a list of page payloads returned in order for successive
    gamma offsets (an empty list ends pagination). ``trades_by_market`` maps a
    market conditionId to the trade list returned for that market.
    """

    def __init__(self, gamma_pages: list[list[dict]], trades_by_market: dict[str, list[dict]]):
        self._gamma_pages = list(gamma_pages)
        self._gamma_idx = 0
        self._trades_by_market = trades_by_market

    async def get(self, url: str, params: dict | None = None, **kwargs: Any) -> _FakeResp:
        params = params or {}
        if url == weather_discovery.GAMMA_MARKETS_URL:
            if self._gamma_idx < len(self._gamma_pages):
                page = self._gamma_pages[self._gamma_idx]
            else:
                page = []
            self._gamma_idx += 1
            return _FakeResp(page)
        if url == weather_discovery.TRADES_URL:
            cid = params.get("market", "")
            return _FakeResp(self._trades_by_market.get(cid, []))
        return _FakeResp([])


def _temp_market(cid: str, city: str = "New York", degrees: int = 30, date: str = "December 31") -> dict:
    """A gamma market dict whose question parses as a temperature market."""
    return {
        "conditionId": cid,
        "question": f"Will the highest temperature in {city} be {degrees}°C on {date}?",
    }


def _non_temp_market(cid: str) -> dict:
    return {"conditionId": cid, "question": "Will Bitcoin reach $100,000 by end of 2026?"}


def _trade(wallet: str) -> dict:
    return {"proxyWallet": wallet, "side": "BUY", "outcome": "YES", "size": 100, "price": 0.5}


def _quality(
    *,
    win_rate: float | None,
    total: int,
    loss_visibility: str,
    wins: int = 0,
    losses: int = 0,
) -> dict[str, Any]:
    return {
        "win_rate": win_rate,
        "total": total,
        "wins": wins,
        "losses": losses,
        "loss_visibility": loss_visibility,
        "account_age_days": 120.0,
        "profit_factor": 2.5,
    }


def _patch_no_sleep():
    """Avoid real 0.25s sleeps between fetches during tests."""
    async def _instant(*_a, **_k):
        return None
    return patch.object(weather_discovery.asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_zero_temp_markets_returns_empty():
    """No temperature markets among active gamma markets → []. (Also covers the
    common night-time case where there are simply no markets at all.)"""
    http = _FakeHttp(
        gamma_pages=[[_non_temp_market("c1"), _non_temp_market("c2")]],
        trades_by_market={},
    )
    with _patch_no_sleep():
        result = asyncio.run(discover_weather_specialists(http))
    assert result == []


def test_specialist_with_good_winrate_qualifies():
    """A wallet trading 5 weather markets with win_rate=0.90, total=150,
    loss_visibility='verified' qualifies."""
    wallet = "0x" + "a" * 40
    cids = [f"cond{i}" for i in range(5)]
    gamma = [[_temp_market(cid) for cid in cids]]
    trades = {cid: [_trade(wallet)] for cid in cids}
    http = _FakeHttp(gamma, trades)

    async def fake_quality(_http, _wallet, **_kw):
        return _quality(win_rate=0.90, total=150, loss_visibility="verified", wins=135, losses=15)

    with _patch_no_sleep(), patch.object(weather_discovery, "analyze_wallet_quality", fake_quality):
        result = asyncio.run(discover_weather_specialists(http))

    assert len(result) == 1
    row = result[0]
    assert row["wallet"] == wallet
    assert row["weather_markets_traded"] == 5
    assert row["win_rate"] == 0.90
    assert row["total"] == 150
    assert row["loss_visibility"] == "verified"


def test_low_winrate_filtered_out():
    """A specialist with win_rate=0.70 is below the default 0.80 bar → filtered."""
    wallet = "0x" + "b" * 40
    cids = [f"cond{i}" for i in range(5)]
    gamma = [[_temp_market(cid) for cid in cids]]
    trades = {cid: [_trade(wallet)] for cid in cids}
    http = _FakeHttp(gamma, trades)

    async def fake_quality(_http, _wallet, **_kw):
        return _quality(win_rate=0.70, total=150, loss_visibility="verified", wins=105, losses=45)

    with _patch_no_sleep(), patch.object(weather_discovery, "analyze_wallet_quality", fake_quality):
        result = asyncio.run(discover_weather_specialists(http))

    assert result == []


def test_loss_blind_wallet_filtered_out():
    """loss_visibility='none' → filtered even with a (non-None) win_rate.

    A loss-blind 100%-win illusion must never qualify.
    """
    wallet = "0x" + "c" * 40
    cids = [f"cond{i}" for i in range(5)]
    gamma = [[_temp_market(cid) for cid in cids]]
    trades = {cid: [_trade(wallet)] for cid in cids}
    http = _FakeHttp(gamma, trades)

    async def fake_quality(_http, _wallet, **_kw):
        # Even if some upstream produced a rate, loss-blindness disqualifies it.
        return _quality(win_rate=1.0, total=150, loss_visibility="none", wins=150, losses=0)

    with _patch_no_sleep(), patch.object(weather_discovery, "analyze_wallet_quality", fake_quality):
        result = asyncio.run(discover_weather_specialists(http))

    assert result == []


def test_too_few_trades_filtered_out():
    """total=50 is below the default min_total_trades=100 → filtered."""
    wallet = "0x" + "d" * 40
    cids = [f"cond{i}" for i in range(5)]
    gamma = [[_temp_market(cid) for cid in cids]]
    trades = {cid: [_trade(wallet)] for cid in cids}
    http = _FakeHttp(gamma, trades)

    async def fake_quality(_http, _wallet, **_kw):
        return _quality(win_rate=0.95, total=50, loss_visibility="verified", wins=48, losses=2)

    with _patch_no_sleep(), patch.object(weather_discovery, "analyze_wallet_quality", fake_quality):
        result = asyncio.run(discover_weather_specialists(http))

    assert result == []


def test_non_specialist_below_market_threshold_not_vetted():
    """A wallet trading only 3 distinct weather markets (< min_weather_markets=4)
    is not even a candidate, so analyze_wallet_quality is never called for it."""
    wallet = "0x" + "e" * 40
    cids = [f"cond{i}" for i in range(3)]
    gamma = [[_temp_market(cid) for cid in cids]]
    trades = {cid: [_trade(wallet)] for cid in cids}
    http = _FakeHttp(gamma, trades)

    called = {"n": 0}

    async def fake_quality(_http, _wallet, **_kw):
        called["n"] += 1
        return _quality(win_rate=0.99, total=999, loss_visibility="verified")

    with _patch_no_sleep(), patch.object(weather_discovery, "analyze_wallet_quality", fake_quality):
        result = asyncio.run(discover_weather_specialists(http))

    assert result == []
    assert called["n"] == 0


def test_results_sorted_by_winrate_and_capped():
    """Multiple qualifying specialists come back sorted by win_rate desc and
    capped at max_results."""
    cids = [f"cond{i}" for i in range(5)]
    gamma = [[_temp_market(cid) for cid in cids]]
    w_hi = "0x" + "1" * 40
    w_mid = "0x" + "2" * 40
    w_lo = "0x" + "3" * 40
    # Every wallet trades all 5 markets (so all are specialists).
    trades = {cid: [_trade(w_hi), _trade(w_mid), _trade(w_lo)] for cid in cids}
    http = _FakeHttp(gamma, trades)

    by_wallet = {
        w_hi: _quality(win_rate=0.95, total=200, loss_visibility="verified"),
        w_mid: _quality(win_rate=0.88, total=200, loss_visibility="verified"),
        w_lo: _quality(win_rate=0.82, total=200, loss_visibility="verified"),
    }

    async def fake_quality(_http, wallet, **_kw):
        return by_wallet[wallet]

    with _patch_no_sleep(), patch.object(weather_discovery, "analyze_wallet_quality", fake_quality):
        result = asyncio.run(discover_weather_specialists(http, max_results=2))

    assert [r["wallet"] for r in result] == [w_hi, w_mid]
    assert [r["win_rate"] for r in result] == [0.95, 0.88]
