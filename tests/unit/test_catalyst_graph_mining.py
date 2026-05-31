"""W5 B10 learned-graph miner — the eval gate as pytest acceptance criteria.

These tests ARE the gate (docs/plans/selfevolve-W5-graph-mining.md §4). They run
with NO network (the forward-return fetcher is injected, exactly like
test_catalyst_profitability_cron.py). The eval-gate command is:

    pytest tests/unit/test_catalyst_graph_mining.py -q

Numbering matches the plan's acceptance-criteria list (tests 1-17) plus the
SAFETY-frame tests (advisory-plane-only import audit, silence-only multiplier,
never-touch-seed-YAML, default-OFF no-op).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from hermes_quant.catalyst import graph_mining
from hermes_quant.catalyst.eval import SignCase
from hermes_quant.catalyst.graph_mining import (
    MIN_HIT_RATE,
    MIN_SAMPLE,
    EdgeEvidence,
    flip_passes_sign_consistency,
    format_report,
    mine_graph,
    write_candidates,
)
from hermes_quant.catalyst.profitability import (
    MIN_HIT_RATE as PROF_MIN_HIT_RATE,
)
from hermes_quant.catalyst.profitability import (
    MIN_SAMPLE as PROF_MIN_SAMPLE,
)
from hermes_quant.catalyst.profitability import (
    measure_profitability,
)
from hermes_quant.catalyst.propagation import PropagationEdge

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Most tests exercise the miner's behavior, which requires the flag ON.
    The default-OFF tests explicitly override this."""
    monkeypatch.setenv("HERMES_QUANT_GRAPH_MINING", "1")


def _write_log(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a propagation-log.jsonl with the given rows (one JSON dict per line)."""
    p = tmp_path / "propagation-log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _row(symbol: str, source: str, relation: str, symbol_sign: int, asof: str) -> dict:
    """One propagation-log row in the exact shape propagate(..., log=) emits
    (propagation.py:361-368) + the asof stamp log_propagations adds."""
    return {
        "symbol": symbol,
        "source": source,
        "relation": relation,
        "effect_sign": -1,
        "weight": 0.8,
        "symbol_sign": symbol_sign,
        "catalyst_sign": -1,
        "asof": asof,
    }


# A tiny curated graph so the join is deterministic and offline.
_GRAPH = {
    "src_a": [
        PropagationEdge("src_a", "AAA", "sector_member", -1, 0.8),
        PropagationEdge("src_a", "BBB", "sector_member", -1, 0.7),
    ],
}

_ASOF = "2024-01-02T13:00:00+00:00"


def _ev(*, n: int, hits: int, eff: int = -1, w: float = 0.8) -> EdgeEvidence:
    return EdgeEvidence(
        source="src_a",
        target_symbol="AAA",
        relation="sector_member",
        curated_effect_sign=eff,
        curated_weight=w,
        n_scored=n,
        hits=hits,
        sum_signed_return=float(hits),  # arbitrary, sign-consistent for examples
    )


# ===========================================================================
# 1-5. Mechanism / correctness (the per-edge join is honest)
# ===========================================================================


def test_mine_groups_per_edge(tmp_path):  # 1
    """Two edges from the same source to different symbols -> TWO EdgeEvidence
    entries keyed (source, target, relation). The delta from profitability.py."""
    rows = [_row("AAA", "src_a", "sector_member", -1, _ASOF) for _ in range(3)]
    rows += [_row("BBB", "src_a", "sector_member", -1, _ASOF) for _ in range(3)]
    log = _write_log(tmp_path, rows)
    ev = mine_graph(lambda s, d: -5.0, path=log, graph=_GRAPH)
    keys = set(ev.keys())
    assert ("src_a", "AAA", "sector_member") in keys
    assert ("src_a", "BBB", "sector_member") in keys
    assert len(keys) == 2


def test_hit_test_matches_profitability(tmp_path):  # 2
    """For a single-edge log, sign_hit_rate/mean_signed_return equal what
    measure_profitability reports for that relation (same join, only grouping differs)."""
    # mix of hits and misses on one edge
    rows = [_row("AAA", "src_a", "sector_member", -1, _ASOF) for _ in range(5)]

    # fetcher: 3 negative (hit, since symbol_sign=-1), 2 positive (miss). Make it
    # deterministic by symbol+call-count via a closure counter.
    calls = {"n": 0}

    def fetcher(sym, d):
        calls["n"] += 1
        return -2.0 if calls["n"] <= 3 else 4.0

    log = _write_log(tmp_path, rows)
    ev = mine_graph(fetcher, path=log, graph=_GRAPH)
    edge = ev[("src_a", "AAA", "sector_member")]

    # reset and run profitability on the SAME log/fetcher sequence
    calls["n"] = 0
    stats = measure_profitability(fetcher, path=log)
    rel = stats["sector_member"]

    assert edge.sign_hit_rate == pytest.approx(rel.hit_rate)
    assert edge.mean_signed_return == pytest.approx(rel.mean_signed_return)
    assert edge.n_scored == rel.n_scored


def test_empty_or_missing_log_is_silent(tmp_path):  # 3
    """Missing/empty log -> mine_graph returns {} (silence-by-default)."""
    missing = tmp_path / "nope.jsonl"
    assert mine_graph(lambda s, d: -5.0, path=missing, graph=_GRAPH) == {}
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert mine_graph(lambda s, d: -5.0, path=empty, graph=_GRAPH) == {}


def test_fetcher_none_or_flat_unscored(tmp_path):  # 4
    """A row whose fetcher returns None or 0 is not scored (profitability.py:121)."""
    rows = [_row("AAA", "src_a", "sector_member", -1, _ASOF) for _ in range(3)]
    log = _write_log(tmp_path, rows)
    assert mine_graph(lambda s, d: None, path=log, graph=_GRAPH) == {}
    assert mine_graph(lambda s, d: 0, path=log, graph=_GRAPH) == {}


def test_asof_is_lookahead_honest(tmp_path):  # 5
    """The fetcher is called with the row's asof date parsed from ISO; a row with
    no/invalid asof is skipped (profitability.py:113-119)."""
    seen: list = []

    def fetcher(sym, d):
        seen.append(d)
        return -3.0

    good = _row("AAA", "src_a", "sector_member", -1, "2024-03-15T09:30:00Z")
    bad = _row("AAA", "src_a", "sector_member", -1, "not-a-date")
    noasof = _row("AAA", "src_a", "sector_member", -1, _ASOF)
    del noasof["asof"]
    log = _write_log(tmp_path, [good, bad, noasof])
    ev = mine_graph(fetcher, path=log, graph=_GRAPH)
    # only the good row was scored
    assert ev[("src_a", "AAA", "sector_member")].n_scored == 1
    assert seen == [datetime.fromisoformat("2024-03-15T09:30:00+00:00").date()]


# ===========================================================================
# 6-10. Eval-gate thresholds (MIN_SAMPLE / MIN_HIT_RATE — the held-out bar)
# ===========================================================================


def test_reuses_profitability_bars():  # bar provenance
    """The miner reuses profitability's MIN_SAMPLE/MIN_HIT_RATE — no second const."""
    assert MIN_SAMPLE is PROF_MIN_SAMPLE
    assert MIN_HIT_RATE is PROF_MIN_HIT_RATE


def test_below_min_sample_keeps_silent():  # 6
    """n_scored < MIN_SAMPLE -> KEEP and multiplier 1.0 regardless of hit-rate."""
    ev = _ev(n=MIN_SAMPLE - 1, hits=0)  # 0% hit-rate but thin evidence
    assert ev.verdict == "KEEP"
    assert ev.confidence_multiplier == 1.0
    assert ev.suggested_effect_sign == ev.curated_effect_sign  # no flip on thin data


def test_inverted_edge_flips():  # 7
    """n_scored >= MIN_SAMPLE and sign_hit_rate < 0.5 -> FLIP_SIGN and
    suggested_effect_sign == -curated_effect_sign."""
    ev = _ev(n=MIN_SAMPLE + 10, hits=3, eff=-1)  # ~10% hit-rate
    assert ev.sign_hit_rate < 0.5
    assert ev.verdict == "FLIP_SIGN"
    assert ev.suggested_effect_sign == 1


def test_weak_edge_downweights():  # 8
    """MIN_SAMPLE cleared, 0.5 <= hit < MIN_HIT_RATE -> DOWNWEIGHT and
    0.0 < confidence_multiplier < 1.0."""
    n = 100
    hits = 55  # 0.55, between 0.5 and 0.6
    ev = _ev(n=n, hits=hits)
    assert 0.5 <= ev.sign_hit_rate < MIN_HIT_RATE
    assert ev.verdict == "DOWNWEIGHT"
    assert 0.0 < ev.confidence_multiplier < 1.0


def test_coinflip_edge_prunes():  # 9
    """hit_rate == 0.5 exactly, cleared -> multiplier taper hits 0.0 -> PRUNE."""
    ev = _ev(n=100, hits=50)
    assert ev.sign_hit_rate == 0.5
    assert ev.confidence_multiplier == 0.0
    assert ev.verdict == "PRUNE"


def test_clears_bar_keeps():  # 10
    """hit_rate >= MIN_HIT_RATE, cleared -> KEEP, multiplier 1.0."""
    ev = _ev(n=100, hits=70)  # 0.70 >= 0.60
    assert ev.sign_hit_rate >= MIN_HIT_RATE
    assert ev.verdict == "KEEP"
    assert ev.confidence_multiplier == 1.0


# ===========================================================================
# 11-15. SAFETY frame (the load-bearing invariants)
# ===========================================================================


@pytest.mark.parametrize("hits", list(range(0, 101, 5)))
def test_multiplier_is_silence_only(hits):  # 11
    """For ALL evidence, 0.0 <= confidence_multiplier <= 1.0 (property test over
    hit-rates): it NEVER exceeds 1.0 -> never amplifies above the curated weight."""
    ev = _ev(n=100, hits=hits)
    assert 0.0 <= ev.confidence_multiplier <= 1.0


def test_write_candidates_never_touches_seed_yaml(tmp_path, monkeypatch):  # 12
    """Run mine_graph + write_candidates; assert the seed YAML bytes are unchanged
    AND only the candidate file was written (never auto-mutate the seed YAML)."""
    seed = tmp_path / "propagation_graph.yaml"
    seed_bytes = b"edges:\n  src_a:\n    - {target: AAA, relation: sector_member, effect_sign: -1, weight: 0.8}\n"
    seed.write_bytes(seed_bytes)
    monkeypatch.setattr("hermes_quant.catalyst.propagation.graph_path", lambda: seed)
    # also point graph_mining's default candidate path into tmp
    cand = tmp_path / "graph-mine-candidates.json"

    rows = [_row("AAA", "src_a", "sector_member", -1, _ASOF) for _ in range(MIN_SAMPLE + 5)]
    log = _write_log(tmp_path, rows)
    # all misses -> FLIP_SIGN actionable -> a candidate gets written
    ev = mine_graph(lambda s, d: 5.0, path=log, graph=_GRAPH)
    n = write_candidates(ev, path=cand)

    assert n >= 1
    assert seed.read_bytes() == seed_bytes  # seed YAML untouched, byte-for-byte
    assert cand.exists()  # only the candidate file was written


def test_only_actionable_verdicts_emitted(tmp_path):  # 13
    """write_candidates writes only FLIP_SIGN/DOWNWEIGHT/PRUNE rows; KEEP not in diff."""
    keep = _ev(n=100, hits=70)  # KEEP
    flip = EdgeEvidence("src_a", "BBB", "sector_member", -1, 0.7, n_scored=100, hits=10)
    cand = tmp_path / "cand.json"
    n = write_candidates({("k1",): keep, ("k2",): flip}, path=cand)
    assert n == 1
    payload = json.loads(cand.read_text())
    verdicts = {c["verdict"] for c in payload["candidates"]}
    assert verdicts == {"FLIP_SIGN"}
    assert "KEEP" not in verdicts


def test_candidates_carry_provenance(tmp_path):  # 14
    """Every candidate row carries provenance + generated_at (Oracle-Fallacy tag);
    the candidate file is NOT a path mine_graph reads (no self-ingestion)."""
    flip = _ev(n=100, hits=10)
    cand = tmp_path / "graph-mine-candidates.json"
    write_candidates({("k",): flip}, path=cand)
    payload = json.loads(cand.read_text())
    assert payload["provenance"] == "graph_mining.mine_graph"
    for row in payload["candidates"]:
        assert row["provenance"] == "graph_mining.mine_graph"
        assert "generated_at" in row
    # self-ingestion guard: feeding the candidate file to mine_graph yields {} (it is
    # not a propagation log — every "row" is the JSON object, not per-line edges).
    assert mine_graph(lambda s, d: -5.0, path=cand, graph=_GRAPH) == {}


def test_flip_requires_sign_consistency(tmp_path):  # 15
    """A FLIP_SIGN evidence whose flipped graph FAILS run_sign_consistency is tagged
    sign_consistency_passed=False in the diff (don't flip on noise)."""
    # Curated graph: blue origin -> RKLB bearish on negative catalyst (the real seed).
    # Flipping that edge's sign makes the disaster-bullish, which FAILS the check.
    flip = EdgeEvidence("blue origin", "RKLB", "competitor", -1, 0.85, n_scored=100, hits=10)
    sign_cases = [
        SignCase("Blue Origin New Glenn rocket explodes during test", "RKLB", "negative", "bearish"),
    ]
    # direct helper: flipping a correctly-signed edge breaks consistency
    assert flip_passes_sign_consistency(flip, sign_cases) is False

    cand = tmp_path / "cand.json"
    write_candidates({("k",): flip}, path=cand, sign_cases=sign_cases)
    payload = json.loads(cand.read_text())
    row = payload["candidates"][0]
    assert row["verdict"] == "FLIP_SIGN"
    assert row["sign_consistency_passed"] is False


# ===========================================================================
# 16. Default-OFF (byte-identical off-state)
# ===========================================================================


def test_disabled_returns_empty(tmp_path, monkeypatch):  # 16
    """With HERMES_QUANT_GRAPH_MINING unset/"0", mine_graph returns {} and
    write_candidates writes nothing (the file is never created)."""
    rows = [_row("AAA", "src_a", "sector_member", -1, _ASOF) for _ in range(MIN_SAMPLE + 5)]
    log = _write_log(tmp_path, rows)

    monkeypatch.delenv("HERMES_QUANT_GRAPH_MINING", raising=False)
    assert mine_graph(lambda s, d: 5.0, path=log, graph=_GRAPH) == {}

    monkeypatch.setenv("HERMES_QUANT_GRAPH_MINING", "0")
    assert mine_graph(lambda s, d: 5.0, path=log, graph=_GRAPH) == {}

    # write_candidates is also gated: even handed actionable evidence, it no-ops.
    flip = _ev(n=100, hits=10)
    cand = tmp_path / "graph-mine-candidates.json"
    assert write_candidates({("k",): flip}, path=cand) == 0
    assert not cand.exists()


def test_empty_corpus_report_is_silent():
    """format_report on no evidence -> a 'no scored edges' line (silence-by-default)."""
    assert "no scored edges" in format_report({})


# ===========================================================================
# ADVISORY-PLANE-ONLY: the module imports NONE of the risk/governance/sizing surface
# ===========================================================================


def test_module_is_advisory_plane_only():
    """graph_mining must NOT import the risk gate, kill-switch/governance, or the
    discrete sizing ladder — it lives entirely in the advisory plane (ADR-0080 D80.1).
    Static import audit over the module source (catches transitive risk-surface pulls
    via the names it binds at module scope)."""
    src = Path(graph_mining.__file__).read_text(encoding="utf-8")
    forbidden = [
        "risk.gate",
        "risk import gate",
        "from hermes_quant.risk",
        "import hermes_quant.risk",
        "governance",
        "kill_switch",
        "killswitch",
        "sizing_ladder",
        "from hermes_quant.aggregators",
        "from hermes_quant.agents",
    ]
    for token in forbidden:
        assert token not in src, f"graph_mining must not reference {token!r}"

    # And dynamically: nothing the module pulled in transitively binds a risk gate.
    mod_dict = vars(graph_mining)
    for name, obj in mod_dict.items():
        modname = getattr(obj, "__module__", "") or ""
        assert "risk" not in modname, f"{name} comes from a risk module ({modname})"
        assert "governance" not in modname, f"{name} comes from governance ({modname})"


# ===========================================================================
# 17. Cron watchdog (mirror test_catalyst_profitability_cron.py exactly)
# ===========================================================================


def _load_cron_module():
    """Import the ops script execv-safely (it re-execs the venv at import)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-catalyst-graph-mine.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_catalyst_graph_mine", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.executable
    try:
        sys.executable = str(venv_py)  # neutralize the script's execv guard
        spec.loader.exec_module(mod)
    finally:
        sys.executable = saved
    return mod


@pytest.fixture(scope="module")
def cron():
    return _load_cron_module()


def _cron_ev(*, n: int, hits: int, target: str = "AAA") -> EdgeEvidence:
    return EdgeEvidence(
        source="src_a",
        target_symbol=target,
        relation="sector_member",
        curated_effect_sign=-1,
        curated_weight=0.8,
        n_scored=n,
        hits=hits,
        sum_signed_return=float(hits),
    )


def test_cron_transition_first_clearance(cron):
    cur = {"src_a|AAA|sector_member": {"cleared": True, "verdict": "FLIP_SIGN"}}
    baseline = {"src_a|AAA|sector_member": {"cleared": False, "verdict": "KEEP"}}
    out = cron._transitions(cur, baseline)
    assert out == ["src_a|AAA|sector_member CLEARED MIN_SAMPLE (FLIP_SIGN)"]


def test_cron_transition_standing_state_silent(cron):
    state = {"src_a|AAA|sector_member": {"cleared": True, "verdict": "KEEP"}}
    assert cron._transitions(state, state) == []


def test_cron_transition_new_uncleared_silent(cron):
    cur = {"src_a|AAA|sector_member": {"cleared": False, "verdict": "KEEP"}}
    assert cron._transitions(cur, {}) == []


def test_cron_transition_verdict_flip(cron):
    cur = {"src_a|AAA|sector_member": {"cleared": True, "verdict": "PRUNE"}}
    baseline = {"src_a|AAA|sector_member": {"cleared": True, "verdict": "DOWNWEIGHT"}}
    out = cron._transitions(cur, baseline)
    assert out == ["src_a|AAA|sector_member verdict DOWNWEIGHT -> PRUNE"]


def test_cron_silent_when_no_evidence(cron, monkeypatch, tmp_path, capsys):  # 17a
    monkeypatch.setattr(cron, "mine_graph", lambda *a, **k: {})
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert cron.main() == 0
    assert capsys.readouterr().out == ""


def test_cron_silent_when_unchanged(cron, monkeypatch, tmp_path, capsys):  # 17b
    ev = {("src_a", "AAA", "sector_member"): _cron_ev(n=MIN_SAMPLE + 5, hits=MIN_SAMPLE + 5)}
    monkeypatch.setattr(cron, "mine_graph", lambda *a, **k: ev)
    verdict = ev[("src_a", "AAA", "sector_member")].verdict
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"src_a|AAA|sector_member": {"cleared": True, "verdict": verdict}}))
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert cron.main() == 0
    assert capsys.readouterr().out == ""


def test_cron_emits_on_edge_clearance(cron, monkeypatch, tmp_path, capsys):  # 17c
    ev = {("src_a", "AAA", "sector_member"): _cron_ev(n=MIN_SAMPLE + 1, hits=5)}  # FLIP_SIGN
    monkeypatch.setattr(cron, "mine_graph", lambda *a, **k: ev)
    monkeypatch.setattr(cron, "write_candidates", lambda *a, **k: 1)  # no real write
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"src_a|AAA|sector_member": {"cleared": False, "verdict": "KEEP"}}))
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert cron.main() == 0
    out = capsys.readouterr().out
    assert "CLEARED MIN_SAMPLE" in out
    assert "src_a|AAA|sector_member" in out


def test_cron_emits_on_verdict_flip(cron, monkeypatch, tmp_path, capsys):  # 17d
    ev = {("src_a", "AAA", "sector_member"): _cron_ev(n=MIN_SAMPLE + 10, hits=5)}  # FLIP_SIGN
    monkeypatch.setattr(cron, "mine_graph", lambda *a, **k: ev)
    monkeypatch.setattr(cron, "write_candidates", lambda *a, **k: 1)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"src_a|AAA|sector_member": {"cleared": True, "verdict": "DOWNWEIGHT"}}))
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert cron.main() == 0
    out = capsys.readouterr().out
    assert "verdict DOWNWEIGHT -> FLIP_SIGN" in out


def test_cron_verbose_always_prints(cron, monkeypatch, tmp_path, capsys):  # 17e
    ev = {("src_a", "AAA", "sector_member"): _cron_ev(n=MIN_SAMPLE + 5, hits=MIN_SAMPLE + 5)}
    monkeypatch.setattr(cron, "mine_graph", lambda *a, **k: ev)
    verdict = ev[("src_a", "AAA", "sector_member")].verdict
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"src_a|AAA|sector_member": {"cleared": True, "verdict": verdict}}))
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    monkeypatch.setattr(sys, "argv", ["prog", "--verbose"])
    assert cron.main() == 0
    out = capsys.readouterr().out
    assert "per-edge mining" in out.lower()


def test_cron_flag_off_silent(cron, monkeypatch, tmp_path, capsys):  # 17f / 16-cron
    """With the flag OFF, mine_graph returns {} -> the cron is silent (no_agent)."""
    monkeypatch.delenv("HERMES_QUANT_GRAPH_MINING", raising=False)
    # Use the REAL mine_graph (not stubbed) so the flag gate is what produces {}.
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert cron.main() == 0
    assert capsys.readouterr().out == ""
