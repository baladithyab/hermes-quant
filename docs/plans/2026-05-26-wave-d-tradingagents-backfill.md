# Wave D Plan — TradingAgents Pattern Backfill

**ADR**: ADR-0038
**Branch**: main (commits push direct; this is a low-risk infrastructure wave)
**Reviewer**: Codex CLI per-track
**Test budget**: 56+ new unit tests
**Constraint**: bit-identical legacy paths when env flags off

## Track decomposition

Three tracks chosen for **non-overlapping edit surface** so they can run
in parallel via `delegate_task` without stepping on each other.

### Track A — CI hygiene + vendor routing (P8, P11, P12)

**Owner**: subagent A (claude-sonnet-4.6 worker, role=leaf)
**Edit surface**:
- `tests/conftest.py` (append fixture; do NOT touch existing
  `_isolate_governance_audit_log`)
- `hermes_quant/data/vendor_routing.py` (NEW)
- `hermes_quant/config/vendor_config.py` (NEW; create the config dir if missing)
- `tests/unit/wave_d/test_autouse_dummy_keys.py` (NEW, 3 tests)
- `tests/unit/wave_d/test_vendor_routing.py` (NEW, 10+ tests including
  `test_vendor_completeness`)
- `tests/unit/wave_d/test_vendor_config.py` (NEW, 8+ tests)

**Out of scope**: do NOT modify `hermes_quant/data/base.py::fetch_with_chain`
— vendor_routing is an additive surface.

**Acceptance**:
- 21+ new tests pass
- `pytest tests/unit/wave_d/` green
- `ruff check tests/unit/wave_d hermes_quant/data/vendor_routing.py
  hermes_quant/config/vendor_config.py` zero new errors

### Track B — Daemon watermark store (P3)

**Owner**: subagent B (claude-sonnet-4.6 worker, role=leaf)
**Edit surface**:
- `hermes_quant/daemon/watermark.py` (NEW; SQLite-backed)
- `hermes_quant/daemon/tick_loop.py` (CALL site only — read watermark
  before analyst loop, write after `signal_bus.emit()` returns)
- `tests/unit/wave_d/test_watermark.py` (NEW, 12+ tests)

**Out of scope**: do NOT change `signal_bus.emit()` signature; do NOT
change the JSONL row shape; do NOT add watermark surface to the journal.

**Profile awareness**: `_resolve_profile_path` mimics whatever
`hermes_quant/daemon/halt_state.py` already does (read it to learn the
pattern; do not re-invent).

**Acceptance**:
- 12+ new tests pass
- `pytest tests/unit/wave_d/test_watermark.py
  tests/unit/daemon/test_tick_loop.py` green
- One integration test demonstrating skip-on-replay
- `ruff` clean on touched files

### Track C — BarSnapshot schema + quant_doctor mirror (P5, P6)

**Owner**: subagent C (claude-sonnet-4.6 worker, role=leaf)
**Edit surface**:
- `hermes_quant/schemas/__init__.py` (NEW package)
- `hermes_quant/schemas/bar_snapshot.py` (NEW)
- `hermes_quant/tools.py::quant_doctor` (replace body with DaemonState
  mirror; keep tool registration unchanged)
- `tests/unit/wave_d/test_bar_snapshot.py` (NEW, 15+ tests)
- `tests/unit/wave_d/test_quant_doctor_mirror.py` (NEW, 8+ tests)

**Hard constraint — JSONL parity**: under
`HERMES_QUANT_SNAPSHOT_V2=0` (default), `BarSnapshot.to_jsonl_row()` MUST
produce a dict that is **deep-equal** to today's emit shape. Track C
includes a parametric parity test that compares the two for each pipeline
stage.

**Out of scope**: do NOT change Analyst / Aggregator / RiskGate Protocol
signatures. BarSnapshot is an internal state model; the public Protocol
contract stays intact.

**Acceptance**:
- 23+ new tests pass
- `quant_doctor` still callable from Hermes' tool layer (registration
  unchanged)
- JSONL parity test green under default env
- `ruff` clean on touched files

## Cross-track invariants

1. **No package import cycles**. Track B imports nothing from C; C imports
   nothing from B. A imports nothing from either.
2. **No new top-level deps**. If a subagent thinks it needs a new package,
   it must STOP and surface the question instead of adding it.
3. **Frozen dataclasses / Pydantic models** wherever practical. Wave D is
   infrastructure; immutability is the default.
4. **Test-first allowed but not required**. We're not in TDD strict mode —
   subagents may write tests after impl, but every PR'd track must include
   the test count specified.
5. **Each track commits independently** to main with a `Wave-D-<letter>`
   prefix in commit message.

## Sync step (after all 3 tracks land)

- Run full test suite from repo root.
- Net new failures must be 0.
- Push all 3 commits to remote.
- Sync any cron-runner scripts to `~/.hermes/scripts/` (no scripts modified
  in Wave D — this is a no-op step but documented for safety).

## Review step (Codex CLI)

After commits land, scatter 3 separate Codex critique subagents — one per
track. Each Codex review focuses on:
- Schema correctness (Pydantic frozen+forbid, watermark PK uniqueness,
  conftest no-leak guarantees).
- Concurrency safety (SQLite WAL, no race in `tick_loop` watermark
  read/write).
- Test coverage completeness (claimed counts vs. actual).

## Cross-model final review

After Codex revisions land: send the cumulative diff to
`anthropic/claude-opus-4.7` (judge tier per ADR-0037) for a final
"does this match ADR-0038" check before declaring Wave D complete.
