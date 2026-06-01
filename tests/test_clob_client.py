from bot.clob_client import apply_clob_proxy, build_clob_client

class _FakeHelpers:
    _http_client = None

def test_apply_clob_proxy_patches_module_client(monkeypatch):
    fake = _FakeHelpers()
    monkeypatch.setattr("bot.clob_client._clob_http", fake, raising=False)
    apply_clob_proxy("http://user:pass@host:1234")
    assert fake._http_client is not None  # patched with a proxied httpx.Client

def test_apply_clob_proxy_noop_on_empty(monkeypatch):
    fake = _FakeHelpers()
    monkeypatch.setattr("bot.clob_client._clob_http", fake, raising=False)
    apply_clob_proxy("")
    assert fake._http_client is None
