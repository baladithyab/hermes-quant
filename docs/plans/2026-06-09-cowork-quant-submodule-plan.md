# cowork-quant — git-submodule plan to remake hermes-quant as a Claude Cowork/desktop plugin

> Date: 2026-06-09. Author: Claude (Cowork session) with Codeseys.
> Decision inputs: repo name **cowork-quant** (private, `baladithyab/cowork-quant`);
> **lean rebuild** of the deterministic core (port the math, not the daemon);
> scope: **v0.1 advisor vertical slice + daily brief + live dashboard**, with
> full PDR parity as the ongoing development trajectory.

---

## 1. What hermes-quant researched, planned, and implemented (ground truth)

### 1.1 Researched (60+ notes in `docs/research/`, charter in `docs/charter/`)

- **Founding charter (2026-05-13):** PDR (Perception→Decision→Reaction) mapped to
  trading. Multi-analyst mixture-of-experts committee; uniform `AnalystView`
  (direction, magnitude, confidence, horizon); Bayesian aggregation; deterministic
  risk gate; *rewarded for correct inaction* (silence-by-default). Kronos is one
  analyst, never the oracle.
- **RL discipline:** what works (walk-forward + embargo, cost-aware log-return
  reward, PPO on the aggregator only) vs. what doesn't (single-asset RL,
  portfolio-value reward, analyst self-play). RL aggregator deferred and later
  marked DO_NOT_BUILD for post-training.
- **Reference-project autopsies** (TradingAgents, AI-Trader, Vibe-Trading,
  moon-dev, FutureSim): the convergent failure is *the LLM becomes final
  execution authority somewhere*. Nine concrete rejected patterns with
  file:line citations live in AGENTS.md. The inverse — deterministic risk gate
  + HITL — is the project's identity.
- **Domain research:** Alpaca options API, options risk prior art, retrospective
  loop architectures, screener literature, catalyst/event onboarding, order
  lifecycle/fills, admissibility/shortability, macro-event risk (pre-FOMC drift),
  overnight-drift anomaly, LEAPS convex sleeve, self-evolution SOTA, Hermes
  plugin/cron mechanics.

### 1.2 Planned (90 ADRs; roadmap 2026-05-31)

- Immutable rails: deterministic risk gate (ADR-0004), discrete sizing ladder
  {0, ±0.05, ±0.10, ±0.15, ±0.20} of NAV, kill-switch, HITL propose→approve→react
  (ADR-0015), LLMs out of the money path (ADR-0012), asof-honesty / no-lookahead
  CI gates (ADR-0019, 0051, 0068, 0069). Every capability ships default-OFF,
  eval-gated, byte-identical when off.
- Open tracks: strategy-openness (ADR-0082), horizon/settlement instrument
  (ADR-0083), scheduled-event calendar + pre-event guard (ADR-0084), optional-MCP
  enablement, operator-gated activations.

### 1.3 Implemented (v0.6.4 — ~82K LOC src, ~84K LOC tests, 40 modules)

| Layer | Shipped |
|---|---|
| Perception | data providers (yfinance/ccxt/alpaca + AlphaVantage fallback), PerceptionFrame, catalyst ingest + social producers, PIT universe, event-risk extras, regime HMM + heuristic classifier |
| Decision | analysts (classical TA, microstructure-lite, Kronos/Kairos, fundamentals, overnight-drift), BMA + stacking aggregators, deliberative committee turns (LLM-backed, structured output), bull/bear debate, three-way risk committee, deterministic structure-selection table for options |
| Reaction | deterministic risk gate (8 rules), paper reactors (equity, multi-leg options, Alpaca paper, Robinhood-MCP), pre-trade admissibility, order lifecycle/idempotency, portfolio caps at the reactor seam |
| Memory/learning | evidence store, hypothesis registry, persistent memory + reflection, retro loops (daily/weekly/quarterly playbook), belief store, self-evolution W-flags (advisory-plane only) |
| Eval/honesty | walk-forward backtester + cost model, flag-ablation harness, lookahead sentinel, shuffle-timestamp CI gate, shadow account counterfactual, run cards |
| Ops | daemon (systemd), unified status CLI, doctor, daily report/brief, 9-server optional-MCP registry (4 keyless live: yahoo-finance, sec-edgar, coingecko, tradingview), governance plane, Seeds issue tracking |

**Gap vs. plan:** intraday deferred (by design), RL absent (by design), live
trading absent (by design — paper/HITL only), several operator-gated
activations idle (crons, creds).

---

## 2. Why Cowork, and what changes

Hermes-quant assumes a long-running gateway + sidecar daemon + CLI. Cowork has
none of those — but it has direct equivalents that actually *simplify* the
design:

| hermes-quant concept | cowork-quant equivalent |
|---|---|
| Sidecar daemon + Hermes cron | **Cowork scheduled tasks** (daily brief, settlement run, weekly retro) |
| Hermes plugin tools (read-only views) | **Commands** `/brief /scan /propose /settle /retro /doctor /status` |
| LLM committee stages (OpenRouter scatter) | **Claude in-session + subagents** (`agents/` — bull, bear, risk-skeptic) |
| Risk gate, sizing, regime, settlement, ledger | **Deterministic Python in `scripts/`**, executed in the sandbox; Claude proposes, scripts dispose |
| `~/.hermes/quant/` (signals.jsonl, ticks.db, state.json) | `quant-state/` directory in the mounted workspace folder (JSONL + SQLite, same schemas) |
| optional-MCPs registry (plugin.yaml) | **`.mcp.json`** — keyless read-only servers (yahoo-finance, sec-edgar, coingecko) + the user's already-connected Robinhood MCP (read-only: portfolio, chains, quotes) |
| HITL propose→approve CLI | **AskUserQuestion** approval; *execution always stays human* — Cowork policy and ADR-0007/0015 both forbid the agent placing orders |
| `quant_status` chat surface | **Live artifact dashboard** (positions, paper P&L, open hypotheses, regime) |
| FLAGS.md env flags | plugin config file (`quant-state/config.yaml`), same default-OFF discipline |

**Hard constraint to design around:** a Cowork plugin must never place orders or
move money — not via computer use, not via the Robinhood MCP's write tools.
This is *stricter* than hermes-quant (which allows CLI-confirmed paper orders)
and aligns with its strongest rail. cowork-quant is therefore an
**advisor + paper-ledger system**: it proposes, sizes, and tracks; the human
executes in their broker app and confirms fills back (or fills are read back
via the read-only broker MCP).

---

## 3. Repo + submodule mechanics

### 3.1 Create the private repo and wire the submodule

```bash
# 1. Create private repo (needs gh auth as baladithyab)
gh repo create baladithyab/cowork-quant --private \
  --description "Claude Cowork plugin: PDR multi-analyst trading advisor (hermes-quant sibling)"

# 2. From the hermes-quant repo root, add as submodule
cd /mnt/e/CS/github/hermes-quant     # or E:\CS\github\hermes-quant on Windows
git submodule add git@github.com:baladithyab/cowork-quant.git cowork-quant
git config -f .gitmodules submodule.cowork-quant.branch main

# 3. Commit the pointer in the parent
git add .gitmodules cowork-quant
git commit -m "feat(cowork): add cowork-quant submodule (Claude Cowork plugin sibling)"

# 4. Day-to-day: work inside cowork-quant/, push independently
cd cowork-quant
git checkout main && git add -A && git commit -m "..." && git push origin main
# then bump the pointer in the parent when you want hermes-quant to track it:
cd .. && git add cowork-quant && git commit -m "chore(cowork): bump submodule"

# 5. Fresh clones of hermes-quant
git clone --recurse-submodules git@github.com:baladithyab/hermes-quant.git
git submodule update --init --remote cowork-quant   # pull latest main
```

Notes: both repos live under the same account so private-submodule access is a
non-issue. Keep the submodule shallow-coupled — cowork-quant must build and
install *standalone* (it is what gets zipped into the `.plugin` file); it may
*reference* hermes-quant docs/ADRs by URL, never by relative path.

### 3.2 cowork-quant repo layout (Cowork plugin shape)

```
cowork-quant/
├── .claude-plugin/
│   └── plugin.json              # name: cowork-quant, semver, author, license
├── commands/                    # user-initiated slash commands (.md, directives for Claude)
│   ├── brief.md                 # /brief — daily PDR market brief (regime, watchlist, events, open book)
│   ├── scan.md                  # /scan <ticker|watchlist> — run analyst fan-out, show committee view
│   ├── propose.md               # /propose <ticker> — full PDR turn → sized proposal → AskUserQuestion approval → ledger
│   ├── settle.md                # /settle — mark fills, compute horizon returns, update calibration
│   ├── retro.md                 # /retro — weekly retrospective over the ledger + hypothesis registry
│   ├── status.md                # /status — book, P&L, regime, gate state (reads quant-state/)
│   └── doctor.md                # /doctor — env/deps/MCP/data freshness checks
├── skills/
│   ├── quant-core/              # SKILL.md: the PDR methodology, rails, sizing ladder, silence-by-default
│   │   └── references/          #   charter distillation, anti-patterns table, glossary
│   ├── analysts/                # SKILL.md: analyst playbooks (TA, fundamentals, catalyst, overnight-drift)
│   │   └── references/          #   per-analyst rubrics + AnalystView schema
│   ├── risk-gate/               # SKILL.md: how/when to invoke the deterministic gate scripts
│   └── options-playbook/        # SKILL.md: covered-call/CSP/wheel + structure-selection table (v0.2+)
├── agents/
│   ├── bull-analyst.md          # adversarial debate roles (ADR-0065 port)
│   ├── bear-analyst.md
│   └── risk-skeptic.md          # three-way risk committee voice (ADR-0043 port)
├── scripts/                     # THE deterministic core (lean rebuild, ported math + tests)
│   ├── quantcore/               # small pip-installable package, stdlib + pandas/numpy only
│   │   ├── gate.py              # risk gate: ¼-Kelly cap, ladder, cost gate, breakers (port of risk/gate.py)
│   │   ├── sizing.py            # discrete ladder {0,±0.05,±0.10,±0.15,±0.20}
│   │   ├── regime.py            # heuristic regime classifier (port; HMM later)
│   │   ├── ledger.py            # paper ledger: proposals, approvals, fills, positions (JSONL, append-only)
│   │   ├── settle.py            # exit-join + horizon-return math (ADR-0083 Phase 0b port)
│   │   ├── calibration.py       # analyst confidence ECE tracking
│   │   ├── schemas.py           # AnalystView, Proposal, Fill, HypothesisCard (Pydantic)
│   │   └── lookahead.py         # asof-clamp helpers; bar-time vs decision-time honesty (ADR-0068/0069)
│   ├── pyproject.toml
│   └── tests/                   # ported property tests (gate invariants via hypothesis)
├── .mcp.json                    # yahoo-finance, sec-edgar, coingecko (keyless read-only)
├── hooks/                       # (later, if at all) e.g. block any tool call matching order-placement
├── README.md
├── AGENTS.md                    # rails for any agent working in this repo (inherit hermes-quant's tone)
└── CHANGELOG.md
```

State lives in the *user's workspace folder*, not the plugin:
`<workspace>/quant-state/{ledger.jsonl, hypotheses.jsonl, calibration.json, config.yaml, briefs/}`.

---

## 4. Rails carried over verbatim (non-negotiable)

1. **Silence by default.** Committee disagreement → no proposal. Stale data → skip + warn.
2. **Hard rules over LLM judgment.** Claude *drafts* views; `quantcore.gate`
   decides admissibility and size. Gate output is final; no prompt overrides it.
3. **Discrete sizing ladder** {0, ±0.05, ±0.10, ±0.15, ±0.20} of NAV.
4. **No order execution, ever.** Stricter than hermes-quant: not even
   CLI-confirmed paper orders through broker write APIs. Human executes;
   fills are confirmed back manually or read via read-only broker MCP.
5. **Asof-honesty.** Every brief/scan stamps decision-time; settlement joins on
   bar-time; no still-forming bars in analyst inputs.
6. **Structured output only.** AnalystViews and Proposals are Pydantic-validated
   by scripts before entering the ledger; free-text never drives state
   (anti-pattern table #3/#4/#5).
7. **Replayability.** Append-only JSONL ledger; every proposal carries
   evidence_ids (data snapshot hashes + analyst views).
8. **Default-OFF for new capabilities**, flag in `config.yaml`, measured before promoted.

## 5. What does NOT port

- The daemon, systemd units, tick loop, signal bus → replaced by scheduled tasks + on-demand commands.
- freqtrade/Nautilus consumers → no execution engine at all.
- Kronos/Kairos torch stack → out of v0.x (sandbox-unfriendly; revisit as an optional HF-API analyst).
- Hermes gateway integration, Discord slash command, plugin.yaml, entry-points.
- RL training module (DO_NOT_BUILD inherited).
- Microstructure analyst (no L2 data in Cowork context).

---

## 6. Phased roadmap

### v0.1 — Advisor vertical slice + brief + dashboard (the proving slice)

1. **Scaffold**: plugin.json, README, AGENTS.md, quantcore package with gate +
   sizing + ledger + schemas, ported gate property tests green.
2. **/scan**: data via yfinance (sandbox) or yahoo-finance MCP → Claude runs
   TA + fundamentals + catalyst analyst rubrics → AnalystViews validated by
   schemas.py → committee summary (agreement/disagreement, no sizing yet).
3. **/propose**: scan → `quantcore.gate` admissibility + size →
   AskUserQuestion (approve/reject/modify-down) → ledger entry. Reject path
   and silence path tested first (money-software discipline).
4. **/brief** + **scheduled daily brief**: regime call, watchlist scan deltas,
   open-book status, scheduled-event proximity (FOMC/CPI/NFP seed calendar —
   ADR-0084 C2 port).
5. **/settle** + **/status**: manual fill confirmation, horizon returns,
   paper P&L.
6. **Live artifact dashboard**: positions, P&L curve, open hypotheses, regime,
   calibration table; reads quant-state/ + read-only broker MCP.
7. Package as `cowork-quant.plugin`, validate, install, dogfood ≥2 weeks.

### v0.2 — Decision-layer parity

Bull/bear debate agents (ADR-0065), three-way risk committee (ADR-0043),
hypothesis registry + run cards (ADR-0048), weekly /retro with calibration
feedback (ADR-0026), event-risk pre-event guard in the gate (ADR-0084 C4).

### v0.3 — Options + portfolio awareness

Options playbook skill (covered-call/CSP/wheel), deterministic structure-selection
table (ADR-0082 A4), Greeks-aware gate rules (ADR-0027), portfolio-aware dynamic
Kelly (ADR-0071), Robinhood MCP read-only chains as the options data path.

### v0.4 — Self-evolution (advisory plane only)

Weekly meta-retro, factor-weight proposer, belief store distillation
(ADR-0080/0081 ports) — proposals surface to the human; nothing self-applies.

### Parity definition

cowork-quant reaches "full PDR parity" when every *decision-layer* ADR has a
port or an explicit WONT_PORT note in its AGENTS.md — execution-layer ADRs
(reactors, order lifecycle) are permanently out of scope by rail #4.

---

## 7. Open items / risks

- **gh auth**: repo creation + SSH submodule URL need the user's credentials —
  operator step, not agent step.
- **Sandbox network**: yfinance from the Cowork sandbox depends on allowlisted
  egress; the keyless MCPs are the fallback data path. /doctor must test both.
- **State location**: quant-state/ in the workspace folder persists across
  sessions but is user-visible/editable — ledger integrity check (hash chain)
  worth adding early.
- **License**: Apache-2.0 inherited; private repo now, decision later on
  publishing.
