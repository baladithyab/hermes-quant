"""hermes_quant.learning — closing the learning loop (L2 lane).

This package wires analyst *skill* so it PERSISTS across recommend() calls,
DECAYS with recency, and CONSTRAINS the default decision path. It closes three
interlocking seeds:

  - f254 — per-analyst calibration (skill keyed by analyst, not one global blob)
  - c96e — persisted, recency-refit BMA Beta posteriors (skill survives restart)
  - 57f6 — reflections that apply a bounded haircut on a default-path decision

Everything here is pure-Python, deterministic, and offline-testable. The hard
invariant across the whole package is asof-HONESTY: a posterior or haircut used
to make a decision at time T may only depend on outcomes that were *observable*
strictly before T. Using a future outcome to calibrate a past decision is
lookahead and corrupts every backtest.
"""

from __future__ import annotations
