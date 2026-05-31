# R — Trading-Cron Registry & HERMES_QUANT_* Flag Inventory (2026-05-30)

> **Scope:** reconcile the canonical trading-cron set against what is actually
> deployed on the Hermes host, and enumerate every `HERMES_QUANT_*` feature flag
> with its enablement precondition. Pure local recon.
>
> **Sources cited inline:** deployed `~/.hermes/cron/jobs.json` (live registry,
> parsed 2026-05-30 17:00), `~/.hermes/scripts/quant-*` (the COPY crons actually
> run — drifts from repo `ops/scripts/`), ADR-0035 (cadence), ADR-0052
> (promotion), `~/.hermes/skills/mlops/hermes-quant-operations/SKILL.md` (deployed
> ops skill), and `docs/research/2026-05-30-understanding-wiki.md`.

---

## 0. How these crons are registered (mechanism — not cron(8))

The trading crons are **DB-backed jobs in `~/.hermes/cron/jobs.json`**, registered
through the Hermes `cronjob` agent/MCP tool (`action=create|list|delete`), NOT via
`crontab`/cron(8) and NOT via the Claude-session `CronCreate` tool. Each job stores
`{schedule:{kind:cron, expr, display}, command, no_agent, deliver, enabled}`. The
DB scheduler invokes the **deployed `~/.hermes/scripts/` copy**, which drifts from
the repo `ops/scripts/` — `md5sum` the two before editing (SKILL.md:568,588). All
schedule `expr` values are gateway-local **Pacific Time (PT)** with DST auto-applied
(ADR-0035:51).

`no_agent=True` ⇒ deterministic script, reproducible, silence-by-default (empty
stdout = no Discord message; SKILL.md:20). `no_agent=False` ⇒ LLM-driven brief
(prompt carries the env-var prefix for autonomy; SKILL.md:106,129).

---

## 1. Trading-cron registry (what the system expects vs what's deployed)

**18 quant crons registered** in `jobs.json` (all `enabled=True`). Discord channel
`1508194266306969611` = `#hermes-quant`. The 2 NEW this loop (rows 17–18) are
**built + scripts deployed but NOT yet in `jobs.json`** — registration is the
pending action.

| # | Cron name | Schedule (PT) | Deployed script | Deliver | no_agent | Armed | What it does (1 line) | Silence contract |
|---|---|---|---|---|---|---|---|---|
| 1 | quant-universe-scan-daily | `15 3 * * 1-5` | quant-universe-scan.py | discord:#hq | ✓ | n/a | Daily premarket liquidity-universe scan (ADR-0035:55) | quiet unless universe delta |
| 2 | quant-watchlist-evolve-daily | `30 3 * * 1-5` | quant-watchlist-evolve.py | discord:#hq | ✓ | n/a | Evolve play-fit watchlist post-scan (ADR-0035:56) | quiet unless watchlist change |
| 3 | quant-catalyst-coverage-daily | `45 3 * * 1-5` | quant-catalyst-coverage.py | origin | ✓ | n/a | Change-detecting coverage watchdog (PR #31) | empty unless coverage drops |
| 4 | quant-halts-watchdog-daily | `0 5 * * *` | quant-halts-watchdog.py | discord:#hq:thread | ✓ | n/a | Warn on stale operator halts (>7d) | silent if no stale halt |
| 5 | quant-daily-premarket-interim | `30 5 * * 1-5` | quant-daily-interim.py | discord:#hq | ✗ (LLM) | AUTONOMY=paper | Premarket advisor brief, auto-fires | brief always; "🤖 Auto-fired N/M" |
| 6 | quant-playbook-tick-daily | `0 6 * * 1-5` | quant-playbook-tick-armed.sh | local | ✓ | --armed | Daily 5-play decision tick (ADR-0035:57) | local; silent unless fire |
| 7 | quant-playbook-weekly | `30 6 * * 1` | quant-playbook-weekly.py | local | ✓ | (script:, not yet armed) | Mon rebalance: roll/expire near-DTE legs (ADR-0035:58) | local; silent unless action |
| 8 | quant-proposals-ttl-watchdog-daily | `30 6 * * 1-5` | quant-proposals-ttl-watchdog.py | discord:#hq | ✓ | n/a | Expire stale pending proposals | silent if none stale |
| 9 | quant-autonomous-tick-30min | `30 6-13 * * 1-5` | quant-autonomous-tick-armed.sh | discord:#hq | ✓ | AUTONOMOUS+ARMED | 30-min in-market PDR loop, flock-guarded | per-fire delta only |
| 10 | quant-catalyst-ingest-30min | `0,30 6-13 * * 1-5` | quant-catalyst-ingest.py | local | ✓ | n/a | Ingest GN-RSS catalyst packets every 30min | local; quiet |
| 11 | quant-daily-midday-interim | `0 8 * * 1-5` | quant-daily-interim.py | discord:#hq | ✗ (LLM) | AUTONOMY=paper | Midday advisor brief, auto-fires | brief always |
| 12 | quant-hourly-market-tick | `0 7-13 * * 1-5` | quant-hourly-tick-armed.sh | discord:#hq | ✓ | AUTONOMOUS+ARMED | Hourly read-only monitor + phase-7 propose/fire (ADR-0035:61,164) | silent unless fire/halt/err |
| 13 | quant-daily-eod-interim | `30 12 * * 1-5` | quant-daily-interim.py | discord:#hq | ✗ (LLM) | AUTONOMY=paper | EOD advisor brief, auto-fires | brief always |
| 14 | quant-portfolio-daily-eod | `5 13 * * 1-5` | quant-portfolio-daily.py | discord:#hq | ✓ | n/a | EOD portfolio snapshot (compact + MEDIA: md) | silent if book empty |
| 15 | quant-strategy-retro-weekly | `0 13 * * 0` | quant-strategy-retro-weekly.py | discord:#hq | ✓ | n/a | Sun weekly trailing-7d retro (SKILL.md:143) | silent if 0 fills + 0 open |
| 16 | quant-playbook-quarterly | `30 6 1-7 1,4,7,10 1` | quant-playbook-quarterly.py | discord:#hq | ✓ | n/a | 1st-Mon-of-quarter factor/rebalance review (ADR-0035:59) | quiet unless flag |
| **17** | **quant-catalyst-profitability (NEW, weekly)** | *expected* `0 7 * * 1` | quant-catalyst-profitability.py | local | ✓ | n/a | Weekly: join propagation-log vs fwd returns; verdict per relation class | silent until MIN_SAMPLE=20 crossed or verdict flips |
| **18** | **quant-calibrator-drift (NEW, weekly)** | `0 7 * * 1` (script docstring:9) | quant-calibrator-drift.py | local | ✓ | n/a | Weekly raw→calibrated drift check; alert if drift>5% (ADR-0009 §P0-2) | exits 0 silent unless `should_alert` |

ADR-0052 promotion crons (`promotion-cron.py`) are **operator/CI-run, not in the
host `jobs.json`** — out of the deployed trading-cron count.

**Total deployed quant crons: 16. With the 2 NEW (built, scripts present, not yet
registered): 18.**

---

## 2. `HERMES_QUANT_*` flag inventory (all 45)

`grep -rhoE 'HERMES_QUANT_[A-Z_]+' hermes_quant ops | sort -u` → **45 flags**.
Currently SET in deployed `~/.hermes/.env`: **only `HERMES_QUANT_SEMANTIC_ENABLED=1`**.

### 2.1 Risk-changing ENABLE flags (default OFF; gating matters)

| Flag | Gates | Default | Precondition to flip |
|---|---|---|---|
| `ADMISSIBILITY` | ADR-0077 pre-trade shortability/borrow gate (else NullOracle) | 0 (bit-identical OFF) | **GATED** — task#11 hardening + a fidelity eval; it can only REJECT, but flipping mid-book with the 38 synthetic shorts restates P&L |
| `BORROW_COST` | Daily borrow-carry accrual on shorts | 0 | **GATED** — pairs with ADMISSIBILITY; needs borrow-aware P&L restatement first |
| `OPTIONS_GATE` | ADR options risk gate (raises `OptionsGateDisabled` when off) | 0 | **GATED** — #11 + multi-leg reactor (ADR-0029) not built |
| `OPTIONS_LIVE_CHAIN` | Live options-chain fetch (AND credentials) | 0 | **GATED** — needs options data layer + eval |
| `MULTILEG_REACTOR` | Multi-leg paper execution (`set NOWHERE`) | 0 | **GATED** — ADR-0029 reactor doesn't exist; 6/6 say fidelity-first |
| `AUTONOMY` (=paper) | Advisor-layer auto-fire in briefs | unset | money-path; flip per-cron-prompt after side-by-side (live SET) |
| `AUTONOMOUS` / `AUTONOMOUS_ARMED` | Playbook/hourly phase-7 propose / actually-fire | unset | both required to fire; armed-wrapper toggle (live via wrappers) |
| `PORTFOLIO_CAPS` | Greedy gross/net/cash headroom clip on fires | 0 | money-path; flipping at 880% gross silences all fires |
| `PAPER_SLIPPAGE_MODEL` (=v0.2) | Realistic slipped fill prices | v0.1 | changes recorded fills → reflector/calibrator baked in old behavior |
| `SEMANTIC_ENABLED` | HermesSemanticAnalyst as BMA peer | 0 (**SET=1 in .env**) | gated on `catalyst.eval` PASS (negative-control + precision); peer-only, can't fire alone |
| `CATALYST_ONBOARDING` | Catalyst-driven universe onboarding | 0 | AND-gated on `SEMANTIC_ENABLED=1`; ADR-0075 not built |
| `FUNDAMENTALS_ENABLED` | FundamentalsAnalyst in loadout | 0 | abstain-capable peer; needs eval before live weight |

### 2.2 Abstain-only / observe-only ENABLE flags (default OFF)

| Flag | Gates | Default | Precondition |
|---|---|---|---|
| `DIRECTION_BIAS_GATE` | Neutralize advisor reco whose direction can't route any eligible play (the AXP-SHORT-via-CSP fix) | 0 | **SAFE-NOW** — can only abstain MORE, never fires/widens/flips (script:319). Flip after one dry-run tick |
| `IC_DEDUP_AT_INGEST` | Reject redundant alpha factors at registration (B38) | 0 | **SAFE-NOW** — registry-only, never touches money path; raises before append |
| `CALIBRATOR_AUTO_REFIT` | Auto-refit isotonic calibrator on drift (else alert-only) | 0 | **SAFE-NOW** — drift cron alerts regardless; auto-refit after a clean dry-run + drift-log review |
| `REGIME_HMM` | HMM regime classifier (else heuristic) | 0 | abstain/label-only; R6 HMM label brittleness — dry-run first |
| `ANALYSTS_USE_REGIME` | Regime-aware confidence haircut on analyst votes | unset | can only haircut (down) → safe after side-by-side |
| `OPEN_GUard` *(=0 kill-flag)* | Advisor intraday dedup (ADR-0072) — **default ON** | ON | strictly safer; `=0` is debug-only, never flip in prod |
| `RESEARCH_DEBATE` / `_ROUNDS` | LLM bull/bear research stage | 0 | evidence-only (can't override gate); cost not correctness |
| `RISK_COMMITTEE_LLM` / `RISK_ROUNDS` | LLM 3-way risk committee | 0 | committee silences-only (0.0-mult), never amplifies → safe |
| `REFLECTION` / `REFLECTOR_LLM` | Post-trade reflection / LLM reflector | 0 | memory-only, no order path (live SET=1 in wrappers) |
| `TRADER_LLM` | LLM-structured trader proposal | 0 | proposal feeds deterministic gate → evidence-only |
| `MEMORY_INJECT` | Inject BM25 memory into committee | 0 | context-only |
| `DELIBERATIVE` / `_PROMOTE` / `_RISK` / `_DEEP_MODEL` / `_QUICK_MODEL` | Playbook deliberative committee turns | 0 / unset | evidence-only committee path |
| `WATERMARK_ENABLED` | Daemon idempotency watermark store | 0 (daemon not used) | dormant; safe |

### 2.3 Config/path flags (not gates — values, not on/off)

`ALPHA_ZOO_DIR`, `EVIDENCE_DIR`, `JOURNAL_PATH`, `HORIZONS`, `KNOWLEDGE_CUTOFF`
(default 2025-01-01), `LOG_LEVEL` (default WARNING), `PAPER_INITIAL_CASH` (default
100000), `PREWARM_WORKERS`, `SNAPSHOT_V`, `IC_DEDUP_THRESHOLD` (float 0–1),
`PLAYBOOK_DRY_RUN`, `PLAYBOOK_TICK_MOCK` (test/debug stubs). No money-path
correctness dependency; set as needed.

---

## 3. SAFE-NOW vs GATED split (the actionable answer)

**SAFE-NOW** (abstain-only / no money-path correctness dependency — flip after a
single dry-run): `DIRECTION_BIAS_GATE`, `IC_DEDUP_AT_INGEST`,
`CALIBRATOR_AUTO_REFIT`, `REGIME_HMM`, `ANALYSTS_USE_REGIME`, `RESEARCH_DEBATE`,
`RISK_COMMITTEE_LLM`, `REFLECTION`/`REFLECTOR_LLM`, `TRADER_LLM`, `MEMORY_INJECT`,
`DELIBERATIVE*`, `FUNDAMENTALS_ENABLED` (peer, abstain-capable, eval-then-flip).

**GATED** (need task#11 pre-go-live hardening + a real eval before flip):
`ADMISSIBILITY`, `BORROW_COST`, `OPTIONS_GATE`, `OPTIONS_LIVE_CHAIN`,
`MULTILEG_REACTOR`, `PORTFOLIO_CAPS`, `PAPER_SLIPPAGE_MODEL`, `CATALYST_ONBOARDING`
(AND-gated on SEMANTIC + ADR-0075). `SEMANTIC_ENABLED` is gated-but-already-flipped
(eval passed knife-edge; live `.env:437`).
