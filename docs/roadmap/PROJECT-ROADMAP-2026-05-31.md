# Hermes-Quant Roadmap — 2026-05-31 (consolidated, seed-tracked)

> Supersedes the planning portions of PROJECT-ROADMAP-2026-05-27.md. This is the authoritative
> forward roadmap after the 2026-05-31 backlog drive + the strategy/horizon/event-calendar/MCP
> architecture studies. **Seeds (`sd`) is the live tracker** (AGENTS.md policy); this doc is the
> sequenced human-readable view. Every row maps to a seed ID. Rails are immutable and override any
> seed: the deterministic risk gate (ADR-0004), the discrete sizing ladder {0,±0.05,±0.10,±0.15,±0.20},
> and the kill-switch are never mutated; every new capability ships default-OFF, eval-gated,
> byte-identical when off, asof-honest.

## DONE this drive (shipped + committed)
- **PDR-1..4 perception layer** (velocity/convergence/saturation) — ADR-0079; RR2 fixed the per-item
  cron defeat so convergence can fire on a batched multi-source feed.
- **B-backlog build set** — B13/14/18/21/25/30 (Wave-1), B32 validation suite, B22 AlphaVantage
  fallback, B20 EDGAR insider, B34 reporting-lag asof, B50 stacking, B46 deterministic compaction,
  B36 PIT universe, B27 rule-mining skeleton, **B01 multi-leg PRODUCER seam** (covered-call/CSP/wheel
  fire end-to-end on paper), B09 larger eval set.
- **Review-team fixes** — RR1/RR5/RR6/RR7/RR8/RR9 (money-path test coverage, integrity-reader logging,
  velocity-peak key, the convergence wiring).
- **B08 social producers live** (Reddit Atom .rss + Trends RSS, recency-gated) — HERMES_QUANT_SOCIAL_INGEST=1.
- **MCP optional registry** (9 servers, all disabled-by-default — see MCP-INTEGRATION.md).
- **Seeds internalized** (AGENTS.md) + **Hermes self-onboarding/meta-loop doc** (HERMES-SELF-ONBOARDING.md).
- Research notes: docs/research/2026-05-31-r-*.md (B20/B22/B23/B24/B27/B29/B32/B46, pdr234-seams,
  strategy-openness-and-horizon, financial-calendar-event-risk) + review-team-findings + backlog-audit-reconciled.

## ROADMAP — sequenced by gate

### Track A — Strategy-openness (ADR-0082, proposed)
The pipeline is fixed-play + multi-leg gated-not-deliberated. Make it strategy-open WITHOUT letting an
LLM pick legs (the deterministic table + options_gate decide).
- A1 `[0878, P1]` derive the play registry from PROFILES (kill the parallel score_all/PLAY_NAMES lists). READY.
- A2 `[3e56, P2]` registry-open play loader (YAML/entry-point, default-OFF). Blocked-by A1.
- A3 `[26dc, P2]` ResearchPlan.structure_intent additive enum (deliberation proposes a coarse structure). READY.
- A4 `[0afd, P2]` deterministic stance×IV-regime structure-selection table (intent→structure; table+gate decide). Blocked-by A3.

### Track B — Horizon (ADR-0083, proposed): DEFER intraday, build the unblockers
Interday-only by design; the edge is multi-day. Build the measurement instrument first; intraday only on a measured edge.
- B1 `[a643, P1]` Phase 0a: per-timeframe still-forming-bar discipline (close the intraday no-lookahead hole). READY.
- B2 `[3045, P1]` Phase 0b: settlement v0.1.2 — exit-fill joining + horizon-return math (the eval instrument). READY.
- B3 `[4d37, P3]` Phase 1: long-horizon intraday mode — **DEFERRED**, blocked-by B2 + a measured-edge gate. `gated:deferred`.

### Track C — Scheduled-event calendar (ADR-0084, proposed): add it, asof-honest, gate-immutable
Two-timestamp (announced_at/scheduled_for) events plug into the EXISTING check_view_lookahead; event_risk
is a read-only extras field the agents deliberate + a deterministic pre-event reject/abstain backstop.
- C1 `[a754, P1]` calendar data adapter (stdlib, catalyst/ingest.py clone). READY.
- C2 `[ea31, P1]` fomc_calendar.seed.yaml + load + annual-refresh + freshness assertion. Blocked-by C1.
- C3 `[e3de, P1]` event_risk PerceptionFrame extras field + calendar_market_extras seam (default-OFF). Blocked-by C1.
- C4 `[743b, P1]` pre-event REJECT/abstain guard + options earnings-proximity IV-crush check (additive, gate-immutable). Blocked-by C3.
- C5 `[5dc7, P2]` earnings + BLS.ics/FRED source wiring (best-effort, key-absent→silence). `gated:data`. Blocked-by C1.

### Track D — MCP servers (disabled-by-default; see MCP-INTEGRATION.md)
- D1 `[58e9, P2]` operator-enable alpaca MCP read-only (ALPACA_TOOLSETS allowlist + PAPER_TRADE=true; creds already on disk). `gated:operator`.
- D2 `[b9eb, P2]` surface enabled read-only MCP tools to the agents/deliberation (when an operator enables one). READY.
- D3 `[5a63, P3]` keep optional-mcps manifests version-pinned (periodic). `gated:maintenance`.

### Track E — Externally-gated activations (operator / data / market — NOT agent-completable)
These are built/ready; the gate is outside code. Surfaced, never dropped.
- `[ba90, B05]` catalyst onboarding: build admission-precision eval axis + operator flip. `gated:operator`.
- `[8b01, B06]` register profitability cron in Hermes cron.db. `gated:operator`.
- `[b67a, B07]` raise consumer-trend haircut. `gated:data` (needs B06 firing + ≥20 samples ≥0.60). Blocked-by B06.
- `[afa4, B10]` learned-graph mining: flip + register + corpus volume. `gated:operator`.
- `[71ef, B11]` calibrator-drift: deploy + register Monday cron. `gated:operator`.
- `[6bb9, B12]` promote PORTFOLIO_CAPS+SLIPPAGE default-ON after one clean side-by-side day. `gated:operator`.
- `[2f01, B38]` flip IC_DEDUP_AT_INGEST (enablement-only). `[d9d8, B15]` ADR-freeze (governance).
- `[4665, B41]` decide LLM-stages-production-default path. `[817b, B43]` full-universe load test (skip-tier v0.9+).
- `[79f5, B45]` Alpha Zoo population (deferred v0.2; RL = DO_NOT_BUILD). `[243d, B48]` remove react.live fallback (blocked on a LIVE reactor).

### Track F — Residual nits (low value, non-blocking)
- `[1ef6]` RR11-19 LOW review batch (logger.debug→info, inject-clock recency). `[1ba8]` B27 lift-guard test mutation-killing.

## DO_NOT_BUILD (researched + decided)
- B23 task-routing decision tree (cargo-cult for a fixed deterministic pipeline — Anthropic workflow-vs-agent).
- B24 5-section prompt + frozen memory (covered; the novel FinMem self-adaptive-risk piece is a NAMED rail violation, ADR-0080).
- B39 LangGraph ToolNode-for-all-analysts (HITL-surprise deferral). RL post-training (explicit operator do-not-build).
- B29 separate SQLite Goal Ledger → FOLDED into the JSONL HypothesisRegistry (ADR-0048 Alt C already rejected SQLite).

## The critical path (next loop)
1. **B2 settlement v0.1.2** `[3045]` — unblocks horizon measurement + is the eval instrument for everything.
2. **A1 play registry** `[0878]` + **A3 structure_intent** `[26dc]` — strategy-openness foundations (independent, parallel).
3. **C1 calendar adapter** `[a754]` → C2/C3 → **C4 pre-event guard** — the highest-leverage new risk signal.
4. Operator track (E) clears as the operator registers crons / loads creds / audits the side-by-side day.
