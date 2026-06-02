"""
Lightweight copy-trade signals from Polymarket Data API activity stream.
Requires COPY_WATCH_WALLETS and AGENT_COPY=true.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Set

import httpx

from bot.categories import MarketCategory
from bot.copy_rules import build_candidate, limit_price_with_buffer, passes_filters, wallet_score
from bot.http_retry import get_json_retry
from bot.models import TradeIntent

log = logging.getLogger("polymarket.agent.copy")

ACTIVITY_URL = "https://data-api.polymarket.com/activity"


class CopySignalAgent:
    name = "copy_signal"
    priority = 100

    def __init__(self, settings: Any):
        self.settings = settings
        self._seen: Set[str] = set()
        self._cold_start = True
        self.last_note: str = ""

    @property
    def is_cold_start(self) -> bool:
        return self._cold_start

    async def propose(self, http: httpx.AsyncClient) -> list[TradeIntent]:
        if not self.settings.agent_copy or not self.settings.copy_watch_wallets:
            self.last_note = "disabled or no wallets configured"
            return []

        intents: list[TradeIntent] = []
        wallets_polled = 0
        wallets_score_skipped = 0
        candidates_seen_dup = 0
        candidates_cold_skipped = 0
        candidates_filter_rejected = 0
        candidates_market_capped = 0
        candidates_stale_skipped = 0
        api_errors = 0
        cycle_markets: dict[str, int] = {}
        max_per_market = 2

        for wallet in self.settings.copy_watch_wallets:
            try:
                rows = await get_json_retry(
                    http,
                    ACTIVITY_URL,
                    params={"user": wallet, "limit": "40"},
                )
                if not isinstance(rows, list):
                    api_errors += 1
                    continue
            except Exception as e:
                log.warning("copy poll %s…: %s", wallet[:10], e)
                api_errors += 1
                continue

            wallets_polled += 1

            score, parts = wallet_score(
                rows if isinstance(rows, list) else [],
                wallet=wallet,
                default_bet_usd=float(self.settings.default_bet_usd),
                settings=self.settings,
            )
            min_score = float(getattr(self.settings, "copy_min_wallet_score", 0.0) or 0.0)
            if score < min_score:
                log.info(
                    "copy skip wallet=%s score=%.3f < %.3f parts=%s",
                    wallet[:10],
                    score,
                    min_score,
                    {k: round(v, 3) if isinstance(v, (int, float)) else v for k, v in parts.items()},
                )
                wallets_score_skipped += 1
                continue

            for entry in rows:
                c = build_candidate(entry, wallet, float(self.settings.default_bet_usd))
                if c is None:
                    continue

                if c.tx_key in self._seen:
                    candidates_seen_dup += 1
                    continue
                self._seen.add(c.tx_key)
                if self._cold_start:
                    candidates_cold_skipped += 1
                    continue

                # Freshness guard: skip stale signals (price has moved since the whale's
                # trade). Default 10 min; tunable via copy_max_signal_age_seconds.
                max_age = float(getattr(self.settings, "copy_max_signal_age_seconds", 600.0) or 0.0)
                if max_age > 0:
                    ts = entry.get("timestamp")
                    try:
                        age_s = time.time() - float(ts) if ts else 0.0
                    except (TypeError, ValueError):
                        age_s = 0.0
                    if age_s > max_age:
                        candidates_stale_skipped += 1
                        continue

                ok, _reason = passes_filters(self.settings, c)
                if not ok:
                    candidates_filter_rejected += 1
                    continue
                market_key = c.token_id
                cycle_markets[market_key] = cycle_markets.get(market_key, 0) + 1
                if cycle_markets[market_key] > max_per_market:
                    candidates_market_capped += 1
                    continue
                max_px = limit_price_with_buffer(self.settings, c.price)
                usdc = max(self.settings.min_bet_usd, min(self.settings.max_bet_usd, c.usdc))
                # Polymarket enforces a 5-share minimum order; estimate the real cost
                # (~5 * price) so risk caps + balance checks are honest before
                # execution floors the share count.
                usdc = max(usdc, round(5.0 * c.price, 2))
                cond = str(entry.get("conditionId") or entry.get("condition_id") or "")
                try:
                    cat = MarketCategory(c.category)
                except ValueError:
                    cat = MarketCategory.OTHER

                intents.append(
                    TradeIntent(
                        agent=self.name,
                        priority=self.priority,
                        token_id=c.token_id,
                        condition_id=cond,
                        question=c.title[:500],
                        outcome=str(entry.get("outcome") or c.outcome or "unknown"),
                        side="BUY",
                        max_price=max_px,
                        size_usd=usdc,
                        category=cat,
                        strategy="copy_trade",
                        reason=f"wallet={wallet[:10]}… px~{c.price:.3f}",
                        reference_price=c.price,
                        source_wallet=wallet,
                    )
                )

        was_cold = self._cold_start
        if self._cold_start:
            self._cold_start = False
            log.info(
                "CopySignalAgent: cold start done — %d activity keys seeded (no replay)",
                len(self._seen),
            )

        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2500:])

        parts_list = []
        if was_cold:
            parts_list.append(f"cold_start_seeded={candidates_cold_skipped}")
        parts_list.append(f"polled={wallets_polled}/{len(self.settings.copy_watch_wallets)}")
        if wallets_score_skipped:
            parts_list.append(f"score_skip={wallets_score_skipped}")
        if candidates_seen_dup:
            parts_list.append(f"dup={candidates_seen_dup}")
        if candidates_filter_rejected:
            parts_list.append(f"filtered={candidates_filter_rejected}")
        if candidates_stale_skipped:
            parts_list.append(f"stale={candidates_stale_skipped}")
        if candidates_market_capped:
            parts_list.append(f"mkt_cap={candidates_market_capped}")
        if api_errors:
            parts_list.append(f"api_err={api_errors}")
        parts_list.append(f"new={len(intents)}")
        self.last_note = "; ".join(parts_list)

        log.info("CopySignalAgent: %d new signals (%s)", len(intents), self.last_note)
        return intents
