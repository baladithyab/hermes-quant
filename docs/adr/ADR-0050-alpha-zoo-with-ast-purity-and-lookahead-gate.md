# ADR-0050 — Alpha Zoo with AST Purity Gate and Lookahead Sentinel

**Status:** Accepted  
**Date:** 2026-05-27  
**Wave:** 8c  
**Authors:** ARIA (research) + subagent (implementation)  
**Related:** ADR-0045 (backtester), ADR-0048 (hypothesis registry), Wave 6 IC dedup gate

---

## Context

### The Generative-Factor-Mining Slop Problem

LLM-driven factor mining pipelines (R&D-Agent NeurIPS 2025, FactorMiner THU, AlphaPROBE) can
generate hundreds of candidate factor expressions per session.  Without defensive gates, two
failure modes are endemic:

**F4 — Correlation Red Sea:** The miner emits near-identical signals.  The IC dedup gate
(ADR-0045, `ic_dedup.py`) already defends against this with a Pearson correlation threshold.

**F5 — Generative Factor Slop:** The miner emits code that *syntactically* looks like a factor
expression but contains forbidden APIs, implicit I/O, or future-peeking data access.  No
runtime exception is raised; the backtest silently uses contaminated data.  This is the failure
mode ADR-0050 addresses.

Concrete examples of slop factors observed in research:

```python
# Looks like a momentum factor; secretly exfiltrates your bars
bars["close"].to_csv("/tmp/leak.csv")
pd.read_csv("http://attacker.example/data.csv")["close"]

# "RSI" that secretly peeks at tomorrow's close
bars["close"].shift(-1) / bars["close"]  # future normalisation

# Factor that uses eval() to run arbitrary code at compute-time
eval(params.get("injected_code", "bars['close']"))
```

HKUDS/Vibe-Trading's 452-factor Alpha Zoo inspired our implementation.  Their catalog's
defining property is that every factor is a **pure pandas/numpy expression** verified
statically before admission.

---

## Decision

Implement a two-gate defence layer for the Alpha Zoo:

### Gate 1 — AST Purity Gate (`ast_purity.py`)

**Mechanism:** Parse factor source code with `ast.parse()`, walk the AST with
`ast.NodeVisitor`, and reject on any of:

| Trigger | Violation kind | Rationale |
|---|---|---|
| `Name.id` in `FORBIDDEN_NAMES` | `forbidden_name_ref` | Prevents OS, subprocess, eval access |
| `Call(func=Name(id in FORBIDDEN_NAMES))` | `forbidden_name` | Same, for call sites |
| `Attribute.attr` in `FORBIDDEN_ATTRIBUTES` | `forbidden_attribute_access` | Prevents getattr/setattr sandbox escape |
| `Call(func=Attribute(attr in FORBIDDEN_ATTRIBUTES))` | `forbidden_attribute` | Same, for call sites |
| `Attribute.attr` in `FORBIDDEN_PD_METHODS` | `forbidden_pd_method_access` | Prevents pandas I/O exfiltration |
| `Import` nodes | `import_statement` | Factors must be self-contained closures |
| `ImportFrom` nodes | `import_from_statement` | Same |

**FORBIDDEN_NAMES** rationale (selected):

- `os`, `sys`, `subprocess` — shell execution / process inspection
- `open` — file I/O (reading or writing data outside the bars scope)
- `eval`, `exec`, `compile`, `__import__` — arbitrary code execution
- `globals`, `locals`, `vars` — scope introspection / injection vectors
- `requests`, `urllib`, `socket` — network I/O (data contamination)
- `pickle`, `shelve` — deserialization gadget chains
- `random` — non-determinism (backtests must be reproducible)
- `breakpoint`, `input` — interactive hooks (would hang compute() calls)

**Why forbid imports?**  Factors must be pure closures.  They receive `pd`, `np`, `bars`, and
`params` in their evaluation scope; anything else is either unnecessary or a security concern.
A factor that needs a new library should be promoted to a proper Python module, not a zoo entry.

### Gate 2 — Lookahead Sentinel (`lookahead_sentinel.py`)

**Mechanism:** Second AST walk that heuristically detects time-travel patterns.

| Pattern | Kind tag | Example |
|---|---|---|
| `.shift(-N)` positional arg | `negative_shift` | `bars["close"].shift(-1)` |
| `.shift(periods=-N)` keyword | `negative_shift_periods` | `bars["close"].shift(periods=-1)` |
| Shift on target column names | `negative_shift_on_target` | `fwd_return.shift(-1)` |
| `df.iloc[i+N:]` (N > 0) | `forward_iloc_slice` | `bars.iloc[i+1:]` |

**v0.1 limits and known false-positive scenarios:**

1. **Forward-normalised pipelines:** A feature pipeline that intentionally uses `shift(-1)` to
   align labels is legitimate outside a factor expression context.  The sentinel flags it anyway.
   Operators who need this pattern should compute labels separately and not store them as AlphaZoo
   factors.

2. **Dynamic period computation:** `.shift(-lookback)` where `lookback` is a runtime variable is
   NOT detected (the sentinel only catches literal constants).  This is a known false-negative.
   Mitigated by the strict FORBIDDEN_NAMES list (the expression still can't do I/O).

3. **Multi-step look-ahead via rolling:** `bars["close"].rolling(N).apply(lambda x: x[-1])`
   could peek if N is chosen to include future rows — not detected in v0.1.  Deferred.

**False-positive rate:** In the starter set of 15 factors, 0 false positives.  Expected rate in
wild-generated factors: 5–15% (factors in normalisation pipelines may use negative shift
legitimately).

### Compute Sandbox (`AlphaZoo.compute`)

```python
scope = {"pd": pd, "np": np, "bars": bars, "params": params}
result = eval(factor.source_code, {"__builtins__": {}}, scope)
```

- `{"__builtins__": {}}` strips all built-in names from the eval scope.
- `pd` and `np` are explicitly provided so pure pandas/numpy expressions execute correctly.
- This is a **defence-in-depth** layer, not a primary security boundary.  The AST gate is
  the primary defence; the sandbox prevents runtime injection of anything the static gate missed.
- Note: CPython's `{"__builtins__": {}}` does not prevent all escapes (e.g., `().__class__`
  gadget chains in older Python).  The FORBIDDEN_ATTRIBUTES list (`__class__`, `__bases__`,
  `__subclasses__`) closes the most common escape routes at the AST level.

---

## Consequences

### Positive

- Every factor in the zoo is statically verified before it touches data.
- The append-only registry (`alpha_zoo.jsonl`) provides a tamper-evident audit trail.
- `PurityViolation` and `LookaheadDetected` are catchable exceptions — integration tests
  can assert exact rejection reasons.
- Starter set of 15 factors provides immediate value and proves the gates work end-to-end.

### Negative / Trade-offs

- The static gate is conservative.  Some legitimate use-cases (network data providers,
  serialisation pipelines) are blocked and require a bypass mechanism (not yet implemented).
- The lookahead sentinel has false positives on label-normalisation factors.
- `eval()` in `compute()` is inherently lower-trust than a proper plugin system.  For
  production hardening, consider migrating to a RestrictedPython or subprocess-isolated
  evaluator in v0.3.

---

## Starter Set (15 factors, v0.1)

| Name | Category | Key technique |
|---|---|---|
| `alpha_close_minus_open` | Momentum | Arithmetic |
| `alpha_close_to_high_ratio` | Strength | Ratio |
| `alpha_volume_zscore_20` | Volume | Rolling z-score |
| `alpha_log_return_5d` | Momentum | Log return |
| `alpha_high_low_range` | Volatility | Normalised range |
| `alpha_close_above_ma20` | Trend | Binary SMA filter |
| `alpha_rsi_14` | Oscillator | RSI via lambda |
| `alpha_atr_14_relative` | Volatility | ATR / close |
| `alpha_volume_price_corr_20` | Correlation | Rolling Pearson |
| `alpha_momentum_60d` | Momentum | 60d pct_change |
| `alpha_obv_normalised` | Volume | OBV z-score |
| `alpha_price_acceleration` | Mean-reversion | Δmomentum |
| `alpha_volume_weighted_return` | Composite | Return × rel_vol |
| `alpha_bollinger_position` | Mean-reversion | BB %B |
| `alpha_turnover_5d` | Activity | Volume ratio |

**Path to 452:** The WorldQuant Alpha Catalog defines factor families (price, volume,
fundamental, sentiment, options).  v0.2 will bulk-ingest the price + volume families (≈200
factors) via automated template expansion.  v0.3 adds fundamental + sentiment (≈252).  Each
batch will be gated through the same AST purity + lookahead pipeline.

---

## Alternatives Considered

### A — Trust LLM output; skip static analysis

Rejected.  The Generative Factor Slop failure mode is well-documented (R&D-Agent,
FactorMiner).  "Trust but verify" is insufficient; the gate is mandatory.

### B — Sandboxed subprocess per factor

Stronger isolation but 100–1000× slower per compute() call.  Deferred to v0.3 hardening.
Current threat model is internal tooling, not adversarial external inputs.

### C — Allow-list only (no AST scan)

An allow-list of approved pandas/numpy method names was considered.  Rejected because:
  1. The pandas/numpy API surface is large; maintaining a complete allow-list is brittle.
  2. The deny-list of forbidden patterns is smaller and more stable.
  3. The AST approach catches the dangerous categories structurally, not by enumeration.

---

## References

- HKUDS/Vibe-Trading GitHub — https://github.com/HKUDS/Vibe-Trading
- R&D-Agent (NeurIPS 2025) — arxiv:2505.15155
- FactorMiner (THU) — arxiv:2602.14670
- AlphaPROBE — arxiv:2602.11917
- WorldQuant Alpha Catalog — https://platform.worldquant.com/alphas/
- Wave 8c implementation: `hermes_quant/factors/{ast_purity,lookahead_sentinel,alpha_zoo,starter_set}.py`
- Tests: `tests/factors/{test_ast_purity,test_lookahead_sentinel,test_alpha_zoo}.py`
