"""Self-managing copy-trade wallet pool.

Handles:
- Periodic leaderboard re-scan across all categories
- Quality gate enforcement (win rate, streak, min trades)
- Automatic pruning of wallets that drop below thresholds
- Per-wallet performance tracking with live stats
- Category-aware discovery (maps leaderboard categories to bot categories)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from bot.db.kv import upsert_many_kv
from bot.leaderboard import (
    analyze_wallet_quality,
    discover_qualified_wallets,
    CATEGORIES as LB_CATEGORIES,
)

log = logging.getLogger("polymarket.copy_manager")

# How often to re-scan leaderboard (seconds). Default: every 6 hours.
_DEFAULT_REFRESH_INTERVAL = 6 * 3600

# E14c: maximum age of a cached wallet-quality stat before we MUST re-check
# rather than reuse. Prevents the prune path from reusing day-old stats for
# a wallet whose performance has just cratered.
_CACHE_TTL_SECONDS = 600.0


@dataclass
class WalletStats:
    """Live tracking for a watched wallet."""
    wallet: str
    added_at: float = 0.0
    last_checked: float = 0.0
    win_rate: float = 0.0
    wins: int = 0
    losses: int = 0
    max_streak: int = 0
    current_streak: int = 0
    total_pnl: float = 0.0
    source_category: str = ""
    user_name: str = ""
    leaderboard_pnl: float = 0.0
    status: str = "active"  # active | probation | pruned | manual
    account_age_days: float = 0.0
    profit_factor: float = 0.0


@dataclass
class CopyManagerState:
    """Persistent state for the copy manager."""
    wallet_stats: dict[str, WalletStats] = field(default_factory=dict)
    last_refresh: float = 0.0
    last_prune: float = 0.0
    refresh_count: int = 0
    total_added: int = 0
    total_pruned: int = 0


class CopyManager:
    """Runs inside the trading loop to keep copy_watch_wallets fresh and qualified."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.state = CopyManagerState()
        self._http: httpx.AsyncClient | None = None
        self._seed_manual_wallets()

    def _seed_manual_wallets(self) -> None:
        """Seed configured wallets into tracking.

        Auto-manage OFF -> seed as 'manual' (pinned, never pruned) so the user's
        explicit picks survive. Auto-manage ON -> seed as 'active' so the persisted
        list is continuously re-vetted and pruned on each refresh, keeping us on the
        currently-most-successful wallets instead of a frozen list.
        """
        seed_status = "active" if self._auto_manage() else "manual"
        for wallet in list(getattr(self.settings, "copy_watch_wallets", []) or []):
            w = str(wallet).strip().lower()
            if not w:
                continue
            if w in self.state.wallet_stats:
                continue
            self.state.wallet_stats[w] = WalletStats(
                wallet=w,
                added_at=time.time(),
                last_checked=0.0,
                status=seed_status,
            )

    def sync_settings(self, settings: Any) -> None:
        """Apply reloaded settings and keep manual wallets pinned."""
        self.settings = settings
        self._seed_manual_wallets()

    def _refresh_interval(self) -> float:
        return float(getattr(self.settings, "copy_refresh_interval_hours", 6.0) or 6.0) * 3600

    def _min_win_rate(self) -> float:
        return float(getattr(self.settings, "copy_min_win_rate", 0.60) or 0.60)

    def _min_win_streak(self) -> int:
        return int(getattr(self.settings, "copy_min_win_streak", 3) or 3)

    def _min_total_trades(self) -> int:
        return int(getattr(self.settings, "copy_min_total_trades", 5) or 5)

    def _min_account_age_days(self) -> float:
        return float(getattr(self.settings, "copy_min_account_age_days", 30.0) or 0.0)

    def _min_profit_factor(self) -> float:
        return float(getattr(self.settings, "copy_min_profit_factor", 1.5) or 0.0)

    def _max_wallets(self) -> int:
        return int(getattr(self.settings, "copy_max_watched_wallets", 50) or 50)

    def _auto_manage(self) -> bool:
        return bool(getattr(self.settings, "copy_auto_manage", True))

    def _prune_below_win_rate(self) -> float:
        """Wallets that drop below this get pruned. Slightly lower than discovery threshold."""
        return max(self._min_win_rate() - 0.10, 0.0)

    def needs_refresh(self) -> bool:
        if not self._auto_manage():
            return False
        if self.state.last_refresh == 0:
            return True
        return (time.time() - self.state.last_refresh) >= self._refresh_interval()

    async def refresh(self, http: httpx.AsyncClient) -> dict[str, Any]:
        """Full refresh cycle: discover new wallets + prune underperformers."""
        self._http = http
        self._seed_manual_wallets()
        result: dict[str, Any] = {"added": 0, "pruned": 0, "checked": 0, "active": 0}

        try:
            # 1. Discover new qualified wallets from all leaderboard categories
            new_count = await self._discover_and_add(http)
            result["added"] = new_count

            # 2. Re-check existing wallets and prune underperformers
            prune_count = await self._check_and_prune(http)
            result["pruned"] = prune_count

            # 3. Persist updated wallet list to DB
            self._persist_wallets()

            result["active"] = sum(1 for s in self.state.wallet_stats.values() if s.status == "active")
            result["checked"] = len(self.state.wallet_stats)

            self.state.last_refresh = time.time()
            self.state.refresh_count += 1

            log.info(
                "CopyManager refresh #%d: +%d added, -%d pruned, %d active",
                self.state.refresh_count, new_count, prune_count, result["active"],
            )
        except Exception as e:
            log.error("CopyManager refresh failed: %s", e)
            result["error"] = str(e)

        return result

    async def _discover_and_add(self, http: httpx.AsyncClient) -> int:
        """Discover qualified wallets from all leaderboard categories."""
        lb_cats = list(getattr(self.settings, "copy_discover_categories", None) or list(LB_CATEGORIES))

        qualified = await discover_qualified_wallets(
            http,
            categories=lb_cats,
            time_period="MONTH",
            limit_per_category=25,
            min_pnl=0,
            min_win_rate=self._min_win_rate(),
            min_win_streak=self._min_win_streak(),
            min_total_trades=self._min_total_trades(),
            min_account_age_days=self._min_account_age_days(),
            min_profit_factor=self._min_profit_factor(),
        )

        added = 0
        max_w = self._max_wallets()
        active_count = sum(1 for s in self.state.wallet_stats.values() if s.status == "active")

        for q in qualified:
            w = q["wallet"]
            if w in self.state.wallet_stats:
                # Update stats for existing wallet
                st = self.state.wallet_stats[w]
                st.win_rate = q.get("win_rate", st.win_rate)
                st.wins = q.get("wins", st.wins)
                st.losses = q.get("losses", st.losses)
                st.max_streak = q.get("max_streak", st.max_streak)
                st.current_streak = q.get("current_streak", st.current_streak)
                st.total_pnl = q.get("total_pnl", st.total_pnl)
                st.account_age_days = q.get("account_age_days", 0) or 0
                st.profit_factor = q.get("profit_factor", 0) or 0
                st.last_checked = time.time()
                if st.status == "pruned":
                    st.status = "active"
                    added += 1
                continue

            if active_count + added >= max_w:
                break

            self.state.wallet_stats[w] = WalletStats(
                wallet=w,
                added_at=time.time(),
                last_checked=time.time(),
                win_rate=q.get("win_rate", 0),
                wins=q.get("wins", 0),
                losses=q.get("losses", 0),
                max_streak=q.get("max_streak", 0),
                current_streak=q.get("current_streak", 0),
                total_pnl=q.get("total_pnl", 0),
                source_category=q.get("category", "OVERALL"),
                user_name=q.get("userName", ""),
                leaderboard_pnl=q.get("pnl", 0),
                status="active",
                account_age_days=q.get("account_age_days", 0) or 0,
                profit_factor=q.get("profit_factor", 0) or 0,
            )
            added += 1

        self.state.total_added += added
        return added

    async def _check_and_prune(self, http: httpx.AsyncClient) -> int:
        """Re-check all active wallets and prune those below threshold.
        Wallets that were freshly analyzed during _discover_and_add in this same
        cycle re-use cached stats to avoid an N+1 round of /closed-positions calls."""
        prune_threshold = self._prune_below_win_rate()
        min_trades = self._min_total_trades()
        pruned = 0
        now = time.time()
        skip_if_checked_within_s = max(60.0, 0.9 * self._refresh_interval())

        for w, st in list(self.state.wallet_stats.items()):
            if st.status != "active":
                continue

            # E14c: only reuse cached stats if both the discovery-window
            # heuristic AND a hard TTL still consider them fresh. Stale stats
            # silently kept wallets active long after performance dropped.
            cache_fresh = (
                st.last_checked
                and (now - st.last_checked) < skip_if_checked_within_s
                and (now - st.last_checked) < _CACHE_TTL_SECONDS
            )
            if cache_fresh:
                total = int(st.wins + st.losses)
                if total >= min_trades and st.win_rate < prune_threshold:
                    st.status = "pruned"
                    pruned += 1
                    log.info(
                        "CopyManager PRUNED %s (cached): WR=%.0f%% < %.0f%% threshold",
                        w[:12], st.win_rate * 100, prune_threshold * 100,
                    )
                continue

            try:
                quality = await analyze_wallet_quality(http, w, closed_limit=100)
            except Exception as e:
                log.warning("quality check failed for %s: %s", w[:12], e)
                continue

            # analyze_wallet_quality may now return win_rate=None when loss
            # visibility is insufficient. Prune those wallets — we can't keep
            # copying someone whose losing trades we cannot see.
            new_wr = quality.get("win_rate")
            if new_wr is None:
                st.status = "pruned"
                pruned += 1
                st.last_checked = time.time()
                log.info(
                    "CopyManager PRUNED %s: win_rate unverifiable (loss_visibility=%s, active_rt=%d)",
                    w[:12], quality.get("loss_visibility", "?"),
                    quality.get("active_round_trips", 0),
                )
                continue

            st.win_rate = new_wr
            st.wins = quality["wins"]
            st.losses = quality["losses"]
            st.max_streak = quality["max_streak"]
            st.current_streak = quality["current_streak"]
            st.total_pnl = quality["total_pnl"]
            age = quality.get("account_age_days", 0)
            pf = quality.get("profit_factor")
            st.account_age_days = age or 0
            st.profit_factor = pf or 0
            st.last_checked = time.time()

            if quality["total"] >= min_trades and new_wr < prune_threshold:
                st.status = "pruned"
                pruned += 1
                log.info(
                    "CopyManager PRUNED %s: WR=%.0f%% < %.0f%% threshold",
                    w[:12], new_wr * 100, prune_threshold * 100,
                )
            elif age < self._min_account_age_days():
                st.status = "pruned"
                pruned += 1
                log.info(
                    "CopyManager PRUNED %s: account_age_days=%.1f < %.1f threshold",
                    w[:12], age, self._min_account_age_days(),
                )
            elif self._min_profit_factor() > 0 and pf is not None and pf < self._min_profit_factor():
                st.status = "pruned"
                pruned += 1
                log.info(
                    "CopyManager PRUNED %s: profit_factor=%.2f < %.2f threshold",
                    w[:12], pf, self._min_profit_factor(),
                )

        self.state.total_pruned += pruned
        return pruned

    def _persist_wallets(self) -> None:
        """Write the active wallet list back to copy_watch_wallets in DB."""
        watch = [
            w
            for w, st in self.state.wallet_stats.items()
            if st.status in {"active", "manual"}
        ]
        upsert_many_kv({"copy_watch_wallets": json.dumps(watch)})

    def get_managed_wallets(self) -> list[dict[str, Any]]:
        """Return all tracked wallets with their stats for the dashboard."""
        out = []
        for w, st in self.state.wallet_stats.items():
            out.append({
                "wallet": w,
                "status": st.status,
                "win_rate": st.win_rate,
                "wins": st.wins,
                "losses": st.losses,
                "max_streak": st.max_streak,
                "current_streak": st.current_streak,
                "total_pnl": st.total_pnl,
                "source_category": st.source_category,
                "user_name": st.user_name,
                "leaderboard_pnl": st.leaderboard_pnl,
                "added_at": st.added_at,
                "last_checked": st.last_checked,
            })
        out.sort(key=lambda x: (-1 if x["status"] == "active" else 0, -x["win_rate"]))
        return out

    def get_summary(self) -> dict[str, Any]:
        stats = self.state.wallet_stats
        active = sum(1 for s in stats.values() if s.status == "active")
        manual = sum(1 for s in stats.values() if s.status == "manual")
        pruned = sum(1 for s in stats.values() if s.status == "pruned")
        return {
            "active_wallets": active,
            "manual_wallets": manual,
            "pruned_wallets": pruned,
            "total_tracked": len(stats),
            "refresh_count": self.state.refresh_count,
            "total_added": self.state.total_added,
            "total_pruned": self.state.total_pruned,
            "last_refresh": self.state.last_refresh,
            "next_refresh_in_s": max(0, self._refresh_interval() - (time.time() - self.state.last_refresh)) if self.state.last_refresh else 0,
            "auto_manage": self._auto_manage(),
            "min_win_rate": self._min_win_rate(),
            "prune_threshold": self._prune_below_win_rate(),
        }
