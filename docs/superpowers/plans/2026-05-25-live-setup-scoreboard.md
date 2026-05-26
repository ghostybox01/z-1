# Live Setup Card + Real vs Paper Scoreboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a wallet quick-setup card to the main dashboard and a real vs paper scoreboard panel, plus expose `has_private_key` and unconditional `paper_portfolio` in the bot state dict.

**Architecture:** Backend change to `get_state_dict()` adds two fields; frontend-only HTML/CSS/JS adds the setup card (above cycle strip) and scoreboard (below cycle strip). No new API endpoints — the setup card posts to the existing `/api/admin/settings`.

**Tech Stack:** Python 3, FastAPI, SQLite (via existing KV), vanilla JS, IBM Plex Mono/Sans CSS theme.

---

## File Map

| File | Change |
|------|--------|
| `bot/orchestrator.py` | Add `has_private_key` key; remove `dry_run`-only guard on `paper_portfolio` |
| `tests/test_state_propagation.py` | Add `TestStateDictContract` class (4 tests) |
| `templates/index.html` | Add CSS (before `</style>`), setup card HTML, scoreboard HTML, two JS functions, two `updateUI` call-sites |

---

## Task 1: Backend — extend `get_state_dict()`

**Files:**
- Modify: `bot/orchestrator.py:1109`
- Test: `tests/test_state_propagation.py` (append new class)

- [ ] **Step 1: Write failing tests**

Append this class to `tests/test_state_propagation.py` (before the final `if __name__ == "__main__":` block):

```python
class TestStateDictContract(unittest.TestCase):
    """get_state_dict() must always include has_private_key and paper_portfolio."""

    def _make_bot_mock(self, *, private_key="", dry_run=True, paper_summary=None):
        from unittest.mock import MagicMock
        bot = MagicMock()
        bot.settings = Settings(
            polymarket_private_key=private_key,
            dry_run=dry_run,
        )
        bot.state = BotState()
        bot._paper_portfolio = MagicMock()
        bot._paper_portfolio.get_summary.return_value = paper_summary or {}
        bot._copy_manager = MagicMock()
        bot._copy_manager.get_summary.return_value = {}
        bot._copy_manager.get_managed_wallets.return_value = []
        return bot

    def test_has_private_key_true_when_set(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(private_key="0x" + "b" * 64)
        d = TradingBot.get_state_dict(bot)
        self.assertTrue(d["has_private_key"])

    def test_has_private_key_false_when_empty(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(private_key="")
        d = TradingBot.get_state_dict(bot)
        self.assertFalse(d["has_private_key"])

    def test_paper_portfolio_included_in_live_mode(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(dry_run=False, paper_summary={"total_invested": 5.0})
        d = TradingBot.get_state_dict(bot)
        self.assertIn("paper_portfolio", d)
        self.assertEqual(d["paper_portfolio"], {"total_invested": 5.0})

    def test_paper_portfolio_included_in_dry_run_mode(self):
        from bot.orchestrator import TradingBot
        bot = self._make_bot_mock(dry_run=True, paper_summary={"total_invested": 10.0})
        d = TradingBot.get_state_dict(bot)
        self.assertIn("paper_portfolio", d)
        self.assertEqual(d["paper_portfolio"], {"total_invested": 10.0})
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/x/Downloads/polymarket-bot
python -m pytest tests/test_state_propagation.py::TestStateDictContract -v
```

Expected: 4 failures — `KeyError: 'has_private_key'` and `AssertionError` for the live-mode paper_portfolio test.

- [ ] **Step 3: Edit `bot/orchestrator.py` line 1109**

Find the current line (inside `get_state_dict()`):
```python
            "paper_portfolio": self._paper_portfolio.get_summary() if self.settings.dry_run else {},
```

Replace it with:
```python
            "paper_portfolio": self._paper_portfolio.get_summary(),
            "has_private_key": bool(self.settings.polymarket_private_key),
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_state_propagation.py::TestStateDictContract -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add bot/orchestrator.py tests/test_state_propagation.py
git commit -m "feat: expose has_private_key and unconditional paper_portfolio in state dict"
```

---

## Task 2: Frontend — Quick Setup card (CSS + HTML + JS)

**Files:**
- Modify: `templates/index.html`

### Step 2a — CSS

- [ ] **Step 7: Add setup card CSS**

In `templates/index.html`, find the line `</style>` (line 376). Insert the following CSS block **before** it (i.e., replace `</style>` with this block + `</style>`):

```css
/* Quick Setup card */
.setup-card {
  background: linear-gradient(135deg, rgba(61,255,156,0.06), rgba(92,184,255,0.04));
  border: 1.5px solid rgba(61,255,156,0.3);
  border-radius: var(--radius);
  padding: 20px 24px;
  margin-bottom: 20px;
}
.setup-card h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 4px; color: var(--accent); }
.setup-card .setup-desc { font-size: 0.78rem; color: var(--muted); margin-bottom: 18px; }
.setup-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.setup-field label { font-size: 0.65rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); display: block; margin-bottom: 5px; }
.setup-field input, .setup-field select {
  width: 100%; background: var(--surface); border: 1px solid var(--border);
  color: var(--text); border-radius: 6px; padding: 8px 10px;
  font-family: inherit; font-size: 0.82rem;
}
.setup-field input:focus, .setup-field select:focus { outline: none; border-color: var(--accent-dim); }
.setup-toggle { display: flex; align-items: center; gap: 10px; }
.setup-toggle input[type=checkbox] { width: 16px; height: 16px; accent-color: var(--accent); }
.setup-actions { display: flex; gap: 10px; align-items: center; margin-top: 16px; }
.btn-primary { background: rgba(61,255,156,0.12); border-color: var(--accent-dim); color: var(--accent); }
.btn-primary:hover { background: rgba(61,255,156,0.2); }
.setup-msg { font-size: 0.75rem; }
.setup-msg.ok { color: var(--accent); }
.setup-msg.err { color: var(--danger); }
/* Real vs Paper scoreboard */
.scoreboard-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
.sb-item { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.sb-label { font-size: 0.6rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 4px; }
.sb-value { font-size: 1.05rem; font-family: 'IBM Plex Mono', monospace; }
.sb-value.pos { color: var(--accent); }
.sb-value.neg { color: var(--danger); }
.sb-value.muted { color: var(--muted); font-size: 0.82rem; }
</style>
```

### Step 2b — HTML

- [ ] **Step 8: Add setup card HTML**

In `templates/index.html`, find this exact line (inside `<div class="main">`):

```html
  <!-- Smart diagnostic banners (injected by JS) -->
  <div id="diagBanners"></div>

  <!-- Cycle strip -->
```

Replace it with:

```html
  <!-- Smart diagnostic banners (injected by JS) -->
  <div id="diagBanners"></div>

  <!-- Quick Setup card (shown when wallet/key not configured) -->
  <div id="setupCard" style="display:none;">
    <div class="setup-card">
      <h3>&#9881; Quick Setup &mdash; Connect your Polymarket wallet</h3>
      <p class="setup-desc">Enter your wallet credentials to see your real balance and place live trades. Bet sizes are pre-set for small, EV-positive trades ($2&ndash;$3).</p>
      <div class="setup-grid">
        <div class="setup-field" style="grid-column:1/-1;">
          <label>Private Key</label>
          <input type="password" id="setupPrivKey" placeholder="0x... or 64-char hex" autocomplete="off">
        </div>
        <div class="setup-field">
          <label>Wallet Address</label>
          <input type="text" id="setupWalletAddr" placeholder="0x...">
        </div>
        <div class="setup-field">
          <label>Signature Type</label>
          <select id="setupSigType">
            <option value="0">0 &mdash; EOA (standard)</option>
            <option value="1">1 &mdash; Proxy wallet</option>
          </select>
        </div>
        <div class="setup-field">
          <label>Bet Sizes (USD) &mdash; min / default / max</label>
          <div style="display:flex;gap:8px;">
            <input type="number" id="setupMinBet" value="2" min="1" max="100" step="0.5" style="flex:1;" title="Min bet">
            <input type="number" id="setupDefaultBet" value="2.50" min="1" max="100" step="0.5" style="flex:1;" title="Default bet">
            <input type="number" id="setupMaxBet" value="3" min="1" max="100" step="0.5" style="flex:1;" title="Max bet">
          </div>
        </div>
        <div class="setup-field">
          <label>EV Gate &mdash; profitable trades only</label>
          <div class="setup-toggle">
            <input type="checkbox" id="setupEvGate" checked>
            <span style="font-size:0.78rem;">Only bet when expected value is positive</span>
          </div>
        </div>
        <div class="setup-field">
          <label>Enable Real Trading</label>
          <div class="setup-toggle">
            <input type="checkbox" id="setupLive">
            <span style="font-size:0.78rem;color:var(--warn);">Disable Dry Run &mdash; places real orders!</span>
          </div>
        </div>
      </div>
      <div class="setup-actions">
        <button type="button" class="btn btn-primary" onclick="saveSetup()">Save &amp; Connect</button>
        <span id="setupMsg" class="setup-msg"></span>
      </div>
    </div>
  </div>

  <!-- Cycle strip -->
```

### Step 2c — JS functions

- [ ] **Step 9: Add `updateSetupCard` and `saveSetup` JS functions**

In `templates/index.html`, find the line:

```javascript
function renderDiagBanners(d) {
```

Insert the following two functions **immediately before** that line:

```javascript
function updateSetupCard(d) {
  const card = document.getElementById('setupCard');
  if (!d.wallet || !d.has_private_key) {
    card.style.display = '';
    const addrInput = document.getElementById('setupWalletAddr');
    if (d.wallet && !addrInput.value) addrInput.value = d.wallet;
  } else {
    card.style.display = 'none';
  }
}

async function saveSetup() {
  const pk = document.getElementById('setupPrivKey').value.trim();
  const addr = document.getElementById('setupWalletAddr').value.trim();
  const sig = document.getElementById('setupSigType').value;
  const minBet = parseFloat(document.getElementById('setupMinBet').value) || 2;
  const defBet = parseFloat(document.getElementById('setupDefaultBet').value) || 2.5;
  const maxBet = parseFloat(document.getElementById('setupMaxBet').value) || 3;
  const evGate = document.getElementById('setupEvGate').checked;
  const live = document.getElementById('setupLive').checked;
  const msg = document.getElementById('setupMsg');
  msg.className = 'setup-msg'; msg.textContent = 'Saving…';

  const payload = {
    wallet_address: addr,
    polymarket_signature_type: sig,
    min_bet_usd: String(minBet),
    default_bet_usd: String(defBet),
    max_bet_usd: String(maxBet),
    ev_gate_enabled: evGate ? 'true' : 'false',
    dry_run: live ? 'false' : 'true',
  };
  if (pk) payload.polymarket_private_key = pk;

  try {
    const r = await fetch('/api/admin/settings', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ settings: payload }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      msg.className = 'setup-msg err';
      msg.textContent = (j.detail && (j.detail.message || JSON.stringify(j.detail))) || ('Error ' + r.status);
      return;
    }
    msg.className = 'setup-msg ok';
    msg.textContent = '✓ Saved — connecting…';
    setTimeout(() => { document.getElementById('setupCard').style.display = 'none'; fetchState(); }, 900);
  } catch (e) {
    msg.className = 'setup-msg err';
    msg.textContent = 'Network error: ' + e.message;
  }
}

```

- [ ] **Step 10: Wire `updateSetupCard` into `updateUI`**

In `templates/index.html`, find this line inside `updateUI(d)`:

```javascript
  renderDiagBanners(d);
```

Replace it with:

```javascript
  updateSetupCard(d);
  renderDiagBanners(d);
```

- [ ] **Step 11: Commit**

```bash
git add templates/index.html
git commit -m "feat: add Quick Setup card to main dashboard"
```

---

## Task 3: Frontend — Real vs Paper Scoreboard

**Files:**
- Modify: `templates/index.html`

### Step 3a — HTML

- [ ] **Step 12: Add scoreboard panel HTML**

In `templates/index.html`, find this exact line:

```html
  <!-- Errors panel -->
  <div id="errorsBox" class="errors" style="display:none;">
```

Insert the following HTML block **immediately before** that line:

```html
  <!-- Real vs Paper scoreboard -->
  <div id="scoreboardPanel" style="display:none;margin-bottom:20px;">
    <div class="split-row" style="gap:12px;">
      <div class="section" style="flex:1;min-width:0;">
        <h2>Real Trades <span class="badge" id="realModeBadge" style="font-size:0.62rem;"></span></h2>
        <div id="realStats" class="scoreboard-grid"></div>
      </div>
      <div class="section" style="flex:1;min-width:0;">
        <h2>Paper Simulation</h2>
        <div id="paperStats" class="scoreboard-grid"></div>
      </div>
    </div>
  </div>

```

### Step 3b — JS function

- [ ] **Step 13: Add `renderScoreboard` JS function**

In `templates/index.html`, find:

```javascript
function renderDiagBanners(d) {
```

Insert the following function **immediately before** that line (after `saveSetup` from Task 2):

```javascript
function renderScoreboard(d) {
  const panel = document.getElementById('scoreboardPanel');
  const pp = d.paper_portfolio || {};
  const hasReal = (d.trades_placed || 0) > 0 || (d.usdc_balance || 0) > 0;
  const hasPaper = (pp.total_invested || 0) > 0;
  if (!hasReal && !hasPaper) { panel.style.display = 'none'; return; }
  panel.style.display = '';

  const badge = document.getElementById('realModeBadge');
  badge.textContent = d.dry_run ? 'DRY RUN' : 'LIVE';
  badge.className = 'badge ' + (d.dry_run ? 'dry' : 'live');

  const th = d.trade_history || [];
  const filledTrades = th.filter(t => (t.status || '').toLowerCase().includes('fill'));
  const spent = filledTrades.reduce((s, t) => s + (t.cost || 0), 0);
  const fillRate = (d.trades_placed || 0) > 0
    ? Math.round((d.trades_filled || 0) / d.trades_placed * 100)
    : 0;

  document.getElementById('realStats').innerHTML =
    '<div class="sb-item"><div class="sb-label">USDC Balance</div><div class="sb-value">$' + (d.usdc_balance || 0).toFixed(2) + '</div></div>' +
    '<div class="sb-item"><div class="sb-label">Placed / Filled</div><div class="sb-value">' + (d.trades_placed || 0) + ' / ' + (d.trades_filled || 0) + '</div></div>' +
    '<div class="sb-item"><div class="sb-label">Fill Rate</div><div class="sb-value">' + fillRate + '%</div></div>' +
    '<div class="sb-item"><div class="sb-label">USDC Spent</div><div class="sb-value">$' + spent.toFixed(2) + '</div></div>';

  if (!hasPaper) {
    document.getElementById('paperStats').innerHTML =
      '<div class="sb-item" style="grid-column:1/-1;"><div class="sb-label">Paper Simulation</div>' +
      '<div class="sb-value muted">Run in Dry Run mode first to compare</div></div>';
  } else {
    const pnl = pp.unrealized_pnl || 0;
    const pnlPct = pp.unrealized_pnl_pct || 0;
    const cls = pnl >= 0 ? 'pos' : 'neg';
    const sign = pnl >= 0 ? '+' : '';
    document.getElementById('paperStats').innerHTML =
      '<div class="sb-item"><div class="sb-label">Paper Balance</div><div class="sb-value">$' + (pp.paper_balance || 0).toFixed(2) + '</div></div>' +
      '<div class="sb-item"><div class="sb-label">Total Invested</div><div class="sb-value">$' + (pp.total_invested || 0).toFixed(2) + '</div></div>' +
      '<div class="sb-item"><div class="sb-label">Unrealized P&amp;L</div><div class="sb-value ' + cls + '">' + sign + '$' + Math.abs(pnl).toFixed(2) + '</div></div>' +
      '<div class="sb-item"><div class="sb-label">P&amp;L %</div><div class="sb-value ' + cls + '">' + sign + pnlPct.toFixed(1) + '%</div></div>';
  }
}

```

### Step 3c — Wire into `updateUI`

- [ ] **Step 14: Call `renderScoreboard` from `updateUI`**

In `templates/index.html`, find this block inside `updateUI(d)` (the two lines added in Task 2 Step 10):

```javascript
  updateSetupCard(d);
  renderDiagBanners(d);
```

Replace it with:

```javascript
  updateSetupCard(d);
  renderScoreboard(d);
  renderDiagBanners(d);
```

- [ ] **Step 15: Commit**

```bash
git add templates/index.html
git commit -m "feat: add real vs paper scoreboard to main dashboard"
```

---

## Task 4: Smoke verification

- [ ] **Step 16: Run full test suite**

```bash
cd /Users/x/Downloads/polymarket-bot
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: All tests pass. No new failures.

- [ ] **Step 17: Start the bot and verify visually**

```bash
python main.py --ui dashboard
```

Open `http://localhost:5002` in a browser. Verify:
1. The setup card is visible at the top (wallet not configured yet).
2. Filling in a wallet address + any key and clicking "Save & Connect" calls `/api/admin/settings` and hides the card.
3. The scoreboard panel is hidden initially (no trades yet); after forcing a scan it appears once the bot has a USDC balance.

- [ ] **Step 18: Final commit if any polish needed**

```bash
git add -p
git commit -m "fix: polish setup card and scoreboard edge cases"
```
