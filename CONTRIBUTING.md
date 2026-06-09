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

### Optional: the Kronos foundation-model analyst

`KronosAnalyst` is **lazy-loaded and abstains gracefully** when its
dependencies are absent, so the `[test]` setup above is sufficient for
the full suite — Kronos-requiring tests skip cleanly, and the analyst
emits zero-confidence abstain views at runtime rather than erroring.

If you want Kronos *active* (it runs the real model and its tests stop
skipping), be aware of a packaging gotcha:

- The `[kronos]` pip extra installs only the **runtime deps**
  (`torch`, `transformers`, `huggingface_hub`, `einops`, `safetensors`).
- It does **NOT** install the `kronos` Python package itself. Upstream
  [`shiyu-coder/Kronos`](https://github.com/shiyu-coder/Kronos) ships **no
  `pyproject.toml`/`setup.py`**, so `kronos @ git+https://...` cannot be
  pip-installed — the build fails with "does not appear to be a Python
  project". The analyst imports both `from kronos import ...` and
  `from model.kronos import ...`, neither of which the bare repo provides
  on `sys.path`.

The working install is a small local **shim** that adds packaging +
re-export over a clone of upstream:

```bash
# 1. runtime deps (CUDA build auto-selected on a GPU box; CPU build otherwise)
uv pip install --python venv/bin/python \
  torch transformers huggingface_hub einops safetensors tqdm

# 2. the kronos package via the local shim clone
#    (clone of shiyu-coder/Kronos + a pyproject.toml that exposes
#     `kronos` and `model` packages — see ~/.local-src/kronos-upstream/)
uv pip install --python venv/bin/python -e <path-to-kronos-shim>

# 3. verify both import paths the analyst uses
venv/bin/python -c "import kronos; from model.kronos import Kronos, KronosPredictor, KronosTokenizer; print('ok')"
```

Weights load offline from a local mirror (`Kronos-base` +
`Kronos-Tokenizer-base`) when `KronosConfig.weights_dir` points at it and
`HF_HUB_OFFLINE=1` is set; otherwise they download from HF Hub
(`NeoQuasar/Kronos-*`) on first use.

> CI does **not** install Kronos (it installs `.[test]` only), so the 14
> Kronos analyst tests skip on CI by design — they are not a merge gate.
> Most of them run via a `_predictor_factory` test seam and don't need the
> real model anyway; installing the package just flips one
> missing-package abstain test from run → skip.

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
