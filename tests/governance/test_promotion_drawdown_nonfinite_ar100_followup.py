"""ar100 follow-up: a NON-FINITE drawdown-breaker magnitude must FAIL CLOSED.

ar100 (commit 221c983) derives ``rolling_30d_max_drawdown_pct`` from the
``drawdown_circuit_breaker_{pct}`` ``gate_rejection`` reason so the promotion gate's
drawdown block is no longer vacuous. ar101 (commit 4dfd0cb), the sibling drift
derivation, finite-guards every derived value (``if math.isfinite(d)``) — but ar100
does not.

The asymmetry is a latent fail-OPEN on the money gate. ``_drawdown_from_breaker_reason``
returns ``abs(float(...))`` with no finite check, so a ``drawdown_circuit_breaker_nan``
reason yields ``nan``. In ``_collect_metrics`` the merge is
``rolling_30d_max_drawdown_pct = max(0.0, nan)`` — and ``max(0.0, nan)`` returns
``0.0`` (CPython keeps the first arg when no later arg is strictly greater). The NaN is
SWALLOWED to 0.0, so the ``evaluate()`` ar41 finite-guard (which would BLOCK on a
non-finite drawdown) NEVER SEES IT, and the drawdown gate reads a clean 0.0 → promotion
is NOT blocked. (``inf`` is handled correctly because ``max(0.0, inf) = inf`` reaches
the guard; only ``nan`` is swallowed.)

The ADR-0031/ar41 doctrine for a money gate is explicit: a non-finite candidate metric
is un-evaluable and MUST block (fail-closed); finite-guard EVERY money input — a NaN
defeats every ``>``/``<`` comparison gate. The fix propagates a non-finite breaker
magnitude through to the metrics dict so the existing ``evaluate()`` finite-guard
blocks, instead of letting ``max()`` swallow the NaN to 0.0.

A legitimate producer (risk/gate.py Rule 1) finite-guards ``drawdown_pct`` before it
emits the reason, so this is not reachable on the happy path TODAY — but the gate must
not depend on a separate producer's guard to stay fail-closed. Self-contained; does not
import or edit the existing ar100 test module.
"""

from __future__ import annotations

import math

from hermes_quant.governance import promotion


def test_drawdown_breaker_reason_propagates_nonfinite_so_gate_can_fail_closed() -> None:
    """A NaN-encoded drawdown breaker magnitude must NOT be swallowed to 0.0 inside
    _collect_metrics' max() reduction — it must reach the metrics dict as non-finite so
    the evaluate() ar41 finite-guard can block it."""
    # Simulate the exact _collect_metrics reduction with a NaN-encoded breaker reason.
    breaker_dd = promotion._drawdown_from_breaker_reason("drawdown_circuit_breaker_nan")
    assert breaker_dd is None or not math.isfinite(breaker_dd), (
        "a nan-encoded breaker magnitude should be either dropped (None) or a non-finite "
        "float — never a finite value that silently merges away"
    )

    # The load-bearing property: whatever _drawdown_from_breaker_reason returns for a
    # non-finite magnitude, it must NOT let max() collapse a real non-finite drawdown to
    # a clean 0.0. We assert via the documented contract: a non-finite return is propagated
    # by the NaN-safe merge, OR None (dropped) — but if it is a float it must be non-finite.
    inf_dd = promotion._drawdown_from_breaker_reason("drawdown_circuit_breaker_inf")
    assert inf_dd is None or not math.isfinite(inf_dd)


def test_collect_metrics_nan_breaker_does_not_silently_read_as_zero(monkeypatch) -> None:
    """End-to-end through _collect_metrics: an audit-log gate_rejection carrying a
    nan-encoded drawdown breaker must NOT produce rolling_30d_max_drawdown_pct == 0.0
    (which would pass the drawdown gate). It must be non-finite (-> evaluate() blocks)
    OR the row dropped with no other drawdown evidence keeping it at the safe default
    only when there is genuinely no drawdown signal."""
    from datetime import UTC, datetime

    from hermes_quant.governance import audit_log

    asof = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

    class _Evt:
        def __init__(self, kind, payload, asof):
            self.kind = kind
            self.payload = payload
            self.asof = asof

    # One gate_rejection whose reason encodes a NaN drawdown, inside the 30d window.
    rows = [
        _Evt(
            "gate_rejection",
            {"reason": "drawdown_circuit_breaker_nan"},
            datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
        ),
    ]

    def _fake_read(kinds=None):
        for e in rows:
            if kinds is None or e.kind in kinds:
                yield e

    monkeypatch.setattr(audit_log, "read", _fake_read)
    # Neutralize the cross-plane drift-log read so it can't perturb the drawdown metric.
    monkeypatch.setattr(promotion, "_max_calibrator_drift_in_window", lambda *a, **k: 0.0)

    metrics = promotion._collect_metrics(asof)
    dd = metrics["rolling_30d_max_drawdown_pct"]
    # The defect: max(0.0, nan) == 0.0 -> the NaN drawdown reads as zero (fail-OPEN).
    # The fix: a non-finite breaker magnitude propagates as non-finite so evaluate()
    # fail-closes via the ar41 guard.
    assert not (isinstance(dd, float) and dd == 0.0 and math.isfinite(dd)), (
        "a nan-encoded drawdown circuit-breaker was silently swallowed to a clean 0.0 "
        "by max(0.0, nan) — the drawdown gate would NOT block (fail-OPEN). A non-finite "
        "drawdown must propagate so the evaluate() ar41 finite-guard blocks it."
    )
    assert not math.isfinite(dd), "the non-finite drawdown must reach the gate as non-finite"
