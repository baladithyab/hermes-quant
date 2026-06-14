"""cs14 RED proof — the weekly-exit reader empty-book hole.

INVESTIGATE-THEN-RECOMMEND increment (cs14). This file ships the RED proof ONLY;
it touches NO live-path .py. The actual fix lands in a LATER operator-approved
increment (see docs/design/2026-06-13-cs14-weekly-exit-reader-fork.md).

The hole
--------
The live execution producer is `react.paper.PaperReactor`, which serializes an
`react.base.ExecutionRecord` via `react.paper._record_to_dict`. That real shape
carries:
  * ``schema_version = None``           (react/base.py:89 — ``str | None``, never int 1)
  * ``target_position_pct`` (a signed NAV fraction)
  * NO ``qty`` / NO ``side`` / NO ``account_id`` keys (react/paper.py:56-80)

But the LIVE consumer of the book — ``daemon.portfolio_loader.reconstruct_portfolio``,
called by the enabled weekly cron at scripts/quant-playbook-weekly.py:296 — filters:
  * portfolio_loader.py:74  ``r.get("account_id") == account_id``   (HOLE-0)
  * portfolio_loader.py:76  ``r.get("schema_version") == 1``  (int 1)  (HOLE-1)
and then in the loop reads ``rec["side"]`` / ``float(rec["qty"])``
  * portfolio_loader.py:91-92                                          (HOLE-2)

Every live record fails the filter at HOLE-0 (no account_id key) and, even past
that, at HOLE-1 (schema_version is None, not int 1); and even past that, the loop
KeyErrors on ``rec["side"]``/``rec["qty"]`` and ``continue``s (HOLE-2). Net: a real
fill reconstructs to an EMPTY ``pf.positions`` -> the weekly early-returns
``weekly_empty_portfolio`` (quant-playbook-weekly.py:397) -> the -25% LEAPS
drawdown close, the >60d/loss swing stop, and the 3xATR take-profit NEVER run on a
real book.

Why the suite never caught it
-----------------------------
The existing loader tests (tests/unit/test_daemon_lock_discovery.py:111-124,
``_exec``) HAND-ROLL ``{schema_version: 1, side, qty, account_id, asof, ...}`` — a
shape NO live producer emits. They test the loader against a fiction, so the
producer/consumer divergence was invisible. This test instead feeds a record built
by the REAL producer dataclass and serialized by the REAL serializer.

Tests (1) and (2) PASS today: they pin the current broken empty-book behavior as a
characterization (RED-as-documentation). Test (3) is a strict-xfail tripwire: it
asserts the CORRECT behavior (one reconstructed position) that fails today and that
the later fix must flip to PASS — at which point strict-xfail turns the now-passing
test into an XPASS *failure*, forcing the implementer to drop the marker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.daemon.portfolio_loader import reconstruct_portfolio
from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import _record_to_dict

# An account_id the loader filter is told to match. The live record will NOT carry
# this key (HOLE-0) until we inject it to isolate the deeper holes.
ACCOUNT_ID = "alpaca-paper"
ASSET_CLASS = "equity"


def _live_record_dict() -> dict:
    """A record produced by the REAL chain: ExecutionRecord -> _record_to_dict.

    play_tag / schema_version left at their dataclass defaults ("advisor" / None),
    which is exactly the legacy live shape (all 46 records on the live bus
    ~/.hermes/quant/executions.jsonl read schema_version=None and carry no
    side/qty/account_id).
    """
    rec = ExecutionRecord(
        proposal_id="prop-cs14",
        signal_id="sig-swing-AAPL-cs14",
        asset="AAPL",
        asset_class="equity",
        timeframe="1d",
        asof_decision="2026-06-08T13:31:00+00:00",
        asof_execution="2026-06-08T13:31:05+00:00",
        target_position_pct=0.20,  # +20% NAV long; a real fill
        decision_price=200.0,
        fill_price=200.0,
        fill_size_pct=0.20,
        reactor_name="paper",
        human_in_the_loop=False,
    )
    return _record_to_dict(rec)


def _write_bus(tmp_path: Path, record: dict) -> Path:
    bus = tmp_path / "executions.jsonl"
    bus.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return bus


def test_live_record_dict_has_divergent_shape() -> None:
    """Pin the producer/consumer shape divergence at the source.

    Guards the premise of the whole hole: the REAL serializer emits no
    account_id / side / qty keys and schema_version is None (not int 1).
    """
    d = _live_record_dict()
    assert "account_id" not in d  # HOLE-0 premise
    assert d.get("schema_version") is None  # HOLE-1 premise (None, not int 1)
    assert "side" not in d  # HOLE-2 premise
    assert "qty" not in d  # HOLE-2 premise
    assert d.get("target_position_pct") == 0.20  # the fraction the loader ignores


def test_live_record_account_id_absent_drops_at_filter(tmp_path: Path) -> None:
    """HOLE-0: the real record lacks an account_id key, so the loader's
    ``r.get("account_id") == account_id`` filter (portfolio_loader.py:74) drops it
    (None != "alpaca-paper"). pf.positions is empty.

    PASSES today — characterization of the current broken behavior.
    """
    d = _live_record_dict()
    bus = _write_bus(tmp_path, d)

    pf = reconstruct_portfolio(ACCOUNT_ID, ASSET_CLASS, bus_path=bus)

    assert pf.positions == {}


def test_live_record_shape_drops_at_schema_and_qty(tmp_path: Path) -> None:
    """HOLE-1 + HOLE-2: inject account_id to isolate past HOLE-0, then show the
    record STILL drops out.

    * schema_version is None != int 1 -> dropped at the filter (portfolio_loader.py:76).
    * even if we then also set schema_version=1, the loop reads rec["side"]/
      float(rec["qty"]) (portfolio_loader.py:91-92), KeyErrors, and ``continue``s.

    PASSES today — characterization of the current broken behavior.
    """
    d = _live_record_dict()
    d["account_id"] = ACCOUNT_ID  # isolate past HOLE-0
    bus = _write_bus(tmp_path, d)

    pf = reconstruct_portfolio(ACCOUNT_ID, ASSET_CLASS, bus_path=bus)
    assert pf.positions == {}  # HOLE-1: schema_version None != 1 -> filtered out

    # Sub-assert HOLE-2: pass the schema filter, the loop still cannot read the record.
    d["schema_version"] = 1
    bus2 = _write_bus(tmp_path, d)
    pf2 = reconstruct_portfolio(ACCOUNT_ID, ASSET_CLASS, bus_path=bus2)
    assert pf2.positions == {}  # HOLE-2: rec["side"]/rec["qty"] KeyError -> skipped


@pytest.mark.xfail(
    strict=True,
    reason=(
        "cs14: a live-producer ExecutionRecord must reconstruct to 1 position; the "
        "fix lands in a later operator-approved increment. When it lands, this test "
        "XPASSes and strict-xfail FAILS, forcing removal of this marker."
    ),
)
def test_green_live_record_reconstructs_one_position(tmp_path: Path) -> None:
    """The CORRECT behavior the deferred fix must deliver.

    Feed the SAME real producer record (with account_id injected so HOLE-0 is not
    the blocker under study) and expect the loader to reconstruct exactly one
    AAPL position. Fails today (-> recorded XFAIL, suite stays green); the later
    fix flips it to a real PASS.
    """
    d = _live_record_dict()
    d["account_id"] = ACCOUNT_ID
    bus = _write_bus(tmp_path, d)

    pf = reconstruct_portfolio(ACCOUNT_ID, ASSET_CLASS, bus_path=bus)

    assert len(pf.positions) == 1
    assert "AAPL" in pf.positions
