# Contributing to hermes-quant

## Setup

You need Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/baladithyab/hermes-quant.git
cd hermes-quant
uv venv
source venv/bin/activate
uv pip install -e '.[test]'
```

The `[test]` extras group bundles every optional dependency the test
suite touches (`stacking`, `ccxt`, `alpaca`, `yfinance`, `backtest`,
plus `pytest`/`pytest-asyncio`/`pytest-mock`/`pytest-xdist` and `pyarrow`).
It is the canonical "set up a clean dev environment" command.

## Running tests

```bash
# Full suite (~2-3 min, requires the [test] extras above)
pytest --ignore=tests/unit/wave_d -q

# Single file or test
pytest tests/unit/test_bootstrap_calibrator.py -q
pytest tests/unit/test_research_debate_wiring.py::test_t4_run_research_manager_judge_happy_path -q

# v0.4 verification harness (16 executable checks tracing every shipped
# fix to a live verification step)
bash scripts/v0.4-verify-end-to-end.sh
```

CI runs the full suite + v0.4 harness on every PR via
`.github/workflows/pytest.yml`. Green CI is required to merge.

## Known test-pollution caveats

Six tests pass in isolation but fail in full-suite combinatorial runs
because some module-level state leaks between tests:

- `tests/unit/test_bootstrap_calibrator.py::*` (4 tests)
- `tests/unit/test_llm_committee_caller.py::test_deterministic_aggregator_consumes_emitted_turns`
- `tests/unit/test_research_debate_wiring.py::test_t4_run_research_manager_judge_happy_path`

Production code is correct in every case (verified by isolation runs).
The combinatorial-bisect work is tracked for v0.7+ when CI surfaces
which test-ordering combinations are unstable.

## Submitting changes

1. Branch from `main`: `git checkout -b feat/<topic>` (or `fix/`, `docs/`, etc.)
2. Use conventional-commit messages: `feat(<scope>): ...`, `fix(<scope>): ...`
3. Open a PR; CI runs automatically
4. Wait for green CI before merging
5. Squash-merge into main via `gh pr merge <N> --squash --delete-branch`

Architecture decisions live in `docs/adr/`. Substantial changes need
an ADR; see existing ADRs for format.
