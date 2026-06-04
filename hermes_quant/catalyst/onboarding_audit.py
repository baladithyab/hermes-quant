"""hermes_quant.catalyst.onboarding_audit — ADR-0075 pre-flip onboarding audit (seed 2b63).

The READ-ONLY gate-check the operator runs BEFORE flipping
``HERMES_QUANT_CATALYST_ONBOARDING=1`` (seed ba90). It wires the previously
orphaned :func:`hermes_quant.catalyst.eval.run_admission_precision` into a single
pass/fail report: of the out-of-universe names ADR-0075 onboarding would actually
admit, did a sufficient fraction beat the forward-return bar?

Rails (money-software, ADR-0075):
  * REPORT ONLY. This module MEASURES whether the bar is cleared. It NEVER reads,
    writes, sets, or flips ``HERMES_QUANT_CATALYST_ONBOARDING`` (or any env), and it
    never touches ~/.hermes. The flip is an OPERATOR action — the audit just tells
    the operator whether the gate would be defensible.
  * No vacuous pass. ``run_admission_precision`` fails on an empty admitted set
    (``n_scored == 0`` -> ``passed=False``), so a flag can never be flipped on zero
    evidence.
  * Deterministic + offline. Episodes (with their committed realized forward
    returns) come from a versioned fixture or an operator-supplied episodes file;
    no network, no price fetch. External truth, never self-graded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hermes_quant.catalyst.eval import AdmissionEpisode, AdmissionPrecisionResult, run_admission_precision
from hermes_quant.catalyst.onboarding import TAU_CONF, TAU_MAG

# The operator gate this audit informs (seed ba90 / ADR-0075). Named here as DATA,
# never read from the environment — the audit's verdict must not depend on whether
# onboarding is currently enabled.
ONBOARDING_FLAG = "HERMES_QUANT_CATALYST_ONBOARDING"

_OPERATOR_NOTE = (
    "REPORT ONLY. This audit measures whether the ADR-0075 admission-precision bar "
    f"is cleared; flipping {ONBOARDING_FLAG}=1 is an OPERATOR action this audit never "
    "performs. A PASS means the bar is cleared and the flip is defensible; it is not "
    "an instruction to flip."
)


@dataclass(frozen=True)
class OnboardingPreflipAudit:
    """Result of :func:`audit_onboarding_preflip` — the operator's pre-flip evidence.

    ``passed`` mirrors ``result.passed`` (admitted names beat the bar). ``flag`` and
    ``operator_note`` make explicit which operator gate this informs and that the
    flip is never automated.
    """

    passed: bool
    result: AdmissionPrecisionResult
    min_hit_rate: float
    flag: str = ONBOARDING_FLAG
    operator_note: str = _OPERATOR_NOTE

    def to_dict(self) -> dict:
        """Flat, JSON-serializable report (the shape the CLI prints with --json)."""
        return {
            "passed": self.passed,
            "flag": self.flag,
            "min_hit_rate": self.min_hit_rate,
            "n_episodes": self.result.n_episodes,
            "n_admitted": self.result.n_admitted,
            "n_scored": self.result.n_scored,
            "hits": self.result.hits,
            "hit_rate": self.result.hit_rate,
            "misses": list(self.result.misses),
            "rejected": list(self.result.rejected),
            "operator_note": self.operator_note,
        }


def load_admission_episodes(episodes_file: str | Path) -> list[AdmissionEpisode]:
    """Parse an admission-episodes JSON file into ``AdmissionEpisode`` records.

    The file is the versioned fixture
    (``tests/fixtures/catalyst_onboarding/admission_episodes.v1.json``) or any
    operator-curated set with the same shape. Realized forward returns are read
    verbatim (committed external truth) — never fetched.

    Raises ``FileNotFoundError`` if the file is absent (the operator must point at a
    real episode set; a missing file is an error, not a vacuous pass).
    """
    path = Path(episodes_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"admission episodes file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    episodes: list[AdmissionEpisode] = []
    for e in data.get("episodes", []):
        episodes.append(
            AdmissionEpisode(
                symbol=e["symbol"],
                stance=e["stance"],
                confidence=float(e["confidence"]),
                magnitude=float(e["magnitude"]),
                realized_forward_return=float(e["realized_forward_return"]),
                in_universe=bool(e.get("in_universe", False)),
                tradeable=bool(e.get("tradeable", True)),
                horizon=e.get("horizon", "1d"),
                label=e.get("label", ""),
            )
        )
    return episodes


def audit_onboarding_preflip(
    episodes_file: str | Path,
    *,
    min_hit_rate: float = 0.6,
    tau_conf: float = TAU_CONF,
    tau_mag: float = TAU_MAG,
) -> OnboardingPreflipAudit:
    """Run the ADR-0075 admission-precision gate over ``episodes_file`` and report
    pass/fail for the operator's pre-flip decision.

    READ-ONLY: computes ``run_admission_precision`` over the committed episodes and
    wraps the verdict. Does NOT read, set, or flip ``HERMES_QUANT_CATALYST_ONBOARDING``
    (or any env), and does not write any file. ``passed`` is True iff admitted
    out-of-universe names cleared the forward-return bar (``hit_rate >= min_hit_rate``
    over a non-empty scored set).
    """
    episodes = load_admission_episodes(episodes_file)
    result = run_admission_precision(
        episodes,
        min_hit_rate=min_hit_rate,
        tau_conf=tau_conf,
        tau_mag=tau_mag,
    )
    return OnboardingPreflipAudit(
        passed=result.passed,
        result=result,
        min_hit_rate=min_hit_rate,
    )
