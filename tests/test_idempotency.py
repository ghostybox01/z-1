"""Tests for the process-local idempotency key helper."""

import time

import pytest

from bot.categories import MarketCategory
from bot.models import TradeIntent
from bot.orchestrator import intent_idempotency_key


def _make_intent(**overrides) -> TradeIntent:
    defaults = dict(
        agent="test_agent",
        priority=1,
        token_id="abc123token",
        condition_id="cond456",
        question="Will X happen?",
        outcome="Yes",
        side="BUY",
        max_price=0.55,
        size_usd=10.0,
        category=MarketCategory.SPORTS,
        strategy="value_edge",
        reason="test",
    )
    defaults.update(overrides)
    return TradeIntent(**defaults)


def test_same_intent_same_bucket_same_key():
    """Identical intent within the same 60-second bucket produces the same key."""
    now = time.time()
    intent = _make_intent()
    key1 = intent_idempotency_key(intent, now)
    key2 = intent_idempotency_key(intent, now + 0.5)
    assert key1 == key2


def test_same_intent_next_bucket_different_key():
    """Same intent in a different 60-second bucket produces a different key."""
    now = time.time()
    # Force into the next bucket
    next_bucket_time = (int(now // 60) + 1) * 60
    intent = _make_intent()
    key1 = intent_idempotency_key(intent, now)
    key2 = intent_idempotency_key(intent, next_bucket_time)
    assert key1 != key2


def test_different_price_different_key():
    """Changing max_price produces a different key."""
    now = time.time()
    intent_a = _make_intent(max_price=0.55)
    intent_b = _make_intent(max_price=0.60)
    assert intent_idempotency_key(intent_a, now) != intent_idempotency_key(intent_b, now)


def test_different_side_different_key():
    """Changing side (BUY vs SELL) produces a different key."""
    now = time.time()
    intent_buy = _make_intent(side="BUY")
    intent_sell = _make_intent(side="SELL")
    assert intent_idempotency_key(intent_buy, now) != intent_idempotency_key(intent_sell, now)


def test_different_token_different_key():
    """Different token_id produces a different key."""
    now = time.time()
    intent_a = _make_intent(token_id="token_aaa")
    intent_b = _make_intent(token_id="token_bbb")
    assert intent_idempotency_key(intent_a, now) != intent_idempotency_key(intent_b, now)


def test_different_size_different_key():
    """Different size_usd produces a different key."""
    now = time.time()
    intent_a = _make_intent(size_usd=10.0)
    intent_b = _make_intent(size_usd=20.0)
    assert intent_idempotency_key(intent_a, now) != intent_idempotency_key(intent_b, now)


def test_key_length():
    """Key is exactly 16 hex characters."""
    now = time.time()
    key = intent_idempotency_key(_make_intent(), now)
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)
