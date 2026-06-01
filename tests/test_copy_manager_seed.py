"""Auto-managed copy wallets must be re-vettable (active), not frozen (manual)."""

from bot.settings import Settings
from bot.copy_manager import CopyManager


def test_auto_manage_on_seeds_active():
    w = "0x" + "a" * 40
    cm = CopyManager(Settings(copy_watch_wallets=[w], copy_auto_manage=True))
    assert cm.state.wallet_stats[w].status == "active"


def test_auto_manage_off_seeds_manual():
    w = "0x" + "b" * 40
    cm = CopyManager(Settings(copy_watch_wallets=[w], copy_auto_manage=False))
    assert cm.state.wallet_stats[w].status == "manual"
