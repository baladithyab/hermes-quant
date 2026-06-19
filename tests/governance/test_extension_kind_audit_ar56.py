"""ar56 — extension-kind (*_llm_call) audit rows must not crash GovernanceEvent reconstruction.

Found by the parallel find->fix workflow (wf_77dde6fd). LLM producers raw-append rows whose `kind`
(e.g. trader_llm_call / risk_committee_llm_call / reflector_llm_call, ADR-0054) is NOT in VALID_KINDS
but IS stamped at the current schema_version, so the read-side version guard does not skip them.
audit_log.read() then reconstructs GovernanceEvent(kind=<extension>) — kind is a restricted Literal —
raising a pydantic ValidationError on the FIRST poison row. An UNFILTERED reader (kinds=None), notably
governance.promotion._collect_metrics, became permanently un-evaluable (the ADR-0031 D5 promotion gate
crashes) on a deployed log carrying thousands of such rows. Fix: skip-and-log extension kinds on the
read side (mirroring the corrupt-line skip); promotion._collect_metrics also reads a whitelist as
defense-in-depth.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.governance import audit_log, promotion
from hermes_quant.governance.audit_log import GovernanceEvent

NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


def _evt(kind: str, **payload) -> GovernanceEvent:
    return GovernanceEvent(kind=kind, asof=NOW, source="t", payload=payload)


def _raw_append_extension_kind(audit_path: Path, kind: str, asof: datetime = NOW) -> None:
    """Mirror the producers' raw append path: an extension kind NOT in VALID_KINDS but
    stamped at the CURRENT schema_version so the read-side version guard does not skip it."""
    row = {
        "event_id": f"ext-{kind}",
        "kind": kind,
        "schema_version": audit_log.CURRENT_SCHEMA_VERSION,
        "asof": asof.isoformat(),
        "source": "hermes_quant.agents.llm_caller",
        "payload": {"model": "x", "tokens": 10},
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a") as f:
        f.write(json.dumps(row) + "\n")


def test_ar56_read_unfiltered_skips_extension_kinds(audit_path: Path) -> None:
    audit_log.append(_evt("fill", broker="paper", realized_pnl=1.0))
    _raw_append_extension_kind(audit_path, "trader_llm_call")
    _raw_append_extension_kind(audit_path, "reflector_llm_call")
    audit_log.append(_evt("promotion_event", promoted=True))
    rows = list(audit_log.read())  # kinds=None — must NOT raise ValidationError
    assert sorted(r.kind for r in rows) == ["fill", "promotion_event"]


def test_ar56_read_filtered_immune_to_extension_kinds(audit_path: Path) -> None:
    audit_log.append(_evt("fill", broker="paper", realized_pnl=1.0))
    _raw_append_extension_kind(audit_path, "risk_committee_llm_call")
    rows = list(audit_log.read(kinds=["fill"]))
    assert len(rows) == 1 and rows[0].kind == "fill"


def _seed_passing_run(asof: datetime, n_outcomes: int = 100) -> None:
    for i in range(n_outcomes):
        audit_log.append(
            GovernanceEvent(kind="fill", asof=asof - timedelta(days=15), source="paper_reactor",
                            payload={"broker": "paper", "realized_pnl": 1.0 + (i % 3) * 0.1})
        )
    audit_log.append(
        GovernanceEvent(kind="promotion_event", asof=asof - timedelta(days=1), source="weekly_retro",
                        payload={"calibrator_drift": 0.02, "sharpe_95ci_lower": 1.25,
                                 "rolling_30d_max_drawdown_pct": 0.005,
                                 "weekly_retro_promotion_readiness": True})
    )


def test_ar56_promotion_collect_metrics_survives_extension_rows(audit_path: Path) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    _raw_append_extension_kind(audit_path, "trader_llm_call", NOW - timedelta(days=2))
    _raw_append_extension_kind(audit_path, "risk_committee_llm_call", NOW - timedelta(days=2))
    _raw_append_extension_kind(audit_path, "reflector_llm_call", NOW - timedelta(days=2))
    # The previously-crashing call: must succeed and ignore the extension rows.
    metrics = promotion._collect_metrics(NOW)
    assert metrics["paper_outcomes_count"] == 100
    decision = promotion.evaluate(NOW)
    assert decision.promoted is True, f"blocked_by={decision.blocked_by}"
