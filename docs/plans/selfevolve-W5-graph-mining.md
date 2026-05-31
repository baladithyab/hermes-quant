# W5 — B10 Learned-Graph Miner (catalyst edges) — Implementation Plan

**Date:** 2026-05-30
**Wave:** W5 (parallelizable after W1, which is shipped — commit `08326e1`)
**Closes:** **O5** (`propagation.log_propagations` → `propagation-log.jsonl` consumed only by `profitability.py`'s relation-class verdict; per-edge sign/weight learning was DESIGN-ONLY at `hermes_quant/catalyst/graph_mining.py:1`).
**Flag:** `HERMES_QUANT_GRAPH_MINING` (default-OFF).
**Grounds:** capability-map §4 (W5 spec) + §5 (safety frame); ADR-0080 D80.1/D80.3/D80.5/D80.6 (advisory-plane = candidate-edges; the universal eval-gate contract); the existing DESIGN-ONLY docstring at `hermes_quant/catalyst/graph_mining.py:1-79` (this plan IS that build).
**Mechanism:** MemEvolve config-evolution (arXiv:2512.18746) — generalize the proven `profitability.py` per-relation-class verdict to **per-edge** verdicts, propose-only.

A fresh agent can build this with no further research. Every seam is cited `file:line`; every signature, flag idiom, and eval-gate-as-pytest is specified.

---

## 0. The one-paragraph statement of what this is

`profitability.py` already joins `propagation-log.jsonl` against realized forward returns **grouped by relation class** (`brand_self` vs sector edges) and emits PROFITABLE / UNPROFITABLE_CONSIDER_PRUNE / INSUFFICIENT_SAMPLE per class (`hermes_quant/catalyst/profitability.py:80-134`, `MIN_SAMPLE=20`/`MIN_HIT_RATE=0.6` at `:32,35`). W5 builds the **next layer down**: the same join, but grouped by **individual edge** `(source, target_symbol, relation)`, emitting per-edge KEEP / FLIP_SIGN / DOWNWEIGHT / PRUNE verdicts plus a **silence-only** `confidence_multiplier`. It writes those verdicts to a **candidate diff file** (the advisory plane) that the operator reviews. It **NEVER** auto-edits the seed YAML, and the multiplier can only pull an edge's weight **toward 0** (never above its curated weight). It is evidence, never authority — the deterministic risk gate + operator promotion remain the sole path to live policy.

---

## 1. The corpus this consumes (exists today, accreting)

`propagate(..., log=entries)` appends one dict per `(entity→symbol)` edge fire (`hermes_quant/catalyst/propagation.py:361-368`):

```python
{"symbol": <target>, "source": <ent>, "relation": <relation>,
 "effect_sign": <int>, "weight": <float>, "symbol_sign": <int>, "catalyst_sign": <int>}
```

`log_propagations(entries, asof=..., path=...)` (`propagation.py:191-222`) stamps `asof` (the headline PUBLICATION time, lookahead-honest — `synthesize.py:99,112`) on each row and appends to `~/.hermes/quant/catalyst/propagation-log.jsonl` (`propagation.py:188`, identical to `profitability.py:28`).

**The edge key is fully reconstructable from each row** — `source`, `symbol` (= `target_symbol`), `relation` are all present. No schema change to the log is required. (Verified: `propagation.py:361-368` writes exactly these keys.)

---

## 2. New / modified files

### 2.1 `hermes_quant/catalyst/graph_mining.py` — REPLACE the DESIGN-ONLY stub with the build

Currently lines `1-85` are a docstring + `# Intentionally no implementation`. Keep the docstring's design intent and rails verbatim where applicable; add the implementation below it. **Module-level constants reuse the profitability bars** (per the eval-gate spec):

```python
from hermes_quant.catalyst.profitability import MIN_HIT_RATE, MIN_SAMPLE  # 0.6, 20
```

ADR-0080 §"More Information" and the capability-map W5 eval gate both say `MIN_SAMPLE`/`MIN_HIT_RATE` "as in `profitability.py`". The DESIGN docstring's `min_sample=30` was a guess (`graph_mining.py:23,70-71`); **resolve open-question #1 by reusing `profitability.MIN_SAMPLE=20`** as the default and documenting that the per-edge bar can be raised later via the `min_sample` kwarg — do NOT introduce a second hard-coded constant.

#### Dataclass

```python
@dataclass
class EdgeEvidence:
    source: str
    target_symbol: str
    relation: str
    curated_effect_sign: int          # from the live graph (load_graph)
    curated_weight: float             # from the live graph
    n_scored: int = 0
    hits: int = 0                     # sign(fwd) == symbol_sign
    sum_signed_return: float = 0.0
    examples: list[str] = field(default_factory=list)

    @property
    def sign_hit_rate(self) -> float: ...        # hits / n_scored, 0.0 if n==0
    @property
    def mean_signed_return(self) -> float: ...    # sum_signed_return / n_scored

    @property
    def suggested_effect_sign(self) -> int:
        # FLIP iff sign_hit_rate < 0.5 AND n_scored >= MIN_SAMPLE; else curated.
        if self.n_scored >= MIN_SAMPLE and self.sign_hit_rate < 0.5:
            return -self.curated_effect_sign
        return self.curated_effect_sign

    @property
    def confidence_multiplier(self) -> float:
        # SILENCE-ONLY: in [0.0, 1.0]. NEVER amplifies above the curated weight.
        # Below MIN_SAMPLE -> 1.0 (no opinion). Otherwise scale toward 0 as the
        # hit-rate falls below MIN_HIT_RATE; clamp to <= 1.0 always.
        if self.n_scored < MIN_SAMPLE:
            return 1.0
        if self.sign_hit_rate >= MIN_HIT_RATE:
            return 1.0
        # linear taper from MIN_HIT_RATE down to 0.5 (a coin flip => silence)
        m = (self.sign_hit_rate - 0.5) / (MIN_HIT_RATE - 0.5)
        return round(max(0.0, min(1.0, m)), 4)

    @property
    def verdict(self) -> str:
        # KEEP | FLIP_SIGN | DOWNWEIGHT | PRUNE
        if self.n_scored < MIN_SAMPLE:
            return "KEEP"  # insufficient sample => no change proposed (silence)
        if self.sign_hit_rate < 0.5:
            return "FLIP_SIGN"
        if self.confidence_multiplier == 0.0:
            return "PRUNE"
        if self.sign_hit_rate < MIN_HIT_RATE:
            return "DOWNWEIGHT"
        return "KEEP"

    def to_dict(self) -> dict: ...   # for the candidate-diff JSON
```

**Verdict ordering rationale (matches the DESIGN spec at `graph_mining.py:33-35`):** a hit-rate below a coin flip on a sufficient sample is a *wrong sign* (FLIP_SIGN) — flipping it is the correction. A hit-rate between 0.5 and `MIN_HIT_RATE` is a *weak but not inverted* edge → DOWNWEIGHT (multiplier in (0,1)). A multiplier that tapers to exactly 0.0 is PRUNE (the edge is a coin flip at 0.5 — silence it entirely). `KEEP` covers both "clears the bar" and "insufficient sample" (silence-by-default — never propose a change on thin evidence; this is the explicit safety choice).

#### Function signature (matches the DESIGN docstring `graph_mining.py:22-24`)

```python
# Reuse profitability's contract verbatim — same injected fetcher, offline-testable.
from hermes_quant.catalyst.profitability import ForwardReturnFetcher

def mine_graph(
    fetcher: ForwardReturnFetcher,
    *,
    path: Path | None = None,           # defaults to propagation-log.jsonl
    graph: dict[str, list[PropagationEdge]] | None = None,  # curated graph for sign/weight
    min_sample: int = MIN_SAMPLE,
    max_rows: int = 5000,
) -> dict[tuple[str, str, str], EdgeEvidence]:
    """Join the propagation log against realized forward returns, grouped PER EDGE.

    edge_key = (source, target_symbol, relation). For each scored row a "hit" is
    sign(forward_return) == row["symbol_sign"] (the propagated direction), EXACTLY
    as profitability.measure_profitability scores per relation (profitability.py:120-128).
    Silence-by-default on a missing/empty log (returns {}). The miner never sees
    returns when the graph propagates (the fetcher reads the NEXT bar after asof).
    """
```

Implementation mirrors `measure_profitability` (`profitability.py:96-134`) line-for-line, changing only the grouping key from `relation` to `(source, symbol, relation)` and seeding each `EdgeEvidence` with the curated `effect_sign`/`weight` looked up from `graph` (via `load_graph()` from `propagation.py:229` when `graph is None`). Reuse: the `asof` ISO parse (`profitability.py:116-119`), the `fwd is None or fwd == 0 → skip` rule (`:121`), the signed-aligned return accumulation `fwd if sym_sign > 0 else -fwd` (`:125`), and the `realized_sign == sym_sign` hit test (`:126-128`).

> **DO NOT re-implement the join from scratch** — the grouping change is the only delta from `measure_profitability`. Where the body would be identical (`max_rows`/JSONDecodeError/OSError silence), copy it.

#### The FLIP_SIGN sign-consistency prerequisite (DESIGN rail `graph_mining.py:51-55`)

A FLIP_SIGN verdict alone is **not** sufficient to propose a flip — the DESIGN doc requires it to *also* pass the existing market-data-free sign-consistency check (the D74.7 mechanism that caught the OPEC mis-sign by hand, `eval.py:147`, `propagation.py:97-103`). Add a helper:

```python
def flip_passes_sign_consistency(
    ev: EdgeEvidence,
    sign_cases: list[SignCase],   # from hermes_quant.catalyst.eval
    *,
    graph=None,
    aliases=None,
) -> bool:
    """A FLIP_SIGN candidate must ALSO clear run_sign_consistency on the proposed
    (flipped) graph before the operator applies it. Don't flip on noise."""
    # Build a candidate graph with this one edge's effect_sign flipped, run
    # eval.run_sign_consistency(sign_cases, graph=candidate, aliases=aliases),
    # return result.passed. (eval.py:147-184)
```

This is *advisory metadata on the proposal*, not an auto-apply path — it tells the operator "this flip is internally consistent" vs "this flip would break a curated sign expectation, treat as noise."

#### Report formatter (mirror `profitability.format_report`, `profitability.py:137-160`)

```python
def format_report(evidence: dict[tuple[str, str, str], EdgeEvidence]) -> str:
    """Compact human report: per-edge n / hit-rate / multiplier / verdict,
    actionable verdicts (FLIP_SIGN/PRUNE/DOWNWEIGHT) first. Empty corpus ->
    'no scored edges yet' (silence-by-default)."""
```

#### Candidate-diff emitter (the advisory-plane WRITE — the only thing W5 writes)

```python
_DEFAULT_CANDIDATES = Path.home() / ".hermes" / "quant" / "catalyst" / "graph-mine-candidates.json"

def write_candidates(
    evidence: dict[tuple[str, str, str], EdgeEvidence],
    *,
    path: Path | None = None,
) -> int:
    """Write the CANDIDATE graph diff for operator review (advisory plane).
    Only edges with an actionable verdict (FLIP_SIGN/DOWNWEIGHT/PRUNE) are emitted.
    Returns count written. This is the ONLY write W5 performs; it NEVER touches
    the seed/live YAML (propagation.graph_path()). Best-effort; never raises."""
```

The JSON shape is a list of `EdgeEvidence.to_dict()` enriched with `suggested_effect_sign`, `confidence_multiplier`, `verdict`, and (for FLIP_SIGN) `sign_consistency_passed`. Path matches the DESIGN doc exactly (`graph_mining.py:64`).

> **Provenance tag (ADR-0080 D80.4/D80.6).** Each candidate record carries `"provenance": "graph_mining.mine_graph"` and `"generated_at": <iso>` so the operator (and any downstream reader) sees it is the agent's OWN prior output, never ground truth. The candidate file is **not** re-ingested by `mine_graph` — only `propagation-log.jsonl` (external market data via the fetcher) is read back. This is the structural Oracle-Fallacy guard for this wave.

### 2.2 `ops/scripts/quant-catalyst-graph-mine.py` — NEW cron (mirror the profitability watchdog)

Copy `ops/scripts/quant-catalyst-profitability.py` **structurally verbatim** (it is the proven change-detecting `no_agent` watchdog) and change only:

- import `from hermes_quant.catalyst.graph_mining import mine_graph, format_report, write_candidates` (+ `MIN_SAMPLE`).
- reuse the **identical** `_yf_forward_return(symbol, asof)` fetcher (`profitability.py` script lines `34-60`) — copy it; it already returns the next-bar forward return % lookahead-honestly.
- `_BASELINE = ... / "graph-mine-baseline.json"` (new file; the per-edge analog of `profitability-baseline.json`).
- `_current_state(evidence)` projects `{edge_key_str: {"cleared": n>=MIN_SAMPLE, "verdict": verdict}}` (edge key joined as `"source|target|relation"`).
- `_transitions(...)` is **byte-for-byte the profitability one** (`quant-catalyst-profitability.py:92-111`) — emit a line ONLY when an edge crosses `MIN_SAMPLE` for the first time, or a cleared edge flips verdict. Standing state → silent.
- `main()`: call `mine_graph(_yf_forward_return, max_rows=120)`; on any actionable transition, also call `write_candidates(evidence)` and print the candidate-file path; `--verbose` prints the full `format_report`. Empty corpus → `return 0` silent.

**Cron registration** (per DESIGN doc `graph_mining.py:57-65` + `docs/operations/CRON-REGISTRY.md`):
- Job: `quant-catalyst-graph-mine-weekly`, schedule `0 6 * * 6` (Sat 06:00 PT, **after** the profitability cron), `deliver=origin`, `no_agent`.
- Silent unless an edge crosses `min_sample` with a FLIP_SIGN/PRUNE/DOWNWEIGHT verdict.
- Add a row to `docs/operations/CRON-REGISTRY.md` documenting it as default-OFF (the script is inert with the flag off — see §3).

### 2.3 `tests/unit/test_catalyst_graph_mining.py` — NEW (the eval gate as pytest)

See §4 for the full acceptance-criteria test list.

---

## 3. Default-OFF flag-gating idiom (copied from the repo)

The repo idiom is `os.environ.get("HERMES_QUANT_<NAME>", "0") == "1"` (verified across `aggregators/llm_committee.py:296,977`, `react/multileg.py:101-102`, `admissibility/borrow_pnl.py:34`, `catalyst/onboarding.py:76-77`). Use it **exactly**:

```python
import os

def _mining_enabled() -> bool:
    """W5 is default-OFF. The miner is inert (returns {} / writes nothing)
    until HERMES_QUANT_GRAPH_MINING=1. Mirrors multileg.py:101-102."""
    return os.environ.get("HERMES_QUANT_GRAPH_MINING", "0") == "1"
```

**Where the gate lives:** at the top of `mine_graph()` (return `{}` immediately if not enabled) AND at the top of `main()` in the cron (return `0` silent if not enabled). With the flag OFF:
- `mine_graph()` returns `{}` → `write_candidates({})` writes nothing → the candidate file is never created.
- the cron prints nothing and writes no baseline.
- **byte-identical off-state** (ADR-0080 D80.8): the catalyst forward path (`synthesize`/`propagate`/`log_propagations`) is untouched — W5 only adds a *reader* of the already-accreting log, and that reader is gated off.

Document the flag in `docs/operations/feature-enablement` alongside the others; the flip is operator-only and gated on §4 passing.

---

## 4. The eval gate, as pytest-verifiable acceptance criteria

`tests/unit/test_catalyst_graph_mining.py` — these tests ARE the gate. They run with no network (the fetcher is injected, exactly like `test_catalyst_profitability_cron.py`). The eval-gate command is `pytest tests/unit/test_catalyst_graph_mining.py -q` (repo `testpaths=["tests"]`, `pyproject.toml:159-160`).

**Mechanism / correctness (the per-edge join is honest):**
1. `test_mine_groups_per_edge` — a log with two edges from the same source to different symbols produces **two** `EdgeEvidence` entries keyed `(source, target, relation)` (proves grouping is per-edge, not per-relation — the delta from `profitability.py`).
2. `test_hit_test_matches_profitability` — for a single-edge log, the `sign_hit_rate`/`mean_signed_return` equal what `measure_profitability` reports for that relation (proves the join is identical except for grouping).
3. `test_empty_or_missing_log_is_silent` — missing/empty log → `mine_graph` returns `{}` (silence-by-default, `profitability.py:94-95`).
4. `test_fetcher_none_or_flat_unscored` — a row whose fetcher returns `None` or `0` is not scored (`profitability.py:121`).
5. `test_asof_is_lookahead_honest` — the fetcher is called with the row's `asof` date parsed from the ISO `asof` field; a row with no/invalid `asof` is skipped (`profitability.py:113-119`).

**Eval-gate thresholds (MIN_SAMPLE / MIN_HIT_RATE, the SkillOpt held-out bar):**
6. `test_below_min_sample_keeps_silent` — an edge with `n_scored < MIN_SAMPLE` has `verdict == "KEEP"` and `confidence_multiplier == 1.0` regardless of hit-rate (never propose on thin evidence — D80.3 robustness-not-peak).
7. `test_inverted_edge_flips` — `n_scored >= MIN_SAMPLE` and `sign_hit_rate < 0.5` → `verdict == "FLIP_SIGN"` and `suggested_effect_sign == -curated_effect_sign`.
8. `test_weak_edge_downweights` — `MIN_SAMPLE` cleared, `0.5 <= sign_hit_rate < MIN_HIT_RATE` → `verdict == "DOWNWEIGHT"` and `0.0 < confidence_multiplier < 1.0`.
9. `test_coinflip_edge_prunes` — `sign_hit_rate == 0.5` exactly, cleared → multiplier taper hits `0.0` → `verdict == "PRUNE"`.
10. `test_clears_bar_keeps` — `sign_hit_rate >= MIN_HIT_RATE`, cleared → `verdict == "KEEP"`, multiplier `1.0`.

**SAFETY frame (the load-bearing invariants — these are the tests that prove the rails):**
11. `test_multiplier_is_silence_only` — for ALL evidence, `0.0 <= confidence_multiplier <= 1.0` (property test over generated hit-rates: it NEVER exceeds 1.0 → never amplifies above the curated weight — capability-map §5 "MAY tune: silence-only", `graph_mining.py:49-50`).
12. `test_write_candidates_never_touches_seed_yaml` — monkeypatch `propagation.graph_path()` to a tmp path, write a seed YAML, run `mine_graph` + `write_candidates`; assert the seed YAML bytes are unchanged AND only the candidate file (`graph-mine-candidates.json`) was written (proves "never auto-mutate the seed YAML", `graph_mining.py:46-48`).
13. `test_only_actionable_verdicts_emitted` — `write_candidates` writes only FLIP_SIGN/DOWNWEIGHT/PRUNE rows; KEEP edges are not in the diff (the operator only reviews proposed changes).
14. `test_candidates_carry_provenance` — every candidate row has `"provenance"` and `"generated_at"` (Oracle-Fallacy tag, D80.6) and the candidate file is NOT a path `mine_graph` ever reads (no self-ingestion).
15. `test_flip_requires_sign_consistency` — a FLIP_SIGN evidence whose flipped graph FAILS `run_sign_consistency` is tagged `sign_consistency_passed=False` in the diff (advisory: don't flip on noise, `graph_mining.py:51-55`).

**Default-OFF (byte-identical off-state):**
16. `test_disabled_returns_empty` — with `HERMES_QUANT_GRAPH_MINING` unset/`"0"` (monkeypatch `os.environ`), `mine_graph(...)` returns `{}` and `write_candidates` is never reached / writes nothing.

**Cron watchdog (mirror `test_catalyst_profitability_cron.py` exactly):**
17. `test_cron_silent_when_no_evidence` / `test_cron_silent_when_unchanged` / `test_cron_emits_on_edge_clearance` / `test_cron_emits_on_verdict_flip` / `test_cron_verbose_always_prints` — load the script execv-safely via the `_load_cron_module` helper from `test_catalyst_profitability_cron.py:21-35` (copy it), monkeypatch `mine_graph` + `_BASELINE` to `tmp_path`, assert silence/emit on the four transition cases. With the flag OFF, the cron is silent (test #16's cron analog).

**Gate to flip the flag (operator checklist — D80.3 universal eval-gate contract):**
- ALL of tests 1–17 green (`pytest tests/unit/test_catalyst_graph_mining.py -q`).
- External-truth only (D-3): verdicts derive solely from realized forward returns via the injected fetcher; no LLM self-score anywhere in `graph_mining.py`. (Enforced by test 2 + code review: the module imports nothing from `agents/` or `aggregators/`.)
- Held-out (D-4): forward return is the NEXT bar after `asof` (the miner never saw it at propagate time); passing is necessary-not-sufficient → candidate diff goes to the operator, never auto-applies (test 12).
- Robustness-not-peak (D-5): no change proposed below `MIN_SAMPLE` (test 6); the per-edge bar can be RAISED via `min_sample` kwarg but never lowered below `profitability.MIN_SAMPLE`.
- Bounded + provenance (D-6): every candidate Oracle-tagged (test 14); candidate file never re-ingested.
- Propose-only (D-7): the seed YAML is never auto-edited (test 12); the operator manually applies a reviewed diff; the deterministic gate + promotion remain the sole live path.

---

## 5. SAFETY frame applied — the two lists for W5

**What W5 MAY write (the ADVISORY PLANE — capability-map §5, ADR-0080 D80.1):**
- `~/.hermes/quant/catalyst/graph-mine-candidates.json` — the candidate-edge diff (FLIP_SIGN/DOWNWEIGHT/PRUNE proposals + silence-only `confidence_multiplier`), Oracle-provenance-tagged.
- `~/.hermes/quant/catalyst/graph-mine-baseline.json` — the watchdog state for change-detection (no policy content).
- stdout (the cron report, on transition only).

**What W5 MUST NEVER touch (outside the loop, immutable by it — ADR-0080 D80.2):**
- The seed / live propagation YAML (`propagation.graph_path()`, `propagation.py:225-226`) — operator-authored, manually edited only (test 12).
- The deterministic risk gate, the hard risk limits, the discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}`, the kill-switch — W5 has no code path to any of these; it only reads the propagation log and writes the candidate file.
- The `propagation-log.jsonl` corpus itself (read-only to the miner).
- `confidence_multiplier` is **silence-only**: it can pull an edge's effective weight **toward 0** (a wrong edge gets quieter) but is clamped `<= 1.0` so it can NEVER amplify above the curated weight (test 11). This is the single most load-bearing safety property of the wave.

**The propose-only statement (make it explicit in the module docstring and the candidate-file header):**
> The miner PROPOSES edge edits. It is evidence, never authority. The curated graph stays operator-authored; the only path from a candidate diff to live policy runs through manual operator review → manual YAML edit → the deterministic risk gate / promotion machinery, which this loop can never modify (ADR-0080 D80.1). A FLIP_SIGN proposal must additionally clear the market-data-free sign-consistency check (`eval.run_sign_consistency`, `eval.py:147`) — the systematic version of the hand-caught OPEC mis-sign (`propagation.py:97-103`).

---

## 6. Build order (single PR, ~1 day)

1. Implement `EdgeEvidence` + `mine_graph` + `flip_passes_sign_consistency` + `format_report` + `write_candidates` + `_mining_enabled` in `graph_mining.py` (replace the `# Intentionally no implementation` block; KEEP the design docstring, append a "BUILT W5" note). Reuse `profitability.MIN_SAMPLE`/`MIN_HIT_RATE`/`ForwardReturnFetcher` by import — do not redefine.
2. Write `tests/unit/test_catalyst_graph_mining.py` (§4) — TDD: tests 1–16 first, then the cron tests.
3. Copy `ops/scripts/quant-catalyst-profitability.py` → `quant-catalyst-graph-mine.py`, swap the import + grouping + baseline path + flag gate; add cron tests 17.
4. `pytest tests/unit/test_catalyst_graph_mining.py -q` green; `ruff check hermes_quant/catalyst/graph_mining.py ops/scripts/quant-catalyst-graph-mine.py tests/unit/test_catalyst_graph_mining.py`.
5. Register `quant-catalyst-graph-mine-weekly` in `docs/operations/CRON-REGISTRY.md` (default-OFF, gated on §4).

**Files touched:** `hermes_quant/catalyst/graph_mining.py` (modify), `ops/scripts/quant-catalyst-graph-mine.py` (new), `tests/unit/test_catalyst_graph_mining.py` (new), `docs/operations/CRON-REGISTRY.md` (modify, one row).

**Off-state guarantee:** with `HERMES_QUANT_GRAPH_MINING` unset, the catalyst forward path is byte-identical to today; W5 adds only a gated reader of the already-accreting `propagation-log.jsonl` and a gated cron. Nothing in the forward path imports `graph_mining`.
