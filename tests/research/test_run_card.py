"""tests/research/test_run_card.py — RunCard + RunCardLog tests (ADR-0048).

Coverage:
 - RunCard model validation.
 - RunCardLog.record() round-trip.
 - RunCardLog.read() returns correct card.
 - RunCardLog.read_for_hypothesis() filters correctly.
 - Multiple RunCards for same hypothesis accumulated correctly.
 - Append-only: truncate() + update() raise AppendOnlyViolation.
 - Date fields serialise and deserialise correctly.
 - verdict_reasons max count enforcement.
 - verdict_reasons item max_length enforcement.
 - Metrics values must be numeric.
"""

from __future__ import annotations

from datetime import date

import pytest

from hermes_quant.research.run_card import AppendOnlyViolation, RunCard, RunCardLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_run_card(**overrides) -> RunCard:
    defaults = dict(
        hypothesis_id="hyp_AAPL_20250101_aabbcc",
        started_at="2025-01-01T10:00:00+00:00",
        ended_at="2025-03-31T10:00:00+00:00",
        strategy_name="SentimentMomentum",
        strategy_config_hash="a" * 64,
        universe=["AAPL", "MSFT"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        contamination_guard_fired=False,
        metrics={
            "sharpe": 0.82,
            "sortino": 1.1,
            "max_drawdown": -0.08,
            "vs_buyhold_alpha": 0.05,
            "n_decisions": 42.0,
            "total_return": 0.12,
        },
        artifacts={"backtest_log": "/tmp/backtest.jsonl"},
        verdict="validated",
        verdict_reasons=["[PASSED] sharpe >= 0.5 → True"],
    )
    defaults.update(overrides)
    return RunCard(**defaults)


@pytest.fixture
def log(tmp_path):
    return RunCardLog(path=tmp_path / "run_cards.jsonl")


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


def test_run_card_defaults():
    rc = _minimal_run_card()
    assert rc.run_id == ""  # filled by log.record()
    assert rc.contamination_guard_fired is False


def test_run_card_extra_forbid():
    with pytest.raises(Exception):
        _minimal_run_card(unknown_field="bad")


def test_run_card_verdict_reasons_too_many():
    with pytest.raises(Exception):
        _minimal_run_card(verdict_reasons=["r"] * 11)


def test_run_card_verdict_reason_too_long():
    with pytest.raises(Exception):
        _minimal_run_card(verdict_reasons=["x" * 513])


def test_run_card_metrics_non_numeric():
    with pytest.raises(Exception):
        _minimal_run_card(metrics={"sharpe": "not_a_float"})


def test_run_card_valid_verdicts():
    for v in ("validated", "falsified", "inconclusive"):
        rc = _minimal_run_card(verdict=v)
        assert rc.verdict == v


def test_run_card_invalid_verdict():
    with pytest.raises(Exception):
        _minimal_run_card(verdict="maybe")


# ---------------------------------------------------------------------------
# Log round-trip tests
# ---------------------------------------------------------------------------


def test_record_and_read(log):
    rc = _minimal_run_card()
    run_id = log.record(rc)
    assert run_id.startswith("run_hyp_AAPL_")
    recovered = log.read(run_id)
    assert recovered is not None
    assert recovered.hypothesis_id == rc.hypothesis_id
    assert recovered.verdict == rc.verdict
    assert recovered.strategy_name == rc.strategy_name


def test_read_unknown_returns_none(log):
    assert log.read("run_nonexistent_id") is None


def test_read_for_hypothesis_empty(log):
    assert log.read_for_hypothesis("hyp_GHOST_20250101_000000") == []


def test_read_for_hypothesis_multiple(log):
    rc1 = _minimal_run_card(hypothesis_id="hyp_AAPL_20250101_aabbcc", verdict="inconclusive")
    rc2 = _minimal_run_card(hypothesis_id="hyp_AAPL_20250101_aabbcc", verdict="validated")
    log.record(rc1)
    log.record(rc2)
    cards = log.read_for_hypothesis("hyp_AAPL_20250101_aabbcc")
    assert len(cards) == 2
    verdicts = [c.verdict for c in cards]
    assert "inconclusive" in verdicts
    assert "validated" in verdicts


def test_read_for_hypothesis_isolation(log):
    """RunCards for different hypotheses don't bleed into each other."""
    rc_a = _minimal_run_card(hypothesis_id="hyp_AAPL_20250101_aabbcc")
    rc_b = _minimal_run_card(hypothesis_id="hyp_MSFT_20250101_ccddee")
    log.record(rc_a)
    log.record(rc_b)
    assert len(log.read_for_hypothesis("hyp_AAPL_20250101_aabbcc")) == 1
    assert len(log.read_for_hypothesis("hyp_MSFT_20250101_ccddee")) == 1


def test_date_fields_round_trip(log):
    rc = _minimal_run_card(window_start=date(2024, 6, 1), window_end=date(2024, 11, 30))
    run_id = log.record(rc)
    recovered = log.read(run_id)
    assert recovered.window_start == date(2024, 6, 1)
    assert recovered.window_end == date(2024, 11, 30)


def test_metrics_round_trip(log):
    metrics = {
        "sharpe": 1.23,
        "sortino": 2.34,
        "max_drawdown": -0.15,
        "vs_buyhold_alpha": 0.07,
        "n_decisions": 55.0,
        "total_return": 0.18,
    }
    rc = _minimal_run_card(metrics=metrics)
    run_id = log.record(rc)
    recovered = log.read(run_id)
    assert recovered.metrics["sharpe"] == pytest.approx(1.23)
    assert recovered.metrics["sortino"] == pytest.approx(2.34)


# ---------------------------------------------------------------------------
# Append-only enforcement tests
# ---------------------------------------------------------------------------


def test_truncate_raises_append_only(log):
    with pytest.raises(AppendOnlyViolation):
        log.truncate()


def test_update_raises_append_only(log):
    with pytest.raises(AppendOnlyViolation):
        log.update()
