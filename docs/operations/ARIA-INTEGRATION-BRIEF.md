# ARIA Integration Brief — hermes-quant Monday go-live (2026-06-01)

> The dispatch brief for ARIA (Hermes persona) to autonomously integrate the hermes-quant work.
> Authoritative sources ARIA must read + obey: docs/operations/HERMES-SELF-ONBOARDING.md,
> GO-LIVE-CHECKLIST.md, DEPLOY-SYNC.md, CRON-REGISTRY.md, MCP-INTEGRATION.md. Operator authorized
> FULL AUTONOMOUS integrate. The Alpaca MCP is enabled as a **read-only DATA + read-only-account**
> surface (paper) — NOT an order-placement path.

> **CORRECTION (2026-06-01, post-review):** the original brief framed Step 4 as "FULL Alpaca MCP
> capability / broker execution path (no custom client) / do NOT restrict ALPACA_TOOLSETS". That
> was self-contradictory and is RETRACTED. review-team-3 + the live ARIA run both confirmed: the
> Hermes host auto-exposes EVERY `config.yaml mcp_servers` tool to the chat LLM with NO per-tool
> HITL hook (MCP-INTEGRATION.md §3/§8.1), so an unrestricted toolset makes `place_stock_order` /
> `cancel_order` / `close_all_positions` directly LLM-callable — mechanically agent-autonomous order
> placement outside the deterministic gate, which the brief's own RAILS forbid and GO-LIVE-CHECKLIST
> lines 287-288/316-317 explicitly exclude from Monday. Step 4 below is corrected to the read-only
> pin (the only config that satisfies both the intent and every rail). The "MCP as execution rail"
> idea is a SEPARATE future decision requiring real propose→HITL→gate→react→MCP-order wiring that
> does not exist today; it is NOT part of this integration.

## Mission
Integrate the hermes-quant plugin so all cron jobs + pipeline stages are set for Monday market-open,
and enable the Alpaca MCP as a **read-only data + read-only-account** surface (paper). Orders — even
paper — NEVER originate from an LLM/MCP tool call; they flow only through propose→HITL→gate→react.

## Ordered steps (autonomous)
1. **Deploy the REPO_ONLY_NEW scripts** (16 built this drive, none yet live) to ~/.hermes/scripts/ via
   `cp` (safe — nothing to clobber), then `python ops/deploy/quant-deploy-audit.py` and confirm each
   shows SAME before registering its cron.
2. **Reconcile the 8 DRIFT scripts** — THREE-WAY reconcile per DEPLOY-SYNC.md §49, NEVER a blind cp.
   CRITICAL: the deployed playbook-weekly.py / playbook-quarterly.py carry the Wave-1d equity-halt-filter
   safety fix that is NOT in repo — preserve it. playbook-tick/hourly-tick/universe-scan have richer
   deployed emit formatters — preserve them. Merge the repo's PerceptionFrame/B04/ADR-0075/corrupt-row
   additions IN without dropping the deployed fixes. Dry-run + re-audit before finalizing.
3. **Register the owed crons** (CRON-REGISTRY rows 17-21: catalyst-profitability, calibrator-drift,
   weekly-retro, monthly-meta-retro, research-loop + graph-mine) via the Hermes `cronjob` mechanism,
   AFTER their scripts are deployed+SAME, on the schedules in CRON-REGISTRY.md (PT exprs, host TZ=PDT).
4. **Enable the Alpaca MCP (READ-ONLY pin)** in ~/.hermes/config.yaml mcp_servers via the
   mcp/optional-mcps/alpaca/manifest.yaml shape: `command: uvx, args: [alpaca-mcp-server]`, env from
   ~/.hermes/secrets/alpaca.env, **ALPACA_PAPER_TRADE=true** (paper account — non-negotiable).
   **PIN `ALPACA_TOOLSETS` to the read-only allowlist** — `stock-data,crypto-data,options-data,assets,
   corporate-actions,news` — so the server exposes ONLY data tools (live-probe-verified: 34 tools, zero
   `place_*`/`cancel_*`/`close_*`/`replace`/`exercise`). Note: the `account` toolset is EXCLUDED because
   it leaks the account-mutating `update_account_config` (seed 0fc0); re-adding `account` is a documented
   operator trade-off (gains buying-power reads, re-introduces that one write tool). This is the ONLY safe
   boundary — the MCP has no internal HITL hook and the host auto-exposes every tool to the chat LLM.
   config.yaml + .env are tool-guarded, so the actual write is operator-gated (staged block + cred-bridge
   one-liner in the run report). Confirm reload + verify the tool surface has NO order tools.

## RAILS ARIA MUST HONOR (non-negotiable — these override the mission)
- **Paper only**: ALPACA_PAPER_TRADE=true. Never enable live-money trading.
- **The MCP is a read-only DATA surface for Monday; it is a MECHANISM, never an AUTHORITY**: the Alpaca
  MCP is pinned to the read-only toolset (no order tools exist on the surface), so there is no order tool
  for an agent to fire. An agent must NEVER autonomously place/cancel/replace an order. Order placement
  flows ONLY through the existing propose → human-approve (HITL) → deterministic risk gate → react path
  (ADR-0015/ADR-0004). No auto-fire, no LLM-decides-then-orders. Exposing order tools to the chat LLM
  (an unrestricted toolset) is FORBIDDEN — the host has no per-tool HITL hook, so that would be autonomous
  order placement outside the gate. Wiring the MCP as the react-step execution rail (post-gate, post-HITL)
  is a separate future decision, NOT this integration.
- **Deterministic gate / sizing ladder {0,±0.05,±0.10,±0.15,±0.20} / kill-switch are immutable.**
- **New-capability flags stay default-OFF** (ADR-0082/83/84 TREND_VELOCITY/CONVERGENCE/SATURATION/
  EVENT_RISK/OPTIONS_GATE/MULTILEG_REACTOR/DIRECTION_BIAS_GATE/CALENDAR_ENABLED/etc.) — flip only per
  each one's eval gate in SELFEVOLVE-ENABLEMENT/FEATURE-ENABLEMENT, not as part of this integration.
- **Reconcile, never blind-cp** (step 2). Back up config.yaml + any script before mutating.
- Report what was deployed/registered/enabled + any step that needs operator follow-up.
