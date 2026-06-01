# Backlog-Drive — PHASE 1 state capture (2026-05-31)

**HEAD:** 0f7de01. Tree clean. **This session: 24 commits** since b63f8c9.

## What this session already shipped (do NOT re-research/re-build)
- **PDR-2/3/4 perception layer** (28a0a3e/633b0ee/106c4ce + plans f22007a) — TrendVelocity,
  ConvergenceValidator, SaturationScore. All default-OFF, eval-gated, reviewed.
- **B08 social producers LIVE** — Trends `trending/rss` (cac3af0), Reddit Atom `.rss` no-OAuth
  (42f6cb4), wired behind `HERMES_QUANT_SOCIAL_INGEST` (1933064), deploy-drift reconciled
  (4a29cc3), recency filter + new.rss (13ca85a). `SOCIAL_INGEST=1` FLIPPED — live multi-source feed.
- **#36 broker BP** (72e3d8b) — `oracle.live_buying_power()` wired into admissibility (H-adm #1 closed).
- **#37 freqtrade order_filled NameError** (099f7c8) — real latent bug fixed.
- **#38 MultiLegProposal constructor-lock** (f3b6398) — risk_gate_pass=True unforgeable.
- **#31 JSONL isinstance-guard sweep** (bf78093 + bcecff7) — 21 readers + regression test.
- **calibrator atomic write** (fb4ea68); **meta_retro determinism + advisor lint** (88fa8c6).
- Review-follow-up tasks #19/#22/#23/#31 all CLOSED (verified done against HEAD by a completeness critic).

## The drive
The consolidated backlog (`docs/research/2026-05-30-backlog-consolidated.md`) lists 51 B-items +
N-items, mined BEFORE this session. PHASE 2 audits each against current HEAD to find the TRUE open
set; PHASE 3 researches the genuinely-unbuilt capability items (tavily/exa/deepwiki); PHASE 4-7
architect→plan→execute→review in waves with a concurrent review team; PHASE 8 final verification.

Standing safety frame (overrides "just do it"): self-evolution / new capabilities may PROPOSE; the
deterministic risk gate, discrete sizing ladder {0,±0.05,±0.10,±0.15,±0.20}, and kill-switch are
immutable by the loop. Every new capability ships default-OFF, eval-gated, byte-identical when OFF.
No flag flips that DEGRADE the running system or fire on a non-event.
