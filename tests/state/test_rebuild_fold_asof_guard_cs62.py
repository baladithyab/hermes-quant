"""cs62 — rebuild-fold asof-validation parity with the incremental fold (ADR-0004 money-state).

state.db is a CACHE; executions.jsonl is canonical; reconstruct_from is the rebuild path
ops/scripts/quant-ledger-reconcile.py --apply trusts as truth. A THIRD fold-divergence
axis (siblings: cs52 account-partition, cs57 byte-dup dedup, cs51 per-leg key, cs44 parent
skip):

  cs62 (asof-validation asymmetry): the incremental fold (_apply_execution_unsafe) parses
        and validates asof_execution and REJECTS a future-bound or unparseable value (a
        no-lookahead/poison guard, portfolio_state.py:890-907), so the live incremental
        book never folds a poisoned record. But the rebuild fold (_replay_record) had NO
        such asof guard, so reconstruct_from FOLDED a poisoned record (future-dated or
        unparseable asof) the live book correctly dropped — the rebuild DIVERGED from the
        live book, AND a `--apply` would CORRUPT the live state.db to the poisoned rebuild
        AND WEDGE the asof watermark (a future-dated asof advances the watermark past real
        time, after which every real fill looks stale and is skipped).

  THE FIX: both folds call the SAME extracted _validate_asof helper (the cs5257 factoring
  discipline — _resolve_account / _dedup_key are already shared). A clean (valid,
  non-future, parseable) asof is byte-identical in both folds; a poison raises in
  _replay_record, is caught by reconstruct_from's per-record try/except, and lands in
  result.errors WITHOUT being folded — mirroring the incremental reject. The watermark is
  derived from only successfully-FOLDED asofs (max of last_ts), so a poison can never wedge
  it.

Deterministic, no network. HERMES_QUANT_DELTA_NORMALIZER pinned to 0 in each test (the
default-OFF legacy-equivalent fold).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.state.portfolio_state import PortfolioState, _validate_asof

_ASOF = "2026-06-13T15:00:00.000000Z"
_FUTURE_POISON = "9999-12-31T23:59:59.000000Z"
_UNPARSEABLE_POISON = "not-a-timestamp"


def _write(path: Path, recs: list[dict]) -> None:
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _equity_fill(asof: str = _ASOF) -> dict:
    """A plain paper-default equity fill (legacy NAV-fraction path), no leg_quantity,
    top-level account_id present — same shape the cs5257 parity tests use."""
    return {
        "proposal_id": "eq_prop_1",
        "asset": "SPY",
        "asset_class": "equity",
        "asof_execution": asof,
        "fill_price": 100000.0,
        "fill_size_pct": 0.05,
        "reactor_name": "paper",
        "account_id": "paper-default",
    }


def test_cs62_future_dated_asof_rejected_by_rebuild_fold(tmp_path, monkeypatch) -> None:
    """cs62 RED->GREEN: a future-dated asof poison must be REJECTED by the rebuild fold,
    just as the incremental fold rejects it.

    RED today: reconstruct_from folds the poison (SPY 0.05 / cash 95000 / watermark 9999...
    / errors []). GREEN after _replay_record calls the shared _validate_asof: the record
    lands in result.errors, is NOT folded, and the watermark is NOT wedged.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    execs = tmp_path / "executions.jsonl"
    _write(execs, [_equity_fill(_FUTURE_POISON)])

    ps = PortfolioState(state_db_path=tmp_path / "cs62_future.db")
    res = ps.reconstruct_from(execs)

    # The poison did NOT fold.
    assert ("equity", "SPY") not in ps.get_positions("paper-default")
    assert ps.get_cash("paper-default") is None
    assert res.executions_processed == 0
    # It is surfaced as an error mirroring the incremental reject message.
    assert res.errors, "future-dated poison should be recorded as an error, not silently folded"
    assert any("unparseable or future-bound" in msg for _, msg in res.errors)
    # The watermark is NOT wedged to 9999...
    assert ps.get_watermark() is None


def test_cs62_unparseable_asof_rejected_by_rebuild_fold(tmp_path, monkeypatch) -> None:
    """cs62 RED->GREEN: an unparseable asof poison must be REJECTED by the rebuild fold."""
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    execs = tmp_path / "executions.jsonl"
    _write(execs, [_equity_fill(_UNPARSEABLE_POISON)])

    ps = PortfolioState(state_db_path=tmp_path / "cs62_unparse.db")
    res = ps.reconstruct_from(execs)

    assert ("equity", "SPY") not in ps.get_positions("paper-default")
    assert ps.get_cash("paper-default") is None
    assert res.executions_processed == 0
    assert res.errors
    assert any("unparseable or future-bound" in msg for _, msg in res.errors)
    assert ps.get_watermark() is None


def test_cs62_rebuild_matches_incremental_on_poison(tmp_path, monkeypatch) -> None:
    """cs62 RED->GREEN: the rebuild fold and the incremental fold AGREE on a poison —
    both yield an empty book (no position, no cash).

    The incremental fold raises (caller swallows + audits); the rebuild fold records the
    error and folds nothing. Both books are empty => folds agree.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")

    # ── INCREMENTAL fold: apply_execution swallows the raise (audited) ──
    inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    inc.apply_execution(_equity_fill(_FUTURE_POISON))  # rejected, audited, book unchanged
    inc_pos = inc.get_positions("paper-default")
    inc_cash = inc.get_cash("paper-default")

    # ── REBUILD fold ──
    execs = tmp_path / "executions.jsonl"
    _write(execs, [_equity_fill(_FUTURE_POISON)])
    reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    reb.reconstruct_from(execs)
    reb_pos = reb.get_positions("paper-default")
    reb_cash = reb.get_cash("paper-default")

    # Both folds drop the poison: empty book on both sides => no divergence.
    assert inc_pos == {}
    assert reb_pos == {}
    assert inc_cash is None
    assert reb_cash is None


def test_cs62_watermark_reflects_only_folded_asofs(tmp_path, monkeypatch) -> None:
    """cs62 RED->GREEN: the rebuild watermark must reflect only successfully-FOLDED asofs,
    never a poison's asof.

    Log = [clean@2026-06-13T15:00, poison@9999]. The clean fill folds; the poison is
    rejected. The watermark must be the clean asof, NOT 9999 (RED today: 9999 wedges it).
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    execs = tmp_path / "executions.jsonl"
    # Distinct proposal ids so the cs57 dedup does not collapse them.
    clean = _equity_fill(_ASOF)
    poison = _equity_fill(_FUTURE_POISON)
    poison["proposal_id"] = "eq_prop_poison"
    _write(execs, [clean, poison])

    ps = PortfolioState(state_db_path=tmp_path / "cs62_wm.db")
    res = ps.reconstruct_from(execs)

    # The clean fill folded; the poison did not.
    assert ("equity", "SPY") in ps.get_positions("paper-default")
    assert res.executions_processed == 1
    assert res.errors
    # The watermark is the clean asof, NOT the poison's 9999.
    assert ps.get_watermark() == _ASOF
    assert ps.get_watermark() != _FUTURE_POISON


def test_cs62_apply_execution_unsafe_uses_shared_validator(tmp_path, monkeypatch) -> None:
    """Incremental byte-identity rail: _apply_execution_unsafe still rejects a >24h-future
    asof (match='future') and accepts a +1h asof — the extracted helper preserves the exact
    same parse/threshold/exception behavior the governance test pins.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    ps = PortfolioState(state_db_path=tmp_path / "inc_rail.db")

    far_future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    rec = _equity_fill(far_future)
    with pytest.raises(ValueError, match="future"):
        ps._apply_execution_unsafe(rec)

    near_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rec_ok = _equity_fill(near_future)
    rec_ok["proposal_id"] = "eq_prop_near"
    ps._apply_execution_unsafe(rec_ok)  # must NOT raise (clock-skew tolerance)


def test_cs62_clean_rebuild_byte_identical(tmp_path, monkeypatch) -> None:
    """Byte-identity baseline: a single clean paper-default equity fill folds to the SAME
    book as before the cs62 fix. A valid, non-future, parseable asof passes _validate_asof
    untouched, and the watermark = max(folded asofs) = the clean asof.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    execs = tmp_path / "clean.jsonl"
    _write(execs, [_equity_fill(_ASOF)])

    ps = PortfolioState(state_db_path=tmp_path / "cs62_clean.db")
    res = ps.reconstruct_from(execs)

    qty = ps.get_positions("paper-default")[("equity", "SPY")].quantity
    cash = ps.get_cash("paper-default")
    assert qty == 0.05
    assert abs(cash.balance_usd - 95000.0) < 1e-9
    assert abs(cash.equity_total - 100000.0) < 1e-9
    assert ps.get_watermark() == _ASOF
    assert res.errors == []
    assert res.executions_processed == 1


def test_cs62_validate_asof_helper_contract() -> None:
    """Unit contract for the extracted _validate_asof helper: returns None for clean /
    +1h / Z-suffix / naive-past asofs; raises ValueError(match='unparseable or
    future-bound') for a far-future, an unparseable string, and a >24h-future value.
    """
    # Accepts: clean canonical, Z-suffix, +1h clock skew, naive past.
    assert _validate_asof(_ASOF) is None
    assert _validate_asof("2026-06-13T15:00:00.000000Z") is None
    near_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _validate_asof(near_future) is None
    assert _validate_asof("2020-01-01T00:00:00") is None  # naive, far past

    # Rejects: far-future, unparseable, >24h future.
    with pytest.raises(ValueError, match="unparseable or future-bound"):
        _validate_asof(_FUTURE_POISON)
    with pytest.raises(ValueError, match="unparseable or future-bound"):
        _validate_asof(_UNPARSEABLE_POISON)
    far_future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    with pytest.raises(ValueError, match="unparseable or future-bound"):
        _validate_asof(far_future)
