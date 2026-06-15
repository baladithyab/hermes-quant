"""ar13: portfolio-cap units bug — a TRUE-UNIT Position.quantity must be
converted to a NAV-fraction before it is used as gross-exposure input.

Root cause
----------
`Position.quantity` is unit-ambiguous depending on the fill path:

  * legacy / single-leg equity: quantity = NAV-FRACTION (signed target_pct,
    e.g. 0.05 = 5% of NAV).
  * ADR-0086/0088 true-unit path (reactor_metadata.quantity = leg_quantity):
    quantity = SIGNED CONTRACTS/SHARES (true units, e.g. 100 shares).

`PaperReactor._portfolio_cap_clip` historically read `position.quantity`
straight into `RiskPortfolioState.positions` (a symbol -> NAV-fraction map) and
into the de-risking guard's `existing`. When the book carries a true-unit line
(e.g. 100 shares), that 100 was treated as a 10000% NAV-fraction. Two failure
modes:

  * ar13 (fail-OPEN): the de-risk guard `abs(fill_size_pct) <= abs(existing)`
    is ALWAYS True against `existing=100`, so every re-fire on a true-unit-held
    symbol is waved through full-size — the ADR-0087 gross cap is DEFEATED.
  * over-count (fail-CLOSED): a fresh fire sees gross ~= 100 (10000%) and is
    silenced — legitimate fires suppressed.

These tests build a REAL paper-default book via apply_execution so the true-unit
path is exercised end-to-end, then drive the cap seam. They are RED on the
pre-fix code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import PaperReactor


def _make_proposal(symbol: str, *, decision_price: float = 150.0) -> Any:
    proposal = MagicMock()
    proposal.proposal_id = f"prop_{symbol}_units"
    proposal.symbol = symbol
    proposal.asset_class = "equity"
    proposal.timeframe = "1d"
    proposal.advisor_result = {
        "decision_price": decision_price,
        "as_of": "2026-05-27T10:00:00Z",
    }
    return proposal


def _seed_true_unit_equity(
    ps: Any,
    *,
    symbol: str,
    shares: float,
    fill_price: float,
    proposal_id: str,
    asof: str,
) -> None:
    """Apply a true-unit (ADR-0086/0088) equity fill: reactor_metadata.quantity
    carries SIGNED SHARES, so Position.quantity is stored in shares, not as a
    NAV-fraction. Mirrors react/multileg.py's CC equity child write path."""
    ps.apply_execution(
        {
            "proposal_id": proposal_id,
            "asof_execution": asof,
            "asset": symbol,
            "asset_class": "equity",
            "fill_price": fill_price,
            # NAV-fraction proxy is intentionally tiny/irrelevant on the true-unit
            # path; the position is tracked in shares via reactor_metadata.quantity.
            "fill_size_pct": 0.001,
            "reactor_name": "paper",
            "reactor_metadata": {"quantity": shares, "role": "leg"},
        }
    )


class TestPortfolioCapUnitsAr13:
    def test_true_unit_position_does_not_defeat_gross_cap_on_refire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ar13 (fail-OPEN): an exposure-ADDING re-fire on a symbol the book holds
        in TRUE UNITS (100 shares) must still be subject to the gross/cash cap.

        Pre-fix: the de-risk guard reads existing=100 (shares, mis-read as a
        10000% NAV-fraction) so abs(new_fill) <= abs(100) is ALWAYS true and the
        fire is waved through full-size — the cap is defeated.

        With the fix: 100 shares @ $150 on a $100k NAV ~= 15% gross. A re-fire
        that RAISES the symbol's exposure beyond ~15% is exposure-adding and must
        reach the cap machinery (silenced or scaled, never full-pass at the
        original size).
        """
        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
        monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        db_path = tmp_path / "state.db"
        ps_instance = DBPortfolioState(state_db_path=db_path)

        # Book: 100 shares of AAPL @ $150 = $15,000 notional on a $100k NAV
        # (~15% of NAV). Stored in TRUE UNITS via the leg_quantity path.
        _seed_true_unit_equity(
            ps_instance,
            symbol="AAPL",
            shares=100.0,
            fill_price=150.0,
            proposal_id="seed_aapl",
            asof="2026-05-27T09:00:00Z",
        )
        held = ps_instance.get_positions("paper-default")[("equity", "AAPL")]
        assert held.quantity == pytest.approx(100.0)  # shares, not 0.001

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("AAPL")

        with monkeypatch.context() as m:
            m.setattr(ps_mod, "_singleton", ps_instance)
            # A +0.90 re-fire on AAPL is a big exposure ADD relative to the held
            # ~15% gross contribution. It must NOT full-pass at 0.90.
            record = reactor.execute(proposal, fill_size_pct=0.90)

        rmeta = record.reactor_metadata or {}
        # The fix: this exposure-adding re-fire is clipped/silenced, NOT waved
        # through full-size. Pre-fix it full-passes at 0.90 (cap defeated).
        assert record.fill_size_pct != pytest.approx(0.90), (
            "ar13: true-unit held quantity (100 shares) defeated the de-risk "
            "guard and the gross cap was bypassed"
        )
        assert rmeta.get("silenced") or "cap_scaled_from" in rmeta

    def test_true_unit_position_does_not_inflate_gross_and_silence_new_symbol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """over-count (fail-CLOSED): a fresh fire on a NEW symbol must NOT be
        silenced merely because the book holds a true-unit line.

        Pre-fix: 100 shares is read as a 10000% NAV-fraction so gross ~= 100,
        g_room <= 0, and every subsequent fire is silenced.

        With the fix: 100 shares @ $150 / $100k NAV ~= 15% gross, so a modest 5%
        fire on GOOG has ample headroom and passes through.
        """
        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
        monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        db_path = tmp_path / "state.db"
        ps_instance = DBPortfolioState(state_db_path=db_path)

        _seed_true_unit_equity(
            ps_instance,
            symbol="AAPL",
            shares=100.0,
            fill_price=150.0,
            proposal_id="seed_aapl",
            asof="2026-05-27T09:00:00Z",
        )

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("GOOG")

        with monkeypatch.context() as m:
            m.setattr(ps_mod, "_singleton", ps_instance)
            record = reactor.execute(proposal, fill_size_pct=0.05)

        rmeta = record.reactor_metadata or {}
        # The fix: ~15% gross + 5% fire = 20% << 200% gross cap -> full pass.
        assert not rmeta.get("silenced"), (
            "over-count: true-unit held quantity (100 shares) inflated gross "
            "~100x and silenced a legitimate new-symbol fire"
        )
        assert record.fill_size_pct == pytest.approx(0.05)
        assert isinstance(record, ExecutionRecord)


class TestPortfolioCapUnitsByteIdentical:
    """The fix MUST be invisible when the flag is OFF and when the book holds
    only legacy NAV-fraction lines (no true-unit positions)."""

    def test_flag_off_does_not_touch_cap_or_nav(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With HERMES_QUANT_PORTFOLIO_CAPS unset, the cap seam (and the new
        NAV lookup) is never reached — byte-identical to the pre-ADR path even
        with a true-unit book present."""
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        _seed_true_unit_equity(
            ps_instance,
            symbol="AAPL",
            shares=100.0,
            fill_price=150.0,
            proposal_id="seed_aapl",
            asof="2026-05-27T09:00:00Z",
        )

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("AAPL")

        # If the cap seam (or its NAV lookup) is reached with the flag OFF, the
        # clip helper would fire — guardrail makes that loud.
        from unittest.mock import patch

        with patch(
            "hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom",
            side_effect=AssertionError("cap seam must not run when flag is OFF"),
        ):
            with monkeypatch.context() as m:
                m.setattr(ps_mod, "_singleton", ps_instance)
                record = reactor.execute(proposal, fill_size_pct=0.90)

        # Full-size fill lands untouched (flag-OFF byte-identical).
        assert record.fill_size_pct == pytest.approx(0.90)
        assert not (record.reactor_metadata or {}).get("silenced")

    def test_pure_nav_fraction_book_unchanged_over_gross(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A book of ONLY legacy NAV-fraction lines must behave EXACTLY as before
        the units fix: an over-gross new-symbol fire is silenced; the conversion
        helper returns those fractions verbatim (no NAV reweighting)."""
        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        # Two legacy equity fills (NO reactor_metadata.quantity) summing to 200%
        # gross — the classic over-gross book. Stored as NAV-fractions (1.0 each).
        for sym, asof in (("AAPL", "2026-05-27T09:00:00Z"), ("MSFT", "2026-05-27T09:01:00Z")):
            ps_instance.apply_execution(
                {
                    "proposal_id": f"seed_{sym}",
                    "asof_execution": asof,
                    "asset": sym,
                    "asset_class": "equity",
                    "fill_price": 150.0,
                    "fill_size_pct": 1.0,  # 100% long NAV-fraction
                    "reactor_name": "paper",
                    "reactor_metadata": {"paper": True},  # NO 'quantity' key
                }
            )
        held = ps_instance.get_positions("paper-default")
        assert held[("equity", "AAPL")].unit_kind == "nav_fraction"
        assert held[("equity", "AAPL")].quantity == pytest.approx(1.0)

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("GOOG")

        with monkeypatch.context() as m:
            m.setattr(ps_mod, "_singleton", ps_instance)
            record = reactor.execute(proposal, fill_size_pct=0.10)

        # gross = 1.0 + 1.0 = 200% (at the cap); a +10% exposure-adding GOOG fire
        # has no gross/cash headroom -> silenced. Same as pre-fix.
        rmeta = record.reactor_metadata or {}
        assert rmeta.get("silenced") is True
        assert (rmeta.get("silence_reason") or "").startswith("portfolio_cap_")
        assert record.fill_size_pct == pytest.approx(0.0)


class TestPositionGrossFractionHelper:
    """Direct unit tests for the unit-kind -> NAV-fraction conversion."""

    def test_nav_fraction_returned_verbatim(self) -> None:
        from hermes_quant.state.portfolio_state import position_gross_fraction
        from hermes_quant.state.positions import Position

        pos = Position(
            account_id="paper-default",
            asset_class="equity",
            symbol="AAPL",
            quantity=0.05,
            avg_entry_price=150.0,
            last_update_at="2026-05-27T09:00:00Z",
            unit_kind="nav_fraction",
        )
        # Verbatim regardless of NAV — legacy quantity IS the fraction.
        assert position_gross_fraction(pos, nav=100_000.0) == pytest.approx(0.05)
        assert position_gross_fraction(pos, nav=None) == pytest.approx(0.05)

    def test_marker_less_object_treated_as_nav_fraction(self) -> None:
        from hermes_quant.state.portfolio_state import position_gross_fraction

        class DummyPos:
            quantity = 1.0

        # A test-double / old caller with no unit_kind keeps legacy semantics.
        assert position_gross_fraction(DummyPos(), nav=100_000.0) == pytest.approx(1.0)

    def test_true_unit_equity_converted_to_nav_fraction(self) -> None:
        from hermes_quant.state.portfolio_state import position_gross_fraction
        from hermes_quant.state.positions import Position

        pos = Position(
            account_id="paper-default",
            asset_class="equity",
            symbol="AAPL",
            quantity=100.0,  # shares
            avg_entry_price=150.0,
            last_update_at="2026-05-27T09:00:00Z",
            unit_kind="true_unit",
        )
        # 100 * 150 / 100_000 = 0.15 (15% of NAV).
        assert position_gross_fraction(pos, nav=100_000.0) == pytest.approx(0.15)

    def test_true_unit_option_applies_100x_multiplier(self) -> None:
        from hermes_quant.state.portfolio_state import position_gross_fraction
        from hermes_quant.state.positions import Position

        pos = Position(
            account_id="paper-default",
            asset_class="us_option",
            symbol="NVDA260626C00160000",
            quantity=10.0,  # contracts
            avg_entry_price=4.5,  # per-contract premium
            last_update_at="2026-05-27T09:00:00Z",
            unit_kind="true_unit",
        )
        # 10 * 4.5 * 100 / 100_000 = 0.045 (4.5% of NAV; option multiplier 100).
        assert position_gross_fraction(pos, nav=100_000.0) == pytest.approx(0.045)

    def test_true_unit_sign_preserved(self) -> None:
        from hermes_quant.state.portfolio_state import position_gross_fraction
        from hermes_quant.state.positions import Position

        pos = Position(
            account_id="paper-default",
            asset_class="equity",
            symbol="AAPL",
            quantity=-100.0,  # short
            avg_entry_price=150.0,
            last_update_at="2026-05-27T09:00:00Z",
            unit_kind="true_unit",
        )
        assert position_gross_fraction(pos, nav=100_000.0) == pytest.approx(-0.15)

    def test_true_unit_fails_closed_on_bad_nav(self) -> None:
        from hermes_quant.state.portfolio_state import position_gross_fraction
        from hermes_quant.state.positions import Position

        pos = Position(
            account_id="paper-default",
            asset_class="equity",
            symbol="AAPL",
            quantity=100.0,
            avg_entry_price=150.0,
            last_update_at="2026-05-27T09:00:00Z",
            unit_kind="true_unit",
        )
        # No safe conversion -> preserve raw quantity (do NOT zero/hide the line).
        assert position_gross_fraction(pos, nav=None) == pytest.approx(100.0)
        assert position_gross_fraction(pos, nav=0.0) == pytest.approx(100.0)
        assert position_gross_fraction(pos, nav=float("nan")) == pytest.approx(100.0)


class TestUnitKindMigration:
    """The unit_kind column must be added idempotently to a pre-existing state.db
    whose positions table predates the ar13/ar14 fix (real deploy scenario)."""

    def test_legacy_positions_table_migrates_to_unit_kind(self, tmp_path: Path) -> None:
        import sqlite3

        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        db = tmp_path / "state.db"
        # Seed a LEGACY positions table (6 columns, NO unit_kind) with one NAV-
        # fraction row, exactly the pre-fix shape.
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE positions (
                account_id       TEXT NOT NULL,
                asset_class      TEXT NOT NULL,
                symbol           TEXT NOT NULL,
                quantity         REAL NOT NULL,
                avg_entry_price  REAL NOT NULL,
                last_update_at   TEXT NOT NULL,
                PRIMARY KEY (account_id, asset_class, symbol)
            ) WITHOUT ROWID;
            INSERT INTO positions VALUES
                ('paper-default', 'equity', 'AAPL', -0.20, 200.0, '2026-05-25T14:00:00Z');
            """
        )
        conn.commit()
        conn.close()

        # Constructing PortfolioState runs _init_schema -> _migrate_positions_unit_kind.
        ps = DBPortfolioState(state_db_path=db)

        with sqlite3.connect(db) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(positions)")}
            assert "unit_kind" in cols
            # The legacy row was backfilled with the nav_fraction default (its data
            # preserved), so it keeps its legacy NAV-fraction interpretation.
            row = c.execute(
                "SELECT quantity, unit_kind FROM positions WHERE symbol='AAPL'"
            ).fetchone()
            assert row[0] == pytest.approx(-0.20)
            assert row[1] == "nav_fraction"

        # get_positions surfaces the marker; a re-construction is idempotent.
        held = ps.get_positions("paper-default")[("equity", "AAPL")]
        assert held.unit_kind == "nav_fraction"
        DBPortfolioState(state_db_path=db)  # second init must be a no-op (no error)
