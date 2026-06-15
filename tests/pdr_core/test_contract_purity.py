"""ADR-0092 Increment-1 fitness tests for the host-agnostic pdr-core.

Two gates live here:

  1. CONTRACT PURITY — importing ``hermes_quant.pdr_core`` must NOT transitively
     pull in any host/infra module (daemon, react backends, advisor, tools, data
     providers, MCP, broker SDKs). This is the gate that keeps the core movable to
     a standalone repo by a mechanical ``git mv``. It is checked TWO ways:
       (a) a subprocess imports pdr_core and inspects ``sys.modules`` for forbidden
           top-level packages and forbidden ``hermes_quant.<host>`` submodules;
       (b) a static walk of the pdr_core source tree for forbidden import lines.

  2. CONTRACT SHAPE — the frozen TRIAD (AnalystView / Proposal / Fill) is actually
     frozen (mutation raises) and a Proposal rejects any ``target_position_pct`` that
     is off the discrete ladder {0, +-0.05, +-0.10, +-0.15, +-0.20}.

These are written RED-first: with ``hermes_quant.pdr_core`` absent every test in
this file errors at import/collection time. Creating the package turns them GREEN.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Forbidden surface — host/infra the core must never reach for.
# ---------------------------------------------------------------------------

# Forbidden hermes_quant sub-packages/modules (the "host shell" + infra layer).
FORBIDDEN_HERMES_SUBMODULES: tuple[str, ...] = (
    "hermes_quant.daemon",
    "hermes_quant.react",
    "hermes_quant.advisor",
    "hermes_quant.tools",
    "hermes_quant.tool_schemas",
    "hermes_quant.data",
    "hermes_quant.agents",
    "hermes_quant.consumers",
    "hermes_quant.observability",
    "hermes_quant.reconcile",
    "hermes_quant.reporting",
    "hermes_quant.cli",
    "hermes_quant.discord_slash",
    "hermes_quant.committee_runner",
    # pg1: ADR-0092 forbids the core reaching into governance/evidence/state — the
    # core keeps its OWN ladder copy (contracts.py) PRECISELY for this reason, and
    # the gate/BMA/kelly port (Increment 1-cont) is the layer most tempted to
    # `from hermes_quant.governance.invariants import ACTION_SPACE` or reach into
    # state/evidence. Without these the port would import them and stay green,
    # defeating the extraction contract before the port even lands.
    "hermes_quant.governance",
    "hermes_quant.evidence",
    "hermes_quant.state",
)

# Forbidden third-party infra / broker / heavy-IO top-level packages.
FORBIDDEN_TOP_LEVEL: tuple[str, ...] = (
    "alpaca",
    "ccxt",
    "yfinance",
    "discord",
    "mcp",
    "torch",
    "sklearn",
    "requests",
    "httpx",
    "aiohttp",
    # pg1: pydantic is the agents-shell dependency the contract docstring says the
    # core must never reach (governance.audit_log pulls it transitively). The core is
    # stdlib-only frozen dataclasses; a lazy governance import would drag pydantic in.
    "pydantic",
)


def _pdr_core_source_dir() -> Path:
    """Locate hermes_quant/pdr_core on disk WITHOUT importing it.

    Walk up from this test file to the repo root (the dir that contains the
    ``hermes_quant`` package) so the static-walk test can run even if the
    package import is itself broken.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "hermes_quant" / "pdr_core"
        if candidate.is_dir():
            return candidate
    raise AssertionError(
        "hermes_quant/pdr_core/ not found on disk — the package skeleton is absent "
        "(ADR-0092 Increment-1 not yet stood up)."
    )


# ---------------------------------------------------------------------------
# Gate 1a — runtime purity via subprocess sys.modules inspection.
# ---------------------------------------------------------------------------


def test_pdr_core_import_pulls_no_host_or_infra_modules() -> None:
    """A clean subprocess that imports pdr_core must not have any forbidden
    module in sys.modules afterward.

    A subprocess (not an in-process import) is used so the host/infra modules
    already imported by the test session itself can't mask a real leak.
    """
    forbidden_hermes = list(FORBIDDEN_HERMES_SUBMODULES)
    forbidden_top = list(FORBIDDEN_TOP_LEVEL)
    code = f"""
import sys
import importlib
importlib.import_module("hermes_quant.pdr_core")
forbidden_hermes = {forbidden_hermes!r}
forbidden_top = {forbidden_top!r}
leaked = []
for name in list(sys.modules):
    if name in forbidden_hermes or any(
        name == fh or name.startswith(fh + ".") for fh in forbidden_hermes
    ):
        leaked.append(name)
    top = name.split(".", 1)[0]
    if top in forbidden_top:
        leaked.append(name)
if leaked:
    print("LEAKED:" + ",".join(sorted(set(leaked))))
    sys.exit(3)
print("CLEAN")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_pdr_core_source_dir().parents[1]),
    )
    assert proc.returncode == 0, (
        "importing hermes_quant.pdr_core transitively imported a forbidden "
        f"host/infra module.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "CLEAN" in proc.stdout


# ---------------------------------------------------------------------------
# Gate 1b — static walk of pdr_core source for forbidden import lines.
# ---------------------------------------------------------------------------


def _iter_imported_names(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            # level>0 is a relative import inside pdr_core — always allowed.
            if node.level and node.level > 0:
                continue
            if node.module:
                yield node.module


def test_pdr_core_source_has_no_forbidden_imports() -> None:
    """Static AST walk: no .py file under pdr_core may import a forbidden
    host/infra module. Belt-and-suspenders to the runtime subprocess check —
    catches leaks behind lazy/conditional imports that the eager subprocess
    import would miss."""
    src_dir = _pdr_core_source_dir()
    offenders: list[str] = []
    for py in sorted(src_dir.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for imported in _iter_imported_names(tree):
            top = imported.split(".", 1)[0]
            hits_hermes = any(
                imported == fh or imported.startswith(fh + ".")
                for fh in FORBIDDEN_HERMES_SUBMODULES
            )
            hits_top = top in FORBIDDEN_TOP_LEVEL
            if hits_hermes or hits_top:
                offenders.append(f"{py.name}: import {imported}")
    assert not offenders, (
        "pdr_core source imports forbidden host/infra modules: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Gate 2 — TRIAD shape: frozen + ladder enforcement.
# ---------------------------------------------------------------------------


def _make_analyst_view():
    from hermes_quant.pdr_core.contracts import AnalystView

    return AnalystView(
        analyst="momentum_v1",
        asset="AAPL",
        asset_class="equity",
        direction=1,
        magnitude=0.5,
        confidence=0.7,
        confidence_raw=0.9,
        horizon="1d",
        asof_decision="2026-06-12T15:00:00+00:00",
        bar_ts="2026-06-12T14:59:00+00:00",
        rationale="breakout above 50d",
        evidence_ids=("ev1", "ev2"),
    )


def _make_proposal(target_position_pct: float = 0.10):
    from hermes_quant.pdr_core.contracts import Proposal

    return Proposal(
        symbol="AAPL",
        asset_class="equity",
        target_position_pct=target_position_pct,
        gate_reason="signal-passed-gate",
        asof="2026-06-12T15:00:01+00:00",
    )


def _make_fill():
    from hermes_quant.pdr_core.contracts import Fill

    return Fill(
        proposal_id="prop_123",
        asset="AAPL",
        asset_class="equity",
        fill_price=212.34,
        fill_size_pct=0.10,
        asof_execution="2026-06-12T15:00:02+00:00",
    )


def test_triad_dataclasses_are_frozen() -> None:
    """All three TRIAD members must be frozen — mutating any field raises."""
    av = _make_analyst_view()
    prop = _make_proposal()
    fill = _make_fill()
    for obj, field_name in (
        (av, "magnitude"),
        (prop, "target_position_pct"),
        (fill, "fill_price"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field_name, 0.123)


@pytest.mark.parametrize(
    "size", [0.0, 0.05, -0.05, 0.10, -0.10, 0.15, -0.15, 0.20, -0.20]
)
def test_proposal_accepts_on_ladder_sizes(size: float) -> None:
    """Every discrete ladder rung is accepted."""
    prop = _make_proposal(target_position_pct=size)
    assert prop.target_position_pct == size


@pytest.mark.parametrize(
    "bad_size", [0.03, 0.07, 0.25, -0.30, 1.0, 0.5, -0.01, 0.11]
)
def test_proposal_rejects_off_ladder_sizes(bad_size: float) -> None:
    """Any target_position_pct off the discrete ladder must raise at construction."""
    from hermes_quant.pdr_core.contracts import Proposal

    with pytest.raises(ValueError):
        Proposal(
            symbol="AAPL",
            asset_class="equity",
            target_position_pct=bad_size,
            gate_reason="x",
            asof="2026-06-12T15:00:01+00:00",
        )


# ---------------------------------------------------------------------------
# Gate 3 (av1) — AnalystView rejects bool where a Direction int / calibrated
# float is required. In Python ``bool`` subclasses ``int`` (``True == 1``,
# ``float(True) == 1.0``), so a bool silently passes ``direction not in
# (-1,0,1)`` and the ``float(val)`` [0,1] range checks — corrupting the typed
# host-blind PERCEPTION contract the core sizer/gate reads (aggregate.py votes
# ``v.direction * w * v.confidence``; a bool arithmetic-multiplies undetected).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [True, False])
def test_analyst_view_rejects_bool_direction(bad: bool) -> None:
    """A bool ``direction`` must be rejected even though ``True in (-1,0,1)``."""
    from hermes_quant.pdr_core.contracts import AnalystView

    with pytest.raises(ValueError):
        AnalystView(
            analyst="m",
            asset="AAPL",
            asset_class="equity",
            direction=bad,  # type: ignore[arg-type]
            magnitude=0.5,
            confidence=0.7,
            confidence_raw=0.9,
            horizon="1d",
            asof_decision="t",
            bar_ts="t",
        )


@pytest.mark.parametrize("field_name", ["magnitude", "confidence", "confidence_raw"])
@pytest.mark.parametrize("bad", [True, False])
def test_analyst_view_rejects_bool_magnitude_confidence(
    field_name: str, bad: bool
) -> None:
    """A bool magnitude/confidence/confidence_raw must be rejected even though
    ``float(True) == 1.0`` / ``float(False) == 0.0`` pass the [0,1] range check."""
    from hermes_quant.pdr_core.contracts import AnalystView

    kwargs = dict(
        analyst="m",
        asset="AAPL",
        asset_class="equity",
        direction=1,
        magnitude=0.5,
        confidence=0.7,
        confidence_raw=0.9,
        horizon="1d",
        asof_decision="t",
        bar_ts="t",
    )
    kwargs[field_name] = bad  # type: ignore[assignment]
    with pytest.raises(ValueError):
        AnalystView(**kwargs)  # type: ignore[arg-type]


def test_analyst_view_accepts_valid_int_direction_and_float_fields() -> None:
    """The non-triggering path stays byte-identical: a genuine int direction
    (-1/0/1) and genuine float magnitude/confidence/confidence_raw construct."""
    from hermes_quant.pdr_core.contracts import AnalystView

    for direction in (-1, 0, 1):
        av = AnalystView(
            analyst="m",
            asset="AAPL",
            asset_class="equity",
            direction=direction,
            magnitude=0.0,
            confidence=1.0,
            confidence_raw=0.5,
            horizon="1d",
            asof_decision="t",
            bar_ts="t",
        )
        assert av.direction == direction
        assert type(av.direction) is int
        assert av.magnitude == 0.0
        assert av.confidence == 1.0
        assert av.confidence_raw == 0.5


def test_triad_round_trip_smoke() -> None:
    """Construct each member with realistic args (no exception) — guards the
    happy path so a future over-eager validator can't silently break it."""
    av = _make_analyst_view()
    assert av.direction in (-1, 0, 1)
    assert 0.0 <= av.confidence <= 1.0
    assert _make_proposal().asset_class == "equity"
    assert _make_fill().fill_size_pct == 0.10
