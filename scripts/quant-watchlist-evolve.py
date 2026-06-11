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


def main() -> int:
    # Full Alpaca liquid universe — ~500 symbols by default. The library
    # prewarm + 600s cron timeout absorbs the wall time; do NOT silently
    # narrow this without an explicit policy decision.
    universe_path = Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily.json"

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

    summary = evolve_watchlist(scorer=_scorer, universe_path=universe_path)
    _check_budget("after_evolve")

    # Silence-by-default: only print if something happened.
    if summary["events_written"] == 0 and all(
        v["n_active"] == 0 for v in summary["per_play"].values()
    ):
        return 0

    print(f"as_of={summary['as_of']} events={summary['events_written']}")
    for play, stats in summary["per_play"].items():
        if stats["n_active"] or stats["n_onboarded_today"] or stats["n_evicted_today"]:
            top = ", ".join(f"{s}:{sc:.2f}" for s, sc in stats["top5"])
            # Wave 5c: render the explicit bucket status. A play with active rows
            # shows its count; a play with none reads "disabled" rather than the
            # ambiguous active=0.
            status = stats.get("status", "active" if stats["n_active"] else "disabled")
            active_field = (
                f"active={stats['n_active']:3d}"
                if status == "active"
                else f"{'disabled':>10s}"
            )
            print(
                f"  {play:14s} {active_field} "
                f"+{stats['n_onboarded_today']:2d} -{stats['n_evicted_today']:2d}  "
                f"top5: {top}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
