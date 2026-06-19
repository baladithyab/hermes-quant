"""hermes_quant.home — the single source of truth for resolving the runtime home.

ADR-0092 Phase 3 (home/context de-coupling). Before this module the shell
resolved its ``~/.hermes/quant`` state root **three** mutually-inconsistent ways:

  - ~46-59 modules bound ``QUANT_HOME = Path.home() / ".hermes" / "quant"`` at
    IMPORT time (e.g. ``autonomous.py:55``, ``artifacts.py:26``). Because the
    constant is bound when the module is first imported, an env override set
    AFTER import (the normal cron / test-isolation case) is silently ignored —
    this is the bug the operator reproduced: the autonomous tick ignored a
    ``HERMES_HOME`` override because ``autonomous.QUANT_HOME`` was already bound.
  - ``daemon/tick_lock.py`` resolved lazily, honoring ``HERMES_QUANT_HOME``.
  - ``eval/clean_window.py`` resolved lazily, honoring ``HERMES_HOME``.

This module unifies all three into ONE call-time resolver so AEGIS (the
host-agnostic core, ADR-0092/0093) can run against ANY home — a test tmp dir, a
second shell, a standalone daemon — without process-global import-time coupling.

Resolution precedence (highest wins), evaluated AT CALL TIME, never at import:

  1. An explicit ``quant_home=`` argument threaded by the caller (the param the
     2026-06-19 operator decision chose — matches the existing ``run_card.py``
     ``quant_home=`` shape). A threaded path ALWAYS wins; it is how a test or a
     second shell pins its own home deterministically with no env dependency.
  2. ``HERMES_QUANT_HOME`` env var — the quant-shell-specific override
     (pre-existing; honored by ``tick_lock.py``). Points DIRECTLY at the quant
     state root (``…/quant``), NOT at the hermes home.
  3. ``HERMES_HOME`` env var — the upstream Hermes-agent home override (honored
     by ``clean_window.py`` and by ``hermes_constants.get_hermes_home``). Points
     at the HERMES home; the quant root is ``$HERMES_HOME/quant``.
  4. ``Path.home() / ".hermes" / "quant"`` — the production default, byte-
     identical to every legacy import-bound constant.

Why both env vars: ``HERMES_QUANT_HOME`` already existed and some live tooling
sets it; ``HERMES_HOME`` is the var the operator's smoke-test used (and the one
upstream + ``clean_window.py`` honor). Honoring both means this resolver is a
strict superset of all three prior behaviors — no caller loses its override.

Behavioral-parity contract (money software): with NO threaded arg and NEITHER
env var set, :func:`quant_home` returns EXACTLY ``Path.home()/".hermes"/"quant"``
— byte-identical to the legacy constants. The de-coupling is a no-op in
production; it only becomes observable under an explicit override.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["hermes_home", "quant_home", "DEFAULT_QUANT_DIRNAME"]

# The quant state-root directory name under the hermes home. Kept a constant so
# the ``$HERMES_HOME/quant`` composition has a single definition.
DEFAULT_QUANT_DIRNAME = "quant"


def hermes_home(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the Hermes runtime home (``~/.hermes`` by default), at call time.

    Precedence: explicit ``override`` arg > ``HERMES_HOME`` env > ``~/.hermes``.
    (``HERMES_QUANT_HOME`` is intentionally NOT consulted here — it points at the
    quant state root, not the hermes home; see :func:`quant_home`.)
    """
    if override is not None:
        return Path(override).expanduser()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def quant_home(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the hermes-quant cross-process state root, at call time.

    This is THE resolver every shell module should use (threaded as a
    ``quant_home=`` default) instead of binding ``Path.home() / ".hermes" /
    "quant"`` at import. See the module docstring for the full precedence and the
    parity contract.

    Args:
        override: An explicit home the caller threaded down (precedence #1). When
            ``None``, the env vars and then the production default decide.

    Returns:
        The absolute quant state root. Byte-identical to the legacy import-bound
        ``QUANT_HOME`` constant when no override/env is in play.
    """
    # (1) explicit threaded arg always wins — deterministic, env-independent.
    if override is not None:
        return Path(override).expanduser()
    # (2) quant-shell-specific override points DIRECTLY at the quant root.
    quant_env = os.environ.get("HERMES_QUANT_HOME")
    if quant_env:
        return Path(quant_env).expanduser()
    # (3) upstream HERMES_HOME points at the hermes home; quant root is /quant.
    hermes_env = os.environ.get("HERMES_HOME")
    if hermes_env:
        return Path(hermes_env).expanduser() / DEFAULT_QUANT_DIRNAME
    # (4) production default — byte-identical to every legacy constant.
    return Path.home() / ".hermes" / DEFAULT_QUANT_DIRNAME
