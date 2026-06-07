"""Create tables, seed default bot_settings, bootstrap admin user."""

from __future__ import annotations

import json
import logging
from typing import Any

import bcrypt

from bot.config_file import load_config
from bot.db.models import Base, BotSetting, User, configure_engine, session_scope
from bot.settings import default_kv_seed

log = logging.getLogger("polymarket.db.bootstrap")


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("ascii"))
    except Exception:
        return False


def init_database(database_url: str | None = None) -> dict[str, Any]:
    """Idempotent: create schema + defaults + admin if missing."""
    cfg = load_config()
    url = database_url or str(cfg.get("database_url") or "sqlite:///./data/app.db")
    eng = configure_engine(url)
    Base.metadata.create_all(eng)
    # Lightweight migration: create_all() does NOT add new columns to existing
    # tables. Add columns the ORM models expect but an older DB may lack.
    try:
        from sqlalchemy import text as _text
        with eng.begin() as _conn:
            _cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(trade_logs)"))]
            if "source_wallet" not in _cols:
                _conn.execute(_text("ALTER TABLE trade_logs ADD COLUMN source_wallet VARCHAR(128) DEFAULT ''"))
                log.info("migration: added trade_logs.source_wallet")
    except Exception as _exc:
        log.warning("source_wallet migration skipped: %s", _exc)

    with session_scope() as s:
        seed = default_kv_seed()
        for k, v in seed.items():
            row = s.get(BotSetting, k)
            if row is None:
                val = v if isinstance(v, str) else json.dumps(v)
                s.add(BotSetting(key=k, value=val))
        s.commit()

        from sqlalchemy import func, select

        n_users = int(s.scalar(select(func.count()).select_from(User)) or 0)
        if n_users == 0:
            un = str(cfg.get("initial_admin_username") or "admin").lower()
            pw = str(cfg.get("initial_admin_password") or "changeme")
            s.add(
                User(
                    username=un,
                    password_hash=hash_password(pw),
                    role="admin",
                    is_active=True,
                )
            )
            s.commit()
            log.warning(
                "Created bootstrap admin %r — set a strong password in Admin immediately",
                un,
            )

    return cfg
