"""hermes_quant.memory.decisions_render — operator-readable markdown over the
decision log (ADR-0042 / Wave C G3).

PURE, READ-ONLY renderer that materializes the append-only ``decisions.jsonl``
into one markdown block per decision, folding each decision's resolution event
(if any) into the same block.

Discipline (mirrors ``journal/render.py``):
  * markdown is a function of the typed rows — never authored as raw strings
    elsewhere;
  * the renderer NEVER writes to the log (G3 read-only invariant);
  * the renderer NEVER raises on a missing optional key (uses ``.get`` with
    defaults) — an offline render must not crash on a partial row.

Direction-arrow idiom is shared with the settlement journal:
``{1:"↑", -1:"↓", 0:"→"}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_quant.memory.decisions import DecisionLog

DECISIONS_RENDER_HEADER = """\
# hermes-quant decision log

Read-only operator view of the append-only committee decision log
(`decisions.jsonl`, ADR-0042). Each decision appears once, annotated with its
resolution when one exists. Newest decisions first. This view is materialized
on demand and never mutates the log.

"""

_NO_DECISIONS = "_(no decisions recorded)_"

_ARROW = {1: "↑", -1: "↓", 0: "→"}


def _arrow(direction: Any) -> str:
    try:
        return _ARROW.get(int(direction), "?")
    except (TypeError, ValueError):
        return "?"


def render_decision_block(
    decision_row: dict[str, Any],
    resolution_row: dict[str, Any] | None = None,
) -> str:
    """Render ONE decision (+ optional resolution) to a markdown block.

    Heading:  ``## <TICKER> <arrow> <rating> [pending|resolved]``
    Meta:     decision_id, asof_decision, asset_class, direction, confidence,
              target_position_pct, state, and (when a resolution exists)
              reflection_id + asof_resolution.
    Narrative: thesis_summary; risk_debate_summary if present; evidence_ids as
               a comma list.

    Pure; never raises on missing optional keys (uses ``.get`` with defaults).
    """
    ticker = str(decision_row.get("ticker", "?")).upper()
    rating = str(decision_row.get("rating", "?"))
    direction = decision_row.get("direction", 0)
    state = "resolved" if resolution_row is not None else str(
        decision_row.get("state", "pending")
    )

    lines: list[str] = []
    lines.append(f"## {ticker} {_arrow(direction)} {rating} [{state}]")
    lines.append("")

    lines.append("<!-- META_BEGIN -->")
    lines.append(f"decision_id: {decision_row.get('decision_id', '')}")
    lines.append(f"asof_decision: {decision_row.get('asof_decision', '')}")
    lines.append(f"asset_class: {decision_row.get('asset_class', '')}")
    lines.append(f"direction: {direction}")
    conf = decision_row.get("confidence", 0.0)
    try:
        lines.append(f"confidence: {float(conf):.4f}")
    except (TypeError, ValueError):
        lines.append(f"confidence: {conf}")
    tpp = decision_row.get("target_position_pct", 0.0)
    try:
        lines.append(f"target_position_pct: {float(tpp):+.4f}")
    except (TypeError, ValueError):
        lines.append(f"target_position_pct: {tpp}")
    lines.append(f"state: {state}")
    if resolution_row is not None:
        lines.append(f"reflection_id: {resolution_row.get('reflection_id', '')}")
        lines.append(f"asof_resolution: {resolution_row.get('asof_resolution', '')}")
    lines.append("<!-- META_END -->")
    lines.append("")

    thesis = decision_row.get("thesis_summary")
    if thesis:
        lines.append(f"**Thesis**: {thesis}")
        lines.append("")

    risk_summary = decision_row.get("risk_debate_summary")
    if risk_summary:
        lines.append(f"**Risk debate**: {risk_summary}")
        lines.append("")

    evidence_ids = decision_row.get("thesis_evidence_ids") or []
    if evidence_ids:
        lines.append(f"**Evidence**: {', '.join(str(e) for e in evidence_ids)}")
        lines.append("")

    return "\n".join(lines).rstrip("\n")


def render_decisions_md(
    *,
    path: Path | None = None,
    log: DecisionLog | None = None,
    limit: int | None = None,
    state_filter: str | None = None,
) -> str:
    """Materialize the whole ``decisions.jsonl`` into markdown.

    Folds the event chain: each ``decision_id`` appears ONCE, annotated with
    its resolution if one exists. Ordered newest-first by ``asof_decision``.

    Args:
        path:  Path to a ``decisions.jsonl`` (constructs a ``DecisionLog``).
        log:   An existing ``DecisionLog`` (mutually exclusive test seam with
               ``path``; ``log`` wins if both supplied).
        limit: Most-recent N decisions to render.
        state_filter: ``"pending"`` | ``"resolved"`` | ``None`` (=all).

    Empty log → header + ``"_(no decisions recorded)_"``. Pure; read-only.
    """
    if log is None:
        log = DecisionLog(path) if path is not None else DecisionLog()

    # Replay the event chain into (decision_row, resolution_row | None) pairs.
    decisions: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    for row in log.read_all():
        kind = row.get("kind")
        if kind == "decision":
            decisions[row["decision_id"]] = row
        elif kind == "resolution":
            # Keep the latest resolution per decision_id.
            resolutions[row["decision_id"]] = row

    folded: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
        (dec_row, resolutions.get(dec_id))
        for dec_id, dec_row in decisions.items()
    ]

    # Apply state filter.
    if state_filter == "pending":
        folded = [(d, r) for d, r in folded if r is None]
    elif state_filter == "resolved":
        folded = [(d, r) for d, r in folded if r is not None]

    # Newest-first by asof_decision (string ISO-8601 sorts lexically in UTC).
    folded.sort(key=lambda dr: str(dr[0].get("asof_decision", "")), reverse=True)

    if limit is not None:
        folded = folded[:limit]

    if not folded:
        return DECISIONS_RENDER_HEADER + _NO_DECISIONS

    blocks = [render_decision_block(dec_row, res_row) for dec_row, res_row in folded]
    return DECISIONS_RENDER_HEADER + "\n\n".join(blocks)
