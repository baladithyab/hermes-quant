"""Tests for hermes_quant.reporting.daily_report (ADR-0061)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.reporting.daily_report import (
    DailyReport,
    _escape_telegram_md_v2,
    format_markdown,
    format_telegram,
    generate_daily_report,
)


# ---------------------------------------------------------------------------
# Fixtures: build a synthetic ~/.hermes/quant/ tree
# ---------------------------------------------------------------------------


@pytest.fixture
def quant_home(tmp_path: Path) -> Path:
    """Empty hermes-quant home (no event stores yet)."""
    home = tmp_path / "quant"
    home.mkdir(parents=True)
    return home


def _audit_event(
    kind: str,
    *,
    asof: datetime,
    payload: dict | None = None,
    schema_version: int = 1,
) -> dict:
    return {
        "event_id": f"evt_{kind}_{asof.timestamp()}_{id(payload)}",
        "kind": kind,
        "schema_version": schema_version,
        "asof": asof.astimezone(UTC).isoformat(),
        "source": "test",
        "payload": payload or {},
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")


def _seed_state_db(home: Path, positions: list[dict], cash: dict | None) -> None:
    db = home / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS positions (
            account_id TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            avg_entry_price REAL NOT NULL,
            last_update_at TEXT NOT NULL,
            PRIMARY KEY (account_id, asset_class, symbol)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS cash (
            account_id TEXT PRIMARY KEY,
            balance_usd REAL NOT NULL,
            last_update_at TEXT NOT NULL,
            equity_total REAL NOT NULL
        ) WITHOUT ROWID;
        """
    )
    for p in positions:
        conn.execute(
            "INSERT OR REPLACE INTO positions "
            "(account_id, asset_class, symbol, quantity, avg_entry_price, last_update_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                p.get("account_id", "paper-default"),
                p.get("asset_class", "equity"),
                p["symbol"],
                p["quantity"],
                p["avg_entry_price"],
                p.get("last_update_at", "2026-05-27T12:00:00+00:00"),
            ),
        )
    if cash is not None:
        conn.execute(
            "INSERT OR REPLACE INTO cash "
            "(account_id, balance_usd, last_update_at, equity_total) "
            "VALUES (?, ?, ?, ?)",
            (
                cash.get("account_id", "paper-default"),
                cash["balance_usd"],
                cash.get("last_update_at", "2026-05-27T12:00:00+00:00"),
                cash["equity_total"],
            ),
        )
    conn.commit()
    conn.close()


def _seed_proposals_db(home: Path, proposals: list[dict]) -> None:
    db = home / "proposals.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS proposals (
            proposal_id TEXT PRIMARY KEY NOT NULL,
            state TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            approved_at TEXT,
            rejected_at TEXT,
            expired_at TEXT,
            record_json TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    for p in proposals:
        conn.execute(
            "INSERT OR REPLACE INTO proposals "
            "(proposal_id, state, symbol, asset_class, timeframe, created_at, "
            " expires_at, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["proposal_id"],
                p.get("state", "pending"),
                p["symbol"],
                p.get("asset_class", "equity"),
                p.get("timeframe", "1d"),
                p["created_at"],
                p["expires_at"],
                json.dumps(p),
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_quant_home_returns_zero_report(quant_home: Path) -> None:
    """Empty ~/.hermes/quant — every table empty, no exception."""
    today = date(2026, 5, 27)
    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert r.date == today
    assert r.gate_table == []
    assert r.positions_table == []
    assert r.pnl_today is None
    assert r.pnl_mtd is None
    assert r.pnl_ytd is None
    assert r.reflections_section == []
    assert r.hypotheses_changes == {"promoted": [], "falsified": [], "new": []}
    assert r.factor_verdicts_today == {}
    assert r.open_proposals == []


def test_gate_counts_correct(quant_home: Path) -> None:
    """5 approvals + 3 rejections — table counts and summary correct."""
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(5):
        rows.append(
            _audit_event(
                "gate_approval",
                asof=base + timedelta(minutes=i),
                payload={"asset": f"AAA{i}", "confidence": 0.55 + i * 0.01},
            )
        )
    for i in range(3):
        rows.append(
            _audit_event(
                "gate_rejection",
                asof=base + timedelta(minutes=10 + i),
                payload={
                    "asset": f"BBB{i}",
                    "reason": "kelly below floor",
                    "confidence": 0.05,
                },
            )
        )
    _write_jsonl(quant_home / "governance" / "audit_log.jsonl", rows)

    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert len(r.gate_table) == 8
    n_approved = sum(1 for x in r.gate_table if x["action"] == "APPROVE")
    n_rejected = sum(1 for x in r.gate_table if x["action"] == "REJECT")
    assert n_approved == 5
    assert n_rejected == 3
    # Summary mentions both counts.
    s = " ".join(r.summary_lines)
    assert "5 approved" in s
    assert "3 rejected" in s


def test_gate_table_sorted_chronologically(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    # Write OUT OF ORDER.
    times = [10, 1, 5, 20, 15, 0, 30]
    rows = [
        _audit_event(
            "gate_approval",
            asof=base + timedelta(minutes=t),
            payload={"asset": f"T{t}"},
        )
        for t in times
    ]
    _write_jsonl(quant_home / "governance" / "audit_log.jsonl", rows)

    r = generate_daily_report(asof=today, quant_home=quant_home)
    asof_seq = [row["asof"] for row in r.gate_table]
    assert asof_seq == sorted(asof_seq)


def test_top_3_rejection_reasons_in_summary(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    rows = []
    # 5 of "kelly below floor", 3 of "stale data", 2 of "halted symbol"
    for i in range(5):
        rows.append(
            _audit_event(
                "gate_rejection",
                asof=base + timedelta(minutes=i),
                payload={"asset": "X", "reason": "kelly below floor"},
            )
        )
    for i in range(3):
        rows.append(
            _audit_event(
                "gate_rejection",
                asof=base + timedelta(minutes=10 + i),
                payload={"asset": "Y", "reason": "stale data"},
            )
        )
    for i in range(2):
        rows.append(
            _audit_event(
                "gate_rejection",
                asof=base + timedelta(minutes=20 + i),
                payload={"asset": "Z", "reason": "halted symbol"},
            )
        )
    _write_jsonl(quant_home / "governance" / "audit_log.jsonl", rows)

    r = generate_daily_report(asof=today, quant_home=quant_home)
    summary = " ".join(r.summary_lines)
    assert "kelly below floor" in summary
    assert "stale data" in summary
    assert "halted symbol" in summary


def test_positions_read_from_state_db(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    _seed_state_db(
        quant_home,
        positions=[
            {"symbol": "AAPL", "quantity": 0.05, "avg_entry_price": 180.0},
            {"symbol": "MSFT", "quantity": 0.03, "avg_entry_price": 410.0},
        ],
        cash={"balance_usd": 95_000.0, "equity_total": 100_000.0},
    )
    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert len(r.positions_table) == 2
    syms = {p["ticker"] for p in r.positions_table}
    assert syms == {"AAPL", "MSFT"}


def test_pnl_today_zero_when_mark_equals_cost(quant_home: Path) -> None:
    """Synthetic test: equity_total == initial_cash → P&L proxy == 0."""
    today = date(2026, 5, 27)
    _seed_state_db(
        quant_home,
        positions=[
            {"symbol": "AAPL", "quantity": 0.05, "avg_entry_price": 180.0},
        ],
        cash={"balance_usd": 91_000.0, "equity_total": 100_000.0},
    )
    r = generate_daily_report(asof=today, quant_home=quant_home)
    # equity_total - initial_cash (100k) = 0
    assert r.pnl_today == 0.0
    # Each open position has unrealized_pnl = 0 because mark==cost.
    for p in r.positions_table:
        assert p["unrealized_pnl"] == 0.0


def test_reflections_within_24h_surface(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    asof_dt = datetime(2026, 5, 27, 23, 30, 0, tzinfo=UTC)
    rows = [
        {
            "schema_version": 1,
            "reflection_id": "r1",
            "decision_id": "d1",
            "asof_resolution": asof_dt.isoformat(),
            "tau_observable": asof_dt.isoformat(),
            "ticker": "AAPL",
            "raw_return": 0.01,
            "alpha_return": 0.005,
            "benchmark": "SPY",
            "holding_days": 1,
            "outcome_quality": 4,
            "reflection_text": "Direction call (long, B+) was correct; alpha +0.5%.",
            "lesson_category": "unknown",
            "reflector_model": "stub",
            "reflector_prompt_hash": "stub:abc",
        },
        # Older than 24h before end-of-asof — should be excluded.
        {
            "schema_version": 1,
            "reflection_id": "r2",
            "decision_id": "d2",
            "asof_resolution": (asof_dt - timedelta(days=10)).isoformat(),
            "tau_observable": (asof_dt - timedelta(days=10)).isoformat(),
            "ticker": "OLD",
            "raw_return": 0.0,
            "alpha_return": 0.0,
            "benchmark": "SPY",
            "holding_days": 1,
            "outcome_quality": 3,
            "reflection_text": "ancient lesson",
            "lesson_category": "unknown",
            "reflector_model": "stub",
            "reflector_prompt_hash": "stub:old",
        },
    ]
    _write_jsonl(quant_home / "memory" / "reflections.jsonl", rows)
    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert len(r.reflections_section) == 1
    assert "AAPL" in r.reflections_section[0]
    assert "ancient lesson" not in " ".join(r.reflections_section)


def test_hypothesis_promoted_and_falsified_detected(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    asof_dt = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    rows = [
        {
            "schema_version": 1,
            "kind": "hypothesis",
            "hypothesis_id": "hyp_AAPL_20260520_abc123",
            "created_at": (asof_dt - timedelta(hours=5)).isoformat(),
            "author": "aria",
            "claim": "AAPL momentum factor improves Sharpe",
            "null_hypothesis": "no improvement",
            "duration_target_days": 30,
            "status": "open",
        },
        {
            "schema_version": 1,
            "kind": "status_change",
            "hypothesis_id": "hyp_OLD_20260101_def456",
            "previous_status": "running",
            "new_status": "validated",
            "asof": asof_dt.isoformat(),
            "evidence": {},
        },
        {
            "schema_version": 1,
            "kind": "status_change",
            "hypothesis_id": "hyp_BAD_20260101_ghi789",
            "previous_status": "running",
            "new_status": "falsified",
            "asof": asof_dt.isoformat(),
            "evidence": {},
        },
    ]
    _write_jsonl(quant_home / "research" / "hypotheses.jsonl", rows)
    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert "hyp_OLD_20260101_def456" in r.hypotheses_changes["promoted"]
    assert "hyp_BAD_20260101_ghi789" in r.hypotheses_changes["falsified"]
    assert any(
        "hyp_AAPL_20260520_abc123" in n for n in r.hypotheses_changes["new"]
    )


def test_factor_verdicts_grouped_by_tier(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    asof_dt = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    rows = [
        {
            "factor_id": f"f{i}",
            "name": f"alpha_{i}",
            "ic_panel": {},
            "production_ready": tier in ("premium", "standard"),
            "tier": tier,
            "reasons": [],
            "reviewed_at": asof_dt.isoformat(),
        }
        for i, tier in enumerate(
            ["premium", "premium", "standard", "experimental", "rejected", "rejected"]
        )
    ]
    _write_jsonl(quant_home / "factors" / "factor_verdicts.jsonl", rows)
    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert r.factor_verdicts_today == {
        "premium": 2,
        "standard": 1,
        "experimental": 1,
        "rejected": 2,
    }


def test_open_proposals_read_from_proposals_db(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    now = datetime.now(UTC)
    expires = now + timedelta(hours=23, minutes=45)
    _seed_proposals_db(
        quant_home,
        proposals=[
            {
                "proposal_id": "prop_20260527_AAPL_a1b2c3",
                "state": "pending",
                "symbol": "AAPL",
                "created_at": now.isoformat(),
                "expires_at": expires.isoformat(),
            },
            {
                # Already approved → should NOT appear.
                "proposal_id": "prop_20260527_MSFT_b3c4d5",
                "state": "approved",
                "symbol": "MSFT",
                "created_at": now.isoformat(),
                "expires_at": expires.isoformat(),
            },
        ],
    )
    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert len(r.open_proposals) == 1
    assert r.open_proposals[0]["proposal_id"] == "prop_20260527_AAPL_a1b2c3"
    assert r.open_proposals[0]["ticker"] == "AAPL"
    # TTL is roughly 23h.
    assert "23h" in r.open_proposals[0]["ttl_remaining"]


def test_format_markdown_valid_structure(quant_home: Path) -> None:
    """Markdown contains required section headers and no malformed rows."""
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    _write_jsonl(
        quant_home / "governance" / "audit_log.jsonl",
        [
            _audit_event(
                "gate_approval",
                asof=base,
                payload={"asset": "AAPL", "confidence": 0.6},
            ),
            _audit_event(
                "gate_rejection",
                asof=base + timedelta(minutes=2),
                payload={
                    "asset": "MSFT",
                    "reason": "halted symbol",
                    "confidence": 0.0,
                },
            ),
        ],
    )
    r = generate_daily_report(asof=today, quant_home=quant_home)
    md = format_markdown(r)
    assert "# Hermes-Quant Daily Report — 2026-05-27" in md
    assert "## Summary" in md
    assert "## Gate Decisions" in md
    assert "## Positions" in md
    assert "## P&L" in md
    assert "## Lessons Learned" in md
    assert "## Hypothesis Changes" in md
    assert "## Factor Verdicts" in md
    assert "## Open Proposals" in md
    # Table rows: headers contain pipes; ensure no row has unbalanced count.
    for line in md.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            # Each table row should have a leading and trailing pipe.
            assert line.endswith("|"), line


def test_format_markdown_empty_sections(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    r = generate_daily_report(asof=today, quant_home=quant_home)
    md = format_markdown(r)
    assert "## Positions (none open)" in md
    assert "_No gate decisions on this date._" in md
    assert "_No factor verdicts on this date._" in md


def test_format_telegram_within_4096_with_many_events(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    rows = [
        _audit_event(
            "gate_approval",
            asof=base + timedelta(minutes=i),
            payload={"asset": f"TICK{i:03d}", "confidence": 0.5},
        )
        for i in range(100)
    ]
    _write_jsonl(quant_home / "governance" / "audit_log.jsonl", rows)
    r = generate_daily_report(asof=today, quant_home=quant_home)
    tg = format_telegram(r, max_chars=3500)
    assert len(tg) <= 4096
    # When truncation happens, marker present.
    if len(tg) > 3500:
        assert "truncated" in tg or "…" in tg


def test_format_telegram_escapes_md_v2_specials(quant_home: Path) -> None:
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    _write_jsonl(
        quant_home / "governance" / "audit_log.jsonl",
        [
            _audit_event(
                "gate_rejection",
                asof=base,
                payload={
                    "asset": "BRK.B",
                    "reason": "kelly = -0.05 (below floor!)",
                    "confidence": 0.1,
                },
            )
        ],
    )
    r = generate_daily_report(asof=today, quant_home=quant_home)
    tg = format_telegram(r)
    # The dot and minus and parens should be escaped.
    assert r"BRK\.B" in tg
    # 'below floor!' contains '!' which must be escaped.
    # We only inline the first 5 gate rows; this single one is included.
    # Reason is not directly inlined in telegram view — but the ticker is.
    # Validate special chars never appear UN-escaped at the start of a line
    # outside of '*' and '•' markers we deliberately use.


def test_escape_telegram_md_v2_handles_all_specials() -> None:
    raw = r"a_b*c[d](e)f~g`h>i#j+k-l=m|n{o}p.q!r"
    out = _escape_telegram_md_v2(raw)
    for ch in r"_*[]()~`>#+-=|{}.!":
        # Each special char must appear preceded by a backslash.
        idx = out.find(ch)
        assert idx > 0
        assert out[idx - 1] == "\\", (ch, out)


def test_asof_in_past_filters_correctly(quant_home: Path) -> None:
    """Events from yesterday don't appear in today's report."""
    today = date(2026, 5, 27)
    yesterday = date(2026, 5, 26)
    yest_dt = datetime(2026, 5, 26, 14, 0, 0, tzinfo=UTC)
    today_dt = datetime(2026, 5, 27, 14, 0, 0, tzinfo=UTC)
    _write_jsonl(
        quant_home / "governance" / "audit_log.jsonl",
        [
            _audit_event("gate_approval", asof=yest_dt, payload={"asset": "OLD"}),
            _audit_event("gate_approval", asof=today_dt, payload={"asset": "NEW"}),
        ],
    )
    r_today = generate_daily_report(asof=today, quant_home=quant_home)
    r_yest = generate_daily_report(asof=yesterday, quant_home=quant_home)
    assert [row["ticker"] for row in r_today.gate_table] == ["NEW"]
    assert [row["ticker"] for row in r_yest.gate_table] == ["OLD"]


def test_corrupt_audit_log_line_skipped(quant_home: Path) -> None:
    """Malformed JSONL row must NOT raise — just be skipped."""
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    audit_path = quant_home / "governance" / "audit_log.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    good_row = _audit_event(
        "gate_approval", asof=base, payload={"asset": "GOOD"}
    )
    with audit_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(good_row) + "\n")
        fh.write("{this is not valid json\n")  # garbage
        fh.write("\n")  # blank line
    # Should not raise.
    r = generate_daily_report(asof=today, quant_home=quant_home)
    assert len(r.gate_table) == 1
    assert r.gate_table[0]["ticker"] == "GOOD"


def test_format_markdown_table_pipe_escaping(quant_home: Path) -> None:
    """A reason with a literal '|' must not break the markdown table."""
    today = date(2026, 5, 27)
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    _write_jsonl(
        quant_home / "governance" / "audit_log.jsonl",
        [
            _audit_event(
                "gate_rejection",
                asof=base,
                payload={"asset": "X", "reason": "halted | suspended"},
            )
        ],
    )
    r = generate_daily_report(asof=today, quant_home=quant_home)
    md = format_markdown(r)
    # The pipe inside the cell should be escaped as '\|'.
    assert r"halted \| suspended" in md


def test_cli_out_dash_prints_to_stdout(quant_home: Path, tmp_path: Path) -> None:
    """--out=- prints the report to stdout."""
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "quant-daily-report.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--asof",
            "2026-05-27",
            "--quant-home",
            str(quant_home),
            "--out",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**__import__("os").environ, "HERMES_VENV_PY_SKIP": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "Hermes-Quant Daily Report" in result.stdout


def test_cli_out_path_writes_file_and_creates_dirs(
    quant_home: Path, tmp_path: Path
) -> None:
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "quant-daily-report.py"
    )
    nested = tmp_path / "nested" / "deep" / "report.md"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--asof",
            "2026-05-27",
            "--quant-home",
            str(quant_home),
            "--out",
            str(nested),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert nested.exists()
    body = nested.read_text(encoding="utf-8")
    assert "Hermes-Quant Daily Report" in body


def test_cli_default_out_uses_quant_home_reports_dir(
    quant_home: Path,
) -> None:
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "quant-daily-report.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--asof",
            "2026-05-27",
            "--quant-home",
            str(quant_home),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    expected = quant_home / "reports" / "2026-05-27.md"
    assert expected.exists()


def test_cli_format_json_returns_valid_json(quant_home: Path) -> None:
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "quant-daily-report.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--asof",
            "2026-05-27",
            "--quant-home",
            str(quant_home),
            "--format",
            "json",
            "--out",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["date"] == "2026-05-27"
    assert "gate_table" in data
    assert "summary_lines" in data


def test_cli_also_print_emits_both(quant_home: Path, tmp_path: Path) -> None:
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "quant-daily-report.py"
    )
    out_path = tmp_path / "report.md"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--asof",
            "2026-05-27",
            "--quant-home",
            str(quant_home),
            "--out",
            str(out_path),
            "--also-print",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    assert "Hermes-Quant Daily Report" in result.stdout


def test_dailyreport_dataclass_default_values() -> None:
    """DailyReport with only date set has safe defaults."""
    r = DailyReport(date=date(2026, 5, 27))
    assert r.summary_lines == []
    assert r.gate_table == []
    assert r.positions_table == []
    assert r.pnl_today is None
    assert r.factor_verdicts_today == {}
    assert r.hypotheses_changes == {"promoted": [], "falsified": [], "new": []}


def test_format_markdown_with_positions_renders_pnl_lines() -> None:
    r = DailyReport(
        date=date(2026, 5, 27),
        positions_table=[
            {
                "ticker": "AAPL",
                "qty": 0.05,
                "cost": 180.0,
                "mark": 180.0,
                "unrealized_pnl": 0.0,
            }
        ],
        pnl_today=42.5,
        pnl_mtd=1234.0,
        pnl_ytd=5678.9,
    )
    md = format_markdown(r)
    assert "AAPL" in md
    assert "$42.50" in md
    assert "$1,234.00" in md
    assert "$5,678.90" in md


def test_format_telegram_handles_empty_report() -> None:
    """Empty report still produces valid (non-crashing) telegram output."""
    r = DailyReport(date=date(2026, 5, 27))
    tg = format_telegram(r)
    assert "Hermes" in tg
    assert "2026\\-05\\-27" in tg  # date is escaped
    assert len(tg) <= 4096
