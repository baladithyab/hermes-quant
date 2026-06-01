# ARIA Integration Brief — hermes-quant Monday go-live (2026-06-01)

> The dispatch brief for ARIA (Hermes persona) to autonomously integrate the hermes-quant work.
> Authoritative sources ARIA must read + obey: docs/operations/HERMES-SELF-ONBOARDING.md,
> GO-LIVE-CHECKLIST.md, DEPLOY-SYNC.md, CRON-REGISTRY.md, MCP-INTEGRATION.md. Operator authorized
> FULL AUTONOMOUS integrate + FULL Alpaca MCP capability.

## Mission
Integrate the hermes-quant plugin so all cron jobs + pipeline stages are set for Monday market-open,
and enable the Alpaca MCP as the broker execution path (no custom client).

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
4. **Enable the Alpaca MCP (FULL capability)** in ~/.hermes/config.yaml mcp_servers via the
   mcp/optional-mcps/alpaca/manifest.yaml shape: `command: uvx, args: [alpaca-mcp-server]`, env from
   ~/.hermes/secrets/alpaca.env, **ALPACA_PAPER_TRADE=true** (paper account — non-negotiable for now).
   Full toolset (do NOT restrict ALPACA_TOOLSETS) so order/account tools are available via the MCP and
   no custom client is needed. Confirm reload.

## RAILS ARIA MUST HONOR (non-negotiable — these override the mission)
- **Paper only**: ALPACA_PAPER_TRADE=true. Never enable live-money trading.
- **The MCP is a MECHANISM, not an AUTHORITY**: even with the full Alpaca toolset available, an agent
  must NEVER autonomously place/cancel/replace an order. Order placement flows ONLY through the existing
  propose → human-approve (HITL) → deterministic risk gate → react path (ADR-0015/ADR-0004). The MCP order
  tools are the execution rail that path uses, not a bypass. No auto-fire, no LLM-decides-then-orders.
- **Deterministic gate / sizing ladder {0,±0.05,±0.10,±0.15,±0.20} / kill-switch are immutable.**
- **New-capability flags stay default-OFF** (ADR-0082/83/84 TREND_VELOCITY/CONVERGENCE/SATURATION/
  EVENT_RISK/OPTIONS_GATE/MULTILEG_REACTOR/DIRECTION_BIAS_GATE/CALENDAR_ENABLED/etc.) — flip only per
  each one's eval gate in SELFEVOLVE-ENABLEMENT/FEATURE-ENABLEMENT, not as part of this integration.
- **Reconcile, never blind-cp** (step 2). Back up config.yaml + any script before mutating.
- Report what was deployed/registered/enabled + any step that needs operator follow-up.
