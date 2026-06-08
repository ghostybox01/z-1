"""Registered strategy agents — single source of truth for UI and docs.

Strategy space is fully mapped (every approach measured, not guessed). Only
INFORMATION edges survive on an efficient market: copy (proven) + weather
(testing). Price-pattern and speed/arbitrage agents are RETIRED — beaten by the
market, not broken. ``status`` drives how the UI presents each agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentInfo:
    id: str
    title: str
    short: str
    priority: int
    status: str = "retired"  # "live" | "testing" | "retired"


# Ordered: the two agents we actually run/focus on first, retired ones last.
AGENTS: tuple[AgentInfo, ...] = (
    AgentInfo(
        id="copy_signal",
        title="Copy signal",
        short="Mirrors vetted whale BUYs on judgment markets — our proven edge. Judgment-only, per-wallet scored, event-deduped, EV-gated.",
        priority=100,
        status="live",
    ),
    AgentInfo(
        id="weather_arb",
        title="Weather arb",
        short="Open-Meteo forecast vs temperature markets. PAUSED for live capital — a threshold/CDF paper-test is deciding whether the edge is real.",
        priority=90,
        status="testing",
    ),
)


def agents_status(
    settings: Any,
    *,
    cycle_runtime: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """For dashboard / terminal: which agents exist and their live state.

    ``cycle_runtime`` is an optional dict keyed by agent id with per-cycle
    runtime info produced by the orchestrator:
        {
          "scheduled": bool,   — agent was scheduled to run this cycle
          "ran": bool,         — propose() completed (False on exception)
          "intents": int,      — number of intents produced
          "note": str,         — brief human-readable diagnostic
        }
    When omitted the output is backwards-compatible (config-only view).
    """
    enabled = {
        "copy_signal": bool(getattr(settings, "agent_copy", False)),
        "weather_arb": bool(getattr(settings, "agent_weather", False)),
    }
    config_notes: dict[str, str] = {}
    if getattr(settings, "agent_copy", False) and not getattr(settings, "copy_watch_wallets", []):
        config_notes["copy_signal"] = (
            "Copy agent needs agent_copy=true AND copy_watch_wallets populated. "
            "First poll only seeds history (no replay burst)."
        )
    rt = cycle_runtime or {}
    out = []
    for a in AGENTS:
        info = rt.get(a.id, {})
        note = info.get("note", "") or config_notes.get(a.id, "")
        out.append(
            {
                "id": a.id,
                "title": a.title,
                "description": a.short,
                "priority": a.priority,
                "status": a.status,
                "enabled": enabled.get(a.id, False),
                "scheduled": info.get("scheduled", False),
                "ran": info.get("ran", False),
                "intents": info.get("intents", 0),
                "note": note,
            }
        )
    return out
