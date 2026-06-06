"""Weather forward paper-test — NO capital at risk.

Settles the open question from the forecast-skill backtest (2026-06-05): the
Open-Meteo forecast is skillful (sigma ~1.1C on daily max) and Polymarket lists
*threshold* markets ("high above X"), but does our bias-corrected forecast CDF
actually BEAT the market price, or does the market already price the same
forecast?  Live capital can't answer that yet, so we paper-test:

  * ``snapshot`` — find live "highest temperature ... or higher/below" markets,
    compute our CDF P(threshold) vs the market's YES price, append to a JSONL log.
  * ``evaluate`` — for logged snapshots whose markets have resolved, score:
      - Brier(ours) vs Brier(market): is our probability closer to the {0,1} truth?
      - simulated post-cost P&L of betting only when |edge| clears a margin.

Calibration constants come from the 564-city-day skill backtest (MAX temp):
forecast runs ~0.28C cold (actual ~ forecast + bias), residual sigma ~1.12C.
Only "highest temperature" markets are scored — MIN markets need their own
calibration and are skipped to keep the edge measurement clean.

CLI: ``python -m bot.backtest.weather_forward snapshot|evaluate``
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Optional

log = logging.getLogger("polymarket.weather_forward")

# --- calibration (from the 2026-06-05 forecast-skill backtest, MAX temp) ---
SIGMA_MAX = 1.12          # residual std of (forecast - actual) daily high, °C
BIAS_MAX = 0.28           # actual ~ forecast + BIAS (forecast runs cold)
EDGE_MARGIN = 0.12        # only "bet" (in the sim) when |our_prob - price| >= this
COST = 0.02               # round-trip taker cost assumption for the sim P&L

LOG_PATH = "/opt/polymarket-bot/weather_paper_log.jsonl"
GAMMA = "https://gamma-api.polymarket.com/markets"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

_THRESH_RE = re.compile(
    r"highest temperature in (?P<city>.+?) be (?P<val>\d+(?:\.\d+)?)\s*°?\s*(?P<unit>[CF])"
    r"\s*or\s*(?P<dir>higher|above|below|lower|less)",
    re.IGNORECASE,
)


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _phi(x: float) -> float:
    """Standard-normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def forecast_cdf(
    forecast_c: float,
    threshold_c: float,
    above: bool,
    sigma: float = SIGMA_MAX,
    bias: float = BIAS_MAX,
) -> float:
    """Our modelled P(actual high meets the threshold).

    actual high ~ Normal(forecast_c + bias, sigma).  ``above`` True →
    P(actual >= threshold); False → P(actual <= threshold).
    """
    mu = forecast_c + bias
    p_at_or_below = _phi((threshold_c - mu) / sigma)
    return (1.0 - p_at_or_below) if above else p_at_or_below


def parse_threshold_market(question: str) -> Optional[dict]:
    """Parse a 'highest temperature ... or higher/below' question.

    Returns ``{city, threshold_c, above}`` or None if it is not a high-temp
    threshold market.
    """
    m = _THRESH_RE.search(question or "")
    if not m:
        return None
    val = float(m.group("val"))
    unit = m.group("unit").upper()
    threshold_c = f_to_c(val) if unit == "F" else val
    above = m.group("dir").lower() in ("higher", "above")
    return {"city": m.group("city").strip().lower(), "threshold_c": round(threshold_c, 2), "above": above}


def _match_city(city: str, coords: dict) -> Optional[str]:
    if city in coords:
        return city
    return next((k for k in coords if k in city or city in k), None)


async def snapshot(http: Any, now_ts: int) -> list[dict]:
    """Find live high-temp threshold markets and record CDF-vs-price edge.

    ``now_ts`` is passed in (Date.now is unavailable in some contexts) and only
    stamps the record.
    """
    from bot.agents.weather_arb import CITY_COORDS

    markets: list[dict] = []
    for off in (0, 500, 1000):
        try:
            r = await http.get(GAMMA, params={"closed": "false", "limit": "500", "offset": str(off),
                                              "order": "volume", "ascending": "false"})
            data = r.json() if r.status_code == 200 else []
        except Exception as exc:
            log.debug("gamma fetch failed: %s", exc)
            data = []
        for m in data or []:
            parsed = parse_threshold_market(m.get("question", ""))
            if parsed:
                markets.append((m, parsed))

    fc_cache: dict[str, Optional[float]] = {}
    out: list[dict] = []
    for m, p in markets:
        city = _match_city(p["city"], CITY_COORDS)
        if not city:
            continue
        if city not in fc_cache:
            lat, lon = CITY_COORDS[city]
            try:
                j = (await http.get(OPEN_METEO, params={"latitude": lat, "longitude": lon,
                     "daily": "temperature_2m_max", "timezone": "auto", "forecast_days": "2"})).json()
                arr = j.get("daily", {}).get("temperature_2m_max") or []
                fc_cache[city] = float(arr[0]) if arr else None
            except Exception as exc:
                log.debug("forecast fetch failed %s: %s", city, exc)
                fc_cache[city] = None
        fc = fc_cache[city]
        if fc is None:
            continue
        our_prob = round(forecast_cdf(fc, p["threshold_c"], p["above"]), 4)
        try:
            px = json.loads(m.get("outcomePrices") or "[]")
            price = float(px[0]) if px else None
        except Exception:
            price = None
        out.append({
            "ts": now_ts,
            "condition_id": str(m.get("conditionId") or ""),
            "token_id_yes": (lambda t: t[0] if t else "")(json.loads(m.get("clobTokenIds") or "[]") if m.get("clobTokenIds") else []),
            "question": m.get("question", "")[:120],
            "city": city,
            "forecast_c": round(fc, 2),
            "threshold_c": p["threshold_c"],
            "above": p["above"],
            "our_prob": our_prob,
            "market_price": price,
            "edge": round(our_prob - price, 4) if price is not None else None,
            "end_date": m.get("endDate") or m.get("end_date_iso") or "",
            "resolved": None,
        })
    return out


def run_snapshot() -> int:
    """CLI entry: take one snapshot and append to the JSONL log. Returns count."""
    import asyncio
    import httpx

    async def _go() -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as h:
            return await snapshot(h, int(time.time()))

    recs = asyncio.run(_go())
    if recs:
        with open(LOG_PATH, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
    edged = [r for r in recs if r.get("edge") is not None and abs(r["edge"]) >= EDGE_MARGIN]
    log.info("weather paper snapshot: %d markets, %d with |edge|>=%.2f", len(recs), len(edged), EDGE_MARGIN)
    print(f"snapshot: {len(recs)} threshold markets logged, {len(edged)} with |edge|>={EDGE_MARGIN}")
    for r in edged:
        print(f"  edge={r['edge']:+.2f} our={r['our_prob']:.2f} mkt={r['market_price']:.2f} | {r['question'][:60]}")
    return len(recs)


async def _resolve_yes(http: Any, condition_id: str, token_id_yes: str) -> Optional[int]:
    """Return 1 if the YES outcome won, 0 if it lost, None if not resolved/error."""
    try:
        r = await http.get(f"https://clob.polymarket.com/markets/{condition_id}")
        if r.status_code != 200:
            return None
        m = r.json()
        if not m.get("closed"):
            return None
        for tok in m.get("tokens", []):
            if str(tok.get("token_id")) == str(token_id_yes):
                p = tok.get("price")
                if p is not None:
                    return 1 if float(p) >= 0.5 else 0
                w = tok.get("winner")
                if w is not None:
                    return 1 if w else 0
        return None
    except Exception:
        return None


async def evaluate(http: Any) -> dict:
    """Score resolved snapshots: is our CDF closer to truth than the market price?

    Dedupes to the LATEST snapshot per market (closest to resolution), resolves
    the YES outcome, and compares Brier scores + a simulated post-cost P&L of
    betting only when |edge| >= EDGE_MARGIN.
    """
    try:
        with open(LOG_PATH) as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return {"scored": 0, "note": "no log yet"}

    latest: dict[str, dict] = {}
    for r in rows:
        cid = r.get("condition_id") or ""
        if r.get("market_price") is None or not cid:
            continue
        if cid not in latest or r.get("ts", 0) > latest[cid].get("ts", 0):
            latest[cid] = r

    brier_ours = brier_mkt = 0.0
    sim_pnl = 0.0
    n = bets = bet_wins = ours_better = 0
    details: list[dict] = []
    for cid, r in latest.items():
        outcome = await _resolve_yes(http, cid, r.get("token_id_yes", ""))
        if outcome is None:
            continue
        n += 1
        our_p = float(r["our_prob"]); price = float(r["market_price"])
        bo = (our_p - outcome) ** 2
        bm = (price - outcome) ** 2
        brier_ours += bo
        brier_mkt += bm
        if bo < bm:
            ours_better += 1
        edge = our_p - price
        pnl = None
        if edge >= EDGE_MARGIN:          # we think YES underpriced -> buy YES @ price
            pnl = ((1.0 - price) if outcome == 1 else -price) - COST
        elif edge <= -EDGE_MARGIN:       # we think YES overpriced -> buy NO @ (1-price)
            no_px = 1.0 - price
            pnl = ((1.0 - no_px) if outcome == 0 else -no_px) - COST
        if pnl is not None:
            sim_pnl += pnl
            bets += 1
            if pnl > 0:
                bet_wins += 1
        details.append({"q": r["question"][:54], "our": round(our_p, 2), "mkt": round(price, 2),
                        "outcome": outcome, "pnl": (round(pnl, 3) if pnl is not None else None)})

    res = {
        "scored": n,
        "brier_ours": round(brier_ours / n, 4) if n else None,
        "brier_market": round(brier_mkt / n, 4) if n else None,
        "ours_better_pct": round(ours_better / n, 3) if n else None,
        "sim_bets": bets,
        "sim_bet_winrate": round(bet_wins / bets, 3) if bets else None,
        "sim_pnl_per_bet": round(sim_pnl / bets, 4) if bets else None,
        "sim_pnl_total": round(sim_pnl, 4),
        "details": details,
    }
    return res


def run_evaluate() -> dict:
    """CLI entry: score all resolved snapshots and print the verdict."""
    import asyncio
    import httpx

    async def _go() -> dict:
        async with httpx.AsyncClient(timeout=30) as h:
            return await evaluate(h)

    res = asyncio.run(_go())
    print(json.dumps({k: v for k, v in res.items() if k != "details"}, indent=2))
    if res.get("scored"):
        bo, bm = res.get("brier_ours"), res.get("brier_market")
        verdict = "OUR CDF BEATS MARKET" if (bo is not None and bm is not None and bo < bm) else "no forecast edge"
        print(f"VERDICT: {verdict} (lower Brier = better). sim P&L/bet={res.get('sim_pnl_per_bet')}")
        for d in res["details"]:
            print(f"  out={d['outcome']} our={d['our']:.2f} mkt={d['mkt']:.2f} pnl={d['pnl']} | {d['q']}")
    return res


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "snapshot":
        run_snapshot()
    elif cmd == "evaluate":
        run_evaluate()
    else:
        print(f"unknown command: {cmd} (use 'snapshot' or 'evaluate')")
