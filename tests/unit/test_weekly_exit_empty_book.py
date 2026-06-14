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


def test_live_record_without_account_id_now_reconstructs(tmp_path: Path) -> None:
    """cs14 FIX (was HOLE-0): the real record lacks a top-level account_id key, but
    the absolute-target path resolves the account the SAME way the producer's
    state-write seam does (reactor_metadata.account_id or the "paper-default"
    sentinel), so a record with no account_id now reconstructs a real position.

    cs24 UPDATE: this record (reactor "paper", no account_id) resolves to the
    "paper-default" partition. The loader is now account-EQUALITY (not the prior
    set-OR over {account_id, "paper-default"}), so it reconstructs under a
    PAPER-DEFAULT request — the account the weekly actually manages — NOT under an
    "alpaca-paper" request (the separate SHADOW partition). The prior assertion
    requested "alpaca-paper" and passed only because the set-OR pooled the
    paper-default book into the alpaca request; that pooling was the cs24 bug.

    NAV-fraction fallback derivation (reactor_metadata is empty here, so no
    authoritative quantity): qty = target_position_pct * NAV / entry_price
    = 0.20 * 100_000 / 200 = 100.0 long shares, entry/mark = 200.0.
    """
    d = _live_record_dict()
    bus = _write_bus(tmp_path, d)

    # cs24: request the REAL managed book ("paper-default"), not the shadow.
    pf = reconstruct_portfolio("paper-default", ASSET_CLASS, bus_path=bus)

    assert len(pf.positions) == 1
    assert "AAPL" in pf.positions
    pos = pf.positions["AAPL"]
    assert pos.qty == pytest.approx(100.0)  # 0.20 * 100_000 / 200
    assert pos.qty > 0  # long
    assert pos.avg_entry_price == pytest.approx(200.0)
    assert pos.mark_price == pytest.approx(200.0)

    # cs24 anti-pooling guard: the SAME paper-default record must NOT pool into a
    # request for the separate "alpaca-paper" SHADOW partition.
    pf_shadow = reconstruct_portfolio("alpaca-paper", ASSET_CLASS, bus_path=bus)
    assert pf_shadow.positions == {}


def test_live_record_reconstructs_legacy_int1_still_needs_side_qty(tmp_path: Path) -> None:
    """cs14 FIX (was HOLE-1 + HOLE-2): the live shape (schema_version None) is an
    absolute-target record and now reconstructs a position. But the LEGACY int-1
    path is byte-identical — a record stamped schema_version=1 with NO side/qty is
    still genuinely malformed and is skipped (it is NOT an absolute-target record,
    so the new path never sees it, and the legacy loop KeyErrors and continues).

    This pins BOTH halves: the new absolute-target reconstruction (HOLE-1 fixed)
    AND the retained legacy int-1 strictness (a hand-rolled int-1 record without
    side/qty is malformed -> empty book; HOLE-2 legacy-strict regression guard).
    """
    d = _live_record_dict()
    d["account_id"] = ACCOUNT_ID  # legacy-injected account_id is also honored
    bus = _write_bus(tmp_path, d)

    pf = reconstruct_portfolio(ACCOUNT_ID, ASSET_CLASS, bus_path=bus)
    # HOLE-1 fixed: live shape (schema_version None) now reconstructs.
    assert len(pf.positions) == 1
    assert "AAPL" in pf.positions

    # Legacy int-1 strictness retained: stamp schema_version=1 (an int, NOT the
    # absolute-target sentinel) so the new path skips it; the legacy loop then
    # KeyErrors on the absent side/qty and the book is empty.
    d["schema_version"] = 1
    bus2 = _write_bus(tmp_path, d)
    pf2 = reconstruct_portfolio(ACCOUNT_ID, ASSET_CLASS, bus_path=bus2)
    assert pf2.positions == {}  # legacy int-1 without side/qty is malformed -> skipped


def test_green_live_record_reconstructs_one_position(tmp_path: Path) -> None:
    """cs14 GREEN: a live-producer ExecutionRecord reconstructs exactly one position.

    Feed the SAME real producer record (ExecutionRecord -> _record_to_dict, with
    account_id injected) and the loader's absolute-target path reconstructs exactly
    one AAPL position. This was a strict-xfail tripwire under the RED proof; the
    cs14 Option-B loader fix flipped it to a real PASS and the @pytest.mark.xfail
    marker was removed (strict-xfail would FAIL on XPASS otherwise).
    """
    d = _live_record_dict()
    d["account_id"] = ACCOUNT_ID
    bus = _write_bus(tmp_path, d)

    pf = reconstruct_portfolio(ACCOUNT_ID, ASSET_CLASS, bus_path=bus)

    assert len(pf.positions) == 1
    assert "AAPL" in pf.positions
