"""ar92 — reconstruct_portfolio_state must SKIP a no-fill record (no phantom position).

The LIVE DeterministicEquityReactor (HERMES_QUANT_DETERMINISTIC_EQUITY=1) appends a
NO-FILL ExecutionRecord on a bp_rejected / backend_unavailable: it carries the REQUESTED
target_position_pct (non-zero) but fill_price=0.0 / fill_size_pct=0.0 and
reactor_metadata.no_fill=True, and deliberately does NOT reconcile state.db (the
authoritative ledger correctly shows no position).

But `reconstruct_portfolio_state` (the LATEST-TARGET bus replay) keyed the held position
purely off target_position_pct with no fill guard. So when a no-fill was the latest
record for a symbol it conjured a PHANTOM position that never opened. This is reachable
on the LIVE autonomous portfolio-caps path (autonomous.py calls
`reconstruct_portfolio_state(reactor_filter=None)` to count the whole book), where the
phantom inflates the headroom charge → real picks are wrongly shrunk/silenced, and the
weekly playbook can fire a spurious armed CLOSE against the phantom (a real unintended
short).

FIX (ar92): skip a no-fill record — discriminated by reactor_metadata.no_fill==True,
with fill_price==0.0 AND fill_size_pct==0.0 as the corroborating fallback. A legitimate
flatten-to-zero (target 0 with a REAL fill_price) is NOT a no-fill and is preserved.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.portfolio.state import reconstruct_portfolio_state


def _write(bus: Path, records: list[dict]) -> None:
    bus.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _nofill(asset: str, ts: str, target: float = 0.05) -> dict:
    return {
        "asset": asset, "asset_class": "equity", "timeframe": "1d",
        "target_position_pct": target, "asof_execution": ts,
        "fill_price": 0.0, "fill_size_pct": 0.0,
        "reactor_name": "deterministic-equity",
        "reactor_metadata": {"no_fill": True, "bp_rejected": True, "account_id": "paper-default"},
    }


def _real_fill(asset: str, ts: str, target: float, price: float) -> dict:
    return {
        "asset": asset, "asset_class": "equity", "timeframe": "1d",
        "target_position_pct": target, "asof_execution": ts,
        "fill_price": price, "fill_size_pct": target,
        "reactor_name": "deterministic-equity",
        "reactor_metadata": {"account_id": "paper-default"},
    }


def test_ar92_nofill_record_creates_no_phantom_position(tmp_path):
    """THE LEAK: a bp_rejected no-fill as the latest record must NOT define a position
    on the reactor_filter=None autonomous portfolio-caps path."""
    bus = tmp_path / "executions.jsonl"
    _write(bus, [_nofill("NVDA", "2026-05-13T20:00:00Z", target=0.05)])
    ps = reconstruct_portfolio_state(bus, reactor_filter=None)
    assert "NVDA" not in ps.positions, (
        f"phantom position from a no-fill record: {ps.positions} — a bp_rejected fill "
        "moved no position and must not appear in the reconstructed book"
    )


def test_ar92_nofill_after_real_fill_does_not_resurrect_or_phantom(tmp_path):
    """A real fill opens AAPL; a later no-fill (different symbol) must not affect it,
    and the no-fill symbol must stay absent."""
    bus = tmp_path / "executions.jsonl"
    _write(bus, [
        _real_fill("AAPL", "2026-05-13T20:00:00Z", 0.05, 200.0),
        _nofill("NVDA", "2026-05-13T21:00:00Z", target=0.08),
    ])
    ps = reconstruct_portfolio_state(bus, reactor_filter=None)
    assert ps.positions.get("AAPL") == 0.05, "real AAPL fill must be preserved"
    assert "NVDA" not in ps.positions, "no-fill NVDA must not phantom"


def test_ar92_real_fill_preserved_nonvacuous(tmp_path):
    """Non-vacuity: a REAL fill (fill_price>0, fill_size_pct>0) still produces a
    position — the guard targets only no-fills, not all records."""
    bus = tmp_path / "executions.jsonl"
    _write(bus, [_real_fill("MSFT", "2026-05-13T20:00:00Z", 0.05, 400.0)])
    ps = reconstruct_portfolio_state(bus, reactor_filter=None)
    assert ps.positions.get("MSFT") == 0.05


def test_ar92_flatten_to_zero_with_real_fill_is_not_a_nofill(tmp_path):
    """A legitimate flatten-to-zero (target 0 with a REAL fill_price) is NOT a no-fill:
    it correctly closes the position (zero target => absent under drop_zeros), and is
    NOT skipped as if it never happened. We assert the close wins over the prior open
    (the symbol ends flat), distinguishing it from the no-fill skip."""
    bus = tmp_path / "executions.jsonl"
    _write(bus, [
        _real_fill("TSLA", "2026-05-13T20:00:00Z", 0.05, 250.0),   # open
        # flatten: target 0 but a REAL fill_price (the close executed) — fill_size_pct
        # is the close delta magnitude, NOT 0; this is NOT a no-fill.
        {
            "asset": "TSLA", "asset_class": "equity", "timeframe": "1d",
            "target_position_pct": 0.0, "asof_execution": "2026-05-13T21:00:00Z",
            "fill_price": 240.0, "fill_size_pct": 0.05,
            "reactor_name": "deterministic-equity",
            "reactor_metadata": {"account_id": "paper-default"},
        },
    ])
    ps = reconstruct_portfolio_state(bus, reactor_filter=None)
    # The flatten close was honored (TSLA flat => absent under drop_zeros default).
    assert ps.positions.get("TSLA", 0.0) == 0.0, (
        "a real flatten-to-zero close must be honored (TSLA flat), NOT skipped as a no-fill"
    )
