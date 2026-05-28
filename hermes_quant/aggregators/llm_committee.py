"""LLM-backed committee turns (ADR-0037).

Layered ON TOP of the deterministic skeleton in ``deliberative.py``. Calls
OpenRouter (or any OpenAI-compatible endpoint) to produce structured
bull/bear/judge turns and optionally a risk-management triumvirate plus a
portfolio-manager final synthesis.

Failure-closed posture (ADR-0037 §"Failure-closed posture"):
  * Any LLM call that raises, times out, returns invalid structured output,
    or fails Pydantic validation -> that turn is DROPPED.
  * Two consecutive drops in one tick -> return [] and let the
    deterministic aggregator's baseline-fallback handle it.

Two-tier LLM split (ADR-0037 §"Two-tier LLM split"):
  * bull/bear/risk_* roles -> ``config.quick_model``
  * research_manager + portfolio_manager -> ``config.deep_model``

Audit trail: every emitted CommitteeTurn carries a SHA-256 ``prompt_hash``
in its metadata. The hash is computed over the system + user message so
any prompt drift is detectable from the journal.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from hermes_quant.aggregators.deliberative import (
    _DEEP_REQUIRED_ROLES,
    CommitteeTurn,
    DeliberativeConfig,
)
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# Roles emitted by this module, in execution order. The bull/bear pair is
# the first round; max_debate_rounds expands the pair count.
_BULL_BEAR_ROLES: tuple[str, str] = ("bull_researcher", "bear_researcher")
_RISK_ROLES: tuple[str, str, str] = (
    "risk_aggressive",
    "risk_conservative",
    "risk_neutral",
)

# Map role -> prompt template filename.
_PROMPT_FILES: dict[str, str] = {
    "bull_researcher": "bull_bear.md",
    "bear_researcher": "bull_bear.md",
    "research_manager": "research_manager.md",
    "risk_aggressive": "risk_aggressive.md",
    "risk_conservative": "risk_conservative.md",
    "risk_neutral": "risk_neutral.md",
    "portfolio_manager": "portfolio_manager.md",
}


# ---------------------------------------------------------------------------
# Pydantic models for structured LLM output
# ---------------------------------------------------------------------------


class BullBearTurn(BaseModel):
    """Structured output of a bull or bear researcher turn."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    role: Literal["bull_researcher", "bear_researcher"]
    stance: str = Field(..., min_length=1, max_length=512)
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., min_length=1, max_length=8000)
    key_evidence: list[str] = Field(default_factory=list, max_length=20)
    counterarguments: str = Field(..., min_length=1, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchPlan(BaseModel):
    """Structured output of the deep-tier research manager judge."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    recommendation: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., min_length=1, max_length=6000)
    overrules_baseline: bool
    strategic_actions: str = Field(..., min_length=1, max_length=4000)
    horizon_emphasis: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskTurn(BaseModel):
    """Structured output of a risk-management triumvirate member."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    role: Literal["risk_aggressive", "risk_conservative", "risk_neutral"]
    stance: str = Field(..., min_length=1, max_length=512)
    proposed_size_multiplier: float = Field(..., ge=0.0, le=2.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., min_length=1, max_length=4000)
    risk_flags: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PortfolioDecision(BaseModel):
    """Structured output of the deep-tier portfolio manager."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    action: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    size_multiplier: float = Field(..., ge=0.0, le=2.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., min_length=1, max_length=4000)
    vetoed: bool
    veto_source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("veto_source")
    @classmethod
    def _normalize_veto_source(cls, v: str | None) -> str | None:
        if v in ("", "null", "None"):
            return None
        return v


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _direction_from_recommendation(rec: str) -> int:
    """Map 5-tier recommendation to a -1/0/+1 Direction for CommitteeTurn."""
    if rec in ("Buy", "Overweight"):
        return 1
    if rec in ("Sell", "Underweight"):
        return -1
    return 0


def _direction_from_role(role: str) -> int:
    """Bull/bear roles have a fixed direction; others are 0 by default."""
    if role == "bull_researcher":
        return 1
    if role == "bear_researcher":
        return -1
    return 0


def _load_prompt(role: str) -> str:
    """Load the prompt template for a role. Raises if missing (config bug)."""
    fname = _PROMPT_FILES.get(role)
    if fname is None:
        raise KeyError(f"No prompt template registered for role {role!r}")
    path = _PROMPT_DIR / fname
    return path.read_text(encoding="utf-8")


def _split_system_user(template: str) -> tuple[str, str]:
    """Split a prompt template into (system, user) messages.

    Templates use a literal ``SYSTEM:`` and ``USER:`` marker at column 1.
    """
    if "SYSTEM:" not in template or "USER:" not in template:
        raise ValueError(
            "Prompt template missing SYSTEM:/USER: markers; cannot split"
        )
    head, _, rest = template.partition("SYSTEM:")
    sys_text, _, user_text = rest.partition("USER:")
    if head.strip():
        # Anything before the SYSTEM marker is a comment / preamble; drop.
        pass
    return sys_text.strip(), user_text.strip()


def _serialize_views(views: list[AnalystView]) -> str:
    """Serialize analyst views to a compact JSON list for prompt inclusion.

    Drops large/opaque fields (metadata) and rounds floats so the prompt
    stays bounded and reproducible.
    """
    rows = []
    for v in views:
        rows.append(
            {
                "analyst": v.analyst,
                "direction": v.direction,
                "magnitude": round(float(v.magnitude), 6),
                "confidence": round(float(v.confidence), 4),
                "horizon": v.horizon,
                "rationale": (v.rationale or "")[:256],
            }
        )
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def _serialize_signal(signal: AggregatedSignal) -> str:
    return json.dumps(
        {
            "asset": signal.asset,
            "timeframe": signal.timeframe,
            "asset_class": signal.asset_class,
            "direction": int(signal.direction),
            "magnitude": round(float(signal.magnitude), 6),
            "confidence": round(float(signal.confidence), 4),
            "horizon": signal.horizon,
            "aggregator": signal.aggregator,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _serialize_prior_turns(turns: list[CommitteeTurn]) -> str:
    rows = []
    for t in turns:
        d = asdict(t) if dataclasses.is_dataclass(t) else dict(t)
        # Drop large metadata blob from the prompt context.
        d.pop("metadata", None)
        d.pop("input_hash", None)
        rows.append(d)
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def _render_prompt(
    *,
    role: str,
    market_context: MarketContext,
    analyst_views: list[AnalystView],
    baseline_signal: AggregatedSignal,
    prior_turns: list[CommitteeTurn],
    current_response: str | None = None,
    own_history: str | None = None,
    round_index: int | None = None,
    conversational_preamble: str | None = None,
) -> tuple[str, str]:
    """Render system + user messages for a role.

    Returns (system_text, user_text). The two messages are kept separate so
    the LLM client can pass them as ``role='system'`` and ``role='user'``.

    Wave 4 (ADR-0042): When env var HERMES_QUANT_MEMORY_INJECT=1, the
    portfolio_manager prompt is amended with a ``Lessons from prior decisions
    and outcomes:`` block populated from get_past_context().  Default OFF —
    bit-identical to pre-Wave-4 when the env var is absent.  Bull, bear, and
    risk debaters do NOT see the lessons block (ADR-0042 anti-pattern list).

    ADR-0065 (v0.6.1): when called from ``run_research_debate`` (the
    Bull/Bear adversarial stage), four extra placeholders may be supplied:
    ``current_response`` (opponent's last argument verbatim),
    ``own_history`` (caller's own running thread),
    ``round_index`` (1-based debate round number),
    ``conversational_preamble`` (style preamble shared with the risk committee).
    Legacy callers (the parallel-emit ``run_llm_committee`` path) leave these
    None and the renderer substitutes safe sentinels so the prompt template
    formats without raising. The legacy path may produce a different
    ``prompt_hash`` than v0.6.0 because the underlying ``bull_bear.md``
    template was rewritten — see ADR-0065 §Test Plan T10.
    """
    template = _load_prompt(role)
    system_tmpl, user_tmpl = _split_system_user(template)

    role_label = {
        "bull_researcher": "Bull Researcher",
        "bear_researcher": "Bear Researcher",
        "research_manager": "Research Manager",
        "risk_aggressive": "Aggressive Risk Manager",
        "risk_conservative": "Conservative Risk Manager",
        "risk_neutral": "Neutral Risk Manager",
        "portfolio_manager": "Portfolio Manager",
    }.get(role, role)
    role_direction = "bullish" if role == "bull_researcher" else "bearish"

    # -----------------------------------------------------------------------
    # Wave 4 memory injection (HERMES_QUANT_MEMORY_INJECT=1, default OFF).
    # Only portfolio_manager (and research_manager) see the lessons block.
    # Debaters (bull/bear/risk) are deliberately kept clean (ADR-0042).
    # -----------------------------------------------------------------------
    lessons_block = "(none)"
    if role in ("portfolio_manager", "research_manager"):
        if os.environ.get("HERMES_QUANT_MEMORY_INJECT", "0") == "1":
            try:
                from hermes_quant.memory.retriever import (
                    format_context_block,
                    get_past_context,
                )
                asof_dt = market_context.asof if isinstance(market_context.asof, __import__("datetime").datetime) else None  # type: ignore[attr-defined]
                if asof_dt is None:
                    from datetime import UTC, datetime
                    asof_dt = datetime.now(UTC)
                ctx = get_past_context(
                    ticker=market_context.asset,
                    asof=asof_dt,
                )
                lessons_block = format_context_block(ctx, max_chars=2048)
            except Exception:
                logger.warning(
                    "Memory injection failed for role=%r (non-blocking); "
                    "using '(none)'",
                    role,
                )

    fmt: dict[str, Any] = {
        "role": role,
        "role_value": role,
        "role_label": role_label,
        "role_direction": role_direction,
        "asset": market_context.asset,
        "asset_class": market_context.asset_class,
        "horizon": baseline_signal.horizon,
        "asof": str(market_context.asof),
        "n_views": len(analyst_views),
        "analyst_views_json": _serialize_views(analyst_views),
        "baseline_signal_json": _serialize_signal(baseline_signal),
        "prior_turns_json": _serialize_prior_turns(prior_turns),
        "lessons_block": lessons_block,
        # ADR-0065 conversational placeholders. Legacy callers (None) get safe
        # sentinels so the format string resolves; stage callers pass real
        # values that change the prompt hash per round (T6/T7 invariant).
        "current_response": (
            current_response
            if current_response is not None
            else "(no prior turn — open the debate)"
        ),
        "own_history": (
            own_history
            if own_history is not None
            else "(no prior turns by you yet)"
        ),
        "round_index": round_index if round_index is not None else 1,
        "conversational_preamble": (
            conversational_preamble
            if conversational_preamble is not None
            else "Output conversationally as if you are speaking without any special formatting"
        ),
    }

    # Use ``str.format_map`` with a defaultdict-like fallback so missing
    # keys raise loudly during testing rather than silently leaving raw
    # ``{placeholder}`` strings in the prompt.
    try:
        system_text = system_tmpl.format(**fmt)
        user_text = user_tmpl.format(**fmt)
    except KeyError as exc:
        raise KeyError(
            f"Prompt template for role {role!r} references missing placeholder {exc}"
        ) from exc
    return system_text, user_text


def _prompt_hash(system_text: str, user_text: str) -> str:
    h = hashlib.sha256()
    h.update(system_text.encode("utf-8"))
    h.update(b"\n---USER---\n")
    h.update(user_text.encode("utf-8"))
    return h.hexdigest()


def _expected_tier_for_role(role: str) -> Literal["quick", "deep"]:
    # The deterministic skeleton's _DEEP_REQUIRED_ROLES is keyed on the
    # CommitteeRole literal (trader, portfolio_manager). Per ADR-0037, the
    # research_manager judge is also deep-tier, even though the emitted
    # CommitteeTurn fills the portfolio_manager slot in the role enum. So we
    # extend the deep set with "research_manager" for model-selection purposes.
    if role in _DEEP_REQUIRED_ROLES or role == "research_manager":
        return "deep"
    return "quick"


def _model_for_role(role: str, config: DeliberativeConfig) -> str:
    return config.deep_model if _expected_tier_for_role(role) == "deep" else config.quick_model


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible, defaults to OpenRouter)
# ---------------------------------------------------------------------------


def _build_openai_client(config: DeliberativeConfig):
    """Construct an OpenAI-compatible client.

    Uses the ``openai`` SDK with a configurable ``base_url``. Returns None
    if the SDK or API key is unavailable -- callers must treat None as a
    failure-closed signal (drop the turn).
    """
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        logger.warning(
            "LLM committee disabled: env var %s is not set", config.api_key_env
        )
        return None
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        logger.warning("openai SDK not importable; LLM committee disabled")
        return None
    try:
        return OpenAI(
            api_key=api_key,
            base_url=config.api_base_url,
            timeout=config.request_timeout_s,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to construct OpenAI client; LLM committee disabled")
        return None


def _call_llm_json(
    *,
    client: Any,
    model: str,
    system_text: str,
    user_text: str,
    max_tokens: int,
) -> str | None:
    """Invoke the LLM and return the raw JSON string content.

    Failure-closed: returns None on any exception, timeout, or empty body.
    The structured-output JSON-mode is requested via ``response_format``
    when available; failure to honor that is treated identically to any
    other LLM failure (drop the turn). Per ADR-0037 each call is exactly
    one network attempt — we do not retry, because retrying inside one
    turn confuses the "two consecutive failures -> bail" counter.
    """
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except Exception:  # noqa: BLE001
        logger.exception("LLM call failed for model %r", model)
        return None
    try:
        content = resp.choices[0].message.content  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        logger.warning("LLM response missing choices/content for model %r", model)
        return None
    if not content or not content.strip():
        return None
    return str(content)


def _parse_pydantic(raw: str, model_cls: type[BaseModel]) -> BaseModel | None:
    """Strict Pydantic parse. Returns None on JSON or validation failure."""
    if raw is None:
        return None
    text = raw.strip()
    # Some providers fence JSON in ```json ... ```; be defensive but strict.
    if text.startswith("```"):
        # Strip the first fence and any closing fence.
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LLM output not valid JSON; dropping turn")
        return None
    try:
        return model_cls.model_validate(obj)
    except ValidationError as exc:
        logger.warning("LLM output failed Pydantic validation: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Per-role runners
# ---------------------------------------------------------------------------


def _run_one_turn(
    *,
    role: str,
    client: Any,
    config: DeliberativeConfig,
    market_context: MarketContext,
    analyst_views: list[AnalystView],
    baseline_signal: AggregatedSignal,
    prior_turns: list[CommitteeTurn],
) -> CommitteeTurn | None:
    """Run a single LLM-backed turn. Returns None on any failure.

    Caller is responsible for failure-counting (two consecutive drops -> bail).
    """
    model = _model_for_role(role, config)
    tier = _expected_tier_for_role(role)
    try:
        system_text, user_text = _render_prompt(
            role=role,
            market_context=market_context,
            analyst_views=analyst_views,
            baseline_signal=baseline_signal,
            prior_turns=prior_turns,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to render prompt for role %r", role)
        return None
    phash = _prompt_hash(system_text, user_text)

    raw = _call_llm_json(
        client=client,
        model=model,
        system_text=system_text,
        user_text=user_text,
        max_tokens=config.max_tokens_per_turn,
    )
    if raw is None:
        return None

    if role in _BULL_BEAR_ROLES:
        parsed = _parse_pydantic(raw, BullBearTurn)
        if parsed is None or not isinstance(parsed, BullBearTurn):
            return None
        if parsed.role != role:
            logger.warning(
                "LLM returned role=%r but we asked for %r; dropping",
                parsed.role,
                role,
            )
            return None
        return CommitteeTurn(
            role=role,  # type: ignore[arg-type]
            stance=parsed.stance,
            direction=_direction_from_role(role),  # type: ignore[arg-type]
            confidence=parsed.confidence,
            rationale=parsed.rationale,
            model=f"llm:{model}",
            input_hash=None,
            metadata={
                "tier": tier,
                "model_id": model,
                "prompt_hash": phash,
                "key_evidence": list(parsed.key_evidence),
                "counterarguments": parsed.counterarguments,
                "structured": parsed.model_dump(),
            },
            tier=tier,
        )

    if role == "research_manager":
        parsed = _parse_pydantic(raw, ResearchPlan)
        if parsed is None or not isinstance(parsed, ResearchPlan):
            return None
        # The judge maps onto the deep-tier portfolio_manager slot in the
        # deterministic skeleton's CommitteeRole literal. Force tier="deep"
        # so the deterministic aggregator's tier-split filter accepts it.
        return CommitteeTurn(
            role="portfolio_manager",
            stance=f"judge:{parsed.recommendation}",
            direction=_direction_from_recommendation(parsed.recommendation),  # type: ignore[arg-type]
            confidence=parsed.confidence,
            rationale=parsed.rationale,
            model=f"llm:{model}",
            input_hash=None,
            metadata={
                "tier": "deep",
                "model_id": model,
                "prompt_hash": phash,
                "logical_role": "research_manager",
                "recommendation": parsed.recommendation,
                "overrules_baseline": parsed.overrules_baseline,
                "strategic_actions": parsed.strategic_actions,
                "horizon_emphasis": parsed.horizon_emphasis,
                "structured": parsed.model_dump(),
            },
            tier="deep",
        )

    if role in _RISK_ROLES:
        parsed = _parse_pydantic(raw, RiskTurn)
        if parsed is None or not isinstance(parsed, RiskTurn):
            return None
        if parsed.role != role:
            logger.warning(
                "LLM returned role=%r but we asked for %r; dropping",
                parsed.role,
                role,
            )
            return None
        # Risk turns honor the judge's direction; we don't use them to flip
        # direction here (the size multiplier is the lever).
        return CommitteeTurn(
            role=role,  # type: ignore[arg-type]
            stance=parsed.stance,
            direction=_direction_from_role(role),  # type: ignore[arg-type] — 0 by default
            confidence=parsed.confidence,
            rationale=parsed.rationale,
            model=f"llm:{model}",
            input_hash=None,
            metadata={
                "tier": tier,
                "model_id": model,
                "prompt_hash": phash,
                "proposed_size_multiplier": parsed.proposed_size_multiplier,
                "risk_flags": list(parsed.risk_flags),
                "structured": parsed.model_dump(),
            },
            tier=tier,
        )

    if role == "portfolio_manager":
        parsed = _parse_pydantic(raw, PortfolioDecision)
        if parsed is None or not isinstance(parsed, PortfolioDecision):
            return None
        return CommitteeTurn(
            role="portfolio_manager",
            stance=f"final:{parsed.action}",
            direction=_direction_from_recommendation(parsed.action),  # type: ignore[arg-type]
            confidence=parsed.confidence,
            rationale=parsed.rationale,
            model=f"llm:{model}",
            input_hash=None,
            metadata={
                "tier": tier,
                "model_id": model,
                "prompt_hash": phash,
                "logical_role": "portfolio_manager",
                "size_multiplier": parsed.size_multiplier,
                "vetoed": parsed.vetoed,
                "veto_source": parsed.veto_source,
                "structured": parsed.model_dump(),
            },
            tier=tier,
        )

    logger.warning("Unknown role for LLM committee turn: %r", role)
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_llm_committee(
    *,
    market_context: MarketContext,
    analyst_views: list[AnalystView],
    baseline_signal: AggregatedSignal,
    config: DeliberativeConfig,
    client: Any | None = None,
) -> list[CommitteeTurn]:
    """Run the LLM-backed committee and return ordered ``CommitteeTurn`` list.

    The returned list is intended to be pushed into
    ``context.extras['committee_turns']`` for the deterministic aggregator
    in ``deliberative.py`` to consume. The deterministic aggregator's
    intake validators (tier-split, msg-clear, turn-cap) still apply -- this
    module honors them by construction.

    Failure-closed posture (ADR-0037):
      * Any exception, timeout, JSON-parse error, or Pydantic validation
        failure -> the offending turn is dropped (None returned).
      * Two consecutive drops -> we abandon the rest of this tick and
        return whatever we have so far. The deterministic aggregator's
        BMA-fallback covers the rest.

    Args:
      market_context: tick context (asset, asof, ...).
      analyst_views: calibrated analyst outputs already produced for this tick.
      baseline_signal: BMA aggregator output for this tick (the measurable
        the LLM judge can agree-with-or-overrule).
      config: ``DeliberativeConfig``. Returns [] immediately when
        ``enable_llm_turns`` is False.
      client: optional pre-built OpenAI-compatible client (for tests).
        When None, one is constructed from ``config``.

    Returns:
      List of ``CommitteeTurn`` objects ready for the deterministic
      aggregator. May be empty when LLM is disabled, fails, or all turns
      were dropped.
    """
    if not config.enable_llm_turns:
        return []

    if client is None:
        client = _build_openai_client(config)
    if client is None:
        return []

    turns: list[CommitteeTurn] = []
    consecutive_failures = 0

    # ADR-0065 (v0.6.1, G1): research debate stage dispatch.
    # v0.6.1 ships behind HERMES_QUANT_RESEARCH_DEBATE=0 default. When ON, currently logs and falls through
    # to legacy path because _run_one_turn_with_history / _run_research_manager_judge production helpers
    # are not yet wired (deferred to v0.6.2). See ADR-0065 §Implementation Plan §7.
    if os.environ.get("HERMES_QUANT_RESEARCH_DEBATE", "0") == "1":
        logger.warning(
            "HERMES_QUANT_RESEARCH_DEBATE=1 set but production wiring deferred to v0.6.2. "
            "Falling through to legacy bull/bear committee for this tick."
        )

    def _emit(role: str) -> bool:
        nonlocal consecutive_failures
        try:
            turn = _run_one_turn(
                role=role,
                client=client,
                config=config,
                market_context=market_context,
                analyst_views=analyst_views,
                baseline_signal=baseline_signal,
                prior_turns=turns,
            )
        except Exception:  # noqa: BLE001 — failure-closed at the top
            logger.exception("Unexpected failure in LLM committee turn role=%r", role)
            turn = None
        if turn is None:
            consecutive_failures += 1
            return False
        consecutive_failures = 0
        turns.append(turn)
        return True

    # 1) Bull/bear debate, ``max_debate_rounds`` rounds. Each round emits
    #    one bull turn followed by one bear turn (for a total of
    #    ``2 * max_debate_rounds`` bull/bear turns at most). The
    #    deterministic aggregator's bull/bear turn cap still applies as a
    #    second-line defence.
    rounds = max(1, int(config.max_debate_rounds))
    for _ in range(rounds):
        for role in _BULL_BEAR_ROLES:
            if not _emit(role) and consecutive_failures >= 2:
                logger.warning(
                    "LLM committee bailing out after 2 consecutive failures "
                    "(asset=%s, role=%s)",
                    market_context.asset,
                    role,
                )
                return []

    # 2) Research manager judge (deep tier).
    if not _emit("research_manager") and consecutive_failures >= 2:
        return []

    # 3) Optional risk-management triumvirate + portfolio manager.
    if config.enable_risk_mgmt:
        for role in _RISK_ROLES:
            if not _emit(role) and consecutive_failures >= 2:
                logger.warning(
                    "LLM committee bailing out after 2 consecutive risk-mgmt failures"
                )
                return turns  # keep judged + bull/bear; drop subsequent
        # Portfolio manager final synthesis only when the risk turns ran.
        if not _emit("portfolio_manager") and consecutive_failures >= 2:
            return turns

    return turns
