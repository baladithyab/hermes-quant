"""ADR-0092 Increment-2 — the SHADOW adoption seam (default-OFF, observe-only).

Increment 2 is the FIRST increment that touches LIVE wiring (advisor.recommend).
The safety model is SHADOW, NOT cutover:

  * The live gate at ``advisor.py`` (``action = risk_gate.gate(...)``) and its
    returned ``Action`` MUST continue to drive ``result.risk_gate`` UNCHANGED.
  * A new flag ``HERMES_QUANT_PDR_CORE_SHADOW`` (default-OFF) ADDITIONALLY runs
    the ported ``pdr_core`` gate in parallel, maps its ``GateDecision -> Action``,
    compares field-by-field to the LIVE action, and LOGS divergence.
  * Flag-OFF must be BYTE-IDENTICAL to today (the core gate is not even
    constructed; no shadow import is paid).
  * Flag-ON must NOT change the decision — only observe.
  * If the core gate or adapter raises, the shadow swallows it (best-effort) and
    the live action is unaffected.

This file is the safety proof for that seam. It covers BOTH the shell adapter
(``hermes_quant.pdr_core_adapter``) in isolation AND the live ``recommend()``
seam end-to-end (driven by a canned provider, no live IO).
"""

from __future__ import annotations

import logging

import pandas as pd

from hermes_quant.advisor import recommend
from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    MarketState,
    Portfolio,
    Position,
)

UTC_NOW = pd.Timestamp("2026-06-12T15:00:00+00:00")


# ===========================================================================
# Canned provider — drives recommend() with NO live IO (mirrors the
# _RecordingProvider pattern in tests/test_no_lookahead.py).
# ===========================================================================


def _make_bars(n: int = 120, *, base: float = 100.0, trend: float = 0.5, seed: int = 7) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(seed=seed)
    timestamps = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = base + np.arange(n) * trend + rng.normal(0, 0.5, n)
    opens = closes - rng.uniform(0, 0.3, n)
    highs = np.maximum(closes, opens) + rng.uniform(0, 0.4, n)
    lows = np.minimum(closes, opens) - rng.uniform(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


class _CannedProvider:
    name = "canned"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache: bool = True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of
            if cutoff.tzinfo is None:
                cutoff = cutoff.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _recommend_kwargs(provider, risk_gate=None):
    return dict(
        symbol="TEST",
        asset_class="equity",
        as_of="2026-03-15T00:00:00Z",
        provider=provider,
        risk_gate=risk_gate,
        include_lessons=False,
    )


# ===========================================================================
# A risk gate stub the adapter shadow will DISAGREE with on purpose.
# The live gate returns a fixed Action; the core gate (built from the live
# gate's .config) computes its own verdict from the SAME inputs. To force a
# guaranteed divergence we make the live gate emit a hand-crafted Action that
# the deterministic core gate would never produce for the synthetic flat
# portfolio + whatever signal the advisor aggregates.
# ===========================================================================


class _FixedActionGate:
    """A live-gate stand-in that returns a fixed Action regardless of inputs.

    Carries a real ``config`` (a live risk.gate.RiskConfig) so the adapter can
    mirror it into the core RiskConfig. The fixed Action is chosen to DIVERGE
    from whatever the deterministic core gate would emit.
    """

    def __init__(self, action: Action | None):
        from hermes_quant.risk.gate import RiskConfig as LiveRiskConfig

        self.config = LiveRiskConfig()
        self._action = action

    def gate(self, signal, market, portfolio, halt_state):
        return self._action


# ===========================================================================
# SECTION 1 — the shell adapter in isolation.
# ===========================================================================


def test_adapter_module_exports_the_public_surface():
    """The shell adapter exposes the four builders, the lifted map, and the
    shadow runner."""
    import hermes_quant.pdr_core_adapter as adapter

    for name in (
        "core_signal_from",
        "core_market_from",
        "core_portfolio_from",
        "core_risk_config_from",
        "gate_decision_to_action",
        "run_shadow_gate",
    ):
        assert hasattr(adapter, name), f"adapter missing {name}"


def test_gate_decision_to_action_maps_none_to_none():
    from hermes_quant.pdr_core_adapter import gate_decision_to_action

    assert gate_decision_to_action(None) is None


def test_gate_decision_to_action_preserves_halt_triple():
    """The lifted map must carry the full durable-HALT triple verbatim — the
    riskiest coupling (dropping it is a money-safety regression)."""
    from hermes_quant.pdr_core.gate_types import GateDecision
    from hermes_quant.pdr_core_adapter import gate_decision_to_action

    halt_until = UTC_NOW + pd.Timedelta(days=1)
    decision = GateDecision(
        target_position_pct=0.0,
        reason="daily_loss_circuit_breaker_0.1000",
        signal_id="sig-1",
        halt=True,
        halt_scope=("acct", "equity", None),
        halt_until=halt_until,
    )
    action = gate_decision_to_action(decision)
    assert isinstance(action, Action)
    assert action.target_position_pct == 0.0
    assert action.reason == "daily_loss_circuit_breaker_0.1000"
    assert action.signal_id == "sig-1"
    assert action.halt is True
    assert action.halt_scope == ("acct", "equity", None)
    assert action.halt_until == halt_until
    assert type(action.halt_until) is type(halt_until)


def test_core_builders_are_faithful_to_protocol_inputs():
    """The four protocol->core builders copy the gate-read fields 1:1."""
    from hermes_quant.pdr_core_adapter import (
        core_market_from,
        core_portfolio_from,
        core_signal_from,
    )

    sig = AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=UTC_NOW,
        direction=1,
        magnitude=0.03,
        confidence=0.95,
        confidence_raw=0.95,
        horizon="1d",
        components=(),
        aggregator="bma",
        metadata={"id": "sig-xyz"},
    )
    mkt = MarketState(
        asset="AAPL",
        asof=UTC_NOW,
        volatility=0.05,
        commission=0.0001,
        spread=0.0002,
        slippage_estimate=0.0001,
        tz="America/New_York",
    )
    pf = Portfolio(
        account_id="acct",
        asset_class="equity",
        asof=UTC_NOW,
        positions={"AAPL": Position("AAPL", 100.0, 190.0, 200.0, 0.0, 0.0)},
        cash=1000.0,
        equity_total=100_000.0,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=120_000.0,
        daily_open_equity=110_000.0,
    )

    cs = core_signal_from(sig)
    assert (cs.asset, cs.asset_class, cs.direction, cs.magnitude, cs.confidence) == (
        "AAPL",
        "equity",
        1,
        0.03,
        0.95,
    )
    assert cs.asof == UTC_NOW
    assert cs.metadata == {"id": "sig-xyz"}

    cm = core_market_from(mkt)
    assert (cm.asset, cm.volatility, cm.commission, cm.spread, cm.slippage_estimate, cm.tz) == (
        "AAPL",
        0.05,
        0.0001,
        0.0002,
        0.0001,
        "America/New_York",
    )
    assert cm.asof == UTC_NOW

    cp = core_portfolio_from(pf)
    assert (cp.account_id, cp.asset_class) == ("acct", "equity")
    assert cp.equity_total == 100_000.0
    assert cp.peak_equity == 120_000.0
    assert cp.daily_open_equity == 110_000.0
    # the derived reads reproduce protocol.Portfolio's bodies exactly
    assert cp.drawdown_pct == pf.drawdown_pct
    assert cp.daily_loss_pct == pf.daily_loss_pct
    assert cp.current_position_pct("AAPL") == pf.current_position_pct("AAPL")


def test_core_risk_config_mirrors_live_config_fields():
    """Every shared RiskConfig field is copied from the live config; the
    event_risk_enabled flag is taken from the env (default-off)."""
    from hermes_quant.pdr_core_adapter import core_risk_config_from
    from hermes_quant.risk.gate import RiskConfig as LiveRiskConfig

    live = LiveRiskConfig(
        max_position_pct=0.40,
        action_step=0.10,
        cost_multiple=1.5,
        max_drawdown_pct=0.20,
        max_daily_loss_pct=0.10,
        paper_zero_costs=True,
    )
    core = core_risk_config_from(live, event_risk_enabled=False)
    for f in (
        "max_position_pct",
        "action_step",
        "cost_multiple",
        "max_drawdown_pct",
        "max_daily_loss_pct",
        "min_trade_size",
        "quarter_kelly",
        "cooldown_after_loss_minutes",
        "event_risk_window_days",
        "paper_zero_costs",
    ):
        assert getattr(core, f) == getattr(live, f), f"field {f} not mirrored"
    assert core.event_risk_enabled is False

    core_on = core_risk_config_from(live, event_risk_enabled=True)
    assert core_on.event_risk_enabled is True


def test_core_risk_config_falls_back_when_live_config_missing():
    """If the live gate has no .config, the adapter falls back to defaults and
    does not raise (divergence is logged-but-harmless)."""
    from hermes_quant.pdr_core.gate import RiskConfig as CoreRiskConfig
    from hermes_quant.pdr_core_adapter import core_risk_config_from

    core = core_risk_config_from(None, event_risk_enabled=False)
    assert isinstance(core, CoreRiskConfig)


def test_run_shadow_gate_agreement_returns_no_divergence(caplog):
    """When the live action and the core verdict agree, the report has no
    divergence and nothing is logged at WARNING."""
    from hermes_quant.pdr_core_adapter import run_shadow_gate
    from hermes_quant.risk.gate import DefaultRiskGate as LiveGate

    live_gate = LiveGate()
    sig = AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=UTC_NOW,
        direction=1,
        magnitude=0.03,
        confidence=0.95,
        confidence_raw=0.95,
        horizon="1d",
        components=(),
        aggregator="bma",
        metadata={"id": "sig-1"},
    )
    mkt = MarketState("AAPL", UTC_NOW, 0.05, 0.0001, 0.0002, 0.0001, tz="UTC")
    pf = Portfolio(
        "acct", "equity", UTC_NOW, {}, 100_000.0, 100_000.0, 0.0, 0.0, 100_000.0, 100_000.0
    )

    class _NoHalt:
        def is_halted(self, account_id, asset_class, asset=None):
            return False

        def active_halts(self):
            return []

    halt = _NoHalt()
    live_action = live_gate.gate(sig, mkt, pf, halt)

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        report = run_shadow_gate(
            agg_signal=sig,
            market=mkt,
            portfolio=pf,
            halt_state=halt,
            live_action=live_action,
            live_config=live_gate.config,
        )
    assert report is not None
    assert report["diverged"] is False, report
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_run_shadow_gate_divergence_is_detected_and_logged(caplog):
    """When the live action differs from the core verdict, the report flags
    divergence and a WARNING is logged. Critically run_shadow_gate returns a
    report and never mutates the live action."""
    from hermes_quant.pdr_core_adapter import run_shadow_gate
    from hermes_quant.risk.gate import RiskConfig as LiveRiskConfig

    sig = AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=UTC_NOW,
        direction=1,
        magnitude=0.03,
        confidence=0.95,
        confidence_raw=0.95,
        horizon="1d",
        components=(),
        aggregator="bma",
        metadata={"id": "sig-1"},
    )
    mkt = MarketState("AAPL", UTC_NOW, 0.05, 0.0001, 0.0002, 0.0001, tz="UTC")
    pf = Portfolio(
        "acct", "equity", UTC_NOW, {}, 100_000.0, 100_000.0, 0.0, 0.0, 100_000.0, 100_000.0
    )

    class _NoHalt:
        def is_halted(self, account_id, asset_class, asset=None):
            return False

        def active_halts(self):
            return []

    # A deliberately wrong "live" action that the core gate will not match.
    bogus_live = Action(
        target_position_pct=0.20,
        reason="totally_made_up",
        signal_id="sig-1",
        halt=False,
    )

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        report = run_shadow_gate(
            agg_signal=sig,
            market=mkt,
            portfolio=pf,
            halt_state=_NoHalt(),
            live_action=bogus_live,
            live_config=LiveRiskConfig(),
        )
    assert report is not None
    assert report["diverged"] is True, report
    assert report["fields"], "diverged report must name the mismatching fields"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a divergence must emit a WARNING log line"
    )


def test_run_shadow_gate_swallows_exceptions_and_returns_none(caplog):
    """If anything inside the shadow path raises, run_shadow_gate must swallow
    it (best-effort) and return None — never re-raise."""
    from hermes_quant.pdr_core_adapter import run_shadow_gate
    from hermes_quant.risk.gate import RiskConfig as LiveRiskConfig

    # A signal whose .asset attribute access blows up forces a builder raise.
    class _Exploding:
        @property
        def asset(self):
            raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        report = run_shadow_gate(
            agg_signal=_Exploding(),
            market=None,
            portfolio=None,
            halt_state=None,
            live_action=Action(target_position_pct=0.0, reason="x"),
            live_config=LiveRiskConfig(),
        )
    assert report is None  # swallowed
    # a best-effort failure logs at WARNING but never raises
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


# ===========================================================================
# SECTION 2 — the live recommend() seam.
# ===========================================================================


def test_recommend_flag_off_unset_equals_explicit_zero(monkeypatch):
    """Flag-OFF: result.risk_gate is identical whether the flag is UNSET or
    explicitly set to "0" — the two ways an operator leaves the seam dark.

    This is the byte-identical-OFF invariant across both off-states (the seam
    reads ``os.environ.get("HERMES_QUANT_PDR_CORE_SHADOW", "0") == "1"`` so unset
    and "0" must take the SAME dark branch)."""
    bars = _make_bars()

    monkeypatch.delenv("HERMES_QUANT_PDR_CORE_SHADOW", raising=False)
    unset = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "0")
    explicit_zero = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    assert unset["risk_gate"] == explicit_zero["risk_gate"]


def test_recommend_flag_off_does_not_construct_core_gate(monkeypatch):
    """Flag-OFF NON-VACUITY: the core gate is NEVER CONSTRUCTED flag-off.

    The seam swallows ALL exceptions (best-effort shadow), so a raising poison
    is invisible — a vacuous test would assert only that recommend() succeeds,
    which it does even if the shadow ran. Instead we install an OBSERVABLE spy
    on the core gate ctor (which the adapter resolves as
    ``adapter.CoreDefaultRiskGate``) that APPENDS to an external list AND raises.
    The raise is swallowed; the append is NOT.

      * Flag UNSET and flag "0": the spy list stays EMPTY (the branch is dark,
        the adapter is never imported, the core gate is never constructed) and
        recommend() succeeds.
      * Flag "1" (control, in the SAME test): the spy list is NON-empty —
        proving the spy WOULD have fired had the seam run, so the empty list
        flag-off is a real observation, not a dead probe.

    This pins the flag as the SOLE thing gating core-gate construction — the
    byte-identical-OFF invariant the VERIFY mandate requires (patch the core
    gate ctor; confirm flag-off recommend() still succeeds AND never touches
    it)."""
    import hermes_quant.pdr_core_adapter as adapter

    bars = _make_bars()
    constructed: list[object] = []

    def _spy_ctor(*args, **kwargs):
        constructed.append(("core_gate_constructed", args, kwargs))
        raise RuntimeError("core gate ctor poisoned — must be swallowed flag-on")

    monkeypatch.setattr(adapter, "CoreDefaultRiskGate", _spy_ctor)

    # Flag UNSET: dark branch, spy untouched, recommend() succeeds.
    monkeypatch.delenv("HERMES_QUANT_PDR_CORE_SHADOW", raising=False)
    out = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    assert out["risk_gate"] is not None
    assert constructed == [], "core gate was constructed while the flag was UNSET"

    # Flag "0": still dark, spy still untouched.
    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "0")
    out_zero = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    assert out_zero["risk_gate"] is not None
    assert constructed == [], "core gate was constructed while the flag was '0'"

    # Control — flag "1": the SAME spy DOES fire (its raise is swallowed, the
    # live decision is unaffected). This proves the spy is live, so the empty
    # list above is a genuine observation rather than a dead probe.
    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "1")
    out_on = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    assert out_on["risk_gate"] is not None  # poison swallowed, live unaffected
    assert constructed, "control: flag-on must reach + construct the core gate"


def test_recommend_flag_off_is_byte_identical(monkeypatch):
    """Flag-OFF: result.risk_gate (and the whole dict modulo wall-clock) is
    identical whether or not the shadow code exists in the path. The core gate
    is never constructed."""
    monkeypatch.delenv("HERMES_QUANT_PDR_CORE_SHADOW", raising=False)
    bars = _make_bars()
    out = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    # baseline: the same call again (flag still off) is identical on risk_gate
    out2 = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    assert out["risk_gate"] == out2["risk_gate"]


def test_recommend_flag_on_live_action_still_drives_result(monkeypatch, caplog):
    """Flag-ON with a DIVERGENT live gate: result.risk_gate STILL reflects the
    LIVE action (the shadow only observes), and the divergence is logged."""
    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "1")
    monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
    bars = _make_bars()

    # A live gate that emits a fixed, sized long action regardless of inputs.
    # The deterministic core gate, fed the same synthetic flat portfolio +
    # aggregated signal, will produce a DIFFERENT verdict (likely silence or a
    # different rung), forcing a divergence the shadow must log.
    fixed = Action(
        target_position_pct=0.15,
        reason="fixed_live_action_for_test",
        signal_id=None,
        halt=False,
    )
    gate = _FixedActionGate(fixed)

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        out = recommend(**_recommend_kwargs(_CannedProvider(bars), risk_gate=gate))

    # The LIVE action drives result.risk_gate — the fixed sized long shows up.
    rg = out["risk_gate"]
    assert rg["pass"] is True
    assert rg["kelly_fraction"] == 0.15
    assert rg["reason"] == "fixed_live_action_for_test"
    # The shadow observed a divergence (core gate would not emit this action).
    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "flag-on with a divergent live gate must log a shadow divergence"
    )


def test_recommend_flag_on_with_agreeing_gate_does_not_change_result(monkeypatch):
    """Flag-ON with the REAL live gate (live and core agree by construction):
    result.risk_gate is identical to the flag-OFF result. The shadow is a
    pure observer."""
    bars = _make_bars()

    monkeypatch.delenv("HERMES_QUANT_PDR_CORE_SHADOW", raising=False)
    monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
    off = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "1")
    on = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    assert off["risk_gate"] == on["risk_gate"]


def test_recommend_flag_on_shadow_exception_does_not_affect_live(monkeypatch):
    """Flag-ON but the shadow path raises: the live decision is UNAFFECTED.

    We force the adapter's run_shadow_gate to raise; the live action must still
    drive result.risk_gate and recommend() must not raise."""
    import hermes_quant.pdr_core_adapter as adapter

    bars = _make_bars()

    monkeypatch.delenv("HERMES_QUANT_PDR_CORE_SHADOW", raising=False)
    monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
    baseline = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    def _boom(*args, **kwargs):
        raise RuntimeError("shadow exploded")

    monkeypatch.setattr(adapter, "run_shadow_gate", _boom)
    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "1")

    out = recommend(**_recommend_kwargs(_CannedProvider(bars)))
    # live decision unaffected despite the shadow raising
    assert out["risk_gate"] == baseline["risk_gate"]


def test_recommend_flag_on_core_gate_stubbed_divergent_live_action_still_drives(
    monkeypatch, caplog
):
    """LIVE-ACTION-DRIVES, NON-VACUOUS: flag-ON with the CORE GATE itself
    stubbed to emit a deliberately DIFFERENT verdict.

    The ACT-stage test relied on the deterministic core gate INCIDENTALLY
    diverging from a hand-crafted live action. This pins it explicitly: we
    replace the core gate's ``.gate`` so the shadow's mapped Action is a fixed
    sized-short verdict that CANNOT equal the live decision. The contract is
    that result.risk_gate STILL reflects the LIVE action (not the stubbed core
    one), and a divergence WARNING fires."""
    import hermes_quant.pdr_core_adapter as adapter
    from hermes_quant.pdr_core.gate_types import GateDecision

    bars = _make_bars()

    # Baseline: the flag-OFF live decision (the oracle the seam must preserve).
    monkeypatch.delenv("HERMES_QUANT_PDR_CORE_SHADOW", raising=False)
    monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
    baseline = recommend(**_recommend_kwargs(_CannedProvider(bars)))["risk_gate"]

    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "1")

    # Stub the CORE gate so its verdict is a fixed sized-short verdict the live
    # gate (real DefaultRiskGate over the synthetic portfolio) cannot match.
    divergent_core = GateDecision(
        target_position_pct=-0.20,
        reason="stubbed_core_divergent_verdict",
        signal_id="stub",
        halt=False,
        halt_scope=None,
        halt_until=None,
    )

    class _StubCoreGate:
        def __init__(self, *a, **k):
            pass

        def gate(self, *a, **k):
            return divergent_core

    monkeypatch.setattr(adapter, "CoreDefaultRiskGate", _StubCoreGate)

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        out = recommend(**_recommend_kwargs(_CannedProvider(bars)))

    rg = out["risk_gate"]
    # The LIVE decision drives result.risk_gate verbatim — identical to the
    # flag-OFF baseline. The stubbed core wanted a -0.20 short with a made-up
    # reason; NONE of that leaks into the live decision dict.
    assert rg == baseline
    assert "stubbed_core_divergent_verdict" not in rg.values()
    assert -0.20 not in rg.values()
    # And the divergence was observed + logged (live verdict vs stubbed core).
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "stubbed-divergent core must log a shadow divergence WARNING"
    assert any("DIVERGED" in r.getMessage() for r in warnings)


# ===========================================================================
# SECTION 3 — real-data-shape PARITY through the ADAPTER's own path.
#
# The parity gate (tests/pdr_core/test_gate_parity.py) proves the core gate is
# behaviorally identical to the live gate, but it uses ITS OWN paired builders
# and its OWN GateDecision->Action map. This section proves the SHELL ADAPTER's
# builders + map reproduce that parity: on a faithfully-adapted realistic input
# the shadow finds ZERO divergence. The RED-guard underneath proves the parity
# assertion is NON-VACUOUS — corrupt the adapter map and the comparator fires.
# ===========================================================================


def _realistic_inputs():
    """A realistic, sized-long protocol scenario (Rule-6 quarter-Kelly rung)."""
    sig = AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=UTC_NOW,
        direction=1,
        magnitude=0.02,
        confidence=0.90,
        confidence_raw=0.90,
        horizon="1d",
        components=(),
        aggregator="bma",
        metadata={"id": "sig-real-1"},
    )
    mkt = MarketState(
        asset="AAPL",
        asof=UTC_NOW,
        volatility=0.02,
        commission=0.0001,
        spread=0.0002,
        slippage_estimate=0.0001,
        tz="America/New_York",
    )
    pf = Portfolio(
        account_id="alpaca-paper",
        asset_class="equity",
        asof=UTC_NOW,
        positions={},
        cash=1000.0,
        equity_total=100_000.0,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=100_000.0,
        daily_open_equity=100_000.0,
    )

    class _NoHalt:
        def is_halted(self, account_id, asset_class, asset=None):
            return False

        def active_halts(self):
            return []

    return sig, mkt, pf, _NoHalt()


def test_adapter_path_is_field_identical_to_live_gate_on_realistic_input():
    """PARITY: run BOTH the live gate and the adapter path over the SAME
    realistic protocol inputs; the mapped core Action == the live Action
    field-by-field. The shadow must find ZERO divergence on faithful input.

    This exercises the ADAPTER's builders + its lifted GateDecision->Action map
    + its comparator end-to-end against the real live DefaultRiskGate — the
    money-safety oracle (ADR-0004)."""
    from hermes_quant.pdr_core.gate import DefaultRiskGate as CoreGate
    from hermes_quant.pdr_core_adapter import (
        _compare_actions,
        core_market_from,
        core_portfolio_from,
        core_risk_config_from,
        core_signal_from,
        gate_decision_to_action,
    )
    from hermes_quant.risk.gate import DefaultRiskGate as LiveGate

    sig, mkt, pf, halt = _realistic_inputs()

    live_gate = LiveGate()
    live_action = live_gate.gate(sig, mkt, pf, halt)
    # the realistic input must actually produce a sized action (not silence),
    # else the parity assertion would be vacuously true on a None==None.
    assert live_action is not None, "fixture must drive a non-silent live action"
    assert live_action.target_position_pct != 0.0, "fixture must size a position"

    # Build the SAME inputs through the ADAPTER's builders, run the core gate,
    # map via the ADAPTER's lifted map.
    core_cfg = core_risk_config_from(live_gate.config, event_risk_enabled=False)
    core_gate = CoreGate(core_cfg)
    shadow_decision = core_gate.gate(
        core_signal_from(sig),
        core_market_from(mkt),
        core_portfolio_from(pf),
        halt,
    )
    shadow_action = gate_decision_to_action(shadow_decision)

    # Field-by-field identity via the adapter's OWN comparator.
    diverged = _compare_actions(live_action, shadow_action)
    assert diverged == [], (
        f"adapter path diverged from live gate on faithful input: {diverged} "
        f"(live={live_action!r} shadow={shadow_action!r})"
    )
    # Direct field equality, independent of the comparator, as a cross-check.
    assert shadow_action is not None
    assert shadow_action.target_position_pct == live_action.target_position_pct
    assert shadow_action.reason == live_action.reason
    assert shadow_action.signal_id == live_action.signal_id
    assert shadow_action.halt == live_action.halt
    assert shadow_action.halt_scope == live_action.halt_scope
    assert shadow_action.halt_until == live_action.halt_until


def test_parity_comparator_is_non_vacuous_when_adapter_map_is_broken(monkeypatch):
    """RED-PROOF that the parity assertion above is NON-VACUOUS.

    We CORRUPT the adapter's GateDecision->Action map (perturb the sizing
    field) and re-run the SAME realistic parity flow. The comparator MUST now
    report a divergence on ``target_position_pct``. If the comparator stayed
    green here, the parity test above would be meaningless."""
    from dataclasses import replace

    import hermes_quant.pdr_core_adapter as adapter
    from hermes_quant.pdr_core.gate import DefaultRiskGate as CoreGate
    from hermes_quant.risk.gate import DefaultRiskGate as LiveGate

    sig, mkt, pf, halt = _realistic_inputs()

    live_gate = LiveGate()
    live_action = live_gate.gate(sig, mkt, pf, halt)
    assert live_action is not None and live_action.target_position_pct != 0.0

    # Corrupt the map: produce an Action whose sizing is perturbed away from the
    # faithful verdict (this is the "break the adapter mapping" mutation).
    real_map = adapter.gate_decision_to_action

    def _broken_map(decision):
        mapped = real_map(decision)
        if mapped is None:
            return None
        return replace(mapped, target_position_pct=mapped.target_position_pct + 0.05)

    monkeypatch.setattr(adapter, "gate_decision_to_action", _broken_map)

    core_cfg = adapter.core_risk_config_from(live_gate.config, event_risk_enabled=False)
    core_gate = CoreGate(core_cfg)
    shadow_decision = core_gate.gate(
        adapter.core_signal_from(sig),
        adapter.core_market_from(mkt),
        adapter.core_portfolio_from(pf),
        halt,
    )
    shadow_action = adapter.gate_decision_to_action(shadow_decision)

    diverged = adapter._compare_actions(live_action, shadow_action)
    assert "target_position_pct" in diverged, (
        "comparator failed to catch a corrupted adapter map — the parity check "
        "is VACUOUS"
    )


def test_run_shadow_gate_non_vacuous_via_broken_map(monkeypatch, caplog):
    """End-to-end NON-VACUITY: with the adapter map broken, run_shadow_gate
    over a faithful realistic input REPORTS divergence and logs a WARNING.

    This proves run_shadow_gate's agreement path (the green parity result) is a
    real signal — break the map and the SAME call flips to diverged=True."""
    from dataclasses import replace

    import hermes_quant.pdr_core_adapter as adapter
    from hermes_quant.risk.gate import DefaultRiskGate as LiveGate

    sig, mkt, pf, halt = _realistic_inputs()
    live_gate = LiveGate()
    live_action = live_gate.gate(sig, mkt, pf, halt)
    assert live_action is not None

    # Sanity: faithful run agrees (the agreement path is reachable).
    faithful = adapter.run_shadow_gate(
        agg_signal=sig,
        market=mkt,
        portfolio=pf,
        halt_state=halt,
        live_action=live_action,
        live_config=live_gate.config,
    )
    assert faithful is not None and faithful["diverged"] is False, faithful

    # Now corrupt the map and re-run: the SAME faithful input must diverge.
    real_map = adapter.gate_decision_to_action

    def _broken_map(decision):
        mapped = real_map(decision)
        if mapped is None:
            return None
        return replace(mapped, reason=mapped.reason + "_CORRUPTED")

    monkeypatch.setattr(adapter, "gate_decision_to_action", _broken_map)

    with caplog.at_level(logging.WARNING, logger="hermes_quant.pdr_core_adapter"):
        broken = adapter.run_shadow_gate(
            agg_signal=sig,
            market=mkt,
            portfolio=pf,
            halt_state=halt,
            live_action=live_action,
            live_config=live_gate.config,
        )
    assert broken is not None and broken["diverged"] is True, broken
    assert "reason" in broken["fields"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
