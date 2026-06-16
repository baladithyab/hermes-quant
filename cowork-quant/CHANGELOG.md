# Changelog — cowork-quant

## [0.2.0] — 2026-06-12 (in progress)

### Added — Wave 4: honesty + integrity foundation (2026-06-12)
- **Ledger integrity verifier** (B-30, `quantcore.verify_ledger`): recomputes the
  hash chain + cross-module consistency (every fill traces to a prior *approved*
  proposal; every open position is justified) — closes TradeTrap's ledger-poisoning
  surface (2512.02261). Honest threat model: hash chain detects truncation/edits, not
  a forward-recomputing appender; the cross-module check is the real defense.
- **Leakage-masked eval mode** (B-31, `quantcore.mask`): per-episode reshuffled
  ticker→alias / date→day-index codec, un-mask-on-query/re-mask-on-return tool shim,
  and a deterministic baseline-aware de-anonymization probe (KTD-Fin 2605.28359). Rail
  R9: masked replay is the only sanctioned historical-eval mode.
- **Gate-config manifest + determinism-replay** (B-32, `quantcore.manifest` +
  `quantcore.replay`): SHA-256 digest of the immutable governance regime stamped into
  the ledger; byte-identical replay of any gate decision from stored inputs
  (Institutional-AI 2601.11369, DFAH 2601.15322). Canonical JSON kills float-drift.
- **Platform no-execution rails** (B-33, `quantcore.exec_guard` + `hooks/`): a
  PreToolUse deny-hook blocks any order/transfer tool regardless of prompt (rail R12,
  platform-enforced rail #4), and denies approve/fill/resume CLI verbs + AskUserQuestion
  in unattended context; `/watch` gains `disallowed-tools: AskUserQuestion` (fail-closed,
  no self-approval). plugin.json → 0.2.0, registers hooks.

### Fixed — concurrent review team (2026-06-12, found + fixed same day)
- **mask** day-index substring collision (`day_1` corrupted `day_10`+) — zero-padded
  both alias namespaces + longest-first unmask.
- **mask** ticker regex over-matched prose (`ON`/`ALL`/`A`) — now matches only
  universe-membership tickers via exact alternation.
- **exec_guard/hooks** the deny-hook matcher was not a strict superset of the predicate
  — broadened matcher + added `exercise`/`assign`/`execute_*_order` verbs (critical
  before options land).
- **replay** did not verify the stored manifest digest — `assert_replayable` now refuses
  a config that disagrees with its stamped governance regime.
- **verify_ledger** missed a poisoning path — a spurious `resume` (lifting no active
  halt, e.g. to clear a breaker) is now detected.
- **manifest** now pins CODE (`code_version` + `kelly_formula_version`), not just config.

### Tests
212 tests green (Python 3.10) — 168 prior + Wave-4 + review-fix regressions (mask
round-trip/reshuffle/probe/many-day/ticker-membership, ledger verifier orphan/
unapproved/tamper/spurious-resume, manifest digest + canonical determinism + code-pin,
gate-decision replay + tamper-fails-replay + digest-mismatch-rejected, exec-guard
deny/allow matrix incl. exercise/assign).

## [0.1.0] — 2026-06-10 (unreleased)

### Added — core (waves 1-3, 2026-06-10)
- **Event calendar** (B-01, ADR-0084 C1/C2 port): `quantcore.calendar_events`
  + verified 2026 macro seed (8 FOMC decision days, 12 CPI, 12 NFP releases —
  federalreserve.gov / bls.gov, cited in the seed), asof-honest
  `announced_at <= asof` filtering, freshness warnings, `events` CLI.
- **Portfolio caps** (B-02, ADR-0071/0087 port): gate Rule 6.5 — gross-exposure
  cap + max-concurrent-positions, REJECT-only at the single seam, never blocks
  de-risking, never resizes. Default ON (risk-tightening).
- **Regime classifier** (B-03, ADR-0047/0058/0063 port): deterministic
  trend/vol-percentile heuristic incl. hermes 7eb148a NaN-fail-open and
  1b4ee61 dead-zone fixes; `regime` CLI.
- **Hypothesis registry** (B-04, ADR-0048 port): hash-chained
  hypotheses.jsonl, Brier-scored forecast ledger, `hyp` CLI
  (create/forecast/resolve/status/link/summary).
- **Deterministic committee aggregation** (B-05, ADR-0003 port): Beta-binomial
  accuracy weights from calibration, ECE shrinkage, margin rule, unanimity
  bonus, verbatim dissent capture, honest cold-start fallback; `aggregate`
  CLI replaces all in-prompt aggregation arithmetic.
- **Eval harness v0** (B-06): CPCV splits (purge+embargo), probabilistic +
  deflated Sharpe, PBO via CSCV — stdlib-only, formulas cited (Bailey &
  Lopez de Prado 2012/2014/2015). Upgrade over hermes-quant's plain
  walk-forward per the 2026-06-09 SOTA research note.
- **Dashboard** (B-07): `/dashboard` command + self-contained HTML template
  (no CDN, dark-mode, NAV sparkline, book/queue/settles/calibration tables).

### Fixed — R1 review findings (concurrent review team, 2026-06-10)
- **P0 R1-01**: per-analyst calibration judged shorts on direction-adjusted
  P&L, inverting every short tally — now judged on the raw price move; zero
  moves excluded.
- **P0 R1-02**: `fill` was an unvalidated seam — now refuses double-fills,
  asset mismatches, off-ladder sizes, direction flips, and any size above the
  approved target (humans size down or flatten, never up).
- **P0 R1-03**: a later INCREASING fill no longer settles an entry (adds are
  not exits); only reducing/flattening/sign-flip fills close a position.
- **P1 R1-04**: flatten_halt verdicts are now persisted as ledger halt events
  (breakers were dead-on-replay); new HUMAN-ONLY `resume` verb.
- **P1 R1-05**: deterministic 24h proposal TTL — stale approvals refused at
  the CLI, `expire` verb sweeps stale pendings.
- **P1 R1-06** (lean): mark continuity guards (price ±50%, NAV ±30%,
  `--allow-jump` for documented discontinuities).
- **P1 R1-07**: `aggressive` profile emitted off-ladder targets — profiles now
  rail-validated at the config boundary (max 0.20, step 0.05 immutable).

### Added — plugin surface (2026-06-09)
- quantcore deterministic package: gate rules 0–7 (+0.5 degeneracy, +6.5
  caps), exact-formula Kelly (verbatim hermes-quant port), hash-chained
  append-only ledger with portfolio reconstruction, settlement + calibration.
- Commands: `/scan /propose /brief /settle /status /doctor /watch /retro
  /schedule /dashboard`; skills `quant-core`, `analysts`; agents
  bear-analyst / bull-analyst / risk-skeptic; keyless `.mcp.json`
  (yahoo-finance, coingecko, sec-edgar).
- Scheduled-action layer: `/watch` unattended turn (queue-only, never
  approves/fills), `/schedule` cadence setup, `docs/PARITY.md` full
  theoretical-system map.
- Research notes (2026-06-09): foundation models, LLM trading framework
  deltas, SOTA scan.

### Port findings
- hermes-quant `risk/kelly.py` docstring examples are first-order values that
  never matched its exact-formula implementation (doctests never executed);
  quantcore tests assert the TRUE exact-formula values.

### Test suite
168 tests green (Python 3.10): gate invariants incl. hypothesis property
tests (cap/ladder/breaker/gross-exposure), ledger chain integrity, settle
math incl. short-side calibration, CLI guard refusal paths, calendar
asof-honesty, regime determinism, hypothesis Brier math, aggregation
weights, CPCV/DSR/PBO known values.
