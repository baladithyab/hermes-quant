"""Unit tests for HermesQuantConsumer.order_filled (RR7, review-reconcile).

The #37 fix (099f7c8) corrected a latent NameError: order_filled referenced `rate`
and `amount` (params of confirm_trade_entry, NOT order_filled), so the first REAL
fill would NameError. The fix sources fill data off the freqtrade `order` object —
fill price from order.safe_price (fallback average/price) and filled qty from
order.safe_filled (fallback filled/amount) — but shipped with NO test (the e2e
suite doesn't exercise the fill path, which is why the bug went latent).

These tests run order_filled with a realistic order object and assert it:
  * does NOT raise (no NameError on rate/amount);
  * emits exactly one execution record;
  * sources fill_price from order.safe_price and qty from order.safe_filled.

Deterministic: bus paths are redirected to a tmp dir; no signals on the bus so the
orphan-fill path is taken (decision_price == fill_price), which is correct and keeps
the test from depending on signal-cache state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer


class _FakeOrder:
    """Minimal stand-in for a freqtrade Order delivered to order_filled. Carries the
    real attribute names the #37 fix reads (safe_price / safe_filled / side / cost /
    order_id) so the test exercises the actual sourcing seam, not a fallback."""

    def __init__(self, *, side="buy", safe_price=161.25, safe_filled=100.0, cost=16125.0):
        self.side = side
        self.safe_price = safe_price
        self.safe_filled = safe_filled
        self.cost = cost
        self.order_id = "ord-xyz"


class _FakeTrade:
    def __init__(self, trade_id=42):
        self.id = trade_id


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch):
    """A HermesQuantConsumer with bus paths redirected to a hermetic tmp dir."""
    s = HermesQuantConsumer({})
    exec_path = tmp_path / "executions.jsonl"
    sig_path = tmp_path / "signals.jsonl"
    halt_path = tmp_path / "halt_state.json"
    # Instance + module-level path overrides so _emit_execution / _refresh_state
    # touch only the tmp dir (the strategy reads class attrs; override per-instance).
    monkeypatch.setattr(s, "EXECUTION_BUS_PATH", exec_path)
    monkeypatch.setattr(s, "SIGNAL_BUS_PATH", sig_path)
    monkeypatch.setattr(s, "HALT_STATE_MIRROR", halt_path)
    import hermes_quant.consumers.freqtrade.quant_consumer_strategy as mod

    monkeypatch.setattr(mod, "EXECUTION_BUS_PATH", exec_path)
    return s, exec_path


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_order_filled_emits_record_from_order_object(strategy):
    """A realistic fill: order_filled must NOT NameError and must emit ONE record whose
    fill_price/qty come from order.safe_price/safe_filled (the #37 sourcing)."""
    s, exec_path = strategy
    order = _FakeOrder(side="buy", safe_price=161.25, safe_filled=100.0, cost=16125.0)
    now = pd.Timestamp("2026-05-31T12:00:00Z")

    # The bug was a NameError on `rate`/`amount`; if it regresses the broad except in
    # order_filled would swallow it and emit NOTHING. Asserting a record exists is the
    # behavioral proof the fill path runs clean end-to-end.
    s.order_filled("ETH/USDT", _FakeTrade(42), order, now)

    recs = _records(exec_path)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["fill_price"] == 161.25  # from order.safe_price
    assert rec["qty"] == 100.0  # from order.safe_filled
    assert rec["side"] == "buy"
    assert rec["asset"] == "ETH/USDT"
    assert rec["exec_id"] == "exec-42-ord-xyz"
    # Orphan fill (no matching signal on the bus) => decision_price falls back to the
    # fill price so realized_return is 0 (settlement loop skips it). This is correct.
    assert rec["decision_price"] == 161.25
    assert rec["signal_id"] is None
    assert rec["realized_pnl"] is None


def test_order_filled_sources_sell_fill_distinct_from_amount(strategy):
    """safe_price/safe_filled (NOT the confirm_trade_entry rate/amount) are the source:
    a sell with a different safe_price/safe_filled emits exactly those values. If the code
    regressed to referencing rate/amount it would NameError -> empty bus -> this fails."""
    s, exec_path = strategy
    order = _FakeOrder(side="sell", safe_price=158.10, safe_filled=55.0, cost=8695.5)
    now = pd.Timestamp("2026-05-31T13:30:00Z")

    s.order_filled("BTC/USDT", _FakeTrade(7), order, now)

    recs = _records(exec_path)
    assert len(recs) == 1
    assert recs[0]["side"] == "sell"
    assert recs[0]["fill_price"] == 158.10
    assert recs[0]["qty"] == 55.0


def test_ar35_latest_signal_per_asset_is_chronological_not_lexical(strategy, monkeypatch):
    """ar35: the per-asset 'latest signal' cache must order by PARSED TIMESTAMP, not by a
    lexical asof-string compare. We write two ETH signals where the chronologically-LATER
    one (12:00, naive 'YYYY-MM-DD HH:MM:SS') is lexically SMALLER than the earlier one
    (11:00, '...T...Z') because ' ' (0x20) < 'T' (0x54). A lexical compare would cache the
    STALE 11:00 'sell'; the fix must cache the fresh 12:00 'buy'."""
    s, _ = strategy
    sig_path = s.SIGNAL_BUS_PATH
    # Earlier (11:00Z) bearish, later (12:00 naive) bullish — the later one must win.
    earlier = {
        "schema_version": 1, "asset": "ETH/USDT", "type": "signal",
        "asof": "2026-05-31T11:00:00Z", "stance": "bearish",
        "target_position_pct": -0.10,
    }
    later = {
        "schema_version": 1, "asset": "ETH/USDT", "type": "signal",
        "asof": "2026-05-31 12:00:00", "stance": "bullish",  # naive -> assumed UTC
        "target_position_pct": 0.10,
    }
    # Lexical sanity: the later signal's asof string sorts BEFORE the earlier one.
    assert later["asof"] < earlier["asof"], "test premise: later asof must be lexically smaller"
    sig_path.write_text("\n".join(json.dumps(r) for r in [earlier, later]) + "\n", encoding="utf-8")

    s._refresh_state(pd.Timestamp("2026-05-31T12:30:00Z"))

    cached = s._signal_cache.get("ETH/USDT")
    assert cached is not None
    assert cached["asof"] == "2026-05-31 12:00:00", (
        f"ar35: the cache kept the chronologically STALE signal via lexical compare "
        f"(got asof={cached.get('asof')}, stance={cached.get('stance')}); must keep the 12:00 buy"
    )
    assert cached["stance"] == "bullish"
