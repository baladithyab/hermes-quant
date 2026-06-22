"""quantcore.replay — determinism-replay assurance for the gate (B-32, arch §4.7).

DFAH / Replayable Financial Agents (2601.15322): determinism and accuracy are
statistically uncorrelated (r=-0.11) — measure them separately. The LLM committee
is non-deterministic; the discipline (DFAH "schema-first boundary") is that the
committee's structured output is a RECORDED INPUT ARTIFACT, and everything downstream
of it — the deterministic gate — is bit-for-bit replayable.

A DecisionRecord captures the full input closure of a gate call (config + signal +
costs + portfolio) plus the GateDecision it produced. `assert_replayable` re-runs the
gate from the stored inputs ONLY (no live data, no clock, no RNG) and asserts the
canonical hash of the re-derived decision matches the recorded one. This is the
examiner's "reproduce the flagged decision" requirement.

Float/serialization traps that break byte-identical replay are handled by the shared
canonical_json in manifest.py (sorted keys, fixed precision, ISO datetimes).

stdlib + pydantic only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quantcore.config import RiskConfig
from quantcore.gate import GateDecision, RiskGate
from quantcore.manifest import canonical_hash, manifest_digest
from quantcore.schemas import CommitteeSignal, MarketCosts, PortfolioState


@dataclass
class DecisionRecord:
    """The full input closure of one gate decision + its output."""

    config: RiskConfig
    signal: CommitteeSignal
    costs: MarketCosts
    portfolio: PortfolioState
    decision: GateDecision
    stored_digest: str | None = None  # the manifest digest as recorded (if any)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_digest": manifest_digest(self.config),
            "config": self.config.model_dump(mode="json"),
            "signal": self.signal.model_dump(mode="json"),
            "costs": self.costs.model_dump(mode="json"),
            "portfolio": self.portfolio.model_dump(mode="json"),
            "decision": self.decision.model_dump(mode="json"),
            "decision_hash": canonical_hash(self.decision),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DecisionRecord":
        return cls(
            config=RiskConfig(**d["config"]),
            signal=CommitteeSignal(**d["signal"]),
            costs=MarketCosts(**d["costs"]),
            portfolio=PortfolioState(**d["portfolio"]),
            decision=GateDecision(**d["decision"]),
            stored_digest=d.get("manifest_digest"),
        )


def replay_gate(
    config: RiskConfig,
    signal: CommitteeSignal,
    costs: MarketCosts,
    portfolio: PortfolioState,
) -> GateDecision:
    """Pure re-run of the gate from stored inputs only."""
    return RiskGate(config).gate(signal, costs, portfolio)


def assert_replayable(record: DecisionRecord) -> str:
    """Re-derive the decision and assert byte-identical structured output.
    Returns the matching canonical hash on success; raises AssertionError with a
    diff-ish message on mismatch.

    If the record carries a stored manifest digest, FIRST assert the config matches
    that digest — otherwise a record whose config was tampered to disagree with its
    stamped governance regime would replay 'successfully' against the tampered config
    (review finding: attribution gap).
    """
    if record.stored_digest is not None:
        live = manifest_digest(record.config)
        if live != record.stored_digest:
            raise AssertionError(
                "config does not match its stamped manifest digest "
                f"(stored={record.stored_digest}, config={live}) — governance "
                "regime tampered; refusing to attribute this decision."
            )
    redo = replay_gate(record.config, record.signal, record.costs, record.portfolio)
    want = canonical_hash(record.decision)
    got = canonical_hash(redo)
    if want != got:
        raise AssertionError(
            "gate decision is NOT replayable:\n"
            f"  recorded={record.decision.model_dump(mode='json')}\n"
            f"  replayed={redo.model_dump(mode='json')}"
        )
    return got
