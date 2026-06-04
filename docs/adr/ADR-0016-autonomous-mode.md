# ADR-0016: Autonomous mode (silence-bias gated paper-trading)

**Status**: Accepted (2026-05-13), implemented
**Date**: 2026-05-13
**Target**: v0.2.0 (paper-only); live deferred to v0.3+
**Cross-cuts**: ADR-0004 (risk gate), ADR-0010 (settlement journal), ADR-0013 (integration stance), ADR-0014 (advisor), ADR-0015 (HITL React), founding charter §"REACT silence-by-default gates"

---

## Context

PDR (Perceive-Decide-React) has three modes per the ADR-0015 D1 taxonomy:

| Mode | Perceive | Decide | React | Human gate |
|---|---|---|---|---|
| **advise** | yes | yes | NO | n/a (ADR-0014) |
| **hitl** | yes | yes | yes after approve | per-proposal (ADR-0015) |
| **autonomous** | yes | yes | yes if **silence-bias gate** fires | NONE — gate decides (THIS ADR) |

Autonomous mode is what makes hermes-quant a "watch for opportunities" agent. It runs on a cadence, surveys a watchlist, runs the same Perceive→Decide pipeline as advise/HITL, and a 4-dim silence-bias gate decides whether to React. This is the mode the user explicitly asked for: *"autonomous mode so that hermes can automatically keep an eye out for opportunities while also being able to trade it automatically."*

The DEFAULT SAFETY POSTURE is the founding charter's *"rewarded for correct inaction"* invariant. Quoting the charter verbatim:

> *"silence-by-default gates open only when:*
> *  - ensemble disagreement is LOW (high confidence)*
> *  - expected edge > transaction cost + slippage + risk premium*
> *  - position would not violate risk limits (VaR, exposure caps)*
>
> *Otherwise: hold cash, do nothing. This is the most underrated property — most trading systems lose because they over-trade."*

> *"The silence-biased gate principle from Eidolon (-2.0 init bias, 3× FP penalty) is exactly the right prior for trading. Most retail RL trading agents fail because they're rewarded for action. Yours should be **rewarded for correct inaction**."*

The 4-dim silence-bias gate codifies these clauses as ADR-binding behavior. v0.2 ships **paper-only autonomous**; live autonomous is gated behind three independent locks per D6.

The pattern source is Eidolon's `pdr_lwm/decision.py::OutputGateSystem` 7-dim output gate (need / timing / confidence / modality / urgency / compute / adaptation / salience). This ADR collapses to 4 dims for the trading domain — modality and need are constants (we always emit a position-target action), adaptation belongs in v0.4+ continual learning, timing maps onto the cron cadence rather than a per-tick gate dim.

## Decision

### D1: Three-mode taxonomy LOCKED (recap from ADR-0015)

The mode is read from `quant.pdr.mode` in `~/.hermes/config.yaml` per ADR-0015 §D7. Default `advise`. Autonomous tools (`quant_autonomous_tick(dry_run=false)`, the Hermes cron job) refuse to fire unless `mode=autonomous`. The mode flag is read on EVERY tool call (no caching) so an operator can flip without daemon restart per ADR-0015 §D7.

### D2: 4-dim silence-bias gate (charter §"REACT" mapping)

Adapts Eidolon's 7-dim gate to trading. **ALL FOUR dims must exceed their thresholds for the gate to FIRE.** Otherwise: silence.

```python
@dataclass(frozen=True)
class SilenceBiasGateConfig:
    # Confidence: post-calibration ensemble probability. Stricter than
    # HITL (HITL operator can override with judgment; autonomous can't).
    min_confidence: float = 0.65

    # Urgency: expected_signed_edge / volatility. Charter says edge must
    # exceed transaction cost + slippage + risk premium. Codified as
    # a Sharpe-like ratio — at min_urgency=0.5 the edge is half a stdev.
    # Combined with confidence>0.65 this is a meaningful quality threshold.
    min_urgency: float = 0.5

    # Compute budget: how many analysts emitted a view at all. With 2
    # analysts in v0.1.2 (ClassicalTA + MicrostructureLite), require both.
    # When KronosAnalyst lands in v0.3, default raises to 2 of 3.
    # The principle: a single-voice signal is never enough in autonomous mode.
    min_analysts_emitted: int = 2

    # Salience: skip symbols with N+ recent rejections in the journal.
    # The operator's repeated "no" is signal the system shouldn't
    # autonomously override.
    max_recent_rejections: int = 3
    salience_window_hours: int = 168       # 7 days
```

The gate function returns one of:

- `GateDecision.FIRE` — all 4 dims passed; React allowed
- `GateDecision.SILENCE_LOW_CONFIDENCE`
- `GateDecision.SILENCE_LOW_URGENCY`
- `GateDecision.SILENCE_INSUFFICIENT_VOICES`
- `GateDecision.SILENCE_SALIENCE_VETO`

The reason is logged AND included in the autonomous tick output. Operators read why the gate didn't fire and decide if config tuning is needed. **The point of structured silence reasons is to make tuning a data exercise, not a guess.**

### D3: Risk gate runs AFTER silence-bias gate

The existing `DefaultRiskGate` (8 rules per ADR-0004) is NOT replaced. Sequence:

```
advisor.recommend(symbol, ...)
    -> aggregated_signal
    -> silence_bias_gate.evaluate(signal, ctx, journal_lessons)
       -> FIRE | SILENCE_*
    -> if FIRE:
       risk_gate.gate(signal, market, portfolio, halt_state)
       -> Action | None
       -> if Action and target_position_pct != 0:
          paper_react(action)              [v0.2]
          live_react(action) if --live     [v0.3+]
       -> else: log; no React
```

The silence-bias gate is the autonomous-mode-specific *"is this signal worth even considering"* filter. The risk gate (already proven in HITL mode and via the daemon autopilot path) is the universal *"does this trade fit the portfolio"* filter. Both fire in sequence; silence-bias first because it's cheaper.

### D4: Cadence via Hermes cron (NOT systemd daemon)

Per ADR-0013 §D4 (cron-is-for-ops-tasks-not-daemon): autonomous mode uses Hermes cron for the tick. The 3-min hard interrupt that's a dealbreaker for the realtime daemon (sub-second decisions) is FINE for autonomous chat-mode tick (15-min default cadence).

Why not the existing daemon? The daemon is the AUTOPILOT path (signals.jsonl → freqtrade). Autonomous chat-mode is a SECOND consumer of the same Perceive→Decide pipeline that emits paper executions directly via PaperReactor (the HITL adapter). One pipeline, two writers — daemon for autopilot+freqtrade, autonomous for chat-mode operators who don't run freqtrade.

The user invokes:

```bash
hermes quant autonomous start [--cadence 15m] [--watchlist BTC/USDT,ETH/USDT,AAPL]
```

Under the hood this:
1. Writes the watchlist to `~/.hermes/config.yaml::quant.autonomous.watchlist`
2. Writes a tick script to `~/.hermes/scripts/hermes-quant-autonomous-tick.sh`
3. Creates a Hermes cron job: `hermes cron create '15m' --script <path> --no-agent`

The script runs `hermes quant autonomous tick` (no LLM, just the function call) and prints the tick summary; that becomes the cron job's stdout, delivered per the user's cron destination config.

### D5: Watchlist storage

`~/.hermes/config.yaml::quant.autonomous.watchlist` — list of `{symbol, asset_class, timeframe?}` objects. Profile-aware per ADR-0013 §D4 (live-binance and paper-binance can have different watchlists).

```yaml
quant:
  pdr:
    mode: autonomous          # required for autonomous tick to fire
  autonomous:
    cadence: "15m"            # cron schedule expression
    watchlist:
      - symbol: BTC/USDT
        asset_class: crypto
        timeframe: 1h
      - symbol: ETH/USDT
        asset_class: crypto
        timeframe: 1h
      - symbol: AAPL
        asset_class: equity
        timeframe: 1d
    silence_bias:
      min_confidence: 0.65
      min_urgency: 0.5
      min_analysts_emitted: 2
      max_recent_rejections: 3
      salience_window_hours: 168
    max_concurrent_positions: 5    # hard cap; if at cap, gate vetoes new opens
    max_per_tick_opens: 1          # NEW positions per tick; existing exit signals always fire
    kill_switch_pct: 0.10          # cumulative paper P&L floor; below disables autonomous
```

### D6: Live mode gated by THREE independent locks

v0.2 ships paper-only. Live autonomous deferred to v0.3 behind:

1. `quant.autonomous.allow_live: true` in config (default `false`)
2. Broker credentials present in `.env`
3. Per-startup `hermes quant autonomous arm-live --confirm "I understand I'm trading real money"` ceremony — emits a 24h-TTL token; without the token the cron tick falls back to paper

This belt-and-suspenders is deliberate. Per the charter, *"AAAI 2026 acceptance ≠ alpha. Anyone who tells you otherwise is selling something."* Autonomous + live is the most dangerous combo the plugin can offer; the locks reflect that.

### D7: PaperReactor reused (already shipped in ADR-0015)

No new reactor for autonomous mode in v0.2 — the existing `PaperReactor` writes to `executions.jsonl`. The settlement loop reads executions and updates the calibrator (per ADR-0010), so autonomous fills feed the same learning loop HITL fills do. This is the LEARNING property: autonomous-mode P&L over the next N weeks IS the empirical test of whether the silence-bias thresholds are correctly tuned.

### D8: Tick output is operator-readable

Each tick produces a structured summary:

```json
{
  "asof": "2026-05-13T20:00:00Z",
  "watchlist_size": 3,
  "decisions": [
    {"symbol": "BTC/USDT", "gate": "FIRE",
     "action": {"target_position_pct": 0.05, "reason": "..."},
     "execution_id": "exec_..."},
    {"symbol": "ETH/USDT", "gate": "SILENCE_LOW_CONFIDENCE",
     "details": {"confidence": 0.41, "min_required": 0.65}},
    {"symbol": "AAPL", "gate": "SILENCE_INSUFFICIENT_VOICES",
     "details": {"emitted": 1, "min_required": 2}}
  ],
  "fires": 1, "silences": 2, "errors": 0,
  "next_run_at": "2026-05-13T20:15:00Z"
}
```

Hermes cron delivers this stdout to the configured destination (chat, file, etc.). Operators see what the system did/didn't do every tick.

### D9: Per-tick safety rails

- **`max_per_tick_opens` (default 1)** — hard cap on new opens per tick. Prevents autonomous mode from opening 50 positions on a single regime-shift bar.
- **`max_concurrent_positions` (default 5)** — global cap. Reads existing positions from `portfolio_loader` (when ADR-0011 lands) or from `executions.jsonl` (v0.2 fallback).
- **Existing risk gate Rules 1+2** (drawdown + daily-loss circuit breakers) ALWAYS apply — they're the kill switch.
- **`kill_switch_pct` (default 0.10)** — if cumulative paper P&L since autonomous start < `-kill_switch_pct`, autonomous mode disables itself, emits a one-shot alert, and requires `hermes quant autonomous reset --confirm` to re-enable.

### D10: ADR-0016 (autonomous) vs ADR-0015 (HITL) — what's different

| Aspect | HITL (ADR-0015) | Autonomous (ADR-0016) |
|---|---|---|
| Trigger | Operator types `quant_propose` | Cron tick |
| Gate | Operator approves / rejects | 4-dim silence-bias gate |
| Confidence threshold | Whatever advisor returns | `min_confidence` (default 0.65, stricter) |
| Position size | Operator can override Kelly | Kelly only, no override |
| Live trading | `--live` flag at approve time | THREE locks per D6 |
| Audit trail | Journal entry per approve/reject | Journal entry per FIRE; SILENCE entries optional via `quant.autonomous.log_silences` |
| Cadence | Operator-driven, ad hoc | Cron, regular |
| Kill switch | Operator stops typing | `kill_switch_pct` + circuit breakers |

### D11: Five new tools

- `quant_autonomous_tick(dry_run=true)` — runs a single tick over the watchlist; with `dry_run=true` (default for tool surface), reports decisions but does NOT React.
- `quant_autonomous_status()` — current mode, watchlist, last-tick-summary, next-run, kill-switch state.
- `quant_watchlist_add(symbol, asset_class, timeframe?)`
- `quant_watchlist_remove(symbol)`
- `quant_watchlist_list()`

`quant_autonomous_tick(dry_run=false)` is the only "actuator-side" tool the agent gets — and it's gated by `quant.pdr.mode=autonomous` (`mode_mismatch` otherwise). The tool surface defaults to dry-run because LLM agents generating tool calls is the highest-bandwidth source of unintended action; the cron-script path is what fires real (paper) trades by setting `dry_run=False`.

### D12: Failure modes + observability

| Failure | Behavior |
|---|---|
| Single-symbol fetch failure (rate limit, network) | Log + skip that symbol; continue tick |
| All-symbols fail | Tick reports `errors=N`, no Reacts |
| Risk gate returns Action with target=0 (cost gate veto) | Log; no React (matches HITL semantics) |
| Cron tick hits 3-min interrupt | Tick aborts cleanly; next tick picks up from current state |
| `kill_switch_pct` trip | Autonomous mode disables itself; alert; reset ceremony required |
| Watchlist empty | Tick is a no-op with `watchlist_size=0` |
| Mode != autonomous when cron fires | Tick aborts with `mode_mismatch`; cron stays scheduled |

## Consequences

### Positive

- **Closes the third PDR mode.** The user's "watch for opportunities" use case works with one config flag flip.
- **Reuses existing PaperReactor + DefaultRiskGate + advisor pipeline.** Autonomous = orchestration on top, not a parallel system. Bug fixes in any layer benefit all three modes.
- **Cron-based cadence keeps autonomous OUT of the realtime daemon.** Sub-second isn't needed for chat-mode autonomous; the daemon stays the autopilot path.
- **Silence-bias gate is config-tunable.** Operators can tighten/loosen as their dogfooding produces signal — and the structured silence reasons make tuning a data exercise.
- **Live-mode gates are three-deep.** The most dangerous mode of the plugin (autonomous + live) requires explicit ceremony; no single-config-flip path to live.

### Negative

- **Three modes means three test matrices.** Mitigated: shared analyst+aggregator+risk-gate code is exercised by all.
- **Autonomous mode without operator oversight is dangerous.** The locks in D6 + kill-switch in D9 + tick log in D8 are the mitigation, not extras. Operators who skip reviewing tick output WILL get burned on a regime shift.
- **15-min cadence means we miss fast moves.** Appropriate for liquid 1d/1h symbols, NOT appropriate for crypto-futures or earnings plays. Documented as a known limitation; sub-15-min cadence requires the realtime daemon path.
- **Kelly-fractional position sizing in autonomous mode means cold-start results in tiny positions.** Calibrator-not-ready → confidence shrinkage → small Kelly fractions. Feature, not bug, but operators expecting 5% NAV trades on day 1 will see 0.5% trades. Documented in the silence-bias output's `details` field.
- **No new-position guard against existing positions** in v0.2 — the dispatcher relies on the risk gate's universal rules. ADR-0017 (deferred to v0.3) adds per-symbol position-sizing reconciliation.

## Cross-references

- **Founding charter** `docs/charter/2026-05-13-hermes-quant-charter.md` — *"silence-by-default gates"* and *"rewarded for correct inaction"* are the principles this ADR codifies. The 4-dim collapse is a domain-specific reading of the charter's three-bullet REACT clause.
- **ADR-0013 §D4** — cron-is-for-ops-tasks-not-daemon stance. Autonomous tick is an "ops task" (15-min cadence chat-mode), not the realtime daemon.
- **ADR-0014** — advisor pipeline reused; `recommend()` is called identically by HITL and autonomous.
- **ADR-0015 §D6** — `ExecutionRecord` schema reused. Autonomous tick FIRE → `PaperReactor.execute(...)` → same JSONL bus as HITL.
- **ADR-0010** — journal feedback loop. Autonomous FIRE entries get journaled like HITL approvals; SILENCE entries optional via config.
- **ADR-0004** — `DefaultRiskGate` runs after silence-bias gate. The 8 risk-gate rules are the universal trade-fitness filter.
- **Eidolon `pdr_lwm/decision.py::OutputGateSystem`** — pattern source for the multi-dim gate. Eidolon uses 7 dims for general-purpose embodiment; trading collapses cleanly to 4 (need/modality are constants, adaptation belongs in v0.4+ continual learning, timing maps onto the cron cadence).

## Provenance

- User directive 2026-05-13 ("autonomous mode so that hermes can automatically keep an eye out for opportunities while also being able to trade it automatically").
- Founding charter §"REACT" three-bullet silence-by-default specification.
- Founding charter §"Things I want to flag before you start" — *"AAAI 2026 acceptance ≠ alpha"* — motivates the three-lock live-mode gate.
- Eidolon project (`/mnt/e/CS/HF/eidolon/pdr_lwm/`) PDR pattern (NOT forked; pattern only).
- ADR-0015 §D1 three-mode taxonomy.

## Implementation order in v0.2.0

1. `hermes_quant/gates/silence_bias.py` — pure-function 4-dim gate
2. `hermes_quant/watchlist.py` — config-driven symbol list with add/remove/list
3. `hermes_quant/autonomous.py` — `tick(symbols, *, dry_run)` orchestrator
4. Tools: `quant_autonomous_tick`, `quant_autonomous_status`, `quant_watchlist_*`
5. CLI subcommand tree: `hermes quant autonomous {start|stop|status|tick|dry-run|reset}`
6. Test fence (~25 tests covering gate dimensions, mode-mismatch, dry-run safety, kill-switch trip, salience-veto journal integration, paper-only enforcement)
7. CHANGELOG + bump to v0.2.0 + tag

Live-mode + per-symbol position reconciliation deferred to v0.3+.
