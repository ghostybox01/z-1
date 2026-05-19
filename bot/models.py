"""Shared datatypes for agents and orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from bot.categories import MarketCategory


@dataclass
class TradeIntent:
    """Executable intent produced by an agent (before risk checks)."""

    agent: str
    priority: int  # higher = preferred when multiple fire
    token_id: str
    condition_id: str
    question: str
    outcome: str
    side: str  # BUY / SELL
    max_price: float  # worst acceptable limit for BUY (inclusive)
    size_usd: float
    category: MarketCategory
    strategy: str
    reason: str
    # Mid / fair reference at signal time (for min-edge bps gate); optional.
    reference_price: Optional[float] = None
    # Two BUY legs share the same id; orchestrator executes both as one decision.
    bundle_id: Optional[str] = None
    # Phase 2: hours until market resolution (for EV time-discount gate).
    hours_to_resolution: Optional[float] = None


@dataclass
class TradeRecord:
    order_id: str
    market_question: str
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float
    cost_usd: float
    status: str
    timestamp: str
    outcome: str
    strategy: str
    reconcile_note: Optional[str] = None


@dataclass
class BotState:
    mode: str = "live"
    running: bool = False
    usdc_balance: float = 0.0
    portfolio_value: float = 0.0
    total_pnl: float = 0.0
    positions: list[dict] = field(default_factory=list)
    open_orders: list[dict] = field(default_factory=list)
    trade_history: list[TradeRecord] = field(default_factory=list)
    markets_scanned: int = 0
    trades_placed: int = 0
    trades_filled: int = 0
    last_scan: Optional[str] = None
    last_trade: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    started_at: Optional[str] = None
    # Diagnostics
    cex_snapshot: dict[str, Any] = field(default_factory=dict)
    last_intents: list[dict] = field(default_factory=list)
    agents_fired: list[str] = field(default_factory=list)
    last_reconcile_at: Optional[str] = None
    reconcile_updates_last: int = 0
    # Consecutive failed _execute_intent (create/post/critical path); circuit breaker reads this.
    consecutive_exec_failures: int = 0
    # Rejected/skipped intents from last cycle (for dashboard visibility).
    last_skipped_intents: list[dict] = field(default_factory=list)
    # Per-agent runtime diagnostics from last cycle (keyed by agent id).
    cycle_agent_runtime: dict[str, dict[str, Any]] = field(default_factory=dict)
    # E1: snapshot of the three risk caps actually enforced by the orchestrator,
    # populated once at TradingBot init (orchestrator uses cap>0 as enable).
    # Dashboard reads this to show what's truly active even if DB settings change.
    active_caps: dict[str, float] = field(default_factory=dict)
    # Pre-added for downstream captains (E-series). Default cleanly; populated elsewhere.
    # Scoring fallback counter (HMS Scoring).
    scoring_fallback_v1: int = 0
    # Scoring mode label, e.g. "v2" or "v1_fallback" (HMS Scoring).
    scoring_mode: str = "v2"
    # Unix timestamp of last successful balance refresh (HMS Execution).
    balance_refreshed_at: float = 0.0
    # In-cycle USDC reservation to avoid double-spend across legs (HMS Execution).
    reserved_usdc: float = 0.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
