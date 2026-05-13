```
P0 ADR-0009 (P0-1) Kelly numerator still incorrect
  Issue: The amended Kelly formula uses `edge = signal.magnitude * signal.calibrated_probability` as the expected log return μ in f* = μ/σ². For a directional bet with calibrated probability p and magnitude m (return if correct), the expected log return is approximately (2p-1)·m (or exactly p·log(1+m)+(1-p)·log(1-m)), not p·m. Using p·m overestimates the edge when p>0.5, leading to systematic overbetting.
  Why it matters: Overbetting increases drawdowns, violates the Kelly criterion’s optimality, and can cause ruin. The cost gate also uses the same incorrect expected edge, so the transaction-cost threshold is too easy to clear.
  Proposed fix: Compute expected log return correctly. If the signal provides p and m, use μ = p·log(1+m) + (1-p)·log(1-m). For small m, approximate as (2p-1)·m. Use that as the edge in both the Kelly sizer and the cost gate. Update the unit tests to verify with non‑trivial p (e.g., p=0.6, m=0.01 → μ≈0.002, not 0.006).

P1 ADR-0009 (P0-5) Cost gate uses same incorrect edge
  Issue: The cost gate compares `abs(signal.magnitude) * signal.confidence` to `cost_multiple * transaction_cost`. This expected edge is not the expected log return (see P0 above), so the gate may pass trades whose true expected edge is below the cost threshold.
  Why it matters: Trades with negative or insufficient edge after costs may be executed, eroding capital.
  Proposed fix: Use the corrected expected log return (μ) in the cost gate.

v1 P0-1 Kelly formula off by factor of σ
  Status: PARTIALLY-ADDRESSED (denominator fixed to σ², but numerator still wrong – new bug introduced)
  Notes: The amendment corrected the variance term but introduced an incorrect expected return computation. The system still overbets.

v1 P0-2 Aggregator confidence uncalibrated
  Status: ADDRESSED
  Notes: Isotonic calibration per analyst and per aggregator, plus cold‑start shrinkage, directly addresses the original finding.

v1 P0-3 Kronos path‑confidence not calibrated for direction
  Status: ADDRESSED
  Notes: A pre‑fitted isotonic calibrator is shipped, and the Kronos wrapper now distinguishes raw vs calibrated confidence.

**MAINTAIN BLOCK** — Kelly sizing still uses an incorrect expected return, causing systematic overbetting; this is a critical quant error that must be resolved before the block can be lifted.
