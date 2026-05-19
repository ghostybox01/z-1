"""CLOB execution: strict GTD limits with robust polling and cancel."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Mapping, Optional, Tuple

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL

from bot.clob_utils import is_filled_status, is_open_status, is_terminal_status, normalize_order_payload

log = logging.getLogger("polymarket.execution")


# Process-local in-flight idempotency keys. Each submit attempt adds its key
# before posting and removes it after the response (success OR failure). A
# duplicate call within the same time bucket will see the key still present
# and short-circuit before re-posting — this is our client-side defense
# against network-timeout-induced double-submits.
_INFLIGHT_KEYS: set[str] = set()


def _intent_idempotency_key(
    intent: Mapping[str, Any] | Any,
    time_bucket: int = 60,
) -> str:
    """
    Deterministic idempotency key derived from
        (intent_id, token_id, side, size_usd, price, time_bucket_NNs)

    `intent` may be a mapping (dict-like) or any object exposing those
    attributes. Missing fields fall back to empty strings so a same-intent
    re-call still hashes identically. The time bucket coarsens wall-clock
    time so a retry inside the bucket window collides; outside it, a fresh
    key is produced.
    """
    def _get(name: str) -> Any:
        if isinstance(intent, Mapping):
            return intent.get(name)
        return getattr(intent, name, None)

    bucket = max(1, int(time_bucket))
    bucket_idx = int(time.time()) // bucket

    parts = [
        str(_get("intent_id") or _get("id") or ""),
        str(_get("token_id") or ""),
        str(_get("side") or "").upper(),
        f"{float(_get('size_usd') or 0.0):.6f}",
        f"{float(_get('price') or 0.0):.6f}",
        str(bucket_idx),
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _simulate_paper_fill(
    *,
    token_id: str,
    side: str,
    price: float,
    size: float,
    paper_realism_enabled: bool = True,
    slippage_model_bps: float = 50.0,
    latency_ms: float = 500.0,
) -> str:
    """
    Phase 2: realistic paper fill simulation instead of always returning "dry_run".
    Returns a status note similar to live execution.
    When paper_realism_enabled is False, always returns plain "dry_run".
    """
    if not paper_realism_enabled:
        return "dry_run"
    try:
        from bot.paper_realism import simulate_paper_fill
        result = simulate_paper_fill(
            limit_price=price,
            observed_price=price * 0.99,
            size_usd=price * size,
            slippage_model_bps=slippage_model_bps,
            latency_ms=latency_ms,
        )
        if result.filled:
            return f"dry_run:paper_filled@{result.fill_price:.4f}_slip={result.slippage_bps:.0f}bps"
        return f"dry_run:paper_miss:{result.reason}"
    except Exception:
        return "dry_run"


def _extract_post_order_id(resp: Any) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for k in ("orderID", "order_id", "orderId", "id"):
        v = resp.get(k)
        if v:
            return str(v)
    o = resp.get("order")
    if isinstance(o, dict):
        for k in ("orderID", "order_id", "id"):
            v = o.get(k)
            if v:
                return str(v)
    return None


def _poll_order_state(client: Any, oid: str) -> tuple[str, str]:
    """
    Returns (kind, detail) where kind is:
      filled | terminal | open | poll_error
    """
    try:
        info = client.get_order(oid)
    except Exception as e:
        log.warning("get_order %s: %s", oid, e)
        return "poll_error", str(e)

    norm = normalize_order_payload(info)
    st = norm["status"]
    sm, osz = norm["size_matched"], norm["original_size"]

    if is_filled_status(st, sm, osz):
        return "filled", st or "FILLED"
    if is_terminal_status(st) and not is_filled_status(st, sm, osz):
        return "terminal", st or "DONE"
    if st and not is_open_status(st) and not is_filled_status(st, sm, osz):
        return "terminal", st or "DONE"
    return "open", st or "LIVE"


async def place_limit_gtd_then_wait(
    client: Any,
    *,
    token_id: str,
    side: str,
    price: float,
    size: float,
    ttl_seconds: int,
    poll_seconds: float,
    dry_run: bool,
    paper_realism_enabled: bool = True,
    paper_slippage_model_bps: float = 50.0,
    follower_latency_ms: float = 500.0,
    intent: Any = None,
    idempotency_time_bucket: int = 60,
) -> Tuple[Optional[str], str]:
    """
    Post GTD limit; poll until filled / terminal / TTL; cancel if still open.
    Returns (order_id_or_none, note).

    When `intent` is supplied, a deterministic idempotency key is computed and
    a process-local in-flight set blocks duplicate submits within the time
    bucket. py_clob_client has no server-side idempotency hook, so this is
    purely client-side dedupe.
    """
    idem_key: Optional[str] = None
    if intent is not None:
        # Build the key from the intent + the call-site overrides so the
        # actual posted parameters are reflected in the hash.
        intent_for_key = dict(intent) if isinstance(intent, Mapping) else {
            "intent_id": getattr(intent, "intent_id", None) or getattr(intent, "id", None),
        }
        intent_for_key.setdefault("token_id", token_id)
        intent_for_key.setdefault("side", side)
        intent_for_key.setdefault("price", price)
        intent_for_key.setdefault("size_usd", float(price) * float(size))
        idem_key = _intent_idempotency_key(intent_for_key, time_bucket=idempotency_time_bucket)
        if idem_key in _INFLIGHT_KEYS:
            log.warning(
                "submit blocked: idempotency_inflight key=%s token=%s side=%s",
                idem_key,
                token_id[:12],
                side,
            )
            return None, "idempotency_inflight"

    if dry_run:
        paper_result = _simulate_paper_fill(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            paper_realism_enabled=paper_realism_enabled,
            slippage_model_bps=paper_slippage_model_bps,
            latency_ms=follower_latency_ms,
        )
        log.info(
            "[DRY RUN] limit %s size=%.4f @ %.4f token=%s… paper=%s",
            side,
            size,
            price,
            token_id[:12],
            paper_result,
        )
        return f"dry_{int(time.time())}", paper_result

    fee_bps = 0
    try:
        fee_bps = int(client.get_fee_rate_bps(token_id))
    except Exception:
        pass

    exp = int(time.time()) + max(15, int(ttl_seconds))
    order_side = BUY if side.upper() == "BUY" else SELL

    args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=order_side,
        fee_rate_bps=fee_bps,
        expiration=exp,
    )

    if idem_key is not None:
        _INFLIGHT_KEYS.add(idem_key)
        log.info(
            "submit key=%s token=%s side=%s price=%.4f size=%.4f",
            idem_key,
            token_id[:12],
            side,
            float(price),
            float(size),
        )
    try:
        try:
            signed = client.create_order(args)
        except PolyApiException as e:
            log.warning("create_order PolyApiException: %s", e)
            return None, f"create_failed:poly_api:{e.status_code}:{e.error_msg}"
        except Exception as e:
            log.exception("create_order failed")
            return None, f"create_failed:{e}"

        try:
            resp = client.post_order(signed, OrderType.GTD)
        except PolyApiException as e:
            log.warning("post_order PolyApiException: %s", e)
            return None, f"post_failed:poly_api:{e.status_code}:{e.error_msg}"
        except Exception as e:
            log.exception("post_order failed")
            return None, f"post_failed:{e}"

        oid = _extract_post_order_id(resp)
        if not oid:
            log.error("post_order missing id: %s", resp)
            return None, "post_failed:no_order_id"
    finally:
        if idem_key is not None:
            _INFLIGHT_KEYS.discard(idem_key)

    kind0, detail0 = await asyncio.to_thread(_poll_order_state, client, oid)
    if kind0 == "filled":
        return oid, f"filled:{detail0}"
    if kind0 == "terminal":
        return oid, f"closed:{detail0}"

    deadline = time.monotonic() + float(ttl_seconds)
    poll_s = max(0.25, float(poll_seconds))

    while time.monotonic() < deadline:
        kind, detail = await asyncio.to_thread(_poll_order_state, client, oid)
        if kind == "filled":
            return oid, f"filled:{detail}"
        if kind == "terminal":
            return oid, f"closed:{detail}"
        if kind == "poll_error":
            await asyncio.sleep(poll_s)
            continue
        await asyncio.sleep(poll_s)

    try:
        await asyncio.to_thread(client.cancel, oid)
        log.info("Cancelled order %s after TTL", oid)
    except Exception as e:
        log.warning("cancel failed %s: %s", oid, e)
        # verify whether it filled or expired while we tried
        kind, detail = await asyncio.to_thread(_poll_order_state, client, oid)
        if kind == "filled":
            return oid, f"filled:{detail}"
        return oid, f"cancel_error:{e}"

    kind, detail = await asyncio.to_thread(_poll_order_state, client, oid)
    if kind == "filled":
        return oid, f"filled:{detail}"
    if kind == "terminal":
        return oid, f"cancelled_or_terminal:{detail}"
    return oid, "cancelled_ttl"


async def place_market_fok_fallback(
    client: Any,
    *,
    token_id: str,
    side: str,
    amount_usd: float,
    dry_run: bool,
    intent: Any = None,
    idempotency_time_bucket: int = 60,
) -> Tuple[Optional[str], str]:
    idem_key: Optional[str] = None
    if intent is not None:
        intent_for_key = dict(intent) if isinstance(intent, Mapping) else {
            "intent_id": getattr(intent, "intent_id", None) or getattr(intent, "id", None),
        }
        intent_for_key.setdefault("token_id", token_id)
        intent_for_key.setdefault("side", side)
        intent_for_key.setdefault("price", 0.0)
        intent_for_key.setdefault("size_usd", float(amount_usd))
        idem_key = _intent_idempotency_key(intent_for_key, time_bucket=idempotency_time_bucket)
        if idem_key in _INFLIGHT_KEYS:
            log.warning(
                "market submit blocked: idempotency_inflight key=%s token=%s side=%s",
                idem_key,
                token_id[:12],
                side,
            )
            return None, "idempotency_inflight"

    if dry_run:
        return f"dry_mkt_{int(time.time())}", "dry_run"

    from py_clob_client.clob_types import MarketOrderArgs

    order_side = BUY if side.upper() == "BUY" else SELL
    mo = MarketOrderArgs(token_id=token_id, amount=amount_usd, side=order_side)

    if idem_key is not None:
        _INFLIGHT_KEYS.add(idem_key)
        log.info(
            "submit key=%s token=%s side=%s amount_usd=%.4f kind=market_fok",
            idem_key,
            token_id[:12],
            side,
            float(amount_usd),
        )
    try:
        try:
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, OrderType.FOK)
        except PolyApiException as e:
            return None, f"market_fok_failed:poly_api:{e.status_code}:{e.error_msg}"
        except Exception as e:
            return None, f"market_fok_failed:{e}"
        oid = _extract_post_order_id(resp)
        return oid, "market_fok"
    finally:
        if idem_key is not None:
            _INFLIGHT_KEYS.discard(idem_key)
