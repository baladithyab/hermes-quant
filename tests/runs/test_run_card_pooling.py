"""d97e — the run card surfaces the ag03 hierarchical-pooling warm-up band.

An operator running in cron mode must be able to SEE whether the headline
weighting is still warming up. The pooling diagnostics (effective-n per cell,
warm-up flags, headline_in_warmup) are surfaced into the run card ONLY when the
EXISTING default-OFF HERMES_QUANT_HIERARCHICAL_POOLING flag is set.

The hard invariant: with the flag OFF, the run card is byte-identical to today —
the block is absent from both the JSON and the Markdown even if a caller passes
diagnostics. Pure-Python, deterministic; every assertion RED-proven.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_quant.runs.run_card import write_run_card


def _config() -> dict[str, object]:
    return {
        "symbol": "AAPL",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "interval": "1d",
        "engine": "hermes-replay",
        "initial_cash": 100_000,
    }


def _metrics() -> dict[str, object]:
    return {"total_return": 0.031, "max_drawdown": 0.006}


def _pooling() -> dict[str, Any]:
    return {
        "cells": {
            "kronos|volatile": {
                "effective_n": 3.0,
                "pooled_skill": 0.5,
                "warmup": True,
            }
        },
        "n_cells": 1,
        "n_warmup_cells": 1,
        "warmup_n_threshold": 30.0,
        "shrinkage_k": 8.0,
        "headline_in_warmup": True,
    }


def _load_card(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_volatile(card: dict[str, Any]) -> dict[str, Any]:
    """Drop the wall-clock ``generated_at`` so two cards can be compared for the
    structural byte-identity the flag-off invariant is about."""
    return {k: v for k, v in card.items() if k != "generated_at"}


def test_flag_off_omits_pooling_block_byte_identical(tmp_path, monkeypatch):
    """Default-OFF: passing diagnostics is a no-op — the card (JSON + Markdown) is
    byte-identical to the no-diagnostics card built for the same run.

    Built for the SAME run_id (overwriting) so run_id/run_dir match and only the
    ``generated_at`` wall-clock differs, which is neutralized."""
    monkeypatch.delenv("HERMES_QUANT_HIERARCHICAL_POOLING", raising=False)

    write_run_card("same-run", _config(), _metrics(), quant_home=tmp_path)
    base_json = _load_card(tmp_path / "runs" / "same-run" / "run_card.json")
    base_md = (tmp_path / "runs" / "same-run" / "run_card.md").read_text(encoding="utf-8")

    write_run_card(
        "same-run",
        _config(),
        _metrics(),
        quant_home=tmp_path,
        pooling_diagnostics=_pooling(),
    )
    diag_json = _load_card(tmp_path / "runs" / "same-run" / "run_card.json")
    diag_md = (tmp_path / "runs" / "same-run" / "run_card.md").read_text(encoding="utf-8")

    assert "hierarchical_pooling" not in base_json
    assert "hierarchical_pooling" not in diag_json
    assert _strip_volatile(base_json) == _strip_volatile(diag_json)

    assert "Hierarchical Pooling" not in base_md
    assert "Hierarchical Pooling" not in diag_md
    # Markdown carries no generated_at-dependent line beyond the header timestamp,
    # which differs between the two writes; strip that ONE line.
    def _no_gen(md: str) -> str:
        return "\n".join(ln for ln in md.splitlines() if not ln.startswith("Generated:"))

    assert _no_gen(base_md) == _no_gen(diag_md)


def test_flag_on_surfaces_pooling_block_in_json_and_md(tmp_path, monkeypatch):
    """With the flag on AND diagnostics supplied, the card carries the warm-up band."""
    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")

    card = write_run_card(
        "run-pool",
        _config(),
        _metrics(),
        quant_home=tmp_path,
        pooling_diagnostics=_pooling(),
    )

    assert card["hierarchical_pooling"] == _pooling()
    loaded = _load_card(tmp_path / "runs" / "run-pool" / "run_card.json")
    assert loaded["hierarchical_pooling"]["headline_in_warmup"] is True
    assert loaded["hierarchical_pooling"]["cells"]["kronos|volatile"]["effective_n"] == 3.0

    md = (tmp_path / "runs" / "run-pool" / "run_card.md").read_text(encoding="utf-8")
    assert "## Hierarchical Pooling" in md
    assert "headline_in_warmup: True" in md
    assert "effective_n" in md


def test_flag_on_but_no_diagnostics_omits_block(tmp_path, monkeypatch):
    """Flag on but the caller passes no diagnostics → no block (nothing to surface)."""
    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")

    card = write_run_card("run-none", _config(), _metrics(), quant_home=tmp_path)
    assert "hierarchical_pooling" not in card
    md = (tmp_path / "runs" / "run-none" / "run_card.md").read_text(encoding="utf-8")
    assert "Hierarchical Pooling" not in md
