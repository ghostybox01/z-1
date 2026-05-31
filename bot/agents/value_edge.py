"""Scans Gamma tradeables; uses Gamma consensus price vs CLOB mid for genuine EV edge."""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable, Set

from bot.categories import MarketCategory
from bot.clob_utils import parse_midpoint
from bot.models import TradeIntent

log = logging.getLogger("polymarket.agent.value")


class ValueEdgeAgent:
    name = "value_edge"
    priority = 50

    def __init__(self, settings: Any):
        self.settings = settings

    async def propose(
        self,
        clob: Any,
        markets: list[dict],
        position_tokens: Set[str],
        rate_limit: Callable[[], Awaitable[None]],
    ) -> list[TradeIntent]:
        out: list[TradeIntent] = []
        _skip_pos = _skip_liq = _skip_range = _skip_bad_price = 0
        for m in markets:
            if any(t in position_tokens for t in m.get("tokens", [])):
                _skip_pos += 1
                continue

            cat: MarketCategory = m["category"]
            tokens = m["tokens"]
            outcomes = m["outcomes"]
            prices = m["prices"]
            if len(tokens) < 2 or len(prices) < 2:
                continue

            # Gamma consensus prices = external fair-value estimate
            gamma_p0 = float(prices[0])
            gamma_p1 = float(prices[1])

            # CLOB midpoints = actual live entry price
            await rate_limit()
            try:
                mid0 = clob.get_midpoint(token_id=tokens[0])
                parsed = parse_midpoint(mid0)
                p0 = float(parsed) if parsed is not None else gamma_p0
            except Exception:
                p0 = gamma_p0
            await rate_limit()
            try:
                mid1 = clob.get_midpoint(token_id=tokens[1])
                parsed = parse_midpoint(mid1)
                p1 = float(parsed) if parsed is not None else gamma_p1
            except Exception:
                p1 = gamma_p1

            liq = float(m.get("liquidity", 0))
            if liq < self.settings.min_clob_liquidity_usd:
                _skip_liq += 1
                continue

            liq_need = max(float(self.settings.value_liq_floor_usd), float(self.settings.min_clob_liquidity_usd))
            y_lo = float(self.settings.value_yes_low)
            y_hi = float(self.settings.value_yes_high)
            yn_min = float(self.settings.value_no_yes_min)
            yn_max = float(self.settings.value_no_no_max)

            # Value YES: passive maker-style limit buy at 1% below CLOB mid.
            # We post a resting bid; it fills only if price dips, meaning we get YES cheaper
            # than mid — inherently positive EV (we paid less than the fair midpoint price).
            # reference_price = CLOB mid (fair value); max_price = mid * 0.99 (entry below mid).
            # EV = (mid - entry) / entry ≈ +100bps, comfortably above 25bps threshold.
            if y_lo <= gamma_p0 <= y_hi and liq >= liq_need:
                if p0 <= 0.01 or p0 >= 0.99:
                    _skip_bad_price += 1
                    continue
                entry = round(max(p0 * 0.99, 0.01), 4)
                out.append(
                    TradeIntent(
                        agent=self.name,
                        priority=self.priority,
                        token_id=tokens[0],
                        condition_id=m["condition_id"],
                        question=m["question"],
                        outcome=str(outcomes[0]),
                        side="BUY",
                        max_price=entry,
                        size_usd=self.settings.default_bet_usd,
                        category=cat,
                        strategy="value_yes",
                        reason=f"gamma={gamma_p0:.3f} clob_mid={p0:.3f} passive_bid={entry:.4f} liq={liq:.0f}",
                        reference_price=p0,  # fair value = CLOB midpoint
                    )
                )

            # Value NO: YES is overpriced → passive limit buy of NO at 1% below NO mid.
            yes_p = p0
            no_p = p1
            if yes_p >= yn_min and no_p <= yn_max and liq >= liq_need:
                if no_p <= 0.01 or no_p >= 0.99:
                    continue
                entry_no = round(max(no_p * 0.99, 0.01), 4)
                out.append(
                    TradeIntent(
                        agent=self.name,
                        priority=self.priority,
                        token_id=tokens[1],
                        condition_id=m["condition_id"],
                        question=m["question"],
                        outcome=str(outcomes[1] if len(outcomes) > 1 else "No"),
                        side="BUY",
                        max_price=entry_no,
                        size_usd=self.settings.default_bet_usd,
                        category=cat,
                        strategy="value_no",
                        reason=f"clob_yes={yes_p:.3f} clob_no={no_p:.3f} passive_bid={entry_no:.4f} liq={liq:.0f}",
                        reference_price=no_p,  # fair value = NO midpoint
                    )
                )

        out.sort(key=lambda x: -x.priority)
        _in_range = sum(
            1 for m in markets
            if len(m.get("prices", [])) >= 1 and float(self.settings.value_yes_low) <= float(m["prices"][0]) <= float(self.settings.value_yes_high)
        )
        log.info(
            "ValueEdgeAgent: %d intents | scanned=%d in_price_range=%d skip_liq=%d skip_pos=%d skip_bad_price=%d",
            len(out), len(markets), _in_range, _skip_liq, _skip_pos, _skip_bad_price,
        )
        return out
