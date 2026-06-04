"""hermes_quant.cli.admission_precision — `hermes quant admission-precision` (seed 2b63).

Exposes the ADR-0075 admission-precision gate (the pre-flip onboarding audit) as an
operator CLI verb. Runs :func:`hermes_quant.catalyst.onboarding_audit.audit_onboarding_preflip`
over an episodes file and reports pass/fail with a gate-style exit code:

    0   gate clears (admitted out-of-universe names beat the forward-return bar)
    1   gate fails  (bar not cleared — flag must NOT be flipped)
    2   error       (missing / unparseable episodes file)

READ-ONLY (ADR-0007): never flips HERMES_QUANT_CATALYST_ONBOARDING. The verb only
MEASURES whether the bar is cleared; the flip is a separate operator action.
"""

from __future__ import annotations

import argparse
import json
import sys

from hermes_quant.catalyst.onboarding_audit import audit_onboarding_preflip


def cmd_admission_precision(args: argparse.Namespace) -> int:
    """Implement `hermes quant admission-precision --episodes-file FILE`.

    Returns 0 if the gate passes, 1 if it fails, 2 on error.
    """
    episodes_file = getattr(args, "episodes_file", None)
    if not episodes_file:
        print("admission-precision: --episodes-file is required", file=sys.stderr)
        return 2

    # Override tau thresholds only when explicitly supplied; otherwise the audit
    # uses the live onboarding TAU_CONF / TAU_MAG (the gate the operator will flip).
    tau_conf = getattr(args, "tau_conf", None)
    tau_mag = getattr(args, "tau_mag", None)
    min_hit_rate = getattr(args, "min_hit_rate", 0.6)

    kwargs = {"min_hit_rate": min_hit_rate}
    if tau_conf is not None:
        kwargs["tau_conf"] = tau_conf
    if tau_mag is not None:
        kwargs["tau_mag"] = tau_mag

    try:
        audit = audit_onboarding_preflip(episodes_file, **kwargs)
    except FileNotFoundError as exc:
        print(f"admission-precision: {exc}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        print(f"admission-precision: failed to parse episodes: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        print(json.dumps(audit.to_dict(), indent=2, default=str))
    else:
        _pretty_print(audit)

    return 0 if audit.passed else 1


def _pretty_print(audit) -> None:
    r = audit.result
    marker = "PASS" if audit.passed else "FAIL"
    print(f"admission-precision — {marker}  (gates {audit.flag})")
    print(f"  episodes:  {r.n_episodes}")
    print(f"  admitted:  {r.n_admitted}")
    print(f"  scored:    {r.n_scored}  (admitted & directionally scorable)")
    print(f"  hits:      {r.hits}")
    print(f"  hit_rate:  {r.hit_rate:.2%}  (bar: {audit.min_hit_rate:.2%})")
    if r.misses:
        print(f"  misses:    {', '.join(r.misses)}")
    if r.rejected:
        print(f"  rejected:  {', '.join(r.rejected)}")
    print()
    print(f"  {audit.operator_note}")
