"""
Phase 2 EV (expected value) math for trade gating.

Provides: slippage-adjusted EV, minimum absolute expected profit,
time-to-resolution discount, and EV-aware pass/fail gating.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class EVResult:
    """Full EV analysis for a trade candidate."""
    raw_ev: float = 0.0
    slippage_adjusted_ev: float = 0.0
    absolute_expected_profit_usd: float = 0.0
    time_discount: float = 1.0
    passes: bool = False
    reason: str = ""


def compute_ev(
    *,
    entry_price: float,
    fair_price: float,
    size_usd: float,
    slippage_bps: float = 0.0,
    fee_bps: float = 0.0,
    hours_to_resolution: Optional[float] = None,
    min_ev_bps: float = 0.0,
    min_profit_usd: float = 0.0,
    time_discount_rate: float = 0.0,
) -> EVResult:
    """
    Compute expected value for a BUY trade.

    Fair price p = probability of YES outcome.
    Entry price = what we'd pay per share.
    EV = p * (1 - entry) - (1 - p) * entry = p - entry
    Slippage & fees are subtracted from EV.

    Args:
        entry_price: limit price for BUY
        fair_price: estimated true probability (from mid, model, etc.)
        size_usd: notional bet size
        slippage_bps: estimated slippage in basis points
        fee_bps: exchange fee in basis points
        hours_to_resolution: hours until market resolves (for time discount)
        min_ev_bps: minimum EV in bps to pass gate
        min_profit_usd: minimum absolute expected profit to pass gate
        time_discount_rate: annualized discount rate for time value (0 = off)
    """
    result = EVResult()

    if entry_price <= 0.001 or entry_price >= 0.999:
        result.reason = "extreme_entry_price"
        return result

    if fair_price <= 0.001 or fair_price >= 0.999:
        result.reason = "extreme_fair_price"
        return result

    raw_ev_per_share = fair_price - entry_price
    result.raw_ev = raw_ev_per_share

    slip_cost = entry_price * slippage_bps / 10000.0
    fee_cost = entry_price * fee_bps / 10000.0
    adjusted_ev_per_share = raw_ev_per_share - slip_cost - fee_cost
    result.slippage_adjusted_ev = adjusted_ev_per_share

    # Time discount: penalize trades that tie up capital for a long time
    if hours_to_resolution is not None and hours_to_resolution > 0 and time_discount_rate > 0:
        years = hours_to_resolution / 8760.0
        discount = 1.0 / (1.0 + time_discount_rate * years)
        result.time_discount = discount
        adjusted_ev_per_share *= discount

    # clamp matches gate threshold at line 58 (>0.001) so a valid market doesn't get inflated share count
    shares = size_usd / max(entry_price, 0.001)
    result.absolute_expected_profit_usd = adjusted_ev_per_share * shares

    # Gate checks
    # clamp matches gate threshold at line 58 (>0.001) so a valid market doesn't get inflated share count
    ev_bps = (adjusted_ev_per_share / max(entry_price, 0.001)) * 10000.0

    if min_ev_bps > 0 and ev_bps < min_ev_bps:
        result.reason = f"ev_{ev_bps:.1f}bps_lt_{min_ev_bps:.0f}"
        result.passes = False
        return result

    if min_profit_usd > 0 and result.absolute_expected_profit_usd < min_profit_usd:
        result.reason = (
            f"profit_{result.absolute_expected_profit_usd:.2f}_lt_{min_profit_usd:.2f}"
        )
        result.passes = False
        return result

    if adjusted_ev_per_share <= 0:
        result.reason = f"negative_ev_{adjusted_ev_per_share:.4f}"
        result.passes = False
        return result

    result.passes = True
    result.reason = f"ev_ok_{ev_bps:.1f}bps_profit_{result.absolute_expected_profit_usd:.2f}"
    return result


def resolution_time_gate(
    hours_to_resolution: Optional[float],
    *,
    min_hours: float = 2.0,
    max_hours: float = 0.0,
    discount_rate: float = 0.0,
) -> tuple[bool, float, str]:
    """
    Gate based on time-to-resolution.
    Returns (passes, time_discount_factor, reason).
    """
    if hours_to_resolution is None:
        return True, 1.0, "unknown_resolution_time"

    if hours_to_resolution < min_hours:
        return False, 0.0, f"resolves_in_{hours_to_resolution:.1f}h_lt_{min_hours:.0f}h"

    if max_hours > 0 and hours_to_resolution > max_hours:
        return False, 0.0, f"resolves_in_{hours_to_resolution:.0f}h_gt_{max_hours:.0f}h"

    if discount_rate > 0 and hours_to_resolution > 0:
        years = hours_to_resolution / 8760.0
        factor = 1.0 / (1.0 + discount_rate * years)
    else:
        factor = 1.0

    return True, factor, "ok"
