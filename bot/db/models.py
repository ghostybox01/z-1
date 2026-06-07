"""SQLAlchemy models: users, KV settings, trades, article/social signals."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class TradeLog(Base):
    __tablename__ = "trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(128), index=True)
    market_question: Mapped[str] = mapped_column(Text)
    condition_id: Mapped[str] = mapped_column(String(128))
    token_id: Mapped[str] = mapped_column(String(256))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    cost_usd: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    strategy: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(128))
    reconcile_note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_wallet: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class WalletScoreCache(Base):
    """Phase 2: cached wallet skill scores with timestamp for decay tracking."""
    __tablename__ = "wallet_score_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(128), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    components_json: Mapped[str] = mapped_column(Text, default="{}")
    category_scores_json: Mapped[str] = mapped_column(Text, default="{}")
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    decay_factor: Mapped[float] = mapped_column(Float, default=1.0)
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )


class PaperTradeLog(Base):
    """Phase 2: paper/dry-run trade outcomes for realism tracking."""
    __tablename__ = "paper_trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(256))
    entry_price: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=0.0)
    fill_probability: Mapped[float] = mapped_column(Float, default=0.0)
    filled: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )


class ArticleSignal(Base):
    __tablename__ = "article_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(String(1024), default="")
    image_path: Mapped[str] = mapped_column(String(1024), default="")
    keywords: Mapped[str] = mapped_column(Text, default="[]")
    sentiment: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


_engine = None
SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        raise RuntimeError("Database not initialized")
    return _engine


def configure_engine(database_url: str):
    global _engine, SessionLocal
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    _engine = create_engine(database_url, echo=False, future=True, connect_args=connect_args)
    SessionLocal = sessionmaker(_engine, expire_on_commit=False, autoflush=False, future=True)
    return _engine


@contextmanager
def session_scope():
    """Yield a Session; caller must ``commit()`` for writes. Always closes."""
    if SessionLocal is None:
        raise RuntimeError("Database not initialized")
    s = SessionLocal()
    try:
        yield s
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
