"""quant_consumer_strategy.py — freqtrade IStrategy that reads hermes-quant signals.

Per ADR-0008. Drop into freqtrade's user_data/strategies/ directory.

Reads ~/.hermes/quant/signals.jsonl, marks entries based on the daemon's
emitted signals, and uses target_position_pct for sizing.

Heartbeat dead-man-switch (per synthesis-v2 §P0-C): if the daemon hasn't
emitted a heartbeat in `dead_man_switch_seconds`, OR if no heartbeat has
been observed within `bootstrap_grace_seconds` of strategy start, enter
safe-stop (force-exit all positions, refuse new entries).

Halt handling: signal records with type='halt' or halt=True force-exit
positions for the affected scope and refuse new entries until a non-halt
signal arrives OR the operator clears the halt manually.

Stale signal guard: signals older than `max_signal_age_minutes` (default
30 for 1h timeframe) are ignored.

executions.jsonl back-channel: on every fill (entry or exit), the strategy
appends an execution record so the daemon's settlement loop can compute
RealizedOutcome / EpisodeOutcome for analyst calibration. Uses the same
flock-protected append helper as the daemon (synthesis-v2 §P0-B).

This is intentionally TINY (~250 LOC). The intelligence is upstream in
hermes-quant; freqtrade owns order management.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    from freqtrade.strategy import IStrategy
except ImportError:
    # Allow import for test/inspection without freqtrade installed
    IStrategy = object  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local copy of append_locked + path defaults so the strategy works even
# without hermes-quant installed in the freqtrade environment.
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
SIGNAL_BUS_PATH = QUANT_HOME / "signals.jsonl"
EXECUTION_BUS_PATH = QUANT_HOME / "executions.jsonl"
HALT_STATE_MIRROR = QUANT_HOME / "halt_state.json"
RECORD_BYTE_CAP = 16384


@contextmanager
def append_locked(path: Path):
    """flock-protected append context (mirror of hermes_quant.daemon.signal_bus)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            os.fsync(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _parse_asof_utc(value: object) -> pd.Timestamp | None:
    """Parse an ISO asof string to a UTC pd.Timestamp, or None if absent/unparseable/NaT.

    ar35: used to order signals by CHRONOLOGY, not by lexical string compare (which only
    sorts correctly when every asof shares one exact format). Naive timestamps are assumed
    UTC (matching the heartbeat parse above). NaT / errors -> None so the caller can refuse
    to act on an unorderable timestamp rather than mis-rank it.
    """
    if value is None or value == "":
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _emit_execution(record: dict) -> None:
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > RECORD_BYTE_CAP:
        # Phase-8 P1-γ (synthesis 2026-05-13): elevate to ERROR and surface
        # the signal_id so an operator can trace which fill was dropped.
        # The strategy can't easily raise into freqtrade without crashing
        # the strategy thread, so we log loudly and drop. The dropped
        # record is a money-software defect (settlement loop won't see
        # this fill) but is still less catastrophic than crashing the
        # strategy.
        logger.error(
            "execution record too large (%d bytes > %d cap); DROPPED — "
            "signal_id=%s exec_id=%s asset=%s side=%s qty=%s",
            len(encoded),
            RECORD_BYTE_CAP,
            record.get("signal_id"),
            record.get("exec_id"),
            record.get("asset"),
            record.get("side"),
            record.get("qty"),
        )
        return
    try:
        with append_locked(EXECUTION_BUS_PATH) as fd:
            os.write(fd, encoded)
    except OSError as e:
        logger.warning("execution emission failed: %s", e)


def _read_jsonl_tail(path: Path, n: int = 1000, max_chunk: int = 4_194_304) -> list[dict]:
    if not path.exists():
        return []
    size = path.stat().st_size
    if size == 0:
        return []
    chunk_size = min(size, max_chunk)
    with open(path, "rb") as f:
        f.seek(max(0, size - chunk_size))
        chunk = f.read()
    if size > chunk_size:
        first_nl = chunk.find(b"\n")
        if first_nl < 0:
            return []
        chunk = chunk[first_nl + 1 :]
    out = []
    for line in chunk.split(b"\n"):
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue  # valid JSON but not an object (corrupt/partial append) — skip
        out.append(rec)
    return out[-n:]


# ---------------------------------------------------------------------------
# HermesQuantConsumer strategy
# ---------------------------------------------------------------------------


class HermesQuantConsumer(IStrategy):
    """Freqtrade strategy that consumes signals from hermes-quant daemon."""

    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False  # spot-only; flip for margin/futures pairs
    process_only_new_candles = True
    startup_candle_count = 0

    # Per ADR-0008
    SIGNAL_BUS_PATH = SIGNAL_BUS_PATH
    EXECUTION_BUS_PATH = EXECUTION_BUS_PATH
    HALT_STATE_MIRROR = HALT_STATE_MIRROR

    # Per synthesis-v2 §P0-C
    bootstrap_grace_seconds = 120.0
    dead_man_switch_seconds = 60.0

    # Stale signal threshold = max(timeframe_seconds * 2, 600)
    max_signal_age_minutes = 30.0

    minimal_roi = {"0": 100.0}  # disable freqtrade's TP/SL — daemon controls
    stoploss = -0.99  # effectively disabled — daemon controls

    def __init__(self, config: dict | None = None, **kwargs):
        if IStrategy is not object:
            super().__init__(config or {}, **kwargs)
        self._strategy_start_time = pd.Timestamp.utcnow()
        self._last_heartbeat: pd.Timestamp | None = None
        self._safe_stop_active = False
        self._safe_stop_reason = ""
        self._signal_cache: dict[str, dict] = {}  # latest signal per pair
        # Edge-triggered drop-reason latch per pair so a degraded upstream is
        # logged ONCE on the had-signal -> dropping transition (not every candle).
        # value is the reason currently latched for the pair, or None when the
        # pair last produced a usable signal (recovery clears it).
        self._signal_drop_reason: dict[str, str | None] = {}

    # -------------------------------------------------------------------------
    # IStrategy hooks
    # -------------------------------------------------------------------------

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return df

    def populate_entry_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["enter_long"] = 0
        if self._safe_stop_active:
            return df

        signal = self._latest_signal_for(
            metadata["pair"], df["date"].iloc[-1] if "date" in df.columns else None
        )
        if signal is None:
            return df
        if signal.get("halt", False):
            return df  # halts blocked at refresh time
        if signal.get("direction") == 1:
            df.iloc[-1, df.columns.get_loc("enter_long")] = 1
        return df

    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["exit_long"] = 0
        if self._safe_stop_active:
            df["exit_long"] = 1  # exit everything on safe-stop
            return df
        signal = self._latest_signal_for(
            metadata["pair"], df["date"].iloc[-1] if "date" in df.columns else None
        )
        if signal is None:
            return df
        if signal.get("halt", False):
            df["exit_long"] = 1  # halt → exit
            return df
        if signal.get("direction") in (-1, 0):
            df.iloc[-1, df.columns.get_loc("exit_long")] = 1
        return df

    def custom_stake_amount(
        self,
        pair: str,
        current_time,
        current_rate,
        proposed_stake,
        min_stake,
        max_stake,
        leverage,
        entry_tag,
        side,
        **kwargs,
    ):
        signal = self._latest_signal_for(pair, current_time)
        if signal is None:
            return 0
        target = abs(float(signal.get("target_position_pct", 0.0)))
        # ar31: a NON-FINITE target sizes the FULL allowed stake instead of silencing.
        # float() does not catch nan/inf, and `target <= 0` is False for nan, so the
        # guard below is bypassed and `min(max_stake, wallet * nan)` returns max_stake
        # (and `min(max_stake, inf)` likewise) — a sizing fail-OPEN off an externally-
        # writable signals.jsonl bus. Treat non-finite as a zero-size silence.
        if not math.isfinite(target) or target <= 0:
            return 0
        try:
            wallet_balance = self.wallets.get_total_stake_amount()
        except Exception:
            wallet_balance = max_stake
        intended = wallet_balance * target
        # Silence-by-default (ADR-0004): if the quant's intended notional is a positive
        # value below the exchange minimum, it CANNOT be honored without breaching the
        # deterministic quarter-Kelly sizing (kelly.py action_step/max_position). Per the
        # freqtrade contract a returned positive stake < min_stake is silently clamped UP
        # to min_stake — a dishonest OVER-SIZE. Return 0 (honest no-trade) instead. None
        # min_stake means the exchange reports no minimum, so no clamp applies.
        if min_stake is not None and 0 < intended < min_stake:
            return 0
        return min(max_stake, intended)

    def confirm_trade_entry(
        self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side, **kwargs
    ) -> bool:
        # Refresh dead-man-switch + halt state right before entry
        self._refresh_state(current_time)
        if self._safe_stop_active:
            logger.warning("entry blocked — safe-stop active: %s", self._safe_stop_reason)
            return False
        return True

    def confirm_trade_exit(
        self,
        pair,
        trade,
        order_type,
        amount,
        rate,
        time_in_force,
        sell_reason,
        current_time,
        **kwargs,
    ) -> bool:
        return True

    def order_filled(self, pair, trade, order, current_time, **kwargs) -> None:
        """Emit execution record on every fill (entry or exit).

        Per Phase-8 synthesis P0-A.2 (2026-05-13): decision_price is read
        from the cached signal record's `decision_price` field (which the
        daemon now persists per P0-A.1). Falls back to `rate` only if no
        matching signal is found — this fallback yields realized_return=0
        for orphan fills, which is correct (we have no decision context).
        """
        try:
            # order_filled delivers fill data on the `order` object (freqtrade Order:
            # safe_price = fill price, safe_filled = filled qty). NOT via `rate`/`amount`
            # — those are confirm_trade_entry params and were undefined here (latent
            # NameError on the first real fill). getattr-guarded for defensiveness.
            _fill_price = getattr(order, "safe_price", None)
            if _fill_price is None:
                _fill_price = getattr(order, "average", None) or getattr(order, "price", 0.0)
            _fill_amount = getattr(order, "safe_filled", None)
            if _fill_amount is None:
                _fill_amount = getattr(order, "filled", None) or getattr(order, "amount", 0.0)
            signal = self._latest_signal_for(pair, current_time)
            signal_id = signal.get("id") if signal else None
            if signal is not None and "decision_price" in signal:
                # P0-A.2: real decision-time bar close from the bus record
                decision_price = float(signal["decision_price"])
            else:
                # Orphan fill or pre-fix bus record. Fall back to fill price
                # so realized_return = 0 (settlement loop will skip this for
                # calibrator updates per P0-A.3).
                decision_price = float(_fill_price)

            record = {
                "schema_version": 1,
                "exec_id": f"exec-{trade.id}-{order.order_id if hasattr(order, 'order_id') else 'unknown'}",
                "asof": (current_time or pd.Timestamp.utcnow()).isoformat(),
                "asset": pair,
                "side": "buy" if order.side == "buy" else "sell",
                "qty": float(_fill_amount),
                "fill_price": float(_fill_price),
                "decision_price": decision_price,
                "fees": float(getattr(order, "cost", 0.0)) * 0.001,  # estimate
                "account_id": "freqtrade",
                "asset_class": "crypto",
                "signal_id": signal_id,
                "realized_pnl": None,  # daemon settlement loop computes
            }
            _emit_execution(record)
        except Exception as e:
            logger.warning("execution emission failed: %s", e)

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _refresh_state(self, current_time: datetime | pd.Timestamp | None) -> None:
        """Update _last_heartbeat from bus + check dead-man + check halts."""
        now = pd.Timestamp(current_time) if current_time else pd.Timestamp.utcnow()
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        if self._strategy_start_time.tzinfo is None:
            self._strategy_start_time = self._strategy_start_time.tz_localize("UTC")

        # Read recent records from bus
        records = _read_jsonl_tail(self.SIGNAL_BUS_PATH, n=200)

        # Update heartbeat. ar43: scan newest-first; the first record with a VALID,
        # parseable asof wins. A null/empty/NaT asof (a garbage-producing daemon) must
        # NOT poison _last_heartbeat with NaT — that would make the age check below
        # evaluate `nan > dead_man_switch_seconds` (always False) and silently DISABLE
        # the dead-man-switch (FAIL-OPEN on a safety rail). Funnel through _parse_asof_utc
        # and `continue` past unparseable records so an earlier VALID heartbeat is still
        # found; if none is valid, _last_heartbeat stays None and the
        # no_heartbeat_observed branch fires after bootstrap.
        for r in reversed(records):
            if r.get("type") != "heartbeat":
                continue
            parsed = _parse_asof_utc(r.get("asof"))
            if parsed is None:
                continue
            if self._last_heartbeat is None or parsed > self._last_heartbeat:
                self._last_heartbeat = parsed
            break

        # Cache latest signal per pair
        for r in records:
            if r.get("type") == "heartbeat":
                continue
            asset = r.get("asset")
            if not asset:
                continue
            # ar35: keep most recent per asset by PARSED-TIMESTAMP order, not a LEXICAL
            # string compare. `r["asof"] > cur["asof"]` on raw strings only sorts
            # correctly when every asof shares one exact format; mixed forms (a trailing
            # "Z" vs "+00:00", a naive "YYYY-MM-DD HH:MM:SS", differing precision) sort
            # lexically out of chronological order, so a STALE signal can win the cache and
            # the consumer then sizes/trades on its (possibly opposite) direction. Parse
            # both to UTC Timestamps and compare those; an unparseable/NaT candidate is
            # never allowed to displace an existing cached signal.
            cur = self._signal_cache.get(asset)
            if cur is None:
                self._signal_cache[asset] = r
                continue
            new_ts = _parse_asof_utc(r.get("asof"))
            cur_ts = _parse_asof_utc(cur.get("asof"))
            if new_ts is None:
                continue  # can't establish recency -> do not displace
            if cur_ts is None or new_ts > cur_ts:
                self._signal_cache[asset] = r

        # Dead-man-switch (synthesis-v2 §P0-C)
        bootstrap_age = (now - self._strategy_start_time).total_seconds()
        if self._last_heartbeat is None:
            if bootstrap_age > self.bootstrap_grace_seconds:
                self._enter_safe_stop("no_heartbeat_observed_after_bootstrap")
        else:
            age = (now - self._last_heartbeat).total_seconds()
            if age > self.dead_man_switch_seconds:
                self._enter_safe_stop("heartbeat_stale")

        # Halt mirror check
        try:
            if self.HALT_STATE_MIRROR.exists():
                halts = json.loads(self.HALT_STATE_MIRROR.read_text())
                if halts:
                    self._enter_safe_stop(f"halt_active: {halts[0].get('reason', 'unknown')}")
        except (json.JSONDecodeError, OSError):
            pass

    def _latest_signal_for(self, pair: str, current_time) -> dict | None:
        """Return the most recent valid signal for this pair, or None.

        The four fail-CLOSED drop branches below (no cached signal, wrong schema,
        stale asof, unparseable asof) are correct money-software posture — a
        degraded upstream must NOT produce a trade. But a bare `return None` makes
        the halt unobservable: a daemon emitting fresh heartbeats but garbage
        per-asset signals keeps the heartbeat dead-man-switch satisfied, so the
        operator sees zero new trades, zero logs, and a live daemon —
        indistinguishable from a deliberate quiet day (ar28: unobservable silence
        on a money rail defeats the operator). Each drop therefore logs ONCE on the
        had-signal -> dropping edge via _note_signal_drop (suppressed on repeats;
        re-logged after recovery), mirroring the _enter_safe_stop idiom.
        """
        self._refresh_state(current_time)

        sig = self._signal_cache.get(pair)
        if sig is None:
            # No signal cached yet (startup / quiet pair) is the normal state, not
            # a degradation of an existing signal — do not log/latch it.
            return None

        # Schema version check
        if sig.get("schema_version") != 1:
            self._note_signal_drop(
                pair, "bad_schema", f"schema_version={sig.get('schema_version')!r}"
            )
            return None

        # Stale signal guard
        # ar43: pd.Timestamp returns NaT (WITHOUT raising) for None / "" / nan / JSON-null
        # asof — only literal-garbage strings raise. A NaT asof makes age_minutes NaN, and
        # `nan > threshold` is False, so the staleness check would NOT trip and an
        # unknowable-age signal would be RETURNED (fail-OPEN) to drive an entry + sizing.
        # An asof we cannot order cannot establish freshness, so refuse to act — fail
        # CLOSED, matching the documented contract that stale signals are ignored. Reuse
        # the ar35 _parse_asof_utc helper (NaT/garbage -> None).
        asof = _parse_asof_utc(sig.get("asof"))
        if asof is None:
            return None
        try:
            now = _parse_asof_utc(current_time) if current_time else pd.Timestamp.utcnow().tz_localize("UTC")
            if now is None:
                return None
            age_minutes = (now - asof).total_seconds() / 60.0
            if age_minutes > self.max_signal_age_minutes:
                self._note_signal_drop(
                    pair, "stale", f"age_minutes={age_minutes:.1f} > {self.max_signal_age_minutes}"
                )
                return None
        except (ValueError, KeyError) as e:
            self._note_signal_drop(pair, "unparseable_asof", f"asof={sig.get('asof')!r} ({e})")
            return None

        # Usable signal — clear any latched drop reason so a future drop logs again.
        self._note_signal_recovery(pair)
        return sig

    def _note_signal_drop(self, pair: str, reason: str, detail: str) -> None:
        """Edge-triggered drop logging: warn ONCE on the transition into (or between)
        drop reasons for a pair, then suppress repeats until the pair recovers.

        Runs every candle per pair, so naive per-call logging would spam — we only
        log when the latched reason for the pair changes (mirrors _enter_safe_stop,
        which only logs on the not-active -> active edge)."""
        if self._signal_drop_reason.get(pair) != reason:
            logger.warning(
                "signal dropped for %s: %s (%s) — trading halted for this pair "
                "until a usable signal arrives (no trade emitted)",
                pair,
                reason,
                detail,
            )
        self._signal_drop_reason[pair] = reason

    def _note_signal_recovery(self, pair: str) -> None:
        """Clear a pair's latched drop reason on a usable signal; log the recovery
        edge so the operator can see WHEN trading resumed."""
        if self._signal_drop_reason.get(pair) is not None:
            logger.warning("signal recovered for %s — usable signal observed", pair)
        self._signal_drop_reason[pair] = None

    def _enter_safe_stop(self, reason: str) -> None:
        if not self._safe_stop_active:
            logger.warning("entering safe-stop: %s", reason)
        self._safe_stop_active = True
        self._safe_stop_reason = reason
