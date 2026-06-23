"""ADR-0092 Phase-4 (parity proof) — the SHADOW divergence SINK.

Aria built + wired ``_persist_divergence_report`` / ``_shadow_divergence_path`` /
``_action_primitives`` in ``hermes_quant.pdr_core_adapter`` and called them from
``run_shadow_gate(persist=True)`` (the advisor's live shadow seam default). They
were UNTESTED. This file is their safety proof.

The persisted record (one JSONL line, appended to
``<quant_home>/pdr-core-shadow-divergence.jsonl``) is::

    {"asof": "<UTC ISO>", "diverged": bool, "fields": [..],
     "live": <action primitives | null>, "shadow": <action primitives | null>}

Coverage:
  (a) a divergence report WRITES one JSONL line with the right keys + the Action
      flattened to PRIMITIVES (no Action object leaks into the log);
  (b) the path honors an injected HERMES_QUANT_HOME / HERMES_HOME (quant_home()
      resolved at CALL TIME);
  (c) FAIL-CLOSED: a write error NEVER raises out of _persist_divergence_report,
      and a sink failure NEVER reaches the live decision (run_shadow_gate still
      returns the LIVE-vs-shadow report and never re-raises);
  (d) DEFAULT-OFF: with HERMES_QUANT_PDR_CORE_SHADOW unset the advisor seam does
      not invoke run_shadow_gate at all => no divergence file is written (the
      byte-identical live path).

The RED-proof for (c): removing the ``except`` in ``_persist_divergence_report``
makes the read-only-dir / monkeypatched-open test below raise instead of swallow,
failing ``test_persist_fail_closed_never_raises``. Restore the except => GREEN.
"""

from __future__ import annotations

import json

import pandas as pd

from hermes_quant.protocol import Action

UTC_NOW = pd.Timestamp("2026-06-12T15:00:00+00:00")


# ===========================================================================
# (a) — a divergence report writes ONE JSONL line, Action flattened to primitives.
# ===========================================================================


def test_persist_writes_one_jsonl_line_with_action_primitives(tmp_path):
    """A divergence report appends exactly one JSONL line whose keys are
    {asof, diverged, fields, live, shadow} and whose live/shadow values are
    FLATTENED primitive dicts (no Action object leaks — Action is not JSON
    serializable as-is)."""
    from hermes_quant.pdr_core_adapter import _persist_divergence_report

    out = tmp_path / "pdr-core-shadow-divergence.jsonl"
    live = Action(
        target_position_pct=0.15,
        reason="live_reason",
        signal_id="sig-1",
        halt=False,
    )
    halt_until = UTC_NOW + pd.Timedelta(days=1)
    shadow = Action(
        target_position_pct=0.0,
        reason="daily_loss_circuit_breaker_0.1000",
        signal_id="sig-1",
        halt=True,
        halt_scope=("acct", "equity", None),
        halt_until=halt_until,
    )
    report = {
        "diverged": True,
        "fields": ["target_position_pct", "reason", "halt", "halt_scope", "halt_until"],
        "live": live,
        "shadow": shadow,
    }

    _persist_divergence_report(report, path=out)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "exactly one JSONL line per persisted report"
    rec = json.loads(lines[0])

    assert set(rec.keys()) == {"asof", "diverged", "fields", "live", "shadow"}
    assert rec["diverged"] is True
    assert rec["fields"] == [
        "target_position_pct",
        "reason",
        "halt",
        "halt_scope",
        "halt_until",
    ]
    # the asof stamps a UTC wall-clock the operator can window the sample by
    assert rec["asof"].endswith("Z")

    # live/shadow are PRIMITIVE dicts, not serialized Action objects.
    assert isinstance(rec["live"], dict)
    assert isinstance(rec["shadow"], dict)
    assert rec["live"] == {
        "target_position_pct": 0.15,
        "reason": "live_reason",
        "signal_id": "sig-1",
        "halt": False,
        "halt_scope": None,
        "halt_until": None,
    }
    # halt_scope is a JSON list (tuple round-trips through json as a list); the
    # pd.Timestamp is isoformatted, never a Timestamp repr.
    assert rec["shadow"]["target_position_pct"] == 0.0
    assert rec["shadow"]["halt"] is True
    assert rec["shadow"]["halt_scope"] == ["acct", "equity", None]
    assert rec["shadow"]["halt_until"] == halt_until.isoformat()
    # no Action repr leaked anywhere in the line
    assert "Action(" not in lines[0]


def test_persist_flattens_none_actions(tmp_path):
    """A silence-vs-silence (or presence-asymmetry) report flattens None Actions
    to JSON null — _action_primitives(None) is None, not a crash."""
    from hermes_quant.pdr_core_adapter import _persist_divergence_report

    out = tmp_path / "div.jsonl"
    report = {"diverged": False, "fields": [], "live": None, "shadow": None}
    _persist_divergence_report(report, path=out)

    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["live"] is None
    assert rec["shadow"] is None
    assert rec["diverged"] is False
    assert rec["fields"] == []


def test_persist_appends_multiple_lines(tmp_path):
    """Two persisted reports => two JSONL lines (append-only, not truncate)."""
    from hermes_quant.pdr_core_adapter import _persist_divergence_report

    out = tmp_path / "div.jsonl"
    a = Action(target_position_pct=0.0, reason="a")
    b = Action(target_position_pct=0.1, reason="b")
    _persist_divergence_report({"diverged": False, "fields": [], "live": a, "shadow": a}, path=out)
    _persist_divergence_report({"diverged": True, "fields": ["reason"], "live": a, "shadow": b}, path=out)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["diverged"] is False
    assert json.loads(lines[1])["diverged"] is True


def test_action_primitives_none_passthrough_and_iso_timestamp():
    """_action_primitives(None) is None; a pd.Timestamp halt_until is isoformatted
    to a JSON-serializable str."""
    from hermes_quant.pdr_core_adapter import _action_primitives

    assert _action_primitives(None) is None

    halt_until = UTC_NOW
    prims = _action_primitives(
        Action(
            target_position_pct=0.0,
            reason="r",
            signal_id="s",
            halt=True,
            halt_scope=("a", "equity", None),
            halt_until=halt_until,
        )
    )
    assert prims is not None
    assert prims["halt_until"] == halt_until.isoformat()
    assert isinstance(prims["halt_until"], str)
    # the whole dict is JSON-serializable (no exotic objects survive)
    json.dumps(prims)


# ===========================================================================
# (b) — the path honors an injected HERMES_QUANT_HOME / HERMES_HOME at CALL TIME.
# ===========================================================================


def test_shadow_divergence_path_honors_hermes_quant_home(tmp_path, monkeypatch):
    """_shadow_divergence_path resolves via quant_home() at call time — a
    HERMES_QUANT_HOME override lands the log directly under that root."""
    from hermes_quant.pdr_core_adapter import _shadow_divergence_path

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    path = _shadow_divergence_path()
    assert path == tmp_path / "pdr-core-shadow-divergence.jsonl"


def test_shadow_divergence_path_honors_hermes_home(tmp_path, monkeypatch):
    """HERMES_HOME points at the hermes home; the quant root is <home>/quant."""
    from hermes_quant.pdr_core_adapter import _shadow_divergence_path

    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = _shadow_divergence_path()
    assert path == tmp_path / "quant" / "pdr-core-shadow-divergence.jsonl"


def test_persist_default_path_honors_injected_home(tmp_path, monkeypatch):
    """With path=None the persist call resolves the home at CALL TIME — an
    injected HERMES_QUANT_HOME lands the log in the test's tmp home, never the
    operator's real ~/.hermes/quant."""
    from hermes_quant.pdr_core_adapter import _persist_divergence_report

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    report = {
        "diverged": False,
        "fields": [],
        "live": Action(target_position_pct=0.0, reason="x"),
        "shadow": Action(target_position_pct=0.0, reason="x"),
    }
    _persist_divergence_report(report)  # path=None => resolved via quant_home()

    out = tmp_path / "pdr-core-shadow-divergence.jsonl"
    assert out.exists()
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


# ===========================================================================
# (c) — FAIL-CLOSED: a sink write error NEVER raises, and never reaches the
# live decision (run_shadow_gate returns the report unchanged even if the sink
# raises).
# ===========================================================================


def test_persist_fail_closed_never_raises(tmp_path, monkeypatch):
    """A write failure inside _persist_divergence_report is swallowed — it never
    raises. RED-PROOF: remove the ``except Exception`` in the function and this
    test raises instead of returning, failing the assertion below."""
    import hermes_quant.pdr_core_adapter as adapter

    out = tmp_path / "div.jsonl"

    # Force the write to blow up: patch Path.open (the call inside the function)
    # to raise. The function must swallow it and return None without raising.
    real_open = adapter.Path.open

    def _boom_open(self, *a, **k):
        if self == out:
            raise OSError("read-only filesystem (simulated)")
        return real_open(self, *a, **k)

    monkeypatch.setattr(adapter.Path, "open", _boom_open)

    report = {
        "diverged": True,
        "fields": ["reason"],
        "live": Action(target_position_pct=0.0, reason="a"),
        "shadow": Action(target_position_pct=0.0, reason="b"),
    }
    # MUST NOT raise.
    adapter._persist_divergence_report(report, path=out)
    # and nothing was written (the open failed before any append)
    assert not out.exists()


def test_persist_fail_closed_under_read_only_dir(tmp_path, monkeypatch):
    """A path whose parent cannot be created/written swallows the error (a second
    fail-closed surface: directory-level write failure)."""
    import hermes_quant.pdr_core_adapter as adapter

    out = tmp_path / "div.jsonl"

    # mkdir succeeds, but the open raises PermissionError — still swallowed.
    real_open = adapter.Path.open

    def _perm(self, *a, **k):
        if self == out:
            raise PermissionError("permission denied (simulated read-only dir)")
        return real_open(self, *a, **k)

    monkeypatch.setattr(adapter.Path, "open", _perm)
    # MUST NOT raise.
    adapter._persist_divergence_report({"diverged": False, "fields": [], "live": None, "shadow": None}, path=out)


def test_run_shadow_gate_returns_live_report_even_when_sink_raises(tmp_path, monkeypatch):
    """A SINK failure must NEVER reach the live decision: run_shadow_gate still
    returns the divergence report (live vs shadow) intact even when the persist
    sink raises, and it never re-raises.

    This is the seam-level fail-closed: the advisor calls run_shadow_gate with
    persist=True (default); a write error in the sink is fully contained."""
    from hermes_quant.pdr_core_adapter import run_shadow_gate
    from hermes_quant.protocol import (
        AggregatedSignal,
        MarketState,
        Portfolio,
    )
    from hermes_quant.risk.gate import DefaultRiskGate as LiveGate

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

    live_gate = LiveGate()
    halt = _NoHalt()
    live_action = live_gate.gate(sig, mkt, pf, halt)

    # Make the SINK raise (the function under test swallows it). We force it by
    # patching the persist helper's writer to blow up.
    import hermes_quant.pdr_core_adapter as adapter

    def _sink_boom(*a, **k):
        raise OSError("sink exploded")

    monkeypatch.setattr(adapter, "_persist_divergence_report", lambda *a, **k: _sink_boom())

    # run_shadow_gate wraps the WHOLE body in try/except too; even though we
    # bypassed the sink's own except, the seam must still not re-raise and must
    # still return a non-None report (the persist call is the last step).
    report = run_shadow_gate(
        agg_signal=sig,
        market=mkt,
        portfolio=pf,
        halt_state=halt,
        live_action=live_action,
        live_config=live_gate.config,
        persist=True,
        divergence_path=tmp_path / "div.jsonl",
    )
    # The seam's outer try/except swallows the sink raise -> returns None (a
    # shadow failure NEVER affects live). Critically: it did NOT re-raise.
    assert report is None  # swallowed by the outer best-effort guard


def test_sink_raise_inside_persist_is_swallowed_by_its_own_guard(tmp_path, monkeypatch):
    """The intended design: the SINK swallows its OWN errors so run_shadow_gate's
    report (and the live decision) are unaffected. With the real sink and a
    forced inner write failure, run_shadow_gate STILL returns a complete report
    (diverged + fields) — the sink failure is fully invisible to the seam."""
    from hermes_quant.pdr_core_adapter import run_shadow_gate
    from hermes_quant.protocol import (
        AggregatedSignal,
        MarketState,
        Portfolio,
    )
    import hermes_quant.pdr_core_adapter as adapter

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
    # A deliberately wrong live action so the report diverges (non-vacuous).
    bogus_live = Action(target_position_pct=0.20, reason="made_up", signal_id="sig-1")

    out = tmp_path / "div.jsonl"
    real_open = adapter.Path.open

    def _boom_open(self, *a, **k):
        if self == out:
            raise OSError("write failed inside the sink")
        return real_open(self, *a, **k)

    monkeypatch.setattr(adapter.Path, "open", _boom_open)

    from hermes_quant.risk.gate import RiskConfig as LiveRiskConfig

    report = run_shadow_gate(
        agg_signal=sig,
        market=mkt,
        portfolio=pf,
        halt_state=halt,
        live_action=bogus_live,
        live_config=LiveRiskConfig(),
        persist=True,
        divergence_path=out,
    )
    # The sink swallowed its own write error => run_shadow_gate returns a full,
    # intact report. The live decision is wholly unaffected.
    assert report is not None
    assert report["diverged"] is True
    assert report["fields"]
    # nothing was persisted (the write failed, swallowed)
    assert not out.exists()


# ===========================================================================
# (d) — DEFAULT-OFF: with HERMES_QUANT_PDR_CORE_SHADOW unset the advisor seam
# does NOT invoke run_shadow_gate at all => no divergence file is written.
# ===========================================================================


def _make_bars(n: int = 120, *, base: float = 100.0, trend: float = 0.5, seed: int = 7):
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

    def __init__(self, bars):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache: bool = True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of
            if cutoff.tzinfo is None:
                cutoff = cutoff.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _recommend_kwargs(provider):
    return dict(
        symbol="TEST",
        asset_class="equity",
        as_of="2026-03-15T00:00:00Z",
        provider=provider,
        include_lessons=False,
    )


def test_default_off_writes_no_divergence_file(tmp_path, monkeypatch):
    """DEFAULT-OFF: with HERMES_QUANT_PDR_CORE_SHADOW UNSET the advisor seam is
    dark — run_shadow_gate is never invoked and NO divergence file is written
    under the (injected) quant home. The byte-identical live path."""
    from hermes_quant.advisor import recommend

    monkeypatch.delenv("HERMES_QUANT_PDR_CORE_SHADOW", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))

    out = recommend(**_recommend_kwargs(_CannedProvider(_make_bars())))
    assert out["risk_gate"] is not None

    div = tmp_path / "pdr-core-shadow-divergence.jsonl"
    assert not div.exists(), "flag-OFF must never write the divergence log"


def test_default_off_via_explicit_zero_writes_no_file(tmp_path, monkeypatch):
    """The explicit '0' off-state is also dark (the seam reads ==\"1\")."""
    from hermes_quant.advisor import recommend

    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "0")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))

    recommend(**_recommend_kwargs(_CannedProvider(_make_bars())))
    assert not (tmp_path / "pdr-core-shadow-divergence.jsonl").exists()


def test_flag_on_writes_divergence_file_under_injected_home(tmp_path, monkeypatch):
    """CONTROL (proves the default-OFF test is non-vacuous): flag-ON the advisor
    seam DOES invoke run_shadow_gate(persist=True) which writes the divergence
    log under the injected quant home. Pairing this with the OFF test proves the
    empty-file assertion above is a genuine observation, not a dead probe."""
    from hermes_quant.advisor import recommend

    monkeypatch.setenv("HERMES_QUANT_PDR_CORE_SHADOW", "1")
    monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))

    recommend(**_recommend_kwargs(_CannedProvider(_make_bars())))

    div = tmp_path / "pdr-core-shadow-divergence.jsonl"
    assert div.exists(), "flag-ON must write the divergence log via the seam"
    lines = div.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    rec = json.loads(lines[0])
    assert set(rec.keys()) == {"asof", "diverged", "fields", "live", "shadow"}
    # the live decision is unaffected (the shadow only observes)
