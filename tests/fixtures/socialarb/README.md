# tests/fixtures/socialarb — versioned Camillo social-arb eval set (N13)

Committed, offline, deterministic eval inputs for the PDR-2 TrendVelocity gate and the
catalyst social-arb harness. **N13: versioned fixture, NEVER `/tmp`.** Prior to PDR-2 the
labels lived at `/tmp/phase0_labels.json` (a desync/non-reproducibility hazard); they now
live here and are committed so the unit tests run with no network and no live yfinance.

## Files

### `camillo_labels.json`
The 5 documented Camillo consumer-trend social-arb cases (CELH / CROX / DIIBF / TPR / NWL)
with **REAL yfinance forward returns**, captured once by
`ops/scripts/quant-catalyst-socialarb-labels.py` and committed. Each entry:
`{label, ticker, date, headline, window, fwd_return_pct, detail:[entry_px, exit_px, entry_date], err}`.

Realized forward returns (the external truth the D74.7 directional-precision gate scores against):

| ticker | surfaced   | fwd window | fwd_return_pct | direction |
|--------|------------|-----------:|---------------:|-----------|
| CELH   | 2021-03-01 |      120 d  |       +13.95 % | hit       |
| CROX   | 2020-06-01 |      150 d  |       +82.83 % | hit       |
| DIIBF  | 2020-04-15 |      180 d  |      +469.77 % | hit       |
| TPR    | 2023-08-01 |      120 d  |       -25.29 % | miss      |
| NWL    | 2017-09-01 |      120 d  |       -34.82 % | miss      |

All 5 cases propagate a **bullish** stance (positive consumer-trend). 3/5 realized positive
=> directional precision = 0.60 hit-rate, clearing the D74.7 `>= 0.6` bar **exactly** on n=5
(TPR/NWL are the documented false positives — see `synthesize.py` haircut comment). The
N13 note in the labels script: regenerate by running the labels script, then **commit the
output here — never re-introduce the `/tmp` coupling.**

### `interest_series.json`
Per-symbol **weekly interest counts** — the input to
`hermes_quant.perception.velocity.compute_trend_velocity`. Each entry:
`{ "<SYM>": {"freq": "W", "asof": "<ISO-Z>", "counts": [{"week_start": "YYYY-MM-DD", "n": int}, ...], "note": "..."} }`.

`week_start` is the Monday of each ISO week (a parseable date, so the loader rebuilds the
period index via `pd.Timestamp(week_start).to_period("W")` without pandas' brittle
`YYYY-Wnn` string parsing). `asof` equals the matching case's surfaced `date` so the
velocity score's lookahead anchor == the eval case's publication date (no future bucket).

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

# Re-run the social-arb edge eval against the committed fixture:
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-catalyst-socialarb-eval.py
```

The interest series is hand-maintained alongside the labels; if a label's surfaced `date`
changes, update the matching `asof` here so the lookahead anchor stays aligned.
