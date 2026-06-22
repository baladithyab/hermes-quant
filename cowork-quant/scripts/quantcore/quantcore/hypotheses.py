"""quantcore.hypotheses — append-only hypothesis registry + Brier-scored forecasts.

ADR-0048 posture, lean port: every research claim is PRE-REGISTERED as a
falsifiable hypothesis *before* evidence is gathered, so post-hoc
rationalisation is structurally impossible. ADR-0048 explicitly rejected
SQLite (Alt C) in favour of the append-only JSONL pattern — this module
keeps that decision.

One file: <quant-state>/hypotheses.jsonl. Every event is one line:
  {"event": <kind>, "ts": <iso-utc>, "seq": <int>, "prev_hash": <sha256-12>, ...payload}

Event kinds:
  create    — pre-registration of a falsifiable hypothesis (never mutated)
  forecast  — a probability forecast attached to a hypothesis
  resolve   — outcome of one forecast; carries brier = (p - outcome)^2
  status    — lifecycle transition: open -> supported | refuted | retired
  link      — associates a ledger proposal_id with a hypothesis

Integrity: same seq + sha256(prev_line)[:12] chain as quantcore.ledger
(small hash helpers reimplemented locally — they are private to the ledger).
Current state is MATERIALIZED by replaying events; the file is never edited
in place. Writes are atomic-append with fsync.

This is the SOTA-note "Brier-scored forecast ledger": each forecast states
p = P(the hypothesis' predicted outcome occurs) over a horizon, and on
resolution the Brier score (p - outcome)^2 is frozen into the chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from quantcore.ledger import parse_iso

HYPOTHESES_NAME = "hypotheses.jsonl"

HypothesisStatus = Literal["open", "supported", "refuted", "retired"]

#: Lifecycle: a hypothesis is resolved (supported/refuted) or shelved
#: (retired) exactly once; resolved hypotheses may only be retired.
#: No transition re-opens a hypothesis — the registry is a pre-commitment
#: device, not a scratchpad (ADR-0048).
_TRANSITIONS: dict[str, set[str]] = {
    "open": {"supported", "refuted", "retired"},
    "supported": {"retired"},
    "refuted": {"retired"},
    "retired": set(),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:12]


def _require_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must be tz-aware (UTC)")
    return v.astimezone(UTC)


def new_hypothesis_id() -> str:
    return uuid.uuid4().hex[:16]


def new_forecast_id() -> str:
    return uuid.uuid4().hex[:16]


class Forecast(BaseModel):
    """One probability forecast on a hypothesis.

    `p` is the stated probability that the statement's predicted outcome
    occurs within `horizon`. On resolution, brier = (p - outcome)^2 where
    outcome is 1.0 if the predicted outcome occurred, else 0.0.
    """

    forecast_id: str = Field(min_length=8)
    p: float = Field(ge=0.0, le=1.0, description="P(predicted outcome occurs)")
    horizon: str = Field(min_length=1, description="e.g. '5d', '2w'")
    made_at: datetime
    resolved: bool = False
    outcome: bool | None = None
    brier: float | None = None

    _utc = field_validator("made_at")(_require_utc)


class Hypothesis(BaseModel):
    """A pre-registered, falsifiable research claim (max 500 chars)."""

    hypothesis_id: str = Field(min_length=8)
    statement: str = Field(min_length=1, max_length=500)
    created_at: datetime
    status: HypothesisStatus = "open"
    linked_proposal_ids: list[str] = Field(default_factory=list)
    forecasts: list[Forecast] = Field(default_factory=list)

    _utc = field_validator("created_at")(_require_utc)

    @field_validator("statement")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("statement must be a non-blank falsifiable claim")
        return v


class HypothesisRegistry:
    """Append-only JSONL event store; state reconstructed by replay."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / HYPOTHESES_NAME

    # -- write ---------------------------------------------------------------

    def _last_line(self) -> str | None:
        if not self.path.exists():
            return None
        last = None
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        return last

    def _append(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        last = self._last_line()
        record = {
            "event": event,
            "ts": _now().isoformat(),
            "seq": (json.loads(last)["seq"] + 1) if last else 0,
            "prev_hash": _hash_line(last) if last else "genesis",
            **payload,
        }
        line = json.dumps(record, default=str, sort_keys=True)
        with open(self.path, "a", buffering=1) as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return record

    def create(
        self,
        statement: str,
        *,
        hypothesis_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Hypothesis:
        """Pre-register a falsifiable hypothesis. Refuses duplicate ids."""
        hyp = Hypothesis(
            hypothesis_id=hypothesis_id or new_hypothesis_id(),
            statement=statement,
            created_at=created_at or _now(),
            status="open",
        )
        if self.get(hyp.hypothesis_id) is not None:
            raise ValueError(f"hypothesis {hyp.hypothesis_id!r} already registered")
        self._append("create", {"hypothesis": hyp.model_dump(mode="json")})
        return hyp

    def forecast(
        self,
        hypothesis_id: str,
        p: float,
        horizon: str,
        *,
        forecast_id: str | None = None,
        made_at: datetime | None = None,
    ) -> Forecast:
        """Attach a probability forecast to an existing, non-retired hypothesis."""
        hyp = self._require(hypothesis_id)
        if hyp.status == "retired":
            raise ValueError(f"hypothesis {hypothesis_id!r} is retired; no new forecasts")
        fc = Forecast(
            forecast_id=forecast_id or new_forecast_id(),
            p=p,
            horizon=horizon,
            made_at=made_at or _now(),
        )
        if any(f.forecast_id == fc.forecast_id for f in hyp.forecasts):
            raise ValueError(f"forecast {fc.forecast_id!r} already exists on {hypothesis_id!r}")
        self._append(
            "forecast",
            {"hypothesis_id": hypothesis_id, "forecast": fc.model_dump(mode="json")},
        )
        return fc

    def resolve_forecast(self, hypothesis_id: str, forecast_id: str, outcome: bool) -> Forecast:
        """Resolve one forecast: brier = (p - outcome)^2, frozen into the chain.

        Idempotency: re-resolving an already-resolved forecast is REFUSED
        (raises ValueError) — a Brier score is never silently overwritten.
        """
        hyp = self._require(hypothesis_id)
        fc = next((f for f in hyp.forecasts if f.forecast_id == forecast_id), None)
        if fc is None:
            raise ValueError(f"no forecast {forecast_id!r} on hypothesis {hypothesis_id!r}")
        if fc.resolved:
            raise ValueError(f"forecast {forecast_id!r} already resolved; refusing double-resolve")
        brier = (fc.p - (1.0 if outcome else 0.0)) ** 2
        self._append(
            "resolve",
            {
                "hypothesis_id": hypothesis_id,
                "forecast_id": forecast_id,
                "outcome": bool(outcome),
                "brier": brier,
            },
        )
        return fc.model_copy(update={"resolved": True, "outcome": bool(outcome), "brier": brier})

    def set_status(self, hypothesis_id: str, status: str, note: str = "") -> Hypothesis:
        """Lifecycle transition; invalid transitions are refused (ValueError)."""
        hyp = self._require(hypothesis_id)
        allowed = _TRANSITIONS[hyp.status]
        if status not in allowed:
            raise ValueError(
                f"invalid status transition {hyp.status!r} -> {status!r} "
                f"(allowed: {sorted(allowed) or 'none — terminal'})"
            )
        self._append(
            "status",
            {"hypothesis_id": hypothesis_id, "from": hyp.status, "to": status, "note": note},
        )
        return hyp.model_copy(update={"status": status})

    def link_proposal(self, hypothesis_id: str, proposal_id: str) -> Hypothesis:
        """Associate a ledger proposal with a hypothesis (idempotent no-op event-wise)."""
        hyp = self._require(hypothesis_id)
        if proposal_id in hyp.linked_proposal_ids:
            return hyp  # already linked; no duplicate event
        self._append("link", {"hypothesis_id": hypothesis_id, "proposal_id": proposal_id})
        return hyp.model_copy(
            update={"linked_proposal_ids": [*hyp.linked_proposal_ids, proposal_id]}
        )

    # -- read ----------------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify_chain(self) -> tuple[bool, str]:
        """True if seq is contiguous and every prev_hash matches. O(n)."""
        prev_line: str | None = None
        if not self.path.exists():
            return True, "empty"
        with open(self.path) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("seq") != i:
                    return False, f"seq gap at line {i}: got {rec.get('seq')}"
                expected = _hash_line(prev_line) if prev_line else "genesis"
                if rec.get("prev_hash") != expected:
                    return False, f"hash mismatch at line {i}"
                prev_line = line
        return True, "ok"

    def hypotheses(self) -> list[Hypothesis]:
        """Materialize current state by replaying events, in creation order."""
        hyps: dict[str, Hypothesis] = {}
        for rec in self.events():
            kind = rec["event"]
            if kind == "create":
                h = dict(rec["hypothesis"])
                if isinstance(h.get("created_at"), str):
                    h["created_at"] = parse_iso(h["created_at"])
                hyp = Hypothesis(**h)
                hyps[hyp.hypothesis_id] = hyp
            elif kind == "forecast":
                hyp = hyps.get(rec["hypothesis_id"])
                if hyp is None:
                    continue  # defensive: orphan event (chain verify flags tamper)
                f = dict(rec["forecast"])
                if isinstance(f.get("made_at"), str):
                    f["made_at"] = parse_iso(f["made_at"])
                hyp.forecasts.append(Forecast(**f))
            elif kind == "resolve":
                hyp = hyps.get(rec["hypothesis_id"])
                if hyp is None:
                    continue
                for fc in hyp.forecasts:
                    if fc.forecast_id == rec["forecast_id"] and not fc.resolved:
                        fc.resolved = True
                        fc.outcome = bool(rec["outcome"])
                        fc.brier = float(rec["brier"])
                        break
            elif kind == "status":
                hyp = hyps.get(rec["hypothesis_id"])
                if hyp is not None:
                    hyp.status = rec["to"]
            elif kind == "link":
                hyp = hyps.get(rec["hypothesis_id"])
                if hyp is not None and rec["proposal_id"] not in hyp.linked_proposal_ids:
                    hyp.linked_proposal_ids.append(rec["proposal_id"])
        return list(hyps.values())

    def get(self, hypothesis_id: str) -> Hypothesis | None:
        for hyp in self.hypotheses():
            if hyp.hypothesis_id == hypothesis_id:
                return hyp
        return None

    def _require(self, hypothesis_id: str) -> Hypothesis:
        hyp = self.get(hypothesis_id)
        if hyp is None:
            raise ValueError(f"unknown hypothesis {hypothesis_id!r}")
        return hyp

    def open_hypotheses(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses() if h.status == "open"]

    def brier_summary(self) -> dict[str, Any]:
        """Mean Brier + count, per hypothesis and overall (resolved forecasts only).

        Hypotheses with no resolved forecasts are excluded from per_hypothesis;
        overall mean_brier is None when nothing has resolved yet.
        """
        per: dict[str, dict[str, Any]] = {}
        all_scores: list[float] = []
        for hyp in self.hypotheses():
            scores = [f.brier for f in hyp.forecasts if f.resolved and f.brier is not None]
            if scores:
                per[hyp.hypothesis_id] = {
                    "mean_brier": sum(scores) / len(scores),
                    "count": len(scores),
                }
                all_scores.extend(scores)
        return {
            "overall": {
                "mean_brier": (sum(all_scores) / len(all_scores)) if all_scores else None,
                "count": len(all_scores),
            },
            "per_hypothesis": per,
        }
