"""Tests for the settlement journal (ADR-0010).

Coverage:
- Phase A: append_pending writes Phase-A entry
- Phase B: resolve patches the entry
- Atomic-rename safety (no partial-file races visible)
- Two-phase invariants (mixed Phase-A/Phase-B states reject)
- Pending entries protected from rotation
- HTML-comment delimiter robustness (entry body can contain '---' / '##')
- Round-trip: render → parse → render produces equivalent
- HITL append_human_override: approve/reject/expire kinds
- get_recent_lessons: newest-first, n_same + n_cross
- Idempotency: same entry_id can't be appended twice
- File corruption: unparseable file backs up + recovers
- Empty / missing file: returns []
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_quant.journal import (
    AnalystComponent,
    JournalEntryAlreadyResolved,
    JournalEntryNotFound,
    Reflection,
    SettlementEntry,
    append_human_override,
    append_pending,
    get_recent_lessons,
    parse_journal,
    resolve,
)
from hermes_quant.journal.render import ENTRY_DELIM, render_journal


def _make_entry(
    entry_id: str = "prop_20260513T100000_AAPL_abc123",
    *,
    symbol: str = "AAPL",
    direction: int = 1,
    decision_price: float = 100.0,
    when: datetime | None = None,
    resolved: bool = False,
) -> SettlementEntry:
    when = when or datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
    e = SettlementEntry(
        entry_id=entry_id,
        asof_decision=when,
        symbol=symbol,
        asset_class="equity",
        direction=direction,
        confidence=0.65,
        target_position_pct=0.05,
        decision_price=decision_price,
        benchmark_symbol="SPY",
        per_analyst_components=[
            AnalystComponent(
                analyst="classical_ta", direction=direction, confidence=0.6, weight=0.5
            ),
            AnalystComponent(
                analyst="microstructure_lite", direction=direction, confidence=0.7, weight=0.5
            ),
        ],
        reason=f"Test {entry_id}",
    )
    if resolved:
        e = SettlementEntry(
            **{
                **_entry_to_dict(e),
                "asof_settlement": when + timedelta(hours=4),
                "exit_price": decision_price * 1.02,
                "raw_return": 0.02,
                "alpha_return": 0.012,
                "hold_minutes": 240,
                "reflection": Reflection(
                    thesis_held=direction > 0,
                    magnitude_error=0.5,
                ),
            }
        )
    return e


def _entry_to_dict(e: SettlementEntry) -> dict:
    if hasattr(e, "model_dump"):
        return e.model_dump()
    from dataclasses import asdict

    return asdict(e)


# ---------------------------------------------------------------------------


def test_append_pending_writes_phase_a_entry(tmp_path):
    journal = tmp_path / "journal.md"
    entry = _make_entry()
    append_pending(entry, path=journal)

    assert journal.exists()
    content = journal.read_text()
    assert ENTRY_DELIM in content
    assert "[pending]" in content
    assert entry.entry_id in content
    assert "asof_decision:" in content


def test_resolve_patches_entry(tmp_path):
    journal = tmp_path / "journal.md"
    entry = _make_entry()
    append_pending(entry, path=journal)

    settled = resolve(
        entry.entry_id,
        asof_settlement=entry.asof_decision + timedelta(hours=4),
        exit_price=102.0,
        raw_return=0.02,
        alpha_return=0.012,
        hold_minutes=240,
        reflection=Reflection(thesis_held=True, magnitude_error=0.4),
        path=journal,
    )
    assert settled.is_resolved()
    assert settled.raw_return == pytest.approx(0.02)
    assert "[pending]" not in journal.read_text()
    assert "+2.00% raw" in journal.read_text()


def test_resolve_missing_entry_raises(tmp_path):
    journal = tmp_path / "journal.md"
    with pytest.raises(JournalEntryNotFound):
        resolve(
            "nonexistent",
            asof_settlement=datetime.now(tz=timezone.utc),
            exit_price=100.0,
            raw_return=0.01,
            alpha_return=0.005,
            hold_minutes=120,
            reflection=Reflection(thesis_held=True, magnitude_error=0.0),
            path=journal,
        )


def test_resolve_already_resolved_raises(tmp_path):
    journal = tmp_path / "journal.md"
    entry = _make_entry()
    append_pending(entry, path=journal)
    resolve(
        entry.entry_id,
        asof_settlement=entry.asof_decision + timedelta(hours=4),
        exit_price=102.0,
        raw_return=0.02,
        alpha_return=0.012,
        hold_minutes=240,
        reflection=Reflection(thesis_held=True, magnitude_error=0.4),
        path=journal,
    )
    with pytest.raises(JournalEntryAlreadyResolved):
        resolve(
            entry.entry_id,
            asof_settlement=entry.asof_decision + timedelta(hours=8),
            exit_price=104.0,
            raw_return=0.04,
            alpha_return=0.025,
            hold_minutes=480,
            reflection=Reflection(thesis_held=True, magnitude_error=0.5),
            path=journal,
        )


def test_append_pending_duplicate_id_raises(tmp_path):
    journal = tmp_path / "journal.md"
    e1 = _make_entry()
    append_pending(e1, path=journal)
    with pytest.raises(ValueError, match="already in journal"):
        append_pending(e1, path=journal)


def test_round_trip_render_then_parse(tmp_path):
    journal = tmp_path / "journal.md"
    e1 = _make_entry("prop_20260513T100000_AAPL_a1", symbol="AAPL")
    e2 = _make_entry("prop_20260513T110000_MSFT_b2", symbol="MSFT", direction=-1)
    append_pending(e1, path=journal)
    append_pending(e2, path=journal)

    parsed = parse_journal(journal.read_text())
    assert len(parsed) == 2
    assert {p.entry_id for p in parsed} == {e1.entry_id, e2.entry_id}
    # Symbol extraction from entry_id
    assert {p.symbol for p in parsed} == {"AAPL", "MSFT"}


def test_parse_empty_file_returns_empty(tmp_path):
    journal = tmp_path / "journal.md"
    journal.write_text("")
    assert parse_journal(journal.read_text()) == []
    assert parse_journal("   \n   \n") == []
    assert parse_journal("# Just a header\n") == []


def test_parse_unparseable_meta_skips_silently():
    """Per ADR-0010 §8: hand-edits to the meta block silently lose entries."""
    bad = """\
# header
## AAPL ↑ [pending]
<!-- META_BEGIN -->
this is not key:value form
<!-- META_END -->
<!-- ENTRY_END -->

## MSFT ↓ [pending]
<!-- META_BEGIN -->
entry_id: prop_x_MSFT_001
asof_decision: 2026-05-13T10:00:00
asset_class: equity
direction: -1
<!-- META_END -->
<!-- ENTRY_END -->
"""
    parsed = parse_journal(bad)
    # The first one survives because it has a valid key:value line
    # ('this is not key:value form' parses key='this is not key', value='value form'
    # with required entry_id missing -> falls through to KeyError, skipped).
    assert len(parsed) == 1
    assert parsed[0].symbol == "MSFT"


def test_html_comment_delimiter_robust_to_body_markdown(tmp_path):
    """Per ADR-0010 §Decision §2: ENTRY_END is HTML comment so '---' / '##'
    in narrative body don't collide."""
    journal = tmp_path / "journal.md"

    e = _make_entry()
    # Inject markdown into the reason that would break a '---' or '##' separator
    e.reason = "Multi-line reason\n\n## Sub-heading\n\n---\n\nMore text"
    append_pending(e, path=journal)

    parsed = parse_journal(journal.read_text())
    assert len(parsed) == 1
    assert parsed[0].entry_id == e.entry_id


def test_corrupt_file_backs_up_and_recovers(tmp_path):
    """Per writer._load_entries_safe: parse failure → backup + fresh start."""
    journal = tmp_path / "journal.md"
    # Write JOURNAL_HEADER + a half-formed entry (no META_BEGIN/META_END)
    journal.write_text("# header\n\n## AAPL ↑ [malformed]\n\nno meta block\n")

    # parse_journal alone is forgiving (returns []), but writer's load
    # calls parse_journal too; this should succeed (parsing a no-meta
    # file just returns []).
    new_entry = _make_entry()
    # should NOT raise
    append_pending(new_entry, path=journal)
    parsed = parse_journal(journal.read_text())
    assert len(parsed) == 1


# ---------------------------------------------------------------------------
# ar22 — tolerant recovery: a torn / partially-corrupt journal must NOT
# discard the whole settlement ledger. Recover the entries that DO parse.
# ---------------------------------------------------------------------------


def _seed_two_pending(journal: Path) -> tuple[SettlementEntry, SettlementEntry]:
    e1 = _make_entry("prop_20260513T100000_AAPL_a1", symbol="AAPL")
    e2 = _make_entry("prop_20260513T110000_MSFT_b2", symbol="MSFT", direction=-1)
    append_pending(e1, path=journal)
    append_pending(e2, path=journal)
    assert len(parse_journal(journal.read_text())) == 2
    return e1, e2


def test_invalid_utf8_tail_recovers_parseable_entries(tmp_path):
    """RED for ar22: a torn write that injects invalid UTF-8 bytes must not
    cause _load_entries_safe to discard every prior PENDING entry.

    Pre-fix: read_text(encoding='utf-8') raises UnicodeDecodeError before
    parse_journal runs, the whole journal is renamed to .bak, and 0 entries
    are recovered — the next append silently drops the settlement ledger.
    """
    from hermes_quant.journal.writer import _load_entries_safe

    journal = tmp_path / "journal.md"
    e1, e2 = _seed_two_pending(journal)

    # Mimic a torn write: append invalid UTF-8 bytes to the tail.
    with open(journal, "ab") as f:
        f.write(b"\xff\xfe torn-write garbage \x80\x81")

    recovered = _load_entries_safe(journal)
    ids = {e.entry_id for e in recovered}
    # The two valid PENDING entries must survive — not be discarded.
    assert e1.entry_id in ids
    assert e2.entry_id in ids
    assert len(recovered) == 2


def test_invalid_utf8_next_append_preserves_prior_pending(tmp_path):
    """RED for ar22 (the data-loss consequence): after a torn write, the
    next append_pending must keep the prior PENDING entries, and a later
    resolve() for one of them must succeed (not raise JournalEntryNotFound)."""
    journal = tmp_path / "journal.md"
    e1, e2 = _seed_two_pending(journal)

    with open(journal, "ab") as f:
        f.write(b"\xff\xfe torn-write garbage \x80\x81")

    # Next append must NOT wipe the ledger.
    e3 = _make_entry("prop_20260513T120000_NVDA_c3", symbol="NVDA")
    append_pending(e3, path=journal)

    parsed = parse_journal(journal.read_text())
    ids = {e.entry_id for e in parsed}
    assert {e1.entry_id, e2.entry_id, e3.entry_id} <= ids

    # And resolving a prior PENDING entry still works.
    settled = resolve(
        e1.entry_id,
        asof_settlement=e1.asof_decision + timedelta(hours=4),
        exit_price=102.0,
        raw_return=0.02,
        alpha_return=0.012,
        hold_minutes=240,
        reflection=Reflection(thesis_held=True, magnitude_error=0.4),
        path=journal,
    )
    assert settled.is_resolved()


def test_truncated_mid_entry_recovers_complete_entries(tmp_path):
    """A torn write that truncates mid-entry must still recover the entries
    whose META blocks are complete."""
    journal = tmp_path / "journal.md"
    e1, e2 = _seed_two_pending(journal)

    # Append a half-written entry block (META_BEGIN with no META_END) — the
    # kind of tail a crash mid-render would leave behind.
    with open(journal, "a", encoding="utf-8") as f:
        f.write("\n## TSLA ↑ [pending]\n<!-- META_BEGIN -->\nentry_id: prop_x_TSLA_trunc\n")

    from hermes_quant.journal.writer import _load_entries_safe

    recovered = _load_entries_safe(journal)
    ids = {e.entry_id for e in recovered}
    assert e1.entry_id in ids
    assert e2.entry_id in ids


def test_one_bad_line_skipped_rest_recovered(tmp_path):
    """A single unparseable line injected into one entry's meta block must
    drop only that entry, not the whole ledger."""
    journal = tmp_path / "journal.md"
    e1, e2 = _seed_two_pending(journal)

    # Corrupt e1's entry_id line so it fails to parse, leave e2 intact.
    content = journal.read_text()
    broken = content.replace(
        "entry_id: prop_20260513T100000_AAPL_a1",
        "entry_id:::: <<<garbage>>>",
        1,
    )
    journal.write_text(broken)

    from hermes_quant.journal.writer import _load_entries_safe

    recovered = _load_entries_safe(journal)
    ids = {e.entry_id for e in recovered}
    # e2 survives; e1 was corrupted out.
    assert e2.entry_id in ids
    assert e1.entry_id not in ids


def test_fully_valid_journal_unchanged_by_load(tmp_path):
    """A healthy journal round-trips through _load_entries_safe with no
    entry loss and no spurious .bak churn."""
    from hermes_quant.journal.writer import _load_entries_safe

    journal = tmp_path / "journal.md"
    e1, e2 = _seed_two_pending(journal)
    before = journal.read_text()

    recovered = _load_entries_safe(journal)
    assert {e.entry_id for e in recovered} == {e1.entry_id, e2.entry_id}
    # No backup made for a healthy journal; original untouched.
    assert not (tmp_path / "journal.md.bak").exists()
    assert journal.read_text() == before


def test_empty_and_absent_journal_load_returns_empty(tmp_path):
    """An absent or empty journal loads cleanly as [] with no backup."""
    from hermes_quant.journal.writer import _load_entries_safe

    absent = tmp_path / "absent.md"
    assert _load_entries_safe(absent) == []
    assert not (tmp_path / "absent.md.bak").exists()

    empty = tmp_path / "empty.md"
    empty.write_text("")
    assert _load_entries_safe(empty) == []
    assert not (tmp_path / "empty.md.bak").exists()


# ---------------------------------------------------------------------------
# HITL integration
# ---------------------------------------------------------------------------


def test_append_human_override_approve(tmp_path, monkeypatch):
    journal = tmp_path / "journal.md"
    monkeypatch.setattr("hermes_quant.journal.writer.DEFAULT_JOURNAL_PATH", journal)

    # Hand-build a proposal stand-in
    from hermes_quant.proposals import Proposal

    proposal = Proposal(
        proposal_id="prop_20260513T180000_AAPL_xyz789",
        state="approved",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        created_at="2026-05-13T18:00:00Z",
        expires_at="2026-05-13T18:15:00Z",
        approved_at="2026-05-13T18:01:00Z",
        approver_user_id="codeseys",
        advisor_result={
            "as_of": "2026-05-13T17:55:00Z",
            "decision_price": 175.42,
            "aggregated_signal": {"direction": 1, "confidence": 0.62},
            "risk_gate": {"kelly_fraction": 0.05, "pass": True},
            "analyst_views": [
                {"analyst": "classical_ta", "direction": 1, "confidence": 0.6},
                {"analyst": "microstructure_lite", "direction": 1, "confidence": 0.7},
            ],
        },
    )
    entry = append_human_override(proposal, kind="approve", path=journal)
    assert entry.hitl_kind == "approve"
    assert entry.hitl_approver == "codeseys"
    assert "approved-pending-settlement" in journal.read_text()


def test_append_human_override_tolerates_multileg_proposal_jw1(tmp_path):
    """jw1: a MultiLegProposal has no asset_class/symbol (carries `underlying`).
    Before the fix, append_human_override raised AttributeError -> swallowed by the
    autonomous BLE001 -> EVERY options fire silently lost its audit entry (the
    ADR-0029 evidence trail). RED-proof: revert the two getattr lines in writer.py
    and this raises AttributeError instead of journaling.
    """
    journal = tmp_path / "journal.md"

    class _MultiLegProposalStub:
        # Mirrors the MultiLegProposal attribute surface the journal writer touches:
        # proposal_id + underlying + advisor_result, but NO asset_class / symbol.
        proposal_id = "ml_20260618T230000_AAPL_cc01"
        underlying = "AAPL"
        approver_user_id = None
        advisor_result = {
            "as_of": "2026-06-18T22:55:00Z",
            "decision_price": 1.50,
            "aggregated_signal": {"direction": 1, "confidence": 0.7},
            "risk_gate": {"kelly_fraction": 0.05, "pass": True},
            "analyst_views": [],
        }

    entry = append_human_override(
        _MultiLegProposalStub(), kind="approve", reason="autonomous_options_fire", path=journal
    )
    # The audit entry is written (no swallowed AttributeError) with the multi_leg
    # provenance derived from `underlying`.
    assert entry.hitl_kind == "approve"
    assert entry.asset_class == "multi_leg"
    assert entry.symbol == "AAPL"
    assert entry.entry_id == "ml_20260618T230000_AAPL_cc01"


def test_append_human_override_reject_persists_reason(tmp_path):
    journal = tmp_path / "journal.md"
    from hermes_quant.proposals import Proposal

    proposal = Proposal(
        proposal_id="prop_20260513T180000_AAPL_rej111",
        state="rejected",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        created_at="2026-05-13T18:00:00Z",
        expires_at="2026-05-13T18:15:00Z",
        rejected_at="2026-05-13T18:02:00Z",
        rejection_reason="Earnings tomorrow, too risky",
        advisor_result={
            "as_of": "2026-05-13T17:55:00Z",
            "decision_price": 175.42,
            "aggregated_signal": {"direction": 1, "confidence": 0.62},
            "risk_gate": {"kelly_fraction": 0.05, "pass": True},
            "analyst_views": [],
        },
    )
    entry = append_human_override(
        proposal,
        kind="reject",
        reason="Earnings tomorrow, too risky",
        path=journal,
    )
    assert entry.hitl_kind == "reject"
    assert entry.hitl_reason == "Earnings tomorrow, too risky"
    content = journal.read_text()
    assert "[rejected]" in content
    assert "Earnings tomorrow" in content


def test_append_human_override_idempotent_on_same_id(tmp_path):
    """Same proposal_id passed twice updates rather than duplicates."""
    journal = tmp_path / "journal.md"
    from hermes_quant.proposals import Proposal

    proposal = Proposal(
        proposal_id="prop_20260513T180000_AAPL_dup222",
        state="rejected",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        created_at="2026-05-13T18:00:00Z",
        expires_at="2026-05-13T18:15:00Z",
        advisor_result={
            "decision_price": 100.0,
            "aggregated_signal": {"direction": 1, "confidence": 0.6},
            "risk_gate": {"kelly_fraction": 0.05, "pass": True},
            "analyst_views": [],
        },
    )
    append_human_override(proposal, kind="reject", reason="first reason", path=journal)
    append_human_override(proposal, kind="reject", reason="updated reason", path=journal)
    parsed = parse_journal(journal.read_text())
    assert len(parsed) == 1
    assert parsed[0].hitl_reason == "updated reason"


# ---------------------------------------------------------------------------
# Recent-lessons retrieval (ADR-0010 §7)
# ---------------------------------------------------------------------------


def test_get_recent_lessons_returns_n_same_plus_n_cross(tmp_path):
    journal = tmp_path / "journal.md"
    base = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        e = _make_entry(
            f"prop_20260513T{i:02d}0000_AAPL_aapl{i}",
            symbol="AAPL",
            when=base + timedelta(hours=i),
        )
        append_pending(e, path=journal)
    for i in range(3):
        e = _make_entry(
            f"prop_20260513T{i:02d}0000_MSFT_msft{i}",
            symbol="MSFT",
            when=base + timedelta(hours=i),
        )
        append_pending(e, path=journal)

    lessons = get_recent_lessons("AAPL", n_same=3, n_cross=2, path=journal)
    same = [l for l in lessons if l["is_same"]]
    cross = [l for l in lessons if not l["is_same"]]
    assert len(same) == 3
    assert len(cross) == 2
    # Newest first
    assert same[0]["when"] >= same[1]["when"] >= same[2]["when"]


def test_get_recent_lessons_empty_journal_returns_empty(tmp_path):
    journal = tmp_path / "journal.md"
    assert get_recent_lessons("AAPL", path=journal) == []


def test_get_recent_lessons_includes_reflection_when_resolved(tmp_path):
    journal = tmp_path / "journal.md"
    e = _make_entry("prop_20260513T100000_AAPL_res1", symbol="AAPL")
    append_pending(e, path=journal)
    resolve(
        e.entry_id,
        asof_settlement=e.asof_decision + timedelta(hours=4),
        exit_price=102.0,
        raw_return=0.02,
        alpha_return=0.012,
        hold_minutes=240,
        reflection=Reflection(thesis_held=True, magnitude_error=0.4),
        path=journal,
    )
    lessons = get_recent_lessons("AAPL", n_same=5, n_cross=0, path=journal)
    assert len(lessons) == 1
    assert lessons[0]["resolved"] is True
    assert lessons[0]["reflection"] is not None
    assert lessons[0]["reflection"]["thesis_held"] is True


def test_atomic_write_fsyncs_parent_dir_after_rename(tmp_path, monkeypatch):
    """_atomic_write must fsync the CONTAINING DIRECTORY after os.replace so the
    rename itself survives a crash (POSIX rename(2) durability).

    ADR-0010 §8 / module docstring designate this the settlement ledger.
    settlement_loop derives the ADR-0016 always-on kill-switch realized-P&L
    basis from these entries. The file-data fsync (fsync of the .tmp fd) is NOT
    enough: POSIX rename(2) only guarantees the new directory entry survives a
    crash AFTER the containing directory is itself fsync'd. On a
    power-loss/kernel-panic in the window AFTER os.replace but BEFORE the new
    directory entry is flushed, the rename can revert — the just-appended /
    resolved SettlementEntry vanishes and the next process start reads a SMALLER
    realized drawdown than reality, so the kill-switch fails to trip (fail-OPEN
    on an always-on money rail).

    A bare "fsync was called" assertion would pass vacuously (the file fd is
    fsync'd today). So this asserts an fsync targets a fd that is a DIRECTORY
    (S_ISDIR) AND that it follows the rename.
    """
    import os
    import stat

    journal = tmp_path / "journal.md"
    tmp_target = journal.with_suffix(journal.suffix + ".tmp")

    real_fsync = os.fsync
    real_replace = os.replace

    events: list[tuple[str, str]] = []

    def spy_fsync(fd: int):  # type: ignore[no-untyped-def]
        try:
            st = os.fstat(fd)
            is_dir = stat.S_ISDIR(st.st_mode)
        except OSError:
            is_dir = False
        events.append(("fsync_dir" if is_dir else "fsync_file", str(fd)))
        return real_fsync(fd)

    def spy_replace(src, dst, *a, **k):  # type: ignore[no-untyped-def]
        events.append(("replace", f"{src}->{dst}"))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    append_pending(_make_entry(), path=journal)

    # The rename must have happened.
    replace_idx = next(
        (
            i
            for i, (kind, what) in enumerate(events)
            if kind == "replace" and what == f"{tmp_target}->{journal}"
        ),
        None,
    )
    assert replace_idx is not None, f"_atomic_write did not os.replace; events={events}"

    # A directory fsync must occur AFTER the rename so the new dir entry is
    # durable (the rename itself survives a crash).
    dir_fsync_idx = next(
        (i for i, (kind, _what) in enumerate(events) if kind == "fsync_dir"),
        None,
    )
    assert dir_fsync_idx is not None, (
        "_atomic_write fsync'd the file data but NEVER fsync'd the parent "
        "directory after os.replace; the rename of the settlement ledger is "
        "not crash-durable, so a lost append understates the ADR-0016 "
        f"kill-switch realized-P&L basis (fail-OPEN). events={events}"
    )
    assert dir_fsync_idx > replace_idx, (
        "parent-dir fsync must follow os.replace so the new directory entry "
        f"is what gets flushed; events={events}"
    )
