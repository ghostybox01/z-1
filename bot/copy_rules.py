"""Reusable copy-trading candidate extraction and filter rules."""

from __future__ import annotations

import logging
import math
from statistics import median
from dataclasses import dataclass
from typing import Any, Optional

from bot.categories import classify_market

log = logging.getLogger("polymarket.copy_rules")


@dataclass
class CopyCandidate:
    wallet: str
    token_id: str
    tx_key: str
    title: str
    slug: str
    tags_text: str
    category: str
    outcome: str
    price: float
    usdc: float


def extract_token_id(entry: dict[str, Any]) -> str | None:
    asset = entry.get("asset") or entry.get("asset_id")
    if isinstance(asset, str) and len(asset) > 30:
        return asset
    if isinstance(asset, dict):
        for k in ("token_id", "tokenId", "id"):
            v = asset.get(k)
            if isinstance(v, str) and len(v) > 20:
                return v
    for k in ("clobTokenId", "tokenId", "token_id", "asset_id"):
        v = entry.get(k)
        if isinstance(v, str) and len(v) > 20:
            return v
    return None


def extract_price(entry: dict[str, Any]) -> float | None:
    for k in ("price", "avgPrice", "avg_price"):
        v = entry.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def build_candidate(entry: dict[str, Any], wallet: str, default_bet_usd: float) -> Optional[CopyCandidate]:
    if entry.get("type") and str(entry["type"]).upper() != "TRADE":
        return None
    side = str(entry.get("side", "")).upper()
    if side and side != "BUY":
        return None

    tid = extract_token_id(entry)
    if not tid:
        return None

    title = str(entry.get("title") or entry.get("question") or "unknown")
    slug = str(entry.get("slug") or "")
    tags = entry.get("tags")
    if isinstance(tags, list):
        tags_text = " ".join(str(x) for x in tags if x is not None)
    else:
        tags_text = str(tags or "")
    pseudo = {"question": title, "slug": slug, "tags": tags}
    cat = classify_market(pseudo).value

    px = extract_price(entry) or 0.5
    px = max(0.02, min(0.98, px))
    usdc = float(entry.get("usdcSize") or entry.get("amount") or default_bet_usd)
    outc = str(entry.get("outcome") or "unknown").strip().lower()
    txh = str(entry.get("transactionHash") or entry.get("id") or "")
    tx_key = f"{wallet}:{txh}:{tid}"
    return CopyCandidate(
        wallet=str(wallet).lower().strip(),
        token_id=tid,
        tx_key=tx_key,
        title=title,
        slug=slug,
        tags_text=tags_text,
        category=cat,
        outcome=outc,
        price=px,
        usdc=usdc,
    )


def _contains_any(text: str, terms: list[str]) -> bool:
    if not terms:
        return False
    t = text.lower()
    for k in terms:
        s = str(k).strip().lower()
        if s and s in t:
            return True
    return False


def passes_filters(settings: Any, c: CopyCandidate) -> tuple[bool, str]:
    allowed_cats = [str(x).lower() for x in (getattr(settings, "copy_allowed_categories", []) or [])]
    if allowed_cats and c.category.lower() not in set(allowed_cats):
        return False, "category_not_allowed"

    allowed_outcomes = [str(x).lower() for x in (getattr(settings, "copy_allowed_outcomes", []) or [])]
    if allowed_outcomes and c.outcome not in set(allowed_outcomes):
        return False, "outcome_not_allowed"

    text_ctx = f"{c.title} {c.slug} {c.tags_text}".lower()
    req_kw = [str(x).lower() for x in (getattr(settings, "copy_required_keywords", []) or [])]
    blk_kw = [str(x).lower() for x in (getattr(settings, "copy_blocked_keywords", []) or [])]
    if req_kw and not _contains_any(text_ctx, req_kw):
        return False, "required_keywords_miss"
    if blk_kw and _contains_any(text_ctx, blk_kw):
        return False, "blocked_keyword_hit"

    min_usd = float(getattr(settings, "copy_min_usd", 0.0) or 0.0)
    max_usd_filter = float(getattr(settings, "copy_max_usd", 0.0) or 0.0)
    if c.usdc < min_usd:
        return False, "below_copy_min_usd"
    if max_usd_filter > 0 and c.usdc > max_usd_filter:
        return False, "above_copy_max_usd"

    pmin = float(getattr(settings, "copy_min_price", 0.0) or 0.0)
    pmax = float(getattr(settings, "copy_max_price", 1.0) or 1.0)
    if c.price < pmin or c.price > pmax:
        return False, "copy_price_out_of_range"

    if not bool(getattr(settings, "copy_allow_unknown_outcome", True)):
        if c.outcome not in ("yes", "no"):
            return False, "unknown_outcome_filtered"

    return True, "ok"


def limit_price_with_buffer(settings: Any, px: float) -> float:
    pad_bps = float(getattr(settings, "copy_price_buffer_bps", 300.0) or 300.0)
    return round(min(px * (1.0 + pad_bps / 10000.0), 0.99), 4)


def wallet_score(
    rows: list[dict[str, Any]],
    *,
    wallet: str,
    default_bet_usd: float,
    settings: Any,
    state: Any = None,
) -> tuple[float, dict[str, float]]:
    """
    Heuristic quality score in [0,1] for source-wallet ranking.

    Phase 2: delegates to wallet_score_v2 when available, providing category-aware
    skill, timing quality, consistency, sample-size Bayesian shrinkage, and decay.
    Falls back to v1 heuristic if v2 import/runtime fails. Fallbacks emit a
    WARNING log line and, when ``state`` is provided, increment
    ``state.scoring_fallback_v1`` so operators can see silent v1 usage.

    TODO(Task 12): wire ``state`` through from CopySignalAgent and admin_api so
    the counter is always populated. Currently most callers do not pass state
    and only the warning log fires. See bot/agents/copy_signal.py:70 and
    bot/web/admin_api.py:631.
    """
    try:
        from bot.wallet_scoring import wallet_score_v2
        score, result = wallet_score_v2(
            rows, wallet=wallet, default_bet_usd=default_bet_usd, settings=settings,
        )
        return score, result.components
    except Exception as e:
        log.warning("scoring fallback: %s", e, exc_info=True)
        if state is not None:
            # getattr/setattr in case Task 1 hasn't landed the field yet.
            setattr(state, "scoring_fallback_v1", getattr(state, "scoring_fallback_v1", 0) + 1)

    # V1 fallback
    cands: list[CopyCandidate] = []
    for e in rows:
        c = build_candidate(e, wallet, default_bet_usd)
        if c is not None:
            cands.append(c)
    n = len(cands)
    if n == 0:
        return 0.0, {"n": 0.0, "outcome": 0.0, "price": 0.0, "size": 0.0}

    known_outcome = sum(1 for c in cands if c.outcome in ("yes", "no")) / n
    sane_price = sum(1 for c in cands if 0.05 <= c.price <= 0.95) / n
    med_usd = median([float(c.usdc) for c in cands])
    size_factor = min(max(med_usd / 50.0, 0.0), 1.0)
    activity_factor = min(max(math.log1p(n) / math.log1p(80), 0.0), 1.0)

    score = 0.40 * activity_factor + 0.25 * known_outcome + 0.20 * sane_price + 0.15 * size_factor
    ovs = getattr(settings, "copy_wallet_score_overrides", {}) or {}
    try:
        score += float(ovs.get(str(wallet).lower().strip(), 0.0) or 0.0)
    except Exception:
        pass
    score = min(max(score, 0.0), 1.0)
    return score, {
        "n": float(n),
        "outcome": known_outcome,
        "price": sane_price,
        "size": size_factor,
    }
