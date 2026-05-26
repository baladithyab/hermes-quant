"""hermes_quant.daemon.discovery — Entry-point loader for analysts/aggregators/providers.

Per ADR-0007: discoverable extensions via [project.entry-points.*] in pyproject.toml:

  [project.entry-points."hermes_quant.analysts"]
  classical_ta = "hermes_quant.analysts.classical_ta:ClassicalTAAnalyst"

  [project.entry-points."hermes_quant.aggregators"]
  bma = "hermes_quant.aggregators.bma:BMAAggregator"

  [project.entry-points."hermes_quant.data_providers"]
  yfinance = "hermes_quant.data.yfinance_provider:YFinanceProvider"

The daemon's bootstrap reads `hermes-quant.config.yaml::quant.<role>.enabled`
to choose which discovered classes to instantiate.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any, TypeVar

from hermes_quant.protocol import (
    Aggregator,
    Analyst,
    DataProvider,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


ANALYSTS_GROUP = "hermes_quant.analysts"
AGGREGATORS_GROUP = "hermes_quant.aggregators"
DATA_PROVIDERS_GROUP = "hermes_quant.data_providers"


def _load_entry_points(group: str) -> dict[str, type]:
    """Load entry points for a group, returning {name: class}.

    Failures during discovery (missing module, ImportError) are logged and
    the entry point is skipped — never crash the daemon at boot for one
    bad plugin.
    """
    out: dict[str, type] = {}
    try:
        eps = importlib.metadata.entry_points(group=group)
    except Exception as e:  # noqa: BLE001
        logger.warning("entry_points(%s) failed: %s", group, e)
        return out

    for ep in eps:
        try:
            cls = ep.load()
            out[ep.name] = cls
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to load entry point %s in %s: %s", ep.name, group, e)
            continue
    return out


def discover_analysts() -> dict[str, type]:
    """Load all registered analyst classes."""
    return _load_entry_points(ANALYSTS_GROUP)


def discover_aggregators() -> dict[str, type]:
    return _load_entry_points(AGGREGATORS_GROUP)


def discover_data_providers() -> dict[str, type]:
    return _load_entry_points(DATA_PROVIDERS_GROUP)


def instantiate_analysts(
    enabled_names: list[str] | None = None,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[Analyst]:
    """Instantiate registered analysts.

    Args:
        enabled_names: if provided, only instantiate analysts in this list.
            None = instantiate all discovered.
        overrides: per-name kwargs override map.

    Returns:
        List of instantiated analysts in deterministic name-sorted order.
    """
    discovered = discover_analysts()
    overrides = overrides or {}
    enabled = enabled_names if enabled_names is not None else sorted(discovered)
    out: list[Analyst] = []
    for name in enabled:
        cls = discovered.get(name)
        if cls is None:
            logger.warning("analyst %s not registered; skipping", name)
            continue
        kwargs = overrides.get(name, {})
        try:
            instance = cls(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to instantiate analyst %s: %s", name, e)
            continue
        # Light protocol check
        if not hasattr(instance, "analyze"):
            logger.warning("analyst %s missing analyze(); skipping", name)
            continue
        out.append(instance)
    return out


def instantiate_aggregator(name: str, **kwargs: Any) -> Aggregator | None:
    """Instantiate a single aggregator by registered name."""
    discovered = discover_aggregators()
    cls = discovered.get(name)
    if cls is None:
        logger.warning("aggregator %s not registered", name)
        return None
    try:
        return cls(**kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to instantiate aggregator %s: %s", name, e)
        return None


def instantiate_data_provider(name: str, **kwargs: Any) -> DataProvider | None:
    discovered = discover_data_providers()
    cls = discovered.get(name)
    if cls is None:
        logger.warning("data provider %s not registered", name)
        return None
    try:
        return cls(**kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to instantiate data provider %s: %s", name, e)
        return None
