from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_quant.runs.run_card import SCHEMA_VERSION, write_run_card


def _config() -> dict[str, object]:
    return {
        "symbol": "AAPL",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "interval": "1d",
        "engine": "hermes-replay",
        "initial_cash": 100_000,
        "_runtime_note": "ignored by config hash",
    }


def _metrics() -> dict[str, object]:
    return {
        "total_return": 0.031,
        "max_drawdown": 0.006,
        "validation": {"walk_forward": "pass"},
        "equity_curve": [100_000, 101_000, 103_100],
    }


def _load_card(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_run_card_round_trip_on_tmp_path(tmp_path):
    card = write_run_card(
        "run-001",
        _config(),
        _metrics(),
        quant_home=tmp_path,
        data_sources=["fixtures/bars/AAPL-1d-2020-01-01-2026-01-01.parquet"],
        warnings=["fixture data"],
    )

    json_path = tmp_path / "runs" / "run-001" / "run_card.json"
    loaded = _load_card(json_path)

    assert loaded == card
    assert loaded["run_dir"] == str(tmp_path / "runs" / "run-001")


def test_schema_version_is_0_2(tmp_path):
    card = write_run_card("run-001", _config(), _metrics(), quant_home=tmp_path)

    assert SCHEMA_VERSION == "0.2"
    assert card["schema_version"] == "0.2"


def test_evidence_ids_defaults_to_empty_json_array(tmp_path):
    write_run_card("run-001", _config(), _metrics(), quant_home=tmp_path)

    loaded = _load_card(tmp_path / "runs" / "run-001" / "run_card.json")
    assert "evidence_ids" in loaded
    assert loaded["evidence_ids"] == []


def test_config_hash_is_deterministic_for_same_config(tmp_path):
    first = write_run_card("run-001", _config(), _metrics(), quant_home=tmp_path)
    second = write_run_card("run-002", _config(), _metrics(), quant_home=tmp_path)

    assert first["reproducibility"]["config_hash"] == second["reproducibility"]["config_hash"]


def test_json_markdown_and_reproducibility_section_are_written(tmp_path):
    write_run_card("run-001", _config(), _metrics(), quant_home=tmp_path)

    run_dir = tmp_path / "runs" / "run-001"
    assert (run_dir / "run_card.json").exists()
    assert (run_dir / "run_card.md").exists()
    assert "## Reproducibility" in (run_dir / "run_card.md").read_text(encoding="utf-8")
