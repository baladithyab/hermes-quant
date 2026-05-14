# v0.4.3 BTC/USDT dogfood comparison

Date: 2026-05-14  
Venue/provider: `ccxt:kraken` via `hermes quant backtest` cache path  
Window: last 7 days at run time, 1h bars, warmup 24 bars  
Artifacts: `/tmp/hermes-quant-dogfood-v043/`

## Variants

1. `btc-usdt-mvp`
2. `btc-usdt-deliberative` without semantic packets
3. `btc-usdt-deliberative` with one curated neutral semantic packet plus deterministic committee-turn artifact

## Results

| variant | decisions | fires | return | buy_hold | excess | sharpe |
|---|---:|---:|---:|---:|---:|---:|
| `btc-usdt-mvp` | 1 | 2 | -0.07% | +0.12% | -0.19% | -4.655 |
| `btc-usdt-deliberative` no packets | 0 | 0 | +0.00% | +0.12% | -0.12% | +0.000 |
| `btc-usdt-deliberative` curated packet | 0 | 0 | +0.00% | +0.12% | -0.12% | +0.000 |

## Interpretation

This was a smoke comparison, not a promotion test. The deliberative recipe was
more silent than the MVP over this short window. That is acceptable for the
current stage because the committee layer is designed to reduce confidence under
missing/insufficient semantic evidence rather than amplify risk.

The curated semantic-packet variant did not produce trades in this short run.
That confirms the artifact path does not accidentally force action. It also
shows the next dogfood requirement: autonomous perception should generate a
fresh packet cadence across the whole evaluation window, not a single static
packet.

## Charter decision

Do **not** proceed to RL/live based on this smoke. Continue improving perception
automation, packet coverage, and committee calibration. The current result is
useful evidence that the default deliberative path fails closed.
