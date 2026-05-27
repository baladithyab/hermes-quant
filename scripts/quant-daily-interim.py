#!/usr/bin/env python3
"""
quant-daily-interim.py — INTERIM daily picker (Phase 0.2 of 2026-05-23 plan).

This is intentionally minimal: it scans the universe with the existing
hermes-quant advisor (equity directional bias only, no options yet) and
prints a brief that the cron LLM wrapper formats for Discord.

The full options picker (covered call / CSP / wheel / LEAPS / swing) lands
in Phase 4 Wave C and replaces this script. Until then this gives Codeseys
a daily artifact while we build the real thing.

Posture preserved from AGENTS.md:
- READ-ONLY (no orders, no state mutation)
- Silence-by-default (skips symbols with insufficient bars or stale data)
- Money never goes through tools (this script prints text; the LLM
  wrapper sends it; user approves any actual trade in HITL)
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make sure we use the hermes-agent venv where hermes-quant is installed.
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

UNIVERSE_PATH = Path.home() / ".hermes" / "scripts" / "quant-universe-interim.txt"
WATCHLIST_PATH = Path.home() / ".hermes" / "quant" / "watchlist" / "play-fit.json"


def load_universe() -> list[tuple[str, str, str]]:
    """Load the universe of symbols to score.

    Strategy (post-2026-05-26 watchlist-shadow fix):
      - Read the txt fallback first as the BASELINE universe (~38 symbols).
      - Then merge in active watchlist symbols, preserving their tier
        (defaults to "active" so they get scanned every day, not just EOD).
      - Symbols already in the txt fallback keep their txt tier; new
        symbols from the watchlist are appended as "active".

    This avoids the regression where a sparse watchlist (e.g. 7 symbols
    from a single onboarding wave) silently shadowed the 38-symbol
    txt-defined universe. Union semantics: txt is the floor, watchlist
    adds to it.

    Returns: list of (ticker, asset_class, tier).
      asset_class is always 'midcap' (we don't differentiate downstream).
      tier is 'active' for watchlist symbols, or per-row for the txt file.
    """
    # Baseline: txt fallback.
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    if UNIVERSE_PATH.exists():
        for line in UNIVERSE_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            ticker, klass, tier = parts[0].upper(), parts[1], parts[2]
            if ticker not in seen:
                seen.add(ticker)
                rows.append((ticker, klass, tier))

    # Merge: append active watchlist symbols not already present.
    if WATCHLIST_PATH.exists():
        try:
            data = json.loads(WATCHLIST_PATH.read_text())
            for play, entries in (data.get("plays") or {}).items():
                for entry in entries:
                    if entry.get("state") != "active":
                        continue
                    sym = (entry.get("symbol") or "").upper()
                    if sym and sym not in seen:
                        seen.add(sym)
                        rows.append((sym, "midcap", "active"))
        except Exception:
            # Watchlist parse failure is non-fatal — txt baseline is fine.
            pass

    return rows


def recommend_one(symbol: str, asset_class: str = "equity", timeframe: str = "1d") -> dict:
    """Wrap advisor.recommend with safe error handling.

    advisor.recommend returns a dict shaped like:
        {symbol, asset_class, timeframe, as_of, recipe, data_quality,
         analyst_views: list, aggregated_signal: dict|None,
         risk_gate: {pass, gated_reason, kelly_fraction, recommended_action},
         lessons, caveats, doctor}

    The full advisor result is preserved in `_advisor_result` so the brief
    main() can later hand it to ProposalStore.propose() for actionable picks
    — that is what makes `approve <PROPOSAL_ID>` resolvable later. Without
    this the HITL contract printed in the brief is broken (no proposals get
    created, so `quant_approve` always returns not_found).
    """
    try:
        from hermes_quant.advisor import recommend
        result = recommend(symbol=symbol, asset_class=asset_class, timeframe=timeframe)
        agg = result.get("aggregated_signal") or {}
        gate = result.get("risk_gate") or {}
        dq = result.get("data_quality") or {}
        # Coerce dataclasses → dict if needed
        if hasattr(agg, "__dict__") and not isinstance(agg, dict):
            agg = {f: getattr(agg, f, None)
                   for f in ("direction", "confidence", "magnitude", "horizon")}
        return {
            "symbol": symbol,
            "ok": True,
            "asset_class": asset_class,
            "timeframe": timeframe,
            "data_ok": bool(dq.get("bars_received", 0) > 0),
            "data_quality": dq,
            "direction": agg.get("direction") if isinstance(agg, dict) else None,
            "confidence": agg.get("confidence") if isinstance(agg, dict) else None,
            "magnitude": agg.get("magnitude") if isinstance(agg, dict) else None,
            "horizon": agg.get("horizon") if isinstance(agg, dict) else None,
            "kelly_fraction": gate.get("kelly_fraction"),
            "gate_pass": gate.get("pass", False),
            "gated_reason": gate.get("gated_reason"),
            "recommended_action": gate.get("recommended_action"),
            "rationale": (result.get("caveats", [None]) or [None])[0],
            "lessons": result.get("lessons", []),
            # Stash the full advisor result for downstream proposal creation.
            # NOT serialized into the brief — only used in main() before format.
            "_advisor_result": result,
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "ok": False,
            "asset_class": asset_class,
            "timeframe": timeframe,
            "error": f"{type(exc).__name__}: {exc}",
            "trace_short": traceback.format_exc().splitlines()[-1] if traceback.format_exc() else None,
        }


def create_proposals_for_actionables(actionables: list[dict]) -> list[dict]:
    """Persist a Proposal per actionable pick so HITL `approve <ID>` works.

    Also builds a TraderProposal (ADR-0044 Wave 2) for each actionable and
    embeds it in advisor_result['trader_proposal'] before the Proposal is
    persisted.  This ensures every approval in the audit trail carries
    explicit stop_loss + entry_price + time_horizon.

    Returns the same list with `proposal_id` and `expires_at` populated on
    each entry. Items where proposal creation failed are left without a
    proposal_id and a `proposal_error` field is set; the brief still lists
    them but flags them as non-approvable.

    Notes:
      - We strip the bulky `_advisor_result` from the returned dicts before
        the brief formatter sees them (keeps the persisted JSON sane), but
        the full result IS handed to store.propose() so approval can replay
        the gate decision.
      - TTL defaults to 15 minutes per ADR-0015 §D9. For a daily/EOD brief
        that's tight; we override to 24h so the operator has overnight to
        respond.
    """
    try:
        from hermes_quant.proposals import get_default_store
    except Exception as exc:
        # Non-fatal: brief still goes out, no proposals get created.
        for v in actionables:
            v["proposal_error"] = f"proposal store unavailable: {type(exc).__name__}: {exc}"
            v.pop("_advisor_result", None)
        return actionables

    # --- ADR-0044 Wave 2: build TraderProposal for each actionable ---
    try:
        from hermes_quant.agents.trader import TraderNode
        _trader_node = TraderNode()
    except Exception as exc:
        _trader_node = None  # type: ignore[assignment]

    # Wave 3 (ADR-0043): 3-way risk committee runs after the trader,
    # before the deterministic risk gate. Failure to construct is non-fatal —
    # we just skip risk-debate enrichment.
    try:
        from hermes_quant.agents.risk_committee import RiskCommittee
        _risk_committee = RiskCommittee()
    except Exception:
        _risk_committee = None  # type: ignore[assignment]

    store = get_default_store()
    for v in actionables:
        full = v.pop("_advisor_result", None)
        if full is None:
            v["proposal_error"] = "no advisor_result captured"
            continue

        # Build TraderProposal and embed in advisor_result
        if _trader_node is not None:
            try:
                research_plan = _extract_research_plan(full)
                advisor_signal = full.get("aggregated_signal") or {}
                # Coerce dataclass → dict for TraderNode
                if hasattr(advisor_signal, "__dict__") and not isinstance(advisor_signal, dict):
                    advisor_signal = {
                        f: getattr(advisor_signal, f, None)
                        for f in ("direction", "confidence", "magnitude", "metadata",
                                  "data_quality")
                    }
                trader_proposal = _trader_node(research_plan, advisor_signal)
                full["trader_proposal"] = trader_proposal.model_dump(mode="json")
                # Surface key fields for the brief formatter
                v["trader_stop_loss"] = full["trader_proposal"].get("stop_loss")
                v["trader_entry_price"] = full["trader_proposal"].get("entry_price")
                v["trader_target_price"] = full["trader_proposal"].get("target_price")
                v["trader_time_horizon_days"] = full["trader_proposal"].get("time_horizon_days")
                v["trader_size_fraction"] = full["trader_proposal"].get("size_fraction")
                v["trader_warning"] = full["trader_proposal"].get("warning_message")

                # Wave 3 (ADR-0043): 3-way risk committee debate.
                # Committee can ONLY silence (multiplier ≤ 1.0), never amplify.
                if _risk_committee is not None:
                    try:
                        risk_summary = _risk_committee.debate(
                            trader_proposal,
                            research_plan,
                            proposal_id=v.get("symbol", "unknown"),
                        )
                        full["risk_debate_summary"] = risk_summary.model_dump(mode="json")
                        v["risk_silence_multiplier"] = risk_summary.silence_multiplier
                        v["risk_final_recommendation"] = risk_summary.final_recommendation
                        v["risk_n_rounds"] = risk_summary.n_rounds
                    except Exception as exc_rc:
                        v["risk_debate_error"] = f"{type(exc_rc).__name__}: {exc_rc}"
            except Exception as exc_tp:
                v["trader_proposal_error"] = f"{type(exc_tp).__name__}: {exc_tp}"

        try:
            proposal = store.propose(
                symbol=v["symbol"],
                asset_class=v.get("asset_class") or "equity",
                timeframe=v.get("timeframe") or "1d",
                advisor_result=full,
                ttl_minutes=24 * 60,  # 24h — daily-cadence brief
            )
            v["proposal_id"] = proposal.proposal_id
            v["expires_at"] = proposal.expires_at
        except Exception as exc:
            v["proposal_error"] = f"{type(exc).__name__}: {exc}"
    return actionables


def _extract_research_plan(advisor_result: dict) -> dict:
    """Extract a research_plan dict from advisor_result for TraderNode.

    Tries several locations where the committee output may live:
      1. advisor_result['research_plan']  (ideal — if committee_runner stored it)
      2. Reconstruct from aggregated_signal recommendation fields
      3. Conservative fallback: Hold with low confidence

    Returns a dict compatible with TraderNode expectations:
        {recommendation, confidence, rationale, strategic_actions, horizon_emphasis}
    """
    # Path 1: direct research_plan key
    rp = advisor_result.get("research_plan")
    if isinstance(rp, dict) and rp.get("recommendation"):
        return rp

    # Path 2: reconstruct from aggregated_signal + committee turns
    agg = advisor_result.get("aggregated_signal") or {}
    if hasattr(agg, "__dict__") and not isinstance(agg, dict):
        agg = {f: getattr(agg, f, None) for f in ("direction", "confidence", "magnitude")}

    direction = agg.get("direction") if isinstance(agg, dict) else None
    confidence = agg.get("confidence") if isinstance(agg, dict) else 0.5

    # Map direction → 5-tier recommendation
    if direction == 1:
        recommendation = "Buy"
    elif direction == -1:
        recommendation = "Sell"
    else:
        recommendation = "Hold"

    # Pull rationale from caveats or lessons
    caveats = advisor_result.get("caveats") or []
    rationale = (caveats[0] if caveats else None) or "Signal generated by hermes-quant advisor."

    return {
        "recommendation": recommendation,
        "confidence": float(confidence) if confidence is not None else 0.5,
        "rationale": str(rationale)[:2000],
        "strategic_actions": "Execute via paper broker at market open.",
        "horizon_emphasis": None,
    }


def rank_picks(views: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Sort views into actionable / silent / data_blocked / failed.

    actionable    = ok + data_ok + risk_gate passed (recommended_action != gated)
    silent        = ok + data_ok + gate said hold cash (low conviction or rules tripped)
    data_blocked  = ok-but-no-data (weekend, listing changes, yfinance hiccup) — not an error
    failed        = exception during recommend()
    """
    actionable, silent, data_blocked, failed = [], [], [], []
    for v in views:
        if not v.get("ok"):
            failed.append(v)
            continue
        if not v.get("data_ok"):
            data_blocked.append(v)
            continue
        if v.get("gate_pass") and v.get("direction") in (-1, 1):
            actionable.append(v)
        else:
            silent.append(v)

    def score(v):
        c = v.get("confidence")
        return 0.0 if c is None else abs(c - 0.5)

    actionable.sort(key=score, reverse=True)
    silent.sort(key=score, reverse=True)
    return actionable, silent, data_blocked, failed


def format_brief(actionable: list[dict], silent: list[dict],
                 data_blocked: list[dict], failed: list[dict],
                 universe_size: int, halt_note: str | None = None) -> str:
    now = datetime.now(timezone.utc).astimezone()
    lines = []
    lines.append(f"# 📊 Hermes-Quant Daily Brief — {now.strftime('%a %Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append("> ⚠️ **Interim build** — equity directional bias only. "
                 "Options layer (covered calls, CSP, wheel, LEAPS, swings) lands later this week.")
    lines.append("")
    if halt_note:
        lines.append(f"> 🛑 **Account halted:** {halt_note}")
        lines.append("> Picks below are advisory only until lift; `approve` will refuse.")
        lines.append("")
    lines.append(f"**Universe scanned:** {universe_size}  •  "
                 f"**Actionable:** {len(actionable)}  •  "
                 f"**Silent (low conviction):** {len(silent)}  •  "
                 f"**Data-blocked:** {len(data_blocked)}  •  "
                 f"**Errors:** {len(failed)}")
    lines.append("")

    if actionable:
        lines.append("## 🎯 Top picks (HITL — reply `approve <PROPOSAL_ID>` to paper-fire)")
        lines.append("")
        for v in actionable[:5]:
            d = v.get("direction")
            arrow = "🟢 LONG" if d == 1 else "🔴 SHORT" if d == -1 else "⚪ FLAT"
            conf = v.get("confidence")
            mag = v.get("magnitude")
            kelly = v.get("kelly_fraction")
            rat = v.get("rationale") or ""
            pid = v.get("proposal_id")
            perr = v.get("proposal_error")
            lines.append(f"- **{v['symbol']}** {arrow}  "
                         f"conf={conf:.2f} mag={mag:+.3%} "
                         f"kelly={kelly:.3f} horizon={v.get('horizon','?')}")
            if pid:
                lines.append(f"  - `{pid}`  ← `approve {pid}` to paper-fire  "
                             f"(expires {v.get('expires_at','?')})")
            elif perr:
                lines.append(f"  - ⚠️ proposal not created: {perr}")
            # ADR-0044 Wave 2: TraderProposal fields
            entry = v.get("trader_entry_price")
            stop = v.get("trader_stop_loss")
            target = v.get("trader_target_price")
            horizon_days = v.get("trader_time_horizon_days")
            size_frac = v.get("trader_size_fraction")
            tw = v.get("trader_warning")
            trader_parts = []
            if entry is not None:
                trader_parts.append(f"entry≈${entry:.2f}")
            if stop is not None:
                trader_parts.append(f"stop=${stop:.2f}")
            if target is not None:
                trader_parts.append(f"target=${target:.2f}")
            if horizon_days is not None:
                trader_parts.append(f"horizon={horizon_days}d")
            if size_frac is not None:
                trader_parts.append(f"size={size_frac:.0%}")
            if trader_parts:
                lines.append(f"  - 📐 {' | '.join(trader_parts)}")
            if tw:
                lines.append(f"  - ⚠️ trader-warning: {tw}")
            elif v.get("trader_proposal_error"):
                lines.append(f"  - ⚠️ trader-proposal error: {v['trader_proposal_error']}")

            # Wave 3 (ADR-0043): Risk debate subsection — 3 personas' critiques
            # and the final silence_multiplier.
            risk_summary = (v.get("_advisor_result") or {}).get("risk_debate_summary")
            if risk_summary is None:
                # The advisor_result was already popped/embedded; the formatter
                # may need to re-read it from the surfaced fields.
                pass
            silence_mult = v.get("risk_silence_multiplier")
            if silence_mult is not None:
                lines.append(
                    f"  - 🛡️ Risk debate (mult={silence_mult:.2f}, "
                    f"{v.get('risk_n_rounds', 0)} round(s)): "
                    f"{v.get('risk_final_recommendation', '')}"
                )
            elif v.get("risk_debate_error"):
                lines.append(f"  - ⚠️ risk-debate error: {v['risk_debate_error']}")
            if rat:
                lines.append(f"  - {rat}")
        lines.append("")

    if silent:
        top_silent = ", ".join(f"{v['symbol']}({v.get('confidence',0):.2f})" for v in silent[:8])
        lines.append(f"## 💤 Watching (no conviction): {top_silent}")
        lines.append("")

    if data_blocked:
        blocked_syms = ", ".join(v["symbol"] for v in data_blocked[:15])
        lines.append(f"## 🚧 Data-blocked ({len(data_blocked)}, weekend / provider): {blocked_syms}")
        lines.append("")

    if failed:
        lines.append(f"## ⚠️ Errors ({len(failed)})")
        for v in failed[:5]:
            lines.append(f"- {v['symbol']}: {v.get('error','unknown')}")
        lines.append("")

    lines.append("---")
    lines.append("_Disclaimer: educational analysis only, not financial advice. "
                 "This is a paper-trading research system._")
    return "\n".join(lines)


def _read_halt_note() -> str | None:
    """Return a short halt note if any active halt covers the paper account.

    Reads ~/.hermes/quant/halt_state.json. Surfaces an account-wide halt
    (`asset_class == "*"`) for `alpaca-paper` so the brief doesn't pretend
    actionables are approvable when they're not. Crypto-only halts are
    ignored (equity brief). Returns None when nothing relevant is halted.
    """
    halt_path = Path.home() / ".hermes" / "quant" / "halt_state.json"
    if not halt_path.exists():
        return None
    try:
        halts = json.loads(halt_path.read_text())
    except Exception:
        return None
    for h in halts or []:
        if h.get("account_id") != "alpaca-paper":
            continue
        if h.get("asset_class") not in ("*", "equity", None):
            continue
        reason = (h.get("reason") or "").splitlines()[0][:200]
        epoch = h.get("halt_epoch")
        return f"epoch={epoch} — {reason}"
    return None


def main() -> int:
    universe = load_universe()
    # Active tier scanned every day; watch tier only at EOD or when --eod is passed
    is_eod = "--eod" in sys.argv
    rows = [r for r in universe if r[2] == "active" or (is_eod and r[2] == "watch")]
    views = [recommend_one(t, "equity", "1d") for t, _klass, _tier in rows]
    actionable, silent, data_blocked, failed = rank_picks(views)
    # Persist Proposals so HITL `approve <PROPOSAL_ID>` actually has a target.
    actionable = create_proposals_for_actionables(actionable)
    # Strip _advisor_result from non-actionable views before persisting JSON
    # (it's huge and not needed by the retro loop). Actionables already had
    # it stripped inside create_proposals_for_actionables.
    for bucket in (silent, data_blocked, failed):
        for v in bucket:
            v.pop("_advisor_result", None)
    halt_note = _read_halt_note()
    brief = format_brief(actionable, silent, data_blocked, failed, len(rows), halt_note=halt_note)
    # Print to stdout — the cron LLM wrapper picks this up
    print(brief)
    # Also persist a structured copy for later retro-loop ingestion
    out_dir = Path.home() / ".hermes" / "quant" / "daily-briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}-interim.json").write_text(json.dumps(
        {"actionable": actionable, "silent": silent,
         "data_blocked": data_blocked, "failed": failed,
         "universe_size": len(rows), "is_eod": is_eod,
         "halt_note": halt_note},
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
