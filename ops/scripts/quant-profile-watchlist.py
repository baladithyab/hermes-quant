#!/usr/bin/env python3
"""Standalone profile-fit watchlist builder (CLI shim) — W3 / aegis seam.

Builds ONE asof-pinned profile-fit watchlist from a universe artifact and the
unified TickerProfile fitness, with ZERO hermes-cron / state.db / broker
dependencies. This is the genuinely-standalone path the aegis package wants: it
imports ``hermes_quant.playbook.profile_scan.build_profile_watchlist`` and runs
it against a universe artifact alone.

Unlike the per-play ``quant-watchlist-evolve.py`` cron tick, this tool:

  * emits a SINGLE ranked list (not the ``{play: [...]}`` 5-bucket fan-out),
  * NEVER pre-picks a strategy (the decision layer owns that),
  * OWNS its asof (defaults to the universe artifact's own asof) so it is
    replay-safe / no-lookahead,
  * has a ``--no-fetch`` mode that builds profiles from the artifact's own
    fields ALONE (zero network), and
  * writes to a NEW path (``profile-fit.json``) so it never clobbers
    ``play-fit.json``.

The library entry is always runnable by hand regardless of
``HERMES_QUANT_PROFILE_SCAN`` (running a tool by hand is the operator's choice);
the FLAG only gates the automatic cron/autonomous integration of this output.

Usage:
    quant-profile-watchlist --universe PATH [--asof ISO] [--out PATH]
                            [--max N] [--no-fetch] [--json]

Posture
-------
Silence-by-default: a missing/empty universe artifact -> empty result, exit 0,
no file clobber. Stdout is quiet unless ``--json`` (machine output) or the
ranked top-N (human breadcrumb) is requested.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Re-exec under the hermes-agent venv if available (where hermes_quant is
# installed). Best-effort: when the venv is absent (e.g. an aegis host with
# hermes_quant on its own path) we just run under the current interpreter.
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quant-profile-watchlist",
        description=(
            "Build ONE asof-pinned profile-fit watchlist from a universe "
            "artifact (standalone; no cron/state.db/broker)."
        ),
    )
    p.add_argument(
        "--universe",
        required=True,
        help="Path to the asof-pinned universe artifact JSON "
        "({asof, symbols:[{symbol, avg_dollar_volume_30d, last_close, "
        "tradable, shortable}]}).",
    )
    p.add_argument(
        "--asof",
        default=None,
        help="As-of ISO timestamp/date override. Defaults to the universe "
        "artifact's own asof (no-lookahead honesty).",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output path for the single profile-fit watchlist JSON. "
        "Defaults to ~/.hermes/quant/watchlist/profile-fit.json. NEVER "
        "play-fit.json.",
    )
    p.add_argument(
        "--max",
        type=int,
        default=None,
        help="Global cap on the emitted active list (default 50).",
    )
    p.add_argument(
        "--listing-table",
        default=None,
        help="Optional point-in-time listing-table JSON ({SYMBOL: "
        "{listed_at, delisted_at}}) for survivorship-safe filtering.",
    )
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Build profiles from the universe artifact fields ALONE (zero "
        "network). market_cap/realized_vol/spread abstain; the genuinely-"
        "standalone aegis path.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the ranked result as JSON to stdout.",
    )
    return p


def _load_listing_table(path: str | None):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"WARNING: could not read listing table {path!r}: {exc}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Import here (after the venv re-exec) so the re-exec path is cheap.
    from hermes_quant.playbook.profile_scan import build_profile_watchlist

    listing_table = _load_listing_table(args.listing_table)

    kwargs: dict = {
        "asof": args.asof,
        "fetch": not args.no_fetch,
        "listing_table": listing_table,
    }
    if args.out is not None:
        kwargs["out_path"] = args.out
    if args.max is not None:
        kwargs["max_watchlist"] = args.max

    result = build_profile_watchlist(args.universe, **kwargs)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    # Human breadcrumb: silence-by-default unless there is something to show.
    active = result.get("active") or []
    if not active:
        # Silence-by-default — nothing to report (missing/empty universe or no
        # eligible names). Exit 0 (the tool ran fine; the universe was empty).
        return 0

    print(
        f"profile-fit watchlist — {len(active)} active "
        f"(scanned={result.get('n_scanned')}, eligible={result.get('n_eligible')}, "
        f"cap={result.get('max_watchlist')}) — asof={result.get('asof')}"
    )
    print("```")
    for row in active[:20]:
        print(
            f"  {row['symbol']:8s} fit={row['fit_score']:.3f} "
            f"shortable={row['shortable']!s:5s} horizons={','.join(row['horizon_set'])}"
        )
    if len(active) > 20:
        print(f"  ... +{len(active) - 20} more")
    print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
