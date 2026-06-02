"""Registered strategy agents — single source of truth for UI and docs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentInfo:
    id: str
    title: str
    short: str
    priority: int


AGENTS: tuple[AgentInfo, ...] = (
    AgentInfo(
        id="value_edge",
        title="Value edge",
        short="Scans Gamma tradeables; compares CLOB mid vs bands; proposes BUY on Yes/No value setups.",
        priority=50,
    ),
    AgentInfo(
        id="copy_signal",
        title="Copy signal",
        short="Watches COPY_WATCH_WALLETS activity on Data API; proposes mirroring new BUYs (cold-start dedupes history).",
        priority=100,
    ),
    AgentInfo(
        id="latency_arb",
        title="Latency arb",
        short="Gamma outcomePrices vs CLOB mid; BUY when the slower quote is richer than the book (cross-feed lag).",
        priority=65,
    ),
    AgentInfo(
        id="bundle_arb",
        title="Bundle arb",
        short="When best YES ask + best NO ask is below 1 (net of buffer), BUY both legs as one execution unit.",
        priority=72,
    ),
    AgentInfo(
        id="zscore_edge",
        title="Z-score edge",
        short="Rolling z-score on YES mid per market; mean-reversion BUY on stretched YES or NO.",
        priority=48,
    ),
    AgentInfo(
        id="weather_arb",
        title="Weather arb",
        short="Compares Open-Meteo GFS forecast to Polymarket temperature markets; BUY YES/NO when forecast edge > threshold.",
        priority=90,
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
        "value_edge": bool(getattr(settings, "agent_value", True)),
        "copy_signal": bool(getattr(settings, "agent_copy", False)),
        "latency_arb": bool(getattr(settings, "agent_latency", False)),
        "bundle_arb": bool(getattr(settings, "agent_bundle", False)),
        "zscore_edge": bool(getattr(settings, "agent_zscore", False)),
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
                "enabled": enabled.get(a.id, False),
                "scheduled": info.get("scheduled", False),
                "ran": info.get("ran", False),
                "intents": info.get("intents", 0),
                "note": note,
            }
        )
    return out
