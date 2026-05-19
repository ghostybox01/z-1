"""PnL- and win-rate-aware sizing from recent DB trade outcomes (common bot pattern)."""

from __future__ import annotations

from bot.db.kv import recent_trade_statuses


def pnl_aware_size_multiplier(*, window: int = 28) -> float:
    """
    Uses crude execution quality: filled vs cancelled in recent window.
    Maps to multiplier in [0.78, 1.12] — conservative when cancels dominate.
    """
    rows = recent_trade_statuses(limit=max(8, window))
    if len(rows) < 6:
        # warmup: shrink size until we have a P&L signal
        return 0.5
    filled = sum(1 for x in rows if str(x).lower() == "filled")
    cancelled = sum(1 for x in rows if str(x).lower() == "cancelled")
    total = filled + cancelled + 1e-6
    win_proxy = filled / total
    # Center at ~0.35 filled ratio for limit bots (many cancels are normal)
    adj = (win_proxy - 0.35) * 0.55
    m = 1.0 + adj
    return max(0.78, min(1.12, m))
