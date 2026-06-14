"""hermes_quant.portfolio.state — reconstruct PortfolioState from executions.jsonl.

ADR-0071 dependency. The portfolio-aware Stage-2 sizer (`risk.portfolio_normalize`)
needs a current-positions snapshot, but `state.db` only has a `halts` table as of
2026-05-28. This module provides the reconstruction.

Semantics (PaperReactor pre-ADR-0029):
    Each fill carries `target_position_pct` = the NEW intended size for that symbol.
    Two fills on AAPL "+0.20" and "-0.20" do NOT cancel — the second supersedes the
    first. So:

        positions[symbol] = LATEST fill's target_position_pct, by asof_execution

    A target_position_pct of 0.0 means "close" (the position is gone).

    NOT delta-summed.

Forward compatibility:
    When ADR-0029 multi-leg lands, the per-leg fills change shape and this helper
    will need to evolve to aggregate by leg. Today single-symbol equity is the only
    asset class hitting PaperReactor.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.risk.portfolio_normalize import PortfolioState

_DEFAULT_EXECUTIONS_PATH = Path("~/.hermes/quant/executions.jsonl").expanduser()


def _record_account(rec: dict) -> str:
    """Resolve the account partition a bus record belongs to.

    cs18: the executions.jsonl bus carries NO top-level ``account_id`` for the
    live equity producers (verified on the live bus: 0/46 records). The account
    lives inside ``reactor_metadata.account_id`` (react/paper.py emits no acct;
    react/deterministic_equity.py + react/alpaca_paper.py both nest it there).

    Resolution mirrors the cs14 weekly-exit loader EXACTLY
    (daemon/portfolio_loader.py:103-110, operator-approved 4d5cc42):
        top-level ``account_id``  ->  ``reactor_metadata.account_id``  ->
        the ``"paper-default"`` sentinel.

    So a "paper" fill (no account stamp) and a "deterministic-equity" fill (acct
    "paper-default") both resolve to ``paper-default`` — the synthetic book the
    autonomous fires actually hit — while an "alpaca_paper" fill resolves to the
    separate ``alpaca-paper`` SHADOW book.
    """
    acct = rec.get("account_id")
    if acct:
        return str(acct)
    meta_acct = (rec.get("reactor_metadata") or {}).get("account_id")
    if meta_acct:
        return str(meta_acct)
    return "paper-default"


def reconstruct_portfolio_state(
    executions_path: Path | str | None = None,
    *,
    asof: str | None = None,
    drop_zeros: bool = True,
    reactor_filter: str | None = "paper",
    account: str | None = None,
) -> PortfolioState:
    """Walk executions.jsonl and return a PortfolioState snapshot.

    Args:
        executions_path: path to the executions JSONL log. Defaults to
            `~/.hermes/quant/executions.jsonl`.
        asof: ISO-8601 UTC string. If provided, only fills with
            `asof_execution <= asof` contribute. Default: include all.
        drop_zeros: if True (default), positions whose latest fill's
            target_position_pct is 0 are dropped from the snapshot
            (= "the position is closed"). If False, they're retained as
            explicit zero entries.
        reactor_filter: only fills whose `reactor_name` matches contribute.
            Default "paper" — keeps live broker fills out of the paper-state
            view if/when both rails are running. Pass None to include all.
        account: cs18 — if set (e.g. "paper-default"), only fills whose resolved
            account partition (see `_record_account`) matches contribute. This
            EXCLUDES the deliberately-separate ``alpaca-paper`` SHADOW book from a
            ``paper-default`` reconstruction. Default None = include ALL accounts
            (byte-identical to the pre-cs18 whole-book behavior — the asset-only
            key collapses cross-account symbols and pools cross-account fractions,
            which is the cs18 pooling bug when reactor_filter is also None). This
            is ADDITIVE: it does NOT change the {symbol: float} return type; it
            only narrows which records feed the existing asset-keyed collapse.

    Returns:
        PortfolioState with the snapshot's `positions` dict.

    Behavior on missing/empty log:
        Empty file or nonexistent path: returns empty PortfolioState (cash=100%).
        Malformed JSON line: skipped silently (log warning is emitted by the
        caller's logging layer if needed; this helper is fail-soft so a single
        bad line doesn't black-hole the whole reconstruction).
    """
    path = (
        Path(executions_path).expanduser()
        if executions_path is not None
        else _DEFAULT_EXECUTIONS_PATH
    )

    if not path.exists():
        return PortfolioState(positions={})

    # Walk in order; later fills supersede earlier fills per symbol.
    latest_per_symbol: dict[str, tuple[str, float]] = {}
    # symbol -> (asof_execution, target_position_pct)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return PortfolioState(positions={})

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue  # valid JSON but not an object (corrupt/partial append) — skip

        if reactor_filter is not None:
            if rec.get("reactor_name") != reactor_filter:
                continue

        # cs18: when an account partition is requested, drop records that resolve
        # to a DIFFERENT account BEFORE the asset-only collapse, so a separate book
        # (e.g. the alpaca-paper shadow) cannot pool into or mask this account.
        if account is not None and _record_account(rec) != account:
            continue

        asset = rec.get("asset")
        target = rec.get("target_position_pct")
        ts = rec.get("asof_execution")
        if asset is None or target is None or ts is None:
            continue

        if asof is not None and ts > asof:
            continue

        prior = latest_per_symbol.get(asset)
        if prior is None or ts >= prior[0]:
            try:
                latest_per_symbol[asset] = (ts, float(target))
            except (TypeError, ValueError):
                continue

    positions: dict[str, float] = {}
    for asset, (_ts, t) in latest_per_symbol.items():
        if drop_zeros and t == 0.0:
            continue
        positions[asset] = t

    return PortfolioState(positions=positions)
