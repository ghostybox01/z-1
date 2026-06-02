"""Multi-agent orchestrator: Gamma scan, CEX gate, risk, strict execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Any, Optional, Set

import httpx
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
from bot.clob_client import apply_clob_proxy, build_clob_client

from bot.agents.bundle_arb import BundleArbAgent
from bot.agents.copy_signal import CopySignalAgent
from bot.agents.latency_arb import LatencyArbAgent
from bot.agents.registry import agents_status
from bot.agents.value_edge import ValueEdgeAgent
from bot.agents.zscore_edge import ZScoreEdgeAgent
from bot.copy_manager import CopyManager
from bot.ev_math import copy_ev
from bot.paper_portfolio import PaperPortfolio
from bot.categories import MarketCategory
from bot.cex import fetch_cex_bundle, infer_crypto_asset_from_text
from bot.execution import place_limit_gtd_then_wait, place_market_fok_fallback
from bot.gamma import scan_tradeable_markets
from bot.http_retry import get_json_retry
from bot.models import BotState, TradeIntent, TradeRecord, utc_now_iso
from bot.reconcile import reconcile_trade_records_inplace, snapshot_open_orders
from bot.db.kv import append_paper_trade_log, append_trade_log
from bot.db.models import TradeLog, session_scope
from bot.exposure import category_exposure_usd, condition_exposure_usd, rolling_notional_usd
from bot.execution_plan import plan_execution_units
from bot.market_intel import hours_until_resolution_end
from bot.orderbook import orderbook_buy_depth_ok, spread_mid_bps
from bot.risk import gate_intent
from bot.settings import Settings
from bot.settings_validation import live_risk_caps_ok
from bot.signals import intent_signal_boost
from bot.sizing import pnl_aware_size_multiplier
from bot.structured_log import slog
from bot.validate import is_valid_polygon_address, is_valid_private_key_hex

log = logging.getLogger("polymarket.orchestrator")


def intent_idempotency_key(intent, now_epoch: float) -> str:
    """Deterministic short key; same intent within the same 60s bucket -> same key."""
    bucket = int(now_epoch // 60)
    raw = f"{intent.token_id}|{intent.side}|{round(float(intent.size_usd), 2)}|{round(float(intent.max_price), 3)}|{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class TradingBot:
    """Production-style bot with category toggles, agents, and GTD execution."""

    def __init__(self):
        self.settings = Settings.load()
        self.state = BotState(mode="dry_run" if self.settings.dry_run else "live")
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
        # Per-session geoblock blocklist: markets that returned 403 are skipped automatically
        self._geoblocked_tokens: Set[str] = set()
        # Process-local idempotency guard: short hashes of recently-submitted intents
        self._recent_submit_keys: set[str] = set()

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
        apply_clob_proxy(getattr(self.settings, "clob_https_proxy", "") or "")
        try:
            st = self.settings.polymarket_signature_type
            funder = self.settings.wallet_address if st == 1 else None
            self.clob = build_clob_client(
                private_key=self.settings.polymarket_private_key,
                signature_type=st,
                funder=funder,
            )
            log.info("CLOB V2 L2 auth OK")
        except Exception as e:
            log.exception("CLOB init failed")
            self.state.errors.append(f"Init: {e}")
            return False

        # E3: rehydrate trade history from DB so the trade counter, dedupe, and
        # rolling-notional cap survive restarts (reconcile only sees open CLOB
        # orders; it cannot recover filled trades lost from in-memory state).
        try:
            await asyncio.to_thread(self.load_recent_trades)
        except Exception as e:
            log.warning("load_recent_trades failed on boot: %s", e)
        await self.refresh_balance()
        await self.refresh_positions()
        self.state.started_at = utc_now_iso()
        self.state.running = True
        return True

    def load_recent_trades(self, *, max_rows: int = 200) -> int:
        """Rehydrate state.trade_history from the trade_logs table on boot so the
        trade counter, dedupe, and rolling-notional cap survive a restart.
        Bounded to the most recent `max_rows` rows; exception-safe (returns 0)."""
        from sqlalchemy import select
        loaded: list[TradeRecord] = []
        try:
            with session_scope() as s:
                rows = list(
                    s.scalars(
                        select(TradeLog).order_by(TradeLog.id.desc()).limit(int(max_rows))
                    ).all()
                )
                rows.reverse()  # oldest-first, matching the live append order
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
        self.state.trades_placed = len(loaded)
        self.state.trades_filled = sum(1 for t in loaded if "filled" in str(t.status or "").lower())
        log.info("load_recent_trades: rehydrated %d trades from DB", len(loaded))
        return len(loaded)

    async def refresh_balance(self):
        if not self.settings.wallet_address or not self._http:
            if self.settings.dry_run and self.state.usdc_balance <= 0:
                self.state.usdc_balance = self.settings.default_bet_usd * 100
                log.info("DRY RUN: no wallet configured, using simulated balance $%.2f", self.state.usdc_balance)
            return
        # Primary: CLOB exchange balance (USDC deposited and ready to trade)
        if self.clob is not None:
            try:
                loop = asyncio.get_event_loop()
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.settings.polymarket_signature_type,
                )
                resp = await loop.run_in_executor(
                    None, lambda: self.clob.get_balance_allowance(params=params)
                )
                raw = resp.get("balance", "0") if isinstance(resp, dict) else "0"
                self.state.usdc_balance = int(raw) / 1e6
                return
            except Exception:
                pass  # fall through to on-chain fallback
        # Fallback: on-chain ERC-20 balance (wallet not yet deposited to exchange)
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
        for url in (
            "https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
        ):
            try:
                r = await self._http.post(url, json=payload)
                res = r.json().get("result", "0x0")
                if res and res != "0x":
                    self.state.usdc_balance = int(res, 16) / 1e6
                    return
            except Exception:
                continue

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

        # Market-name fallback only — and only worth a full scan if a scanning agent
        # is active. In copy-only mode we rely on the /positions title instead, so the
        # name lookup never triggers the slow scan.
        if not self._market_cache and self.clob and (
            self.settings.agent_value or self.settings.agent_latency
            or self.settings.agent_bundle or self.settings.agent_zscore
        ):
            await self._gamma_scan()

        our_tokens = {str(t.token_id) for t in self.state.trade_history if getattr(t, "token_id", "")}
        summ = {"active": 0, "won": 0, "lost": 0, "realized_pnl": 0.0}
        counted: set[str] = set()
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
            redeemable = bool(pos.get("redeemable", False))
            current_value = float(pos.get("currentValue", 0) or 0)
            if token_id in our_tokens and token_id not in counted:
                counted.add(token_id)
                _rd = bool(pos.get("redeemable", False))
                _cv = float(pos.get("currentValue", 0) or 0)
                _iv = float(pos.get("initialValue", 0) or 0)
                if _rd and _cv > 0.01:
                    summ["won"] += 1; summ["realized_pnl"] += (_cv - _iv)
                elif _rd:
                    summ["lost"] += 1; summ["realized_pnl"] += (0.0 - _iv)
                else:
                    summ["active"] += 1
            # Skip settled positions worth ~nothing (resolved losers): they are
            # done, not open, and the old avg-price fallback valued them at cost,
            # inflating the portfolio. Keep redeemable winners (currentValue > 0).
            if redeemable and current_value < 0.01:
                continue
            avg_price = float(pos.get("avgPrice", pos.get("avg_price", 0)) or 0)
            # Trust the data-API's authoritative price/value/PnL — it already
            # reflects resolution. Do NOT recompute from stale orderbook mids
            # (that also avoids 404 "no orderbook" spam on settled markets).
            cur_price = float(pos.get("curPrice", pos.get("current_price", 0)) or 0)
            initial_value = float(pos.get("initialValue", 0) or 0)
            pnl = current_value - initial_value
            pnl_pct = (pnl / initial_value * 100.0) if initial_value > 0 else 0.0
            mi = self._find_market_by_token(token_id)
            market_name = pos.get("title", pos.get("question", "")) or (
                mi.get("question", "") if mi else ""
            )
            outcome = pos.get("outcome", "") or (mi.get("outcome_name", "") if mi else "")
            cid_pos = str(pos.get("conditionId") or "")
            if not cid_pos and mi:
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
                    "value": round(current_value, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "redeemable": redeemable,
                }
            )
        summ["realized_pnl"] = round(summ["realized_pnl"], 2)
        self.state.position_summary = summ
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
                depth=self.settings.reconcile_history_depth,
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
        max_pages = max(1, int(getattr(self.settings, "gamma_max_pages", 2) or 2))
        vol_supp = max(0, int(getattr(self.settings, "gamma_volume_supplement_pages", 3) or 3))
        markets, cache = await scan_tradeable_markets(
            self._http,
            self._rate_limit,
            max_pages=max_pages,
            min_liquidity=self.settings.min_clob_liquidity_usd,
            min_volume=self.settings.min_gamma_volume,
            volume_supplement_pages=vol_supp,
        )
        self._market_cache = cache
        self.state.markets_scanned = len(markets)
        self.state.last_scan = utc_now_iso()
        # Category breakdown for diagnostics
        from collections import Counter
        cat_counts = Counter(str(m.get("category", "unknown")) for m in markets)
        log.info("Market categories: %s", dict(cat_counts.most_common()))
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
            over_caps = dict(getattr(self.settings, "category_exposure_caps", {}) or {})
            for c, add_u in cat_new.items():
                cap = float(over_caps.get(c, 0.0) or 0.0)
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

    def _note_exec_result(self, ok: bool) -> None:
        if ok:
            self.state.consecutive_exec_failures = 0
        else:
            self.state.consecutive_exec_failures = int(self.state.consecutive_exec_failures or 0) + 1

    async def run_cycle(self):
        if not self._http:
            return
        if not self.clob and not self.settings.dry_run:
            return
        log.info("——— cycle start ———")
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

        await self.refresh_balance()
        await self.refresh_positions()

        caps_ok, caps_reason = live_risk_caps_ok(self.settings)
        if not caps_ok:
            log.warning("live risk caps: %s — skipping cycle", caps_reason)
            if caps_reason not in self.state.errors:
                self.state.errors.append(caps_reason)
            return

        reserve = max(0.0, float(self.settings.balance_buffer_usd))
        if self.clob and self.state.usdc_balance < self.settings.min_bet_usd + reserve:
            log.warning("Balance below min bet + buffer")
            return

        # Only the market-scanning agents (value/latency/bundle/zscore) consume the
        # ~1.5k-market Gamma scan; copy-trading uses the /activity stream instead.
        # Skip the scan when none of those agents are active so the copy cycle stays
        # fast (seconds, not minutes) and the disabled agents can't slow it down.
        need_scan = bool(self.clob) and (
            self.settings.agent_value or self.settings.agent_latency
            or self.settings.agent_bundle or self.settings.agent_zscore
        )
        markets = await self._gamma_scan() if need_scan else []
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
                self._copy_agent.propose(self._http))))

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
                    if self.state.usdc_balance < need:
                        log.info("skip bundle: insufficient balance (need %.2f incl. buffer)", need)
                        continue
                    ok1 = await self._execute_intent(a)
                    if not ok1:
                        self._note_exec_result(False)
                        continue
                    rolling_n += float(a.size_usd)
                    if a.condition_id:
                        condition_extra[a.condition_id] = condition_extra.get(a.condition_id, 0.0) + float(a.size_usd)
                    acat = str(a.category.value).lower()
                    category_extra[acat] = category_extra.get(acat, 0.0) + float(a.size_usd)
                    ok2 = await self._execute_intent(b)
                    self._note_exec_result(bool(ok2))
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
                    continue

                intent = unit[0]
                # Skip market if it was geo-blocked in a previous attempt this session
                if intent.token_id in self._geoblocked_tokens:
                    skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": "geoblocked"})
                    continue
                # Don't take both sides of one market. We've copied opposite sides of
                # the same game handicap from two different whales before (one side
                # always loses, bleeding spread+buffer for a near-wash). Skip if we
                # already hold a different outcome token of the same condition_id.
                if intent.condition_id and any(
                    str(p.get("condition_id") or "") == intent.condition_id
                    and str(p.get("token_id") or "") != intent.token_id
                    and float(p.get("size") or 0) > 0
                    for p in self.state.positions
                ):
                    skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": "opposing_side_held"})
                    log.info("skip intent: opposing side already held for cond %s…", str(intent.condition_id)[:14])
                    continue
                # Re-entry guard (copy intents only): skip if we already hold this
                # exact token (same token_id, non-zero size). Prevents stacking into
                # a position we already copied from a prior cycle.
                if intent.strategy == "copy_trade" and any(
                    str(p.get("token_id") or "") == intent.token_id
                    and float(p.get("size") or 0) > 0
                    for p in self.state.positions
                ):
                    skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": "already_held_token"})
                    log.info("skip intent: copy token already held (%s…)", intent.token_id[:16])
                    continue
                # Time-to-resolution gate (copy intents only).
                # Min 4h: too close to resolution for a follower to get good execution.
                # Max copy_max_hours_to_resolution (default 720h/30d): don't tie up
                # capital in ultra-long-horizon markets whose outcome is too uncertain.
                if intent.strategy == "copy_trade" and intent.hours_to_resolution is not None:
                    _h = intent.hours_to_resolution
                    _min_h = float(getattr(self.settings, "copy_min_hours_to_resolution", 4.0) or 4.0)
                    _max_h = float(getattr(self.settings, "copy_max_hours_to_resolution", 720.0) or 720.0)
                    if _h < _min_h:
                        skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": f"copy_resolves_too_soon_{_h:.1f}h"})
                        log.info("skip intent: copy resolves in %.1fh < %.0fh floor", _h, _min_h)
                        continue
                    if _max_h > 0 and _h > _max_h:
                        skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": f"copy_resolves_too_far_{_h:.0f}h"})
                        log.info("skip intent: copy resolves in %.0fh > %.0fh cap", _h, _max_h)
                        continue
                # EV-at-our-entry gate (copy intents only). We pay a follow-buffer
                # (copy_price_buffer_bps) so a trade the whale profits on can be -EV
                # for us. Using the source wallet's win rate as the success prob,
                # skip copies whose EV per $1 (held to resolution) is below the floor.
                # Also gate on minimum absolute expected profit so tiny bets stay out.
                if intent.strategy == "copy_trade":
                    p = self._copy_manager.get_wallet_winrate(getattr(intent, "source_wallet", ""))
                    entry = float(intent.max_price)  # already includes the follow-buffer
                    min_ev = float(getattr(self.settings, "copy_min_ev", 0.02) or 0.0)
                    if p is None or entry <= 0.0 or entry >= 1.0:
                        reason = "ev_unknown_winrate" if p is None else "ev_bad_price"
                        skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": reason})
                        log.info("skip intent: %s (%s)", intent.strategy, reason); continue
                    ev = copy_ev(p, entry)   # EV per $1 staked, held to resolution
                    if ev < min_ev:
                        skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": f"unprofitable_ev_{ev:.3f}"})
                        log.info("skip intent: copy unprofitable at our entry (ev=%.3f < %.3f, p=%.2f, entry=%.3f)", ev, min_ev, p, entry); continue
                    # Minimum absolute expected profit: ev * size_usd must cover the
                    # operational cost (fees, gas, spread) of placing the order.
                    min_profit = float(getattr(self.settings, "copy_min_expected_profit_usd", 0.15) or 0.0)
                    if min_profit > 0:
                        expected_profit = ev * float(intent.size_usd)
                        if expected_profit < min_profit:
                            skipped.append({"agent": intent.agent, "strategy": intent.strategy, "question": intent.question[:80], "reason": f"copy_min_profit_{expected_profit:.2f}"})
                            log.info("skip intent: copy expected profit $%.2f < $%.2f min (ev=%.3f, size=%.2f)", expected_profit, min_profit, ev, intent.size_usd); continue
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
                if self.state.usdc_balance < need:
                    log.info("skip: insufficient balance (need %.2f incl. buffer)", need)
                    continue

                ok_ex = await self._execute_intent(intent)
                # Detect geoblock (403): auto-blocklist this token, don't count as circuit breaker failure
                last_err = self.state.errors[-1] if self.state.errors else ""
                if not ok_ex and "403" in str(last_err) and "region" in str(last_err).lower():
                    self._geoblocked_tokens.add(intent.token_id)
                    log.info(
                        "Geoblock 403: auto-listed token %s… (%s)",
                        intent.token_id[:16],
                        intent.question[:50],
                    )
                else:
                    self._note_exec_result(ok_ex)
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
                    depth=self.settings.reconcile_history_depth,
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

    async def _execute_intent(self, intent: TradeIntent) -> bool:
        if not self.settings.dry_run and self.clob is None:
            self.state.errors.append("exec:no_clob_for_live_trade")
            return False
        _idem = intent_idempotency_key(intent, time.time())
        if _idem in self._recent_submit_keys:
            log.info("skip duplicate intent (idempotency) %s %s", intent.strategy, intent.token_id[:12])
            return False
        self._recent_submit_keys.add(_idem)
        if len(self._recent_submit_keys) > 500:
            self._recent_submit_keys = set(list(self._recent_submit_keys)[-250:])
        await self._rate_limit()
        tick = 0.01
        if self.clob:
            try:
                tick = float(self.clob.get_tick_size(intent.token_id))
            except Exception:
                tick = 0.01
        price = round(intent.max_price / tick) * tick
        price = round(min(max(price, tick), 1.0 - tick), 6)
        # Round notional UP to >= $1 (Polymarket marketable-order minimum).
        target_usd = max(1.0, float(intent.size_usd))
        size_shares = math.ceil((target_usd / price) * 100.0) / 100.0
        # Polymarket enforces a 5-share minimum order size. A $1 bet only clears
        # 5 shares when price <= $0.20 (longshots); floor at 5 shares so we can
        # also copy favorites — high-probability, high-win-rate bets.
        if size_shares < 5.0:
            size_shares = 5.0
        # Reflect the true notional so daily/exposure caps + the balance check
        # account for actual spend, not the $1 target estimate.
        intent.size_usd = round(price * size_shares, 2)

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

        ts = utc_now_iso()
        rec = TradeRecord(
            order_id=oid or "none",
            market_question=intent.question,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            side=intent.side,
            price=price,
            size=size_shares,
            cost_usd=round(price * size_shares, 2),
            status=status,
            timestamp=ts,
            outcome=intent.outcome,
            strategy=f"{intent.strategy}:{note}",
        )
        self.state.trade_history.append(rec)
        self.state.trades_placed += 1

        if self.settings.dry_run and status in ("dry_run", "dry_run_filled"):
            self._paper_portfolio.record_fill(
                token_id=intent.token_id,
                condition_id=intent.condition_id,
                market=intent.question,
                outcome=intent.outcome,
                side=intent.side,
                price=price,
                shares=size_shares,
                cost_usd=round(price * size_shares, 2),
                timestamp=ts,
                strategy=intent.strategy,
            )
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
                price=price,
                size=size_shares,
                cost_usd=round(price * size_shares, 2),
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
                        fill_price=price if status == "dry_run_filled" else 0.0,
                        slippage_bps=0.0,
                        fill_probability=0.0,
                        filled=status == "dry_run_filled",
                        latency_ms=float(self.settings.follower_latency_ms),
                        reason=str(note)[:200],
                    )
                except Exception as pe:
                    log.warning("DB paper trade log: %s", pe)

        try:
            await asyncio.to_thread(_persist)
        except Exception as e:
            log.warning("DB trade log: %s", e)

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
            # Pace the loop to the fastest active agent. Copy-trading reacts to the
            # /activity stream and wants a short poll; the market-scanning agents only
            # need the slower scan interval. In copy-only mode this makes copy react in
            # ~seconds instead of waiting a full scan interval (the scan is skipped too).
            need_scan = (
                self.settings.agent_value or self.settings.agent_latency
                or self.settings.agent_bundle or self.settings.agent_zscore
            )
            if self.settings.agent_copy and not need_scan:
                sleep_s = float(self.settings.copy_poll_seconds or 15.0)
            else:
                sleep_s = float(self.settings.scan_interval_seconds or 120.0)
            await asyncio.sleep(max(2.0, sleep_s))

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
            "paper_portfolio": self._paper_portfolio.get_summary(),
            "has_private_key": bool(self.settings.polymarket_private_key),
            "position_summary": self.state.position_summary,
            "total_equity_usd": round(float(self.state.usdc_balance or 0) + float(self.state.portfolio_value or 0), 2),
            "total_pnl_usd": round(
                float(self.state.usdc_balance or 0) + float(self.state.portfolio_value or 0)
                - float(getattr(self.settings, "starting_bankroll_usd", 24.0) or 0.0),
                2,
            ),
        }
