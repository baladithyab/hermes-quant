"""ar29/ar30/ar31 — three defects the sixth convergence review (wf wxpq3w6y0) RED-verified.

  ar29 — catalyst profitability fetcher entered at the SAME-day bar (>= asof) despite the
         documented NEXT-bar (> asof) contract -> same-bar lookahead inflated the brand_self
         edge that drives the live CONSUMER_TREND_CONFIDENCE_HAIRCUT raise/prune decision.
  ar30 — freqtrade consumer custom_stake_amount sized the FULL allowed stake on a non-finite
         target_position_pct (float() doesn't catch nan/inf; nan<=0 is False) — sizing fail-OPEN
         off an externally-writable signals.jsonl bus.
  ar31 — deterministic backend _require_bp admitted a NaN notional past the anti-over-leverage
         BP gate (nan<=0 False; nan>bp+eps False) -> a NaN-priced fill. LIVE path
         (HERMES_QUANT_DETERMINISTIC_EQUITY=1 in ~/.hermes/.env).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_quant.react.backend import BackendUnavailableError
from hermes_quant.react.backends.deterministic_backend import DeterministicBackend


# --------------------------------------------------------------------------- #
# ar31 — deterministic backend BP gate fails CLOSED on a non-finite notional
# --------------------------------------------------------------------------- #
def _install_account(monkeypatch, *, balance_usd=100_000.0, equity_total=100_000.0):
    import hermes_quant.state.portfolio_state as ps

    class _Cash:
        account_id = "paper-default"
        last_update_at = "2026-06-05T00:00:00Z"

        def __init__(self):
            self.balance_usd = balance_usd
            self.equity_total = equity_total

    class _PF:
        def get_cash(self, account_id):
            return _Cash()

    monkeypatch.setattr(ps, "get_portfolio_state", lambda *a, **k: _PF())
    monkeypatch.setattr(ps, "_default_initial_cash", lambda: balance_usd)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_ar31_require_bp_fails_closed_on_nonfinite(monkeypatch, bad):
    _install_account(monkeypatch)
    be = DeterministicBackend()
    with pytest.raises(BackendUnavailableError):
        be._require_bp(bad, what="equity NVDA (non-finite)")


def test_ar31_require_bp_allows_finite_credit_and_affordable(monkeypatch):
    _install_account(monkeypatch, balance_usd=100_000.0)
    be = DeterministicBackend()
    # Byte-identical on the finite path: a credit (<=0) and an affordable debit pass.
    be._require_bp(-50.0, what="credit")  # no raise
    be._require_bp(10_000.0, what="affordable")  # no raise


def test_ar31_submit_equity_nan_price_refused(monkeypatch):
    _install_account(monkeypatch)
    be = DeterministicBackend()
    with pytest.raises(BackendUnavailableError):
        # A NaN decision_price makes notional NaN -> the BP gate must refuse it,
        # not book a NaN-priced fill.
        be.submit_equity(
            symbol="NVDA", signed_qty=100.0, decision_price=float("nan"),
            client_order_id="x",
        )


# --------------------------------------------------------------------------- #
# ar30 — freqtrade consumer sizes 0 (not max_stake) on a non-finite target
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_ar30_custom_stake_zero_on_nonfinite_target(monkeypatch, bad):
    from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer

    s = HermesQuantConsumer({})
    # Stub the signal lookup to return a non-finite target (the externally-writable bus).
    monkeypatch.setattr(s, "_latest_signal_for", lambda pair, ct: {"target_position_pct": bad})

    class _Wallets:
        def get_total_stake_amount(self):
            return 5_000.0

    s.wallets = _Wallets()
    stake = s.custom_stake_amount(
        pair="ETH/USDT", current_time=None, current_rate=100.0,
        proposed_stake=1_000.0, min_stake=10.0, max_stake=1_000.0,
        leverage=1.0, entry_tag=None, side="long",
    )
    assert stake == 0, f"ar30: a non-finite target ({bad}) sized {stake}, must silence to 0"


def test_ar30_custom_stake_finite_target_byte_identical(monkeypatch):
    from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer

    s = HermesQuantConsumer({})
    monkeypatch.setattr(s, "_latest_signal_for", lambda pair, ct: {"target_position_pct": 0.10})

    class _Wallets:
        def get_total_stake_amount(self):
            return 5_000.0

    s.wallets = _Wallets()
    stake = s.custom_stake_amount(
        pair="ETH/USDT", current_time=None, current_rate=100.0,
        proposed_stake=1_000.0, min_stake=10.0, max_stake=1_000.0,
        leverage=1.0, entry_tag=None, side="long",
    )
    # min(max_stake=1000, 5000*0.10=500) = 500 — unchanged by the finite-guard.
    assert stake == pytest.approx(500.0)


# --------------------------------------------------------------------------- #
# ar29 — catalyst profitability enters at the NEXT bar (> asof), not same-day
# --------------------------------------------------------------------------- #
def _load_cron():
    """Import the ops script (it re-execs the venv at import — guard against that)."""
    path = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-catalyst-profitability.py"
    spec = importlib.util.spec_from_file_location("quant_catalyst_profitability_ar29", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_ar29_forward_return_enters_next_bar_not_same_day(monkeypatch):
    import datetime as _dt

    import pandas as pd

    cron = _load_cron()
    # Daily closes: asof=2026-06-01 (a bar exists ON asof — an intraday-published signal).
    # close[D]=100 (publication day), close[D+1]=110 (the first TRADEABLE bar after asof).
    idx = pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"])
    df = pd.DataFrame({"Close": pd.Series([100.0, 110.0, 121.0, 121.0], index=idx)})

    # Patch yfinance.download (the only network call) to our deterministic frame.
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *a, **k: df)

    ret = cron._yf_forward_return("X", _dt.date(2026, 6, 1))
    assert ret is not None
    # ar29: entry must be the NEXT bar (close[D+1]=110), NOT the same-day close[D]=100.
    # The 21-day fwd window exceeds the frame, so exit falls to the last bar (121).
    # Correct return = 121/110 - 1; the buggy same-day entry would give 121/100 - 1.
    assert ret == pytest.approx((121.0 / 110.0 - 1) * 100), (
        f"ar29: forward return {ret} implies a same-day (>= asof) entry; the next-bar "
        f"(> asof) contract requires entry=110 -> {(121.0/110.0-1)*100:.4f}, not "
        f"{(121.0/100.0-1)*100:.4f}"
    )
