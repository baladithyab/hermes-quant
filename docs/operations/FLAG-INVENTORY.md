# HERMES_QUANT_* flag inventory (GENERATED — do not hand-edit)

> Regenerate with `python ops/scripts/quant-flag-inventory.py --write`. This is the
> authoritative list of every flag READ in `hermes_quant/` with its CODE default. The
> enablement runbooks (FEATURE-ENABLEMENT.md, SELFEVOLVE-ENABLEMENT.md) explain the
> eval-gate-to-flip for the capability flags; this table is just the source-of-truth
> defaults so the docs can't silently drift. Empty default = required/path-style (not a
> boolean capability toggle). Every capability flag defaults `'0'` (default-OFF rail).

**41 flags** (resolvable default).

| Flag | Code default | Source |
|---|---|---|
| `HERMES_QUANT_ADMISSIBILITY` | `0` | `hermes_quant/admissibility/oracle.py:414` |
| `HERMES_QUANT_ANALYSTS_USE_REGIME` | `0` | `hermes_quant/memory/meta_retro.py:70` |
| `HERMES_QUANT_BORROW_COST` | `0` | `hermes_quant/admissibility/borrow_pnl.py:34` |
| `HERMES_QUANT_CALENDAR_ENABLED` | `0` | `hermes_quant/catalyst/wiring.py:108` |
| `HERMES_QUANT_CATALYST_ONBOARDING` | `0` | `hermes_quant/catalyst/onboarding.py:76` |
| `HERMES_QUANT_CONVERGENCE` | `0` | `hermes_quant/catalyst/synthesize.py:124` |
| `HERMES_QUANT_DATA_FALLBACK` | `0` | `hermes_quant/data/chain.py:39` |
| `HERMES_QUANT_EVENT_RISK` | `0` | `hermes_quant/advisor.py:1108` |
| `HERMES_QUANT_FUNDAMENTALS_ENABLED` | `0` | `hermes_quant/advisor.py:362` |
| `HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG` | `0` | `hermes_quant/data/fundamentals_provider.py:117` |
| `HERMES_QUANT_GRAPH_MINING` | `0` | `hermes_quant/catalyst/graph_mining.py:157` |
| `HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD` | `0.85` | `hermes_quant/research/hypothesis_novelty.py:40` |
| `HERMES_QUANT_IC_DEDUP_THRESHOLD` | `0.99` | `hermes_quant/factors/ic_dedup.py:44` |
| `HERMES_QUANT_JOURNAL_PATH` | `` | `hermes_quant/journal/writer.py:32` |
| `HERMES_QUANT_KNOWLEDGE_CUTOFF` | `` | `hermes_quant/eval/stockbench.py:47` |
| `HERMES_QUANT_MCP_READS_ENABLED` | `0` | `hermes_quant/data/mcp_bridge.py:194` |
| `HERMES_QUANT_MEMORY_INJECT` | `0` | `hermes_quant/aggregators/llm_committee.py:310` |
| `HERMES_QUANT_MEMORY_SPLIT` | `0` | `hermes_quant/aggregators/llm_committee.py:331` |
| `HERMES_QUANT_MULTILEG_REACTOR` | `0` | `hermes_quant/react/mleg_fill.py:116` |
| `HERMES_QUANT_OPEN_GUARD` | `1` | `hermes_quant/risk/open_guard.py:304` |
| `HERMES_QUANT_OPTIONS_GATE` | `0` | `hermes_quant/risk/options_gate.py:493` |
| `HERMES_QUANT_OPTIONS_LIVE_CHAIN` | `0` | `hermes_quant/options/data.py:413` |
| `HERMES_QUANT_PAPER_INITIAL_CASH` | `` | `hermes_quant/state/portfolio_state.py:79` |
| `HERMES_QUANT_PAPER_SLIPPAGE_MODEL` | `v0.1` | `hermes_quant/react/multileg.py:403` |
| `HERMES_QUANT_PIT_UNIVERSE` | `0` | `hermes_quant/evidence/adapters/form4.py:82` |
| `HERMES_QUANT_PLAYS_OPEN` | `0` | `hermes_quant/playbook/play_loader.py:206` |
| `HERMES_QUANT_PREWARM_WORKERS` | `` | `hermes_quant/playbook/scorers.py:757` |
| `HERMES_QUANT_REFLECTION` | `0` | `hermes_quant/react/multileg.py:268` |
| `HERMES_QUANT_REFLECTOR_LLM` | `0` | `hermes_quant/memory/reflector.py:554` |
| `HERMES_QUANT_RESEARCH_DEBATE` | `0` | `hermes_quant/aggregators/llm_committee.py:1140` |
| `HERMES_QUANT_RESEARCH_LOOP` | `0` | `hermes_quant/research/research_loop.py:88` |
| `HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK` | `0` | `hermes_quant/research/risk_tier.py:221` |
| `HERMES_QUANT_SATURATION` | `0` | `hermes_quant/analysts/semantic.py:140` |
| `HERMES_QUANT_SEMANTIC_ENABLED` | `0` | `hermes_quant/advisor.py:377` |
| `HERMES_QUANT_SHADOW_RULE_MINING` | `0` | `hermes_quant/shadow/rule_mining.py:162` |
| `HERMES_QUANT_STACKING` | `0` | `hermes_quant/aggregators/bma.py:174` |
| `HERMES_QUANT_STRUCTURE_SELECT` | `0` | `hermes_quant/options/structure_select.py:111` |
| `HERMES_QUANT_TRADER_LLM` | `0` | `hermes_quant/agents/trader.py:478` |
| `HERMES_QUANT_TREND_VELOCITY` | `0` | `hermes_quant/catalyst/synthesize.py:199` |
| `HERMES_QUANT_WATERMARK_ENABLED` | `0` | `hermes_quant/daemon/tick_loop.py:67` |
| `HERMES_QUANT_WEEKLY_RETRO` | `0` | `hermes_quant/aggregators/llm_committee.py:345` |
