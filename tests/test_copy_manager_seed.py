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


def test_get_wallet_winrate_case_insensitive():
    w = "0x" + "c" * 40
    cm = CopyManager(Settings(copy_watch_wallets=[w]))
    st = cm.state.wallet_stats[w]
    st.win_rate = 0.82
    st.wins, st.losses = 9, 2
    # Lookup with mixed/upper case still finds it.
    assert cm.get_wallet_winrate(w.upper()) == 0.82
    assert cm.get_wallet_winrate(w) == 0.82


def test_get_wallet_winrate_unknown_returns_none():
    cm = CopyManager(Settings(copy_watch_wallets=[]))
    assert cm.get_wallet_winrate("0x" + "d" * 40) is None
    assert cm.get_wallet_winrate("") is None


def test_get_wallet_winrate_zero_resolved_returns_none():
    w = "0x" + "e" * 40
    cm = CopyManager(Settings(copy_watch_wallets=[w]))
    st = cm.state.wallet_stats[w]
    st.win_rate = 0.0
    st.wins, st.losses = 0, 0  # no resolved outcomes → no usable winrate
    assert cm.get_wallet_winrate(w) is None


def test_max_loss_streak_helper_reads_setting():
    cm = CopyManager(Settings(copy_watch_wallets=[], copy_max_loss_streak=4))
    assert cm._max_loss_streak() == 4
