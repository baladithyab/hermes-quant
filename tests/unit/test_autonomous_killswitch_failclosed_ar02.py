"""ar02 — the AUTONOMOUS daily-loss kill-switch (ADR-0016 D9) must not fail-OPEN.

Codebase-archaeology (wf_6c09513e, beyond .seeds) RED-reproduced a compound fail-OPEN in the
SECONDARY kill-switch rail (distinct from, and independent of, the ADR-0004 deterministic gate):

  compute_cumulative_realized_pnl_pct() returned 0.0 (== "no breach" at autonomous.py:446) on ANY
  compute error — e.g. the FIFO matcher join_exit_fills crashing mid-book — so a catastrophic
  realized-loss book PLUS a transient parse/matcher fault silently disarmed the rail.

Money-software fix (last-known-good + degraded-audit, healthy path byte-identical):
  - On a SUCCESSFUL compute, persist the value to a last-known sidecar.
  - On a compute error, return the LAST-KNOWN value (conservative: carries the most recent real
    loss forward so a losing book that briefly fails to parse stays tripped), NOT 0.0.
  - Emit a `state_reconstruction_failed` governance audit event whenever the rail goes blind, so
    an operator sees the secondary rail degraded.
  - Cold start (no last-known) still returns 0.0 — we cannot fabricate a loss, and the deterministic
    gate (independent, fail-CLOSED) remains the final authority.

Offline-deterministic: synthetic executions.jsonl in tmp_path, NAV + paths stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_quant.autonomous as autonomous
from hermes_quant.governance import audit_log


@pytest.fixture
def killswitch_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    qh = tmp_path / "quant"
    qh.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(autonomous, "QUANT_HOME", qh)
    monkeypatch.setattr(autonomous, "KILL_SWITCH_PATH", qh / "autonomous_kill_switch.json")
    # Last-known sidecar lives under QUANT_HOME; route the audit log to tmp too.
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", tmp_path / "governance" / "audit_log.jsonl")
    monkeypatch.setattr(autonomous, "_account_nav_usd", lambda: 100_000.0)
    return qh


def _losing_book(path: Path) -> None:
    """A book whose ONE round-trip realizes a catastrophic loss: open long (+10 delta) @100,
    close (-10 delta, an offsetting sell) @50 = -50% realized return on the entry notional.
    (Delta semantics — production default HERMES_QUANT_DELTA_NORMALIZER OFF — so the close is
    an explicit offsetting -10 fill, not a flatten-to-0 target.)"""
    recs = [
        {"proposal_id": "open", "signal_id": "s", "asset": "AAPL", "asset_class": "equity",
         "asof_execution": "2026-06-14T09:00:00Z", "fill_price": 100.0, "fill_size_pct": 10.0,
         "account_id": "paper-default"},
        {"proposal_id": "close", "signal_id": "s", "asset": "AAPL", "asset_class": "equity",
         "asof_execution": "2026-06-14T11:00:00Z", "fill_price": 50.0, "fill_size_pct": -10.0,
         "account_id": "paper-default"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_healthy_compute_persists_last_known_and_is_byte_identical(killswitch_paths: Path) -> None:
    """The healthy path returns the real loss AND persists it to the last-known sidecar
    (the persist is additive; the returned value is unchanged from pre-ar02)."""
    execs = killswitch_paths / "executions.jsonl"
    _losing_book(execs)
    frac = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert frac < 0.0, "a losing book must report a negative realized fraction"
    # The last-known sidecar now exists and records that loss.
    sidecar = killswitch_paths / "autonomous_cum_pnl_last_known.json"
    assert sidecar.exists(), "a successful compute must persist the last-known value"
    saved = json.loads(sidecar.read_text())
    assert saved["cum_pnl_pct"] == pytest.approx(frac)


def test_matcher_crash_returns_last_known_loss_not_zero(
    killswitch_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE FIX (was the fail-OPEN): a compute error AFTER a prior successful losing compute must
    return the LAST-KNOWN loss, not 0.0 — so the rail does not silently disarm on a transient fault."""
    execs = killswitch_paths / "executions.jsonl"
    _losing_book(execs)
    # 1) Healthy compute persists the loss.
    healthy = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert healthy < 0.0
    # 2) Now the FIFO matcher faults mid-book.
    import hermes_quant.daemon.settlement_loop as sl

    def _boom(records):  # noqa: ANN001
        raise RuntimeError("matcher fault mid-book")

    monkeypatch.setattr(sl, "join_exit_fills", _boom)
    degraded = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert degraded == pytest.approx(healthy), (
        "on a compute error with a prior losing last-known, the rail must carry the loss "
        "forward (conservative), NOT return 0.0 (the fail-OPEN)"
    )
    assert degraded < 0.0


def test_compute_error_emits_degraded_audit_event(
    killswitch_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the rail goes blind (compute error), an operator-visible audit event is emitted."""
    execs = killswitch_paths / "executions.jsonl"
    _losing_book(execs)
    autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)  # seed last-known
    import hermes_quant.daemon.settlement_loop as sl
    monkeypatch.setattr(sl, "join_exit_fills", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    events = [e for e in audit_log.read() if e.kind == "state_reconstruction_failed"]
    assert events, "a blind kill-switch rail must emit a state_reconstruction_failed audit event"
    assert any("cumulative" in str(e.payload).lower() or "kill" in str(e.payload).lower()
               for e in events)


def test_cold_start_compute_error_returns_zero(
    killswitch_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With NO last-known sidecar (cold start), a compute error still returns 0.0 — we cannot
    fabricate a loss, and the deterministic gate (independent, fail-CLOSED) is the final authority."""
    execs = killswitch_paths / "executions.jsonl"
    _losing_book(execs)
    import hermes_quant.daemon.settlement_loop as sl
    monkeypatch.setattr(sl, "join_exit_fills", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    # No prior successful compute => no sidecar.
    frac = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert frac == 0.0
