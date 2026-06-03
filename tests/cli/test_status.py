"""Tests for hermes_quant.cli.status — unified `quant status` CLI (ADR-0059)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.cli import status as status_mod
from hermes_quant.cli.status import (
    DEFAULT_QUANT_HOME,
    StatusReport,
    _read_tail_lines,
    format_status_human,
    format_status_json,
    quant_status,
    run_cli,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _audit(kind: str, asof: datetime, *, payload: dict | None = None) -> dict:
    return {
        "event_id": f"evt-{kind}-{asof.isoformat()}",
        "kind": kind,
        "schema_version": 1,
        "asof": asof.isoformat(),
        "source": "test",
        "payload": payload or {},
    }


def _now() -> datetime:
    # Pinned reference for deterministic windowing in tests.
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


def _make_state_db(db_path: Path, *, positions: list[tuple], cash: list[tuple]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                account_id TEXT, asset_class TEXT, symbol TEXT,
                quantity REAL, avg_entry_price REAL, last_update_at TEXT,
                PRIMARY KEY (account_id, asset_class, symbol)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cash (
                account_id TEXT PRIMARY KEY,
                balance_usd REAL, last_update_at TEXT, equity_total REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)", positions
        )
        conn.executemany("INSERT INTO cash VALUES (?, ?, ?, ?)", cash)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_quant_home_returns_zeroed_report(tmp_path: Path) -> None:
    """A pristine quant_home with no files must produce a valid empty report."""
    report = quant_status(quant_home=tmp_path, now=_now())
    assert isinstance(report, StatusReport)
    assert report.audit_summary == {}
    assert report.proposed_today == 0
    assert report.approved_today == 0
    assert report.rejected_today == 0
    assert report.top_rejection_reasons == []
    assert report.recent_decisions == []
    assert report.recent_reflections == []
    assert report.recent_hypotheses == []
    assert report.recent_run_cards == []
    assert report.open_hypotheses_count == 0
    assert report.positions == []
    assert report.cash == []
    # Canonical tier slots must always exist with zeros.
    for tier in ("premium", "standard", "experimental", "rejected"):
        assert report.factor_verdict_summary.get(tier) == 0
    # No warnings on a clean tree.
    assert report.warnings == []


def test_audit_log_in_window_counts(tmp_path: Path) -> None:
    """Mixed gate_approval / gate_rejection events inside the window are counted."""
    now = _now()
    rows = [
        _audit("proposal_emitted", now - timedelta(hours=2)),
        _audit("gate_approval", now - timedelta(hours=1)),
        _audit("gate_approval", now - timedelta(hours=3)),
        _audit("gate_rejection", now - timedelta(hours=2), payload={"reason": "size_cap"}),
        _audit("gate_rejection", now - timedelta(hours=4), payload={"reason": "size_cap"}),
        _audit("gate_rejection", now - timedelta(hours=5), payload={"reason": "stale_data"}),
    ]
    _write_jsonl(tmp_path / "governance" / "audit_log.jsonl", rows)
    report = quant_status(quant_home=tmp_path, now=now)
    assert report.proposed_today == 1
    assert report.approved_today == 2
    assert report.rejected_today == 3
    # top rejection reasons must be sorted by count desc
    reasons = report.top_rejection_reasons
    assert reasons[0] == ("size_cap", 2)
    assert ("stale_data", 1) in reasons


def test_audit_log_outside_window_excluded(tmp_path: Path) -> None:
    now = _now()
    rows = [
        _audit("gate_approval", now - timedelta(hours=1)),     # in 24h
        _audit("gate_approval", now - timedelta(days=3)),      # out of 24h
        _audit("gate_rejection", now - timedelta(days=10), payload={"reason": "x"}),  # out
    ]
    _write_jsonl(tmp_path / "governance" / "audit_log.jsonl", rows)
    report = quant_status(quant_home=tmp_path, now=now, asof_window=timedelta(hours=24))
    assert report.approved_today == 1
    assert report.rejected_today == 0
    assert report.top_rejection_reasons == []


def test_malformed_jsonl_line_skipped_with_warning(tmp_path: Path) -> None:
    audit_path = tmp_path / "governance" / "audit_log.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    valid = _audit("gate_approval", _now() - timedelta(hours=1))
    with open(audit_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(valid) + "\n")
        fh.write("this is not json\n")
        fh.write("{ also bad\n")
        fh.write(json.dumps(_audit("gate_rejection", _now() - timedelta(hours=2),
                                   payload={"reason": "bad"})) + "\n")
    report = quant_status(quant_home=tmp_path, now=_now())
    assert report.approved_today == 1
    assert report.rejected_today == 1
    assert any("audit_log.jsonl" in w and "malformed" in w for w in report.warnings)


def test_tail_read_only_reads_last_window(tmp_path: Path) -> None:
    """Files larger than 256 KiB must NOT be fully loaded into memory."""
    audit_path = tmp_path / "governance" / "audit_log.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    # Build a >1 MiB synthetic file: pad with dummy "fill" rows, then put a
    # small set of "interesting" rows at the end.
    now = _now()
    pad_row = json.dumps(
        {
            "event_id": "x",
            "kind": "fill",
            "schema_version": 1,
            "asof": (now - timedelta(days=30)).isoformat(),
            "source": "synthetic",
            "payload": {"junk": "x" * 500},
        }
    )
    tail_rows = [
        _audit("gate_approval", now - timedelta(hours=1)),
        _audit("gate_rejection", now - timedelta(hours=2), payload={"reason": "synthetic"}),
    ]
    with open(audit_path, "w", encoding="utf-8") as fh:
        # Write ~2000 fat lines first → well over 1 MiB.
        for _ in range(2000):
            fh.write(pad_row + "\n")
        for r in tail_rows:
            fh.write(json.dumps(r) + "\n")
    size = audit_path.stat().st_size
    assert size > 1024 * 1024  # sanity: fixture really is >1 MiB

    report = quant_status(quant_home=tmp_path, now=now)
    # We only see the tail rows we appended after the fat padding (in-window).
    assert report.approved_today == 1
    assert report.rejected_today == 1
    # Most of the padded "fill" rows are discarded by the tail window AND would
    # be out-of-window anyway; ensure no audit_summary explosion.
    assert report.audit_summary.get("fill", 0) == 0


def test_format_human_has_all_sections(tmp_path: Path) -> None:
    report = quant_status(quant_home=tmp_path, now=_now())
    text = format_status_human(report)
    assert isinstance(text, str)
    assert text  # non-empty
    for section in (
        "audit_log (governance)",
        "recent decisions (memory)",
        "recent reflections (memory)",
        "hypotheses (research)",
        "recent run cards (research)",
        "factor verdicts (factors)",
        # Prefix-match: the header carries a cost-basis/MTM suffix since
        # ADR-0086 Phase 1 relabeled equity_total ("positions / cash
        # (state.db — cost-basis, not MTM)"). Match the stable prefix so the
        # test doesn't break on the (correct) relabel.
        "positions / cash (state.db",
    ):
        assert section in text, f"missing section: {section}"
    # Empty stores show the placeholder.
    assert "(no events yet)" in text


def test_format_json_round_trips(tmp_path: Path) -> None:
    now = _now()
    _write_jsonl(
        tmp_path / "governance" / "audit_log.jsonl",
        [_audit("gate_approval", now - timedelta(hours=1))],
    )
    report = quant_status(quant_home=tmp_path, now=now)
    raw = format_status_json(report)
    obj = json.loads(raw)
    assert obj["approved_today"] == 1
    assert obj["quant_home"] == str(tmp_path)
    assert "audit_summary" in obj


def test_state_db_missing_no_crash(tmp_path: Path) -> None:
    report = quant_status(quant_home=tmp_path, now=_now())
    assert report.positions == []
    assert report.cash == []
    # No warnings about state.db when it simply doesn't exist.
    assert not any("state.db" in w for w in report.warnings)


def test_state_db_present_surfaces_positions_and_cash(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(
        db,
        positions=[
            ("paper-default", "equity", "AAPL", 100.0, 175.5, "2026-05-27T11:00:00+00:00"),
            ("paper-default", "equity", "MSFT", -50.0, 410.0, "2026-05-27T11:30:00+00:00"),
        ],
        cash=[("paper-default", 50_000.0, "2026-05-27T11:30:00+00:00", 75_000.0)],
    )
    report = quant_status(quant_home=tmp_path, now=_now())
    assert len(report.positions) == 2
    syms = {p.symbol for p in report.positions}
    assert syms == {"AAPL", "MSFT"}
    assert len(report.cash) == 1
    assert report.cash[0].balance_usd == 50_000.0
    assert report.cash[0].equity_total == 75_000.0


def test_factor_verdict_tier_counts(tmp_path: Path) -> None:
    factor_path = tmp_path / "factors" / "factor_verdicts.jsonl"
    rows = [
        {"factor_id": "f1", "tier": "premium", "name": "n", "ic_panel": {},
         "production_ready": True, "reasons": [], "reviewed_at": "2026-05-27T10:00:00+00:00"},
        {"factor_id": "f2", "tier": "standard", "name": "n", "ic_panel": {},
         "production_ready": True, "reasons": [], "reviewed_at": "2026-05-27T10:01:00+00:00"},
        {"factor_id": "f3", "tier": "experimental", "name": "n", "ic_panel": {},
         "production_ready": False, "reasons": [], "reviewed_at": "2026-05-27T10:02:00+00:00"},
        {"factor_id": "f4", "tier": "rejected", "name": "n", "ic_panel": {},
         "production_ready": False, "reasons": [], "reviewed_at": "2026-05-27T10:03:00+00:00"},
        # Latest-per-factor wins: f1 re-graded as rejected.
        {"factor_id": "f1", "tier": "rejected", "name": "n", "ic_panel": {},
         "production_ready": False, "reasons": [], "reviewed_at": "2026-05-27T11:00:00+00:00"},
    ]
    _write_jsonl(factor_path, rows)
    report = quant_status(quant_home=tmp_path, now=_now())
    summary = report.factor_verdict_summary
    assert summary["standard"] == 1
    assert summary["experimental"] == 1
    # f1 was overwritten from premium to rejected → rejected=2, premium=0.
    assert summary["rejected"] == 2
    assert summary["premium"] == 0


def test_run_cards_falsified_highlighted(tmp_path: Path) -> None:
    rc_path = tmp_path / "research" / "run_cards.jsonl"
    rows = [
        {"run_id": "rc1", "verdict": "validated", "strategy_name": "ema-cross",
         "hypothesis_id": "h1", "started_at": "2026-05-27T09:00:00+00:00",
         "ended_at": "2026-05-27T09:30:00+00:00",
         "strategy_config_hash": "abc", "universe": [],
         "window_start": "2026-01-01", "window_end": "2026-05-01",
         "metrics": {}, "artifacts": {}, "verdict_reasons": []},
        {"run_id": "rc2", "verdict": "falsified", "strategy_name": "rsi-mean-rev",
         "hypothesis_id": "h2", "started_at": "2026-05-27T10:00:00+00:00",
         "ended_at": "2026-05-27T10:30:00+00:00",
         "strategy_config_hash": "def", "universe": [],
         "window_start": "2026-01-01", "window_end": "2026-05-01",
         "metrics": {}, "artifacts": {}, "verdict_reasons": []},
    ]
    _write_jsonl(rc_path, rows)
    report = quant_status(quant_home=tmp_path, now=_now())
    assert len(report.recent_run_cards) == 2
    text = format_status_human(report)
    assert "FALSIFIED" in text


def test_store_filter_in_json_output(tmp_path: Path) -> None:
    """--store audit must restrict JSON output to the audit slice only."""
    now = _now()
    _write_jsonl(
        tmp_path / "governance" / "audit_log.jsonl",
        [_audit("gate_approval", now - timedelta(hours=1))],
    )
    _write_jsonl(
        tmp_path / "memory" / "decisions.jsonl",
        [{"kind": "decision", "decision_id": "d1", "ticker": "AAPL", "side": "long",
          "asof_decision": (now - timedelta(hours=2)).isoformat()}],
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_cli(
            [
                "--quant-home",
                str(tmp_path),
                "--format",
                "json",
                "--store",
                "audit",
            ]
        )
    assert rc == 0
    obj = json.loads(buf.getvalue())
    # When --store is non-"all", we emit a {store_name: slice} dict.
    assert "audit" in obj
    audit_slice = obj["audit"]
    assert "approved_today" in audit_slice
    # The decisions slice is NOT included.
    assert "recent_decisions" not in audit_slice


def test_window_hours_changes_counts(tmp_path: Path) -> None:
    now = _now()
    rows = [
        _audit("gate_approval", now - timedelta(hours=2)),    # in 24h, in 168h
        _audit("gate_approval", now - timedelta(hours=48)),   # not in 24h, in 168h
        _audit("gate_approval", now - timedelta(hours=120)),  # not in 24h, in 168h
    ]
    _write_jsonl(tmp_path / "governance" / "audit_log.jsonl", rows)
    r1 = quant_status(quant_home=tmp_path, now=now, asof_window=timedelta(hours=1))
    r24 = quant_status(quant_home=tmp_path, now=now, asof_window=timedelta(hours=24))
    r168 = quant_status(quant_home=tmp_path, now=now, asof_window=timedelta(hours=168))
    assert r1.approved_today == 0
    assert r24.approved_today == 1
    assert r168.approved_today == 3


def test_nonexistent_quant_home_path(tmp_path: Path) -> None:
    bogus = tmp_path / "does" / "not" / "exist"
    report = quant_status(quant_home=bogus, now=_now())
    assert isinstance(report, StatusReport)
    assert report.proposed_today == 0
    assert report.positions == []


def test_tail_read_returns_only_tail_for_large_file(tmp_path: Path) -> None:
    """Direct unit test of the tail-reader: only the last bytes are decoded."""
    big = tmp_path / "big.jsonl"
    head_marker = {"id": "HEAD_MARKER", "k": "head"}
    tail_marker = {"id": "TAIL_MARKER", "k": "tail"}
    pad = {"id": "pad", "k": "pad", "junk": "z" * 600}
    with open(big, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head_marker) + "\n")
        for _ in range(1500):
            fh.write(json.dumps(pad) + "\n")
        fh.write(json.dumps(tail_marker) + "\n")
    assert big.stat().st_size > 256 * 1024

    warnings: list[str] = []
    rows = _read_tail_lines(big, warnings)
    ids = [r.get("id") for r in rows]
    assert "TAIL_MARKER" in ids
    assert "HEAD_MARKER" not in ids


def test_top_rejection_reasons_sorted_desc(tmp_path: Path) -> None:
    now = _now()
    payloads = (
        ["alpha"] * 5
        + ["beta"] * 3
        + ["gamma"] * 7
        + ["delta"] * 1
    )
    rows = [
        _audit("gate_rejection", now - timedelta(hours=1), payload={"reason": p})
        for p in payloads
    ]
    _write_jsonl(tmp_path / "governance" / "audit_log.jsonl", rows)
    report = quant_status(quant_home=tmp_path, now=now)
    top = report.top_rejection_reasons
    # Top 3 only, sorted desc by count.
    assert len(top) == 3
    assert top[0] == ("gamma", 7)
    assert top[1] == ("alpha", 5)
    assert top[2] == ("beta", 3)


def test_naive_timestamps_treated_as_utc(tmp_path: Path) -> None:
    """Events with naive ISO strings (no tz) must be treated as UTC."""
    now = _now()
    naive_recent = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    naive_old = (now - timedelta(days=10)).replace(tzinfo=None).isoformat()
    rows = [
        {
            "event_id": "1",
            "kind": "gate_approval",
            "schema_version": 1,
            "asof": naive_recent,
            "source": "test",
            "payload": {},
        },
        {
            "event_id": "2",
            "kind": "gate_approval",
            "schema_version": 1,
            "asof": naive_old,
            "source": "test",
            "payload": {},
        },
    ]
    _write_jsonl(tmp_path / "governance" / "audit_log.jsonl", rows)
    report = quant_status(quant_home=tmp_path, now=now)
    assert report.approved_today == 1


def test_quant_home_none_defaults_to_hermes_home(monkeypatch, tmp_path: Path) -> None:
    """quant_home=None must resolve to ~/.hermes/quant (we mock the resolution)."""
    fake_home = tmp_path / "hq"
    monkeypatch.setattr(status_mod, "DEFAULT_QUANT_HOME", fake_home)
    # Directory doesn't even need to exist — must still produce empty report.
    report = quant_status(quant_home=None, now=_now())
    assert report.quant_home == str(fake_home)
    assert report.audit_summary == {}


def test_default_quant_home_constant_resolves_to_dot_hermes() -> None:
    """The DEFAULT_QUANT_HOME constant must point under ~/.hermes/quant."""
    expected = Path.home() / ".hermes" / "quant"
    assert DEFAULT_QUANT_HOME == expected


def test_decisions_and_reflections_recent_lists(tmp_path: Path) -> None:
    """Recent decisions/reflections are returned newest-last via tail."""
    now = _now()
    decisions = [
        {"kind": "decision", "decision_id": f"d{i}", "ticker": "AAPL",
         "side": "long",
         "asof_decision": (now - timedelta(hours=24 - i)).isoformat()}
        for i in range(10)
    ]
    _write_jsonl(tmp_path / "memory" / "decisions.jsonl", decisions)
    reflections = [
        {"reflection_id": f"r{i}", "ticker": "AAPL",
         "asof_resolution": (now - timedelta(hours=24 - i)).isoformat()}
        for i in range(6)
    ]
    _write_jsonl(tmp_path / "memory" / "reflections.jsonl", reflections)
    report = quant_status(quant_home=tmp_path, now=now)
    assert len(report.recent_decisions) == 5
    assert len(report.recent_reflections) == 3
    # Newest first per spec ("recent_decisions: last 5", expected newest-first).
    assert report.recent_decisions[0]["decision_id"] == "d9"
    assert report.recent_reflections[0]["reflection_id"] == "r5"


def test_open_hypotheses_count_and_recent(tmp_path: Path) -> None:
    rows = [
        {"kind": "hypothesis", "hypothesis_id": "h1", "title": "alpha",
         "status": "open", "created_at": "2026-05-27T10:00:00+00:00"},
        {"kind": "hypothesis", "hypothesis_id": "h2", "title": "beta",
         "status": "open", "created_at": "2026-05-27T10:01:00+00:00"},
        {"kind": "hypothesis", "hypothesis_id": "h3", "title": "gamma",
         "status": "open", "created_at": "2026-05-27T10:02:00+00:00"},
        # h2 transitions to falsified.
        {"kind": "status_change", "hypothesis_id": "h2",
         "new_status": "falsified", "asof": "2026-05-27T11:00:00+00:00"},
    ]
    _write_jsonl(tmp_path / "research" / "hypotheses.jsonl", rows)
    report = quant_status(quant_home=tmp_path, now=_now())
    # h1 + h3 are still open; h2 was falsified.
    assert report.open_hypotheses_count == 2
    titles = {h.get("title") for h in report.recent_hypotheses}
    assert "alpha" in titles or "beta" in titles or "gamma" in titles


def test_run_cli_exit_code_zero_on_missing_path(tmp_path: Path) -> None:
    """The CLI must always exit 0 even if the quant_home is missing."""
    bogus = tmp_path / "totally" / "does" / "not" / "exist"
    rc = run_cli(["--quant-home", str(bogus), "--format", "json"])
    assert rc == 0


def test_human_format_shows_position_and_cash(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(
        db,
        positions=[
            ("paper-default", "equity", "AAPL", 10.0, 175.5, "2026-05-27T11:00:00+00:00"),
        ],
        cash=[("paper-default", 1234.56, "2026-05-27T11:00:00+00:00", 3000.0)],
    )
    report = quant_status(quant_home=tmp_path, now=_now())
    text = format_status_human(report)
    assert "AAPL" in text
    assert "1234.56" in text
    assert "3000.00" in text


def test_script_entry_point_executes(tmp_path: Path) -> None:
    """Smoke-test the actual scripts/quant-status.py via subprocess."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "quant-status.py"
    assert script.exists(), f"script missing: {script}"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--quant-home",
            str(tmp_path),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    obj = json.loads(result.stdout)
    assert obj["quant_home"] == str(tmp_path)
    assert obj["proposed_today"] == 0
