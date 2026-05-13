# Research Lens: Trading Framework Integration for hermes-quant

## 1. Framework Comparison Matrix

Evaluating the landscape for an ARIA-powered (LLM/Agentic) trading framework requires balancing backtest fidelity, live-execution reliability, and the architectural flexibility to handle asynchronous, non-deterministic agent inference times.

*   **freqtrade + FreqAI** ([freqtrade/freqtrade](https://github.com/freqtrade/freqtrade))
    *   *Maturity/License:* Highly mature, GPL-3.0.
    *   *Broker/Exchange:* Crypto-native (via CCXT). No TradFi support.
    *   *Paper/Backtest:* Excellent paper trading. Vectorized backtester (fast, but assumes static logic, which is hard for dynamic LLM agents).
    *   *ML/RL:* FreqAI is best-in-class for continuous training/inferencing of traditional ML models.
    *   *API/Plugins:* Highly opinionated `IStrategy` class. Hard to use as a library; it wants to be the host framework.
    *   *Verdict:* The obvious incumbent for crypto, but its synchronous loop struggles with 10-second LLM inference times unless heavily modified.
*   **NautilusTrader** ([nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader))
    *   *Maturity/License:* Maturing rapidly, MIT.
    *   *Broker/Exchange:* TradFi (Interactive Brokers, FIX) + Crypto (Binance, dYdX, etc.).
    *   *Paper/Backtest:* Institutional-grade. Rust core, event-driven, ultra-high fidelity (tick/orderbook level).
    *   *ML/RL:* Bring-your-own. No native FreqAI equivalent.
    *   *API/Plugins:* Modern, fully typed Python API over a Rust engine. Highly extensible.
    *   *Verdict:* The best technical foundation for a modern, multi-asset system, but carries a steeper learning curve and heavier build process.
*   **Backtrader** ([mementum/backtrader](https://github.com/mementum/backtrader))
    *   *Maturity/License:* Legacy, GPL-3.0.
    *   *Broker/Exchange:* Broad (IBKR, Oanda, etc.).
    *   *Paper/Backtest:* Pure Python, event-driven. Slow for large datasets.
    *   *Verdict:* Effectively unmaintained since 2019. Do not use for greenfield projects.
*   **VectorBT / VectorBTpro** ([polakowo/vectorbt](https://github.com/polakowo/vectorbt))
    *   *Maturity/License:* Mature, Apache-2.0 (Pro is commercial).
    *   *Broker/Exchange:* Agnostic (data-in, signals-out). Pro adds live execution.
    *   *Paper/Backtest:* Insanely fast (Numba-compiled pandas operations).
    *   *Verdict:* Incredible for parameter sweeping, but poor for path-dependent, stateful LLM agents that cannot be vectorized.
*   **QuantConnect / LEAN** ([QuantConnect/Lean](https://github.com/QuantConnect/Lean))
    *   *Maturity/License:* Enterprise-grade, Apache-2.0.
    *   *Broker/Exchange:* Universal (TradFi + Crypto).
    *   *Paper/Backtest:* Excellent, but heavily tied to their cloud data.
    *   *Verdict:* C# core with Python wrappers. Running locally requires heavy Docker setups. Too monolithic for a lightweight Hermes plugin.
*   **Hummingbot** ([hummingbot/hummingbot](https://github.com/hummingbot/hummingbot))
    *   *Maturity/License:* Mature, Apache-2.0.
    *   *Verdict:* Purpose-built for high-frequency market making and arbitrage. Not suitable for directional, agent-driven alpha strategies.
*   **CCXT / CCXT Pro** ([ccxt/ccxt](https://github.com/ccxt/ccxt))
    *   *Maturity/License:* Industry standard, MIT.
    *   *Verdict:* Not a framework. It is an exchange normalization library. Essential if building natively for crypto.
*   **Alpaca SDK** ([alpacahq/alpaca-py](https://github.com/alpacahq/alpaca-py))
    *   *Maturity/License:* Official SDK, Apache-2.0.
    *   *Verdict:* Broker-specific API wrapper. Excellent for execution, zero backtesting capability.

## 2. Integration Patterns

Given `hermes-quant` is an ARIA-powered Hermes Agent plugin, it must handle asynchronous reasoning (LLMs take seconds to reply). This fundamentally breaks standard synchronous `next()` loops found in traditional backtesters.

### (A) Hermes-native plugin (Build from scratch)
*Architecture:* `hermes-quant` uses `ccxt` + `alpaca-py` for execution, `yfinance` for data, and implements its own simple backtest/paper-trade loop.
*   **Pros:** Zero framework bloat. Total control over the async agent loop.
*   **Cons:** Rebuilding a backtester is a notorious engineering tar pit. You will spend months fighting lookahead bias, corporate actions (splits/dividends), and slippage modeling.
*   **90 Minutes:** A cron-job script that fetches daily bars, asks the LLM for a signal, and places a market order.
*   **90 Days:** A buggy, untrustworthy backtester that overstates historical performance.

### (B) Freqtrade-host with Hermes overlay
*Architecture:* Freqtrade runs the main loop. `hermes-quant` is imported as a library inside a custom Freqtrade `IStrategy`.
*   **Pros:** Leverages the user's existing Freqtrade knowledge. Solves order management, trailing stops, and risk management out of the box.
*   **Cons:** Freqtrade is crypto-only. Furthermore, Freqtrade's backtester is vectorized; it cannot easily simulate a stateful LLM agent that takes 5 seconds to "think" per candle.
*   **90 Minutes:** A Freqtrade strategy that makes blocking HTTP calls to a Hermes agent (will fail in backtesting due to speed).
*   **90 Days:** Fighting Freqtrade's architecture to support non-deterministic, slow agent inference.

### (C) NautilusTrader-host with Hermes overlay
*Architecture:* NautilusTrader runs the event loop. Hermes acts as an asynchronous `DataClient` or `ExecutionClient`.
*   **Pros:** Rust-native speed. True event-driven architecture perfectly supports async agent inference. Supports TradFi (Alpaca) and Crypto natively.
*   **Cons:** Steep learning curve. Heavy dependencies.
*   **90 Minutes:** Struggling to compile the environment and understand the `Actor` model.
*   **90 Days:** An institutional-grade, multi-asset trading system with perfect backtest-to-live fidelity.

### (D) Sidecar (Decoupled Architecture)
*Architecture:* `hermes-quant` runs as a standalone daemon. It ingests data, runs ARIA agents, and writes signals (e.g., `{"asset": "BTC", "action": "buy", "confidence": 0.8}`) to a local Redis queue, ZMQ, or REST endpoint. Freqtrade or Nautilus runs as a pure execution engine, consuming these signals.
*   **Pros:** Complete separation of concerns. The agent can take as long as it wants to think. The execution engine handles risk, sizing, and routing. You can backtest the agent by feeding it historical data and logging signals, then feeding those signals into Freqtrade's backtester.
*   **Cons:** Requires managing two processes.
*   **90 Minutes:** Hermes writes a JSON signal to disk; a simple Freqtrade strategy reads the JSON and executes.
*   **90 Days:** A robust, language-agnostic microservice architecture where agents and execution engines scale independently.

## 3. Alpaca SDK Specifics

The user explicitly requested Alpaca integration.

*   **Canonical Library:** Use `alpaca-py` ([alpacahq/alpaca-py](https://github.com/alpacahq/alpaca-py)). The older `alpaca-trade-api` is deprecated and lacks support for newer endpoints.
*   **Free Tier Limits:**
    *   *Rate Limits:* 200 requests per minute.
    *   *Market Data:* The free tier uses the **IEX** (Investors Exchange) feed. This is real-time, but IEX only represents ~2-3% of total market volume. You will see gaps in intraday data. SIP (all exchanges) requires a paid plan ($99/mo).
*   **Crypto on Alpaca:** Alpaca supports crypto, but it routes through partner exchanges. Spreads can be wider and fees higher (up to 0.25% per trade) compared to native Binance/Kraken via CCXT. Use Alpaca for equities; use CCXT for crypto.
*   **Auth Patterns:** For an unattended Hermes agent, use **API Key + Secret Key**. OAuth is strictly for building B2B/B2C applications where *other* users log in with their Alpaca accounts.
*   **Minimal Snippet (alpaca-py):**

```python
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# 1. Fetch Data
data_client = StockHistoricalDataClient("API_KEY", "SECRET_KEY")
request_params = StockBarsRequest(
    symbol_or_symbols=["SPY"],
    timeframe=TimeFrame.Day,
    start="2023-01-01"
)
bars = data_client.get_stock_bars(request_params)

# 2. Place Paper Trade
trading_client = TradingClient("API_KEY", "SECRET_KEY", paper=True)
order_data = MarketOrderRequest(
    symbol="SPY",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.GTC
)
order = trading_client.submit_order(order_data)
print(f"Order ID: {order.id}")
```

## 4. yfinance Bootstrap Path

`yfinance` ([ranaroussi/yfinance](https://github.com/ranaroussi/yfinance)) scrapes Yahoo Finance. It is the standard zero-cost bootstrap tool.

*   **Coverage:** Unmatched for free tools. US Equities, ETFs, global indices, and crypto (via `-USD` suffix, e.g., `BTC-USD`).
*   **Latency & Realities:** Equity data is subject to exchange delays (typically 15 minutes for real-time quotes). It is strictly an End-of-Day (EOD) or low-frequency tool.
*   **Rate Limits:** Because it scrapes an undocumented API, aggressive polling (e.g., fetching 1-minute bars every minute) will result in temporary IP bans.
*   **Alternatives:** When `yfinance` breaks, migrate to Alpaca's data API (for US Equities), Tiingo (excellent cheap EOD/intraday data), or Polygon.io.
*   **Recommendation for v0.1:** **Yes, with caveats.** It is perfectly acceptable for a daily-bar strategy where the agent runs once a day after market close. It is absolutely unviable for intraday trading.

## 5. Recommendation

### Primary Path for v0.1.0: Option D (Sidecar) with Freqtrade
**Why:** The user already has Freqtrade and NostalgiaForInfinity cloned. Freqtrade is battle-tested for execution, risk management, and trailing stops. By decoupling `hermes-quant` into a sidecar, you avoid the nightmare of forcing a slow, asynchronous LLM agent into Freqtrade's synchronous vectorized backtester.
**How:** `hermes-quant` runs as a daemon, fetches daily data via `yfinance` or `ccxt`, runs the ARIA agent, and outputs a target portfolio state (e.g., `{"BTC": 0.5, "ETH": 0.5}`) to a local JSON file or Webhook. Freqtrade runs a lightweight strategy that simply reads this state and executes the necessary trades to align the portfolio.
**What we lose:** A single-process architecture. The user must run two terminals (Hermes + Freqtrade).

### Secondary Path for v0.2.0: Option C (NautilusTrader)
**Why:** Once the user wants to trade US Equities via Alpaca (Freqtrade cannot do this) or requires tick-level backtest fidelity for their agents, NautilusTrader is the only modern open-source framework capable of handling it. Its event-driven Rust core natively supports the asynchronous nature of agentic workflows.

## 6. Three Concrete Gotchas

1.  **yfinance Silent Data Corruption:** `yfinance` will occasionally return rows with `NaN` values, or volume strictly equal to `0` for halted tickers or API glitches. Furthermore, real-time intraday fetches often fail to adjust for splits/dividends correctly until the next day. *Mitigation:* Always implement a data validation layer (e.g., `df.dropna()`, check for zero volume) before passing data to the ARIA agent.
2.  **Alpaca Paper vs. Live Slippage:** The Alpaca Paper API fills market orders immediately at the last quoted price. The Live API routes to real market makers, resulting in slippage, partial fills, or rejected orders during high volatility. *Mitigation:* Never use `MarketOrderRequest` in live trading for illiquid assets; always use `LimitOrderRequest` with a calculated buffer, and implement partial-fill handling in your execution logic.
3.  **Freqtrade Strategy Reload Wipes State:** Freqtrade allows hot-reloading of strategies. However, if your Hermes sidecar or Freqtrade strategy maintains any in-memory state (e.g., conversation history for the LLM, or rolling agent confidence scores), a strategy reload will wipe it. *Mitigation:* The ARIA agent must be completely stateless, recovering its context entirely from disk/database on every invocation.
