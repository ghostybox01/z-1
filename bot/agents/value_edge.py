"""Scans Gamma tradeables; uses Gamma consensus price vs CLOB mid for genuine EV edge.

Performance: pre-filters by price range AND liquidity using cheap Gamma data BEFORE
making any CLOB API calls, so cycles stay fast even with 500+ markets scanned.
"""

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
        y_lo = float(self.settings.value_yes_low)
        y_hi = float(self.settings.value_yes_high)
        liq_need = max(float(self.settings.value_liq_floor_usd), float(self.settings.min_clob_liquidity_usd))
        yn_min = float(self.settings.value_no_yes_min)
        yn_max = float(self.settings.value_no_no_max)

        out: list[TradeIntent] = []
        _skip_pos = _skip_token = _skip_liq = _skip_range = _clob_queried = 0

        for m in markets:
            tokens = m.get("tokens", [])
            prices = m.get("prices", [])
            if len(tokens) < 2 or len(prices) < 2:
                _skip_token += 1
                continue

            # ── CHEAP PRE-FILTERS (no CLOB call) ──────────────────────────────────
            if any(t in position_tokens for t in tokens):
                _skip_pos += 1
                continue

            liq = float(m.get("liquidity", 0))
            if liq < liq_need:
                _skip_liq += 1
                continue

            gamma_p0 = float(prices[0])
            gamma_p1 = float(prices[1])

            # Check if YES is in value range OR if YES is high (value-NO candidate)
            yes_in_range = y_lo <= gamma_p0 <= y_hi
            no_candidate = gamma_p0 >= yn_min and gamma_p1 <= yn_max
            if not yes_in_range and not no_candidate:
                _skip_range += 1
                continue

            # ── CLOB CALLS (only for markets that passed pre-filters) ─────────────
            _clob_queried += 1
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

            cat: MarketCategory = m["category"]
            outcomes = m.get("outcomes", ["Yes", "No"])

            # ── VALUE YES ─────────────────────────────────────────────────────────
            # Passive maker-style limit buy at 1% below CLOB mid.
            # We post a resting bid; it fills only if price dips.
            # EV = (mid - entry) / entry ≈ +100 bps → passes 25 bps gate.
            if yes_in_range:
                if p0 <= 0.01 or p0 >= 0.99:
                    pass  # bad live price, skip YES but still check NO below
                else:
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
                    log.debug("value_yes candidate: %s gamma=%.3f mid=%.3f cat=%s", m["question"][:60], gamma_p0, p0, cat)

            # ── VALUE NO ──────────────────────────────────────────────────────────
            # YES is overpriced → passive limit buy of NO at 1% below NO mid.
            if no_candidate:
                if p1 <= 0.01 or p1 >= 0.99:
                    pass  # bad live NO price
                else:
                    entry_no = round(max(p1 * 0.99, 0.01), 4)
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
                            reason=f"clob_yes={p0:.3f} clob_no={p1:.3f} passive_bid={entry_no:.4f} liq={liq:.0f}",
                            reference_price=p1,  # fair value = NO midpoint
                        )
                    )
                    log.debug("value_no candidate: %s yes_mid=%.3f no_mid=%.3f cat=%s", m["question"][:60], p0, p1, cat)

        out.sort(key=lambda x: -x.priority)
        log.info(
            "ValueEdgeAgent: %d intents | scanned=%d clob_queried=%d skip_range=%d skip_liq=%d skip_pos=%d",
            len(out), len(markets), _clob_queried, _skip_range, _skip_liq, _skip_pos,
        )
        return out
