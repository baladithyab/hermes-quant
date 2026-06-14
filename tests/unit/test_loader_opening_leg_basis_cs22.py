"""cs22 RED->GREEN — the loader anchored avg_entry_price on the LATEST add, not the opener.

THE BUG
-------
``daemon.portfolio_loader.reconstruct_portfolio`` (absolute-target path) keeps the ONE
record with MAX ``asof_execution`` per symbol (``abs_latest``) for QTY/sign — correct,
since the live producer writes an absolute signed NAV-fraction TARGET per fill. But it
ALSO derived ``entry_price`` (-> ``Position.avg_entry_price``) from that SAME latest
record. For a MULTI-FILL same-sign position (open @P0, add @P1 later) the cost basis
became P1 (the add), not P0 (the opener).

That basis drives the weekly readers' sign-aware pnl_pct + drawdown
(scripts/quant-playbook-weekly.py compute_pnl_drawdown), so a routine size-up silently
re-anchored the LEAPS -25% drawdown stop and the swing take-profit/stop onto the wrong
price.

THE FIX (cs22)
--------------
The BASIS now anchors on the OPENING (establishing) leg — the first same-held-sign fill
of the current run, after the last flat/flip — the loader analogue of the weekly's
``_establishing_leg`` (cs27/cs28). QTY/sign stay latest-target. A single-fill position
is byte-identical (the opening leg IS the latest record).
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.daemon.portfolio_loader import reconstruct_portfolio
from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import _record_to_dict

ASSET_CLASS = "equity"


def _fill(asset: str, pct: float, *, fill_price: float, ts: str) -> dict:
    """A paper-default absolute-target fill (no account_id -> paper-default)."""
    return _record_to_dict(
        ExecutionRecord(
            proposal_id=f"prop-{asset}-{ts}",
            signal_id=f"sig-{asset}",
            asset=asset,
            asset_class=ASSET_CLASS,
            timeframe="1d",
            asof_decision=ts,
            asof_execution=ts,
            target_position_pct=pct,
            decision_price=fill_price,
            fill_price=fill_price,
            fill_size_pct=pct,
            reactor_name="paper",
            human_in_the_loop=False,
        )
    )


def _write_bus(tmp_path: Path, recs: list[dict]) -> Path:
    bus = tmp_path / "executions.jsonl"
    bus.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return bus


def test_multifill_short_add_anchors_on_opening_leg(tmp_path: Path) -> None:
    """cs22 GREEN: AVGO opens short -0.10 @200, then a same-sign ADD -0.20 @250.

    avg_entry_price must be the OPENING leg (200.0), NOT the latest add (250.0).
    qty/sign still come from the -0.20 latest target (qty<0).
    RED-on-current: the loader returned 250.0 (the latest add's fill_price).
    """
    bus = _write_bus(
        tmp_path,
        [
            _fill("AVGO", -0.10, fill_price=200.0, ts="2026-06-08T13:31:05+00:00"),
            _fill("AVGO", -0.20, fill_price=250.0, ts="2026-06-08T13:31:06+00:00"),
        ],
    )
    pf = reconstruct_portfolio("paper-default", ASSET_CLASS, bus_path=bus)
    assert "AVGO" in pf.positions
    pos = pf.positions["AVGO"]
    assert pos.avg_entry_price == 200.0  # opening leg, NOT the 250 add
    assert pos.qty < 0  # still a short (latest target -0.20)


def test_single_fill_long_byte_identical(tmp_path: Path) -> None:
    """cs22: a single-fill LONG (+0.20 @200) — opening leg IS the latest record, so
    avg_entry_price is unchanged (byte-identical to the pre-cs22 loader)."""
    bus = _write_bus(
        tmp_path,
        [_fill("AAPL", 0.20, fill_price=200.0, ts="2026-06-08T13:31:05+00:00")],
    )
    pf = reconstruct_portfolio("paper-default", ASSET_CLASS, bus_path=bus)
    assert pf.positions["AAPL"].avg_entry_price == 200.0
    assert pf.positions["AAPL"].qty > 0


def test_single_fill_short_byte_identical(tmp_path: Path) -> None:
    """cs22: a single-fill SHORT (-0.20 @200) — opening leg IS the latest record, so
    avg_entry_price is unchanged (byte-identical)."""
    bus = _write_bus(
        tmp_path,
        [_fill("BA", -0.20, fill_price=200.0, ts="2026-06-08T13:31:05+00:00")],
    )
    pf = reconstruct_portfolio("paper-default", ASSET_CLASS, bus_path=bus)
    assert pf.positions["BA"].avg_entry_price == 200.0
    assert pf.positions["BA"].qty < 0


def test_reopen_across_flat_anchors_on_post_flat_reopen(tmp_path: Path) -> None:
    """cs22: short -0.20 @200, flat 0.0 @250 (close), short -0.20 @300 (re-open).

    The boundary (flat) resets the run, so the establishing leg is the POST-flat
    re-open (300.0), proving the boundary reset rather than the original opener (200)."""
    bus = _write_bus(
        tmp_path,
        [
            _fill("NVDA", -0.20, fill_price=200.0, ts="2026-06-08T13:31:05+00:00"),
            _fill("NVDA", 0.0, fill_price=250.0, ts="2026-06-08T13:31:06+00:00"),
            _fill("NVDA", -0.20, fill_price=300.0, ts="2026-06-08T13:31:07+00:00"),
        ],
    )
    pf = reconstruct_portfolio("paper-default", ASSET_CLASS, bus_path=bus)
    assert "NVDA" in pf.positions
    assert pf.positions["NVDA"].avg_entry_price == 300.0  # post-flat re-open
    assert pf.positions["NVDA"].qty < 0
