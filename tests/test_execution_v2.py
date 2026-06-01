import asyncio
from bot import execution

class FakeClient:
    def __init__(self, post_resp, order_states):
        self._post_resp = post_resp
        self._states = list(order_states)
        self.cancelled = []
    def get_tick_size(self, token_id): return "0.01"
    def get_neg_risk(self, token_id): return False
    def create_order(self, args, options=None): return {"args": args, "options": options}
    def post_order(self, signed, order_type): return self._post_resp
    def get_order(self, oid): return self._states.pop(0) if self._states else {"status": "LIVE"}
    def cancel_orders(self, hashes): self.cancelled.extend(hashes); return {"canceled": list(hashes)}

def test_limit_fills_immediately():
    c = FakeClient(post_resp={"orderID": "abc"},
                   order_states=[{"status": "FILLED", "size_matched": "100", "original_size": "100"}])
    oid, note = asyncio.run(execution.place_limit_gtd_then_wait(
        c, token_id="t"*30, side="BUY", price=0.05, size=100,
        ttl_seconds=15, poll_seconds=0.25, dry_run=False))
    assert oid == "abc" and note.startswith("filled")

def test_limit_cancels_after_ttl():
    c = FakeClient(post_resp={"orderID": "xyz"},
                   order_states=[{"status": "LIVE"}, {"status": "LIVE"}])
    oid, note = asyncio.run(execution.place_limit_gtd_then_wait(
        c, token_id="t"*30, side="BUY", price=0.05, size=100,
        ttl_seconds=1, poll_seconds=0.25, dry_run=False))
    assert oid == "xyz" and "xyz" in c.cancelled
