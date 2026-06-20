"""Integration-suite fixtures.

aegis-ci-hang guard
-------------------
Several ``test_advisor_e2e`` tests call ``hermes_quant.advisor.recommend()``
WITHOUT passing an explicit ``analysts=`` loadout, so they build the canonical
default loadout (``_build_default_analysts``, ADR-0018) which includes a REAL
``KronosAnalyst``. The first ``analyze()`` with valid bars lazy-loads weights via
``from_pretrained("NeoQuasar/Kronos-*")`` — a live HuggingFace metadata fetch
(and a full weight DOWNLOAD on a checkout with no ``~/.cache/huggingface``). On a
network-less / slow-network CI box that connection blocks and the full pytest
sweep hangs near the end (~95%).

The fixture below flips the HuggingFace hub into OFFLINE mode for the duration of
every integration test. ``from_pretrained`` then resolves from the local cache if
present, or fail-fasts to the already-tested ``KronosAnalyst`` abstain path
(zero-confidence view) — it NEVER opens a socket. Test LOGIC is unchanged: an
abstaining analyst is a supported degrade, and the advisor-e2e tests assert dict
shape + gate behavior, not a Kronos view.

The flip is applied at RUNTIME on the already-imported ``huggingface_hub``
constant (and the ``transformers`` offline mirror, if present), because both read
their offline flag from a module-level constant snapshotted at import time —
setting the ``HF_HUB_OFFLINE`` env var alone would be a no-op for an already
imported hub. ``monkeypatch`` restores the originals after each test, so a test
that explicitly opts into a live fetch (none today) is unaffected and no global
state leaks across the session.

A genuinely-live integration run can opt OUT by setting
``HERMES_QUANT_LIVE_HF=1`` (mirrors the ``HERMES_QUANT_LIVE_LLM`` opt-in idiom in
the top-level conftest). This is OFF by default — the offline guard is the
security/CI-correct default for an offline test suite.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _hf_hub_offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the HuggingFace hub offline so the real default-loadout Kronos
    analyst can never reach the live Hub during an integration test."""
    if os.environ.get("HERMES_QUANT_LIVE_HF", "").strip() == "1":
        # Explicit live opt-in: leave the hub online.
        return

    # huggingface_hub reads HF_HUB_OFFLINE into a module constant at import time;
    # flip the live constant so already-imported callers see offline mode.
    try:
        import huggingface_hub.constants as hf_constants

        monkeypatch.setattr(hf_constants, "HF_HUB_OFFLINE", True, raising=False)
    except ImportError:
        pass

    # transformers keeps its own offline mirror; flip it too if present.
    try:
        import transformers.utils.hub as tf_hub

        monkeypatch.setattr(tf_hub, "_is_offline_mode", True, raising=False)
    except (ImportError, AttributeError):
        pass

    # Belt-and-suspenders for any code path / subprocess that re-reads the env.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
