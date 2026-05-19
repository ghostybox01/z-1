"""
Phase 2 wallet skill scoring: category-aware, timing-quality, consistency,
sample-size Bayesian shrinkage, and exponential time-decay.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median, stdev
from typing import Any, Optional

from bot.copy_rules import CopyCandidate, build_candidate


@dataclass
class CategorySkill:
    """Per-category performance summary for a wallet."""
    category: str
    n_trades: int = 0
    win_rate: float = 0.0
    avg_edge: float = 0.0
    consistency: float = 0.0


@dataclass
class WalletScoreV2Result:
    """Rich output from the v2 wallet scorer."""
    total_score: float = 0.0
    category_scores: dict[str, float] = field(default_factory=dict)
    timing_quality: float = 0.0
    consistency: float = 0.0
    sample_penalty: float = 1.0
    decay_factor: float = 1.0
    components: dict[str, float] = field(default_factory=dict)


# Minimum trades before we trust a wallet at all
_ABSOLUTE_MIN_TRADES = 3
# Bayesian prior: assume mediocre baseline
_PRIOR_SCORE = 0.35
_PRIOR_WEIGHT = 8


def _exponential_decay_weight(age_hours: float, half_life_hours: float = 168.0) -> float:
    """Exponential decay: half-life defaults to 7 days (168h)."""
    if half_life_hours <= 0:
        return 1.0
    return math.exp(-0.693 * age_hours / half_life_hours)


def _timing_quality_score(candidates: list[CopyCandidate]) -> float:
    """
    Measures how good the entry prices are: reward buying at moderate prices
    (0.15-0.65 range is sweet spot for BUY-side conviction), penalize buying
    near extremes. Also rewards consistency in price selection.
    """
    if not candidates:
        return 0.0
    sweet_spot_count = 0
    for c in candidates:
        if 0.10 <= c.price <= 0.70:
            sweet_spot_count += 1
    sweet_ratio = sweet_spot_count / len(candidates)

    prices = [c.price for c in candidates]
    # stdev requires >= 2 samples; sentinel 0.0 means "no spread observed"
    if len(prices) >= 2:
        price_std = stdev(prices)
    else:
        price_std = 0.0
    if len(prices) >= 3:
        dispersion_penalty = min(price_std / 0.3, 1.0)
        return sweet_ratio * 0.7 + (1.0 - dispersion_penalty) * 0.3
    return sweet_ratio


def _consistency_score(candidates: list[CopyCandidate]) -> float:
    """
    Reward wallets that trade consistently across multiple markets/categories
    rather than one-offs. High consistency = diversified, repeatable behavior.
    """
    if len(candidates) < 2:
        return 0.0
    cats = defaultdict(int)
    markets = set()
    for c in candidates:
        cats[c.category] += 1
        markets.add(c.title[:60])

    n_categories = len(cats)
    n_markets = len(markets)

    cat_diversity = min(n_categories / 3.0, 1.0)
    market_diversity = min(n_markets / max(len(candidates) * 0.5, 1.0), 1.0)

    cat_counts = list(cats.values())
    # stdev requires >= 2 samples; sentinel 0.0 means "no spread observed"
    if len(cat_counts) >= 2:
        mean_c = sum(cat_counts) / len(cat_counts)
        cat_std = stdev(cat_counts)
        cat_balance = 1.0 - min(cat_std / max(mean_c, 1.0), 1.0)
    else:
        cat_balance = 0.3

    return 0.35 * cat_diversity + 0.35 * market_diversity + 0.30 * cat_balance


def _sample_size_penalty(n: int, min_trusted: int = 10) -> float:
    """
    Bayesian-style penalty for small samples. Returns multiplier in (0, 1].
    With n < min_trusted, shrinks score toward prior.
    """
    if n <= 0:
        return 0.0
    if n >= min_trusted * 2:
        return 1.0
    return n / (n + _PRIOR_WEIGHT)


def _category_skill_scores(candidates: list[CopyCandidate]) -> dict[str, float]:
    """
    Per-category skill: fraction of trades with sane entry prices and known outcomes.
    Returns {category: score_0_to_1}.
    """
    by_cat: dict[str, list[CopyCandidate]] = defaultdict(list)
    for c in candidates:
        by_cat[c.category].append(c)

    out: dict[str, float] = {}
    for cat, cands in by_cat.items():
        n = len(cands)
        if n == 0:
            out[cat] = 0.0
            continue
        known_outcome = sum(1 for c in cands if c.outcome in ("yes", "no")) / n
        sane_price = sum(1 for c in cands if 0.05 <= c.price <= 0.95) / n
        med_usd = median([c.usdc for c in cands])
        size_factor = min(max(med_usd / 50.0, 0.0), 1.0)
        cat_sample_penalty = _sample_size_penalty(n, min_trusted=5)
        raw = 0.40 * known_outcome + 0.30 * sane_price + 0.30 * size_factor
        out[cat] = raw * cat_sample_penalty + _PRIOR_SCORE * (1.0 - cat_sample_penalty)
    return out


def wallet_score_v2(
    rows: list[dict[str, Any]],
    *,
    wallet: str,
    default_bet_usd: float,
    settings: Any,
    now_epoch: Optional[float] = None,
) -> tuple[float, WalletScoreV2Result]:
    """
    Phase 2 wallet score: richer, more realistic than v1.

    Returns (score_0_to_1, WalletScoreV2Result).
    """
    if now_epoch is None:
        now_epoch = time.time()

    hl_raw = getattr(settings, "wallet_score_decay_half_life_hours", None)
    half_life_hours = float(hl_raw) if hl_raw is not None else 168.0

    cands: list[CopyCandidate] = []
    ages_hours: list[float] = []
    for e in rows:
        c = build_candidate(e, wallet, default_bet_usd)
        if c is not None:
            cands.append(c)
            ts = e.get("timestamp") or e.get("createdAt") or e.get("created_at")
            if ts is not None:
                try:
                    if isinstance(ts, (int, float)):
                        ts_epoch = float(ts)
                    else:
                        s = str(ts).strip()
                        try:
                            ts_epoch = float(s)
                        except ValueError:
                            ts_epoch = _parse_iso_epoch(s)
                    age_h = max(0.0, (now_epoch - ts_epoch) / 3600.0)
                except (ValueError, TypeError, OSError):
                    age_h = 0.0
            else:
                age_h = 0.0
            ages_hours.append(age_h)

    n = len(cands)
    result = WalletScoreV2Result()

    if n < _ABSOLUTE_MIN_TRADES:
        result.components = {"n": float(n), "reason": "too_few_trades"}
        return 0.0, result

    # --- Component scores ---

    # 1) Category-aware skill
    cat_scores = _category_skill_scores(cands)
    result.category_scores = cat_scores
    if cat_scores:
        cat_weights = {cat: len([c for c in cands if c.category == cat]) for cat in cat_scores}
        total_w = sum(cat_weights.values()) or 1
        weighted_cat_score = sum(cat_scores[cat] * cat_weights[cat] for cat in cat_scores) / total_w
    else:
        weighted_cat_score = 0.0

    # 2) Timing quality
    timing = _timing_quality_score(cands)
    result.timing_quality = timing

    # 3) Consistency
    consistency = _consistency_score(cands)
    result.consistency = consistency

    # 4) Activity factor (log-scaled)
    activity_factor = min(max(math.log1p(n) / math.log1p(80), 0.0), 1.0)

    # 5) Known outcome ratio
    known_outcome = sum(1 for c in cands if c.outcome in ("yes", "no")) / n

    # 6) Sane price ratio
    sane_price = sum(1 for c in cands if 0.05 <= c.price <= 0.95) / n

    # 7) Size factor
    med_usd = median([c.usdc for c in cands])
    size_factor = min(max(med_usd / 50.0, 0.0), 1.0)

    # Raw composite
    raw_score = (
        0.25 * weighted_cat_score
        + 0.15 * timing
        + 0.10 * consistency
        + 0.20 * activity_factor
        + 0.10 * known_outcome
        + 0.10 * sane_price
        + 0.10 * size_factor
    )

    # 8) Sample-size Bayesian shrinkage
    sample_pen = _sample_size_penalty(n)
    result.sample_penalty = sample_pen
    shrunk_score = raw_score * sample_pen + _PRIOR_SCORE * (1.0 - sample_pen)

    # 9) Time decay: weight recent trades more
    if ages_hours and half_life_hours > 0:
        weights = [_exponential_decay_weight(a, half_life_hours) for a in ages_hours]
        avg_decay = sum(weights) / len(weights) if weights else 1.0
        decay_mult = 0.5 + 0.5 * avg_decay
    else:
        decay_mult = 1.0
    result.decay_factor = decay_mult

    final = shrunk_score * decay_mult

    # Manual overrides
    ovs = getattr(settings, "copy_wallet_score_overrides", {}) or {}
    try:
        final += float(ovs.get(str(wallet).lower().strip(), 0.0) or 0.0)
    except Exception:
        pass

    final = min(max(final, 0.0), 1.0)
    result.total_score = final
    result.components = {
        "n": float(n),
        "cat_skill": round(weighted_cat_score, 4),
        "timing": round(timing, 4),
        "consistency": round(consistency, 4),
        "activity": round(activity_factor, 4),
        "known_outcome": round(known_outcome, 4),
        "sane_price": round(sane_price, 4),
        "size": round(size_factor, 4),
        "sample_penalty": round(sample_pen, 4),
        "decay": round(decay_mult, 4),
        "raw": round(raw_score, 4),
    }
    return final, result


def _parse_iso_epoch(s: str) -> float:
    """Best-effort ISO 8601 -> epoch seconds."""
    import datetime as dt
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(s.replace(" ", "T"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()
