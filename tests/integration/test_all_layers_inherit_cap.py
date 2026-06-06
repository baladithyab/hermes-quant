"""ADR-0087 acceptance gate — all firing layers inherit the portfolio cap.

This is the integration test the 2026-06-02 incident lacked. The incident root
cause was that the portfolio cap (ADR-0071: 200% gross / 100% net / 20% cash) was
wired into only ONE of the four trade-firing layers (autonomous-tick), so the
advisor layer stacked the paper book to 41.6x gross. ADR-0087 centralizes the cap
at the ``PaperReactor.execute()`` seam so that EVERY firing layer inherits it "by
construction" — they all pass through that one chokepoint.

This test proves (or DISPROVES) that claim for the four documented firing layers:

  - advisor    : ops/scripts/quant-daily-interim.py::auto_approve_actionables
                 -> hermes_quant.tools.quant_approve
                 -> hermes_quant.react.dispatch.select_reactor
                 -> PaperReactor.execute(play_tag="advisor")          [SEAM]
  - autonomous : hermes_quant/autonomous.py::_react
                 -> PaperReactor.execute(play_tag="autonomous")       [SEAM]
  - playbook   : ops/scripts/quant-playbook-tick.py::process_pair
                 -> place_paper_market_order  -> Alpaca REST /orders  [BYPASSES SEAM]
  - hourly     : ops/scripts/quant-hourly-tick.py::maybe_run_autonomous_phase
                 -> quant-playbook-tick.run_tick (same bypass path)   [BYPASSES SEAM]

CRITICAL FINDING (encoded as tests below, NOT papered over):
  The "all layers inherit the cap by construction" premise of ADR-0087 is
  CURRENTLY FALSE on `main` for TWO of the four layers. The **playbook** and
  **hourly** layers do NOT route fills through ``PaperReactor.execute`` at all —
  they call ``place_paper_market_order`` which POSTs directly to the Alpaca REST
  ``/orders`` endpoint, never touching the reactor seam (and therefore never the
  ADR-0087 cap precondition). The hourly layer is a thin wrapper that imports and
  calls the playbook tick's ``run_tick``, so it inherits the same bypass.

  ``hermes_quant.react.paper`` is never imported by quant-playbook-tick.py.

  This is exactly the failure mode ADR-0087 was written to make structurally
  impossible. Until those two layers are re-pointed through the reactor seam (or
  the cap is otherwise enforced on their REST path), centralizing the clip at
  ``execute()`` does NOT cap playbook/hourly fires. See the
  ``test_critical_finding_*`` cases below, which assert the bypass explicitly so
  this fact is loud and regression-guarded.

What IS proven green here:
  1. The SEAM enforces the cap for every ``play_tag`` (advisor/playbook/hourly/
     autonomous) — i.e. ANY fill that goes through ``execute`` into a full book is
     silenced when the flag is on, and passes when the flag is off. This is the
     "by construction" property for layers that actually use the seam.
  2. The advisor and autonomous layers' real reactor-call helpers
     (``quant_approve`` and ``autonomous._react``) DO drive ``PaperReactor.execute``
     — verified by instrumenting the seam and exercising the helper.

No real ~/.hermes/quant data is touched: every state.db / executions.jsonl lives
under tmp_path, and PortfolioState is injected via the module singleton (matching
tests/unit/test_paper_reactor_cap.py fixture style). No production code is modified
and no network call is made.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import PaperReactor

REPO_ROOT = Path(__file__).resolve().parents[2]

# The four documented firing layers (ADR-0087 acceptance gate enumerates them).
ALL_FIRING_LAYERS = ["advisor", "playbook", "hourly", "autonomous"]


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/unit/test_paper_reactor_cap.py
# ---------------------------------------------------------------------------


def _make_proposal(symbol: str = "AAPL") -> Any:
    """A minimal pending-equity proposal stand-in, as the unit cap test uses."""
    proposal = MagicMock()
    proposal.proposal_id = "prop_alllayers_001"
    proposal.symbol = symbol
    proposal.asset_class = "equity"
    proposal.timeframe = "1d"
    proposal.reactor_metadata = None  # account resolves to "paper-default"
    proposal.advisor_result = {
        "decision_price": 150.0,
        "as_of": "2026-06-02T10:00:00Z",
    }
    return proposal


def _full_book_portfolio_state(tmp_path: Path):
    """Build a DB-backed PortfolioState seeded to a 200% gross book (cap-breached).

    Two equity lines at 1.0 NAV-fraction each => gross 200%, cash sleeve fully
    consumed (cash_pct = 1 - 2.0 = -1.0 < the 20% reserve). Any new fire must be
    silenced at the seam.
    """
    from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

    class DummyPos:
        def __init__(self, quantity: float) -> None:
            self.quantity = quantity

    ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
    ps_instance.get_positions = MagicMock(
        return_value={
            ("equity", "AAPL"): DummyPos(1.0),
            ("equity", "MSFT"): DummyPos(1.0),
        }
    )
    return ps_instance


# ---------------------------------------------------------------------------
# (a) The SEAM enforces the cap for EVERY play_tag — the "by construction"
#     property. Parametrized over all four firing layers' play_tags.
# ---------------------------------------------------------------------------


class TestSeamCapsEveryLayer:
    @pytest.mark.parametrize("play_tag", ALL_FIRING_LAYERS)
    def test_full_book_silences_when_flag_on(
        self, play_tag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON + full book => the seam silences the fire, regardless of layer.

        This is the coverage the incident lacked: a new fire into a maxed book is
        refused at the single chokepoint, so any layer that routes through
        ``execute`` inherits the cap. We tag the fill with each layer's play_tag to
        demonstrate the seam does not special-case the source.
        """
        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("NVDA")

        import hermes_quant.state.portfolio_state as ps_mod

        ps_instance = _full_book_portfolio_state(tmp_path)
        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(
                proposal, fill_size_pct=0.10, play_tag=play_tag
            )

        meta = record.reactor_metadata or {}
        assert meta.get("silenced") is True, (
            f"layer={play_tag}: fire into a full book was NOT silenced by the "
            f"seam cap (flag ON) — this is the incident failure mode"
        )
        assert str(meta.get("silence_reason", "")).startswith("portfolio_cap_")
        assert record.fill_size_pct == pytest.approx(0.0)
        # play_tag is carried onto the silenced audit record (source attribution).
        assert record.play_tag == play_tag

        # No position-moving fill was written to the bus.
        bus_text = (tmp_path / "executions.jsonl").read_text()
        assert bus_text == "", (
            f"layer={play_tag}: a silenced fire still wrote to executions.jsonl"
        )

    @pytest.mark.parametrize("play_tag", ALL_FIRING_LAYERS)
    def test_full_book_NOT_capped_when_flag_off(
        self, play_tag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag OFF (default) => the SAME full-book fire is NOT silenced by the cap.

        Proves default-off safety: with HERMES_QUANT_PORTFOLIO_CAPS unset, the cap
        seam is a bit-identical no-op for every layer and the fill proceeds. (A
        guardrail asserts the cap primitive is never even consulted.)
        """
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("NVDA")

        import hermes_quant.state.portfolio_state as ps_mod

        ps_instance = _full_book_portfolio_state(tmp_path)

        with patch(
            "hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom",
            side_effect=AssertionError(
                "cap primitive must NOT be consulted when flag is OFF"
            ),
        ):
            with patch.object(ps_mod, "_singleton", ps_instance):
                record = reactor.execute(
                    proposal, fill_size_pct=0.10, play_tag=play_tag
                )

        meta = record.reactor_metadata or {}
        assert not meta.get("silenced"), (
            f"layer={play_tag}: cap silenced a fire with the flag OFF — "
            f"default-off safety violated"
        )
        assert record.fill_size_pct == pytest.approx(0.10)
        assert record.play_tag == play_tag


# ---------------------------------------------------------------------------
# (b) The advisor + autonomous layers' REAL reactor-call helpers drive the seam.
#     We instrument PaperReactor.execute and exercise each layer's cheap entry
#     point, asserting the seam was hit (so the cap they inherit is the seam cap).
# ---------------------------------------------------------------------------


class TestLayersDriveTheSeam:
    def test_autonomous_layer_fires_through_paper_reactor_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """autonomous._react -> PaperReactor.execute (the seam). Directly exercised.

        We spy on ``PaperReactor.execute`` and call the autonomous fire helper with
        a synthetic watchlist entry + advisor_result. The spy proves the autonomous
        layer's fill goes through the seam (and thus inherits the ADR-0087 cap).
        """
        from hermes_quant import autonomous as auto_mod
        from hermes_quant.watchlist import WatchlistEntry

        seam_calls: list[dict[str, Any]] = []
        real_execute = PaperReactor.execute

        def _spy_execute(self, proposal, **kwargs):  # type: ignore[no-untyped-def]
            seam_calls.append({"symbol": proposal.symbol, "kwargs": kwargs})
            # Return a benign record without touching the real bus/state.
            return ExecutionRecord(
                proposal_id=proposal.proposal_id,
                signal_id=None,
                asset=proposal.symbol,
                asset_class=proposal.asset_class,
                timeframe=proposal.timeframe,
                asof_decision="2026-06-02T10:00:00Z",
                asof_execution="2026-06-02T10:00:00Z",
                target_position_pct=kwargs.get("fill_size_pct", 0.0),
                decision_price=150.0,
                fill_price=150.0,
                fill_size_pct=kwargs.get("fill_size_pct", 0.0),
                reactor_name="paper",
                human_in_the_loop=True,
                approver_user_id=kwargs.get("approver_user_id"),
                reactor_metadata={"paper": True},
                bar_ts=None,
                play_tag=kwargs.get("play_tag", "advisor"),
            )

        entry = WatchlistEntry(symbol="AAPL", asset_class="equity", timeframe="1d")
        advisor_result = {"decision_price": 150.0, "as_of": "2026-06-02T10:00:00Z"}

        with patch.object(PaperReactor, "execute", _spy_execute):
            auto_mod._react(advisor_result, entry, fill_size_pct=0.05)

        assert len(seam_calls) == 1, (
            "autonomous layer did NOT route its fill through PaperReactor.execute"
        )
        assert seam_calls[0]["symbol"] == "AAPL"
        assert seam_calls[0]["kwargs"].get("play_tag") == "autonomous"
        assert real_execute is PaperReactor.execute  # spy cleanly removed

    def test_advisor_layer_fires_through_paper_reactor_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """advisor (quant_approve) -> select_reactor -> PaperReactor.execute (seam).

        We seed a real pending proposal into a tmp proposal store, point the default
        store at it, instrument ``PaperReactor.execute``, and call ``quant_approve``.
        The spy proves the advisor approve path drives the seam.
        """
        # Pin reactor selection to the legacy PaperReactor this test spies on.
        # select_reactor() routes to DeterministicEquityReactor when
        # HERMES_QUANT_DETERMINISTIC_EQUITY=1 (or AlpacaPaperReactor when
        # ALPACA_PAPER=1), and those flags can leak in from the ambient
        # environment (.env / cron wrappers set DETERMINISTIC_EQUITY=1 live).
        # Without this isolation the spy on PaperReactor.execute sees 0 calls.
        monkeypatch.setenv("HERMES_QUANT_DETERMINISTIC_EQUITY", "0")
        monkeypatch.setenv("HERMES_QUANT_ALPACA_PAPER", "0")
        monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "0")

        import hermes_quant.proposals as proposals_mod
        from hermes_quant.proposals import ProposalStore

        store = ProposalStore(
            bus_path=tmp_path / "proposals.jsonl",
            db_path=tmp_path / "proposals.db",
        )
        proposal = store.propose(
            symbol="AAPL",
            asset_class="equity",
            timeframe="1d",
            advisor_result={
                "decision_price": 150.0,
                "as_of": "2026-06-02T10:00:00Z",
                "risk_gate": {"kelly_fraction": 0.05},
            },
        )

        seam_calls: list[dict[str, Any]] = []

        def _spy_execute(self, prop, **kwargs):  # type: ignore[no-untyped-def]
            seam_calls.append({"symbol": prop.symbol, "kwargs": kwargs})
            return ExecutionRecord(
                proposal_id=prop.proposal_id,
                signal_id=None,
                asset=prop.symbol,
                asset_class=prop.asset_class,
                timeframe=prop.timeframe,
                asof_decision="2026-06-02T10:00:00Z",
                asof_execution="2026-06-02T10:00:00Z",
                target_position_pct=kwargs.get("fill_size_pct", 0.0),
                decision_price=150.0,
                fill_price=150.0,
                fill_size_pct=kwargs.get("fill_size_pct", 0.0),
                reactor_name="paper",
                human_in_the_loop=True,
                approver_user_id=kwargs.get("approver_user_id"),
                reactor_metadata={"paper": True},
                bar_ts=None,
                play_tag=kwargs.get("play_tag", "advisor"),
            )

        import hermes_quant.tools as _tools_mod
        from hermes_quant.tools import quant_approve

        # quant_approve is HITL-gated (quant.pdr.mode=hitl); force that mode so
        # the approve path actually drives the reactor seam in this test rather
        # than short-circuiting with a mode_mismatch under the default 'advise'.
        monkeypatch.setattr(_tools_mod, "_read_pdr_mode", lambda: "hitl")

        with patch.object(proposals_mod, "get_default_store", return_value=store):
            with patch.object(PaperReactor, "execute", _spy_execute):
                quant_approve({"proposal_id": proposal.proposal_id})

        assert len(seam_calls) == 1, (
            "advisor approve path did NOT route its fill through "
            "PaperReactor.execute (select_reactor seam)"
        )
        assert seam_calls[0]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# (c) CRITICAL FINDING — playbook + hourly BYPASS the seam.
#     These are NOT skips and NOT papered over: they assert the bypass exists so
#     the ADR-0087 "by construction" claim is shown to be currently FALSE for
#     these two layers, and so a future fix (re-pointing them at the seam) trips
#     these guards and forces this test to be updated alongside the fix.
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestCriticalFindingPlaybookHourlyBypassSeam:
    """The playbook + hourly layers fire via Alpaca REST, NOT the reactor seam.

    NOTE (post-#61, ADR-0087): the leverage-runaway exposure this finding
    surfaced has since been MITIGATED at the script level — quant-playbook-tick.py
    gained an aggregate tick-notional cap (build_aggregate_tick_budget, default-OFF
    behind HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP, armed in the cron wrappers). That
    cap lives in the SCRIPT, not the reactor seam, so the structural assertions
    below still hold: these layers do not route fills through PaperReactor.execute.
    The durable ADR-0087 fix (re-pointing them at the seam) remains future work;
    when it lands, these guards trip and this test must be updated.
    """

    PLAYBOOK = REPO_ROOT / "ops" / "scripts" / "quant-playbook-tick.py"
    HOURLY = REPO_ROOT / "ops" / "scripts" / "quant-hourly-tick.py"

    def test_scripts_exist(self) -> None:
        assert self.PLAYBOOK.is_file(), f"missing {self.PLAYBOOK}"
        assert self.HOURLY.is_file(), f"missing {self.HOURLY}"

    def test_playbook_layer_does_not_import_or_use_paper_reactor_seam(self) -> None:
        """CRITICAL: playbook fires via Alpaca REST, NOT PaperReactor.execute.

        The playbook tick's fire path is process_pair() -> place_paper_market_order(),
        which POSTs to the Alpaca /orders REST endpoint. It never imports the reactor
        and never calls execute(), so the ADR-0087 seam cap CANNOT apply to it. If
        this layer is ever re-pointed at the reactor seam (the ADR-0087 fix), this
        assertion will fail and this test must be updated to exercise the new path.
        """
        src = _read(self.PLAYBOOK)

        assert "place_paper_market_order" in src, (
            "playbook fire helper changed — re-audit the cap inheritance path"
        )
        assert "PaperReactor" not in src, (
            "playbook NOW references PaperReactor — if it routes fills through the "
            "seam, ADR-0087 cap inheritance may now hold; update this test to "
            "exercise the real playbook fire path through execute()"
        )
        assert "select_reactor" not in src
        assert "from hermes_quant.react" not in src
        # The bypass: the fire path goes to the broker REST endpoint directly.
        assert "/orders" in src

    def test_playbook_module_loads_without_importing_paper_reactor(self) -> None:
        """Import the playbook script as a module; confirm it pulls in no reactor.

        We load it under a stub module name to avoid colliding with the real
        ``hermes_quant.react.paper`` already imported by this test process, then
        check that the loaded module exposes ``place_paper_market_order`` and does
        NOT expose any reactor seam symbol.
        """
        spec = importlib.util.spec_from_file_location(
            "_adr0087_playbook_probe", self.PLAYBOOK
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        assert hasattr(mod, "place_paper_market_order"), (
            "playbook tick no longer exposes place_paper_market_order — "
            "re-audit the fire path"
        )
        assert hasattr(mod, "run_tick")
        # The module-level namespace must not have bound the reactor seam.
        assert not hasattr(mod, "PaperReactor")
        assert not hasattr(mod, "select_reactor")

    def test_hourly_layer_delegates_to_playbook_tick_same_bypass(self) -> None:
        """CRITICAL: hourly autonomous phase wraps the playbook tick's run_tick.

        maybe_run_autonomous_phase() imports quant-playbook-tick.py and calls
        run_tick(); it does not itself touch the reactor. So hourly inherits the
        playbook bypass — neither fires through PaperReactor.execute, and neither
        inherits the ADR-0087 seam cap. If hourly is ever wired to the reactor
        directly, this guard will trip.
        """
        src = _read(self.HOURLY)

        assert "maybe_run_autonomous_phase" in src
        assert "run_tick" in src, (
            "hourly no longer delegates to the playbook tick's run_tick — "
            "re-audit which seam (if any) hourly fires fall through"
        )
        assert "PaperReactor" not in src
        assert "select_reactor" not in src
        assert "from hermes_quant.react" not in src


# ---------------------------------------------------------------------------
# Summary assertion — keep the layer accounting explicit and machine-checked.
# ---------------------------------------------------------------------------


def test_adr0087_layer_inheritance_accounting() -> None:
    """One place that states which layers inherit the seam cap on `main`.

    INHERIT (route through PaperReactor.execute):  advisor, autonomous
    BYPASS  (route around the seam via Alpaca REST): playbook, hourly

    This codifies the critical finding: ADR-0087's "all four layers inherit the
    cap by construction" is NOT yet true on `main`. Exactly two of four inherit.
    """
    inherit = {"advisor", "autonomous"}
    bypass = {"playbook", "hourly"}

    assert inherit | bypass == set(ALL_FIRING_LAYERS)
    assert inherit & bypass == set()
    # The whole point of ADR-0087: this set should eventually be empty. Today it
    # is not — surface that loudly rather than asserting the ADR's premise as fact.
    assert bypass, (
        "If this set is empty, all four layers now route through the seam and "
        "ADR-0087's by-construction claim holds — update this accounting."
    )
