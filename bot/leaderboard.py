"""Polymarket leaderboard: auto-discover top wallets by PnL for copy-trading."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bot.http_retry import get_json_retry

log = logging.getLogger("polymarket.leaderboard")

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
CLOSED_POSITIONS_URL = "https://data-api.polymarket.com/closed-positions"

CATEGORIES = ("OVERALL", "POLITICS", "SPORTS", "CRYPTO", "FINANCE")
TIME_PERIODS = ("DAY", "WEEK", "MONTH", "ALL")


async def fetch_leaderboard(
    http: httpx.AsyncClient,
    *,
    category: str = "OVERALL",
    time_period: str = "MONTH",
    sort_by: str = "PNL",
    limit: int = 25,
) -> list[dict[str, Any]]:
    cat = category.upper()
    if cat not in CATEGORIES:
        cat = "OVERALL"
    tp = time_period.upper()
    if tp not in TIME_PERIODS:
        tp = "MONTH"
    sb = sort_by.upper()
    if sb not in ("PNL", "VOL"):
        sb = "PNL"
    lim = max(1, min(50, limit))

    try:
        data = await get_json_retry(
            http,
            LEADERBOARD_URL,
            params={
                "category": cat,
                "timePeriod": tp,
                "sortBy": sb,
                "limit": str(lim),
            },
        )
        if not isinstance(data, list):
            log.warning("leaderboard returned non-list: %s", type(data))
            return []
        return data
    except Exception as e:
        log.warning("leaderboard fetch failed: %s", e)
        return []


async def discover_top_wallets(
    http: httpx.AsyncClient,
    *,
    categories: list[str] | None = None,
    time_period: str = "MONTH",
    limit_per_category: int = 10,
    min_pnl: float = 0.0,
) -> list[dict[str, Any]]:
    """Fetch top wallets across one or more categories, deduplicated.

    Returns a list of dicts with: wallet, rank, pnl, vol, userName, category.
    """
    cats = categories or ["OVERALL"]
    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for cat in cats:
        entries = await fetch_leaderboard(
            http,
            category=cat,
            time_period=time_period,
            limit=limit_per_category,
        )
        for entry in entries:
            wallet = (entry.get("proxyWallet") or "").strip().lower()
            if not wallet or not wallet.startswith("0x") or len(wallet) != 42:
                continue
            pnl = float(entry.get("pnl") or 0)
            if pnl < min_pnl:
                continue
            if wallet in seen:
                continue
            seen.add(wallet)
            results.append({
                "wallet": wallet,
                "rank": int(entry.get("rank") or 0),
                "pnl": pnl,
                "vol": float(entry.get("vol") or 0),
                "userName": entry.get("userName") or "",
                "category": cat,
            })

    results.sort(key=lambda x: -x["pnl"])
    return results


async def analyze_wallet_quality(
    http: httpx.AsyncClient,
    wallet: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Analyze a wallet's closed positions to determine win rate and streaks.

    Returns: {wallet, total, wins, losses, win_rate, current_streak,
              max_streak, total_pnl, avg_win, avg_loss, positions}
    """
    w = wallet.strip().lower()
    try:
        data = await get_json_retry(
            http,
            CLOSED_POSITIONS_URL,
            params={"user": w, "limit": str(max(1, min(500, limit)))},
        )
        if not isinstance(data, list):
            data = []
    except Exception as e:
        log.warning("closed-positions for %s: %s", w[:12], e)
        data = []

    wins = 0
    losses = 0
    cur_streak = 0
    max_streak = 0
    total_pnl = 0.0
    win_pnls: list[float] = []
    loss_pnls: list[float] = []

    for p in data:
        pnl = float(p.get("realizedPnl") or 0)
        total_pnl += pnl
        if pnl > 0:
            wins += 1
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
            win_pnls.append(pnl)
        else:
            losses += 1
            # Promote current win-streak into max_streak BEFORE zeroing it;
            # otherwise W-W-W-L would record max_streak=0.
            max_streak = max(max_streak, cur_streak)
            cur_streak = 0
            loss_pnls.append(pnl)

    total = wins + losses
    return {
        "wallet": w,
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total) if total > 0 else 0.0,
        "current_streak": cur_streak,
        "max_streak": max_streak,
        "total_pnl": total_pnl,
        "avg_win": (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0,
        "avg_loss": (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0,
    }


async def discover_qualified_wallets(
    http: httpx.AsyncClient,
    *,
    categories: list[str] | None = None,
    time_period: str = "MONTH",
    limit_per_category: int = 25,
    min_pnl: float = 0.0,
    min_win_rate: float = 0.60,
    min_win_streak: int = 3,
    min_total_trades: int = 5,
) -> list[dict[str, Any]]:
    """Discover top wallets and filter by actual win rate and streak.

    1. Fetches leaderboard candidates
    2. For each, fetches closed positions and computes win rate + streak
    3. Only returns wallets that meet all quality thresholds
    """
    candidates = await discover_top_wallets(
        http,
        categories=categories,
        time_period=time_period,
        limit_per_category=limit_per_category,
        min_pnl=min_pnl,
    )

    qualified: list[dict[str, Any]] = []
    for cand in candidates:
        quality = await analyze_wallet_quality(http, cand["wallet"])
        merged = {**cand, **quality}

        if quality["total"] < min_total_trades:
            merged["_rejected"] = f"too_few_trades ({quality['total']} < {min_total_trades})"
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue
        if quality["win_rate"] < min_win_rate:
            merged["_rejected"] = f"low_win_rate ({quality['win_rate']:.0%} < {min_win_rate:.0%})"
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue
        if quality["max_streak"] < min_win_streak:
            merged["_rejected"] = f"low_streak ({quality['max_streak']} < {min_win_streak})"
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue

        qualified.append(merged)
        log.info(
            "leaderboard QUALIFIED %s: WR=%.0f%% streak=%d trades=%d pnl=$%.0f",
            cand["wallet"][:12], quality["win_rate"] * 100,
            quality["max_streak"], quality["total"], quality["total_pnl"],
        )

    qualified.sort(key=lambda x: (-x["win_rate"], -x["max_streak"], -x["pnl"]))
    return qualified
