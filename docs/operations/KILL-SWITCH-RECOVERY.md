# Kill-switch recovery runbook (operator)

**Status:** authoritative recovery procedure for both kill-switches
**Date:** 2026-06-01
**Why this exists:** the doc+setup audit (`docs/research/2026-06-01-r-plugin-setup-doc-audit.md`)
found that the project ships **two distinct kill-switches with overlapping names**, and the
*governance* one's clear-path (mint a `HumanApprovalToken`) was code-reachable but **had no
documented operator procedure** — an emergency stop with no published restart. This runbook
closes that gap. Both clear-paths below were verified working on 2026-06-01.

> **Money-software note.** A kill-switch trip is fail-CLOSED: the system stops opening/
> increasing positions. Clearing it is a deliberate, human-only act. Neither clear-path
> auto-grants — that is the safety property, not a bug.

---

## There are TWO kill-switches — identify which one tripped first

| | **Governance kill-switch** | **Autonomous kill-switch** |
|---|---|---|
| **Module** | `hermes_quant/governance/kill_switch.py` | `hermes_quant/autonomous.py` |
| **State file** | `~/.hermes/quant/state.json` (`halt: true`) | `~/.hermes/quant/autonomous_kill_switch.json` |
| **What it gates** | the governance plane / hard halt (`is_halted()`) | autonomous-tick auto-trip (re-enables autonomous mode) |
| **Trips on** | explicit governance halt | an autonomous safety condition (drawdown/anomaly auto-trip) |
| **Clear path** | mint a `HumanApprovalToken` (scope `kill_switch_clear`) → `clear(token)` | `hermes quant autonomous reset --confirm` |

Check which is set:
```bash
python3 -c "import json;print('governance halt:', json.load(open('$HOME/.hermes/quant/state.json')).get('halt'))" 2>/dev/null
ls -la ~/.hermes/quant/autonomous_kill_switch.json 2>/dev/null && echo "autonomous kill-switch FILE PRESENT (tripped)" || echo "autonomous kill-switch: not tripped"
```

---

## Recovery A — Autonomous kill-switch (the easy one, has a CLI)

```bash
hermes quant autonomous reset --confirm
# (--confirm is REQUIRED; re-enables autonomous mode after a trip)
```
If the gateway-integrated `hermes` CLI isn't on PATH (base pip install), use the module:
```bash
~/.hermes/hermes-agent/venv/bin/python3 -m hermes_quant.cli autonomous reset --confirm
```
This calls `reset_kill_switch()` and deletes `autonomous_kill_switch.json`. **It does NOT
clear the governance halt** — if both tripped, also do Recovery B.

---

## Recovery B — Governance kill-switch (mint a HumanApprovalToken, then clear)

The governance `clear()` requires a single-use `HumanApprovalToken` with
`scope='kill_switch_clear'` and `target_ref='state.json'`. There is no CLI verb for the
mint today (tracked: a `hermes quant govern grant-clear` verb would be the clean fix); mint
it directly. **You are the human authorizing this — that is the intended trust model
(`grant_token` records the grant; it does not verify identity).**

```bash
~/.hermes/hermes-agent/venv/bin/python3 - <<'PY'
from hermes_quant.governance import approvals, kill_switch
# 1) mint the single-use, short-TTL clear token (you, the operator, are the grantor)
tok = approvals.grant_token(
    "kill_switch_clear", "state.json",
    granted_by="<your-operator-id>",   # e.g. "codeseys" — recorded in the audit log
    ttl_minutes=10,
)
print("minted clear token:", tok.token_id)
# 2) clear the halt (consumes the token; atomic; writes halt_cleared_at)
kill_switch.clear(tok)
print("governance halt cleared:", not kill_switch.is_halted())
PY
```

What this does, step by step:
1. `grant_token(...)` mints + persists a single-use token (TTL 10 min) and appends an
   `approval_granted` row to the governance audit log (so the clear is attributable).
2. `kill_switch.clear(tok)` validates the token scope/target, flips `halt:false` atomically,
   stamps `halt_cleared_at`, and **consumes** the token (it cannot be reused).

Verify cleared:
```bash
python3 -c "import json;print('halt now:', json.load(open('$HOME/.hermes/quant/state.json')).get('halt'))"
```

---

## Important caveat — clearing a kill-switch does NOT flatten or cancel orders

The emergency stop is **intent-only**: it halts *new/increasing* opening activity. It does
**NOT** cancel resting broker orders or flatten existing positions (`cli/halts.py`
`cmd_emergency_stop` step 4 is intent-only; honestly noted in `ROLLOUT.md` §5). If you need
positions flat, do that explicitly at the broker (paper: Alpaca dashboard) — clearing the
halt only re-permits the pipeline to act; it does not undo or unwind anything.

---

## Rollback / re-halt

To re-assert the governance halt (fail-closed; no token needed to STOP):
```bash
~/.hermes/hermes-agent/venv/bin/python3 -c "from hermes_quant.governance import kill_switch; kill_switch.fire(reason='operator manual re-halt')"
```

## Related
- `GO-LIVE-CHECKLIST.md` EMERGENCY row · `ROLLOUT.md` §5 · `ADR-0031` (governance plane) ·
  `ADR-0004` (deterministic gate — the kill-switch sits OUTSIDE and above it, immutable by the loop).
