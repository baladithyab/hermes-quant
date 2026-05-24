# ADR-0034: Run Cards

**Status:** Proposed
**Date:** 2026-05-24
**Wave:** A.5 follow-up (port from Vibe-Trading reference)
**Cost:** $0

---

## Context

The 2026-05-24 reference-project synthesis identified Vibe-Trading run cards
as a wholesale-port candidate. The implementation request references this as
CV4; in the current synthesis file the concrete Run Cards item is listed under
U2 and Wave G.5. That item names Run Cards as the artifact hermes-quant needs
for reproducible backtest and research review: one machine-readable JSON file,
one human-readable Markdown file, a deterministic configuration hash, optional
source hashes, warning capture, validation results, and an artifact manifest
with SHA-256 hashes.

That shape fits hermes-quant's current architecture. ADR-0001 requires the
sidecar daemon to be replayable from disk. ADR-0020 defines the backtest
harness as the way to run historical bars through the same decision surface
used by the daemon. ADR-0031, ADR-0032, and ADR-0033 add the governance audit
log, the trading flow contract, and evidence records. Together these ADRs need
a compact per-run record that can answer: what configuration did this run use,
what data was consumed, what validation result was observed, which artifacts
were written, and which governance or evidence records can be followed for
audit.

Vibe-Trading's `agent/backtest/run_card.py` is a small MIT-licensed module and
already implements most of this surface in about 200 lines. The port is
therefore narrower and safer than designing a new artifact format. The schema
is forked at import time because hermes-quant owns different forward
compatibility hooks from this point onward.

## Decision

Port the module into `hermes_quant/runs/run_card.py` with the original MIT
attribution preserved at the top of the file and the complete license text
copied to `LICENSES/MIT-Vibe-Trading.txt`.

The hermes-quant fork uses `SCHEMA_VERSION = "0.2"`. It writes run output
under the cross-process state root `~/.hermes/quant/runs/<run_id>/`, resolved
with `pathlib.Path.home() / ".hermes" / "quant"` unless a test-only
`quant_home` override is supplied. The public function remains intentionally
small: `write_run_card()` takes a run id, configuration mapping, metrics
mapping, optional data source names, optional strategy path, optional warnings,
and the hermes-specific audit hooks.

The JSON payload includes the upstream-compatible core fields:
`schema_version`, `generated_at`, run path, backtest summary,
`reproducibility.config_hash`, optional `strategy_hash`, `data_sources`,
scalar `metrics`, optional `validation`, `warnings`, and an artifact manifest.
The Markdown payload mirrors those sections for operator review and includes a
dedicated `## Reproducibility` section.

The hermes-owned fields are:

- `evidence_ids: list[str]`, defaulting to an empty list, for ADR-0033 Evidence
  Store linkage.
- `flow_name: str | None`, defaulting to `None`, for ADR-0032 Trading Flow
  Contract linkage.
- `governance_audit_log_offset: int | None`, defaulting to `None`, for
  ADR-0031 governance-plane audit walkback.

The port removes Vibe-Trading package assumptions and any reference to its
internal `agent.backtest` layout. Artifact discovery is local to the resolved
run directory and focuses on hermes run outputs plus an optional `artifacts/`
subdirectory. The module does not spawn daemons, place trades, open network
connections, or mutate Hermes core state. It only writes reproducibility
artifacts for the run id it was given.

## Cross-References

- ADR-0001: sidecar architecture and disk replay requirement.
- ADR-0020: backtest harness, which should emit run cards for replayed runs.
- ADR-0031: governance audit log; run cards carry the audit log offset.
- ADR-0032: trading flow contract; run cards carry the flow name.
- ADR-0033: evidence store; run cards carry evidence ids.
- `docs/architecture/2026-05-24-reference-project-synthesis.md`: identifies
  Run Cards as the Vibe-Trading wholesale-port candidate.

## Test Plan

The initial test file is `tests/runs/test_run_card.py` and covers the
minimum contract needed before wiring run cards into the backtest harness:

1. `test_write_run_card_round_trip_on_tmp_path` writes a run card under a
   temporary hermes-quant home, reloads `run_card.json`, and asserts the JSON
   round-trips to the returned payload.
2. `test_schema_version_is_0_2` asserts the forked schema version is `0.2`.
3. `test_evidence_ids_defaults_to_empty_json_array` asserts the JSON payload
   includes `evidence_ids` as an empty list when no evidence is supplied.
4. `test_config_hash_is_deterministic_for_same_config` asserts two runs with
   the same configuration produce the same deterministic `config_hash`.
5. `test_json_markdown_and_reproducibility_section_are_written` asserts both
   `run_card.json` and `run_card.md` exist and the Markdown includes
   `## Reproducibility`.

## Consequences

Run cards become the per-run audit envelope for research and backtests without
adding a service dependency or LLM call. They also provide a stable target for
later ADR-0031, ADR-0032, and ADR-0033 integrations. The main near-term gap is
that the backtest harness must still call `write_run_card()` at the end of a
run; this ADR only lands the artifact contract and its basic tests.
