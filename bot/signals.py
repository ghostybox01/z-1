"""Match admin-entered article/social signals (keywords, sentiment, images metadata) to intents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select

from bot.db.models import ArticleSignal, session_scope

# Symmetric sentiment factor: same magnitude for positive and negative sentiment
# to avoid systematic long bias. Midpoint of previous asymmetric values (0.06, 0.08).
SENTIMENT_FACTOR = 0.07


@dataclass
class SignalView:
    title: str
    keywords: str
    sentiment: float
    weight: float


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", text.lower()) if len(t) > 2}


def active_signals() -> list[SignalView]:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        q = (
            select(ArticleSignal)
            .where(ArticleSignal.active.is_(True))
            .where(or_(ArticleSignal.expires_at.is_(None), ArticleSignal.expires_at > now))
        )
        rows = list(s.scalars(q).all())
        return [
            SignalView(
                title=r.title,
                keywords=r.keywords or "[]",
                sentiment=float(r.sentiment or 0.0),
                weight=float(r.weight or 1.0),
            )
            for r in rows
        ]


def intent_signal_boost(question: str) -> tuple[float, str]:
    qset = _tokens(question)
    if not qset:
        return 1.0, ""
    best_m = 1.0
    best_note = ""
    sigs = active_signals()
    for sig in sigs:
        try:
            kws = json.loads(sig.keywords or "[]")
        except json.JSONDecodeError:
            kws = []
        if not isinstance(kws, list):
            continue
        overlap = qset & {str(k).lower() for k in kws if len(str(k)) > 2}
        if not overlap:
            continue
        strength = min(1.0, 0.15 * len(overlap)) * float(sig.weight or 1.0)
        sent = max(-1.0, min(1.0, float(sig.sentiment or 0.0)))
        if sent >= 0.15:
            m = 1.0 + SENTIMENT_FACTOR * strength * sent
            note = f"signal+:{sig.title[:40]}"
        elif sent <= -0.15:
            m = 1.0 - SENTIMENT_FACTOR * strength * abs(sent)
            note = f"signal-:{sig.title[:40]}"
        else:
            m = 1.0
            note = ""
        if abs(m - 1.0) > abs(best_m - 1.0):
            best_m = m
            best_note = note
    best_m = max(0.88, min(1.12, best_m))
    return best_m, best_note
