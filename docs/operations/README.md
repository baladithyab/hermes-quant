# hermes-quant operations docs — start here

A human reading-order for the operations docs. Read top-to-bottom for a fresh setup; jump
to the relevant section for a specific task. Each doc is labeled **[operator]** (you run it)
or **[agent]** (ARIA/Hermes-facing; you may read it but it's written for the agent).

> **The one rule that governs all of these:** the deterministic risk gate (ADR-0004) + HITL
> (ADR-0015) are the sole, immutable order authority. Everything below makes the system
> *propose* / *observe* / *evolve* better; a human approves every order and ships every
> policy change. No flag, cron, or MCP is ever on the order path.

## 1. Install & integrate (first time)
1. **[../../README.md](../../README.md)** — install the plugin into the Hermes venv +
   enable it via the `config.yaml plugins.enabled` allow-list (NOT `hermes plugins enable`).
2. **[HERMES-INTEGRATION.md](HERMES-INTEGRATION.md)** [agent] — how the plugin loads, the
   tool/cron/skill surface, the manifest contract, the config.yaml allow-list mechanism.
3. **[HERMES-SELF-ONBOARDING.md](HERMES-SELF-ONBOARDING.md)** [agent] — ARIA's self-onboarding:
   the flag index, the crons it owns, and the meta-loop it runs on top of the plugin.

## 2. Go-live (Monday readiness)
4. **[GO-LIVE-CHECKLIST.md](GO-LIVE-CHECKLIST.md)** [operator] — the pre-market readiness
   checklist + verdict. (Some state may lag; cross-check live with `quant-deploy-audit.py`.)
5. **[DEPLOY-SYNC.md](DEPLOY-SYNC.md)** [operator] — the repo↔`~/.hermes/scripts` three-way
   reconcile discipline (§ reconcile; never blind-cp; the deployed copies can carry live
   safety fixes not in repo). Run `python ops/deploy/quant-deploy-audit.py` — clean = `{SAME:N}`.
6. **[CRON-REGISTRY.md](CRON-REGISTRY.md)** [operator] — the registered crons (firing layers +
   self-evolution + catalyst), schedules (PT), and what each fires.

## 3. Enablement (turning capabilities on — all default-OFF, eval-gated)
7. **[FEATURE-ENABLEMENT.md](FEATURE-ENABLEMENT.md)** [operator] — the trading/perception
   feature flags (semantic, catalyst, direction-bias, options gate, event-risk, …): default,
   what each gates, the eval gate to flip, rollback.
8. **[SELFEVOLVE-ENABLEMENT.md](SELFEVOLVE-ENABLEMENT.md)** [operator] — the W1–W7 self-evolution
   waves: enable order, per-flag eval gate, the `.env` one-liners, the crons. Advisory-plane
   only (ADR-0080) — the system proposes; a human ships.
9. **[2026-05-31-selfevolve-flag-flip-decision.md](2026-05-31-selfevolve-flag-flip-decision.md)**
   [operator] — the live-state record of which flags are currently flipped + why.
10. **[ROLLOUT.md](ROLLOUT.md)** [operator] — the promotion / standard-of-truth process: how a
    proposed change (weight, edge, hypothesis) reaches live (OOS backtest → promotion gate →
    operator sign-off). Includes the emergency-stop semantics (§5).

## 4. External surfaces
11. **[MCP-INTEGRATION.md](MCP-INTEGRATION.md)** [operator] — the optional-MCP registry
    (disabled-by-default, creds-gated, read-only-pinned), the enable runbook, and the
    money-write policy (no order-capable MCP is ever auto-enabled; the host auto-exposes
    MCP tools to the chat LLM with no HITL hook, so order toolsets stay OFF).

## 5. When something trips — recovery
- **[KILL-SWITCH-RECOVERY.md](KILL-SWITCH-RECOVERY.md)** [operator] — the two kill-switches
  (governance vs autonomous), how to tell which tripped, and the exact clear procedure for
  each (incl. minting the governance `HumanApprovalToken`). **Clearing a halt does NOT cancel
  open orders or flatten positions** — do that explicitly at the broker.

---
### Live-state pointers (not in repo — on the host)
- Flags: `~/.hermes/.env` (tool-guarded — operator edits) · Crons: `~/.hermes/cron.db`
- Config + MCP servers: `~/.hermes/config.yaml` (tool-guarded) · Secrets: `~/.hermes/secrets/`
- Runtime state: `~/.hermes/quant/` (state.json, halt_state.json, proposals/executions jsonl,
  daily-briefs, aria-runs) · Deployed scripts: `~/.hermes/scripts/quant-*.py`
- Staged operator runbooks (this session): `~/.hermes/quant/aria-runs/`
  (alpaca-mcp-ENABLE-RUNBOOK.md, W-FLAG-ENABLEMENT-2026-06-01.md, setup-2026-06-01.md)
