"""Regression coverage for the playbook/hourly aggregate cap bypass.

The live playbook script is a cron artifact under ops/scripts rather than an
importable package module, so these tests load it with importlib and fake HOME.
They exercise the direct Alpaca order path without network calls.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-playbook-tick.py"

FIRE_SYMBOLS = ("AAPL", "MSFT", "NVDA", "TSLA", "GOOGL")
FIXED_TS = "2026-06-04T13:00:00Z"
FIXED_DATE_ET = "2026-06-04"


def _load_tick_module(monkeypatch: pytest.MonkeyPatch, root: Path, *, name: str):
    fake_home = root / "home"
    fake_home.mkdir(parents=True)
    (fake_home / ".hermes" / "quant" / "watchlist").mkdir(parents=True)
    (fake_home / ".hermes" / "quant" / "playbook").mkdir(parents=True)
    (fake_home / ".hermes" / "secrets").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_TICK_MOCK", "1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    mod.HERMES_HOME = fake_home / ".hermes"
    mod.QUANT_HOME = mod.HERMES_HOME / "quant"
    mod.WATCHLIST_PATH = mod.QUANT_HOME / "watchlist" / "play-fit.json"
    mod.HALT_MIRROR_PATH = mod.QUANT_HOME / "halt_state.json"
    mod.PLAYBOOK_DIR = mod.QUANT_HOME / "playbook"
    mod.JOURNAL_PATH = mod.PLAYBOOK_DIR / "tick-journal.jsonl"
    mod.SECRETS_PATH = mod.HERMES_HOME / "secrets" / "alpaca.env"
    mod.utcnow_iso = lambda: FIXED_TS
    mod.today_et_date = lambda: FIXED_DATE_ET
    return mod


def _write_fire_watchlist(mod: Any, symbols: tuple[str, ...] = FIRE_SYMBOLS) -> None:
    mod.WATCHLIST_PATH.write_text(
        json.dumps(
            {
                "as_of": FIXED_TS,
                "plays": {
                    "swing": [
                        {
                            "symbol": symbol,
                            "play": "swing",
                            "state": "active",
                            "last_score": 0.9,
                            "consecutive_days_above_floor": 1,
                            "consecutive_days_below_onboard": 0,
                            "extras": {},
                            "last_seen_at": FIXED_TS,
                            "onboarded_at": FIXED_TS,
                            "eviction_reason": None,
                        }
                        for symbol in symbols
                    ]
                },
            }
        )
    )


def _fire_result(kelly_fraction: float = 0.05) -> dict[str, Any]:
    return {
        "risk_gate": {
            "pass": True,
            "recommended_action": "long",
            "kelly_fraction": kelly_fraction,
            "gated_reason": None,
        },
        "aggregated_signal": {
            "direction": 1,
            "magnitude": 0.03,
            "confidence": 0.7,
            "horizon": "1d",
            "aggregator": "test",
        },
        "as_of": FIXED_TS,
        "caveats": [],
    }


def _force_all_fire(monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    monkeypatch.setattr(mod, "call_advisor", lambda symbol: _fire_result())


def _install_order_spy(monkeypatch: pytest.MonkeyPatch, mod: Any) -> list[dict[str, Any]]:
    placed: list[dict[str, Any]] = []

    def fake_order(symbol: str, notional_usd: float, *, side: str = "buy") -> dict[str, Any]:
        placed.append({"symbol": symbol, "notional_usd": notional_usd, "side": side})
        idx = len(placed)
        return {
            "id": f"order-{idx}",
            "client_order_id": f"client-{idx}",
            "submitted_at": FIXED_TS,
        }

    monkeypatch.setattr(mod, "place_paper_market_order", fake_order)
    return placed


def _journal_rows(mod: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in mod.JOURNAL_PATH.read_text().splitlines() if line.strip()]


def _decision_rows(mod: Any) -> list[dict[str, Any]]:
    return [row for row in _journal_rows(mod) if row.get("decision")]


def _ready_fire_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, module_name: str):
    mod = _load_tick_module(monkeypatch, tmp_path / module_name, name=module_name)
    _write_fire_watchlist(mod)
    _force_all_fire(monkeypatch, mod)
    placed = _install_order_spy(monkeypatch, mod)
    return mod, placed


def test_flag_off_documents_current_per_fire_only_bypass(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", raising=False)
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_off")
    monkeypatch.setattr(
        mod,
        "read_alpaca_account_equity",
        lambda: (_ for _ in ()).throw(AssertionError("OFF path must not read account equity")),
    )

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 5
    assert summary["silenced"] == 0
    assert len(placed) == 5
    assert sum(order["notional_usd"] for order in placed) == pytest.approx(5000.0)


def test_flag_on_silences_fires_that_would_breach_aggregate_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_on")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 1250.0)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 2
    assert summary["silenced"] == 3
    assert len(placed) == 2
    assert sum(order["notional_usd"] for order in placed) == pytest.approx(2000.0)
    assert sum(order["notional_usd"] for order in placed) <= 2500.0

    silenced = [row for row in _decision_rows(mod) if row["decision"] == "silenced"]
    assert len(silenced) == 3
    for row in silenced:
        assert "portfolio_cap" in row["reason"]
        assert "aggregate" in row["reason"]
        assert row["aggregate_cap_ceiling_usd"] == pytest.approx(2500.0)
        assert row["aggregate_cap_consumed_usd"] == pytest.approx(2000.0)


def test_flag_on_unreadable_equity_silences_every_fire(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_no_equity")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: None)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 0
    assert summary["silenced"] == 5
    assert placed == []
    assert all("account_equity" in row["reason"] for row in _decision_rows(mod))


def test_flag_unset_is_byte_identical_to_explicit_off(monkeypatch, tmp_path):
    def run_case(module_name: str, flag_value: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if flag_value is None:
            monkeypatch.delenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", raising=False)
        else:
            monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", flag_value)
        mod, _placed = _ready_fire_run(monkeypatch, tmp_path, module_name=module_name)
        summary = mod.run_tick(dry_run=False)
        return summary, _journal_rows(mod)

    baseline_summary, baseline_rows = run_case("qpt_aggregate_unset", None)
    explicit_off_summary, explicit_off_rows = run_case("qpt_aggregate_zero", "0")

    assert explicit_off_summary == baseline_summary
    assert explicit_off_rows == baseline_rows


@pytest.mark.parametrize("bad_equity", [math.nan, math.inf, -math.inf])
def test_flag_on_non_finite_equity_silences_every_fire(monkeypatch, tmp_path, bad_equity):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name=f"qpt_bad_equity_{bad_equity}")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: bad_equity)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 0
    assert summary["silenced"] == 5
    assert placed == []
    assert all("portfolio_cap_aggregate_breach" in row["reason"] for row in _decision_rows(mod))


def test_flag_on_non_finite_notional_silences_without_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_bad_notional")
    _write_fire_watchlist(mod, symbols=("AAPL",))
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 100_000.0)
    monkeypatch.setattr(mod, "kelly_to_notional", lambda advisor_result: math.nan)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 0
    assert summary["silenced"] == 1
    assert placed == []
    row = _decision_rows(mod)[0]
    assert row["decision"] == "silenced"
    assert "non_finite_notional" in row["reason"]


def test_flag_on_dry_run_path_does_not_fetch_equity_or_place(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_dry_run")
    monkeypatch.setattr(
        mod,
        "read_alpaca_account_equity",
        lambda: (_ for _ in ()).throw(AssertionError("dry-run must not build budget")),
    )

    summary = mod.run_tick(dry_run=True)

    assert summary["fired"] == 5
    assert summary["silenced"] == 0
    assert placed == []
