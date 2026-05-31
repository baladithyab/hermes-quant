"""hermes_quant.perception.velocity_source — the injectable interest-series seam (PDR-2).

``interest_timestamps_by_symbol`` returns, per touched symbol, the list of observation
timestamps (<= asof) that feed the TrendVelocity producer. It is the ONE seam between
the pure ``velocity`` math and the data source, kept tiny + injectable so tests
monkeypatch it (like ``synthesize._DEFAULT_STORE`` is patched, test_no_lookahead.py).

Silence-by-default: any error / no data -> ``{}`` -> no velocity -> the synthesize swap
falls back to severity -> byte-identical to today. This NEVER raises.

  * Unit/eval phase (no live producers; B08 pending): reads the catalyst packet store
    (``synthesize._DEFAULT_STORE``) — each stored packet's ``asof`` (publication time)
    is an interest observation for its asset symbol, truncated to <= asof.
  * Live phase (B08): the same shape is fed real Reddit/Trends observation timestamps;
    swap the producer below or inject a replacement at the call site.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)


def interest_timestamps_by_symbol(
    symbol: str,
    asof: datetime | pd.Timestamp,
    *,
    horizon: str | None = None,
) -> dict[str, list[datetime]]:
    """Per-symbol observation timestamps (<= asof) for the velocity producer.

    Reads the catalyst packet store and groups each packet's publication ``asof`` by
    its ``asset``. Returns ``{}`` on ANY error or when the store is absent/empty
    (silence-by-default — the caller then sources magnitude from severity). The
    ``symbol`` / ``horizon`` args scope future live producers; today every stored
    symbol's series is returned (the builder scores only the ones it attaches).
    """
    try:
        from hermes_quant.catalyst.synthesize import _DEFAULT_STORE

        asof_ts = pd.Timestamp(asof)
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")
        store = _DEFAULT_STORE
        if not store.exists():
            return {}
        by_symbol: dict[str, list[datetime]] = defaultdict(list)
        for line in store.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue  # valid JSON but not an object (corrupt/partial append) — skip
            asset = raw.get("asset")
            asof_raw = raw.get("asof")
            if not asset or not asof_raw:
                continue
            try:
                ts = pd.Timestamp(asof_raw)
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if ts <= asof_ts:  # LOOKAHEAD CUT — only observations <= the decision asof
                by_symbol[str(asset)].append(ts.to_pydatetime())
        return dict(by_symbol)
    except Exception as exc:  # noqa: BLE001 — silence-by-default, never block frame build
        logger.debug("interest_timestamps_by_symbol(%s) failed: %s", symbol, exc)
        return {}


__all__ = ["interest_timestamps_by_symbol"]
