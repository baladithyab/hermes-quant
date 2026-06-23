"""Autouse storage isolation for the aggregators package tests.

A BMAAggregator with HERMES_QUANT_HIERARCHICAL_POOLING=1 (ag03) or
HERMES_QUANT_L2_POSTERIOR_PERSIST=1 (L2) persists to module-constant directories
(``pooling_store.POOLING_DIR`` / ``posterior_store.POSTERIOR_DIR``) that are bound
at IMPORT time from ``artifacts.QUANT_HOME`` (a ``Path.home()`` constant). Env-flag
isolation alone does NOT protect the real ``~/.hermes`` — a mid-test
``monkeypatch.setenv("HERMES_QUANT_HOME", ...)`` is too late, the constant is already
bound. So a flag-ON aggregator test would write the operator's real pooling/posterior
store (pool-test-isolation-leak; wave-2 verify finding: POSTERIOR_DIR was uncovered).

This conftest redirects BOTH constants into ``tmp_path`` for EVERY test in
``tests/aggregators/``, mirroring ``tests/learning/conftest.py`` — so any current OR
future aggregator test that builds a BMAAggregator is isolated by construction, not by
a per-file fixture someone must remember to add.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_ag03_pooling_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hermes_quant.learning import pooling_store

    isolated = tmp_path / "ag03_hierarchical_pooling"
    monkeypatch.setattr(pooling_store, "POOLING_DIR", isolated, raising=True)
    return isolated


@pytest.fixture(autouse=True)
def _isolate_l2_posterior_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hermes_quant.learning import posterior_store

    isolated = tmp_path / "l2_learning_posteriors"
    monkeypatch.setattr(posterior_store, "POSTERIOR_DIR", isolated, raising=True)
    return isolated
