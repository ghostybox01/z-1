"""Polymarket leaderboard: auto-discover top wallets by PnL for copy-trading.

Data-source notes (verified empirically on 2026-05-19):
  * ``/closed-positions`` is **server-capped at 50 rows** regardless of the ``limit``
    requested AND **only returns positions that closed at a profit** (server filters
    losses). Treating it as a complete history yields a fake 100% win rate.
  * ``/trades`` returns full BUY/SELL activity with no artificial cap. Use this as
    the authoritative source for active-trading round-trips.
  * Most Polymarket P&L is realized at **market resolution** (binary outcome, no
    SELL), not via active selling. A top wallet often has 480+ BUY and only a
    handful of SELL trades in its last 500. Round-trip reconstruction therefore
    misses most P&L — leaderboard PnL is the truth signal for total performance.

Wallet quality is a **multi-source decision**:
  1. Active round-trips from /trades give us verifiable wins AND losses.
  2. /closed-positions gives us a recent slice of resolved wins (filtered, capped).
  3. Leaderboard PnL is the authoritative total.

A wallet only qualifies when we have enough loss visibility (≥2 verified losses
from round-trips OR ≥20 resolved wins for large-sample confidence). Anything
else risks copying a wallet whose losing trades we simply cannot see.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

import httpx

from bot.http_retry import get_json_retry

log = logging.getLogger("polymarket.leaderboard")

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
CLOSED_POSITIONS_URL = "https://data-api.polymarket.com/closed-positions"
TRADES_URL = "https://data-api.polymarket.com/trades"

CLOSED_POSITIONS_SERVER_CAP = 50
_PNL_EPSILON = 1e-6

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


def _reconstruct_round_trips(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk chronological trades, FIFO-match BUY→SELL to produce realized round-trips.

    Each round-trip is ``{asset, pnl, size, buy_price, sell_price, ts}`` where
    ``ts`` is the timestamp of the SELL (the close event).

    Notes:
      * Per-asset inventory is a FIFO queue of (price, size) lots.
      * BUYs add to inventory at their price.
      * SELLs consume oldest lots first; each consumed slice yields one round-trip.
      * A SELL with no inventory is ignored (would imply we're missing earlier BUYs
        beyond our trade window — out of scope to reconstruct).
      * Trades with non-positive size or unparseable fields are skipped silently.

    This intentionally does NOT see "wins" from market-resolution events (held
    YES that resolves to $1). Those show up in /closed-positions instead.
    """
    def _f(v: Any, default: float = 0.0) -> float:
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    # Defensive sort — chronological by timestamp, oldest first
    try:
        ordered = sorted(trades, key=lambda t: _f(t.get("timestamp")))
    except Exception:
        ordered = list(trades)

    books: dict[str, list[list[float]]] = defaultdict(list)  # asset -> [[px, size], ...]
    round_trips: list[dict[str, Any]] = []

    for t in ordered:
        a = t.get("asset")
        side = str(t.get("side") or "").upper()
        px = _f(t.get("price"))
        sz = _f(t.get("size"))
        ts = _f(t.get("timestamp"))
        if not a or sz <= 0 or px <= 0:
            continue
        if side == "BUY":
            books[a].append([px, sz])
        elif side == "SELL":
            remaining = sz
            while remaining > 1e-9 and books[a]:
                buy_px, buy_sz = books[a][0]
                take = min(remaining, buy_sz)
                pnl = (px - buy_px) * take
                round_trips.append({
                    "asset": str(a),
                    "pnl": pnl,
                    "size": take,
                    "buy_price": buy_px,
                    "sell_price": px,
                    "ts": ts,
                })
                buy_sz -= take
                remaining -= take
                if buy_sz < 1e-9:
                    books[a].pop(0)
                else:
                    books[a][0][1] = buy_sz
            # SELL with no inventory: silently drop (window truncation)

    return round_trips


def _open_inventory_size(books: dict[str, list[list[float]]]) -> int:
    """Number of distinct assets still held after reconstruction."""
    return sum(1 for a, lots in books.items() if lots and sum(l[1] for l in lots) > 1e-9)


async def _safe_fetch(
    http: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    label: str,
) -> list[dict[str, Any]]:
    try:
        data = await get_json_retry(http, url, params=params)
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("%s fetch failed: %s", label, e)
        return []


async def analyze_wallet_quality(
    http: httpx.AsyncClient,
    wallet: str,
    *,
    trade_limit: int = 500,
    closed_limit: int = 500,
    leaderboard_pnl: float | None = None,
    limit: int | None = None,  # deprecated alias; kept for backward-compat
) -> dict[str, Any]:
    """Multi-source wallet quality analysis.

    Combines /trades (full BUY/SELL → active round-trips with real wins AND
    losses) with /closed-positions (recent resolved winners, server-capped at 50,
    server-filtered to wins only). Returns a dict containing the same
    backward-compatible keys as before plus richer quality signals:

      Backward-compatible:
        wallet, total, wins, losses, win_rate, current_streak, max_streak,
        total_pnl, avg_win, avg_loss

      New (data-quality signals):
        active_round_trips         — completed BUY→SELL pairs (verified)
        active_wins, active_losses — round-trip outcomes
        resolved_wins              — from /closed-positions (filtered to wins)
        observed_total_pnl         — sum of round-trip pnl + closed-position pnl
        data_truncation_risk       — True iff closed-positions returned >= cap
        loss_visibility            — "verified" | "partial" | "none"
        verified_total             — active wins + active losses (only what we
                                     can verify both sides of)
        open_position_count        — assets still in inventory after walk

    ``win_rate`` semantics (changed): computed from VERIFIED outcomes only
    (active round-trips). If we cannot observe at least one loss AND the
    resolved-wins sample is small (<20), ``win_rate`` is set to ``None`` so the
    caller's gate can reject the wallet rather than treat zero observed losses
    as 100% performance.
    """
    w = wallet.strip().lower()
    # Allow the old keyword arg to work for backward compat
    if limit is not None and closed_limit == 500:
        closed_limit = int(limit)

    trade_lim = max(1, min(500, trade_limit))
    closed_lim = max(1, min(500, closed_limit))

    trades_raw, closed_raw = await asyncio.gather(
        _safe_fetch(http, TRADES_URL, {"user": w, "limit": str(trade_lim)}, f"trades {w[:12]}"),
        _safe_fetch(http, CLOSED_POSITIONS_URL, {"user": w, "limit": str(closed_lim)}, f"closed-positions {w[:12]}"),
    )

    # --- Active round-trips (verified wins AND losses) ----------------------
    round_trips = _reconstruct_round_trips(trades_raw)
    active_wins = sum(1 for rt in round_trips if rt["pnl"] > _PNL_EPSILON)
    active_losses = sum(1 for rt in round_trips if rt["pnl"] < -_PNL_EPSILON)
    active_pnl = sum(rt["pnl"] for rt in round_trips)

    # --- Resolved wins (filtered by server; losses are invisible here) -------
    resolved_wins = 0
    resolved_pnl = 0.0
    resolved_events: list[tuple[float, float]] = []  # (timestamp, pnl)
    for p in closed_raw:
        pnl = 0.0
        try:
            pnl = float(p.get("realizedPnl") or 0)
        except (TypeError, ValueError):
            continue
        if pnl > _PNL_EPSILON:
            resolved_wins += 1
        resolved_pnl += pnl
        try:
            ts = float(p.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        resolved_events.append((ts, pnl))

    data_truncation_risk = len(closed_raw) >= CLOSED_POSITIONS_SERVER_CAP

    # --- Loss visibility verdict --------------------------------------------
    if active_losses > 0:
        loss_visibility = "verified"
    elif (active_wins + resolved_wins) >= 20:
        # Large-sample confidence even without observed losses
        loss_visibility = "partial"
    else:
        loss_visibility = "none"

    # --- Win rate (verified only) -------------------------------------------
    verified_total = active_wins + active_losses
    if verified_total == 0:
        # No round-trips at all — wallet is buy-and-hold-to-resolution. We
        # cannot compute a verifiable win rate; signal None so callers reject.
        win_rate: float | None = None
    elif loss_visibility == "none":
        win_rate = None
    else:
        win_rate = active_wins / verified_total

    # --- Streaks (chronological merge of round-trips + resolved closes) -----
    merged: list[tuple[float, float]] = [(rt["ts"], rt["pnl"]) for rt in round_trips]
    merged.extend(resolved_events)
    merged.sort(key=lambda x: x[0])
    cur_streak = 0
    max_streak = 0
    for _, pnl in merged:
        if pnl > _PNL_EPSILON:
            cur_streak += 1
            if cur_streak > max_streak:
                max_streak = cur_streak
        elif pnl < -_PNL_EPSILON:
            if cur_streak > max_streak:
                max_streak = cur_streak
            cur_streak = 0
        # pnl == 0 neither extends nor breaks a streak

    # --- Backward-compatible aggregates -------------------------------------
    total = verified_total + resolved_wins  # what the old field meant
    wins = active_wins + resolved_wins
    losses = active_losses
    win_pnls = [rt["pnl"] for rt in round_trips if rt["pnl"] > _PNL_EPSILON]
    loss_pnls = [rt["pnl"] for rt in round_trips if rt["pnl"] < -_PNL_EPSILON]
    observed_total_pnl = active_pnl + resolved_pnl

    # Compute open-position count by re-walking trades minimally
    books: dict[str, list[list[float]]] = defaultdict(list)
    def _f2(v: Any) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    for t in sorted(trades_raw, key=lambda t: _f2(t.get("timestamp"))):
        side = str(t.get("side") or "").upper()
        a = t.get("asset")
        sz = _f2(t.get("size"))
        px = _f2(t.get("price"))
        if not a or sz <= 0 or px <= 0:
            continue
        if side == "BUY":
            books[a].append([px, sz])
        elif side == "SELL":
            remaining = sz
            while remaining > 1e-9 and books[a]:
                lot_px, lot_sz = books[a][0]
                take = min(remaining, lot_sz)
                remaining -= take
                lot_sz -= take
                if lot_sz < 1e-9:
                    books[a].pop(0)
                else:
                    books[a][0][1] = lot_sz

    return {
        # backward-compatible
        "wallet": w,
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,  # may be None now — callers must handle
        "current_streak": cur_streak,
        "max_streak": max_streak,
        "total_pnl": observed_total_pnl,
        "avg_win": (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0,
        "avg_loss": (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0,
        # new — data-quality signals
        "active_round_trips": len(round_trips),
        "active_wins": active_wins,
        "active_losses": active_losses,
        "resolved_wins": resolved_wins,
        "observed_total_pnl": observed_total_pnl,
        "leaderboard_pnl": leaderboard_pnl,
        "data_truncation_risk": data_truncation_risk,
        "loss_visibility": loss_visibility,
        "verified_total": verified_total,
        "open_position_count": _open_inventory_size(books),
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
        quality = await analyze_wallet_quality(
            http, cand["wallet"], leaderboard_pnl=cand.get("pnl")
        )
        merged = {**cand, **quality}

        # Sample-size gate. With truncated closed-positions, demand more.
        required_sample = (
            max(min_total_trades * 4, 20)
            if quality["data_truncation_risk"]
            else min_total_trades
        )
        if quality["total"] < required_sample:
            merged["_rejected"] = (
                f"too_few_trades ({quality['total']} < {required_sample}, "
                f"truncation_risk={quality['data_truncation_risk']})"
            )
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue

        # Loss-visibility gate. Don't qualify wallets whose losses we can't see.
        if quality["loss_visibility"] == "none":
            merged["_rejected"] = (
                f"loss_invisible (active_rt={quality['active_round_trips']}, "
                f"resolved_wins={quality['resolved_wins']}) — refusing to copy a "
                f"wallet whose losing trades are invisible to us"
            )
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue

        # Win-rate gate. ``win_rate`` is None when we can't verify it.
        wr = quality["win_rate"]
        if wr is None:
            merged["_rejected"] = (
                f"win_rate_unverifiable (verified_total={quality['verified_total']}, "
                f"loss_visibility={quality['loss_visibility']})"
            )
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue
        if wr < min_win_rate:
            merged["_rejected"] = f"low_win_rate ({wr:.0%} < {min_win_rate:.0%})"
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue

        if quality["max_streak"] < min_win_streak:
            merged["_rejected"] = f"low_streak ({quality['max_streak']} < {min_win_streak})"
            log.info("leaderboard skip %s: %s", cand["wallet"][:12], merged["_rejected"])
            continue

        # Sanity: if leaderboard reports massive PnL but we observe a tiny
        # fraction, the wallet's history goes far outside our trade window.
        # Don't reject (the leaderboard signal is real), but log so we know.
        lb_pnl = cand.get("pnl") or 0.0
        obs_pnl = quality["observed_total_pnl"]
        if lb_pnl > 0 and obs_pnl > 0 and obs_pnl < lb_pnl * 0.05:
            log.info(
                "leaderboard note %s: observed PnL $%.0f is <5%% of leaderboard PnL $%.0f "
                "(history extends beyond our trade window)",
                cand["wallet"][:12], obs_pnl, lb_pnl,
            )

        qualified.append(merged)
        log.info(
            "leaderboard QUALIFIED %s: WR=%.0f%% streak=%d active_rt=%d "
            "(W=%d/L=%d) resolved_wins=%d obs_pnl=$%.0f loss_vis=%s trunc=%s",
            cand["wallet"][:12], wr * 100,
            quality["max_streak"], quality["active_round_trips"],
            quality["active_wins"], quality["active_losses"],
            quality["resolved_wins"], quality["observed_total_pnl"],
            quality["loss_visibility"], quality["data_truncation_risk"],
        )

    qualified.sort(key=lambda x: (-(x["win_rate"] or 0), -x["max_streak"], -x["pnl"]))
    return qualified
