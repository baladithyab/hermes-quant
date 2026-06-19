"""hermes_quant.playbook.profile_scan — asof profile-fit scanner pipeline (W3).

ONE asof-pinned profile-fit scanner that REPLACES the legacy 5-bucket fan-out
(``watchlist_evolution.evolve_watchlist`` over ``play-fit.json``), emitting a
SINGLE profile-fit watchlist (not ``{play: [...]}``). The decision layer
(``structure_select`` + the deterministic gate) picks the STRUCTURE per-tick;
the watchlist NEVER pre-picks ``covered_call``/``csp``/etc. — it answers only
"does this ticker fit WHAT WE TRADE" (profile-fit), strategy-agnostic.

Pipeline
--------
1. SCAN UNIVERSE (asof-pinned, no-lookahead): read the universe artifact
   (``~/.hermes/quant/universe/alpaca-daily.json`` — already asof-stamped).
   Pass symbols through ``universe.point_in_time.filter_listed_at_asof`` FIRST
   so a backtest/replay universe is survivorship-safe (default-OFF passthrough
   today; the asof seam is already there). The artifact's per-symbol fields
   (ADV, last_close, tradable, shortable) seed the TickerProfile DIRECTLY — no
   yfinance refetch for the liquidity/price/tradable rails; only
   market_cap/quote_type/realized_vol/spread come from
   ``compute_play_snapshot`` (reuse ``prewarm_snapshot_cache`` for the parallel
   fetch).
2. PROFILE-FIT SCORE: build the unified ``TickerProfile`` per symbol (asof =
   the universe artifact's asof, threaded through ``compute_play_snapshot``'s
   asof param so all features are as-of-honest — NO ``datetime.now`` leakage),
   score it ONCE against the single ``TICKER_PROFILE`` via
   ``score_ticker_profile``. NO per-play loop, NO strategy bucketing.
3. SELECT: rank eligible tickers by fit_score desc (symbol asc tie-break,
   deterministic, NaN-safe), apply ONE global cap (``max_watchlist``).
4. EMIT ONE WATCHLIST: write a single profile-fit state file
   (``~/.hermes/quant/watchlist/profile-fit.json`` — NEW path so it never
   clobbers ``play-fit.json``) whose active rows each carry: symbol,
   asset_class, options_eligible, shortable, horizon_set (the multi-horizon
   list), fit_score, asof.

The importable core is ``build_profile_watchlist`` — the package seam: aegis
can import it with a universe artifact + the profile fitness and get a
watchlist with zero hermes-cron/state.db/broker dependencies. It is READ-ONLY
(touches no risk gate, no state.db, no kill-switch — same posture as
``point_in_time.py`` and the advisor).

Posture (RAILS)
---------------
* **Default-OFF.** ``HERMES_QUANT_PROFILE_SCAN=1`` gates the CRON/autonomous
  integration of this scanner's output. When OFF, this module is never entered
  by the cron path; ``evolve_watchlist`` runs the EXISTING 5-bucket path
  verbatim over ``play-fit.json``. The standalone CLI / library entry is always
  runnable directly (running a tool by hand is the operator's choice).
* **Silence-by-default.** A missing/malformed universe artifact -> empty
  result, no raise, no file clobber.
* **No-lookahead.** ``asof`` defaults to the universe artifact's own asof and
  is threaded through every snapshot fetch; never ``datetime.now``.
* **Atomic writes.** ``profile-fit.json`` is written via tempfile + fsync +
  ``os.replace``.
* **Read-only.** Touches no risk gate, no state.db, no kill-switch.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from hermes_quant.home import quant_home as _resolve_quant_home
from hermes_quant.universe.point_in_time import filter_listed_at_asof

# Parallel snapshot prewarm + per-symbol enrich live in scorers. Bound at module
# scope so tests can monkeypatch them (the asof-thread RED test). They are the
# ONLY data-acquisition dependency, and only touched when ``fetch=True``.
from .scorers import compute_play_snapshot, prewarm_snapshot_cache

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Default-OFF flag (RAILS). Quoted-literal "0" default so the flag-inventory
# scanner's _CONST regex picks it up; fail-closed == "1" so a typo never
# silently enables the cron/autonomous integration of this scanner's output.
# --------------------------------------------------------------------------- #
_FLAG = "HERMES_QUANT_PROFILE_SCAN"


def _profile_scan_enabled() -> bool:
    """True iff the cron/autonomous integration of the profile scanner is enabled.

    Fail-closed: only the exact string ``"1"`` enables it. Any other value
    (unset, ``"0"``, a typo like ``"true"``) leaves the legacy 5-bucket path the
    default. NOTE the standalone library entry (``build_profile_watchlist``) and
    the CLI are ALWAYS runnable regardless of this flag — the flag gates the
    automatic cron wiring, not a hand-run of the tool.
    """
    return os.environ.get(_FLAG, "0") == "1"


# Default output path for the SINGLE profile-fit watchlist. NEW path — it never
# clobbers the legacy per-play ``play-fit.json``.
DEFAULT_PROFILE_WATCHLIST_PATH = (
    _resolve_quant_home() / "watchlist" / "profile-fit.json"
)
DEFAULT_UNIVERSE_PATH = (
    _resolve_quant_home() / "universe" / "alpaca-daily.json"
)

# Default global cap on the single watchlist (replaces per-play caps).
DEFAULT_MAX_WATCHLIST = 50


# --------------------------------------------------------------------------- #
# W1 / W2 composition seam.
#
# W1 owns the unified TickerProfile fitness (ticker_profile.py): TickerProfile,
# score_ticker_profile(snapshot) -> TickerFitness. W2 owns the multi-horizon set
# (horizons.py): default_horizon_set(). We import them so this lane COMPOSES with
# W1/W2 when they land. Until then (this lane built in isolation), a minimal
# in-module fallback that mirrors the AGREED contract exactly keeps the pipeline
# testable AND keeps the byte-identical posture: the fallback reuses the EXISTING
# scorers grammar (_score_against / _eval_rule / _eval_eviction) verbatim — no
# new rule engine — and the least-restrictive profile-fit floors from the design.
# When W1/W2's real modules are present they WIN (the import succeeds and the
# fallback is dead).
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - exercised once W1 lands
    from .ticker_profile import (  # type: ignore[attr-defined]
        TickerFitness,
        score_ticker_profile,
    )

    _HAVE_W1 = True
except Exception:  # noqa: BLE001 - W1 not landed yet in this lane
    _HAVE_W1 = False

    from dataclasses import dataclass, field

    from .profiles import PlayProfile
    from .scorers import _eval_eviction, _eval_rule  # reuse EXISTING grammar

    # Eligibility floor for the unified profile-fit score (same 0.6/0.4 weighting
    # as scorers.py). Eval-gated starting point.
    _FIT_FLOOR = 0.65

    @dataclass
    class TickerFitness:  # type: ignore[no-redef]
        """Result of scoring one symbol against the single TICKER_PROFILE.

        Mirrors ``scorers.PlayFitness`` (symbol/fit_score/pass_hard/eligible/
        failed_rules) but for the ONE strategy-agnostic profile-fit profile.
        """

        symbol: str
        fit_score: float
        pass_hard: bool
        eligible: bool
        failed_rules: list[str] = field(default_factory=list)

    # The single strategy-agnostic profile-fit profile. PROFILE-FIT rules only —
    # the LEAST-restrictive floor across the 5 profiles so the scanner is
    # strategy-agnostic. Strategy-specific rules (regime gates, per-play tightened
    # floors, momentum/quality/credit fields) DO NOT live here; they drop to the
    # decision layer (structure_select + the gate). The fallback uses NO
    # regime_gates so it is regime-agnostic (the watchlist must NOT pre-deny on
    # regime). spread_pct / realized_vol_30d are SOFT so missing data never
    # rejects (silence-by-default).
    # market_cap_usd is the small-cap-trap rail expressed as an EVICTION (the
    # "loosest eviction floor (csp's 5e8)" per the design). Eviction semantics
    # are exactly what the standalone --no-fetch path needs: a PRESENT cap below
    # 5e8 evicts, while ABSENT cap (None) does NOT evict (it abstains —
    # "those rails abstain ... missing data never rejects"). Keeping it hard
    # would fail-closed on absent data and reject every liquid name in the
    # zero-network mode. spread_pct / realized_vol_30d are SOFT so missing data
    # never rejects (silence-by-default). NO regime_gates — the watchlist must
    # NOT pre-deny on regime (the decision gate owns regime-vs-direction).
    _TICKER_PROFILE_FALLBACK = PlayProfile(
        name="ticker_profile",
        bias="agnostic",
        hard_rules={
            "quote_type": ("eq", "EQUITY"),
            "avg_dollar_volume_30d": ("ge", 2e6),
            "last_close": ("between", 5.0, 500.0),
        },
        soft_rules={
            "spread_pct": ("le", 0.01),
            "realized_vol_30d": ("between", 0.05, 1.5),
        },
        eviction_rules={
            "non_equity": ("ne_field", "quote_type", "EQUITY"),
            "price_too_low": ("lt_field", "last_close", 5.0),
            "adv_too_thin": ("lt_field", "avg_dollar_volume_30d", 2e6),
            "market_cap_too_small": ("lt_field", "market_cap_usd", 5e8),
            "vol_runaway": ("gt_field", "realized_vol_30d", 2.0),
            "not_tradable": ("ne_field", "tradable", True),
        },
    )

    def score_ticker_profile(snapshot: dict) -> TickerFitness:  # type: ignore[no-redef]
        """Score a snapshot ONCE against the single TICKER_PROFILE.

        Reuses the EXISTING rule grammar (``_eval_rule`` / ``_eval_eviction``)
        verbatim — NO new rule engine. ONE profile, ONE score, NO play bucketing.

        Differs from ``scorers._score_against`` in ONE deliberate way that the
        strategy-agnostic profile-fit posture requires: a SOFT rule whose input
        is absent (None/NaN -> ``_eval_rule`` returns None) ABSTAINS — it is
        excluded from the soft denominator rather than counted as a miss. That
        is the "missing data never rejects" semantics the standalone --no-fetch
        path needs: a liquid, in-band, tradable EQUITY whose yfinance-only
        spread/vol fields are absent must NOT be dragged below the fit floor by
        the absence alone. (scorers._score_against counts a None soft as a 0,
        which would reject every zero-network profile.) Hard rules keep the
        silence-by-default fail-on-None semantics; evictions keep the
        don't-evict-on-missing-data semantics.
        """
        profile = _TICKER_PROFILE_FALLBACK
        symbol = str(snapshot.get("symbol", "?"))
        failed: list[str] = []

        # --- evictions (missing data does NOT evict) --------------------- #
        evicted = False
        for ev_name, ev_rule in profile.eviction_rules.items():
            if _eval_eviction(snapshot, ev_rule):
                failed.append(f"evict:{ev_name}")
                evicted = True

        # --- hard rules (None -> fail; silence-by-default) --------------- #
        n_hard = len(profile.hard_rules)
        n_hard_pass = 0
        for fname, rule in profile.hard_rules.items():
            result = _eval_rule(snapshot.get(fname), rule)
            if result is True:
                n_hard_pass += 1
            else:
                failed.append(f"hard:{fname}")
        pass_hard = (n_hard_pass == n_hard) and not evicted

        # --- soft rules (None -> ABSTAIN; excluded from denominator) ----- #
        n_soft_eval = 0
        n_soft_pass = 0
        for fname, rule in profile.soft_rules.items():
            result = _eval_rule(snapshot.get(fname), rule)
            if result is None:
                continue  # abstain — missing data never rejects
            n_soft_eval += 1
            if result is True:
                n_soft_pass += 1

        # --- score (same 0.6/0.4 weighting as scorers.py) ---------------- #
        hard_frac = (n_hard_pass / n_hard) if n_hard else 1.0
        # No evaluable soft rule -> soft_frac defaults to 1.0 (mirrors
        # scorers.py `pass_soft = ... if n_soft else True`).
        soft_frac = (n_soft_pass / n_soft_eval) if n_soft_eval else 1.0
        score = 0.6 * hard_frac + 0.4 * soft_frac
        score = max(0.0, min(1.0, score))

        eligible = bool(pass_hard and score >= _FIT_FLOOR and not evicted)
        return TickerFitness(
            symbol=symbol,
            fit_score=round(score, 4),
            pass_hard=pass_hard,
            eligible=eligible,
            failed_rules=failed,
        )


try:  # pragma: no cover - exercised once W2 lands
    from .horizons import default_horizon_set  # type: ignore[attr-defined]

    _HAVE_W2 = True
except Exception:  # noqa: BLE001 - W2 not landed yet in this lane
    _HAVE_W2 = False

    _ZERO_DTE_FLAG = "HERMES_QUANT_ZERO_DTE"

    def default_horizon_set() -> list[str]:  # type: ignore[no-redef]
        """The 5-rung multi-horizon set attached to every profile-fit entry.

        0D/0DTE is OMITTED unless ``HERMES_QUANT_ZERO_DTE=1`` (its own
        default-OFF flag; fail-closed == "1"), so by default the operator SEES
        1D-30D.
        """
        base = ["1D", "7D", "14D", "30D"]
        if os.environ.get(_ZERO_DTE_FLAG, "0") == "1":
            return ["0D", *base]
        return base


# --------------------------------------------------------------------------- #
# asof helpers
# --------------------------------------------------------------------------- #


def _coerce_asof_dt(asof: date | datetime | str | None) -> datetime | None:
    """Coerce an asof to a tz-aware datetime. None -> None (caller decides).

    Unlike ``compute_play_snapshot`` we DO NOT default a None asof to
    ``datetime.now`` here — the WHOLE point of this scanner is no-lookahead, so
    a None asof must be resolved to the universe artifact's own asof upstream,
    never silently to wall-clock.
    """
    if asof is None:
        return None
    if isinstance(asof, datetime):
        return asof if asof.tzinfo is not None else asof.replace(tzinfo=UTC)
    if isinstance(asof, date):
        return datetime(asof.year, asof.month, asof.day, tzinfo=UTC)
    if isinstance(asof, str):
        text = asof.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.fromisoformat(text[:10])
            except ValueError:
                logger.warning("profile_scan: unparseable asof %r", asof)
                return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


# --------------------------------------------------------------------------- #
# Universe artifact read
# --------------------------------------------------------------------------- #


def _read_universe_artifact(universe_path: Path) -> tuple[str | None, list[dict]]:
    """Return ``(asof, rows)`` from the universe artifact.

    Silence-by-default: a missing/malformed file -> ``(None, [])``. Each row is
    the per-symbol dict from the artifact (symbol, avg_dollar_volume_30d,
    last_close, tradable, shortable, ...). Bare-string symbol entries are
    normalized to a ``{"symbol": ...}`` dict so downstream is uniform.
    """
    if not universe_path.exists():
        logger.warning("profile_scan: universe artifact missing: %s", universe_path)
        return None, []
    try:
        payload = json.loads(universe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("profile_scan: failed to parse universe artifact: %s", exc)
        return None, []
    artifact_asof = payload.get("asof")
    rows: list[dict] = []
    for entry in payload.get("symbols") or []:
        if isinstance(entry, str):
            rows.append({"symbol": entry})
        elif isinstance(entry, dict) and entry.get("symbol"):
            rows.append(dict(entry))
    return (str(artifact_asof) if artifact_asof is not None else None), rows


# --------------------------------------------------------------------------- #
# TickerProfile snapshot assembly
# --------------------------------------------------------------------------- #


def _build_snapshot(
    artifact_row: dict,
    asof_dt: datetime,
    *,
    fetch: bool,
) -> dict:
    """Build the scored snapshot dict for ONE symbol.

    Seeds the liquidity/price/tradable/shortable rails DIRECTLY from the universe
    artifact's own fields (no yfinance needed for these), then — only when
    ``fetch=True`` — overlays the yfinance-derived
    market_cap/quote_type/realized_vol/spread from ``compute_play_snapshot``
    (which the caller has parallel-prewarmed). With ``fetch=False`` those rails
    abstain (None -> soft-rule miss / hard-rule fail for quote_type), the
    genuinely-standalone zero-network path.

    The returned dict is the SNAPSHOT the scorer grammar consumes — the artifact
    fields ALWAYS win for the rails the artifact owns (ADV/last_close/tradable),
    so a stale yfinance value can't override the asof-pinned artifact liquidity.
    """
    symbol = str(artifact_row["symbol"]).upper()

    snap: dict[str, Any] = {
        "symbol": symbol,
        "asof": asof_dt.isoformat(),
        # Profile-fit fields the snapshot may or may not fill:
        "quote_type": None,
        "market_cap_usd": None,
        "realized_vol_30d": None,
        "spread_pct": None,
    }

    # Liquidity/price/tradable/shortable — seeded DIRECTLY from the artifact.
    snap["avg_dollar_volume_30d"] = artifact_row.get("avg_dollar_volume_30d")
    snap["last_close"] = artifact_row.get("last_close")
    snap["tradable"] = artifact_row.get("tradable")
    snap["shortable"] = artifact_row.get("shortable")
    # spread_pct: source from the artifact when present (else None -> rail
    # abstains via the soft-rule None semantics; silence-by-default).
    if "spread_pct" in artifact_row:
        snap["spread_pct"] = artifact_row.get("spread_pct")

    if fetch:
        try:
            enriched = compute_play_snapshot(symbol, asof_dt)
        except Exception:  # noqa: BLE001 - silence-by-default per symbol
            enriched = None
        if isinstance(enriched, dict):
            # Overlay ONLY the fields the artifact does not own. The artifact's
            # asof-pinned liquidity/price/tradable rails are authoritative.
            for f in ("quote_type", "market_cap_usd", "realized_vol_30d", "spread_pct"):
                v = enriched.get(f)
                if v is not None:
                    snap[f] = v
    else:
        # Zero-network standalone path: with no quote_type from the artifact we
        # cannot prove EQUITY from yfinance. Treat the artifact's tradable
        # us_equity universe as EQUITY-shaped for the profile-fit equity rail
        # (the universe scanner already filtered to asset_class==us_equity), so
        # the optionable-equity hard rule does not auto-fail on missing data.
        snap["quote_type"] = "EQUITY"

    return snap


# --------------------------------------------------------------------------- #
# Ranking (deterministic, NaN-safe)
# --------------------------------------------------------------------------- #


def _rank_key(symbol: str, fit_score: float) -> tuple[int, float, str]:
    """Deterministic, NaN-safe rank key: fit_score DESC, symbol ASC tie-break.

    NaN scores sort to the tail (treated as lowest). Mirrors
    ``watchlist_evolution._enforce_cap_trim``'s NaN-safe ordering idiom so the
    selection is identical across runs (no RNG, no wall-clock).
    """
    is_nan = fit_score != fit_score  # True only for NaN
    return (1 if is_nan else 0, 0.0 if is_nan else -fit_score, symbol)


# --------------------------------------------------------------------------- #
# Atomic write
# --------------------------------------------------------------------------- #


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic write: tempfile + fsync + os.replace. POSIX-safe.

    Mirrors ``watchlist_evolution._atomic_write_json`` so the new emit path has
    the same crash-safety as the legacy one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
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


# --------------------------------------------------------------------------- #
# Public API — the importable core (the package seam)
# --------------------------------------------------------------------------- #


def build_profile_watchlist(
    universe_path: Path | str = DEFAULT_UNIVERSE_PATH,
    asof: date | datetime | str | None = None,
    *,
    fetch: bool = True,
    max_watchlist: int = DEFAULT_MAX_WATCHLIST,
    listing_table: Any | None = None,
    out_path: Path | str | None = None,
    force_pit: bool | None = None,
) -> dict:
    """Build ONE profile-fit watchlist from an asof-pinned universe artifact.

    This is the package seam: aegis can import it with a universe artifact + the
    profile fitness and get a watchlist with ZERO hermes-cron/state.db/broker
    dependencies. READ-ONLY: touches no risk gate, no state.db, no kill-switch.

    Pipeline: read asof-pinned universe artifact -> run
    ``filter_listed_at_asof`` FIRST (no-lookahead) -> build a ``TickerProfile``
    snapshot per symbol (seed liquidity/price/tradable from the artifact;
    ``fetch=False`` skips yfinance) -> score ONCE via the W1 profile fitness ->
    rank by fit_score (NaN-safe deterministic) -> apply ONE global cap -> attach
    the W2 default horizon set -> emit ONE ``profile-fit.json`` (NEW path; never
    touches ``play-fit.json``).

    Args:
        universe_path: the asof-pinned universe artifact (the only required
            external input).
        asof: as-of date for the no-lookahead snapshot. Defaults to the universe
            artifact's OWN asof (replay-safe honesty). Never silently
            ``datetime.now``.
        fetch: when True, parallel-prewarm + enrich each profile via yfinance
            (``compute_play_snapshot``) for market_cap/quote_type/realized_vol/
            spread. When False, build profiles from the artifact fields ALONE
            (zero network — the standalone aegis path); the yfinance-only rails
            abstain.
        max_watchlist: the ONE global cap on the emitted active list.
        listing_table: optional point-in-time listing table for survivorship-
            safe filtering (``filter_listed_at_asof``).
        out_path: where to write the single profile-fit JSON. Defaults to
            ``profile-fit.json``. NEVER ``play-fit.json``.
        force_pit: test/override seam forwarded to ``filter_listed_at_asof``
            (``True``/``False`` forces PIT on/off; ``None`` consults the
            ``HERMES_QUANT_PIT_UNIVERSE`` flag).

    Returns:
        ``{"asof": <iso str>, "active": [row, ...], "max_watchlist": int,
           "n_scanned": int, "n_eligible": int}`` where each ``row`` carries
        ``symbol, asset_class, options_eligible, shortable, horizon_set,
        fit_score, asof``. On a missing/empty universe -> ``active == []``
        (silence-by-default), and NO file is written.
    """
    universe_path = Path(universe_path)

    artifact_asof, rows = _read_universe_artifact(universe_path)

    # Resolve the effective asof: caller override wins, else the artifact's own
    # asof (no-lookahead honesty), else — only if both are absent and we have an
    # empty universe — we surface the caller's asof string verbatim (which may
    # be None) without leaking now().
    effective_asof_str = (
        (asof if isinstance(asof, str) else None)
        or artifact_asof
    )
    asof_dt = _coerce_asof_dt(asof) or _coerce_asof_dt(artifact_asof)

    # Silence-by-default: missing/empty universe -> empty result, no file write,
    # no clobber.
    if not rows:
        return {
            "asof": effective_asof_str if effective_asof_str is not None else (
                asof if isinstance(asof, str) else None
            ),
            "active": [],
            "max_watchlist": max_watchlist,
            "n_scanned": 0,
            "n_eligible": 0,
        }

    if asof_dt is None:
        # We have rows but no parseable asof anywhere. Refuse to silently leak
        # wall-clock now() into the snapshot — fail-closed to an empty list with
        # a warning. (The standalone caller can always pass --asof explicitly.)
        logger.warning(
            "profile_scan: universe has %d symbols but no parseable asof "
            "(artifact_asof=%r, asof arg=%r) — refusing to leak now(); empty result",
            len(rows),
            artifact_asof,
            asof,
        )
        return {
            "asof": effective_asof_str,
            "active": [],
            "max_watchlist": max_watchlist,
            "n_scanned": len(rows),
            "n_eligible": 0,
        }

    emit_asof = effective_asof_str or asof_dt.isoformat()

    # 1) SCAN — PIT filter FIRST (no-lookahead / survivorship-safe). Default-OFF
    #    passthrough preserves order + identity (the asof seam is already there).
    symbols_in_order = [str(r["symbol"]).upper() for r in rows]
    row_by_symbol = {str(r["symbol"]).upper(): r for r in rows}
    kept = filter_listed_at_asof(
        symbols_in_order, asof_dt, listing_table, force=force_pit
    )

    # 2) Optional parallel prewarm of the yfinance snapshot cache (fetch path
    #    only). asof is THREADED so the cache key + every feature is as-of-honest
    #    — never datetime.now(). Silence-by-default on prewarm failure.
    if fetch:
        try:
            prewarm_snapshot_cache(list(kept), asof=asof_dt)
        except Exception as exc:  # noqa: BLE001 - never crash the scan on prewarm
            logger.warning("profile_scan: prewarm failed (%s); per-symbol fallback", exc)

    # 3) PROFILE-FIT SCORE — ONE profile, ONE score per symbol. NO per-play loop.
    horizon_set = list(default_horizon_set())
    scored: list[dict] = []
    n_eligible = 0
    for symbol in kept:
        artifact_row = row_by_symbol.get(symbol)
        if artifact_row is None:
            continue
        snap = _build_snapshot(artifact_row, asof_dt, fetch=fetch)
        fit = score_ticker_profile(snap)
        if not fit.eligible:
            continue
        n_eligible += 1
        # options_eligible: optionable-equity check. We have no options-chain
        # probe in the standalone path, so we conservatively mark an EQUITY-
        # shaped tradable name options_eligible (the decision layer's
        # options_gate is the FINAL authority on whether a chain actually
        # trades). shortable comes straight from the artifact.
        is_equity = snap.get("quote_type") == "EQUITY"
        scored.append(
            {
                "symbol": symbol,
                # rt05: emit the CANONICAL watchlist class, not the Alpaca
                # universe FILTER token. The universe artifact's own
                # ``filters.asset_class == "us_equity"`` is the Alpaca scanner
                # contract (see universe.alpaca_scanner) and stays "us_equity";
                # but the autonomous/advisor tick + watchlist key on the
                # canonical "equity" (watchlist._VALID_ASSET_CLASSES), so the
                # EMITTED row must carry "equity" to route correctly once W4 is
                # wired into the live tick. materialize_profile_fit_entries also
                # validates this fail-closed (defense in depth).
                "asset_class": "equity",
                "options_eligible": bool(is_equity and artifact_row.get("tradable", True)),
                "shortable": bool(artifact_row.get("shortable", False)),
                "horizon_set": list(horizon_set),
                "fit_score": float(fit.fit_score),
                "asof": emit_asof,
            }
        )

    # 4) SELECT — rank by fit_score desc (symbol asc tie-break, NaN-safe) and
    #    apply the ONE global cap.
    scored.sort(key=lambda row: _rank_key(row["symbol"], row["fit_score"]))
    cap = max(0, int(max_watchlist))
    active = scored[:cap]

    result = {
        "asof": emit_asof,
        "active": active,
        "max_watchlist": cap,
        "n_scanned": len(rows),
        "n_eligible": n_eligible,
    }

    # 5) EMIT ONE WATCHLIST — write the single profile-fit JSON. NEW path; the
    #    legacy play-fit.json is never touched.
    target = Path(out_path) if out_path is not None else DEFAULT_PROFILE_WATCHLIST_PATH
    if target.name == "play-fit.json":
        # Hard guard: this scanner must NEVER write the legacy per-play state.
        raise ValueError(
            "profile_scan refuses to write play-fit.json — emit the SINGLE "
            "profile-fit watchlist to a NEW path (default profile-fit.json)"
        )
    _atomic_write_json(target, result)

    return result


__all__ = [
    "build_profile_watchlist",
    "score_ticker_profile",
    "TickerFitness",
    "default_horizon_set",
    "DEFAULT_PROFILE_WATCHLIST_PATH",
    "DEFAULT_UNIVERSE_PATH",
    "DEFAULT_MAX_WATCHLIST",
]
