"""hermes_quant.catalyst.profitability — measure catalyst-signal edge from the live log.

"Benefit from profitability" done honestly: instead of ASSUMING the catalyst signal
(especially the weak-eval consumer-trend class) makes money, this joins the append-only
propagation log against realized forward returns and reports directional hit-rate +
mean signed forward return, broken out BY RELATION CLASS (brand_self vs sector edges).

This is the live-feedback loop that decides whether to RAISE the consumer-trend
confidence haircut (`CONSUMER_TREND_CONFIDENCE_HAIRCUT`) toward 1.0 — or pull the edges.
A class that doesn't beat its hit-rate floor on accumulated live data should lose weight,
not gain it. The fidelity rule: forward return is measured from the NEXT bar after the
propagation's ``asof`` (lookahead-honest), and the graph never sees returns.

Offline + deterministic: realized returns are INJECTED (a fetcher callback), so this
module has no network dependency and is unit-testable. The ops runner wires yfinance.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LOG = Path.home() / ".hermes" / "quant" / "catalyst" / "propagation-log.jsonl"

# A relation class must clear this directional hit-rate on accumulated live data to
# justify carrying (or raising) its confidence weight. Mirrors the D74.7 precision bar.
MIN_HIT_RATE = 0.6
# Minimum scored propagations before a class's hit-rate is trusted at all (n=5 was a
# knife-edge; require more live evidence before acting on the live number).
MIN_SAMPLE = 20


@dataclass
class RelationStats:
    relation: str
    n_scored: int = 0
    hits: int = 0
    sum_signed_return: float = 0.0
    examples: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return (self.hits / self.n_scored) if self.n_scored else 0.0

    @property
    def mean_signed_return(self) -> float:
        return (self.sum_signed_return / self.n_scored) if self.n_scored else 0.0

    @property
    def verdict(self) -> str:
        if self.n_scored < MIN_SAMPLE:
            return "INSUFFICIENT_SAMPLE"
        if self.hit_rate >= MIN_HIT_RATE and self.mean_signed_return > 0:
            return "PROFITABLE"
        if self.hit_rate < 0.5 or self.mean_signed_return < 0:
            return "UNPROFITABLE_CONSIDER_PRUNE"
        return "MARGINAL_HOLD"

    def to_dict(self) -> dict:
        return {
            "relation": self.relation,
            "n_scored": self.n_scored,
            "hits": self.hits,
            "hit_rate": round(self.hit_rate, 4),
            "mean_signed_return_pct": round(self.mean_signed_return, 3),
            "verdict": self.verdict,
            "examples": self.examples[:5],
        }


# fetcher(symbol, asof_date) -> forward return % from the next bar (signed), or None.
ForwardReturnFetcher = Callable[[str, date], float | None]


def measure_profitability(
    fetcher: ForwardReturnFetcher,
    *,
    path: Path | None = None,
    max_rows: int = 5000,
) -> dict[str, RelationStats]:
    """Join the propagation log against realized forward returns, grouped by relation.

    Each log row carries: symbol, relation, symbol_sign (the propagated direction),
    asof (publication time). For each, the fetcher returns the realized forward return
    from the next tradeable bar; a "hit" is when sign(forward_return) == symbol_sign.
    Returns {relation: RelationStats}. Silence-by-default on a missing/empty log.
    """
    p = path or _DEFAULT_LOG
    if not p.exists():
        return {}
    stats: dict[str, RelationStats] = {}
    rows_seen = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if rows_seen >= max_rows:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows_seen += 1
            sym = row.get("symbol")
            relation = row.get("relation", "unknown")
            sym_sign = row.get("symbol_sign")
            asof = row.get("asof")
            if not sym or sym_sign is None or not asof:
                continue
            try:
                asof_date = datetime.fromisoformat(asof.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                continue
            fwd = fetcher(sym, asof_date)
            if fwd is None or fwd == 0:
                continue  # no realized data or flat — unscored
            st = stats.setdefault(relation, RelationStats(relation=relation))
            st.n_scored += 1
            st.sum_signed_return += fwd if sym_sign > 0 else -fwd  # signed-aligned return
            realized_sign = 1 if fwd > 0 else -1
            if realized_sign == sym_sign:
                st.hits += 1
            if len(st.examples) < 5:
                st.examples.append(f"{sym} sign={sym_sign:+d} fwd={fwd:+.1f}%")
    except OSError as e:  # noqa: BLE001
        logger.warning("catalyst.profitability: log read failed: %s", e)
        return {}
    return stats


def format_report(stats: dict[str, RelationStats]) -> str:
    """Compact human report: per-relation hit-rate + verdict, consumer-trend first."""
    if not stats:
        return "catalyst profitability: no scored propagations yet (log empty or no realized data)."
    lines = ["Catalyst-signal profitability by relation class (lookahead-honest):"]
    # consumer-trend (brand_self) first — it's the one whose weight we're deciding.
    order = sorted(stats.values(), key=lambda s: (s.relation != "brand_self", -s.n_scored))
    for s in order:
        lines.append(
            f"  {s.relation:14s} n={s.n_scored:4d} hit={s.hit_rate:.2f} "
            f"meanRet={s.mean_signed_return:+.2f}%  -> {s.verdict}"
        )
    bs = stats.get("brand_self")
    if bs is not None:
        lines.append("")
        if bs.verdict == "PROFITABLE":
            lines.append("  ACTION: consumer-trend cleared its bar on live data — consider RAISING "
                         "CONSUMER_TREND_CONFIDENCE_HAIRCUT toward 1.0.")
        elif bs.verdict == "UNPROFITABLE_CONSIDER_PRUNE":
            lines.append("  ACTION: consumer-trend is NOT paying on live data — keep the haircut low "
                         "or prune the edges. Do NOT raise weight.")
        else:
            lines.append(f"  ACTION: consumer-trend {bs.verdict} — hold the 0.5 haircut, accumulate more data.")
    return "\n".join(lines)
