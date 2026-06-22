# cowork-quant

> Claude Cowork plugin: multi-analyst PDR (Perception→Decision→Reaction)
> trading advisor. Sibling of [hermes-quant](https://github.com/baladithyab/hermes-quant)
> — same charter, same rails, rebuilt lean for Claude Cowork/desktop.
> **Advisor + paper-ledger only. This plugin never places orders or moves money.**

## Design in one paragraph

Claude runs the analyst committee in-session (TA, fundamentals, catalyst,
debate subagents) and emits structured `AnalystView`s. A small deterministic
Python package (`scripts/quantcore/`) — ported math from hermes-quant — does
everything that touches money-adjacent state: committee aggregation
(calibration-weighted BMA), risk gate (¼-Kelly cap, cost gate, breakers,
portfolio caps, event blackout), discrete sizing ladder {0, ±0.05, ±0.10,
±0.15, ±0.20} of NAV, hash-chained paper ledger, settlement, calibration,
hypothesis registry, and an eval harness (CPCV + deflated Sharpe + PBO).
The human executes trades in their own broker and confirms fills back.
Silence by default.

## Status

v0.1.0 (unreleased): 168 tests green. See CHANGELOG.md, BACKLOG.md, and the
plan at `hermes-quant/docs/plans/2026-06-09-cowork-quant-submodule-plan.md`.

## Layout

```
.claude-plugin/plugin.json   manifest
commands/                    /brief /scan /propose /settle /status /doctor
                             /watch (unattended turn) /retro /schedule /dashboard
skills/                      quant-core, analysts
agents/                      bull-analyst, bear-analyst, risk-skeptic
scripts/quantcore/           deterministic core: gate, kelly, aggregate, ledger,
                             settle, calendar_events, regime, hypotheses, evalx
assets/                      dashboard_template.html (self-contained, no CDN)
docs/PARITY.md               hermes-quant theoretical system -> cowork-quant map
docs/reviews/                review-team findings (R1...)
docs/research/               inspiration-corpus research notes
BACKLOG.md                   live tracker (seeds-style)
```

## Autonomous cadence (scheduled actions)

`/schedule` sets up Cowork scheduled tasks running `/watch` — the unattended
PDR turn (hermes-quant ADR-0016/0024 autonomous mode): settle, mark, scan the
watchlist, run the debate agents, and QUEUE gate-approved proposals in the
ledger. Nothing enters the book until the human reviews interactively;
the proposal TTL (24h) is enforced deterministically at the CLI — stale
approvals are refused outright. Scheduled turns never approve, fill,
resume halts, or edit config.

## Rails (non-negotiable)

1. Silence by default — disagreement or stale data → no proposal.
2. Hard rules over LLM judgment — the gate's output is final.
3. Discrete sizing ladder, enforced at config, gate, proposal AND fill seams.
4. No order execution, ever — stricter than hermes-quant.
5. Asof-honesty — decision-time stamped; no still-forming bars.
6. Structured output only — Pydantic-validated before entering the ledger.
7. Append-only hash-chained ledgers with integrity verification.
8. New capabilities default-OFF until measured (risk-tightening rules may
   default ON).

## License

Apache-2.0.
