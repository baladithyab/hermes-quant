"""hermes_quant.react.multileg — multi-leg paper reactor (ADR-0029 B01 go-live BODY).

DEFAULT-OFF. Until ``HERMES_QUANT_MULTILEG_REACTOR=1`` (set NOWHERE in repo or
deploy), every ``execute()`` raises ``MultiLegReactorDisabled`` and NOTHING is
written — bit-identical to the Wave-B2 scaffold. The operator's flip is a separate,
later, deliberate act after the ADR-0029 D7 60-day / N>=100 evidence window.

When enabled, ``execute()`` fills an ALREADY-GATED (``options_gate`` as a
PRECONDITION, never bypassed) + ALREADY-HITL-APPROVED ``MultiLegProposal`` on a
deterministic no-creds ``PaperBroker`` (or live-paper Alpaca when creds present —
deferred this wave), writes ONE parent + one child-per-leg ``ExecutionRecord`` to
``executions.jsonl``, reconciles into ``state.db``, and records the PMCC shadow
counterfactual on a PMCC open. PAPER-ONLY: no live mleg path is reachable; the live
rail stays behind ``LiveTradingApproval`` in ``react/live.py`` (ADR-0029 D7).

Rails (plan §10): consume-the-gate (never re-run / bypass); exactly-once
(``client_order_id`` + bus ``multi_leg_id`` dedup); admissibility (ADR-0077) on the
equity leg of a CC via the shared precondition; slippage (ADR-0070) asymmetric
(passthrough on option legs, v0.2 envelope on the CC equity leg); two-row
reconciliation; all times UTC; ``Decimal`` money on the proposal; discrete sizing
untouched (the reactor fills the gate-admitted contracts, never widens).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, append_locked

from .admissibility_precondition import admissibility_reject_equity
from .backend import (
    BrokerBackend,
    FillResult,
    select_backend,
)
from .base import ExecutionRecord
from .mleg_fill import LegFill
from .paper import _enforce_fill_size_invariant

logger = logging.getLogger(__name__)


class MultiLegReactorDisabled(RuntimeError):  # noqa: N818 — plan/ADR-0029-mandated name
    """Raised by execute() when HERMES_QUANT_MULTILEG_REACTOR != 1."""


class LiveMultiLegNotAuthorized(RuntimeError):  # noqa: N818 — ADR-0029-D7-mandated name
    """Hard refusal: live multi-leg is gated behind a future promotion ADR
    (ADR-0029 D7). Not a config flag. Defined-but-unreachable this wave."""


class GateRejectedProposal(RuntimeError):  # noqa: N818 — plan-mandated name
    """Raised when execute() is asked to fill a proposal whose risk_gate_pass is not
    True. The deterministic gate is FINAL authority; the reactor refuses and writes
    nothing (gate-is-final-authority rail)."""


class MultiLegFillRejected(RuntimeError):  # noqa: N818 — plan-mandated name
    """Broker returned rejected/expired. Caught internally and surfaced as a no-fill
    parent record, NOT raised to the caller (so the proposal can be retried)."""


def _account_nav_usd() -> float | None:
    """Best-available paper-account NAV (USD), or None on any failure (fail-closed).

    Mirrors PaperReactor's NAV source so the equity-leg admissibility share
    conversion uses the SAME NAV as the equity reactor, without importing paper.py.
    """
    try:
        from hermes_quant.state.portfolio_state import (
            _default_initial_cash,
            get_portfolio_state,
        )

        cash = get_portfolio_state().get_cash("paper-default")
        if cash is not None and cash.equity_total > 0:
            return float(cash.equity_total)
        boot = _default_initial_cash()
        return float(boot) if boot > 0 else None
    except Exception as exc:  # noqa: BLE001 — fail-closed: unknown NAV => None.
        logger.warning("multileg-react: NAV lookup failed (admissibility fail-closed): %s", exc)
        return None


class MultiLegPaperReactor:
    """Paper-only multi-leg reactor. Interface-compatible with PaperReactor; inert
    unless HERMES_QUANT_MULTILEG_REACTOR=1."""

    name = "multileg-paper"
    requires_credentials = False

    def __init__(self, executions_path: Path | None = None) -> None:
        # Mirror PaperReactor.__init__ surface (default EXECUTION_BUS_PATH) but DO
        # NOT open/write/touch anything until the reactor is enabled. While disabled,
        # the reactor must leave the executions bus untouched (the scaffold tests
        # assert "bus not created while disabled"). The mkdir/touch is done lazily
        # inside _execute_enabled, NOT here.
        self.executions_path = executions_path or EXECUTION_BUS_PATH

    @staticmethod
    def _enabled() -> bool:
        return os.environ.get("HERMES_QUANT_MULTILEG_REACTOR", "0") == "1"

    def execute(
        self,
        proposal: Any,  # MultiLegProposal; Any at the Protocol boundary
        *,
        fill_size_pct: float,
        approver_user_id: str | None = None,
        play_tag: str = "advisor",
    ) -> ExecutionRecord:
        """Fill an already-gated + already-HITL-approved MultiLegProposal on paper.

        Returns the PARENT ExecutionRecord; child (per-leg) records are written to the
        bus as side-effects. Default-OFF: raises MultiLegReactorDisabled (first check)
        and writes nothing unless HERMES_QUANT_MULTILEG_REACTOR=1.

        B13: ``play_tag`` (advisor/playbook/autonomous) is stamped onto the parent and
        every child record so the retro/settlement loop can attribute the family by
        source. Default "advisor" keeps existing callers bit-for-bit.
        """
        if not self._enabled():
            raise MultiLegReactorDisabled(
                "multi-leg reactor is default-OFF; set "
                "HERMES_QUANT_MULTILEG_REACTOR=1 to enable (gated by ADR-0029 D7)"
            )
        fill_size_pct = _enforce_fill_size_invariant(proposal, fill_size_pct)
        return self._execute_enabled(
            proposal,
            fill_size_pct=fill_size_pct,
            approver_user_id=approver_user_id,
            play_tag=play_tag,
        )

    # ------------------------------------------------------------------
    # Enabled body
    # ------------------------------------------------------------------
    def _execute_enabled(
        self,
        proposal: Any,
        *,
        fill_size_pct: float,
        approver_user_id: str | None,
        play_tag: str = "advisor",
    ) -> ExecutionRecord:
        # ── Step 1: precondition re-assert (gate is FINAL authority). ───────────
        # The reactor NEVER re-runs options_gate — it TRUSTS the proposal's copied
        # gate result (from_gate_result guarantees it came from the gate). A
        # risk_gate_pass != True proposal is refused BEFORE any write.
        if proposal.risk_gate_pass is not True:
            raise GateRejectedProposal(
                f"{proposal.proposal_id}: risk_gate_pass is not True "
                f"(reason={proposal.risk_gate_reason!r}); reactor refuses to fill a "
                "gate-rejected proposal (gate is final authority)"
            )

        # ── Step 2: idempotency claim (ADR-0078 D78.3 shape). ───────────────────
        # The bus is NOT created here — only on an actual record write (step 8). A
        # disabled reactor, a gate-rejected proposal, an admissibility reject, and a
        # broker-reject no-fill all leave the bus untouched (no empty file, no
        # records). _existing_parent tolerates a missing bus.
        multi_leg_id = proposal.proposal_id
        client_order_id = self._stable_coid(proposal)
        existing = self._existing_parent(multi_leg_id)
        if existing is not None:
            logger.info(
                "multileg-react: idempotency hit on multi_leg_id=%s; returning "
                "existing parent (no-op re-fire)",
                multi_leg_id,
            )
            return existing

        # ── Step 3: timestamps. ─────────────────────────────────────────────────
        asof_decision = _iso_utc(proposal.asof)
        now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        net_price = float(proposal.net_debit_credit)

        # ── Step 4: admissibility on a SHORT equity leg of a CC (ADR-0077/0079). ─
        # The CC's +100-long leg is admissible by construction (qty>0 => long path,
        # the helper short-circuits). The guard exists for a future short-stock
        # collar leg (qty<0). DEFAULT-OFF behind HERMES_QUANT_ADMISSIBILITY.
        if proposal.stock_leg is not None and proposal.stock_leg.qty < 0:
            equity_target_pct = -abs(fill_size_pct) if fill_size_pct else -1e-9
            reject = admissibility_reject_equity(
                symbol=proposal.underlying,
                asset_class="equity",
                fill_size_pct=equity_target_pct,
                decision_price=(
                    float(proposal.stock_leg.basis_per_share)
                    if proposal.stock_leg.basis_per_share
                    else 0.0
                ),
                nav_provider=_account_nav_usd,
                asof_decision=asof_decision,
                asof_execution=now,
                reactor_name=self.name,
                proposal_id=proposal.proposal_id,
                signal_id=None,
                timeframe="",
                bar_ts=None,
                approver_user_id=approver_user_id,
                play_tag=play_tag,
                extra_metadata={
                    "multi_leg_id": multi_leg_id,
                    "strategy_kind": proposal.strategy_kind,
                    "role": "parent",
                },
            )
            if reject is not None:
                # No-fill parent audit record. Mirror PaperReactor: NOT appended to
                # the bus (the caller persists it as the proposal's audit trail).
                return reject

        # ── Step 5: submit + poll the fill via the pluggable backend (ADR-0088). ─
        # select_backend() routes to AlpacaBackend when HERMES_QUANT_ALPACA_PAPER=1
        # AND creds are present, else the DeterministicBackend simulator (the default
        # everywhere / in CI). The deterministic path mirrors the OLD PaperBroker
        # deterministic fill math, so default behavior is preserved bit-for-bit.
        backend = select_backend()
        try:
            leg_fills, parent_status, net_fill = self._fill(
                proposal, backend, client_order_id=client_order_id, net_price=net_price
            )
        except MultiLegFillRejected as exc:
            logger.warning("multileg-react: %s — writing no-fill record", exc)
            return self._write_nofill_parent(
                proposal,
                multi_leg_id=multi_leg_id,
                client_order_id=client_order_id,
                asof_decision=asof_decision,
                asof_execution=now,
                fill_size_pct=fill_size_pct,
                approver_user_id=approver_user_id,
                status=str(exc),
                play_tag=play_tag,
            )

        # ── Step 6: slippage (ADR-0070), asymmetric (research §2.2 / §3.6). ─────
        # Option legs: passthrough (paper fills at the live NBBO). EQUITY leg of a
        # CC: v0.2 envelope when HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2, else
        # passthrough. Seed (proposal_id, asof_execution) for replay equality.
        leg_fills = self._apply_equity_slippage(
            proposal, leg_fills, fill_size_pct=fill_size_pct, asof_execution=now
        )

        # ── Step 7: build records — Shape (B) (research §3.4). ──────────────────
        parent, children = self._build_records(
            proposal,
            leg_fills=leg_fills,
            multi_leg_id=multi_leg_id,
            client_order_id=client_order_id,
            broker_order_id=f"paper-{client_order_id[:16]}",
            parent_status=parent_status,
            net_fill=net_fill,
            asof_decision=asof_decision,
            asof_execution=now,
            fill_size_pct=fill_size_pct,
            approver_user_id=approver_user_id,
            play_tag=play_tag,
        )

        # ── Step 8: write atomically — parent first, then children, ONE lock. ───
        self._write_family(parent, children)

        # ── Step 9: reconcile into state.db (best-effort, non-blocking). ────────
        self._reconcile_state(children)

        # ── Step 10: PMCC-shadow record on a PMCC open (research §4.2). ─────────
        if proposal.strategy_kind == "pmcc":
            self._record_pmcc_shadow(proposal, multi_leg_id=multi_leg_id)

        # ── Step 11: reflection hook on a close (default OFF). ──────────────────
        if os.environ.get("HERMES_QUANT_REFLECTION", "0") == "1":
            self._maybe_reflect(parent, proposal)

        # ── Step 12: audit + return parent. ─────────────────────────────────────
        logger.info(
            "multileg-react: %s kind=%s outer_qty=%d net=%+.4f legs=%d status=%s",
            multi_leg_id,
            proposal.strategy_kind,
            proposal.outer_qty,
            net_fill,
            len(children),
            parent_status,
        )
        return parent

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------
    @staticmethod
    def _stable_coid(proposal: Any) -> str:
        """Deterministic client_order_id (ADR-0078): sha256 of
        (proposal_id, strategy_kind, sorted (symbol, intent), outer_qty)."""
        legs = sorted(
            (leg.symbol, leg.position_intent) for leg in proposal.option_legs
        )
        raw = json.dumps(
            [proposal.proposal_id, proposal.strategy_kind, legs, proposal.outer_qty],
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _existing_parent(self, multi_leg_id: str) -> ExecutionRecord | None:
        """Scan the bus tail for an already-recorded parent with this multi_leg_id.
        Returns the reconstructed parent ExecutionRecord (exactly-once no-op) or None.
        """
        if not self.executions_path.exists():
            return None
        try:
            raw = self.executions_path.read_bytes()
        except OSError:
            return None
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue  # silence-by-default: a valid-JSON non-dict line (corrupt append)
            meta = rec.get("reactor_metadata") or {}
            if meta.get("multi_leg_id") == multi_leg_id and meta.get("role") == "parent":
                return _dict_to_record(rec)
        return None

    # ------------------------------------------------------------------
    # Fill
    # ------------------------------------------------------------------
    def _fill(
        self,
        proposal: Any,
        backend: BrokerBackend,
        *,
        client_order_id: str,
        net_price: float,
    ) -> tuple[list[LegFill], str, float]:
        """Route per strategy_kind via the pluggable BrokerBackend (ADR-0088).

        Returns ``(leg_fills, parent_status, net_fill)`` in the SAME shape the rest
        of the reactor (``_apply_equity_slippage`` / ``_build_records`` /
        ``_reconcile_state``) already consumes — the backend's ``FillResult`` (and
        its mleg child ``FillResult``s) are converted to ``LegFill`` via
        ``_fillresult_to_legfill``.

        CC: equity BUY + single-leg SELL the call. CSP: single-leg SELL the put.
        mleg structures (>=2 option legs): ONE ``submit_option_mleg``. A backend that
        returns a reject/expire status OR RAISES (BP / unavailable / submit reject)
        is converted to ``MultiLegFillRejected`` so the caller's existing except
        writes the no-fill parent — a backend exception NEVER crashes the reactor.
        """
        fills: list[LegFill] = []
        if proposal.is_mleg:
            try:
                parent: FillResult = backend.submit_option_mleg(
                    proposal.option_legs,
                    outer_qty=proposal.outer_qty,
                    net_limit_price=net_price,
                    client_order_id=client_order_id,
                )
            except Exception as exc:  # noqa: BLE001 — any backend failure -> no-fill
                raise self._as_fill_rejected(exc, proposal) from exc
            self._guard_result(parent, proposal)
            leg_fills = [_fillresult_to_legfill(child) for child in parent.legs]
            # ADR-0088 F2: an mleg parent's filled_qty is the outer spread count
            # (>=1), so _guard_result on the parent alone can pass even if every
            # child filled 0 (a degenerate "parent filled, children empty" venue
            # snapshot). Guard the CHILDREN: if no leg actually moved, treat the
            # whole structure as a no-fill rather than emitting empty children.
            if not any(lf.filled_qty != 0.0 for lf in leg_fills):
                raise MultiLegFillRejected(
                    f"{proposal.proposal_id}: mleg parent status={parent.status!r} "
                    "but no child leg filled (zero-fill children); no-fill"
                )
            net_fill = parent.net_fill_price if parent.net_fill_price is not None else net_price
            return leg_fills, parent.status, net_fill

        # Single option leg (CC short call / CSP short put).
        option_leg = proposal.option_legs[0]
        try:
            opt_res: FillResult = backend.submit_option_single(
                option_leg,
                qty=proposal.outer_qty,
                limit_price=net_price,
                client_order_id=client_order_id,
            )
        except Exception as exc:  # noqa: BLE001 — any backend failure -> no-fill
            raise self._as_fill_rejected(exc, proposal) from exc
        self._guard_result(opt_res, proposal)
        fills.append(_fillresult_to_legfill(opt_res))

        # Covered-call equity leg: a SEPARATE equity order (research §1.3).
        if proposal.stock_leg is not None:
            basis = (
                float(proposal.stock_leg.basis_per_share)
                if proposal.stock_leg.basis_per_share
                else 0.0
            )
            try:
                eq_res: FillResult = backend.submit_equity(
                    symbol=proposal.underlying,
                    signed_qty=float(proposal.stock_leg.qty),
                    decision_price=basis,
                    client_order_id=client_order_id + "-eq",
                )
            except Exception as exc:  # noqa: BLE001 — any backend failure -> no-fill
                raise self._as_fill_rejected(exc, proposal) from exc
            self._guard_result(eq_res, proposal)
            fills.append(_fillresult_to_legfill(eq_res))

        net_fill = sum(f.filled_avg_price * _leg_sign(f) for f in fills if _is_option(f))
        return fills, "filled", net_fill if net_fill else net_price

    @staticmethod
    def _guard_result(res: FillResult, proposal: Any) -> None:
        """Guard a backend FillResult: a reject/expire/timeout surfaces as a no-fill
        (MultiLegFillRejected). Preserves the old _guard_fill reject semantics and
        also treats a non-fill terminal (e.g. ``unfilled_timeout``) as a no-fill."""
        if res.status in {"rejected", "expired"} or not res.is_fill:
            raise MultiLegFillRejected(
                f"backend_status={res.status} for {proposal.proposal_id}"
            )

    @staticmethod
    def _as_fill_rejected(exc: Exception, proposal: Any) -> MultiLegFillRejected:
        """Convert a backend exception into a MultiLegFillRejected no-fill reason so
        _execute_enabled's existing except writes the no-fill parent (a backend BP /
        unavailable / submit reject must NEVER crash the reactor — fail to a no-fill,
        never a fabricated fill)."""
        if isinstance(exc, MultiLegFillRejected):
            return exc
        return MultiLegFillRejected(
            f"backend_error={type(exc).__name__}: {exc} for {proposal.proposal_id}"
        )

    @staticmethod
    def _guard_fill(status: str, proposal: Any) -> None:
        if status in {"rejected", "expired"}:
            raise MultiLegFillRejected(
                f"broker_status={status} for {proposal.proposal_id}"
            )

    # ------------------------------------------------------------------
    # Slippage (asymmetric)
    # ------------------------------------------------------------------
    def _apply_equity_slippage(
        self,
        proposal: Any,
        leg_fills: list[LegFill],
        *,
        fill_size_pct: float,
        asof_execution: str,
    ) -> list[LegFill]:
        # ADR-0070 / FLAGS.md Tier-A: default ON (v0.2). Set
        # HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.1 to opt OUT (legacy passthrough).
        if os.environ.get("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2") != "v0.2":
            return leg_fills  # passthrough (option + equity)
        if proposal.stock_leg is None:
            return leg_fills  # no equity leg => nothing asymmetric to slip
        from hermes_quant.react.slippage_model import apply_slippage

        out: list[LegFill] = []
        for f in leg_fills:
            # Only the EQUITY leg of a CC is slipped; option legs stay passthrough.
            if _is_option(f) or f.symbol != proposal.underlying:
                out.append(f)
                continue
            target_pct = abs(fill_size_pct) if proposal.stock_leg.qty > 0 else -abs(fill_size_pct)
            try:
                slipped, _ = apply_slippage(
                    decision_price=f.filled_avg_price,
                    target_pct=target_pct or 1e-9,
                    asof_execution=asof_execution,
                    proposal_id=proposal.proposal_id,
                    asset_class="equity",
                )
            except ValueError:
                slipped = f.filled_avg_price
            out.append(
                LegFill(
                    symbol=f.symbol,
                    filled_avg_price=slipped,
                    filled_qty=f.filled_qty,
                    status=f.status,
                    position_intent=f.position_intent,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Record building — Shape (B)
    # ------------------------------------------------------------------
    def _build_records(
        self,
        proposal: Any,
        *,
        leg_fills: list[LegFill],
        multi_leg_id: str,
        client_order_id: str,
        broker_order_id: str,
        parent_status: str,
        net_fill: float,
        asof_decision: str,
        asof_execution: str,
        fill_size_pct: float,
        approver_user_id: str | None,
        play_tag: str = "advisor",
    ) -> tuple[ExecutionRecord, list[ExecutionRecord]]:
        ng = proposal.net_greeks
        net_greeks_dict = {
            "delta": ng.delta,
            "gamma": ng.gamma,
            "theta": ng.theta,
            "vega": ng.vega,
            "rho": ng.rho,
        }
        leg_symbols = [f.symbol for f in leg_fills if _is_option(f)]
        parent = ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id=None,
            asset=proposal.underlying,
            asset_class="multi_leg",
            timeframe="",
            asof_decision=asof_decision,
            asof_execution=asof_execution,
            target_position_pct=fill_size_pct,
            decision_price=float(proposal.net_debit_credit),
            fill_price=net_fill,
            fill_size_pct=fill_size_pct,
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata={
                "multi_leg_id": multi_leg_id,
                "strategy_kind": proposal.strategy_kind,
                "outer_qty": proposal.outer_qty,
                "net_greeks": net_greeks_dict,
                "client_order_id": client_order_id,
                "broker_order_id": broker_order_id,
                "leg_symbols": leg_symbols,
                "parent_status": parent_status,
                "risk_gate_bucket": proposal.risk_gate_bucket,
                "paper": True,
                "role": "parent",
            },
            bar_ts=None,
            play_tag=play_tag,
        )

        children: list[ExecutionRecord] = []
        leg_index = 0
        for f in leg_fills:
            if _is_option(f):
                # Option child (us_option). quantity = signed contracts.
                children.append(
                    ExecutionRecord(
                        proposal_id=proposal.proposal_id,
                        signal_id=None,
                        asset=f.symbol,
                        asset_class="us_option",
                        timeframe="",
                        asof_decision=asof_decision,
                        asof_execution=asof_execution,
                        target_position_pct=_signed_frac(f, fill_size_pct),
                        decision_price=f.filled_avg_price,
                        fill_price=f.filled_avg_price,
                        fill_size_pct=_signed_frac(f, fill_size_pct),
                        reactor_name=self.name,
                        human_in_the_loop=True,
                        approver_user_id=approver_user_id,
                        reactor_metadata={
                            "multi_leg_id": multi_leg_id,
                            "leg_index": leg_index,
                            "position_intent": f.position_intent,
                            "ratio_qty": _ratio_for(proposal, f.symbol),
                            "contracts": abs(int(f.filled_qty)),
                            "quantity": f.filled_qty,  # signed contracts
                            "role": "leg",
                            "paper": True,
                            "option_pricing": "iex_possibly_delayed",
                        },
                        bar_ts=None,
                        play_tag=play_tag,
                    )
                )
                leg_index += 1
            else:
                # CC equity child (equity). quantity = signed shares.
                children.append(
                    ExecutionRecord(
                        proposal_id=proposal.proposal_id,
                        signal_id=None,
                        asset=f.symbol,
                        asset_class="equity",
                        timeframe="",
                        asof_decision=asof_decision,
                        asof_execution=asof_execution,
                        target_position_pct=fill_size_pct,
                        decision_price=f.filled_avg_price,
                        fill_price=f.filled_avg_price,
                        fill_size_pct=fill_size_pct,
                        reactor_name=self.name,
                        human_in_the_loop=True,
                        approver_user_id=approver_user_id,
                        reactor_metadata={
                            "multi_leg_id": multi_leg_id,
                            "quantity": f.filled_qty,  # signed shares
                            "role": "equity_leg",
                            "paper": True,
                        },
                        bar_ts=None,
                        play_tag=play_tag,
                    )
                )
        return parent, children

    # ------------------------------------------------------------------
    # Bus write (atomic family)
    # ------------------------------------------------------------------
    def _write_family(
        self, parent: ExecutionRecord, children: list[ExecutionRecord]
    ) -> None:
        lines = [
            json.dumps(_record_to_dict(parent), separators=(",", ":"), sort_keys=True)
            + "\n"
        ]
        for child in children:
            lines.append(
                json.dumps(_record_to_dict(child), separators=(",", ":"), sort_keys=True)
                + "\n"
            )
        payload = "".join(lines).encode("utf-8")
        # Lazy mkdir on the FIRST actual write (append_locked opens with O_CREAT).
        self.executions_path.parent.mkdir(parents=True, exist_ok=True)
        with append_locked(self.executions_path) as fd:
            os.write(fd, payload)

    def _write_nofill_parent(
        self,
        proposal: Any,
        *,
        multi_leg_id: str,
        client_order_id: str,
        asof_decision: str,
        asof_execution: str,
        fill_size_pct: float,
        approver_user_id: str | None,
        status: str,
        play_tag: str = "advisor",
    ) -> ExecutionRecord:
        """No-fill parent audit record on a broker reject/expire. NOT appended to the
        bus (mirror PaperReactor._admissibility_reject) — never fabricate a fill, and
        leave no position-mutating family on the bus for a rejected order."""
        return ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id=None,
            asset=proposal.underlying,
            asset_class="multi_leg",
            timeframe="",
            asof_decision=asof_decision,
            asof_execution=asof_execution,
            target_position_pct=fill_size_pct,
            decision_price=float(proposal.net_debit_credit),
            fill_price=0.0,
            fill_size_pct=0.0,
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata={
                "multi_leg_id": multi_leg_id,
                "strategy_kind": proposal.strategy_kind,
                "client_order_id": client_order_id,
                "broker_status": status,
                "no_fill": True,
                "paper": True,
                "role": "parent",
            },
            bar_ts=None,
            play_tag=play_tag,
        )

    # ------------------------------------------------------------------
    # State reconciliation
    # ------------------------------------------------------------------
    def _reconcile_state(self, children: list[ExecutionRecord]) -> None:
        """Best-effort: apply each CHILD to state.db (the parent is an audit rollup,
        NOT a position). Failure must NOT block the fill (silence-by-default)."""
        try:
            from hermes_quant.state.portfolio_state import get_portfolio_state

            ps = get_portfolio_state()
            for child in children:
                rec = _record_to_dict(child)
                if "account_id" not in rec or not rec.get("account_id"):
                    rec["account_id"] = (
                        (child.reactor_metadata or {}).get("account_id") or "paper-default"
                    )
                ps.apply_execution(rec)
        except Exception as exc:  # pragma: no cover — defensive, non-blocking
            logger.warning(
                "multileg-react: PortfolioState.apply_execution failed (non-blocking): %s",
                exc,
            )

    # ------------------------------------------------------------------
    # PMCC shadow
    # ------------------------------------------------------------------
    def _record_pmcc_shadow(self, proposal: Any, *, multi_leg_id: str) -> None:
        """On a PMCC open, record the SAME two-leg structure as a PMCC shadow, joined
        on note==multi_leg_id (research §4.2). Best-effort, non-blocking."""
        try:
            from hermes_quant.shadow.pmcc import (
                OptionLeg as ShadowLeg,
            )
            from hermes_quant.shadow.pmcc import (
                PMCCPosition,
                record_pmcc,
            )

            longs = [leg for leg in proposal.option_legs if leg.side == "buy"]
            shorts = [leg for leg in proposal.option_legs if leg.side == "sell"]
            if not longs or not shorts:
                logger.warning(
                    "multileg-react: pmcc shadow skipped — need 1 long + 1 short call leg"
                )
                return
            long_leg, short_leg = longs[0], shorts[0]
            spot = float(proposal.breakeven_underlying[0]) if proposal.breakeven_underlying else 0.0
            pos = PMCCPosition(
                symbol=proposal.underlying,
                opened_at=_iso_utc(proposal.asof),
                long_leg=ShadowLeg(
                    side="long",
                    expiry=long_leg.expiry.isoformat(),
                    strike=float(long_leg.strike),
                    entry_premium=float(long_leg.fill_price or 0.0),
                    entry_iv=float((long_leg.greeks_at_decision and long_leg.greeks_at_decision.iv) or 0.0),
                    contracts=long_leg.ratio_qty * proposal.outer_qty,
                ),
                short_leg=ShadowLeg(
                    side="short",
                    expiry=short_leg.expiry.isoformat(),
                    strike=float(short_leg.strike),
                    entry_premium=float(short_leg.fill_price or 0.0),
                    entry_iv=float((short_leg.greeks_at_decision and short_leg.greeks_at_decision.iv) or 0.0),
                    contracts=short_leg.ratio_qty * proposal.outer_qty,
                ),
                spot_at_open=spot,
                note=multi_leg_id,
            )
            record_pmcc(pos)
        except Exception as exc:  # pragma: no cover — defensive, non-blocking
            logger.warning("multileg-react: pmcc shadow record failed (non-blocking): %s", exc)

    # ------------------------------------------------------------------
    # Reflection hook
    # ------------------------------------------------------------------
    def _maybe_reflect(self, parent: ExecutionRecord, proposal: Any) -> None:
        is_close = any(
            leg.position_intent.endswith("_to_close") for leg in proposal.option_legs
        )
        if not is_close:
            return
        try:
            from hermes_quant.memory._paper_reflection_hook import maybe_reflect_on_close

            maybe_reflect_on_close(parent, proposal)
        except Exception as exc:  # pragma: no cover — non-blocking
            logger.warning("multileg-react: reflection hook failed (non-blocking): %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso_utc(dt: datetime) -> str:
    """ISO-8601 UTC seconds with 'Z'. Aware-or-naive tolerant (fail-closed to UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_option(f: LegFill) -> bool:
    """Heuristic: option legs are OCC-21 (parse succeeds); equity legs are tickers."""
    from hermes_quant.options.occ import OccParseError, parse_occ

    try:
        parse_occ(f.symbol)
        return True
    except OccParseError:
        return False


def _fillresult_to_legfill(fr: FillResult) -> LegFill:
    """Adapt a backend ``FillResult`` to the ``LegFill`` the reactor's record-builder
    consumes (ADR-0088 wiring). Maps the four position-moving fields verbatim:
    symbol / filled_avg_price / filled_qty (signed TRUE units) / status /
    position_intent. The parent mleg ``FillResult`` is expanded BY THE CALLER (one
    ``LegFill`` per ``fr.legs`` child); this adapter handles a single leg/equity/
    child result. The conversion is lossless for everything ``_build_records`` /
    ``_reconcile_state`` read, so those stay unchanged."""
    return LegFill(
        symbol=fr.symbol,
        filled_avg_price=fr.filled_avg_price,
        filled_qty=fr.filled_qty,
        status=fr.status,
        position_intent=fr.position_intent,
    )


def _leg_sign(f: LegFill) -> float:
    return 1.0 if f.filled_qty >= 0 else -1.0


def _signed_frac(f: LegFill, fill_size_pct: float) -> float:
    """Signed per-leg NAV-fraction proxy (retained for the calibrator's reader, plan
    §9 OQ4). The AUTHORITATIVE size is reactor_metadata.quantity."""
    sgn = 1.0 if f.filled_qty >= 0 else -1.0
    return sgn * abs(fill_size_pct)


def _ratio_for(proposal: Any, symbol: str) -> int:
    for leg in proposal.option_legs:
        if leg.symbol == symbol:
            return leg.ratio_qty
    return 1


def _record_to_dict(record: ExecutionRecord) -> dict[str, Any]:
    """Serialize an ExecutionRecord to a JSONL-safe dict (mirror paper._record_to_dict)."""
    return {
        "proposal_id": record.proposal_id,
        "signal_id": record.signal_id,
        "asset": record.asset,
        "asset_class": record.asset_class,
        "timeframe": record.timeframe,
        "asof_decision": record.asof_decision,
        "asof_execution": record.asof_execution,
        "target_position_pct": record.target_position_pct,
        "decision_price": record.decision_price,
        "fill_price": record.fill_price,
        "fill_size_pct": record.fill_size_pct,
        "reactor_name": record.reactor_name,
        "human_in_the_loop": record.human_in_the_loop,
        "approver_user_id": record.approver_user_id,
        "reactor_metadata": record.reactor_metadata or {},
        "bar_ts": record.bar_ts,
        "play_tag": record.play_tag,  # B13: source of the fire
    }


def _dict_to_record(rec: dict[str, Any]) -> ExecutionRecord:
    """Reconstruct an ExecutionRecord from a bus dict (for the idempotency no-op)."""
    return ExecutionRecord(
        proposal_id=rec["proposal_id"],
        signal_id=rec.get("signal_id"),
        asset=rec["asset"],
        asset_class=rec.get("asset_class", "multi_leg"),
        timeframe=rec.get("timeframe", ""),
        asof_decision=rec.get("asof_decision", ""),
        asof_execution=rec.get("asof_execution", ""),
        target_position_pct=float(rec.get("target_position_pct", 0.0)),
        decision_price=float(rec.get("decision_price", 0.0)),
        fill_price=float(rec.get("fill_price", 0.0)),
        fill_size_pct=float(rec.get("fill_size_pct", 0.0)),
        reactor_name=rec.get("reactor_name", "multileg-paper"),
        human_in_the_loop=bool(rec.get("human_in_the_loop", True)),
        approver_user_id=rec.get("approver_user_id"),
        reactor_metadata=rec.get("reactor_metadata") or {},
        bar_ts=rec.get("bar_ts"),
        play_tag=rec.get("play_tag", "advisor"),  # B13: default "advisor" for pre-B13 records
    )
