"""hermes_quant.risk.target_weight — signed NAV-fraction target-weight resolver.

This module hosts the canonical resolver for the signed per-symbol portfolio
target weight (as a fraction of NAV) consumed by the portfolio-cap gate
(``clip_one_to_remaining_headroom``). It was hoisted out of the deployed ops
script ``ops/scripts/quant-daily-interim.py`` (review finding P2-3) so that
production AND tests import the IDENTICAL object — no ``spec_from_file_location``
file-load harness, no ``sys.executable`` execv-neutralizing dance. The original
2026-06-03 bug being fixed was a test/prod input divergence; importing prod code
via a file-load harness reintroduced a thinner version of that same risk, so the
durable fix is a real importable module.

The cap gate needs a signed per-symbol target weight as a fraction of NAV. The
actionable-builder populates ``kelly_fraction`` (signed, e.g. -0.20) and the
trader/risk fields, but it does NOT set a ``target_position_pct`` key. Reading
that missing key as 0.0 made the cap silence EVERY pick as ``zero_target`` — a
plumbing break that masqueraded as "cap full" (see INCIDENT-2026-06-02
follow-up; bug found 2026-06-03).

Resolution order (first usable wins):
  1. ``target_position_pct`` — explicit override if a caller set it.
  2. signed ``kelly_fraction`` × ``risk_silence_multiplier`` — the canonical
     path. kelly_fraction already carries direction and the quarter-Kelly
     magnitude; the risk-debate committee multiplier (≤1.0) shrinks it.
  3. sign(direction) × |trader_size_fraction| × risk mult — fallback when
     kelly_fraction is absent but the trader proposed a size.

Contract: returns ``(signed_weight, source_tag)``. ``source_tag`` is ``None``
when no size field was resolvable — i.e. ``(0.0, None)`` signals a PLUMBING
BREAK, NOT a benign zero. The caller MUST treat a ``None`` source as a loud
error, NEVER as a cap silence.
"""

from __future__ import annotations


def resolve_target_weight(actionable: dict) -> tuple[float, str | None]:
    """Resolve the signed portfolio target weight for an actionable.

    The cap gate (clip_one_to_remaining_headroom) needs a signed per-symbol
    target weight as a fraction of NAV. The actionable-builder populates
    `kelly_fraction` (signed, e.g. -0.20) and the trader/risk fields, but it
    does NOT set a `target_position_pct` key. Reading that missing key as 0.0
    made the cap silence EVERY pick as `zero_target` — a plumbing break that
    masqueraded as "cap full" (see INCIDENT-2026-06-02 follow-up; bug found
    2026-06-03).

    Resolution order (first usable wins):
      1. `target_position_pct` — explicit override if a caller set it.
      2. signed `kelly_fraction` × risk_silence_multiplier — the canonical
         path. kelly_fraction already carries direction and the quarter-Kelly
         magnitude; the risk-debate committee multiplier (≤1.0) shrinks it.
      3. sign(direction) × |trader_size_fraction| × risk mult — fallback when
         kelly_fraction is absent but the trader proposed a size.

    Returns (signed_weight, source_tag). source_tag is None when no size field
    was resolvable — the caller MUST treat that as a loud error, NOT silence.
    """
    v = actionable
    explicit = v.get("target_position_pct")
    if explicit is not None:
        try:
            return float(explicit), "explicit"
        except (TypeError, ValueError):
            pass

    mult = v.get("risk_silence_multiplier")
    try:
        mult = float(mult) if mult is not None else 1.0
    except (TypeError, ValueError):
        mult = 1.0
    # Defensive clamp: committee can only silence (≤1.0), never amplify.
    if mult < 0.0:
        mult = 0.0
    elif mult > 1.0:
        mult = 1.0

    kelly = v.get("kelly_fraction")
    if kelly is not None:
        try:
            kf = float(kelly)
            if kf != 0.0:
                return kf * mult, "kelly_x_riskmult"
        except (TypeError, ValueError):
            pass

    size = v.get("trader_size_fraction")
    direction = v.get("direction")
    if size is not None and direction is not None:
        try:
            mag = abs(float(size))
            sign = 1.0 if float(direction) > 0 else -1.0
            if mag != 0.0:
                return sign * mag * mult, "tradersize_x_riskmult"
        except (TypeError, ValueError):
            pass

    return 0.0, None
