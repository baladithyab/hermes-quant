"""cs21 RED->GREEN — the armed weekly close fired the WRONG side/size via a broken call.

THE BUG (latent; armed=DRY-RUN by default, so it fails-closed today)
--------------------------------------------------------------------
``scripts/quant-playbook-weekly.py::_fire_equity_close`` had THREE defects, any one
of which made an armed CLOSE fail-closed to CLOSE_FAILED rather than fire correctly:

  1. ``from hermes_quant.reactor import PaperReactor`` — that module does NOT exist
     (the real class is ``hermes_quant.react.paper.PaperReactor``). ModuleNotFoundError,
     captured into the ``{"ok": False, "error": "PaperReactor import failed: ..."}``
     envelope on EVERY armed close.
  2. It hardcoded ``"side": "sell"`` for ANY position. Selling against a SHORT
     (qty < 0) DEEPENS the short instead of covering it — the opposite of a close.
  3. It called ``reactor.execute({...dict...})`` positionally; the real signature is
     ``execute(proposal, *, fill_size_pct, ...)`` (a Proposal object + a keyword-only
     ``fill_size_pct``), so even past the import a dict would TypeError.

THE FIX (cs21)
--------------
The close DIRECTION is now OPPOSITE the held sign (short qty<0 -> BUY to cover; long
qty>0 -> SELL), the helper builds a REAL ``Proposal``, and it fires
``fill_size_pct = -target_position_pct`` — the NEGATIVE of the held NAV-fraction.

WHY ``-held`` AND NOT ``0.0`` (the load-bearing refinement)
-----------------------------------------------------------
A ``_fire_equity_close`` Proposal carries no ``reactor_metadata.quantity``, so the fill
takes the NAV-fraction lane and folds into the LIVE default-regime state.db as a raw
DELTA: ``new_qty = old_qty + fill_size_pct`` (portfolio_state.py:1124;
``HERMES_QUANT_DELTA_NORMALIZER`` unset == regime 0). On that ledger:
  * ``fill_size_pct = 0.0`` is a SILENT NO-OP — a held short -0.20 stays -0.20 (NOT flat).
  * ``fill_size_pct = -held`` flattens it, sign-correct: short (-0.20) folds +0.20 ->
    0 (buy-to-cover); long (+0.30) folds -0.30 -> 0 (sell).

Two test layers:
  LAYER A (spy, no I/O) — assert the captured ``fill_size_pct == -target_position_pct``
    and the side/Proposal shape, importlib-loading the scripts/ twin.
  LAYER B (fold-through) — seed a real PortfolioState under the DEFAULT regime and prove
    the close delta the helper emits actually flattens the position. This is the proof
    that 0.0 was wrong: RED against 0.0 (position survives), GREEN against -held.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.proposals import Proposal
from hermes_quant.state.portfolio_state import PortfolioState

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "quant-playbook-weekly.py"
if not SCRIPT_PATH.exists():
    SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "quant-playbook-weekly.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT_PATH.exists(),
    reason=f"quant-playbook-weekly.py not found at {SCRIPT_PATH}",
)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("qpw_cs21", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass field-annotation resolution finds it.
    sys.modules["qpw_cs21"] = m
    spec.loader.exec_module(m)
    return m


class _ExecuteSpy:
    """Captures the (proposal, fill_size_pct, play_tag) execute() was called with.

    Patched onto ``PaperReactor.execute`` as a plain function (via ``staticmethod``
    so the bound-instance ``self`` is NOT injected — the spy sees exactly the args
    the helper passes). Returns a stub object carrying a ``proposal_id`` attribute so
    the helper's success-return maps a REAL attribute (ExecutionRecord has
    .proposal_id, NOT .execution_id), without touching the bus or state.db.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, proposal, *, fill_size_pct, play_tag="advisor",
                approver_user_id=None):
        self.calls.append(
            {
                "proposal": proposal,
                "fill_size_pct": fill_size_pct,
                "play_tag": play_tag,
                "approver_user_id": approver_user_id,
            }
        )

        class _Rec:
            pass

        rec = _Rec()
        rec.proposal_id = proposal.proposal_id
        return rec


@pytest.fixture
def spy(monkeypatch):
    s = _ExecuteSpy()
    # Patch the unbound function on the CLASS so PaperReactor().execute(...) routes
    # to s.execute(...) — the real reactor instance's self is discarded, the helper's
    # (proposal, fill_size_pct, play_tag) arrive verbatim. No bus / state.db I/O.
    monkeypatch.setattr(
        "hermes_quant.react.paper.PaperReactor.execute",
        lambda _self, proposal, **kw: s.execute(proposal, **kw),
    )
    return s


# ---------------------------------------------------------------------------
# LAYER A — spy unit tests (no I/O): the close fires fill_size_pct == -held,
# correct side, and a VALID Proposal+keyword call.
#
# RED-on-current proof for the import/dict/side defects: against the unfixed
# helper the spy is NEVER reached (ModuleNotFoundError on `import hermes_quant.reactor`).
# RED-on-0.0 proof for the SIZE: against the fill_size_pct=0.0 helper, the
# `== -target_position_pct` assertions below fail (0.0 != +0.20).
# ---------------------------------------------------------------------------


def test_short_close_buys_to_cover(mod, spy):
    """A held SHORT (target -0.20) closes with a BUY (cover) firing +0.20 (= -held)."""
    held = -0.20
    placed = mod._fire_equity_close(
        "AVGO", qty=-100.0, target_position_pct=held, reason="leaps_drawdown",
        decision_price=200.0,
    )

    assert placed["ok"] is True, placed
    assert placed["side"] == "buy"  # cover a short
    assert len(spy.calls) == 1
    call = spy.calls[0]
    prop = call["proposal"]
    assert isinstance(prop, Proposal)  # a real Proposal, not a dict (no TypeError)
    assert prop.symbol == "AVGO"
    assert prop.asset_class == "equity"
    assert prop.timeframe == "1d"
    assert prop.advisor_result["close_side"] == "buy"
    # The close DELTA is -held: short -0.20 -> +0.20 (buy-to-cover), NOT 0.0.
    assert call["fill_size_pct"] == pytest.approx(0.20)
    assert call["fill_size_pct"] == pytest.approx(-held)
    assert call["play_tag"] == "playbook"


def test_long_close_sells(mod, spy):
    """A held LONG (target +0.30) closes with a SELL firing -0.30 (= -held)."""
    placed = mod._fire_equity_close(
        "AAPL", qty=100.0, target_position_pct=0.30, reason="swing_tp",
        decision_price=200.0,
    )

    assert placed["ok"] is True, placed
    assert placed["side"] == "sell"
    assert len(spy.calls) == 1
    call = spy.calls[0]
    prop = call["proposal"]
    assert isinstance(prop, Proposal)
    assert prop.advisor_result["close_side"] == "sell"
    # The close DELTA is -held: long +0.30 -> -0.30 (sell), NOT 0.0.
    assert call["fill_size_pct"] == pytest.approx(-0.30)
    assert call["play_tag"] == "playbook"


def test_execute_called_as_valid_proposal_plus_keyword(mod, spy):
    """The invocation must be a Proposal positionally + keyword fill_size_pct —
    a valid call (no TypeError, no ModuleNotFoundError, no positional dict)."""
    placed = mod._fire_equity_close(
        "NVDA", qty=-50.0, target_position_pct=-0.15, reason="leaps_drawdown",
        decision_price=200.0,
    )
    assert placed["ok"] is True, placed
    assert "error" not in placed
    call = spy.calls[0]
    # fill_size_pct arrived as a KEYWORD (the spy declares it keyword-only) and is -held.
    assert call["fill_size_pct"] == pytest.approx(0.15)
    assert isinstance(call["proposal"], Proposal)
    # The mapped execution_id is the ExecutionRecord.proposal_id (NOT .execution_id).
    assert placed["execution_id"] == call["proposal"].proposal_id


def test_state_is_approved_and_kind_equity(mod, spy):
    """The minted Proposal routes to the equity PaperReactor (kind 'equity')
    and is built in the approved state."""
    mod._fire_equity_close(
        "MSFT", qty=100.0, target_position_pct=0.10, reason="swing_tp",
        decision_price=200.0,
    )
    prop = spy.calls[0]["proposal"]
    assert prop.proposal_kind == "equity"
    assert prop.state == "approved"


def test_held_nav_fraction_returns_latest_target(mod):
    """`_held_nav_fraction` returns the LATEST-by-asof target for a multi-fill asset.

    Open short -0.10 @ ts05, then a same-sign ADD -0.20 @ ts06 -> the held NAV-fraction
    is the latest target -0.20 (the abs_latest record the loader keys qty/sign off),
    so the close fires -(-0.20) = +0.20 (buy-to-cover the full -0.20 short)."""
    executions = [
        {"asset": "AVGO", "target_position_pct": -0.10, "asof_execution": "2026-06-08T13:31:05+00:00"},
        {"asset": "AVGO", "target_position_pct": -0.20, "asof_execution": "2026-06-08T13:31:06+00:00"},
        {"asset": "OTHER", "target_position_pct": 0.99, "asof_execution": "2026-06-08T13:31:07+00:00"},
    ]
    assert mod._held_nav_fraction(executions, "AVGO") == pytest.approx(-0.20)
    # No matching record -> 0.0 (the close no-ops, which is safe).
    assert mod._held_nav_fraction(executions, "MISSING") == 0.0


# ---------------------------------------------------------------------------
# LAYER B — fold-through proof (NO mocked execute): seed a real PortfolioState
# under the DEFAULT regime and fold the close DELTA the helper emits. Proves
# that -held flattens the live ledger and 0.0 does NOT.
#
# RED-on-0.0: with the old fill_size_pct=0.0, the folded delta is 0.0 (a no-op);
# the seeded position SURVIVES (new_qty == old_qty) and these asserts FAIL.
# GREEN after -held: the +/-held delta folds the position to ~0 (flat -> dropped).
# ---------------------------------------------------------------------------


def _recent_iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _open_fill(asset: str, pct: float, *, fill_price: float, pid: str, ts: str) -> dict:
    """A paper-default NAV-fraction-lane open fill (NO reactor_metadata.quantity)."""
    return {
        "account_id": "paper-default",
        "asset_class": "equity",
        "asset": asset,
        "fill_size_pct": pct,
        "fill_price": fill_price,
        "target_position_pct": pct,
        "asof_execution": ts,
        "proposal_id": pid,
    }


@pytest.mark.parametrize(
    ("asset", "open_pct"),
    [("AVGO", -0.20), ("AAPL", 0.30)],
    ids=["short_covered_by_positive_delta", "long_sold_by_negative_delta"],
)
def test_close_delta_flattens_default_regime_state_db(
    monkeypatch, mod, asset, open_pct
):
    """The helper's ACTUAL emitted fill_size_pct flattens the DEFAULT-regime state.db.

    This drives the REAL ``_fire_equity_close`` through a spy that captures the
    ``fill_size_pct`` it fires, then folds THAT captured value into a real
    PortfolioState.apply_execution. The position must reach ~0 (flat -> dropped from
    get_positions). Proven for BOTH a short (the helper fires a POSITIVE delta =
    buy-to-cover) and a long (a NEGATIVE delta = sell).

    RED on the old fill_size_pct=0.0 helper: the captured delta is 0.0, the fold is a
    no-op (new_qty == old_qty), so the seeded position SURVIVES and the final
    `asset not in positions` assertion FAILS. GREEN after the -held edit.
    """
    # Pin the DEFAULT delta-fold regime (raw-delta fold; new_qty = old_qty + delta).
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)

    # Spy that captures the fill_size_pct the helper actually fires (no bus I/O).
    captured: dict = {}

    def _spy_execute(_self, proposal, *, fill_size_pct, play_tag="advisor",
                     approver_user_id=None):
        captured["fill_size_pct"] = fill_size_pct

        class _Rec:
            pass

        rec = _Rec()
        rec.proposal_id = proposal.proposal_id
        return rec

    monkeypatch.setattr(
        "hermes_quant.react.paper.PaperReactor.execute", _spy_execute
    )

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.db"
        ps = PortfolioState(state_db_path=db)

        # Seed the held position (open fill, NAV-fraction lane).
        ps.apply_execution(
            _open_fill(asset, open_pct, fill_price=200.0, pid=f"open-{asset}",
                       ts=_recent_iso(2))
        )
        positions = ps.get_positions("paper-default")
        assert ("equity", asset) in positions, "open fill did not seed the position"
        assert positions[("equity", asset)].quantity == pytest.approx(open_pct)

        # Recover the held NAV-fraction the call site would pass, then drive the REAL
        # helper. The qty sign matches the held sign so the audit side is correct too.
        held = mod._held_nav_fraction(
            [_open_fill(asset, open_pct, fill_price=200.0, pid=f"open-{asset}",
                        ts=_recent_iso(2))],
            asset,
        )
        placed = mod._fire_equity_close(
            asset,
            qty=(open_pct * 1000.0),  # signed proxy share count; sign = held sign
            target_position_pct=held,
            reason="leaps_drawdown",
            decision_price=200.0,
        )
        assert placed["ok"] is True, placed
        # short held<0 -> buy-to-cover; long held>0 -> sell.
        assert placed["side"] == ("buy" if open_pct < 0 else "sell")

        close_delta = captured["fill_size_pct"]
        # The helper fired -held: short -0.20 -> +0.20 (cover); long +0.30 -> -0.30 (sell).
        assert close_delta == pytest.approx(-open_pct)

        # Fold the close DELTA the helper actually fired (distinct proposal_id so the
        # idempotency guard lets it apply). This mirrors what PaperReactor.execute
        # would write for the close fire on the live default-regime state.db.
        ps.apply_execution(
            {
                "account_id": "paper-default",
                "asset_class": "equity",
                "asset": asset,
                "fill_size_pct": close_delta,
                "fill_price": 200.0,
                # Per-record absolute target the close WRITES; for the default-regime
                # fold only fill_size_pct (the delta) is read. Carry open+delta = 0.
                "target_position_pct": open_pct + close_delta,
                "asof_execution": _recent_iso(1),
                "proposal_id": f"close-{asset}",
            }
        )

        # The position must be FLAT — dropped from get_positions (|qty| < 1e-12).
        positions_after = ps.get_positions("paper-default")
        assert ("equity", asset) not in positions_after, (
            f"close delta {close_delta} did NOT flatten {asset}; "
            f"remaining positions: {positions_after}"
        )
