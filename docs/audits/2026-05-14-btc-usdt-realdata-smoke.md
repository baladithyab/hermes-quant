# BTC/USDT real-data smoke — charter empirical gate

Date: 2026-05-14  
Repo state: after Wave G (`accacbc`) + Wave I (`88566f7`)  
Data source: `CcxtProvider(exchange_id="kraken")`  
Dataset: `BTC/USDT`, `1h`, 720 closed bars (`2026-04-14 04:00:00Z` → `2026-05-14 03:00:00Z`)

## Why Kraken, not Binance

Attempting Binance from this host failed with HTTP 451:

> Service unavailable from a restricted location according to 'b. Eligibility'

Kraken succeeded via ccxt and produced 720 lookahead-safe closed bars.

## Commands

Data fetch:

```python
from hermes_quant.data.ccxt_provider import CcxtProvider
import pandas as pd

p = CcxtProvider(exchange_id="kraken")
bars = p.fetch_bars(
    "BTC/USDT",
    "crypto",
    "1h",
    lookback_bars=720,
    as_of=pd.Timestamp.now(tz="UTC"),
)
bars.to_csv("/tmp/btc-usdt-1h-720-kraken.csv", index=False)
```

Single contiguous replay:

```bash
hermes quant backtest \
  --symbol BTC/USDT \
  --asset-class crypto \
  --timeframe 1h \
  --bars-file /tmp/btc-usdt-1h-720-kraken.csv \
  --output-dir /tmp/hq-btc-single \
  --json
```

Purged walk-forward replay:

```bash
hermes quant backtest \
  --symbol BTC/USDT \
  --asset-class crypto \
  --timeframe 1h \
  --bars-file /tmp/btc-usdt-1h-720-kraken.csv \
  --output-dir /tmp/hq-btc-wf \
  --walk-forward \
  --n-splits 3 \
  --json
```

Note: in this dev shell, `hermes_quant.cli` is not module-executable (`python -m hermes_quant.cli` lacks `__main__.py`), so the smoke used the CLI dispatcher directly. The installed Hermes CLI path should still route through `setup_argparse()` + `dispatch()`.

## Results

### Single contiguous 30-day replay

```json
{
  "n_bars": 660,
  "n_decisions": 4,
  "n_fires": 7,
  "n_settlements": 4,
  "total_return_pct": -0.000503488441389699,
  "buy_hold_total_return_pct": 0.06513681588687037,
  "excess_return_vs_buy_hold_pct": -0.06564030432826007,
  "sharpe": -0.5498346958536677,
  "buy_hold_sharpe": 1.21042159126135,
  "max_drawdown_pct": -0.0009020143863084123
}
```

Interpretation:

- Strategy was almost flat (-0.05%) over a period where BTC buy-and-hold gained +6.51%.
- Excess return vs buy-and-hold was **-6.56%**.
- The silence-biased gate avoided drawdown, but also missed the upside.

### 3-fold purged walk-forward replay

```json
{
  "n_splits": 3,
  "mean_excess_return_vs_buy_hold_pct": 0.0015219263514163102,
  "mean_sharpe_delta": 2.1081796997675233,
  "positive_excess_fold_rate": 0.6666666666666666,
  "total_decisions": 2,
  "total_settlements": 2
}
```

Interpretation:

- Walk-forward aggregate excess return was slightly positive (+0.15 percentage points).
- But the sample is too sparse: only **2 total decisions** across all folds.
- This is not enough statistical evidence to graduate to live reactors or RL aggregator work.

## Charter decision

**Do not proceed to RL aggregator or live reactors based on this smoke.**

The charter says:

> If your three-analyst committee on BTC can't beat buy-and-hold risk-adjusted, more analysts won't fix it.

The current evidence is mixed but insufficient:

- Contiguous 30-day replay: **fails** the buy-and-hold gate.
- Walk-forward: superficially positive, but **too few decisions** to trust.

Next engineering target should remain evaluation/data plumbing, not learned policy:

1. Add OHLCV file cache so repeated BTC backtests are cheap and deterministic.
2. Extend the real-data window (e.g. 6-24 months, multiple venues/timeframes).
3. Add minimum-decision / minimum-settlement thresholds to the charter gate.
4. Improve analyst sensitivity only after reproducing this smoke from cached data.

## Follow-ups

- Add `hermes_quant/cli/__main__.py` or a dedicated script entry for easier `python -m` smoke tests.
- Implement V03-7 OHLCV cache under `~/.hermes/quant/cache/<provider>/<symbol>-<timeframe>.parquet`.
- Add a `--provider ccxt:<exchange>` path in the CLI fetch fallback so users can request Kraken/Coinbase explicitly.
