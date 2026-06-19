# Operator Action Packet — 2026-06-13

**Why this exists:** the backlog has two classes an autonomous agent cannot resolve itself — (A) **verified-live safety risks** that need an operator lever now, and (B) **operator-gated enablement** items (live `.env` flips, Hermes `cron.db` registrations) that are operator actions by definition. This packet gives the exact commands + the evidence, so "addressed" is honest: the agent surfaced + verified + scoped; the operator decides + runs.

Branch with all referenced fixes: `docs/rearchitecture-shared-pdr-core` (not pushed).

---

## A. URGENT — verified-live safety (operator's call, recommended now)

### A1. Make the det-equity firing path observe-only until Increment 0 lands

**Risk (VERIFIED in code, seed cr01 / ra02):** `react/paper.py:448` `_portfolio_cap_clip` reads `position.quantity` as a NAV-fraction, but the deterministic-equity fold (`state/portfolio_state.py:984`) stores **true shares** (~33.33) via `reactor_metadata.quantity`. When a det-equity position is held:
- the de-risking guard `abs(fill_size_pct) <= abs(existing)` becomes `abs(0.05) <= abs(33.33)` → always true → **portfolio cap entirely bypassed** when adding to a held position; and
- opening any new name is **falsely silenced** because held shares inflate gross exposure to thousands of percent.

Both `HERMES_QUANT_PORTFOLIO_CAPS` and `HERMES_QUANT_DETERMINISTIC_EQUITY` are flipped-on (armed) per FLAGS.md. This is the same incident class as the 2026-06-02 41.6×-gross runaway. Externally cross-validated by the 2026-06-13 architecture research (nautilus synchronous-gate vs vn.py async-subscriber = this exact bypass mechanism).

**Why not hot-patch it:** the fix needs a read-time mark-injection seam to convert shares→NAV-fraction (`position_pct = qty × mark / equity`), but the money-path write loop must stay network-free, and the ledger has no per-row unit discriminator. That is Increment 0 (the shared fold + mark seam), not a surgical patch — hot-patching with a heuristic on live money math is exactly what the posture forbids.

**Recommended operator mitigation (reversible, one line):** route the autonomous/armed firing path away from the deterministic-equity reactor until Increment 0 ships, OR pause the det-equity arming. Concretely, the agent will produce the exact `.env`/wrapper edit on request — the safe options are (a) set the autonomous path's reactor selection so it cannot reach the det-equity backend while a non-flat equity book exists, or (b) make `quant-playbook-tick` / `quant-autonomous-tick` observe-only for one cycle. **Operator decides which; the agent does not flip live `.env`.**

### A2. Options gate NaN guard — FIXED in code (no operator action)

Seed cr02 (NaN/inf spot/nav/greeks fail-open in `options_gate`) is **fixed** on the branch (commit `6e17488`, 90/90 tests, ruff clean). The options gate is itself default-OFF (`HERMES_QUANT_OPTIONS_GATE`), so there is no live exposure; the fix lands whenever options work resumes. No operator action.

---

## B. Operator-gated enablement (produce-command-then-operator-runs)

These are `.env` flips + Hermes `cron.db` registrations. The agent cannot run them (live box). For each: the command + the eval evidence that must be green first.

| Seed | Action | Command (operator runs) | Gate before flipping |
|---|---|---|---|
| `ba90` (B05) | Enable catalyst onboarding | `echo 'HERMES_QUANT_CATALYST_ONBOARDING=1' >> ~/.hermes/.env` | catalyst onboarding eval green + graph coverage ≥ threshold |
| `8b01` (B06) | Register profitability cron | register `quant-catalyst-profitability-daily` in Hermes `cron.db` | script deployed to `~/.hermes/scripts/` + dry-run clean |
| `afa4` (B10) | Enable graph mining + cron | `echo 'HERMES_QUANT_GRAPH_MINING=1' >> ~/.hermes/.env` + register `quant-catalyst-graph-mine` | corpus seeded; cron-cadence only (low risk) |
| `71ef` (B11) | Deploy + register calibrator-drift | deploy script + register Monday cron | code/tests/silence green (already built) |
| `6bb9` (B12) | Promote PORTFOLIO_CAPS+SLIPPAGE default-ON | flip code defaults after one clean side-by-side | **BLOCKED by A1** — do NOT promote caps default-ON until the cr01 unit bug is fixed (Increment 0), or the cap math is wrong whenever a det-equity position is held |
| `58e9`/`e18b` | Enable Alpaca MCP read-only | paste staged config block + cred-bridge + reload | read-only + account toolset only; see `0fc0` (account toolset leaks `update_account_config` — fix manifest first) |
| `9048` | GO-LIVE + deploy-sync + cron-registry destale | reconcile `~/.hermes/scripts` ↔ repo `ops/scripts` | deploy-drift-watch clean for one cycle |

**Note on `6bb9`:** the critique surfaced that promoting `PORTFOLIO_CAPS` to default-ON is now *coupled* to the `cr01` unit fix — promoting it while the cap reads shares-as-fraction would arm the broken math more widely. Sequenced behind Increment 0. (This is a dependency the original seed didn't know about.)

---

## C. What the agent is doing in parallel (no operator action)

Code/docs items being worked on the branch behind default-OFF flags or as pure docs fixes: the `ra*` rearchitecture epic (ADR-0092, confirmed by research), Increment 0 scope (which subsumes cr00/cr01/ra01), the docs-reality cluster (cr07/cr08/cr13–cr17/ra09/ra11), and the cr02 fix already landed. Deferred-with-justification (eval-gated prior decisions): `817b` (load test v0.9+), `79f5` (Alpha Zoo/RL DO_NOT_BUILD), `4d37` (intraday, gated on measured edge), `d9d8` (ADR freeze — governance).
