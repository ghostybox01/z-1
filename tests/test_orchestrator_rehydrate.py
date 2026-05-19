"""E3 regression: rehydrate trade_history from DB on boot.

A crash mid-cycle that placed CLOB orders before persistence/dedupe state
flushed would leave state.trade_history empty on the next boot, causing
duplicate orders because dedupe sees no history. We now reload recent
trades from the trade_logs table before the first run_cycle.

These tests configure an in-memory SQLite DB, seed TradeLog rows, and
assert TradingBot.load_recent_trades populates state.trade_history with
the right ordering and the bounded window.
"""

from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace

from bot.db.models import Base, TradeLog, configure_engine, session_scope
from bot.models import BotState
from bot.orchestrator import TradingBot


def _seed_trade(session, *, idx: int, age_hours: float = 0.0, status: str = "filled") -> None:
    """Insert a TradeLog with a backdated created_at for window tests."""
    when = dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours)
    row = TradeLog(
        order_id=f"oid-{idx}",
        market_question=f"market {idx}?",
        condition_id=f"cond-{idx}",
        token_id=f"tok-{idx}",
        side="BUY",
        price=0.5 + (idx * 0.01),
        size=10.0 + idx,
        cost_usd=(0.5 + (idx * 0.01)) * (10.0 + idx),
        status=status,
        strategy=f"strat-{idx}",
        outcome="Yes",
        reconcile_note=None,
        created_at=when,
    )
    session.add(row)


def _make_bot_skeleton() -> TradingBot:
    """Build a TradingBot without running __init__ (which loads Settings
    from the same DB and would interfere with our fixture)."""
    bot = TradingBot.__new__(TradingBot)
    bot.state = BotState(mode="dry_run")
    bot.settings = SimpleNamespace(structured_log=False)
    return bot


class TestLoadRecentTrades(unittest.TestCase):
    """In-memory SQLite fixture per test — no shared state."""

    def setUp(self) -> None:
        # In-memory SQLite, fresh schema for each test.
        configure_engine("sqlite:///:memory:")
        from bot.db.models import get_engine
        Base.metadata.create_all(get_engine())

    def test_rehydrates_three_rows_oldest_first(self) -> None:
        with session_scope() as s:
            _seed_trade(s, idx=1, age_hours=3.0)
            _seed_trade(s, idx=2, age_hours=2.0)
            _seed_trade(s, idx=3, age_hours=1.0)
            s.commit()

        bot = _make_bot_skeleton()
        n = bot.load_recent_trades()

        self.assertEqual(n, 3)
        self.assertEqual(len(bot.state.trade_history), 3)
        # Ordered oldest-first to match the live append invariant.
        order_ids = [t.order_id for t in bot.state.trade_history]
        self.assertEqual(order_ids, ["oid-1", "oid-2", "oid-3"])
        # Round-trip of fields we depend on for dedupe / rolling notional.
        first = bot.state.trade_history[0]
        self.assertEqual(first.token_id, "tok-1")
        self.assertEqual(first.condition_id, "cond-1")
        self.assertAlmostEqual(first.price, 0.51, places=4)
        self.assertAlmostEqual(first.size, 11.0, places=4)
        self.assertEqual(first.status, "filled")
        self.assertEqual(first.outcome, "Yes")

    def test_window_filters_stale_rows(self) -> None:
        """Rows older than the window must NOT come back."""
        with session_scope() as s:
            _seed_trade(s, idx=1, age_hours=0.5)        # in
            _seed_trade(s, idx=2, age_hours=12.0)       # in
            _seed_trade(s, idx=3, age_hours=48.0)       # out (default 24h)
            s.commit()

        bot = _make_bot_skeleton()
        n = bot.load_recent_trades(hours=24.0)
        self.assertEqual(n, 2)
        ids = {t.order_id for t in bot.state.trade_history}
        self.assertEqual(ids, {"oid-1", "oid-2"})

    def test_bounded_max_rows(self) -> None:
        """Seed 300 fresh rows, assert <= 200 loaded (default cap)."""
        with session_scope() as s:
            for i in range(300):
                _seed_trade(s, idx=i, age_hours=0.1)
            s.commit()

        bot = _make_bot_skeleton()
        n = bot.load_recent_trades()
        self.assertLessEqual(n, 200)
        self.assertEqual(n, 200)  # exact cap is the contract
        self.assertEqual(len(bot.state.trade_history), 200)

    def test_empty_db_returns_zero_and_clears_history(self) -> None:
        bot = _make_bot_skeleton()
        # Pre-populate with a junk record to prove rehydrate replaces it
        # rather than appending (we own the boot path).
        from bot.models import TradeRecord
        bot.state.trade_history = [
            TradeRecord(
                order_id="stale", market_question="", condition_id="",
                token_id="", side="BUY", price=0.0, size=0.0, cost_usd=0.0,
                status="", timestamp="", outcome="", strategy="",
            )
        ]
        n = bot.load_recent_trades()
        self.assertEqual(n, 0)
        self.assertEqual(bot.state.trade_history, [])

    def test_query_failure_returns_zero_without_raising(self) -> None:
        """If the DB query blows up, load returns 0 and leaves history alone
        — boot must never hard-fail because we couldn't rehydrate."""
        from bot.models import TradeRecord

        bot = _make_bot_skeleton()
        existing = TradeRecord(
            order_id="keep", market_question="", condition_id="",
            token_id="", side="BUY", price=0.0, size=0.0, cost_usd=0.0,
            status="", timestamp="", outcome="", strategy="",
        )
        bot.state.trade_history = [existing]

        # Break the engine by pointing it at a bogus URL with no schema.
        import bot.db.models as dbm
        dbm._engine = None
        dbm.SessionLocal = None

        n = bot.load_recent_trades()
        self.assertEqual(n, 0)
        # Existing history preserved on error (we only overwrite on success).
        self.assertEqual(bot.state.trade_history, [existing])


if __name__ == "__main__":
    unittest.main()
