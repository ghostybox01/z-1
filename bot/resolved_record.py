"""Persistent resolved-record tracker: true win-rate + realized P&L per strategy.

Resolution is *value-aware*.  A market is REALIZED (won/lost) only when the CLOB
endpoint reports ``closed == True``; those outcomes are cached permanently.  For
markets that are still officially open we ALSO read the current token price, so a
position whose price has already collapsed (<=5c) or spiked (>=95c) is reported
as *leaning* lost/won instead of a misleadingly neutral "pending".  Leaning
outcomes are NOT cached (price can still move) and feed an *unrealized* P&L
bucket kept separate from realized P&L.

This closes the gap that made the dashboard hide losses-in-progress: a weather
bet sitting at 0.1c was shown as hopeful "pending" until Polymarket flipped the
official ``closed`` flag hours later.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("polymarket.resolved_record")

_CLOB_MARKET_URL = "https://clob.polymarket.com/markets/{condition_id}"

# A still-open position is treated as "decided" once the market is this confident.
LEAN_LOSS_PX = 0.05  # token price <= 5c  -> all-but-certain loss
LEAN_WIN_PX = 0.95   # token price >= 95c -> all-but-certain win


async def resolve_token(http: Any, condition_id: str, token_id: str) -> Optional[str]:
    """Query CLOB and classify a token's current status.

    Returns one of:
      * ``"won"`` / ``"lost"`` — market is ``closed`` (REALIZED, safe to cache).
      * ``"winning"`` / ``"losing"`` — market still open but its price has
        already collapsed (<=``LEAN_LOSS_PX``) or spiked (>=``LEAN_WIN_PX``);
        all-but-certain but UNREALIZED, so callers must NOT cache it.
      * ``"open"`` — genuinely in play (price between the lean thresholds, or
        the token's price could not be read).
      * ``None`` — network/parse error; callers should treat as transient.
    """
    try:
        url = _CLOB_MARKET_URL.format(condition_id=condition_id)
        resp = await http.get(url)
        if resp.status_code != 200:
            log.debug("resolve_token: HTTP %s for %s", resp.status_code, condition_id)
            return None
        m = resp.json()

        # Locate our token within the market (works for open and closed markets).
        tok_price: Optional[float] = None
        tok_winner: Optional[bool] = None
        found = False
        for tok in m.get("tokens", []):
            if str(tok.get("token_id")) == str(token_id):
                found = True
                p = tok.get("price")
                tok_price = float(p) if p is not None else None
                tok_winner = tok.get("winner")
                break

        if m.get("closed"):
            # REALIZED outcome.
            if tok_price is not None:
                return "won" if tok_price >= 0.5 else "lost"
            if tok_winner is not None:
                return "won" if tok_winner else "lost"
            return None  # closed but no price/winner — treat as transient error

        # OPEN market — value-aware lean based on the live price.
        if found and tok_price is not None:
            if tok_price <= LEAN_LOSS_PX:
                return "losing"
            if tok_price >= LEAN_WIN_PX:
                return "winning"
            return "open"
        return "open"  # price unreadable → can't decide, leave genuinely open
    except Exception as exc:
        log.debug("resolve_token error %s: %s", condition_id, exc)
        return None


async def compute_resolved_record(
    http: Any,
    trades: list[dict],
    cache: dict[str, str],
    held_tokens: Optional[set] = None,
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
    held_tokens:
        Optional set of token-ids we CURRENTLY hold (from ``/positions`` with
        size>0).  When provided, an open-market trade whose token is not held
        was exited (sold or redeemed) and is counted under ``exited`` instead of
        a misleading "leaning" position — keeping the record 1:1 with the
        wallet.  Closed/realized outcomes (won/lost) always count regardless.

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

    # Fetch outcomes for all unresolved tokens.  REALIZED outcomes (won/lost)
    # are cached permanently; leaning/open/None results are kept only for this
    # run so they re-evaluate next cycle as the price moves.
    live_status: dict[str, Optional[str]] = {}
    for tid, cid in unresolved.items():
        outcome = await resolve_token(http, cid, tid)
        if outcome in ("won", "lost"):
            cache[tid] = outcome
        else:
            live_status[tid] = outcome  # "winning"/"losing"/"open"/None

    # Aggregate stats.
    def _empty_stats() -> dict:
        return {
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "exited": 0,
            "leaning_wins": 0,
            "leaning_losses": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "win_rate": None,
        }

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
        sd = by_strategy[strat_key]

        # Realized outcomes live in the permanent cache; leaning/open are this run.
        outcome = cache.get(tid)
        if outcome is None:
            outcome = live_status.get(tid)

        if outcome == "won":
            # REALIZED win (market closed) — counts even if already redeemed.
            shares = (cost / price) if price > 0 else 0.0
            pnl = shares * 1.0 - cost
            for d in (overall, sd):
                d["wins"] += 1
                d["realized_pnl"] += pnl
            continue
        if outcome == "lost":
            # REALIZED loss (market closed) — counts even if token discarded.
            for d in (overall, sd):
                d["losses"] += 1
                d["realized_pnl"] += -cost
            continue

        # Open-market token (winning/losing/open/None).  If holdings are known
        # and we no longer hold this token, the position was exited (sold or
        # redeemed) — it is NOT a current lean, so the wallet wouldn't show it.
        if held_tokens is not None and tid and tid not in held_tokens:
            for d in (overall, sd):
                d["exited"] += 1
            continue

        if outcome == "winning":
            shares = (cost / price) if price > 0 else 0.0
            upnl = shares * 1.0 - cost
            for d in (overall, sd):
                d["leaning_wins"] += 1
                d["unrealized_pnl"] += upnl
        elif outcome == "losing":
            for d in (overall, sd):
                d["leaning_losses"] += 1
                d["unrealized_pnl"] += -cost
        else:  # "open" or None — genuinely undecided
            for d in (overall, sd):
                d["pending"] += 1

    # Compute win rates.
    def _set_win_rate(d: dict) -> None:
        resolved = d["wins"] + d["losses"]
        d["win_rate"] = (d["wins"] / resolved) if resolved > 0 else None
        d["realized_pnl"] = round(d["realized_pnl"], 4)
        d["unrealized_pnl"] = round(d["unrealized_pnl"], 4)

    _set_win_rate(overall)
    for v in by_strategy.values():
        _set_win_rate(v)

    return {"overall": overall, "by_strategy": by_strategy}
