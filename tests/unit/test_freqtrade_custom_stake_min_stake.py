"""Unit tests for HermesQuantConsumer.custom_stake_amount min_stake honesty (ar65).

Defect (RED-verified): custom_stake_amount computed `min(max_stake, wallet_balance *
target)` and NEVER compared the result against the freqtrade-supplied `min_stake` arg
(declared in the signature, referenced nowhere). Per the freqtrade IStrategy contract,
a returned positive stake BELOW min_stake is silently adjusted UP to min_stake (via the
min_pair_stake_amount clamp) — or the entry is dropped. Either way the quant's intended
sub-min-notional sizing is breached:

  target=0.001 (0.1% NAV), wallet=2000 USDT, Binance min-notional min_stake=10.0
  => intended = 2000 * 0.001 = 2.0 USDT  (< min_stake)
  => old code returned 2.0; freqtrade clamps UP to 10.0 = 5x the intended 0.1% sizing,
     a silent OVER-SIZE breaching the deterministic quarter-Kelly action_step/max_position
     sizing (kelly.py, ADR-0004).

Fix (silence-by-default): if the intended notional is a positive value below min_stake,
the quant's intended size cannot be honored without breaching deterministic sizing, so
return 0 (an honest under-size / no-trade, never a dishonest inflated entry). min_stake
is guarded for None per the freqtrade contract (None when the exchange reports no minimum).

Mirrors the ar30 harness: _latest_signal_for stubbed to return a controlled signal and
self.wallets stubbed to a fake reporting get_total_stake_amount. Distinct from ar30/ar31
(non-finite / <=0 target guards) — those used target=0.10 x wallet=5000=500 >> 10.0, so
the wallet*target < min_stake branch was never exercised.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer


class _FakeWallets:
    def __init__(self, total: float):
        self._total = total

    def get_total_stake_amount(self) -> float:
        return self._total


def _make_strategy(monkeypatch, *, target: float, wallet: float):
    s = HermesQuantConsumer({})
    # Stub the signal lookup so custom_stake_amount sees a controlled target (mirrors
    # the ar30 harness — keeps the test off bus/cache state).
    monkeypatch.setattr(
        s, "_latest_signal_for", lambda pair, current_time: {"target_position_pct": target}
    )
    s.wallets = _FakeWallets(wallet)
    return s


def _stake(s, *, min_stake, max_stake):
    return s.custom_stake_amount(
        pair="BTC/USDT",
        current_time=pd.Timestamp("2026-06-16T12:00:00Z"),
        current_rate=100.0,
        proposed_stake=0.0,
        min_stake=min_stake,
        max_stake=max_stake,
        leverage=1.0,
        entry_tag=None,
        side="long",
    )


def test_sub_min_notional_returns_zero_not_inflated(monkeypatch):
    """target=0.001 x wallet=2000 = 2.0 USDT intended, below Binance min_stake=10.0.
    Old code returned 2.0 (freqtrade then clamps UP to 10.0 = 5x over-size). The fix
    must return 0 — an honest no-trade, never a dishonest inflated entry."""
    s = _make_strategy(monkeypatch, target=0.001, wallet=2000.0)
    stake = _stake(s, min_stake=10.0, max_stake=1000.0)
    assert stake == 0, f"sub-min notional must silence to 0, got {stake!r}"


def test_at_or_above_min_notional_unchanged(monkeypatch):
    """A target whose intended notional is >= min_stake is honored unchanged — the fix
    is byte-identical for the in-range path (ar30's target=0.10 x wallet=5000=500 case)."""
    s = _make_strategy(monkeypatch, target=0.10, wallet=5000.0)
    stake = _stake(s, min_stake=10.0, max_stake=1000.0)
    assert stake == 500.0


def test_min_stake_none_does_not_drop_trade(monkeypatch):
    """The freqtrade contract allows min_stake=None (exchange reports no minimum). The
    fix must guard for None and NOT silence — return the intended notional unchanged."""
    s = _make_strategy(monkeypatch, target=0.001, wallet=2000.0)
    stake = _stake(s, min_stake=None, max_stake=1000.0)
    assert stake == 2.0


def test_exactly_at_min_stake_is_honored(monkeypatch):
    """Intended notional == min_stake is honored (the clamp boundary is strict-below):
    target=0.005 x wallet=2000 = 10.0 == min_stake => returned unchanged."""
    s = _make_strategy(monkeypatch, target=0.005, wallet=2000.0)
    stake = _stake(s, min_stake=10.0, max_stake=1000.0)
    assert stake == 10.0
