"""Default-off bridge for probing the extracted ``aegis.contracts`` package.

This module is diagnostic only. It does not replace ``hermes_quant.pdr_core``
contracts in live paths, and it does not import AEGIS unless explicitly enabled
by YAML config or compatibility env override.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import ModuleType
from typing import Any

from hermes_quant.pdr_core import contracts as hermes_contracts

ENABLE_ENV = "HERMES_QUANT_AEGIS_CONTRACTS_SHADOW"
SRC_ENV = "HERMES_QUANT_AEGIS_SRC"
CONFIG_ENV = "HERMES_QUANT_AEGIS_CONFIG"


@dataclass(frozen=True)
class AegisContractBridgeConfig:
    """Resolved bridge config.

    YAML is the primary surface. ``ENABLE_ENV`` and ``SRC_ENV`` are compatibility
    overrides for operators who already use env-based wiring.
    """

    contracts_shadow: bool = False
    source_path: Path | None = None


@dataclass(frozen=True)
class AegisContractBridgeStatus:
    """Result of a default-off AEGIS contract bridge probe."""

    enabled: bool
    loaded: bool
    reason: str
    module: str | None = None
    source: str | None = None
    errors: tuple[str, ...] = ()


def _coerce_bool(value: object, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError(f"{label} must be boolean, got {value!r}")


def _optional_path(value: object, *, label: str) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string path, got {type(value).__name__}")
    return Path(value).expanduser()


def _load_yaml_config(path: str | Path | None) -> AegisContractBridgeConfig:
    if path is None:
        return AegisContractBridgeConfig()
    try:
        import yaml  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - diagnostic config failure
        raise ValueError(f"PyYAML unavailable while reading {path}: {exc}") from exc
    config_path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read config {config_path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{config_path} must contain a YAML mapping")
    bridge = raw.get("hermes_bridge", {})
    if bridge is None:
        bridge = {}
    if not isinstance(bridge, Mapping):
        raise ValueError("hermes_bridge must be a YAML mapping")
    return AegisContractBridgeConfig(
        contracts_shadow=_coerce_bool(
            bridge.get("contracts_shadow", False),
            label="hermes_bridge.contracts_shadow",
        ),
        source_path=_optional_path(bridge.get("source_path"), label="hermes_bridge.source_path"),
    )


def _resolve_config(
    env: Mapping[str, str],
    config_path: str | Path | None,
) -> AegisContractBridgeConfig:
    resolved = _load_yaml_config(config_path or env.get(CONFIG_ENV))
    enabled = resolved.contracts_shadow
    source_path = resolved.source_path
    if ENABLE_ENV in env:
        enabled = _coerce_bool(env[ENABLE_ENV], label=ENABLE_ENV)
    if SRC_ENV in env:
        source_path = _optional_path(env[SRC_ENV], label=SRC_ENV)
    return AegisContractBridgeConfig(contracts_shadow=enabled, source_path=source_path)


def _ensure_src_path(source_path: Path | None) -> None:
    if source_path is None:
        return
    if source_path.is_dir():
        text = str(source_path)
        if text not in sys.path:
            sys.path.insert(0, text)


def _field_names(cls: type[Any]) -> tuple[str, ...]:
    return tuple(field.name for field in fields(cls))


def _compare_contracts(aegis_contracts: ModuleType) -> tuple[str, ...]:
    errors: list[str] = []
    for name in ("AnalystView", "Proposal", "Fill"):
        hermes_cls = getattr(hermes_contracts, name)
        aegis_cls = getattr(aegis_contracts, name, None)
        if aegis_cls is None:
            errors.append(f"aegis.contracts missing {name}")
            continue
        if _field_names(aegis_cls) != _field_names(hermes_cls):
            errors.append(
                f"{name} fields differ: aegis={_field_names(aegis_cls)!r} "
                f"hermes={_field_names(hermes_cls)!r}"
            )
    for name in ("POSITION_LADDER", "OPTION_ASSET_CLASSES", "FILL_SCHEMA_VERSION"):
        if getattr(aegis_contracts, name, None) != getattr(hermes_contracts, name):
            errors.append(
                f"{name} differs: aegis={getattr(aegis_contracts, name, None)!r} "
                f"hermes={getattr(hermes_contracts, name)!r}"
            )
    return tuple(errors)


def probe_aegis_contracts(
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> AegisContractBridgeStatus:
    """Probe whether Hermes can import and parity-check ``aegis.contracts``.

    The probe is disabled by default. YAML config is primary:

    ``hermes_bridge.contracts_shadow: true``
    ``hermes_bridge.source_path: /path/to/aegis/src``

    Existing env vars remain as overrides for compatibility.
    """

    actual_env = os.environ if env is None else env
    try:
        bridge_config = _resolve_config(actual_env, config_path)
    except ValueError as exc:
        return AegisContractBridgeStatus(
            enabled=False,
            loaded=False,
            reason="aegis bridge config invalid",
            errors=(str(exc),),
        )
    if not bridge_config.contracts_shadow:
        return AegisContractBridgeStatus(
            enabled=False,
            loaded=False,
            reason="aegis bridge disabled",
        )
    _ensure_src_path(bridge_config.source_path)
    try:
        aegis_contracts = importlib.import_module("aegis.contracts")
    except Exception as exc:  # noqa: BLE001 - diagnostic bridge must fail closed
        return AegisContractBridgeStatus(
            enabled=True,
            loaded=False,
            reason="aegis.contracts import failed",
            errors=(str(exc),),
        )
    errors = _compare_contracts(aegis_contracts)
    return AegisContractBridgeStatus(
        enabled=True,
        loaded=not errors,
        reason="ok" if not errors else "contract parity mismatch",
        module=aegis_contracts.__name__,
        source=getattr(aegis_contracts, "__file__", None),
        errors=errors,
    )
