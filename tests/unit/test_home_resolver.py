"""Unit tests for hermes_quant.home — the single home resolver (ADR-0092 ph3).

Covers the precedence ladder and, critically, the BEHAVIORAL-PARITY contract:
with no threaded arg and neither env var, quant_home() is byte-identical to the
legacy import-bound ``Path.home() / ".hermes" / "quant"`` constant. The whole
de-coupling is a no-op in production and only observable under an override.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant.home import (
    DEFAULT_QUANT_DIRNAME,
    hermes_home,
    quant_home,
)


def _clear_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)


# ---------------------------------------------------------------------------
# Parity: default path is byte-identical to the legacy constant.
# ---------------------------------------------------------------------------


def test_quant_home_default_is_byte_identical_to_legacy_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No arg + no env -> EXACTLY Path.home()/.hermes/quant (the legacy form)."""
    _clear_home_env(monkeypatch)
    assert quant_home() == Path.home() / ".hermes" / "quant"


def test_hermes_home_default_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_home_env(monkeypatch)
    assert hermes_home() == Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Precedence ladder for quant_home().
# ---------------------------------------------------------------------------


def test_threaded_arg_wins_over_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit threaded arg beats BOTH env vars (precedence #1)."""
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path / "from_quant_env"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "from_hermes_env"))
    explicit = tmp_path / "explicit_root"
    assert quant_home(explicit) == explicit


def test_hermes_quant_home_env_points_directly_at_quant_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HERMES_QUANT_HOME is the quant root DIRECTLY (no /quant suffix)."""
    _clear_home_env(monkeypatch)
    root = tmp_path / "direct_quant_root"
    monkeypatch.setenv("HERMES_QUANT_HOME", str(root))
    assert quant_home() == root  # NOT root / "quant"


def test_hermes_quant_home_beats_hermes_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When both env vars set, the quant-specific one wins (precedence #2 > #3)."""
    quant_root = tmp_path / "q"
    monkeypatch.setenv("HERMES_QUANT_HOME", str(quant_root))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert quant_home() == quant_root


def test_hermes_home_env_appends_quant_dirname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HERMES_HOME points at the hermes home; quant root is $HERMES_HOME/quant."""
    _clear_home_env(monkeypatch)
    hhome = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hhome))
    assert quant_home() == hhome / DEFAULT_QUANT_DIRNAME
    assert hermes_home() == hhome


def test_tilde_in_env_is_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_home_env(monkeypatch)
    monkeypatch.setenv("HERMES_QUANT_HOME", "~/somewhere/quant")
    resolved = quant_home()
    assert "~" not in str(resolved)
    assert resolved == (Path.home() / "somewhere" / "quant")


def test_resolution_is_call_time_not_import_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of ph3: setting the env AFTER import still takes effect.

    This is the property the legacy import-bound QUANT_HOME constant lacked —
    the bug the operator reproduced (tick ignored HERMES_HOME because the
    constant was already bound at import).
    """
    _clear_home_env(monkeypatch)
    first = quant_home()
    assert first == Path.home() / ".hermes" / "quant"
    # Now set an override that did NOT exist at import time:
    later = tmp_path / "set_after_import"
    monkeypatch.setenv("HERMES_QUANT_HOME", str(later))
    assert quant_home() == later  # honored — because resolution is call-time
