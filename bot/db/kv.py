"""Read/write bot_settings key-value store and Phase 2 persistence helpers."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select

from bot.db.models import BotSetting, PaperTradeLog, TradeLog, WalletScoreCache, session_scope


def load_all_kv() -> dict[str, str]:
    with session_scope() as s:
        rows = list(s.scalars(select(BotSetting)).all())
        return {r.key: r.value for r in rows}


def upsert_kv(key: str, value: str) -> None:
    with session_scope() as s:
        row = s.get(BotSetting, key)
        if row is None:
            s.add(BotSetting(key=key, value=value))
        else:
            row.value = value
        s.commit()


def upsert_many_kv(data: dict[str, Any]) -> None:
    with session_scope() as s:
        for k, v in data.items():
            val = v if isinstance(v, str) else json.dumps(v)
            row = s.get(BotSetting, str(k))
            if row is None:
                s.add(BotSetting(key=str(k), value=val))
            else:
                row.value = val
        s.commit()


def append_trade_log(
    *,
    order_id: str,
    market_question: str,
    condition_id: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    cost_usd: float,
    status: str,
    strategy: str,
    outcome: str,
    source_wallet: str = "",
    reconcile_note: str | None = None,
) -> None:
    with session_scope() as s:
        s.add(
            TradeLog(
                order_id=order_id,
                market_question=market_question,
                condition_id=condition_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                cost_usd=cost_usd,
                status=status,
                strategy=strategy,
                outcome=outcome,
                source_wallet=source_wallet,
                reconcile_note=reconcile_note,
            )
        )
        s.commit()


def recent_trade_statuses(limit: int = 40) -> list[str]:
    with session_scope() as s:
        q = select(TradeLog.status).order_by(TradeLog.id.desc()).limit(limit)
        return list(s.scalars(q).all())


# --- Phase 2: wallet score cache ---

def upsert_wallet_score(
    wallet: str,
    score: float,
    components: dict,
    category_scores: dict,
    sample_count: int,
    decay_factor: float,
) -> None:
    """Insert or update a cached wallet score."""
    import datetime as dt
    with session_scope() as s:
        existing = s.query(WalletScoreCache).filter(
            WalletScoreCache.wallet == wallet.lower().strip()
        ).first()
        if existing:
            existing.score = score
            existing.components_json = json.dumps(components)
            existing.category_scores_json = json.dumps(category_scores)
            existing.sample_count = sample_count
            existing.decay_factor = decay_factor
            existing.computed_at = dt.datetime.now(dt.UTC)
        else:
            s.add(WalletScoreCache(
                wallet=wallet.lower().strip(),
                score=score,
                components_json=json.dumps(components),
                category_scores_json=json.dumps(category_scores),
                sample_count=sample_count,
                decay_factor=decay_factor,
            ))
        s.commit()


def get_wallet_score_cache(wallet: str) -> Optional[dict]:
    """Retrieve cached wallet score or None."""
    with session_scope() as s:
        row = s.query(WalletScoreCache).filter(
            WalletScoreCache.wallet == wallet.lower().strip()
        ).order_by(WalletScoreCache.computed_at.desc()).first()
        if row is None:
            return None
        try:
            components = json.loads(row.components_json or "{}")
        except (json.JSONDecodeError, TypeError):
            components = {}
        try:
            category_scores = json.loads(row.category_scores_json or "{}")
        except (json.JSONDecodeError, TypeError):
            category_scores = {}
        return {
            "score": row.score,
            "components": components,
            "category_scores": category_scores,
            "sample_count": row.sample_count,
            "decay_factor": row.decay_factor,
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        }


# --- Phase 2: paper trade logging ---

def append_paper_trade_log(
    *,
    order_id: str,
    token_id: str,
    entry_price: float,
    fill_price: float = 0.0,
    slippage_bps: float = 0.0,
    fill_probability: float = 0.0,
    filled: bool = False,
    latency_ms: float = 0.0,
    reason: str = "",
) -> None:
    """Persist a paper trade simulation result."""
    with session_scope() as s:
        s.add(PaperTradeLog(
            order_id=order_id,
            token_id=token_id,
            entry_price=entry_price,
            fill_price=fill_price,
            slippage_bps=slippage_bps,
            fill_probability=fill_probability,
            filled=filled,
            latency_ms=latency_ms,
            reason=reason,
        ))
        s.commit()


def paper_trade_fill_rate(limit: int = 100) -> float:
    """Recent paper trade fill rate for realism tracking."""
    with session_scope() as s:
        rows = s.query(PaperTradeLog).order_by(
            PaperTradeLog.id.desc()
        ).limit(limit).all()
        if not rows:
            return 0.0
        filled = sum(1 for r in rows if r.filled)
        return filled / len(rows)
