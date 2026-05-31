"""
Rolling z-score on YES CLOB mid per market (condition_id): enter on stretched dislocation,
mean-reversion style (cheap YES → BUY YES; rich YES → BUY NO).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Deque, Dict, Set

from bot.clob_utils import parse_midpoint
from bot.models import TradeIntent

log = logging.getLogger("polymarket.agent.zscore")


class ZScoreEdgeAgent:
    name = "zscore_edge"
    priority = 48

    def __init__(self, settings: Any):
        self.settings = settings
        self._yes_mids: Dict[str, Deque[float]] = defaultdict(deque)

    async def propose(
        self,
        clob: Any,
        markets: list[dict],
        position_tokens: Set[str],
        rate_limit: Callable[[], Awaitable[None]],
    ) -> list[TradeIntent]:
        if not getattr(self.settings, "agent_zscore", False):
            return []
        win = max(5, int(getattr(self.settings, "zscore_window", 24)))
        z_min = float(getattr(self.settings, "zscore_entry_abs", 2.2))
        need_n = max(3, int(getattr(self.settings, "zscore_min_samples", 12)))

        out: list[TradeIntent] = []
        for m in markets:
            tokens = m.get("tokens", [])
            outcomes = m.get("outcomes", ["Yes", "No"])
            if len(tokens) < 2:
                continue
            cid = str(m.get("condition_id", "") or "")
            if not cid:
                continue

            cat = m["category"]
            q = m.get("question", "")

            # Pre-filter using Gamma consensus price to avoid wasting CLOB calls
            gamma_prices = m.get("prices", [])
            if gamma_prices:
                gp0 = float(gamma_prices[0])
                if gp0 <= 0.03 or gp0 >= 0.97:
                    continue  # extreme Gamma price → CLOB will also be extreme, skip

            await rate_limit()
            try:
                raw0 = clob.get_midpoint(token_id=tokens[0])
                parsed = parse_midpoint(raw0)
                p0 = float(parsed) if parsed is not None else None
            except Exception:
                p0 = None
            if p0 is None or p0 <= 0.02 or p0 >= 0.98:
                continue

            hist = self._yes_mids[cid]
            hist.append(p0)
            while len(hist) > win:
                hist.popleft()

            if len(hist) < need_n:
                continue

            xs = list(hist)
            mu = sum(xs) / len(xs)
            var = sum((x - mu) ** 2 for x in xs) / max(len(xs) - 1, 1)
            sd = math.sqrt(var) if var > 1e-12 else 0.0
            if sd < 1e-4:
                continue
            z = (p0 - mu) / sd

            liq = float(m.get("liquidity", 0))
            liq_need = max(float(self.settings.value_liq_floor_usd), float(self.settings.min_clob_liquidity_usd))
            if liq < liq_need:
                continue

            usd = float(self.settings.default_bet_usd)

            if z <= -z_min and tokens[0] not in position_tokens:
                await rate_limit()
                try:
                    raw1 = clob.get_midpoint(token_id=tokens[1])
                    p1 = float(parse_midpoint(raw1)) if parse_midpoint(raw1) is not None else (1.0 - p0)
                except Exception:
                    p1 = 1.0 - p0
                if p1 <= 0.01 or p1 >= 0.99:
                    continue
                out.append(
                    TradeIntent(
                        agent=self.name,
                        priority=self.priority,
                        token_id=tokens[0],
                        condition_id=cid,
                        question=q,
                        outcome=str(outcomes[0] if outcomes else "Yes"),
                        side="BUY",
                        max_price=round(min(p0 * 1.015, 0.99), 4),
                        size_usd=usd,
                        category=cat,
                        strategy="zscore_buy_yes",
                        reason=f"z={z:.2f} p0={p0:.3f} mu={mu:.3f} sd={sd:.4f}",
                        # fair value = historical mean (mean-reversion target); EV > 0 when mu > entry
                        reference_price=round(min(max(mu, 0.01), 0.99), 4),
                    )
                )
            elif z >= z_min and tokens[1] not in position_tokens:
                await rate_limit()
                try:
                    raw1 = clob.get_midpoint(token_id=tokens[1])
                    p1 = float(parse_midpoint(raw1)) if parse_midpoint(raw1) is not None else (1.0 - p0)
                except Exception:
                    p1 = 1.0 - p0
                if p1 <= 0.01 or p1 >= 0.99:
                    continue
                # For NO (z >= +z_min → YES overpriced): fair value of NO = 1 - mu
                no_mu = 1.0 - mu
                out.append(
                    TradeIntent(
                        agent=self.name,
                        priority=self.priority,
                        token_id=tokens[1],
                        condition_id=cid,
                        question=q,
                        outcome=str(outcomes[1] if len(outcomes) > 1 else "No"),
                        side="BUY",
                        max_price=round(min(p1 * 1.015, 0.99), 4),
                        size_usd=usd,
                        category=cat,
                        strategy="zscore_buy_no",
                        reason=f"z={z:.2f} p0={p0:.3f} mu={mu:.3f} sd={sd:.4f}",
                        # fair value of NO = 1 - mean YES price (mean-reversion target)
                        reference_price=round(min(max(no_mu, 0.01), 0.99), 4),
                    )
                )

        log.info("ZScoreEdgeAgent: %d intents", len(out))
        return out
