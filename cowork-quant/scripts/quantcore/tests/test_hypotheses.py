"""Hypothesis registry: roundtrip, chain integrity, Brier scoring, lifecycle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
UTC = timezone.utc

import pytest

from quantcore.hypotheses import (
    Forecast,
    Hypothesis,
    HypothesisRegistry,
    new_forecast_id,
    new_hypothesis_id,
)

ASOF = datetime(2026, 6, 9, 14, 0, tzinfo=UTC)


def _seed(reg: HypothesisRegistry, statement="AAPL momentum persists 5d after earnings beats"):
    hyp = reg.create(statement, created_at=ASOF)
    fc = reg.forecast(hyp.hypothesis_id, p=0.7, horizon="5d", made_at=ASOF)
    return hyp, fc


# -- create / read roundtrip ---------------------------------------------------


def test_create_read_roundtrip(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, fc = _seed(reg)
    # fresh instance reads the same materialized state from disk
    reg2 = HypothesisRegistry(tmp_path)
    got = reg2.get(hyp.hypothesis_id)
    assert got is not None
    assert got.statement == hyp.statement
    assert got.status == "open"
    assert got.created_at == ASOF
    assert len(got.forecasts) == 1
    assert got.forecasts[0].forecast_id == fc.forecast_id
    assert got.forecasts[0].p == 0.7
    assert got.forecasts[0].horizon == "5d"
    assert got.forecasts[0].resolved is False
    assert got.forecasts[0].brier is None


def test_duplicate_hypothesis_id_refused(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, _ = _seed(reg)
    with pytest.raises(ValueError, match="already registered"):
        reg.create("another claim", hypothesis_id=hyp.hypothesis_id)


def test_model_validation():
    with pytest.raises(ValueError):
        Hypothesis(hypothesis_id="short", statement="x", created_at=ASOF)  # id < 8 chars
    with pytest.raises(ValueError):
        Hypothesis(hypothesis_id=new_hypothesis_id(), statement="y" * 501, created_at=ASOF)
    with pytest.raises(ValueError):
        Forecast(forecast_id=new_forecast_id(), p=1.2, horizon="5d", made_at=ASOF)
    with pytest.raises(ValueError):  # naive datetime refused
        Hypothesis(
            hypothesis_id=new_hypothesis_id(), statement="z", created_at=datetime(2026, 6, 9)
        )


# -- chain integrity -----------------------------------------------------------


def test_chain_verifies_and_detects_tamper(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, fc = _seed(reg)
    reg.resolve_forecast(hyp.hypothesis_id, fc.forecast_id, outcome=True)
    ok, msg = reg.verify_chain()
    assert ok, msg
    # tamper with a middle line
    lines = reg.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["forecast"]["p"] = 0.99  # rewrite history: claim a sharper forecast
    lines[1] = json.dumps(rec, sort_keys=True)
    reg.path.write_text("\n".join(lines) + "\n")
    ok, msg = reg.verify_chain()
    assert not ok


# -- Brier scoring -------------------------------------------------------------


def test_brier_p07_outcome_true(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, fc = _seed(reg)
    resolved = reg.resolve_forecast(hyp.hypothesis_id, fc.forecast_id, outcome=True)
    assert resolved.brier == pytest.approx(0.09)
    # persisted, not just returned
    got = HypothesisRegistry(tmp_path).get(hyp.hypothesis_id)
    assert got.forecasts[0].resolved is True
    assert got.forecasts[0].outcome is True
    assert got.forecasts[0].brier == pytest.approx(0.09)


def test_brier_p07_outcome_false(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, fc = _seed(reg)
    resolved = reg.resolve_forecast(hyp.hypothesis_id, fc.forecast_id, outcome=False)
    assert resolved.brier == pytest.approx(0.49)
    got = HypothesisRegistry(tmp_path).get(hyp.hypothesis_id)
    assert got.forecasts[0].outcome is False
    assert got.forecasts[0].brier == pytest.approx(0.49)


def test_double_resolve_refused(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, fc = _seed(reg)
    reg.resolve_forecast(hyp.hypothesis_id, fc.forecast_id, outcome=True)
    n_events = len(reg.events())
    with pytest.raises(ValueError, match="already resolved"):
        reg.resolve_forecast(hyp.hypothesis_id, fc.forecast_id, outcome=False)
    # no event appended, Brier unchanged
    assert len(reg.events()) == n_events
    assert reg.get(hyp.hypothesis_id).forecasts[0].brier == pytest.approx(0.09)


def test_resolve_unknown_refused(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, _ = _seed(reg)
    with pytest.raises(ValueError, match="unknown hypothesis"):
        reg.resolve_forecast("nope-nope-nope", "whatever-id", outcome=True)
    with pytest.raises(ValueError, match="no forecast"):
        reg.resolve_forecast(hyp.hypothesis_id, "missing-forecast", outcome=True)


# -- status lifecycle ----------------------------------------------------------


def test_status_transitions_valid(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, _ = _seed(reg)
    reg.set_status(hyp.hypothesis_id, "supported", note="3/4 forecasts hit")
    assert reg.get(hyp.hypothesis_id).status == "supported"
    reg.set_status(hyp.hypothesis_id, "retired")
    assert reg.get(hyp.hypothesis_id).status == "retired"


def test_status_invalid_transitions_refused(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    hyp, _ = _seed(reg)
    with pytest.raises(ValueError, match="invalid status transition"):
        reg.set_status(hyp.hypothesis_id, "open")  # open -> open
    reg.set_status(hyp.hypothesis_id, "refuted")
    with pytest.raises(ValueError, match="invalid status transition"):
        reg.set_status(hyp.hypothesis_id, "supported")  # refuted -> supported
    reg.set_status(hyp.hypothesis_id, "retired")
    with pytest.raises(ValueError, match="invalid status transition"):
        reg.set_status(hyp.hypothesis_id, "open")  # retired is terminal
    # retired hypotheses accept no new forecasts
    with pytest.raises(ValueError, match="retired"):
        reg.forecast(hyp.hypothesis_id, p=0.5, horizon="1d")


def test_open_hypotheses_and_link_proposal(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    h1, _ = _seed(reg, "claim one is falsifiable")
    h2 = reg.create("claim two is falsifiable", created_at=ASOF)
    reg.set_status(h2.hypothesis_id, "refuted")
    assert [h.hypothesis_id for h in reg.open_hypotheses()] == [h1.hypothesis_id]
    reg.link_proposal(h1.hypothesis_id, "prop-abc-123")
    reg.link_proposal(h1.hypothesis_id, "prop-abc-123")  # idempotent, no dup
    got = reg.get(h1.hypothesis_id)
    assert got.linked_proposal_ids == ["prop-abc-123"]


# -- brier_summary -------------------------------------------------------------


def test_brier_summary_aggregates(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    h1 = reg.create("hypothesis alpha", created_at=ASOF)
    h2 = reg.create("hypothesis beta", created_at=ASOF)
    f1 = reg.forecast(h1.hypothesis_id, p=0.7, horizon="5d", made_at=ASOF)
    f2 = reg.forecast(h1.hypothesis_id, p=0.9, horizon="5d", made_at=ASOF)
    f3 = reg.forecast(h2.hypothesis_id, p=0.2, horizon="1w", made_at=ASOF)
    reg.resolve_forecast(h1.hypothesis_id, f1.forecast_id, outcome=True)  # 0.09
    reg.resolve_forecast(h1.hypothesis_id, f2.forecast_id, outcome=False)  # 0.81
    reg.resolve_forecast(h2.hypothesis_id, f3.forecast_id, outcome=False)  # 0.04
    s = reg.brier_summary()
    assert s["per_hypothesis"][h1.hypothesis_id]["count"] == 2
    assert s["per_hypothesis"][h1.hypothesis_id]["mean_brier"] == pytest.approx(0.45)
    assert s["per_hypothesis"][h2.hypothesis_id]["count"] == 1
    assert s["per_hypothesis"][h2.hypothesis_id]["mean_brier"] == pytest.approx(0.04)
    assert s["overall"]["count"] == 3
    assert s["overall"]["mean_brier"] == pytest.approx((0.09 + 0.81 + 0.04) / 3)


def test_unresolved_forecasts_excluded_from_summary(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    h1, f1 = _seed(reg, "resolved hypothesis")
    h2 = reg.create("never resolved hypothesis", created_at=ASOF)
    reg.forecast(h2.hypothesis_id, p=0.5, horizon="2d", made_at=ASOF)  # stays open
    reg.forecast(h1.hypothesis_id, p=0.8, horizon="3d", made_at=ASOF)  # also unresolved
    reg.resolve_forecast(h1.hypothesis_id, f1.forecast_id, outcome=True)
    s = reg.brier_summary()
    assert s["overall"]["count"] == 1
    assert s["overall"]["mean_brier"] == pytest.approx(0.09)
    assert s["per_hypothesis"][h1.hypothesis_id]["count"] == 1
    assert h2.hypothesis_id not in s["per_hypothesis"]


def test_brier_summary_empty(tmp_path):
    reg = HypothesisRegistry(tmp_path)
    s = reg.brier_summary()
    assert s["overall"] == {"mean_brier": None, "count": 0}
    assert s["per_hypothesis"] == {}
