"""hermes_quant.memory.retriever — Layer 3: BM25 retriever + Oracle Fallacy guard
(ADR-0042).

Gated by env var HERMES_QUANT_MEMORY_INJECT (default ON, FLAGS.md Tier A; set
=0 to opt out). With the flag explicitly =0 the injection site is skipped and
behavior is bit-identical to the pre-Wave-4 path; ON-by-default with no lessons
available also yields "(none)" (a no-op without memory data).

Oracle Fallacy guard (arxiv:2605.19337 §4.2)
---------------------------------------------------
HARD RULE: any reflection whose tau_observable >= asof is EXCLUDED before k-
selection.  This prevents an agent from "learning" from an episode whose
outcome narrative embeds future-knowledge that was not knowable at decision
time.

The canonical regression test is in tests/memory/test_oracle_fallacy.py.

BM25 retrieval
--------------
Uses rank-bm25 when available.  Falls back to a local 30-line BM25
implementation (same formula: Okapi BM25 k1=1.5, b=0.75) when the library
is not installed, and emits a one-time warning.

Cross-sector filtering
----------------------
Reads GICS sector from ~/.hermes/quant/cache/sector-beta-cache.json if
present.  When absent, cross_sector returns [] and
aggregate_stats.sector_cache_unavailable is set to True.
"""

from __future__ import annotations

import json
import logging
import math
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.home import quant_home as _resolve_quant_home
from hermes_quant.memory.decisions import MEMORY_HOME

# Read-site belief-freshness horizon (silence-by-default safety rail). A
# distilled belief is dropped at injection time once it is older than this many
# tier half-lives since asof_distilled — so a paused/dead weekly-retro PRODUCER
# cron degrades to "no beliefs injected" rather than leaking stale beliefs into a
# live capital decision. 2x half-life => weekly beliefs go stale after ~28d,
# monthly after ~120d. Enforced on every read, independent of producer liveness.
STALE_BELIEF_HALF_LIVES = 2.0

logger = logging.getLogger(__name__)

REFLECTIONS_PATH = MEMORY_HOME / "reflections.jsonl"
SECTOR_CACHE_PATH = _resolve_quant_home() / "cache" / "sector-beta-cache.json"

# ---------------------------------------------------------------------------
# BM25 availability
# ---------------------------------------------------------------------------

_BM25_CLASS = None
_BM25_WARNED = False


def _get_bm25_class():
    global _BM25_CLASS, _BM25_WARNED
    if _BM25_CLASS is not None:
        return _BM25_CLASS
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]
        _BM25_CLASS = BM25Okapi
        return _BM25_CLASS
    except ImportError:
        if not _BM25_WARNED:
            warnings.warn(
                "rank-bm25 not installed; falling back to local BM25 implementation. "
                "Install with: pip install rank-bm25",
                ImportWarning,
                stacklevel=4,
            )
            _BM25_WARNED = True
        return None


# ---------------------------------------------------------------------------
# Local BM25 (fallback, 30-line Okapi implementation)
# ---------------------------------------------------------------------------

class _LocalBM25:
    """Minimal Okapi BM25 (k1=1.5, b=0.75).

    f(q,d) = Σ_t idf(t) * tf(t,d)*(k1+1) / (tf(t,d) + k1*(1-b+b*|d|/avgdl))
    where idf(t) = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
    """

    k1: float = 1.5
    b: float = 0.75

    def __init__(self, corpus: list[list[str]]) -> None:
        self._N = len(corpus)
        self._avgdl = sum(len(d) for d in corpus) / max(self._N, 1)
        self._tf: list[dict[str, float]] = []
        df: dict[str, int] = defaultdict(int)
        for doc in corpus:
            tf: dict[str, float] = defaultdict(float)
            for tok in doc:
                tf[tok] += 1.0
            self._tf.append(dict(tf))
            for tok in set(doc):
                df[tok] += 1
        self._idf: dict[str, float] = {
            t: math.log((self._N - n + 0.5) / (n + 0.5) + 1)
            for t, n in df.items()
        }

    def get_scores(self, query: list[str]) -> list[float]:
        scores = []
        for i, tf in enumerate(self._tf):
            dl = sum(tf.values())
            score = 0.0
            for tok in query:
                if tok not in tf:
                    continue
                idf = self._idf.get(tok, 0.0)
                f = tf[tok]
                score += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
                )
            scores.append(score)
        return scores


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ResolvedDecision:
    """Compact representation of a resolved decision for context injection."""

    reflection_id: str
    decision_id: str
    asof: str                  # ISO-8601 UTC decision timestamp
    tau_observable: str        # ISO-8601 UTC — when outcome became knowable
    ticker: str
    rating: str
    raw_return: float
    alpha_return: float
    holding_days: int
    lesson: str                # reflection_text (verbatim)
    lesson_category: str
    outcome_quality: int


@dataclass
class AggregateStats:
    """Aggregate statistics over resolved decisions for a ticker."""

    ticker: str
    n_resolved: int = 0
    hit_rate: float = 0.0         # fraction of decisions with alpha_return > 0
    avg_alpha: float = 0.0
    avg_holding_days: float = 0.0
    open_positions_count: int = 0  # count of pending decisions (not yet resolved)
    sector_cache_unavailable: bool = False


@dataclass
class PastContext:
    """Full memory context returned by get_past_context()."""

    same_ticker: list[ResolvedDecision] = field(default_factory=list)
    cross_ticker: list[ResolvedDecision] = field(default_factory=list)
    cross_sector: list[ResolvedDecision] = field(default_factory=list)
    aggregate_stats: AggregateStats = field(
        default_factory=lambda: AggregateStats(ticker="")
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _tokenize(text: str) -> list[str]:
    """Very simple whitespace + lowercase tokenizer."""
    import re
    return re.findall(r"[a-zA-Z0-9]+", text.lower())


def _load_reflections(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with open(path) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except Exception:
                logger.warning("reflections.jsonl: skipping malformed row")
    return rows


def _load_decisions(decisions_path: Path) -> dict[str, dict[str, Any]]:
    """Load pending decision rows keyed by decision_id."""
    from hermes_quant.memory.decisions import DECISIONS_PATH
    p = decisions_path if decisions_path != SECTOR_CACHE_PATH else DECISIONS_PATH
    if not p.exists():
        return {}
    pending: dict[str, dict[str, Any]] = {}
    resolved_ids: set[str] = set()
    with open(p) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("kind") == "decision":
                pending[row["decision_id"]] = row
            elif row.get("kind") == "resolution":
                resolved_ids.add(row["decision_id"])
    return {k: v for k, v in pending.items() if k not in resolved_ids}


def _load_sector_cache(path: Path) -> dict[str, str]:
    """Load ticker→sector mapping. Returns {} if cache is absent or malformed."""
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        # Support two formats: {ticker: sector} or {ticker: {sector: ...}}
        result = {}
        for ticker, val in data.items():
            if isinstance(val, str):
                result[ticker.upper()] = val
            elif isinstance(val, dict):
                result[ticker.upper()] = val.get("sector", val.get("gics_sector", ""))
        return result
    except Exception:
        logger.warning("sector-beta-cache.json could not be loaded")
        return {}


def _row_to_resolved(row: dict[str, Any]) -> ResolvedDecision:
    return ResolvedDecision(
        reflection_id=str(row.get("reflection_id", "")),
        decision_id=str(row.get("decision_id", "")),
        asof=str(row.get("asof_resolution", "")),
        tau_observable=str(row.get("tau_observable", "")),
        ticker=str(row.get("ticker", "")).upper(),
        rating=str(row.get("rating", "")),
        raw_return=float(row.get("raw_return", 0) or 0),
        alpha_return=float(row.get("alpha_return", 0) or 0),
        holding_days=int(row.get("holding_days", 0) or 0),
        lesson=str(row.get("reflection_text", "")),
        lesson_category=str(row.get("lesson_category", "")),
        outcome_quality=int(row.get("outcome_quality", 3) or 3),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_past_context(
    ticker: str,
    asof: datetime,
    *,
    k_same_ticker: int = 5,
    k_cross_ticker: int = 3,
    k_cross_sector: int = 2,
    only_resolved: bool = True,
    reflections_path: Path | None = None,
    decisions_path: Path | None = None,
    sector_cache_path: Path | None = None,
    query_text: str | None = None,
) -> PastContext:
    """Retrieve past decision context for a committee turn.

    Oracle Fallacy guard (arxiv:2605.19337 §4.2)
    --------------------------------------------
    HARD RULE applied FIRST, before k-selection:
        candidates = [r for r in reflections
                      if r.tau_observable is not None
                      and r.tau_observable < asof]

    Any reflection whose tau_observable >= asof is unconditionally excluded.
    This prevents the agent from learning from episodes whose outcome narrative
    embeds future-knowledge.

    Parameters
    ----------
    ticker:
        Target ticker (case-insensitive).
    asof:
        Decision timestamp. Only reflections with tau_observable < asof are
        eligible.
    k_same_ticker, k_cross_ticker, k_cross_sector:
        How many items to return per bucket.
    only_resolved:
        When True (default), only include fully-resolved reflections.
    reflections_path:
        Override for tests.
    decisions_path:
        Override for tests (used to count open positions).
    sector_cache_path:
        Override for tests.
    query_text:
        Optional free-text used as the BM25 query for cross_ticker ranking.
        When None, the ticker itself is used as the query.

    Returns
    -------
    PastContext
    """
    _rpath = reflections_path or REFLECTIONS_PATH
    _spath = sector_cache_path or SECTOR_CACHE_PATH
    from hermes_quant.memory.decisions import DECISIONS_PATH as _DPATH
    _dpath = decisions_path or _DPATH

    ticker_upper = ticker.upper()

    # --- ensure asof is tz-aware ---
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    else:
        asof = asof.astimezone(UTC)

    # --- load raw reflections ---
    raw_rows = _load_reflections(_rpath)

    # -----------------------------------------------------------------------
    # Oracle Fallacy guard (HARD RULE) — MUST happen before any k-selection
    # -----------------------------------------------------------------------
    eligible: list[dict[str, Any]] = []
    for row in raw_rows:
        tau_str = row.get("tau_observable")
        if tau_str is None:
            continue  # not yet observable — exclude
        tau = _parse_dt(tau_str)
        if tau is None:
            continue
        if tau >= asof:
            # Future-knowledge: EXCLUDED
            continue
        eligible.append(row)

    # --- split by ticker ---
    same_ticker_rows = [r for r in eligible if r.get("ticker", "").upper() == ticker_upper]
    other_ticker_rows = [r for r in eligible if r.get("ticker", "").upper() != ticker_upper]

    # --- same_ticker: sort by recency (newest first → return oldest-first) ---
    def _asof_key(r: dict[str, Any]) -> str:
        return str(r.get("asof_resolution", ""))

    same_ticker_sorted = sorted(same_ticker_rows, key=_asof_key, reverse=True)[:k_same_ticker]
    same_ticker_sorted.reverse()  # oldest first per ADR-0042

    # --- cross_ticker: BM25 over thesis_summary ---
    cross_ticker: list[ResolvedDecision] = []
    if other_ticker_rows:
        query_tokens = _tokenize(query_text or ticker_upper)
        corpus = [_tokenize(r.get("reflection_text", "") + " " + r.get("ticker", ""))
                  for r in other_ticker_rows]

        bm25_cls = _get_bm25_class()
        if bm25_cls is not None:
            bm25 = bm25_cls(corpus)
            scores = bm25.get_scores(query_tokens)
        else:
            bm25_local = _LocalBM25(corpus)
            scores = bm25_local.get_scores(query_tokens)

        indexed = sorted(enumerate(other_ticker_rows), key=lambda iv: scores[iv[0]], reverse=True)
        cross_ticker = [_row_to_resolved(r) for _, r in indexed[:k_cross_ticker]]

    # --- cross_sector ---
    sector_cache = _load_sector_cache(_spath)
    sector_unavailable = len(sector_cache) == 0 and not _spath.exists()

    cross_sector: list[ResolvedDecision] = []
    if sector_cache:
        target_sector = sector_cache.get(ticker_upper, "")
        if target_sector:
            sector_rows = [
                r for r in other_ticker_rows
                if sector_cache.get(r.get("ticker", "").upper(), "") == target_sector
            ]
            sector_sorted = sorted(sector_rows, key=_asof_key, reverse=True)[:k_cross_sector]
            cross_sector = [_row_to_resolved(r) for r in sector_sorted]

    # --- aggregate stats ---
    all_same = [r for r in eligible if r.get("ticker", "").upper() == ticker_upper]
    n_resolved = len(all_same)
    hit_rate = (
        sum(1 for r in all_same if float(r.get("alpha_return", 0) or 0) > 0) / n_resolved
        if n_resolved > 0
        else 0.0
    )
    avg_alpha = (
        sum(float(r.get("alpha_return", 0) or 0) for r in all_same) / n_resolved
        if n_resolved > 0
        else 0.0
    )
    avg_holding_days = (
        sum(int(r.get("holding_days", 0) or 0) for r in all_same) / n_resolved
        if n_resolved > 0
        else 0.0
    )

    # open positions count — read from decisions log
    pending_decisions = _load_decisions(_dpath)
    open_count = sum(
        1 for row in pending_decisions.values()
        if row.get("ticker", "").upper() == ticker_upper
    )

    stats = AggregateStats(
        ticker=ticker_upper,
        n_resolved=n_resolved,
        hit_rate=round(hit_rate, 4),
        avg_alpha=round(avg_alpha, 6),
        avg_holding_days=round(avg_holding_days, 2),
        open_positions_count=open_count,
        sector_cache_unavailable=sector_unavailable,
    )

    return PastContext(
        same_ticker=[_row_to_resolved(r) for r in same_ticker_sorted],
        cross_ticker=cross_ticker,
        cross_sector=cross_sector,
        aggregate_stats=stats,
    )


def format_context_block(ctx: PastContext, max_chars: int = 2048) -> str:
    """Render PastContext as the PM-prompt injection block.

    Format per ADR-0042:
      [YYYY-MM-DD | TICKER | RATING | +RAW% | +ALPHA% | DAYSd]
      {reflection_text}

    Same-ticker first, then cross-ticker, then cross-sector. Empty → (none).
    """
    lines: list[str] = []

    def _render_section(items: list[ResolvedDecision], header: str) -> None:
        if not items:
            return
        lines.append(header)
        for item in items:
            date_str = item.asof[:10] if len(item.asof) >= 10 else item.asof
            lines.append(
                f"[{date_str} | {item.ticker} | {item.rating} | "
                f"{item.raw_return:+.1%} | {item.alpha_return:+.1%} | {item.holding_days}d]"
            )
            lines.append(item.lesson)

    _render_section(ctx.same_ticker, "--- Same-ticker history ---")
    _render_section(ctx.cross_ticker, "--- Cross-ticker analogs ---")
    _render_section(ctx.cross_sector, "--- Cross-sector analogs ---")

    if not lines:
        return "(none)"

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars - 3] + "..."
    return result


def format_context_block_split(
    ctx: PastContext,
    *,
    max_chars: int = 2048,
    rich_lesson_chars: int = 400,
    lean: bool = True,
) -> str:
    """Render PastContext with ASYMMETRIC verbosity (Wave C G15).

    The retrieval split (same_ticker / cross_ticker / cross_sector) already
    exists upstream in ``get_past_context``; this renderer differentiates how
    much of each bucket is surfaced so the most-relevant (same-ticker) context
    stays rich while weaker analogs are bounded to one line each:

      same_ticker  → RICH: fact line + full lesson (truncated to
                     ``rich_lesson_chars``) + lesson_category + outcome_quality.
      cross_ticker → LEAN (when ``lean``): single line
                     ``[date|TICKER|RATING|+alpha%]`` (no lesson).
      cross_sector → LEAN (when ``lean``): same one-line form.

    Section order is unchanged (same → cross-ticker → cross-sector). Empty
    context → ``"(none)"``. Output is clipped to ``max_chars`` with the same
    trailing-``"..."`` rule as ``format_context_block``.

    ``format_context_block`` is intentionally left UNCHANGED for back-compat;
    the existing ``llm_committee.py`` caller is not touched by this function.
    """
    lines: list[str] = []

    def _fact_line(item: ResolvedDecision) -> str:
        date_str = item.asof[:10] if len(item.asof) >= 10 else item.asof
        return (
            f"[{date_str} | {item.ticker} | {item.rating} | "
            f"{item.raw_return:+.1%} | {item.alpha_return:+.1%} | {item.holding_days}d]"
        )

    def _lean_line(item: ResolvedDecision) -> str:
        date_str = item.asof[:10] if len(item.asof) >= 10 else item.asof
        return (
            f"[{date_str}|{item.ticker}|{item.rating}|{item.alpha_return:+.1%}]"
        )

    def _render_rich(items: list[ResolvedDecision], header: str) -> None:
        if not items:
            return
        lines.append(header)
        for item in items:
            lines.append(_fact_line(item))
            lesson = item.lesson or ""
            if len(lesson) > rich_lesson_chars:
                lesson = lesson[:rich_lesson_chars] + "..."
            lines.append(lesson)
            tail_bits = []
            if item.lesson_category:
                tail_bits.append(f"category={item.lesson_category}")
            tail_bits.append(f"quality={item.outcome_quality}")
            lines.append(" ".join(tail_bits))

    def _render_lean(items: list[ResolvedDecision], header: str) -> None:
        if not items:
            return
        lines.append(header)
        for item in items:
            if lean:
                lines.append(_lean_line(item))
            else:
                lines.append(_fact_line(item))
                lines.append(item.lesson)

    _render_rich(ctx.same_ticker, "--- Same-ticker history ---")
    _render_lean(ctx.cross_ticker, "--- Cross-ticker analogs ---")
    _render_lean(ctx.cross_sector, "--- Cross-sector analogs ---")

    if not lines:
        return "(none)"

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars - 3] + "..."
    return result


# ---------------------------------------------------------------------------
# W2 weekly-retro belief injection (ADR-0081 §2 — selective propagation)
# ---------------------------------------------------------------------------

_BELIEF_ROLE_TAG: dict[str, str] = {
    "portfolio_manager": "PM",
    "research_manager": "RM",
    "bull_researcher": "BULL",
    "bear_researcher": "BEAR",
    "risk_aggressive": "RISK_AGG",
    "risk_conservative": "RISK_CON",
    "risk_neutral": "RISK_NEU",
}


def load_active_beliefs(
    role: str,
    asof: datetime,
    *,
    beliefs_path: Path | None = None,
) -> list[dict]:
    """Return active beliefs for `role` whose oracle_provenance.tau_observable_max < asof.

    Belief-level Oracle Fallacy guard — the SAME rule applied to reflections at
    line 351-362, lifted to the belief level so a distilled belief never surfaces an
    outcome that was not knowable at the decision asof. Reads beliefs.jsonl via
    weekly_retro.materialize_active. Returns [] when the file is absent (the
    default-OFF path is byte-identical).

    Also bumps the FINMEM access-counter for each surfaced belief
    (weekly_retro.access_touch) — best-effort, wrapped so a write failure never breaks
    retrieval.
    """
    from hermes_quant.memory import weekly_retro

    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    else:
        asof = asof.astimezone(UTC)

    bpath = beliefs_path or weekly_retro.BELIEFS_PATH
    if not bpath.exists():
        return []

    try:
        rows = weekly_retro.load_belief_rows(path=bpath)
        active = weekly_retro.materialize_active(rows, asof)
    except Exception:
        logger.warning("load_active_beliefs: belief store read failed; returning []")
        return []

    out: list[dict] = []
    for b in active:
        if b.role != role:
            continue  # selective propagation: only this role's beliefs
        # Read-site freshness guard (silence-by-default): a belief is only as
        # trustworthy as the producer that last refreshed it. The weekly-retro
        # PRODUCER cron (ops/scripts/quant-weekly-retro.py) decays/expires beliefs,
        # but it is opt-in — if it is paused, dead, or unset, beliefs would
        # otherwise materialize forever. We refuse to inject any belief older than
        # STALE_BELIEF_HALF_LIVES x its tier half-life since asof_distilled, so a
        # silent producer degrades to "no beliefs injected" (true silence) rather
        # than "stale beliefs injected into a live capital decision". Independent
        # of producer liveness; enforced on every read.
        _asof_distilled = _parse_dt(getattr(b, "asof_distilled", ""))
        _hl = float(getattr(b, "half_life_days", 0.0) or 0.0)
        if _asof_distilled is not None and _hl > 0.0:
            age_days = (asof - _asof_distilled).total_seconds() / 86400.0
            if age_days > STALE_BELIEF_HALF_LIVES * _hl:
                logger.info(
                    "load_active_beliefs: dropping STALE belief %s "
                    "(age %.1fd > %.1fx half-life %.1fd) — producer may be paused",
                    b.belief_id,
                    age_days,
                    STALE_BELIEF_HALF_LIVES,
                    _hl,
                )
                continue
        out.append(
            {
                "belief_id": b.belief_id,
                "role": b.role,
                "tier": b.tier,
                "lesson_category": b.lesson_category,
                "ticker": _belief_ticker(b),
                "verbal_delta": b.verbal_delta,
                "alpha_evidence": b.alpha_evidence,
                "support_n": b.support_n,
            }
        )
        # FINMEM access bump — best-effort, never raises.
        try:
            weekly_retro.access_touch(b.belief_id, path=bpath)
        except Exception:
            logger.warning(
                "load_active_beliefs: access_touch failed for %s (non-blocking)",
                b.belief_id,
            )
    return out


def _belief_ticker(belief: Any) -> str:
    """Best-effort ticker recovery from a Belief for the digest header.

    The ticker is encoded in the belief_id (bel_<tier>_<role>_<TICKER>_<hash>); the
    verbal_delta also opens with it. Recover from the id token before the hash.
    """
    parts = str(getattr(belief, "belief_id", "")).split("_")
    if len(parts) >= 5:
        return parts[-2]
    return ""


def format_beliefs_digest(beliefs: list[dict], *, max_chars: int = 768) -> str:
    """Render active beliefs as a compact digest block.

    Format::

        --- Distilled beliefs (weekly retro) ---
        [PM | thesis_invalidation_at_earnings | AAPL | +1.8% alpha | n=5]
        {verbal_delta}

    Empty -> "" (so the caller can cleanly skip prepending). Clipped to max_chars.
    """
    if not beliefs:
        return ""

    lines = ["--- Distilled beliefs (weekly retro) ---"]
    for b in beliefs:
        role_tag = _BELIEF_ROLE_TAG.get(str(b.get("role", "")), str(b.get("role", "")))
        ticker = str(b.get("ticker", "")) or "*"
        category = str(b.get("lesson_category", ""))
        alpha = float(b.get("alpha_evidence", 0.0) or 0.0)
        n = int(b.get("support_n", 0) or 0)
        lines.append(
            f"[{role_tag} | {category} | {ticker} | {alpha:+.1%} alpha | n={n}]"
        )
        lines.append(str(b.get("verbal_delta", "")))

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars - 3] + "..."
    return result
