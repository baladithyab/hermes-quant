"""rt03 — the flag SoT scanner must capture EVERY HERMES_QUANT_* flag the code reads.

ra09 established the generated FLAG-INVENTORY.md as the single flag source-of-truth, but the
ra09 review (wskowi3jx) found scan() SILENTLY MISSES two classes of flag:

  1. No-inline-default reads: `os.environ.get("HERMES_QUANT_X") == "1"` (a default-OFF boolean
     toggle written without a literal default arg) and `os.environ.get(CONST) or ""`. The old
     regexes hard-required a `, <default>` group. This dropped MONEY-PATH flags:
     HERMES_QUANT_PORTFOLIO_CAPS (the gross/net/cash cap rail), HERMES_QUANT_DISSENT_CAP (the BMA
     decision gate), HERMES_QUANT_BROKER_BACKEND (the broker selector).
  2. Global-constant collision: scan() built ONE global consts dict keyed by the bare constant
     NAME across all files (last-write-wins), so generic names reused in multiple modules collided
     — ENV_FLAG = MONTHLY_META_RETRO (meta_retro.py) was overwritten by ENV_FLAG =
     ANALYSTS_USE_REGIME (regime_aware_confidence.py), and _FLAG = INSIDER_ENABLED (form4.py) by
     _FLAG = PIT_UNIVERSE. The colliding flags were silently DROPPED.

An operator must not read "N flags" as "every live toggle" when a cap rail is missing. These
tests assert the scanner now captures the previously-missed flags. They import scan() from the
ops script via spec_from_file_location (the script filename has hyphens).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_scanner():
    path = REPO / "ops" / "scripts" / "quant-flag-inventory.py"
    spec = importlib.util.spec_from_file_location("quant_flag_inventory", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_default_money_path_flags_are_captured():
    """PORTFOLIO_CAPS / DISSENT_CAP / BROKER_BACKEND are read WITHOUT an inline default
    (== "1" / `or ""`); the scanner must still capture them — they are money-path."""
    flags = _load_scanner().scan()
    for f in (
        "HERMES_QUANT_PORTFOLIO_CAPS",
        "HERMES_QUANT_DISSENT_CAP",
        "HERMES_QUANT_BROKER_BACKEND",
    ):
        assert f in flags, f"{f} is read in hermes_quant/ but MISSING from the flag SoT scan"


def test_constant_collision_does_not_drop_flags():
    """ENV_FLAG and _FLAG are reused across modules with different values; per-file resolution
    must keep BOTH colliding flags instead of last-write-wins dropping one."""
    flags = _load_scanner().scan()
    # ENV_FLAG collision: meta_retro.py vs regime_aware_confidence.py
    assert "HERMES_QUANT_MONTHLY_META_RETRO" in flags, "ENV_FLAG collision dropped MONTHLY_META_RETRO"
    assert "HERMES_QUANT_ANALYSTS_USE_REGIME" in flags, "ANALYSTS_USE_REGIME missing"
    # _FLAG collision: form4.py vs point_in_time.py
    assert "HERMES_QUANT_INSIDER_ENABLED" in flags, "_FLAG collision dropped INSIDER_ENABLED"


def test_analysts_use_regime_source_is_correct_file():
    """The collision also mis-attributed ANALYSTS_USE_REGIME's source to meta_retro.py.
    Per-file resolution must point it at its real read site (regime/)."""
    flags = _load_scanner().scan()
    _, loc = flags["HERMES_QUANT_ANALYSTS_USE_REGIME"]
    assert "regime" in loc, f"ANALYSTS_USE_REGIME source mis-attributed: {loc}"


def test_with_default_reads_keep_their_literal_default():
    """A flag read with a literal default anywhere must keep that default (not be overwritten
    by an empty no-default capture) — with-default wins over no-default."""
    flags = _load_scanner().scan()
    # DELTA_NORMALIZER is read as environ.get(..., "0") — must show '0', not empty.
    assert flags["HERMES_QUANT_DELTA_NORMALIZER"][0] == "0"
