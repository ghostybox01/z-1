"""Single place that builds the Polymarket CLOB V2 client and applies the proxy."""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from py_clob_client_v2.client import ClobClient
import py_clob_client_v2.http_helpers.helpers as _clob_http

log = logging.getLogger("polymarket.clob")

CLOB_HOST = "https://clob.polymarket.com"  # V2 server is the same host (confirmed by spike)


def apply_clob_proxy(proxy_url: str) -> None:
    """Route ALL CLOB traffic through the proxy by replacing the SDK's module-level httpx client."""
    url = (proxy_url or "").strip()
    if not url:
        return
    try:
        _clob_http._http_client = httpx.Client(http2=True, proxy=url)
        log.info("CLOB proxy active: %s…", url[:40])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("CLOB proxy setup failed: %s", exc)


def build_clob_client(*, private_key: str, signature_type: int, funder: Optional[str]) -> ClobClient:
    """Construct the V2 client and derive L2 creds. Caller applies the proxy FIRST."""
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=137,
        signature_type=signature_type,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_key())
    return client
