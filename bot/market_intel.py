"""Resolution timing and other market metadata from Gamma payloads."""

from __future__ import annotations

import datetime as dt
import os
from typing import Any, Optional


# E14h: numeric timestamps from Gamma come in either seconds or milliseconds.
# A naive threshold of 1e12 (≈ year 33658 in seconds) cleanly separates
# millis-since-epoch from seconds-since-epoch today, but Unix seconds will
# cross this around year 33658 — not 2033 as one might guess from a
# *binary* 2^31 cliff. The 2033 risk is a 32-bit time_t overflow, which is
# unrelated to this parser but worth flagging since the magic number used
# to be the only thing standing between the parser and a 1970 datetime.
# The threshold is configurable via MARKET_INTEL_MS_THRESHOLD so the
# default can be lowered (e.g. for tests) without a code change.
_MS_THRESHOLD = float(os.environ.get("MARKET_INTEL_MS_THRESHOLD", "1e12") or 1e12)


def _raw_dict(market: dict[str, Any]) -> dict[str, Any]:
    r = market.get("raw")
    return r if isinstance(r, dict) else market


def hours_until_resolution_end(market: dict[str, Any]) -> Optional[float]:
    """
    Hours until market end / resolution, if parseable from Gamma-style fields.
    Returns None if unknown (gate should not block).
    """
    raw = _raw_dict(market)
    candidates: list[Any] = []
    for k in ("endDate", "end_date_iso", "umaEndDate"):
        v = raw.get(k)
        if v is not None and v != "":
            candidates.append(v)
    now = dt.datetime.now(dt.timezone.utc)
    for v in candidates:
        # Treat numerics above the configured threshold as milliseconds; the
        # legacy magic `1e12` is now `_MS_THRESHOLD` (env-overridable).
        if isinstance(v, (int, float)) and v > _MS_THRESHOLD:
            try:
                end = dt.datetime.fromtimestamp(float(v) / 1000.0, tz=dt.timezone.utc)
                return (end - now).total_seconds() / 3600.0
            except (OSError, OverflowError, ValueError):
                continue
        s = str(v).strip()
        if not s or s.lower() in ("null", "none"):
            continue
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            end = dt.datetime.fromisoformat(s.replace(" ", "T"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=dt.timezone.utc)
            return (end - now).total_seconds() / 3600.0
        except (TypeError, ValueError):
            continue
    return None
