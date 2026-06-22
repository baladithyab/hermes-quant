# Parity map — hermes-quant theoretical system → cowork-quant

> The question this answers: can the WHOLE theoretical system hermes-quant
> embodies (90 ADRs) be applied to the Cowork version? Verdict: **yes for the
> decision-theoretic core, with one structural substitution** — the always-on
> daemon becomes discrete scheduled turns. Because hermes-quant already chose
> interday-only (ADR-0083: "the edge is multi-day; intraday deferred on a
> measured-edge gate"), the cadence ceiling costs almost nothing theoretical.
> Execution-layer ADRs are permanently out of scope by rail #4 (no execution).

Legend: ✅ shipped (v0.1) · 🔜 planned (version) · 🔁 substituted mechanism ·
❌ WONT_PORT (with reason)

## Perception

| hermes-quant | ADRs | cowork-quant | Status |
|---|---|---|---|
| Daemon tick loop | 0001 | 🔁 Scheduled /watch turns (Cowork scheduled tasks) | ✅ command; user schedules via /schedule |
| Data providers (yfinance/ccxt/alpaca) | 0005, 0017 | yahoo-finance/coingecko MCPs + sandbox yfinance | ✅ |
| asof-honesty, still-forming-bar discipline | 0068, 0069 | bar_ts vs asof_decision in schemas; skills enforce closed-bars | ✅ |
| Scheduled-event calendar + event_risk | 0073, 0084 | event_risk on CommitteeSignal + gate blackout rule | ✅ (calendar seed file 🔜 v0.2) |
| Catalyst ingest (8-K, social) | 0074, 0075, 0076 | catalyst rubric via sec-edgar MCP; social ❌ (recency-gated feeds need a daemon; revisit) | partial |
| Regime classifier (heuristic + HMM) | 0047, 0058, 0063 | heuristic regime read in /brief; HMM 🔜 v0.2 in quantcore | partial |
| PIT universe, microstructure | 0036; charter | ❌ microstructure (no live L2 in Cowork); universe = user watchlist | WONT_PORT |

## Decision

| hermes-quant | ADRs | cowork-quant | Status |
|---|---|---|---|
| Analyst protocol (uniform AnalystView) | 0002 | quantcore.schemas.AnalystView (Pydantic) | ✅ |
| Analyst pool (TA, fundamentals, catalyst, overnight-drift) | 0064, 0089 | rubrics in `analysts` skill (TA/fundamentals/catalyst); overnight-drift 🔜 v0.2 | ✅ core 3 |
| Kronos/Kairos foundation-model analyst | 0018 | 🔜 v0.3 hosted-API FoundationModelAnalyst (kairos-serve /predict or TimeGPT — see 2026-06-09 research) | planned |
| BMA/stacking aggregation | 0003 | 🔁 in-session committee aggregation rules (analysts skill); numeric BMA weights 🔜 v0.2 once calibration data exists | substituted |
| LLM committee turns, structured output | 0037, 0044, 0054 | Claude in-session + Pydantic validation at CLI boundary | ✅ (stronger: no string-grep anywhere) |
| Bull/bear adversarial debate | 0065, 0066 | bear-analyst / bull-analyst agents, engineered dissent | ✅ |
| Three-way risk committee | 0043, 0056 | risk-skeptic agent (book-level, down-size only) | ✅ lean |
| RL aggregator | 0006 | ❌ DO_NOT_BUILD inherited | WONT_PORT |

## Reaction

| hermes-quant | ADRs | cowork-quant | Status |
|---|---|---|---|
| Deterministic risk gate (8 rules) | 0004, 0009 | quantcore.gate rules 0–7 + new degeneracy rule | ✅ |
| ¼-Kelly + discrete ladder | 0004 | quantcore.kelly verbatim; ladder validated in Proposal schema | ✅ |
| HITL propose→decide→react | 0015 | /propose AskUserQuestion flow; fills require recorded approvals | ✅ |
| Autonomous mode (perceive+propose, never execute) | 0016, 0024 | /watch unattended turn: queue-only, 24h expiry | ✅ |
| Paper reactors, broker backends, order lifecycle | 0029, 0039, 0070, 0078, 0088 | ❌ NO execution surface of any kind (rail #4, stricter than hermes) | WONT_PORT |
| Pre-trade admissibility/shortability | 0077 | 🔜 v0.2 admissibility checks in /propose (shortable? tradeable?) | planned |
| Portfolio caps at reactor seam, dynamic Kelly | 0071, 0087 | risk-skeptic agent now; deterministic portfolio-cap rule in gate 🔜 v0.2 | partial |
| Options (Greeks gate, structure table, multi-leg) | 0027, 0028, 0082 | 🔜 v0.3 options-playbook skill + options_gate port | planned |

## Memory / learning / governance

| hermes-quant | ADRs | cowork-quant | Status |
|---|---|---|---|
| Settlement + horizon returns | 0010, 0083 | quantcore.settle | ✅ |
| Ledger authority, replayability | 0011, 0085, 0086 | hash-chained JSONL ledger; portfolio reconstructed | ✅ (hash chain is NEW — user-visible state dir needs tamper evidence) |
| Evidence store + lookahead gate | 0033, 0051 | evidence_ids on views 🔜 v0.2 snapshot store + CI-style lookahead checks | planned |
| Calibration (ECE) per analyst | 0002/0003 | quantcore.settle calibration + /retro shrinkage advice | ✅ |
| Persistent memory + reflection | 0042 | 🔁 quant-state/briefs + retros + Cowork memory | ✅ lean |
| Hypothesis registry + run cards | 0034, 0048 | 🔜 v0.2 hypotheses.jsonl + thesis tracking in /retro | planned |
| Retro loops (daily/weekly/quarterly) | 0026, 0035 | /watch report (daily) + /retro (weekly); quarterly 🔜 | ✅ core |
| Self-evolution, advisory plane only | 0080, 0081 | /retro ADVISORY section; human applies config changes | ✅ posture |
| Governance audit plane | 0031, 0041 | gate_decision events in ledger (provenance = views attached) | ✅ lean |
| Backtest harness, walk-forward, ablation | 0020, 0045, 0090 | 🔜 v0.2+: CPCV + deflated-Sharpe instead of plain walk-forward (per 2026-06-09 SOTA note); leakage-masked eval mode | planned (upgraded) |
| Shadow account counterfactual | 0049 | 🔜 v0.3 shadow ledger (what-if book without the human's rejections) | planned |
| Alpha zoo / factor oracle | 0050, 0055 | ❌ population-scale factor mining needs compute + daemon; revisit only after eval harness | deferred |

## The structural substitution, stated plainly

hermes-quant: `daemon → signal bus → consumer` (continuous).
cowork-quant: `scheduled turn → ledger queue → interactive session` (discrete).

Everything else in the theory — silence-by-default, hard-rules-over-learned-
policy, calibrated confidence, asof-honesty, replayable evidence, advisory-
plane evolution — is cadence-independent and ports intact. The two real
losses are (a) anything requiring continuous market presence (microstructure,
intraday reaction, real-time social ingest) and (b) any execution surface —
and (b) is a feature, not a loss: it is the strongest version of the rail
every reference project broke.

## Wave update — 2026-06-10

Waves 1-3 shipped: event calendar w/ verified 2026 seed (B-01 ✅), portfolio
caps Rule 6.5 (B-02 ✅), regime classifier (B-03 ✅), hypothesis registry +
Brier ledger (B-04 ✅), deterministic BMA aggregation w/ ECE shrinkage
(B-05 ✅ — the "🔜 v0.2 numeric BMA" row above is now shipped), eval harness
CPCV+DSR+PBO (B-06 ✅), dashboard (B-07 ✅). Concurrent review (R1, 22
findings) + fix wave: 3 P0 money-math bugs fixed (short calibration
inversion, unvalidated fill seam, add-counted-as-exit) + halt persistence,
proposal TTL, mark guards, profile rail validation. 168 tests green.

## Refinement update — 2026-06-12 (five-stream re-survey → v0.2 plan)

Re-survey: `docs/research/2026-06-12-r-resurvey-and-refinement.md`. Decisions + specs:
`docs/2026-06-12-v0.2-architecture-refinement.md`. Waves 4–7: `BACKLOG.md`.

**The new literature tightens the charter — it does not move a wall.** The debate-failure
cluster (2601.19921, 2508.17536, 2511.07784, 2602.01011), the determinism subfield
(DFAH 2601.15322, CGAE 2603.15639), the ledger-poisoning attack surface (TradeTrap
2512.02261), and the prompt-constitution-fails result (Institutional AI 2601.11369,
Cohen's d=1.28) are, in aggregate, peer-reviewed arguments *for* our design: deterministic
gate as final authority, confidence×track-record aggregation, engineered dissent, immutable
governance manifest, no LLM-as-gate.

**Reclassifications (previously deferred -> now full build targets, behind flags):**

| Row | Was | Now |
|---|---|---|
| Kronos/Kairos FoundationModelAnalyst | planned v0.3 | **SPEC'D** (arch sec 4.12) — interface-first HTTP client, abstain-on-error, plugin-side rolling-IC kill-switch; operator hosts/pins backend |
| Options (Greeks gate, structure table) | planned v0.3 | **SPEC'D** (arch sec 4.13) — deterministic structure table + Greek caps; multi-leg advisory card; no execution |
| Shadow account counterfactual | planned v0.3 | **SPEC'D** (arch sec 4.14) — pre-HITL gate-sized book vs real/B&H/random; attribution-honest |
| Retro loops (quarterly) | planned | **SPEC'D** (arch sec 4.15) — `/retro --quarterly` advisory-only config proposals |
| Evidence store + lookahead gate | planned v0.2 | **READY** B-39 + leakage-masked eval B-31 (model-weight lookahead, not just data) |
| Backtest harness | planned (upgraded) | + leakage-masked mode (B-31) + alpha-after-attribution (B-36) — returns != skill |

**New rails (arch sec 3):** R9 honesty-over-history (forward-only is the gold standard; masked
replay mandatory) · R10 attribution-before-applause · R11 verify-state-on-load · R12
platform-enforced no-execution (PreToolUse deny-hook).

**Deliberate non-ports reaffirmed:** Lean 4 formal gate (overkill — stay on hypothesis +
invariant-coverage manifest), RL aggregator, any execution surface, microstructure/real-time
social, alpha-zoo population search (keep only QuantaAlpha's hypothesis<->code consistency idea).

**Net parity verdict update:** the decision-theoretic core was already at parity; v0.2 closes
the *honesty* and *governance-artifact* gaps (leakage-masking, attribution, ledger verifier,
manifest+replay, adversarial drills) that the 2026 literature shows are the difference between
honest machinery and honest *numbers*. Execution-layer ADRs remain permanently out of scope by
rail #4 — now also enforced at the platform layer (R12).
