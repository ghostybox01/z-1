"""E13: max_streak must be promoted before cur_streak is zeroed on a loss."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from bot import leaderboard


def _positions(seq: str) -> list[dict]:
    """Map a 'W'/'L' string into closed-position dicts with realizedPnl."""
    out = []
    for ch in seq:
        out.append({"realizedPnl": 1.0 if ch == "W" else -1.0})
    return out


async def _analyze(seq: str) -> dict:
    async def fake_get_json(_http, _url, params=None):
        return _positions(seq)

    with patch.object(leaderboard, "get_json_retry", side_effect=fake_get_json):
        return await leaderboard.analyze_wallet_quality(None, "0x" + "a" * 40)


def _run(seq: str) -> dict:
    return asyncio.run(_analyze(seq))


class TestLeaderboardMaxStreak(unittest.TestCase):
    def test_wwwl(self):
        # W-W-W-L: the L must promote the 3-win streak before zeroing.
        self.assertEqual(_run("WWWL")["max_streak"], 3)

    def test_wwwlw(self):
        # W-W-W-L-W: longest is still 3.
        self.assertEqual(_run("WWWLW")["max_streak"], 3)

    def test_lllww(self):
        # L-L-L-W-W: best run is the trailing pair.
        self.assertEqual(_run("LLLWW")["max_streak"], 2)

    def test_all_wins(self):
        # W-W-W-W-W: end-of-loop case (no losing trade), max == 5.
        self.assertEqual(_run("WWWWW")["max_streak"], 5)


if __name__ == "__main__":
    unittest.main()
