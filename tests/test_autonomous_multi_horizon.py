"""W5 — Autonomous multi-horizon tick wiring + horizon-aware DTE thread.

Behind ``HERMES_QUANT_MULTI_HORIZON_TICK`` (default-OFF), the autonomous tick
threads ``entry.horizon_set`` into ``recommend_multi_horizon`` (fanning the
analyst views out across the rung timeframes) and threads the chosen rung's
``dte_bucket_for_horizon`` into ``build_and_persist_multi_leg(dte_min=,dte_max=)``
so structure_select's kind AND the horizon's DTE window both flow to the producer.

Invariants proved here:
  * Flag OFF  -> the tick calls the single ``advisor_recommend(timeframe=...)``
    exactly as today; ``recommend_multi_horizon`` is NEVER invoked (byte-identical).
  * Flag ON + ``entry.horizon_set`` present -> the multi-horizon fan-out runs
    ``recommend_multi_horizon(symbol, horizons=[HORIZONS[r].timeframe for r in set])``.
  * Flag ON but NO ``horizon_set`` on the entry -> falls back to the single
    timeframe call (byte-identical; the field is W4 add-only and may be absent).
  * The chosen rung's DTE bucket threads to ``build_and_persist_multi_leg`` via
    ``dte_min=``/``dte_max=``. The 30D rung resolves to (25, 45) == today's fixed
    default, so the 30D-only path is byte-identical to the current options path.
  * ``structure_select`` stays the FINAL kind authority — the watchlist entry
    carries a horizon_set (timeframe + DTE window) but never a StrategyKind.

These tests build the seam against W2/W4's contract (a ``hermes_quant.playbook
.horizons`` module exposing ``HORIZONS`` + ``dte_bucket_for_horizon`` and a
``horizon_set`` attribute on the watchlist entry). When those siblings are not
yet merged, the tests inject a stub ``horizons`` module + a lightweight entry
stand-in so the autonomous seam composes without duplicating their work.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

import hermes_quant.autonomous as auto


# --------------------------------------------------------------------------- #
# Lightweight stand-ins (the seam must read these via getattr, never hard-import)
# --------------------------------------------------------------------------- #


@dataclass
class _Entry:
    """A WatchlistEntry-shaped stand-in carrying the W4 add-only horizon_set.

    The real WatchlistEntry is a frozen dataclass; W4 adds ``horizon_set`` as an
    add-only field. The autonomous seam must read it via ``getattr(entry,
    "horizon_set", None)`` so an entry WITHOUT the field (pre-W4) falls back to
    the single-timeframe path. This stand-in lets us drive both shapes.
    """

    symbol: str
    asset_class: str = "equity"
    timeframe: str = "1d"
    options_eligible: bool = False
    horizon_set: list[str] | None = None


@dataclass
class _HorizonRung:
    timeframe: str
    dte_min: int
    dte_max: int


def _install_stub_horizons(monkeypatch: pytest.MonkeyPatch) -> dict[str, _HorizonRung]:
    """Install a stub ``hermes_quant.playbook.horizons`` matching W2/W4's contract.

    HORIZONS maps a rung label -> (timeframe, DTE bucket); dte_bucket_for_horizon
    returns (dte_min, dte_max). 30D resolves to the current fixed default (25, 45).
    """
    rungs = {
        "1D": _HorizonRung("1d", 1, 7),
        "7D": _HorizonRung("1w", 7, 14),
        "14D": _HorizonRung("1w", 14, 30),
        "30D": _HorizonRung("1M", 25, 45),
    }
    mod = types.ModuleType("hermes_quant.playbook.horizons")
    mod.HORIZONS = rungs  # type: ignore[attr-defined]

    def dte_bucket_for_horizon(rung: str) -> tuple[int, int]:
        r = rungs[rung]
        return (r.dte_min, r.dte_max)

    mod.dte_bucket_for_horizon = dte_bucket_for_horizon  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_quant.playbook.horizons", mod)
    return rungs


# --------------------------------------------------------------------------- #
# Tick harness: force autonomous mode + a clean kill-switch so tick() reaches
# the watchlist loop. We inject advisor_recommend + symbols so no network runs.
# --------------------------------------------------------------------------- #


def _force_tick_preconditions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(
        auto,
        "_read_kill_switch",
        lambda: auto.KillSwitchState(
            tripped=False,
            tripped_at=None,
            cumulative_pnl_pct=0.0,
            threshold_pct=0.10,
            reason=None,
        ),
    )
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda: 0.0)
    # Silence-bias config + rails: defaults are fine; ensure no per-position stop.
    monkeypatch.delenv("HERMES_QUANT_PER_POSITION_STOP", raising=False)
    monkeypatch.delenv("HERMES_QUANT_AUTONOMOUS_OPTIONS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_SEMANTIC_ENABLED", raising=False)
    # Frame injection default-ON reads SEMANTIC_ENABLED; force it OFF so no live
    # perception frame is built (keeps the advisor_recommend call shape minimal).
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")


def _gated_advisor_result() -> dict[str, Any]:
    """A minimal recommend()-shaped dict that GATES (so no fire / React)."""
    return {
        "symbol": "AAPL",
        "asset_class": "equity",
        "timeframe": "1d",
        "aggregated_signal": {"direction": 0, "magnitude": 0.0, "confidence": 0.0},
        "risk_gate": {"pass": False, "gated_reason": "test", "kelly_fraction": 0.0},
        "lessons": [],
    }


# --------------------------------------------------------------------------- #
# 1) Flag OFF -> byte-identical single-timeframe call; multi-horizon never runs.
# --------------------------------------------------------------------------- #


def test_flag_off_uses_single_timeframe_and_never_calls_multi_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTI_HORIZON_TICK", raising=False)
    _force_tick_preconditions(monkeypatch)
    _install_stub_horizons(monkeypatch)

    calls: list[dict[str, Any]] = []

    def fake_recommend(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return _gated_advisor_result()

    multi_called: list[Any] = []
    import hermes_quant.advisor as advisor

    monkeypatch.setattr(
        advisor,
        "recommend_multi_horizon",
        lambda *a, **k: multi_called.append((a, k)) or [],
    )

    entry = _Entry(symbol="AAPL", timeframe="1d", horizon_set=["1D", "7D", "14D", "30D"])
    auto.tick(dry_run=True, symbols=[entry], advisor_recommend=fake_recommend)

    # The single-timeframe advisor_recommend ran with the entry's own timeframe.
    assert len(calls) == 1, f"expected 1 single-tf call, got {len(calls)}"
    assert calls[0]["timeframe"] == "1d"
    # The multi-horizon fan-out was NEVER touched (byte-identical OFF).
    assert multi_called == [], "recommend_multi_horizon must not run with the flag OFF"


# --------------------------------------------------------------------------- #
# 2) Flag ON + horizon_set -> recommend_multi_horizon runs with rung timeframes.
# --------------------------------------------------------------------------- #


def test_flag_on_with_horizon_set_fans_out_to_multi_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTI_HORIZON_TICK", "1")
    _force_tick_preconditions(monkeypatch)
    rungs = _install_stub_horizons(monkeypatch)

    multi_calls: list[tuple[tuple, dict]] = []
    import hermes_quant.advisor as advisor

    def fake_multi(symbol: str, **kwargs: Any) -> list[Any]:
        multi_calls.append(((symbol,), kwargs))
        return []  # no views -> the per-rung fan-out is recorded but emits nothing

    monkeypatch.setattr(advisor, "recommend_multi_horizon", fake_multi)

    # advisor_recommend still produces the gate spine (gated => no fire).
    def fake_recommend(**kwargs: Any) -> dict[str, Any]:
        return _gated_advisor_result()

    horizon_set = ["1D", "7D", "14D", "30D"]
    entry = _Entry(symbol="AAPL", timeframe="1d", horizon_set=horizon_set)
    auto.tick(dry_run=True, symbols=[entry], advisor_recommend=fake_recommend)

    assert len(multi_calls) == 1, (
        f"expected recommend_multi_horizon called once, got {len(multi_calls)}"
    )
    _, kwargs = multi_calls[0]
    threaded = list(kwargs.get("horizons", []))
    expected = [rungs[r].timeframe for r in horizon_set]
    # Dedup-preserving order: 1d, 1w, 1w, 1M -> the fan-out must carry the rung
    # timeframes (recommend_multi_horizon itself dedupes internally).
    assert threaded == expected, f"threaded={threaded!r} expected={expected!r}"


# --------------------------------------------------------------------------- #
# 3) Flag ON but entry has NO horizon_set -> single-timeframe fallback.
# --------------------------------------------------------------------------- #


def test_flag_on_without_horizon_set_falls_back_to_single_timeframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTI_HORIZON_TICK", "1")
    _force_tick_preconditions(monkeypatch)
    _install_stub_horizons(monkeypatch)

    import hermes_quant.advisor as advisor

    multi_called: list[Any] = []
    monkeypatch.setattr(
        advisor,
        "recommend_multi_horizon",
        lambda *a, **k: multi_called.append((a, k)) or [],
    )

    calls: list[dict[str, Any]] = []

    def fake_recommend(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return _gated_advisor_result()

    # horizon_set absent (None) — emulates a pre-W4 entry.
    entry = _Entry(symbol="AAPL", timeframe="1d", horizon_set=None)
    auto.tick(dry_run=True, symbols=[entry], advisor_recommend=fake_recommend)

    assert len(calls) == 1 and calls[0]["timeframe"] == "1d"
    assert multi_called == [], (
        "absent horizon_set must fall back to single-tf even with the flag ON"
    )


# --------------------------------------------------------------------------- #
# 4) DTE bucket threads to recipes via _originate_mleg_proposal; 30D == (25,45).
# --------------------------------------------------------------------------- #


@dataclass
class _Result:
    decisions: list[Any] = field(default_factory=list)
    fires: int = 0
    silences: int = 0


def _build_advisor_result_for_options() -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "asset_class": "equity",
        "aggregated_signal": {"direction": 1, "magnitude": 0.5},
        "structure_intent": object(),
    }


def test_dte_bucket_threads_to_build_and_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chosen rung's dte_bucket_for_horizon flows into build_and_persist_multi_leg."""
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS_OPTIONS", "1")
    rungs = _install_stub_horizons(monkeypatch)

    import hermes_quant.options.recipes as recipes
    import hermes_quant.options.structure_select as ss

    # structure_select stays the FINAL kind authority — it picks the kind; the
    # watchlist entry never carries a StrategyKind. Return a sentinel kind.
    _sentinel_kind = object()
    monkeypatch.setattr(
        ss, "select_structure_for_plan", lambda plan, iv_rank=None: _sentinel_kind
    )

    captured: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> tuple[Any, Any]:
        captured.update(kwargs)
        # Return (build_result, persisted=None) so the helper abstains AFTER we
        # have captured the threaded dte_min/dte_max (no reactor / fill needed).
        return (types.SimpleNamespace(reason="captured"), None)

    monkeypatch.setattr(recipes, "build_and_persist_multi_leg", fake_build)
    # get_default_store import inside the helper must not touch disk.
    import hermes_quant.proposals as proposals

    monkeypatch.setattr(proposals, "get_default_store", lambda: object())

    result = _Result()
    # 30D rung -> (25, 45) == the current fixed default.
    rung = "30D"
    out = auto._originate_mleg_proposal(
        symbol="AAPL",
        asof=auto.datetime.now(tz=auto.UTC),
        advisor_result=_build_advisor_result_for_options(),
        nav=100_000.0,
        options_buying_power=0.0,
        iv_rank=0.5,
        structure_intent=object(),
        paper_zero_costs=False,
        result=result,
        horizon_rung=rung,
    )
    assert out is None  # abstained at persisted=None (after capture)
    assert captured.get("dte_min") == rungs[rung].dte_min == 25
    assert captured.get("dte_max") == rungs[rung].dte_max == 45
    # structure_select stays the FINAL kind authority: the KIND handed to the
    # producer is the one select_structure_for_plan returned, NOT anything the
    # horizon rung derived. The watchlist entry / horizon only picks the DTE window.
    assert captured.get("strategy_kind") is _sentinel_kind


def test_no_horizon_rung_keeps_default_dte_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rung passed (flag-OFF / 30D-only path) -> NO dte kwargs threaded.

    Byte-identical: build_and_persist_multi_leg keeps its own _DEFAULT_DTE_MIN/MAX
    (25/45). The helper must NOT inject dte_min/dte_max when horizon_rung is None.
    """
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS_OPTIONS", "1")

    import hermes_quant.options.recipes as recipes
    import hermes_quant.options.structure_select as ss

    monkeypatch.setattr(
        ss, "select_structure_for_plan", lambda plan, iv_rank=None: object()
    )

    captured: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> tuple[Any, Any]:
        captured.update(kwargs)
        return (types.SimpleNamespace(reason="captured"), None)

    monkeypatch.setattr(recipes, "build_and_persist_multi_leg", fake_build)
    import hermes_quant.proposals as proposals

    monkeypatch.setattr(proposals, "get_default_store", lambda: object())

    result = _Result()
    auto._originate_mleg_proposal(
        symbol="AAPL",
        asof=auto.datetime.now(tz=auto.UTC),
        advisor_result=_build_advisor_result_for_options(),
        nav=100_000.0,
        options_buying_power=0.0,
        iv_rank=0.5,
        structure_intent=object(),
        paper_zero_costs=False,
        result=result,
        # horizon_rung omitted -> defaults to None
    )
    assert "dte_min" not in captured, "no rung must NOT inject dte_min (byte-identical)"
    assert "dte_max" not in captured, "no rung must NOT inject dte_max (byte-identical)"
