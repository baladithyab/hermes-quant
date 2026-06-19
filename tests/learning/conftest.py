"""Autouse isolation for the L2 learning package tests.

The top-level conftest auto-isolates governance / evidence / portfolio-state and
all ``HERMES_QUANT_*`` env flags, but the L2 posterior store keys its default
location off a *module constant* (``posterior_store.POSTERIOR_DIR``), not an env
var. Env-flag isolation alone would not protect the real ``~/.hermes`` from a
test that calls ``save_posteriors(path=None)``. This fixture monkeypatches the
constant into ``tmp_path`` so a learning test can never pollute the user home.

The same hazard applies to the ag03 pooler store (``pooling_store.POOLING_DIR``,
d97e), so it is redirected too.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_l2_posterior_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hermes_quant.learning import posterior_store

    isolated = tmp_path / "l2_learning_posteriors"
    monkeypatch.setattr(posterior_store, "POSTERIOR_DIR", isolated, raising=True)
    return isolated


@pytest.fixture(autouse=True)
def _isolate_ag03_pooling_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hermes_quant.learning import pooling_store

    isolated = tmp_path / "ag03_hierarchical_pooling"
    monkeypatch.setattr(pooling_store, "POOLING_DIR", isolated, raising=True)
    return isolated
