# MCP-INTEGRATION — optional, DISABLED-BY-DEFAULT MCP servers for hermes-quant

**Audience:** Operator (Codeseys) — single-human paper-trading host.
**Status:** Authoritative policy + enable runbook for the optional MCP registry the
plugin ships at [`mcp/optional-mcps/`](../../mcp/optional-mcps/).
**Cross-refs:**
- [`HERMES-INTEGRATION.md`](HERMES-INTEGRATION.md) — how the plugin loads;
  §2 `register(ctx)` contract, §3 money-via-CLI-only enforcement.
- [`FEATURE-ENABLEMENT.md`](FEATURE-ENABLEMENT.md) §0 — the `.env` tool-guard
  (the agent cannot flip flags or write creds; the operator runs every enable).
- `AGENTS.md` "Plugin authoring constraints" / "Money never goes through tools".
- [ADR-0004](../adr/ADR-0004-risk-gate.md) deterministic risk gate ·
  [ADR-0015](../adr/ADR-0015-hitl-propose-decide-react.md) HITL propose-decide-react ·
  [ADR-0007](../adr/ADR-0007-plugin-shape.md) plugin shape ·
  [ADR-0039](../adr/ADR-0039-robinhood-mcp-reactor.md) +
  [ADR-0067](../adr/ADR-0067-robinhood-mcp-usage-research-amendment.md) the
  Robinhood Agentic money-write reactor (a separate rail — see §6).

---

## TL;DR

The plugin ships a **declared-but-disabled** MCP catalog at
`mcp/optional-mcps/<name>/manifest.yaml`. None of it is wired into the running
agent — `register(ctx)` has no `register_mcp` path (HERMES-INTEGRATION.md §2).
A manifest here is a *vetted, version-pinned, creds-as-placeholders recipe*.

An MCP server only becomes live when **the operator** edits the host
`~/.hermes/config.yaml` `mcp_servers:` block (creds via the tool-guarded
`~/.hermes/.env` / `~/.hermes/secrets/`) and confirms the reload
(`approvals.mcp_reload_confirm: true`). The agent produces the commands; the
operator runs them.

**Money-write MCP tools are NEVER auto-enabled and NEVER wired to fire. The
deterministic risk gate (ADR-0004) + HITL propose-decide-react (ADR-0015) remain
the final authority — an MCP order tool does not bypass the gate.** See §3.

---

## 1. How the host MCP mechanism works (verified)

The host `~/.hermes/config.yaml` has an `mcp_servers:` block (line 505) that
consumes the standard MCP shape in **two** styles:

- **url-style** (remote HTTP/SSE) — `tavily`, `exa`, `deepwiki`, `aws-knowledge`:
  ```yaml
  <name>:
    url: https://...
    timeout: 120
  ```
- **stdio command-style** — `context7` (`bunx @upstash/context7-mcp`), `strands`
  / `time` (`uvx ...`), `playwright` (`npx ...`):
  ```yaml
  <name>:
    command: uvx            # uvx + bunx + npx are all on PATH
    args: ["package"]
    env: {KEY: value}
  ```

Guardrails the host already enforces:
- `approvals.mcp_reload_confirm: true` — enabling/reloading an MCP **prompts**.
- `command_allowlist` — gates dangerous shell shapes.
- `~/.hermes/hermes-agent/optional-mcps/<name>/manifest.yaml` — the host's
  **declared-but-optional** registry (existing: `linear/`, `n8n/`). Presence =
  vetted candidate, not activation; `hermes mcp install <ns>/<name>` materializes
  it into `config.yaml mcp_servers` after the operator confirms.

The plugin's `mcp/optional-mcps/` mirrors that exact schema so the same mental
model and (where supported) the same `hermes mcp install` tooling apply.

---

## 2. The verified server list

Legend — **R/W**: `read-only` = no order/money surface; `money-write*` =
underlying server can place orders but the shipped recipe pins a hard read-only
switch; `MONEY-WRITE` = excluded.

| name          | transport | install (pinned)                                    | creds                                  | R/W           | provenance |
|---------------|-----------|-----------------------------------------------------|----------------------------------------|---------------|------------|
| alpaca        | stdio     | `uvx alpaca-mcp-server` (2.0.2)                     | ALPACA_API_KEY/_SECRET (+ pins)        | money-write\* → **read-only as shipped** | official (alpacahq) |
| tradingview   | stdio     | `bunx -y tradingview-mcp-server@0.6.1`             | none                                   | read-only     | unofficial (fiale) |
| robinhood     | stdio     | `uvx robinhood-mcp` (0.1.2)                         | RH user/pass/TOTP (unofficial login)   | read-only     | community (only read-only RH MCP) |
| longbridge    | stdio     | official `longbridge-mcp --readonly`               | LONGBRIDGE_APP_KEY/SECRET/TOKEN        | money-write\* → **read-only as shipped** | official |
| sec-edgar     | stdio     | `uvx --from sec-edgar-mcp==<pin> sec-edgar-mcp`    | SEC_EDGAR_USER_AGENT (non-secret)      | read-only     | community |
| polygon       | stdio     | `uvx --from git+...mcp_polygon@v0.1.0 mcp_polygon` | POLYGON_API_KEY                        | read-only     | official (pin pre-rebrand) |
| fred          | stdio     | `bunx @cyanheads/federal-reserve-mcp-server`       | FRED_API_KEY (free)                    | read-only     | community |
| coingecko     | http      | url `https://mcp.api.coingecko.com/mcp`            | none (keyless public)                  | read-only     | official |
| yahoo-finance | stdio     | `uvx mcp-yahoo-finance`                            | none                                   | read-only     | community |

Full per-server transport / install / creds / capabilities / read-vs-write
detail lives in each [`mcp/optional-mcps/<name>/manifest.yaml`](../../mcp/optional-mcps/).
Capability highlights:

- **alpaca** — ~60 tools / 9 toolsets. READ: account, assets/calendar/clock,
  stock/crypto/options data, corporate-actions, news. WRITE (the `trading`
  toolset, **excluded by the shipped `ALPACA_TOOLSETS` allowlist**):
  place_stock/crypto/option_order, replace/cancel order, close_position,
  close_all_positions, exercise_options_position, update_account_config.
  The repo states verbatim: "This server can place real trades and access your
  portfolio." The shipped recipe pins `ALPACA_PAPER_TRADE=true` **and**
  `ALPACA_TOOLSETS="account,stock-data,crypto-data,options-data,assets,corporate-actions,news"`
  — there is **no internal HITL hook**; excluding the `trading` toolset at launch
  is the only safe boundary. Same Alpaca account/keys the read-only shortability
  oracle uses — enabling the full toolset would grant order authority over the
  account Hermes trades through.
- **tradingview** — 12 read-only screener/TA tools (screen_*, get_ta_summary,
  rank_by_ta, presets); anonymous public scanner. No order/alert/account write.
- **robinhood** — 15 get_*/search tools; "read-only by design, cannot execute
  trades." Full unofficial login (user/pass/TOTP) → high blast radius.
- **longbridge** — ~107 read tools (US+HK quotes/fundamentals/calendar/account
  reads). `--readonly` de-registers trade-submit/cancel/replace-order.
- **sec-edgar** — filings/XBRL/insider Form 3/4/5; complements the catalyst
  classifier (8-K) + insider signal.
- **polygon / fred / coingecko / yahoo-finance** — read-only market-data, macro,
  crypto, and (lowest-reliability fallback) Yahoo data.

---

## 3. CRITICAL — money-write policy (the rail that does not bend)

hermes-quant is **money-software (paper posture)**. The following is absolute:

1. **No MCP that can place orders / close positions / withdraw / mutate account
   config may be auto-enabled.** Every manifest ships `enabled: false`.
2. **No money-write MCP tool is ever wired to fire without explicit per-order
   human approval.** There is no agent code path that calls an MCP order tool.
3. **An MCP order tool does NOT bypass the deterministic gate.** ADR-0004's
   risk gate + ADR-0015's propose-decide-react remain the **sole** order
   authority. LLM/committee/semantic/social/MCP outputs are *evidence that can
   only silence, never authorize* (HERMES-INTEGRATION.md §3, point 4). Money
   moves only through the CLI lifecycle (`hermes quant ...`) with confirmation
   (AGENTS.md "Money never goes through tools", ADR-0007), never through a
   chat-reachable tool.
4. **Hard read-only switches are pinned where the server can write.** Alpaca:
   `ALPACA_TOOLSETS` excludes `trading`+`watchlists`, `ALPACA_PAPER_TRADE=true`.
   Longbridge: `--readonly` (official binary) / `LONGBRIDGE_MCP_READ_ONLY=true`.
   If those switches are removed, the server becomes money-write and **must not
   be enabled**.
5. **Creds-gated = OFF until the operator opts in.** `~/.hermes/.env` and
   `~/.hermes/secrets/` are tool-guarded; the agent cannot write them
   (FEATURE-ENABLEMENT.md §0). The agent produces the enable commands; the
   operator runs them by hand.

The `x_hermes_quant.classification.{read_only,money_write,underlying_can_write}`
field in each manifest is the machine-readable record of this. Any future
tooling that auto-materializes manifests MUST refuse to enable a manifest where
`money_write: true` (or where `underlying_can_write: true` and the pinned
read-only switch has been removed) without an explicit, separate operator
confirmation.

---

## 4. Sample `mcpServers` block (copy into config.yaml — creds as placeholders)

This is the standard `{"mcpServers": {...}}` shape the host consumes, rendered as
the `config.yaml mcp_servers:` YAML the operator pastes. **Every entry is
disabled by virtue of NOT being present in the live file** — paste only the one
you are enabling, with real creds substituted from `~/.hermes/.env`. The
`# enabled: false` comments are documentation; presence in the live file = ON.

```yaml
# ~/.hermes/config.yaml  — mcp_servers: (append, then confirm reload)
mcp_servers:
  # ... existing tavily/exa/deepwiki/context7/... entries stay ...

  # --- READ-ONLY, no creds: safe-once-opted-in ---
  # enabled: false  (omit from live file to keep OFF)
  tradingview:
    command: bunx
    args: ["-y", "tradingview-mcp-server@0.6.1"]
    env: {}

  coingecko:                              # url-style, keyless public endpoint
    url: https://mcp.api.coingecko.com/mcp
    timeout: 120

  sec-edgar:
    command: uvx
    args: ["--from", "sec-edgar-mcp", "sec-edgar-mcp"]
    env:
      SEC_EDGAR_USER_AGENT: "Your Name (you@example.com)"   # PLACEHOLDER (non-secret)

  # --- READ-ONLY, creds required: OFF until creds loaded ---
  # enabled: false
  polygon:
    command: uvx
    args: ["--from", "git+https://github.com/polygon-io/mcp_polygon@v0.1.0", "mcp_polygon"]
    env:
      POLYGON_API_KEY: "${POLYGON_API_KEY}"                 # PLACEHOLDER

  fred:
    command: bunx
    args: ["@cyanheads/federal-reserve-mcp-server@latest"]
    env:
      FRED_API_KEY: "${FRED_API_KEY}"                       # PLACEHOLDER

  # --- BROKERAGE, underlying-can-write: read-only ONLY via pinned switches ---
  # enabled: false  — DO NOT remove the toolset/readonly pins below.
  alpaca:
    command: uvx
    args: ["alpaca-mcp-server"]
    env:
      ALPACA_API_KEY: "${ALPACA_API_KEY}"                   # PLACEHOLDER
      ALPACA_SECRET_KEY: "${ALPACA_SECRET_KEY}"             # PLACEHOLDER
      ALPACA_PAPER_TRADE: "true"                            # NEVER "false"
      ALPACA_TOOLSETS: "account,stock-data,crypto-data,options-data,assets,corporate-actions,news"

  longbridge:
    command: longbridge-mcp
    args: ["--readonly"]                                    # NEVER remove
    env:
      LONGBRIDGE_APP_KEY: "${LONGBRIDGE_APP_KEY}"           # PLACEHOLDER
      LONGBRIDGE_APP_SECRET: "${LONGBRIDGE_APP_SECRET}"     # PLACEHOLDER
      LONGBRIDGE_ACCESS_TOKEN: "${LONGBRIDGE_ACCESS_TOKEN}" # PLACEHOLDER

  robinhood:
    command: uvx
    args: ["robinhood-mcp"]
    env:
      ROBINHOOD_USERNAME: "${ROBINHOOD_USERNAME}"           # PLACEHOLDER
      ROBINHOOD_PASSWORD: "${ROBINHOOD_PASSWORD}"           # PLACEHOLDER
      # ROBINHOOD_TOTP_SECRET: "${ROBINHOOD_TOTP_SECRET}"   # optional
```

---

## 5. Exact operator enable runbook

The agent cannot do steps 1–4 (tool-guarded files + reload-confirm). The agent
*produces* these commands; the **operator runs them**.

```bash
# 1. Load creds into the tool-guarded env (operator only). Same shape as every
#    HERMES_QUANT_* flag (FEATURE-ENABLEMENT.md §0). Example for polygon:
echo 'POLYGON_API_KEY=<your-key>' >> ~/.hermes/.env
#    (Alpaca keys may instead live in ~/.hermes/secrets/alpaca.env, already present
#    for the read-only oracle — reused here, see the alpaca manifest warning.)

# 2. Add the server to ~/.hermes/config.yaml under mcp_servers: — copy the
#    relevant block from §4, substituting real creds. For BROKERAGE servers keep
#    the read-only pins (ALPACA_TOOLSETS / ALPACA_PAPER_TRADE=true / --readonly).

# 3. Reload. mcp_reload_confirm: true will PROMPT — confirm intentionally.
#    (Reload via the gateway's reload path / restart; do NOT pkill from inside
#    the gateway — self-kill, see HERMES-INTEGRATION.md §1.3.)

# 4. VERIFY the loaded tool surface is read-only. For alpaca/longbridge confirm
#    NO place_*_order / close_*_position / cancel_* / trade-submit-order /
#    update_account_config tools appear. If any do, the read-only pin did NOT
#    apply — STOP, remove the server, do not use.
```

Rollback is always: delete the `mcp_servers.<name>` block from `config.yaml`,
remove the cred line from `~/.hermes/.env` (e.g.
`sed -i '/POLYGON_API_KEY=/d' ~/.hermes/.env`), reload.

Where `hermes mcp install` supports plugin-shipped catalogs, the equivalent is
`hermes mcp install hermes-quant/<name>` followed by the same verify step — but
the explicit `config.yaml` edit above always works for entry-point plugins.

---

## 6. Excluded / deferred (money-write or untrustworthy provenance)

These were found by research and **deliberately not shipped** as enable-able
manifests:

- **Order-placement Robinhood MCPs** — Open-Agent-Tools/open-stocks-mcp,
  ryanfrigo/robinhood-mcp-server, trayders/trayd-mcp, kevin1chun/robinhood-for-agents,
  markswendsen-code/mcp-robinhood, rwlarow-ui, rohitsingh-iitd. All money-write,
  all unofficial full-login. Excluded.
- **TradingView write surfaces** — PyPI `tradingview-mcp` v0.9.1 (DEBARPAN2000)
  has `execute_order`/`execute_portfolio_trade` (paper today, live = "future");
  the CDP-bridge variants (tradesdontlie/ulianbass) write to your real
  TradingView account. Excluded. Only the read-only fiale-plus screener ships.
- **Interactive Brokers MCPs + all-in-one crypto trading servers** — all
  community, all order-placement (jinyiabc, xiao81, code-rabi, rcontesti,
  ArjunDivecha; aitrados, cryptomcp/allinone). Excluded.
- **Alpaca / Longbridge default configs** that load the `trading` toolset (bare
  `uvx alpaca-mcp-server` with no `ALPACA_TOOLSETS`; the Longbridge hosted HTTP
  endpoint and Rust self-host binary, which have **no** read-only flag). Only the
  toolset-pinned / `--readonly` recipe ships.
- **Finnhub / FMP** — credible read-only community servers exist but lower-value
  than Polygon+FRED+EDGAR here. Deferred until a fundamentals gap appears.

**Robinhood Agentic Trading (the money-write rail)** is tracked separately under
[ADR-0039](../adr/ADR-0039-robinhood-mcp-reactor.md) +
[ADR-0067](../adr/ADR-0067-robinhood-mcp-usage-research-amendment.md) as a
`RobinhoodMCPReactor` behind ADR-0011's broker-adapter seam — a reactor with a
default-off feature flag and a mandatory ≥4-week shadow burn-in, gated by the
deterministic risk gate + HITL. That is **not** an auto-exposed agent MCP tool
and is **not** part of this read-only registry.

## 7. Provenance

Verified server provenance and write-surface flags are recorded per-manifest and
in the research synthesis that seeded this doc. Brokerage servers
(alpaca/longbridge/robinhood) and every excluded order-placement server were
cross-checked against the official org repo / PyPI / npm registry before
classification.

---

## Live state (2026-05-31) — keyless read-only servers ENABLED

Operator opted in to enable all KEYLESS read-only servers. The following 4 are now LIVE in
`~/.hermes/config.yaml` `mcp_servers:` (backup at `~/.hermes/config.yaml.pre-keyless-mcp`):

| Server | Transport | Pin | Verified |
|---|---|---|---|
| **tradingview** | stdio `bunx -y tradingview-mcp-server@0.6.1` | 0.6.1 | npm resolve ✓ |
| **coingecko** | http `https://mcp.api.coingecko.com/mcp` | server v5.1.1 | MCP initialize handshake ✓ |
| **yahoo-finance** | stdio `uvx mcp-yahoo-finance` | latest | uvx resolved 51 pkgs ✓ |
| **sec-edgar** | stdio `uvx --from sec-edgar-mcp sec-edgar-mcp` (UA env set) | latest (the `==1.0.0` pin was wrong — that version does NOT exist on PyPI; corrected to latest) | uvx resolve ✓ (edgartools) |

All 4 are **read-only data** (`money_write: false`, `underlying_can_write: false`) — no order surface,
so safe to enable without the gate/HITL concerns that apply to alpaca/robinhood/longbridge. A gateway
reload is needed for an already-running gateway to pick them up (`mcp_reload_confirm: true`).

**Still DISABLED (cred-gated, until the operator loads creds):** alpaca (creds on disk; pinned
read-only via ALPACA_TOOLSETS when enabled), robinhood, longbridge (all 3 have an underlying
order/money surface — never auto-enabled), polygon, fred (read-only data, need an API key).

Rollback any: remove its block from `~/.hermes/config.yaml` `mcp_servers:` (or `cp` the backup) + reload.

---

## 8. CONSUMPTION — how an analyst / the deliberation actually CALLS an enabled read-only MCP tool

§1–§7 cover the *enable/disable policy* and the operator runbook. This section
answers the PHASE-5 question: once a read-only server is LIVE in `config.yaml`
(the 4 keyless ones are, as of 2026-05-31), **how does an analyst, a recipe, or
the deliberation consume its data?** All claims below are verified by a live
probe on this host (`~/.hermes/hermes-agent/venv/bin/python3`).

### 8.1 The host AUTO-EXPOSES MCP tools to the agent — no plugin wiring needed

This is the headline fact, and it means the bulk of this is **doc-only**:

The host's `tools/mcp_tool.py::discover_mcp_tools()` connects to every
`config.yaml mcp_servers:` entry, calls `list_tools()`, and **registers each one
into the same global `tools.registry` that the plugin's 16 `quant_*` tools live
in** (`_register_server_tools` → `registry.register(...)`). The naming is
deterministic:

```
mcp_<sanitized-server>_<sanitized-tool>     # toolset:  mcp-<server>
# hyphens → underscores; so sec-edgar → mcp_sec_edgar_*, yahoo-finance → mcp_yahoo_finance_*
```

`register(ctx)` plays **no role** here — there is no `register_mcp` path in the
PluginContext (HERMES-INTEGRATION.md §2), and there does not need to be. The
host owns MCP discovery; the plugin just *sees the tools appear* in the registry
exactly like its own.

**Live probe (2026-05-31) — the 4 keyless servers register 47 quant-relevant tools:**
```
$ ~/.hermes/hermes-agent/venv/bin/python3 -c "
from tools.mcp_tool import discover_mcp_tools
names = discover_mcp_tools()
print(len([n for n in names if any(s in n for s in
      ('coingecko','tradingview','yahoo','sec_edgar'))]), 'quant-relevant of', len(names))"
47 quant-relevant of 133
```
Examples that landed: `mcp_sec_edgar_analyze_8k`, `mcp_sec_edgar_get_recent_filings`,
`mcp_sec_edgar_get_form4_details`, `mcp_tradingview_screen_stocks`,
`mcp_tradingview_get_ta_summary`/`rank_by_ta` (via presets), `mcp_coingecko_execute`,
`mcp_yahoo_finance_get_current_stock_price`.

### 8.2 Path A — the chat LLM / advisor briefs see them FOR FREE

Globally-enabled MCP servers are **available on all platforms by default**
(`hermes_cli/tools_config.py`: the server name is treated as a toolset that is
included unless a platform sets an explicit MCP allowlist or the `no_mcp`
sentinel). So:

- In **chat / Discord**, the LLM can already call `mcp_sec_edgar_get_recent_filings`,
  `mcp_tradingview_screen_stocks`, etc. — type a question, the model picks the tool.
- In the **`no_agent=False` advisor-brief crons** (`quant-daily-*-interim`,
  HERMES-INTEGRATION.md §4.1), the prompt runs with `enabled_toolsets`; because
  the default toolset set already includes globally-enabled MCP servers, the
  brief LLM can reach SEC-EDGAR filings as evidence or a TradingView screen
  *without any change to the cron record* — unless a cron pins an explicit
  toolset list that omits them (then add the server name to that list).

**Rail:** an LLM calling a read-only MCP tool produces *evidence only*. It can
silence (veto), never authorize. The deterministic gate (ADR-0004) + HITL
(ADR-0015) remain the sole order authority (§3, HERMES-INTEGRATION.md §3 pt 4).
None of these 47 tools can place an order — they are all data reads.

### 8.3 Path B — programmatic consumption from deterministic package code

For an **analyst**, a **recipe builder**, or a **`no_agent=True` cron script**
(pure Python, no LLM) that wants to fold an MCP read into a computation, the
underlying host seam is:

```python
from tools.mcp_tool import discover_mcp_tools   # host module; only inside the gateway process
from tools.registry import registry
discover_mcp_tools()                              # idempotent; ensures the server is connected
raw = registry.dispatch("mcp_sec_edgar_get_recent_filings", {"ticker": "AAPL"})
# raw is a JSON string (Hermes tool convention): {"result": ...} or {"error": ...}
```

**Live probe — end-to-end dispatch returns real data:**
```
$ ~/.hermes/hermes-agent/venv/bin/python3 -c "
from tools.mcp_tool import discover_mcp_tools; from tools.registry import registry
discover_mcp_tools()
print(registry.dispatch('mcp_tradingview_list_presets', {})[:80])"
{"result": "[\n  {\n    \"key\": \"quality_stocks\", ...
```

Rather than hand-roll those `registry.dispatch` calls (and re-implement the
rails at each call site), the plugin ships **one thin, tested seam**:

#### `hermes_quant/data/mcp_bridge.py` — default-OFF, read-only, fail-closed (PHASE 5, additive)

```python
from hermes_quant.data import mcp_bridge

# Safe wrapper: returns None on ANY failure (flag off / denied / unavailable),
# so an MCP read is OPTIONAL ENRICHMENT that degrades silently to the existing
# behavior. This is the form decision code should use.
filings = mcp_bridge.try_read_tool("mcp_sec_edgar_get_recent_filings", {"ticker": "AAPL"})
if filings is not None:
    ...  # fold into evidence, stamped with the wall-clock fetch time (no back-dating)

# Strict wrapper: raises a typed error (McpReadsDisabledError / McpReadDeniedError /
# McpReadUnavailableError) when you want to handle the specific failure mode.
preset = mcp_bridge.call_read_tool("mcp_tradingview_list_presets")
```

The bridge enforces every money-software rail **in-module**, so it is safe to
call from anywhere:

- **Default OFF.** Both wrappers no-op unless the operator sets
  `HERMES_QUANT_MCP_READS_ENABLED=1` in the tool-guarded `~/.hermes/.env`
  (FEATURE-ENABLEMENT.md §0). With the flag unset, the dispatch backend is
  **never even touched** — the existing path is byte-identical. (Verified by
  `test_strict_raises_when_disabled` / `test_safe_returns_none_when_disabled`:
  `discover_calls == 0` and `dispatch_calls == []` when OFF.)
- **Read-only server allowlist.** Only `{coingecko, tradingview, yahoo_finance,
  sec_edgar}` are reachable. Brokerage / underlying-can-write servers (alpaca,
  robinhood, longbridge) and even read-only-but-cred-gated polygon are
  **absent** and cannot be added without a code change + review.
- **Money-write denylist (defense-in-depth).** Even on an allowlisted server, a
  tool whose name contains a write verb (`place_order`, `execute_order`,
  `cancel_order`, `close_position`, `trade`, `buy`, `sell`, `withdraw`, …) is
  refused *before* dispatch. The 4 shipped servers have no such tools; this
  guards against a future server version silently adding a write surface.
- **Fail-closed + asof-honest.** Any error returns `None` (safe) / raises
  (strict); an `{"error": ...}` envelope is treated as a failure. The module
  does not touch time — callers MUST stamp an MCP read with the wall-clock
  fetch time and honor `evidence/lookahead_gate.py`; never back-date it.

The bridge is **purely additive and imports cleanly even outside the host**
(the `tools.registry` import is deferred to call time), so the plugin's offline
test runner exercises it against an injected fake backend
(`tests/unit/test_data_mcp_bridge.py`, 31 tests, no live network). It is wired
into **no** existing path — an analyst/recipe opts in by importing it, and only
when the operator has flipped the flag.

### 8.4 Worked examples (the brief's two scenarios)

- **CoinGecko crypto data into a crypto recipe.** The coingecko MCP exposes a
  generic `mcp_coingecko_execute` (+ `search_docs`) gateway to the CoinGecko
  API. A crypto recipe's perception step can call
  `mcp_bridge.try_read_tool("mcp_coingecko_execute", {...})` for a spot price /
  market snapshot as an *additional evidence input* alongside the existing
  `CcxtProvider` OHLCV chain — it does not replace the deterministic
  bars-based features, it enriches them, and silently degrades to None if the
  flag is off or the server is down.
- **SEC-EDGAR filings as evidence.** `mcp_sec_edgar_get_recent_filings` /
  `analyze_8k` / `get_form4_details` complement the catalyst classifier (8-K)
  and the insider signal. A `no_agent` catalyst/coverage cron, or the catalyst
  ingest step, can pull a filing as a typed evidence packet (stamped at fetch
  time, run through `evidence/lookahead_gate.py`) that feeds the semantic /
  catalyst path — again as *evidence that can only silence, never authorize*.

In both cases the read is OPTIONAL and OFF by default; nothing about the
deterministic gate / sizing ladder / kill-switch is touched.

---
