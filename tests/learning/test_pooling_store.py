"""d97e — atomic, asof-stamped persistence of the ag03 hierarchical pooler state.

The ag03 HierarchicalPooler holds per-(analyst, regime, epoch) correctness cells
(the effective-n / warm-up state that drives the headline weighting) ONLY in
memory. A cron-mode restart therefore loses the whole warm-up band and an
operator-visible "headline still warming up" signal silently resets to cold. This
module persists a snapshot of the pooler cells + per-analyst epoch map to a single
JSON artifact and loads it back, mirroring posterior_store (atomic tmp+rename,
fail-safe loads, asof stamping).

Pure-Python, offline, deterministic. Money-software TDD: every assertion RED-proven.
"""

from __future__ import annotations

import json

import pandas as pd

from hermes_quant.learning import pooling_store
from hermes_quant.learning.hierarchical_pooling import HierarchicalPooler
from hermes_quant.learning.pooling_store import load_pooler_state, save_pooler_state


def _pooler() -> HierarchicalPooler:
    p = HierarchicalPooler()
    # epoch '' (pre-provenance) cell, n=3 (2 wins / 1 loss)
    p.observe("kronos", "volatile", correct=True)
    p.observe("kronos", "volatile", correct=True)
    p.observe("kronos", "volatile", correct=False)
    # a provenance-tagged cell on a different analyst
    p.observe("sentiment", "bull", correct=True, model_id="model-v1")
    return p


def test_round_trip_preserves_cells_and_epoch_map(tmp_path):
    path = tmp_path / "pooler.json"
    p = _pooler()
    save_pooler_state(p, path=path, asof=pd.Timestamp("2026-06-17T00:00:00Z"))

    reloaded = HierarchicalPooler()
    load_pooler_state(reloaded, path=path)

    # The (analyst, regime, epoch) cells survive byte-for-byte.
    assert reloaded._cells.keys() == p._cells.keys()
    for key, cell in p._cells.items():
        rc = reloaded._cells[key]
        assert rc.alpha == cell.alpha
        assert rc.beta == cell.beta
        assert rc.analyst == cell.analyst
        assert rc.regime == cell.regime
        assert rc.epoch == cell.epoch
    # The per-analyst active-epoch map (so a model change still re-enters warm-up)
    # survives too.
    assert reloaded._epoch_of == p._epoch_of


def test_reload_preserves_effective_n_and_warmup_band(tmp_path):
    """The load-bearing honesty fields — effective_n + warm-up — survive a restart."""
    path = tmp_path / "pooler.json"
    p = _pooler()
    diag_before = p.cell_diagnostics("kronos", "volatile", epoch="")
    save_pooler_state(p, path=path, asof=pd.Timestamp("2026-06-17T00:00:00Z"))

    reloaded = HierarchicalPooler()
    load_pooler_state(reloaded, path=path)
    diag_after = reloaded.cell_diagnostics("kronos", "volatile", epoch="")

    assert diag_after["effective_n"] == diag_before["effective_n"] == 3.0
    assert diag_after["warmup"] == diag_before["warmup"] is True


def test_load_missing_file_is_noop_cold_start(tmp_path):
    """Cold-start: no persisted file yet → the pooler is left empty, never crashes."""
    p = HierarchicalPooler()
    load_pooler_state(p, path=tmp_path / "absent.json")
    assert p._cells == {}
    assert p._epoch_of == {}


def test_load_corrupt_file_is_noop_cold_start(tmp_path):
    """A corrupt artifact must not crash the decision path — cold-start instead."""
    path = tmp_path / "pooler.json"
    path.write_text("{ this is not valid json ", encoding="utf-8")
    p = HierarchicalPooler()
    load_pooler_state(p, path=path)
    assert p._cells == {}


def test_load_skips_corrupt_cell_rows_and_keeps_valid(tmp_path):
    """A poisoned cell row is dropped (cold for that cell); valid cells survive."""
    path = tmp_path / "pooler.json"
    payload = {
        "schema_version": 1,
        "artifact_kind": "ag03_hierarchical_pooling",
        "asof": "2026-06-17T00:00:00+00:00",
        "epoch_of": {"kronos": ""},
        "cells": [
            {"analyst": "kronos", "regime": "volatile", "epoch": "", "alpha": 2.0, "beta": 1.0},
            # negative count — poisoned, must be skipped not loaded
            {"analyst": "kronos", "regime": "bull", "epoch": "", "alpha": -1.0, "beta": 1.0},
            # non-finite — poisoned, must be skipped
            {"analyst": "x", "regime": "y", "epoch": "", "alpha": float("inf"), "beta": 0.0},
        ],
    }
    # float("inf") is not valid JSON via the strict reader; write through json which
    # emits Infinity, and rely on the loader's tolerant parse to reject it.
    path.write_text(json.dumps(payload), encoding="utf-8")
    p = HierarchicalPooler()
    load_pooler_state(p, path=path)
    assert ("kronos", "volatile", "") in p._cells
    assert p._cells[("kronos", "volatile", "")].alpha == 2.0
    assert ("kronos", "bull", "") not in p._cells
    assert ("x", "y", "") not in p._cells


def test_write_is_atomic_no_tmp_left_behind(tmp_path):
    path = tmp_path / "pooler.json"
    save_pooler_state(_pooler(), path=path, asof=pd.Timestamp("2026-06-17T00:00:00Z"))
    assert list(tmp_path.glob("*.tmp")) == []
    assert path.exists()


def test_payload_is_asof_stamped(tmp_path):
    path = tmp_path / "pooler.json"
    asof = pd.Timestamp("2026-06-17T12:00:00Z")
    save_pooler_state(_pooler(), path=path, asof=asof)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["asof"] == asof.isoformat()
    assert "cells" in payload


def test_default_path_isolated_to_tmp(tmp_path, monkeypatch):
    """No explicit path → the module-default dir (redirected into tmp by conftest)."""
    isolated = tmp_path / "ag03_pooler_default"
    monkeypatch.setattr(pooling_store, "POOLING_DIR", isolated, raising=True)
    save_pooler_state(
        _pooler(),
        path=None,
        asof=pd.Timestamp("2026-06-17T00:00:00Z"),
        recipe_key="btc-usdt-mvp",
    )
    written = list(isolated.glob("*.json"))
    assert len(written) == 1
    assert "btc-usdt-mvp" in written[0].name
