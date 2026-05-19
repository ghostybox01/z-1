"""CLOB reconciliation: open orders snapshot + refresh recent trade rows from get_order."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from bot.clob_utils import is_filled_status, is_open_status, is_terminal_status, normalize_order_payload

log = logging.getLogger("polymarket.reconcile")


def normalize_open_order(raw: Any) -> dict[str, Any]:
    """Flatten get_orders row for dashboard / storage."""
    if not isinstance(raw, dict):
        return {
            "order_id": None,
            "token_id": None,
            "side": "",
            "price": None,
            "original_size": None,
            "size_matched": None,
            "status": "",
        }
    oid = raw.get("id") or raw.get("orderID") or raw.get("order_id")
    tok = raw.get("asset_id") or raw.get("token_id") or raw.get("tokenId")

    def _f(x: Any) -> Optional[float]:
        if x is None:
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    st = str(raw.get("status") or raw.get("state") or "").upper()
    return {
        "order_id": str(oid) if oid else None,
        "token_id": str(tok) if tok else None,
        "side": str(raw.get("side") or "").upper(),
        "price": _f(raw.get("price")),
        "original_size": _f(raw.get("original_size") or raw.get("size")),
        "size_matched": _f(raw.get("size_matched") or raw.get("filled_size") or raw.get("filledSize")),
        "status": st,
    }


def canonical_status_from_order_payload(raw: Any) -> str:
    """Map normalized CLOB order to a stable TradeRecord.status vocabulary."""
    norm = normalize_order_payload(raw)
    st = norm["status"]
    sm, osz = norm["size_matched"], norm["original_size"]
    if is_filled_status(st, sm, osz):
        return "filled"
    if is_open_status(st):
        return "open"
    if is_terminal_status(st):
        if st in ("CANCELED", "CANCELLED", "EXPIRED"):
            # A cancel that observed a non-zero partial fill is itself a
            # fill event (the matched portion settled); the remainder was
            # cancelled. We surface it as "filled" so downstream P&L sees
            # the fill, and reconcile_trade_records_inplace records the
            # partial-cancel detail in the reconcile_note.
            if sm is not None and sm > 0:
                return "filled"
            return "cancelled"
        return "closed"
    return "unknown"


# Statuses considered TERMINAL by reconcile: once recorded, they may only be
# overwritten by a strictly more authoritative terminal observation (and
# never by a stale non-terminal snapshot).
_TERMINAL_STATUSES = frozenset({"filled", "cancelled", "closed"})

# Among terminal statuses, "filled" is absorbing: no later snapshot can
# overwrite it. "cancelled" / "closed" can be upgraded to "filled" if a
# later snapshot reveals a fill (e.g. partial-fill discovered post-cancel),
# but cannot be downgraded to each other or to non-terminal statuses.


def _rank_status(s: str) -> int:
    """Higher = more terminal / informative for conflict resolution."""
    return {
        "unknown": 0,
        "open": 1,
        "submitted": 2,
        "market_fok": 3,
        "closed": 4,
        "cancelled": 5,
        "filled": 6,
        "dry_run": 7,
    }.get(s, 0)


def merge_trade_status(previous: str, api_status: str) -> Optional[str]:
    """
    If API gives a strictly better-resolved status, return the new one.

    Rules (terminal-absorbing semantics):
      * "filled" is absolutely absorbing — no later observation can overwrite
        it. A subsequent CANCELLED with size_matched == 0 is treated as a
        stale-snapshot downgrade and rejected.
      * "cancelled" / "closed" are terminal: they may only be replaced by a
        "filled" observation (e.g. when a later poll reveals the matched
        portion of a cancelled order). They cannot fall back to "open" /
        "submitted" / "unknown".
      * Non-terminal previous statuses can transition to anything more
        informative.
      * "dry_run" is opaque to reconcile.

    Rejected transitions are logged at DEBUG by callers.
    """
    if api_status == "unknown":
        return None
    if previous == "dry_run":
        return None

    # filled is absorbing — no downgrade, ever.
    if previous == "filled":
        if api_status == "filled":
            return None  # already filled, no change
        log.debug(
            "reconcile: rejected %s -> %s (filled is absorbing)",
            previous,
            api_status,
        )
        return None

    # Other terminal statuses: only upgrade to filled is allowed.
    if previous in _TERMINAL_STATUSES:
        if api_status == "filled":
            return "filled"
        if api_status == previous:
            return None
        log.debug(
            "reconcile: rejected %s -> %s (terminal status only upgrades to filled)",
            previous,
            api_status,
        )
        return None

    # Previous is non-terminal. Standard rank-based upgrade.
    if _rank_status(api_status) > _rank_status(previous):
        return api_status
    if api_status == "filled" and previous != "filled":
        return "filled"
    if api_status == "cancelled" and previous in ("open", "submitted", "unknown"):
        return "cancelled"
    if api_status == "open" and previous == "submitted":
        return "open"
    return None


def snapshot_open_orders(clob: Any, *, display_limit: int = 40) -> list[dict[str, Any]]:
    """Blocking: fetch all open orders via client, return newest-first slice for UI."""
    rows = clob.get_orders()
    if not isinstance(rows, list):
        return []
    norm = [normalize_open_order(r) for r in rows]
    norm = [x for x in norm if x.get("order_id")]
    norm.sort(key=lambda x: str(x.get("order_id") or ""), reverse=True)
    return norm[:display_limit]


def reconcile_trade_records_inplace(
    clob: Any,
    records: list[Any],
    *,
    depth: int = 15,
    sleep_between_s: float = 0.06,
) -> int:
    """
    Blocking: poll get_order for last `depth` records; mutates .status and .reconcile_note.
    Skips dry-run ids. Returns count of rows updated.

    Terminal-absorbing reconcile:
      * Once a record reaches "filled" it is never downgraded.
      * "cancelled"/"closed" records may only transition to "filled".
      * Partial-fill cancels (CANCELLED with size_matched > 0) are recorded
        as filled with both the matched size and cancelled remainder noted
        in reconcile_note, so neither event is lost.
      * Rejected transitions are emitted at DEBUG for visibility.
    """
    if depth <= 0:
        return 0
    slice_ = records[-depth:] if len(records) > depth else records
    updated = 0
    for rec in slice_:
        oid = getattr(rec, "order_id", "") or ""
        if not oid or oid == "none" or oid.startswith("dry_"):
            continue
        st0 = getattr(rec, "status", "")
        if st0 == "dry_run":
            continue
        try:
            raw = clob.get_order(oid)
        except Exception as e:
            log.debug("get_order %s: %s", oid[:16], e)
            time.sleep(sleep_between_s)
            continue

        norm = normalize_order_payload(raw)
        raw_status = norm["status"]
        sm = norm["size_matched"]
        osz = norm["original_size"]
        api_st = canonical_status_from_order_payload(raw)
        merged = merge_trade_status(st0, api_st)

        # Detect partial-fill + cancel: the canonical status was promoted to
        # "filled" because size_matched > 0, but the underlying CLOB status
        # was a cancel. We must record both the fill and the cancel of the
        # remainder so neither event is dropped.
        is_partial_cancel = (
            raw_status in ("CANCELED", "CANCELLED", "EXPIRED")
            and sm is not None and sm > 0
            and (osz is None or sm < osz * 0.999)
        )

        if merged and merged != st0:
            rec.status = merged
            if is_partial_cancel:
                remainder = (osz - sm) if (osz is not None and sm is not None) else None
                rec.reconcile_note = (
                    f"clob:partial_fill_cancelled:size_matched={sm}"
                    + (f";cancelled_remainder={remainder}" if remainder is not None else "")
                )
            else:
                rec.reconcile_note = f"clob:{api_st}"
            updated += 1
        else:
            # No status change. Either a no-op or a rejected downgrade —
            # the merge function already logged a debug for downgrades.
            # For partial-cancel where status was already "filled" we still
            # want to enrich the note with the cancel-of-remainder fact.
            if is_partial_cancel and st0 == "filled" and not getattr(rec, "reconcile_note", None):
                remainder = (osz - sm) if (osz is not None and sm is not None) else None
                rec.reconcile_note = (
                    f"clob:partial_fill_cancelled:size_matched={sm}"
                    + (f";cancelled_remainder={remainder}" if remainder is not None else "")
                )
            if api_st != st0 and merged is None:
                # Explicit per-order log of the rejected transition for
                # debugging stale-snapshot incidents.
                log.debug(
                    "reconcile: rejected %s -> %s for order %s",
                    st0,
                    api_st,
                    oid[:16],
                )

        time.sleep(sleep_between_s)
    return updated
