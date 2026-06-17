"""tests/ops/test_quant_flatten_paper_default_cs25.py — cs25 flatten-coverage regression.

ops/scripts/quant-flatten-paper-default.py is the operator headroom-recovery tool. It
MUST flatten the SAME position set the post-cs16 canonical reconstruction (the autonomous
D9 safety rail + portfolio-caps gate, autonomous.py:534/:565) sees, or the operator
believes the book is flat while residual det-equity/alpaca positions remain open and the
cap STILL counts them in gross/net (no headroom freed).

cs25 RED: the script enumerated positions via reconstruct_portfolio_state(BUS) with the
DEFAULT reactor_filter='paper' — the narrow "paper-only slice" autonomous.py:505-508 warns
UNDER-counts the book. cs16 (commit 2f1a280) widened the cap to reactor_filter=None (paper
+ deterministic-equity + alpaca_paper). So a deterministic-equity position on the
paper-default book was COUNTED by the cap but MISSED by the flatten script => left open,
false "flat".

cs25 GREEN: the script enumerates through canonical_open_positions() ==
reconstruct_portfolio_state(reactor_filter=None, account="paper-default"), the SAME seam
the cap reads (cs16 reactor_filter=None) scoped to the synthetic paper-default book (cs18 —
the deliberately-separate alpaca-paper SHADOW book is EXCLUDED because its real broker
positions must close via the broker, not a synthetic bus append).

The script is a standalone dash-named file (not a package module), so we load it via
spec_from_file_location.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "scripts"
    / "quant-flatten-paper-default.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("ops_flatten_cs25", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_bus(tmp_path: Path, records: list[dict]) -> str:
    bus = tmp_path / "executions.jsonl"
    with open(bus, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(bus)


# A three-reactor book exactly like the live bus cs16 measured: a 'paper' open (BA),
# a 'deterministic-equity' open on the SAME paper-default book (AAPL), and an
# 'alpaca_paper' SHADOW open whose account lives in reactor_metadata (T).
_MIXED_BOOK = [
    {
        "asset": "BA",
        "target_position_pct": 0.20,
        "asof_execution": "2026-06-10T10:00:00Z",
        "reactor_name": "paper",
    },
    {
        "asset": "AAPL",
        "target_position_pct": -0.15,
        "asof_execution": "2026-06-10T11:00:00Z",
        "reactor_name": "deterministic-equity",
    },
    {
        "asset": "T",
        "target_position_pct": 0.10,
        "asof_execution": "2026-06-10T12:00:00Z",
        "reactor_name": "alpaca_paper",
        "reactor_metadata": {"account_id": "alpaca-paper"},
    },
]


def test_canonical_enumeration_includes_det_equity_missed_by_paper_slice(tmp_path):
    """RED->GREEN: the script's enumeration must include the deterministic-equity
    position the narrow reactor_filter='paper' slice (the pre-cs25 path) MISSED.

    A book whose only opens are det-equity/alpaca was reported FLAT by the old script.
    """
    from hermes_quant.portfolio.state import reconstruct_portfolio_state

    bus = _write_bus(tmp_path, _MIXED_BOOK)
    mod = _load_script()

    # ar97 UPDATE: the default reactor_filter='paper' is now the paper-BOOK FAMILY
    # {paper, deterministic-equity} (both account_id=paper-default), so the default view
    # ALREADY includes AAPL (det-equity) + BA (paper); the separate alpaca-paper shadow
    # (T) stays excluded. (Pre-ar97 this default undercounted to {BA} only — the exact
    # bug cs25's canonical_open_positions worked around; ar97 fixed it at the source, so
    # the workaround and the source default now AGREE on the paper-default book.)
    default_paper_book = set(reconstruct_portfolio_state(bus).positions)
    assert default_paper_book == {"BA", "AAPL"}, (
        "ar97: default reactor_filter='paper' is the paper-book family "
        f"{{paper, deterministic-equity}}; got {sorted(default_paper_book)}"
    )
    assert "T" not in default_paper_book, "alpaca_paper shadow must stay out of the paper-book view"

    # The canonical enumeration the script uses (reactor_filter=None — the WHOLE book).
    flattened = set(mod.canonical_open_positions(bus))

    # AAPL (deterministic-equity, paper-default) MUST be enumerated for flatten.
    assert "AAPL" in flattened, (
        "deterministic-equity paper-default position MUST be flattened (cs16/cs25): "
        f"got {sorted(flattened)}"
    )
    assert "BA" in flattened


def test_canonical_enumeration_excludes_alpaca_shadow_book(tmp_path):
    """cs18: the alpaca-paper SHADOW book (account_id in reactor_metadata) MUST be
    EXCLUDED — its real broker positions cannot be closed by a synthetic bus append.
    """
    bus = _write_bus(tmp_path, _MIXED_BOOK)
    mod = _load_script()

    flattened = set(mod.canonical_open_positions(bus))
    assert "T" not in flattened, (
        "alpaca-paper SHADOW position must NOT be synthetically flattened (cs18): "
        f"got {sorted(flattened)}"
    )
    # The synthetically-closable paper-default book is exactly {BA (paper), AAPL (det-eq)}.
    assert flattened == {"BA", "AAPL"}


def test_canonical_view_equals_post_cs16_cap_minus_shadow(tmp_path):
    """The flatten set must equal the canonical paper-default partition the cap reads.

    The cap uses reactor_filter=None (whole book incl. the alpaca shadow per cs18's
    known partition-disagreement); the flatten set is that book account-scoped to
    paper-default. So flatten_set == cap_view - alpaca_shadow.
    """
    from hermes_quant.portfolio.state import reconstruct_portfolio_state

    bus = _write_bus(tmp_path, _MIXED_BOOK)
    mod = _load_script()

    cap_view = set(reconstruct_portfolio_state(bus, reactor_filter=None).positions)
    flattened = set(mod.canonical_open_positions(bus))

    assert flattened == cap_view - {"T"}, (
        f"flatten set {sorted(flattened)} must equal cap view {sorted(cap_view)} "
        "minus the alpaca-paper shadow {T}"
    )


def test_already_aligned_book_is_byte_identical(tmp_path):
    """A book whose ONLY opens are 'paper' (the pre-cs25 universe) flattens the SAME
    set under the old slice and the new canonical seam — no behavior change.
    """
    from hermes_quant.portfolio.state import reconstruct_portfolio_state

    paper_only = [
        {
            "asset": "BA",
            "target_position_pct": 0.20,
            "asof_execution": "2026-06-10T10:00:00Z",
            "reactor_name": "paper",
        },
        {
            "asset": "ASTS",
            "target_position_pct": -0.20,
            "asof_execution": "2026-06-10T11:00:00Z",
            "reactor_name": "paper",
        },
    ]
    bus = _write_bus(tmp_path, paper_only)
    mod = _load_script()

    old_slice = set(reconstruct_portfolio_state(bus).positions)
    flattened = set(mod.canonical_open_positions(bus))
    assert flattened == old_slice == {"BA", "ASTS"}


def test_closed_position_not_re_flattened(tmp_path):
    """A det-equity symbol later closed to target 0 must NOT appear in the flatten set
    (drop_zeros default), so the tool does not emit a spurious close.
    """
    records = [
        {
            "asset": "AAPL",
            "target_position_pct": -0.15,
            "asof_execution": "2026-06-10T11:00:00Z",
            "reactor_name": "deterministic-equity",
        },
        {
            "asset": "AAPL",
            "target_position_pct": 0.0,
            "asof_execution": "2026-06-10T13:00:00Z",
            "reactor_name": "deterministic-equity",
        },
        {
            "asset": "BA",
            "target_position_pct": 0.20,
            "asof_execution": "2026-06-10T10:00:00Z",
            "reactor_name": "paper",
        },
    ]
    bus = _write_bus(tmp_path, records)
    mod = _load_script()

    flattened = set(mod.canonical_open_positions(bus))
    assert flattened == {"BA"}, (
        f"closed AAPL must not be re-flattened; got {sorted(flattened)}"
    )
