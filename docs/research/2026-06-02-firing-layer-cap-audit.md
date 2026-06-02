# Firing-layer portfolio-cap audit (2026-06-02)

Authored by orchestrator (the delegated subagent timed out; answered directly via grep + source read).

## The four trade-firing layers and their cap coverage

| Layer | Fire entry point | Reads `HERMES_QUANT_PORTFOLIO_CAPS`? | Clips via `clip_one_to_remaining_headroom`? | Status |
|---|---|---|---|---|
| **Autonomous-tick** | `hermes_quant/autonomous.py` (~line 388) → `clip` then `PaperReactor.execute` | ✅ yes | ✅ yes | **CAPPED (reference impl)** |
| **Advisor** | deployed `quant-daily-interim.py::auto_approve_actionables` → `quant_approve` → `PaperReactor.execute` | ✅ yes (patched 2026-06-02) | ✅ yes (patched 2026-06-02) | **CAPPED (deployed only; not vendored)** |
| **Playbook** | `quant-playbook-tick.py` → equity proposal → `PaperReactor.execute` (swing/leaps; others silenced pending ADR-0029) | ❌ no | ❌ no | **UNCAPPED — P1 gap** |
| **Hourly** | `quant-hourly-tick.py::maybe_run_autonomous_phase` (gated `HERMES_QUANT_AUTONOMOUS=1`) → propose+fire | ❌ no | ❌ no | **UNCAPPED — P1 gap** |

## Root structural observation

All four layers ultimately call **`PaperReactor.execute(proposal, *, fill_size_pct, ...)`** (`hermes_quant/react/paper.py:81`) to write the fill. That method already hosts a cross-cutting precondition — the **ADR-0077/0079 admissibility check** runs inside `execute()` before the record is appended. **It does NOT host a portfolio-cap clip.**

The cap is currently re-implemented per-layer (autonomous in-package, advisor in the deployed script). That's exactly the failure shape that caused the incident: *a risk control only some layers honor is not a risk control.* Two layers were missed.

## Recommendation: centralize the cap at the reactor seam

Add the portfolio-cap clip **inside `PaperReactor.execute()`**, behind the existing `HERMES_QUANT_PORTFOLIO_CAPS=1` flag, as a REACTION-layer precondition alongside admissibility:

- On entry, if the flag is set, reconstruct the current book (it already has `executions.jsonl` access via the bus), build `PortfolioState`, and `clip_one_to_remaining_headroom(symbol, fill_size_pct, state, caps)`.
- If clipped to ~0 → **silence the fire** (return a `silenced` ExecutionRecord or raise a typed `PortfolioCapSilenced` the callers already tolerate), with `reactor_metadata.silence_reason = "portfolio_cap_<reason>"`.
- If scaled down → execute at the clipped `fill_size_pct` and record `reactor_metadata.cap_scaled_from/to`.

### Why the seam, not per-script

- **Can't-forget-a-layer**: every current AND future firing path inherits the cap. The incident happened because a layer was added without the cap; centralizing makes that structurally impossible.
- **DRY**: one implementation, one test surface, instead of N drifting copies (the advisor copy is already drifting from the autonomous copy).
- **Consistent with the existing admissibility precondition** already living in `execute()` — the cap is the same KIND of cross-cutting reaction-layer gate.

### Migration discipline (avoid double-clipping)

The autonomous layer and the patched advisor script ALREADY clip before calling `execute()`. If the reactor ALSO clips, a pre-clipped fire would be clipped twice. Resolution: once the reactor-seam cap lands, the per-layer clips become redundant and should be REMOVED (autonomous in-package; advisor in the deployed script) — leaving the reactor as the single authority. Sequence the wave so the reactor cap lands first (default-OFF), then the per-layer clips are deleted in the same wave, gated behind the same flag flip. Keep the env var default-OFF until verified, per the money-software rollout rule.

### Caveat — `fill_size_pct` semantics

`PaperReactor.execute` documents `fill_size_pct` as "signed fraction of NAV (e.g. +0.05 = 5% long)". `clip_one_to_remaining_headroom` operates on exactly that unit (NAV fractions), so the centralization is unit-compatible. Confirm the running-state reconstruction inside the reactor reads net signed weights per `(asset_class, symbol)` — same shape the autonomous layer already builds.
