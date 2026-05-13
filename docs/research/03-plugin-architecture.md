# Research Lens: Hermes Plugin Architecture for hermes-quant Daemon + Kronos Wrapping

**Authored by orchestrator** after Kimi K2.6 dispatch failure (8579 reasoning tokens, empty content). Provenance: synthesizing from hermes-s2s reference plugin (full source loaded in context), `references/plugin-authoring.md`, and Kronos repo material loaded earlier. Mark for re-review when route bug fixed.

## 1. How does a Hermes plugin host a long-running background loop?

The hermes-quant daemon needs to tick every 1m / 5m / 1h, fetch bars, run analysts, emit signals. Four candidate hosting patterns; only one is right for this case.

### (a) `cronjob` tool to schedule ticks ❌

The Hermes `cronjob` tool spins up a fresh agent process per tick with full LLM loop, model invocation, and tool dispatch. That's correct for "every morning summarize my email" but completely wrong for a 1-min trading tick — the cold-start tax (model load, plugin import, tool registry build) is ~2-5 seconds, the LLM call dominates the tick budget even when no thinking is needed, and you're paying token spend per tick whether or not the agent has anything to decide. Cron is for *agent tasks*, not for *signal computation*. **Reject.**

### (b) `terminal(background=True)` from a tool handler ⚠️

A tool handler could spawn the daemon via `terminal(background=True, command="hermes-quant-daemon ...")`, returning the session_id. This works but the daemon is bound to the parent agent's process tree — gateway restart kills it, agent session reset kills it, no auto-restart, no health monitoring. Acceptable for one-shot dev work; not for "this is funding the lights" production posture. **Reject for primary path; keep as fallback for ad-hoc backtests.**

### (c) Custom asyncio task in `register()` ⚠️

The plugin's `register(ctx)` could spawn an asyncio task on the gateway's event loop. This is what hermes-s2s's audio bridge does — it works because the gateway IS the host process and the loop is long-lived. Two problems for hermes-quant: (1) the loop runs only when the gateway is running — if user is in CLI-only mode there's no daemon, (2) heavy compute (Kronos forward pass, model inference) on the gateway's event loop will starve other adapters and degrade Discord/Telegram responsiveness. The hermes-s2s skill has a documented gotcha for exactly this — voice processing was originally inline and starved the gateway. **Reject for primary path.**

### (d) External process the plugin starts and monitors via systemd / launchd ✅

Pattern: hermes-quant ships a `hermes-quant-daemon` console_scripts entry point. The plugin's CLI subcommand `hermes quant start` does:
1. Verify config and credentials present
2. Generate a systemd user unit at `~/.config/systemd/user/hermes-quant.service` (or launchd plist on macOS)
3. `systemctl --user enable --now hermes-quant.service`

The daemon is fully decoupled from the gateway. It reads its config from `~/.hermes/config.yaml`, writes ticks to `~/.hermes/quant/ticks.db` (SQLite WAL mode) and signals to `~/.hermes/quant/signals.jsonl`, and logs to `~/.hermes/logs/quant-daemon.log` via Python's stdlib logging.

The Hermes plugin tools (`quant_status`, `quant_show_signals`, etc.) **read** the SQLite/JSONL files — they don't drive the loop. This gives clean separation:
- Daemon: stateless control flow, restartable, low-latency signal computation
- Plugin tools: read-only views into daemon state, surface to the agent
- CLI subcommands: control-plane (start/stop/restart, config edit, backtest)

**Accept. This is the primary path.**

For Windows/WSL where systemd-user may not be available, the install script falls back to a tmux-based detached session pattern (verified in the hermes-agent skill's "Spawning Additional Hermes Instances" section).

### Logging and observability

The daemon emits structured JSON logs to `~/.hermes/logs/quant-daemon.log` plus rotates via `logging.handlers.RotatingFileHandler`. Tick metadata goes to a SQLite `ticks` table with columns `(ts, asset, analyst_views_json, aggregated_signal_json, action, reason)`. `quant_status` tool reads the last N ticks; `quant_show_signals` reads the JSONL bus directly. MLflow integration is optional — if `MLFLOW_TRACKING_URI` is set, the daemon mirrors per-tick metrics; if not, it's silent.

## 2. Wrapping the Kronos foundation model as an analyst

Kronos models on HuggingFace: `NeoQuasar/Kronos-Tokenizer-base` + `NeoQuasar/Kronos-small` (24.7M params, max_context=512) or `NeoQuasar/Kronos-base` (102M). MIT license, autoregressive decoder transformer over BSQ-quantized OHLCV tokens. Inference returns probabilistic forecasts: `predictor.predict(df, x_timestamp, y_timestamp, pred_len, T=1.0, top_p=0.9, sample_count=N)` — sampling N forecast paths and averaging gives mean + per-quantile bands.

### The wrapping contract

```python
class KronosAnalyst:
    name = "kronos-small"
    timeframes = ["5m", "1h"]
    asset_classes = ["crypto", "equity"]

    def __init__(self, model_id="NeoQuasar/Kronos-small",
                 tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
                 device="auto", lookback=400, pred_len=24,
                 sample_count=20):
        self._predictor = None  # lazy
        self._cfg = locals()

    def _ensure_loaded(self):
        if self._predictor is not None:
            return
        from model import Kronos, KronosTokenizer, KronosPredictor
        tok = KronosTokenizer.from_pretrained(self._cfg["tokenizer_id"])
        m = Kronos.from_pretrained(self._cfg["model_id"])
        device = _resolve_device(self._cfg["device"])
        self._predictor = KronosPredictor(m, tok, device=device, max_context=512)

    def analyze(self, ctx: MarketContext) -> AnalystView:
        self._ensure_loaded()
        df = ctx.bars[-self._cfg["lookback"]:][["open","high","low","close","volume","amount"]]
        x_ts = ctx.bars["timestamp"][-self._cfg["lookback"]:]
        y_ts = _generate_future_timestamps(ctx.bars, self._cfg["pred_len"], ctx.timeframe)
        # Sample N paths for uncertainty
        paths = self._predictor.predict(df, x_ts, y_ts, pred_len=self._cfg["pred_len"],
                                        T=1.0, top_p=0.9,
                                        sample_count=self._cfg["sample_count"])
        return self._views_from_paths(paths, ctx.last_close)
```

### Lazy loading is mandatory

Loading Kronos-small from HF is ~65 MB download + ~200 MB RAM. The plugin's `register()` MUST NOT eagerly load it — that would break installs on machines without internet at startup, slow gateway boot by ~5-10s, and waste RAM if the user never actually uses the analyst. Lazy-load on first `analyze()` call. Cache the loaded predictor on the analyst instance, not in module globals (test isolation).

### Calibration: forecast distribution → confidence score

Kronos returns N sampled paths over `pred_len` future bars. Translation to `AnalystView`:

- **direction**: sign of (median_path_close[-1] - last_close). If the central tendency is up, direction=+1; down, -1.
- **magnitude**: `(median_path_close[-1] - last_close) / last_close` — expected percent return over the prediction horizon.
- **confidence**: Use the *agreement rate* across sampled paths. `confidence = max(P(path_close[-1] > last_close), P(path_close[-1] < last_close))` — fraction of paths agreeing with the directional view. Alternative: 1 - normalized_IQR; if the inter-quartile range is small relative to last_close, confidence is high.
- **horizon**: `pred_len * timeframe`. A pred_len=24 on 1h bars is a 24-hour view.

This calibration is approximate. The right way is to back-test the calibration: did sample-count=20 paths with 0.8 agreement actually produce 80% directional accuracy on held-out data? If not, fit an isotonic regression over the agreement rate -> realized accuracy mapping. v0.1 ships with the naive calibration and a CI test that verifies agreement-rate vs. realized-accuracy correlation > 0.5 on a fixture dataset; v0.2 adds the isotonic post-processor.

### Inference latency budget

For a 5-min tick on Kronos-small (24.7M params, max_context=512, sample_count=20):
- CPU (modern x86): ~3-5s per analyze() call. Acceptable — the tick has 300s of slack.
- CUDA on a 5090 / V100 / 3050: ~200-400ms. Trivial.
- Apple Silicon (mps): ~1-2s. Fine.

The user's environment includes Yggdrasil (Huginn 4xV100, Muninn 2x3050) but the daemon will run on the local dev machine for v0.1 — no cluster dependency. CPU-only fallback works for 5-min and 1h ticks; only fails for sub-minute frequencies which we're explicitly NOT shipping in v0.1.

### Optional dependency hygiene

`pyproject.toml` puts Kronos behind an optional extra:
```toml
[project.optional-dependencies]
kronos = ["torch>=2.0", "transformers>=4.35", "huggingface_hub>=0.20", "einops>=0.7"]
all = ["hermes-quant[kronos,kairos,backtest,freqtrade]"]
```
Installation:
```
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-quant'[kronos]'
```
The `KronosAnalyst` import wraps in try/except — if torch isn't installed, the analyst registers as `unavailable` in `quant_status` and the aggregator silently drops it from the ensemble.

### Kairos for crypto fine-tune

`Shadowell/Kairos-base-crypto` is the BTC/ETH 1-min fine-tune of Kronos-base; it reports rank-IC +0.076 / ICIR +0.484 on 2-asset 2-year spot — the only direction with consistent alpha in the public Kronos derivative landscape. v0.1 ships **two** Kronos analyst variants: `kronos-small` (general) and `kairos-base-crypto` (fine-tuned for the BTC/ETH MVP). Same wrapping code, different model_id. The aggregator can compare them.

## 3. Analyst module protocol — concrete interface design

```python
from typing import Protocol, Literal, runtime_checkable
from dataclasses import dataclass
import pandas as pd

@dataclass(frozen=True)
class MarketContext:
    asset: str                   # e.g. "BTC/USDT", "AAPL"
    timeframe: str               # "1m", "5m", "1h", "1d"
    bars: pd.DataFrame           # OHLCV with 'timestamp' column, last row = current
    last_close: float
    last_volume: float
    asof: pd.Timestamp           # decision timestamp
    extras: dict                 # provider-specific extras (orderbook, news, etc.)

@dataclass(frozen=True)
class AnalystView:
    analyst: str                 # name of the analyst module
    direction: Literal[-1, 0, +1]
    magnitude: float             # expected return as fraction (e.g. 0.012 = 1.2%)
    confidence: float            # in [0, 1]
    horizon: str                 # "5m", "1h", "1d" — over what window the view holds
    rationale: str | None = None # optional human-readable explanation
    metadata: dict | None = None # provider-specific extras (CIs, sub-scores, etc.)

@runtime_checkable
class Analyst(Protocol):
    name: str
    timeframes: list[str]
    asset_classes: list[str]
    enabled: bool

    def analyze(self, ctx: MarketContext) -> AnalystView | None: ...
    def health(self) -> dict: ...   # for `quant_doctor`
```

### Sync vs async

Sync. Tick frequencies are 1m+; LLM-based analysts (news-LLM via OpenRouter scatter) can use `delegate_task` internally which is sync from the analyst's perspective. The aggregator runs analysts in `concurrent.futures.ThreadPoolExecutor` for I/O-bound analysts (network calls), in-thread for CPU-bound. v0.2 may migrate to async if we add tick frequencies < 1 minute.

### Pure vs stateful

Mixed. The protocol is pure (deterministic given context) but allows internal state (model weights, rolling caches, calibration history). The contract: same `MarketContext` should produce same `AnalystView` *modulo* sampling randomness (Kronos's `sample_count` introduces noise; that's fine — the aggregator is robust to noise). Stateful learning is encapsulated inside the analyst (it can update its own calibration given realized outcomes via a separate `update(realized: RealizedOutcome)` method that's called by the daemon's settlement loop).

### Versioning

The `Analyst` Protocol is the v1 contract. Future versions add fields with defaults to `MarketContext` and `AnalystView` (e.g., `MarketContext.orderbook` in v1.1, `AnalystView.regime_class` in v1.2). The aggregator ignores fields it doesn't recognize. This is the same pattern as Hermes core's gradual schema evolution. Concrete rule: dataclass fields ADDED ONLY, never renamed or removed before a major version bump.

## 4. Aggregator design space (v0.1, no RL)

### Bayesian Model Averaging

Implementable but the math gets ugly when analysts emit heterogeneous outputs (some directional only, some with magnitude+confidence). The clean version:
- Each analyst's prior weight `w_a` ∝ recent forecast accuracy (e.g., last 30 days realized P(direction correct) - 0.5)
- Aggregated direction = `sign(sum_a w_a * confidence_a * direction_a)`
- Aggregated magnitude = `sum_a w_a * confidence_a * magnitude_a / sum_a w_a * confidence_a` (confidence-weighted average)
- Aggregated confidence = `1 - exp(-disagreement_penalty * Var(direction_a))` where disagreement reduces confidence

This is the v0.1 baseline. ~80 lines of code. Beats a simple equal-weight ensemble in DeepSeek's research note.

### Logistic stacking baseline

Strictly simpler: features = vector of (direction, magnitude, confidence) per analyst (= 3*N floats); target = next-bar return sign (or quantile bucket); fit `LogisticRegression` on a rolling window. Output probability → confidence; sign → direction. ~30 lines using sklearn. v0.1 ships **both** Bayesian and logistic stacking and lets the user pick via config. The RL aggregator (v0.2) replaces this with a learned policy.

### Disagreement-aware position sizing

Critical detail: high analyst disagreement should reduce position size, not just confidence. Concrete rule:
```python
disagreement = np.var([v.direction * v.confidence for v in views])
position_scaler = max(0, 1 - 2 * disagreement)  # 0 disagreement → 1, full → 0
```
This is the "silence by default" prior from Eidolon's PDR architecture, ported to trading. When analysts disagree, the system defaults to flat.

### Calibration

Track aggregator confidence vs realized direction-correct rate in a rolling 30-day window. Surface in `quant_doctor` as a calibration plot (or a single "calibration_ece" number — Expected Calibration Error). If ECE > 0.1, warn the user. v0.2 adds isotonic post-processing.

## 5. Risk gate — concrete v0.1 rules

```python
def gate(signal: AggregatedSignal, market: MarketState, portfolio: Portfolio,
         risk_cfg: RiskConfig) -> Optional[Action]:
    # 1. Action space discrete (DeepSeek's anti-leverage-gambling rule)
    target_size = signal.direction * min(
        risk_cfg.max_position_pct,
        QUARTER_KELLY * signal.magnitude / max(market.volatility, 1e-4),
    )
    target_size = round_to_step(target_size, step=risk_cfg.action_step)  # e.g. 0.05
    target_size = clip(target_size, -risk_cfg.max_position_pct, +risk_cfg.max_position_pct)

    # 2. Transaction-cost-aware threshold
    expected_edge = abs(signal.magnitude) * signal.confidence
    transaction_cost = market.commission + 0.5 * market.spread + market.slippage_estimate
    if expected_edge < risk_cfg.cost_multiple * transaction_cost:
        return None  # silence — not worth the friction

    # 3. Drawdown circuit breaker
    if portfolio.drawdown_pct > risk_cfg.max_drawdown_pct:
        return Action(target_position=0)  # flatten and pause

    # 4. Daily loss circuit breaker
    if portfolio.daily_loss_pct > risk_cfg.max_daily_loss_pct:
        return Action(target_position=0, halt_until=next_session_open())

    # 5. Position change vs current
    delta = target_size - portfolio.current_position
    if abs(delta) < risk_cfg.min_trade_size:
        return None  # don't churn

    return Action(target_position=target_size)
```

Rules are HARD, not learned. The RL aggregator (v0.2) cannot circumvent these — it only emits the candidate signal, gate decides whether to act. This matches the deep-work-loop's "guardrails are deterministic" discipline.

### Kelly fractional with sane defaults

```python
QUARTER_KELLY = 0.25
risk_cfg = RiskConfig(
    max_position_pct=0.20,         # never more than 20% NAV in one position
    action_step=0.05,              # discrete sizes 0, 0.05, 0.10, 0.15, 0.20
    cost_multiple=2.0,             # edge must be 2x transaction cost
    max_drawdown_pct=0.15,         # 15% from peak → flatten
    max_daily_loss_pct=0.05,       # 5% in a day → halt until next session
    min_trade_size=0.02,           # don't trade below 2% delta
)
```

Saner than freqtrade defaults for a learning-mode v0.1.

## 6. Three plugin-authoring gotchas specific to hermes-quant

### (a) Heavy ML deps in pyproject extras break install

Kronos pulls torch + transformers + huggingface_hub. On a fresh box without GPU, `pip install hermes-quant[kronos]` will install CPU-only torch (~800 MB). On a CUDA box, the user MAY want CUDA torch — but pip's torch index resolution is broken by default. The README install section MUST give explicit guidance:

```bash
# CPU-only (works everywhere)
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-quant'[all]'

# CUDA 12.1 (Linux/WSL with NVIDIA GPU)
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-quant'[all]' \
    --extra-index-url https://download.pytorch.org/whl/cu121
```

A `quant_doctor` tool reports the resolved torch version + CUDA availability so the user can verify post-install.

### (b) SQLite tick log file growth

A 1-min tick × 30 days × 5 assets = 216,000 rows. With analyst views serialized as JSON in each row, that's ~50-100 MB. Acceptable for SQLite, but:
- Use WAL mode (`PRAGMA journal_mode=WAL`) for concurrent reads from `quant_status` while daemon writes
- Auto-prune ticks older than `retention_days` (default 90) on daemon startup
- Vacuum monthly via `PRAGMA incremental_vacuum`
- Backup the DB to `~/.hermes/quant/backups/ticks-YYYY-MM-DD.db` weekly

For higher tick frequencies (sub-minute), migrate to Parquet column-store. Not needed for v0.1.

### (c) MLflow tracking server vs file-based

MLflow has two modes:
- **File mode** (`MLFLOW_TRACKING_URI=file:///path`) — simple, no server, but the UI requires `mlflow ui` running locally
- **Server mode** (`MLFLOW_TRACKING_URI=http://...`) — needs a server (which the user has at `mlflow.muspelheim.svc.cluster.yggdrasil.io:5000` per the eidolon-cluster-ops skill)

For a plugin we ship, **default to file mode at `~/.hermes/quant/mlruns`**. If `MLFLOW_TRACKING_URI` is set in the environment, the daemon respects it. Don't depend on the cluster's MLflow being reachable — that's a deployment-specific assumption that breaks for users without the eidolon cluster.

### (d) [Bonus] Discord slash command for live status

Per the hermes-s2s plugin's deferred-slash-install pattern, adding `/quant status` to Discord requires:
1. Install via `pre_gateway_dispatch` hook (NOT `register()` — bot not logged in yet)
2. Force `await tree.sync()` after `add_command` (fingerprint-skip blocks the implicit re-sync)
3. Sentinel attribute on the tree to prevent double-install on subsequent dispatches

The hermes-s2s `voice/slash.py` is the working reference. Copy the pattern verbatim for v0.1; refactor to a Hermes-core helper later if more plugins need it.

### (e) [Bonus] Don't let the daemon write to the gateway's session_id

The daemon emits signals to `~/.hermes/quant/signals.jsonl` and `ticks.db`. It must NOT write to Hermes's `~/.hermes/sessions/*.db` — that's the chat-history store. Hermes-quant has its own state, separately namespaced. Plugin tools READ from quant's state and surface to the agent's session, but the daemon's clock is decoupled from any specific session.
