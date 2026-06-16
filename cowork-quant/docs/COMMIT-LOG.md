# Commit state — 2026-06-10

Git operations through the Cowork mount corrupt metadata (E: drive), so
commits are OPERATOR-GATED: run these on your machine after BOOTSTRAP.md.
This file is the prepared commit sequence; delete entries as you commit.

## Repo state at time of writing

Everything in cowork-quant/ is uncommitted (repo not yet initialized).
quantcore: 35 tests green (Python 3.10, `cd scripts/quantcore && pip install
-e .[dev] && pytest tests -q`). Parent repo (hermes-quant) has one new
uncommitted file: `docs/plans/2026-06-09-cowork-quant-submodule-plan.md`.

## Prepared commit sequence (cowork-quant, after git init)

1. `feat(core): quantcore deterministic package — gate rules 0-7, exact Kelly,
   hash-chained ledger, settlement+calibration (port of hermes-quant v0.6.4;
   35 tests incl. hypothesis property tests)`
   — scripts/quantcore/**
2. `feat(plugin): manifest, skills (quant-core, analysts), commands
   (scan/propose/brief/settle/status/doctor), debate agents, keyless .mcp.json`
   — .claude-plugin/, commands/{scan,propose,brief,settle,status,doctor}.md,
   skills/, agents/, .mcp.json, README.md, AGENTS.md, .gitignore
3. `feat(autonomy): scheduled-action layer — /watch unattended turn, /retro,
   /schedule cadence (ADR-0016/0024/0026/0035 ports) + PARITY.md`
   — commands/{watch,retro,schedule}.md, docs/PARITY.md, CHANGELOG.md
4. `docs(research): inspiration-corpus notes (foundation models, framework
   deltas, SOTA scan) — 2026-06-09 agent fan-out`
   — docs/research/*
5. Wave commits (see BACKLOG.md statuses; one commit per wave):
   `feat(core): wave-1 — event calendar, portfolio caps, regime, hypotheses`
   `feat(core): wave-2 — BMA aggregation, eval harness (CPCV+DSR), dashboard`

## 2026-06-12 — v0.2 re-survey, refinement, and build waves

Design docs (commit first):
6. `docs(research): full 5-stream re-survey (2026-06-12) — frameworks/committee,
   eval/honesty, risk/governance, foundation models, Cowork platform`
   — docs/research/2026-06-12-r-resurvey-and-refinement.md
7. `docs(arch): v0.2 architecture refinement + full specs (gated items →
   build targets); PARITY + BACKLOG updated (Waves 4-7)`
   — docs/2026-06-12-v0.2-architecture-refinement.md, docs/PARITY.md, BACKLOG.md

Code waves (one commit per wave; see BACKLOG Waves 4-7 + CHANGELOG):
8. `feat(core): wave-4 honesty+integrity — ledger verifier + cross-module check
   (B-30), leakage-masked eval mode + de-anon probe (B-31), gate-config manifest
   + determinism-replay (B-32)`
   — quantcore/{verify_ledger,mask,manifest,replay}.py + tests
9. `feat(plugin): wave-4 platform rails — PreToolUse deny-hook (no order tool
   fires), /watch disallowed-tools: AskUserQuestion, SessionStart halt inject (B-33)`
   — hooks/hooks.json, commands/watch.md
10. `feat(core): wave-5 decision quality — committee v2 weighting + analyst_weights
    (B-34), pre-gate screener (B-35), alpha-after-attribution (B-36), calibration
    upgrade Brier-skill+ECE-CI (B-37)`
    — quantcore/{analyst_weights,screener,attribution}.py, aggregate.py/settle.py edits + tests
11. `feat(core): wave-6 robustness+ports — adversarial drills (B-38), evidence
    store (B-39), admissibility (B-40), universe evolution (B-41), live dashboard (B-42)`
12. `feat(core): wave-7 capabilities (flag-gated) — options gate+structure+greeks
    (B-20), FoundationModelAnalyst client (B-21), shadow ledger (B-22), quarterly
    meta-retro (B-26)`

NOTE: code waves 8-12 are being implemented 2026-06-12; statuses tracked live in
BACKLOG.md. Tests run in the Linux sandbox (mount is read-stale on fresh rewrites;
new files sync fine). Commit only after the operator runs BOOTSTRAP.md (git init).

## Parent repo (hermes-quant) commits

1. `docs(plans): cowork-quant submodule plan (2026-06-09)`
2. After cowork-quant push: `git submodule add ... cowork-quant` per BOOTSTRAP.md.
