#!/usr/bin/env python3
"""Per-play evolving watchlist tick (CLI shim).

Reads the universe at ~/.hermes/quant/universe/alpaca-daily.json (full Alpaca
liquid universe, ~500 symbols), parallel-prewarms the per-symbol yfinance
snapshot cache, then runs the play-fitness scorers against it via
``evolve_watchlist``. Atomic-writes the evolved watchlist state to
~/.hermes/quant/watchlist/play-fit.json. Append-only audit log at
~/.hermes/quant/watchlist/journal.jsonl.

PERFORMANCE (2026-05-27)
------------------------
Root cause of the prior 120s timeouts: ``score_symbol`` calls
``compute_play_snapshot``, which makes 3 yfinance HTTP requests per symbol.
Run serially across 500 symbols that's ~3-5 minutes wall time.

Fix lives in the library — ``hermes_quant.playbook.scorers.prewarm_snapshot_cache``
fans the same fetches across a thread pool (default 12 workers) so the cache
is warm before ``evolve_watchlist``'s serial per-play loop starts. After
prewarm, ``score_symbol`` lookups are pure dict reads.

Cron's ``script_timeout_seconds`` was also bumped 120→600 in
``~/.hermes/config.yaml`` for headroom; under normal conditions the prewarm
finishes in ~30-60s so the timeout is no longer the operational ceiling.

History note: a 2026-05-27 sibling-agent patch landed a duplicate inline
parallelization here AND silently narrowed the universe to top-100 symbols.
The library prewarm replaces the inline path; the universe stays at full
500 symbols (narrowing was a semantic policy change unrelated to the
timeout problem). Sidecar backup at
``quant-watchlist-evolve.py.sibling-2026-05-27.bak`` if the narrowed-universe
path ever needs to be restored.

CRON SUGGESTION
---------------
Run weekdays at 06:45 America/New_York — after the universe scanner
completes at 06:30 ET — so the watchlist is fresh before the open. Cron
in UTC:

    45 11 * * 1-5  source $HOME/.hermes/secrets/alpaca.env && \\
        $HOME/.hermes/hermes-agent/venv/bin/python3 \\
        $HOME/.hermes/scripts/quant-watchlist-evolve.py \\
        >> $HOME/.hermes/quant/watchlist/evolve.log 2>&1

POSTURE
-------
Silence-by-default: if the universe file is missing the script logs a
warning and exits 0 without modifying any state. The scorer is
dependency-injected — when ``hermes_quant.playbook.scorers`` is available
it is used; otherwise a stub keeps the loop running without onboarding
anything (stub score < onboard floor).

Stdout is silent when no events fire; only summary on changes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Re-exec under the hermes-agent venv if needed (where hermes_quant is installed).
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

# Best-effort: source alpaca creds from ~/.hermes/secrets/alpaca.env so that
# any data-dependent scorer can find them. The evolution function itself
# does NOT call alpaca/yfinance — but the injected scorer might.
SECRETS = Path.home() / ".hermes" / "secrets" / "alpaca.env"
if SECRETS.exists() and not (
    os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
):
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")  # bash strips shell quotes; Python doesn't
            os.environ.setdefault(k.strip(), v)

logging.basicConfig(
    level=os.environ.get("HERMES_QUANT_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


from hermes_quant.playbook.watchlist_evolution import evolve_watchlist  # noqa: E402

# Try to import the real scorer + the parallel prewarm helper. If the scorers
# module isn't ready yet (older hermes-quant install), fall back to None —
# evolve_watchlist will use its silent stub. Keeps the cron green during any
# in-flight library refactor.
try:
    from hermes_quant.playbook.scorers import (  # type: ignore[attr-defined]
        prewarm_snapshot_cache,
        score_symbol,
    )

    _scorer = score_symbol
except (ImportError, AttributeError):
    _scorer = None
    prewarm_snapshot_cache = None  # type: ignore[assignment]


def _read_universe_symbols(universe_path: Path) -> list[str]:
    """Parse the universe JSON and return the list of tradable symbols.

    Returns [] on any parse error so the caller can fall through to
    serial scoring rather than crash. evolve_watchlist re-reads the same
    file and surfaces structural errors via its own logging path.
    """
    if not universe_path.exists():
        return []
    try:
        payload = json.loads(universe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[str] = []
    for entry in payload.get("symbols") or []:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict) and "symbol" in entry:
            out.append(str(entry["symbol"]))
    return out


# ---------------------------------------------------------------------------
# Profile-fit scanner seam (W6) — DEFAULT-OFF behind HERMES_QUANT_PROFILE_SCAN.
#
# When the flag is unset (the default), this whole block is dead: main() runs
# the EXISTING 5-bucket evolve_watchlist path over play-fit.json byte-identical
# and build_profile_watchlist (W3) is never imported. When the flag is "1", the
# cron calls build_profile_watchlist with the universe path + the universe
# artifact's asof, which emits a SINGLE profile-fit.json (the autonomous-
# consumed watchlist) and the 5-bucket evolve is skipped.
#
# The flag CONSTANT-of-record lives in hermes_quant/playbook/profile_scan.py
# (W3); the inventory scanner picks it up there. This cron only *reads* it to
# decide which path to take — the read is fail-closed (== "1") to match the
# project flag idiom.
# ---------------------------------------------------------------------------
_PROFILE_SCAN_FLAG = "HERMES_QUANT_PROFILE_SCAN"

# Exposed at module scope as a test/injection seam. None until the first ON-path
# call lazily imports the real W3 core. Defensive — mirrors the prewarm /
# catalyst import idiom above so the cron never crashes if hermes_quant doesn't
# yet ship profile_scan (in-flight library refactor / older install).
_build_profile_watchlist = None  # type: ignore[assignment]


def _profile_scan_enabled() -> bool:
    """True only when HERMES_QUANT_PROFILE_SCAN == "1" (fail-closed default-OFF)."""
    return os.environ.get(_PROFILE_SCAN_FLAG, "0") == "1"


def _read_universe_asof(universe_path: Path) -> str | None:
    """Return the universe artifact's asof string (no-lookahead anchor), or None.

    The profile-fit scanner is asof-pinned: the watchlist is built as-of the
    universe artifact's own stamp, NOT datetime.now — so a replay/backtest
    universe yields an as-of-honest watchlist. Returns None if the file is
    missing/unparseable; the caller treats None as "skip" (silence-by-default).
    """
    if not universe_path.exists():
        return None
    try:
        payload = json.loads(universe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    asof = payload.get("asof")
    return str(asof) if asof else None


def _resolve_profile_watchlist_path() -> Path:
    """The single profile-fit.json target this cron requests from the W3 core.

    Mirrors ``profile_scan.DEFAULT_PROFILE_WATCHLIST_PATH`` (the NEW path —
    never ``play-fit.json``). We resolve it HERE so the cron OWNS the path it
    asked the builder to write, and can surface the TRUE destination in the
    breadcrumb instead of guessing.

    rt06-fix (test-isolation): resolve from the runtime home at CALL time rather
    than importing ``profile_scan.DEFAULT_PROFILE_WATCHLIST_PATH``. That constant
    is frozen at the W3 module's IMPORT time, so a later home redirect
    (the standard test-isolation idiom, and any future home reconfiguration) would
    not take effect and the cron would surface a stale real-home path. Computing
    the same literal here is byte-identical in production (home is stable) AND
    honors a redirected home — closing an import-order test-isolation leak that
    made test_on_calls_build_profile_watchlist fail only when a sibling test
    imported profile_scan first.

    aegis-ra-home2 (ADR-0092 home-decouple residue): route through
    ``hermes_quant.home.quant_home`` so an injected ``HERMES_QUANT_HOME`` /
    ``HERMES_HOME`` redirects the watchlist output to the SAME quant root every
    other shell module resolves — the prior ``Path.home() / ".hermes" / "quant"``
    literal honored only a ``Path.home`` redirect and silently ignored both env
    overrides. Byte-identical in production (no env -> ``quant_home()`` is exactly
    ``Path.home()/".hermes"/"quant"``).
    """
    from hermes_quant.home import quant_home

    return quant_home() / "watchlist" / "profile-fit.json"


def _run_profile_scan(universe_path: Path) -> dict | None:
    """ON-path: build the single profile-fit watchlist via the W3 core.

    Lazily imports ``build_profile_watchlist`` from
    ``hermes_quant.playbook.profile_scan`` and calls it with the universe path
    + the artifact's asof + the resolved out_path THIS cron requests. Returns
    the build summary dict (with the requested ``out_path`` attached so the
    caller can surface the true destination), or None when the universe is
    missing / the core isn't available (silence-by-default — never crash the
    cron). Writes the single profile-fit.json (the autonomous-consumed
    watchlist); does NOT touch play-fit.json.

    The W3 ``build_profile_watchlist`` contract returns
    ``{"asof", "active":[...], "max_watchlist", "n_scanned", "n_eligible"}`` —
    there is NO ``n_active`` and NO ``out_path`` key. The caller derives the
    active count from ``len(active)`` and reads the path WE requested here.
    """
    global _build_profile_watchlist

    asof = _read_universe_asof(universe_path)
    if asof is None:
        # No asof anchor → no no-lookahead-honest scan possible. Stay silent.
        return None

    builder = _build_profile_watchlist
    if builder is None:
        try:
            from hermes_quant.playbook.profile_scan import (  # type: ignore
                build_profile_watchlist as builder,
            )
        except (ImportError, AttributeError) as exc:
            # W3 core not present in this install — silence-by-default, fall
            # through to nothing (the OFF default still owns play-fit.json).
            print(
                f"WARNING: HERMES_QUANT_PROFILE_SCAN=1 but build_profile_watchlist "
                f"is unavailable ({type(exc).__name__}: {exc}); skipping profile scan",
                file=sys.stderr,
            )
            return None
        _build_profile_watchlist = builder

    out_path = _resolve_profile_watchlist_path()
    summary = builder(universe_path, asof, out_path=out_path)
    # Attach the path WE requested so the caller's breadcrumb prints the true
    # destination, not a guess. (The W3 contract itself carries no out_path.)
    if isinstance(summary, dict):
        summary["out_path"] = str(out_path)
    return summary


def main() -> int:
    # Full Alpaca liquid universe — ~500 symbols by default. The library
    # prewarm + 600s cron timeout absorbs the wall time; do NOT silently
    # narrow this without an explicit policy decision.
    #
    # cx-watchlist-home (codex PR#91 P2): resolve the universe READ through the SAME
    # quant_home() the watchlist OUTPUT uses (_profile_fit_out_path). Pre-fix the read
    # was hardcoded to Path.home()/.hermes/quant while the output honored an injected
    # HERMES_QUANT_HOME / HERMES_HOME -> under an override the script read the universe
    # from one home and wrote the watchlist to another (input/output home split).
    # Byte-identical in production (no env -> quant_home() == Path.home()/.hermes/quant).
    from hermes_quant.home import quant_home
    universe_path = quant_home() / "universe" / "alpaca-daily.json"

    # Wall-clock budget guard (added 2026-05-28). The cron's
    # script_timeout_seconds is 600s. If this script approaches that
    # ceiling we emit a stderr warning so a perf regression is caught
    # BEFORE the next cron run silently times out and corrupts the
    # downstream watchlist.
    import time
    _start_ts = time.monotonic()

    def _check_budget(stage: str, soft_warn_pct: float = 0.5, hard_warn_pct: float = 0.85) -> None:
        """Emit stderr warnings if elapsed approaches the cron timeout.

        soft_warn_pct (0.5 = 300s) — log info-level for trend tracking.
        hard_warn_pct (0.85 = 510s) — log loud warning for operator attention.
        """
        # Read the active cron timeout from config (matches scheduler)
        try:
            from hermes_agent.config import load_config  # type: ignore
            cfg = load_config() or {}
            cron_to = int((cfg.get("cron") or {}).get("script_timeout_seconds") or 600)
        except Exception:
            cron_to = 600  # match scheduler default we ship today

        elapsed = time.monotonic() - _start_ts
        if elapsed >= cron_to * hard_warn_pct:
            print(
                f"BUDGET HARD WARN: stage={stage} elapsed={elapsed:.1f}s "
                f"(>{int(hard_warn_pct*100)}% of {cron_to}s cron timeout). "
                f"Bump script_timeout_seconds or fix perf regression.",
                file=sys.stderr,
            )
        elif elapsed >= cron_to * soft_warn_pct:
            print(
                f"budget soft: stage={stage} elapsed={elapsed:.1f}s "
                f"(>{int(soft_warn_pct*100)}% of {cron_to}s cron timeout)",
                file=sys.stderr,
            )

    # 2026-05-27 hardening (P0-3 from parallel-critique review): if the
    # upstream universe file is stale (>25h old) the entire downstream
    # chain (watchlist → brief → playbook-tick) operates on yesterday's
    # data with no warning. Fail loudly instead — surface the stale-input
    # condition to the cron's stderr so the agent-deliver path catches it.
    if universe_path.exists():
        age_hours = (
            datetime.now(timezone.utc).timestamp() - universe_path.stat().st_mtime
        ) / 3600.0
        if age_hours > 25.0:
            print(
                f"WARNING: universe file is {age_hours:.1f}h old (> 25h); "
                f"watchlist evolution skipped to avoid stale-data cascade. "
                f"Path: {universe_path}",
                file=sys.stderr,
            )
            return 1

    # Parallel-prewarm the per-symbol yfinance snapshot cache before
    # evolve_watchlist's serial loop runs. The cache lives in
    # hermes_quant.playbook.scorers._SNAPSHOT_CACHE and is keyed by
    # (SYMBOL, YYYY-MM-DD) in UTC — score_symbol reads it on every call.
    # Prewarming converts the 500-symbol × 3-HTTP workload from ~3-5 min
    # serial to ~30-60s parallel.
    #
    # Silence-by-default: if the helper isn't available (older hermes_quant
    # install), or the universe failed to parse, we just skip prewarm and
    # let score_symbol fall back to serial fetches. The cron's
    # script_timeout_seconds: 600 in ~/.hermes/config.yaml gives that path
    # enough headroom too.
    universe_symbols = _read_universe_symbols(universe_path)
    if universe_symbols and prewarm_snapshot_cache is not None:
        try:
            summary = prewarm_snapshot_cache(universe_symbols)
            # One-line breadcrumb on stderr — keeps stdout silent-by-default
            # but surfaces perf data to the audit log.
            if summary["prewarmed"] or summary["errors"]:
                print(
                    f"prewarm: warmed={summary['prewarmed']} "
                    f"skipped={summary['skipped']} "
                    f"errors={summary['errors']} "
                    f"elapsed={summary['elapsed_s']}s",
                    file=sys.stderr,
                )

            # 2026-05-28 hardening: abort if upstream data feed is failing.
            # Without this, a yfinance rate-limit (HTTP 401 "Invalid Crumb"
            # from Yahoo's anti-bot) silently zeroes every score, causing
            # evolve_watchlist to evict ~all plays. The fix: if the
            # prewarm error rate exceeds a threshold, refuse to corrupt
            # the watchlist — exit 1 and let the next cron retry.
            #
            # Threshold: if >50% of prewarm attempts errored AND fewer than
            # 10% succeeded, the data feed is broken. Don't trust the
            # downstream scoring.
            if universe_symbols:
                attempted = max(1, summary["prewarmed"] + summary["errors"])
                error_rate = summary["errors"] / attempted
                success_rate = summary["prewarmed"] / max(1, len(universe_symbols))
                if error_rate > 0.5 and success_rate < 0.1:
                    print(
                        f"ABORT: data feed unreliable — "
                        f"prewarm errors={summary['errors']} "
                        f"prewarmed={summary['prewarmed']} "
                        f"universe={len(universe_symbols)} "
                        f"error_rate={error_rate:.1%} success_rate={success_rate:.1%}. "
                        f"Refusing to corrupt watchlist with bad-data scores. "
                        f"Likely yfinance rate-limit; next cron at 03:30 PT will retry.",
                        file=sys.stderr,
                    )
                    return 2  # distinct from "stale universe" (1) and "ok" (0)
        except Exception as exc:  # noqa: BLE001 — never let prewarm crash the cron
            print(
                f"WARNING: prewarm_snapshot_cache failed "
                f"({type(exc).__name__}: {exc}); falling back to serial scoring",
                file=sys.stderr,
            )
    _check_budget("after_prewarm")

    # W6 profile-fit scanner branch — DEFAULT-OFF (HERMES_QUANT_PROFILE_SCAN).
    # When ON, the cron emits the SINGLE profile-fit.json via the W3 core and
    # SKIPS the 5-bucket catalyst-onboard + evolve_watchlist path entirely. The
    # prewarm above already warmed the snapshot cache build_profile_watchlist
    # reuses. When OFF (the default) this block is a no-op and main() falls
    # through to the existing 5-bucket path byte-identical.
    if _profile_scan_enabled():
        summary = _run_profile_scan(universe_path)
        _check_budget("after_profile_scan")
        if summary is None:
            # Universe missing / W3 core unavailable — stayed silent, did
            # nothing. Exit 0 (silence-by-default); the OFF default still owns
            # play-fit.json so nothing was corrupted.
            return 0
        # Read the REAL build_profile_watchlist contract: the active count is
        # len(active) (there is no "n_active" key), and the destination is the
        # path _run_profile_scan requested + attached (not a hardcoded guess).
        n_active = len(summary.get("active") or [])
        out_path = summary.get("out_path", "~/.hermes/quant/watchlist/profile-fit.json")
        # Silence-by-default: only print on a non-empty watchlist.
        if n_active:
            print(
                f"🎯 **profile-fit scan** — {n_active} active "
                f"(asof={summary.get('asof')}) → {out_path}"
            )
        return 0

    # ADR-0075 catalyst onboarding (Seam A). DEFAULT-OFF: catalyst_admissions
    # returns [] unless BOTH HERMES_QUANT_CATALYST_ONBOARDING=1 AND
    # HERMES_QUANT_SEMANTIC_ENABLED=1 are set, so with the flags off the kwargs
    # below are all empty -> evolve_watchlist output is bit-for-bit identical to
    # today. When on, <=3 strong out-of-universe catalyst names that pass the
    # fail-closed tradeability gate are unioned into the scored universe,
    # fast-tracked (same-day onboard), and tagged admitted_via=catalyst.
    fast_track: set[str] = set()
    admission_extras: dict[str, dict] = {}
    extra_universe: list[str] = []
    try:
        from hermes_quant.catalyst.onboarding import catalyst_admissions, default_tradeable

        admissions = catalyst_admissions(
            set(universe_symbols), tradeable=default_tradeable
        )
        for a in admissions:
            extra_universe.append(a.symbol)
            fast_track.add(a.symbol)
            admission_extras[a.symbol] = {
                "admitted_via": a.admitted_via,
                "catalyst_horizon": a.horizon,
                "catalyst_asof": a.packet_asof,
                "catalyst_stance": a.stance,
            }
        if admissions:
            print(
                "catalyst-onboard: admitted "
                + ", ".join(f"{a.symbol}({a.stance})" for a in admissions),
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — never let onboarding crash the cron
        print(
            f"WARNING: catalyst onboarding skipped ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )

    summary = evolve_watchlist(
        scorer=_scorer,
        universe_path=universe_path,
        fast_track_symbols=fast_track or None,
        admission_extras=admission_extras or None,
        extra_universe_symbols=extra_universe or None,
    )
    _check_budget("after_evolve")

    # Silence-by-default: only print if something happened.
    if summary["events_written"] == 0 and all(
        v["n_active"] == 0 for v in summary["per_play"].values()
    ):
        return 0

    # Aggregate counters for the headline so the operator can scan in 1s.
    total_active = sum(v["n_active"] for v in summary["per_play"].values())
    total_onboarded = sum(v["n_onboarded_today"] for v in summary["per_play"].values())
    total_evicted = sum(v["n_evicted_today"] for v in summary["per_play"].values())

    # Headline tells the story: "watchlist evolve: N active, +X onboarded, -Y evicted"
    headline_emoji = "📋"
    if total_evicted > total_onboarded * 2 and total_evicted > 5:
        # Heavy eviction signals regime/quality shift — flag it.
        headline_emoji = "📉"
    elif total_onboarded > total_evicted * 2 and total_onboarded > 5:
        headline_emoji = "📈"
    print(
        f"{headline_emoji} **watchlist evolve** — {total_active} active "
        f"(+{total_onboarded} onboarded, -{total_evicted} evicted) — "
        f"as_of={summary['as_of']}"
    )
    print("```")
    for play, stats in summary["per_play"].items():
        if stats["n_active"] or stats["n_onboarded_today"] or stats["n_evicted_today"]:
            top = ", ".join(f"{s}:{sc:.2f}" for s, sc in stats["top5"])
            print(
                f"  {play:14s} active={stats['n_active']:3d} "
                f"+{stats['n_onboarded_today']:2d} -{stats['n_evicted_today']:2d}  "
                f"top5: {top}"
            )
    print("```")
    print(
        f"_events_written={summary['events_written']} → "
        f"~/.hermes/quant/watchlist/journal.jsonl_"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
