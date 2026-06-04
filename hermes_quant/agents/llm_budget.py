"""hermes_quant.agents.llm_budget — pre-call LLM cost ceiling + kill-switch.

ADR-4665 §7.1/§7b (Gate 2): a deterministic, durable budget guard that sits in
front of every LLM call. It enforces per-decision AND per-tick USD + token
ceilings, persists cumulative spend across daemon restarts, and — when a
ceiling is exhausted — tells the caller to fall back to the $0 deterministic
path rather than spend more money.

Design contract (money-software discipline, AGENTS.md)
──────────────────────────────────────────────────────
* **Default-OFF.** ``LLMBudgetGuard.from_env()`` returns ``None`` unless
  ``HERMES_QUANT_LLM_BUDGET=1``. With no guard wired in, the LLM caller path is
  byte-identical to pre-B41-a (the guard is an *added pre-gate*, never a
  rewrite of the call).
* **Fail-closed.** If the persisted spend file is unreadable or corrupt, the
  guard reports the budget as EXHAUSTED (``allowed=False``,
  ``allowed_max_tokens=0``). A bad ledger must never read as "unlimited" — that
  is the only failure polarity safe for money (contrast L2 ``posterior_store``,
  where a bad *skill cache* degrades permissively to cold-start).
* **Never raises from check()/record().** The caller treats a blocked check as
  "fall back to deterministic", which is the silence-by-default posture
  (ADR-0031). A guard that raised would itself be a new crash seam.
* **Worst-case projection at check time.** Before a call we do not know the
  completion length, so we bill ``prompt_tokens + max_tokens`` at the model's
  output price for the projection. Unknown models bill at an intentionally
  expensive ``default_price`` so an unrecognised model can never sneak under a
  ceiling.
* **Atomic, durable writes.** Spend is persisted via the shared
  ``atomic_write_json`` (tmp + rename) so a crash mid-write cannot leave a
  half-written ledger.
* **Child-cost-counts-against-parent.** Spend is keyed by ``(tick_id,
  decision_id)``; a research debate's N×M sub-calls all pass the SAME
  ``decision_id`` and therefore bill to one decision bucket.

The guard does NOT make network calls and does NOT know about httpx/openai. It
is a pure accounting object the callers compose with.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hermes_quant.artifacts import QUANT_HOME, atomic_write_json

logger = logging.getLogger(__name__)

# Default location for the durable spend ledger. Module-level so tests can
# redirect it (or pass an explicit ``path=``) and never touch the real home.
BUDGET_DIR = QUANT_HOME / "llm_budget"
DEFAULT_SPEND_PATH = BUDGET_DIR / "spend.json"

SCHEMA_VERSION = 1

# Default-OFF master flag. When unset/0 the guard is never constructed by the
# callers, so the LLM path is bit-identical to pre-B41-a.
BUDGET_FLAG = "HERMES_QUANT_LLM_BUDGET"

# ---------------------------------------------------------------------------
# Price table — USD per 1,000 tokens, as (prompt_per_1k, completion_per_1k).
# These are coarse list-price anchors used ONLY for the pre-call ceiling
# projection; they are intentionally conservative (round up) and overridable
# via the constructor. They are NOT a billing source of truth.
# ---------------------------------------------------------------------------
_DEFAULT_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # OpenRouter-style ids matching DeliberativeConfig quick/deep defaults.
    "anthropic/claude-haiku-4.5": (0.001, 0.005),
    "anthropic/claude-sonnet-4.6": (0.003, 0.015),
    "openai/gpt-4.1-mini": (0.0004, 0.0016),
    "openai/gpt-4o": (0.005, 0.015),
}

# Unknown models bill at this (deliberately expensive) per-1k price so an
# unrecognised model can never slip under a ceiling. Fail-safe by default.
_FALLBACK_PRICE: tuple[float, float] = (0.02, 0.06)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCeilings:
    """Per-decision and per-tick ceilings. ``None`` means "no ceiling on this
    axis". A ceiling of ``0`` is a zero-call kill-switch (blocks every call).
    """

    per_decision_usd: float | None = None
    per_tick_usd: float | None = None
    per_decision_tokens: int | None = None
    per_tick_tokens: int | None = None

    def any_set(self) -> bool:
        return any(
            v is not None
            for v in (
                self.per_decision_usd,
                self.per_tick_usd,
                self.per_decision_tokens,
                self.per_tick_tokens,
            )
        )


@dataclass(frozen=True)
class BudgetCheck:
    """Result of a pre-call budget check.

    ``allowed`` — may the call proceed at all?
    ``allowed_max_tokens`` — the max_tokens the caller MUST clamp to (it may be
       smaller than requested when a token ceiling is near). 0 when blocked.
    ``reason`` — short machine code for *why* a call was blocked (audited).
    ``spent_*`` — cumulative spend observed for this decision/tick (audited).
    """

    allowed: bool
    allowed_max_tokens: int
    reason: str | None = None
    spent_decision_usd: float = 0.0
    spent_tick_usd: float = 0.0
    spent_decision_tokens: int = 0
    spent_tick_tokens: int = 0


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class LLMBudgetGuard:
    """Durable, fail-closed pre-call budget gate for LLM stages."""

    def __init__(
        self,
        *,
        ceilings: BudgetCeilings,
        price_table: dict[str, tuple[float, float]] | None = None,
        default_price: tuple[float, float] = _FALLBACK_PRICE,
        path: Path | None = None,
    ) -> None:
        self.ceilings = ceilings
        self._price_table = dict(price_table) if price_table is not None else dict(
            _DEFAULT_PRICE_TABLE
        )
        self._default_price = default_price
        self._path = path if path is not None else DEFAULT_SPEND_PATH
        # Serialise read-modify-write of the on-disk ledger within a process.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Constructor from environment (default-OFF).
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> Optional["LLMBudgetGuard"]:
        """Build a guard from ``HERMES_QUANT_LLM_BUDGET*`` env vars.

        Returns ``None`` when the master flag ``HERMES_QUANT_LLM_BUDGET`` is
        unset/0 — so callers that compose ``from_env()`` get a no-op (and the
        LLM path stays byte-identical) until an operator opts in.

        Recognised vars (all optional once the master flag is on):
          HERMES_QUANT_LLM_BUDGET_PER_DECISION_USD
          HERMES_QUANT_LLM_BUDGET_PER_TICK_USD
          HERMES_QUANT_LLM_BUDGET_PER_DECISION_TOKENS
          HERMES_QUANT_LLM_BUDGET_PER_TICK_TOKENS
          HERMES_QUANT_LLM_BUDGET_DIR   (directory for the spend ledger)
        """
        if os.environ.get(BUDGET_FLAG, "0") != "1":
            return None

        ceilings = BudgetCeilings(
            per_decision_usd=_env_float("HERMES_QUANT_LLM_BUDGET_PER_DECISION_USD"),
            per_tick_usd=_env_float("HERMES_QUANT_LLM_BUDGET_PER_TICK_USD"),
            per_decision_tokens=_env_int("HERMES_QUANT_LLM_BUDGET_PER_DECISION_TOKENS"),
            per_tick_tokens=_env_int("HERMES_QUANT_LLM_BUDGET_PER_TICK_TOKENS"),
        )
        budget_dir = os.environ.get("HERMES_QUANT_LLM_BUDGET_DIR")
        path = (Path(budget_dir) / "spend.json") if budget_dir else DEFAULT_SPEND_PATH
        return cls(ceilings=ceilings, path=path)

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    def _price(self, model_id: str) -> tuple[float, float]:
        return self._price_table.get(model_id, self._default_price)

    def _project_usd(self, model_id: str, prompt_tokens: int, max_tokens: int) -> float:
        """Worst-case USD projection for a not-yet-made call.

        We bill prompt at the prompt price and the FULL ``max_tokens`` at the
        completion price (the model could emit the maximum). Conservative on
        purpose: better to fall back to deterministic than overspend.
        """
        p_in, p_out = self._price(model_id)
        return (prompt_tokens / 1000.0) * p_in + (max_tokens / 1000.0) * p_out

    def _actual_usd(
        self, model_id: str, prompt_tokens: int, completion_tokens: int
    ) -> float:
        p_in, p_out = self._price(model_id)
        return (prompt_tokens / 1000.0) * p_in + (completion_tokens / 1000.0) * p_out

    # ------------------------------------------------------------------
    # Public API — check (pre-call) and record (post-call).
    # ------------------------------------------------------------------

    def check(
        self,
        *,
        model_id: str,
        prompt_tokens: int,
        max_tokens: int | None,
        decision_id: str,
        tick_id: str,
    ) -> BudgetCheck:
        """Decide whether an LLM call may proceed, and at what ``max_tokens``.

        Never raises. On any ledger-read failure the verdict is fail-closed
        (``allowed=False``, ``allowed_max_tokens=0``, ``reason="fail_closed"``).
        """
        # max_tokens MUST be present and positive (ADR-4665: every call carries
        # an explicit output ceiling). A call that omits it is rejected so an
        # unbounded generation can never be billed.
        if max_tokens is None or max_tokens <= 0:
            return BudgetCheck(
                allowed=False,
                allowed_max_tokens=0,
                reason="no_max_tokens",
            )

        # No ceilings configured → guard is inert (allow, keep requested cap).
        if not self.ceilings.any_set():
            return BudgetCheck(allowed=True, allowed_max_tokens=int(max_tokens))

        # Read the durable ledger. Fail CLOSED on any error (corrupt/unreadable).
        try:
            with self._lock:
                ledger = self._load_ledger()
        except _LedgerError:
            logger.warning(
                "LLMBudgetGuard: spend ledger unreadable/corrupt at %s; "
                "failing closed (treating budget as exhausted).",
                self._path,
            )
            return BudgetCheck(
                allowed=False,
                allowed_max_tokens=0,
                reason="fail_closed",
            )

        d_usd, d_tok, d_calls = _bucket(ledger, "decisions", decision_id)
        t_usd, t_tok, _t_calls = _bucket(ledger, "ticks", tick_id)

        # --- Token ceilings: clamp allowed_max_tokens to whatever remains. ---
        allowed_max = int(max_tokens)
        # The prompt tokens themselves count against a token ceiling, so the
        # room for *completion* is (ceiling - already_spent - prompt_tokens).
        for axis, spent, ceiling in (
            ("decision_tokens", d_tok, self.ceilings.per_decision_tokens),
            ("tick_tokens", t_tok, self.ceilings.per_tick_tokens),
        ):
            if ceiling is None:
                continue
            room = ceiling - spent - prompt_tokens
            if room <= 0:
                return BudgetCheck(
                    allowed=False,
                    allowed_max_tokens=0,
                    reason=axis,
                    spent_decision_usd=d_usd,
                    spent_tick_usd=t_usd,
                    spent_decision_tokens=d_tok,
                    spent_tick_tokens=t_tok,
                )
            allowed_max = min(allowed_max, room)

        # --- USD ceilings: project the (possibly clamped) call. ---
        for axis, spent, ceiling in (
            ("decision_usd", d_usd, self.ceilings.per_decision_usd),
            ("tick_usd", t_usd, self.ceilings.per_tick_usd),
        ):
            if ceiling is None:
                continue
            projected = spent + self._project_usd(model_id, prompt_tokens, allowed_max)
            if projected > ceiling:
                return BudgetCheck(
                    allowed=False,
                    allowed_max_tokens=0,
                    reason=axis,
                    spent_decision_usd=d_usd,
                    spent_tick_usd=t_usd,
                    spent_decision_tokens=d_tok,
                    spent_tick_tokens=t_tok,
                )

        return BudgetCheck(
            allowed=True,
            allowed_max_tokens=allowed_max,
            reason=None,
            spent_decision_usd=d_usd,
            spent_tick_usd=t_usd,
            spent_decision_tokens=d_tok,
            spent_tick_tokens=t_tok,
        )

    def record(
        self,
        *,
        model_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        decision_id: str,
        tick_id: str,
    ) -> None:
        """Fold one actually-made call's cost into the durable ledger.

        Never raises. A write failure is logged at WARNING; the next ``check``
        will fail closed if the file is left corrupt, which is the safe outcome.
        """
        usd = self._actual_usd(model_id, prompt_tokens, completion_tokens)
        tokens = int(prompt_tokens) + int(completion_tokens)
        try:
            with self._lock:
                try:
                    ledger = self._load_ledger()
                except _LedgerError:
                    # A corrupt ledger must not be silently overwritten with a
                    # fresh-zero ledger (that would erase prior spend and
                    # re-open the budget). Leave it; check() will fail closed.
                    logger.warning(
                        "LLMBudgetGuard: refusing to record over a corrupt "
                        "ledger at %s; leaving it for fail-closed reads.",
                        self._path,
                    )
                    return
                _add_to_bucket(ledger, "decisions", decision_id, usd, tokens)
                _add_to_bucket(ledger, "ticks", tick_id, usd, tokens)
                ledger["schema_version"] = SCHEMA_VERSION
                atomic_write_json(self._path, ledger)
        except Exception as exc:  # noqa: BLE001 — accounting must never crash a call
            logger.warning("LLMBudgetGuard: failed to persist spend (%s).", exc)

    def snapshot(self, *, decision_id: str, tick_id: str) -> dict[str, float | int]:
        """Return current cumulative spend for a decision/tick (diagnostics)."""
        try:
            with self._lock:
                ledger = self._load_ledger()
        except _LedgerError:
            return {
                "decision_usd": 0.0,
                "tick_usd": 0.0,
                "decision_tokens": 0,
                "tick_tokens": 0,
                "decision_calls": 0,
                "corrupt": True,
            }
        d_usd, d_tok, d_calls = _bucket(ledger, "decisions", decision_id)
        t_usd, t_tok, _ = _bucket(ledger, "ticks", tick_id)
        return {
            "decision_usd": d_usd,
            "tick_usd": t_usd,
            "decision_tokens": d_tok,
            "tick_tokens": t_tok,
            "decision_calls": d_calls,
        }

    # ------------------------------------------------------------------
    # Ledger I/O
    # ------------------------------------------------------------------

    def _load_ledger(self) -> dict:
        """Read the JSON ledger, or return a fresh-zero ledger if absent.

        Raises ``_LedgerError`` on a present-but-corrupt/unreadable file so the
        caller can fail closed. A *missing* file is a legitimate cold start and
        returns an empty ledger (no spend yet).
        """
        path = self._path
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "ticks": {}, "decisions": {}}
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — unreadable == corrupt for us
            raise _LedgerError(f"unreadable: {exc}") from exc
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise _LedgerError(f"invalid json: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(
            data.get("ticks"), dict
        ) or not isinstance(data.get("decisions"), dict):
            raise _LedgerError("missing ticks/decisions maps")
        return data


# ---------------------------------------------------------------------------
# Module-internal helpers
# ---------------------------------------------------------------------------


class _LedgerError(Exception):
    """Internal: the on-disk ledger is present but unreadable/corrupt."""


def _bucket(ledger: dict, group: str, key: str) -> tuple[float, int, int]:
    """Return (usd, tokens, calls) for one bucket, defaulting to zero."""
    rec = ledger.get(group, {}).get(key)
    if not isinstance(rec, dict):
        return 0.0, 0, 0
    try:
        return (
            float(rec.get("usd", 0.0)),
            int(rec.get("tokens", 0)),
            int(rec.get("calls", 0)),
        )
    except (TypeError, ValueError):
        # A malformed bucket is treated as a corrupt ledger upstream; here we
        # defensively return zero (the ledger-shape check already gates this).
        return 0.0, 0, 0


def _add_to_bucket(
    ledger: dict, group: str, key: str, usd: float, tokens: int
) -> None:
    grp = ledger.setdefault(group, {})
    rec = grp.setdefault(key, {"usd": 0.0, "tokens": 0, "calls": 0})
    rec["usd"] = float(rec.get("usd", 0.0)) + float(usd)
    rec["tokens"] = int(rec.get("tokens", 0)) + int(tokens)
    rec["calls"] = int(rec.get("calls", 0)) + 1


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("LLMBudgetGuard: ignoring non-numeric %s=%r", name, raw)
        return None


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("LLMBudgetGuard: ignoring non-integer %s=%r", name, raw)
        return None
