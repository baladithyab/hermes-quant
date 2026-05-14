# v0.3 test-quality review

Reviewer: subagent (test-quality lens)
Scope: tests/unit/test_ccxt_provider.py, test_kronos_analyst.py, test_evaluation.py,
test_autonomous_cron_writer.py + skim of impl. Charter: silence > action.

## Verdict: MERGE_WITH_FOLLOWUPS

The new test files are **broadly solid** — silence paths *are* covered (Kronos
abstain × 4, ccxt DataQualityError × 2, autonomous cron-failure-graceful × 1),
the as_of lookahead filter has tight ≤-vs-< boundary tests, and the cron
writer mocks branch on returncode/timeout/OSError. **No P0 blockers.**

But two real gaps: (a) `tests/test_no_lookahead.py` — the named CI gate —
**still uses inline scaffolding, not `shuffle_timestamps_test()`** (wave-D
follow-up not done), and (b) several "test the test" assertions in
test_evaluation.py are vacuous: they verify result-shape but not the
falsifying claim ("would this catch a lookahead-buggy analyst?"). Ship v0.3,
file the followups, don't backslide.

---

## P0 — critical missing coverage

### P0-1 — `tests/test_no_lookahead.py` does NOT use `shuffle_timestamps_test`
**file**: `tests/test_no_lookahead.py:24-238`
The file's docstring promises CI-gate enforcement of the no-lookahead
invariant via the shuffle technique, but the body imports
`hermes_quant.advisor.recommend` + `_RecordingProvider` and runs
slice-equivalence checks. It never calls the new
`evaluation.lookahead.shuffle_timestamps_test`. Per the original task,
this is the wave-D follow-up that wasn't completed. The new
`shuffle_timestamps_test` IS unit-tested in `test_evaluation.py` (✓) but
is NOT wired into the gate that actually blocks releases.
**suggested**: add a `tests/test_no_lookahead.py::test_shipped_analysts_pass_shuffle_test`
that iterates over ClassicalTAAnalyst, MicrostructureLite, KronosAnalyst,
runs each through `shuffle_timestamps_test` with a directional-accuracy
score_fn, and asserts `result.passed` at α=0.05.

### P0-2 — `test_lookahead_real_signal_passes` is vacuous
**file**: `tests/unit/test_evaluation.py:143-163`
Asserts only `result.real_score in {-1.0, 0.0, 1.0}` and
`len(result.shuffled_scores) == 10`. Does NOT assert
`result.passed` is True for the (no-leak) momentum scorer. The doctring
even hedges: "depends on whether shuffled scores cluster around real or
away" — meaning the test is shape-only. A truly broken
`shuffle_timestamps_test` returning constant `passed=False` would still
pass this assertion.
**suggested**: split into two tests:
  (a) `test_lookahead_caught_by_buggy_analyst`: build a `def cheater(bars): return float(bars["timestamp"].iloc[-1].value)` scorer that
      returns sorted-timestamp-dependent value; assert
      `result.passed is False` (or that p-value rejects the null).
  (b) `test_lookahead_passed_by_clean_analyst`: ensure a clean scorer
      that uses ONLY OHLC (not order/time) yields `result.passed is True`.
This is the only way to verify the test would actually CATCH a buggy
analyst.

### P0-3 — KronosAnalyst production weight-load failure path untested
**missing_test_for**: `hermes_quant/analysts/kronos.py:245-249`
The production `_lazy_load` catches `(OSError, RuntimeError, ValueError)`
from `KronosTokenizer.from_pretrained` / `Kronos.from_pretrained` and sets
`_abstain_reason = "weight_load_failed: ..."`. Tests only exercise the
test-seam factory path (`_predictor_factory`). If the real
`from_pretrained` raises a non-listed exception class (e.g.
`huggingface_hub.HfHubHTTPError`, `transformers.utils.HfFolder` errors —
neither is OSError/RuntimeError/ValueError), the abstain path won't trip,
and the analyst will fail loudly at the next .analyze() call instead of
silencing. This is a charter-violation latent bug.
**suggested**: monkeypatch `kronos.Kronos.from_pretrained` to raise
`HfHubHTTPError` (or any subclass of `Exception` that is NOT in the
caught tuple), call `analyze()`, assert returns
zero-confidence abstain and `_abstain_reason` is set. OR widen the
caught tuple to `Exception` in production code.

---

## P1 — useful follow-up

### P1-1 — CcxtProvider: no test for duplicate timestamp in raw exchange response
**missing_test_for**: `hermes_quant/data/ccxt_provider.py:192` (validate_bars
dedupes, but exchange-side dup is not tested as a path)
Exchanges occasionally return duplicate kline timestamps after a partial
recovery from outage. `validate_bars(dedupe_timestamp=True)` handles it
but no test exercises FakeExchange returning `[[ts, ...], [ts, ...], [ts2, ...]]`
and asserting result has unique timestamps.
**suggested**: extend FakeExchange with duplicate rows; assert
`len(result["timestamp"].unique()) == len(result)`.

### P1-2 — CcxtProvider: no test for negative-volume row drop
**missing_test_for**: `hermes_quant/data/base.py:85` (`out[out["volume"] > 0]`)
Halted-ticker pattern is tested via `validate_bars` unit tests (presumably)
but NOT through the ccxt path. A bar with negative volume (some exchanges
emit `-1` as a halt sentinel) should be silently dropped.
**suggested**: FakeExchange with one negative-volume row; verify it's
filtered before as_of test.

### P1-3 — CcxtProvider: pagination only tested single-page
**file**: `tests/unit/test_ccxt_provider.py:303-320` is partial-page
termination only.
The multi-page since-monotonicity loop (`new_since = chunk[-1][0] + 1`,
loop continues until `new_since >= as_of_ms` OR `< 1000` rows) is not
exercised. A bug in `cur_since = new_since` could produce infinite loop
(bounded by `max_iters=20`) and tests wouldn't detect it.
**suggested**: FakeExchange that holds 2500 rows; `lookback_bars=2000`;
assert `len(fake.calls) >= 2` and result has all expected bars.

### P1-4 — CcxtProvider: no test for ExchangeError (auth/bad-request) mapping
**file**: `tests/unit/test_ccxt_provider.py` (no test)
`_fetch_with_retry` maps `ExchangeError` (non-RateLimit, non-BadSymbol)
to fatal `DataProviderError`. Untested.
**suggested**: `fake.raise_on_call = [_ccxt.AuthenticationError("bad key")]`;
assert raises `DataProviderError` and `len(fake.calls) == 1`.

### P1-5 — KronosAnalyst: NaN-filled / empty / short paths
**missing_test_for**: `hermes_quant/analysts/kronos.py:300-302`
If `predict_distributional` returns paths containing NaN, `np.median(NaN)`
returns NaN → `direction` is set via the `else: direction = 0` branch
(NaN > 0 is False, NaN < 0 is False), so we'd silently flip to abstain
direction with confidence 0.50. Probably right, but untested.
If paths shape is `[0, pred_len, n_features]`, `paths[:, -1, 0]` is empty
→ `np.median([])` raises (warning + NaN). `np.mean(np.sign(...))` of
empty → NaN.
**suggested**: 3 tests:
  (a) `FakePredictor` returns `np.full((30, 12, 5), np.nan)`; assert
      analyst returns abstain (or flat) view, never raises.
  (b) `FakePredictor` returns `np.zeros((0, 12, 5))`; assert abstain
      with `inference_error` reason.
  (c) `FakePredictor` returns `np.zeros((10, 12, 5))` when
      `sample_count=30`; assert analyst doesn't blindly trust the
      smaller sample (or at minimum, doesn't raise).

### P1-6 — KronosAnalyst: factory called exactly once after failure (not asserted)
**file**: `tests/unit/test_kronos_analyst.py:138-143`
Comment in test admits: "we can't directly assert this without instrumentation".
But it's important: once `_abstain_reason` is set, we must NOT re-invoke
the factory (could trigger expensive HF download repeatedly).
**suggested**: wrap the factory in a counter:
`call_count = 0; def boom(): nonlocal call_count; call_count += 1; raise ...`
then assert `call_count == 1` after multiple `analyze()` calls.

### P1-7 — PurgedWalkForward: duplicate timestamps untested
**missing_test_for**: `hermes_quant/evaluation/cv.py:142-150` (_extract_timestamps)
If `df["timestamp"]` has duplicate entries (e.g., from a buggy resampler),
the sort doesn't dedupe and folds may overlap. Probably not catastrophic
(assert_no_leakage is on Timestamps, not row indices) but warrants
explicit behavior.
**suggested**: `df` with 200 rows where rows 50-60 share a single timestamp;
assert split() either dedupes, or raises, or yields valid splits — pin
the behavior.

### P1-8 — PurgedWalkForward: out-of-order timestamps before split
**missing_test_for**: `hermes_quant/evaluation/cv.py:145` (`sort_values`)
`_extract_timestamps` sorts ascending. But `df` itself isn't sorted, so
the test uses `_hourly_bars` which is monotone. If a caller passes a
shuffled df, the timestamps would sort but rows would be misaligned with
their features. Pin the behavior: should `split()` reject unsorted input,
or is sorting the contract?
**suggested**: shuffle df rows; assert split still yields correct ordering
OR raises a clear error.

### P1-9 — DSR boundary tests
**file**: `tests/unit/test_evaluation.py:226-237`
- `n_observations=30` exactly is NOT tested (only n=10 reject and n=252
  accept). The boundary `< 30` is tested as reject; `== 30` as accept is
  not.
- `n_trials=1` is the default and tested, but `n_trials=2` (where the
  Bailey-LdP `expected max` formula activates) is NOT directly verified
  against a known value; only relative comparison.
**suggested**: `test_dsr_n_obs_exactly_30_accepted` + numerical regression
test for `deflated_sharpe(1.0, n_trials=10, n_observations=252)` against
a hand-computed expected value (~0.85 ish).

### P1-10 — Cron writer: no test for "stdout has neither created nor job_id"
**missing_test_for**: `hermes_quant/cli/__init__.py:807-816`
If `hermes cron create` succeeds (rc=0) but stdout is empty / "OK", the
job_id parser returns `None` and `result["job_id"] is None`. Path is
implicitly handled but not asserted.
**suggested**: `mock_run.return_value.stdout = "OK\n"`; assert
`result["created"] is True and result["job_id"] is None`.

### P1-11 — Cron writer: regex collision case
**file**: `tests/unit/test_autonomous_cron_writer.py:110-126`
The regex `\b([a-f0-9]{6,}|[A-Z0-9_-]{4,})\b` will match the FIRST
qualifying token. Stdout `"Created job NAMED hermes-quant-15m\n"` would
match `NAMED` (5 caps) before reaching the actual ID. Not tested.
**suggested**: stdout with "Created cron job NAMED hermes-quant-15m id=abc123def";
assert `result["job_id"] == "abc123def"` (or pin current ambiguous
behavior so it doesn't silently change).

---

## P2 — nice-to-have

### P2-1 — `_FakePredictor` `agreement_n` walrus typo
**file**: `tests/unit/test_kronos_analyst.py:87`
`n_agree = int(agreement_n := round(self.agreement * sample_count))` —
the walrus binds `agreement_n` then `int()`s it; harmless but
non-idiomatic and the variable is unused.

### P2-2 — `test_default_as_of_is_now` is wall-clock dependent
**file**: `tests/unit/test_ccxt_provider.py:165-177`
Uses `datetime.now(tz=timezone.utc)`. Theoretically flaky if the test
sleeps past the next hour boundary mid-execution. Use `freezegun` or
inject a clock.

### P2-3 — Kronos `update_calibrator` is_noop_stub doesn't verify state
**file**: `tests/unit/test_kronos_analyst.py:303-324`
"Should not raise; behavior is no-op" — but doesn't assert that
calibrator state was NOT mutated. A future change that accidentally
forwards the outcome to `calibrator.fit()` would slip through.
**suggested**: snapshot `a.calibrator.status()` before and after; assert
equal.

### P2-4 — Kronos config_clip_overrides_apply doesn't test BOTH bounds
**file**: `tests/unit/test_kronos_analyst.py:366-375`
Only the `clip_high` override is exercised (agreement=1.0 → 0.95). The
`clip_low=0.10` path is not asserted. (Not easy to hit via path-agreement
which is bounded ≥0.5 with the median trick, but a custom predictor
forcing direction-mismatch could.)

### P2-5 — Cron writer: no test for autonomous_start when shutil.which is None AND rc=0
Tests cover `shutil.which=None` (early return) and `rc=0` (success), but
no test for "shutil.which finds it, subprocess returns 0, but stdout is
malformed UTF-8 / non-text". Edge edge case.

---

## Notes

### Money-software silence-path coverage assessment

| Component | Action-path tests | Silence-path tests | Verdict |
|---|---|---|---|
| KronosAnalyst | 6 (predict up/down, clip high/low, calibrator, config) | 5 (no-load, missing pkg, factory fail, inference exc, insufficient bars) | **OK — silence ≥ action** |
| CcxtProvider | 5 (as_of admit, lookback, pagination, retry-success, health) | 7 (3× validation reject, empty, insufficient, RateLimit, BadSymbol, NetworkError exhaust, missing ccxt) | **OK — silence > action** |
| Cron writer | 2 (no-cron, with-cron-success) | 4 (no PATH, rc≠0, timeout, OSError, graceful-fail) | **OK** |
| PurgedWalkForward | 4 (yields N, ordering, embargo gap, datetime index) | 4 (zero embargo, invalid pcts, too few rows, no timestamp) | **OK** |
| shuffle_timestamps_test | 3 (random, constant, real-signal — but P0-2!) | 1 (no timestamp col reject) | **WEAK — see P0-2** |
| DSR | 5 (basic, zero, negative, more-trials, kurtosis) | 3 (tiny n, invalid trials, extreme skew) | **OK** |

Charter compliance on silence-by-default: passes for Kronos and Ccxt
provider. The shuffle test is the weak link (P0-2) and the CI gate
itself isn't using the new module (P0-1).

### Mock-defeats-test risk audit

- **Kronos `_predictor_factory`**: Diverges from production lazy-load: the
  test seam bypasses the `from kronos import` block entirely. P0-3 above.
  Recommend at least ONE integration-style test that monkeypatches
  `sys.modules["kronos"]` to a stub package.
- **CcxtProvider `_exchange_factory`**: Diverges from `ccxt.binance(...)`
  construction. Tests do hit the production `ccxt.RateLimitExceeded` etc.
  classes via `import ccxt`, so the error-mapping path is realistic. ✓
- **Cron writer `subprocess.run` mock**: Faithful to production branches
  (returncode, TimeoutExpired, OSError). ✓ One uncovered: empty stdout
  on success (P1-10).

### CI-gate completeness

`tests/test_no_lookahead.py` is the named release-blocker gate per
ADR-0006. Before v0.3 it ran inline `_RecordingProvider` slice-equality
checks. After v0.3, it STILL runs only those — the new
`shuffle_timestamps_test` is unit-tested in `test_evaluation.py` but is
not wired into the named gate. **Wave-D follow-up was not done.** This
is the most actionable P0 from this review (P0-1).

### Overall

494 pass / 1 skip is a healthy footprint. The new files raise coverage
materially. v0.3 ships; queue P0-1, P0-2, P0-3 as v0.3.1 followups
before the next analyst lands.
