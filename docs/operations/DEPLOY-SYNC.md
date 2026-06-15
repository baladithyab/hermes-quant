# DEPLOY-SYNC — repo ↔ deployed cron-script reconciliation (the anti-drift mechanism)

**Status:** authoritative operational guard
**Date:** 2026-05-30
**Tool:** `ops/deploy/quant-deploy-audit.py` · **Tests:** `tests/cron/test_deploy_audit.py`
**Source:** concurrent meta-review M01/M02 — the load-bearing operational gap.

---

## The problem (why this exists)

Hermes crons execute the **deployed** copies under `~/.hermes/scripts/quant-*.py`, **NOT**
the repo `ops/scripts/` copies. The two have drifted in **both directions**:

- The repo carries this session's fixes the deployed copies lack: the **direction-bias gate**
  (`73d7ed4`), **catalyst PDR wiring** (`2e84f28`), **ADR-0075 onboarding seam** (`3776a35`).
  → These are **NOT live** until redeployed. The AXP-SHORT-via-CSP bug the loop "fixed" is
  still firing in production.
- The deployed copies carry live wirings the repo never had. The audit found **4 scripts that
  run live but were NEVER vendored into the repo at all**:
  `quant-halts-watchdog.py`, `quant-portfolio-daily.py`, `quant-proposals-ttl-watchdog.py`,
  `quant-strategy-retro-weekly.py`. And 9 scripts DIFFER (deployed copies are *larger* — e.g.
  deployed `quant-daily-interim.py` is 904 lines vs the repo's 889).

**Therefore a blind `cp ops/scripts/* ~/.hermes/scripts/` is DESTRUCTIVE** — it would clobber
live functionality. Deploy is a **reconciliation**, never a blind copy. This is the
money-software "look at the target before overwriting" rule applied to ops scripts.

## The audit tool

```bash
# Read-only drift report (exit 1 if any drift, 0 if clean):
python ops/deploy/quant-deploy-audit.py
# Machine-readable:
python ops/deploy/quant-deploy-audit.py --json
```

Verdicts per `quant-*.py`:

| Verdict | Meaning | Action |
|---|---|---|
| `SAME` | byte-identical repo ↔ deployed | none |
| `DRIFT` | both exist but differ | **reconcile** (merge, don't clobber) |
| `REPO_ONLY_NEW` | in repo, not deployed (e.g. the 2 new crons) | deploy after review |
| `DEPLOYED_ONLY` | live but never vendored to repo | **vendor deployed → repo first** |

Current state (2026-05-30): **3 SAME · 9 DRIFT · 7 REPO_ONLY_NEW · 4 DEPLOYED_ONLY**.

## Reconciliation procedure (the only safe deploy) {#reconciliation-procedure}

> **Anchor note (seed `9048`):** other docs reference this section as "DEPLOY-SYNC §49" — that
> "§49" is a **line-number artifact** (this heading sits at line 49), not a section number; this
> doc has no numbered sections (it uses `M0x` labels). The canonical reference is **DEPLOY-SYNC
> "Reconciliation procedure" step N** (the four numbered steps below). Treat any "§49 step N" you
> see elsewhere as "Reconciliation-procedure step N".

For each non-`SAME` script, in this order:

1. **`DEPLOYED_ONLY` first — vendor live → repo.** Copy the live script into `ops/scripts/`,
   review the diff, commit. Now the repo is the superset-of-record for that script.
   ```bash
   cp ~/.hermes/scripts/quant-halts-watchdog.py ops/scripts/   # then review + commit
   ```
2. **`DRIFT` — three-way merge, repo-fix INTO the richer deployed copy.** The deployed copy
   usually has live wirings; the repo has the session fix. Merge the repo's *specific change*
   (e.g. the direction-bias screen) into the deployed copy, NOT the whole file. Verify with a
   dry run before it goes live.
3. **`REPO_ONLY_NEW` — deploy after review.** Safe to copy repo → deployed (nothing to clobber).
   ```bash
   cp ops/scripts/quant-catalyst-profitability.py ~/.hermes/scripts/
   cp ops/scripts/quant-calibrator-drift.py ~/.hermes/scripts/
   ```
4. **Snapshot + re-audit.** After reconciling, `python ops/deploy/quant-deploy-audit.py` should
   report 0 drift for the reconciled files. Snapshot the agreed hashes:
   ```bash
   python ops/deploy/quant-deploy-audit.py --write-manifest   # -> ops/deploy/deploy-manifest.json
   ```

## Coupling: a redeploy alone is NOT enough for the direction-bias fix (M04)

The direction-bias gate (`B04`) is gated behind `HERMES_QUANT_DIRECTION_BIAS_GATE` (default 0).
The deployed armed wrapper `~/.hermes/scripts/quant-autonomous-tick-armed.sh` sets
`REFLECTION=1`, `PORTFOLIO_CAPS=1`, `PAPER_SLIPPAGE_MODEL=v0.2` but **NOT** the direction-bias
flag. So even after redeploying the script, the screen stays OFF. Enabling B04 is **two**
coupled steps: (a) redeploy `quant-autonomous-tick.py`, **and** (b) add
`export HERMES_QUANT_DIRECTION_BIAS_GATE=1` to the armed wrapper. See FEATURE-ENABLEMENT.md.

## Repo ≠ deploy posture (M09)

The "no flag hard-enabled" claim in the Codex synthesis was **repo-only**. The deployed armed
wrappers already hard-set `HERMES_QUANT_PORTFOLIO_CAPS=1` and `PAPER_SLIPPAGE_MODEL=v0.2` — so
B12 is **silently live in production**. Any posture audit must check BOTH the repo AND
`~/.hermes/scripts/*-armed.sh`, because they diverge.

## CI guard (recommended next step, not yet wired)

Add `python ops/deploy/quant-deploy-audit.py` to a CI job that runs where the deployed tree is
mounted (or against a recorded manifest), failing the build on unreviewed drift. In plain CI
(no `~/.hermes/`) the tool exits 0 (nothing to compare) — so it is safe to add unconditionally;
it only bites where a deployed tree exists.
