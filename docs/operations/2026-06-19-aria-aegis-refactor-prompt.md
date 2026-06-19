# Prompt for Aria — refactor & reintegrate AEGIS as the primary system (retire old hermes-quant coupling)

Paste the block below to Aria. This is the LARGER ask (distinct from the arm/run hand-off in
`2026-06-19-aria-handoff-prompt.md`): drive the ADR-0092/0093 rearchitecture to completion so
**AEGIS (the host-agnostic PDR core) is the system**, and `hermes-quant` becomes a thin host shell
over it — instead of today's state where the trading logic is fused into the hermes monolith.

This is a multi-phase epic. **Run it through `deep-work-loop-tiered` (ultracode), not as one edit.**

---

**Goal.** Today `hermes_quant/` is a 286-file / ~107K-LOC monolith. `hermes_quant/pdr_core/` (8
modules: gate, kelly, aggregate, contracts, portfolio_sizing/snapshot, gate_types) is the START of
the extracted host-agnostic AEGIS core (ADR-0092), but the rearchitecture is incomplete: ADR-0092
and ADR-0093 are still `proposed`, the core still leaks host imports, and the runtime is
home-coupled. Finish it so AEGIS is the engine and hermes is one shell (cowork is the other).

**Verified current state (start here — don't re-survey from scratch):**
- ADR-0092 (shared pdr-core, two shells) + ADR-0093 (AEGIS host-neutral name) are `proposed`.
  Read both; the decision is made (Option E, 2026-06-12) — your job is to EXECUTE + then move them
  to `accepted` when the acceptance gate is green.
- `pdr_core/` imports leak OUT of the core: 2 real leaks to break — `hermes_quant.risk` and
  `hermes_quant.protocol` (grep `from hermes_quant` in `hermes_quant/pdr_core/*.py`). A
  host-agnostic core must not import its host. Move the shared types DOWN into the core (or invert
  the dependency) so `pdr_core` imports nothing from `hermes_quant.*`.
- **46 hardcoded `Path.home() / ".hermes"` module constants** across the shell (e.g.
  `autonomous.py:62 HERMES_HOME`, `quant-autonomous-tick.py:62`). These are the home-coupling that
  makes the system un-embeddable and un-isolatable: I proved the tick ignores a `HERMES_HOME` env
  override because the constant is bound at import. Thread `home`/`QuantHome` as a parameter
  (or a single injected context object) through the core + the shell entry points, so AEGIS can run
  against any home (tests, a second shell, a standalone daemon) without process-global coupling.
- The options stack + watchlist rearch landed this session (see
  `2026-06-19-aria-handoff-prompt.md`): ageq2/ml00b/agperc3/agmon1/agmon2/ml01b/ag01b/bf76b/jw1 +
  W1-W6. These are the FIRST consumers that should sit cleanly on the AEGIS core — use them as the
  reference shape for what "a shell calling the core" looks like.

**Phases (deep-work-loop):**

1. **Investigate/architect.** Map the true core/shell boundary: which of the 286 modules are
   genuinely host-agnostic decision/risk/perception logic (→ AEGIS core) vs. hermes-specific I/O
   (cron scripts, Discord, the `~/.hermes` home, Alpaca wiring → shell). Produce the dependency
   graph + the target package layout. Decide: in-repo `pdr_core/` promotion vs. a separate `aegis`
   package (ADR-0092 ac03 names physical extraction + PyPI publish as a later, operator-gated step
   — do NOT publish; just make the boundary clean in-repo first).

2. **Break the core's host imports.** Remove the `hermes_quant.risk`/`hermes_quant.protocol` leaks
   from `pdr_core` (move shared contracts into the core; the host imports the core, never the
   reverse). Add a guard test that fails if `pdr_core` ever imports `hermes_quant.*` again.

3. **De-couple the home/context.** Replace the 46 `Path.home()` module constants with a threaded
   `home`/context. Acceptance: the autonomous tick + every cron script runs against an injected
   `HERMES_HOME` (prove with an isolated-tmp-home integration test — the thing that DIDN'T work when
   I smoke-tested this session). This single change unlocks real test isolation + the standalone
   daemon (ac02).

4. **Reintegrate the shell on the clean core.** Repoint `autonomous.py` + the analysts + the
   options stack to consume the AEGIS core through its public surface only. Keep every flag
   default-OFF + byte-identical (the rearchitecture must be a no-op behaviorally — prove with the
   existing parity tests; ADR-0091 Inc-0 already established the 4-fold parity grid pattern).

5. **Accept the ADRs + verify.** When the acceptance gate is green (full sweep + the no-leak guard +
   the home-isolation test + behavioral parity), move ADR-0092/0093 to `accepted` and update the
   README index.

**Hard rails (money software):**
- This is a REFACTOR — it must be **behaviorally byte-identical** with flags at their defaults.
  Every step ships behind the parity tests; a refactor that changes a decision is a bug.
- silence-by-default, fail-CLOSED, no-lookahead, deterministic gate is final authority — unchanged.
- NEVER edit `cowork-quant/` or `ADR-0094/0096` (cowork-owned). cowork is the OTHER shell; coordinate
  the core's public contract with it via ADR-0095 (single contract source) — the hermes-side mirror
  `contract_mirror.py` + its parity test already exist; keep them in sync, don't fork the contract.
- Don't flip source flag defaults to arm anything. Don't push/force-push shared branches or touch
  secrets/CI without operator approval.
- Use worktree-isolated lanes for file-disjoint work; cherry-pick (drop stale FLAG-INVENTORY, regen
  on-branch); `git archive HEAD` integrity-check after each merge.

**Branch:** start from `docs/rearchitecture-shared-pdr-core` (has the full options stack + W1-W6).
The two open PRs (#87 hermes-only, #88 cowork-split) are STALE — they predate this session's +26
commits and have no actionable review comments (Codex returned its no-suggestions boilerplate).
Refresh or supersede them as part of phase 5; that's an operator/push decision.

---
