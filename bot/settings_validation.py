"""Strict validation/normalization for admin settings patches."""

from __future__ import annotations

import json
from typing import Any

from bot.categories import MarketCategory
from bot.settings import default_kv_seed
from bot.validate import is_valid_polygon_address, is_valid_private_key_hex


def live_risk_caps_ok(settings: Any) -> tuple[bool, str]:
    """Fail-closed guard: live trading requires at least one positive exposure cap.

    When dry_run is False and all of max_condition_exposure_usd /
    max_category_exposure_usd / max_daily_notional_usd are <= 0, every exposure
    gate is disabled (cap <= 0 means "off") -> unbounded exposure. Block the cycle.
    Dry-run always passes.
    """
    if getattr(settings, "dry_run", True):
        return True, ""
    caps = (
        float(getattr(settings, "max_condition_exposure_usd", 0.0) or 0.0),
        float(getattr(settings, "max_category_exposure_usd", 0.0) or 0.0),
        float(getattr(settings, "max_daily_notional_usd", 0.0) or 0.0),
    )
    if all(c <= 0 for c in caps):
        return False, (
            "live trading blocked: set at least one of max_condition_exposure_usd / "
            "max_category_exposure_usd / max_daily_notional_usd > 0"
        )
    return True, ""

BOOL_KEYS = {
    "dry_run",
    "strict_execution",
    "allow_market_fallback",
    "trading_paused",
    "cex_require_dispersion",
    "structured_log",
    "reconcile_enabled",
    "agent_value",
    "agent_copy",
    "agent_latency",
    "agent_bundle",
    "agent_zscore",
    "cex_gate_crypto",
    "signals_enabled",
    "pnl_sizing_enabled",
    "orderbook_gate_enabled",
    "ws_allow_anonymous",
    "spread_gate_enabled",
    "resolution_gate_enabled",
    "copy_allow_unknown_outcome",
    "copy_auto_manage",
    "ENABLE_CRYPTO_SHORT",
    "ENABLE_CRYPTO_OTHER",
    "ENABLE_SPORTS",
    "ENABLE_POLITICS",
    "ENABLE_MACRO",
    "ENABLE_WEATHER",
    "ENABLE_SCIENCE_TECH",
    "ENABLE_ENTERTAINMENT",
    "ENABLE_GEOPOLITICS",
    "ENABLE_OTHER",
    "telegram_enabled",
    "telegram_on_trade",
    "telegram_on_error",
    "telegram_on_cycle_summary",
    "telegram_on_balance_change",
    "telegram_daily_report",
    # Phase 2 booleans
    "paper_realism_enabled",
    "ev_gate_enabled",
}

INT_RANGES: dict[str, tuple[int, int | None]] = {
    "polymarket_signature_type": (0, 1),
    "port": (1, 65535),
    "scan_interval_seconds": (5, None),
    "max_trades_per_cycle": (1, 50),
    "order_ttl_seconds": (5, 3600),
    "zscore_window": (5, 500),
    "zscore_min_samples": (3, 500),
    "copy_poll_seconds": (2, 3600),
    "copy_min_win_streak": (0, 100),
    "copy_min_total_trades": (0, 500),
    "copy_max_watched_wallets": (1, 200),
    "reconcile_history_depth": (1, 500),
    "open_orders_display_limit": (1, 1000),
    "pnl_sizing_window": (5, 365),
    "min_edge_bps": (0, 5000),
    "circuit_breaker_max_fails": (0, 100),
}

FLOAT_RANGES: dict[str, tuple[float, float | None]] = {
    "default_bet_usd": (0.01, None),
    "min_bet_usd": (0.01, None),
    "max_bet_usd": (0.01, None),
    "min_clob_liquidity_usd": (0.0, None),
    "min_gamma_volume": (0.0, None),
    "order_poll_seconds": (0.1, 120.0),
    "balance_buffer_usd": (0.0, None),
    "max_cex_dispersion_bps": (0.0, None),
    "latency_min_dislocation_bps": (0.0, None),
    "bundle_max_pair_cost": (0.01, 1.0),
    "bundle_min_liquidity_usd": (0.0, None),
    "zscore_entry_abs": (0.1, 10.0),
    "value_yes_low": (0.01, 0.99),
    "value_yes_high": (0.01, 0.99),
    "value_no_yes_min": (0.01, 0.99),
    "value_no_no_max": (0.01, 0.99),
    "value_liq_floor_usd": (0.0, None),
    "orderbook_min_bid_share": (0.0, 1.0),
    "max_spread_bps": (0.0, None),
    "min_hours_to_resolution": (0.0, None),
    "max_condition_exposure_usd": (0.0, None),
    "max_daily_notional_usd": (0.0, None),
    "daily_notional_window_hours": (1.0, None),
    "copy_min_usd": (0.0, None),
    "copy_max_usd": (0.0, None),
    "copy_min_price": (0.0, 1.0),
    "copy_max_price": (0.0, 1.0),
    "copy_price_buffer_bps": (0.0, 5000.0),
    "copy_min_wallet_score": (0.0, 1.0),
    "copy_min_win_rate": (0.0, 1.0),
    "copy_refresh_interval_hours": (0.5, 168.0),
    "max_category_exposure_usd": (0.0, None),
}

INT_RANGES_EXTRA: dict[str, tuple[int, int | None]] = {
    "telegram_daily_report_hour": (0, 23),
}
INT_RANGES.update(INT_RANGES_EXTRA)

FLOAT_RANGES_EXTRA: dict[str, tuple[float, float | None]] = {
    "reconcile_poll_sleep_s": (0.01, 10.0),
    "wallet_score_decay_half_life_hours": (1.0, None),
    "ev_min_edge_bps": (0.0, None),
    "ev_min_profit_usd": (0.0, None),
    "ev_slippage_estimate_bps": (0.0, None),
    "ev_fee_bps": (0.0, None),
    "ev_time_discount_rate": (0.0, None),
    "ev_max_hours_to_resolution": (0.0, None),
    "max_slippage_bps": (0.0, None),
    "min_survivability": (0.0, 1.0),
    "post_entry_drift_bps": (0.0, None),
    "follower_latency_ms": (0.0, None),
    "paper_slippage_model_bps": (0.0, None),
}
FLOAT_RANGES.update(FLOAT_RANGES_EXTRA)

TEXT_KEYS = {"host", "dashboard_secret", "wallet_address", "strategy_profile", "ui_mode", "polymarket_private_key", "telegram_bot_token", "telegram_chat_id"}
LIST_KEYS = {
    "copy_watch_wallets",
    "copy_allowed_categories",
    "copy_allowed_outcomes",
    "copy_required_keywords",
    "copy_blocked_keywords",
    "copy_discover_categories",
}
DICT_FLOAT_KEYS = {"copy_wallet_score_overrides", "category_exposure_caps"}


def _as_bool(v: Any) -> tuple[bool | None, str | None]:
    if isinstance(v, bool):
        return v, None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True, None
    if s in ("0", "false", "no", "off"):
        return False, None
    return None, "must be boolean"


def _as_int(v: Any) -> tuple[int | None, str | None]:
    try:
        return int(float(str(v).strip())), None
    except Exception:
        return None, "must be integer"


def _as_float(v: Any) -> tuple[float | None, str | None]:
    try:
        return float(str(v).strip()), None
    except Exception:
        return None, "must be number"


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        arr = v
    elif isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            j = json.loads(s)
            if isinstance(j, list):
                arr = j
            else:
                arr = [x.strip() for x in s.replace("\n", ",").split(",")]
        except json.JSONDecodeError:
            arr = [x.strip() for x in s.replace("\n", ",").split(",")]
    else:
        arr = [v]
    out: list[str] = []
    for x in arr:
        sx = str(x).strip()
        if sx:
            out.append(sx)
    return out


def _as_dict_float(v: Any) -> dict[str, float]:
    if isinstance(v, dict):
        obj = v
    elif isinstance(v, str):
        s = v.strip()
        if not s:
            return {}
        try:
            j = json.loads(s)
            if not isinstance(j, dict):
                return {}
            obj = j
        except json.JSONDecodeError:
            return {}
    else:
        return {}
    out: dict[str, float] = {}
    for k, x in obj.items():
        sk = str(k).strip().lower()
        if not sk:
            continue
        try:
            out[sk] = float(x)
        except (TypeError, ValueError):
            continue
    return out


def validate_and_normalize_settings_patch(patch: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    allowed = set(default_kv_seed().keys())
    normalized: dict[str, str] = {}
    errors: dict[str, str] = {}

    for k, raw in patch.items():
        if k not in allowed:
            errors[k] = "unknown setting key"
            continue

        if k in BOOL_KEYS:
            b, err = _as_bool(raw)
            if err:
                errors[k] = err
            else:
                normalized[k] = "true" if b else "false"
            continue

        if k in INT_RANGES:
            iv, err = _as_int(raw)
            if err or iv is None:
                errors[k] = err or "must be integer"
                continue
            lo, hi = INT_RANGES[k]
            if iv < lo or (hi is not None and iv > hi):
                errors[k] = f"must be in range [{lo}, {hi if hi is not None else 'inf'}]"
                continue
            normalized[k] = str(iv)
            continue

        if k in FLOAT_RANGES:
            fv, err = _as_float(raw)
            if err or fv is None:
                errors[k] = err or "must be number"
                continue
            lo, hi = FLOAT_RANGES[k]
            if fv < lo or (hi is not None and fv > hi):
                errors[k] = f"must be in range [{lo}, {hi if hi is not None else 'inf'}]"
                continue
            normalized[k] = str(fv)
            continue

        if k in LIST_KEYS:
            vals = [x.lower() for x in _as_list(raw)]
            if k == "copy_watch_wallets":
                bad = [x for x in vals if not is_valid_polygon_address(x)]
                if bad:
                    errors[k] = f"invalid wallet addresses: {', '.join(bad[:3])}"
                    continue
            if k == "copy_allowed_categories":
                allowed_cats = {c.value for c in MarketCategory}
                bad = [x for x in vals if x not in allowed_cats]
                if bad:
                    errors[k] = f"invalid categories: {', '.join(bad[:5])}"
                    continue
            if k == "copy_allowed_outcomes":
                bad = [x for x in vals if x not in ("yes", "no", "unknown")]
                if bad:
                    errors[k] = "allowed outcomes are yes,no,unknown"
                    continue
            normalized[k] = json.dumps(vals)
            continue

        if k in DICT_FLOAT_KEYS:
            vals = _as_dict_float(raw)
            if k == "category_exposure_caps":
                allowed_cats = {c.value for c in MarketCategory}
                bad = [x for x in vals.keys() if x not in allowed_cats]
                if bad:
                    errors[k] = f"invalid categories: {', '.join(bad[:5])}"
                    continue
            normalized[k] = json.dumps(vals)
            continue

        if k in TEXT_KEYS:
            s = str(raw).strip()
            if k == "wallet_address":
                if s and not is_valid_polygon_address(s):
                    errors[k] = "invalid polygon address"
                    continue
                normalized[k] = s.lower()
                continue
            if k == "polymarket_private_key":
                if "…" in s:
                    errors[k] = "masked key not allowed; paste full key"
                    continue
                if s and not is_valid_private_key_hex(s):
                    errors[k] = "invalid private key format"
                    continue
                normalized[k] = s
                continue
            if k == "ui_mode":
                if s.lower() not in ("both", "dashboard", "terminal"):
                    errors[k] = "ui_mode must be both|dashboard|terminal"
                    continue
                normalized[k] = s.lower()
                continue
            normalized[k] = s
            continue

        normalized[k] = str(raw).strip()

    # cross-field checks when both provided.
    try:
        if "min_bet_usd" in normalized and "max_bet_usd" in normalized:
            if float(normalized["min_bet_usd"]) > float(normalized["max_bet_usd"]):
                errors["min_bet_usd"] = "must be <= max_bet_usd"
        if "copy_min_price" in normalized and "copy_max_price" in normalized:
            if float(normalized["copy_min_price"]) > float(normalized["copy_max_price"]):
                errors["copy_min_price"] = "must be <= copy_max_price"
        if "value_yes_low" in normalized and "value_yes_high" in normalized:
            if float(normalized["value_yes_low"]) > float(normalized["value_yes_high"]):
                errors["value_yes_low"] = "must be <= value_yes_high"
    except Exception:
        pass

    return normalized, errors
