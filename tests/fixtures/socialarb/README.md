# tests/fixtures/socialarb — versioned Camillo social-arb eval set (N13)

Committed, offline, deterministic eval inputs for the PDR-2 TrendVelocity gate and the
catalyst social-arb harness. **N13: versioned fixture, NEVER `/tmp`.** Prior to PDR-2 the
labels lived at `/tmp/phase0_labels.json` (a desync/non-reproducibility hazard); they now
live here and are committed so the unit tests run with no network and no live yfinance.

## Files

### `camillo_labels.json`
The documented consumer-trend social-arb cases with **REAL yfinance forward returns**,
captured once by `ops/scripts/quant-catalyst-socialarb-labels.py` and committed. Each entry:
`{label, ticker, date, headline, window, fwd_return_pct, detail:[entry_px, exit_px, entry_date], err, provenance}`.

**B09 (Wave-4) expansion.** The Phase-0 set was **5** cases (CELH/CROX/DIIBF/TPR/NWL) that
cleared the D74.7 `>=0.60` directional bar at a **knife-edge 3/5=0.60** (TPR/NWL the
documented false positives). B09 grows the set to **12** by adding 7 documented
consumer-trend social-arb episodes, so the precision **measurement** is higher-confidence,
and runs `run_precision` over it at a **STATED HIGHER `min_hit_rate` = 0.70** (see
`ops/scripts/quant-catalyst-socialarb-eval.py::MIN_HIT_RATE` and
`tests/unit/test_catalyst_socialarb_eval_b09.py`). B09 does **NOT** change the
`synthesize.py` consumer-trend haircut — that is B07, data-gated; B09 only raises the
eval bar so B07 acts on a stronger number.

Realized forward returns (the external truth the directional-precision gate scores against).
All cases propagate a **bullish** stance (a positive consumer-trend); the surfaced date is
chosen by the **documented** surfacing of the trend, NOT by hindsight of the return (the
anti-cherry-pick discipline — the misses are KEPT). Each row's `provenance` field records why
the case is defensible.

| ticker | set     | surfaced   | fwd window | fwd_return_pct | direction | provenance (short)                                   |
|--------|---------|------------|-----------:|---------------:|-----------|------------------------------------------------------|
| CELH   | Phase-0 | 2021-03-01 |      120 d  |       +13.95 % | hit       | TikTok energy-drink Gen-Z virality                   |
| CROX   | Phase-0 | 2020-06-01 |      150 d  |       +82.83 % | hit       | healthcare-worker + celebrity-collab resurgence      |
| DIIBF  | Phase-0 | 2020-04-15 |      180 d  |      +469.77 % | hit       | pandemic bicycle shortage (demand>supply)            |
| TPR    | Phase-0 | 2023-08-01 |      120 d  |       -25.29 % | **miss**  | Coach social popularity, but Capri-deal selloff      |
| NWL    | Phase-0 | 2017-09-01 |      120 d  |       -34.82 % | **miss**  | Elmer's slime craze, but conglomerate weakness       |
| ELF    | B09     | 2023-01-15 |      150 d  |       +94.11 % | hit       | #beautytok Gen-Z demand, sustained sales growth      |
| DECK   | B09     | 2022-10-01 |      150 d  |       +30.30 % | hit       | UGG Tasman/Minis TikTok autumn-2022 resurgence       |
| YETI   | B09     | 2021-01-15 |      150 d  |       +27.63 % | hit       | viral premium-drinkware (Rambler) craze              |
| MNST   | B09     | 2020-04-01 |      150 d  |       +57.93 % | hit       | energy-drink category strength (CELH sector-peer)    |
| CMG    | B09     | 2022-01-15 |      150 d  |       -15.71 % | **miss**  | TikTok menu-hack virality, but 2022 market drawdown  |
| PTON   | B09     | 2020-03-15 |      150 d  |      +195.33 % | hit       | early-pandemic at-home-fitness viral demand          |
| WING   | B09     | 2023-02-15 |      150 d  |       +11.21 % | hit       | social-driven QSR digital-order momentum             |

**Result: 9/12 directionally positive => directional precision = 0.75 hit-rate**, clearing
the stated higher `>=0.70` bar (vs the Phase-0 knife-edge 0.60 on n=5). The 3 misses
(TPR/NWL/CMG) are the documented false positives — kept, not cherry-picked away.

**Edge promotion status.** The Phase-0 five are already promoted to the live seed YAML +
`propagation.py` (the n=5 eval cleared 0.60). The 7 B09 brands (ELF/DECK/YETI/MNST/CMG/
PTON/WING) live ONLY as **EVAL-only in-memory edges** in the eval script and the B09 test —
they are eval INPUTS, not promoted edges, until a future wave decides to wire them live.

N13 note: regenerate by running the labels script, then **commit the output here — never
re-introduce the `/tmp` coupling.**

### `interest_series.json`
Per-symbol **weekly interest counts** — the input to
`hermes_quant.perception.velocity.compute_trend_velocity`. Each entry:
`{ "<SYM>": {"freq": "W", "asof": "<ISO-Z>", "counts": [{"week_start": "YYYY-MM-DD", "n": int}, ...], "note": "..."} }`.

`week_start` is the Monday of each ISO week (a parseable date, so the loader rebuilds the
period index via `pd.Timestamp(week_start).to_period("W")` without pandas' brittle
`YYYY-Wnn` string parsing). `asof` equals the matching case's surfaced `date` so the
velocity score's lookahead anchor == the eval case's publication date (no future bucket).

This series covers the **Phase-0 five only** (it is the PDR-2 velocity input, not the B09
precision input); the 7 B09 brands have committed returns but no interest series — the B09
eval scores DIRECTION via the severity-sourced magnitude path, which needs no velocity score.

**Provenance / how the series were derived:** these are documented-narrative-derived
synthetic GN-RSS/social interest volumes (NOT live Reddit/Trends — that is the B08 data
wave, see plan §8). Each profile is an *accelerating* consumer-trend series matching the
Camillo DETECT signature (a quiet trailing baseline then a sharp week-over-week spike), so
the velocity score is fully reproducible and the `velocity_magnitude` lands inside the
severity band `[0, 0.06]` (rail #2). They reproduce the SLOPE shape, not exact historical
search volumes; the eval scores DIRECTION (stance) against the real returns above.

## Regeneration (N13 discipline)

```bash
# Recapture the REAL forward-return labels (writes the fixture, never /tmp):
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-catalyst-socialarb-labels.py
# -> writes tests/fixtures/socialarb/camillo_labels.json (commit the result)

# Re-run the social-arb edge eval against the committed fixture (B09: 12 cases, min 0.70):
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-catalyst-socialarb-eval.py
```

The interest series is hand-maintained alongside the labels; if a label's surfaced `date`
changes, update the matching `asof` here so the lookahead anchor stays aligned.
