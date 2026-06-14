from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from unittest.mock import MagicMock, patch

from hermes_quant.react.paper import PaperReactor
from hermes_quant.react.base import ExecutionRecord


def _make_proposal(symbol: str = "AAPL") -> Any:
    proposal = MagicMock()
    proposal.proposal_id = "prop_cap_001"
    proposal.symbol = symbol
    proposal.asset_class = "equity"
    proposal.timeframe = "1d"
    proposal.advisor_result = {
        "decision_price": 150.0,
        "as_of": "2026-05-27T10:00:00Z",
    }
    return proposal


class TestPaperReactorPortfolioCap:
    def test_flag_off_is_bit_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HERMES_QUANT_PORTFOLIO_CAPS unset, execute() must not touch the cap seam."""

        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        # Guardrail: if the cap helper is ever invoked with flag OFF, this test fails.
        with patch(
            "hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom",
            side_effect=AssertionError("clip_one_to_remaining_headroom should not be called when flag is OFF"),
        ):
            reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
            proposal = _make_proposal()

            record = reactor.execute(proposal, fill_size_pct=0.05)

        assert isinstance(record, ExecutionRecord)
        assert record.asset == "AAPL"
        assert record.fill_size_pct == pytest.approx(0.05)
        assert not (record.reactor_metadata or {}).get("silenced")

    def test_cap_silences_over_gross(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With flag ON and a book at 200% gross, a new exposure-ADDING fire is
        silenced at the seam.

        NB: the fire must be in a symbol with no offsetting existing position
        (a genuinely exposure-adding fill). A reduce-toward-flat fire in AAPL or
        MSFT is de-risking and now (correctly) passes through unclipped, so we
        fire a NEW symbol (GOOG) to exercise the over-gross silence path.
        """

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("GOOG")

        import hermes_quant.state.portfolio_state as ps_mod

        # Build a minimal PortfolioState instance and seed it with 200% gross exposure.
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")

        class DummyPos:
            def __init__(self, quantity: float) -> None:
                self.quantity = quantity

        # positions maps (asset_class, symbol) -> position
        ps_instance.get_positions = MagicMock(
            return_value={
                ("equity", "AAPL"): DummyPos(1.0),
                ("equity", "MSFT"): DummyPos(1.0),
            }
        )

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.10)

        # Silenced record: no position-moving fill appended, but an audit trail is returned.
        assert (record.reactor_metadata or {}).get("paper") is True
        assert (record.reactor_metadata or {}).get("silenced") is True
        silence_reason = (record.reactor_metadata or {}).get("silence_reason", "")
        assert silence_reason.startswith("portfolio_cap_")
        assert record.fill_size_pct == pytest.approx(0.0)

        # PortfolioState.apply_execution should not see a position-moving fill
        positions_after = ps_instance.get_positions("paper-default")
        # Our fake get_positions is still wired, but quantity should be unchanged (no new GOOG line)
        assert positions_after[("equity", "AAPL")].quantity == pytest.approx(1.0)
        assert positions_after[("equity", "MSFT")].quantity == pytest.approx(1.0)

    def test_cap_passes_with_headroom(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With flag ON and an empty book, the cap seam passes the fill through."""

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("AAPL")

        import hermes_quant.state.portfolio_state as ps_mod

        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        # Empty book: get_positions returns empty dict
        ps_instance.get_positions = MagicMock(return_value={})

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.05)

        assert isinstance(record, ExecutionRecord)
        assert record.asset == "AAPL"
        assert record.fill_size_pct == pytest.approx(0.05)
        assert not (record.reactor_metadata or {}).get("silenced")

        # PortfolioState should have applied the execution and now reflect the position
        positions = ps_instance.get_positions("paper-default")
        # Our MagicMock get_positions still returns {}, so instead rely on apply_execution side effects
        # by reading positions via the real method on a fresh instance reconstructed from executions.
        fresh_ps = DBPortfolioState(state_db_path=ps_instance.db_path)
        fresh_positions = fresh_ps.get_positions("paper-default")
        assert ("equity", "AAPL") in fresh_positions
        assert fresh_positions[("equity", "AAPL")].quantity == pytest.approx(0.05)

    def test_cap_scales_partial_headroom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0087 acceptance gate: a fire that fits PARTIALLY is scaled to the
        remaining headroom, a real fill lands at the clipped size, and
        cap_scaled_from/to/factor are recorded in reactor_metadata.

        Book setup: a single +0.75 long position. Under PortfolioCaps.standard()
        (200% gross / 100% net / 20% cash), the binding cap is the cash sleeve:
        cash_headroom = (1.0 - 0.20) - 0.75 = 0.05. A +0.20 fire therefore clips
        to +0.05 (scale_factor = 0.25). Net cap is not binding
        (prospective_net = 0.75 + 0.20 = 0.95 <= 1.0).
        """

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("NVDA")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")

        class DummyPos:
            def __init__(self, quantity: float) -> None:
                self.quantity = quantity

        # Existing book: +0.75 long in AAPL -> gross=0.75, net=+0.75.
        # cash_headroom = 0.8 - 0.75 = 0.05; gross_headroom = 2.0 - 0.75 = 1.25.
        # The min (cash) binds at 0.05.
        ps_instance.get_positions = MagicMock(
            return_value={
                ("equity", "AAPL"): DummyPos(0.75),
            }
        )

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.20)

        # A REAL fill landed (not silenced) at the clipped, scaled-down size.
        assert isinstance(record, ExecutionRecord)
        assert record.asset == "NVDA"
        rmeta = record.reactor_metadata or {}
        assert not rmeta.get("silenced")
        assert record.fill_size_pct == pytest.approx(0.05)
        assert record.target_position_pct == pytest.approx(0.05)

        # Cap audit trail surfaced in reactor_metadata.
        assert rmeta.get("cap_scaled_from") == pytest.approx(0.20)
        assert rmeta.get("cap_scaled_to") == pytest.approx(0.05)
        assert rmeta.get("cap_scale_factor") == pytest.approx(0.25)

        # The scaled fill is a position-moving fill: it was appended to the bus
        # and applied to PortfolioState at the CLIPPED size, not the original.
        fresh_ps = DBPortfolioState(state_db_path=ps_instance.db_path)
        fresh_positions = fresh_ps.get_positions("paper-default")
        assert ("equity", "NVDA") in fresh_positions
        assert fresh_positions[("equity", "NVDA")].quantity == pytest.approx(0.05)

    def test_cap_full_pass_records_no_scale_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: a fire that fits ENTIRELY passes through unscaled and
        records NO cap_scaled_* metadata (full-pass path unchanged)."""

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("AAPL")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        # Empty book -> full headroom -> a 5% fire fits entirely (scale_factor=1.0).
        ps_instance.get_positions = MagicMock(return_value={})

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.05)

        rmeta = record.reactor_metadata or {}
        assert record.fill_size_pct == pytest.approx(0.05)
        assert not rmeta.get("silenced")
        # Full pass: no scale audit keys present.
        assert "cap_scaled_from" not in rmeta
        assert "cap_scaled_to" not in rmeta
        assert "cap_scale_factor" not in rmeta

    def test_cap_derisking_partial_sell_passes_unclipped_on_capped_book(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1 trade-correctness fix (Codex example): a DE-RISKING fill on a book
        with essentially no remaining headroom must execute at its FULL requested
        size, never clipped by the headroom cap.

        Book: AAPL existing target = +0.75 -> gross=0.75, cash_headroom = 0.05.
        An approved AAPL fill that moves the target to -0.20 (partial sell that
        flips toward flat) REDUCES this symbol's gross contribution from 0.75 to
        0.20 -> it FREES headroom. The pre-fix code clipped -0.20 down to -0.05.
        The fix passes -0.20 through unclipped and never consults the clip helper.
        """

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("AAPL")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")

        class DummyPos:
            def __init__(self, quantity: float) -> None:
                self.quantity = quantity

        ps_instance.get_positions = MagicMock(
            return_value={("equity", "AAPL"): DummyPos(0.75)}
        )

        # Guardrail: de-risking fast-path must short-circuit BEFORE the clip
        # helper is consulted. If the helper is called, this test fails loudly.
        with patch(
            "hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom",
            side_effect=AssertionError(
                "clip_one_to_remaining_headroom must not be called for a de-risking fill"
            ),
        ):
            with patch.object(ps_mod, "_singleton", ps_instance):
                record = reactor.execute(proposal, fill_size_pct=-0.20)

        assert isinstance(record, ExecutionRecord)
        assert record.asset == "AAPL"
        rmeta = record.reactor_metadata or {}
        assert not rmeta.get("silenced")
        assert record.fill_size_pct == pytest.approx(-0.20)
        assert record.target_position_pct == pytest.approx(-0.20)
        assert "cap_scaled_from" not in rmeta
        assert "cap_scaled_to" not in rmeta
        assert "cap_scale_factor" not in rmeta

    def test_cap_derisking_shrink_same_sign_passes_unclipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """De-risking variant: a same-sign SHRINK toward zero (not a flip) on a
        nearly-full book also passes through unclipped.

        Book: AAPL +0.75 (cash_headroom = 0.05). A fill reducing the target to
        +0.30 lowers gross from 0.75 to 0.30 -> exposure-reducing -> full pass.
        """

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("AAPL")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")

        class DummyPos:
            def __init__(self, quantity: float) -> None:
                self.quantity = quantity

        ps_instance.get_positions = MagicMock(
            return_value={("equity", "AAPL"): DummyPos(0.75)}
        )

        with patch(
            "hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom",
            side_effect=AssertionError(
                "clip helper must not be called for a same-sign shrink (de-risking)"
            ),
        ):
            with patch.object(ps_mod, "_singleton", ps_instance):
                record = reactor.execute(proposal, fill_size_pct=0.30)

        assert record.fill_size_pct == pytest.approx(0.30)
        assert not (record.reactor_metadata or {}).get("silenced")

    def test_cap_does_not_collapse_same_symbol_cross_asset_class(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cs60: the cap pos_map must key on (asset_class, symbol), not the bare
        symbol, so two DISTINCT positions that share an underlying symbol but
        differ in asset_class are accounted SEPARATELY.

        Book: an equity AAPL at +0.40 NAV AND a us_option AAPL at +0.40 NAV.
        These are two distinct canonical positions keyed
        (equity, AAPL) and (us_option, AAPL).

          * Correct accounting (separate buckets): gross = 0.80.
            cash_headroom = (1.0 - 0.20) - 0.80 = 0.0. A new exposure-ADDING
            GOOG +0.10 fire has NO headroom -> it is SILENCED.
          * Buggy collapse (bare-symbol key): the second AAPL line overwrites
            the first, so gross is mis-summed to 0.40, cash_headroom = 0.40, and
            the GOOG fire FULL-PASSES at +0.10 — the cap UNDER-counts the gross
            exposure of a same-symbol cross-asset-class book and admits a fire it
            should have silenced.

        net cap is not the binding constraint either way (prospective net in the
        buggy case is 0.40 + 0.10 = 0.50 <= 1.0).
        """

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("GOOG")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")

        class DummyPos:
            def __init__(self, quantity: float) -> None:
                self.quantity = quantity

        # Two distinct positions sharing the underlying "AAPL" symbol but in
        # different asset classes. A bare-symbol pos_map collapses these into one
        # bucket (the second overwrites the first); the canonical
        # (asset_class, symbol) key keeps them separate.
        ps_instance.get_positions = MagicMock(
            return_value={
                ("equity", "AAPL"): DummyPos(0.40),
                ("us_option", "AAPL"): DummyPos(0.40),
            }
        )

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.10)

        # Correct accounting: gross is 0.80, cash_headroom is 0.0 -> the new
        # GOOG fire is silenced. Under the bare-symbol collapse it would
        # full-pass at +0.10.
        rmeta = record.reactor_metadata or {}
        assert rmeta.get("silenced") is True, (
            "same-symbol cross-asset-class gross was under-counted "
            f"(record={record.fill_size_pct}, meta={rmeta})"
        )
        silence_reason = rmeta.get("silence_reason", "")
        assert silence_reason.startswith("portfolio_cap_")
        assert record.fill_size_pct == pytest.approx(0.0)

    def test_cap_adding_into_same_symbol_still_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invariant guard: increasing abs(target) on an EXISTING position is an
        exposure-ADDING fill and must still be subject to the headroom cap (the
        de-risking fast-path must NOT swallow it).

        Book: AAPL +0.75 (gross=0.75, cash_headroom=0.05). A fill raising the
        target to +0.90 ADDS gross (abs 0.90 > abs 0.75). The clip helper is
        reached and the fill does NOT full-pass at 0.90 (cash headroom only 0.05).
        """

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("AAPL")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")

        class DummyPos:
            def __init__(self, quantity: float) -> None:
                self.quantity = quantity

        ps_instance.get_positions = MagicMock(
            return_value={("equity", "AAPL"): DummyPos(0.75)}
        )

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.90)

        # Exposure-adding fill reached the cap machinery; it must NOT full-pass.
        assert record.fill_size_pct != pytest.approx(0.90)
        rmeta = record.reactor_metadata or {}
        # Either silenced (clip to ~0) or partially scaled; both prove the adding
        # path is still active.
        assert rmeta.get("silenced") or "cap_scaled_from" in rmeta
