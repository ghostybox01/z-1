# Polymarket V2 Migration + Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the bot placing real orders again on Polymarket's V2 CLOB, on a hardened and *validated* execution path — not the current illusory-edge one.

**Architecture:** The bot is built on the now-dead `py_clob_client` (V1), which Polymarket's V2 CLOB (cutover 2026-04-28) rejects with `order_version_mismatch`. We migrate the execution + auth + balance layer to `py_clob_client_v2`, then layer safety hardening on top, then add validation tooling (paper-sim fix + backtester) so we can measure edge before scaling.

**Tech Stack:** Python 3.12, `py_clob_client_v2==1.0.1` (replaces `py-clob-client`), httpx (proxy), FastAPI, SQLAlchemy/SQLite, pytest.

---

## Proven facts (from the 2026-06-01 spike — do not re-litigate)

- **Root cause:** Polymarket migrated the CLOB to V2 on 2026-04-28. V1 orders are rejected `order_version_mismatch` for **all keys/sig-types** (verified with a throwaway key). The old `py-clob-client` repo is archived/read-only.
- **The fix works:** `py_clob_client_v2` posting to **`https://clob.polymarket.com`** (unchanged host) through the Nigeria proxy returned `maker address not allowed, please use the deposit wallet flow` for a throwaway key — i.e. the V2 order was **accepted at the version/signature layer**. `order_version_mismatch` is gone.
- **V2 API surface (confirmed by introspection of v1.0.1):**
  - Module: `py_clob_client_v2` (coexists with `py_clob_client`; no import clash).
  - HTTP proxy patch: `py_clob_client_v2.http_helpers.helpers._http_client = httpx.Client(http2=True, proxy=<url>)` — same mechanism as V1.
  - Client: `ClobClient(host, key, chain_id=137, signature_type, funder)` — unchanged constructor.
  - Creds: `create_or_derive_api_key()` → `ApiCreds(api_key, api_secret, api_passphrase)`, then `set_api_creds(creds)`. (V1 name `create_or_derive_api_creds` no longer exists.)
  - Order build/post: `create_order(OrderArgs(token_id, price, size, side, expiration), options=PartialCreateOrderOptions(tick_size, neg_risk)|None)` then `post_order(signed, OrderType.GTD)`; or `create_and_post_order(order_args, options=None, order_type=OrderType.GTC)`. `OrderArgs` (V2) fields: `token_id, price, size, side, expiration, builder_code, metadata, user_usdc_balance` — **no `nonce`, no `fee_rate_bps`**.
  - `OrderType`: `GTC, GTD, FOK, FAK`. `BUY/SELL` in `py_clob_client_v2.order_builder.constants`.
  - Status/cancel: `get_order(order_id)`, `cancel_order(order_id)` (V1 `cancel` gone), plus `cancel_orders`, `cancel_all`.
  - Balance: `get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=<st>))`; `update_balance_allowance(params)` to set approvals. `AssetType.COLLATERAL|CONDITIONAL`.
  - **V2 collateral is PolyUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`** (not USDC.e `0x2791…`). Funds must be deposited/wrapped to PolyUSD; the proxy needs PolyUSD allowance to the V2 exchange.
  - V2 exchanges: `exchange_v2=0xE111180000d2663C0091e4f400237545B87B996B`, `neg_risk_exchange_v2=0xe2222d279d744050d28e00520010520000310F59`. (These are the addresses someone correctly found but wrongly put in the V1 SDK.)
  - **PolyApiException (V2):** no `.status_code`/`.error_msg` attributes (init is `(resp, error_msg)`) — use `str(e)` in notes everywhere execution.py currently formats `{e.status_code}:{e.error_msg}`.
  - **MarketOrderArgsV2 fields (confirmed):** `token_id, amount, side, price, order_type, user_usdc_balance, builder_code, metadata`.
- **Env:** Running bot is at `/opt/polymarket-bot` on the VPS (systemd `polymarket-bot`), DRY-friendly. `py_clob_client_v2` is already `pip install`-ed there alongside V1 (additive; running bot untouched). Local dev repo: `/Users/x/Downloads/polymarket-bot`. Tests: `make test` (offline). The VPS `py_clob_client/config.py` was restored to canonical V1 addresses (harmless; V1 path is being retired).

---

## Program overview (4 plans, sequenced)

| # | Plan | Why | Depends on |
|---|------|-----|-----------|
| **1** | **V2 execution migration** (this doc, detailed below) | Critical path — without it, zero orders | — |
| **2** | **Safety hardening** (re-implement from `claude/sweet-blackburn-011b97` as reference) | Idempotency, ghost-order detection, DB rehydration, risk-cap boot guard, bundle unwind — before real orders scale | Plan 1 |
| **3** | **Paper-sim realism fix** | Stop the rigged-positive paper P&L; mark losers at resolution; feed the real book | independent |
| **4** | **Backtester** | Measure whether any strategy has edge before going live | independent |

**Sequencing rationale.** Plans 1 and 2 both rewrite `bot/execution.py` and `bot/orchestrator.py`. The hardening branch diverged from an *old* main and `main` has since moved 16 commits, so a git-merge is a messy 3-way conflict compounded by the SDK swap. Instead we **re-implement** the branch's safety features directly on the V2 base (the branch is the reference/spec). Plans 3 and 4 touch the dry-run/analysis path only (no live risk) and can run in parallel by another worker.

**Go-live gate (applies after Plan 1+2):** paper → single tiny live order (manual) → monitor 24h → enable agents. Do NOT flip the live bot to the V2 path until Task 1.9 smoke passes AND the user has funded PolyUSD.

---

## Plan 1: V2 execution migration

**File structure:**
- Modify: `requirements.txt` — swap SDK.
- Create: `bot/clob_client.py` — single module that constructs the V2 client + applies the proxy patch + derives creds (extracted from `orchestrator.initialize` so it is unit-testable and there is ONE place that imports the SDK).
- Modify: `bot/execution.py` — V2 order build/post/cancel; drop fee-in-order; `cancel`→`cancel_order`.
- Modify: `bot/orchestrator.py` — use `bot/clob_client.py`; V2 `BalanceAllowanceParams` (with `signature_type`); V2 imports.
- Create: `tests/test_clob_client.py`, `tests/test_execution_v2.py` — unit tests with a fake V2 client.
- Create: `tests/smoke_v2_live.py` — gated live smoke (formalized spike), skipped unless `PM_LIVE_SMOKE=1`.

> Run all pytest via the repo venv: `source .venv/bin/activate` first (or `.venv/bin/python -m pytest`). Keep `make test` green at every commit.

### Task 1.0: Confirm remaining V2 shapes (no code change — grounds the rest)

- [ ] **Step 1: Introspect V2 market-order + response shapes** in a scratch venv (already at `/tmp/v2probe` locally, or `.venv` on VPS).

```bash
.venv/bin/python - <<'PY'
import dataclasses as dc
from py_clob_client_v2 import clob_types as ct
print("MarketOrderArgs:", [f.name for f in dc.fields(ct.MarketOrderArgsV2)])
print("OrderType.GTD:", ct.OrderType.GTD)
PY
```

Expected: prints field names (e.g. `token_id, amount, side, ...`) and `OrderType.GTD` value. Record them; used in Task 1.5 / 1.6. If `MarketOrderArgsV2` differs from assumptions, adjust Task 1.6 accordingly.

- [ ] **Step 2: Commit nothing** (investigation only).

### Task 1.1: Swap the SDK dependency

**Files:** Modify `requirements.txt`

- [ ] **Step 1: Replace the V1 client line**

In `requirements.txt`, change:
```
py-clob-client>=0.34.0
```
to:
```
py-clob-client-v2>=1.0.1
```

- [ ] **Step 2: Install into the dev venv**

Run: `.venv/bin/pip install -r requirements.txt`
Expected: `py_clob_client_v2` installed; no errors.

- [ ] **Step 3: Confirm nothing else imports the old SDK**

Run: `grep -rn "py_clob_client\b\|from py_clob_client " bot/ | grep -v py_clob_client_v2`
Expected: only `bot/clob_client.py`, `bot/execution.py`, `bot/orchestrator.py` (the files this plan migrates). Note any other hits and migrate them too.

- [ ] **Step 4: Commit**
```bash
git add requirements.txt && git commit -m "build: swap py-clob-client -> py-clob-client-v2 for CLOB V2"
```

### Task 1.2: Extract a testable client factory (`bot/clob_client.py`)

**Files:** Create `bot/clob_client.py`, Test `tests/test_clob_client.py`

- [ ] **Step 1: Write the failing test** (`tests/test_clob_client.py`)

```python
from bot.clob_client import apply_clob_proxy, build_clob_client

class _FakeHelpers:
    _http_client = None

def test_apply_clob_proxy_patches_module_client(monkeypatch):
    fake = _FakeHelpers()
    monkeypatch.setattr("bot.clob_client._clob_http", fake, raising=False)
    apply_clob_proxy("http://user:pass@host:1234")
    assert fake._http_client is not None  # patched with a proxied httpx.Client

def test_apply_clob_proxy_noop_on_empty(monkeypatch):
    fake = _FakeHelpers()
    monkeypatch.setattr("bot.clob_client._clob_http", fake, raising=False)
    apply_clob_proxy("")
    assert fake._http_client is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_clob_client.py -v`
Expected: FAIL (ImportError: cannot import name 'apply_clob_proxy').

- [ ] **Step 3: Implement `bot/clob_client.py`**

```python
"""Single place that builds the Polymarket CLOB V2 client and applies the proxy."""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from py_clob_client_v2.client import ClobClient
import py_clob_client_v2.http_helpers.helpers as _clob_http

log = logging.getLogger("polymarket.clob")

CLOB_HOST = "https://clob.polymarket.com"  # V2 server is the same host (confirmed by spike)


def apply_clob_proxy(proxy_url: str) -> None:
    """Route ALL CLOB traffic through the proxy by replacing the SDK's module-level httpx client."""
    url = (proxy_url or "").strip()
    if not url:
        return
    try:
        _clob_http._http_client = httpx.Client(http2=True, proxy=url)
        log.info("CLOB proxy active: %s…", url[:40])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("CLOB proxy setup failed: %s", exc)


def build_clob_client(*, private_key: str, signature_type: int, funder: Optional[str]) -> ClobClient:
    """Construct the V2 client and derive L2 creds. Caller applies the proxy FIRST."""
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=137,
        signature_type=signature_type,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_key())
    return client
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_clob_client.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**
```bash
git add bot/clob_client.py tests/test_clob_client.py
git commit -m "feat(clob): V2 client factory + proxy patch, extracted and tested"
```

### Task 1.3: Migrate `orchestrator.initialize()` + balance to V2

**Files:** Modify `bot/orchestrator.py`

- [ ] **Step 1: Replace the V1 imports** near the top of `bot/orchestrator.py`

Remove:
```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
```
Add:
```python
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
from bot.clob_client import apply_clob_proxy, build_clob_client
```
(`ClobClient` type hint: use `from py_clob_client_v2.client import ClobClient` if you keep the annotation.)

- [ ] **Step 2: Replace the proxy patch + client construction** in `initialize()` (the block currently patching `py_clob_client.http_helpers.helpers._http_client` and calling `ClobClient(...)` + `create_or_derive_api_creds()`):

```python
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
```

- [ ] **Step 3: Add `signature_type` to the balance params** in `refresh_balance()`

Change:
```python
params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
```
to:
```python
params = BalanceAllowanceParams(
    asset_type=AssetType.COLLATERAL,
    signature_type=self.settings.polymarket_signature_type,
)
```

- [ ] **Step 4: Run the offline suite** (orchestrator import must not break)

Run: `make test`
Expected: PASS (no import errors; existing tests still green).

- [ ] **Step 5: Commit**
```bash
git add bot/orchestrator.py
git commit -m "feat(orchestrator): construct CLOB V2 client + V2 balance params"
```

### Task 1.4: Migrate the limit-order path in `bot/execution.py`

**Files:** Modify `bot/execution.py`, Test `tests/test_execution_v2.py`

- [ ] **Step 1: Write the failing test** with a fake V2 client (`tests/test_execution_v2.py`)

```python
import asyncio
from bot import execution

class FakeClient:
    def __init__(self, post_resp, order_states):
        self._post_resp = post_resp
        self._states = list(order_states)
        self.cancelled = []
    def get_tick_size(self, token_id): return "0.01"
    def get_neg_risk(self, token_id): return False
    def create_order(self, args, options=None): return {"args": args, "options": options}
    def post_order(self, signed, order_type): return self._post_resp
    def get_order(self, oid): return self._states.pop(0) if self._states else {"status": "LIVE"}
    def cancel_order(self, oid): self.cancelled.append(oid); return {"canceled": [oid]}

def test_limit_fills_immediately():
    c = FakeClient(post_resp={"orderID": "abc"},
                   order_states=[{"status": "FILLED", "size_matched": "100", "original_size": "100"}])
    oid, note = asyncio.run(execution.place_limit_gtd_then_wait(
        c, token_id="t"*30, side="BUY", price=0.05, size=100,
        ttl_seconds=15, poll_seconds=0.25, dry_run=False))
    assert oid == "abc" and note.startswith("filled")

def test_limit_cancels_after_ttl():
    c = FakeClient(post_resp={"orderID": "xyz"},
                   order_states=[{"status": "LIVE"}, {"status": "LIVE"}])
    oid, note = asyncio.run(execution.place_limit_gtd_then_wait(
        c, token_id="t"*30, side="BUY", price=0.05, size=100,
        ttl_seconds=1, poll_seconds=0.25, dry_run=False))
    assert oid == "xyz" and "xyz" in c.cancelled
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_execution_v2.py -v`
Expected: FAIL (current execution.py uses V1 `OrderArgs(fee_rate_bps=...)`, `client.cancel`, and `py_clob_client` imports).

- [ ] **Step 3: Migrate `bot/execution.py`**

Replace the V1 imports:
```python
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL
```
with V2:
```python
from py_clob_client_v2.clob_types import OrderArgs, OrderType
from py_clob_client_v2.exceptions import PolyApiException
from py_clob_client_v2.order_builder.constants import BUY, SELL
```

In `place_limit_gtd_then_wait`, replace the order build (remove the fee fetch + `fee_rate_bps`; V2 orders carry no fee field). Replace:
```python
    fee_bps = 0
    try:
        fee_bps = int(client.get_fee_rate_bps(token_id))
    except Exception:
        pass

    exp = int(time.time()) + max(15, int(ttl_seconds))
    order_side = BUY if side.upper() == "BUY" else SELL

    args = OrderArgs(
        token_id=token_id, price=price, size=size, side=order_side,
        fee_rate_bps=fee_bps, expiration=exp,
    )
    try:
        signed = client.create_order(args)
    ...
```
with:
```python
    exp = int(time.time()) + max(15, int(ttl_seconds))
    order_side = BUY if side.upper() == "BUY" else SELL

    args = OrderArgs(token_id=token_id, price=price, size=size, side=order_side, expiration=exp)
    try:
        signed = client.create_order(args)  # options=None -> SDK fetches tick_size + neg_risk
    except PolyApiException as e:
        # V2 PolyApiException has no .status_code/.error_msg attributes — use str(e).
        log.warning("create_order PolyApiException: %s", e)
        return None, f"create_failed:poly_api:{e}"
    except Exception as e:
        log.exception("create_order failed")
        return None, f"create_failed:{e}"
```

Also update the **`post_order` PolyApiException catch** (and any other `{e.status_code}:{e.error_msg}` in this file) the same way — replace `{e.status_code}:{e.error_msg}` with `{e}`, since V2's exception does not expose those attributes.

In the same function, change the TTL cancel call `client.cancel(oid)` → `client.cancel_order(oid)` (there are two `client.cancel(` occurrences — the post-TTL cancel and the verify-after-cancel-failure path; update both).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_execution_v2.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Run full suite**

Run: `make test`
Expected: PASS. (If the old `tests/test_execution*.py` import the V1 SDK and now fail, update their imports to `py_clob_client_v2` in the same commit.)

- [ ] **Step 6: Commit**
```bash
git add bot/execution.py tests/test_execution_v2.py
git commit -m "feat(execution): V2 limit order build/post/cancel (no in-order fee)"
```

### Task 1.5: Disable the market-FOK fallback under V2 (until ported)

**Files:** Modify `bot/execution.py`

Rationale: `allow_market_fallback` defaults **false** and `strict_execution` defaults **true**, so this path is normally dead. Porting `MarketOrderArgsV2` is deferred to its own task; until then it must fail safe rather than call V1 APIs.

- [ ] **Step 1: Guard `place_market_fok_fallback`** — at the top of the function (after the `dry_run` shortcut), return a clear not-implemented note instead of building a V1 market order:

```python
    # V2 market-order port pending (Task 1.6). Fallback is off by default.
    return None, "market_fok_failed:v2_market_order_not_implemented"
```
Leave the rest of the function in place below for the port.

- [ ] **Step 2: Run suite**

Run: `make test`
Expected: PASS.

- [ ] **Step 3: Commit**
```bash
git add bot/execution.py
git commit -m "chore(execution): fail-safe market fallback until V2 market port"
```

### Task 1.6: Port the market-FOK path to V2 (uses Task 1.0 field names)

**Files:** Modify `bot/execution.py`, Test `tests/test_execution_v2.py`

- [ ] **Step 1: Write a failing test** for the market path using the FakeClient (add `create_market_order`/`post_order` returning an id), asserting `place_market_fok_fallback` returns the id. (Mirror the limit test; use the real `MarketOrderArgsV2` field names from Task 1.0.)

- [ ] **Step 2: Run → fail.** `.venv/bin/python -m pytest tests/test_execution_v2.py -k market -v`

- [ ] **Step 3: Implement** using `from py_clob_client_v2.clob_types import MarketOrderArgs` and the confirmed fields, replacing the guard from Task 1.5 with the real V2 build + `post_order(signed, OrderType.FOK)`.

- [ ] **Step 4: Run → pass.** Then `make test`.

- [ ] **Step 5: Commit** `feat(execution): port market-FOK fallback to V2`.

### Task 1.7: Update paper-realism observed price + keep dry-run green

**Files:** Modify `bot/execution.py` (the `_simulate_paper_fill` call site only if its imports changed) — verify dry-run path still works end-to-end.

- [ ] **Step 1:** Run `make test` and confirm `tests/test_paper_realism.py`, `tests/test_paper_portfolio.py` pass unchanged (paper path does not touch the SDK). No code change expected; this task is a checkpoint. If anything imports the V1 SDK transitively, fix it here.

- [ ] **Step 2: Commit** only if a fix was needed.

### Task 1.8: Grep-sweep for any remaining V1 references

- [ ] **Step 1:** Run `grep -rn "py_clob_client\b\|create_or_derive_api_creds\|\.cancel(" bot/ | grep -v py_clob_client_v2 | grep -v cancel_order`
Expected: no hits. Fix any stragglers.
- [ ] **Step 2: Commit** if needed.

### Task 1.9: Gated live smoke test (formalized spike)

**Files:** Create `tests/smoke_v2_live.py`

- [ ] **Step 1: Write the gated smoke** (NOT run by `make test`; requires real keys + opt-in):

```python
"""Live V2 smoke. Run on the VPS ONLY: PM_LIVE_SMOKE=1 .venv/bin/python tests/smoke_v2_live.py
Posts ONE non-marketable BUY (price 0.01) with the configured key, then cancels.
Success = NOT order_version_mismatch."""
import os, sys, json
if os.environ.get("PM_LIVE_SMOKE") != "1":
    print("skipped (set PM_LIVE_SMOKE=1)"); sys.exit(0)
import httpx
from bot.db.bootstrap import init_database; init_database()
from bot.db.kv import load_all_kv
from bot.clob_client import apply_clob_proxy, build_clob_client
from py_clob_client_v2.clob_types import OrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import BUY
kv = load_all_kv()
apply_clob_proxy(kv.get("clob_https_proxy", ""))
st = int(kv.get("polymarket_signature_type", "1") or "1")
c = build_clob_client(private_key=kv["polymarket_private_key"], signature_type=st,
                      funder=(kv.get("wallet_address") or None) if st == 1 else None)
m = httpx.get("https://gamma-api.polymarket.com/markets",
              params={"limit": 1, "active": "true", "closed": "false",
                      "order": "liquidityClob", "ascending": "false"}, timeout=30).json()[0]
toks = m["clobTokenIds"]; toks = json.loads(toks) if isinstance(toks, str) else toks
try:
    resp = c.create_and_post_order(OrderArgs(token_id=toks[0], price=0.01, size=100, side=BUY),
                                   order_type=OrderType.GTC)
    print("POST_OK", resp)
    oid = resp.get("orderID") or resp.get("order_id")
    if oid: c.cancel_order(oid); print("cancelled", oid)
except Exception as e:
    s = repr(e)
    print("POST_ERR", s)
    assert "order_version_mismatch" not in s, "STILL V1 — migration incomplete"
    print("PASS: version accepted (any other error is funding/maker-state, not version)")
```

- [ ] **Step 2: Run on the VPS after deploy** (Task 1.10): `PM_LIVE_SMOKE=1 .venv/bin/python tests/smoke_v2_live.py`
Expected: `POST_OK` (if PolyUSD funded) or a non-`order_version_mismatch` error → PASS.

- [ ] **Step 3: Commit** `test: gated live V2 smoke`.

### Task 1.10: Deploy to VPS + verify (no agents armed yet)

- [ ] **Step 1:** Ensure `trading_paused=true` (or `dry_run=true`) in the VPS DB before deploy so the cycle does not place orders during rollout.
- [ ] **Step 2:** Deploy (git pull on VPS or scp), `/opt/polymarket-bot/.venv/bin/pip install -r requirements.txt`, `systemctl restart polymarket-bot`.
- [ ] **Step 3:** `journalctl -u polymarket-bot -f` — confirm "CLOB V2 L2 auth OK", balance reads, no import errors, no `order_version_mismatch`.
- [ ] **Step 4:** Run Task 1.9 smoke on the VPS → PASS.
- [ ] **Step 5:** STOP. Hand back to user for the go-live gate (PolyUSD funding + the controlled go-live protocol). Do not arm agents here.

---

## Plan 2: Safety hardening (own cycle — re-implement from branch reference)

Source of truth: branch `claude/sweet-blackburn-011b97` (commits E1–E15). Re-implement on the V2 base, each behind a test:
- **Idempotency key** on submit (E5): stable client-side key blocks duplicate posts.
- **Ghost-order detection** (E6): on ambiguous submit failure, reconcile before retry.
- **DB rehydrate trade history on boot** (E3).
- **Hard-fail boot when risk caps are all zero** (E1) — directly relevant since current defaults are 0/unlimited.
- **Bundle partial-fill unwind** (E4, behind flag).
- **Reconcile partial-fill correctness** (E7).
Write its own spec+plan before coding.

## Plan 3: Paper-sim realism fix (own cycle — independent, no live risk)

- Feed `simulate_paper_fill` the **real observed book price** (not `limit*0.99`) in `bot/execution.py:_simulate_paper_fill`.
- Mark paper positions at **resolution** (0/1) when a market closes, not just mid — book realized losses in `bot/paper_portfolio.py`.
- Add a regression test asserting a YES bought at 0.30 that resolves NO books −cost.

## Plan 4: Backtester (own cycle — independent, highest validation value)

- New `bot/backtest/` package: load historical Gamma/CLOB prices + trades, replay each agent's `propose()` against history, simulate fills with spread/adverse-selection, report per-strategy realized PnL after fees.
- Goal: answer "does any agent have positive edge?" before scaling live. Write its own spec+plan.

---

## Global verification & rollback

- **Every commit:** `make test` stays green.
- **Rollback (Plan 1):** `git revert` the range, `pip install -r requirements.txt` (restores `py-clob-client`), restart. The V1 SDK still imports; it just can't place orders (pre-existing state).
- **Go-live protocol (after Plan 1+2, user-gated):** fund PolyUSD → confirm `update_balance_allowance` set for the V2 exchange → one manual tiny live order via smoke → 24h monitor at `max_trades_per_cycle=1`, `max_bet_usd` small, exposure caps > 0 → only then re-enable agents.
- **Do not** scale size or arm agents until Plan 4 shows a measured edge. The order bug being fixed does not mean the strategies are profitable.
