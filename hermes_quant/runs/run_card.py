"""# Ported from Vibe-Trading (MIT). Copyright (c) 2026 Vibe-Trading Contributors.
# See LICENSES/MIT-Vibe-Trading.txt for full text.

Run card generation for hermes-quant research and backtest runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "0.2"
RUN_SUMMARY_KEYS = (
    "asset",
    "asset_class",
    "symbol",
    "symbols",
    "start_date",
    "end_date",
    "interval",
    "timeframe",
    "engine",
    "initial_cash",
    "source",
    "mode",
)


def _hierarchical_pooling_enabled() -> bool:
    """True iff HERMES_QUANT_HIERARCHICAL_POOLING=1 (read at CALL TIME).

    d97e: gates the ag03 pooling warm-up block in the run card. When unset/!="1"
    the run card is byte-identical to the pre-d97e card — the block is omitted
    from JSON and Markdown even if a caller passes ``pooling_diagnostics``. This
    reuses the EXISTING flag that gates the BMA pooler (bma.py); it is the
    canonical first read for the flag inventory scan (bma.py:293 sorts first), so
    this read does NOT add a flag-inventory row.
    """
    return os.environ.get("HERMES_QUANT_HIERARCHICAL_POOLING", "0") == "1"


def default_quant_home() -> Path:
    """Return the hermes-quant cross-process state root."""
    return Path.home() / ".hermes" / "quant"


def run_dir_for(run_id: str, *, quant_home: Path | None = None) -> Path:
    """Return ``~/.hermes/quant/runs/<run_id>`` for a safe run id."""
    safe_run_id = _validate_run_id(run_id)
    root = Path(quant_home).expanduser() if quant_home is not None else default_quant_home()
    return root / "runs" / safe_run_id


def write_run_card(
    run_id: str,
    config: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    quant_home: Path | None = None,
    data_sources: Sequence[str] | None = None,
    strategy_path: Path | str | None = None,
    warnings: Sequence[str] | None = None,
    evidence_ids: Sequence[str] | None = None,
    flow_name: str | None = None,
    governance_audit_log_offset: int | None = None,
    pooling_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write JSON and Markdown run cards for a research or backtest run.

    Args:
        run_id: Identifier used under ``~/.hermes/quant/runs/<run_id>/``.
        config: Full run configuration. Only a summary and hash are stored.
        metrics: Run metrics. Scalar values are stored; ``validation`` is
            stored separately when present.
        quant_home: Optional override for tests; defaults to ``~/.hermes/quant``.
        data_sources: Data sources used by the run.
        strategy_path: Optional strategy source file to hash for reproducibility.
        warnings: Optional warnings to include in the card.
        evidence_ids: Evidence Store ids referenced by this run.
        flow_name: Trading Flow Contract name when this run is flow-bound.
        governance_audit_log_offset: Governance audit log offset for audit walkback.
        pooling_diagnostics: ag03 hierarchical-pooling status (effective-n per
            cell, warm-up flags, ``headline_in_warmup``) as produced by
            ``BMAAggregator.status()["hierarchical_pooling"]`` (d97e). Surfaced
            into the card so a cron-mode operator can SEE whether the headline
            weighting is still warming up. Included ONLY when the default-OFF
            HERMES_QUANT_HIERARCHICAL_POOLING flag is set; when the flag is off
            the card is byte-identical to today (the block is omitted even if a
            value is passed).

    Returns:
        The run card payload written to ``run_card.json``.
    """
    run_id = _validate_run_id(run_id)
    run_dir = run_dir_for(run_id, quant_home=quant_home)
    run_dir.mkdir(parents=True, exist_ok=True)

    config_file = run_dir / "config.json"
    reproducibility: dict[str, Any] = {
        "config_hash": _file_hash(config_file) if config_file.exists() else _json_hash(config),
    }
    if strategy_path is not None:
        strategy_file = Path(strategy_path).expanduser()
        if strategy_file.exists() and strategy_file.is_file():
            reproducibility["strategy_hash"] = _file_hash(strategy_file)

    card: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "flow_name": flow_name,
        "evidence_ids": list(evidence_ids or []),
        "governance_audit_log_offset": governance_audit_log_offset,
        "backtest": _run_summary(config),
        "reproducibility": reproducibility,
        "data_sources": list(data_sources or []),
        "metrics": _scalar_metrics(metrics),
        "warnings": list(warnings or []),
        "artifacts": _list_artifacts(run_dir),
    }
    if "validation" in metrics:
        card["validation"] = metrics["validation"]

    # d97e: surface the ag03 hierarchical-pooling warm-up band ONLY when the
    # default-OFF flag is set AND a caller supplied diagnostics. With the flag
    # off the card is byte-identical to the pre-d97e card (no key, no MD section).
    if pooling_diagnostics is not None and _hierarchical_pooling_enabled():
        card["hierarchical_pooling"] = _normalize_pooling(pooling_diagnostics)

    json_path = run_dir / "run_card.json"
    md_path = run_dir / "run_card.md"
    json_path.write_text(
        json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(card), encoding="utf-8")
    return card


def _validate_run_id(run_id: str) -> str:
    run_id = str(run_id).strip()
    if not run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a non-empty path segment")
    if Path(run_id).is_absolute() or "/" in run_id or "\\" in run_id:
        raise ValueError("run_id must not contain path separators")
    if any(part in {"", ".", ".."} for part in Path(run_id).parts):
        raise ValueError("run_id must not contain relative path traversal")
    return run_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {key: val for key, val in value.items() if not str(key).startswith("_")},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_summary(config: Mapping[str, Any]) -> dict[str, Any]:
    return {key: config.get(key) for key in RUN_SUMMARY_KEYS if key in config}


def _normalize_pooling(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy the pooling diagnostics into plain JSON-safe dicts.

    Mappings are converted to plain ``dict`` so the in-memory card equals the
    JSON-loaded card (the round-trip assert) regardless of the concrete Mapping
    type the caller passed.
    """

    def _coerce(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): _coerce(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_coerce(v) for v in value]
        return value

    return {str(k): _coerce(v) for k, v in diagnostics.items()}


def _scalar_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in metrics.items() if key != "validation" and _is_scalar(value)
    }


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _list_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    for relative in (
        Path("config.json"),
        Path("signals.jsonl"),
        Path("fills.jsonl"),
        Path("portfolio.json"),
    ):
        path = run_dir / relative
        if path.exists() and path.is_file():
            candidates.append(path)

    artifacts_dir = run_dir / "artifacts"
    if artifacts_dir.exists() and artifacts_dir.is_dir():
        candidates.extend(path for path in artifacts_dir.rglob("*") if path.is_file())

    artifacts = []
    for path in sorted(candidates, key=lambda item: item.relative_to(run_dir).as_posix()):
        artifacts.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _file_hash(path),
            }
        )
    return artifacts


def _render_markdown(card: Mapping[str, Any]) -> str:
    lines = [
        "# Backtest Run Card",
        "",
        f"Generated: {card['generated_at']}",
        f"Run ID: `{card['run_id']}`",
        f"Run directory: `{card['run_dir']}`",
        "",
        "## Governance",
        f"- flow_name: {card.get('flow_name') or 'None'}",
        f"- evidence_ids: {len(card.get('evidence_ids', []))}",
        f"- governance_audit_log_offset: {card.get('governance_audit_log_offset')}",
        "",
        "## Backtest Summary",
    ]

    backtest = card.get("backtest", {})
    if backtest:
        lines.extend(f"- {key}: {value}" for key, value in backtest.items())
    else:
        lines.append("- No backtest summary fields provided.")

    lines.extend(["", "## Reproducibility"])
    reproducibility = card.get("reproducibility", {})
    lines.append(f"- config_hash: `{reproducibility.get('config_hash', '')}`")
    if "strategy_hash" in reproducibility:
        lines.append(f"- strategy_hash: `{reproducibility['strategy_hash']}`")

    lines.extend(["", "## Data Sources"])
    data_sources = card.get("data_sources", [])
    if data_sources:
        lines.extend(f"- {source}" for source in data_sources)
    else:
        lines.append("- None recorded.")

    lines.extend(["", "## Metrics"])
    metric_values = card.get("metrics", {})
    if metric_values:
        lines.extend(f"- {key}: {value}" for key, value in metric_values.items())
    else:
        lines.append("- No scalar metrics recorded.")

    lines.extend(["", "## Validation"])
    if "validation" in card:
        validation = card["validation"]
        if isinstance(validation, Mapping):
            lines.extend(f"- {key}: {value}" for key, value in validation.items())
        else:
            lines.append(f"- {validation}")
    else:
        lines.append("- Not present.")

    if "hierarchical_pooling" in card:
        lines.extend(_render_pooling(card["hierarchical_pooling"]))

    warnings = card.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings"])
        lines.extend(f"- {warning}" for warning in warnings)

    lines.extend(["", "## Artifacts"])
    artifacts = card.get("artifacts", [])
    if artifacts:
        lines.extend(
            f"- `{artifact['path']}` ({artifact['size_bytes']} bytes, sha256 `{artifact['sha256']}`)"
            for artifact in artifacts
        )
    else:
        lines.append("- None found.")

    return "\n".join(lines) + "\n"


def _render_pooling(pooling: Mapping[str, Any]) -> list[str]:
    """Render the ag03 hierarchical-pooling warm-up band (d97e).

    The load-bearing operator signal is ``headline_in_warmup`` and the per-cell
    ``effective_n`` — so an operator can see at a glance that a near-1.0-looking
    cell is actually a thin warm-up cell and the headline weighting is not yet a
    confident track record.
    """
    lines: list[str] = ["", "## Hierarchical Pooling (ag03)"]
    lines.append(f"- headline_in_warmup: {pooling.get('headline_in_warmup')}")
    lines.append(f"- n_cells: {pooling.get('n_cells')}")
    lines.append(f"- n_warmup_cells: {pooling.get('n_warmup_cells')}")
    lines.append(f"- warmup_n_threshold: {pooling.get('warmup_n_threshold')}")
    cells = pooling.get("cells")
    if isinstance(cells, Mapping) and cells:
        lines.append("- cells:")
        for label, diag in cells.items():
            if isinstance(diag, Mapping):
                eff = diag.get("effective_n")
                warm = diag.get("warmup")
                pooled = diag.get("pooled_skill")
                lines.append(
                    f"  - `{label}`: effective_n={eff}, warmup={warm}, pooled_skill={pooled}"
                )
            else:
                lines.append(f"  - `{label}`: {diag}")
    return lines
