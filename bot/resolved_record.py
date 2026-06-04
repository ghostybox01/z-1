"""Persistent resolved-record tracker: true win-rate + realized P&L per strategy.

Resolution logic mirrors the CLOB market endpoint response — a market is
"resolved" when `closed == True`.  The final token price (or `winner` flag)
determines win/loss.  Open markets are retried next cycle; resolved outcomes
are cached permanently because they never change.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("polymarket.resolved_record")

_CLOB_MARKET_URL = "https://clob.polymarket.com/markets/{condition_id}"


async def resolve_token(http: Any, condition_id: str, token_id: str) -> Optional[str]:
    """Query CLOB and return "won", "lost", "open", or None (error / unknown).

    "open" is returned for markets that are not yet closed.  None is returned
    on any network or parse error — callers should treat None as transient and
    retry later.
    """
    try:
        url = _CLOB_MARKET_URL.format(condition_id=condition_id)
        resp = await http.get(url)
        if resp.status_code != 200:
            log.debug("resolve_token: HTTP %s for %s", resp.status_code, condition_id)
            return None
        m = resp.json()
        if not m.get("closed"):
            return "open"
        for tok in m.get("tokens", []):
            if str(tok.get("token_id")) == str(token_id):
                p = tok.get("price")
                if p is not None:
                    return "won" if float(p) >= 0.5 else "lost"
                winner = tok.get("winner")
                if winner is not None:
                    return "won" if winner else "lost"
        # Token found in closed market but no price/winner — treat as error
        return None
    except Exception as exc:
        log.debug("resolve_token error %s: %s", condition_id, exc)
        return None


async def compute_resolved_record(
    http: Any,
    trades: list[dict],
    cache: dict[str, str],
) -> dict:
    """Compute win-rate and realized P&L from a list of trade dicts.

    Parameters
    ----------
    http:
        An ``httpx.AsyncClient`` (or compatible) instance.
    trades:
        List of dicts with keys: ``condition_id``, ``token_id``,
        ``cost_usd``, ``price``, ``strategy``.
    cache:
        Persistent dict ``{token_id: "won"|"lost"}``.  Updated in-place for
        newly resolved tokens; "open"/None results are NOT stored so they get
        retried next time.

    Returns
    -------
    dict with shape::

        {
          "overall": {
              "wins": int, "losses": int, "pending": int,
              "realized_pnl": float, "win_rate": float | None
          },
          "by_strategy": {
              "<strategy>": { same fields }
          }
        }
    """
    # Deduplicate: resolve each distinct (condition_id, token_id) pair once.
    unresolved: dict[str, str] = {}  # token_id -> condition_id
    for t in trades:
        tid = str(t.get("token_id") or "")
        cid = str(t.get("condition_id") or "")
        if tid and cid and tid not in cache:
            unresolved[tid] = cid

    # Fetch outcomes for all unresolved tokens.
    for tid, cid in unresolved.items():
        outcome = await resolve_token(http, cid, tid)
        if outcome in ("won", "lost"):
            cache[tid] = outcome
        # "open" and None are intentionally not cached — retry next cycle.

    # Aggregate stats.
    def _empty_stats() -> dict:
        return {"wins": 0, "losses": 0, "pending": 0, "realized_pnl": 0.0, "win_rate": None}

    overall = _empty_stats()
    by_strategy: dict[str, dict] = {}

    for t in trades:
        tid = str(t.get("token_id") or "")
        cost = float(t.get("cost_usd") or 0.0)
        price = float(t.get("price") or 0.0)
        strat = str(t.get("strategy") or "unknown")
        # Normalise strategy: strip the ":note" suffix stored by the orchestrator.
        strat_key = strat.split(":")[0] if ":" in strat else strat

        if strat_key not in by_strategy:
            by_strategy[strat_key] = _empty_stats()

        outcome = cache.get(tid)
        if outcome == "won":
            shares = (cost / price) if price > 0 else 0.0
            pnl = shares * 1.0 - cost
            overall["wins"] += 1
            overall["realized_pnl"] += pnl
            by_strategy[strat_key]["wins"] += 1
            by_strategy[strat_key]["realized_pnl"] += pnl
        elif outcome == "lost":
            pnl = -cost
            overall["losses"] += 1
            overall["realized_pnl"] += pnl
            by_strategy[strat_key]["losses"] += 1
            by_strategy[strat_key]["realized_pnl"] += pnl
        else:
            overall["pending"] += 1
            by_strategy[strat_key]["pending"] += 1

    # Compute win rates.
    def _set_win_rate(d: dict) -> None:
        resolved = d["wins"] + d["losses"]
        d["win_rate"] = (d["wins"] / resolved) if resolved > 0 else None
        d["realized_pnl"] = round(d["realized_pnl"], 4)

    _set_win_rate(overall)
    for v in by_strategy.values():
        _set_win_rate(v)

    return {"overall": overall, "by_strategy": by_strategy}
