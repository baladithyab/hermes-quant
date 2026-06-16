"""ar39 — the LIVE ADR-0016 §D9 realized-drawdown kill-switch basis must not read the
WHOLE never-rotated executions.jsonl into memory + re-run the O(n) FIFO matcher over the
ENTIRE lifetime bus on EVERY autonomous tick.

The defect (perf/availability fail-OPEN on the always-on rail):
  ``compute_cumulative_realized_pnl_pct()`` (called every ``quant-autonomous-tick-30min``)
  did ``path.read_text().splitlines()`` + ``join_exit_fills(all_records)`` over the append-only,
  never-rotated bus (signal_bus.py: bus files are never rotated in v0.1). Per-tick RSS + CPU
  grow without bound, eventually slowing the kill-switch computation past the tick deadline /
  risking OOM — degrading the secondary rail (fail-OPEN).

The fix (incremental settlement with a persisted checkpoint):
  Persist a durable sidecar (atomic tmp+rename+fsync, mirror _persist_last_known_cum_pnl)
  holding the consumed bus byte-offset + line count, the carry-in ``open_lots`` from
  ``join_exit_fills``, the accumulated cumulative realized-P&L fraction from already-settled
  (evicted) round-trips, and the file inode + max-asof boundary. Each call reads ONLY the bytes
  PAST the offset, runs ``join_exit_fills(new_records, open_lots=checkpoint_open_lots)``, adds
  the new round-trips' contribution, advances the checkpoint, and returns the total.

CORRECTNESS IS PARAMOUNT (a wrong kill-switch basis is worse than a slow one). The incremental
result MUST EQUAL the full-replay result for ANY bus — including a position OPENED in an early
batch and CLOSED in a later batch (cross-checkpoint pairing — the carry-in open_lots is exactly
what prevents a naive tail-slice from mis-pairing it). On a missing/corrupt checkpoint OR a
bus that shrank/was rotated (offset > file size, different inode), FALL BACK to a full replay
(fail-safe) and rebuild the checkpoint. Preserves ar25 NAV-fraction weighting, ar34 paper-default
filter, ar02 degraded fail-closed (full-replay byte-identical to pre-fix on the same bus).

Offline-deterministic: synthetic executions.jsonl in tmp_path, NAV + paths stubbed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import hermes_quant.autonomous as autonomous
from hermes_quant.daemon.settlement_loop import join_exit_fills


@pytest.fixture
def ks_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    qh = tmp_path / "quant"
    qh.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(autonomous, "QUANT_HOME", qh)
    monkeypatch.setattr(autonomous, "_account_nav_usd", lambda: 100_000.0)
    return qh


def _fill(asset: str, pct: float, price: float, asof: str, pid: str) -> dict:
    """A paper-default single-leg fill in DELTA semantics (production default
    HERMES_QUANT_DELTA_NORMALIZER OFF): qty is the signed NAV-fraction delta."""
    return {
        "proposal_id": pid,
        "signal_id": "s",
        "asset": asset,
        "asset_class": "equity",
        "asof_execution": asof,
        "fill_price": price,
        "fill_size_pct": pct,
        "account_id": "paper-default",
    }


def _append(path: Path, recs: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _full_replay(path: Path) -> float:
    """The pre-fix FULL-REPLAY reference: read whole bus, match once, sum.

    Replicated here (NOT calling the function under test) so the equality test is a true
    cross-check, not a tautology — even after the function under test goes incremental."""
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    round_trips, _open = join_exit_fills(records)
    frac = 0.0
    for rt in round_trips:
        if getattr(rt, "account_id", "paper-default") != "paper-default":
            continue
        term = rt.realized_return * rt.qty
        if not math.isfinite(term):
            continue
        frac += term
    return frac


# --------------------------------------------------------------------------- #
# The keystone: incremental == full replay across appends, INCLUDING a position
# opened in batch 1 and closed in batch 3 (cross-checkpoint pairing).
# --------------------------------------------------------------------------- #
def test_incremental_equals_full_replay_across_appends_cross_checkpoint(ks_home: Path) -> None:
    execs = ks_home / "executions.jsonl"

    # Batch 1: open AAPL long (+10% NAV) @100; open MSFT long (+5%) @200.
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "a-open"),
        _fill("MSFT", 5.0, 200.0, "2026-06-14T09:05:00Z", "m-open"),
    ])
    inc1 = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert inc1 == pytest.approx(_full_replay(execs)), (
        "batch 1 (only opens, no realized round-trips yet): incremental must equal full replay"
    )

    # Batch 2: close MSFT (-5%) @180 = a realized loss; open NVDA (+8%) @50.
    _append(execs, [
        _fill("MSFT", -5.0, 180.0, "2026-06-14T11:00:00Z", "m-close"),
        _fill("NVDA", 8.0, 50.0, "2026-06-14T11:05:00Z", "n-open"),
    ])
    inc2 = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    full2 = _full_replay(execs)
    assert inc2 == pytest.approx(full2), (
        "batch 2 (MSFT round-trip settles): incremental must equal full replay"
    )
    assert inc2 < inc1 or inc2 < 0.0, "the MSFT loss must move the fraction negative"

    # Batch 3: CLOSE AAPL (-10%) @50 — AAPL was OPENED in batch 1 (cross-checkpoint pairing).
    # A naive tail-slice that dropped the batch-1 AAPL open would mis-pair this close.
    _append(execs, [
        _fill("AAPL", -10.0, 50.0, "2026-06-14T13:00:00Z", "a-close"),
    ])
    inc3 = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    full3 = _full_replay(execs)
    assert inc3 == pytest.approx(full3), (
        "batch 3 closes a position OPENED in batch 1 — cross-checkpoint FIFO pairing. The "
        "carried-in open_lots is exactly what makes incremental == full here; a tail-slice fails."
    )
    # AAPL -50% on a 10%-NAV position = -0.05; this must be reflected.
    assert inc3 == pytest.approx(full3) and inc3 < inc2, (
        "the AAPL -50% realized loss (cross-checkpoint) must further depress the fraction"
    )


def test_incremental_byte_identical_final_fraction_vs_prefix_full_replay(ks_home: Path) -> None:
    """The FINAL incremental fraction on a full open+close bus must be byte-identical to the
    pre-fix full replay on the same bus (no drift from the checkpoint/serialization)."""
    execs = ks_home / "executions.jsonl"
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "open"),
        _fill("AAPL", -10.0, 50.0, "2026-06-14T11:00:00Z", "close"),
    ])
    # Drive several incremental ticks (no new fills between them) — the value must be stable
    # and equal to the full replay.
    inc_a = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    inc_b = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    full = _full_replay(execs)
    assert inc_a == full, "incremental final fraction must be byte-identical to full replay"
    assert inc_b == inc_a, "re-ticking with no new fills must return the identical cached fraction"


def test_first_call_no_checkpoint_does_full_replay(ks_home: Path) -> None:
    """First call (no checkpoint sidecar) must full-replay from scratch and match."""
    execs = ks_home / "executions.jsonl"
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "open"),
        _fill("AAPL", -10.0, 50.0, "2026-06-14T11:00:00Z", "close"),
    ])
    # No checkpoint exists yet.
    frac = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert frac == _full_replay(execs)


def test_corrupt_checkpoint_falls_back_to_full_replay(ks_home: Path) -> None:
    """A corrupt checkpoint sidecar must be ignored and a full replay done (fail-safe)."""
    execs = ks_home / "executions.jsonl"
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "open"),
        _fill("AAPL", -10.0, 50.0, "2026-06-14T11:00:00Z", "close"),
    ])
    # Seed a checkpoint via a healthy compute.
    good = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    ckpt = autonomous._incremental_checkpoint_path(execs)
    assert ckpt.exists(), "a successful compute must persist the incremental checkpoint"
    # Corrupt it.
    ckpt.write_text("{ this is not valid json", encoding="utf-8")
    recovered = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert recovered == pytest.approx(good) == pytest.approx(_full_replay(execs)), (
        "a corrupt checkpoint must fall back to a full replay and match"
    )


def test_missing_checkpoint_after_seed_falls_back_to_full_replay(ks_home: Path) -> None:
    execs = ks_home / "executions.jsonl"
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "open"),
        _fill("AAPL", -10.0, 50.0, "2026-06-14T11:00:00Z", "close"),
    ])
    good = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    ckpt = autonomous._incremental_checkpoint_path(execs)
    ckpt.unlink()
    recovered = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert recovered == pytest.approx(good)


def test_bus_shrank_or_rotated_falls_back_to_full_replay(ks_home: Path) -> None:
    """If the bus shrank (offset > current file size) or was rotated (different inode), the
    checkpoint is stale; the rail must full-replay the now-current bus and match it."""
    execs = ks_home / "executions.jsonl"
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "open"),
        _fill("AAPL", -10.0, 50.0, "2026-06-14T11:00:00Z", "close"),
    ])
    autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)  # seed checkpoint
    # Rotate: replace the bus with a SMALLER, different bus (offset now > file size).
    execs.unlink()
    _append(execs, [
        _fill("TSLA", 4.0, 10.0, "2026-06-15T09:00:00Z", "t-open"),
        _fill("TSLA", -4.0, 5.0, "2026-06-15T11:00:00Z", "t-close"),
    ])
    recovered = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert recovered == pytest.approx(_full_replay(execs)), (
        "after a shrink/rotation the checkpoint offset is stale; full-replay the current bus"
    )


def test_normalizer_flag_flip_invalidates_checkpoint_and_replays(
    ks_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HERMES_QUANT_DELTA_NORMALIZER changes the matcher's reading of the SAME bytes
    (ADR-0091 item 11). A checkpoint built under one flag state must be INVALIDATED under
    the other — the second call must full-replay under the new flag, not return the cached
    fraction. Guards the incremental fast-path from silently freezing the flag-OFF basis."""
    execs = ks_home / "executions.jsonl"
    # Open then RE-AFFIRM (a same-target re-post) then flatten — the normalizer collapses
    # the re-affirmation; legacy reads it as a second delta. Same bytes, different basis.
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "open"),
        _fill("AAPL", 10.0, 105.0, "2026-06-14T10:00:00Z", "reaffirm"),
        _fill("AAPL", 0.0, 90.0, "2026-06-14T11:00:00Z", "flatten"),
    ])
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    frac_off = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    frac_on = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert frac_on == pytest.approx(_full_replay(execs)), (
        "flipping the normalizer flag must invalidate the checkpoint and full-replay under "
        "the new flag (the cached flag-OFF fraction must NOT be returned)"
    )
    assert frac_off != frac_on, "the flag flip must change the live kill-switch basis"


def test_partial_trailing_line_not_consumed_then_settles_when_complete(ks_home: Path) -> None:
    """A concurrent settlement writer may leave a PARTIAL trailing line (no newline yet).
    The incremental read must NOT consume it (offset stops at the last newline); once the
    line completes the next tick settles it. Mirrors signal_bus.tail's partial-line handling."""
    execs = ks_home / "executions.jsonl"
    _append(execs, [_fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "open")])
    autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)  # seed checkpoint
    # Write a PARTIAL close line (no trailing newline — an in-flight write).
    close = _fill("AAPL", -10.0, 50.0, "2026-06-14T11:00:00Z", "close")
    partial = json.dumps(close)[:-5]  # truncated, no "}" and no "\n"
    with open(execs, "a", encoding="utf-8") as f:
        f.write(partial)
    mid = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert mid == pytest.approx(0.0), "a partial trailing line must not be consumed (no round-trip)"
    # Now complete the line (overwrite the partial with the full record + newline).
    text = execs.read_text(encoding="utf-8")
    text = text[: len(text) - len(partial)] + json.dumps(close) + "\n"
    execs.write_text(text, encoding="utf-8")
    done = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    assert done == pytest.approx(_full_replay(execs)) and done < 0.0, (
        "once the trailing line completes, the round-trip settles and equals full replay"
    )


def test_out_of_order_late_asof_append_still_equals_full_replay(ks_home: Path) -> None:
    """DEFENSIVE: if a late-appended fill carries an asof EARLIER than already-settled records
    (the bus is not guaranteed strictly asof-monotonic), the incremental path must still equal
    the full replay — the implementation falls back to full replay when monotonicity breaks."""
    execs = ks_home / "executions.jsonl"
    # Batch 1: a complete AAPL round-trip at 09:00 -> 11:00 (settles, evicted).
    _append(execs, [
        _fill("AAPL", 10.0, 100.0, "2026-06-14T09:00:00Z", "a-open"),
        _fill("AAPL", -10.0, 90.0, "2026-06-14T11:00:00Z", "a-close"),
    ])
    autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    # Batch 2: a MSFT round-trip whose asof is EARLIER (08:00 -> 10:00) than the settled AAPL.
    _append(execs, [
        _fill("MSFT", 6.0, 50.0, "2026-06-14T08:00:00Z", "m-open"),
        _fill("MSFT", -6.0, 40.0, "2026-06-14T10:00:00Z", "m-close"),
    ])
    inc = autonomous.compute_cumulative_realized_pnl_pct(executions_path=execs)
    full = _full_replay(execs)
    assert inc == pytest.approx(full), (
        "an out-of-order late asof must not corrupt the basis — incremental must equal full replay"
    )
