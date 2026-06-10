#!/usr/bin/env python3
"""quant-deploy-audit.py — drift audit between repo ops/scripts/ and deployed ~/.hermes/scripts/.

THE problem this exists for (meta-review M01/M02, 2026-05-30): Hermes crons execute the
DEPLOYED copies under ``~/.hermes/scripts/``, NOT the repo ``ops/scripts/`` copies. The two
have drifted in BOTH directions — the deployed copies carry live wirings the repo lost
(Issue #23: deployed daily-interim was 904 lines vs a 247-line repo stub), AND the repo now
carries session fixes (direction-bias gate, catalyst wiring, onboarding) the deployed copies
lack. A blind ``cp ops/scripts/* ~/.hermes/scripts/`` would be DESTRUCTIVE — it would clobber
live functionality. So this tool does NOT deploy; it AUDITS and reports, so a human can
reconcile deliberately.

Money-software discipline: never overwrite a live trading script you didn't reconcile. This
tool is read-only — it computes a checksum manifest and a per-file drift verdict, and exits
non-zero when drift exists so CI can flag it. The actual deploy is a separate, reviewed step
(see docs/operations/CRON-REGISTRY.md "deploy-sync").

Usage:
    python ops/deploy/quant-deploy-audit.py [--deployed-dir ~/.hermes/scripts] [--json]
    python ops/deploy/quant-deploy-audit.py --write-manifest   # snapshot current repo hashes

Exit codes:
    0  repo and deployed agree on every tracked script (no drift)
    1  drift detected (repo-only changes, deployed-only changes, or missing files)
    2  usage / IO error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_SCRIPTS = Path(__file__).resolve().parents[2] / "ops" / "scripts"
DEFAULT_DEPLOYED = Path.home() / ".hermes" / "scripts"
MANIFEST = Path(__file__).resolve().parent / "deploy-manifest.json"

# Scripts that are intentionally REPO-ONLY tooling — they operate on the repo
# itself or are run in-place from the checkout, and are NOT meant to be deployed
# to ~/.hermes/scripts/. Excluding them keeps the audit honest: without this they
# show as perpetual REPO_ONLY_NEW "drift", training the operator to ignore the
# alert (the cry-wolf failure that would defeat the whole point of the watchdog).
#   - quant-adr-index.py / quant-flag-inventory.py: regenerate repo docs from
#     source; they read/write repo files, never run on the box.
#   - quant-deploy-drift-watch.py: the drift watchdog itself — runs FROM the repo
#     checkout (the audit resolves repo paths relative to itself), wired to cron
#     via quant-deploy-drift-watch-armed.sh, never deployed flat.
REPO_ONLY_TOOLING: frozenset[str] = frozenset(
    {
        "quant-adr-index.py",
        "quant-flag-inventory.py",
        "quant-deploy-drift-watch.py",
    }
)


def _sha(p: Path) -> str | None:
    """Return the sha256 of a file, or None if it does not exist."""
    if not p.is_file():
        return None
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def audit(deployed_dir: Path) -> dict:
    """Compare every ``quant-*.py`` script in repo ops/scripts/ against the deployed copy.

    Returns a dict with per-file verdicts. A file is one of:
      SAME            — byte-identical repo vs deployed
      REPO_ONLY_NEW   — exists in repo, not deployed (e.g. the 2 new crons not yet deployed)
      DEPLOYED_ONLY   — exists deployed, not in repo (live-only script never vendored)
      DRIFT           — both exist but differ (the dangerous case — reconcile, don't clobber)

    Scripts in REPO_ONLY_TOOLING are excluded from the REPO side only — they are
    repo-only by design (doc generators, the watchdog itself), so a repo copy is
    expected and would otherwise be perpetual false-positive REPO_ONLY_NEW noise.
    They are NOT excluded from the deployed side: these tools must NEVER be
    deployed, so an accidental deployed copy SHOULD surface (as DEPLOYED_ONLY) for
    cleanup rather than being silently ignored (Codex P2, 2026-06-10).
    """
    repo_files = {
        p.name: p
        for p in sorted(REPO_SCRIPTS.glob("quant-*.py"))
        if p.name not in REPO_ONLY_TOOLING
    }
    # Deployed side is NOT filtered: an unexpected deployed copy of a repo-only
    # tool is itself a drift signal (it should be cleaned up, not hidden).
    deployed_files = {p.name: p for p in sorted(deployed_dir.glob("quant-*.py"))}
    all_names = sorted(set(repo_files) | set(deployed_files))

    results: list[dict] = []
    for name in all_names:
        repo_p = repo_files.get(name)
        dep_p = deployed_files.get(name)
        repo_hash = _sha(repo_p) if repo_p else None
        dep_hash = _sha(dep_p) if dep_p else None
        if repo_hash and dep_hash:
            verdict = "SAME" if repo_hash == dep_hash else "DRIFT"
        elif repo_hash and not dep_hash:
            verdict = "REPO_ONLY_NEW"
        elif dep_hash and not repo_hash:
            verdict = "DEPLOYED_ONLY"
        else:
            verdict = "MISSING_BOTH"  # unreachable (name came from one of the dirs)
        results.append(
            {
                "script": name,
                "verdict": verdict,
                "repo_lines": len(repo_p.read_text().splitlines()) if repo_p else None,
                "deployed_lines": len(dep_p.read_text().splitlines()) if dep_p else None,
            }
        )

    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    drifted = [r["script"] for r in results if r["verdict"] in ("DRIFT", "DEPLOYED_ONLY", "REPO_ONLY_NEW")]
    return {"deployed_dir": str(deployed_dir), "counts": counts, "drifted": drifted, "files": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deployed-dir", default=str(DEFAULT_DEPLOYED))
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the human report")
    ap.add_argument("--write-manifest", action="store_true", help="snapshot current repo hashes to deploy-manifest.json")
    args = ap.parse_args(argv)

    if args.write_manifest:
        manifest = {p.name: _sha(p) for p in sorted(REPO_SCRIPTS.glob("quant-*.py"))}
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {len(manifest)} repo hashes to {MANIFEST}")
        return 0

    deployed_dir = Path(args.deployed_dir).expanduser()
    if not deployed_dir.is_dir():
        # Not an error in CI (no deployed tree there): report repo-only and exit 0.
        print(f"deployed dir not present ({deployed_dir}); repo-only audit, no drift to report.")
        return 0

    report = audit(deployed_dir)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"=== deploy drift audit: {REPO_SCRIPTS} <-> {deployed_dir} ===")
        for r in report["files"]:
            mark = {"SAME": "  ok ", "DRIFT": " !!! ", "REPO_ONLY_NEW": " new ", "DEPLOYED_ONLY": " dep "}.get(r["verdict"], " ??? ")
            print(f"{mark}{r['verdict']:<14} {r['script']:<34} repo={r['repo_lines']} deployed={r['deployed_lines']}")
        print(f"\ncounts: {report['counts']}")
        if report["drifted"]:
            print(
                "\nDRIFT PRESENT — do NOT blind-copy. The deployed copies may carry live wirings "
                "the repo lacks (and vice-versa). Reconcile per docs/operations/CRON-REGISTRY.md "
                "before deploying. DRIFT=both-differ, DEPLOYED_ONLY=live-only-never-vendored, "
                "REPO_ONLY_NEW=repo-new-not-yet-deployed."
            )
    return 1 if report["drifted"] else 0


if __name__ == "__main__":
    sys.exit(main())
