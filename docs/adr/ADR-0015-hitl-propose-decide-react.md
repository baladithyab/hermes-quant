# ADR-0015: HITL propose-decide-react surface

**Status:** Proposed (target: v0.1.2)
**Supersedes:** none
**Amends:** none (extends ADR-0013 dual-surface; completes the third PDR mode alongside ADR-0014)
**Cross-cuts:** ADR-0001 (sidecar), ADR-0002 (analyst protocol), ADR-0003 (calibrators), ADR-0004 (risk gate), ADR-0007 (plugin shape), ADR-0008 (signal bus), ADR-0009 (executions back-channel), ADR-0010 (settlement journal), ADR-0011 (portfolio reconstruction), ADR-0012 (LLMAnalyst deferred), ADR-0013 (integration stance), ADR-0014 (advisor surface)

---

## Context

hermes-quant implements PERCEIVE-DECIDE-REACT (PDR) for trading. The pattern is **similar to but not forked from** Eidolon's `pdr_lwm/environment.py::AutonomousAgent.run()` loop — the founding charter ([`docs/charter/2026-05-13-hermes-quant-charter.md`](../charter/2026-05-13-hermes-quant-charter.md)) directed "fork the pattern, not the trained weights" because market data needs market-data encoders.

v0.1.1 shipped Perceive (`DataProvider` → `MarketContext`, ADR-0005) and Decide (`Analyst` pool → `BMAAggregator` → `DefaultRiskGate`, ADR-0002 / 0003 / 0004). The autopilot daemon ships React via `signals.jsonl` → freqtrade (ADR-0008). ADR-0014 added an **advise-only** surface — synchronous `quant_recommend` that runs Perceive→Decide and stops short of any React side-effect.

The three PDR modes the project recognizes are:

| Mode           | Perceive | Decide | React                                  | Operator posture                                                                                            |
|----------------|----------|--------|----------------------------------------|-------------------------------------------------------------------------------------------------------------|
| **advise**     | yes      | yes    | NO (return guidance only)              | Chat-mode "what does the system say about X" — shipped in ADR-0014 as `quant_recommend`.                    |
| **hitl**       | yes      | yes    | YES, after human approval              | **This ADR.** Operator-in-the-loop trading; agent proposes, human approves/rejects, paper-or-live React executes. |
| **autonomous** | yes      | yes    | YES, gated by 4-dim silence-bias gate  | v0.2 — daemon-on-tick or cron-on-tick, no human gate; gated by ensemble disagreement / cost / risk / regime. |

The user directive that motivates this ADR is verbatim: *"trading guidance — HITL or automated, not just guidance."* Advisor mode alone reduces hermes-quant to a chat toy. HITL is the bridge that produces real (initially paper-book) trades, generates real settlement outcomes, lets the calibrator learn from human-overridden decisions, and gives the operator a way to dogfood the v0.1.1 stack on real decisions before flipping `quant.pdr.mode: autonomous`.

### Why HITL must exist before autonomous

The charter §"Layer 3: Risk/Execution gate" is explicit on silence-by-default:

> **Hard rules, not learned:** Kelly cap … Drawdown circuit breakers … Transaction-cost-aware threshold: `|expected_edge| > 2 × (commission + half_spread + estimated_slippage)`. If the gate says no, the system holds cash. **Silence by default.**

…and on the inversion of the trading reward signal (charter §"The PDR mapping is actually clean here"):

> The silence-biased gate principle from Eidolon (-2.0 init bias, 3× FP penalty) is exactly the right prior for trading. Most retail RL trading agents fail because they're rewarded for action. Yours should be **rewarded for correct inaction**.

HITL operationalizes that prior at the operator layer: **a human is the silence-bias gate while the autonomous gate matures.** Until the v0.2 4-dim silence-bias gate has accumulated enough realized fills to be calibrated, the operator IS the gate. Every approve/reject is both an execution decision AND a label that trains the calibrator on what the human considers a "correct inaction" (D8). The charter §"What I'd build first" makes this path explicit: *"5. Paper-trade for 4-8 weeks logging every decision + every analyst's contribution. 6. Then introduce the RL aggregator…"* HITL fills weeks 1-8 of that timeline at the chat surface — `pip install -e '.[yfinance]'` plus one config flag, no broker required.

### Hard constraints inherited from prior ADRs

- **Tools are read-only by Hermes plugin convention** (ADR-0007). This ADR introduces tools that produce side-effects. Resolution: the React side-effect happens **inside the approve handler** on a record the human has explicitly identified by `proposal_id` (D4) — the human has named the artifact, the LLM is not selecting which trade to execute.
- **Money never goes through advisor-style tools.** Per `AGENTS.md`: *"if a tool could place a real-money trade, an accidental 'yeah do that' in chat could move thousands of dollars."* v0.1.2 ships paper React only; live React deferred to v0.2 behind a per-call `--live` flag (D10).
- **Reproducibility (ADR-0001).** Every approval/rejection appends to on-disk artifacts; backtest replay against identical bars + identical human-decision log must reproduce identical executions.
- **Public PluginContext only (ADR-0013 D2).** All five new tools register through `ctx.register_tool(...)`. Zero monkeypatches.

## Decision

### D1: Three-state proposal lifecycle

Every proposal exists in exactly one of three terminal-or-pending states:

```
                         ┌─► approved   (React fires, executions.jsonl appended)
                         │
              pending ───┤─► rejected   (React skipped, journal records human override)
                         │
                         └─► expired    (TTL elapsed; treated as rejected with reason="ttl_elapsed")
```

The state machine is **one-way**: no `pending → pending` reissue, no `rejected → approved` revival. A rejected or expired proposal is dead; the operator must call `quant_propose` again. This matters because the advisor result embedded in the proposal (D4) is a snapshot of the calibrator + analyst views at proposal time; reviving a stale proposal would execute against a stale snapshot — the failure mode the cost gate is designed to prevent.

Default TTL is **15 minutes**, configurable per-call via `ttl_minutes` and globally via `quant.hitl.default_ttl_minutes`. Rationale: long enough for an operator on Discord/CLI to read and form a view, short enough that intraday signals don't go stale. For 1d-timeframe equity proposals operators may extend to 240 minutes per-call.

Approved proposals trigger React. Rejected and expired proposals do **not** trigger React, but DO append to the settlement journal as "human override" lessons (D8 — the LEARNING property, not just a check valve).

### D2: Storage = `~/.hermes/quant/proposals.jsonl` + SQLite index

JSONL append-only is the source of truth, mirroring the `signals.jsonl` shape from ADR-0008. SQLite (`proposals.db`) is a derived index keyed by `proposal_id` for O(log n) lookup of pending status, recent proposals per symbol, and the global pending count for the TTL sweep.

**Layout** (profile-aware per ADR-0013 D4):

```
~/.hermes/profiles/<profile>/quant/
    proposals.jsonl       # append-only source of truth
    proposals.db          # SQLite index, derivable from JSONL
    executions.jsonl      # paper React output (and live React in v0.2)
    journal.md            # ADR-0010 settlement journal (cross-cut)
    signals.jsonl         # ADR-0008 wire-format bus (autopilot only)
```

If the daemon is not running, `proposals.db` is rebuilt on next `quant_propose` call by replaying `proposals.jsonl` (mirrors the state.db rebuild pattern in `AGENTS.md` §"Cross-process state"). Atomic-rename writes use the same fsync→rename pattern ADR-0010 specifies for `journal.md` and ADR-0001 specifies for `state.json`.

### D3: `proposal_id` format

```
prop_<UTC_ISO_seconds>_<symbol>_<random6>

example: prop_2026-05-13T184230_AAPL_7f3a91
```

`prop_` prefix distinguishes from `sig_` (ADR-0008) and `exec_` (ADR-0009 P0-3). UTC ISO seconds are sortable and tz-unambiguous. Symbol enables `grep AAPL proposals.jsonl` for one-symbol audits without parsing JSON. 6-char random hex (24 bits) protects against collisions in the same wall-clock second; sufficient for the expected proposal volume (≪ 1000/day in HITL mode). Format is stable; tooling that parses by index (`split('_')`) is supported.

### D4: Five new tools

The Hermes plugin convention is "tools = read-only views" (ADR-0007). This ADR introduces tools whose handlers produce side-effects. Structural decisions:

1. There are exactly **two side-effect-producing tools**: `quant_propose` (writes a `pending` proposal) and `quant_approve` (executes React). One write per tool.
2. The React side-effect happens **inside `quant_approve`'s handler**, not in a separate `quant_execute` tool. Splitting would create a window where an approved-but-not-executed proposal is reachable by another caller; the inline pattern keeps the audit trail tight: one call → approval-mark + execution-record + journal-append in one transaction. Append-then-mark order: if the process crashes between, the rebuild logic in D2 re-classifies as expired on next read.
3. The two read-only tools (`quant_pending`, `quant_proposal`) follow the ADR-0007 convention strictly — pure projections.

| Tool             | Args                                                                                          | Side-effect                                                                                                                  | Returns                                                          |
|------------------|-----------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------|
| `quant_propose`  | `symbol`, `asset_class?`, `timeframe?`, `lookback_bars?`, `ttl_minutes?`, `sizing_mode?`      | Runs the advisor pipeline (ADR-0014), writes a `pending` proposal to `proposals.jsonl` + index                              | `{proposal_id, advisor_result, expires_at}`                      |
| `quant_approve`  | `proposal_id`, `size_override_pct?`                                                           | Validates not expired, runs Reactor (D5), advances state to `approved`, appends `executions.jsonl`, appends journal         | `{proposal_id, execution_record, journal_entry_id}`              |
| `quant_reject`   | `proposal_id`, `reason`                                                                       | Advances state to `rejected`, appends settlement journal as human-override lesson                                           | `{proposal_id, status: "rejected"}`                              |
| `quant_pending`  | `limit?`, `symbol?`                                                                           | None (read-only)                                                                                                            | `[{proposal_id, symbol, expires_at, advisor_summary}, ...]`      |
| `quant_proposal` | `proposal_id`                                                                                 | None (read-only)                                                                                                            | Full proposal record incl. final state and (if any) execution    |

`quant_propose` reuses `advisor.recommend()` from ADR-0014 §D2 verbatim; the proposal record embeds the recommend-shape under `proposal.advisor_result` so any consumer downstream of the proposal has the same view the operator saw at propose time. There is **no** second call to `recommend()` at approve time — the proposal IS the snapshot.

### D5: React adapters via Protocol

```python
# hermes_quant/react/base.py
from typing import Protocol

class Reactor(Protocol):
    name: str
    requires_credentials: bool

    def execute(self, proposal: Proposal, fill_size_pct: float) -> ExecutionRecord:
        """Execute the proposal's intended action. Must be idempotent on proposal_id."""
        ...
```

v0.1.2 ships **one** concrete reactor: **`PaperReactor`** synthesizes a fill at the proposal's `decision_price` (the close-at-propose-time embedded in the advisor result), writes an `ExecutionRecord` to `executions.jsonl` with the same shape the daemon's freqtrade consumer would write. The paper book is therefore byte-format-compatible with the autopilot surface — the portfolio reconstruction pipeline (ADR-0011) does not distinguish HITL paper fills from autopilot freqtrade fills, modulo the `human_in_the_loop: True` flag and the `reactor_name: "paper"` discriminator (D6).

v0.2 adds **`AlpacaReactor`** (live equity, requires `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` from the Hermes credential pool per ADR-0013 D4 §2) and **`CcxtReactor`** (live crypto, per-exchange credentials). Live reactors are gated behind explicit `--live` opt-in (D10) **plus** broker credential present in the pool. A live reactor that cannot find its credential raises `ReactorCredentialError` rather than silently falling back to paper — silent fallback is the wrong failure mode for money software.

### D6: `ExecutionRecord` schema

Matches the existing `executions.jsonl` shape from ADR-0009 P0-3, extended with HITL fields:

```python
class ExecutionRecord(BaseModel):
    # ── Identity ─────────────────────────────────────────────────────
    execution_id:        str            # exec_<UTC_iso_seconds>_<symbol>_<random6>
    proposal_id:         str            # links to proposal (HITL) or None (autopilot)
    signal_id:           str            # AggregatedSignal id that drove the proposal/decision

    # ── Asset + timing ──────────────────────────────────────────────
    asset:               str
    asset_class:         Literal["crypto", "equity", "etf", "fx"]
    timeframe:           str
    asof_decision:       datetime       # bar timestamp (MarketContext.asof) at propose
    asof_execution:      datetime       # wall-clock at approve (paper) or broker fill ts (live)

    # ── Sizing + fills ──────────────────────────────────────────────
    target_position_pct: float          # signed, post-gate, post-override
    decision_price:      float          # close at propose time
    fill_price:          float          # decision_price for paper; broker-reported for live
    fill_size_pct:       float          # actual; differs from target only on partial-fill (live)

    # ── Reactor metadata ────────────────────────────────────────────
    reactor_name:        Literal["paper", "alpaca", "ccxt"]
    reactor_metadata:    dict           # broker order id, latency, slippage estimate, etc.

    # ── HITL fields ─────────────────────────────────────────────────
    human_in_the_loop:   bool           # True for HITL, False for autopilot/autonomous
    approver_user_id:    Optional[str]  # Discord user id / CLI user; None for autonomous
```

`ExecutionRecord` is the universal join key between the proposal (HITL), the journal (ADR-0010), and the portfolio reconstruction loader (ADR-0011). HITL mode does not bypass `portfolio_loader`; paper executions feed the same reconstruction pipeline so PnL and drawdown attribution work identically across modes.

### D7: Mode resolution

```yaml
quant:
  pdr:
    mode: advise   # advise | hitl | autonomous
  hitl:
    default_ttl_minutes: 15
  calibration:
    learn_from_rejections: true   # see D8
```

Default is **advise**. Operators must explicitly opt into HITL by setting `quant.pdr.mode: hitl`. Autonomous mode (v0.2) is likewise an explicit opt-in.

`quant_propose` checks the mode at the **tool boundary** and returns a structured error if mode != hitl:

```python
{"error": "mode_mismatch",
 "current_mode": "advise",
 "required_mode": "hitl",
 "remediation": "Set quant.pdr.mode: hitl in config.yaml to enable HITL."}
```

This is a **config error, not a runtime error** — returned synchronously without contacting the data provider. `quant_approve` / `quant_reject` apply the same check plus proposal-state validation. `quant_doctor` (ADR-0007) is extended in v0.1.2 to surface the current mode prominently — the three-mode matrix plus paper/live (v0.2) plus per-profile config plus per-profile broker creds is enough cognitive load that doctor MUST be the operator's confidence-building artifact.

### D8: Calibrator learns from approvals AND rejections

This is the **critical** decision of the ADR. It distinguishes HITL as a LEARNING surface from HITL as a check valve.

**Why approvals alone are not enough:** if the calibrator only learns from realized fills (ADR-0003), then a rejected proposal is invisible to the model. The model continues producing the same kind of proposal the operator consistently rejects, forever. This converts the human from a teacher into a perpetual gate.

**The decision:** when the operator rejects a proposal, the settlement journal records the rejection with `reason`, AND the calibrator's update path treats the rejected proposal AS IF its `realized_outcome = opposite-of-direction` — i.e. the human's verdict is treated as the realized outcome. The calibrator updates its confidence distribution on the kind of pattern the human rejected. After enough rejections of "long AAPL on RSI=38 oversold reversal", the calibrator lowers confidence on that pattern. After enough rejections of any proposal at all, the calibrator's overall confidence distribution shrinks toward the silence-bias prior — exactly the charter's "rewarded for correct inaction" property surfacing through the human as the supervisory signal.

This is the LEARNING property of HITL. Without it, the human is a check valve only, not a teacher.

**CAVEAT:** The rejection-as-realized-outcome update is gated by `quant.calibration.learn_from_rejections: true` (default `true`). Both stances are reasonable:

- **`true` (default)** — human verdicts shape the model. The model learns the operator's risk tolerance over time. Failure mode: an operator who rejects everything trains the model to predict flat-only.
- **`false`** — rejections appear in the journal as context but do not update calibrators; calibrator behaviour is identical to the autopilot surface. Failure mode: the human is a perpetual check valve, the model never adapts to operator preferences.

Default is `true` because the v0.1.2 use case is paper-trading the calibrator into shape during weeks 1-8 of the charter timeline. An operator running HITL indefinitely as a personal trade-review tool may prefer `false`. **Mitigation against the "rejects everything" failure mode:** the calibrator update path enforces a minimum sample size (default: 20 rejections per analyst per direction, configured via `quant.calibration.min_rejection_samples`) before rejection-feedback gates engage; the first few rejections are recorded as journal context only.

### D9: TTL handling

Two-mode sweep:

- **Active sweep** — when the daemon is running, a 60-second cron-style loop in `daemon/hitl_sweep.py` checks `proposals.db` for expired pending proposals and transitions them to `expired`.
- **Lazy sweep** — when the daemon is not running (typical in pure chat-mode HITL), every read-side tool call (`quant_propose`, `quant_pending`, `quant_proposal`) re-checks TTL on the proposals it touches before returning. On any TTL expiry detected during a tool call, the tool advances state and appends to the journal as a side effect of the read.

The lazy path means TTL semantics are best-effort during quiet periods — a proposal whose TTL elapsed at 12:00 may not be marked `expired` until a read at 14:00. For calibrator-update purposes this does not matter (the journal entry's effective timestamp is `created_at + ttl_minutes`, not detection wall-clock); for operator UX, `quant_pending` always shows live TTL state because the tool re-checks before responding.

Expired proposals are journaled with `reason="ttl_elapsed"` and treated identically to rejections for calibrator updates (subject to the D8 gate). Rationale: an expired proposal IS evidence the human did not think the proposal was worth acting on within its valid window — informationally identical to an explicit rejection. The charter's "correct inaction" principle does not distinguish "rejected" from "did not approve in time."

**Belt-and-suspenders:** every read-side lookup of a `pending` proposal re-checks TTL even if the active sweep is running, to protect against the active sweep being delayed.

### D10: Paper-vs-live boundary

**v0.1.2 ships paper React only.** `PaperReactor` is the only concrete `Reactor` registered. Operators cannot run live trades through HITL in v0.1.2 even with broker credentials configured.

**v0.2 introduces live React behind three independent gates:**

1. **Per-call `--live` flag** on `quant_approve` (CLI) or `live=True` keyword arg (tool/slash). The flag is **per-call, never persistent** — operators must explicitly type it every time. This pre-empts the "I forgot I was on live" footgun from session-state-modal interfaces.
2. **Broker credential present** in the Hermes credential pool (ADR-0013 D4 §2). Absent credentials → `ReactorCredentialError`.
3. **Second confirmation prompt** on the CLI (`Confirm live trade of 10% NAV in AAPL? [y/N]`) and on the slash command (a follow-up `/quant confirm <proposal_id>` that must arrive within 60 seconds of the `--live` approve attempt).

Per-call rather than persistent `--live` is the load-bearing design choice. `AGENTS.md`: *"Live trading goes through the CLI ONLY, with explicit confirmation prompts."* Every live trade must be a fresh explicit decision, not a holdover from a previous session's mode flip.

### D11: CLI surface

```
hermes quant propose <SYMBOL>
    [--asset-class equity] [--timeframe 1d] [--lookback 200]
    [--ttl-minutes 15] [--sizing-mode default]

hermes quant approve <PROPOSAL_ID>
    [--size-override <PCT>]
    [--live]                 # deferred to v0.2; rejected at parse time in v0.1.2

hermes quant reject <PROPOSAL_ID> --reason <TEXT>

hermes quant pending [-n 20] [--asset SYMBOL]

hermes quant proposal <PROPOSAL_ID> [--json]
```

Fits under the existing `hermes quant` group from ADR-0007 — no new top-level command, no `--profile` collision (the global `--use-profile` convention from ADR-0013 applies). Default output for `propose` and `proposal` is rich-formatted (panel + table + caveats), matching `hermes quant recommend` from ADR-0014. The `--json` flag on `proposal` returns the raw record for piping.

### D12: Slash command surface

Under the `/quant` multiplexer (ADR-0007):

- `/quant propose AAPL` — emits a chat reply with `proposal_id`, advisor summary (direction / confidence / expected edge / gate decision), and the canonical follow-up prompt: `Approve with /quant approve <id> or reject with /quant reject <id> <reason>. Expires at 2026-05-13T18:57:30Z (15m).`
- `/quant approve <id>` — runs the approve handler, replies with the ExecutionRecord summary.
- `/quant reject <id> <reason>` — runs the reject handler, replies with confirmation and a journal-entry pointer.
- `/quant pending` — lists pending proposals.
- `/quant proposal <id>` — returns the full record.

**Discord button-based UI** (👍 reaction = approve, 👎 reaction = reject) is **deferred to v0.2**. Text-mode is the v0.1.2 baseline so it works on every Hermes-supported platform (Discord, CLI, future adapters). The button UI is a v0.2 nicety, not a v0.1.2 prerequisite — shipping the text path first means platform-portability ships with the feature.

## Consequences

### Positive

- **Real paper-trade book starts populating.** Operators dogfood the v0.1.1 analyst+aggregator+gate stack on actual decisions without configuring freqtrade. This is the artifact the charter §"What I'd build first" calls for; HITL produces it directly.
- **Safe on-ramp before autonomous.** Operators see what the system would do without staking real money or risking unattended bugs. HITL is the canonical pre-flight for v0.2 autonomous mode.
- **Richer calibrator training signal.** The autopilot daemon trains calibrators on realized fills only. HITL adds human verdicts (D8) as a parallel training signal that captures operator preferences, risk-tolerance, and pattern-rejection that pure realized-fill training cannot.
- **Natural audit trail.** Every trade has three on-disk records — proposal (proposals.jsonl), execution (executions.jsonl), journal entry (journal.md). Forensic reconstruction of "why did we trade this?" is a `grep proposal_id` away.
- **Zero-credential bootstrap.** HITL on `yfinance` data + paper React works with `pip install -e '.[yfinance]'` and one config flag — same bootstrap cost as advisor mode.

### Negative

- **Three-mode config matrix increases operator cognitive load.** Operators must understand advise / hitl / autonomous, plus paper / live (v0.2), plus per-profile config, plus per-profile broker credentials. **Mitigation:** `quant_doctor` surfaces current mode prominently (D7); CLI prefixes responses with mode tag (e.g. `[hitl/paper] Proposal prop_2026-05-13T...`).
- **`quant_propose` in advise-mode-config is a config error, not a runtime error.** Could surprise operators who forgot to flip the mode flag. **Mitigation:** doctor catches this; `mode_mismatch` includes a remediation string with the exact config key (D7).
- **Calibrator training on rejections has a "rejects everything" failure mode.** **Mitigation:** D8 is config-gated (`learn_from_rejections`); the rejection-feedback gate enforces a minimum sample size before engaging.
- **TTL semantics race with sweep cadence.** **Mitigation:** every read-side lookup re-checks TTL (D9 belt-and-suspenders); the journal entry's effective timestamp is `created_at + ttl_minutes`, not detection wall-clock.
- **Side-effect-producing tools, contra ADR-0007's "tools = read-only views" rule.** **Mitigation:** the side-effect is gated by an explicit `proposal_id` the human has named; the LLM is not selecting which trade to execute. The two side-effect tools are tagged in the schema with `produces_side_effect: true` so any future Hermes-core capability check (e.g. a `--dry-run` global flag) can degrade them to read-only at the dispatcher layer.

## Cross-references

- **ADR-0013** (integration stance) — HITL surface uses public `PluginContext` only, zero monkeypatches. Tools and CLI subcommands register through `register_tool` / `register_cli_command` exclusively.
- **ADR-0014** (advisor surface) — `quant_propose` reuses `advisor.recommend()` verbatim; the advisor result is embedded under `proposal.advisor_result`. No second call at approve time — the proposal IS the snapshot.
- **ADR-0010** (settlement journal) — approval, rejection, and expiry all append journal entries through `journal.append_pending` / `journal.resolve` (for approvals that go on to settle) and through a new `journal.append_human_override` (for rejections + expiries) using the existing `Reflection` shape from ADR-0010 §6 with `rule_version="human-override-v1"`.
- **ADR-0011** (portfolio reconstruction) — paper executions feed the same reconstruction pipeline as autopilot; HITL mode does not bypass `portfolio_loader`. The `human_in_the_loop` flag on `ExecutionRecord` is informational, not a routing key.
- **ADR-0012** (LLMAnalyst deferred) — the HITL human IS the LLM-equivalent decision gate in v0.1.2; LLMAnalyst lands v0.3.0 as an OPTIONAL parallel approve-or-reject voice (not a replacement for the human). When LLMAnalyst lands, the operator sees both the human approval state and the LLM's parallel verdict; agreement strengthens the calibrator update, disagreement is logged for review.
- **ADR-0009 P0-3** (`executions.jsonl` back-channel) — the `ExecutionRecord` schema in D6 extends ADR-0009's shape with HITL fields (`human_in_the_loop`, `approver_user_id`) but is otherwise byte-format-compatible with daemon-side autopilot fills.
- **Founding charter** ([`docs/charter/2026-05-13-hermes-quant-charter.md`](../charter/2026-05-13-hermes-quant-charter.md)) §"Layer 3: Risk/Execution gate (the 'reaction' silence layer)":
  > *"If the gate says no, the system holds cash. **Silence by default.**"*

  …and §"The PDR mapping is actually clean here":

  > *"Most retail RL trading agents fail because they're rewarded for action. Yours should be **rewarded for correct inaction**."*

  HITL is the operationalization of both clauses at the operator layer: the human is the silence-bias gate, and the calibrator's training signal includes "the human chose silence" as a first-class label.

## Implementation order in v0.1.2

1. **Proposal store** — `proposals.jsonl` + `proposals.db` with the atomic-rename writer, the JSONL→SQLite rebuild path, and the `proposal_id` minter. Gates everything else.
2. **`PaperReactor`** — writes `executions.jsonl` in the ADR-0009 P0-3 shape extended with the D6 HITL fields. Idempotent on `proposal_id`.
3. **Tools, in order**: `quant_propose` → `quant_approve` → `quant_reject` → `quant_pending` → `quant_proposal`. Each ships with its schema in `hermes_quant/tools/schemas.py`.
4. **CLI subcommands** wrapping the tools (D11). The `--live` flag parses but rejects with "deferred to v0.2" in v0.1.2.
5. **Slash multiplexer additions** (D12). Text-mode only; Discord reactions deferred to v0.2.
6. **Doctor extension** (D7) — surface current PDR mode + paper/live indicator + last-tick-or-propose timestamp.

### Test fence (REQUIRED for v0.1.2 release)

`tests/integration/test_hitl_e2e.py` — 12 named tests:

1. `test_propose_creates_pending_proposal`
2. `test_propose_in_advise_mode_returns_mode_mismatch`
3. `test_approve_pending_executes_paper_react`
4. `test_approve_expired_returns_expired_error`
5. `test_approve_already_approved_returns_idempotency_hit`
6. `test_reject_pending_appends_human_override_lesson`
7. `test_ttl_sweep_active_marks_expired`
8. `test_ttl_sweep_lazy_marks_expired_on_next_read`
9. `test_calibrator_learns_from_rejection_when_gated_on`
10. `test_calibrator_ignores_rejection_when_gated_off`
11. `test_audit_trail_completeness_propose_approve_settle`
12. `test_proposal_jsonl_db_index_rebuild_after_corruption`

Tests 7 + 8 cover the TTL race surface in D9. Test 11 walks the full proposal → execution → journal → portfolio_loader → settlement loop and asserts every artifact is reachable from `proposal_id`. Test 12 deletes `proposals.db` mid-run and asserts the JSONL replay rebuilds an identical index.

## Provenance

- **Founding charter** (`docs/charter/2026-05-13-hermes-quant-charter.md`) §"Layer 3" silence-by-default principle and §"PDR mapping" reward-for-correct-inaction principle. Quoted verbatim in Cross-references.
- **ADR-0014** §D2 — `advisor.recommend()` is the embedded snapshot underlying every proposal. HITL is "advisor + write + approve gate."
- **ADR-0010** §6 `Reflection` model — reused for human-override lessons with `rule_version="human-override-v1"`.
- **ADR-0009 P0-3** — `executions.jsonl` back-channel format. HITL paper executions write to the same file with the same shape, enabling the portfolio_loader to read both sources transparently.
- **Eidolon `pdr_lwm/environment.py::AutonomousAgent.run()`** — pattern reference only, **not forked**. The PDR three-mode taxonomy (advise / hitl / autonomous) is hermes-quant-specific; Eidolon's embodiment loop has no human-approval state because the autonomous agent is the only consumer of its perception stream. HITL is the market-domain extension that the embodied case did not need.
