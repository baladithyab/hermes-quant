"""ADR-0092 Phase-4 — the OFFLINE parity-report harness over the shadow log.

``ops/scripts/quant-pdr-core-parity-report.py`` READS
``<quant_home>/pdr-core-shadow-divergence.jsonl`` (the sink the shadow seam
appends to) and summarizes pdr_core-vs-shell-gate agreement: total records,
diverged count, agreement rate, a per-field divergence tally, and the
first/last asof (the window). It is READ-ONLY, never reads live market data,
never mutates anything, and is silence-by-default (a missing/empty log =>
"no divergence records yet", exit 0).

RED-proof for the agreement-rate math: a fixture with a known diverged count.
If the harness counted divergences wrong, ``agreement_rate`` flips and
``test_agreement_rate_and_field_tally`` fails.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "scripts"
    / "quant-pdr-core-parity-report.py"
)
_LOG_NAME = "pdr-core-shadow-divergence.jsonl"


def _load():
    spec = importlib.util.spec_from_file_location("quant_pdr_core_parity_report", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quant_pdr_core_parity_report"] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load()


def _rec(asof: str, diverged: bool, fields: list[str]) -> dict:
    return {
        "asof": asof,
        "diverged": diverged,
        "fields": fields,
        "live": {"target_position_pct": 0.1, "reason": "x"},
        "shadow": {"target_position_pct": 0.0, "reason": "y"},
    }


def _write_log(home: Path, records: list[dict]) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    log = home / _LOG_NAME
    with log.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return log


# ===========================================================================
# Core summary math — the agreement rate + per-field tally.
# ===========================================================================


def test_agreement_rate_and_field_tally(tmp_path, monkeypatch):
    """A fixture with 5 records, 2 diverged => 3/5 agreement (0.60). The
    per-field tally counts which Action fields diverged most.

    RED-PROOF: the agreement rate is computed from the diverged COUNT; if the
    harness miscounted (e.g. counted 1 or 3 instead of 2), this 0.60 assertion
    flips and fails."""
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    _write_log(
        tmp_path,
        [
            _rec("2026-06-18T10:00:00Z", False, []),
            _rec("2026-06-18T10:05:00Z", True, ["target_position_pct"]),
            _rec("2026-06-18T10:10:00Z", False, []),
            _rec("2026-06-18T10:15:00Z", True, ["target_position_pct", "reason"]),
            _rec("2026-06-18T10:20:00Z", False, []),
        ],
    )

    summary = H.summarize_log(tmp_path / _LOG_NAME)

    assert summary["total"] == 5
    assert summary["diverged"] == 2
    assert summary["agreed"] == 3
    assert summary["agreement_rate"] == 0.6
    # per-field tally: target_position_pct in 2 diverged records, reason in 1
    assert summary["field_tally"]["target_position_pct"] == 2
    assert summary["field_tally"]["reason"] == 1
    # window first/last asof
    assert summary["first_asof"] == "2026-06-18T10:00:00Z"
    assert summary["last_asof"] == "2026-06-18T10:20:00Z"


def test_all_agreed_is_full_agreement(tmp_path):
    """All records agreed => agreement_rate 1.0, empty field tally."""
    _write_log(
        tmp_path,
        [
            _rec("2026-06-18T10:00:00Z", False, []),
            _rec("2026-06-18T10:01:00Z", False, []),
        ],
    )
    summary = H.summarize_log(tmp_path / _LOG_NAME)
    assert summary["total"] == 2
    assert summary["diverged"] == 0
    assert summary["agreement_rate"] == 1.0
    assert summary["field_tally"] == {}


def test_all_diverged_is_zero_agreement(tmp_path):
    """All records diverged => agreement_rate 0.0; the tally counts every field."""
    _write_log(
        tmp_path,
        [
            _rec("2026-06-18T10:00:00Z", True, ["reason"]),
            _rec("2026-06-18T10:01:00Z", True, ["reason", "halt"]),
        ],
    )
    summary = H.summarize_log(tmp_path / _LOG_NAME)
    assert summary["diverged"] == 2
    assert summary["agreement_rate"] == 0.0
    assert summary["field_tally"]["reason"] == 2
    assert summary["field_tally"]["halt"] == 1


def test_field_tally_ranked_most_divergent_first(tmp_path):
    """The per-field tally is ordered most-divergent first (the operator wants to
    see which Action field diverges most)."""
    _write_log(
        tmp_path,
        [
            _rec("t1", True, ["reason"]),
            _rec("t2", True, ["reason"]),
            _rec("t3", True, ["reason", "target_position_pct"]),
            _rec("t4", True, ["halt"]),
        ],
    )
    summary = H.summarize_log(tmp_path / _LOG_NAME)
    items = list(summary["field_tally"].items())
    # reason (3) must come before target_position_pct (1) and halt (1)
    assert items[0] == ("reason", 3)


# ===========================================================================
# Silence-by-default — missing / empty / malformed log never crashes.
# ===========================================================================


def test_missing_log_is_clean_no_records(tmp_path):
    """A missing log file => 'no records' summary, total 0, never crashes."""
    summary = H.summarize_log(tmp_path / _LOG_NAME)  # file does not exist
    assert summary["total"] == 0
    assert summary["diverged"] == 0
    assert summary["agreement_rate"] is None  # undefined over zero records
    assert summary["field_tally"] == {}


def test_empty_log_is_clean_no_records(tmp_path):
    """An empty (zero-byte) log => 'no records', total 0."""
    (tmp_path / _LOG_NAME).write_text("", encoding="utf-8")
    summary = H.summarize_log(tmp_path / _LOG_NAME)
    assert summary["total"] == 0
    assert summary["agreement_rate"] is None


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    """A torn / non-JSON line is skipped (the harness reads line-by-line); the
    valid records still summarize. Best-effort read, never crash."""
    home = tmp_path
    home.mkdir(parents=True, exist_ok=True)
    log = home / _LOG_NAME
    with log.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_rec("t1", False, [])) + "\n")
        f.write("{not valid json\n")  # torn line
        f.write(json.dumps(_rec("t2", True, ["reason"])) + "\n")
    summary = H.summarize_log(log)
    assert summary["total"] == 2  # the two valid lines
    assert summary["diverged"] == 1
    assert summary["agreement_rate"] == 0.5


# ===========================================================================
# main() — exit codes, --json, --home resolution, silence-by-default.
# ===========================================================================


def test_main_missing_log_exits_zero_with_no_records_message(tmp_path, capsys, monkeypatch):
    """main over a home with no log: prints 'no divergence records yet', exit 0
    (silence-by-default — a missing log is a valid honest state, not an error)."""
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    rc = H.main([])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "no divergence records" in out


def test_main_human_summary_exit_zero(tmp_path, capsys):
    """main prints a human summary + exits 0 over a populated log."""
    _write_log(
        tmp_path,
        [
            _rec("2026-06-18T10:00:00Z", False, []),
            _rec("2026-06-18T10:05:00Z", True, ["reason"]),
        ],
    )
    rc = H.main(["--home", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "agreement" in out.lower()
    assert "2" in out  # total records


def test_main_json_output(tmp_path, capsys):
    """--json emits the machine summary dict; the human prose is suppressed."""
    _write_log(
        tmp_path,
        [
            _rec("2026-06-18T10:00:00Z", False, []),
            _rec("2026-06-18T10:05:00Z", True, ["reason"]),
            _rec("2026-06-18T10:10:00Z", False, []),
        ],
    )
    rc = H.main(["--home", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 3
    assert payload["diverged"] == 1
    assert payload["agreement_rate"] == round(2 / 3, 6)
    assert payload["field_tally"]["reason"] == 1


def test_main_home_resolution_honors_hermes_quant_home(tmp_path, capsys, monkeypatch):
    """With no --home, main resolves the log via quant_home() (HERMES_QUANT_HOME)."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    _write_log(tmp_path, [_rec("t1", True, ["halt"])])
    rc = H.main(["--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["diverged"] == 1


def test_main_explicit_home_overrides_env(tmp_path, capsys, monkeypatch):
    """An explicit --home is threaded as the quant_home override (precedence #1)."""
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path / "env_home"))
    explicit = tmp_path / "explicit"
    _write_log(explicit, [_rec("t1", False, []), _rec("t2", False, [])])
    rc = H.main(["--home", str(explicit), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 2
    assert payload["agreement_rate"] == 1.0


# ===========================================================================
# AGGREGATE (PERCEIVE) layer — the comparable / not-comparable split.
#
# The aggregate-layer shadow (run_shadow_aggregate) marks a tick
# ``comparable: false`` when the live BMA diverges from the cold-start core port
# BY DESIGN (a fitted isotonic calibrator or a set learning flag). Those ticks
# must be EXCLUDED from the agreement rate — counting them as diverged would
# slander a faithful port. The gate log carries no ``comparable`` key, so every
# gate record defaults comparable (the prior gate-only behavior is byte-identical).
# ===========================================================================

_AGG_LOG_NAME = "pdr-core-shadow-aggregate-divergence.jsonl"


def _agg_rec(asof, *, comparable=True, diverged=False, fields=None, reason=None) -> dict:
    rec = {"asof": asof, "comparable": comparable, "diverged": diverged,
           "fields": fields or []}
    if reason is not None:
        rec["reason"] = reason
    return rec


def _write_agg_log(home: Path, records: list[dict]) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    log = home / _AGG_LOG_NAME
    with log.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return log


def test_aggregate_agreement_rate_excludes_not_comparable(tmp_path):
    """5 records: 2 comparable-agree, 1 comparable-diverge, 2 not-comparable
    (by-design skip). Agreement rate is 2/3 over the COMPARABLE ticks only —
    the 2 skips do NOT drag the rate down.

    RED-PROOF: if not-comparable ticks were counted as diverged, the rate would
    be 2/5 (0.40) not 2/3 (0.667) — this assertion flips and fails."""
    log = _write_agg_log(
        tmp_path,
        [
            _agg_rec("t1", comparable=True, diverged=False),
            _agg_rec("t2", comparable=True, diverged=False),
            _agg_rec("t3", comparable=True, diverged=True, fields=["direction"]),
            _agg_rec("t4", comparable=False, reason="learning_flag_active"),
            _agg_rec("t5", comparable=False, reason="fitted_calibrator_active"),
        ],
    )
    summary = H.summarize_log(log)
    assert summary["total"] == 5
    assert summary["comparable"] == 3
    assert summary["not_comparable"] == 2
    assert summary["diverged"] == 1
    assert summary["agreed"] == 2
    assert summary["agreement_rate"] == round(2 / 3, 6)
    assert summary["field_tally"] == {"direction": 1}
    assert summary["not_comparable_reasons"] == {
        "fitted_calibrator_active": 1,
        "learning_flag_active": 1,
    }


def test_gate_log_back_compat_all_comparable(tmp_path):
    """A gate-layer record has NO ``comparable`` key => it defaults comparable, so
    the gate report is byte-identical to the prior gate-only behavior."""
    log = _write_log(  # the gate-shaped _rec has no `comparable` key
        tmp_path,
        [
            _rec("t1", False, []),
            _rec("t2", True, ["reason"]),
        ],
    )
    summary = H.summarize_log(log)
    assert summary["total"] == 2
    assert summary["comparable"] == 2
    assert summary["not_comparable"] == 0
    assert summary["agreement_rate"] == 0.5


def test_all_not_comparable_yields_none_rate(tmp_path):
    """Every tick skipped by-design => zero comparable => agreement_rate None
    (undefined), NOT 0.0 — the operator sees 'no comparable sample yet'."""
    log = _write_agg_log(
        tmp_path,
        [
            _agg_rec("t1", comparable=False, reason="fitted_calibrator_active"),
            _agg_rec("t2", comparable=False, reason="fitted_calibrator_active"),
        ],
    )
    summary = H.summarize_log(log)
    assert summary["total"] == 2
    assert summary["comparable"] == 0
    assert summary["agreement_rate"] is None
    assert summary["not_comparable_reasons"] == {"fitted_calibrator_active": 2}


def test_main_layer_aggregate_reads_the_aggregate_log(tmp_path, capsys, monkeypatch):
    """--layer aggregate resolves the aggregate log (not the gate log)."""
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    _write_agg_log(tmp_path, [_agg_rec("t1", comparable=True, diverged=False)])
    # a gate log with a divergence — must NOT be read when --layer aggregate
    _write_log(tmp_path, [_rec("t1", True, ["reason"])])
    rc = H.main(["--layer", "aggregate", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["log_path"].endswith(_AGG_LOG_NAME)
    assert payload["total"] == 1
    assert payload["diverged"] == 0  # read the aggregate log, not the gate's divergence


def test_main_layer_defaults_to_gate(tmp_path, capsys, monkeypatch):
    """--layer defaults to gate (back-compat: the prior CLI had no --layer)."""
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    _write_log(tmp_path, [_rec("t1", True, ["reason"])])
    rc = H.main(["--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["log_path"].endswith(_LOG_NAME)
    assert payload["total"] == 1
    assert payload["diverged"] == 1
