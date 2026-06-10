#!/usr/bin/env python3
"""Operator flatten of the paper-default advisor/autonomous book.

Closes every OPEN position in the `reactor_filter='paper'` view (the view the
ADR-0087 portfolio cap and the autonomous tick actually read) by emitting a
proper CLOSE fill per symbol:

  * target_position_pct = 0.0   -> reconstruct_portfolio_state keys on this;
                                   a close MUST set it to 0 or the position
                                   stays "open" in the cap's view (the prior
                                   2026-06-07 ASTS flatten missed this and
                                   ASTS is still showing -0.20).
  * fill_size_pct        = -held -> offsetting signed size realizes the P&L.
  * play_tag             = matches the position's originating tag so the
                                   settlement loop attributes realized P&L to
                                   the right source (advisor vs autonomous).

After emitting closes it rebuilds state.db from the bus so the stale derived
cache (which had AAPL +1.0 drift) is re-projected from the source of truth.

Run with the repo venv from the repo dir. DRY-RUN by default; pass --fire to
actually emit.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

REPO = "/mnt/e/CS/github/hermes-quant"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hermes_quant.portfolio.state import reconstruct_portfolio_state  # noqa: E402
from hermes_quant.react.paper import (  # noqa: E402
    ExecutionRecord,
    _record_to_dict,
)
from hermes_quant.daemon.signal_bus import append_locked  # noqa: E402
import json  # noqa: E402

BUS = os.path.expanduser("~/.hermes/quant/executions.jsonl")
FIRE = "--fire" in sys.argv

# Per-symbol originating play_tag, read from the bus earlier. Anything not
# listed defaults to "advisor". (AAL/BA/CBOE/CDNS were autonomous; the shorts
# AVGO/CRM/META/ORCL and ASTS were advisor.)
PLAY_TAG = {
    "AAL": "autonomous", "BA": "autonomous", "CBOE": "autonomous",
    "CDNS": "autonomous",
    "ASTS": "advisor", "AVGO": "advisor", "CRM": "advisor",
    "META": "advisor", "ORCL": "advisor",
}

# Current marks (decision_price) — pulled from a fresh snapshot below if avail,
# else fall back to the last bus decision_price per symbol.


def latest_bus_prices() -> dict[str, float]:
    import json
    px: dict[str, float] = {}
    with open(BUS) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("decision_price") is not None and e.get("asset"):
                px[e["asset"]] = float(e["decision_price"])
    return px


def main() -> int:
    ps = reconstruct_portfolio_state(BUS)  # reactor_filter='paper' (cap view)
    open_pos = dict(ps.positions)
    if not open_pos:
        print("Book already flat (paper view). Nothing to do.")
        return 0

    marks = latest_bus_prices()
    gross_before = sum(abs(v) for v in open_pos.values())
    print(f"OPEN paper positions: {len(open_pos)} | gross before: {gross_before:.4f}")
    print(f"Mode: {'FIRE' if FIRE else 'DRY-RUN'}\n")

    now = datetime.now(tz=timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    closed = 0

    for sym, held in sorted(open_pos.items()):
        offset = -float(held)  # offsetting signed size to realize P&L
        tag = PLAY_TAG.get(sym, "advisor")
        mark = marks.get(sym)
        print(f"  CLOSE {sym:<6} held={held:+.4f} -> fill_size_pct={offset:+.4f} "
              f"target_position_pct=0.0 (EXPLICIT) play_tag={tag} mark={mark}")
        if not FIRE:
            continue
        # Build a proper CLOSE record DIRECTLY. PaperReactor.execute() hardcodes
        # record.target_position_pct = fill_size_pct (paper.py:266), so it can
        # NEVER emit a flat (target=0) record — driving it with offsetting fills
        # just flips the position. reconstruct_portfolio_state keys on
        # target_position_pct, so a close MUST carry target_position_pct=0.0.
        # We therefore append the close record directly via the same
        # _record_to_dict + append_locked path the reactor uses.
        rec = ExecutionRecord(
            proposal_id=f"prop_{now:%Y%m%dT%H%M%S}_{sym}_FLATCLOSE",
            signal_id=None,
            asset=sym,
            asset_class="equity",
            timeframe="1d",
            asof_decision=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            asof_execution=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            target_position_pct=0.0,          # <-- closes it in the cap/reader view
            decision_price=mark,
            fill_price=mark,                  # paper: fill_price = decision_price
            fill_size_pct=offset,             # <-- realizes P&L (offsetting leg)
            reactor_name="paper",
            human_in_the_loop=True,
            approver_user_id="operator-flatten",
            reactor_metadata={
                "paper": True,
                "advisor_caveats": [
                    "operator flatten 2026-06-08: reset paper-default book to "
                    "free ADR-0087 cap headroom; direct zero-target close "
                    "(reactor.execute cannot emit target=0)"
                ],
                "operator_flatten": True,
            },
            bar_ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            play_tag=tag,
        )
        line = json.dumps(_record_to_dict(rec), separators=(",", ":"),
                          sort_keys=True) + "\n"
        with append_locked(BUS) as fd:
            os.write(fd, line.encode("utf-8"))
        print(f"        -> appended close; target_position_pct=0.0 "
              f"fill_size_pct={offset:+.4f}")
        closed += 1

    if not FIRE:
        print("\n(dry-run — re-run with --fire to emit closes)")
        return 0

    # Rebuild state.db from the bus so the stale cache (AAPL +1.0 drift) is
    # re-projected from source of truth.
    print(f"\nEmitted {closed} closes. Rebuilding state.db from bus...")
    from hermes_quant.state.portfolio_state import PortfolioState as _PS
    from pathlib import Path
    db = os.path.expanduser("~/.hermes/quant/state.db")
    res = _PS(state_db_path=Path(db)).reconstruct_from(Path(BUS))
    print(f"reconstruct_from: processed={res.executions_processed} "
          f"accounts={sorted(res.accounts_seen)} errors={len(res.errors)}")

    # Verify flat in the cap view
    ps2 = reconstruct_portfolio_state(BUS)
    g2 = sum(abs(v) for v in ps2.positions.values())
    print(f"\nPOST-FLATTEN cap view: {len(ps2.positions)} open | gross: {g2:.4f}")
    for s, v in sorted(ps2.positions.items()):
        print(f"  {s:<6} {v:+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
