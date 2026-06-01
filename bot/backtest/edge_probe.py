"""Edge probe: replay the value_edge passive-bid rule over CLOB price history.

Pure core (replay_passive_bid / ProbeResult) has no network dependencies and is
fully unit-testable.  The network layer (fetch_price_series / run_edge_probe) and
the CLI __main__ block require a live CLOB client and are exercised on the VPS.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("polymarket.backtest.edge_probe")

# ---------------------------------------------------------------------------
# Pure, testable core
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    n_signals: int = 0
    n_fills: int = 0
    fill_rate: float = 0.0
    total_pnl_per_share: float = 0.0   # sum over fills of (exit - entry - fees)
    mean_pnl_per_fill: float = 0.0
    mean_pnl_bps: float = 0.0          # mean per-fill pnl relative to entry, in bps
    win_rate: float = 0.0              # fraction of fills with pnl > 0


def replay_passive_bid(
    prices: list[float],
    *,
    entry_discount: float = 0.01,
    value_lo: float = 0.20,
    value_hi: float = 0.45,
    fill_window: int = 10,
    hold_horizon: int = 30,
    fee_bps: float = 0.0,
) -> ProbeResult:
    """Replay the value_edge passive-bid rule over a chronological mid-price series.

    For each index i where value_lo <= prices[i] <= value_hi (a 'signal'):
      entry = prices[i] * (1 - entry_discount)
      FILL MODEL (adverse selection): the bid fills at the first j in (i, i+fill_window]
        where prices[j] <= entry (price dropped to our bid). If no such j, no fill.
      On fill at j: exit_idx = min(j + hold_horizon, len(prices)-1)
        pnl_per_share = prices[exit_idx] - entry - (entry * fee_bps / 10000.0)
      Accumulate. Each i is an independent signal (a probe, not a portfolio sim).
    Returns ProbeResult with aggregates (guard divide-by-zero: 0.0 when n_fills==0).
    """
    n = len(prices)
    n_signals = 0
    fills_pnl: list[float] = []
    fills_bps: list[float] = []

    for i in range(n):
        p = prices[i]
        if not (value_lo <= p <= value_hi):
            continue
        n_signals += 1

        entry = p * (1.0 - entry_discount)

        # Adverse-selection fill model: only fills if price drops to our bid
        fill_j: int | None = None
        window_end = min(i + fill_window, n - 1)
        for j in range(i + 1, window_end + 1):
            if prices[j] <= entry:
                fill_j = j
                break

        if fill_j is None:
            continue  # no fill

        exit_idx = min(fill_j + hold_horizon, n - 1)
        fee = entry * fee_bps / 10_000.0
        pnl = prices[exit_idx] - entry - fee
        fills_pnl.append(pnl)
        fills_bps.append((pnl / entry) * 10_000.0 if entry > 0 else 0.0)

    n_fills = len(fills_pnl)
    if n_fills == 0:
        return ProbeResult(
            n_signals=n_signals,
            n_fills=0,
            fill_rate=0.0,
            total_pnl_per_share=0.0,
            mean_pnl_per_fill=0.0,
            mean_pnl_bps=0.0,
            win_rate=0.0,
        )

    total_pnl = sum(fills_pnl)
    mean_pnl = total_pnl / n_fills
    mean_bps = sum(fills_bps) / n_fills
    fill_rate = n_fills / n_signals if n_signals > 0 else 0.0
    win_rate = sum(1 for pnl in fills_pnl if pnl > 0) / n_fills

    return ProbeResult(
        n_signals=n_signals,
        n_fills=n_fills,
        fill_rate=fill_rate,
        total_pnl_per_share=total_pnl,
        mean_pnl_per_fill=mean_pnl,
        mean_pnl_bps=mean_bps,
        win_rate=win_rate,
    )


# ---------------------------------------------------------------------------
# Network layer: data fetch + driver
# ---------------------------------------------------------------------------

def fetch_price_series(
    client: Any,
    token_id: str,
    interval: str = "1m",
    fidelity: int = 60,
) -> list[float]:
    """Return chronological mid prices from CLOB get_prices_history.

    Build PricesHistoryParams(market=token_id, interval=interval, fidelity=fidelity).
    The response is typically {"history": [{"t":..., "p": "0.34"}, ...]}; extract floats of 'p'
    (be defensive about dict/list shapes and string/float). Return [] on error.
    """
    try:
        from py_clob_client_v2.clob_types import PricesHistoryParams
        params = PricesHistoryParams(market=token_id, interval=interval, fidelity=fidelity)
        resp = client.get_prices_history(params)

        # Defensive extraction: resp may be dict with 'history' key, or a list directly
        if isinstance(resp, dict):
            history = resp.get("history", [])
        elif isinstance(resp, list):
            history = resp
        else:
            log.warning("Unexpected prices_history shape: %s", type(resp))
            return []

        prices: list[float] = []
        for item in history:
            if isinstance(item, dict):
                raw = item.get("p", item.get("price", None))
            else:
                raw = item
            if raw is None:
                continue
            try:
                prices.append(float(raw))
            except (ValueError, TypeError):
                continue

        return prices
    except Exception as exc:
        log.warning("fetch_price_series(%s): %s", token_id, exc)
        return []


def run_edge_probe(
    client: Any,
    http: Any,
    *,
    n_markets: int = 40,
    interval: str = "1m",
    fidelity: int = 60,
    fee_bps: float = 0.0,
    hold_horizon: int = 30,
    fill_window: int = 10,
) -> dict:
    """Scan gamma for liquid binary markets, fetch price history, run replay_passive_bid,
    and AGGREGATE across markets into one ProbeResult-like dict plus per-market rows.

    Returns {'aggregate': {...}, 'markets_probed': int, 'rows': [...]}.
    Prints a readable summary with a clear EDGE / NO EDGE verdict.
    """
    # Fetch liquid binary markets from Gamma
    rows: list[dict] = []
    try:
        resp = http.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "limit": n_markets,
                "active": "true",
                "closed": "false",
                "order": "liquidityClob",
                "ascending": "false",
            },
        )
        resp.raise_for_status()
        markets_raw = resp.json()
    except Exception as exc:
        log.error("Gamma fetch failed: %s", exc)
        markets_raw = []

    markets_probed = 0
    agg_signals = 0
    agg_fills = 0
    agg_pnl = 0.0
    agg_bps_weighted = 0.0
    agg_wins = 0

    for m in markets_raw:
        # Extract the YES token ID (tokens[0] / clobTokenIds[0])
        raw_tokens = m.get("clobTokenIds", m.get("clob_token_ids", ""))
        if isinstance(raw_tokens, str):
            try:
                token_ids = json.loads(raw_tokens) if raw_tokens.strip().startswith("[") else [raw_tokens]
            except (json.JSONDecodeError, ValueError):
                continue
        elif isinstance(raw_tokens, list):
            token_ids = raw_tokens
        else:
            continue

        if not token_ids:
            continue

        yes_token = token_ids[0]
        if not yes_token:
            continue

        prices = fetch_price_series(client, yes_token, interval=interval, fidelity=fidelity)
        if len(prices) < fill_window + 2:
            log.debug("Skipping %s: only %d price points", str(yes_token)[:16], len(prices))
            continue

        result = replay_passive_bid(
            prices,
            fill_window=fill_window,
            hold_horizon=hold_horizon,
            fee_bps=fee_bps,
        )
        markets_probed += 1
        agg_signals += result.n_signals
        agg_fills += result.n_fills
        agg_pnl += result.total_pnl_per_share
        agg_bps_weighted += result.mean_pnl_bps * result.n_fills
        agg_wins += int(round(result.win_rate * result.n_fills))

        rows.append({
            "token_id": yes_token,
            "question": m.get("question", "")[:80],
            "n_signals": result.n_signals,
            "n_fills": result.n_fills,
            "fill_rate": round(result.fill_rate, 4),
            "mean_pnl_per_fill": round(result.mean_pnl_per_fill, 6),
            "mean_pnl_bps": round(result.mean_pnl_bps, 2),
            "win_rate": round(result.win_rate, 4),
            "price_points": len(prices),
        })

    # Aggregate
    agg_fill_rate = agg_fills / agg_signals if agg_signals > 0 else 0.0
    agg_mean_pnl = agg_pnl / agg_fills if agg_fills > 0 else 0.0
    agg_mean_bps = agg_bps_weighted / agg_fills if agg_fills > 0 else 0.0
    agg_win_rate = agg_wins / agg_fills if agg_fills > 0 else 0.0

    aggregate = {
        "n_signals": agg_signals,
        "n_fills": agg_fills,
        "fill_rate": round(agg_fill_rate, 4),
        "total_pnl_per_share": round(agg_pnl, 6),
        "mean_pnl_per_fill": round(agg_mean_pnl, 6),
        "mean_pnl_bps": round(agg_mean_bps, 2),
        "win_rate": round(agg_win_rate, 4),
    }

    # Print readable summary
    print(f"\n{'='*60}")
    print(f"Edge Probe Results  —  {markets_probed} markets scanned")
    print(f"{'='*60}")
    print(f"  Total signals:   {agg_signals}")
    print(f"  Total fills:     {agg_fills}")
    print(f"  Fill rate:       {agg_fill_rate:.1%}")
    print(f"  Mean PnL/fill:   {agg_mean_pnl:+.6f} price units")
    print(f"  Mean PnL:        {agg_mean_bps:+.1f} bps/fill")
    print(f"  Win rate:        {agg_win_rate:.1%}")

    if agg_fills > 0:
        verdict = f"EDGE: +{agg_mean_bps:.1f} bps/fill" if agg_mean_bps > 0 else f"NO EDGE: {agg_mean_bps:.1f} bps/fill"
        print(f"\n  VERDICT: {verdict}")
    else:
        print("\n  VERDICT: NO FILLS — insufficient data")
    print(f"{'='*60}\n")

    return {
        "aggregate": aggregate,
        "markets_probed": markets_probed,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# CLI entry point (run on VPS with live keys + proxy)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import pathlib

    # Ensure repo root is on path
    _repo_root = str(pathlib.Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    import httpx
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        from bot.db.bootstrap import init_database
        init_database()

        from bot.db.kv import load_all_kv
        kv = load_all_kv()

        private_key = kv.get("polymarket_private_key", "")
        wallet_address = kv.get("wallet_address", "")
        sig_type = int(kv.get("polymarket_signature_type", "0") or 0)
        proxy_url = kv.get("proxy_url", "") or kv.get("http_proxy", "")

        if not private_key:
            print("ERROR: polymarket_private_key not set in bot settings.")
            sys.exit(1)

        from bot.clob_client import apply_clob_proxy, build_clob_client

        if proxy_url:
            apply_clob_proxy(proxy_url)

        funder = wallet_address if sig_type == 1 else None
        client = build_clob_client(
            private_key=private_key,
            signature_type=sig_type,
            funder=funder,
        )

        proxy_kwargs: dict = {"proxy": proxy_url} if proxy_url else {}
        with httpx.Client(timeout=30, **proxy_kwargs) as http:
            run_edge_probe(client, http)

    except Exception as _exc:
        print(f"ERROR: {_exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
