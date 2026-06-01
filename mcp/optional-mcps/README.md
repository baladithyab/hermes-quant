# hermes-quant optional-MCP registry (DISABLED-BY-DEFAULT)

> **Update 2026-05-31:** the 4 KEYLESS read-only servers (tradingview, coingecko, yahoo-finance, sec-edgar) are now `enabled: true` (operator opt-in, live in `~/.hermes/config.yaml`). The cred-gated + order-capable servers (alpaca/robinhood/longbridge/polygon/fred) remain `enabled: false`. See docs/operations/MCP-INTEGRATION.md 'Live state'.

This directory is the plugin-shipped **declared-but-disabled** MCP catalog. It
mirrors the Hermes host pattern at
`~/.hermes/hermes-agent/optional-mcps/<name>/manifest.yaml` (see the host's
`linear/` and `n8n/` manifests for the canonical schema this set was modelled
on).

**Presence of a manifest here is NOT activation.** A manifest in this directory
is a *vetted candidate* — a curated, version-pinned, creds-as-placeholders
launch recipe that an operator can copy into the Hermes host config. Nothing in
this directory is wired into the running agent. The plugin's `register(ctx)`
has **no** `register_mcp` path; MCP servers are activated only by the operator
editing the host `~/.hermes/config.yaml` `mcp_servers:` block (or running
`hermes mcp install ...`) and reloading. See
[`../../docs/operations/MCP-INTEGRATION.md`](../../docs/operations/MCP-INTEGRATION.md)
for the authoritative policy + exact enable runbook.

## Why every manifest is `enabled: false`

hermes-quant is **money-software (paper posture)**. The standing rails:

1. **Silence-by-default.** New surfaces ship OFF and prove themselves behind an
   eval gate before any default flip.
2. **The deterministic risk gate (ADR-0004) + HITL propose-decide-react
   (ADR-0015) are the final authority.** An MCP tool — including any broker
   order tool — does **not** bypass the gate. It is never on the action path.
3. **Anything needing credentials is OFF until the operator loads creds and
   explicitly opts in.** `~/.hermes/.env` and `~/.hermes/secrets/` are
   tool-guarded; a coding agent cannot write them (FEATURE-ENABLEMENT.md §0).

## The `read_only` vs `money_write` flag

Every manifest carries an `x_hermes_quant.classification` block with a
`read_only` boolean and a `money_write` boolean. This is the load-bearing field
for this repo's posture:

- `read_only: true, money_write: false` — market-data / account-read only. Safe
  to enable after creds + opt-in. Cannot place orders, cannot move money,
  cannot mutate the brokerage account.
- `money_write: true` — exposes order-placement / position-close / withdrawal /
  account-config-mutation tools. **MUST stay OFF, behind explicit per-MCP
  operator opt-in, NEVER auto-enabled, NEVER wired to auto-fire.** Where the
  server offers a hard server-side read-only switch (Alpaca `ALPACA_TOOLSETS`
  allowlist that excludes `trading`; Longbridge `--readonly` /
  `LONGBRIDGE_MCP_READ_ONLY=true`), the manifest pins that switch so the
  *shipped* recipe is read-only even though the underlying server *can* write.

## What ships here vs what was deliberately excluded

Included (research verdict `INCLUDE_DISABLED_*`):

| name          | provenance            | read-only as shipped | creds needed                       |
|---------------|-----------------------|----------------------|------------------------------------|
| alpaca        | official (alpacahq)   | yes (toolset-pinned) | ALPACA_API_KEY / _SECRET           |
| tradingview   | unofficial (fiale)    | yes (no write API)   | none                               |
| robinhood     | community (read-only) | yes (by design)      | RH user/pass/TOTP (unofficial API) |
| longbridge    | official legacy stdio | yes (`--readonly`)   | LONGBRIDGE_APP_KEY/SECRET/TOKEN     |
| sec-edgar     | community             | yes                  | UA string (non-secret)             |
| polygon       | official (polygon-io) | yes                  | POLYGON_API_KEY                    |
| fred          | community             | yes                  | FRED_API_KEY (free)                |
| coingecko     | official hosted       | yes                  | none (keyless public endpoint)     |
| yahoo-finance | community             | yes                  | none (unofficial scraper)          |

Deliberately **excluded** (money-write or untrustworthy provenance) — see
MCP-INTEGRATION.md §"Excluded / deferred":

- Every order-placement Robinhood MCP (Open-Agent-Tools/open-stocks-mcp,
  ryanfrigo, trayders/trayd-mcp, kevin1chun, markswendsen, rwlarow, rohitsingh)
  — money-write, unofficial full-login.
- PyPI `tradingview-mcp` v0.9.1 (DEBARPAN2000) — has `execute_order` (paper
  today, live = "future"); the CDP-bridge TradingView servers (tradesdontlie /
  ulianbass) — write to your real TradingView account.
- All Interactive Brokers MCPs and the all-in-one crypto trading servers — all
  community, all order-placement.
- The Alpaca/Longbridge default (no-toolset / hosted) configs that load the
  `trading` toolset — only the toolset-pinned read-only recipe ships.

> The **Robinhood Agentic Trading** money-write rail is tracked separately under
> [ADR-0039](../../docs/adr/ADR-0039-robinhood-mcp-reactor.md) +
> [ADR-0067](../../docs/adr/ADR-0067-robinhood-mcp-usage-research-amendment.md)
> as a `RobinhoodMCPReactor` behind ADR-0011's broker-adapter seam — that is a
> reactor design (default-off flag + ≥4-week shadow burn-in), NOT an
> auto-exposed agent MCP tool, and is out of scope for this registry.
