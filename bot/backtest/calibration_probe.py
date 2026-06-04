"""Favorite-longshot calibration probe: is tail mispricing REAL & PROFITABLE?

Question this answers
---------------------
Becker's 72M-trade Kalshi study found a "favorite-longshot bias": cheap
contracts (< 0.15) win LESS than their price implies (overpriced → you should
SELL them / buy the opposite NO), while expensive contracts (> 0.85) win MORE
than implied (underpriced → BUY them).  The middle (~0.30-0.70) is roughly
calibrated.  Before we build an agent around this, we measure — on REAL
Polymarket trade data — whether that same shape exists here AND whether the
edge survives a realistic 2% round-trip cost (spread + fees).

This is READ-ONLY analysis.  No trading, no order placement, no wallet keys.

Method (mirrors the spec)
-------------------------
1. Gather a BROAD sample of real BUY trades across MANY wallets of MIXED quality
   (leaderboard OVERALL+POLITICS+SPORTS+CRYPTO+FINANCE, a RANGE of ranks — not
   just the top — to dilute survivorship bias).  Each BUY gives us a contract we
   can observe at its entry PRICE.
2. Resolve each bought token to its final 0/1 outcome using copy_probe's
   two-stage (Gamma → CLOB) resolver, cached by condition_id.  Only RESOLVED
   markets count; still-open markets are skipped.
3. Build the empirical CALIBRATION CURVE: bucket each resolved trade by entry
   price, and per bucket report n, mean_price (implied prob), actual_win_rate,
   and gap = actual - implied.  The favorite-longshot signature is gap < 0 in
   the low buckets and gap > 0 in the high buckets.
4. SIMULATE the strategy after a per-trade COST (default 2%):
     * contract at price p < LONGSHOT_MAX  ->  BUY THE OPPOSITE (NO):
         entry      = (1 - p) + COST
         no_resolved = 1 - token_resolved          (1 if token lost, else 0)
         pnl/$1     = (no_resolved - entry) / entry
     * contract at price p > FAVORITE_MIN  ->  BUY IT (YES):
         entry      = p + COST
         pnl/$1     = (token_resolved - entry) / entry
   One bet per resolved trade in those price ranges.  Aggregate n / win-rate /
   mean & median pnl per $1 (and bps) — for the longshot-NO side, the
   favorite-YES side, and combined.
5. Print the calibration table, the strategy simulation, and a one-line VERDICT
   for each side.

Survivorship caveat (stated honestly in the verdict)
----------------------------------------------------
The trade universe is sourced from leaderboard wallets.  Even taking a wide
range of ranks, these are wallets that traded enough to appear at all — their
PICKS may be better than the market average, which would INFLATE absolute win
rates.  So read the calibration GAP / SHAPE at the tails (does cheap underperform
its price, does expensive overperform) as the robust signal, not the absolute
levels — and treat a positive simulated return as suggestive, not proof.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

# REUSE copy_probe's proven resolution machinery (Gamma + CLOB, retry/backoff).
from bot.backtest.copy_probe import (
    DATA_API,
    _get_retry,  # noqa: F401  (kept available; resolve_condition uses it internally)
    _interpret_market,
    resolve_condition,
)

log = logging.getLogger("polymarket.backtest.calibration_probe")

# --- Strategy / cost constants (configurable) ------------------------------
# Per-trade round-trip cost (spread + fees) applied to our ENTRY price.
COST = 0.02
# A contract is a "longshot" (candidate to fade / buy the opposite NO) below this.
LONGSHOT_MAX = 0.15
# A contract is a "favorite" (candidate to buy) above this.
FAVORITE_MIN = 0.85

# Calibration price buckets: (lo, hi) half-open [lo, hi).  The final bucket is
# inclusive of 1.0.  Chosen to be fine at the tails (where the edge lives) and
# coarse in the well-calibrated middle, per the spec.
BUCKETS: list[tuple[float, float]] = [
    (0.00, 0.05),
    (0.05, 0.10),
    (0.10, 0.15),
    (0.15, 0.20),
    (0.20, 0.30),
    (0.30, 0.50),
    (0.50, 0.70),
    (0.70, 0.80),
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.95),
    (0.95, 1.00),
]

# Leaderboard categories to mine for a BROAD, mixed-quality wallet set.
DISCOVER_CATEGORIES: list[str] = ["OVERALL", "POLITICS", "SPORTS", "CRYPTO", "FINANCE"]


# ---------------------------------------------------------------------------
# Pure, testable core
# ---------------------------------------------------------------------------

def bucket_index(price: float) -> Optional[int]:
    """Return the index of the calibration bucket containing ``price``.

    Buckets are half-open [lo, hi); the final bucket includes 1.0.  Prices
    outside [0, 1] return None.
    """
    if price < 0.0 or price > 1.0:
        return None
    for i, (lo, hi) in enumerate(BUCKETS):
        # Last bucket is inclusive on the right so price == 1.0 lands somewhere.
        if i == len(BUCKETS) - 1:
            if lo <= price <= hi:
                return i
        elif lo <= price < hi:
            return i
    return None


def score_favorite(entry_price: float, token_resolved: float) -> tuple[float, bool]:
    """P&L per $1 for BUYING a favorite contract (the YES side) at ``entry_price``.

    entry_price    : our cost per $1 = p + COST (clamped to <= 1 not required;
                     a cost-padded near-1 favorite just earns ~0 if it wins).
    token_resolved : final outcome of the bought token, 0.0 or 1.0.

    pnl/$1 = (token_resolved - entry_price) / entry_price ; win iff token won.
    """
    if entry_price <= 0:
        return 0.0, token_resolved >= 0.5
    pnl = (token_resolved - entry_price) / entry_price
    return pnl, token_resolved >= 0.5


def score_longshot_no(entry_price: float, token_resolved: float) -> tuple[float, bool]:
    """P&L per $1 for FADING a longshot — i.e. BUYING THE OPPOSITE (NO) side.

    The observed token trades cheap (p < LONGSHOT_MAX); the thesis says it is
    overpriced, so we buy the opposite outcome.

    entry_price    : our cost per $1 on the NO = (1 - p) + COST.
    token_resolved : final outcome of the ORIGINAL (cheap) token, 0.0 or 1.0.
                     The NO pays $1 when the original token LOSES (resolves 0).

    no_resolved = 1 - token_resolved
    pnl/$1      = (no_resolved - entry_price) / entry_price ; win iff token lost.
    """
    if entry_price <= 0:
        return 0.0, token_resolved < 0.5
    no_resolved = 1.0 - token_resolved
    pnl = (no_resolved - entry_price) / entry_price
    return pnl, token_resolved < 0.5


@dataclass
class Bucket:
    lo: float
    hi: float
    n: int = 0
    sum_price: float = 0.0
    wins: int = 0  # number of bought tokens that resolved to 1

    @property
    def mean_price(self) -> float:
        return self.sum_price / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def gap(self) -> float:
        """actual_win_rate - implied_prob (mean entry price)."""
        return self.win_rate - self.mean_price


@dataclass
class SideStats:
    """Aggregates for one leg of the strategy (longshot-NO or favorite-YES)."""

    label: str
    n: int = 0
    wins: int = 0
    pnls: list[float] = field(default_factory=list)  # per-$1 pnl per bet

    def add(self, pnl: float, win: bool) -> None:
        self.n += 1
        self.wins += int(win)
        self.pnls.append(pnl)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def mean_pnl(self) -> float:
        return (sum(self.pnls) / self.n) if self.n else 0.0

    @property
    def median_pnl(self) -> float:
        return statistics.median(self.pnls) if self.pnls else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(self.pnls)


@dataclass
class CalibrationResult:
    n_wallets: int = 0
    n_buys: int = 0           # raw BUY trades collected
    n_unique_markets: int = 0
    n_resolved: int = 0       # trades whose market resolved 0/1
    n_skipped_open: int = 0   # market not resolved yet
    n_skipped_error: int = 0  # resolution lookup/parse failed
    buckets: list[Bucket] = field(default_factory=list)
    longshot: SideStats = field(default_factory=lambda: SideStats("LONGSHOT-NO  (fade p<%.2f)" % LONGSHOT_MAX))
    favorite: SideStats = field(default_factory=lambda: SideStats("FAVORITE-YES (buy  p>%.2f)" % FAVORITE_MIN))
    combined: SideStats = field(default_factory=lambda: SideStats("COMBINED"))


def build_buckets() -> list[Bucket]:
    return [Bucket(lo, hi) for (lo, hi) in BUCKETS]


def tally_trade(
    result: CalibrationResult,
    price: float,
    token_resolved: float,
) -> None:
    """Fold one RESOLVED trade into the calibration buckets + strategy sides.

    Pure (no I/O).  ``price`` is the entry price of the bought token;
    ``token_resolved`` is its final 0/1 outcome.
    """
    bi = bucket_index(price)
    if bi is not None:
        b = result.buckets[bi]
        b.n += 1
        b.sum_price += price
        b.wins += int(token_resolved >= 0.5)

    # Strategy legs (one bet per qualifying resolved trade).
    if price < LONGSHOT_MAX:
        entry = (1.0 - price) + COST
        pnl, win = score_longshot_no(entry, token_resolved)
        result.longshot.add(pnl, win)
        result.combined.add(pnl, win)
    elif price > FAVORITE_MIN:
        entry = price + COST
        pnl, win = score_favorite(entry, token_resolved)
        result.favorite.add(pnl, win)
        result.combined.add(pnl, win)


# ---------------------------------------------------------------------------
# Network layer
# ---------------------------------------------------------------------------

async def fetch_wallet_buys(http: httpx.AsyncClient, wallet: str, *, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch a wallet's recent trades; return only BUY entries (entry signals)."""
    try:
        r = await _get_retry(http, f"{DATA_API}/trades", params={"user": wallet, "limit": str(limit)})
        if r is None or r.status_code != 200:
            return []
        data = r.json()
    except Exception as exc:
        log.warning("trades fetch failed for %s: %s", wallet[:12], exc)
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if str(t.get("side", "")).upper() == "BUY"]


async def discover_mixed_wallets(
    http: httpx.AsyncClient,
    *,
    per_category: int = 50,
    rank_stride: int = 1,
) -> list[str]:
    """Discover a BROAD, mixed-quality wallet set from the leaderboard.

    Pulls the top ``per_category`` wallets in each of OVERALL/POLITICS/SPORTS/
    CRYPTO/FINANCE over MONTH and ALL windows, then optionally thins by
    ``rank_stride`` to sample a RANGE of ranks rather than only the very top
    (reducing — not eliminating — survivorship bias).  Deduped, order-stable.
    """
    from bot.leaderboard import fetch_leaderboard

    seen: set[str] = set()
    out: list[str] = []
    for time_period in ("MONTH", "ALL"):
        for cat in DISCOVER_CATEGORIES:
            try:
                rows = await fetch_leaderboard(
                    http, category=cat, time_period=time_period, limit=per_category
                )
            except Exception as exc:
                log.warning("leaderboard %s/%s failed: %s", cat, time_period, exc)
                continue
            # Sample a range of ranks (stride) so we are not pure top-of-book.
            for e in rows[::rank_stride]:
                w = (e.get("proxyWallet") or "").strip().lower()
                if w.startswith("0x") and len(w) == 42 and w not in seen:
                    seen.add(w)
                    out.append(w)
            await asyncio.sleep(0.2)  # throttle
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def run_calibration_probe(
    *,
    per_category: int = 50,
    trades_per_wallet: int = 500,
    max_wallets: int = 0,
    target_trades: int = 10_000,
) -> CalibrationResult:
    """Build the empirical calibration curve + simulate the FLB strategy.

    ``max_wallets`` (0 = no cap) limits wallets processed (smoke runs).
    ``target_trades`` is informational — we collect from all discovered wallets
    and report how many BUYs we gathered against this target.
    """
    result = CalibrationResult()
    result.buckets = build_buckets()
    cache: dict[str, Optional[dict[str, Any]]] = {}
    CONCURRENCY = 5

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "calibration-probe/1.0"}) as http:
        wallets = await discover_mixed_wallets(http, per_category=per_category)
        if max_wallets > 0:
            wallets = wallets[:max_wallets]
        result.n_wallets = len(wallets)
        log.info("discovered %d mixed-quality wallets (target %d trades)", len(wallets), target_trades)

        # --- Pass 1: fetch every wallet's BUYs (throttled, modest concurrency).
        sem_fetch = asyncio.Semaphore(CONCURRENCY)

        async def _get_buys(w: str) -> tuple[str, list[dict[str, Any]]]:
            async with sem_fetch:
                buys = await fetch_wallet_buys(http, w, limit=trades_per_wallet)
                await asyncio.sleep(0.1)  # throttle between wallet fetches
                return w, buys

        buys_by_wallet = await asyncio.gather(*[_get_buys(w) for w in wallets])

        # (price, condition_id, token_id) for every BUY with the fields we need.
        trades: list[tuple[float, str, str]] = []
        unique_cids: set[str] = set()
        for wallet, buys in buys_by_wallet:
            for entry in buys:
                token_id = str(entry.get("asset") or "")
                condition_id = str(entry.get("conditionId") or entry.get("condition_id") or "")
                raw_price = entry.get("price")
                if not token_id or not condition_id or raw_price is None:
                    continue
                try:
                    price = float(raw_price)
                except (TypeError, ValueError):
                    continue
                if not (0.0 < price < 1.0):
                    continue  # degenerate / already-resolved-looking print
                trades.append((price, condition_id, token_id))
                unique_cids.add(condition_id)
        result.n_buys = len(trades)
        result.n_unique_markets = len(unique_cids)
        log.info(
            "collected %d BUY trades across %d unique markets",
            result.n_buys, result.n_unique_markets,
        )

        # --- Pass 2: resolve each UNIQUE market once (the slow I/O), throttled.
        cid_list = list(unique_cids)
        sem = asyncio.Semaphore(CONCURRENCY)

        async def _resolve_one(cid: str) -> None:
            async with sem:
                try:
                    await resolve_condition(http, cid, "", cache)
                except Exception:
                    cache.setdefault(cid, cache.get(cid))
                await asyncio.sleep(0.05)  # gentle throttle

        CHUNK = 250
        for i in range(0, len(cid_list), CHUNK):
            await asyncio.gather(*[_resolve_one(cid) for cid in cid_list[i:i + CHUNK]])
            log.info("  resolved %d/%d markets", min(i + CHUNK, len(cid_list)), len(cid_list))

        # --- Pass 3: tally every trade from the cache (no I/O).
        for price, condition_id, token_id in trades:
            try:
                status, resolved = _interpret_market(cache.get(condition_id), token_id)
            except Exception:
                result.n_skipped_error += 1
                continue
            if status == "open":
                result.n_skipped_open += 1
                continue
            if status == "error" or resolved is None:
                result.n_skipped_error += 1
                continue
            result.n_resolved += 1
            tally_trade(result, price, resolved)

    _print_verdict(result)
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_calibration_table(result: CalibrationResult) -> None:
    print(f"\n{'='*78}")
    print("CALIBRATION CURVE  —  entry price (implied prob) vs ACTUAL resolution rate")
    print(f"{'='*78}")
    print(f"  {'bucket':<13} {'n':>7} {'mean_price':>11} {'win_rate':>10} {'gap':>9}  signature")
    print(f"  {'-'*13} {'-'*7} {'-'*11} {'-'*10} {'-'*9}  {'-'*9}")
    for b in result.buckets:
        if b.n == 0:
            sig = ""
            print(f"  [{b.lo:.2f},{b.hi:.2f})   {0:>7} {'-':>11} {'-':>10} {'-':>9}  {sig}")
            continue
        # FLB signature flags: cheap should under-resolve (gap<0), expensive over (gap>0).
        sig = ""
        if b.hi <= LONGSHOT_MAX:
            sig = "OVERPRICED" if b.gap < 0 else "(not overpriced)"
        elif b.lo >= FAVORITE_MIN:
            sig = "UNDERPRICED" if b.gap > 0 else "(not underpriced)"
        print(
            f"  [{b.lo:.2f},{b.hi:.2f})   {b.n:>7} {b.mean_price:>11.3f} "
            f"{b.win_rate:>10.3f} {b.gap:>+9.3f}  {sig}"
        )
    print(f"  {'-'*78}")
    print("  gap = actual_win_rate - mean_price (implied).  FLB: gap<0 in low buckets,")
    print("  gap>0 in high buckets.  The middle (0.30-0.70) should be ~0 (calibrated).")


def _side_verdict(side: SideStats) -> str:
    if side.n == 0:
        return f"  {side.label}: NO BETS in range — cannot judge."
    mean_bps = side.mean_pnl * 10_000.0
    med_bps = side.median_pnl * 10_000.0
    sign = "PROFITABLE" if side.mean_pnl > 0 else "UNPROFITABLE"
    return (
        f"  {side.label}: {sign} after {COST*100:.0f}% cost — "
        f"n={side.n}, win-rate {side.win_rate:.1%}, "
        f"mean {side.mean_pnl:+.4f}/$1 ({mean_bps:+.0f} bps), "
        f"median {side.median_pnl:+.4f}/$1 ({med_bps:+.0f} bps)."
    )


def _print_strategy_sim(result: CalibrationResult) -> None:
    print(f"\n{'='*78}")
    print(f"STRATEGY SIMULATION  —  favorite-longshot bets, {COST*100:.0f}% round-trip cost")
    print(f"{'='*78}")
    print(f"  {'side':<30} {'n':>6} {'win_rate':>9} {'mean/$1':>10} {'mean_bps':>10} {'med_bps':>9} {'tot_pnl':>9}")
    print(f"  {'-'*30} {'-'*6} {'-'*9} {'-'*10} {'-'*10} {'-'*9} {'-'*9}")
    for side in (result.longshot, result.favorite, result.combined):
        if side.n == 0:
            print(f"  {side.label:<30} {0:>6} {'-':>9} {'-':>10} {'-':>10} {'-':>9} {'-':>9}")
            continue
        print(
            f"  {side.label:<30} {side.n:>6} {side.win_rate:>8.1%} "
            f"{side.mean_pnl:>+10.4f} {side.mean_pnl*1e4:>+10.0f} "
            f"{side.median_pnl*1e4:>+9.0f} {side.total_pnl:>+9.2f}"
        )
    print(f"  {'-'*78}")
    print("  (one bet per resolved trade in range, $1 each; pnl/$1 is per-bet return.)")


def _print_verdict(result: CalibrationResult) -> None:
    print(f"\n{'='*78}")
    print(f"Favorite-Longshot Calibration Probe  —  {result.n_wallets} mixed-quality wallets")
    print(f"{'='*78}")
    print(f"  Raw BUY trades collected:     {result.n_buys}")
    print(f"  Unique markets:               {result.n_unique_markets}")
    print(f"  RESOLVED (scored) trades:     {result.n_resolved}")
    print(f"  Skipped — still open:         {result.n_skipped_open}")
    print(f"  Skipped — resolution error:   {result.n_skipped_error}")

    if result.n_resolved == 0:
        print(f"  {'-'*60}")
        print("  VERDICT: NO RESOLVED TRADES — insufficient data to judge calibration.")
        print(f"{'='*78}\n")
        return

    _print_calibration_table(result)
    _print_strategy_sim(result)

    # --- Per-side one-line verdicts -----------------------------------------
    print(f"\n{'='*78}")
    print("VERDICT")
    print(f"{'='*78}")
    print(_side_verdict(result.longshot))
    print(_side_verdict(result.favorite))
    print(_side_verdict(result.combined))
    print(f"  {'-'*76}")

    # Build / don't-build recommendation per side.
    ls, fv = result.longshot, result.favorite
    ls_go = ls.n >= 30 and ls.mean_pnl > 0 and ls.median_pnl >= 0
    fv_go = fv.n >= 30 and fv.mean_pnl > 0 and fv.median_pnl >= 0
    if ls_go and fv_go:
        rec = "BUILD BOTH sides — tail edge clears the 2% cost on the longshot AND favorite legs."
    elif fv_go and not ls_go:
        rec = "BUILD ONLY the FAVORITE-YES side — it clears costs; the longshot-NO leg does not."
    elif ls_go and not fv_go:
        rec = "BUILD ONLY the LONGSHOT-NO side — it clears costs; the favorite-YES leg does not."
    else:
        rec = "DO NOT BUILD — neither tail leg clears the 2% cost on this sample."
    print(f"  RECOMMENDATION: {rec}")
    print(f"  {'-'*76}")

    # --- Honest caveat -------------------------------------------------------
    print("  CAVEAT (survivorship): the trade universe comes from leaderboard wallets.")
    print("  Even sampling a range of ranks, these wallets traded enough to rank at all,")
    print("  so their PICKS may beat the market average — this INFLATES absolute win")
    print("  rates and simulated returns.  The robust signal is the calibration GAP /")
    print("  SHAPE at the tails (does cheap under-resolve, does expensive over-resolve),")
    print("  not the absolute levels.  Treat a positive sim as suggestive, not proof;")
    print("  a live agent must re-measure edge on its own executable fills.")
    print(f"{'='*78}\n")


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
        asyncio.run(run_calibration_probe())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    except Exception as _exc:  # pragma: no cover
        print(f"ERROR: {_exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
