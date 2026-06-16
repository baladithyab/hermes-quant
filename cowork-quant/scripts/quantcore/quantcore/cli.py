"""quantcore.cli — JSON-in / JSON-out command surface for the plugin commands.

Claude's slash commands shell out to this; stdout is always a single JSON
document. Validation failure or any error => {"ok": false, "error": ...} and
exit 1 — the calling command must ABSTAIN, never repair.

Usage:
  python -m quantcore.cli gate     --state-dir D --signal-json FILE
  python -m quantcore.cli propose  --state-dir D --signal-json FILE
  python -m quantcore.cli decide   --state-dir D --proposal-id ID --decision approval|rejection [--note ...]
  python -m quantcore.cli fill     --state-dir D --fill-json FILE
  python -m quantcore.cli mark     --state-dir D --asset A --price P --nav N [--allow-jump]
  python -m quantcore.cli settle   --state-dir D
  python -m quantcore.cli status   --state-dir D
  python -m quantcore.cli verify   --state-dir D
  python -m quantcore.cli expire   --state-dir D
  python -m quantcore.cli resume   --state-dir D --note TEXT

`resume` is HUMAN-ONLY: it clears a circuit-breaker halt. The calling command
prompt MUST confirm with the human before invoking it — quantcore cannot tell
who is calling, so the confirmation gate lives at the prompt seam.
`mark --allow-jump` overrides the price/NAV sanity guards for documented
discontinuities (stock splits, deposits); never pass it routinely.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path

from quantcore.config import load_state_config
from quantcore.gate import RiskGate
from quantcore.ledger import Ledger, new_proposal_id
from quantcore.schemas import CommitteeSignal, Fill, MarketCosts, Proposal
from quantcore.settle import calibration_report, settle


def _emit(obj: dict, ok: bool = True) -> int:
    print(json.dumps({"ok": ok, **obj}, default=str, indent=2))
    return 0 if ok else 1


def _load_signal(path: str) -> tuple[CommitteeSignal, MarketCosts]:
    raw = json.loads(Path(path).read_text())
    return CommitteeSignal(**raw["signal"]), MarketCosts(**raw["costs"])


def _persist_halt_if_any(ledger: Ledger, decision) -> None:
    """R1-04: a flatten_halt verdict must DURABLY halt the book. The portfolio
    replay only reads halt/resume events, so the breaker is dead code unless
    the verdict is persisted here. Cleared only by `resume` (human-confirmed)
    or, for timed halts, by halt_until passing."""
    if decision.verdict == "flatten_halt":
        ledger.append(
            "halt",
            {
                "reason": decision.reason,
                "halt_until": decision.halt_until.isoformat() if decision.halt_until else None,
            },
        )


def cmd_gate(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = Ledger(state_dir)
    cfg = load_state_config(state_dir)
    signal, costs = _load_signal(args.signal_json)
    decision = RiskGate(cfg.risk_config()).gate(signal, costs, ledger.portfolio(cfg))
    ledger.append("gate_decision", {"decision": decision.model_dump(mode="json"), "asset": signal.asset})
    _persist_halt_if_any(ledger, decision)
    return _emit({"decision": decision.model_dump(mode="json")})


def cmd_propose(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = Ledger(state_dir)
    cfg = load_state_config(state_dir)
    signal, costs = _load_signal(args.signal_json)
    portfolio = ledger.portfolio(cfg)
    decision = RiskGate(cfg.risk_config()).gate(signal, costs, portfolio)
    ledger.append("gate_decision", {"decision": decision.model_dump(mode="json"), "asset": signal.asset})
    _persist_halt_if_any(ledger, decision)
    if decision.verdict != "action":
        return _emit({"proposal": None, "decision": decision.model_dump(mode="json")})
    proposal = Proposal(
        proposal_id=new_proposal_id(),
        signal=signal,
        target_position_pct=decision.target_position_pct,
        current_position_pct=decision.current_position_pct,
        delta_pct=decision.target_position_pct - decision.current_position_pct,
        gate_reason=decision.reason,
        created_at=datetime.now(UTC),
    )
    ledger.record_proposal(proposal)
    return _emit(
        {"proposal": proposal.model_dump(mode="json"), "decision": decision.model_dump(mode="json")}
    )


def cmd_decide(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = Ledger(state_dir)
    pending = {p.proposal_id for p in ledger.pending_proposals()}
    if args.proposal_id not in pending:
        return _emit({"error": f"proposal {args.proposal_id} not pending"}, ok=False)
    if args.decision == "approval":
        # R1-05: deterministic TTL — a stale proposal can never be approved.
        # Rejection/expired are still allowed (closing stale state is fine).
        ttl = load_state_config(state_dir).risk_config().proposal_ttl_hours
        fresh = {p.proposal_id for p in ledger.pending_proposals(ttl_hours=ttl)}
        if args.proposal_id not in fresh:
            return _emit(
                {
                    "error": (
                        f"proposal {args.proposal_id} is stale (older than the "
                        f"{ttl}h proposal TTL); approval refused — run `expire` "
                        "or re-propose against current data"
                    )
                },
                ok=False,
            )
    rec = ledger.record_decision_on_proposal(args.proposal_id, args.decision, args.note or "")
    return _emit({"recorded": rec})


def cmd_fill(args: argparse.Namespace) -> int:
    """Record a human-confirmed fill. This is the LAST seam before the book,
    so it re-validates everything the prompt layer promised (R1-02): the fill
    must reference an approved, never-filled proposal for the same asset, and
    its size must be on the 0.05 ladder, within the approved target, and in
    the target's direction. Humans may size DOWN or flatten — never up."""
    ledger = Ledger(Path(args.state_dir))
    fill = Fill(**json.loads(Path(args.fill_json).read_text()))

    def refuse(msg: str) -> int:
        return _emit({"error": msg}, ok=False)

    events = ledger.events()
    proposal = None
    for rec in events:
        if rec["event"] == "proposal" and rec["proposal"]["proposal_id"] == fill.proposal_id:
            proposal = rec["proposal"]
    if proposal is None:
        return refuse(f"proposal {fill.proposal_id} not found in ledger")
    if not any(
        rec["event"] == "approval" and rec["proposal_id"] == fill.proposal_id for rec in events
    ):
        return refuse(f"proposal {fill.proposal_id} has no recorded approval")
    if any(
        rec["event"] == "fill" and rec["fill"]["proposal_id"] == fill.proposal_id
        for rec in events
    ):
        return refuse(
            f"proposal {fill.proposal_id} already has a recorded fill — "
            "double-fill refused (one proposal, one fill)"
        )
    prop_asset = proposal["signal"]["asset"]
    if fill.asset != prop_asset:
        return refuse(
            f"fill asset {fill.asset!r} does not match proposal asset {prop_asset!r}"
        )
    target = float(proposal["target_position_pct"])
    pct = float(fill.filled_position_pct)
    if abs(pct) > abs(target) + 1e-9:
        return refuse(
            f"fill size {pct} exceeds the approved target {target} — the human "
            "may size DOWN or flatten, never up"
        )
    if abs(pct) > 1e-9:  # 0.0 == flatten, always ladder-legal
        rem = abs(pct) % 0.05
        if min(rem, 0.05 - rem) > 1e-9:
            return refuse(
                f"fill size {pct} is not a 0.05 ladder multiple — "
                "off-ladder sizes never enter the book (rail #3)"
            )
        if pct * target < 0:
            return refuse(
                f"fill direction (sign of {pct}) differs from the approved "
                f"target {target} — direction flips need a new proposal"
            )
    rec = ledger.record_fill(fill)
    return _emit({"recorded": rec})


def cmd_mark(args: argparse.Namespace) -> int:
    """Record a price/NAV mark. R1-06 lean mitigation: self-reported marks get
    sanity guards — positive finite values, and continuity vs the previous
    mark (price within 50% per asset, NAV within 30%) unless --allow-jump is
    passed for a documented discontinuity (split, deposit). The first mark for
    an asset (or the first NAV) is unconstrained beyond positivity."""
    ledger = Ledger(Path(args.state_dir))
    price = float(args.price)
    nav = float(args.nav)
    if not math.isfinite(price) or price <= 0:
        return _emit(
            {"error": f"mark price must be positive and finite, got {args.price}"}, ok=False
        )
    if not math.isfinite(nav) or nav <= 0:
        return _emit(
            {"error": f"mark nav must be positive and finite, got {args.nav}"}, ok=False
        )
    prev_price: float | None = None
    prev_nav: float | None = None
    for rec in ledger.events():
        if rec["event"] == "mark":
            prev_nav = float(rec["nav"])
            if rec["asset"] == args.asset:
                prev_price = float(rec["price"])
    if not args.allow_jump:
        if prev_price is not None and abs(price - prev_price) / prev_price > 0.50:
            return _emit(
                {
                    "error": (
                        f"mark price {price} differs from the previous {args.asset} "
                        f"mark {prev_price} by more than 50% — refused; if this is a "
                        "split or other documented discontinuity, re-run with --allow-jump"
                    )
                },
                ok=False,
            )
        if prev_nav is not None and abs(nav - prev_nav) / prev_nav > 0.30:
            return _emit(
                {
                    "error": (
                        f"nav {nav} differs from the previous nav {prev_nav} by more "
                        "than 30% — refused; if this is a real discontinuity "
                        "(deposit/withdrawal), re-run with --allow-jump"
                    )
                },
                ok=False,
            )
    rec = ledger.record_mark(args.asset, price, nav)
    return _emit({"recorded": rec})


def cmd_resume(args: argparse.Namespace) -> int:
    """HUMAN-ONLY: clear an active halt. The calling command prompt MUST have
    confirmed with the human before invoking this — quantcore cannot verify
    the caller, so the confirmation gate is a documented prompt obligation."""
    ledger = Ledger(Path(args.state_dir))
    if not ledger.portfolio().halted:
        return _emit({"error": "no active halt — nothing to resume"}, ok=False)
    rec = ledger.append("resume", {"note": args.note or ""})
    return _emit({"recorded": rec})


def cmd_expire(args: argparse.Namespace) -> int:
    """Sweep stale pending proposals into explicit 'expired' events (R1-05).
    Idempotent: expired proposals leave the pending set, so a second run is a
    no-op. Fresh proposals are never touched."""
    state_dir = Path(args.state_dir)
    ledger = Ledger(state_dir)
    ttl = load_state_config(state_dir).risk_config().proposal_ttl_hours
    fresh = {p.proposal_id for p in ledger.pending_proposals(ttl_hours=ttl)}
    expired = [
        ledger.record_decision_on_proposal(
            p.proposal_id, "expired", f"auto-expired: older than {ttl}h proposal TTL"
        )
        for p in ledger.pending_proposals()
        if p.proposal_id not in fresh
    ]
    return _emit({"n_expired": len(expired), "expired": expired})


def cmd_settle(args: argparse.Namespace) -> int:
    new = settle(Ledger(Path(args.state_dir)))
    return _emit({"n_settled": len(new), "settled": new})


def cmd_aggregate(args: argparse.Namespace) -> int:
    """Deterministic committee aggregation (B-05): replaces the in-prompt
    arithmetic. Input file: {"views": [AnalystView, ...]}. Calibration-based
    ECE shrinkage is applied per-view BEFORE aggregation."""
    import json as _json

    from quantcore.aggregate import aggregate, shrink_confidence
    from quantcore.schemas import AnalystView
    from quantcore.settle import calibration_report

    state_dir = Path(args.state_dir)
    raw = _json.loads(Path(args.views_json).read_text())
    views = [AnalystView(**v) for v in raw["views"]]
    calib_path = state_dir / "calibration.json"
    calibration = (
        _json.loads(calib_path.read_text()) if calib_path.exists() else {}
    )
    report = calibration_report(state_dir)
    shrunk = []
    for v in views:
        ece = (report.get(v.analyst) or {}).get("ece")
        if ece is not None:
            v = v.model_copy(update={"confidence": shrink_confidence(v.confidence, ece)})
        shrunk.append(v)
    result = aggregate(shrunk, calibration)
    return _emit({"aggregate": result})


def cmd_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = Ledger(state_dir)
    cfg = load_state_config(state_dir)
    portfolio = ledger.portfolio(cfg)
    ok, msg = ledger.verify_chain()
    return _emit(
        {
            "portfolio": portfolio.model_dump(mode="json"),
            "pending_proposals": [p.model_dump(mode="json") for p in ledger.pending_proposals()],
            "ledger_integrity": {"ok": ok, "detail": msg},
            "calibration": calibration_report(state_dir),
            "risk_profile": cfg.profile,
        }
    )


def cmd_verify(args: argparse.Namespace) -> int:
    ledger_ok, ledger_msg = Ledger(Path(args.state_dir)).verify_chain()
    from quantcore.hypotheses import HypothesisRegistry

    hyp_ok, hyp_msg = HypothesisRegistry(Path(args.state_dir)).verify_chain()
    return _emit(
        {"ledger": ledger_msg, "hypotheses": hyp_msg},
        ok=ledger_ok and hyp_ok,
    )


def cmd_events(args: argparse.Namespace) -> int:
    from quantcore.calendar_events import (
        DEFAULT_SEED_PATH,
        freshness_check,
        load_seed,
        upcoming,
    )

    seed = Path(args.seed) if args.seed else DEFAULT_SEED_PATH
    events, warnings = load_seed(seed)
    asof = (
        datetime.fromisoformat(args.asof.replace("Z", "+00:00"))
        if args.asof
        else datetime.now(UTC)
    )
    out = upcoming(
        events,
        asof=asof,
        window_days=float(args.window_days),
        high_impact_only=not args.all_impact,
    )
    fresh = freshness_check(events, asof=asof)
    return _emit({"events": out, "seed_warnings": warnings, "freshness": fresh})


def cmd_regime(args: argparse.Namespace) -> int:
    from quantcore.regime import classify_regime

    payload = json.loads(Path(args.closes_json).read_text())
    closes = payload["closes"] if isinstance(payload, dict) else payload
    asof = (
        datetime.fromisoformat(args.asof.replace("Z", "+00:00"))
        if args.asof
        else datetime.now(UTC)
    )
    read = classify_regime([float(c) for c in closes], asof)
    return _emit({"regime": read.model_dump(mode="json")})


def cmd_hyp(args: argparse.Namespace) -> int:
    from quantcore.hypotheses import HypothesisRegistry

    reg = HypothesisRegistry(Path(args.state_dir))
    action = args.action
    if action == "create":
        h = reg.create(statement=args.statement)
        return _emit({"hypothesis": h.model_dump(mode="json")})
    if action == "forecast":
        f = reg.forecast(args.hypothesis_id, p=float(args.p), horizon=args.horizon)
        return _emit({"forecast": f.model_dump(mode="json")})
    if action == "resolve":
        outcome = args.outcome.strip().lower() in ("true", "1", "yes")
        fc = reg.resolve_forecast(args.hypothesis_id, args.forecast_id, outcome)
        return _emit({"resolved": fc.model_dump(mode="json")})
    if action == "status":
        h = reg.set_status(args.hypothesis_id, args.to, note=args.note or "")
        return _emit({"status": h.model_dump(mode="json")})
    if action == "link":
        h = reg.link_proposal(args.hypothesis_id, args.proposal_id)
        return _emit({"linked": h.model_dump(mode="json")})
    if action == "summary":
        return _emit(
            {
                "summary": reg.brier_summary(),
                "open": [h.model_dump(mode="json") for h in reg.open_hypotheses()],
            }
        )
    return _emit({"error": f"unknown hyp action {action}"}, ok=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="quantcore")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, *flags: tuple[str, dict]):
        sp = sub.add_parser(name)
        sp.add_argument("--state-dir", required=True)
        for flag, kw in flags:
            sp.add_argument(flag, **kw)
        sp.set_defaults(fn=fn)

    add("gate", cmd_gate, ("--signal-json", {"required": True}))
    add("propose", cmd_propose, ("--signal-json", {"required": True}))
    add(
        "decide",
        cmd_decide,
        ("--proposal-id", {"required": True}),
        ("--decision", {"required": True, "choices": ["approval", "rejection", "expired"]}),
        ("--note", {"default": ""}),
    )
    add("fill", cmd_fill, ("--fill-json", {"required": True}))
    add(
        "mark",
        cmd_mark,
        ("--asset", {"required": True}),
        ("--price", {"required": True}),
        ("--nav", {"required": True}),
        ("--allow-jump", {"action": "store_true"}),
    )
    add("settle", cmd_settle)
    add("aggregate", cmd_aggregate, ("--views-json", {"required": True}))
    add("status", cmd_status)
    add("verify", cmd_verify)
    add("expire", cmd_expire)
    # resume is HUMAN-ONLY (see cmd_resume docstring): the prompt must confirm.
    add("resume", cmd_resume, ("--note", {"required": True}))

    # events/regime don't need --state-dir; register without the helper
    sp = sub.add_parser("events")
    sp.add_argument("--seed", default=None)
    sp.add_argument("--asof", default=None)
    sp.add_argument("--window-days", default="7")
    sp.add_argument("--all-impact", action="store_true")
    sp.set_defaults(fn=cmd_events)

    sp = sub.add_parser("regime")
    sp.add_argument("--closes-json", required=True)
    sp.add_argument("--asof", default=None)
    sp.set_defaults(fn=cmd_regime)

    add(
        "hyp",
        cmd_hyp,
        ("action", {"choices": ["create", "forecast", "resolve", "status", "link", "summary"]}),
        ("--statement", {"default": ""}),
        ("--hypothesis-id", {"default": ""}),
        ("--forecast-id", {"default": ""}),
        ("--p", {"default": "0.5"}),
        ("--horizon", {"default": "5d"}),
        ("--outcome", {"default": ""}),
        ("--to", {"default": ""}),
        ("--note", {"default": ""}),
        ("--proposal-id", {"default": ""}),
    )

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as e:  # noqa: BLE001 — fail closed with a JSON error
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
