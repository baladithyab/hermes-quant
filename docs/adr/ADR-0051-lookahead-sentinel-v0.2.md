# ADR-0051 — Lookahead Sentinel v0.2: Closing MoA Review False-Negatives

**Status:** Accepted  
**Date:** 2026-05-27  
**Wave:** v0.2-1 hardening  
**Authors:** subagent (implementation), ARIA (design), MoA review panel (Claude Opus 4.7)  
**Related:** ADR-0050 (v0.1 lookahead sentinel), MoA review 7-8 finding I1  
**Implements:** MoA review I1 — "boolean-mask future-peek detection or document exclusion"

---

## Context

ADR-0050 introduced the v0.1 lookahead sentinel that catches:

- `.shift(-N)` / `.shift(periods=-N)` literal negative constants
- `df.iloc[i+N:]` literal forward-slicing

The cross-model MoA review (Wave 7-8, reviewer: Claude Opus 4.7) identified six
residual false-negative classes in finding **I1** that bypass the v0.1 gate entirely:

| Finding ref | Pattern | Example | v0.1 status |
|---|---|---|---|
| I1-a | Boolean-mask future-peek | `bars[bars.index > today]` | **undetected** |
| I1-b | Variable negative shift | `n = -1; df.shift(n)` | **undetected** |
| I1-c | Forward label index | `df.loc[future_idx:]` | **undetected** |
| I1-d | `pct_change(-N)` negative periods | `bars["close"].pct_change(-1)` | **undetected** |
| I1-e | `diff(-N)` negative periods | `bars["close"].diff(-1)` | **undetected** |
| I1-f | Rolling-lambda future-peek | `rolling(5).apply(lambda x: x[-1])` | **undetected** (noted in ADR-0050 §v0.1 limits #3) |

The MoA vote was: **AGREE — document for v0.1, implement v0.2**.

This ADR documents the v0.2 implementation that closes all six gap classes.

---

## Decision

Extend `_LookaheadVisitor` with new detection patterns while preserving all v0.1
`SuspicionKind` values and backward compatibility.

### New `SuspicionKind` values

| Kind | Closes | Mechanism |
|---|---|---|
| `boolean_mask_future_peek` | I1-a | `visit_Subscript` → `_check_boolean_mask_future_peek` |
| `variable_negative_shift` | I1-b | `visit_Assign` pre-scan + `_resolve_negative_var` |
| `forward_label_index` | I1-c | `visit_Subscript` `.loc` branch + `_is_forward_label_name` |
| `pct_change_negative` | I1-d | `visit_Call` extended to cover `_NEGATIVE_PERIOD_METHODS` |
| `diff_negative` | I1-e | Same `_NEGATIVE_PERIOD_METHODS` set |
| `rolling_lambda_future` | I1-f | `visit_Call` `.apply` branch + `_lambda_uses_negative_index` |

### Pattern details

#### I1-a — `boolean_mask_future_peek`

**Detection logic:** When `visit_Subscript` sees a slice that is an `ast.Compare`
node, `_check_boolean_mask_future_peek` is called.  It fires when:

1. The left operand is a date/index accessor: `df.index`, `df.dates`, `df['date']`,
   `df['timestamp']`, etc.
2. The comparison operator is `>` or `>=` (forward-looking).
3. The comparator contains a Name whose `.id` is in `_TEMPORAL_BOUNDARY_NAMES`:
   `{'today', 'asof', 'now', 'current', 'future', 'cutoff', 'horizon',
    'end_date', 'forecast_date'}`.
4. BinOp comparators like `asof + timedelta(days=1)` are handled by recursing
   into the left side of the BinOp.

**Safe variants:** `bars[bars.index < today]`, `bars[bars.index <= today]`,
`bars[bars['close'] > 100]` — all pass cleanly.

#### I1-b — `variable_negative_shift`

**Detection logic:** A two-phase approach:

1. `visit_Assign` pre-scans all assignment nodes and populates:
   - `_neg_assigned_names: dict[str, int]` — maps name → negative constant
     (`n = -1`, `lag = -5`, etc.)
   - `_pos_assigned_names: dict[str, int]` — maps name → positive constant
     (`n = 1`, `lookback = 5`, etc.)
2. In `visit_Call` for `.shift()`, if the positional argument is not a literal
   constant (`_const_int` returns None), `_extract_shifted_var_name` extracts
   the base Name:
   - `shift(n)` → Name `n` → look up in `_neg_assigned_names`
   - `shift(-n)` → UnaryOp(USub, Name `n`) → look up in `_pos_assigned_names`,
     return `-pos_val` (negative effective value)

**Handles both spec-required forms:**
- `n = -1; df.shift(n)` → fires `variable_negative_shift`
- `n = 1; df.shift(-n)` → fires `variable_negative_shift`

**Known v0.2 limits** (see §v0.3 plan):
- Single-assignment only.  `m = 1; n = -m; df.shift(n)` is NOT caught.
- The assignment must precede the shift call in the AST walk order (top-down).
  Assignments inside function bodies are only seen after the function def node is
  entered.
- `**kwargs` unpacking: `df.shift(**d)` is not analysed.

#### I1-c — `forward_label_index`

**Detection logic:** In `visit_Subscript`, when the value attribute is `loc`,
the slice is examined for a lower-bound Name.  If the Name's `.id` (case-folded)
contains any substring from `_FORWARD_LABEL_SUBSTRINGS`:

```python
("future", "next", "ahead", "fwd", "plus", "forward", "asof_plus")
```

the suspicion is recorded.

**Safe variants:** `df.loc[historical_date]`, `df.loc[start_date:]`,
`df.loc['2024-01-01':]` — all pass.

**False-positive risk:** A variable legitimately named `next_week_start` used as
a backward boundary would be flagged.  Operators should rename such variables or
use `iloc` instead.

#### I1-d / I1-e — `pct_change_negative` / `diff_negative`

**Detection logic:** `visit_Call` is extended with a check on
`_NEGATIVE_PERIOD_METHODS = frozenset({"pct_change", "diff"})`.  Both positional
and keyword (`periods=`) argument forms are checked.  A negative constant fires
the appropriate kind.

**Safe variants:** `pct_change()`, `pct_change(1)`, `pct_change(5)`,
`diff()`, `diff(1)` — all pass.

#### I1-f — `rolling_lambda_future`

**Detection logic:** In `visit_Call`, when `attr == "apply"` and the receiver is a
`.rolling(...)` call (detected by `_is_rolling_receiver`), the first positional
argument is checked.  If it is a `Lambda` whose body is a `Subscript` with a
negative constant index, `rolling_lambda_future` is recorded.

**Rationale:** `rolling(N).apply(lambda x: x[-1])` fetches the *last* element of
the rolling window.  In a properly aligned time series this is the most-recent
(newest) bar in the window — which, if the window definition is misconfigured or
if the series already contains future data, becomes a future bar.

**Safe variants:** `rolling(N).apply(lambda x: x[0])`,
`rolling(N).apply(lambda x: x.mean())` — pass.

**Known v0.2 limits:**
- Only simple `lambda x: x[-K]` bodies are analysed.  `lambda x: np.sum(x) + x[-1]`
  would NOT be caught.
- `expanding().apply(lambda x: x[-1])` is NOT caught (only `.rolling()` receiver
  is matched).  Both are documented v0.3 targets.

---

## Backward Compatibility

All v0.1 `SuspicionKind` values are unchanged:

| Kind (v0.1) | Preserved? |
|---|---|
| `negative_shift` | ✅ |
| `negative_shift_periods` | ✅ |
| `negative_shift_on_target` | ✅ |
| `negative_shift_periods_on_target` | ✅ |
| `forward_iloc_slice` | ✅ |

The existing `tests/factors/test_lookahead_sentinel.py` (23 tests) passes
unchanged.  No public API signatures were modified.

---

## Test Coverage

`tests/factors/test_lookahead_advanced.py` adds **53 new tests** across 7 classes:

| Class | Patterns tested |
|---|---|
| `TestBooleanMaskFuturePeek` | 9 tests (5 fire, 4 safe) |
| `TestVariableNegativeShift` | 7 tests (4 fire, 3 safe/edge) |
| `TestForwardLabelIndex` | 9 tests (6 fire, 3 safe) |
| `TestPctChangeNegative` | 7 tests (3 fire, 4 safe) |
| `TestDiffNegative` | 7 tests (3 fire, 4 safe) |
| `TestRollingLambdaFuture` | 6 tests (2 fire, 3 safe, 1 gap-doc) |
| `TestCompoundPatterns` | 8 tests (5 compound fire, 3 clean) |

---

## Residual Heuristic Limits (documented false-negative surface)

The following patterns remain undetected in v0.2 and are explicitly deferred:

| Gap | Example | Reason deferred |
|---|---|---|
| Dynamic boolean mask | `df[df.index > user_cutoff]` where `user_cutoff` is not in `_TEMPORAL_BOUNDARY_NAMES` | Requires alias tracking |
| Multi-hop variable shift | `m = 2; n = -m; df.shift(n)` | Requires symbolic propagation |
| `expanding().apply(lambda x: x[-1])` | As written | Receiver check is rolling-only |
| `np.where` future condition | `np.where(idx > today, ...)` | No `np.where` visitor |
| `resample` forward fill | `df.resample("1D").ffill()` on future-padded data | Context-dependent |
| `merge`/`join` on future index | `df.merge(future_df, ...)` | Out-of-scope by design |
| Lambda with mixed indexing | `lambda x: np.sum(x) + x[-1]` | Deep lambda analysis deferred |

---

## v0.3 Deferred Plan

If the residual false-negative surface proves exploitable in practice, v0.3 will:

1. **Symbolic variable tracking:** Replace the single-assignment table with a
   simple data-flow lattice (SSA-lite) to propagate constant folding through
   two-hop assignments and augmented assignment (`n -= 1`).
2. **Alias expansion for temporal names:** Widen `_TEMPORAL_BOUNDARY_NAMES` via
   a configurable allowlist, or use type annotations if present (`asof: pd.Timestamp`).
3. **Rolling/expanding unification:** Generalise `_is_rolling_receiver` to cover
   `.expanding()`, `.ewm()`, and `.resample()` chains.
4. **Lambda body deep analysis:** Walk the full lambda body AST rather than
   only checking the top-level expression.
5. **Subprocess-isolated factor evaluation** (ADR-0050 §Alternative B) as a
   defence-in-depth layer for production if the threat model escalates to
   adversarial external inputs.

---

## Consequences

### Positive

- Six residual false-negative classes from the MoA review are now caught.
- All detection is pure-stdlib `ast` — zero new dependencies.
- Backward compatibility with v0.1 is maintained exactly.
- New suspicion kinds are machine-readable tags usable for automated triage.

### Negative / Trade-offs

- The boolean-mask detector has a potential false-positive when a legitimate
  backward-filter uses a variable named `today` or `now` as a lookback anchor
  (e.g. `df[df.index > now - pd.Timedelta(days=30)]` would fire — though this
  is genuinely suspicious and should be flagged).
- The forward-label-index heuristic may flag legitimate variables whose names
  happen to contain `fwd`, `next`, or `forward` as non-temporal identifiers.
- Variable-shift detection requires the assignment to appear *before* the shift
  call in the source; hoisted assignments inside decorated functions or class
  bodies may be missed.

---

## References

- MoA review 7-8 finding I1: `/tmp/moa-review78-claude.md §I1`
- ADR-0050: `docs/adr/ADR-0050-alpha-zoo-with-ast-purity-and-lookahead-gate.md`
- Implementation: `hermes_quant/factors/lookahead_sentinel.py`
- Tests: `tests/factors/test_lookahead_advanced.py`
