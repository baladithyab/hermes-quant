"""c96e — atomic, asof-stamped persistence of per-analyst Beta posteriors.

The store serializes a ``{analyst_name: (alpha, beta, n_observations,
last_observable_asof)}`` snapshot to a single JSON artifact via the canonical
atomic-write helper (tmp + rename), and loads it back byte-for-byte. This is
what lets per-analyst skill survive across recommend() calls and process
restarts instead of resetting to the prior every time.

Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import json

import pandas as pd

from hermes_quant.learning import posterior_store
from hermes_quant.learning.posterior_store import (
    PersistedPosterior,
    load_posteriors,
    save_posteriors,
)


def _snapshot() -> dict[str, PersistedPosterior]:
    return {
        "classical-ta": PersistedPosterior(
            alpha=7.0,
            beta=5.0,
            n_observations=2,
            last_observable_asof=pd.Timestamp("2026-05-30T00:00:00Z"),
        ),
        "kronos": PersistedPosterior(
            alpha=5.0,
            beta=6.0,
            n_observations=1,
            last_observable_asof=pd.Timestamp("2026-05-29T00:00:00Z"),
        ),
    }


def test_round_trip_preserves_posteriors(tmp_path):
    path = tmp_path / "posteriors.json"
    snap = _snapshot()
    save_posteriors(snap, path=path, asof=pd.Timestamp("2026-06-01T00:00:00Z"))

    loaded = load_posteriors(path=path)
    assert loaded == snap


def test_load_missing_file_returns_empty_not_crash(tmp_path):
    """Cold-start: no persisted file yet → empty dict, never an exception."""
    loaded = load_posteriors(path=tmp_path / "does-not-exist.json")
    assert loaded == {}


def test_load_corrupt_file_returns_empty_not_crash(tmp_path):
    """A corrupt artifact must not crash the aggregator — fall back to empty
    (which the caller treats as cold-start), mirroring the calibrator pickle's
    fail-safe behavior."""
    path = tmp_path / "posteriors.json"
    path.write_text("{ this is not valid json ", encoding="utf-8")
    loaded = load_posteriors(path=path)
    assert loaded == {}


def test_write_is_atomic_no_tmp_left_behind(tmp_path):
    path = tmp_path / "posteriors.json"
    save_posteriors(_snapshot(), path=path, asof=pd.Timestamp("2026-06-01T00:00:00Z"))
    # The tmp file used during the atomic rename must not survive.
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    assert path.exists()


def test_payload_is_asof_stamped(tmp_path):
    """The artifact records the asof at which it was written, for auditability
    and so a future loader can reason about staleness."""
    path = tmp_path / "posteriors.json"
    asof = pd.Timestamp("2026-06-01T12:00:00Z")
    save_posteriors(_snapshot(), path=path, asof=asof)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["asof"] == asof.isoformat()
    assert "posteriors" in payload


def test_default_path_is_isolated_to_tmp(tmp_path, monkeypatch):
    """When no explicit path is given, the store uses the module-level default
    dir. The autouse fixture (below) redirects that constant into tmp_path so a
    test never writes the real ~/.hermes. This asserts the wiring works."""
    isolated = tmp_path / "l2_posteriors_default"
    monkeypatch.setattr(posterior_store, "POSTERIOR_DIR", isolated, raising=True)
    save_posteriors(
        _snapshot(),
        path=None,
        asof=pd.Timestamp("2026-06-01T00:00:00Z"),
        recipe_key="btc-usdt-mvp",
    )
    written = list(isolated.glob("*.json"))
    assert len(written) == 1
    assert "btc-usdt-mvp" in written[0].name
