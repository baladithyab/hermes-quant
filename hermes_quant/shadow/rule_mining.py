"""hermes_quant.shadow.rule_mining — B27 shadow-rule induction (FLAG-GATED SKELETON).

DEFER_DATA_GATED (docs/research/2026-05-31-r-B27.md): the induction METHOD is settled
and low-risk, but the resolved-profitable corpus does NOT exist on disk yet (the
settlement journal has 198 entries, 0 resolved, 0 profitable; `per_analyst_components`
is dropped on parse — reader.py:173). Inducing if-then rules from an empty/single-digit
set of profitable trades is exactly the spurious-discovery failure the literature warns
against (Harvey & Liu: 7 trials suffice for a fake Sharpe>1). So B27 ships the SAME shape
as `catalyst/graph_mining.py` (D80) held while ITS corpus accreted: the designed,
default-OFF, PROPOSE-only, silence-by-default module — ready, but inert (returns ``[]``,
writes nothing) until the journal holds ``>= MIN_SAMPLE`` resolved profitable trades.

----------------------------------------------------------------------------------
What this module is (and is NOT)
----------------------------------------------------------------------------------
* It INDUCES at most 3-5 auditable ``if X <op> threshold then <direction>`` candidate
  rules from RESOLVED trades, ranked by their Wilson lower bound (NOT point win-rate).
* It is the LEARNED analogue of the hand-coded rules in ``shadow/rules.py`` —
  ``default_rules()`` is UNCHANGED; induced rules are a SEPARATE proposed set the
  operator may promote by hand. B27 does NOT replace the hand-coded rules at runtime;
  it PROPOSES candidates to augment/replace them under human review. "Hard rules over
  learned policy" is preserved: the deterministic risk gate stays the final authority,
  and this loop can NEVER edit it.
* PROPOSE-only: induced rules are written to ``shadow-rule-candidates.json`` for
  operator review and NEVER mutate ``default_rules()`` or any live decision path.

----------------------------------------------------------------------------------
Induction method — deterministic single-feature 1R / depth-1 decision stump
----------------------------------------------------------------------------------
Per the note (§1a Holte 1993, §1b reject RIPPER/Apriori at tiny n, §1e):
1. Load resolved entries; build a feature matrix of DECISION-TIME fields only (§2c).
   Label = ``1 if alpha_return > 0 else 0`` — the OUTCOME, NEVER a feature (no-lookahead).
2. For each candidate feature, scan candidate thresholds (numeric: midpoints between
   sorted distinct values, capped to <=5 bins per OneR's anti-overfit guidance;
   categorical: each level). Each (feature, op, threshold, direction) is ONE trial —
   count them (``n_trials_tested``) for the Bonferroni correction (Jensen 1997).
3. For each candidate compute support ``n``, ``wins``, point ``win_rate``, the 95%
   **Wilson lower bound** (stdlib closed-form, §3), ``lift = win_rate / base_rate``,
   and a one-sided binomial p-value vs the base rate, Bonferroni-corrected by
   ``n_trials_tested``.
4. Gates (ALL must pass to PROPOSE): ``n >= MIN_SAMPLE`` AND ``wilson_lb >= MIN_WILSON_LB``
   AND ``lift >= 1.0 + LIFT_MARGIN`` AND ``bonferroni_p <= ALPHA``. Otherwise the verdict
   NAMES the failing gate. Keep the top 3-5 survivors ranked by ``wilson_lb`` (so a
   tiny-but-perfect 4/4 rule, whose Wilson LB ~0.51, can NEVER win).
5. Optional time-ordered hold-out (older 70% / newer 30%) as the no-snooping rail; only
   attempted when ``n >= CONFIDENT_N`` (else ``confident=False``, still PROPOSE-able).

----------------------------------------------------------------------------------
Rails (non-negotiable — AGENTS.md, mirrors graph_mining.py / ADR-0080)
----------------------------------------------------------------------------------
* No-lookahead: ``alpha_return`` is the LABEL, never a feature. The hold-out split is
  TIME-ORDERED (older->newer), never random/shuffled (random leaks future info into the
  "train" fold for autocorrelated returns).
* Determinism: the stdlib 1R scan sorts features/thresholds in a fixed order so ties
  resolve identically every run; no RNG, no LLM.
* Overfit on tiny n (the central risk) — all guards required: (1) ``n >= MIN_SAMPLE``
  floor; (2) gate on Wilson LOWER bound, not point win-rate; (3) ``lift > 1`` (a rule
  that "predicts buy when everything already buys" has lift~1 and adds nothing — the
  bread->milk trap); (4) Bonferroni by #trials (Harvey-Liu: 7 trials => spurious
  Sharpe>1); (5) depth-1 and <=5 numeric bins (OneR anti-overfit); (6) emit <=5 rules.
* Silence-by-default: empty or sub-MIN_SAMPLE corpus => return ``[]``, write nothing,
  the candidate file is not even created (mirror ``graph_mining.write_candidates``).
* Default-OFF / fail-closed: behind ``HERMES_QUANT_SHADOW_RULE_MINING`` (default "0");
  flag-off is a bit-for-bit no-op. On any read/parse error, log-warn and return ``[]``
  (never raise into a caller) — the profitability.py / pmcc.py best-effort idiom.
* Propose-only (the money rail): NEVER auto-applies; the candidate JSON is advisory.
* Oracle-Fallacy guard: every candidate carries ``provenance`` and the candidate file is
  NEVER re-ingested — only the journal (external realized outcomes) is read back, exactly
  as graph_mining.py reads only ``propagation-log.jsonl``, never its own candidate file.

----------------------------------------------------------------------------------
B26 DATA GATE (the activation trigger — verified on disk, note §5)
----------------------------------------------------------------------------------
This module stays a silent no-op until BOTH:
  (a) the journal holds ``>= MIN_SAMPLE`` resolved profitable trades (the settlement
      loop must actually resolve trades lookahead-honestly — a B26 deliverable), AND
  (b) ``per_analyst_components`` is round-tripped from the META block (reader.py:173
      currently drops it) so the rich per-analyst feature surface is readable.
Until (b) lands the feature set is the thin scalar fallback (direction / confidence /
target_position_pct / asset_class), which is still inducible but much weaker. Flip
``HERMES_QUANT_SHADOW_RULE_MINING=1`` ONLY once (a) holds. See note §6.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

# Reuse the repo's existing floor — do NOT redefine (note §2b; same as graph_mining).
from hermes_quant.catalyst.profitability import MIN_SAMPLE
from hermes_quant.home import quant_home as _resolve_quant_home
from hermes_quant.journal.models import SettlementEntry
from hermes_quant.journal.reader import parse_journal

logger = logging.getLogger(__name__)

# The corpus the miner reads back (external realized outcomes): the resolved entries of
# the settlement journal. Identical default to journal.writer.DEFAULT_JOURNAL_PATH.
_DEFAULT_JOURNAL = _resolve_quant_home() / "journal.md"

# The ONLY thing this module writes: the candidate-rule diff for operator review
# (advisory plane). Mirrors graph_mining's graph-mine-candidates.json contract.
_DEFAULT_CANDIDATES = (
    _resolve_quant_home() / "shadow" / "shadow-rule-candidates.json"
)

# GT-Score "minimally stable" tier (note §1c): a rule is only "confident" (and only then
# eligible for the time-ordered hold-out cross-check) at n >= CONFIDENT_N. 20 is the floor
# (MIN_SAMPLE, reused); 50 is the confident tier.
CONFIDENT_N = 50

# Wilson 95% LOWER bound the win-rate must clear to PROPOSE (mirrors the repo's
# MIN_HIT_RATE=0.6 precision bar, applied to the LOWER bound, not the point estimate).
MIN_WILSON_LB = 0.60

# Lift margin: a rule must beat the base rate by more than this (kills the bread->milk
# "high confidence, lift~1, adds nothing" trap — note §1b/§4).
LIFT_MARGIN = 0.05

# Bonferroni-corrected significance bar (Jensen 1997 / Harvey-Liu — note §1c).
ALPHA = 0.05

# z for the 95% Wilson interval (two-sided 95% => z=1.96). Closed form, no scipy.
_Z_95 = 1.959963984540054

# Max candidate rules emitted (note §4: "emit at most 3-5 rules").
_MAX_RULES = 5

# Max distinct numeric thresholds scanned per feature (OneR anti-overfit: few bins).
_MAX_NUMERIC_BINS = 5

# Oracle-Fallacy provenance tag: every candidate is the agent's OWN prior output, never
# ground truth, never re-ingested (mirrors graph_mining._PROVENANCE).
_PROVENANCE = "shadow.rule_mining.mine_rules"

# Decision-time (lookahead-safe) numeric features. ``alpha_return`` / ``raw_return`` /
# ``exit_price`` are OUTCOMES and are deliberately ABSENT (no-lookahead, note §2c/§4).
_NUMERIC_FEATURES = ("confidence", "target_position_pct")

# Decision-time categorical features.
_CATEGORICAL_FEATURES = ("direction", "asset_class")

Direction = Literal["buy", "sell"]
Op = Literal["<=", ">", "==", "in"]
Verdict = Literal[
    "PROPOSE",
    "INSUFFICIENT_SAMPLE",
    "FAILS_WILSON",
    "FAILS_LIFT",
    "FAILS_BONFERRONI",
]


def _mining_enabled() -> bool:
    """B27 is default-OFF. The miner is inert (returns [] / writes nothing) until
    ``HERMES_QUANT_SHADOW_RULE_MINING=1``. Mirrors graph_mining._mining_enabled."""
    return os.environ.get("HERMES_QUANT_SHADOW_RULE_MINING", "0") == "1"


# --------------------------------------------------------------------------- #
# Small-sample binomial primitives (stdlib only — no scipy dependency, note §3).
# --------------------------------------------------------------------------- #
def wilson_lower_bound(wins: int, n: int, *, z: float = _Z_95) -> float:
    """95% Wilson score interval LOWER bound for a binomial proportion (Brown, Cai &
    DasGupta 2001 — note §1d). The Wald (normal-approx) interval is "chaotic and
    unacceptably poor" at small n / extreme p; Wilson is the recommended small-n CI.

    A perfect-but-tiny rule (e.g. 4/4) has point precision 1.0 but a Wilson lower bound
    around ~0.51 at 95% — so it is correctly NOT trusted. Gating on this lower bound is
    the single most load-bearing overfit guard for B27.

    Returns 0.0 for n<=0 (no opinion). Closed form; no dependency.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, centre - half)


def _binom_sf_ge(k: int, n: int, p: float) -> float:
    """One-sided binomial p-value: P(X >= k) for X ~ Binomial(n, p), via stdlib
    ``math.comb`` (note §3 — "can also be done with math.comb in stdlib if avoiding
    scipy is preferred"). Returns a probability in [0, 1]. The shadow plane prizes
    dependency-light code (pmcc.py vendors its own Black-Scholes for this reason), so we
    use the closed stdlib form rather than scipy.stats.binomtest.
    """
    if n <= 0:
        return 1.0
    if p <= 0.0:
        # With base rate 0, any win is "significant"; guard the degenerate case.
        return 0.0 if k > 0 else 1.0
    if p >= 1.0:
        return 1.0
    k = max(0, min(k, n))
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (p**i) * ((1 - p) ** (n - i))
    return min(1.0, max(0.0, total))


# --------------------------------------------------------------------------- #
# Induced-rule model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InducedRule:
    """One auditable depth-1 ``if <feature> <op> <threshold> then <direction>`` rule
    induced from resolved trades, with the full overfit-guard audit trail.

    Every field is human-inspectable: ``reason`` renders the if-then in prose,
    ``provenance`` tags it as the agent's own proposal (never authority), and the
    gate metrics (``wilson_lb`` / ``lift`` / ``bonferroni_p``) show WHY it passed/failed.
    """

    feature: str
    op: Op
    threshold: float | str  # numeric cut OR categorical value
    direction: Direction  # the action the rule recommends
    n: int  # resolved trades the rule covers (support)
    wins: int  # of those, # profitable (alpha_return > 0)
    win_rate: float  # wins / n (point estimate — NOT the gate)
    wilson_lb: float  # 95% Wilson lower bound (THE gate)
    base_rate: float  # corpus-wide profitable rate (the null this rule must beat)
    lift: float  # win_rate / base_rate (must exceed 1.0 + LIFT_MARGIN)
    n_trials_tested: int  # # candidate (feature, op, threshold, direction) pairs scanned
    bonferroni_p: float  # corrected one-sided significance of this rule vs base rate
    verdict: Verdict
    confident: bool  # passed the time-ordered hold-out (only attempted at n >= CONFIDENT_N)
    reason: str  # human-readable if-then summary
    provenance: str = _PROVENANCE
    generated_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["win_rate"] = round(self.win_rate, 4)
        d["wilson_lb"] = round(self.wilson_lb, 4)
        d["base_rate"] = round(self.base_rate, 4)
        d["lift"] = round(self.lift, 4)
        d["bonferroni_p"] = round(self.bonferroni_p, 6)
        return d


# A candidate split before gating: the raw (feature, op, threshold, direction) hypothesis
# plus the rows it covers. Kept internal — only surviving InducedRules leave the module.
@dataclass
class _Candidate:
    feature: str
    op: Op
    threshold: float | str
    direction: Direction
    # indices into the (time-ordered) row list this candidate covers
    covered: list[int] = field(default_factory=list)
    wins: int = 0  # of covered rows, # profitable


# --------------------------------------------------------------------------- #
# Feature extraction (decision-time fields ONLY — no-lookahead, note §2c)
# --------------------------------------------------------------------------- #
def _direction_label(d: int) -> Direction | None:
    """Map the entry's numeric direction to a buy/sell action. direction==0 (flat/no
    trade) has no actionable direction and is excluded from rule induction."""
    if d > 0:
        return "buy"
    if d < 0:
        return "sell"
    return None


def _feature_value(entry: SettlementEntry, feature: str) -> float | str | None:
    """Read a single decision-time feature off an entry. Returns None when absent so the
    row is skipped for that feature (never lookahead — outcome fields are not here)."""
    if feature == "confidence":
        return float(entry.confidence)
    if feature == "target_position_pct":
        return float(entry.target_position_pct)
    if feature == "direction":
        return int(entry.direction)
    if feature == "asset_class":
        return str(entry.asset_class)
    return None


def _resolved_profitable_rows(
    entries: list[SettlementEntry],
) -> list[SettlementEntry]:
    """Time-ordered (older->newer by ``asof_decision``) resolved entries that have a
    settled ``alpha_return`` and an actionable (non-flat) direction. The label
    ``1 if alpha_return > 0 else 0`` is derived downstream; this only filters to the
    inducible corpus. Sorting is fixed so the hold-out split and tie-breaks are
    deterministic (note §4 determinism rail)."""
    rows = [
        e
        for e in entries
        if e.is_resolved()
        and e.alpha_return is not None
        and _direction_label(int(e.direction)) is not None
    ]
    rows.sort(key=lambda e: (e.asof_decision.isoformat(), e.entry_id))
    return rows


def _numeric_thresholds(values: list[float]) -> list[float]:
    """Candidate numeric cut-points: midpoints between sorted distinct values, capped to
    <=_MAX_NUMERIC_BINS (OneR anti-overfit — few bins, note §1a/§4). Deterministic order."""
    distinct = sorted(set(values))
    if len(distinct) < 2:
        return []
    mids = [(distinct[i] + distinct[i + 1]) / 2.0 for i in range(len(distinct) - 1)]
    if len(mids) <= _MAX_NUMERIC_BINS:
        return mids
    # Evenly subsample to <=_MAX_NUMERIC_BINS, keeping a fixed, reproducible selection.
    step = len(mids) / _MAX_NUMERIC_BINS
    return [mids[int(i * step)] for i in range(_MAX_NUMERIC_BINS)]


def _enumerate_candidates(rows: list[SettlementEntry]) -> list[_Candidate]:
    """Enumerate every depth-1 (feature, op, threshold, direction) split over the corpus.

    Each emitted candidate is ONE trial (counted for the Bonferroni correction). For a
    numeric feature we emit both ``<=`` and ``>`` at each threshold; for a categorical
    feature we emit ``==`` (numeric direction) / ``in`` (string class) per level. The
    rule's recommended ``direction`` is the MAJORITY traded direction among the rows it
    covers — the learned analogue of "follow when X holds". Deterministic throughout.
    """
    labels = [1 if (e.alpha_return or 0.0) > 0 else 0 for e in rows]
    candidates: list[_Candidate] = []

    def _make(feature: str, op: Op, threshold: float | str, mask: list[bool]) -> None:
        covered = [i for i, m in enumerate(mask) if m]
        if not covered:
            return
        # Recommended direction = majority traded direction among covered rows (ties ->
        # "buy", a fixed deterministic tie-break). This is what the rule would DO.
        buys = sum(
            1 for i in covered if _direction_label(int(rows[i].direction)) == "buy"
        )
        sells = len(covered) - buys
        direction: Direction = "buy" if buys >= sells else "sell"
        wins = sum(labels[i] for i in covered)
        candidates.append(
            _Candidate(
                feature=feature,
                op=op,
                threshold=threshold,
                direction=direction,
                covered=covered,
                wins=wins,
            )
        )

    for feature in _NUMERIC_FEATURES:
        vals: list[float] = []
        ok: list[bool] = []
        for e in rows:
            v = _feature_value(e, feature)
            ok.append(isinstance(v, (int, float)))
            vals.append(float(v) if isinstance(v, (int, float)) else 0.0)
        usable = [vals[i] for i in range(len(vals)) if ok[i]]
        for thr in _numeric_thresholds(usable):
            _make(feature, "<=", thr, [ok[i] and vals[i] <= thr for i in range(len(rows))])
            _make(feature, ">", thr, [ok[i] and vals[i] > thr for i in range(len(rows))])

    for feature in _CATEGORICAL_FEATURES:
        raw = [_feature_value(e, feature) for e in rows]
        levels = sorted({v for v in raw if v is not None}, key=str)
        for level in levels:
            op: Op = "==" if isinstance(level, (int, float)) else "in"
            _make(feature, op, level, [raw[i] == level for i in range(len(rows))])

    return candidates


def _holdout_confident(
    cand: _Candidate, rows: list[SettlementEntry]
) -> bool:
    """Time-ordered hold-out cross-check (note §4 no-snooping rail). Refit nothing —
    just confirm the SAME split still clears MIN_WILSON_LB on the newer 30% of the
    corpus. Only attempted when n >= CONFIDENT_N (else the caller marks confident=False).
    The split is by row index over the time-ordered ``rows`` (older 70% / newer 30%),
    NEVER random/shuffled (autocorrelated returns would leak)."""
    n = len(rows)
    if n < CONFIDENT_N:
        return False
    cut = int(n * 0.7)
    newer = set(range(cut, n))
    test_idx = [i for i in cand.covered if i in newer]
    if len(test_idx) < 1:
        return False
    labels = [1 if (rows[i].alpha_return or 0.0) > 0 else 0 for i in test_idx]
    wins = sum(labels)
    return wilson_lower_bound(wins, len(test_idx)) >= MIN_WILSON_LB


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def mine_rules(
    *,
    path: Path | None = None,
    entries: list[SettlementEntry] | None = None,
    max_rules: int = _MAX_RULES,
) -> list[InducedRule]:
    """Induce up to ``max_rules`` auditable if-then ShadowRule candidates from RESOLVED
    profitable trades. PROPOSE-only; never auto-applied.

    DEFAULT-OFF: returns ``[]`` immediately unless ``HERMES_QUANT_SHADOW_RULE_MINING=1``
    (bit-for-bit no-op when the flag is off). Silence-by-default: a missing/empty journal
    or a sub-MIN_SAMPLE resolved-profitable corpus returns ``[]``. Best-effort: any
    read/parse error log-warns and returns ``[]`` (never raises into a caller).

    ``entries`` may be injected directly (offline/deterministic tests); otherwise the
    journal at ``path`` (default ``~/.hermes/quant/journal.md``) is parsed.

    Method: deterministic single-feature 1R / depth-1 decision stump over decision-time
    features ONLY (no-lookahead); gate on the Wilson LOWER bound (not point win-rate),
    require lift > 1 + margin and a Bonferroni-corrected significant edge over the base
    rate; rank survivors by ``wilson_lb`` and emit the top ``max_rules``.
    """
    if not _mining_enabled():
        return []  # default-OFF: bit-for-bit no-op
    try:
        if entries is None:
            p = path or _DEFAULT_JOURNAL
            if not p.exists():
                return []  # silence-by-default (no journal yet)
            entries = parse_journal(p.read_text(encoding="utf-8"))
        rows = _resolved_profitable_rows(entries)
    except OSError as e:  # noqa: BLE001
        logger.warning("shadow.rule_mining: journal read failed: %s", e)
        return []
    except Exception as e:  # noqa: BLE001  (parse is best-effort; never raise into a caller)
        logger.warning("shadow.rule_mining: corpus build failed: %s", e)
        return []

    n_total = len(rows)
    if n_total < MIN_SAMPLE:
        return []  # silence-by-default: insufficient resolved corpus (the B26 data gate)

    base_wins = sum(1 for e in rows if (e.alpha_return or 0.0) > 0)
    base_rate = base_wins / n_total if n_total else 0.0

    candidates = _enumerate_candidates(rows)
    n_trials = len(candidates)
    if n_trials == 0:
        return []

    generated_at = datetime.now(UTC).isoformat()
    induced: list[InducedRule] = []
    for c in candidates:
        n = len(c.covered)
        win_rate = c.wins / n if n else 0.0
        wlb = wilson_lower_bound(c.wins, n)
        lift = (win_rate / base_rate) if base_rate > 0 else float("inf")
        raw_p = _binom_sf_ge(c.wins, n, base_rate)
        bonf_p = min(1.0, raw_p * n_trials)

        # Gate cascade — the verdict NAMES the first failing gate (silence-by-default
        # never proposes on a failed gate). Order matters: sample -> Wilson -> lift ->
        # Bonferroni, cheapest/most-fundamental first.
        if n < MIN_SAMPLE:
            verdict: Verdict = "INSUFFICIENT_SAMPLE"
        elif wlb < MIN_WILSON_LB:
            verdict = "FAILS_WILSON"
        elif lift < 1.0 + LIFT_MARGIN:
            verdict = "FAILS_LIFT"
        elif bonf_p > ALPHA:
            verdict = "FAILS_BONFERRONI"
        else:
            verdict = "PROPOSE"

        confident = (
            _holdout_confident(c, rows) if verdict == "PROPOSE" else False
        )
        thr_str = (
            f"{c.threshold:.4g}" if isinstance(c.threshold, (int, float)) else str(c.threshold)
        )
        reason = (
            f"if {c.feature} {c.op} {thr_str} then {c.direction} "
            f"({c.wins}/{n} win, winRate={win_rate:.2f}, wilsonLB={wlb:.2f}, "
            f"lift={lift:.2f}, bonferroniP={bonf_p:.3f}) -> {verdict}"
        )
        induced.append(
            InducedRule(
                feature=c.feature,
                op=c.op,
                threshold=c.threshold,
                direction=c.direction,
                n=n,
                wins=c.wins,
                win_rate=win_rate,
                wilson_lb=wlb,
                base_rate=base_rate,
                lift=lift,
                n_trials_tested=n_trials,
                bonferroni_p=bonf_p,
                verdict=verdict,
                confident=confident,
                reason=reason,
                generated_at=generated_at,
            )
        )

    # Keep only PROPOSE rules; rank by Wilson LOWER bound (NOT win_rate, so tiny-but-
    # perfect rules can't win), tie-break by support then a stable feature/op/threshold
    # key for byte-stable output. Emit the top max_rules (note §4: <=5).
    proposed = [r for r in induced if r.verdict == "PROPOSE"]
    proposed.sort(
        key=lambda r: (-r.wilson_lb, -r.n, r.feature, r.op, str(r.threshold))
    )
    return proposed[: max(0, max_rules)]


def format_report(rules: list[InducedRule]) -> str:
    """Compact human report of the proposed if-then rules. Silence-by-default on an
    empty list (no proposed rules yet — the inert/data-gated state)."""
    if not rules:
        return (
            "shadow rule-mine: no proposed rules yet "
            "(corpus empty, below MIN_SAMPLE, or no rule cleared the overfit guards)."
        )
    lines = ["Induced shadow-rule candidates (PROPOSE-only, lookahead-honest):"]
    for r in rules:
        flag = " [confident]" if r.confident else ""
        lines.append(f"  {r.reason}{flag}")
    return "\n".join(lines)


def write_candidates(
    rules: list[InducedRule],
    *,
    path: Path | None = None,
) -> int:
    """Write the CANDIDATE rule set for operator review (advisory plane). Returns count
    written. This is the ONLY write B27 performs; it NEVER touches ``default_rules()`` or
    any live decision path. Best-effort; never raises.

    DEFAULT-OFF and silence-by-default: writes nothing (returns 0) when disabled or when
    there are no PROPOSE-able rules — the file is not even created (mirrors
    graph_mining.write_candidates exactly)."""
    if not _mining_enabled():
        return 0  # default-OFF: bit-for-bit no-op
    proposed = [r for r in rules if r.verdict == "PROPOSE"]
    if not proposed:
        return 0  # silence-by-default: no proposed rules -> no file written
    generated_at = datetime.now(UTC).isoformat()
    payload = {
        # Header documents the propose-only / advisory-plane contract for any reader.
        "_note": (
            "ADVISORY PLANE (B27 / docs/research/2026-05-31-r-B27.md). These are "
            "PROPOSALS, not authority. The hand-coded shadow.rules.default_rules() stay "
            "operator-authored; the ONLY path to live policy is manual operator review -> "
            "manual promotion -> the deterministic risk gate, which this loop can never "
            "modify. Rules are induced from RESOLVED trades only (alpha_return is the "
            "label, never a feature — no-lookahead) and gated on the Wilson lower bound + "
            "lift>1 + Bonferroni-by-#trials. Never re-ingest this file (Oracle-Fallacy guard)."
        ),
        "provenance": _PROVENANCE,
        "generated_at": generated_at,
        "candidates": [r.to_dict() for r in proposed],
    }
    p = path or _DEFAULT_CANDIDATES
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    except OSError as e:  # noqa: BLE001
        logger.warning("shadow.rule_mining: candidate write failed: %s", e)
        return 0
    return len(proposed)
