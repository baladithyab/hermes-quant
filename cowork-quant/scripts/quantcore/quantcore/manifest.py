"""quantcore.manifest — immutable gate-config governance manifest (B-32, arch §4.7).

Institutional AI (2601.11369) showed prompt-only "constitutions" do NOT bind under
optimization pressure (Cohen's d=1.28 for an enforced manifest vs no reliable effect
for a prompt constitution). The lesson: the governance regime must be an immutable,
hash-pinned artifact, and every decision must be attributable to an exact regime.

A gate manifest is the canonical serialization of the gate's policy: the RiskConfig
(caps, breakers, Kelly fraction), the immutable SIZING_LADDER (rail #3), and the
screener taxonomy version. Its SHA-256 digest is stamped into the ledger as the first
event of each session, so every later proposal is attributable to "config SHA abc…"
— the difference between "the gate had some config" and "this decision was made under
config <digest>, here it is." Satisfies IOSCO Recordkeeping + FINRA audit-trail.

Widening the ladder already requires an ADR (rail #3); the manifest makes any config
change VISIBLE in the audit trail.

Also exports the canonical serialization helpers used by replay.py — byte-identical
output requires one shared, deterministic JSON form (sorted keys, fixed float
precision, ISO datetimes). stdlib only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from quantcore.config import RiskConfig
from quantcore.schemas import SIZING_LADDER

#: bump when the screener's reason-code taxonomy changes (arch §4.2)
SCREENER_TAXONOMY_VERSION = "1"
SCREENER_REASON_CODES = (
    "concentration",
    "turnover",
    "regime_mismatch",
    "evidence_stale",
)

#: bump on ANY change to the deterministic decision code (gate/kelly). The manifest
#: must pin CODE, not just config: a kelly.py change alters decisions under the same
#: config, and without this the digest would be unchanged and replay would
#: mis-attribute the decision to the wrong regime (review finding).
CODE_VERSION = "0.2.0"
KELLY_FORMULA_VERSION = "1"

#: rounding for canonical float serialization. IEEE-754 last-bit drift and
#: cross-arch FP differences break byte-identical replay; round before hashing.
_FLOAT_DP = 9


def _canon(obj: Any) -> Any:
    """Recursively normalize to a deterministic, JSON-safe form."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        # normalize -0.0 -> 0.0 and round to kill last-bit drift
        r = round(obj, _FLOAT_DP)
        return 0.0 if r == 0 else r
    if isinstance(obj, (int, str)) or obj is None:
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _canon(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if hasattr(obj, "model_dump"):  # pydantic v2
        return _canon(obj.model_dump(mode="json"))
    return str(obj)


def canonical_json(obj: Any) -> str:
    return json.dumps(_canon(obj), sort_keys=True, separators=(",", ":"))


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def gate_manifest(config: RiskConfig) -> dict[str, Any]:
    """The immutable governance regime for a session."""
    return {
        "manifest_version": "1",
        "risk_config": config.model_dump(mode="json"),
        "sizing_ladder": list(SIZING_LADDER),
        "screener_taxonomy_version": SCREENER_TAXONOMY_VERSION,
        "screener_reason_codes": list(SCREENER_REASON_CODES),
        "code_version": CODE_VERSION,
        "kelly_formula_version": KELLY_FORMULA_VERSION,
    }


def manifest_digest(config: RiskConfig) -> str:
    return canonical_hash(gate_manifest(config))


def write_session_manifest(ledger, config: RiskConfig) -> dict[str, Any]:
    """Append the session governance stamp to the ledger (call at session start).

    Idempotent within a session is the caller's concern; the digest makes
    duplicate stamps harmless (same digest) and config changes visible (new digest).
    """
    man = gate_manifest(config)
    digest = canonical_hash(man)
    return ledger.append(
        "session_manifest", {"manifest_digest": digest, "manifest": man}
    )
