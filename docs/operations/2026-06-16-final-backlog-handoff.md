# Final backlog handoff — the 12 remaining open items (2026-06-16)

**State:** `.seeds` = 195 total · 169 closed · 14 deferred-with-gate · **12 open-active**. The
agent-codeable axis is **exhausted** — a final concurrent review team (`w52jkujdv`) falsification-tested
all 12 against actual code + ran the full sweep (**3074 passed, 2 skipped, 0 failed**) and returned
**`nothing-agent-remains`**. Every open item's agent-side deliverable (code / tests / eval-axis /
wiring / runbook / recommendation doc) is committed and verified; each remaining gate is an
**operator / live-network / data-accumulation / governance** action the agent cannot perform, or is
out-of-edit-scope.

This doc is the single-pass handoff to clear the 12. It supersedes the operator-facing parts of
`2026-06-14-operator-action-packet.md` (which is now partially stale: it still lists 71ef/8b01/afa4 as
pending — those were RUN by the operator and reconciled CLOSED on 2026-06-16 against verified live
`jobs.json`).

> **Reconcile-after-run contract:** after the operator runs each block below, the agent reconciles the
> seed to `closed` **against verified live state** (`~/.hermes/cron/jobs.json` enabled=True / `.env`
> grep / `diff -q` byte-identical), never by fiat — exactly as 71ef/8b01/afa4 were closed this session.

---

## A. Alpaca MCP enable (closes 58e9, e18b, 0fc0)

Agent-side: DONE — staged config block + read-only allowlist + enable runbook in
`docs/operations/MCP-INTEGRATION.md` (§5, lines ~206-228); the account-toolset trade-off (0fc0) is
documented + operator-accepted (`ALPACA_PAPER_TRADE=true`, settings-only, RE-EVALUATE-if-live warning).

Operator: paste the staged config block into `config.yaml`, wire the cred-bridge, reload the gateway.
**Verify:** the alpaca MCP responds read-only; `ALPACA_PAPER_TRADE=true` is in effect. → agent closes
58e9/e18b/0fc0 against live state.

## B. Capability flag flips (closes ba90, 2f01; advances 6bb9)

Each instrument is BUILT + green; only the `.env` flip remains (verified absent from live `.env`).

- **ba90** — `echo 'HERMES_QUANT_CATALYST_ONBOARDING=1' >> ~/.hermes/.env` + the watchlist-evolve cron
  wrapper. The ADR-0075 admission-precision eval axis (`hermes_quant/catalyst/eval.py:371
  run_admission_precision`, vacuous-pass-safe) is built + 91 tests green; run it on real admission
  episodes to a green hit-rate before the flip.
- **2f01** — `echo 'HERMES_QUANT_IC_DEDUP_AT_INGEST=1' >> ~/.hermes/.env`. The `factor_returns`
  register wiring is complete (`alpha_zoo.py:294/334/352`, `starter_set.py:272`). Enablement-only
  (dedup is strictly tighter; no eval gate).
- **6bb9** — promote `PORTFOLIO_CAPS` + `PAPER_SLIPPAGE_MODEL=v0.2` to **code-default-ON** after ONE
  clean side-by-side paper day. NOTE: both are ALREADY live via the armed-wrapper cron exports
  (`~/.hermes/scripts/*-armed.sh`); 6bb9 is the *code-default* promotion (an operator process-gate:
  observe one clean day, then the agent flips the code default in a flag-gated PR).

## C. Data-gated (afa4, b67a — auto-progress with corpus volume + time)

Operator flip + cron are DONE (verified: `HERMES_QUANT_GRAPH_MINING=1` in `.env`, graph-mine cron
enabled, scripts byte-identical). The only residual is **data volume**:

- **afa4** — the propagation-log corpus must reach `MIN_SAMPLE=20`/edge before graph-mining proposes
  edges. No action — it accrues as the catalyst crons fire.
- **b67a** — raise `CONSUMER_TREND_CONFIDENCE_HAIRCUT` (currently 0.5, `synthesize.py:59`) toward 1.0
  only after B06 fires + ≥20 brand_self propagations measured at ≥0.60 hit-rate. Raising now amplifies
  an unproven edge. When the data clears, this is a 1-line agent change behind the measured-edge gate.

## D. Architecture / governance / maintenance (243d, d9d8, 5a63)

- **243d** — remove the `react.live` fallback in `promotion.py` ONLY once a LIVE (non-paper) reactor
  lands. Verified: no live reactor exists (`react/live.py:100 LiveBroker._submit_mleg_order_impl`
  raises `NotImplementedError`); `select_reactor` routes only to paper. Gated on a future live-reactor ADR.
- **d9d8** — re-commit the 2-week ADR freeze through end of June. Per `OPERATOR-DECISIONS-20260605.md:34`
  this is a **governance act / operator's signature**, not an agent artifact.
- **5a63** — periodic optional-MCP version-pin check. 5 of 9 manifests are pinned; 4 (fred/longbridge/
  sec-edgar/yahoo-finance) float `latest`/`main` **by design** (each says "operator: pin before
  enabling" — disabled recipes pinned at enable-time against live PyPI). Re-check pins against live
  upstream when enabling any of them.

## E. Out-of-edit-scope (cw1)

- **cw1** — `cowork-quant/scripts/quantcore/tests/test_mask.py` is a corrupted binary (3713 bytes, 36
  null bytes, `ast.parse` fails). Verified unrecoverable in-repo: cowork-quant has no `.git` and the
  file is untracked in hermes-quant git; editing `cowork-quant/` is forbidden by the agent's scope.
  **Operator:** recover `test_mask.py` from a local backup, or regenerate it; then `quantcore` is
  `212/212` (currently `207/207` excluding the corrupt file). The deterministic core itself is sound
  (`quantcore/mask.py` imports clean) — only the test file is corrupt.

---

## What "done" means here

The literal "zero open items" is **not reachable by the agent alone** — by the money-software posture
(ADR: agent proposes, operator executes live changes), every remaining item needs an operator/live/
data/governance action or is out-of-edit-scope. The reachable-and-achieved state is: **the review team
confirms nothing agent-doable remains**, every gate premise is re-verified against actual code + live
state, and the operator is already clearing the handoff (3 crons run + reconciled this session). As the
operator runs each block above, the agent reconciles the seed to `closed` against live state — the path
to literal zero runs through this handoff + time (the data gates).
