"""quantcore.settle — settlement: realized outcomes + analyst calibration.

Port of the ADR-0083 Phase-0b intent (lean): join entry fills to exit fills
(or to the latest mark when a horizon expires), compute realized returns,
append 'settle' events, and update per-analyst calibration tallies.

Settlement joins on BAR/FILL time, never decision time (asof-honesty rail #5).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any

from quantcore.ledger import Ledger, parse_iso

CALIBRATION_NAME = "calibration.json"

_HORIZON_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_horizon(horizon: str) -> timedelta:
    """'5d' -> 5 days; '1h' -> 1 hour. Raises ValueError on junk (caller abstains)."""
    h = horizon.strip().lower()
    if len(h) < 2 or h[-1] not in _HORIZON_UNITS:
        raise ValueError(f"unparseable horizon: {horizon!r}")
    qty = float(h[:-1])
    if qty <= 0:
        raise ValueError(f"non-positive horizon: {horizon!r}")
    return timedelta(**{_HORIZON_UNITS[h[-1]]: qty})


def settle(ledger: Ledger, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Settle every fill whose horizon has expired and that has a later
    opposite/flattening fill OR a usable mark price. Returns new settle events.

    Idempotent: a fill already referenced by a settle event is skipped.
    """
    now = now or datetime.now(UTC)
    events = ledger.events()
    settled_fill_seqs = {
        rec["entry_seq"] for rec in events if rec["event"] == "settle" and "entry_seq" in rec
    }
    props = {
        rec["proposal"]["proposal_id"]: rec["proposal"]
        for rec in events
        if rec["event"] == "proposal"
    }
    latest_mark: dict[str, float] = {}
    for rec in events:
        if rec["event"] == "mark":
            latest_mark[rec["asset"]] = float(rec["price"])

    out: list[dict[str, Any]] = []
    fills = [r for r in events if r["event"] == "fill"]
    for rec in fills:
        if rec["seq"] in settled_fill_seqs:
            continue
        f = rec["fill"]
        prop = props.get(f["proposal_id"])
        if prop is None:
            continue
        sig = prop["signal"]
        if abs(float(f["filled_position_pct"])) < 1e-9:
            continue  # a flattening fill is an exit, not an entry
        try:
            horizon_td = parse_horizon(sig["horizon"])
        except ValueError:
            continue  # abstain on junk horizons; never guess
        filled_at = parse_iso(f["filled_at"])
        expires = filled_at + horizon_td
        if now < expires:
            continue  # horizon still running

        entry_price = float(f["fill_price"])
        entry_pct = float(f["filled_position_pct"])
        # Exit price: the first later same-asset fill that REDUCES exposure
        # (smaller magnitude, sign flip, or flatten-to-zero). An INCREASING
        # fill is an add, not an exit — fall through to the horizon mark
        # (R1-03: never settle an entry at the price of a later add).
        exit_price: float | None = None
        exit_kind = "mark"
        for later in fills:
            if later["seq"] <= rec["seq"]:
                continue
            lf = later["fill"]
            if lf["asset"] != f["asset"]:
                continue
            new_pct = float(lf["filled_position_pct"])
            reduces = (
                abs(new_pct) < 1e-9
                or new_pct * entry_pct < 0
                or abs(new_pct) < abs(entry_pct) - 1e-9
            )
            if reduces:
                exit_price = float(lf["fill_price"])
                exit_kind = "fill"
                break
        if exit_price is None:
            exit_price = latest_mark.get(f["asset"])
        if exit_price is None:
            continue  # no honest exit price -> leave unsettled, never invent

        direction = 1 if entry_pct > 0 else -1
        # raw_move: the RAW price move, independent of fill direction.
        # realized: P&L of the actual position (direction-adjusted).
        raw_move = (exit_price - entry_price) / entry_price
        realized = direction * raw_move
        predicted_direction = int(sig["direction"])
        # direction_correct == "the committee signal's direction matched the
        # raw price move" (R1-01 re-derivation). A zero move is not correct.
        correct = (
            predicted_direction != 0
            and raw_move != 0
            and (raw_move > 0) == (predicted_direction > 0)
        )
        ev = ledger.append(
            "settle",
            {
                "entry_seq": rec["seq"],
                "proposal_id": f["proposal_id"],
                "asset": f["asset"],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "exit_kind": exit_kind,
                "horizon": sig["horizon"],
                "realized_return": realized,
                "direction_correct": bool(correct),
                "views": [
                    {"analyst": v["analyst"], "direction": v["direction"], "confidence": v["confidence"]}
                    for v in sig.get("views", [])
                ],
            },
        )
        out.append(ev)
        _update_calibration(ledger.state_dir, sig.get("views", []), raw_move)
    return out


def _update_calibration(state_dir: Path, views: list[dict], raw_move: float) -> None:
    """Per-analyst tallies in confidence buckets — feeds ECE reporting.

    A view's correctness is judged on the RAW price move (exit-entry)/entry,
    NEVER on the fill-direction-adjusted realized return — judging on
    `realized` inverted every short (R1-01). A zero raw move carries no
    directional information, so it is excluded entirely (no tally).
    """
    if raw_move == 0:
        return
    path = state_dir / CALIBRATION_NAME
    data = json.loads(path.read_text()) if path.exists() else {}
    for v in views:
        analyst = v["analyst"]
        bucket = f"{min(int(float(v['confidence']) * 10), 9) / 10:.1f}"
        rec = data.setdefault(analyst, {}).setdefault(bucket, {"n": 0, "n_correct": 0})
        rec["n"] += 1
        v_dir = int(v["direction"])
        was_correct = v_dir != 0 and (raw_move > 0) == (v_dir > 0)
        if was_correct:
            rec["n_correct"] += 1
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def calibration_report(state_dir: Path) -> dict[str, Any]:
    """Per-analyst expected-calibration-error summary for /status and /retro."""
    path = state_dir / CALIBRATION_NAME
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    report: dict[str, Any] = {}
    for analyst, buckets in data.items():
        rows = []
        total_n = 0
        weighted_err = 0.0
        for bucket, rec in sorted(buckets.items()):
            n, nc = rec["n"], rec["n_correct"]
            acc = nc / n if n else 0.0
            rows.append({"confidence": float(bucket), "n": n, "accuracy": round(acc, 3)})
            total_n += n
            weighted_err += n * abs(acc - float(bucket))
        report[analyst] = {
            "buckets": rows,
            "n": total_n,
            "ece": round(weighted_err / total_n, 4) if total_n else None,
        }
    return report
