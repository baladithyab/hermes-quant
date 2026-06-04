# INCIDENT — Advisor-layer leverage runaway (41.6× gross paper book)

**Date discovered:** 2026-06-02
**Severity:** High (paper-only; no real-money exposure, but invalidated the entire `paper-default` book + the system's own P&L accounting)
**Status:** Resolved — book reset, advisor cap gate shipped, advisor crons armed.

---

## Summary

The hermes-quant `paper-default` simulated book accumulated to **41.6× gross / 32.4× net-short exposure** (4,160% gross, 3,240% net short) on a nominal $100k paper account — a book no real broker would permit (Alpaca paper caps at 2× margin). Marked-to-market against live yfinance closes, the book showed **−$30,657 unrealized**. The system's own ledger (`state.db.cash.equity_total = $112,117`) disagreed because it **never marks open positions** — it only tracks reconstructed cash, so it believed the book was +$12k.

The −$30k was a **leverage artifact**, not a realized loss: −0.66% of the $4.66M gross notional, amplified ~32× by net-short exposure into a rising tape. A properly-capped (2× gross) version of the same directional bet would have shown ≈ −$1.5k.

## Root cause

Three trade-firing layers exist (advisor / playbook / autonomous-tick). **Only the autonomous-tick layer read `HERMES_QUANT_PORTFOLIO_CAPS` and clipped fires against running headroom** (ADR-0071, PR #20). The **advisor layer** (`quant-daily-interim.py`, fired by the premarket / midday / EOD crons under `HERMES_QUANT_AUTONOMY=paper`) had **no portfolio-cap call at all** — its auto-fire loop approved every actionable at ±20% NAV with zero portfolio awareness.

Fill volume by day on the advisor layer: 111 fills (5/28), 54 (5/29), 7 (6/1), 40 (6/2) = 205 uncapped ±20% positions stacked over 5 sessions. The autonomous-tick wrapper's own comment even predicted "~880% gross" and fail-closed; the advisor layer blew past that to 4,160% because it never checked headroom.

Contributing: the `quant_status` tool reported a phantom `operator_emergency_stop` halt (reading the stale May-13 `signals.jsonl` fixture, not the canonical `state.db.halts` table — all halts there carry `cleared_at`). This masked the real issue behind a non-existent halt.

## Remediation (2026-06-02)

1. **Book reset** — wiped `paper-default` positions (69), reset cash to flat $100k, cleared `processed_fills` (670) + `executions_replayed`, truncated `executions.jsonl`. Corrupt state backed up: `state.db.bak-RESET-20260602T165347Z`, `executions.jsonl.bak-RESET-*`, `proposals.db.bak-RESET-*`.

2. **Advisor cap gate** — `auto_approve_actionables()` in `quant-daily-interim.py` now clips each fire via `hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom` against running headroom under `PortfolioCaps.standard()` (200% gross / 100% net / 20% cash), seeded from the live `state.db` book. Gated behind `HERMES_QUANT_PORTFOLIO_CAPS=1` (default-OFF preserves prior behavior; fail-OPEN with loud `_cap_warn` if init fails). Verified: 12 shorts at −0.2 → 4 fire (cash reserve binds at 80% gross), 8 silenced `headroom_breached`.

3. **Crons armed** — added `HERMES_QUANT_PORTFOLIO_CAPS=1` to the step-1 command of `quant-daily-premarket-interim`, `quant-daily-midday-interim`, `quant-daily-eod-interim` (`~/.hermes/cron/jobs.json`, backed up `jobs.json.bak-CAPFIX-*`).

## Follow-ups (not yet done)

- **`equity_total` never marks-to-market.** The system's P&L accounting is cash-only and silently wrong on any open book. ADR-worthy: add a mark-to-market pass to the portfolio-state reconstruction so `equity_total` reflects current marks.
- **`quant_status` reads the stale signal-bus for halt state** instead of `state.db.halts`. Fix the tool to read the canonical table.
- **Playbook + hourly layers** should be audited for the same cap-gate gap (autonomous-tick has it; advisor now has it; playbook/hourly unverified).
- **Deployed-script drift**: the fix lives in the deployed `~/.hermes/scripts/quant-daily-interim.py` (not git-tracked). The reusable clip logic is already in-repo (`hermes_quant.risk.portfolio_normalize`); the deployed script is a thin caller. Vendor the live ops scripts back into the repo (Issue #23).

## Lesson

A risk control that only one of N firing layers respects is not a risk control. ADR-0071's caps were correct and tested — they were just never wired into the layer doing the most firing. When a safety gate ships behind an env flag, audit **every** call site that should honor it, not just the one in the PR.

## Follow-up status (deep-work loop, 2026-06-02)

A vision-driven deep-work loop ran the post-incident architecture pass. Outcomes:

**Shipped this session (committed):**
- **ADR-0086 Phase 1 — read-time mark-to-market.** `PortfolioState.get_marked_equity(account, mark_prices)` computes honest signed MTM equity on demand (the −$30k-reported-as-+$12k bug is now a regression-locked test). `cli/status.py` relabels `state.db.equity_total` as cost-basis (not MTM) so the status tool can never again present it as live P&L. No schema change, firing/cap path untouched.
- **ADR-0087 — centralized portfolio cap at the `PaperReactor.execute()` seam**, default-OFF behind `HERMES_QUANT_PORTFOLIO_CAPS`, mirroring the admissibility-reject precedent. When armed, ALL four firing layers inherit the cap by construction — the structural fix for "a layer forgot the cap."
- **Found-bug fixes:** two stale admissibility tests (root-caused to commit `72e3d8b` plumbing `available_bp` into the autonomous seam only) updated; Phase-8 review hardening (account-resolution in the cap seam instead of hardcoded `paper-default`; `nav_ref<=0` guard).

**Found-bug logged, NOT fixed (out of MTM/cap scope — ADR-0077/0079 domain):**
- **Seam divergence:** commit `72e3d8b` plumbed live buying-power (`available_bp`) into the AUTONOMOUS firing seam but NOT the PaperReactor seam (`gate_order.py:144` documents bp as "not cheaply available at the paper seam"). The two seams that are supposed to agree now diverge on a short's admissibility when bp is the deciding factor. Test `test_autonomous_and_reactor_seams_produce_identical_verdict` was rewritten to PIN the divergence (visible, not silently green) with a FINDING docstring. Restoring parity = plumb live bp into the reactor seam too.

**Deferred (ADR-0086 Phase 2, gated by `docs/research/2026-06-02-premortem.md`):**
- Full share-quantity + dollar-accounting migration (`executions.jsonl` gains signed `qty`; positions in shares; cash in real dollars; the cash-delta dimensional bug and `abs()`-short-sign bug fixed at the write path). Pre-mortem flagged 5 failure modes (NAV_at_fill bootstrapping circularity, share-migration blast radius across ≥4 consumers, reconcile divergence) that make it a 3-4 wave arc in its own session, not safe to half-ship during incident cleanup.
- The `quant_status` stale-halt read was already fixed in the repo (ADR-0085 #49, commit `0cd16e7`) — the live tool was just running a stale venv; reinstalled via `uv pip install -e .` this session.
- The per-layer cap clips (autonomous in-package, advisor in the deployed script) remain in place; they will be removed in favor of the reactor seam when `HERMES_QUANT_PORTFOLIO_CAPS` is flipped on for the seam — sequenced with the Phase-2 share migration because the cap reads weights and Phase 2 changes weight units.
