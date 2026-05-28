# ADR-0061 — Daily Markdown Report

- **Status:** Accepted
- **Date:** 2026-05-27
- **Supersedes:** none
- **Superseded-By:** none
- **Related:** ADR-0041 (signal_provenance), ADR-0042 (memory & reflection), ADR-0048 (HypothesisRegistry & RunCard), ADR-0055 (FactorOracle), ADR-0059 (unified status CLI)

## Context

After the v0.4-1 status CLI lands, an operator can introspect the system at any moment. They cannot, however, easily produce a **daily summary** suitable for sharing in chat, archiving as evidence, or reviewing offline. Each event store records its own slice of the day; synthesizing a coherent narrative across all six requires custom code per request.

The PRD calls for a daily report. Existing scripts (`quant-daily-interim.py`) create *Proposals* but do not summarize the *day*. We need a single Markdown document, generated on demand or on a cron, covering: gate decisions (approved & rejected with reasons), open positions, P&L, recent reflections, hypothesis-registry deltas, factor verdicts, and outstanding 24h-TTL proposals.

The report must be **publishable** — i.e. trivially deliverable to Telegram or Discord without further formatting work — and **archivable** — i.e. dropped on disk under `~/.hermes/quant/reports/{date}.md` for offline review.

## Decision

Implement `hermes_quant.reporting.daily_report` exposing:

- `DailyReport` dataclass holding all sections (gate_table, positions_table, pnl_today/mtd/ytd, reflections, hypotheses_changes, factor_verdicts_today, open_proposals, summary_lines).
- `generate_daily_report(asof, quant_home)` — pure function; reads from event stores using tail-read semantics; never raises on malformed JSONL (silence-by-default per ADR-0031).
- `format_markdown(report)` — emits a multi-section GitHub-flavoured Markdown document.
- `format_telegram(report, max_chars=3500)` — Markdown-V2 escaped, truncated to ≤ 4096 chars with a `(truncated, see local file)` footer.

CLI: `scripts/quant-daily-report.py` with `--asof`, `--format markdown|telegram|json`, `--out PATH` (default `~/.hermes/quant/reports/{asof}.md`), `--also-print`. Exit 0 always (read-only).

The report is **not** wired to send_message or any delivery surface in this ADR. The cron job that posts daily summaries calls the CLI, captures the output, and delivers it via its preferred channel. This keeps `daily_report` a pure data + formatting module.

## Consequences

### Positive
- Single source of truth for "what happened today": one CLI, one file format.
- Composable with cron: `hermes cronjob create … 'python scripts/quant-daily-report.py --format telegram | telegram-send'`.
- Archivable: file-per-day under `~/.hermes/quant/reports/` provides a sequential record independent of the event stores' own retention.
- Offline-reviewable: Markdown renders correctly in any editor, on GitHub, in chat clients, etc.
- Read-only: cannot mutate any event store or state.db.

### Negative
- Tail-read overhead: each report scans the last 256KB of each event store. At current volumes (<10MB across all stores) this is fast; if the audit log grows unbounded, the tail size may need to grow proportionally. Mitigation: ROLLOUT.md notes audit log rotation as future work.
- Telegram MD-V2 fragility: special characters in user-supplied data (ticker, reasons) must be backslash-escaped. We implement a small `_escape_telegram_md_v2` helper. A regression in the escape table breaks Telegram rendering silently — covered by tests.
- Duplication risk: the gate-summary section overlaps with `quant status`. We keep them separate because their consumers differ: status is real-time (stdout), report is historical (file/chat).

### Neutral
- ~830 LoC module + ~150 LoC CLI + ~790 LoC tests. No new runtime dependencies.

## Alternatives Considered

1. **Real-time web dashboard** — host a small Flask/FastAPI app rendering the same data. Rejected as scope-creep: introduces a process to manage, a port to expose, and authentication concerns. The Markdown report covers 95% of the use case at 5% of the cost.

2. **Email delivery** — embed the report into an SMTP send. Rejected: friction (SMTP credentials, email-client rendering quirks), and the operator already has Hermes-managed Telegram + Discord delivery surfaces.

3. **Inline P&L computation from a price feed** — extend the report to fetch live marks for each open position and compute live unrealized P&L. Rejected for v0.4: requires wiring a live data provider into the reporting pipeline, increasing failure modes. Current report uses cost basis only; live marks are deferred to a future ADR.

4. **Single multi-store SQL view** — build a Postgres/SQLite view that joins all six stores. Rejected: append-only JSONL files are the canonical store and adding a SQL projection layer would be a parallel store to keep in sync. Tail-reading the JSONL is the simplest correct approach.

## References

- ADR-0041 — signal_provenance audit trail (the gate event source)
- ADR-0042 — memory & reflection (decisions.jsonl + reflections.jsonl source)
- ADR-0048 — HypothesisRegistry & RunCard (hypotheses.jsonl + run_cards.jsonl source)
- ADR-0055 — FactorOracle (factor_verdicts.jsonl source)
- ADR-0059 — unified status CLI (sibling read-only surface)
- `hermes_quant/reporting/daily_report.py` — implementation
- `scripts/quant-daily-report.py` — CLI
- `tests/reporting/test_daily_report.py` — 20+ tests
