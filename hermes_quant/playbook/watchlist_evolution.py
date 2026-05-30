"""hermes_quant.playbook.watchlist_evolution — Per-play evolving watchlists.

State lives at ``~/.hermes/quant/watchlist/play-fit.json`` (current ranked
state) and ``~/.hermes/quant/watchlist/journal.jsonl`` (append-only audit
log). The scorer is a dependency injected by the caller; this module
performs no data acquisition.

Discipline (per AGENTS.md, money-software):

* **Silence by default.** Missing universe → log warning, return empty
  summary, do not raise. Missing watchlist file → bootstrap empty.
* **Atomic writes.** ``play-fit.json`` is written via ``tempfile`` +
  ``os.replace``; the journal is line-buffered append-only.
* **Append-only journal.** Past entries are never rewritten.
* **No data acquisition here.** The scorer (``Callable[[str, str], float]``)
  is injected; the parallel scorers module owns Alpaca/yfinance calls.

Onboard rule
------------
A symbol is onboarded to a play's active list when:

  * Its score has been ``>= onboard_floor`` for ``sticky_onboard_days``
    consecutive scoring runs, AND
  * The play's active list has fewer than ``max_per_play`` entries.

Evict rule (in order of precedence)
-----------------------------------
  * Score below ``evict_floor`` on this run → evict immediately.
  * Score below ``onboard_floor`` for 7 consecutive runs → evict.
  * Explicit ``eviction_rules`` from a PlayProfile fire → evict.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------


PLAY_NAMES: tuple[str, ...] = (
    "covered_call",
    "csp",
    "wheel",
    "leaps",
    "swing",
)

DEFAULT_WATCHLIST_PATH = Path.home() / ".hermes" / "quant" / "watchlist" / "play-fit.json"
DEFAULT_JOURNAL_PATH = Path.home() / ".hermes" / "quant" / "watchlist" / "journal.jsonl"
DEFAULT_UNIVERSE_PATH = Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily.json"

# How many consecutive runs below the onboard floor (but above the evict
# floor) before slow-eviction fires.
SLOW_EVICT_RUNS = 7

# State labels for the schema.
STATE_CANDIDATE = "candidate"
STATE_ACTIVE = "active"
STATE_EVICTED = "evicted"

# Journal action verbs.
ACTION_ONBOARD = "onboard"
ACTION_EVICT = "evict"
ACTION_SCORE_UPDATE = "score_update"


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchlistEntry:
    """A single (symbol, play) row in the watchlist.

    The dataclass is frozen so each evolution step produces a fresh
    immutable record; in-place mutation would defeat the journal-as-truth
    invariant.
    """

    symbol: str
    play: str
    # Timestamps may be NaT — typing as ``Any`` is the least-bad escape from
    # pandas' poor typing of NaT. Runtime behavior: ``pd.Timestamp | NaT``.
    onboarded_at: Any
    last_seen_at: Any
    last_score: float
    consecutive_days_above_floor: int
    state: str
    eviction_reason: str | None = None
    # Number of consecutive runs the score has been below the onboard floor
    # (used for slow-eviction). Reset to 0 on any run >= onboard_floor.
    consecutive_days_below_onboard: int = 0
    # Optional per-play extras a scorer may want to surface (kept opaque so
    # we can extend without schema migrations). Stored on disk verbatim.
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["onboarded_at"] = self.onboarded_at.isoformat() if self.onboarded_at is not None else None
        d["last_seen_at"] = self.last_seen_at.isoformat() if self.last_seen_at is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WatchlistEntry:
        return cls(
            symbol=d["symbol"],
            play=d["play"],
            onboarded_at=pd.Timestamp(d["onboarded_at"]) if d.get("onboarded_at") else pd.NaT,
            last_seen_at=pd.Timestamp(d["last_seen_at"]) if d.get("last_seen_at") else pd.NaT,
            last_score=float(d["last_score"]),
            consecutive_days_above_floor=int(d.get("consecutive_days_above_floor", 0)),
            state=d.get("state", STATE_CANDIDATE),
            eviction_reason=d.get("eviction_reason"),
            consecutive_days_below_onboard=int(d.get("consecutive_days_below_onboard", 0)),
            extras=dict(d.get("extras") or {}),
        )


# -----------------------------------------------------------------------------
# Scorer protocol
# -----------------------------------------------------------------------------


# A scorer returns a per-play fitness in [0, 1] for one symbol. Real scorers
# fetch bars / chains / IV — but they live in a separate module so this one
# remains data-pure and trivially unit-testable.
PlayScorer = Callable[[str, str], float]


def stub_scorer(score: float = 0.5) -> PlayScorer:
    """Return a scorer that emits a constant score for any (symbol, play).

    Used by the smoke run and unit tests; production callers inject a real
    scorer that owns its own data acquisition.
    """

    def _scorer(symbol: str, play: str) -> float:  # noqa: ARG001 — protocol shape
        return score

    return _scorer


# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------


def _read_universe(universe_path: Path) -> list[str]:
    """Return the list of tradable symbols from the universe artifact.

    Silence-by-default: missing or malformed file → empty list (caller
    short-circuits and exits 0).
    """
    if not universe_path.exists():
        logger.warning("watchlist_evolution: universe file missing: %s", universe_path)
        return []
    try:
        payload = json.loads(universe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("watchlist_evolution: failed to parse universe: %s", exc)
        return []
    raw_symbols = payload.get("symbols") or []
    out: list[str] = []
    for entry in raw_symbols:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict) and "symbol" in entry:
            out.append(str(entry["symbol"]))
    return out


def _read_watchlist(
    watchlist_path: Path,
) -> tuple[Any, dict[str, list[WatchlistEntry]]]:
    """Return ``(as_of, plays)``. If the file is missing, return empty state."""
    if not watchlist_path.exists():
        return None, {p: [] for p in PLAY_NAMES}
    try:
        payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("watchlist_evolution: corrupt state file, bootstrapping empty: %s", exc)
        return None, {p: [] for p in PLAY_NAMES}
    as_of = pd.Timestamp(payload["as_of"]) if payload.get("as_of") else None
    plays: dict[str, list[WatchlistEntry]] = {p: [] for p in PLAY_NAMES}
    for play, entries in (payload.get("plays") or {}).items():
        if play not in plays:
            # Forward-compatible: ignore unknown plays rather than crashing.
            continue
        plays[play] = [WatchlistEntry.from_dict(e) for e in entries]
    return as_of, plays


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic write: tempfile + fsync + os.replace. POSIX-safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_journal(journal_path: Path, events: list[dict[str, Any]]) -> None:
    """Append journal events as JSONL. Append-only — never rewrite past lines.

    Each line is independently flushed + fsynced so a crash mid-batch leaves
    a valid prefix on disk.
    """
    if not events:
        return
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "a", encoding="utf-8", buffering=1) as f:
        for ev in events:
            f.write(json.dumps(ev, sort_keys=True) + "\n")
            f.flush()
        os.fsync(f.fileno())


# -----------------------------------------------------------------------------
# Evolution rules
# -----------------------------------------------------------------------------


def _evolve_one_play(
    *,
    play: str,
    universe: list[str],
    current: list[WatchlistEntry],
    scorer: PlayScorer,
    asof: pd.Timestamp,
    onboard_floor: float,
    evict_floor: float,
    sticky_onboard_days: int,
    max_per_play: int,
    eviction_rules: Callable[[WatchlistEntry, float], str | None] | None,
    fast_track_symbols: set[str] | None = None,
    admission_extras: dict[str, dict] | None = None,
    position_lookup: Callable[[str], bool] | None = None,
) -> tuple[list[WatchlistEntry], list[dict[str, Any]]]:
    """Run one evolution step for one play.

    Returns the new ranked entry list and the journal events emitted by
    this step.
    """

    # ADR-0075 catalyst onboarding: fast-track symbols onboard same-day
    # (sticky_onboard_days=0) and carry admission_extras (admitted_via=catalyst,
    # horizon, asof) to disk. Both default to empty -> all existing callers are
    # bit-for-bit identical.
    fast_track = fast_track_symbols or set()
    admission_extras = admission_extras or {}

    # Index existing rows by symbol so we can update in place (logically; the
    # dataclass is frozen, so each "update" is a `replace`).
    by_symbol: dict[str, WatchlistEntry] = {e.symbol: e for e in current}
    events: list[dict[str, Any]] = []
    new_rows: list[WatchlistEntry] = []

    # Active count is the gate for the max_per_play cap. We re-tally as we
    # go so onboards within this run respect the cap.
    active_count = sum(1 for e in current if e.state == STATE_ACTIVE)

    seen_symbols: set[str] = set()

    for symbol in universe:
        if symbol in seen_symbols:
            # Universe deduplicates upstream, but be defensive.
            continue
        seen_symbols.add(symbol)

        score = float(scorer(symbol, play))
        prev = by_symbol.get(symbol)
        prev_score = prev.last_score if prev is not None else None
        prev_state = prev.state if prev is not None else None

        if prev is None:
            # First time we've ever seen this symbol for this play.
            row = WatchlistEntry(
                symbol=symbol,
                play=play,
                onboarded_at=pd.NaT,
                last_seen_at=asof if score >= onboard_floor else pd.NaT,
                last_score=score,
                consecutive_days_above_floor=1 if score >= onboard_floor else 0,
                state=STATE_CANDIDATE,
                eviction_reason=None,
                consecutive_days_below_onboard=0 if score >= onboard_floor else 1,
            )
        else:
            # Existing row — update streak counters.
            if score >= onboard_floor:
                streak_above = (prev.consecutive_days_above_floor or 0) + 1
                streak_below = 0
                last_seen = asof
            else:
                streak_above = 0
                streak_below = (prev.consecutive_days_below_onboard or 0) + 1
                last_seen = prev.last_seen_at
            row = replace(
                prev,
                last_score=score,
                last_seen_at=last_seen,
                consecutive_days_above_floor=streak_above,
                consecutive_days_below_onboard=streak_below,
            )

        # ---- Evict rules (precedence: fast → slow → explicit) -----------
        evict_reason: str | None = None
        if score < evict_floor:
            evict_reason = f"score<{evict_floor:.2f} (fast)"
        elif (
            row.state == STATE_ACTIVE
            and row.consecutive_days_below_onboard >= SLOW_EVICT_RUNS
            and not _catalyst_eviction_protected(row, asof, position_lookup)
        ):
            # Sticky-removal protection (ADR-0075 §1.4; Nautilus #3359 /
            # LEAN CanRemoveMember): a catalyst-admitted row with an open position
            # whose horizon hasn't closed is NOT slow-evicted — let the position
            # close out over the catalyst's horizon first. _catalyst_eviction_protected
            # returns True (protect) only for admitted_via=catalyst rows within
            # horizon; non-catalyst rows fall through to the normal slow-evict.
            evict_reason = f"score<{onboard_floor:.2f} for {SLOW_EVICT_RUNS} runs (slow)"
        elif eviction_rules is not None:
            try:
                rule_reason = eviction_rules(row, score)
            except Exception as exc:  # noqa: BLE001 — defensive, never crash the loop
                logger.warning(
                    "watchlist_evolution: eviction_rules raised for %s/%s: %s",
                    symbol,
                    play,
                    exc,
                )
                rule_reason = None
            if rule_reason:
                evict_reason = rule_reason

        if evict_reason and row.state != STATE_EVICTED:
            # Only emit an evict event if this was meaningful (had been
            # active or candidate); transitioning candidate→evicted is
            # journalled so we have an audit trail.
            new_state = STATE_EVICTED
            row = replace(
                row,
                state=new_state,
                eviction_reason=evict_reason,
                consecutive_days_above_floor=0,
            )
            if prev_state in (STATE_ACTIVE, STATE_CANDIDATE):
                events.append(
                    _event(
                        asof=asof,
                        play=play,
                        symbol=symbol,
                        action=ACTION_EVICT,
                        reason=evict_reason,
                        score_before=prev_score,
                        score_after=score,
                    )
                )

        # ---- Onboard rule (only if not evicted this run) ----------------
        # ADR-0075: a catalyst-fast-tracked symbol needs 0 consecutive runs (a
        # 1-day catalyst must be actionable that day); universe names keep the
        # configured sticky window.
        effective_sticky = 0 if symbol in fast_track else sticky_onboard_days
        if (
            row.state != STATE_EVICTED
            and row.state != STATE_ACTIVE
            and row.consecutive_days_above_floor >= effective_sticky
            and active_count < max_per_play
        ):
            onboard_extras = admission_extras.get(symbol)
            row = replace(
                row,
                state=STATE_ACTIVE,
                onboarded_at=asof,
                eviction_reason=None,
                extras=dict(onboard_extras) if onboard_extras else row.extras,
            )
            active_count += 1
            reason = (
                f"catalyst fast-track >= {onboard_floor:.2f}"
                if symbol in fast_track
                else f"sticky({sticky_onboard_days}) >= {onboard_floor:.2f}"
            )
            events.append(
                _event(
                    asof=asof,
                    play=play,
                    symbol=symbol,
                    action=ACTION_ONBOARD,
                    reason=reason,
                    score_before=prev_score,
                    score_after=score,
                )
            )

        # ---- Score-update event (only on meaningful score deltas) -------
        if (
            prev is not None
            and prev_score is not None
            and abs(score - prev_score) >= 0.05
            and row.state != STATE_EVICTED
            # don't emit a score_update on the same run we onboarded —
            # the onboard event already records it.
            and not (prev_state != STATE_ACTIVE and row.state == STATE_ACTIVE)
        ):
            events.append(
                _event(
                    asof=asof,
                    play=play,
                    symbol=symbol,
                    action=ACTION_SCORE_UPDATE,
                    reason="score delta >= 0.05",
                    score_before=prev_score,
                    score_after=score,
                )
            )

        new_rows.append(row)

    # Carry over any historical rows for symbols that fell out of the
    # universe (operator may want the audit trail). They are not re-scored
    # this run; their state stays put.
    for existing_symbol, existing_row in by_symbol.items():
        if existing_symbol not in seen_symbols:
            new_rows.append(existing_row)

    # Rank: active first (by score desc), then candidates (by score desc),
    # then evicted (by score desc — debug aid, the rank doesn't matter
    # operationally for evicted rows).
    state_rank = {STATE_ACTIVE: 0, STATE_CANDIDATE: 1, STATE_EVICTED: 2}
    new_rows.sort(key=lambda r: (state_rank.get(r.state, 9), -r.last_score, r.symbol))

    return new_rows, events


def _horizon_to_days(horizon: str | None) -> int:
    """Coarse horizon -> trading-day window for sticky-removal protection.

    Catalyst horizons are short (days-to-weeks). Conservative mapping; an
    unknown/unparseable horizon defaults to 1 day (the catalyst's minimum).
    """
    if not horizon:
        return 1
    h = str(horizon).strip().lower()
    table = {"1d": 1, "1w": 7, "2w": 14, "1m": 30, "1q": 90}
    return table.get(h, 1)


def _catalyst_eviction_protected(
    row: WatchlistEntry,
    asof: pd.Timestamp,
    position_lookup: Callable[[str], bool] | None,
) -> bool:
    """True iff `row` is a catalyst-admitted name that must NOT be slow-evicted yet.

    Protection holds for an ``admitted_via=catalyst`` row whose catalyst horizon
    window has NOT elapsed AND that has a live position. Position lookup is
    best-effort: if it is unavailable or raises, we fail SAFE toward holding (treat
    as having a position) so a catalyst's horizon isn't cut short by a missing
    position feed. A non-catalyst row is never protected (returns False -> normal
    slow-evict). Once the horizon elapses, protection lifts and the row evicts
    normally on the next qualifying run.
    """
    extras = row.extras or {}
    if extras.get("admitted_via") != "catalyst":
        return False
    # Horizon window check: protect only while inside the catalyst horizon.
    onboarded = row.onboarded_at
    if onboarded is not None and onboarded is not pd.NaT:
        try:
            elapsed_days = (asof - onboarded).days
            if elapsed_days >= _horizon_to_days(extras.get("catalyst_horizon")):
                return False  # horizon elapsed -> protection lifts
        except (TypeError, ValueError):
            pass  # unparseable timestamps -> fall through to fail-safe hold
    # Live-position check: fail-safe toward holding when unknown.
    if position_lookup is None:
        return True
    try:
        return bool(position_lookup(row.symbol))
    except Exception:  # noqa: BLE001 — missing position feed -> fail-safe hold
        return True


def _event(
    *,
    asof: pd.Timestamp,
    play: str,
    symbol: str,
    action: str,
    reason: str,
    score_before: float | None,
    score_after: float,
) -> dict[str, Any]:
    return {
        "event_id": uuid.uuid4().hex,
        "asof": asof.isoformat(),
        "play": play,
        "symbol": symbol,
        "action": action,
        "reason": reason,
        "score_before": score_before,
        "score_after": score_after,
    }


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------


def evolve_watchlist(
    universe_path: Path = DEFAULT_UNIVERSE_PATH,
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
    journal_path: Path = DEFAULT_JOURNAL_PATH,
    *,
    max_per_play: int = 50,
    onboard_floor: float = 0.65,
    evict_floor: float = 0.45,
    sticky_onboard_days: int = 3,
    scorer: PlayScorer | None = None,
    eviction_rules: Callable[[WatchlistEntry, float], str | None] | None = None,
    asof: pd.Timestamp | None = None,
    plays: tuple[str, ...] = PLAY_NAMES,
    fast_track_symbols: set[str] | None = None,
    admission_extras: dict[str, dict] | None = None,
    position_lookup: Callable[[str], bool] | None = None,
    extra_universe_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Run one evolution step over the universe + current watchlist state.

    Reads the universe and the current ``play-fit.json``, scores every
    universe symbol against every play (via the injected ``scorer``),
    applies onboard/evict rules, atomic-writes the new state, and appends
    journal events.

    On missing universe (silence-by-default), returns an empty summary.

    ADR-0075 catalyst onboarding (all default ``None`` -> existing callers
    bit-for-bit identical):
      * ``fast_track_symbols`` — symbols that onboard same-day
        (``sticky_onboard_days=0``) rather than after the usual sticky window.
      * ``admission_extras`` — ``{symbol: extras_dict}`` stamped onto the
        ``WatchlistEntry.extras`` of an onboarded admitted name (carries
        ``admitted_via=catalyst`` / horizon / asof to disk).
      * ``position_lookup`` — best-effort ``symbol -> has_open_position`` used by
        the sticky-removal protection (a catalyst-admitted name with an open
        position is not slow-evicted before its horizon closes; fail-safe to hold
        when unknown).

    Returns
    -------
    dict
        ``{
            'as_of': ISO8601,
            'per_play': {
                play: {
                    'n_active', 'n_onboarded_today', 'n_evicted_today',
                    'top5': [(symbol, score), ...],
                }
            },
            'events_written': int,
        }``
    """

    universe_path = Path(universe_path)
    watchlist_path = Path(watchlist_path)
    journal_path = Path(journal_path)

    if asof is None:
        # pandas >= 2.0 returns a tz-aware UTC timestamp from utcnow().
        # Older versions returned naive; normalize to tz-aware UTC either way.
        ts = pd.Timestamp.utcnow()
        asof = ts if ts.tzinfo is not None else ts.tz_localize("UTC")

    universe = _read_universe(universe_path)
    # ADR-0075: union catalyst-admitted out-of-universe names into the scored
    # universe (append, dedup, preserve order). Empty/None -> bit-identical.
    if extra_universe_symbols:
        _seen = set(universe)
        for s in extra_universe_symbols:
            if s and s not in _seen:
                universe.append(s)
                _seen.add(s)
    if not universe:
        # Silence-by-default — return an empty-but-valid summary.
        return {
            "as_of": asof.isoformat(),
            "per_play": {
                p: {"n_active": 0, "n_onboarded_today": 0, "n_evicted_today": 0, "top5": []}
                for p in plays
            },
            "events_written": 0,
        }

    if scorer is None:
        # No scorer injected — silent stub so the daemon can run before
        # the scorers module lands. This is a deliberate degradation:
        # everything stays in candidate state, nothing is evicted, no
        # onboards fire (0.5 < 0.65 default floor).
        scorer = stub_scorer(0.5)

    _, current_state = _read_watchlist(watchlist_path)

    new_state: dict[str, list[WatchlistEntry]] = {}
    all_events: list[dict[str, Any]] = []
    summary: dict[str, dict[str, Any]] = {}

    for play in plays:
        prior = current_state.get(play, [])
        new_rows, events = _evolve_one_play(
            play=play,
            universe=universe,
            current=prior,
            scorer=scorer,
            asof=asof,
            onboard_floor=onboard_floor,
            evict_floor=evict_floor,
            sticky_onboard_days=sticky_onboard_days,
            max_per_play=max_per_play,
            eviction_rules=eviction_rules,
            fast_track_symbols=fast_track_symbols,
            admission_extras=admission_extras,
            position_lookup=position_lookup,
        )
        new_state[play] = new_rows
        all_events.extend(events)

        active = [r for r in new_rows if r.state == STATE_ACTIVE]
        n_onboarded = sum(1 for ev in events if ev["action"] == ACTION_ONBOARD)
        n_evicted = sum(1 for ev in events if ev["action"] == ACTION_EVICT)
        # Top 5 active rows (already sorted by score desc within active).
        top5 = [(r.symbol, r.last_score) for r in active[:5]]
        summary[play] = {
            "n_active": len(active),
            "n_onboarded_today": n_onboarded,
            "n_evicted_today": n_evicted,
            "top5": top5,
        }

    # Persist new state + journal. Journal first so a crash between the
    # two leaves an audit trail without a stale state file (worst case:
    # journal entries that don't match an updated state, which we'd
    # detect on next run anyway because state is recomputed every tick).
    _append_journal(journal_path, all_events)

    payload = {
        "as_of": asof.isoformat(),
        "plays": {p: [r.to_dict() for r in rows] for p, rows in new_state.items()},
    }
    _atomic_write_json(watchlist_path, payload)

    return {
        "as_of": asof.isoformat(),
        "per_play": summary,
        "events_written": len(all_events),
    }


def get_active_watchlist(
    play: str | None = None,
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
) -> list[str]:
    """Return active symbols. If ``play`` is None, union across all plays.

    Order: stable across calls — for a single play, ranked by descending
    score; for the union, deduplicated in encounter order across the play
    list (covered_call → csp → wheel → leaps → swing).
    """
    _, state = _read_watchlist(Path(watchlist_path))

    if play is not None:
        rows = state.get(play, [])
        return [r.symbol for r in rows if r.state == STATE_ACTIVE]

    seen: set[str] = set()
    union: list[str] = []
    for p in PLAY_NAMES:
        for r in state.get(p, []):
            if r.state == STATE_ACTIVE and r.symbol not in seen:
                union.append(r.symbol)
                seen.add(r.symbol)
    return union
