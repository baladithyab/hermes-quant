"""hermes_quant.governance.static_scanner — forbidden-import + forbidden-symbol
static check (Vibe-Trading pattern, ADR-0031).

Rejects code/text containing broker-SDK references when called in 'research'
or 'paper' mode. Used at TWO call sites:
  1. ADR-0026 retrospective amendment loop, before applying a code_change
     to risk/** or proposals.py. The scanner runs after the file-path
     allowlist check but before HITL review.
  2. ADR-0030 methodology YAML loader, on rule_text fields. A methodology
     YAML cannot contain a literal broker call.

This is layer-3 of the live-trading defense:
  Layer 1: ADR-0029 D7 type-level LiveTradingApproval gate (no method)
  Layer 2: governance.invariants 'live_orders_blocked_in_research_mode'
  Layer 3: static_scanner (this module) — rejects raw text early
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

Mode = Literal["research", "paper", "shadow", "live"]

# Forbidden symbol patterns. Format: (pattern, severity, reason).
# Severity 'block' = always reject. 'warn_research' = reject in research/paper.
ForbiddenSymbol = tuple[str, Literal["block", "warn_research"], str]

FORBIDDEN_SYMBOLS: Final[tuple[ForbiddenSymbol, ...]] = (
    # Broker order-submission symbols. Block in research/paper mode.
    (
        r"\balpaca[\w.]*\.submit_order\b",
        "warn_research",
        "Direct alpaca.submit_order call — use PaperReactor or governance-gated LiveBroker",
    ),
    (
        r"\balpaca[\w.]*\.submit_mleg_order\b",
        "warn_research",
        "Multi-leg submission — only PaperBroker.submit_mleg_order is allowed",
    ),
    (
        r"\bccxt[\w.]*\.create_order\b",
        "warn_research",
        "CCXT live order — paper mode only",
    ),
    (
        r"\bibapi[\w.]*\.placeOrder\b",
        "warn_research",
        "Interactive Brokers placeOrder — not yet supported",
    ),
    # Live-trading marker imports. Always block.
    (
        r"from\s+hermes_quant\.react\.live\s+import\s+LiveBroker",
        "block",
        "LiveBroker construction is governance-only; never imported in code_change",
    ),
    # Naked HTTP POST to broker endpoints.
    (
        r"requests\.post\([^)]*alpaca\.markets",
        "block",
        "Direct HTTP POST to alpaca — use the SDK with the governance gate",
    ),
    (
        r"requests\.post\([^)]*tradier\.com",
        "block",
        "Direct HTTP POST to tradier — not authorized",
    ),
    # Bypass-the-risk-gate red flags.
    (
        r"\bbypass_risk_gate\b",
        "block",
        "Explicit risk-gate bypass — forbidden by ADR-0027 immutables",
    ),
    (
        r"\bskip_immutables?\b",
        "block",
        "Immutables can never be skipped",
    ),
    # The moon-dev anti-pattern: LLM substring-match disables daily loss limit.
    (
        r"if\s+['\"]?(disable|skip|ignore)['\"]?\s+in\s+\w+",
        "block",
        "Substring-match flag check — see AGENTS.md anti-pattern #1 (moon-dev risk_agent.py:319)",
    ),
)


@dataclass(frozen=True)
class ScanFinding:
    pattern: str
    severity: Literal["block", "warn_research"]
    reason: str
    line_number: int | None
    matched_text: str


@dataclass(frozen=True)
class ScanResult:
    blocked: bool
    findings: tuple[ScanFinding, ...]
    mode: Mode

    @property
    def blocking_findings(self) -> tuple[ScanFinding, ...]:
        return tuple(
            f
            for f in self.findings
            if f.severity == "block"
            or (self.mode in ("research", "paper") and f.severity == "warn_research")
        )


class StaticScannerError(Exception):
    """Raised by require_clean() when blocking findings are present."""


def scan_text(text: str, *, mode: Mode = "research") -> ScanResult:
    """Run all forbidden-symbol patterns against `text`. Returns a ScanResult
    with `blocked=True` iff any finding is blocking under the given mode.
    """
    findings: list[ScanFinding] = []
    for pattern, severity, reason in FORBIDDEN_SYMBOLS:
        for match in re.finditer(pattern, text):
            line_number = text[: match.start()].count("\n") + 1
            findings.append(
                ScanFinding(
                    pattern=pattern,
                    severity=severity,
                    reason=reason,
                    line_number=line_number,
                    matched_text=match.group(0)[:120],
                )
            )
    blocked = any(
        f.severity == "block" or (mode in ("research", "paper") and f.severity == "warn_research")
        for f in findings
    )
    return ScanResult(blocked=blocked, findings=tuple(findings), mode=mode)


def require_clean(text: str, *, mode: Mode = "research") -> None:
    """Raises StaticScannerError if text is not clean under given mode.
    Used as a gate at retro-amendment + methodology-YAML load points.
    """
    result = scan_text(text, mode=mode)
    if result.blocked:
        reasons = "\n".join(
            f"  line {f.line_number}: {f.reason} (matched: {f.matched_text!r})"
            for f in result.blocking_findings
        )
        raise StaticScannerError(f"Static scanner rejected text (mode={mode}):\n{reasons}")
