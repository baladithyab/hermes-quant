# HERMES_QUANT_* flag inventory (GENERATED — do not hand-edit)

> Regenerate with `python ops/scripts/quant-flag-inventory.py --write`. This is the
> authoritative list of every flag READ in `hermes_quant/` with its CODE default. The
> enablement runbooks (FEATURE-ENABLEMENT.md, SELFEVOLVE-ENABLEMENT.md) explain the
> eval-gate-to-flip for the capability flags; this table is just the source-of-truth
> defaults so the docs can't silently drift. Empty default = required/path-style (not a
> boolean capability toggle). Every capability flag defaults `'0'` (default-OFF rail).

**87 flags** (resolvable default).

| Flag | Code default | Source |
|---|---|---|
| `HERMES_QUANT_ACCOUNT_LOCK` | `0` | `hermes_quant/react/paper.py:334` |
| `HERMES_QUANT_ADMISSIBILITY` | `0` | `hermes_quant/admissibility/oracle.py:414` |
| `HERMES_QUANT_ALPACA_PAPER` | `0` | `hermes_quant/react/backend.py:152` |
| `HERMES_QUANT_ALPACA_SHADOW` | `0` | `hermes_quant/react/alpaca_shadow.py:40` |
| `HERMES_QUANT_ANALYSTS_USE_REGIME` | `0` | `hermes_quant/regime/regime_aware_confidence.py:26` |
| `HERMES_QUANT_ANALYST_ADMISSION` | `0` | `hermes_quant/advisor.py:564` |
| `HERMES_QUANT_AUTONOMOUS_OPTIONS` | `0` | `hermes_quant/autonomous.py:1528` |
| `HERMES_QUANT_BORROW_COST` | `0` | `hermes_quant/admissibility/borrow_pnl.py:34` |
| `HERMES_QUANT_BROKER_BACKEND` | `` | `hermes_quant/react/backend.py:149` |
| `HERMES_QUANT_CALENDAR_ENABLED` | `0` | `hermes_quant/catalyst/wiring.py:108` |
| `HERMES_QUANT_CATALYST_ONBOARDING` | `0` | `hermes_quant/catalyst/onboarding.py:101` |
| `HERMES_QUANT_COMPOSITE_LEG_OPS` | `0` | `hermes_quant/options/leg_ops.py:82` |
| `HERMES_QUANT_CONVERGENCE` | `0` | `hermes_quant/catalyst/synthesize.py:124` |
| `HERMES_QUANT_DATA_FALLBACK` | `0` | `hermes_quant/data/chain.py:39` |
| `HERMES_QUANT_DELTA_NORMALIZER` | `0` | `hermes_quant/autonomous.py:718` |
| `HERMES_QUANT_DETERMINISTIC_EQUITY` | `0` | `hermes_quant/react/dispatch.py:80` |
| `HERMES_QUANT_DISSENT_CAP` | `` | `hermes_quant/aggregators/bma.py:1243` |
| `HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE` | `0` | `hermes_quant/advisor.py:1384` |
| `HERMES_QUANT_ESTIMATES_ANALYST` | `0` | `hermes_quant/advisor.py:494` |
| `HERMES_QUANT_EVENT_RISK` | `0` | `hermes_quant/advisor.py:1497` |
| `HERMES_QUANT_EVIDENCE_DIR` | `` | `hermes_quant/evidence/store.py:60` |
| `HERMES_QUANT_FRED_MACRO` | `0` | `hermes_quant/data/fred_macro.py:146` |
| `HERMES_QUANT_FUNDAMENTALS_ENABLED` | `0` | `hermes_quant/advisor.py:476` |
| `HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG` | `1` | `hermes_quant/data/fundamentals_provider.py:146` |
| `HERMES_QUANT_GRAPH_MINING` | `0` | `hermes_quant/catalyst/graph_mining.py:158` |
| `HERMES_QUANT_GROUNDING_ENFORCE` | `0` | `hermes_quant/grounding/enforcement.py:156` |
| `HERMES_QUANT_HIERARCHICAL_POOLING` | `0` | `hermes_quant/aggregators/bma.py:293` |
| `HERMES_QUANT_HOME` | `` | `hermes_quant/daemon/tick_lock.py:117` |
| `HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD` | `0.85` | `hermes_quant/research/hypothesis_novelty.py:57` |
| `HERMES_QUANT_IC_DEDUP_AT_INGEST` | `` | `hermes_quant/factors/alpha_zoo.py:333` |
| `HERMES_QUANT_IC_DEDUP_THRESHOLD` | `0.99` | `hermes_quant/factors/ic_dedup.py:75` |
| `HERMES_QUANT_INSIDER_ANALYST` | `0` | `hermes_quant/advisor.py:507` |
| `HERMES_QUANT_INSIDER_ENABLED` | `0` | `hermes_quant/evidence/adapters/form4.py:93` |
| `HERMES_QUANT_IRON_CONDOR` | `0` | `hermes_quant/options/structure_select.py:155` |
| `HERMES_QUANT_JOURNAL_PATH` | `` | `hermes_quant/journal/writer.py:33` |
| `HERMES_QUANT_KNOWLEDGE_CUTOFF` | `` | `hermes_quant/eval/stockbench.py:47` |
| `HERMES_QUANT_LLM_BUDGET` | `0` | `hermes_quant/agents/llm_budget.py:176` |
| `HERMES_QUANT_LLM_BUDGET_DIR` | `` | `hermes_quant/agents/llm_budget.py:185` |
| `HERMES_QUANT_MCP_READS_ENABLED` | `0` | `hermes_quant/data/mcp_bridge.py:194` |
| `HERMES_QUANT_MEMORY_INJECT` | `1` | `hermes_quant/aggregators/llm_committee.py:312` |
| `HERMES_QUANT_MEMORY_SPLIT` | `0` | `hermes_quant/aggregators/llm_committee.py:333` |
| `HERMES_QUANT_MONTHLY_META_RETRO` | `0` | `hermes_quant/memory/meta_retro.py:70` |
| `HERMES_QUANT_MULTILEG_REACTOR` | `0` | `hermes_quant/react/mleg_fill.py:116` |
| `HERMES_QUANT_OPENBB` | `0` | `hermes_quant/advisor.py:495` |
| `HERMES_QUANT_OPENBB_LIVE` | `0` | `hermes_quant/advisor.py:330` |
| `HERMES_QUANT_OPEN_GUARD` | `1` | `hermes_quant/risk/open_guard.py:304` |
| `HERMES_QUANT_OPTIONS_EVIDENCE_GATE` | `0` | `hermes_quant/autonomous.py:2352` |
| `HERMES_QUANT_OPTIONS_GATE` | `0` | `hermes_quant/risk/options_gate.py:529` |
| `HERMES_QUANT_OPTIONS_LIVE_CHAIN` | `0` | `hermes_quant/options/data.py:427` |
| `HERMES_QUANT_OPTIONS_PERCEIVE` | `0` | `hermes_quant/autonomous.py:2388` |
| `HERMES_QUANT_OVERNIGHT_DRIFT` | `0` | `hermes_quant/advisor.py:544` |
| `HERMES_QUANT_PAPER_INITIAL_CASH` | `` | `hermes_quant/state/portfolio_state.py:255` |
| `HERMES_QUANT_PAPER_SLIPPAGE_MODEL` | `v0.2` | `hermes_quant/react/deterministic_equity.py:523` |
| `HERMES_QUANT_PDR_CORE_SHADOW` | `0` | `hermes_quant/advisor.py:1486` |
| `HERMES_QUANT_PER_POSITION_STOP` | `0` | `hermes_quant/autonomous.py:2080` |
| `HERMES_QUANT_PIT_UNIVERSE` | `0` | `hermes_quant/universe/point_in_time.py:54` |
| `HERMES_QUANT_PLAYS_OPEN` | `0` | `hermes_quant/playbook/play_loader.py:206` |
| `HERMES_QUANT_PORTFOLIO_CAPS` | `` | `hermes_quant/autonomous.py:2159` |
| `HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING` | `0` | `hermes_quant/pdr_core/portfolio_sizing.py:70` |
| `HERMES_QUANT_POST_LOSS_COOLDOWN` | `0` | `hermes_quant/autonomous.py:784` |
| `HERMES_QUANT_PREWARM_WORKERS` | `` | `hermes_quant/playbook/scorers.py:816` |
| `HERMES_QUANT_RAIL_LOCK_TIMEOUT_S` | `` | `hermes_quant/autonomous.py:87` |
| `HERMES_QUANT_REFLECTION` | `1` | `hermes_quant/react/deterministic_equity.py:696` |
| `HERMES_QUANT_REFLECTOR_LLM` | `0` | `hermes_quant/memory/reflector.py:559` |
| `HERMES_QUANT_REGIME_HMM` | `` | `hermes_quant/regime/detector.py:123` |
| `HERMES_QUANT_RESEARCH_DEBATE` | `0` | `hermes_quant/aggregators/llm_committee.py:1145` |
| `HERMES_QUANT_RESEARCH_LOOP` | `0` | `hermes_quant/research/research_loop.py:88` |
| `HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK` | `0` | `hermes_quant/research/risk_tier.py:221` |
| `HERMES_QUANT_RUN_BACKTEST` | `0` | `hermes_quant/cli/ablate.py:273` |
| `HERMES_QUANT_SATURATION` | `0` | `hermes_quant/analysts/semantic.py:141` |
| `HERMES_QUANT_SEMANTIC_ENABLED` | `1` | `hermes_quant/advisor.py:526` |
| `HERMES_QUANT_SHADOW_RULE_MINING` | `0` | `hermes_quant/shadow/rule_mining.py:162` |
| `HERMES_QUANT_SIGNED_EQUITY` | `0` | `hermes_quant/state/portfolio_state.py:214` |
| `HERMES_QUANT_SLIPPAGE_GATE` | `0` | `hermes_quant/pdr_core_adapter.py:75` |
| `HERMES_QUANT_SLIPPAGE_HAIRCUT` | `0` | `hermes_quant/risk/slippage_haircut.py:70` |
| `HERMES_QUANT_STACKING` | `0` | `hermes_quant/aggregators/bma.py:277` |
| `HERMES_QUANT_STRUCTURE_SELECT` | `0` | `hermes_quant/options/structure_select.py:133` |
| `HERMES_QUANT_TAKE_PROFIT_SWEEP` | `0` | `hermes_quant/autonomous.py:1652` |
| `HERMES_QUANT_TICK_LOCK` | `1` | `hermes_quant/react/paper.py:261` |
| `HERMES_QUANT_TICK_LOCK_TIMEOUT_S` | `` | `hermes_quant/daemon/tick_lock.py:88` |
| `HERMES_QUANT_TP_TRANCHE` | `0` | `hermes_quant/autonomous.py:1661` |
| `HERMES_QUANT_TRADER_LLM` | `0` | `hermes_quant/agents/trader.py:504` |
| `HERMES_QUANT_TREND_VELOCITY` | `0` | `hermes_quant/catalyst/synthesize.py:211` |
| `HERMES_QUANT_VERTICAL_SPREADS` | `0` | `hermes_quant/options/structure_select.py:143` |
| `HERMES_QUANT_WATCHLIST_CAP_TRIM` | `0` | `hermes_quant/playbook/watchlist_evolution.py:169` |
| `HERMES_QUANT_WATCH_REGISTRY` | `0` | `hermes_quant/autonomous.py:1670` |
| `HERMES_QUANT_WEEKLY_RETRO` | `1` | `hermes_quant/aggregators/llm_committee.py:350` |
