"""ar15 (cs14/cs16/cs17 schema-drift family): the quarterly review's
``load_positions_from_executions`` must consume the REAL live producer shape.

The defect: the loader filtered ``if rec.get("schema_version") != 1: continue``
and then read ``rec["side"]`` / ``rec["qty"]``. But the LIVE producer
(``hermes_quant.react.base.ExecutionRecord`` serialized via
``hermes_quant.react.paper._record_to_dict``) emits NO ``schema_version`` key
(so ``.get(...)`` is ``None != 1`` -> dropped), NO ``side`` and NO ``qty`` —
it carries a signed ``target_position_pct`` (and ``fill_size_pct``) plus
``fill_price``. Result: every live execution was dropped, the quarterly review
reconstructed an EMPTY book, reported cash-only NAV, and emitted ZERO
factor/sector/beta breach proposals. A silently-empty risk surface is a
fail-open defect.

These tests build REAL ExecutionRecords, serialize them via the SAME
``_record_to_dict`` the producer uses (never a hand-rolled dict — that is
exactly how this family hid before), write them to a temp executions.jsonl,
and assert the quarterly loader reconstructs a NON-EMPTY book. The remediation
mirrors the cs14 weekly-exit loader fix (absolute-target / signed-NAV-fraction
reconstruction).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import _record_to_dict

QUARTERLY_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "quant-playbook-quarterly.py"
)
if not QUARTERLY_SCRIPT.exists():
    QUARTERLY_SCRIPT = Path.home() / ".hermes" / "scripts" / "quant-playbook-quarterly.py"

pytestmark = pytest.mark.skipif(
    not QUARTERLY_SCRIPT.exists(),
    reason=f"quant-playbook-quarterly.py not found at {QUARTERLY_SCRIPT}",
)


@pytest.fixture(scope="module")
def quarterly_module():
    """Import the script as a module without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "quarterly_under_test_schema", QUARTERLY_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quarterly_under_test_schema"] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_record(
    *,
    asset: str,
    target_position_pct: float,
    fill_price: float,
    asof_execution: str,
    reactor_name: str = "paper",
    reactor_metadata: dict | None = None,
    asset_class: str = "equity",
) -> dict:
    """Build a REAL ExecutionRecord and serialize via the producer's own
    ``_record_to_dict``. This is the exact shape the live bus carries — no
    ``schema_version``, no ``side``, no ``qty`` keys."""
    rec = ExecutionRecord(
        proposal_id=f"prop-{asset}-{asof_execution}",
        signal_id=f"sig-{asset}",
        asset=asset,
        asset_class=asset_class,
        timeframe="1d",
        asof_decision=asof_execution,
        asof_execution=asof_execution,
        target_position_pct=target_position_pct,
        decision_price=fill_price,
        fill_price=fill_price,
        fill_size_pct=target_position_pct,
        reactor_name=reactor_name,
        human_in_the_loop=True,
        approver_user_id=None,
        reactor_metadata=reactor_metadata,
    )
    return _record_to_dict(rec)


def _write_bus(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "executions.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Producer-shape sanity: prove the keys the OLD loader required are ABSENT.
# --------------------------------------------------------------------------- #


def test_live_record_has_no_legacy_keys():
    """The live producer shape carries none of schema_version/side/qty."""
    rec = _real_record(
        asset="NVDA",
        target_position_pct=0.20,
        fill_price=500.0,
        asof_execution="2026-06-01T14:30:00Z",
    )
    assert rec.get("schema_version") is None  # .get(...) != 1 -> old loader drops it
    assert "side" not in rec
    assert "qty" not in rec
    # but the signed NAV-fraction target IS present
    assert rec["target_position_pct"] == 0.20
    assert rec["fill_price"] == 500.0


# --------------------------------------------------------------------------- #
# RED: the loader must NOT silently return an empty book for live records.
# --------------------------------------------------------------------------- #


def test_loader_reconstructs_nonempty_book_from_live_records(
    quarterly_module, tmp_path, monkeypatch
):
    qm = quarterly_module
    # A real long fill: +20% NAV in NVDA @ $500. With the 100k default NAV the
    # implied share count is 0.20 * 100_000 / 500 = 40 shares long.
    bus = _write_bus(
        tmp_path,
        [
            _real_record(
                asset="NVDA",
                target_position_pct=0.20,
                fill_price=500.0,
                asof_execution="2026-06-01T14:30:00Z",
            ),
        ],
    )
    monkeypatch.setattr(qm, "EXECUTIONS_PATH", bus)

    cash, positions = qm.load_positions_from_executions()

    # The bug reconstructs an EMPTY book (silently-empty risk surface).
    assert len(positions) == 1, (
        "live execution record was dropped -> empty book (fail-open). "
        f"got positions={positions}"
    )
    nvda = positions[0]
    assert nvda.symbol == "NVDA"
    # 0.20 * 100_000 / 500 = 40 shares LONG (signed positive).
    assert nvda.qty == pytest.approx(40.0)
    assert nvda.last_price == pytest.approx(500.0)
    # market value ~ 0.20 * NAV
    assert nvda.market_value == pytest.approx(20_000.0)


def test_loader_signs_short_target_negative(quarterly_module, tmp_path, monkeypatch):
    qm = quarterly_module
    # A short fill: -10% NAV in TSLA @ $250 -> -40 shares.
    bus = _write_bus(
        tmp_path,
        [
            _real_record(
                asset="TSLA",
                target_position_pct=-0.10,
                fill_price=250.0,
                asof_execution="2026-06-02T14:30:00Z",
            ),
        ],
    )
    monkeypatch.setattr(qm, "EXECUTIONS_PATH", bus)
    cash, positions = qm.load_positions_from_executions()
    assert len(positions) == 1
    tsla = positions[0]
    assert tsla.symbol == "TSLA"
    # -0.10 * 100_000 / 250 = -40 shares SHORT (signed negative).
    assert tsla.qty == pytest.approx(-40.0)
    assert tsla.market_value == pytest.approx(-10_000.0)


def test_loader_prefers_reactor_metadata_quantity(
    quarterly_module, tmp_path, monkeypatch
):
    qm = quarterly_module
    # reactor_metadata.quantity is the AUTHORITATIVE signed absolute share count
    # (det-equity / live broker anchor). It wins over the NAV-fraction derivation.
    bus = _write_bus(
        tmp_path,
        [
            _real_record(
                asset="AAPL",
                target_position_pct=0.05,
                fill_price=200.0,
                asof_execution="2026-06-03T14:30:00Z",
                reactor_name="deterministic-equity",
                reactor_metadata={"quantity": 33.0},
            ),
        ],
    )
    monkeypatch.setattr(qm, "EXECUTIONS_PATH", bus)
    cash, positions = qm.load_positions_from_executions()
    assert len(positions) == 1
    aapl = positions[0]
    assert aapl.symbol == "AAPL"
    assert aapl.qty == pytest.approx(33.0)  # authoritative, not 0.05*100k/200=25


def test_loader_latest_target_supersedes_not_sums(
    quarterly_module, tmp_path, monkeypatch
):
    qm = quarterly_module
    # Two fills for the same symbol — the LATER one SUPERSEDES (absolute target),
    # it does NOT delta-sum (which would double-count / dual-ledger inflate).
    bus = _write_bus(
        tmp_path,
        [
            _real_record(
                asset="MSFT",
                target_position_pct=0.10,
                fill_price=400.0,
                asof_execution="2026-06-01T14:30:00Z",
            ),
            _real_record(
                asset="MSFT",
                target_position_pct=0.25,
                fill_price=400.0,
                asof_execution="2026-06-05T14:30:00Z",  # later -> wins
            ),
        ],
    )
    monkeypatch.setattr(qm, "EXECUTIONS_PATH", bus)
    cash, positions = qm.load_positions_from_executions()
    assert len(positions) == 1
    msft = positions[0]
    # latest target 0.25 -> 0.25*100k/400 = 62.5 shares (NOT 0.35 summed).
    assert msft.qty == pytest.approx(62.5)
    assert msft.market_value == pytest.approx(25_000.0)


def test_loader_drops_flat_latest_target(quarterly_module, tmp_path, monkeypatch):
    qm = quarterly_module
    # Open then close-to-flat: the latest target of 0.0 means the position is
    # closed and must be DROPPED (not reported as a zero-qty ghost).
    bus = _write_bus(
        tmp_path,
        [
            _real_record(
                asset="GOOG",
                target_position_pct=0.15,
                fill_price=150.0,
                asof_execution="2026-06-01T14:30:00Z",
            ),
            _real_record(
                asset="GOOG",
                target_position_pct=0.0,
                fill_price=160.0,
                asof_execution="2026-06-06T14:30:00Z",  # flatten
            ),
        ],
    )
    monkeypatch.setattr(qm, "EXECUTIONS_PATH", bus)
    cash, positions = qm.load_positions_from_executions()
    assert positions == []


# --------------------------------------------------------------------------- #
# GREEN end-to-end: the risk surface is no longer silently empty — a
# concentrated live book produces factor-exposure breach proposals.
# --------------------------------------------------------------------------- #


def test_live_book_produces_breach_proposals(quarterly_module, tmp_path, monkeypatch):
    qm = quarterly_module
    # One live fill at 50% NAV in a single name. Reconstructed it is a top-1
    # concentration (> 15%) AND a net-dollar-exposure breach (> 60%? no, 50% —
    # but top-1 and big single position fire). Prove proposals are NON-empty.
    bus = _write_bus(
        tmp_path,
        [
            _real_record(
                asset="NVDA",
                target_position_pct=0.50,
                fill_price=500.0,
                asof_execution="2026-06-01T14:30:00Z",
            ),
        ],
    )
    monkeypatch.setattr(qm, "EXECUTIONS_PATH", bus)
    cash, positions = qm.load_positions_from_executions()
    assert positions, "reconstructed an empty book from a real live fill"

    # Mark the position deterministically (no network) and compute metrics.
    for p in positions:
        p.last_price = 500.0
        p.sector = "Tech"
        p.beta = 1.0
    metrics = qm.compute_metrics(cash, positions)

    # The risk surface SEES the position: top-1 concentration breach fires.
    assert any("top-1 concentration: NVDA" in f for f in metrics.flags), (
        f"no breach flagged for a 50%-NAV single name; flags={metrics.flags}"
    )
    assert metrics.rebalance_proposals, (
        "ZERO rebalance proposals from a concentrated live book "
        "(silently-empty risk surface)"
    )
    assert any(
        prop["kind"] == "trim_top_position" for prop in metrics.rebalance_proposals
    )
