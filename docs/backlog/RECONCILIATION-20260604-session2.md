# hermes-quant backlog reconciliation — 2026-06-04 (session 2)

Authoritative per-item disposition after this session's autonomous backlog burn-down.
Main HEAD at time of writing: **`42b41bc`** (B41-e merged).

## Merged this session (PRs)

| PR | Seed(s) | What | Class |
|---|---|---|---|
| #51 | 29ca | tool-count destale 16→17 (quant_insider) | docs |
| #52 | RV4/5/6 | persisted-state defaults harden (posterior/watchlist/grounding) | fix |
| #53 | RV1/2/3 | **HIGH** fail-closed on non-finite admission state (gate breaker bypass) | fix |
| #54 | b12e (B41-d) | TraderNodeLLM deterministic numeric override at producing seam | feat |
| #55 | c6f4 (B41-c) | reflector faithfulness / no-leakage eval axis | feat |
| #56 | ed7c+615c (B41-a/f) | LLM cost-ceiling + zero-call kill-switch + pinned config | feat |
| #57 | 20b6 (B41-b) | **keystone** OOS LLM-beats-fallback gate | feat |
| #58 | — | seed reconcile + file pe01/02/03 | chore |
| #59 | 0956 (B41-e) | debate-dissent OOS gate + whole-debate budget | feat |

Plus wave-1 #47–#50 (safety/oos/learning/grounding) merged earlier in the prior context.

**B41 LLM-production-gate cluster: a/b/c/d/e/f ALL SHIPPED.** Only B41-g (8db9) remains — amends ADR-0062, respects the active ADR-freeze → **deferred**.

## In flight
- `fix/playbook-aggregate-cap` (proc_a66a9478afd8) — closes the CRITICAL playbook/hourly cap-bypass (see below). Default-OFF behind `HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP`.

## CRITICAL finding (live on main, fix in flight)
`ops/scripts/quant-playbook-tick.py::place_paper_market_order` POSTs market orders directly to Alpaca `/v2/orders` with only a PER-FIRE $1000 clamp (`kelly_to_notional`) — **no aggregate/portfolio cap**. The playbook + hourly firing layers both use this path, so 2 of 4 firing layers bypass the `PaperReactor` cap seam (ADR-0004 / ADR-0087). N fires × $1k can sum past gross-exposure with no check — same class as INCIDENT-2026-06-02. Matches PR #37's finding. Fix adds a tick-level aggregate-notional budget reusing `risk/portfolio_normalize.py` headroom, default-OFF, fail-closed.

## Prior-session stranded PR cluster (#33/34/35/37/41 — 2026-06-03)
One workstream (ledger-honesty + cap-centralization, ADR-0085/86/87, incident response). **Half-superseded**: the doc half (ADR-0085/86/87 + INCIDENT + cap-audit research) is already on main; the code+test payloads are NOT. Each branch 11–12 ahead / 16 behind. `paper.py` deltas overlap this session's #47 HARD_FILL_CEILING rework → needs semantic reconciliation, not blind rebase. Disposition pending operator steer (options A/B/C posted to Discord). Recommended: fix the live bypass as a fresh clean lane (in flight), cherry-pick #37's integration test, triage-close the rest after salvaging unique non-paper.py payloads.

## Open seeds: 24 — disposition

### Buildable-now (autonomously actionable)
*(none remaining in the B41/RV line — cluster complete; next actionable items are the cap-fix lane + test-debt below)*

### Pre-existing test-debt (filed this session, verified failing on clean base 44a51f2)
- **pe01** — `test_call_advisor_with_deliberative_attaches_committee_keys` (deliberative committee-keys assertion stale)
- **pe02** — `wave_d/test_bar_snapshot.py` jsonl-parity (schema_version 1↔2 divergence — provenance fields added, parity test never updated)
- **pe03** — `wave_d/test_autouse_dummy_keys.py` (env-key autouse fixture drift)
Fixable but need an intent decision (is v1↔v2 snapshot divergence the intended contract?). Candidate for a follow-up lane.

### Operator-gated (flag flip / config paste / cron register / token mint — ADR-0080, NOT auto-completable)
ba90 (B05 catalyst onboarding flip), 8b01 (B06 cron register), afa4 (B10 graph-mining flip), 71ef (B11 calibrator-drift deploy), 2f01 (B38 IC-dedup flip), 58e9/e18b (Alpaca MCP enable), 8188 (kill-switch token mint CLI), 9048 (go-live/deploy-sync)

### Data/dwell-gated (blocked on accumulated data or elapsed time)
b67a (B07 — needs B06 firing + ≥20 samples), 6bb9 (B12 — needs clean side-by-side window)

### Freeze / deferred (ADR-freeze active, v0.2/v0.9 deferrals, DO_NOT_BUILD)
d9d8 (B15 freeze re-commit — governance), 817b (B43 v0.9+), 79f5 (B45 v0.2 + DO_NOT_BUILD), 243d (B48 — needs LIVE reactor), 4d37 (ADR-0083 — gated on measured edge), 8db9 (B41-g — ADR-0062 amend, freeze-respecting), 5a63 (MCP version-pin maintenance), 0fc0 (MCP account-toolset leak), 335e (settlement drain — needs join_exit_fills wiring)

## Test reality (honest)
The full `tests/unit` suite is NOT all-green — 7 pre-existing failures (pe01/02/03 cluster) that fail identically on clean base `44a51f2`. This session's merges introduced **zero new failures**. Per-merge verification used targeted slices (eval/governance/agents/research_debate) which ARE green. The full-suite gap is the pe01-03 test-debt, now tracked.
