# v0.3 correctness review

Reviewer: subagent (line-by-line vs ADR-0017/0018/0019).
Files reviewed: `data/ccxt_provider.py`, `analysts/kronos.py`,
`evaluation/{cv,lookahead,dsr}.py`, `cli/__init__.py` (autonomous-start path),
`protocol.py`.

## Verdict: MERGE_WITH_FOLLOWUPS

No P0s ship to money-software users in the lookahead/numeric-correctness lane.
The two notable concerns (lookahead-test mechanics, narrow HF-load except
catch) are P1 with bounded blast radius; tracked below for v0.3.1.

---

## P0 findings

None. The lookahead-safety contract at the leaf is correctly implemented:

- `ccxt_provider.py:205-206` — `bar_close_time = df["timestamp"] + pd.Timedelta(seconds=tf_seconds); df = df[bar_close_time <= as_of]` matches ADR-0017 §D3 verbatim. In-flight bar at `as_of` is dropped (open_ts + tf_seconds > as_of for the in-flight bar).
- `ccxt_provider.py:152-153` — naïve `as_of` is localized to UTC before arithmetic. No tz confusion.
- `kronos.py:325-330` — `np.clip(sign_agreement, low=0.30, high=0.85)` is the verbatim ADR-0018 §D3 hard clip.
- `kronos.py:251-265` — zero-confidence abstain returns AnalystView with `confidence=0.0`, which BMA filters via `confidence < 0.10` per ADR-0018 §D4. Calibrator-poisoning prevented.
- `dsr.py:90-99` — variance term `1 - skew*SR + (kurtosis-1)/4 * SR²` matches Bailey–LdP 2014 eq 4. PSR z-statistic uses `sqrt(n-1)`. Correct.

---

## P1 findings

### 1. `kronos.py:245` — narrow exception catch on HF weight load
- **Severity**: P1 (bounded; abstain-on-failure is the worst outcome but only the wrong abstain *reason* string is logged for unknown failures).
- **Description**: `_lazy_load` catches `(OSError, RuntimeError, ValueError)`. `huggingface_hub` raises `RepositoryNotFoundError`, `GatedRepoError`, `LocalEntryNotFoundError` — these subclass `OSError` in current versions, so they're covered. But `huggingface_hub.errors.HfHubHTTPError` subclasses `requests.HTTPError` which is `IOError` (alias of OSError) → covered. `torch.cuda.OutOfMemoryError` is `RuntimeError` → covered. However, *any other* Kronos package raise (e.g. config-shape mismatch raising `KeyError` or `AttributeError`) escapes and crashes the analyze() call upstream of the lazy-load try. Real-world drift on a 3rd-party package warrants the broader catch.
- **Fix**: Broaden to `except Exception as exc:` for the weight-load block (the test-factory branch already does this at line 205). Distinct path from inference-time exception is preserved because `_abstain_reason` is set permanently here vs per-call in `analyze()`.

### 2. `lookahead.py:130-134` — shuffle is logically right, but commentary is misleading and lookahead-detection coverage is narrow
- **Severity**: P1 (no false-positive risk; the test under-detects certain row-indexing-based leaks).
- **Description**:
  - The code permutes the `timestamp` column then `sort_values("timestamp")` — net effect is that OHLC rows are scrambled relative to their original temporal positions (because pandas preserves row association during `sort_values`). The inline comment says *"OHLCV stays in original order"* — that's false post-sort. **Doc lie**.
  - Statistical mechanics: an analyst that exclusively uses `bars["close"].iloc[-N:]` (no timestamp comparisons) will be perturbed by this shuffle (since the rows reorder). An analyst that does timestamp-conditional gating (e.g. `bars[bars.timestamp < ctx.asof]`) WILL detect the shuffle. Coverage is asymmetric.
  - The ADR-0019 §D3 pass condition `p_value > alpha` ("not distinguishable from shuffled") is the contract; the implementation matches. Statistically this means "fail to reject null that shuffled == real", i.e. analyst is no better than scrambled — this gates BENIGN analysts as passing. A genuinely-edged analyst with strong real_score would *fail* (p ≈ 1/(n+1) = 0.09 with n=10). This is the spec, not a code bug, but worth flagging — the gate as written **passes nullity, not safety**.
- **Fix**:
  1. Update the comment block at lines 126-130 to say *"OHLC rows are reordered by the sort"*.
  2. Track follow-up: reconsider whether `shuffle_timestamps_test` should compute `p = (n_below_real + 1) / (n+1)` and flip the pass condition to detect *unexpectedly-good* shuffled scores (the canonical lookahead signature). The current implementation is faithful to the ADR but the ADR's logic chain is questionable; defer to a separate ADR amendment review (out of scope here).

### 3. `ccxt_provider.py:172` — pagination break condition `new_since >= as_of_ms` may truncate before reaching `as_of`
- **Severity**: P1 (bounded — `lookback_bars` request is satisfied by buffer, but in edge cases of late-arrived bars at the head, this short-circuits paging one chunk early).
- **Description**: `if new_since <= cur_since or new_since >= as_of_ms: break` — the second clause stops paging once `chunk[-1][0] + 1 >= as_of_ms`. The bar at `chunk[-1]` opens at `as_of_ms - 1` ms, which is still a valid bar (its close time is `open + tf_seconds*1000`, far past `as_of`). This break happens BEFORE the as_of filter — so we do request that bar's chunk. After validate + filter, we drop it. Net: behavior is correct, but the comment says "we've reached now" which is off-by-one — we've reached `as_of`, not "now". Termination is safe.
- **Fix**: Cosmetic — clarify comment. Alternatively, check `new_since > as_of_ms` (strict) for symmetry with the filter, which uses `<=` semantics. Currently passes tests; not blocking.

### 4. `kronos.py:170` — `metadata["raw_confidence_clipped"]` flag is float-equality on `np.clip` boundary
- **Severity**: P1 doc/observability (no trade impact).
- **Description**: `raw_confidence == self.config.raw_confidence_clip_high` after `np.clip(..., low, high)` is exact (numpy returns the literal bound). Safe in practice. But if a user passes a non-float clip (e.g. `np.float32` cast somewhere), float equality could spuriously fail. Low risk.
- **Fix**: Use `math.isclose(raw_confidence, clip_high)` or compare to `np.float64(clip_high)`. Polish.

---

## P2 findings

### 5. `ccxt_provider.py:203-204` — dead/redundant tz normalization
- `as_of` is normalized at lines 148-153. Lines 203-204 re-normalize with a faulty branch (`tz_convert` requires tz-aware input — would raise on tz-naive — but the prior block guarantees tz-aware so this branch is unreachable). Remove.

### 6. `ccxt_provider.py:197-199` — dtype re-cast comment hints at hidden upstream issue
- The `astype("datetime64[ns, UTC]")` after `validate_bars` is defensive. Either fix `validate_bars` to guarantee ns-precision (preferred) or document the pandas-version dependency. Currently masks the symptom.

### 7. `ccxt_provider.py:163` — `max_iters = 20` is silent truncation
- 20 chunks × 1000 bars = 20K. For a `1m` request with `lookback_bars=20000`, the loop terminates without reaching the user's window. No warning logged. Add `logger.warning` when `iter == max_iters - 1` with non-empty chunk.

### 8. `cv.py:121-122` — embargo can produce negative train window
- If `embargo_pct > train_pct` (pathological config), `train_end < train_start`. No guard. Add `if train_end <= train_start: raise ValueError("embargo larger than train_pct")` in the loop or in `__init__`.

### 9. `cv.py:147` — `_extract_timestamps` returns a sorted Series but the caller assumes monotonic input
- Defensive sort is fine; could short-circuit if `df` already sorted (perf P2 only).

### 10. `kronos.py:374` — column reorder in `_DistributionalKronosPredictor.predict_distributional`
- `out_df[["close", "open", "high", "low", "volume"]]` puts close at column 0 to match `_direction_from_paths`'s `paths[:, -1, 0]` close-extraction. Self-consistent. Brittle to upstream Kronos column drift; document the contract on `_direction_from_paths` line 300 ("close = column 0 by our reorder, NOT by Kronos default ordering").

### 11. `kronos.py:368-374` — `sample_count` calls into `predict(sample_count=1)` is `O(N)` HF inference
- ADR-0018 §D2 footnote acknowledges this is the v0.3 "robust fallback". Latency on CPU is 30 × 3-10s = 90-300s/call → exceeds the 180s/symbol budget mentioned in ADR-0018 context. Tracked, not blocking.

### 12. `cli/__init__.py:813` — regex job_id extraction is fuzzy
- `\b([a-f0-9]{6,}|[A-Z0-9_-]{4,})\b` could pick up incidental words. Tested mentally: "created" fails (`r`,`t` not hex); "Created" fails (capital C only). Looks fine in practice but a structured `--json` output from `hermes cron create` would be cleaner. Pre-existing CLI design, not v0.3-introduced.

### 13. `dsr.py:75` — `z_e = sqrt(2 * log(n_trials))` undefined for `n_trials=1`
- Branch at line 69 short-circuits `n_trials==1` to `sharpe_threshold=0.0`, so unreachable. Safe, but worth an inline assertion comment.

---

## Notes (positive observations)

- **Lookahead invariant at the leaf is solid** (`ccxt_provider.py:201-206`). The `+ tf_seconds` shift to convert open→close time is the canonical fix; matches ADR-0017 §D3 sketch line-for-line.
- **Kronos failure-mode triage is well-staged**: package-missing → permanent abstain; weight-load → permanent abstain; per-call inference → transient abstain. Three distinct paths, each logged at appropriate severity (info/warning/warning+exc_info).
- **DSR PSR z-statistic** (`dsr.py:99`) and Beasley–Springer–Moro coefficients (`dsr.py:118-147`) match the Wichura/canonical implementation. Tested against ADR-0019 §D4 reference.
- **Pagination monotonicity guard** (`ccxt_provider.py:171-173`) defends against exchange returning out-of-order or duplicate `since` chunks. Infinite-loop-safe.
- **`BadSymbol` correctly bypasses retry** (`ccxt_provider.py:264`) — raises `DataProviderError` immediately, ahead of generic `ExchangeError` catch. ccxt's class hierarchy makes this order-sensitive; it's correctly ordered.
- **`RateLimitExceeded` exponential backoff** (`ccxt_provider.py:254-263`) — `delay = base_delay_s * (2 ** attempt)` = 2s, 4s, 8s. Caught BEFORE generic `NetworkError` (RLE is subclass of NE in ccxt), so rate-limit gets the dedicated handler. Correct shape.
- **`PurgedWalkForward.assert_no_leakage`** (`cv.py:31-42`) is called per fold yield — defense-in-depth; fold arithmetic is verified at runtime, not just at construction.
- **`shuffle_timestamps_test` Laplace-smoothed p-value** `(n+1)/(N+1)` (`lookahead.py:141`) is the right correction for finite-permutation tests.

---

## Followups for v0.3.1

1. Broaden `kronos._lazy_load` exception catch to `Exception` (P1 #1).
2. Doc fix on `lookahead.shuffle_timestamps_test` shuffle comment (P1 #2.1).
3. Track ADR amendment for shuffle-test pass-condition logic (P1 #2.2).
4. Add `max_iters` truncation warning (P2 #7).
5. Embargo > train_pct guard in `PurgedWalkForward` (P2 #8).
