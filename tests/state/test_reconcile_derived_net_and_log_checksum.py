"""ADR-0091 Option E acceptance gate (:372 reconcile-semantics, :374 byte-identical log).

Two gate items, both about NOT mutating the canonical log:

(:372) RECONCILE SEMANTICS — quant-ledger-reconcile compares the DERIVED net (the
  fold's positions.quantity), NOT the raw fill_size_pct field. The reconcile tool
  (ops/scripts/quant-ledger-reconcile.py) reads the LIVE state.db positions vs a fresh
  reconstruct_from() rebuild of the SAME log and diffs them. Because both sides are the
  DERIVED projection (not the raw size field):
    - log-vs-projection no longer falsely reports 0 divergence: an OLD-fold state.db
      (built flag-OFF: re-affirmations inflated) vs a NEW-fold rebuild (flag-ON: corrected)
      reports a NON-ZERO CHANGED divergence — proving the fix MOVED the projection.
    - the reconcile reads positions.quantity (the carry-forward fold output), never the
      raw fill_size_pct of any single record.

(:374) EXECUTIONS.JSONL BYTE-IDENTICAL — flipping HERMES_QUANT_DELTA_NORMALIZER mutates
  NO bytes of the executions log. The correction is a new INTERPRETATION applied on the
  next rebuild; the append-only event log is never rewritten. A sha256 of the log file is
  identical before and after a flag-OFF rebuild, a flag-ON rebuild, and a settlement pass.

Deterministic, offline. The reconcile diff/positions helpers are imported from the actual
ops script so the test exercises the SHIPPING comparison logic, not a re-derivation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_quant.daemon.settlement_loop import join_exit_fills
from hermes_quant.state.portfolio_state import PortfolioState

_RECONCILE_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-ledger-reconcile.py"
)


def _load_reconcile_module():
    """Import the ops/scripts/quant-ledger-reconcile.py module by path (it is a script,
    not an installed package) so the test uses its ACTUAL _positions/_diff helpers."""
    spec = importlib.util.spec_from_file_location("_quant_ledger_reconcile", _RECONCILE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(asset, target, *, pid, asof, price=100.0, acct="paper-default"):
    return {
        "proposal_id": pid,
        "signal_id": None,
        "asset": asset,
        "asset_class": "equity",
        "timeframe": "1d",
        "asof_decision": asof,
        "asof_execution": asof,
        "target_position_pct": target,
        "decision_price": price,
        "fill_price": price,
        "fill_size_pct": target,  # ABSOLUTE target (the bug shape)
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "account_id": acct,
    }


def _reaffirm_stream(asset="AAPL", target=0.05, n=12) -> list[dict]:
    # proposal_id is namespaced by asset so two streams concatenated do NOT collide on
    # the cs57 5-col dedup key (legacy equity rows key on (proposal_id, asof, "", "", "")).
    return [
        _rec(asset, target, pid=f"{asset}_p{i}", asof=f"2026-06-06T10:{i:02d}:00Z")
        for i in range(n)
    ]


def _write(path: Path, recs: list[dict]) -> None:
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _raw_size_sum(path: Path, asset: str) -> float:
    """The RAW fill_size_pct SUM the reconcile must NOT use — folding raw sizes
    re-creates the inflation (12 x 0.05 == 0.60). The reconcile reads the DERIVED
    positions.quantity instead."""
    total = 0.0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("asset") == asset:
            total += float(r["fill_size_pct"])
    return total


# --------------------------------------------------------------------------- #
# :372 — reconcile compares DERIVED net; OLD-fold vs NEW-fold reports NON-zero.
# --------------------------------------------------------------------------- #


def test_reconcile_reads_derived_net_not_raw_fill_size(tmp_path, monkeypatch):
    """The reconcile's truth (reconstruct_from -> positions.quantity, read by the script's
    _positions) is the DERIVED carry-forward net, NOT the raw fill_size_pct sum.

    Under flag ON, the 12 re-affirms fold to the single intended 0.05 (derived net),
    while the raw fill_size_pct sum is the inflated 0.60. The reconcile reads 0.05."""
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    reconcile = _load_reconcile_module()
    bus = tmp_path / "executions.jsonl"
    _write(bus, _reaffirm_stream())

    rebuilt_db = tmp_path / "rebuilt.db"
    PortfolioState(state_db_path=rebuilt_db).reconstruct_from(bus)
    rebuilt = reconcile._positions(rebuilt_db, "paper-default")

    # The reconcile's DERIVED net is the single intended 0.05.
    assert rebuilt["AAPL"][0] == pytest.approx(0.05, rel=1e-9)
    # ... and that is NOT the raw fill_size_pct sum (which would be the inflated 0.60).
    assert _raw_size_sum(bus, "AAPL") == pytest.approx(0.60, rel=1e-9)
    assert rebuilt["AAPL"][0] != pytest.approx(_raw_size_sum(bus, "AAPL"), rel=1e-3)


def test_old_fold_vs_new_fold_reports_nonzero_divergence(tmp_path, monkeypatch):
    """OLD-fold (flag-OFF) state.db vs NEW-fold (flag-ON) rebuild over the SAME log reports
    a NON-ZERO CHANGED divergence — proving the fix MOVED the projection (the reconcile no
    longer falsely reports 0 divergence for an inflated live book)."""
    reconcile = _load_reconcile_module()
    bus = tmp_path / "executions.jsonl"
    _write(bus, _reaffirm_stream())

    # OLD fold: build the LIVE state.db flag-OFF (re-affirmations inflate to 0.60).
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    live_db = tmp_path / "live_old.db"
    PortfolioState(state_db_path=live_db).reconstruct_from(bus)
    live = reconcile._positions(live_db, "paper-default")
    assert live["AAPL"][0] == pytest.approx(0.60, rel=1e-9), "OLD fold must be inflated"

    # NEW fold: rebuild a scratch db flag-ON (corrected to 0.05).
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    new_db = tmp_path / "new.db"
    PortfolioState(state_db_path=new_db).reconstruct_from(bus)
    rebuilt = reconcile._positions(new_db, "paper-default")
    assert rebuilt["AAPL"][0] == pytest.approx(0.05, rel=1e-9), "NEW fold must be corrected"

    # The reconcile diff reports a NON-ZERO CHANGED divergence (AAPL moved 0.60 -> 0.05).
    phantom, changed, new = reconcile._diff(live, rebuilt)
    assert "AAPL" in changed, (
        "reconcile must report AAPL as CHANGED (0.60 -> 0.05); a raw-fill_size_pct "
        "comparison would have reported 0 divergence (both logs identical)"
    )
    assert phantom == [] and new == []


def test_reconcile_zero_divergence_when_already_corrected(tmp_path, monkeypatch):
    """Byte-identity baseline: a live state.db ALREADY built with the SAME (flag-ON) fold
    as the rebuild reports ZERO divergence — the reconcile is idempotent on a corrected
    book and only flags the OLD-vs-NEW move above."""
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    reconcile = _load_reconcile_module()
    bus = tmp_path / "executions.jsonl"
    _write(bus, _reaffirm_stream())

    live_db = tmp_path / "live_new.db"
    PortfolioState(state_db_path=live_db).reconstruct_from(bus)
    scratch_db = tmp_path / "scratch.db"
    PortfolioState(state_db_path=scratch_db).reconstruct_from(bus)

    live = reconcile._positions(live_db, "paper-default")
    rebuilt = reconcile._positions(scratch_db, "paper-default")
    phantom, changed, new = reconcile._diff(live, rebuilt)
    assert (phantom, changed, new) == ([], [], [])


# --------------------------------------------------------------------------- #
# :374 — flipping the flag mutates NO bytes of executions.jsonl.
# --------------------------------------------------------------------------- #


def test_executions_log_byte_identical_across_flag_flip(tmp_path, monkeypatch):
    """The flag flip is a fold-time INTERPRETATION change — it never rewrites the
    append-only event log. sha256(executions.jsonl) is identical before/after a flag-OFF
    rebuild, a flag-ON rebuild, AND a settlement pass."""
    bus = tmp_path / "executions.jsonl"
    _write(bus, _reaffirm_stream())
    checksum_before = _sha256(bus)

    # flag-OFF rebuild (reads the log, writes state.db only).
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    PortfolioState(state_db_path=tmp_path / "off.db").reconstruct_from(bus)
    assert _sha256(bus) == checksum_before, "flag-OFF rebuild mutated the log"

    # flag-ON rebuild (the historical correction — applied to state.db, NOT the log).
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    PortfolioState(state_db_path=tmp_path / "on.db").reconstruct_from(bus)
    assert _sha256(bus) == checksum_before, "flag-ON rebuild mutated the log"

    # settlement FIFO pass under the flag (the i0c pre-pass works on shallow copies).
    join_exit_fills([json.loads(line) for line in bus.read_text().splitlines() if line.strip()])
    assert _sha256(bus) == checksum_before, "settlement pass mutated the log"

    # The log still contains all 12 raw absolute-target records (record count unchanged).
    lines = [line for line in bus.read_text().splitlines() if line.strip()]
    assert len(lines) == 12
    assert all(json.loads(line)["fill_size_pct"] == pytest.approx(0.05) for line in lines), (
        "every raw record must still carry the ABSOLUTE 0.05 target (no in-place rewrite)"
    )


def test_record_count_unchanged_by_rebuild(tmp_path, monkeypatch):
    """The firing/cap path (reconstruct seed) is unchanged: a rebuild reads the log and
    leaves its record COUNT and content untouched (it only writes state.db)."""
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    bus = tmp_path / "executions.jsonl"
    recs = _reaffirm_stream() + _reaffirm_stream(asset="BA", target=-0.20, n=6)
    _write(bus, recs)
    before_bytes = bus.read_bytes()

    out_db = tmp_path / "out.db"
    res = PortfolioState(state_db_path=out_db).reconstruct_from(bus)

    assert bus.read_bytes() == before_bytes, "rebuild must not touch the log bytes"
    assert res.executions_processed == 18  # 12 AAPL + 6 BA, all folded
    # state.db holds the corrected derived nets.
    con = sqlite3.connect(str(out_db))
    try:
        rows = dict(
            con.execute(
                "SELECT symbol, quantity FROM positions WHERE account_id='paper-default'"
            ).fetchall()
        )
    finally:
        con.close()
    assert rows["AAPL"] == pytest.approx(0.05, rel=1e-9)
    assert rows["BA"] == pytest.approx(-0.20, rel=1e-9)
