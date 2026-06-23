"""Regression guard for aegis-ci-hang: the advisor e2e suite must never make a
live HuggingFace network fetch.

ROOT CAUSE (the ~95% full-sweep hang)
-------------------------------------
``hermes_quant.advisor._build_default_analysts()`` includes a *real*
``KronosAnalyst`` (ADR-0018). Every ``test_advisor_e2e`` test that calls
``recommend()`` WITHOUT passing an explicit ``analysts=`` loadout therefore
builds that real analyst, and the first ``analyze()`` with valid bars triggers
``KronosAnalyst._lazy_load()`` ->
``KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")`` +
``Kronos.from_pretrained("NeoQuasar/Kronos-base")``. ``from_pretrained``
performs a live HuggingFace metadata fetch (and, on a clean checkout with no
``~/.cache/huggingface`` cache, a full weight DOWNLOAD). On a network-less or
slow-network CI box that connection blocks — the full ``pytest`` sweep hangs
near the end with no per-test guard that fail-fasts.

THE GUARD (matching the repo's offline-test idiom)
--------------------------------------------------
``tests/integration/conftest.py`` installs an autouse fixture that flips the
HuggingFace hub into OFFLINE mode for the duration of every advisor-e2e test.
``from_pretrained`` then uses the local cache if present, or fail-fasts to the
already-tested ``KronosAnalyst`` abstain path (zero-confidence view) — it NEVER
opens a socket. The advisor's documented behavior is unchanged: an abstaining
analyst is a supported degrade, and these tests assert dict SHAPE + gate
behavior, not a Kronos view. A tight ``@pytest.mark.timeout`` on the module is
a second belt-and-suspenders rail so any residual hang dies fast.

This file asserts the guard is ACTIVE — it RED-proves the bug: with the guard
removed, ``HF_HUB_OFFLINE`` is False during ``recommend()`` and the default
loadout is free to reach the live Hub.
"""

from __future__ import annotations

import pandas as pd
import pytest

# cx-advisor-hf-import (codex PR#91 P2): huggingface_hub is an OPTIONAL dep (the
# KronosAnalyst is the only consumer, and the analyst degrades to an abstain path
# when it's absent). Importing it at module scope crashed COLLECTION on any env
# without it (a collection error fails the whole run, not just this module). Skip
# the module cleanly when HF is unavailable — the OFFLINE-guard contract it asserts
# only has meaning when huggingface_hub is installed in the first place.
hf_constants = pytest.importorskip("huggingface_hub.constants")

from hermes_quant.advisor import _build_default_analysts, recommend


def _make_bars(n: int = 100, start_price: float = 100.0) -> pd.DataFrame:
    timestamps = pd.date_range(start="2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = [start_price + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.3 for c in closes],
            "low": [c - 0.4 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
        }
    )


class _FakeProvider:
    name = "fake"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self) -> None:
        self._bars = _make_bars()

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True):
        return self._bars


def test_default_loadout_includes_real_kronos() -> None:
    """Documents WHY the guard is needed: the default loadout really does build
    a live-network-capable KronosAnalyst. If this ever stops being true the
    guard can be revisited; until then it must stay."""
    analysts = _build_default_analysts()
    names = [type(a).__name__ for a in analysts]
    assert "KronosAnalyst" in names, (
        "default loadout no longer includes KronosAnalyst; the ci-hang guard "
        "premise has changed — re-evaluate tests/integration/conftest.py"
    )


def test_recommend_default_loadout_runs_with_hub_offline() -> None:
    """The autouse offline guard must be ACTIVE while the default-loadout
    ``recommend()`` runs, so the real KronosAnalyst can never open a socket to
    HuggingFace. RED without the guard: ``HF_HUB_OFFLINE`` is False here and the
    real ``from_pretrained`` is free to make a live metadata fetch / download
    (the ~95% sweep hang).
    """
    # The guard must be active before we ever touch the default loadout.
    assert hf_constants.HF_HUB_OFFLINE is True, (
        "HuggingFace hub is ONLINE during an advisor-e2e test — the default "
        "KronosAnalyst loadout can reach the live Hub and hang the sweep; "
        "the offline guard in tests/integration/conftest.py is missing"
    )

    # No analysts= -> real default loadout (incl. KronosAnalyst). With the guard
    # active this completes offline (cache-or-abstain), never on the network.
    result = recommend(
        symbol="FAKE",
        asset_class="equity",
        timeframe="1d",
        provider=_FakeProvider(),
        include_lessons=False,
    )
    assert result["symbol"] == "FAKE"
    assert isinstance(result["analyst_views"], list)
