"""Wire the Wave-3 'AI-efficiency' sleeve as candidate symbols for the playbook scorers.

The watchlist auto-evolves from the Alpaca liquidity universe; this script writes an
EXPLICIT candidate sleeve so the playbook scorers evaluate the cost-heavy / margin-
expansion names regardless of whether the liquidity screen surfaces them. Onboarding
still requires the normal 3-consecutive-days-above-0.65-floor sticky rule — this just
guarantees the names are in the scored set.

Candidates (from the 2026-05-30 Wave-3 screen; tilted toward internal-cost-cutters over
labor-arbitrage resellers): AMZN (anchor), ACN, UPS, KNX, HCA, TGT, DASH, PGR.
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

WAVE3_SLEEVE = ["AMZN", "ACN", "UPS", "KNX", "HCA", "TGT", "DASH", "PGR"]
_DEST = Path.home() / ".hermes" / "quant" / "watchlist" / "wave3-candidates.json"


def main() -> int:
    payload = {
        "as_of": datetime.now(UTC).isoformat(),
        "sleeve": "wave3_ai_efficiency",
        "rationale": "cost-heavy businesses with AI margin-expansion runway; "
                     "internal-cost-cutters favored over labor-arbitrage resellers (BPO/IT-services "
                     "score high but are AI disintermediation targets).",
        "symbols": WAVE3_SLEEVE,
    }
    _DEST.parent.mkdir(parents=True, exist_ok=True)
    _DEST.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {len(WAVE3_SLEEVE)} Wave-3 candidates to {_DEST}")
    print(f"symbols: {WAVE3_SLEEVE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
