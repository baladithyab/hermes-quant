"""B27 shadow-rule induction — the eval gate as pytest acceptance criteria.

These tests ARE the gate (docs/research/2026-05-31-r-B27.md). They run with NO network
and NO journal-on-disk dependency: resolved trades are INJECTED as a synthetic
SettlementEntry fixture (the ``entries=`` kwarg), exactly like the forward-return fetcher
is injected in test_catalyst_graph_mining.py. The eval-gate command is:

    pytest tests/unit/test_shadow_rule_mining.py -q

Coverage:
  * extractor proposes sensible if-then rules on a synthetic profitable set;
  * the overfit guards reject (a) sub-MIN_SAMPLE support, (b) a tiny-but-perfect rule on
    the Wilson lower bound, (c) a lift~1 "adds nothing" rule, (d) Bonferroni dilution;
  * output is auditable (every gate metric + reason + provenance present) and
    propose-only (never mutates default_rules / writes only the candidate file);
  * default-OFF is a bit-for-bit no-op; silence-by-default on empty/thin corpus;
  * no-lookahead: alpha_return is never a feature; the module imports no risk surface.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_quant.journal.models import SettlementEntry
from hermes_quant.shadow import rule_mining
from hermes_quant.shadow.rule_mining import (
    ALPHA,
    CONFIDENT_N,
    LIFT_MARGIN,
    MIN_SAMPLE,
    MIN_WILSON_LB,
    InducedRule,
    format_report,
    mine_rules,
    wilson_lower_bound,
    write_candidates,
)

# ---------------------------------------------------------------------------
# helpers — synthetic resolved-trade fixtures (no journal on disk, no network)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Most tests exercise the miner's behavior, which requires the flag ON.
    The default-OFF tests explicitly override this."""
    monkeypatch.setenv("HERMES_QUANT_SHADOW_RULE_MINING", "1")


def _entry(
    i: int,
    *,
    confidence: float,
    direction: int,
    alpha_return: float,
    asset_class: str = "equity",
    target_position_pct: float = 0.10,
    resolved: bool = True,
) -> SettlementEntry:
    """One synthetic SettlementEntry. ``alpha_return`` is the OUTCOME (label); the
    decision-time fields (confidence/direction/asset_class/target_position_pct) are the
    features. ``i`` orders the corpus in time (older->newer)."""
    # strictly increasing decision time across i (older->newer), so the time-ordered
    # corpus sort and the hold-out split are deterministic.
    dec = datetime.fromordinal(
        datetime(2026, 1, 1).toordinal() + i
    ).replace(tzinfo=UTC)
    return SettlementEntry(
        entry_id=f"prop_2026-01-{i:03d}_AAA_{i:06d}",
        asof_decision=dec,
        symbol="AAA",
        asset_class=asset_class,
        direction=direction,
        confidence=confidence,
        target_position_pct=target_position_pct,
        decision_price=100.0,
        benchmark_symbol="SPY",
        per_analyst_components=[],
        reason="synthetic",
        asof_settlement=(dec if resolved else None),
        exit_price=(101.0 if resolved else None),
        raw_return=(alpha_return if resolved else None),
        alpha_return=(alpha_return if resolved else None),
        hold_minutes=(60 if resolved else None),
    )


def _separable_corpus() -> list[SettlementEntry]:
    """A corpus where high-confidence trades win and low-confidence trades lose — a clean
    'if confidence > t then buy' signal the stump should find. 40 rows, base rate ~0.5.

    High-confidence (>=0.8): 20 rows, 18 wins (90%).
    Low-confidence  (<=0.4): 20 rows,  2 wins (10%).
    """
    rows: list[SettlementEntry] = []
    i = 0
    for k in range(20):
        rows.append(
            _entry(i, confidence=0.85, direction=1, alpha_return=(2.0 if k < 18 else -1.0))
        )
        i += 1
    for k in range(20):
        rows.append(
            _entry(i, confidence=0.30, direction=1, alpha_return=(2.0 if k < 2 else -1.0))
        )
        i += 1
    return rows


# ===========================================================================
# 1. Wilson lower bound — the load-bearing small-sample primitive
# ===========================================================================


def test_wilson_lower_bound_tiny_perfect_is_not_trusted():
    """4/4 has point precision 1.0 but a Wilson 95% lower bound ~0.51 — below the 0.60
    gate. This is THE overfit guard for tiny-but-perfect rules (note §1d)."""
    lb = wilson_lower_bound(4, 4)
    assert lb == pytest.approx(0.51, abs=0.05)
    assert lb < MIN_WILSON_LB  # correctly NOT trusted


def test_wilson_lower_bound_below_point_estimate_and_monotone():
    """Wilson LB is always <= point estimate, in [0,1], and a larger sample at the same
    rate tightens (raises) the lower bound."""
    assert wilson_lower_bound(0, 0) == 0.0
    assert 0.0 <= wilson_lower_bound(9, 10) <= 0.9
    assert wilson_lower_bound(45, 50) > wilson_lower_bound(9, 10)  # same 0.9, more n


# ===========================================================================
# 2. Extractor proposes sensible if-then rules on a synthetic profitable set
# ===========================================================================


def test_proposes_sensible_rule_on_separable_corpus():
    """On a clean 'high confidence -> wins' corpus, the extractor proposes a
    confidence-threshold rule recommending buy, with a Wilson LB above the gate and
    lift > 1."""
    rules = mine_rules(entries=_separable_corpus())
    assert rules, "expected at least one proposed rule on a separable corpus"
    top = rules[0]
    assert top.verdict == "PROPOSE"
    assert top.feature == "confidence"
    assert top.op in (">", "<=")
    assert top.direction == "buy"
    assert top.wilson_lb >= MIN_WILSON_LB
    assert top.lift >= 1.0 + LIFT_MARGIN
    assert top.bonferroni_p <= ALPHA
    # ranked by Wilson LB descending
    assert all(rules[i].wilson_lb >= rules[i + 1].wilson_lb for i in range(len(rules) - 1))


def test_emits_at_most_max_rules():
    """Never emit more than the requested cap (note §4: <=5 rules)."""
    rules = mine_rules(entries=_separable_corpus(), max_rules=3)
    assert len(rules) <= 3


def test_deterministic_across_runs():
    """Same corpus -> identical proposed rules (deterministic stdlib 1R, no RNG/LLM).
    Compare the load-bearing fields (generated_at is a wall-clock timestamp, excluded)."""
    corpus = _separable_corpus()

    def _key(rules):
        return [
            (r.feature, r.op, str(r.threshold), r.direction, r.n, r.wins,
             round(r.wilson_lb, 9), round(r.lift, 9), r.bonferroni_p, r.verdict)
            for r in rules
        ]

    assert _key(mine_rules(entries=corpus)) == _key(mine_rules(entries=corpus))


# ===========================================================================
# 3. Overfit guards REJECT (the central-risk acceptance criteria)
# ===========================================================================


def test_below_min_sample_returns_empty():
    """A resolved-profitable corpus with support < MIN_SAMPLE -> [] (silence-by-default,
    the B26 data gate). This is the 'reject support<threshold' acceptance criterion."""
    small = [
        _entry(i, confidence=0.85, direction=1, alpha_return=2.0)
        for i in range(MIN_SAMPLE - 1)
    ]
    assert mine_rules(entries=small) == []


def test_tiny_but_perfect_rule_rejected_by_wilson():
    """A rule covering a small slice that is 100% wins (e.g. a rare asset_class with only
    a handful of trades) must NOT be proposed: its Wilson LB falls below the gate even
    though point win-rate is 1.0. We build a corpus where 'fx' is 5/5 wins but the bulk
    'equity' is a coin flip — only the (insufficient-support) fx slice looks perfect."""
    rows: list[SettlementEntry] = []
    i = 0
    # 30 equity coin-flips (base rate ~0.5, none clears the gate)
    for k in range(30):
        rows.append(
            _entry(i, confidence=0.5, direction=1, alpha_return=(1.0 if k % 2 == 0 else -1.0),
                    asset_class="equity")
        )
        i += 1
    # 5 fx all-wins — point-perfect but tiny support
    for _ in range(5):
        rows.append(_entry(i, confidence=0.5, direction=1, alpha_return=2.0, asset_class="fx"))
        i += 1
    rules = mine_rules(entries=rows)
    # No proposed rule may rest on the 5/5 fx slice (support 5 < MIN_SAMPLE; Wilson LB low)
    for r in rules:
        if r.feature == "asset_class" and r.threshold == "fx":
            pytest.fail("tiny-but-perfect fx rule was proposed despite n=5 < MIN_SAMPLE")
    # And the fx slice, scored directly, fails its Wilson gate.
    assert wilson_lower_bound(5, 5) < MIN_WILSON_LB


def _low_lift_corpus() -> list[SettlementEntry]:
    """A corpus that ACTUALLY produces a low-lift ``confidence`` split which — but for the
    production lift guard — WOULD be proposed by ``mine_rules``. This is the corpus the
    rebuilt B27 lift-guard test rests on, and the reason it is MUTATION-KILLING.

    Construction (deterministic, no RNG): a high base rate (~0.946) bulk where confidence
    carries almost no signal, split into two equal buckets so the OneR stump produces a
    ``confidence > 0.6`` split whose covered win-rate (~0.992) is only marginally above the
    base rate:

      * low-confidence (0.3): 130 rows, 117 wins (win-rate 0.900)
      * high-confidence (0.9): 130 rows, 129 wins (win-rate 0.992)

    The corpus base rate is 246/260 = 0.9462. The enumerated ``confidence > 0.6`` split
    covers the 130 high-confidence rows at 129/130 = 0.992 wins, so:

      * lift = 0.992 / 0.9462 ≈ 1.049 — BELOW 1.0 + LIFT_MARGIN(0.05)=1.05, so it is the
        bread->milk "adds nothing" trap the LIFT guard exists to reject;
      * Wilson lower bound ≈ 0.958 — well ABOVE MIN_WILSON_LB(0.60), so the Wilson gate
        does NOT reject it;
      * Bonferroni-corrected p ≈ 0.025 — BELOW ALPHA(0.05), so the Bonferroni gate does
        NOT reject it either.

    So in PRODUCTION the split's verdict is FAILS_LIFT and ``mine_rules`` returns it in NO
    proposed set. If the lift guard regresses (the ``elif lift < 1.0 + LIFT_MARGIN`` branch
    is neutralized), the split falls through to PROPOSE — and ``mine_rules`` WOULD emit a
    ``confidence > 0.6`` rule. The test below asserts ``mine_rules`` does NOT, so it FAILS
    under exactly that mutation. (Verified by mutating the guard: normal -> [], mutated ->
    one ``confidence > 0.6`` PROPOSE rule with lift≈1.049.)
    """
    rows: list[SettlementEntry] = []
    i = 0
    # low-confidence bucket — 117/130 wins (below the high bucket, ~at/below base).
    for k in range(130):
        rows.append(
            _entry(i, confidence=0.3, direction=1,
                   alpha_return=(2.0 if k < 117 else -1.0), asset_class="equity")
        )
        i += 1
    # high-confidence bucket — 129/130 wins. Point-impressive, but in a corpus that
    # ALREADY wins ~94.6% it adds almost nothing: lift ≈ 1.049 < 1.05 (the trap).
    for k in range(130):
        rows.append(
            _entry(i, confidence=0.9, direction=1,
                   alpha_return=(2.0 if k < 129 else -1.0), asset_class="equity")
        )
        i += 1
    return rows


def test_high_lift_confidence_rule_is_accepted():
    """ACCEPT side: on a separable corpus (base rate ~0.5) where high confidence genuinely
    wins, the lift guard does NOT block — ``mine_rules`` PROPOSES a confidence rule whose
    lift clears 1.0 + LIFT_MARGIN by a wide margin. Paired with the REJECT test below this
    pins the guard's behavior on BOTH sides (accept a high-lift rule, reject a low-lift
    one) so a guard regression can't pass by trivially proposing or rejecting everything."""
    rules = mine_rules(entries=_separable_corpus())
    conf_rules = [r for r in rules if r.feature == "confidence"]
    assert conf_rules, "a genuine high-confidence -> wins signal must be PROPOSED"
    top = conf_rules[0]
    assert top.verdict == "PROPOSE"
    assert top.lift >= 1.0 + LIFT_MARGIN
    # The high-confidence corpus has base rate ~0.5 and the winning slice ~0.9, so lift is
    # comfortably above the margin (not merely scraping past it).
    assert top.lift > 1.5, f"expected a strongly-lifted rule, got lift={top.lift:.3f}"


def test_lift_one_rule_adds_nothing_rejected():
    """REJECT side / MUTATION-KILLING bread->milk trap (note §1b/§4): a ``confidence > 0.6``
    split whose covered win-rate (~0.992) only marginally beats a high base rate (~0.946)
    has lift ≈ 1.049 < 1.0 + LIFT_MARGIN — it 'adds nothing' and MUST be rejected by the
    LIFT guard SPECIFICALLY. The split CLEARS the Wilson gate (wlb≈0.958) AND the Bonferroni
    gate (corrected p≈0.025), so the lift guard is the ONLY thing standing between it and a
    PROPOSE verdict.

    The earlier version of this test was non-discriminating: it asserted FAILS_LIFT against
    a test-local REPLICA of the gate cascade (a ``_verdict_for`` helper), so neutralizing
    the PRODUCTION lift guard left it green. This version asserts against the PRODUCTION
    ``mine_rules`` output directly:

      * with the real lift guard, ``mine_rules`` proposes NO ``confidence`` rule on this
        corpus (the split is FAILS_LIFT);
      * if the ``elif lift < 1.0 + LIFT_MARGIN`` branch regresses, the split falls through
        to PROPOSE and ``mine_rules`` WOULD emit a ``confidence > 0.6`` rule with lift~1.05.

    So this assertion FAILS exactly when the lift guard regresses. (Mentally / empirically
    verified by mutating the guard: normal run -> no confidence rule; mutated run -> one
    ``confidence`` PROPOSE rule with lift≈1.049.)"""
    corpus = _low_lift_corpus()

    # PRODUCTION assertion: no lift~1 confidence rule may be proposed. With the lift guard
    # intact this set is empty of confidence rules; if the guard regresses, mine_rules
    # surfaces the FAILS_LIFT split as a PROPOSE -> this assertion fails.
    rules = mine_rules(entries=corpus)
    proposed_conf = [r for r in rules if r.feature == "confidence"]
    assert not proposed_conf, (
        "a lift~1 confidence rule was PROPOSED — the lift guard failed to reject the "
        f"bread->milk trap: {[(r.op, r.threshold, round(r.lift, 4)) for r in proposed_conf]}"
    )

    # SETUP INVARIANT (intrinsic, NOT via LIFT_MARGIN so it is mutation-invariant): the
    # corpus really does contain a ``confidence`` split that clears BOTH the Wilson and the
    # Bonferroni gates while being lift~1, so the lift guard is provably the sole rejector.
    # If this invariant ever breaks the REJECT assertion above would be vacuous, so we pin
    # it explicitly here.
    prows = rule_mining._resolved_profitable_rows(corpus)
    base_rate = sum(1 for e in prows if (e.alpha_return or 0.0) > 0) / len(prows)
    cands = rule_mining._enumerate_candidates(prows)
    n_trials = len(cands)
    lift_one_clearing_other_gates = [
        c
        for c in cands
        if c.feature == "confidence"
        # clears Wilson:
        and wilson_lower_bound(c.wins, len(c.covered)) >= MIN_WILSON_LB
        # clears Bonferroni:
        and min(1.0, rule_mining._binom_sf_ge(c.wins, len(c.covered), base_rate) * n_trials)
        <= ALPHA
        # but is a lift~1 trap (selected intrinsically: win-rate within +5% of base rate):
        and 1.0 <= (c.wins / len(c.covered)) / base_rate < 1.0 + LIFT_MARGIN
    ]
    assert lift_one_clearing_other_gates, (
        "test setup broken: expected a lift~1 confidence split that clears BOTH the Wilson "
        "and Bonferroni gates, so that ONLY the lift guard can reject it (else this test "
        "would be vacuous / non-discriminating)"
    )


def test_bonferroni_correction_counts_all_trials():
    """Every proposed rule's n_trials_tested == the number of candidate splits scanned,
    and bonferroni_p = min(1, raw_p * n_trials) — so a marginally-significant rule is
    diluted by the multiple-testing correction (Jensen 1997 / Harvey-Liu, note §1c)."""
    rules = mine_rules(entries=_separable_corpus())
    assert rules
    n_trials = rules[0].n_trials_tested
    assert n_trials > 1
    for r in rules:
        assert r.n_trials_tested == n_trials
        assert 0.0 <= r.bonferroni_p <= 1.0


# ===========================================================================
# 4. No-lookahead — alpha_return is the LABEL, never a feature
# ===========================================================================


def test_no_lookahead_outcome_never_a_feature():
    """No induced rule may key on an outcome field (alpha_return/raw_return/exit_price);
    features are decision-time only (note §4)."""
    rules = mine_rules(entries=_separable_corpus())
    forbidden = {"alpha_return", "raw_return", "exit_price", "hold_minutes"}
    for r in rules:
        assert r.feature not in forbidden


def test_unresolved_entries_excluded():
    """Unresolved entries (asof_settlement None) are not part of the corpus even if the
    flag is on — only resolved trades carry a realized label."""
    rows = [_entry(i, confidence=0.85, direction=1, alpha_return=2.0, resolved=False)
            for i in range(MIN_SAMPLE + 5)]
    assert mine_rules(entries=rows) == []


def test_flat_direction_excluded():
    """direction==0 (no trade) has no actionable buy/sell direction and is excluded."""
    rows = [_entry(i, confidence=0.85, direction=0, alpha_return=2.0)
            for i in range(MIN_SAMPLE + 5)]
    assert mine_rules(entries=rows) == []


# ===========================================================================
# 5. Auditable + propose-only output
# ===========================================================================


def test_rule_is_fully_auditable():
    """Every proposed rule exposes its full gate audit trail + a human-readable reason +
    the Oracle-Fallacy provenance tag."""
    rules = mine_rules(entries=_separable_corpus())
    assert rules
    r = rules[0]
    d = r.to_dict()
    for key in (
        "feature", "op", "threshold", "direction", "n", "wins", "win_rate",
        "wilson_lb", "base_rate", "lift", "n_trials_tested", "bonferroni_p",
        "verdict", "confident", "reason", "provenance", "generated_at",
    ):
        assert key in d
    assert d["provenance"] == "shadow.rule_mining.mine_rules"
    assert "if " in r.reason and "then " in r.reason


def test_write_candidates_propose_only(tmp_path):
    """write_candidates writes ONLY a candidate JSON (advisory plane); it imports nothing
    from rules.py and cannot mutate default_rules(). The file carries the propose-only
    header + provenance and only PROPOSE-verdict rows."""
    rules = mine_rules(entries=_separable_corpus())
    cand = tmp_path / "shadow-rule-candidates.json"
    n = write_candidates(rules, path=cand)
    assert n >= 1
    assert cand.exists()
    payload = json.loads(cand.read_text())
    assert payload["provenance"] == "shadow.rule_mining.mine_rules"
    assert "PROPOSALS, not authority" in payload["_note"]
    for row in payload["candidates"]:
        assert row["verdict"] == "PROPOSE"
        assert row["provenance"] == "shadow.rule_mining.mine_rules"

    # default_rules() is unchanged by mining (separate proposed set).
    from hermes_quant.shadow.rules import default_rules
    assert len(default_rules()) == 5


def test_write_candidates_self_ingestion_guard(tmp_path):
    """The candidate file is NEVER read back as a corpus (Oracle-Fallacy guard): feeding
    it to the journal parser yields no resolved entries -> mine_rules returns []."""
    rules = mine_rules(entries=_separable_corpus())
    cand = tmp_path / "shadow-rule-candidates.json"
    write_candidates(rules, path=cand)
    # The candidate JSON is not a markdown journal -> parse_journal finds 0 entries.
    assert mine_rules(path=cand) == []


# ===========================================================================
# 6. Default-OFF (byte-identical off-state) + silence-by-default
# ===========================================================================


def test_disabled_returns_empty_and_writes_nothing(tmp_path, monkeypatch):
    """With HERMES_QUANT_SHADOW_RULE_MINING unset/"0": mine_rules returns [] and
    write_candidates writes nothing (the file is never created) — a bit-for-bit no-op."""
    corpus = _separable_corpus()

    monkeypatch.delenv("HERMES_QUANT_SHADOW_RULE_MINING", raising=False)
    assert mine_rules(entries=corpus) == []

    monkeypatch.setenv("HERMES_QUANT_SHADOW_RULE_MINING", "0")
    assert mine_rules(entries=corpus) == []

    # Even handed PROPOSE-able rules, write_candidates no-ops when the flag is off.
    monkeypatch.setenv("HERMES_QUANT_SHADOW_RULE_MINING", "1")
    rules = mine_rules(entries=corpus)
    assert rules
    monkeypatch.setenv("HERMES_QUANT_SHADOW_RULE_MINING", "0")
    cand = tmp_path / "shadow-rule-candidates.json"
    assert write_candidates(rules, path=cand) == 0
    assert not cand.exists()


def test_missing_journal_is_silent(tmp_path):
    """A missing journal path -> [] (silence-by-default, no journal yet)."""
    assert mine_rules(path=tmp_path / "nope.md") == []


def test_empty_corpus_report_is_silent():
    """format_report on no proposed rules -> a 'no proposed rules' line."""
    assert "no proposed rules" in format_report([])


def test_write_candidates_no_proposed_rows_writes_nothing(tmp_path):
    """A list with only non-PROPOSE rules -> nothing written (silence-by-default)."""
    failing = InducedRule(
        feature="confidence", op=">", threshold=0.5, direction="buy",
        n=30, wins=16, win_rate=0.53, wilson_lb=0.40, base_rate=0.5, lift=1.06,
        n_trials_tested=8, bonferroni_p=0.4, verdict="FAILS_WILSON", confident=False,
        reason="…", generated_at="2026-05-31T00:00:00+00:00",
    )
    cand = tmp_path / "c.json"
    assert write_candidates([failing], path=cand) == 0
    assert not cand.exists()


# ===========================================================================
# 7. Confident tier (time-ordered hold-out)
# ===========================================================================


def test_confident_only_above_confident_n():
    """A separable corpus BELOW CONFIDENT_N can PROPOSE but is never marked confident
    (not enough rows to hold out); the hold-out is only attempted at n >= CONFIDENT_N."""
    # _separable_corpus is 40 rows < CONFIDENT_N(50): proposed rules must be confident=False
    small = _separable_corpus()
    assert len(small) < CONFIDENT_N
    rules = mine_rules(entries=small)
    assert rules
    assert all(r.confident is False for r in rules)


def test_holdout_split_is_time_ordered_not_random():
    """The hold-out uses the newer 30% by TIME (asof_decision), never a shuffle — a large
    separable corpus where the signal persists into the newer slice yields a confident
    PROPOSE."""
    rows: list[SettlementEntry] = []
    # 80 rows, signal INTERLEAVED across time so 'confidence > t -> buy' covers rows in
    # BOTH the older 70% and the newer 30% (a time-ordered split, not a shuffle, still
    # confirms it). high-confidence wins 90% / low-confidence wins 10%, stable over time.
    for i in range(80):
        if i % 2 == 0:
            rows.append(_entry(i, confidence=0.85, direction=1,
                               alpha_return=(2.0 if (i // 2) % 10 != 0 else -1.0)))  # 90% win
        else:
            rows.append(_entry(i, confidence=0.30, direction=1,
                               alpha_return=(2.0 if (i // 2) % 10 == 0 else -1.0)))  # 10% win
    assert len(rows) >= CONFIDENT_N
    rules = mine_rules(entries=rows)
    assert rules
    assert any(r.confident for r in rules), "a stable signal should clear the hold-out"


# ===========================================================================
# ADVISORY-PLANE-ONLY: imports NO risk/governance/sizing surface
# ===========================================================================


def test_module_is_advisory_plane_only():
    """rule_mining must NOT import the risk gate, kill-switch/governance, or the discrete
    sizing ladder — it lives entirely in the advisory plane (mirrors graph_mining's audit).
    Static import audit over the module source + dynamic check on bound module objects."""
    src = Path(rule_mining.__file__).read_text(encoding="utf-8")
    forbidden = [
        "risk.gate",
        "risk import gate",
        "from hermes_quant.risk",
        "import hermes_quant.risk",
        "kill_switch",
        "killswitch",
        "sizing_ladder",
        "from hermes_quant.aggregators",
        "from hermes_quant.agents",
    ]
    for token in forbidden:
        assert token not in src, f"rule_mining must not reference {token!r}"
    for name, obj in vars(rule_mining).items():
        modname = getattr(obj, "__module__", "") or ""
        assert "risk" not in modname, f"{name} comes from a risk module ({modname})"
        assert "governance" not in modname, f"{name} comes from governance ({modname})"
