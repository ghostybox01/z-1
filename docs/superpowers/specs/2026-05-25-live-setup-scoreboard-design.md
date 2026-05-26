# Live Setup Card + Real vs Paper Scoreboard

**Date:** 2026-05-25  
**Status:** Approved

## Goal

Three user-facing improvements to the Polymarket bot dashboard:

1. A **Quick Setup card** on the main dashboard so users can enter their Polymarket wallet private key, enable real trading, and pre-configure $2–3 profitable bets without navigating to the admin panel.
2. A **Real vs Paper scoreboard** panel that shows real-trade stats alongside paper simulation stats side-by-side for ongoing profitability monitoring.
3. A minor **backend tweak** to always surface paper portfolio data and expose a `has_private_key` flag in the WebSocket state.

---

## Components

### 1. `bot/orchestrator.py` — State dict changes

Two small additions to `get_state_dict()`:

- **`has_private_key: bool`** — `bool(self.settings.polymarket_private_key)`. Used by the frontend to decide whether to show the setup card. Never exposes the key itself.
- **`paper_portfolio`** — Remove the `if self.settings.dry_run else {}` guard. Always call `self._paper_portfolio.get_summary()`. In live mode it returns an empty dict if no paper trades were recorded, which the frontend handles gracefully.

### 2. `templates/index.html` — Quick Setup card

**Visibility:** Rendered when `!d.wallet || !d.has_private_key`. Hidden (collapsed) once saved successfully.

**Fields:**

| Field | Type | Default |
|-------|------|---------|
| Private Key | `<input type="password">` | empty |
| Wallet Address | `<input type="text" placeholder="0x...">` | empty |
| Signature Type | `<select>` 0 / 1 | 0 |
| Enable Real Trading | toggle (`dry_run`) | **off** — user must explicitly opt in |
| Bet min / default / max | number inputs | $2.00 / $2.50 / $3.00 |
| EV Gate | toggle (`ev_gate_enabled`) | **on** — only bet when EV is positive |

**Save flow:** POST `/api/admin/settings` with the settings dict → on `ok: true` hide card → on error show inline message. The endpoint already exists, requires the admin session cookie (already present after login).

**Security note:** Private key travels over HTTPS to the local server (same-machine or trusted LAN). It is stored in the SQLite DB by the existing settings system, masked on read.

### 3. `templates/index.html` — Real vs Paper Scoreboard

A two-column panel inserted after the existing stats row. Shown whenever either column has data (balance > 0, trades > 0, or paper data present).

**Real Trades column** (from WebSocket state `d`):

| Stat | Source |
|------|--------|
| USDC Balance | `d.usdc_balance` |
| Trades placed | `d.trades_placed` |
| Trades filled | `d.trades_filled` |
| Fill rate % | `trades_filled / trades_placed * 100` |
| USDC spent | sum of `t.cost` for filled trades in `d.trade_history` |
| Mode badge | `d.dry_run` → DRY-RUN (amber) or LIVE (green) |

**Paper Simulation column** (from `d.paper_portfolio`):

| Stat | Source |
|------|--------|
| Paper Balance | `pp.paper_balance` |
| Total Invested | `pp.total_invested` |
| Unrealized P&L | `pp.unrealized_pnl` with sign + colour |
| P&L % | `pp.unrealized_pnl_pct` |
| Positions open | count of `d.positions` |

If `d.paper_portfolio` is empty (never ran paper mode), the paper column shows a muted "No paper data yet — run in Dry Run mode first."

---

## Data Flow

```
WebSocket /ws  →  get_state_dict()
                   ├─ has_private_key  (new)
                   ├─ paper_portfolio  (always, was dry_run-only)
                   ├─ usdc_balance, trades_placed, trades_filled
                   └─ trade_history[-50]

index.html JS  →  updateDashboard(d)
                   ├─ if (!d.wallet || !d.has_private_key) showSetupCard()
                   └─ renderScoreboard(d)
```

---

## Error Handling

- Setup card save: shows inline error text if API returns non-ok. Does not clear entered values on failure.
- Scoreboard: gracefully handles missing/zero values with `||0` defaults; never throws.
- Divide-by-zero in fill rate: guarded with `trades_placed > 0` check.

---

## Out of Scope

- On-chain market resolution tracking (real P&L requires waiting for markets to close — deferred).
- Shadow paper trading while in live mode (paper portfolio running in parallel with real trades).
- Non-admin users seeing the setup card (admin session required to save settings).
