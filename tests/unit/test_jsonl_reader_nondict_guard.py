"""Regression: library JSONL readers skip non-dict / corrupt lines, never raise.

Pins the silence-by-default guard added in batch-1 (bf78093) + batch-2: a json.loads
of an append-only log line that yields a valid NON-dict (a bare int/str/list from a
corrupt or partial append) — or a non-JSON line — must be SKIPPED, not dereferenced as
a row. Several of these readers (audit_log, approvals, decisions) previously had the
TypeError UNCAUGHT, so a single corrupt line crashed a governance / reflection / audit
read at runtime. This locks the guard so a future refactor can't silently reintroduce it.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

# A line set every reader must survive: a valid dict, then non-dict valid-JSON lines,
# a non-JSON corrupt line, and a trailing valid dict. The reader must return exactly the
# 2 dict rows and never raise.
_GOOD_A = {"decision_id": "D1", "token_id": "T1", "schema_version": 1, "kind": "x"}
_GOOD_B = {"decision_id": "D2", "token_id": "T2", "schema_version": 1, "kind": "y"}
_BAD_LINES = ["123", '"a-bare-string"', "[1, 2, 3]", "true", "{not valid json"]


def _write_store(tmp_path: Path, name: str, good_rows: list[dict]) -> Path:
    p = tmp_path / name
    lines = [json.dumps(good_rows[0]), *_BAD_LINES, json.dumps(good_rows[1])]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_decisions_read_all_skips_non_dict(tmp_path):
    from hermes_quant.memory.decisions import DecisionLog

    p = _write_store(tmp_path, "decisions.jsonl", [_GOOD_A, _GOOD_B])
    rows = list(DecisionLog(path=p).read_all())  # must NOT raise
    assert [r["decision_id"] for r in rows] == ["D1", "D2"]
    assert all(isinstance(r, dict) for r in rows)


def test_audit_log_read_skips_non_dict(tmp_path, monkeypatch):
    from hermes_quant.governance import audit_log

    # Build two REAL events (the row schema read() expects), then splice the
    # non-dict/corrupt lines between them. The guard must skip the bad lines and read()
    # must return the 2 real events without raising (the bad lines must NOT be mistaken
    # for a schema mismatch — the guard runs BEFORE the schema_version check).
    p = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    ev = audit_log.GovernanceEvent(kind="gate_approval", asof=datetime(2026, 1, 1, tzinfo=UTC), source="test")
    ev2 = audit_log.GovernanceEvent(kind="fill", asof=datetime(2026, 1, 2, tzinfo=UTC), source="test")
    p.write_text("\n".join([ev.model_dump_json(), *_BAD_LINES, ev2.model_dump_json()]) + "\n", encoding="utf-8")
    events = list(audit_log.read())  # must NOT raise on the 5 non-dict/corrupt lines
    assert len(events) == 2


def test_approvals_iter_rows_skips_non_dict(tmp_path, monkeypatch):
    from hermes_quant.governance import approvals

    # _consumed_ids filters on row_type=="consumed"; tag the good rows so the downstream
    # consumer (which derefs row['token_id']) is exercised across the non-dict lines.
    good_a = {**_GOOD_A, "row_type": "consumed"}
    good_b = {**_GOOD_B, "row_type": "consumed"}
    p = _write_store(tmp_path, "tokens.jsonl", [good_a, good_b])
    monkeypatch.setattr(approvals, "TOKEN_STORE_PATH", p)
    rows = list(approvals._iter_rows())  # must NOT raise
    assert {r["token_id"] for r in rows} == {"T1", "T2"}
    # the downstream consumer (does row['token_id']) must also run clean across bad lines
    assert approvals._consumed_ids() == {"T1", "T2"}


def test_hypothesis_iter_rows_skips_non_dict(tmp_path, monkeypatch):
    from hermes_quant.research import hypothesis

    p = _write_store(tmp_path, "hypotheses.jsonl",
                     [{**_GOOD_A, "hypothesis_id": "H1"}, {**_GOOD_B, "hypothesis_id": "H2"}])
    monkeypatch.setattr(hypothesis, "HYPOTHESES_PATH", p)
    rows = list(hypothesis.HypothesisRegistry()._iter_rows())  # must NOT raise
    assert all(isinstance(r, dict) for r in rows) and len(rows) == 2


def test_run_card_iter_rows_skips_non_dict(tmp_path, monkeypatch):
    from hermes_quant.research import run_card

    p = _write_store(tmp_path, "run_cards.jsonl",
                     [{**_GOOD_A, "run_id": "R1"}, {**_GOOD_B, "run_id": "R2"}])
    monkeypatch.setattr(run_card, "RUN_CARDS_PATH", p)
    rows = list(run_card.RunCardLog()._iter_rows())  # must NOT raise
    assert all(isinstance(r, dict) for r in rows) and len(rows) == 2


@pytest.mark.parametrize("only_bad", [True, False])
def test_decisions_all_bad_lines_yield_nothing_no_raise(tmp_path, only_bad):
    """A store of ONLY non-dict/corrupt lines yields zero rows and never raises."""
    from hermes_quant.memory.decisions import DecisionLog

    p = tmp_path / "d.jsonl"
    body = _BAD_LINES if only_bad else [*_BAD_LINES, json.dumps(_GOOD_A)]
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    rows = list(DecisionLog(path=p).read_all())
    assert len(rows) == (0 if only_bad else 1)
