"""Default-off bridge for probing the extracted ``aegis.contracts`` package.

This module is diagnostic only. It does not replace ``hermes_quant.pdr_core``
contracts in live paths, and it does not import AEGIS unless explicitly enabled
with ``HERMES_QUANT_AEGIS_CONTRACTS_SHADOW=1``.
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


@dataclass(frozen=True)
class AegisContractBridgeStatus:
    """Result of a default-off AEGIS contract bridge probe."""

    enabled: bool
    loaded: bool
    reason: str
    module: str | None = None
    source: str | None = None
    errors: tuple[str, ...] = ()


def _flag_enabled(env: Mapping[str, str]) -> bool:
    return env.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _ensure_src_path(env: Mapping[str, str]) -> None:
    raw = env.get(SRC_ENV)
    if not raw:
        return
    src = Path(raw).expanduser()
    if src.is_dir():
        text = str(src)
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
) -> AegisContractBridgeStatus:
    """Probe whether Hermes can import and parity-check ``aegis.contracts``.

    The probe is disabled by default. When enabled, it optionally prepends
    ``HERMES_QUANT_AEGIS_SRC`` to ``sys.path`` so a local checkout can be tested
    without packaging or installing AEGIS.
    """

    actual_env = os.environ if env is None else env
    if not _flag_enabled(actual_env):
        return AegisContractBridgeStatus(
            enabled=False,
            loaded=False,
            reason=f"{ENABLE_ENV} not enabled",
        )
    _ensure_src_path(actual_env)
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
