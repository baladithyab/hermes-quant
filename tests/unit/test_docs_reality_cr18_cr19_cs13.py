"""tests/unit/test_docs_reality_cr18_cr19_cs13.py — docs-reality fixes (workstream docs-reality-2).

cr18: AGENTS.md:72 must NOT claim kronos.py defines KairosAnalyst (only KronosAnalyst exists).
cr19: AGENTS.md:96 comment for dsr.py must reflect implemented state (not 'v0.2 placeholder').
cs13: reflector.py module docstring + memory/__init__.py must say default ON (set =0 to opt out).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parent.parent.parent  # /mnt/e/CS/github/hermes-quant


# ---------------------------------------------------------------------------
# cr18: kronos.py must not define KairosAnalyst
# ---------------------------------------------------------------------------


def test_cr18_agents_md_no_kairosanalyst_claim():
    """AGENTS.md must not claim both KronosAnalyst and KairosAnalyst for kronos.py."""
    agents_md = (REPO / "AGENTS.md").read_text()
    # The stale claim is "both KronosAnalyst and KairosAnalyst"
    assert "KairosAnalyst" not in agents_md, (
        "AGENTS.md still claims kronos.py defines KairosAnalyst, "
        "but only KronosAnalyst exists in that file."
    )


def test_cr18_kronos_py_has_no_kairosanalyst():
    """kronos.py must not define a class named KairosAnalyst (verify the source truth)."""
    kronos_src = (REPO / "hermes_quant" / "analysts" / "kronos.py").read_text()
    assert "class KairosAnalyst" not in kronos_src, (
        "kronos.py unexpectedly defines KairosAnalyst — cr18 premise changed."
    )


# ---------------------------------------------------------------------------
# cr19: AGENTS.md dsr.py comment must not say 'placeholder'
# ---------------------------------------------------------------------------


def test_cr19_agents_md_dsr_not_placeholder():
    """AGENTS.md dsr.py comment must not say 'placeholder'."""
    agents_md = (REPO / "AGENTS.md").read_text()
    # Find the line referencing dsr.py
    matching_lines = [ln for ln in agents_md.splitlines() if "dsr.py" in ln]
    assert matching_lines, "AGENTS.md has no line referencing dsr.py"
    dsr_line = matching_lines[0]
    assert "placeholder" not in dsr_line.lower(), (
        f"AGENTS.md dsr.py comment still says 'placeholder': {dsr_line!r}"
    )


def test_cr19_dsr_is_implemented():
    """dsr.py must exist and define deflated_sharpe (not be a stub/placeholder)."""
    dsr_path = REPO / "hermes_quant" / "evaluation" / "dsr.py"
    assert dsr_path.exists(), "dsr.py not found at expected path"
    src = dsr_path.read_text()
    assert "def deflated_sharpe" in src, "dsr.py does not define deflated_sharpe"


# ---------------------------------------------------------------------------
# cs13: reflector.py docstring must say 'default ON (set =0 to opt out)'
# ---------------------------------------------------------------------------


def test_cs13_reflector_docstring_default_on():
    """reflector.py module docstring must reflect that HERMES_QUANT_REFLECTION defaults ON."""
    reflector_path = REPO / "hermes_quant" / "memory" / "reflector.py"
    src = reflector_path.read_text()
    # The old text says "Default OFF"
    assert "Default OFF" not in src, (
        "reflector.py still says 'Default OFF' — must say 'default ON (set =0 to opt out)'"
    )
    assert "default ON" in src or "Default ON" in src, (
        "reflector.py must say 'default ON (set =0 to opt out)'"
    )


def test_cs13_memory_init_docstring_default_on():
    """memory/__init__.py docstring must reflect that HERMES_QUANT_REFLECTION defaults ON."""
    init_path = REPO / "hermes_quant" / "memory" / "__init__.py"
    src = init_path.read_text()
    assert "default OFF" not in src, (
        "memory/__init__.py still says 'default OFF' for HERMES_QUANT_REFLECTION"
    )
    assert "default ON" in src or "Default ON" in src, (
        "memory/__init__.py must say 'default ON (set =0 to opt out)'"
    )


# ---------------------------------------------------------------------------
# Verify call-site truth: all actual os.environ.get calls use "1" as default
# ---------------------------------------------------------------------------


def test_cs13_callsite_default_is_1_paper_py():
    """paper.py must use os.environ.get('HERMES_QUANT_REFLECTION', '1') (default ON)."""
    src = (REPO / "hermes_quant" / "react" / "paper.py").read_text()
    assert 'get("HERMES_QUANT_REFLECTION", "1")' in src or "get('HERMES_QUANT_REFLECTION', '1')" in src, (
        "paper.py does not use '1' as the default for HERMES_QUANT_REFLECTION"
    )


def test_cs13_callsite_default_is_1_deterministic_equity():
    """deterministic_equity.py must use os.environ.get('HERMES_QUANT_REFLECTION', '1')."""
    src = (REPO / "hermes_quant" / "react" / "deterministic_equity.py").read_text()
    assert 'get("HERMES_QUANT_REFLECTION", "1")' in src or "get('HERMES_QUANT_REFLECTION', '1')" in src, (
        "deterministic_equity.py does not use '1' as the default for HERMES_QUANT_REFLECTION"
    )
