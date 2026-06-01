"""Registry-open play loader (ADR-0082 Part A continuation).

Plays were a hardcoded, frozen, code-level registry (`profiles.PROFILES`). After
seed 0878 every derived surface (`scorers.score_all` / `scorers.PLAY_NAMES` / the
per-play wrappers / `direction_bias`) is computed FROM `PROFILES`, so `PROFILES`
is the single source of truth. This module adds the missing discovery seam: a
play can now be declared externally — either as a YAML ``PlayProfile`` under
``~/.hermes/quant/plays/*.yaml`` or via a ``hermes_quant.plays`` entry point —
and merged into `PROFILES` at registry-build time, mirroring the
analyst/aggregator discovery in ``daemon.discovery`` /
``recipes.instantiate_recipe_*``.

Rails (this module is money-software-adjacent — it widens the eligibility layer,
never the gate):

* **Default-OFF.** Discovery only runs when ``HERMES_QUANT_PLAYS_OPEN == "1"``.
  With the flag off (the default), :func:`build_play_registry` returns a registry
  that is the built-in 5 plays, byte-identical to ``profiles.PROFILES`` today —
  nothing is read from disk or entry points.
* **Fail-closed on bias.** A loaded play with no ``bias`` (or a non-string /
  unrecognized bias) is *incompatible* and is SKIPPED, not crashed and not
  silently defaulted to bullish. ``direction_bias`` already treats an unknown
  bias as incompatible, but we refuse to even install such a play so it can never
  reach a scorer. (The built-in dataclass default of ``bias="bullish"`` is only
  for the in-code profiles; an externally-declared play must say so explicitly.)
* **Silence-by-default.** A malformed YAML file, an unloadable entry point, a
  duplicate id, or a profile that collides with a built-in name is logged at
  WARNING and skipped — one bad external play never crashes registry build.
* **No new selection authority.** This only widens *which* plays a symbol may be
  eligible for. It does not size, optimize across plays, or touch the gate.

The merged registry flows through the PROFILES-derived ``score_all`` /
``PLAY_NAMES`` / ``score_play`` from seed 0878 with no further code edits: see
:func:`install_external_plays`, which mutates the live ``profiles.PROFILES`` and
refreshes ``scorers.PLAY_NAMES`` in place.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .direction_bias import _LONG_OK, _SHORT_OK
from .profiles import PROFILES, PlayProfile

logger = logging.getLogger(__name__)

QUANT_HOME = Path.home() / ".hermes" / "quant"
USER_PLAY_DIR = QUANT_HOME / "plays"
PLAYS_ENTRY_POINT_GROUP = "hermes_quant.plays"

# Default-OFF flag, fail-closed: discovery runs ONLY when this is exactly "1".
PLAYS_OPEN_ENV = "HERMES_QUANT_PLAYS_OPEN"

# A play's declared bias must be one of these. ``direction_bias`` admits
# {"bullish", "agnostic"} for LONG and {"bearish", "agnostic"} for SHORT, so the
# full recognized set is their union. Anything else is fail-closed (skip).
_VALID_BIASES: frozenset[str] = _LONG_OK | _SHORT_OK

# Play-name grammar mirrors recipes.PDRRecipe.validate's id grammar so external
# names are predictable and never collide with rule-tuple machinery.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

# The four rule-bearing mapping fields of a PlayProfile (everything except
# ``name`` and ``bias``). Stored as dicts whose values are rule tuples.
_RULE_FIELDS: tuple[str, ...] = (
    "hard_rules",
    "soft_rules",
    "eviction_rules",
    "regime_gates",
)


def plays_open_enabled() -> bool:
    """True iff external play discovery is enabled (default-OFF, fail-closed)."""
    return os.environ.get(PLAYS_OPEN_ENV, "0") == "1"


# --------------------------------------------------------------------------- #
# Mapping -> PlayProfile (the YAML / entry-point dict shape)
# --------------------------------------------------------------------------- #


def _tuplize_rule(value: Any) -> Any:
    """Recursively convert YAML lists into tuples so rule values hash-match the
    in-code profiles' tuple grammar (``("between", lo, hi)`` etc).

    YAML has no tuple type; rules round-trip through lists. We normalize lists to
    tuples (recursively, so nested ``["or", ["lt", 30], ["gt", 70]]`` works) and
    leave scalars untouched.
    """
    if isinstance(value, list):
        return tuple(_tuplize_rule(v) for v in value)
    return value


def _normalize_rule_map(raw: Any, *, field_name: str, play_name: str) -> dict:
    """Normalize one rule-bearing field (hard/soft/eviction/regime) to a dict of
    tuple-valued rules.

    ``regime_gates`` is a label->action string map (NOT tuple rules), so its
    values are passed through unchanged; the other three are field->rule-tuple
    maps and get list->tuple normalization on each value.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"play {play_name!r}: {field_name} must be a mapping, got {type(raw).__name__}"
        )
    if field_name == "regime_gates":
        # label -> action string; leave as-is (validated cheaply below).
        return dict(raw)
    return {str(k): _tuplize_rule(v) for k, v in raw.items()}


def play_profile_from_mapping(data: Mapping[str, Any]) -> PlayProfile:
    """Build a :class:`PlayProfile` from a YAML/JSON mapping.

    Fail-closed validation (raises ``ValueError`` on any violation; the caller
    converts a raise into a skip+log so one bad play never crashes the build):

    * ``name`` is required, must match the id grammar, and must not collide with
      a built-in play name.
    * ``bias`` is REQUIRED and must be one of {"bullish","bearish","agnostic"}.
      There is no silent default here — an external play with no bias is
      incompatible and refused (the dataclass default is only for in-code use).
    * Unknown top-level keys are rejected (typos fail early).
    * Rule maps are normalized (lists -> tuples) for hash/grammar parity.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"play definition must be a mapping, got {type(data).__name__}")

    allowed_keys = {"name", "bias", *_RULE_FIELDS}
    unknown = set(data) - allowed_keys
    if unknown:
        raise ValueError(f"play definition has unknown keys: {sorted(unknown)}")

    name = data.get("name")
    if not isinstance(name, str) or not name or not _NAME_RE.fullmatch(name):
        raise ValueError(f"play definition has invalid/missing name: {name!r}")

    # Bias is REQUIRED and explicit for external plays (fail-closed). Refuse a
    # play that omits it or declares an unrecognized one — never default-fill it.
    bias = data.get("bias")
    if not isinstance(bias, str) or bias not in _VALID_BIASES:
        raise ValueError(
            f"play {name!r}: bias is required and must be one of "
            f"{sorted(_VALID_BIASES)}; got {bias!r} (fail-closed)"
        )

    kwargs: dict[str, Any] = {"name": name, "bias": bias}
    for field_name in _RULE_FIELDS:
        kwargs[field_name] = _normalize_rule_map(
            data.get(field_name), field_name=field_name, play_name=name
        )

    profile = PlayProfile(**kwargs)  # type: ignore[arg-type]
    return profile


# --------------------------------------------------------------------------- #
# YAML discovery (~/.hermes/quant/plays/*.yaml)
# --------------------------------------------------------------------------- #


def _load_yaml(path: Path) -> Any:
    import yaml  # pyyaml is a project dep; lazy import keeps unit cost off import

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_user_plays(*, root: Path | None = None) -> dict[str, PlayProfile]:
    """Load user-editable plays from ``~/.hermes/quant/plays/*.yaml``.

    Silence-by-default: a file that fails to parse, fails validation (incl. a
    missing/invalid bias), uses a built-in play name, or duplicates an already-
    loaded id is logged at WARNING and SKIPPED — never raised. Returns the
    successfully-loaded external plays keyed by name, in filename-sorted order.
    """
    base = root or USER_PLAY_DIR
    if not base.exists():
        return {}
    out: dict[str, PlayProfile] = {}
    for path in sorted([*base.glob("*.yaml"), *base.glob("*.yml")]):
        try:
            raw = _load_yaml(path)
        except Exception as exc:  # noqa: BLE001 — bad YAML never crashes the build
            logger.warning("skipping malformed play YAML %s: %s", path, exc)
            continue
        if not isinstance(raw, Mapping):
            logger.warning("skipping play YAML %s: top level must be a mapping", path)
            continue
        try:
            profile = play_profile_from_mapping(raw)
        except Exception as exc:  # noqa: BLE001 — invalid play (e.g. no bias) is skipped
            logger.warning("skipping invalid play %s: %s", path, exc)
            continue
        if profile.name in PROFILES:
            logger.warning(
                "skipping play %s: name %r collides with a built-in play",
                path,
                profile.name,
            )
            continue
        if profile.name in out:
            logger.warning(
                "skipping play %s: duplicate external play id %r", path, profile.name
            )
            continue
        out[profile.name] = profile
    return out


# --------------------------------------------------------------------------- #
# Entry-point discovery ([project.entry-points."hermes_quant.plays"])
# --------------------------------------------------------------------------- #


def _coerce_entry_point_obj(obj: Any) -> PlayProfile:
    """Coerce a loaded entry-point object into a :class:`PlayProfile`.

    Accepts (in order): a ``PlayProfile`` instance; a zero-arg callable/factory
    returning one (or a mapping); or a mapping. Anything else raises ValueError
    (caller converts to a skip).
    """
    if isinstance(obj, PlayProfile):
        return obj
    if callable(obj) and not isinstance(obj, Mapping):
        produced = obj()
        if isinstance(produced, PlayProfile):
            return produced
        if isinstance(produced, Mapping):
            return play_profile_from_mapping(produced)
        raise ValueError(f"entry-point factory returned {type(produced).__name__}, not a play")
    if isinstance(obj, Mapping):
        return play_profile_from_mapping(obj)
    raise ValueError(f"entry-point object is {type(obj).__name__}, not a PlayProfile/mapping")


def discover_entry_point_plays() -> dict[str, PlayProfile]:
    """Load external plays registered under ``hermes_quant.plays``.

    Mirrors ``daemon.discovery._load_entry_points``: per-entry-point failures
    (import error, bad object, missing bias, name collision with a built-in) are
    logged at WARNING and skipped. Returns successfully-loaded plays by name in
    entry-point-name-sorted order.
    """
    out: dict[str, PlayProfile] = {}
    try:
        eps = importlib.metadata.entry_points(group=PLAYS_ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 — discovery itself must never crash
        logger.warning("entry_points(%s) failed: %s", PLAYS_ENTRY_POINT_GROUP, exc)
        return out
    for ep in sorted(eps, key=lambda e: e.name):
        try:
            obj = ep.load()
            profile = _coerce_entry_point_obj(obj)
        except Exception as exc:  # noqa: BLE001 — one bad plugin never crashes boot
            logger.warning(
                "failed to load play entry point %s in %s: %s",
                ep.name,
                PLAYS_ENTRY_POINT_GROUP,
                exc,
            )
            continue
        if profile.name in PROFILES:
            logger.warning(
                "skipping entry-point play %s: name %r collides with a built-in play",
                ep.name,
                profile.name,
            )
            continue
        if profile.name in out:
            logger.warning(
                "skipping entry-point play %s: duplicate external play id %r",
                ep.name,
                profile.name,
            )
            continue
        out[profile.name] = profile
    return out


# --------------------------------------------------------------------------- #
# Registry build + install
# --------------------------------------------------------------------------- #


def discover_external_plays(*, user_root: Path | None = None) -> dict[str, PlayProfile]:
    """Discover all external plays (YAML + entry points), default-OFF.

    With ``HERMES_QUANT_PLAYS_OPEN`` unset/!="1" this returns ``{}`` without
    touching disk or entry points (byte-identical off-state). When enabled, YAML
    plays are loaded first, then entry-point plays; an entry-point play whose
    name was already supplied by a YAML file is skipped (YAML wins, logged).
    """
    if not plays_open_enabled():
        return {}
    out: dict[str, PlayProfile] = dict(load_user_plays(root=user_root))
    for name, profile in discover_entry_point_plays().items():
        if name in out:
            logger.warning(
                "skipping entry-point play %r: already provided by a YAML file", name
            )
            continue
        out[name] = profile
    return out


def build_play_registry(*, user_root: Path | None = None) -> dict[str, PlayProfile]:
    """Return the merged play registry: built-in PROFILES + discovered externals.

    Default-OFF: with the flag off, the result is the built-in 5 plays in their
    original insertion order, value-equal to ``profiles.PROFILES`` today. When
    enabled, discovered external plays are appended (after the built-ins, in
    discovery order). Built-ins always take precedence; an external play can
    never shadow a built-in (enforced in discovery by name-collision skip).

    This is a *pure* builder — it does NOT mutate the live ``PROFILES``. Use
    :func:`install_external_plays` for the in-place merge that the
    PROFILES-derived scorers read.
    """
    merged: dict[str, PlayProfile] = dict(PROFILES)
    for name, profile in discover_external_plays(user_root=user_root).items():
        # Defensive: discovery already drops built-in collisions; keep the
        # built-in if one slips through.
        if name in merged:
            logger.warning("external play %r collides with built-in; keeping built-in", name)
            continue
        merged[name] = profile
    return merged


def install_external_plays(*, user_root: Path | None = None) -> tuple[str, ...]:
    """Merge discovered external plays into the live ``profiles.PROFILES`` and
    refresh ``scorers.PLAY_NAMES`` so the PROFILES-derived scorers pick them up.

    ``score_all`` / ``score_play`` iterate ``PROFILES`` at call time, so mutating
    it in place is enough for them. ``PLAY_NAMES`` is a module-level tuple
    captured at import, so we re-derive and re-bind it here (and on the
    watchlist_evolution re-export, which is the same object) to keep every
    PLAY_NAMES consumer consistent.

    Returns the tuple of NEWLY-installed external play names (empty when the flag
    is off — the byte-identical default path). Idempotent: a play already present
    is not re-installed.
    """
    newly: list[str] = []
    for name, profile in discover_external_plays(user_root=user_root).items():
        if name in PROFILES:
            continue
        PROFILES[name] = profile
        newly.append(name)

    # Re-derive PLAY_NAMES from the (possibly mutated) PROFILES and re-bind it on
    # both the scorers module and the watchlist_evolution re-export so they stay
    # the same value. Done unconditionally (cheap) so a no-op call still leaves
    # PLAY_NAMES exactly tuple(PROFILES.keys()).
    from . import scorers as _scorers
    from . import watchlist_evolution as _we

    refreshed = tuple(PROFILES.keys())
    _scorers.PLAY_NAMES = refreshed
    _we.PLAY_NAMES = refreshed

    return tuple(newly)


def example_user_play() -> dict[str, Any]:
    """Minimal YAML-serializable external-play template (for docs/operators)."""
    return {
        "name": "my_bearish_swing",
        "bias": "bearish",
        "hard_rules": {
            "quote_type": ["eq", "EQUITY"],
            "avg_dollar_volume_30d": ["ge", 1e7],
            "last_close": ["between", 10.0, 500.0],
        },
        "soft_rules": {
            "rsi_14": ["gt", 70.0],
        },
        "eviction_rules": {
            "non_equity": ["ne_field", "quote_type", "EQUITY"],
        },
        "regime_gates": {
            "bull": "deny",
            "bear": "allow",
            "volatile": "warn",
            "unknown": "allow",
        },
    }
