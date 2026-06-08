"""Admin + auth JSON API (mounted from server)."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bot.auth_session import issue_token
from bot.categories import MarketCategory
from bot.config_file import load_config, project_root
from bot.copy_rules import build_candidate, limit_price_with_buffer, passes_filters, wallet_score
from bot.db.bootstrap import hash_password
from bot.db.kv import load_all_kv, upsert_many_kv
from bot.db.models import ArticleSignal, User
from bot.exposure import rolling_notional_usd
from bot.http_retry import get_json_retry
from bot.orderbook import orderbook_buy_depth_ok
from bot.risk import gate_intent
from bot.settings import Settings, default_kv_seed
from bot.settings_validation import validate_and_normalize_settings_patch
from bot.leaderboard import (
    analyze_wallet_quality,
    discover_qualified_wallets,
    discover_top_wallets,
    fetch_leaderboard,
    CATEGORIES,
    TIME_PERIODS,
)
from bot.wallet_trades import fetch_wallet_trades
from bot.web.deps import get_current_user, get_db, require_admin, verify_user_password

router = APIRouter(tags=["auth-admin"])
ACTIVITY_URL = "https://data-api.polymarket.com/activity"


def _trader(request: Request):
    t = getattr(request.app.state, "trader", None)
    return t


def _require_webhook_token(x_hook_token: str | None) -> None:
    cfg = load_config()
    expected = str(cfg.get("webhook_token") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="webhook_token not configured")
    provided = (x_hook_token or "").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook token")


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
def api_login(body: LoginBody, request: Request, db: Annotated[Session, Depends(get_db)]):
    u = verify_user_password(db, body.username.strip(), body.password)
    if not u:
        raise HTTPException(status_code=401, detail="Bad credentials")
    cfg = load_config()
    secret = str(cfg.get("session_secret") or "")
    token = issue_token(secret, {"uid": u.id, "role": u.role})
    resp = {"ok": True, "username": u.username, "role": u.role}
    from fastapi.responses import JSONResponse

    r = JSONResponse(resp)
    r.set_cookie(
        key="pm_session",
        value=token,
        httponly=True,
        max_age=86400 * 14,
        samesite="lax",
        path="/",
    )
    return r


@router.post("/api/auth/logout")
def api_logout():
    from fastapi.responses import JSONResponse

    r = JSONResponse({"ok": True})
    r.delete_cookie("pm_session", path="/")
    return r


@router.get("/api/me")
def api_me(user: Annotated[User, Depends(get_current_user)]):
    return {"id": user.id, "username": user.username, "role": user.role}


@router.get("/api/admin/users")
def admin_list_users(_: Annotated[User, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    rows = db.execute(select(User).order_by(User.id)).scalars().all()
    return [{"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active} for u in rows]


class CreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = "user"


@router.post("/api/admin/users")
def admin_create_user(
    body: CreateUserBody,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    un = body.username.strip().lower()
    if db.scalars(select(User).where(User.username == un)).first():
        raise HTTPException(status_code=400, detail="Username exists")
    role = body.role if body.role in ("admin", "user") else "user"
    u = User(username=un, password_hash=hash_password(body.password), role=role, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"id": u.id, "username": u.username, "role": u.role}


class PatchUserBody(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


@router.patch("/api/admin/users/{user_id}")
def admin_patch_user(
    user_id: int,
    body: PatchUserBody,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    if user_id == admin.id and body.is_active is False:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404)
    if body.role in ("admin", "user"):
        u.role = body.role
    if body.is_active is not None:
        u.is_active = body.is_active
    if body.password:
        u.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True}


@router.get("/api/admin/settings")
def admin_get_settings(_: Annotated[User, Depends(require_admin)]):
    kv = load_all_kv()
    # Mask private key for response (show prefix only)
    out = dict(kv)
    pk = out.get("polymarket_private_key") or ""
    if len(pk) > 12:
        out["polymarket_private_key"] = pk[:6] + "…" + pk[-4:]
    return {
        "settings": out,
        "defaults": default_kv_seed(),
        "meta": {
            "categories": [c.value for c in MarketCategory],
        },
    }


class SettingsPatch(BaseModel):
    settings: dict[str, Any]


@router.post("/api/admin/settings")
def admin_save_settings(
    body: SettingsPatch,
    _: Annotated[User, Depends(require_admin)],
):
    normalized, errors = validate_and_normalize_settings_patch(body.settings)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "invalid_settings", "errors": errors})
    upsert_many_kv(normalized)
    return {"ok": True}


@router.get("/api/admin/settings/export")
def admin_export_settings(
    _: Annotated[User, Depends(require_admin)],
    include_secrets: bool = False,
):
    kv = load_all_kv()
    out = dict(kv)
    if not include_secrets:
        if out.get("polymarket_private_key"):
            out["polymarket_private_key"] = ""
    return {"ok": True, "settings": out}


@router.post("/api/admin/settings/import")
def admin_import_settings(
    body: SettingsPatch,
    _: Annotated[User, Depends(require_admin)],
):
    normalized, errors = validate_and_normalize_settings_patch(body.settings)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "invalid_settings", "errors": errors})
    upsert_many_kv(normalized)
    return {"ok": True, "updated": len(normalized)}


class ResetKeysBody(BaseModel):
    keys: list[str]


@router.post("/api/admin/settings/reset")
def admin_reset_settings(
    body: ResetKeysBody,
    _: Annotated[User, Depends(require_admin)],
):
    """Reset specified keys to their defaults."""
    defaults = default_kv_seed()
    to_write: dict[str, str] = {}
    unknown: list[str] = []
    for k in body.keys:
        if k in defaults:
            to_write[k] = defaults[k]
        else:
            unknown.append(k)
    if to_write:
        upsert_many_kv(to_write)
    return {"ok": True, "reset": list(to_write.keys()), "unknown": unknown}


@router.get("/api/admin/settings/meta")
def admin_settings_meta(_: Annotated[User, Depends(require_admin)]):
    """Field metadata for settings UI: groups, descriptions, types, defaults."""
    return {"ok": True, "groups": _settings_field_groups()}


def _settings_field_groups() -> list[dict]:
    """Settings field metadata grouped into sections for the UI."""
    return [
        {
            "id": "core",
            "title": "Core Bot Settings",
            "description": "Fundamental bot behavior: mode, timing, and bet sizing.",
            "level": "basic",
            "fields": [
                {"key": "dry_run", "label": "Dry Run Mode", "type": "bool", "help": "When enabled, the bot simulates trades without placing real orders. Recommended for testing."},
                {"key": "trading_paused", "label": "Trading Paused", "type": "bool", "help": "Temporarily pause all trading activity. The bot continues scanning but won't place orders."},
                {"key": "scan_interval_seconds", "label": "Scan Interval (seconds)", "type": "int", "help": "How often the bot scans for new trading opportunities. Lower = more frequent scans (5-300)."},
                {"key": "default_bet_usd", "label": "Default Bet Size (USD)", "type": "float", "help": "The standard amount in USD to wager per trade."},
                {"key": "min_bet_usd", "label": "Minimum Bet (USD)", "type": "float", "help": "The smallest trade size the bot will place."},
                {"key": "max_bet_usd", "label": "Maximum Bet (USD)", "type": "float", "help": "The largest trade size the bot will place."},
                {"key": "max_trades_per_cycle", "label": "Max Trades Per Cycle", "type": "int", "help": "How many trades the bot can execute in a single scan cycle (1-50)."},
                {"key": "balance_buffer_usd", "label": "Balance Buffer (USD)", "type": "float", "help": "Reserve this amount of USDC and don't trade with it. Acts as a safety margin."},
            ],
        },
        {
            "id": "agents",
            "title": "Trading Agents",
            "description": "Enable or disable individual trading strategy agents.",
            "level": "basic",
            "fields": [
                {"key": "agent_copy", "label": "Copy Signal Agent", "type": "bool", "help": "Mirrors trades from watched wallets. Requires wallet addresses to be configured."},
                {"key": "agent_weather", "label": "Weather Arb Agent", "type": "bool", "help": "Compares Open-Meteo forecasts against temperature markets. Paper-test only."},
            ],
        },
        {
            "id": "sizing",
            "title": "Sizing & Signals",
            "description": "Research-signal and PnL-aware sizing controls.",
            "level": "advanced",
            "fields": [
                {"key": "min_edge_bps", "label": "Minimum Edge (bps)", "type": "int", "help": "Minimum price edge in basis points between reference and limit price. 0 = disabled."},
                {"key": "signals_enabled", "label": "Research Signals", "type": "bool", "help": "Use uploaded research signals to bias trade sizing."},
                {"key": "pnl_sizing_enabled", "label": "PnL-Aware Sizing", "type": "bool", "help": "Adjust bet sizes based on recent trading performance."},
                {"key": "pnl_sizing_window", "label": "PnL Sizing Window (days)", "type": "int", "help": "Lookback window for PnL-aware sizing."},
            ],
        },
        {
            "id": "risk",
            "title": "Risk Controls",
            "description": "Gate checks that filter out risky or unwanted trades before execution.",
            "level": "basic",
            "fields": [
                {"key": "cex_gate_crypto", "label": "CEX Gate for Crypto", "type": "bool", "help": "Require CEX price validation for crypto markets before trading."},
                {"key": "cex_require_dispersion", "label": "Require Dispersion Data", "type": "bool", "help": "Block crypto trades when CEX dispersion data is unavailable."},
                {"key": "max_cex_dispersion_bps", "label": "Max CEX Dispersion (bps)", "type": "float", "help": "Maximum allowed dispersion between CEX price sources."},
                {"key": "orderbook_gate_enabled", "label": "Order Book Gate", "type": "bool", "help": "Check order book depth/imbalance before placing BUY orders."},
                {"key": "orderbook_min_bid_share", "label": "Min Bid Share", "type": "float", "help": "Minimum bid-side share of the order book (0.0 to 1.0)."},
                {"key": "spread_gate_enabled", "label": "Spread Gate", "type": "bool", "help": "Reject trades where the bid-ask spread is too wide."},
                {"key": "max_spread_bps", "label": "Max Spread (bps)", "type": "float", "help": "Maximum allowable bid-ask spread in basis points."},
                {"key": "resolution_gate_enabled", "label": "Resolution Time Gate", "type": "bool", "help": "Skip markets resolving too soon (risk of last-minute manipulation)."},
                {"key": "min_hours_to_resolution", "label": "Min Hours to Resolution", "type": "float", "help": "Minimum hours until market resolution to allow trading."},
                {"key": "circuit_breaker_max_fails", "label": "Circuit Breaker (max fails)", "type": "int", "help": "Pause all trading after this many consecutive execution failures. 0 = disabled."},
            ],
        },
        {
            "id": "exposure",
            "title": "Exposure Limits",
            "description": "Position and notional caps to limit overall risk.",
            "level": "advanced",
            "fields": [
                {"key": "max_condition_exposure_usd", "label": "Max Per-Market Exposure (USD)", "type": "float", "help": "Maximum USD exposure to any single market condition. 0 = unlimited."},
                {"key": "max_category_exposure_usd", "label": "Max Per-Category Exposure (USD)", "type": "float", "help": "Maximum USD exposure to any single category. 0 = unlimited."},
                {"key": "category_exposure_caps", "label": "Per-Category Caps (JSON)", "type": "json", "help": "Override per-category exposure limits. E.g. {\"politics\": 100, \"crypto_short\": 50}"},
                {"key": "max_daily_notional_usd", "label": "Max Daily Notional (USD)", "type": "float", "help": "Total USD notional traded per rolling window. 0 = unlimited."},
                {"key": "daily_notional_window_hours", "label": "Daily Window (hours)", "type": "float", "help": "Rolling window for daily notional limit."},
            ],
        },
        {
            "id": "execution",
            "title": "Execution Behavior",
            "description": "How orders are placed and monitored on the CLOB.",
            "level": "advanced",
            "fields": [
                {"key": "strict_execution", "label": "Strict Limit Orders", "type": "bool", "help": "Use strict limit (GTD) orders. When off, may use IOC or market fills."},
                {"key": "allow_market_fallback", "label": "Market FOK Fallback", "type": "bool", "help": "If a limit order doesn't fill, attempt a fill-or-kill market order."},
                {"key": "order_ttl_seconds", "label": "Order TTL (seconds)", "type": "int", "help": "How long a limit order stays live before expiry."},
                {"key": "order_poll_seconds", "label": "Order Poll Interval (seconds)", "type": "float", "help": "How frequently to check order fill status."},
                {"key": "reconcile_enabled", "label": "Auto-Reconcile", "type": "bool", "help": "Automatically reconcile trade records with CLOB each cycle."},
                {"key": "reconcile_history_depth", "label": "Reconcile Depth", "type": "int", "help": "How many recent trades to check during reconciliation."},
                {"key": "reconcile_poll_sleep_s", "label": "Reconcile Poll Sleep (s)", "type": "float", "help": "Delay between CLOB API calls during reconciliation."},
            ],
        },
        {
            "id": "categories",
            "title": "Market Categories",
            "description": "Toggle which market categories the bot is allowed to trade.",
            "level": "basic",
            "fields": [
                {"key": "ENABLE_CRYPTO_SHORT", "label": "Crypto Short-Term", "type": "bool", "help": "Short-term crypto price prediction markets."},
                {"key": "ENABLE_CRYPTO_OTHER", "label": "Crypto Other", "type": "bool", "help": "Other crypto-related markets."},
                {"key": "ENABLE_SPORTS", "label": "Sports", "type": "bool", "help": "Sports outcome prediction markets."},
                {"key": "ENABLE_POLITICS", "label": "Politics", "type": "bool", "help": "Political election and policy markets."},
                {"key": "ENABLE_MACRO", "label": "Macro / Economics", "type": "bool", "help": "Macroeconomic indicator and rate markets."},
                {"key": "ENABLE_WEATHER", "label": "Weather", "type": "bool", "help": "Weather and climate event markets."},
                {"key": "ENABLE_SCIENCE_TECH", "label": "Science & Tech", "type": "bool", "help": "Science, tech, and innovation markets."},
                {"key": "ENABLE_ENTERTAINMENT", "label": "Entertainment", "type": "bool", "help": "Entertainment, awards, and cultural markets."},
                {"key": "ENABLE_GEOPOLITICS", "label": "Geopolitics", "type": "bool", "help": "Geopolitical event markets."},
                {"key": "ENABLE_OTHER", "label": "Other", "type": "bool", "help": "Uncategorized markets."},
            ],
        },
        {
            "id": "copy_trading",
            "title": "Copy Trading",
            "description": "Configure which wallets to copy-trade and apply filters.",
            "level": "basic",
            "fields": [
                {"key": "copy_watch_wallets", "label": "Watch Wallets", "type": "list", "help": "Polygon wallet addresses to copy trades from (one per line)."},
                {"key": "copy_poll_seconds", "label": "Poll Interval (seconds)", "type": "int", "help": "How often to check watched wallets for new trades."},
                {"key": "copy_min_usd", "label": "Min Trade Size (USD)", "type": "float", "help": "Only copy trades at least this size. 0 = no minimum."},
                {"key": "copy_max_usd", "label": "Max Trade Size (USD)", "type": "float", "help": "Cap copied trade size. 0 = no cap."},
                {"key": "copy_min_price", "label": "Min Price", "type": "float", "help": "Only copy when the token price is at least this (0.0-1.0)."},
                {"key": "copy_max_price", "label": "Max Price", "type": "float", "help": "Only copy when the token price is at most this (0.0-1.0)."},
                {"key": "copy_price_buffer_bps", "label": "Price Buffer (bps)", "type": "float", "help": "Extra slippage buffer in bps added to the copied trade's price."},
                {"key": "copy_allow_unknown_outcome", "label": "Allow Unknown Outcomes", "type": "bool", "help": "Copy trades even when the outcome label cannot be determined."},
                {"key": "copy_allowed_categories", "label": "Allowed Categories", "type": "list", "help": "Only copy trades in these categories (comma-separated). Empty = all."},
                {"key": "copy_allowed_outcomes", "label": "Allowed Outcomes", "type": "list", "help": "Only copy these outcomes: yes, no, unknown. Empty = all."},
                {"key": "copy_required_keywords", "label": "Required Keywords", "type": "list", "help": "Only copy trades whose market title contains one of these keywords."},
                {"key": "copy_blocked_keywords", "label": "Blocked Keywords", "type": "list", "help": "Skip trades whose market title contains any of these keywords."},
                {"key": "copy_min_wallet_score", "label": "Min Wallet Score", "type": "float", "help": "Minimum historical performance score for the source wallet (0.0-1.0)."},
                {"key": "copy_min_win_rate", "label": "Min Win Rate", "type": "float", "help": "Minimum win rate (0.0-1.0) for leaderboard wallet import. E.g. 0.60 = 60%. Wallets below (this - 10%) get auto-pruned."},
                {"key": "copy_min_win_streak", "label": "Min Win Streak", "type": "int", "help": "Minimum consecutive wins required for a wallet to qualify for copy-trading."},
                {"key": "copy_min_total_trades", "label": "Min Total Trades", "type": "int", "help": "Minimum number of resolved trades required before a wallet is trusted."},
                {"key": "copy_auto_manage", "label": "Auto-Manage Wallets", "type": "bool", "help": "Automatically discover, monitor, and prune wallets from leaderboard. No manual wallet pasting needed."},
                {"key": "copy_refresh_interval_hours", "label": "Refresh Interval (hours)", "type": "float", "help": "How often to re-scan the leaderboard for new wallets and prune underperformers."},
                {"key": "copy_max_watched_wallets", "label": "Max Watched Wallets", "type": "int", "help": "Maximum number of wallets to watch simultaneously."},
                {"key": "copy_discover_categories", "label": "Discovery Categories", "type": "list", "help": "Leaderboard categories to scan: OVERALL, CRYPTO, SPORTS, POLITICS, FINANCE (one per line)."},
                {"key": "copy_wallet_score_overrides", "label": "Wallet Score Overrides (JSON)", "type": "json", "help": "Override scores for specific wallets. E.g. {\"0xabc...\": 0.15}"},
            ],
        },
        {
            "id": "telegram",
            "title": "Telegram Notifications",
            "description": "Send trade alerts, error reports, and daily summaries to a Telegram chat.",
            "level": "basic",
            "fields": [
                {"key": "telegram_enabled", "label": "Enable Telegram", "type": "bool", "help": "Master switch for Telegram notifications."},
                {"key": "telegram_bot_token", "label": "Bot Token", "type": "secret", "help": "Telegram Bot API token from @BotFather (e.g. 123456:ABC-DEF...)."},
                {"key": "telegram_chat_id", "label": "Chat ID", "type": "text", "help": "Telegram chat/group ID to send messages to. Use @userinfobot to find yours."},
                {"key": "telegram_on_trade", "label": "Notify on Trade", "type": "bool", "help": "Send a message whenever a trade is placed."},
                {"key": "telegram_on_error", "label": "Notify on Error", "type": "bool", "help": "Send a message when the bot encounters errors."},
                {"key": "telegram_on_cycle_summary", "label": "Cycle Summary", "type": "bool", "help": "Send a brief summary after each scan cycle."},
                {"key": "telegram_on_balance_change", "label": "Balance Change Alerts", "type": "bool", "help": "Notify when your USDC balance changes significantly."},
                {"key": "telegram_daily_report", "label": "Daily Report", "type": "bool", "help": "Send a daily performance report."},
                {"key": "telegram_daily_report_hour", "label": "Report Hour (UTC)", "type": "int", "help": "Hour of day (0-23 UTC) to send the daily report."},
            ],
        },
        {
            "id": "ev_gate",
            "title": "EV-Aware Trade Gating",
            "description": "Expected-value checks that estimate whether a trade is profitable after fees and slippage.",
            "level": "advanced",
            "fields": [
                {"key": "ev_gate_enabled", "label": "Enable EV Gate", "type": "bool", "help": "Require trades to pass an expected-value profitability check."},
                {"key": "ev_min_edge_bps", "label": "Min Edge (bps)", "type": "float", "help": "Minimum expected edge after fees, in basis points."},
                {"key": "ev_min_profit_usd", "label": "Min Expected Profit (USD)", "type": "float", "help": "Minimum expected dollar profit per trade."},
                {"key": "ev_slippage_estimate_bps", "label": "Estimated Slippage (bps)", "type": "float", "help": "Assumed slippage for EV calculation."},
                {"key": "ev_fee_bps", "label": "Fee Estimate (bps)", "type": "float", "help": "Trading fee assumption for EV math."},
                {"key": "ev_time_discount_rate", "label": "Time Discount Rate", "type": "float", "help": "Annualized discount rate for time-value of capital locked in a position."},
                {"key": "ev_max_hours_to_resolution", "label": "Max Hours to Resolution", "type": "float", "help": "Skip markets resolving later than this. 0 = no limit."},
            ],
        },
        {
            "id": "paper_realism",
            "title": "Paper Trading Realism",
            "description": "Make dry-run simulations more realistic by modeling slippage.",
            "level": "advanced",
            "fields": [
                {"key": "paper_realism_enabled", "label": "Enable Paper Realism", "type": "bool", "help": "Add simulated slippage and fill uncertainty to dry-run trades."},
                {"key": "paper_slippage_model_bps", "label": "Simulated Slippage (bps)", "type": "float", "help": "Basis points of slippage applied to paper trades."},
                {"key": "max_slippage_bps", "label": "Max Slippage (bps)", "type": "float", "help": "Maximum allowable slippage. 0 = unlimited."},
                {"key": "min_survivability", "label": "Min Survivability", "type": "float", "help": "Minimum fill probability for a trade to be considered viable (0.0-1.0)."},
                {"key": "post_entry_drift_bps", "label": "Post-Entry Drift (bps)", "type": "float", "help": "Expected adverse price movement after entry."},
                {"key": "follower_latency_ms", "label": "Follower Latency (ms)", "type": "float", "help": "Simulated delay for copy-trade execution."},
            ],
        },
        {
            "id": "infrastructure",
            "title": "Infrastructure & Display",
            "description": "Server, logging, and dashboard display settings.",
            "level": "advanced",
            "fields": [
                {"key": "ui_mode", "label": "UI Mode", "type": "select", "options": ["both", "dashboard", "terminal"], "help": "Which interfaces to enable: both, dashboard only, or terminal only."},
                {"key": "host", "label": "Bind Host", "type": "text", "help": "IP address to bind the web server to. Use 0.0.0.0 for all interfaces."},
                {"key": "port", "label": "Port", "type": "int", "help": "TCP port for the web dashboard (1-65535)."},
                {"key": "structured_log", "label": "Structured Logging", "type": "bool", "help": "Emit machine-readable SLOG lines for each event."},
                {"key": "ws_allow_anonymous", "label": "Allow Anonymous WebSocket", "type": "bool", "help": "Let unauthenticated clients connect to the /ws stream."},
                {"key": "open_orders_display_limit", "label": "Open Orders Display Limit", "type": "int", "help": "Max number of open orders shown on the dashboard."},
                {"key": "min_clob_liquidity_usd", "label": "Min CLOB Liquidity (USD)", "type": "float", "help": "Minimum order book liquidity to consider a market tradeable."},
                {"key": "min_gamma_volume", "label": "Min Gamma Volume", "type": "float", "help": "Minimum trading volume from Gamma API to consider a market."},
                {"key": "wallet_score_decay_half_life_hours", "label": "Wallet Score Decay Half-Life (hours)", "type": "float", "help": "How quickly old wallet trade data loses weight in scoring."},
            ],
        },
        {
            "id": "credentials",
            "title": "Exchange Credentials",
            "description": "Polymarket wallet keys and signature settings. Handle with care.",
            "level": "basic",
            "fields": [
                {"key": "polymarket_private_key", "label": "Private Key", "type": "secret", "help": "Your Polymarket wallet private key. Leave blank to keep current. Masked on read."},
                {"key": "wallet_address", "label": "Wallet Address", "type": "text", "help": "Your Polygon wallet address (0x...)."},
                {"key": "polymarket_signature_type", "label": "Signature Type", "type": "select", "options": ["0", "1"], "help": "Polymarket signature type. Usually 0 for EOA wallets."},
                {"key": "dashboard_secret", "label": "Dashboard Secret", "type": "secret", "help": "Optional bearer token for API authentication (legacy)."},
            ],
        },
    ]


@router.get("/api/admin/signals")
def admin_list_signals(_: Annotated[User, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    rows = db.execute(select(ArticleSignal).order_by(ArticleSignal.id.desc()).limit(200)).scalars().all()
    return [
        {
            "id": r.id,
            "title": r.title,
            "summary": r.summary,
            "source_url": r.source_url,
            "image_path": r.image_path,
            "keywords": json.loads(r.keywords or "[]"),
            "sentiment": r.sentiment,
            "weight": r.weight,
            "active": r.active,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        }
        for r in rows
    ]


@router.post("/api/admin/signals")
async def admin_create_signal(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    title: str = Form(...),
    summary: str = Form(""),
    source_url: str = Form(""),
    keywords: str = Form("[]"),
    sentiment: float = Form(0.0),
    weight: float = Form(1.0),
    image: UploadFile | None = File(None),
):
    cfg = load_config()
    upload_dir = Path(cfg.get("upload_dir") or "./data/uploads")
    if not upload_dir.is_absolute():
        upload_dir = project_root() / upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    img_path = ""
    if image and image.filename:
        safe = "".join(c for c in image.filename if c.isalnum() or c in "._-")[:120]
        dest = upload_dir / safe
        with dest.open("wb") as f:
            shutil.copyfileobj(image.file, f)
        img_path = str(dest.relative_to(project_root()))

    try:
        json.loads(keywords)
    except json.JSONDecodeError:
        keywords = "[]"

    sig = ArticleSignal(
        title=title[:512],
        summary=summary,
        source_url=source_url[:1024],
        image_path=img_path[:1024],
        keywords=keywords,
        sentiment=float(sentiment),
        weight=float(weight),
        active=True,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return {"id": sig.id}


class SignalPatchBody(BaseModel):
    active: bool | None = None
    sentiment: float | None = None
    weight: float | None = None
    keywords: list[str] | None = None


@router.patch("/api/admin/signals/{signal_id}")
def admin_patch_signal(
    signal_id: int,
    body: SignalPatchBody,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    sig = db.get(ArticleSignal, signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    if body.active is not None:
        sig.active = bool(body.active)
    if body.sentiment is not None:
        sig.sentiment = float(body.sentiment)
    if body.weight is not None:
        sig.weight = float(body.weight)
    if body.keywords is not None:
        sig.keywords = json.dumps([str(x) for x in body.keywords])
    db.commit()
    return {"ok": True}


@router.delete("/api/admin/signals/{signal_id}")
def admin_delete_signal(
    signal_id: int,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    sig = db.get(ArticleSignal, signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    db.delete(sig)
    db.commit()
    return {"ok": True}


@router.get("/api/admin/wallet-trades")
async def admin_wallet_trades(
    wallet: str,
    request: Request,
    _: Annotated[User, Depends(require_admin)],
):
    """Recent trades for any wallet (collectmarkets2-style raw pull)."""
    bot = _trader(request)
    if not bot or not getattr(bot, "_http", None):
        raise HTTPException(status_code=503, detail="Trader HTTP client not ready")
    rows = await fetch_wallet_trades(bot._http, wallet, limit=120)
    return {"wallet": wallet.lower().strip(), "count": len(rows), "trades": rows[:40]}


@router.get("/api/admin/copy-preview")
async def admin_copy_preview(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
    wallet: str | None = None,
    limit: int = 40,
):
    """
    Preview which recent trades pass copy filters and why.
    Uses current running settings if trader exists, else Settings.load().
    """
    bot = _trader(request)
    if not bot or not getattr(bot, "_http", None):
        raise HTTPException(status_code=503, detail="Trader HTTP client not ready")
    s = getattr(bot, "settings", None) or Settings.load()
    watch = [wallet.strip().lower()] if wallet else list(getattr(s, "copy_watch_wallets", []) or [])
    watch = [w for w in watch if w]
    if not watch:
        return {"ok": True, "wallets": [], "items": [], "note": "no_wallets_configured"}
    lim = max(1, min(int(limit or 40), 120))
    items: list[dict[str, Any]] = []
    for w in watch[:5]:
        try:
            rows = await get_json_retry(bot._http, ACTIVITY_URL, params={"user": w, "limit": str(lim)})
        except Exception as e:
            items.append({"wallet": w, "error": str(e), "rows": []})
            continue
        score, parts = wallet_score(
            rows if isinstance(rows, list) else [],
            wallet=w,
            default_bet_usd=float(getattr(s, "default_bet_usd", 5.0)),
            settings=s,
        )
        out: list[dict[str, Any]] = []
        for entry in rows if isinstance(rows, list) else []:
            c = build_candidate(entry, w, float(getattr(s, "default_bet_usd", 5.0)))
            if c is None:
                continue
            ok, reason = passes_filters(s, c)
            out.append(
                {
                    "wallet": w,
                    "pass": bool(ok),
                    "reason": reason,
                    "title": c.title[:140],
                    "category": c.category,
                    "outcome": c.outcome,
                    "price": c.price,
                    "usd": c.usdc,
                    "token_id": c.token_id,
                    "limit_price": limit_price_with_buffer(s, c.price),
                }
            )
        items.append({"wallet": w, "score": score, "score_parts": parts, "count": len(out), "rows": out[:80]})
    items.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    return {"ok": True, "wallets": watch, "items": items, "min_score": float(getattr(s, "copy_min_wallet_score", 0.0) or 0.0)}


async def _intents_preview_core(bot: Any, agent: str, limit: int) -> dict[str, Any]:
    """
    Preview current agent intents and gate decisions without placing orders.
    agent: all|copy_signal|weather_arb
    """
    allowed_agents = {"all", "copy_signal", "weather_arb"}
    if agent not in allowed_agents:
        raise HTTPException(status_code=400, detail=f"agent must be one of: {sorted(allowed_agents)}")

    await bot._reload_settings_async()
    await bot.refresh_positions()
    await bot.refresh_open_orders()
    if not bot.clob:
        # In dry-run without keys, only copy agent can propose intents.
        if agent in ("all", "copy_signal") and bot.settings.agent_copy and bot.settings.copy_watch_wallets:
            copy_intents = await bot._copy_agent.propose(bot._http)
            lim = max(1, min(int(limit or 80), 250))
            out = [
                {
                    "agent": it.agent,
                    "strategy": it.strategy,
                    "question": it.question[:140],
                    "token_id": it.token_id,
                    "category": it.category.value,
                    "usd": round(float(it.size_usd), 4),
                    "max_price": it.max_price,
                    "pass": True,
                    "reason": "ok",
                    "cex_dispersion_bps": None,
                    "bundle_id": it.bundle_id,
                }
                for it in sorted(copy_intents, key=lambda x: -x.priority)[:lim]
            ]
            return {
                "ok": True,
                "agent": agent,
                "enabled_agents": ["copy_signal"],
                "errors": [],
                "count": len(out),
                "items": out,
                "note": "preview in dry-run without CLOB keys; only copy agent available",
            }
        return {
            "ok": True,
            "agent": agent,
            "enabled_agents": [],
            "errors": [],
            "count": 0,
            "items": [],
            "note": "preview unavailable without CLOB keys for selected agents",
        }
    markets = await bot._gamma_scan()
    pos_tokens = {p.get("token_id") for p in bot.state.positions if p.get("token_id")}

    tasks = []
    labels: list[str] = []
    if agent in ("all", "copy_signal") and bot.settings.agent_copy and bot.settings.copy_watch_wallets:
        labels.append("copy_signal")
        tasks.append(bot._copy_agent.propose(bot._http))
    if agent in ("all", "weather_arb") and bot.settings.agent_weather:
        labels.append("weather_arb")
        tasks.append(bot._weather_agent.propose(bot._http))

    if not tasks:
        return {"ok": True, "agent": agent, "enabled_agents": [], "items": [], "note": "no_enabled_agents"}

    raw = await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[name-defined]
    intents = []
    errors: list[dict[str, str]] = []
    for i, r in enumerate(raw):
        if isinstance(r, Exception):
            errors.append({"agent": labels[i], "error": str(r)})
            continue
        intents.extend(r)
    intents.sort(key=lambda x: -x.priority)

    cex_map = await bot._cex_map_for_intents(intents) if intents else {}
    markets_by_cid = {str(m.get("condition_id") or ""): m for m in markets if m.get("condition_id")}
    rolling_n = rolling_notional_usd(
        bot.state.trade_history,
        hours=float(bot.settings.daily_notional_window_hours or 24.0),
    )

    out: list[dict[str, Any]] = []
    lim = max(1, min(int(limit or 80), 250))
    for it in intents[:lim]:
        await bot._apply_intent_multipliers(it)
        disp = bot._dispersion_for_intent(it, cex_map)

        ok_risk, risk_reason = gate_intent(it, bot.settings, disp)
        if not ok_risk:
            out.append(
                {
                    "agent": it.agent,
                    "strategy": it.strategy,
                    "question": it.question[:140],
                    "token_id": it.token_id,
                    "usd": round(float(it.size_usd), 4),
                    "max_price": it.max_price,
                    "pass": False,
                    "reason": risk_reason,
                }
            )
            continue

        if bot.settings.orderbook_gate_enabled and it.side.upper() == "BUY":
            ok_book = await asyncio.to_thread(
                orderbook_buy_depth_ok,
                bot.clob,
                it.token_id,
                float(bot.settings.orderbook_min_bid_share),
            )
            if not ok_book:
                out.append(
                    {
                        "agent": it.agent,
                        "strategy": it.strategy,
                        "question": it.question[:140],
                        "token_id": it.token_id,
                        "usd": round(float(it.size_usd), 4),
                        "max_price": it.max_price,
                        "pass": False,
                        "reason": "orderbook_imbalance",
                    }
                )
                continue

        ok_adv, adv_reason = await bot._advanced_gates_ok(
            [it],
            markets_by_cid=markets_by_cid,
            rolling_notional=rolling_n,
        )
        out.append(
            {
                "agent": it.agent,
                "strategy": it.strategy,
                "question": it.question[:140],
                "token_id": it.token_id,
                "category": it.category.value,
                "usd": round(float(it.size_usd), 4),
                "max_price": it.max_price,
                "pass": bool(ok_adv),
                "reason": "ok" if ok_adv else adv_reason,
                "cex_dispersion_bps": disp,
                "bundle_id": it.bundle_id,
            }
        )

    return {
        "ok": True,
        "agent": agent,
        "enabled_agents": labels,
        "errors": errors,
        "count": len(out),
        "items": out,
    }


@router.get("/api/admin/intents-preview")
async def admin_intents_preview(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
    agent: str = "all",
    limit: int = 80,
):
    """
    Preview current agent intents and gate decisions without placing orders.
    agent: all|copy_signal|weather_arb
    """
    bot = _trader(request)
    if not bot or not getattr(bot, "_http", None):
        raise HTTPException(status_code=503, detail="Trader not initialized")
    return await _intents_preview_core(bot, agent, limit)


@router.post("/api/admin/reload-settings")
def admin_reload_settings(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
):
    """Hot-reload in-memory Settings on the bot (next cycle also reloads)."""
    bot = _trader(request)
    if not bot:
        raise HTTPException(status_code=503)
    try:
        bot.settings = Settings.load()
        bot.state.mode = "dry_run" if bot.settings.dry_run else "live"
        bot._copy_agent.settings = bot.settings  # type: ignore[attr-defined]
        bot._weather_agent.settings = bot.settings  # type: ignore[attr-defined]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    from bot.agents.registry import agents_status as _agents_status

    statuses = _agents_status(bot.settings)
    return {
        "ok": True,
        "agents": {a["id"]: a["enabled"] for a in statuses},
    }


class HookPauseBody(BaseModel):
    paused: bool


@router.post("/api/hook/pause")
async def hook_pause(
    request: Request,
    body: HookPauseBody,
    x_hook_token: Annotated[str | None, Header(alias="X-Hook-Token")] = None,
):
    _require_webhook_token(x_hook_token)
    upsert_many_kv({"trading_paused": "true" if body.paused else "false"})
    bot = _trader(request)
    if bot:
        await bot._reload_settings_async()
    return {"ok": True, "trading_paused": bool(body.paused)}


@router.post("/api/hook/reload-settings")
async def hook_reload_settings(
    request: Request,
    x_hook_token: Annotated[str | None, Header(alias="X-Hook-Token")] = None,
):
    _require_webhook_token(x_hook_token)
    bot = _trader(request)
    if not bot:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    await bot._reload_settings_async()
    return {"ok": True}


@router.get("/api/hook/intents-preview")
async def hook_intents_preview(
    request: Request,
    agent: str = "all",
    limit: int = 80,
    x_hook_token: Annotated[str | None, Header(alias="X-Hook-Token")] = None,
):
    _require_webhook_token(x_hook_token)
    bot = _trader(request)
    if not bot or not getattr(bot, "_http", None):
        raise HTTPException(status_code=503, detail="Trader not initialized")
    return await _intents_preview_core(bot, agent, limit)


@router.get("/api/hook/telemetry")
async def hook_telemetry(
    request: Request,
    x_hook_token: Annotated[str | None, Header(alias="X-Hook-Token")] = None,
):
    _require_webhook_token(x_hook_token)
    bot = _trader(request)
    if not bot:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    st = bot.get_state_dict()
    statuses: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    for t in st.get("trade_history", []):
        s = str(t.get("status") or "unknown")
        statuses[s] = statuses.get(s, 0) + 1
        strat = str(t.get("strategy") or "").split(":", 1)[0]
        by_strategy[strat] = by_strategy.get(strat, 0) + 1
    return {
        "ok": True,
        "running": st.get("running", False),
        "mode": st.get("mode"),
        "started_at": st.get("started_at"),
        "last_scan": st.get("last_scan"),
        "last_trade": st.get("last_trade"),
        "balance_usdc": st.get("usdc_balance"),
        "portfolio_value": st.get("portfolio_value"),
        "open_orders_count": st.get("open_orders_count"),
        "markets_scanned": st.get("markets_scanned"),
        "trades_placed": st.get("trades_placed"),
        "trades_filled": st.get("trades_filled"),
        "consecutive_exec_failures": st.get("consecutive_exec_failures"),
        "rolling_notional_window_usd": st.get("rolling_notional_window_usd"),
        "status_counts": statuses,
        "strategy_counts": by_strategy,
        "agents_fired": st.get("agents_fired", []),
        "errors": st.get("errors", []),
    }


@router.get("/api/admin/telemetry")
async def admin_telemetry(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
):
    bot = _trader(request)
    if not bot:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    st = bot.get_state_dict()
    return {
        "ok": True,
        "running": st.get("running", False),
        "mode": st.get("mode"),
        "started_at": st.get("started_at"),
        "last_scan": st.get("last_scan"),
        "last_trade": st.get("last_trade"),
        "balance_usdc": st.get("usdc_balance"),
        "portfolio_value": st.get("portfolio_value"),
        "open_orders_count": st.get("open_orders_count"),
        "markets_scanned": st.get("markets_scanned"),
        "trades_placed": st.get("trades_placed"),
        "trades_filled": st.get("trades_filled"),
        "consecutive_exec_failures": st.get("consecutive_exec_failures"),
        "rolling_notional_window_usd": st.get("rolling_notional_window_usd"),
        "agents_fired": st.get("agents_fired", []),
        "errors": st.get("errors", []),
    }


@router.get("/api/admin/leaderboard")
async def admin_leaderboard(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
    category: str = "OVERALL",
    time_period: str = "MONTH",
    limit: int = 25,
):
    bot = _trader(request)
    http = getattr(bot, "_http", None) if bot else None
    if http is None:
        import httpx as _httpx
        http = _httpx.AsyncClient(timeout=30.0)
    try:
        entries = await fetch_leaderboard(
            http,
            category=category,
            time_period=time_period,
            limit=limit,
        )
        return {"ok": True, "category": category.upper(), "time_period": time_period.upper(), "entries": entries}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class LeaderboardImportBody(BaseModel):
    categories: list[str] = Field(default=["OVERALL"])
    time_period: str = Field(default="MONTH")
    limit_per_category: int = Field(default=25, ge=1, le=50)
    min_pnl: float = Field(default=0.0)
    min_win_rate: float = Field(default=0.60, ge=0.0, le=1.0)
    min_win_streak: int = Field(default=3, ge=0, le=100)
    min_total_trades: int = Field(default=5, ge=0, le=500)
    merge: bool = Field(default=True)


@router.post("/api/admin/leaderboard/import")
async def admin_leaderboard_import(
    body: LeaderboardImportBody,
    request: Request,
    _: Annotated[User, Depends(require_admin)],
):
    """Discover top wallets, analyze win rate + streak, import only qualified ones."""
    bot = _trader(request)
    http = getattr(bot, "_http", None) if bot else None
    if http is None:
        import httpx as _httpx
        http = _httpx.AsyncClient(timeout=30.0)

    qualified = await discover_qualified_wallets(
        http,
        categories=body.categories,
        time_period=body.time_period,
        limit_per_category=body.limit_per_category,
        min_pnl=body.min_pnl,
        min_win_rate=body.min_win_rate,
        min_win_streak=body.min_win_streak,
        min_total_trades=body.min_total_trades,
    )
    new_wallets = [w["wallet"] for w in qualified]
    if not new_wallets:
        return {"ok": True, "added": 0, "total": 0, "wallets": [], "qualified": qualified}

    existing: list[str] = []
    if body.merge:
        kv = load_all_kv()
        raw = kv.get("copy_watch_wallets", "[]")
        try:
            existing = json.loads(raw) if raw else []
        except Exception:
            existing = []
        if not isinstance(existing, list):
            existing = []

    merged_set: dict[str, bool] = {}
    for w in existing:
        merged_set[w.lower().strip()] = True
    added = 0
    for w in new_wallets:
        wl = w.lower().strip()
        if wl not in merged_set:
            merged_set[wl] = True
            added += 1

    final = list(merged_set.keys())
    upsert_many_kv({"copy_watch_wallets": json.dumps(final)})

    return {
        "ok": True,
        "added": added,
        "total": len(final),
        "wallets": final,
        "qualified": qualified,
    }


@router.get("/api/admin/wallet-quality")
async def admin_wallet_quality(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
    wallet: str = "",
):
    """Analyze a single wallet's closed positions for win rate and streaks."""
    w = wallet.strip().lower()
    if not w or not w.startswith("0x") or len(w) != 42:
        raise HTTPException(status_code=400, detail="Invalid wallet address")
    bot = _trader(request)
    http = getattr(bot, "_http", None) if bot else None
    if http is None:
        import httpx as _httpx
        http = _httpx.AsyncClient(timeout=30.0)
    quality = await analyze_wallet_quality(http, w)
    return {"ok": True, **quality}


@router.get("/api/admin/copy-manager")
async def admin_copy_manager_status(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
):
    bot = _trader(request)
    if not bot:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    mgr = getattr(bot, "_copy_manager", None)
    if not mgr:
        return {"ok": False, "error": "copy_manager not available"}
    return {
        "ok": True,
        "summary": mgr.get_summary(),
        "wallets": mgr.get_managed_wallets(),
    }


@router.post("/api/admin/copy-manager/refresh")
async def admin_copy_manager_refresh(
    request: Request,
    _: Annotated[User, Depends(require_admin)],
):
    """Force an immediate copy manager refresh (re-scan + prune)."""
    bot = _trader(request)
    if not bot:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    mgr = getattr(bot, "_copy_manager", None)
    http = getattr(bot, "_http", None)
    if not mgr or not http:
        raise HTTPException(status_code=503, detail="copy_manager or http not ready")
    result = await mgr.refresh(http)
    if result.get("added") or result.get("pruned"):
        from bot.settings import Settings
        bot.settings = Settings.load()
        bot._copy_agent.settings = bot.settings
    return {"ok": True, **result}
