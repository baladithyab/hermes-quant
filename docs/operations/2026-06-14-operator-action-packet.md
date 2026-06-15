# Operator Action Packet — 2026-06-14 (supersedes 2026-06-13)

> **⚠️ SUPERSEDED-IN-PART (2026-06-16):** 71ef/8b01/afa4 below were RUN by the operator and reconciled CLOSED on 2026-06-16 (verified live `jobs.json` enabled=True + scripts byte-identical). For the CURRENT remaining 12 open items use **`docs/operations/2026-06-16-final-backlog-handoff.md`** (this packet is retained as history).


**Why this exists:** after this session's 38+ code merges, the only backlog items an autonomous
agent **cannot** resolve itself are **operator-gated enablement** items — live `~/.hermes/.env`
flips and Hermes `cron.db` (`~/.hermes/cron/jobs.json`) registrations — which are operator actions
*by definition* (the agent has no `cronjob` tool and never flips live `.env`/cron). This packet is
the single current source of truth: for each open operator-gated seed, the **exact command** + the
**gate that must be green first**. "Addressed" is honest here = the agent surfaced + verified +
scoped + produced the command; the operator decides + runs.

Branch with all referenced fixes: `docs/rearchitecture-shared-pdr-core` (committed, not pushed).
The 2026-06-13 packet's URGENT item **A1 (det-equity cap unit bypass) is now FIXED in code** —
see the resolution note at the end; the standing safety blocker on `6bb9` is lifted.

---

## A. Enablement flips (`.env`) — agent produces, operator runs

| Seed | Flag / action | Command (operator runs on the live box) | Gate BEFORE flipping |
|---|---|---|---|
| `ba90` (B05) | catalyst onboarding | `echo 'HERMES_QUANT_CATALYST_ONBOARDING=1' >> ~/.hermes/.env` | the ADR-0075 admission-precision eval axis built + GREEN (catalyst-admitted out-of-universe names beat a forward-return bar); see §C |
| `afa4` (B10) | learned-graph mining | `echo 'HERMES_QUANT_GRAPH_MINING=1' >> ~/.hermes/.env` + register the `quant-catalyst-graph-mine` cron (§B) | propagation-log corpus ≥ `MIN_SAMPLE=20`/edge; proposes-only, never auto-edits seed YAML (low risk) |
| `6bb9` (B12) | promote `HERMES_QUANT_PORTFOLIO_CAPS` + `HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2` to default-ON | flip the code defaults (a PR), after one clean side-by-side tick-log day | **UNBLOCKED** — the cr01/cs31 cap-unit bug that previously coupled this is fixed in code this session (`get_marked_equity` + the fold unit handling now derive ×100/×1 from `asset_class`). Still requires one clean side-by-side day before the default-flip. |
| `2f01` (B38) | `IC_DEDUP_AT_INGEST` + pass `factor_returns` at register | `echo 'HERMES_QUANT_IC_DEDUP_AT_INGEST=1' >> ~/.hermes/.env` + wire `factor_returns` at the factor-register call | enablement-only; no eval gate (dedup is strictly tighter) |
| `58e9` / `e18b` | Alpaca MCP read-only | paste the staged Alpaca MCP config block into the MCP config + cred-bridge from `~/.hermes/secrets` + reload; set `ALPACA_TOOLSETS` to the read-only allowlist + `PAPER_TRADE=true` | read-only + account toolset only on the PAPER MCP; see `0fc0` (resolved-accepted below) |

**`58e9`/`e18b` exact toolset note:** keep `ALPACA_TOOLSETS` to the read-only/account allowlist
and `ALPACA_PAPER_TRADE=true`. No order-placement toolset on the paper MCP per the standing
operator decision (paper, no order tools).

---

## B. Cron registrations — `cronjob` agent/MCP only (operator or Hermes cronjob agent runs)

These are JSON-backed jobs in `~/.hermes/cron/jobs.json`, registered **only** via the Hermes
`cronjob` tool (`action ∈ {create,list,update,pause,resume,remove,run}`) — never `cron(8)`/crontab,
never the Claude-session `CronCreate`. Copy-paste templates:

| Seed | Cron | `cronjob` registration (operator/cronjob-agent runs) | Prereq |
|---|---|---|---|
| `8b01` (B06) | catalyst profitability watchdog | `cronjob action=create name=quant-catalyst-profitability-daily schedule='0 8 * * *' command='~/.hermes/scripts/quant-catalyst-profitability.py'` | script deployed to `~/.hermes/scripts/` + dry-run clean |
| `71ef` (B11) | calibrator-drift | `cronjob action=create name=quant-calibrator-drift schedule='0 7 * * 1' command='~/.hermes/scripts/quant-calibrator-drift.py'` (Monday 07:00) | deploy the (built, tested) script to `~/.hermes/scripts/` |
| `afa4` (B10) | graph-mine | `cronjob action=create name=quant-catalyst-graph-mine schedule='0 6 * * 0' command='~/.hermes/scripts/quant-catalyst-graph-mine.py'` | + the `HERMES_QUANT_GRAPH_MINING=1` flip (§A) + corpus volume |

(Exact `schedule`/`command` strings are the operator's to confirm against the deployed script
paths; the cadence is per each seed's design — daily for B06, Monday-07:00 for B11.)

---

## C. Deploy-sync + registry reconciliation (operator audit)

| Seed | Action | Command |
|---|---|---|
| `9048` | GO-LIVE + DEPLOY-SYNC + CRON-REGISTRY destale | reconcile `~/.hermes/scripts` ↔ repo `ops/scripts` (the audit is now `{SAME:32}` exit-0; daily-interim migrated to ADR-0079). Update `docs/operations/CRON-REGISTRY.md` count (says 16; ~22 live after the ARIA 6 + graph-mine row) + `GO-LIVE-CHECKLIST.md`/ARIA-brief which still present the resolved deploy-drift blocker as live | run `ops/scripts/quant-deploy-drift-watch` (or the deploy-sync check) → confirm SAME → edit the two docs |

This is a **doc-reality reconciliation** the agent can largely do (the docs are in-repo); the
operator confirms the live `~/.hermes` side. The two doc edits (CRON-REGISTRY count, GO-LIVE
blocker-resolved) are agent-committable and are being made on the branch.

---

## D. Data/eval-gated (NOT flip-now — a measured prerequisite must clear first)

| Seed | Why it must wait | The gate (do NOT flip early) |
|---|---|---|
| `b67a` (B07) | raising `CONSUMER_TREND_CONFIDENCE_HAIRCUT` (0.5→toward 1.0) amplifies an UNPROVEN edge | B06 cron firing (above) + ≥20 `brand_self` propagations measured at ≥0.60 hit-rate via `profitability.py`, THEN a one-line haircut raise. Raising now is forbidden. |
| `ba90` (B05) | catalyst onboarding admits out-of-universe names | the ADR-0075 admission-precision eval axis must be built + green first (the agent can build the eval axis — see the rearch/eval track; the flip is operator) |
| `8db9` (B41-g) | governance close-out | gated on B41-a/-b landing; then amend ADR-0062 with the five-gate LLM-production-default criteria + per-stage default-flip checklist. Respects the `d9d8` ADR-freeze if active. |

---

## E. Already resolved / accepted (no action — recorded for the audit trail)

- **`0fc0`** — the Alpaca account-toolset `update_account_config` "leak" is a **documented,
  operator-ACCEPTED** trade-off (2026-06-01): with `ALPACA_PAPER_TRADE=true` there is no
  money/position at risk and it only mutates paper config. **Closed-accepted.**
- **2026-06-13 packet A1 (det-equity cap unit bypass)** — **FIXED this session.** The cap/MTM
  math now derives the ×100 (`us_option`) / ×1 (equity) unit from the persisted `asset_class`
  column (the cs31/33 family + the fold unit handling); the bug that read shares-as-NAV-fraction
  is closed. The `6bb9` default-ON promotion is therefore no longer blocked on it.

---

## F. ADR-deferred (gated on a named future increment — deferral is the disposition)

These are **not** "ignored" — each is explicitly gated on a named prerequisite, recorded so the
operator/agent re-opens it at the right time:

| Seed | Deferred until |
|---|---|
| `4d37` | ADR-0083 Phase-1 intraday — blocked-by settlement v0.1.2 (seed 3045) landing AND a *measured* edge (the catalyst/social-arb edge is multi-day; intraday is economically unmotivated today) |
| `79f5` | B45 452-factor Alpha Zoo — deferred v0.2; RL post-training = DO_NOT_BUILD |
| `817b` | B43 full-universe load test — skip-tier, deferred v0.9+ |
| `d9d8` | B15 — re-commit the 2-week ADR freeze through end of June (governance cadence, not a code task) |
| `5a63` | keep optional-MCP manifests pinned to current upstream versions (maintenance cadence) |
| `243d` | B48 — remove the `react.live` promotion.py fallback once a LIVE (not paper) reactor lands |

---

**Bottom line for the operator:** §A + §B are runnable now (with their gates); §C is a
doc-reconcile the agent commits + the operator confirms live; §D waits on data; §E needs nothing;
§F is correctly deferred. Once §A/§B are run, the corresponding seeds close on the operator side.
