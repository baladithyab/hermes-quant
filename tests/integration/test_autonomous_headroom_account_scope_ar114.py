"""ar114 — the autonomous portfolio-caps HEADROOM reconstruct must be scoped to the
``paper-default`` account, NOT the whole cross-account book.

Background (the fail-open, RED-proved by the wave-16 review team and re-confirmed here):
``reconstruct_portfolio_state`` collapses every asset to its LATEST-asof target — it
does NOT sum across books (hermes_quant/portfolio/state.py:208-225). The §D9 CONCURRENT
rail (autonomous.py ~1362) over-COUNTS symbols safely under ``account=None`` — more
cardinality only BLOCKS new opens. But the headroom path (autonomous.py ~1393) sums
GROSS exposure. Under ``account=None`` a smaller, more-recent alpaca-paper SHADOW target
for a ticker REPLACES the larger real paper-default position in the latest-wins collapse
→ gross is UNDER-counted → headroom inflated → a new pick over-trades (fail-open).

The fix scopes the headroom reconstruct to ``account="paper-default"`` (the {paper,
deterministic-equity} family this cap governs), dropping the alpaca shadow BEFORE the
collapse (cs18 partition; mirrors the cs25 flatten seam). alpaca_paper is default-OFF, so
this is byte-identical on the live single-book bus and closes the fail-open the moment
HERMES_QUANT_ALPACA_PAPER is flipped on.

Two layers of coverage:
  1. test_reducer_*  — the MATH: the latest-wins reducer under-counts the cross-account
     pool; account scoping recovers the true paper-default gross. (No tick() — pure seam.)
  2. test_tick_*     — the WIRING: tick()'s headroom reconstruct actually carries
     account="paper-default" and the PortfolioState it builds reflects the real 0.18, not
     the 0.02 shadow. The §D9 COUNT rail stays account=None (safe over-count). Asserted by
     spying on the real call (behavioral), NOT by matching source text (ar11 lesson).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.watchlist import WatchlistEntry


# A ticker held LARGE on the real paper-default book and SMALL+LATER on the alpaca-paper
# SHADOW book. Under latest-wins-per-asset, account=None collapses NVDA to the 0.02
# shadow (the later record), hiding the real 0.18 — the exact fail-open.
_CROSS_ACCOUNT_BUS = [
    {
        "asset": "NVDA",
        "target_position_pct": 0.18,
        "asof_execution": "2026-06-15T10:00:00Z",
        "reactor_name": "paper",  # no account stamp -> resolves to paper-default
        "fill_price": 100.0,
        "fill_size_pct": 0.18,
    },
    {
        "asset": "NVDA",
        "target_position_pct": 0.02,  # SMALLER + LATER -> wins the latest-asof collapse
        "asof_execution": "2026-06-15T12:00:00Z",
        "reactor_name": "alpaca_paper",
        "reactor_metadata": {"account_id": "alpaca-paper"},  # the SHADOW book
        "fill_price": 100.0,
        "fill_size_pct": 0.02,
    },
]


def _write_bus(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_reducer_account_none_undercounts_cross_account_pool(tmp_path):
    """THE MATH (RED rationale): the OLD headroom arg (account=None) lets the small, late
    alpaca shadow REPLACE the large real paper-default NVDA in the latest-wins collapse —
    so the cap would have read gross 0.02 for a position that is really 0.18.
    """
    from hermes_quant.portfolio.state import reconstruct_portfolio_state

    bus = tmp_path / "executions.jsonl"
    _write_bus(bus, _CROSS_ACCOUNT_BUS)

    pooled = reconstruct_portfolio_state(bus, reactor_filter=None).positions
    scoped = reconstruct_portfolio_state(
        bus, reactor_filter=None, account="paper-default"
    ).positions

    # The bug: pooling collapses NVDA to the 0.02 shadow (later asof wins).
    assert pooled.get("NVDA") == pytest.approx(0.02), (
        "ar114 premise: account=None pools the shadow and the latest-wins reducer "
        f"collapses NVDA to the 0.02 shadow; got {pooled}"
    )
    # The fix: account scoping drops the shadow before the collapse -> real 0.18.
    assert scoped.get("NVDA") == pytest.approx(0.18), (
        "ar114 fix: account='paper-default' must recover the real paper-default gross "
        f"(0.18), not the 0.02 shadow; got {scoped}"
    )
    # And the under-count is strictly fail-open: pooled gross < true gross.
    assert pooled["NVDA"] < scoped["NVDA"], (
        "the pool UNDER-counts gross (fail-open: inflated headroom -> over-trade)"
    )


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_quant.autonomous.QUANT_HOME", qhome)
    monkeypatch.setattr("hermes_quant.autonomous.KILL_SWITCH_PATH", qhome / "ks.json")
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(
        auto,
        "_read_safety_rails",
        lambda: {
            "max_per_tick_opens": 5,
            "max_concurrent_positions": 10,
            "kill_switch_pct": 0.10,
            "log_silences": False,
            "allow_live": False,
            "paper_zero_costs": False,
            "require_stop_loss": False,
        },
    )
    # The headroom reconstruct (autonomous.py:1393) only runs when the cap gate is ON.
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    return qhome


def _advisor(kelly):
    return {
        "as_of": "2026-06-15T20:00:00Z",
        "decision_price": 100.0,
        "signal_id": "s",
        "aggregated_signal": {"confidence": 0.85, "direction": 1, "magnitude": 0.05},
        "risk_gate": {"pass": True, "kelly_fraction": kelly, "reason": "ok"},
        "analyst_views": [
            {"analyst": f"A{i}", "metadata": {"atr_relative": 0.05}} for i in range(2)
        ],
        "lessons": [],
    }


def test_tick_headroom_reconstruct_is_account_scoped(isolate, monkeypatch):
    """THE WIRING: tick() must build its headroom PortfolioState from an
    account='paper-default' reconstruct that sees the real 0.18 — while the §D9 COUNT
    rail keeps account=None (safe over-count). Spies on the REAL call so this can't pass
    on the buggy code (which passed only reactor_filter=None).
    """
    qhome = isolate
    _write_bus(qhome / "executions.jsonl", _CROSS_ACCOUNT_BUS)

    import hermes_quant.portfolio.state as pstate

    real = pstate.reconstruct_portfolio_state
    calls: list[dict] = []

    def spy(*args, **kwargs):
        res = real(*args, **kwargs)
        calls.append(
            {"args": args, "kwargs": kwargs, "positions": dict(res.positions)}
        )
        return res

    monkeypatch.setattr(pstate, "reconstruct_portfolio_state", spy)

    auto.tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _advisor(0.05),
    )

    # The headroom call (account="paper-default") must exist AND have seen the real 0.18.
    headroom = [c for c in calls if c["kwargs"].get("account") == "paper-default"]
    assert headroom, (
        "ar114: the headroom reconstruct (autonomous.py:1393) must be scoped to "
        f"account='paper-default'; reconstruct calls seen: "
        f"{[c['kwargs'] for c in calls]}"
    )
    assert headroom[0]["positions"].get("NVDA") == pytest.approx(0.18), (
        "ar114: the account-scoped headroom state must reflect the real paper-default "
        f"NVDA (0.18), not the 0.02 shadow; got {headroom[0]['positions']}"
    )

    # The §D9 COUNT rail must remain account=None (whole book = safe over-count of symbols).
    count_rail = [
        c
        for c in calls
        if c["kwargs"].get("reactor_filter") is None
        and c["kwargs"].get("account") is None
    ]
    assert count_rail, (
        "the §D9 concurrent-position COUNT rail must stay account=None (over-count is "
        f"safe for a cardinality rail); reconstruct calls seen: "
        f"{[c['kwargs'] for c in calls]}"
    )
