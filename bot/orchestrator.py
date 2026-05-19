"""Multi-agent orchestrator: Gamma scan, CEX gate, risk, strict execution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional, Set

import httpx
from py_clob_client.client import ClobClient

from bot.agents.bundle_arb import BundleArbAgent
from bot.agents.copy_signal import CopySignalAgent
from bot.agents.latency_arb import LatencyArbAgent
from bot.agents.registry import agents_status
from bot.agents.value_edge import ValueEdgeAgent
from bot.agents.zscore_edge import ZScoreEdgeAgent
from bot.copy_manager import CopyManager
from bot.paper_portfolio import PaperPortfolio
from bot.categories import MarketCategory
from bot.cex import fetch_cex_bundle, infer_crypto_asset_from_text
from bot.execution import place_limit_gtd_then_wait, place_market_fok_fallback
from bot.gamma import scan_tradeable_markets
from bot.http_retry import get_json_retry
from bot.models import BotState, TradeIntent, TradeRecord, utc_now_iso
from bot.clob_utils import parse_midpoint
from bot.reconcile import reconcile_trade_records_inplace, snapshot_open_orders
from bot.db.kv import append_paper_trade_log, append_trade_log
from bot.db.models import TradeLog, session_scope
from bot.exposure import category_exposure_usd, condition_exposure_usd, rolling_notional_usd
from bot.execution_plan import plan_execution_units
from bot.market_intel import hours_until_resolution_end
from bot.orderbook import orderbook_buy_depth_ok, spread_mid_bps
from bot.risk import gate_intent
from bot.settings import Settings
from bot.signals import intent_signal_boost
from bot.sizing import pnl_aware_size_multiplier
from bot.structured_log import slog
from bot.validate import is_valid_polygon_address, is_valid_private_key_hex

log = logging.getLogger("polymarket.orchestrator")


class BalanceRefreshError(RuntimeError):
    """Raised by refresh_balance() when all configured RPCs fail to return
    a usable USDC balance for the current cycle.

    E2: callers (run_cycle) MUST catch this and abort the cycle — trading on
    a stale balance after a withdrawal or RPC outage is unsafe.
    """

    pass


class TradingBot:
    """Production-style bot with category toggles, agents, and GTD execution."""

    def __init__(self):
        self.settings = Settings.load()
        # E1: hard-fail boot when risk caps are zero unless operator opted in
        # via risk_caps_disabled=True. Runs BEFORE any trading-loop wiring so
        # the bot cannot enter run_forever with unbounded exposure by accident.
        self.settings.validate_risk_caps_at_startup()
        self.state = BotState(mode="dry_run" if self.settings.dry_run else "live")
        # E1: snapshot the caps actually enforced so the dashboard can render them.
        self.state.active_caps = self.settings.active_risk_caps()
        self.clob: Optional[ClobClient] = None
        self._running = False
        self._last_api = 0.0
        self._market_cache: dict[str, dict] = {}
        self._http: Optional[httpx.AsyncClient] = None

        self._value_agent = ValueEdgeAgent(self.settings)
        self._copy_agent = CopySignalAgent(self.settings)
        self._latency_agent = LatencyArbAgent(self.settings)
        self._bundle_agent = BundleArbAgent(self.settings)
        self._zscore_agent = ZScoreEdgeAgent(self.settings)
        self._copy_manager = CopyManager(self.settings)
        self._paper_portfolio = PaperPortfolio()
        # E14b: per-cycle token guard. Cleared at the top of run_cycle so
        # repeat-intent storms (multiple agents firing on the same token in
        # the same cycle) cannot place duplicate orders.
        self._cycle_inflight_tokens: set[str] = set()

        w = self.settings.wallet_address
        log.info(
            "TradingBot init mode=%s value=%s copy=%s lat=%s bundle=%s z=%s wallet=%s…",
            self.state.mode,
            self.settings.agent_value,
            self.settings.agent_copy,
            self.settings.agent_latency,
            self.settings.agent_bundle,
            self.settings.agent_zscore,
            (w[:12] + "…") if w else "(none)",
        )

    async def _reload_settings_async(self) -> None:
        def _run() -> Settings:
            return Settings.load()

        try:
            new_settings = await asyncio.to_thread(_run)
        except Exception as exc:
            log.error("_reload_settings_async failed, keeping old settings: %s", exc)
            return
        self.settings = new_settings
        self.state.mode = "dry_run" if self.settings.dry_run else "live"
        # Keep dashboard snapshot of enforced caps in sync with current settings.
        # Eval semantics in _advanced_gates_ok are unchanged (still cap>0 enables).
        self.state.active_caps = self.settings.active_risk_caps()
        self._value_agent.settings = self.settings
        self._copy_agent.settings = self.settings
        self._latency_agent.settings = self.settings
        self._bundle_agent.settings = self.settings
        self._zscore_agent.settings = self.settings
        self._copy_manager.sync_settings(self.settings)

    async def _rate_limit(self):
        gap = 0.35
        elapsed = time.monotonic() - self._last_api
        if elapsed < gap:
            await asyncio.sleep(gap - elapsed)
        self._last_api = time.monotonic()

    async def initialize(self) -> bool:
        if self.clob is not None:
            return True
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        if not self.settings.polymarket_private_key:
            if self.settings.dry_run:
                log.warning("DRY RUN without CLOB keys — scanning + paper trades only (set keys in Admin for full agent coverage)")
                self.state.errors = [e for e in self.state.errors if e != "No POLYMARKET_PRIVATE_KEY"]
                self.state.started_at = utc_now_iso()
                self.state.running = True
                return True
            self.state.errors.append("No POLYMARKET_PRIVATE_KEY")
            return False
        if not is_valid_private_key_hex(self.settings.polymarket_private_key):
            self.state.errors.append("Invalid POLYMARKET_PRIVATE_KEY format (expect 64 hex chars, optional 0x)")
            return False
        if not self.settings.dry_run:
            if self.settings.polymarket_signature_type == 1 and not is_valid_polygon_address(
                self.settings.wallet_address
            ):
                self.state.errors.append("Live trading with signature type 1 requires valid WALLET_ADDRESS (proxy/funder)")
                return False
            if self.settings.wallet_address and not is_valid_polygon_address(self.settings.wallet_address):
                self.state.errors.append("Invalid WALLET_ADDRESS format")
                return False
        try:
            st = self.settings.polymarket_signature_type
            funder = self.settings.wallet_address if st == 1 else None
            self.clob = ClobClient(
                host="https://clob.polymarket.com",
                key=self.settings.polymarket_private_key,
                chain_id=137,
                signature_type=st,
                funder=funder,
            )
            creds = self.clob.create_or_derive_api_creds()
            self.clob.set_api_creds(creds)
            log.info("CLOB L2 auth OK")
        except Exception as e:
            log.exception("CLOB init failed")
            self.state.errors.append(f"Init: {e}")
            return False

        # E3: rehydrate trade_history from DB BEFORE the first cycle so the
        # in-memory dedupe / rolling-notional window sees prior submits that
        # happened just before a crash. Reconcile only checks open CLOB
        # orders — it cannot recover trades that were persisted to DB but
        # lost from memory.
        try:
            await asyncio.to_thread(self.load_recent_trades)
        except Exception as e:
            log.warning("load_recent_trades failed on boot: %s", e)

        # E2: refresh_balance can raise BalanceRefreshError. On boot we
        # surface the failure but do not crash initialize — the next cycle
        # will retry and abort safely if RPCs are still dark.
        try:
            await self.refresh_balance()
        except BalanceRefreshError as e:
            log.warning("initial balance refresh failed: %s", e)
            self.state.errors.append("balance_refresh_failed")
        await self.refresh_positions()
        self.state.started_at = utc_now_iso()
        self.state.running = True
        return True

    def load_recent_trades(self, *, hours: float = 24.0, max_rows: int = 200) -> int:
        """E3: Populate state.trade_history from the trade_logs DB table on
        boot so dedupe and rolling-notional accounting survive a restart.

        Bounded by (last `hours`) AND `max_rows`, whichever is smaller, so we
        never load an unbounded backlog. Returns the number of records
        loaded for caller visibility / tests.

        Inline query (kept here per Q1 scope: do NOT add new DB modules).
        """
        import datetime as dt

        from sqlalchemy import select

        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=float(hours))
        loaded: list[TradeRecord] = []
        try:
            with session_scope() as s:
                q = (
                    select(TradeLog)
                    .where(TradeLog.created_at >= cutoff)
                    .order_by(TradeLog.id.desc())
                    .limit(int(max_rows))
                )
                rows = list(s.scalars(q).all())
                # Re-sort ascending so trade_history reads oldest-first, the
                # same invariant the live append path maintains.
                rows.reverse()
                for r in rows:
                    ts = r.created_at.isoformat() if r.created_at else utc_now_iso()
                    loaded.append(
                        TradeRecord(
                            order_id=str(r.order_id or ""),
                            market_question=str(r.market_question or ""),
                            condition_id=str(r.condition_id or ""),
                            token_id=str(r.token_id or ""),
                            side=str(r.side or ""),
                            price=float(r.price or 0.0),
                            size=float(r.size or 0.0),
                            cost_usd=float(r.cost_usd or 0.0),
                            status=str(r.status or ""),
                            timestamp=ts,
                            outcome=str(r.outcome or ""),
                            strategy=str(r.strategy or ""),
                            reconcile_note=r.reconcile_note,
                        )
                    )
        except Exception as e:
            log.warning("load_recent_trades query failed: %s", e)
            return 0
        self.state.trade_history = loaded
        log.info("load_recent_trades: rehydrated %d trades from DB (window=%.1fh, cap=%d)",
                 len(loaded), float(hours), int(max_rows))
        return len(loaded)

    async def refresh_balance(self):
        """Refresh USDC balance from Polygon RPCs.

        E2: If *all* configured RPCs fail (or return no usable balance) we
        raise BalanceRefreshError so the caller can abort the cycle rather
        than trading on a stale `state.usdc_balance` (which would be
        catastrophic after a withdrawal or RPC outage). A single RPC failure
        with a working fallback still succeeds — we only escalate when the
        whole pool is dark.
        """
        if not self.settings.wallet_address or not self._http:
            if self.settings.dry_run and self.state.usdc_balance <= 0:
                self.state.usdc_balance = self.settings.default_bet_usd * 100
                log.info("DRY RUN: no wallet configured, using simulated balance $%.2f", self.state.usdc_balance)
            # Treat "no wallet configured" as a non-failure path: the dry-run
            # simulated balance is authoritative, so stamp it so observers see
            # the refresh succeeded for this cycle.
            self.state.balance_refreshed_at = time.time()
            return
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {
                    "to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                    "data": "0x70a08231000000000000000000000000"
                    + self.settings.wallet_address[2:],
                },
                "latest",
            ],
        }
        rpc_urls = (
            "https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
        )
        last_error: Optional[str] = None
        for url in rpc_urls:
            try:
                r = await self._http.post(url, json=payload)
                res = r.json().get("result", "0x0")
                if res and res != "0x":
                    self.state.usdc_balance = int(res, 16) / 1e6
                    self.state.balance_refreshed_at = time.time()
                    return
                # RPC responded but with an empty/invalid balance — record
                # and let the next RPC try.
                last_error = f"{url}:empty_result"
            except Exception as e:
                last_error = f"{url}:{type(e).__name__}"
                continue
        # All RPCs failed (raised or returned no usable balance). Do NOT
        # silently continue with stale state.usdc_balance — surface to caller.
        raise BalanceRefreshError(f"dual_rpc_failure:{last_error or 'unknown'}")

    def _find_market_by_token(self, token_id: str) -> Optional[dict]:
        for _, market in self._market_cache.items():
            tokens = market.get("clobTokenIds", market.get("clob_token_ids", ""))
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens) if tokens.startswith("[") else [tokens]
                except json.JSONDecodeError:
                    continue
            if token_id not in tokens:
                continue
            idx = tokens.index(token_id)
            outcomes = market.get("outcomes", '["Yes", "No"]')
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = ["Yes", "No"]
            on = outcomes[idx] if idx < len(outcomes) else "Unknown"
            return {**market, "outcome_name": on}
        return None

    async def refresh_positions(self):
        if not self.settings.wallet_address or not self._http:
            return
        w = self.settings.wallet_address.lower()
        raw: list = []
        try:
            j = await get_json_retry(
                self._http,
                "https://data-api.polymarket.com/positions",
                params={"user": w, "sizeThreshold": "0.01"},
            )
            raw = j if isinstance(j, list) else []
        except Exception as e:
            log.warning("positions API: %s", e)
            return

        if not self._market_cache and self.clob:
            await self._gamma_scan()

        positions = []
        for pos in raw:
            asset = pos.get("asset")
            if isinstance(asset, dict):
                token_id = asset.get("token_id", "") or asset.get("tokenId", "")
            elif isinstance(asset, str) and len(asset) > 20:
                token_id = asset
            else:
                token_id = pos.get("tokenId", "") or pos.get("token_id", "")
            if not token_id:
                continue
            size = float(pos.get("size", 0) or 0)
            if size <= 0.01:
                continue
            avg_price = float(pos.get("avgPrice", pos.get("avg_price", 0)) or 0)
            cur_price = float(pos.get("curPrice", pos.get("current_price", 0)) or 0)
            mi = self._find_market_by_token(token_id)
            market_name = pos.get("title", pos.get("question", "")) or (
                mi.get("question", "") if mi else ""
            )
            outcome = pos.get("outcome", "") or (mi.get("outcome_name", "") if mi else "")
            if self.clob and cur_price <= 0:
                await self._rate_limit()
                try:
                    mid = self.clob.get_midpoint(token_id=token_id)
                    parsed = parse_midpoint(mid)
                    if parsed is not None:
                        cur_price = float(parsed)
                    elif isinstance(mid, dict):
                        cur_price = float(mid.get("mid", 0) or 0)
                    else:
                        cur_price = 0.0
                except Exception:
                    cur_price = avg_price
            if cur_price <= 0:
                cur_price = avg_price
            pnl = (cur_price - avg_price) * size
            value = cur_price * size
            cid_pos = ""
            if mi:
                cid_pos = str(mi.get("condition_id") or mi.get("conditionId") or "")
            positions.append(
                {
                    "token_id": token_id,
                    "condition_id": cid_pos,
                    "market": market_name or token_id[:16],
                    "outcome": outcome,
                    "side": pos.get("side", "BUY"),
                    "size": round(size, 4),
                    "avg_price": round(avg_price, 4),
                    "current_price": round(cur_price, 4),
                    "value": round(value, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round((pnl / (avg_price * size)) * 100, 2)
                    if avg_price * size > 0
                    else 0,
                }
            )
        self.state.positions = positions
        self.state.portfolio_value = sum(p["value"] for p in positions)
        self.state.total_pnl = sum(p["pnl"] for p in positions)

    async def refresh_open_orders(self) -> None:
        if not self.clob:
            return
        await self._rate_limit()

        def _run() -> list[dict[str, Any]]:
            return snapshot_open_orders(
                self.clob,
                display_limit=self.settings.open_orders_display_limit,
            )

        try:
            rows = await asyncio.to_thread(_run)
            for row in rows:
                tid = row.get("token_id")
                if tid:
                    row["condition_id"] = self._condition_id_for_token(str(tid))
            self.state.open_orders = rows
        except Exception as e:
            log.warning("open_orders: %s", e)

    def _condition_id_for_token(self, token_id: str) -> str:
        tid = (token_id or "").strip()
        if not tid or not self._market_cache:
            return ""
        for cid, m in self._market_cache.items():
            toks = m.get("clobTokenIds", m.get("clob_token_ids", ""))
            if isinstance(toks, str):
                try:
                    toks = json.loads(toks) if toks.startswith("[") else [toks]
                except json.JSONDecodeError:
                    continue
            if tid in toks:
                return str(cid)
        return ""

    async def force_reconcile(self) -> dict[str, Any]:
        """Refresh open orders + poll recent trade rows (for manual / API trigger)."""
        if not self.clob:
            return {"ok": False, "error": "no_clob"}
        try:
            await self.refresh_open_orders()
            n = await asyncio.to_thread(
                reconcile_trade_records_inplace,
                self.clob,
                self.state.trade_history,
                depth=self._bounded_reconcile_depth(),
                sleep_between_s=self.settings.reconcile_poll_sleep_s,
            )
            self.state.reconcile_updates_last = n
            self.state.last_reconcile_at = utc_now_iso()
            return {
                "ok": True,
                "updated": n,
                "open_orders": len(self.state.open_orders),
                "last_reconcile_at": self.state.last_reconcile_at,
            }
        except Exception as e:
            log.warning("force_reconcile: %s", e)
            return {"ok": False, "error": str(e)}

    async def _gamma_scan(self) -> list[dict]:
        assert self._http is not None
        markets, cache = await scan_tradeable_markets(
            self._http,
            self._rate_limit,
            max_pages=2,
            min_liquidity=self.settings.min_clob_liquidity_usd,
            min_volume=self.settings.min_gamma_volume,
        )
        self._market_cache = cache
        self.state.markets_scanned = len(markets)
        self.state.last_scan = utc_now_iso()
        return markets

    async def _cex_map_for_intents(self, intents: list[TradeIntent]) -> dict[str, Optional[float]]:
        """asset -> dispersion_bps (None = skip gate)."""
        out: dict[str, Optional[float]] = {}
        assets: Set[str] = set()
        for it in intents:
            if it.category not in (MarketCategory.CRYPTO_SHORT, MarketCategory.CRYPTO_OTHER):
                continue
            a = infer_crypto_asset_from_text(it.question)
            if a:
                assets.add(a)
        for a in assets:
            bundle = await fetch_cex_bundle(a)
            out[a] = bundle.get("dispersion_bps")
            self.state.cex_snapshot[a] = bundle
        return out

    def _dispersion_for_intent(
        self, it: TradeIntent, cex_map: dict[str, Optional[float]]
    ) -> Optional[float]:
        if it.category not in (MarketCategory.CRYPTO_SHORT, MarketCategory.CRYPTO_OTHER):
            return None
        a = infer_crypto_asset_from_text(it.question)
        if not a:
            return None
        return cex_map.get(a)

    async def _apply_intent_multipliers(self, intent: TradeIntent) -> None:
        mult = 1.0
        if self.settings.pnl_sizing_enabled:
            mult *= await asyncio.to_thread(
                pnl_aware_size_multiplier,
                window=int(self.settings.pnl_sizing_window),
            )
        if self.settings.signals_enabled:
            sm, snote = intent_signal_boost(intent.question)
            mult *= sm
            if snote:
                intent.reason = f"{intent.reason};{snote}"
        intent.size_usd = max(
            self.settings.min_bet_usd,
            min(self.settings.max_bet_usd, float(intent.size_usd) * mult),
        )

    async def _orderbook_gate_passes(self, intent: TradeIntent) -> bool:
        if self.clob is None or not self.settings.orderbook_gate_enabled or intent.side.upper() != "BUY":
            return True
        share = float(self.settings.orderbook_min_bid_share)
        ok_book = await asyncio.to_thread(
            orderbook_buy_depth_ok,
            self.clob,
            intent.token_id,
            share,
        )
        if not ok_book:
            log.info("skip intent: orderbook bid share < %.2f (%s)", share, intent.strategy)
            slog(
                log,
                self.settings.structured_log,
                "intent_skipped",
                strategy=intent.strategy,
                agent=intent.agent,
                reason="orderbook_imbalance",
            )
        return ok_book

    async def _advanced_gates_ok(
        self,
        legs: list[TradeIntent],
        *,
        markets_by_cid: dict[str, dict[str, Any]],
        rolling_notional: float,
        condition_extra_usd: dict[str, float] | None = None,
        category_extra_usd: dict[str, float] | None = None,
    ) -> tuple[bool, str]:
        """Spread, resolution timing, per-condition exposure, rolling daily notional."""
        if not legs:
            return True, "ok"
        total_new = sum(float(x.size_usd) for x in legs)
        cap_d = float(self.settings.max_daily_notional_usd)
        if cap_d > 0 and rolling_notional + total_new > cap_d:
            return (
                False,
                f"daily_notional_{rolling_notional + total_new:.0f}_gt_{cap_d:.0f}",
            )
        cap_c = float(self.settings.max_condition_exposure_usd)
        if cap_c > 0:
            cid = (legs[0].condition_id or "").strip()
            if cid:
                cur = condition_exposure_usd(
                    cid,
                    positions=self.state.positions,
                    open_orders=self.state.open_orders,
                )
                if condition_extra_usd:
                    cur += float(condition_extra_usd.get(cid, 0.0) or 0.0)
                if cur + total_new > cap_c:
                    return False, f"condition_exposure_{cur:.0f}_plus_{total_new:.0f}_gt_{cap_c:.0f}"
        # Category exposure cap (global and/or per-category override)
        cat_map: dict[str, str] = {}
        for cid, m in markets_by_cid.items():
            c = m.get("category")
            cval = getattr(c, "value", c)
            cat_map[str(cid)] = str(cval or "").lower()
        cat_new: dict[str, float] = {}
        for it in legs:
            c = cat_map.get(str(it.condition_id or ""), str(it.category.value)).lower()
            if c:
                cat_new[c] = cat_new.get(c, 0.0) + float(it.size_usd)
        if cat_new:
            global_cap = float(getattr(self.settings, "max_category_exposure_usd", 0.0) or 0.0)
            # E14d: normalize cap keys defensively. Settings.from_kv already
            # lower-cases keys, but operators editing the DB directly can
            # introduce upper-case overrides (e.g. "SPORTS"). Normalise both
            # sides so a cap row never silently no-ops.
            raw_over_caps = dict(getattr(self.settings, "category_exposure_caps", {}) or {})
            over_caps = {str(k).strip().lower(): v for k, v in raw_over_caps.items()}
            for c, add_u in cat_new.items():
                c_key = str(c).strip().lower()
                cap = float(over_caps.get(c_key, 0.0) or 0.0)
                if cap <= 0:
                    cap = global_cap
                if cap <= 0:
                    continue
                cur = category_exposure_usd(
                    c,
                    positions=self.state.positions,
                    open_orders=self.state.open_orders,
                    categories_by_condition=cat_map,
                )
                if category_extra_usd:
                    cur += float(category_extra_usd.get(c, 0.0) or 0.0)
                if cur + float(add_u) > cap:
                    return False, f"category_exposure_{c}_{cur:.0f}_plus_{add_u:.0f}_gt_{cap:.0f}"
        for it in legs:
            if self.settings.resolution_gate_enabled and float(self.settings.min_hours_to_resolution) > 0:
                m = markets_by_cid.get(it.condition_id) or {}
                hr = hours_until_resolution_end(m)
                if hr is not None and hr < float(self.settings.min_hours_to_resolution):
                    return False, f"resolution_in_{hr:.1f}h_lt_min_{self.settings.min_hours_to_resolution}h"
            if self.settings.spread_gate_enabled and self.clob and it.side.upper() == "BUY":
                bps = await asyncio.to_thread(spread_mid_bps, self.clob, it.token_id)
                if bps is not None and bps > float(self.settings.max_spread_bps):
                    return False, f"spread_{bps:.0f}_bps_gt_{self.settings.max_spread_bps}"
        return True, "ok"

    # E14e: cap reconcile depth at a safety floor — operators occasionally
    # configure a huge value that causes the per-cycle reconcile call to
    # spam the CLOB. 200 is the documented maximum reconcile lookback.
    _RECONCILE_DEPTH_MAX = 200

    def _bounded_reconcile_depth(self) -> int:
        return min(int(self.settings.reconcile_history_depth or 0), self._RECONCILE_DEPTH_MAX)

    def _note_exec_result(self, ok: bool, *, is_dry_run: bool = False) -> None:
        # E14a: do NOT reset the live circuit breaker on a dry-run success.
        # Otherwise a string of paper trades would silently clear failures
        # accrued against the live exchange, defeating the safety stop.
        if ok:
            if not is_dry_run:
                self.state.consecutive_exec_failures = 0
        else:
            self.state.consecutive_exec_failures = int(self.state.consecutive_exec_failures or 0) + 1

    async def _unwind_bundle_leg(self, leg: TradeIntent, fill_info: Any) -> None:
        """E4: Unwind a surviving bundle leg whose partner failed.

        First attempt: cancel the leg's outstanding order. If the order is
        already filled (or cancel fails), place an offsetting market order on
        the opposite side sized to the filled amount.

        On success, append `bundle_unwound:order_id=<oid>` to state.errors so
        operators see the exceptional event. On failure, append
        `bundle_unwind_failed:order_id=<oid>`, log at ERROR, and trip the
        circuit breaker via `consecutive_exec_failures` AND set
        `state.unwind_failure_critical = True` so the dashboard surfaces it.

        Behaviour is only invoked from the bundle branch when the
        `bundle_auto_unwind_enabled` flag is True — caller guards this.
        """
        oid = getattr(fill_info, "order_id", None) or "unknown"
        filled_size = float(getattr(fill_info, "size", 0.0) or 0.0)
        status = str(getattr(fill_info, "status", "") or "")
        log.info(
            "bundle_unwind start oid=%s token=%s side=%s filled_size=%.4f status=%s",
            str(oid)[:24], str(leg.token_id)[:12], leg.side, filled_size, status,
        )

        cancel_ok = False
        cancel_err: Optional[str] = None
        # Step 1: cancel the resting order (if any). Filled/closed statuses
        # short-circuit straight to the offset path.
        if self.clob is not None and oid and oid != "unknown" and status not in (
            "filled", "dry_run_filled", "market_fok", "closed"
        ):
            try:
                res = await asyncio.to_thread(self.clob.cancel, oid)
                # CLOB's cancel may return either a dict or a primitive ack;
                # treat absence-of-error as success, but inspect for an
                # explicit "already filled" signal to fall through to offset.
                already = False
                if isinstance(res, dict):
                    nlow = json.dumps(res).lower()
                    already = "filled" in nlow and "already" in nlow
                if already:
                    cancel_err = "already_filled"
                else:
                    cancel_ok = True
                    log.info("bundle_unwind cancel ok oid=%s", str(oid)[:24])
            except Exception as e:
                cancel_err = f"{type(e).__name__}:{e}"
                log.info("bundle_unwind cancel failed oid=%s err=%s", str(oid)[:24], cancel_err)
        else:
            cancel_err = "no_clob_or_already_filled"

        if cancel_ok:
            self.state.errors.append(f"bundle_unwound:order_id={oid}")
            slog(
                log,
                self.settings.structured_log,
                "bundle_unwound",
                order_id=str(oid)[:24],
                strategy=leg.strategy,
                action="cancel",
            )
            return

        # Step 2: cancel did not work (or the order had already filled). Place
        # an offsetting market order opposite the original side, sized to the
        # filled amount in shares (notional ≈ filled_size * leg.max_price).
        opp_side = "SELL" if leg.side.upper() == "BUY" else "BUY"
        # Estimate USD using the recorded fill price when available; otherwise
        # fall back to the leg's limit price.
        ref_price = float(getattr(fill_info, "price", 0.0) or leg.max_price or 0.0)
        amount_usd = max(0.0, filled_size * ref_price) if ref_price > 0 else float(leg.size_usd)

        try:
            oid2, note2 = await place_market_fok_fallback(
                self.clob,
                token_id=leg.token_id,
                side=opp_side,
                amount_usd=amount_usd,
                dry_run=self.settings.dry_run,
            )
        except Exception as e:
            oid2, note2 = None, f"offset_exception:{type(e).__name__}:{e}"

        if oid2 and not str(note2).startswith("market_fok_failed"):
            self.state.errors.append(f"bundle_unwound:order_id={oid}")
            log.info(
                "bundle_unwind offset ok oid=%s offset_oid=%s note=%s",
                str(oid)[:24], str(oid2)[:24], str(note2)[:120],
            )
            slog(
                log,
                self.settings.structured_log,
                "bundle_unwound",
                order_id=str(oid)[:24],
                offset_order_id=str(oid2)[:24],
                strategy=leg.strategy,
                action="offset",
            )
            return

        # Step 3: both cancel and offset failed. Trip the circuit breaker
        # and surface a critical flag so the operator must intervene.
        self.state.errors.append(f"bundle_unwind_failed:order_id={oid}")
        try:
            setattr(self.state, "unwind_failure_critical", True)
        except Exception:
            pass
        cb_max = int(getattr(self.settings, "circuit_breaker_max_fails", 0) or 0)
        if cb_max > 0:
            self.state.consecutive_exec_failures = cb_max + 1
        log.error(
            "bundle_unwind FAILED oid=%s cancel_err=%s offset_note=%s — leg unhedged, operator action required",
            str(oid)[:24], cancel_err, str(note2)[:200],
        )
        slog(
            log,
            self.settings.structured_log,
            "bundle_unwind_failed",
            order_id=str(oid)[:24],
            strategy=leg.strategy,
            cancel_err=str(cancel_err)[:120],
            offset_note=str(note2)[:120],
        )

    async def run_cycle(self):
        if not self._http:
            return
        if not self.clob and not self.settings.dry_run:
            return
        log.info("——— cycle start ———")
        # E14b/g: reset per-cycle guards before any settings reload / scan.
        self._cycle_inflight_tokens = set()
        try:
            self.state.reserved_usdc = 0.0
        except Exception:
            pass
        await self._reload_settings_async()
        slog(
            log,
            self.settings.structured_log,
            "cycle_start",
            paused=self.settings.trading_paused,
            dry_run=self.settings.dry_run,
        )
        if self.settings.trading_paused:
            log.info("TRADING_PAUSED: skipping cycle")
            return

        if self._http and self._copy_manager.needs_refresh():
            try:
                result = await self._copy_manager.refresh(self._http)
                if result.get("added") or result.get("pruned"):
                    await self._reload_settings_async()
                    log.info(
                        "CopyManager auto-refresh: +%d added, -%d pruned, %d active",
                        result.get("added", 0), result.get("pruned", 0), result.get("active", 0),
                    )
            except Exception as e:
                log.warning("CopyManager refresh error: %s", e)

        # E2: Dual-RPC balance failure must abort the cycle so we do not
        # trade on a stale phantom balance after a withdrawal / RPC outage.
        try:
            await self.refresh_balance()
        except BalanceRefreshError as e:
            log.warning("balance_refresh_failed: %s — aborting cycle", e)
            self.state.errors.append("balance_refresh_failed")
            slog(
                log,
                self.settings.structured_log,
                "balance_refresh_failed",
                reason=str(e)[:200],
            )
            return
        await self.refresh_positions()

        reserve = max(0.0, float(self.settings.balance_buffer_usd))
        if self.clob and self.state.usdc_balance < self.settings.min_bet_usd + reserve:
            log.warning("Balance below min bet + buffer")
            return

        markets = await self._gamma_scan()
        pos_tokens = {p["token_id"] for p in self.state.positions}

        agent_tasks: list[tuple[str, asyncio.Task]] = []
        if self.settings.agent_value and self.clob:
            agent_tasks.append(("value_edge", asyncio.ensure_future(
                self._value_agent.propose(self.clob, markets, pos_tokens, self._rate_limit))))
        if self.settings.agent_latency and self.clob:
            agent_tasks.append(("latency_arb", asyncio.ensure_future(
                self._latency_agent.propose(self.clob, markets, pos_tokens, self._rate_limit))))
        if self.settings.agent_bundle and self.clob:
            agent_tasks.append(("bundle_arb", asyncio.ensure_future(
                self._bundle_agent.propose(self.clob, markets, pos_tokens, self._rate_limit))))
        if self.settings.agent_zscore and self.clob:
            agent_tasks.append(("zscore_edge", asyncio.ensure_future(
                self._zscore_agent.propose(self.clob, markets, pos_tokens, self._rate_limit))))
        copy_scheduled = bool(self.settings.agent_copy and self.settings.copy_watch_wallets)
        if copy_scheduled:
            agent_tasks.append(("copy_signal", asyncio.ensure_future(
                self._copy_agent.propose(self._http, state=self.state))))

        tasks = [t for _, t in agent_tasks]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        intents: list[TradeIntent] = []

        runtime: dict[str, dict[str, Any]] = {}
        scheduled_ids = {aid for aid, _ in agent_tasks}
        for aid in ("value_edge", "latency_arb", "bundle_arb", "zscore_edge", "copy_signal"):
            runtime[aid] = {"scheduled": aid in scheduled_ids, "ran": False, "intents": 0, "note": ""}

        if not self.clob:
            for aid in ("value_edge", "latency_arb", "bundle_arb", "zscore_edge"):
                flag = {"value_edge": "agent_value", "latency_arb": "agent_latency",
                        "bundle_arb": "agent_bundle", "zscore_edge": "agent_zscore"}.get(aid, "")
                if getattr(self.settings, flag, False) and aid not in scheduled_ids:
                    runtime[aid]["note"] = "enabled but skipped — no CLOB keys (set polymarket_private_key in Admin)"

        for (aid, _task), r in zip(agent_tasks, results):
            if isinstance(r, Exception):
                log.error("agent error (%s): %s", aid, r)
                self.state.errors.append(f"{aid}: {r}")
                runtime[aid]["ran"] = False
                runtime[aid]["note"] = f"error: {r}"
                continue
            runtime[aid]["ran"] = True
            runtime[aid]["intents"] = len(r)
            intents.extend(r)

        if copy_scheduled:
            cold_note = "cold_start" if self._copy_agent.is_cold_start else ""
            agent_note = getattr(self._copy_agent, "last_note", "")
            note = cold_note or agent_note
            runtime["copy_signal"]["note"] = note
        elif self.settings.agent_copy and not self.settings.copy_watch_wallets:
            runtime["copy_signal"]["note"] = "enabled but no wallets configured"

        self.state.cycle_agent_runtime = runtime
        for aid, info in runtime.items():
            if info["scheduled"]:
                slog(
                    log,
                    self.settings.structured_log,
                    "agent_result",
                    agent=aid,
                    ran=info["ran"],
                    intents=info["intents"],
                    note=info["note"][:200] if info["note"] else "",
                )

        intents.sort(key=lambda x: -x.priority)

        # Phase 2: enrich intents with hours_to_resolution from market cache
        if intents and self._market_cache:
            for it in intents:
                if it.hours_to_resolution is not None:
                    continue
                m = self._market_cache.get(it.condition_id)
                if m:
                    hr = hours_until_resolution_end(m)
                    if hr is not None:
                        it.hours_to_resolution = hr

        cex_map = await self._cex_map_for_intents(intents) if intents else {}

        self.state.last_intents = [
            {
                "agent": i.agent,
                "priority": i.priority,
                "strategy": i.strategy,
                "category": i.category.value,
                "question": i.question[:80],
                "max_price": i.max_price,
                "usd": i.size_usd,
            }
            for i in intents[:30]
        ]
        self.state.agents_fired = list({i.agent for i in intents})
        skipped: list[dict[str, Any]] = []

        cb_max = int(self.settings.circuit_breaker_max_fails or 0)
        skip_placements = cb_max > 0 and self.state.consecutive_exec_failures >= cb_max
        if skip_placements:
            log.warning(
                "circuit_breaker: skipping placements (failures=%s >= %s)",
                self.state.consecutive_exec_failures,
                cb_max,
            )
            slog(
                log,
                self.settings.structured_log,
                "circuit_breaker",
                failures=self.state.consecutive_exec_failures,
                max_fails=cb_max,
            )
            placed = 0
        else:
            await self.refresh_open_orders()
            markets_by_cid: dict[str, dict[str, Any]] = {
                str(m.get("condition_id") or ""): m for m in markets if m.get("condition_id")
            }
            rolling_n = rolling_notional_usd(
                self.state.trade_history,
                hours=float(self.settings.daily_notional_window_hours or 24.0),
            )
            condition_extra: dict[str, float] = {}
            category_extra: dict[str, float] = {}

            units = plan_execution_units(intents)
            placed = 0
            for unit in units:
                if placed >= self.settings.max_trades_per_cycle:
                    break
                if len(unit) == 2:
                    a, b = unit
                    await self._apply_intent_multipliers(a)
                    await self._apply_intent_multipliers(b)
                    da = self._dispersion_for_intent(a, cex_map)
                    db = self._dispersion_for_intent(b, cex_map)
                    ok_a, ra = gate_intent(a, self.settings, da)
                    ok_b, rb = gate_intent(b, self.settings, db)
                    if not ok_a or not ok_b:
                        log.info("skip bundle: %s / %s (%s)", ra, rb, a.strategy)
                        slog(
                            log,
                            self.settings.structured_log,
                            "intent_skipped",
                            strategy=f"{a.strategy}+{b.strategy}",
                            agent=a.agent,
                            reason=f"bundle_gate:{ra}/{rb}",
                        )
                        skipped.append({"agent": a.agent, "strategy": f"{a.strategy}+{b.strategy}", "question": a.question[:80], "reason": f"bundle_gate:{ra}/{rb}"})
                        continue
                    if not await self._orderbook_gate_passes(a):
                        skipped.append({"agent": a.agent, "strategy": a.strategy, "question": a.question[:80], "reason": "orderbook_imbalance"})
                        continue
                    if not await self._orderbook_gate_passes(b):
                        skipped.append({"agent": b.agent, "strategy": b.strategy, "question": b.question[:80], "reason": "orderbook_imbalance"})
                        continue
                    adv_ok, adv_r = await self._advanced_gates_ok(
                        [a, b],
                        markets_by_cid=markets_by_cid,
                        rolling_notional=rolling_n,
                        condition_extra_usd=condition_extra,
                        category_extra_usd=category_extra,
                    )
                    if not adv_ok:
                        log.info("skip bundle: %s", adv_r)
                        slog(
                            log,
                            self.settings.structured_log,
                            "intent_skipped",
                            strategy=f"{a.strategy}+{b.strategy}",
                            agent=a.agent,
                            reason=adv_r,
                        )
                        skipped.append({"agent": a.agent, "strategy": f"{a.strategy}+{b.strategy}", "question": a.question[:80], "reason": adv_r})
                        continue
                    need = a.size_usd + b.size_usd + reserve
                    # E14g: subtract any in-flight reservations from this cycle.
                    effective_balance = self.state.usdc_balance - float(
                        getattr(self.state, "reserved_usdc", 0.0) or 0.0
                    )
                    if effective_balance < need:
                        log.info(
                            "skip bundle: insufficient balance (need %.2f incl. buffer, effective %.2f)",
                            need, effective_balance,
                        )
                        continue
                    ok1 = await self._execute_intent(a)
                    if not ok1:
                        self._note_exec_result(False, is_dry_run=self.settings.dry_run)
                        continue
                    rolling_n += float(a.size_usd)
                    if a.condition_id:
                        condition_extra[a.condition_id] = condition_extra.get(a.condition_id, 0.0) + float(a.size_usd)
                    acat = str(a.category.value).lower()
                    category_extra[acat] = category_extra.get(acat, 0.0) + float(a.size_usd)
                    ok2 = await self._execute_intent(b)
                    self._note_exec_result(bool(ok2), is_dry_run=self.settings.dry_run)
                    if ok2:
                        rolling_n += float(b.size_usd)
                        if b.condition_id:
                            condition_extra[b.condition_id] = condition_extra.get(b.condition_id, 0.0) + float(
                                b.size_usd
                            )
                        bcat = str(b.category.value).lower()
                        category_extra[bcat] = category_extra.get(bcat, 0.0) + float(b.size_usd)
                        placed += 1
                    else:
                        log.warning("bundle partial: second leg failed after first submitted")
                        self.state.errors.append("bundle_partial_second_failed")
                        # E4: leg A is live and unhedged. When the flag is ON
                        # we cancel-or-offset the surviving leg. Default OFF
                        # so live behaviour is unchanged until operators flip
                        # the flag — see Settings.bundle_auto_unwind_enabled.
                        if getattr(self.settings, "bundle_auto_unwind_enabled", False):
                            fill_info_a = (
                                self.state.trade_history[-1]
                                if self.state.trade_history
                                else None
                            )
                            await self._unwind_bundle_leg(a, fill_info_a)
                    continue

                intent = unit[0]
                await self._apply_intent_multipliers(intent)
                disp = self._dispersion_for_intent(intent, cex_map)
                if not await self._orderbook_gate_passes(intent):
                    skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": "orderbook_imbalance"})
                    continue
                ok, reason = gate_intent(intent, self.settings, disp)
                if not ok:
                    log.info("skip intent: %s (%s)", intent.strategy, reason)
                    slog(
                        log,
                        self.settings.structured_log,
                        "intent_skipped",
                        strategy=intent.strategy,
                        agent=intent.agent,
                        reason=reason,
                    )
                    skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": reason})
                    continue
                adv_ok, adv_r = await self._advanced_gates_ok(
                    [intent],
                    markets_by_cid=markets_by_cid,
                    rolling_notional=rolling_n,
                    condition_extra_usd=condition_extra,
                    category_extra_usd=category_extra,
                )
                if not adv_ok:
                    log.info("skip intent: %s (%s)", intent.strategy, adv_r)
                    slog(
                        log,
                        self.settings.structured_log,
                        "intent_skipped",
                        strategy=intent.strategy,
                        agent=intent.agent,
                        reason=adv_r,
                    )
                    skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": adv_r})
                    continue
                need = intent.size_usd + reserve
                effective_balance = self.state.usdc_balance - float(
                    getattr(self.state, "reserved_usdc", 0.0) or 0.0
                )
                if effective_balance < need:
                    log.info(
                        "skip: insufficient balance (need %.2f incl. buffer, effective %.2f)",
                        need, effective_balance,
                    )
                    continue

                ok_ex = await self._execute_intent(intent)
                self._note_exec_result(ok_ex, is_dry_run=self.settings.dry_run)
                if ok_ex:
                    rolling_n += float(intent.size_usd)
                    if intent.condition_id:
                        condition_extra[intent.condition_id] = condition_extra.get(intent.condition_id, 0.0) + float(
                            intent.size_usd
                        )
                    ccat = str(intent.category.value).lower()
                    category_extra[ccat] = category_extra.get(ccat, 0.0) + float(intent.size_usd)
                    placed += 1

        self.state.last_skipped_intents = skipped[:30]

        if self.settings.dry_run and self._paper_portfolio.get_positions():
            try:
                await self._paper_portfolio.refresh_prices(self._http, self.clob)
            except Exception as e:
                log.debug("paper price refresh: %s", e)
            paper = self._paper_portfolio.get_summary()
            self.state.positions = self._paper_portfolio.get_positions()
            self.state.portfolio_value = paper["portfolio_value"]
            self.state.total_pnl = paper["unrealized_pnl"]

        self.state.errors = self.state.errors[-25:]
        log.info("——— cycle end placed=%s ———", placed)
        slog(
            log,
            self.settings.structured_log,
            "cycle_end",
            placed=placed,
            markets_scanned=self.state.markets_scanned,
            balance=round(self.state.usdc_balance, 2),
        )

        if self.clob and self.settings.reconcile_enabled:
            try:
                await self.refresh_open_orders()
                n = await asyncio.to_thread(
                    reconcile_trade_records_inplace,
                    self.clob,
                    self.state.trade_history,
                    depth=self._bounded_reconcile_depth(),
                    sleep_between_s=self.settings.reconcile_poll_sleep_s,
                )
                self.state.reconcile_updates_last = n
                self.state.last_reconcile_at = utc_now_iso()
                slog(
                    log,
                    self.settings.structured_log,
                    "reconcile_done",
                    updated=n,
                    open_orders=len(self.state.open_orders),
                )
            except Exception as e:
                log.warning("reconcile: %s", e)
                self.state.errors.append(f"reconcile:{e}")

    def _resolve_fill_price(
        self,
        *,
        oid: Optional[str],
        note: str,
        status: str,
        limit_price: float,
        limit_size: float,
    ) -> tuple[float, float, str]:
        """E9 fix #11: extract the actual fill price (or VWAP across partials)
        so TradeRecord stores cost-basis truth, not the limit price.

        Returns (price, size, price_source). price_source is a short tag that
        is appended to TradeRecord.strategy so the dashboard / reconciler can
        tell whether a record is final (filled / market_fok) or still pending
        (limit_pending) and treat P&L accordingly.
        """
        # Paper realism encodes the simulated fill price inline in the note
        # like "dry_run:paper_filled@0.4856_slip=50bps". Parse it out so the
        # paper portfolio doesn't book the limit as the fill.
        nlow = (note or "").lower()
        if status == "dry_run_filled" and "paper_filled@" in nlow:
            try:
                tail = nlow.split("paper_filled@", 1)[1]
                # stop at first non-numeric / non-dot char
                buf = []
                for ch in tail:
                    if ch.isdigit() or ch == ".":
                        buf.append(ch)
                    else:
                        break
                if buf:
                    fp = float("".join(buf))
                    if fp > 0:
                        return fp, limit_size, "paper_fill"
            except Exception:
                pass
            return limit_price, limit_size, "paper_fill_parse_failed"

        if status in ("dry_run",):
            return limit_price, limit_size, "dry_run"

        # Live: try to read the actual fill from the order record. Filled
        # GTD and market_fok both populate size_matched; if we can recover
        # an exec price field we use it, otherwise VWAP back-solving from
        # the response isn't reliable so we fall back to the limit price
        # with an explicit tag.
        if status in ("filled", "market_fok") and self.clob is not None and oid:
            try:
                raw = self.clob.get_order(oid)
            except Exception as e:
                log.debug("fill-price get_order %s failed: %s", str(oid)[:16], e)
                raw = None
            if isinstance(raw, dict):
                src = raw.get("order") if isinstance(raw.get("order"), dict) else raw
                # Some CLOB shapes expose an average / executed price directly.
                for k in ("average_price", "avg_price", "averagePrice", "executed_price", "price"):
                    v = src.get(k) if isinstance(src, dict) else None
                    if v is None and isinstance(raw, dict):
                        v = raw.get(k)
                    if v is None:
                        continue
                    try:
                        fp = float(v)
                    except (TypeError, ValueError):
                        continue
                    if fp <= 0:
                        continue
                    sm = None
                    try:
                        from bot.clob_utils import normalize_order_payload
                        sm = normalize_order_payload(raw).get("size_matched")
                    except Exception:
                        sm = None
                    filled_size = float(sm) if (sm is not None and sm > 0) else limit_size
                    return fp, filled_size, ("fok_fill" if status == "market_fok" else "gtd_fill")
            # No usable fill price recoverable — record the limit but tag.
            return limit_price, limit_size, ("fok_unknown" if status == "market_fok" else "gtd_fill_unknown")

        # All other statuses (cancelled, submitted, closed without fill, etc.):
        # the limit price is the only number we have, and any P&L computed
        # against it should be treated as provisional.
        return limit_price, limit_size, "limit_pending"

    async def _execute_intent(self, intent: TradeIntent) -> bool:
        if not self.settings.dry_run and self.clob is None:
            self.state.errors.append("exec:no_clob_for_live_trade")
            return False
        # E14b: skip duplicate-token placements within a single cycle.
        # Without this guard, two agents (e.g. value_edge + copy_signal)
        # firing on the same token would each submit, doubling exposure.
        token_key = str(intent.token_id or "").strip()
        if token_key and token_key in getattr(self, "_cycle_inflight_tokens", set()):
            log.warning(
                "same_cycle_duplicate_token_skipped token=%s strategy=%s",
                token_key[:12], intent.strategy,
            )
            slog(
                log,
                self.settings.structured_log,
                "same_cycle_duplicate_token_skipped",
                token=token_key[:12],
                strategy=intent.strategy,
                agent=intent.agent,
            )
            return False
        if token_key:
            try:
                self._cycle_inflight_tokens.add(token_key)
            except Exception:
                pass
        # E14g: reserve USDC against the in-flight intent so concurrent
        # legs in the same cycle don't both see the full balance.
        reserved_added = False
        try:
            current_reserved = float(getattr(self.state, "reserved_usdc", 0.0) or 0.0)
            setattr(self.state, "reserved_usdc", current_reserved + float(intent.size_usd))
            reserved_added = True
        except Exception:
            pass

        def _release_reservation() -> None:
            if not reserved_added:
                return
            try:
                cur = float(getattr(self.state, "reserved_usdc", 0.0) or 0.0)
                setattr(self.state, "reserved_usdc", max(0.0, cur - float(intent.size_usd)))
            except Exception:
                pass

        await self._rate_limit()
        tick = 0.01
        if self.clob:
            try:
                tick = float(self.clob.get_tick_size(intent.token_id))
            except Exception:
                tick = 0.01
        price = round(intent.max_price / tick) * tick
        price = round(min(max(price, tick), 1.0 - tick), 6)
        size_shares = round(intent.size_usd / price, 2)
        if size_shares < 1.0:
            # E9 fix #10: a forced 1-share floor can spend more than the
            # intent's USD budget when the per-share price is high relative
            # to intent.size_usd. Reject (with a 5% tolerance) instead of
            # silently over-spending; otherwise, allow the 1-share floor.
            cost_if_one_share = price * 1.0
            if cost_if_one_share > intent.size_usd * 1.05:
                msg = (
                    f"intent_too_small token={intent.token_id[:12]} "
                    f"price={price:.4f} intent_usd={intent.size_usd:.2f} "
                    f"one_share_cost={cost_if_one_share:.4f}"
                )
                log.warning(msg)
                slog(
                    log,
                    self.settings.structured_log,
                    "intent_too_small",
                    strategy=intent.strategy,
                    agent=intent.agent,
                    token=intent.token_id[:12],
                    price=round(price, 4),
                    intent_usd=round(intent.size_usd, 2),
                    one_share_cost=round(cost_if_one_share, 4),
                )
                self.state.errors.append(
                    f"intent_too_small:{intent.strategy}:price={price:.4f}:intent_usd={intent.size_usd:.2f}"
                )
                _release_reservation()
                return False
            size_shares = 1.0

        oid, note = await place_limit_gtd_then_wait(
            self.clob,
            token_id=intent.token_id,
            side=intent.side,
            price=price,
            size=size_shares,
            ttl_seconds=self.settings.order_ttl_seconds,
            poll_seconds=self.settings.order_poll_seconds,
            dry_run=self.settings.dry_run,
            paper_realism_enabled=self.settings.paper_realism_enabled,
            paper_slippage_model_bps=self.settings.paper_slippage_model_bps,
            follower_latency_ms=self.settings.follower_latency_ms,
        )

        if oid is None or note.startswith("create_failed") or note.startswith("post_failed"):
            self.state.errors.append(f"exec:{intent.strategy}:{note}")
            log.warning("Execution failed %s: %s", intent.strategy, note)
            slog(
                log,
                self.settings.structured_log,
                "execution_failed",
                strategy=intent.strategy,
                note=str(note)[:200],
            )
            _release_reservation()
            return False

        status = "unknown"
        nlow = note.lower()
        if note == "dry_run" or note.startswith("dry_run:"):
            # Phase 2: paper realism may provide more detail
            if "paper_filled" in nlow:
                status = "dry_run_filled"
            elif "paper_miss" in nlow:
                status = "dry_run_miss"
            else:
                status = "dry_run"
        elif note.startswith("filled:"):
            status = "filled"
        elif note.startswith("closed:"):
            status = "closed"
        elif "cancel" in nlow or "ttl" in nlow:
            status = "cancelled"
        else:
            status = "submitted"

        if status in ("filled", "dry_run_filled"):
            self.state.trades_filled += 1

        if (
            status == "cancelled"
            and self.settings.allow_market_fallback
            and not self.settings.strict_execution
        ):
            oid2, note2 = await place_market_fok_fallback(
                self.clob,
                token_id=intent.token_id,
                side=intent.side,
                amount_usd=intent.size_usd,
                dry_run=self.settings.dry_run,
            )
            if oid2:
                oid = oid2
                note = note2
                status = "market_fok"
            elif str(note2).startswith("market_fok_failed"):
                self.state.errors.append(f"market_fallback:{note2}")

        # E9 fix #11: record the actual fill price (or VWAP across partial
        # fills) instead of the limit price when execution data is available.
        # Falls back to the limit price tagged with price_source=limit_pending
        # when fill data isn't reachable (e.g. order still open / GTD pending).
        recorded_price, recorded_size, price_source = self._resolve_fill_price(
            oid=oid,
            note=note,
            status=status,
            limit_price=price,
            limit_size=size_shares,
        )

        ts = utc_now_iso()
        rec = TradeRecord(
            order_id=oid or "none",
            market_question=intent.question,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            side=intent.side,
            price=recorded_price,
            size=recorded_size,
            cost_usd=round(recorded_price * recorded_size, 2),
            status=status,
            timestamp=ts,
            outcome=intent.outcome,
            strategy=f"{intent.strategy}:{note}|price_source={price_source}",
        )
        # E14f: trade_history append and paper_portfolio.record_fill must
        # not split-brain. If the paper record fails AFTER the trade row was
        # appended, roll back the append so the in-memory view matches the
        # paper portfolio (and the dashboard doesn't show a phantom trade).
        self.state.trade_history.append(rec)
        history_appended = True
        self.state.trades_placed += 1
        try:
            if self.settings.dry_run and status in ("dry_run", "dry_run_filled"):
                self._paper_portfolio.record_fill(
                    token_id=intent.token_id,
                    condition_id=intent.condition_id,
                    market=intent.question,
                    outcome=intent.outcome,
                    side=intent.side,
                    price=recorded_price,
                    shares=recorded_size,
                    cost_usd=round(recorded_price * recorded_size, 2),
                    timestamp=ts,
                    strategy=intent.strategy,
                )
        except Exception as paper_exc:
            log.warning(
                "paper_portfolio.record_fill failed — rolling back trade_history append (oid=%s): %s",
                str(oid)[:24], paper_exc,
            )
            if history_appended and self.state.trade_history and self.state.trade_history[-1] is rec:
                self.state.trade_history.pop()
            self.state.trades_placed = max(0, int(self.state.trades_placed) - 1)
        self.state.last_trade = rec.timestamp
        log.info("Executed %s -> %s %s", intent.strategy, oid, note)
        slog(
            log,
            self.settings.structured_log,
            "execution",
            strategy=intent.strategy,
            order_id=str(oid)[:24],
            status=status,
            note=str(note)[:120],
        )

        def _persist() -> None:
            append_trade_log(
                order_id=str(oid),
                market_question=intent.question,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                side=intent.side,
                price=recorded_price,
                size=recorded_size,
                cost_usd=round(recorded_price * recorded_size, 2),
                status=status,
                strategy=rec.strategy,
                outcome=intent.outcome,
                reconcile_note=rec.reconcile_note,
            )
            if status.startswith("dry_run"):
                try:
                    append_paper_trade_log(
                        order_id=str(oid),
                        token_id=intent.token_id,
                        entry_price=price,
                        fill_price=recorded_price if status == "dry_run_filled" else 0.0,
                        slippage_bps=0.0,
                        fill_probability=0.0,
                        filled=status == "dry_run_filled",
                        latency_ms=float(self.settings.follower_latency_ms),
                        reason=str(note)[:200],
                    )
                except Exception as pe:
                    log.warning("DB paper trade log: %s", pe)

        # E10b: persist-after-success is critical. A successful live order
        # that fails to persist becomes invisible to the DB but visible to
        # the exchange — dedupe / rolling-notional / reconcile all break.
        # One-time retry with a short backoff; if both fail we promote to
        # ERROR, append a structured error, and bump a counter so the
        # operator can see the cycle was degraded (the cycle itself must
        # NOT crash — other orders may have already succeeded).
        persist_ok = False
        last_exc: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                await asyncio.to_thread(_persist)
                persist_ok = True
                break
            except Exception as e:
                last_exc = e
                if attempt == 1:
                    await asyncio.sleep(0.1)
        if not persist_ok:
            err_tag = f"persist_failed:order_id={oid}"
            self.state.errors.append(err_tag)
            log.error(
                "DB trade log FAILED after retry (order_id=%s) — trade visible on exchange but NOT in DB",
                oid,
                exc_info=last_exc,
            )
            try:
                current = int(getattr(self.state, "persist_failures", 0) or 0)
                setattr(self.state, "persist_failures", current + 1)
            except Exception:
                pass
            slog(
                log,
                self.settings.structured_log,
                "persist_failed",
                order_id=str(oid)[:24],
                strategy=intent.strategy,
                error=str(last_exc)[:200] if last_exc else "",
            )

        # E14g: order is now persisted (or attempted) — release the reservation.
        _release_reservation()
        return True

    async def run_forever(self):
        self._running = True
        self.state.running = True
        _initialized = False
        while self._running:
            if not _initialized:
                await self._reload_settings_async()
                ok = await self.initialize()
                if not ok:
                    log.info("Waiting for valid keys in database (Admin → settings)…")
                    await asyncio.sleep(12)
                    continue
                _initialized = True
            elif not self.clob:
                await self._reload_settings_async()
                if self.settings.polymarket_private_key:
                    _initialized = False
                    continue
            try:
                await self.run_cycle()
            except Exception as e:
                log.exception("cycle")
                self.state.errors.append(f"cycle: {e}")
            await asyncio.sleep(self.settings.scan_interval_seconds)

    def stop(self):
        self._running = False
        self.state.running = False

    async def aclose(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    def get_state_dict(self) -> dict[str, Any]:
        return {
            "mode": self.state.mode,
            "running": self.state.running,
            "usdc_balance": self.state.usdc_balance,
            "portfolio_value": round(self.state.portfolio_value, 2),
            "total_pnl": round(self.state.total_pnl, 2),
            "positions": self.state.positions,
            "open_orders": self.state.open_orders,
            "trade_history": [
                {
                    "order_id": t.order_id,
                    "market": t.market_question,
                    "side": t.side,
                    "outcome": t.outcome,
                    "price": t.price,
                    "size": t.size,
                    "cost": t.cost_usd,
                    "status": t.status,
                    "timestamp": t.timestamp,
                    "strategy": t.strategy,
                    "reconcile_note": t.reconcile_note,
                }
                for t in self.state.trade_history[-50:]
            ],
            "markets_scanned": self.state.markets_scanned,
            "trades_placed": self.state.trades_placed,
            "trades_filled": self.state.trades_filled,
            "last_scan": self.state.last_scan,
            "last_trade": self.state.last_trade,
            "started_at": self.state.started_at,
            "errors": self.state.errors[-10:],
            "default_bet": self.settings.default_bet_usd,
            "min_bet": self.settings.min_bet_usd,
            "max_bet": self.settings.max_bet_usd,
            "wallet": self.settings.wallet_address,
            "dry_run": self.settings.dry_run,
            "settings": self.settings.to_public_dict(),
            "cex_snapshot": self.state.cex_snapshot,
            "last_intents": self.state.last_intents[:15],
            "agents_fired": self.state.agents_fired,
            "agents_detail": agents_status(
                self.settings,
                cycle_runtime=self.state.cycle_agent_runtime,
            ),
            "open_orders_count": len(self.state.open_orders),
            "last_reconcile_at": self.state.last_reconcile_at,
            "reconcile_updates_last": self.state.reconcile_updates_last,
            "consecutive_exec_failures": self.state.consecutive_exec_failures,
            "rolling_notional_window_usd": round(
                rolling_notional_usd(
                    self.state.trade_history,
                    hours=float(self.settings.daily_notional_window_hours or 24.0),
                ),
                2,
            ),
            "last_skipped_intents": self.state.last_skipped_intents[:20],
            "copy_manager": self._copy_manager.get_summary(),
            "copy_managed_wallets": self._copy_manager.get_managed_wallets()[:50],
            "paper_portfolio": self._paper_portfolio.get_summary() if self.settings.dry_run else {},
        }
