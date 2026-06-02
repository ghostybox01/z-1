"""Copy-trade edge probe: replay watched wallets' historical BUYs to resolution.

Question this answers
---------------------
If we COPY the BUY trades of "good" Polymarket wallets — paying our real ~3%
follow-buffer on entry — and hold each position to market resolution, do we make
money AFTER that buffer?  The verdict gates a live copy strategy, so correctness
is prioritised over speed.

This is READ-ONLY analysis.  No trading, no order placement, no wallet keys.

How it mirrors live
-------------------
We reuse the EXACT copy logic the live bot uses so the simulation matches what we
would actually do:

  * ``build_candidate``        — turn a raw /trades entry into a CopyCandidate.
  * ``passes_filters``         — apply copy_min_price, blocked keywords, category
                                 / outcome gates, size gates, etc.
  * ``limit_price_with_buffer``— add the ~3% buffer to the whale's price; this is
                                 our simulated entry price (the whole point: does
                                 edge survive the pad?).
  * ``Settings.load()``        — load the live settings the gates read from.

P&L model (held to resolution, no look-ahead)
---------------------------------------------
For each copied BUY signal:

  entry    = limit_price_with_buffer(settings, whale_buy_price)   # our cost / $1
  resolved = final outcome price of the COPIED token  ∈ {0.0, 1.0}
  pnl_per_$ = (resolved - entry) / entry
  win       = resolved >= 0.5

``entry`` is the whale's *historical* trade price (× buffer); ``resolved`` is the
*final* resolution.  There is no look-ahead: we only ever model holding the same
side the whale bought until the market resolves.  Markets that have NOT resolved
yet are SKIPPED and counted separately (skipped-open) — we never guess an
outcome.

Resolution data sources (correctness-critical)
----------------------------------------------
Empirically (verified 2026-06), Gamma's ``/markets?condition_ids=`` index is
INCOMPLETE: for an active top wallet, ~2/3 of its traded markets are simply not
returned by Gamma at all (they return an empty list even queried individually),
and the ones Gamma *does* return tend to be the still-open ones.  Relying on
Gamma alone yields ~zero resolved copies and a meaningless verdict.

The CLOB markets endpoint ``https://clob.polymarket.com/markets/<conditionId>``
DOES carry those markets with full resolution (`closed` + per-token `price`
0/1 + `winner`).  We therefore resolve in two stages, both cached by
condition_id:

  1. Gamma ``/markets?condition_ids=<cid>``  (parse outcomePrices ‖ clobTokenIds)
  2. CLOB ``/markets/<cid>``                 (parse tokens[].price / .winner)

A market only counts as RESOLVED when it is closed AND the copied token's
resolved price is unambiguously 0 or 1 (within tolerance).  Anything else is
skipped-open.

Survivorship caveat
-------------------
The wallet set is pre-selected for success (our hand-picked watch wallets plus
leaderboard winners).  A positive result here is necessary but NOT sufficient to
prove live edge: tomorrow's copyable wallets are not guaranteed to be as good,
and leaderboard ranking is itself an outcome of the very trades we're scoring.
Read the number as "did copying these known-good wallets clear the buffer in
their realised history", not "copy-trading is guaranteed +EV going forward".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("polymarket.backtest.copy_probe")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Hand-picked, proven watch-wallets (the seed set we already trust).
SEED_WALLETS: list[str] = [
    "0x0c0e270cf879583d6a0142fc817e05b768d0434e",
    "0xfbf3d501e88815464642d0e913f15379c3eeb218",
    "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae",
    "0xab828a2bcb4a5a93a94cdeedf3cb70b6211babe5",
    "0x0dcf5b49627c5194805bd1af846fd0bd3388c556",
]

# Categories to mine for additional copyable wallets via the leaderboard.
DISCOVER_CATEGORIES: list[str] = ["SPORTS", "POLITICS", "FINANCE"]

# A resolved binary outcome must be within this tolerance of 0 or 1.
_RESOLVE_TOL = 0.02


# ---------------------------------------------------------------------------
# Pure, testable core
# ---------------------------------------------------------------------------

@dataclass
class WalletResult:
    wallet: str
    n_buys: int = 0              # raw BUY signals seen
    n_passed: int = 0           # passed build_candidate + passes_filters
    n_resolved: int = 0         # resolved markets actually scored
    n_skipped_open: int = 0     # passed filters but market not resolved yet
    n_skipped_filter: int = 0   # rejected by build_candidate / passes_filters
    n_skipped_error: int = 0    # resolution lookup failed / unparseable
    wins: int = 0
    total_pnl_per_dollar: float = 0.0   # sum of (resolved-entry)/entry
    sum_bps: float = 0.0                 # sum of per-copy pnl in bps

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_resolved if self.n_resolved else 0.0

    @property
    def mean_pnl_per_dollar(self) -> float:
        return self.total_pnl_per_dollar / self.n_resolved if self.n_resolved else 0.0

    @property
    def mean_pnl_bps(self) -> float:
        return self.sum_bps / self.n_resolved if self.n_resolved else 0.0


@dataclass
class ProbeAggregate:
    n_wallets: int = 0
    n_buys: int = 0
    n_passed: int = 0
    n_resolved: int = 0
    n_skipped_open: int = 0
    n_skipped_filter: int = 0
    n_skipped_error: int = 0
    wins: int = 0
    total_pnl_per_dollar: float = 0.0
    sum_bps: float = 0.0
    per_wallet: list[WalletResult] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_resolved if self.n_resolved else 0.0

    @property
    def mean_pnl_per_dollar(self) -> float:
        return self.total_pnl_per_dollar / self.n_resolved if self.n_resolved else 0.0

    @property
    def mean_pnl_bps(self) -> float:
        return self.sum_bps / self.n_resolved if self.n_resolved else 0.0


def score_copy(entry_price: float, resolved_price: float) -> tuple[float, float, bool]:
    """Pure P&L for one held-to-resolution copy.

    entry_price    : our simulated cost per $1 (whale price × buffer), in (0,1].
    resolved_price : final outcome of the copied token, 0.0 or 1.0.

    Returns (pnl_per_dollar, pnl_bps, win) where
        pnl_per_dollar = (resolved - entry) / entry
        pnl_bps        = pnl_per_dollar * 10_000
        win            = resolved >= 0.5
    """
    if entry_price <= 0:
        return 0.0, 0.0, resolved_price >= 0.5
    pnl = (resolved_price - entry_price) / entry_price
    return pnl, pnl * 10_000.0, (resolved_price >= 0.5)


def parse_gamma_resolution(market: dict[str, Any], token_id: str) -> Optional[float]:
    """Extract the resolved price (0/1) for ``token_id`` from a Gamma market dict.

    Gamma returns ``clobTokenIds`` and ``outcomePrices`` as JSON-string arrays
    that are positionally parallel.  We only treat a market as resolved when it
    is ``closed`` and the matched outcome price is ~0 or ~1.  Returns None when
    not resolved / not parseable / token not found.
    """
    if not market.get("closed"):
        return None

    raw_tokens = market.get("clobTokenIds", market.get("clob_token_ids", ""))
    raw_prices = market.get("outcomePrices", market.get("outcome_prices", ""))

    tokens = _coerce_json_list(raw_tokens)
    prices = _coerce_json_list(raw_prices)
    if not tokens or not prices or len(tokens) != len(prices):
        return None

    try:
        idx = [str(t) for t in tokens].index(str(token_id))
    except ValueError:
        return None

    try:
        resolved = float(prices[idx])
    except (TypeError, ValueError):
        return None

    return _snap_binary(resolved)


def parse_clob_resolution(market: dict[str, Any], token_id: str) -> Optional[float]:
    """Extract the resolved price (0/1) for ``token_id`` from a CLOB market dict.

    CLOB ``/markets/<cid>`` returns ``{"closed":bool, "tokens":[{"token_id","price",
    "winner","outcome"}, ...]}``.  For a resolved market the winning token has
    price 1 and the loser 0.  Prefer the explicit ``price``; fall back to the
    ``winner`` boolean.  Returns None when not resolved / token not found.
    """
    if not market.get("closed"):
        return None
    tokens = market.get("tokens")
    if not isinstance(tokens, list):
        return None
    for tk in tokens:
        if not isinstance(tk, dict):
            continue
        if str(tk.get("token_id")) != str(token_id):
            continue
        # Prefer explicit price field.
        raw = tk.get("price")
        if raw is not None:
            try:
                snapped = _snap_binary(float(raw))
                if snapped is not None:
                    return snapped
            except (TypeError, ValueError):
                pass
        # Fall back to winner flag.
        if "winner" in tk:
            return 1.0 if bool(tk.get("winner")) else 0.0
        return None
    return None


def _coerce_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _snap_binary(value: float) -> Optional[float]:
    """Snap a near-0/near-1 resolution price to exactly 0.0/1.0; else None."""
    if value <= _RESOLVE_TOL:
        return 0.0
    if value >= 1.0 - _RESOLVE_TOL:
        return 1.0
    return None


# ---------------------------------------------------------------------------
# Network layer
# ---------------------------------------------------------------------------

async def fetch_wallet_buys(http: httpx.AsyncClient, wallet: str, *, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch a wallet's recent trades and return only BUY entries (entry signals)."""
    try:
        r = await http.get(f"{DATA_API}/trades", params={"user": wallet, "limit": str(limit)})
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("trades fetch failed for %s: %s", wallet[:12], exc)
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if str(t.get("side", "")).upper() == "BUY"]


async def _get_retry(http: httpx.AsyncClient, url: str, *, params: Any = None, attempts: int = 5):
    """GET with exponential backoff on 429/5xx so rate limits don't masquerade
    as resolution errors (the bug that voided the first probe run)."""
    delay = 0.5
    r = None
    for _ in range(attempts):
        try:
            r = await http.get(url, params=params)
        except Exception:
            r = None
        if r is not None and r.status_code == 200:
            return r
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return r  # non-retryable (e.g. 404) — hand back as-is
        await asyncio.sleep(delay)
        delay = min(delay * 2, 8.0)
    return r


async def resolve_condition(
    http: httpx.AsyncClient,
    condition_id: str,
    token_id: str,
    cache: dict[str, Optional[dict[str, Any]]],
) -> tuple[str, Optional[float]]:
    """Resolve the final outcome (0/1) of ``token_id`` in market ``condition_id``.

    Two-stage, both cached by condition_id (trades share markets heavily):
      1. Gamma /markets?condition_ids=<cid>
      2. CLOB  /markets/<cid>   (covers the many markets Gamma omits)

    Returns (status, resolved_price) where status is one of:
      "resolved" (resolved_price is 0.0/1.0), "open" (market not resolved yet,
      resolved_price None), "error" (lookup/parse failed, None).
    """
    cached = cache.get(condition_id, "MISS")
    if cached != "MISS":
        return _interpret_market(cached, token_id)

    market: Optional[dict[str, Any]] = None

    # --- Stage 1: Gamma -----------------------------------------------------
    try:
        r = await _get_retry(http, f"{GAMMA_API}/markets", params={"condition_ids": condition_id})
        if r is not None and r.status_code == 200:
            arr = r.json()
            if isinstance(arr, list) and arr:
                market = {"_src": "gamma", **arr[0]}
    except Exception as exc:
        log.debug("gamma resolve %s: %s", condition_id[:14], exc)

    # --- Stage 2: CLOB fallback (much higher coverage) ----------------------
    # Use CLOB when Gamma had nothing, OR when Gamma returned an unresolved
    # market (CLOB sometimes has resolution Gamma hasn't propagated).
    need_clob = market is None or not market.get("closed")
    if need_clob:
        try:
            r = await _get_retry(http, f"{CLOB_API}/markets/{condition_id}")
            if r is not None and r.status_code == 200:
                j = r.json()
                if isinstance(j, dict) and j:
                    clob_market = {"_src": "clob", **j}
                    # Prefer CLOB if it has a resolution, or if Gamma had nothing.
                    if clob_market.get("closed") or market is None:
                        market = clob_market
        except Exception as exc:
            log.debug("clob resolve %s: %s", condition_id[:14], exc)

    cache[condition_id] = market
    return _interpret_market(market, token_id)


def _interpret_market(market: Optional[dict[str, Any]], token_id: str) -> tuple[str, Optional[float]]:
    """Map a cached market dict to (status, resolved_price) for a token."""
    if market is None:
        return "error", None
    src = market.get("_src")
    resolved: Optional[float]
    if src == "gamma":
        resolved = parse_gamma_resolution(market, token_id)
    elif src == "clob":
        resolved = parse_clob_resolution(market, token_id)
    else:
        # Unknown shape: try both parsers.
        resolved = parse_gamma_resolution(market, token_id)
        if resolved is None:
            resolved = parse_clob_resolution(market, token_id)
    if resolved is not None:
        return "resolved", resolved
    if market.get("closed"):
        # Closed but we couldn't pin the token to a 0/1 price (e.g. token not in
        # this market's list, or ambiguous price) — treat as error, not a win.
        return "error", None
    return "open", None


async def discover_wallets(http: httpx.AsyncClient, *, per_category: int = 10) -> list[str]:
    """Seed wallets + leaderboard-discovered wallets (SPORTS/POLITICS/FINANCE), deduped."""
    from bot.leaderboard import fetch_leaderboard

    seen: set[str] = set()
    out: list[str] = []
    for w in SEED_WALLETS:
        wl = w.strip().lower()
        if wl and wl not in seen:
            seen.add(wl)
            out.append(wl)

    for cat in DISCOVER_CATEGORIES:
        try:
            rows = await fetch_leaderboard(http, category=cat, time_period="MONTH", limit=per_category)
        except Exception as exc:
            log.warning("leaderboard %s failed: %s", cat, exc)
            continue
        for e in rows:
            w = (e.get("proxyWallet") or "").strip().lower()
            if w.startswith("0x") and len(w) == 42 and w not in seen:
                seen.add(w)
                out.append(w)

    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def run_copy_probe(
    *,
    per_category: int = 10,
    trades_per_wallet: int = 500,
    max_wallets: int = 0,
) -> ProbeAggregate:
    """Replay every watched wallet's BUYs to resolution and aggregate edge.

    ``max_wallets`` (0 = no cap) limits how many wallets we process — handy for a
    quick smoke run.
    """
    from bot.copy_rules import build_candidate, passes_filters, limit_price_with_buffer
    from bot.settings import Settings

    settings = Settings.load()
    buffer_bps = float(getattr(settings, "copy_price_buffer_bps", 300.0) or 300.0)

    agg = ProbeAggregate()
    cache: dict[str, Optional[dict[str, Any]]] = {}
    CONCURRENCY = 5

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "copy-probe/1.0"}) as http:
        wallets = await discover_wallets(http, per_category=per_category)
        if max_wallets > 0:
            wallets = wallets[:max_wallets]
        agg.n_wallets = len(wallets)
        log.info("probing %d wallets (buffer=%.0f bps)", len(wallets), buffer_bps)

        # --- Pass 1: fetch every wallet's BUYs concurrently, build copy records.
        async def _get_buys(w: str) -> tuple[str, list[dict[str, Any]]]:
            return w, await fetch_wallet_buys(http, w, limit=trades_per_wallet)

        buys_by_wallet = await asyncio.gather(*[_get_buys(w) for w in wallets])

        wr_map: dict[str, WalletResult] = {w: WalletResult(wallet=w) for w in wallets}
        copies: list[tuple[str, str, str, float]] = []  # wallet, token_id, condition_id, entry_px
        unique_cids: set[str] = set()
        for wallet, buys in buys_by_wallet:
            wr = wr_map[wallet]
            wr.n_buys = len(buys)
            log.info("%s — %d BUY signals", wallet[:12], len(buys))
            for entry in buys:
                c = build_candidate(entry, wallet, default_bet_usd=1.0)
                if c is None:
                    wr.n_skipped_filter += 1
                    continue
                ok, _why = passes_filters(settings, c)
                if not ok:
                    wr.n_skipped_filter += 1
                    continue
                wr.n_passed += 1
                entry_px = limit_price_with_buffer(settings, c.price)
                condition_id = str(entry.get("conditionId") or entry.get("condition_id") or "")
                if not condition_id:
                    wr.n_skipped_error += 1
                    continue
                copies.append((wallet, c.token_id, condition_id, entry_px))
                unique_cids.add(condition_id)

        # --- Pass 2: resolve every UNIQUE market once, concurrently (the slow I/O).
        cid_list = list(unique_cids)
        log.info(
            "resolving %d unique markets for %d copies (concurrency=%d)",
            len(cid_list), len(copies), CONCURRENCY,
        )
        sem = asyncio.Semaphore(CONCURRENCY)

        async def _resolve_one(cid: str) -> None:
            async with sem:
                try:
                    # token_id is irrelevant here — resolve_condition populates the
                    # cache (market dict) before interpreting; we re-interpret per
                    # token in Pass 3. Swallow interpret errors from the dummy token.
                    await resolve_condition(http, cid, "", cache)
                except Exception:
                    cache.setdefault(cid, cache.get(cid))

        CHUNK = 300
        for i in range(0, len(cid_list), CHUNK):
            await asyncio.gather(*[_resolve_one(cid) for cid in cid_list[i:i + CHUNK]])
            log.info("  resolved %d/%d markets", min(i + CHUNK, len(cid_list)), len(cid_list))

        # --- Pass 3: score every copy from the cache (no I/O).
        sample_rows: list[tuple[str, float, float, float, int]] = []
        for wallet, token_id, condition_id, entry_px in copies:
            wr = wr_map[wallet]
            try:
                status, resolved = _interpret_market(cache.get(condition_id), token_id)
            except Exception:
                wr.n_skipped_error += 1
                continue
            if status == "open":
                wr.n_skipped_open += 1
                continue
            if status == "error" or resolved is None:
                wr.n_skipped_error += 1
                continue
            pnl, bps, win = score_copy(entry_px, resolved)
            wr.n_resolved += 1
            wr.wins += int(win)
            wr.total_pnl_per_dollar += pnl
            wr.sum_bps += bps
            sample_rows.append((wallet, entry_px, resolved, pnl, int(win)))

        # Dump per-copy samples so we can report MEDIAN + by-price-bucket stats
        # (the mean is dominated by rare longshot jackpots and is misleading).
        try:
            with open("/tmp/copy_probe_samples.csv", "w") as f:
                f.write("wallet,entry,resolved,pnl_per_dollar,win\n")
                for row in sample_rows:
                    f.write("%s,%.4f,%.1f,%.4f,%d\n" % row)
            log.info("wrote %d copy samples to /tmp/copy_probe_samples.csv", len(sample_rows))
        except Exception as exc:
            log.warning("sample csv write failed: %s", exc)

        for w in wallets:
            wr = wr_map[w]
            agg.per_wallet.append(wr)
            agg.n_buys += wr.n_buys
            agg.n_passed += wr.n_passed
            agg.n_resolved += wr.n_resolved
            agg.n_skipped_open += wr.n_skipped_open
            agg.n_skipped_filter += wr.n_skipped_filter
            agg.n_skipped_error += wr.n_skipped_error
            agg.wins += wr.wins
            agg.total_pnl_per_dollar += wr.total_pnl_per_dollar
            agg.sum_bps += wr.sum_bps

    _print_verdict(agg, buffer_bps)
    return agg


def _print_verdict(agg: ProbeAggregate, buffer_bps: float) -> None:
    print(f"\n{'='*72}")
    print(f"Copy-Trade Edge Probe  —  {agg.n_wallets} wallets, ~{buffer_bps:.0f} bps follow-buffer")
    print(f"{'='*72}")
    print(f"  Raw BUY signals seen:        {agg.n_buys}")
    print(f"  Passed copy filters:         {agg.n_passed}")
    print(f"  RESOLVED (scored copies):    {agg.n_resolved}")
    print(f"  Skipped — still open:        {agg.n_skipped_open}")
    print(f"  Skipped — filtered out:      {agg.n_skipped_filter}")
    print(f"  Skipped — resolution error:  {agg.n_skipped_error}")
    print(f"  {'-'*60}")

    if agg.n_resolved == 0:
        print("  VERDICT: NO RESOLVED COPIES — insufficient data to judge edge.")
        print(f"{'='*72}\n")
        return

    wr = agg.win_rate
    mean_d = agg.mean_pnl_per_dollar
    mean_bps = agg.mean_pnl_bps
    total_pnl = agg.total_pnl_per_dollar  # == total $ P&L at $1/copy

    print(f"  Win-rate:                    {wr:.1%}  ({agg.wins}/{agg.n_resolved})")
    print(f"  Mean P&L per $1 staked:      {mean_d:+.4f}   ({mean_bps:+.1f} bps/copy)")
    print(f"  Total P&L @ $1/copy:         {total_pnl:+.2f}  over {agg.n_resolved} copies")
    print(f"  {'-'*60}")

    sign = "CLEARS" if mean_bps > 0 else "DOES NOT CLEAR"
    print(
        f"  VERDICT: Copy edge after {buffer_bps:.0f} bps buffer: "
        f"{mean_bps:+.1f} bps/copy, win-rate {wr:.1%} over {agg.n_resolved} copies "
        f"— {sign} the buffer."
    )
    print(f"{'='*72}\n")

    # Per-wallet table (resolved only, sorted by mean bps).
    rows = [w for w in agg.per_wallet if w.n_resolved > 0]
    rows.sort(key=lambda w: w.mean_pnl_bps, reverse=True)
    print(f"  {'wallet':<14} {'n_res':>6} {'n_open':>7} {'win_rate':>9} {'mean_bps':>10}")
    print(f"  {'-'*14} {'-'*6} {'-'*7} {'-'*9} {'-'*10}")
    for w in rows:
        print(
            f"  {w.wallet[:12]+'..':<14} {w.n_resolved:>6} {w.n_skipped_open:>7} "
            f"{w.win_rate:>8.1%} {w.mean_pnl_bps:>+10.1f}"
        )
    skipped_all = [w for w in agg.per_wallet if w.n_resolved == 0]
    if skipped_all:
        print(f"  ({len(skipped_all)} wallets had 0 resolved copies — all open/filtered)")
    print()
    print("  CAVEAT (survivorship): these wallets are pre-selected winners (hand-picked")
    print("  watch list + leaderboard rank). A positive number is necessary but NOT")
    print("  sufficient evidence that copying arbitrary 'good' wallets is +EV going forward.")
    print(f"{'='*72}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import pathlib

    _repo_root = str(pathlib.Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        asyncio.run(run_copy_probe())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    except Exception as _exc:  # pragma: no cover
        print(f"ERROR: {_exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
