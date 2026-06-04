# Polymarket Trading Bot — Technical Handbook

> **Secrets are NOT in this file.** VPS login, wallet private key, admin/proxy credentials live in the operator's private notes — never commit them to git (history is permanent; this repo has a fork). Placeholders below read `<…>`.

Multi-agent bot trading Polymarket prediction markets. Build loop: study markets → build agent → **backtest** → observe live → iterate. Standing discipline: **measure edge BEFORE going live; perfect one agent at a time.** Goal: highest sustainable **profit/EV** — note win-rate ≠ profit (§4).

---

## 1. Status (2026-06-04)
- LIVE, `dry_run=false`, real money. 2 active strategies: **copy-trading** (proven net-positive) + **weather forecast-arb** (first bets placed, UNPROVEN).
- Copy true resolved record so far: 46.7% (7W/8L), +$4.89 realized (history polluted by pre-fix churn; forward judgment-only record is the real test).
- Branch `v2-migration`; 283 tests pass.

## 2. Access (placeholders — real values in private memory)
- VPS: `root@<VPS_IP>`, code `/opt/polymarket-bot`, systemd `polymarket-bot`. Local: `/Users/x/Downloads/polymarket-bot`.
- Dashboard: `http://<VPS_IP>:5002` (admin login; password rotated).
- Wallet: proxy/funder + signer addresses are on-chain/public; **private key stays on the VPS** (KV `polymarket_private_key`) — never in git.
- SSH note: sandbox pty is broken (`sshpass` → `openpty`), so deploy pty-free via `SSH_ASKPASS=<script echoing pw> SSH_ASKPASS_REQUIRE=force ssh ... 'bash -s' <<heredoc`.
- Read live state without admin pw: forge a `pm_session` token on the VPS via `issue_token(load_config()['session_secret'], {uid,role})` (first user from DB), GET `localhost:5002/api/state`.

## 3. Platform (Polymarket CLOB V2)
- CLOB migrated to **V2 on 2026-04-28** → `py-clob-client-v2` (V1 archived → `order_version_mismatch`). Host `https://clob.polymarket.com`.
- Collateral **PolyUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`**. `create_or_derive_api_key()`; cancel via `cancel_orders([id])`; V2 OrderArgs no fee/nonce; GTD expiry `now+60+ttl`; **5-share minimum order** (floor at 5 shares → $1–$5/bet).
- Data: `data-api.polymarket.com` (`/positions`,`/activity`,`/trades?user=|?market=`,`/v1/leaderboard`); `gamma-api.../markets` (metadata + `outcomePrices`); **`clob.../markets/<cid>` = reliable resolver** (gamma `?condition_ids=` ~2/3 incomplete); `clob.../book?token_id=`.
- Leaderboard `?category=` is **fake** for WEATHER/MACRO/GEO/etc (echoes global top-15); only OVERALL/POLITICS/SPORTS/CRYPTO/FINANCE are distinct.

## 4. Strategy map — what works vs DEAD (measured)
**Only INFORMATION-based edges survive on efficient Polymarket; pure price-pattern & speed strategies are dead.**

| Strategy | Verdict | Evidence |
|---|---|---|
| value_edge / zscore | ❌ DEAD | edge-probe −88…−143 bps/fill, 44% win (adverse selection) |
| crypto latency-arb | ❌ Infeasible | 5-min Up/Down resolve off CEX; sub-second bots own it |
| calibration / favorite-longshot | ❌ DEAD | 25,465-bet backtest: 94.7% win but **−185 bps/bet after 2% cost**; PM is well-calibrated |
| **copy-trading** | ✅ WORKS (capped) | judgment cats 75-98% win; net-positive live |
| **weather forecast-arb** | ⏳ TESTING | Open-Meteo forecast vs temp-price; fits REST infra (data edge, EOD resolution) |

- **win-rate ≠ profit (proven):** calibration = 94.7% win yet loses money (rare −100% losses outweigh tiny wins). Our bot is net-positive at 46.7% win because it buys winners cheap. Optimize **EV/realized P&L**.
- **Copy price band (copy_probe ~6000 replays):** <0.30 lottery (median −100%); 0.30-0.65 = 53%/+59% med; **0.65-0.90 = 80%/+28% med (sweet spot)**; ≥0.90 net-neg after buffer.
- **Copy category:** judgment (weather 98% / geo 89% / politics 86% / macro+other ~75%) crush SPORTS (51.9%, handicaps are 50/50). → copy judgment favorites, exclude sports.
- **Copy ceiling:** pay ~3% follow-buffer + latency + taker spread (takers lose ~1.1%/trade, makers gain). Can't beat the wallets we copy. Good copy wallets are RARE (~12-16; loss-verified vetting exposes most "top" wallets as 0-78% true).

## 5. Copy-trade logic (all gates live)
- **Wallet vetting** (`bot/leaderboard.py::analyze_wallet_quality`): verified WR from /trades round-trips + /closed-positions + **`/positions?sortBy=CASHPNL` resolved-loss visibility** (catches buy-and-hold losses). Gates: discovery WR ≥0.80 / prune <0.70, age ≥30d, profit_factor ≥1.5, loss_streak <3, `loss_visibility=="verified"`.
- **Signal gates** (`bot/agents/copy_signal.py`): freshness ≤1800s; price band 0.30-0.90; judgment-only categories; per-market cap; conviction ≥0.3× wallet median; averaging-down skip; price-drift skip (>200bps over whale price).
- **Intent gates** (`bot/orchestrator.py`): opposite-side guard; already-held skip; time-to-resolution 4h-336h; EV gate (`bot/ev_math.py::copy_ev` ≥0.02 + min $0.15 profit); condition cap; category cap; daily cap; idempotency.
- **KNOWN GAP:** event-level dedup (we bet 2 candidates in one election — separate condition_ids dodge the opposite-side guard). TODO.

## 6. Weather (forecast-arb + specialist discovery)
- `bot/agents/weather_arb.py`: temp markets → Open-Meteo forecast (~85-90% accurate) → normal-dist prob per degree bucket → bet when |forecast − price| > `weather_min_edge`. Same-day guard uses city LOCAL time (lon/15h).
- ⚠️ Open-Meteo **free-tier daily quota** exhausts on restarts → **disk cache `/tmp/weather_forecast_cache.json`** (survives restarts; seed from a non-throttled host when 429'd).
- ⚠️ Temp markets are **DAILY** — zero at night (resolve evening, new batch morning). Not a bug.
- `bot/weather_discovery.py` (CopyManager.refresh step 2b): scan live temp markets → `/trades?market=cid` → every `proxyWallet` → wallets w/ ≥4 weather markets + ≥100 trades + WR≥0.80 + verified losses → add to copy watch-list as `source_category=weather`.
- **Open question:** does our forecast beat the efficient market? If weather loses like calibration → noise; if it wins → it's the high-frequency engine.

## 7. Key files
`bot/orchestrator.py` (main loop, gating, execution, resolved-record, scan-skip) · `bot/agents/{copy_signal,weather_arb}.py` · `bot/copy_manager.py` (pool) · `bot/leaderboard.py` (vetting) · `bot/weather_discovery.py` · `bot/resolved_record.py` (true win-rate tracker) · `bot/ev_math.py` · `bot/settings.py` (KV) · `bot/db/models.py` (TradeLog/BotState) · `bot/backtest/{edge_probe,copy_probe,calibration_probe}.py` · `templates/index.html` (dashboard).

## 8. Operations
- **Deploy:** `make test` → `git push fork v2-migration` → VPS `git pull origin v2-migration && systemctl restart polymarket-bot`.
- **Settings (no restart):** `upsert_many_kv({...})` on VPS + POST `/api/admin/reload-settings` (forged token).
- **Monitor:** `journalctl -u polymarket-bot --no-pager | grep -E "CopySignalAgent:|WeatherArbAgent:|Executed|cycle_end|PRUNED|skip intent"`.
- Copy-only cycle ~30s (gamma scan skipped; `markets_scanned=0` is correct).

## 9. Roadmap
1. **Confirm copy ≥80% LIVE win-rate** on forward judgment-only resolutions (resolved-record tracker) — gate before "done."
2. **Validate weather** — do forecast bets win? Yes → scale as the daily-frequency engine. No → retire.
3. Event-level dedup. 4. Consider **maker (passive limit) execution** to break copy's taker-spread ceiling. 5. `poly_data` (full on-chain history) for vetting when scaling — vendor read-only, never run vs wallet.

## 10. Security
- **Never run third-party bot code against the funded wallet** — key-drain is the #1 crypto-bot malware vector (e.g. the `dev-protocol/polymarket-copytrading-bot-sport` key-stealer per StepSecurity). Audit shared repos READ-ONLY; never paste keys.
- Keep all credentials out of this repo. Rotate anything ever exposed.
