"""Tests for perception artifact stores and CLI surfaces (ADR-0024)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from hermes_quant.artifacts import (
    list_committee_turn_artifacts,
    list_semantic_packets,
    load_committee_turns,
    load_semantic_packet,
    write_semantic_packet,
)
from hermes_quant.cli import dispatch, setup_argparse
from hermes_quant.committee_runner import run_committee_from_packets


def _packet_payload(**overrides):
    payload = {
        "schema_version": 1,
        "asset": "BTC/USDT",
        "asof": "2024-01-01T00:00:00Z",
        "horizon": "1h",
        "stance": "bullish",
        "confidence": 0.75,
        "magnitude": 0.01,
        "summary": "Constructive semantic context.",
        "sources": [{"type": "note", "ref": "unit-test"}],
        "model": "hermes:test",
    }
    payload.update(overrides)
    return payload


def test_write_load_list_semantic_packet(tmp_path: Path):
    path, packet = write_semantic_packet(_packet_payload(), root=tmp_path)
    assert path.exists()
    loaded = load_semantic_packet(path)
    assert loaded.packet_hash == packet["packet_hash"]
    listed = list_semantic_packets(asset="BTC/USDT", root=tmp_path)
    assert listed[0]["packet_hash"] == packet["packet_hash"]


def test_committee_runner_writes_hash_verified_artifact(tmp_path: Path):
    _, packet = write_semantic_packet(_packet_payload(), root=tmp_path / "semantic")
    path, artifact = run_committee_from_packets(
        [packet],
        asset="BTC/USDT",
        asof="2024-01-01T01:00:00Z",
        root=tmp_path / "committee",
    )
    assert path.exists()
    loaded = load_committee_turns(path)
    assert loaded["turns_hash"] == artifact["turns_hash"]
    assert len(loaded["turns"]) == 4
    listed = list_committee_turn_artifacts(asset="BTC/USDT", root=tmp_path / "committee")
    assert listed[0]["turns_hash"] == artifact["turns_hash"]


def test_cli_parses_new_perception_subcommands():
    parser = argparse.ArgumentParser()
    setup_argparse(parser)
    args = parser.parse_args([
        "semantic-packet", "write",
        "--asset", "BTC/USDT",
        "--stance", "neutral",
        "--confidence", "0.4",
        "--magnitude", "0",
        "--summary", "mixed",
    ])
    assert args.quant_cmd == "semantic-packet"
    assert args.semantic_cmd == "write"
    args = parser.parse_args([
        "committee", "run", "--asset", "BTC/USDT", "--semantic-packet-file", "x.json",
    ])
    assert args.committee_cmd == "run"
    args = parser.parse_args(["perception", "start", "--asset", "BTC/USDT", "--dry-run"])
    assert args.perception_cmd == "start"
    args = parser.parse_args(["recipes", "list"])
    assert args.recipes_cmd == "list"
    args = parser.parse_args([
        "committee", "prompt", "--asset", "BTC/USDT", "--semantic-packet-file", "x.json",
    ])
    assert args.committee_cmd == "prompt"


def test_cli_semantic_packet_write_and_validate(tmp_path: Path, capsys):
    parser = argparse.ArgumentParser()
    setup_argparse(parser)
    args = parser.parse_args([
        "semantic-packet", "write",
        "--asset", "BTC/USDT",
        "--horizon", "1h",
        "--stance", "bullish",
        "--confidence", "0.7",
        "--magnitude", "0.01",
        "--summary", "constructive",
        "--source", "note:test",
        "--as-of", "2024-01-01T00:00:00Z",
        "--output-root", str(tmp_path),
        "--json",
    ])
    assert dispatch(args) == 0
    out = json.loads(capsys.readouterr().out)
    path = out["path"]
    assert Path(path).exists()
    args = parser.parse_args([
        "semantic-packet", "validate", path,
        "--asset", "BTC/USDT",
        "--as-of", "2024-01-01T01:00:00Z",
        "--json",
    ])
    assert dispatch(args) == 0
    validate_out = json.loads(capsys.readouterr().out)
    assert validate_out["success"] is True


def test_perception_start_dry_run_contains_cli_writer(capsys):
    parser = argparse.ArgumentParser()
    setup_argparse(parser)
    args = parser.parse_args([
        "perception", "start", "--asset", "BTC/USDT", "--horizon", "1h", "--dry-run", "--json",
    ])
    assert dispatch(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is True
    assert "quant semantic-packet write" in out["prompt"]


def test_perception_status_reports_fresh_packet(tmp_path: Path, capsys):
    from hermes_quant.artifacts import write_semantic_packet
    _, packet = write_semantic_packet(
        _packet_payload(asof=pd.Timestamp.now(tz="UTC").isoformat()),
        root=tmp_path,
    )
    parser = argparse.ArgumentParser()
    setup_argparse(parser)
    args = parser.parse_args([
        "perception", "status", "--recipe-id", "btc-usdt-deliberative", "--packet-root", str(tmp_path), "--json",
    ])
    assert dispatch(args) == 0
    out = json.loads(capsys.readouterr().out)
    row = out["status"]["symbols"][0]
    assert row["status"] == "fresh"
    assert row["latest_packet"]["packet_hash"] == packet["packet_hash"]


def test_committee_prompt_contains_model_ids(tmp_path: Path, capsys):
    _, packet = write_semantic_packet(_packet_payload(), root=tmp_path)
    parser = argparse.ArgumentParser()
    setup_argparse(parser)
    args = parser.parse_args([
        "committee", "prompt", "--asset", "BTC/USDT", "--semantic-packet-file", str(tmp_path / "BTC_USDT" / f"{packet['packet_hash']}.json"), "--models", "openrouter/a,openrouter/b", "--json",
    ])
    assert dispatch(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert "openrouter/a" in out["prompt"]
    assert packet["packet_hash"] in out["prompt"]


def test_backtest_replay_records_semantic_hashes_in_decisions(tmp_path: Path):
    from hermes_quant.backtest.replay import replay
    _, packet = write_semantic_packet(
        _packet_payload(asof="2024-01-01T02:00:00Z"),
        root=tmp_path / "semantic",
    )
    ts = pd.date_range("2024-01-01", periods=80, freq="1h", tz="UTC")
    bars = pd.DataFrame({
        "timestamp": ts,
        "open": [100 + i * 0.1 for i in range(80)],
        "high": [101 + i * 0.1 for i in range(80)],
        "low": [99 + i * 0.1 for i in range(80)],
        "close": [100 + i * 0.1 for i in range(80)],
        "volume": [1000] * 80,
    })
    result = replay(
        bars,
        symbol="BTC/USDT",
        asset_class="crypto",
        timeframe="1h",
        recipe_id="btc-usdt-deliberative",
        semantic_packets=[packet],
        warmup_bars=20,
    )
    assert any(packet["packet_hash"] in d.get("semantic_packet_hashes", []) for d in result.decisions_summary)
