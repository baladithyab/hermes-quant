"""ar32/ar33 — two fail-OPEN defects the seventh convergence review (wf w38a7sxzw) RED-verified.

  ar32 — DeterministicEquityReactor (now-LIVE: HERMES_QUANT_DETERMINISTIC_EQUITY=1) booked a
         NaN-priced, NaN-qty fill because execute() had NO finite-guard on decision_price (the
         cr05 guard the sibling PaperReactor has). The later `price_for_qty <= 0` check does not
         catch NaN. Fix: cr05-style finite-guard -> fail-closed no-fill.
  ar33 — validate_semantic_packet: a NaT asof (pd.Timestamp('') / None) bypassed BOTH the
         future_packet and stale_packet freshness gates (NaT comparisons are all False), admitting
         an unknowable-age signal. Fix: pd.isna(packet_asof) -> invalid_asof.
"""
from __future__ import annotations

import pytest

import hermes_quant.react.deterministic_equity as det_mod
from hermes_quant.proposals import Proposal
from hermes_quant.react.backend import FillResult
from hermes_quant.react.deterministic_equity import DeterministicEquityReactor


# --------------------------------------------------------------------------- #
# ar32 — deterministic-equity reactor rejects a non-finite decision_price
# --------------------------------------------------------------------------- #
class _FakeBackend:
    name = "deterministic"

    def __init__(self):
        self.submitted: list[dict] = []

    def account_equity(self):
        return 100_000.0

    def buying_power(self):
        return 100_000.0

    def submit_equity(self, *, symbol, signed_qty, decision_price, client_order_id):
        self.submitted.append({"symbol": symbol, "signed_qty": signed_qty, "decision_price": decision_price})
        return FillResult(
            symbol=symbol, filled_avg_price=decision_price, filled_qty=float(signed_qty),
            status="filled", position_intent="buy_to_open", order_id="x", source=self.name,
        )


class _CapturePS:
    def __init__(self):
        self.applied: list[dict] = []

    def apply_execution(self, record):
        self.applied.append(record)


def _proposal(*, decision_price):
    return Proposal(
        proposal_id="prop_2026-06-05T00:00:00_AAPL_abc123",
        state="pending", symbol="AAPL", asset_class="equity", timeframe="1d",
        created_at="2026-06-05T00:00:00Z", expires_at="2026-06-05T01:00:00Z",
        advisor_result={"as_of": "2026-06-05T00:00:00Z", "decision_price": decision_price, "signal_id": "sig-1"},
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("HERMES_QUANT_ADMISSIBILITY", "HERMES_QUANT_PORTFOLIO_CAPS",
                "HERMES_QUANT_BROKER_BACKEND", "HERMES_QUANT_ALPACA_PAPER",
                "HERMES_QUANT_DETERMINISTIC_EQUITY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")


def _reactor(tmp_path, monkeypatch):
    backend = _FakeBackend()
    ps = _CapturePS()
    monkeypatch.setattr(det_mod, "select_backend", lambda *a, **kw: backend)
    import hermes_quant.state.portfolio_state as ps_mod
    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: ps)
    r = DeterministicEquityReactor(executions_path=tmp_path / "executions.jsonl")
    return r, backend, ps


@pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), 0.0, -5.0])
def test_ar32_nonfinite_decision_price_is_failclosed_nofill(tmp_path, monkeypatch, bad_price):
    reactor, backend, ps = _reactor(tmp_path, monkeypatch)
    rec = reactor.execute(_proposal(decision_price=bad_price), fill_size_pct=0.10)  # MUST NOT raise
    assert rec.fill_size_pct == 0.0, f"ar32: a {bad_price!r} price booked a non-zero fill"
    assert (rec.reactor_metadata or {}).get("no_fill") is True
    # The backend was NEVER asked to fill a NaN/inf-priced order, and no position applied.
    assert backend.submitted == [], "ar32: a non-finite price reached the backend submit"
    assert ps.applied == [], "ar32: a non-finite-price fill mutated the book"
    # And no NaN price was recorded on the bus.
    import math
    assert math.isfinite(rec.fill_price) or rec.fill_price == 0.0


def test_ar32_finite_price_still_fills(tmp_path, monkeypatch):
    """Byte-identical on the healthy path: a finite price fills normally."""
    reactor, backend, ps = _reactor(tmp_path, monkeypatch)
    rec = reactor.execute(_proposal(decision_price=100.0), fill_size_pct=0.10)
    assert rec.fill_size_pct == pytest.approx(0.10)
    assert (rec.reactor_metadata or {}).get("no_fill") is not True
    assert len(backend.submitted) == 1


# --------------------------------------------------------------------------- #
# ar33 — validate_semantic_packet rejects a NaT / empty / None asof
# --------------------------------------------------------------------------- #
def _build_packet(asof_value):
    from hermes_quant.semantic import SemanticPacket
    return SemanticPacket(
        schema_version=1,
        asset="AAPL",
        asof=asof_value,
        horizon="1d",
        stance="bullish",
        confidence=0.7,
        magnitude=0.5,
        summary="strong guidance raise",
        packet_hash=None,
    )


@pytest.mark.parametrize("bad_asof", ["", None, "not-a-date"])
def test_ar33_unparseable_or_empty_asof_rejected(bad_asof):
    import pandas as pd

    from hermes_quant.semantic import validate_semantic_packet

    pkt = _build_packet(bad_asof)
    ok, reason = validate_semantic_packet(
        pkt, asset="AAPL", asof=pd.Timestamp("2026-06-05T00:00:00Z"), verify_hash=False
    )
    assert ok is False, f"ar33: an unknowable-age asof ({bad_asof!r}) was ADMITTED ({reason})"
    assert reason == "invalid_asof"


def test_ar33_fresh_asof_still_valid():
    import pandas as pd

    from hermes_quant.semantic import validate_semantic_packet

    pkt = _build_packet("2026-06-05T00:00:00Z")
    ok, reason = validate_semantic_packet(
        pkt, asset="AAPL", asof=pd.Timestamp("2026-06-05T00:30:00Z"), verify_hash=False
    )
    assert ok is True, f"ar33: a fresh packet was wrongly rejected ({reason})"


def test_ar33_stale_and_future_still_rejected():
    import pandas as pd

    from hermes_quant.semantic import validate_semantic_packet

    # Stale: packet 3 days before context, default max_age 24h.
    stale = _build_packet("2026-06-01T00:00:00Z")
    ok, reason = validate_semantic_packet(
        stale, asset="AAPL", asof=pd.Timestamp("2026-06-05T00:00:00Z"), verify_hash=False
    )
    assert ok is False and reason == "stale_packet"
    # Future: packet after context.
    fut = _build_packet("2026-06-06T00:00:00Z")
    ok, reason = validate_semantic_packet(
        fut, asset="AAPL", asof=pd.Timestamp("2026-06-05T00:00:00Z"), verify_hash=False
    )
    assert ok is False and reason == "future_packet"
