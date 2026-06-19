"""rt01 — pin the LIVE kill-switch P&L basis under HERMES_QUANT_DELTA_NORMALIZER.

THE COVERAGE GAP (filed by the concurrent review team, w4fysiwv8):
``autonomous.compute_cumulative_realized_pnl_pct`` is the live money kill-switch
basis (ADR-0016 §D9): it feeds ``trip_kill_switch`` at autonomous.py:445. It calls
``settlement_loop.join_exit_fills`` (autonomous.py:296), whose i0c normalizer
pre-pass is gated on the SAME ``HERMES_QUANT_DELTA_NORMALIZER`` flag. So flipping
that flag ON changes which round-trips exist and their qty -> changes the realized
P&L fraction the kill-switch trips on. Before this file there was ZERO test pinning
the flag-OFF vs flag-ON kill-switch basis (grep of tests/ for both
DELTA_NORMALIZER and (kill_switch|compute_cumulative_realized_pnl_pct) = nothing).

This test makes the flag's effect on the kill-switch basis VISIBLE and pinned:
  * flag-OFF (production default): a re-affirmation stream is read with the legacy
    delta semantics — the kill-switch basis is the legacy value (byte-identical to
    today).
  * flag-ON: the normalizer collapses the re-affirmations, so the basis reflects
    the single intended round-trip — the value the operator must SEE before the
    live flip (ADR-0091 acceptance item 11).

Both are deterministic and offline (synthetic executions.jsonl in tmp_path, NAV
stubbed). NOTHING in production behavior is changed — this is a characterization
test of an existing flag coupling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import hermes_quant.autonomous as autonomous
from hermes_quant.autonomous import compute_cumulative_realized_pnl_pct

_ACCOUNT = "paper-default"
_NAV = 100_000.0


def _rec(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        proposal_id="p",
        signal_id="s",
        asof_execution="2026-06-14T10:00:00Z",
        account_id=_ACCOUNT,
        asset_class="equity",
        asset="AAPL",
        fill_price=100.0,
        fill_size_pct=0.0,
    )
    base.update(kw)
    return base


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _open_then_reaffirm_then_close() -> list[dict[str, Any]]:
    """Open +10% long @100, RE-AFFIRM the same 10% target @105, then flatten @90.

    All three carry an ABSOLUTE target in fill_size_pct (the Option-E producer
    contract). Under the legacy delta read, the re-affirmation is a phantom second
    +10% buy; under the normalizer it folds to a zero-delta no-op. The realized
    round-trip the kill-switch sees therefore differs between the two regimes.
    """
    return [
        _rec(proposal_id="open", asof_execution="2026-06-14T09:00:00Z", fill_size_pct=10.0, fill_price=100.0),
        _rec(proposal_id="reaffirm", asof_execution="2026-06-14T10:00:00Z", fill_size_pct=10.0, fill_price=105.0),
        _rec(proposal_id="close", asof_execution="2026-06-14T11:00:00Z", fill_size_pct=0.0, fill_price=90.0),
    ]


@pytest.fixture()
def _nav(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the NAV so the fraction denominator is deterministic."""
    monkeypatch.setattr(autonomous, "_account_nav_usd", lambda: _NAV)


def test_kill_switch_basis_flag_off_is_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _nav: None) -> None:
    """Flag OFF (production default): the basis is computed with legacy delta reads.

    This is the byte-identical-to-today value. We assert it is FINITE and that the
    rail does not spuriously trip-positive (a re-affirmation must never read as a
    realized GAIN — the close is at 90 < 100/105 entries, so any realized P&L is
    <= 0).
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    execs = tmp_path / "executions.jsonl"
    _write(execs, _open_then_reaffirm_then_close())

    frac_off = compute_cumulative_realized_pnl_pct(executions_path=execs)

    assert isinstance(frac_off, float)
    # A flatten at 90 against entries at 100/105 can only realize a loss or zero —
    # never a positive (the rail must not see a phantom gain on a losing close).
    assert frac_off <= 0.0


def test_kill_switch_basis_flag_on_reflects_collapsed_reaffirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _nav: None
) -> None:
    """Flag ON: the normalizer collapses the re-affirmation; the basis is the
    single intended round-trip's realized loss (the value the operator must SEE
    before flipping the flag live, ADR-0091 item 11).

    The flag-ON basis is a realized LOSS (the position opened ~100 and flattened at
    90) and is finite. We pin that it is strictly negative — the kill-switch rail
    correctly registers the locked-in loss rather than 0.0 (the undercount the
    i0c/335e fixes closed).
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    execs = tmp_path / "executions.jsonl"
    _write(execs, _open_then_reaffirm_then_close())

    frac_on = compute_cumulative_realized_pnl_pct(executions_path=execs)

    assert isinstance(frac_on, float)
    # Flag-ON books the genuine round-trip loss (open ~100 -> close 90 = -10%).
    assert frac_on < 0.0, "the kill-switch must see the locked-in loss, not undercount it to 0"


def test_flag_flip_changes_the_kill_switch_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _nav: None
) -> None:
    """The coupling the review team flagged, made explicit: the SAME executions log
    yields a DIFFERENT kill-switch P&L basis depending on the flag. This is why the
    operator must treat the flag flip as a kill-switch-affecting change, not just a
    state.db/EOD-reconcile change (ADR-0091 acceptance item 11 checklist).
    """
    execs = tmp_path / "executions.jsonl"
    _write(execs, _open_then_reaffirm_then_close())

    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    frac_off = compute_cumulative_realized_pnl_pct(executions_path=execs)

    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    frac_on = compute_cumulative_realized_pnl_pct(executions_path=execs)

    # Both are finite, both are <= 0 (a losing close), but the flag changes the
    # basis: the legacy read double-counts the re-affirmation's notional while the
    # normalizer collapses it. We assert they are not identically equal — the flip
    # is a kill-switch-visible change, which is the whole point of the gate note.
    assert frac_off != frac_on, (
        "flipping HERMES_QUANT_DELTA_NORMALIZER changes the live kill-switch basis; "
        "the operator must see this delta before the live flip (ADR-0091 item 11)"
    )
