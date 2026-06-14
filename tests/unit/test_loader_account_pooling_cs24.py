"""cs24 GREEN proof — the loader account set-OR pooled the synthetic book.

THE BUG (was LIVE on ~/.hermes/quant/executions.jsonl)
------------------------------------------------------
`daemon.portfolio_loader.reconstruct_portfolio` admitted absolute-target records
with `_record_account(r) in {account_id, "paper-default"}` — a set-OR that pooled
the ENTIRE synthetic "paper-default" book (PaperReactor + DeterministicEquityReactor
both resolve to "paper-default") into ANY requested account. So a request for the
SEPARATE Alpaca SHADOW partition ("alpaca-paper", react/alpaca_paper.py:67) silently
absorbed the real synthetic managed book.

The weekly-exit cron (scripts/quant-playbook-weekly.py load_portfolio) requested
account_id="alpaca-paper"; with the set-OR it received the paper-default book POOLED
with the lone real alpaca-paper position.

THE FIX (cs24, two coherent parts)
----------------------------------
1. FILTER: the loader is now account-EQUALITY (`_record_account(r) == account_id`),
   matching the cs18 sibling reconstruction
   (portfolio.state.reconstruct_portfolio_state:138) and the strict legacy int-1 path
   (portfolio_loader.py:90). No pooling.
2. ACCOUNT REQUEST: the weekly now requests account_id="paper-default" — the real
   synthetic book the ADR-0035 playbook system trades (autonomous tick + PaperReactor
   + DeterministicEquityReactor all write to it) — not the default-OFF Alpaca shadow.

These tests use the REAL producer chain (ExecutionRecord -> _record_to_dict) across
THREE account partitions and pin: (a) no pooling under either request, (b) the loader
and the cs18 state-reconstruction now AGREE on account semantics, (c) the weekly's
corrected request returns the paper-default book.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.daemon.portfolio_loader import reconstruct_portfolio
from hermes_quant.portfolio.state import reconstruct_portfolio_state
from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import _record_to_dict

ASSET_CLASS = "equity"


def _paper_default_fill(asset: str, pct: float, *, ts: str) -> dict:
    """PaperReactor fill: no account_id, no reactor_metadata.account_id -> paper-default."""
    return _record_to_dict(
        ExecutionRecord(
            proposal_id=f"prop-{asset}",
            signal_id=f"sig-{asset}",
            asset=asset,
            asset_class=ASSET_CLASS,
            timeframe="1d",
            asof_decision=ts,
            asof_execution=ts,
            target_position_pct=pct,
            decision_price=200.0,
            fill_price=200.0,
            fill_size_pct=pct,
            reactor_name="paper",
            human_in_the_loop=False,
        )
    )


def _det_equity_fill(asset: str, pct: float, *, ts: str) -> dict:
    """DeterministicEquityReactor fill: reactor_metadata.account_id='paper-default'."""
    return _record_to_dict(
        ExecutionRecord(
            proposal_id=f"prop-{asset}",
            signal_id=f"sig-{asset}",
            asset=asset,
            asset_class=ASSET_CLASS,
            timeframe="1d",
            asof_decision=ts,
            asof_execution=ts,
            target_position_pct=pct,
            decision_price=200.0,
            fill_price=200.0,
            fill_size_pct=pct,
            reactor_name="deterministic-equity",
            human_in_the_loop=False,
            reactor_metadata={"account_id": "paper-default"},
        )
    )


def _alpaca_fill(asset: str, pct: float, *, ts: str) -> dict:
    """AlpacaPaperReactor fill: reactor_metadata.account_id='alpaca-paper' (SHADOW)."""
    return _record_to_dict(
        ExecutionRecord(
            proposal_id=f"prop-{asset}",
            signal_id=f"sig-{asset}",
            asset=asset,
            asset_class=ASSET_CLASS,
            timeframe="1d",
            asof_decision=ts,
            asof_execution=ts,
            target_position_pct=pct,
            decision_price=50.0,
            fill_price=50.0,
            fill_size_pct=pct,
            reactor_name="alpaca_paper",
            human_in_the_loop=False,
            reactor_metadata={"account_id": "alpaca-paper"},
        )
    )


def _write_bus(tmp_path: Path, recs: list[dict]) -> Path:
    bus = tmp_path / "executions.jsonl"
    bus.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return bus


def _multi_account_bus(tmp_path: Path) -> Path:
    """Two paper-default fills (AAPL, BA) + one alpaca-paper SHADOW fill (T)."""
    return _write_bus(
        tmp_path,
        [
            _paper_default_fill("AAPL", 0.20, ts="2026-06-08T13:31:05+00:00"),
            _det_equity_fill("BA", 0.10, ts="2026-06-08T13:31:06+00:00"),
            _alpaca_fill("T", 0.05, ts="2026-06-08T13:31:07+00:00"),
        ],
    )


def test_alpaca_request_no_longer_pools_the_paper_default_book(tmp_path: Path) -> None:
    """cs24 GREEN: a request for the "alpaca-paper" SHADOW partition returns ONLY the
    real alpaca-paper position (T) — NOT the pooled paper-default book.

    Under the prior set-OR this returned {AAPL, BA, T}. Equality returns {T}.
    """
    bus = _multi_account_bus(tmp_path)
    pf = reconstruct_portfolio("alpaca-paper", ASSET_CLASS, bus_path=bus)
    assert sorted(pf.positions.keys()) == ["T"]
    # The paper-default fills MUST NOT have leaked into the shadow request.
    assert "AAPL" not in pf.positions
    assert "BA" not in pf.positions


def test_paper_default_request_returns_the_real_managed_book(tmp_path: Path) -> None:
    """cs24 GREEN: a request for "paper-default" (what the corrected weekly asks for)
    returns the real synthetic book {AAPL, BA} and EXCLUDES the alpaca shadow (T)."""
    bus = _multi_account_bus(tmp_path)
    pf = reconstruct_portfolio("paper-default", ASSET_CLASS, bus_path=bus)
    assert sorted(pf.positions.keys()) == ["AAPL", "BA"]
    assert "T" not in pf.positions  # alpaca shadow not pooled in


def test_loader_and_state_reconstruction_agree_on_account_semantics(tmp_path: Path) -> None:
    """cs24: the loader (reconstruct_portfolio) and the cs18 state reconstruction
    (reconstruct_portfolio_state, already account-EQUALITY) now AGREE on which symbols
    belong to which partition — they previously DISAGREED (set-OR vs equality)."""
    bus = _multi_account_bus(tmp_path)

    loader_pd = set(reconstruct_portfolio("paper-default", ASSET_CLASS, bus_path=bus).positions)
    loader_alp = set(reconstruct_portfolio("alpaca-paper", ASSET_CLASS, bus_path=bus).positions)

    state_pd = set(
        reconstruct_portfolio_state(bus, reactor_filter=None, account="paper-default").positions
    )
    state_alp = set(
        reconstruct_portfolio_state(bus, reactor_filter=None, account="alpaca-paper").positions
    )

    assert loader_pd == state_pd == {"AAPL", "BA"}
    assert loader_alp == state_alp == {"T"}


def test_weekly_load_portfolio_requests_paper_default(tmp_path: Path) -> None:
    """cs24: the weekly's load_portfolio now reconstructs the paper-default book.

    Drives the REAL load_portfolio (scripts twin) against a multi-account bus and
    asserts it returns the paper-default positions, NOT the pooled/shadow set.
    """
    import importlib.util
    import sys

    weekly_path = Path(__file__).resolve().parents[2] / "scripts" / "quant-playbook-weekly.py"
    mod_name = "quant_playbook_weekly_cs24"
    spec = importlib.util.spec_from_file_location(mod_name, weekly_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses defined in the script can resolve their own
    # module namespace (dataclasses.py reads sys.modules[cls.__module__]).
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        pass

    bus = _multi_account_bus(tmp_path)
    pf, raw = mod.load_portfolio(executions_path=bus)
    assert pf is not None
    assert sorted(pf.positions.keys()) == ["AAPL", "BA"]
    assert "T" not in pf.positions  # the alpaca shadow position is not managed by the weekly
    assert len(raw) == 3  # raw executions are returned unfiltered
    # The reconstructed Portfolio is partitioned to paper-default.
    assert pf.account_id == "paper-default"


@pytest.mark.parametrize(
    "request_account,expected",
    [
        ("paper-default", {"AAPL", "BA"}),
        ("alpaca-paper", {"T"}),
        ("nonexistent-account", set()),
    ],
)
def test_request_account_returns_only_that_partition(
    tmp_path: Path, request_account: str, expected: set[str]
) -> None:
    """cs24: every account request returns EXACTLY its own partition — no pooling,
    no leakage, and an unknown account returns an empty book (not the paper-default
    book by sentinel-fallback, which is what the set-OR did)."""
    bus = _multi_account_bus(tmp_path)
    pf = reconstruct_portfolio(request_account, ASSET_CLASS, bus_path=bus)
    assert set(pf.positions.keys()) == expected
