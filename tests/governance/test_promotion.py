"""Tests for hermes_quant.governance.promotion (ADR-0031 D5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.governance import audit_log, promotion
from hermes_quant.governance.audit_log import GovernanceEvent


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


# Anchor for "now". asof in tests is this constant; we synthesize events at
# offsets relative to it.
NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)


def _seed_passing_run(asof: datetime, n_outcomes: int = 100) -> None:
    """Write enough synthetic events that every threshold passes."""
    for i in range(n_outcomes):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=asof - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0 + (i % 3) * 0.1},
            )
        )

    # Calibrator + sharpe + drawdown + retro-readiness snapshot
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=asof - timedelta(days=1),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.02,
                "sharpe_95ci_lower": 1.25,
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": True,
            },
        )
    )


def test_promotion_gate_blocks_when_outcomes_below_adr29_threshold(
    audit_path: Path,
) -> None:
    """99 settled outcomes → blocked."""
    _seed_passing_run(NOW, n_outcomes=99)
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("paper_outcomes_count=99" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_calibrator_drift_gt_5pct(
    audit_path: Path,
) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=NOW - timedelta(hours=1),
            source="weekly_retro",
            payload={"calibrator_drift": 0.06},
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("calibrator_drift_max" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_immutable_breach_count_nonzero(
    audit_path: Path,
) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    audit_log.append(
        GovernanceEvent(
            kind="gate_rejection",
            asof=NOW - timedelta(days=10),
            source="risk_gate",
            payload={"reason": "net_delta_cap", "immutable_breach": True},
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("immutable_breaches_in_window" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_killswitch_in_14d_window(
    audit_path: Path,
) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    audit_log.append(
        GovernanceEvent(
            kind="kill_switch_fired",
            asof=NOW - timedelta(days=5),
            source="risk_gate",
            payload={"reason": "drawdown"},
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("kill switch fired" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_retro_readiness_false(
    audit_path: Path,
) -> None:
    # Seed everything except the retro-readiness snapshot
    for i in range(100):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=NOW - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0},
            )
        )
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=NOW - timedelta(days=1),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.01,
                "sharpe_95ci_lower": 1.5,
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": False,
            },
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("weekly_retro_promotion_readiness" in r for r in decision.blocked_by)


def test_promotion_gate_passes_when_all_thresholds_pass(audit_path: Path) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    decision = promotion.evaluate(NOW)
    assert decision.promoted is True, f"blocked_by={decision.blocked_by}"
    assert decision.blocked_by == []
    assert decision.paper_outcomes_count >= 100
    assert decision.no_killswitch_in_trailing_14d is True
    assert decision.weekly_retro_promotion_readiness is True


def test_promotion_gate_blocks_when_sharpe_ci_degrades_within_window(
    audit_path: Path,
) -> None:
    """A degrading sharpe_95ci_lower window must BLOCK (archaeology finding):
    the gate is `metrics['sharpe_95ci_lower'] < min` so the reducer must reflect the
    CURRENT (latest) snapshot, not the window's single best moment. A run that was
    once healthy (1.5) but has since degraded below the 1.0 floor (0.2 then 0.1) must
    NOT be promotable. A max() reducer would pick 1.5 and wrongly pass — the fail-open."""
    for i in range(100):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=NOW - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0},
            )
        )
    # Three in-window snapshots, sharpe degrading 1.5 -> 0.2 -> 0.1 (latest is worst).
    for days_ago, sharpe in ((5, 1.5), (3, 0.2), (1, 0.1)):
        audit_log.append(
            GovernanceEvent(
                kind="promotion_event",
                asof=NOW - timedelta(days=days_ago),
                source="weekly_retro",
                payload={
                    "calibrator_drift": 0.01,
                    "sharpe_95ci_lower": sharpe,
                    "rolling_30d_max_drawdown_pct": 0.005,
                    "weekly_retro_promotion_readiness": True,
                },
            )
        )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False, (
        "a sharpe_95ci_lower that degraded to 0.1 (< 1.0 floor) must block promotion; "
        "a max() reducer would pick the stale 1.5 and fail OPEN"
    )
    assert any("sharpe_95ci_lower" in r for r in decision.blocked_by), decision.blocked_by


def test_promotion_gate_sharpe_uses_latest_not_max_when_improving(
    audit_path: Path,
) -> None:
    """Symmetric guard: when the LATEST snapshot is healthy (improved 0.2 -> 1.5),
    the latest-reducer must use 1.5 and PASS the sharpe leg — confirming the fix uses
    the most-recent snapshot, not min() (which would wrongly pick 0.2 and block)."""
    _seed_passing_run(NOW, n_outcomes=100)  # includes a 1.25 snapshot at day-1
    # An EARLIER, worse snapshot that must NOT drag the latest down.
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=NOW - timedelta(days=10),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.01,
                "sharpe_95ci_lower": 0.2,
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": True,
            },
        )
    )
    decision = promotion.evaluate(NOW)
    # The latest in-window sharpe snapshot is 1.25 (day-1) >= 1.0 -> the sharpe leg
    # must NOT be a blocker (the stale 0.2 at day-10 must not block).
    assert not any("sharpe_95ci_lower" in r for r in decision.blocked_by), decision.blocked_by


def test_promotion_gate_emits_audit_event(audit_path: Path) -> None:
    _seed_passing_run(NOW, n_outcomes=100)

    before_rows = list(audit_log.read(kinds=["promotion_event"]))
    n_before = sum(1 for r in before_rows if r.payload.get("row_type") == "evaluate_result")

    promotion.evaluate(NOW)

    after_rows = list(audit_log.read(kinds=["promotion_event"]))
    n_after = sum(1 for r in after_rows if r.payload.get("row_type") == "evaluate_result")
    assert n_after == n_before + 1


def test_load_thresholds_returns_react_live_values() -> None:
    """react.live HAS LANDED — `_load_thresholds()` reads the live binding,
    which mirrors ADR-0029 D7 verbatim. There is no local fallback copy of
    these numbers anymore (ADR-0031 D5: duplication is the failure mode)."""
    thresholds = promotion._load_thresholds()
    assert thresholds["min_paper_outcomes"] == 100
    assert thresholds["min_sharpe_95ci_lower"] == 1.0
    assert thresholds["max_rolling_30d_drawdown_pct"] == 0.01
    assert thresholds["max_calibrator_drift"] == 0.05
    assert thresholds["killswitch_window_days"] == 14


def test_promotion_threshold_path_actually_uses_react_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration test: `_load_thresholds()` returns the dict from
    react.live, not a local copy. We verify by mutating react.live's dict
    and observing the change flow through."""
    from hermes_quant.react import live as react_live

    # Sanity: react.live is present and exports the handle.
    assert hasattr(react_live, "LIVE_APPROVAL_THRESHOLDS")

    # The wire works: bumping a number in react.live shows up here.
    # monkeypatch.setitem auto-restores the original after the test.
    monkeypatch.setitem(react_live.LIVE_APPROVAL_THRESHOLDS, "min_paper_outcomes", 999)
    thresholds = promotion._load_thresholds()
    assert thresholds["min_paper_outcomes"] == 999, (
        "Binding isn't wired — _load_thresholds is not pulling the live "
        "value from react.live.LIVE_APPROVAL_THRESHOLDS."
    )


def test_promotion_threshold_keys_match_react_live() -> None:
    """Every key this evaluator depends on must exist in react.live. The
    required set is the module's own `_REQUIRED_THRESHOLD_KEYS`, so this
    test stays in lockstep with what `evaluate()` actually reads — a future
    key rename in react.live fails HERE instead of failing open in prod."""
    from hermes_quant.react import live as react_live

    actual = set(react_live.LIVE_APPROVAL_THRESHOLDS.keys())
    missing = promotion._REQUIRED_THRESHOLD_KEYS - actual
    assert not missing, (
        f"react.live.LIVE_APPROVAL_THRESHOLDS is missing keys that "
        f"governance.promotion depends on: {sorted(missing)}. Either add "
        f"them to react.live or update promotion._REQUIRED_THRESHOLD_KEYS."
    )


def test_load_thresholds_fails_closed_when_react_live_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old `_LATE_BIND_THRESHOLDS` fallback is GONE (option (a)). If
    react.live cannot be imported, the gate must fail CLOSED — raise, never
    promote on guessed numbers. Setting sys.modules[...] = None forces the
    in-function import to raise ImportError."""
    import sys

    monkeypatch.setitem(sys.modules, "hermes_quant.react.live", None)
    with pytest.raises(RuntimeError, match="single source of truth"):
        promotion._load_thresholds()


def test_load_thresholds_fails_closed_when_required_key_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If react.live drops a key this evaluator reads, fail CLOSED rather
    than silently substitute a default. A missing key sliding through would
    read as a too-lenient (or absent) bound — a fail-OPEN we must prevent."""
    from hermes_quant.react import live as react_live

    truncated = {
        k: v
        for k, v in react_live.LIVE_APPROVAL_THRESHOLDS.items()
        if k != "min_paper_outcomes"
    }
    monkeypatch.setattr(react_live, "LIVE_APPROVAL_THRESHOLDS", truncated)
    with pytest.raises(RuntimeError, match="missing keys"):
        promotion._load_thresholds()


def test_load_thresholds_fails_closed_when_export_not_a_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-dict / empty export is also a contract breach → raise."""
    from hermes_quant.react import live as react_live

    monkeypatch.setattr(react_live, "LIVE_APPROVAL_THRESHOLDS", None)
    with pytest.raises(RuntimeError, match="non-empty dict"):
        promotion._load_thresholds()


@pytest.mark.parametrize(
    "poisoned_value",
    [
        0,  # `paper_outcomes < 0` never blocks → fail-OPEN
        -50,  # negative bound → fail-OPEN
        float("nan"),  # `x < NaN` is always False → fail-OPEN
        float("inf"),  # non-finite bound is meaningless
        "100",  # non-numeric: would crash int()/float() by luck, not design
        None,  # non-numeric
        True,  # bool is an int subclass — must NOT be accepted as a count/bound
    ],
)
def test_load_thresholds_fails_closed_on_degenerate_value(
    monkeypatch: pytest.MonkeyPatch, poisoned_value: object
) -> None:
    """A required key PRESENT but with a degenerate value must fail CLOSED.

    This guards the subtlest fail-OPEN: `min_paper_outcomes=0` or
    `min_sharpe_95ci_lower=NaN` would sail through a key-presence-only check
    and flip the gate OPEN, because evaluate() blocks via `metric < threshold`
    and `x < 0` / `x < NaN` never fires. The loader must reject the bound
    before it can poison a decision."""
    from hermes_quant.react import live as react_live

    poisoned = dict(react_live.LIVE_APPROVAL_THRESHOLDS)
    poisoned["min_paper_outcomes"] = poisoned_value
    monkeypatch.setattr(react_live, "LIVE_APPROVAL_THRESHOLDS", poisoned)
    with pytest.raises(RuntimeError):
        promotion._load_thresholds()


def test_evaluate_blocks_low_sharpe_even_though_drawdown_uses_gt(
    audit_path: Path,
) -> None:
    """End-to-end fail-OPEN regression: with valid ADR-0029 bounds, a run
    whose sharpe is below 1.0 MUST be blocked. This is the live counterpart
    to the degenerate-value unit test — it proves the `<`-comparison bound is
    actually load-bearing on a real evaluate() path, so a future NaN/0 bound
    slipping past the loader would visibly break this test too."""
    # Seed a fully-passing run, then override sharpe to a sub-threshold value.
    for _ in range(100):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=NOW - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0},
            )
        )
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=NOW - timedelta(days=1),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.01,
                "sharpe_95ci_lower": 0.2,  # < 1.0 → must block
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": True,
            },
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("sharpe_95ci_lower" in r for r in decision.blocked_by)


def test_react_live_threshold_spellings_do_not_diverge() -> None:
    """KEY-SHAPE PARITY GUARD (the single most likely real bug).

    react.live carries every authoritative number under TWO spellings — a
    suffix style (its own primary naming) and a prefix style (the one this
    evaluator reads). They are hand-maintained copies, so editing one and
    forgetting the other would silently feed the gate a stale threshold.
    This test FAILS the instant the two spellings of the same bound diverge,
    and pins both to ADR-0029 D7's verbatim numbers."""
    from hermes_quant.react import live as react_live

    t = react_live.LIVE_APPROVAL_THRESHOLDS

    # (suffix_spelling, prefix_spelling, ADR-0029 D7 authoritative value)
    synonyms = [
        ("paper_outcomes_count_min", "min_paper_outcomes", 100),
        ("sharpe_95ci_lower_min", "min_sharpe_95ci_lower", 1.0),
        ("rolling_30d_max_drawdown_pct_max", "max_rolling_30d_drawdown_pct", 0.01),
        ("calibrator_drift_max", "max_calibrator_drift", 0.05),
    ]
    for suffix_key, prefix_key, adr_value in synonyms:
        assert t[suffix_key] == t[prefix_key], (
            f"react.live spellings diverged: {suffix_key}={t[suffix_key]} "
            f"!= {prefix_key}={t[prefix_key]}. Edit BOTH or neither."
        )
        assert t[prefix_key] == adr_value, (
            f"react.live.{prefix_key}={t[prefix_key]} no longer matches "
            f"ADR-0029 D7 (={adr_value}). react.live is the source of truth; "
            f"if ADR-0029 amended this, update react.live AND this test."
        )

    # The window-length keys have no duplicate spelling; pin them to ADR-0029.
    assert t["killswitch_window_days"] == 14
    assert t["immutable_breach_window_days"] == 30
