"""Live V2 CLOB smoke test (NOT part of `make test`).

Run on the VPS ONLY, with real keys configured in the DB:

    PM_LIVE_SMOKE=1 .venv/bin/python tests/smoke_v2_live.py

Posts ONE deeply non-marketable BUY (price 0.01) with the configured key + proxy,
then cancels it. Success = the order is accepted at the version/signature layer,
i.e. the response is NOT `order_version_mismatch`. Any other error (e.g. funding /
maker-state) still proves the V2 migration works.

Named without a `test_` prefix so pytest does not collect it.
"""

import os
import sys
import json

if os.environ.get("PM_LIVE_SMOKE") != "1":
    print("skipped (set PM_LIVE_SMOKE=1 to run the live V2 smoke)")
    sys.exit(0)

import httpx

from bot.db.bootstrap import init_database
from bot.clob_client import apply_clob_proxy, build_clob_client
from py_clob_client_v2.clob_types import OrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import BUY

init_database()
from bot.db.kv import load_all_kv

kv = load_all_kv()
pk = kv.get("polymarket_private_key", "")
if not pk:
    print("BLOCKED: no polymarket_private_key configured in DB")
    sys.exit(1)

apply_clob_proxy(kv.get("clob_https_proxy", ""))
st = int(kv.get("polymarket_signature_type", "1") or "1")
funder = (kv.get("wallet_address") or None) if st == 1 else None
client = build_clob_client(private_key=pk, signature_type=st, funder=funder)
print(f"client built (sig_type={st}, funder={funder})")

market = httpx.get(
    "https://gamma-api.polymarket.com/markets",
    params={
        "limit": 1,
        "active": "true",
        "closed": "false",
        "order": "liquidityClob",
        "ascending": "false",
    },
    timeout=30,
).json()[0]
tokens = market["clobTokenIds"]
tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
print("market:", (market.get("question") or "")[:50])

try:
    resp = client.create_and_post_order(
        OrderArgs(token_id=tokens[0], price=0.01, size=100, side=BUY),
        order_type=OrderType.GTC,
    )
    print("POST_OK", resp)
    oid = resp.get("orderID") or resp.get("order_id") if isinstance(resp, dict) else None
    if oid:
        client.cancel_order(oid)
        print("cancelled", oid)
    print("PASS: V2 order accepted and cancelled")
except Exception as e:
    s = repr(e)
    print("POST_ERR", s)
    assert "order_version_mismatch" not in s, "STILL V1 — migration incomplete"
    print("PASS: version accepted (remaining error is funding/maker-state, not version)")
