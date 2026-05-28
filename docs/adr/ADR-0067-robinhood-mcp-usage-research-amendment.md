# ADR-0067: Robinhood Agentic Trading MCP — usage-research amendment to ADR-0039

**Status:** Proposed
**Date:** 2026-05-28
**Wave:** E (continuation)
**Supersedes:** nothing
**Amends:** [ADR-0039](ADR-0039-robinhood-mcp-reactor.md) — augments §Context, §Decision, §Risks, §Implementation with usage-pattern research one day post-launch
**Cites:**
- ADR-0039 (Robinhood Agentic Trading MCP Reactor — additive equity execution rail)
- ADR-0011 (broker-adapter seam), ADR-0015 (HITL propose-decide-react), ADR-0029 (multi-leg paper reactor)
- `~/wiki/concepts/robinhood-agentic-trading.md` (re-probed 2026-05-28; usage-research section added)

---

## Why an amendment, not a rewrite

ADR-0039 was authored 2026-05-27, hours after RH's announcement, off the marketing copy + a wire-level OAuth probe. Its core decision (`RobinhoodMCPReactor` as additive equity-only rail behind ADR-0011's seam, default-off feature flag, mandatory ≥4-week shadow burn-in) **stands**. Operator was asked to revisit with deeper usage research. This ADR records what changed in our understanding 24 hours later.

The seven sub-decisions of ADR-0039 (D7.1–D7.7) are unchanged. This ADR adds **D7.8–D7.13** and amends two risks.

---

## Context (additions to ADR-0039 §Context)

### C1. Officially-supported clients are a small enumerated set, not "any MCP client"

The Robinhood support article *Agentic Trading overview* (updated 2026-05-26, expanded post-launch) explicitly lists **seven** clients with copy-paste setup:

1. Claude Code — `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`
2. Claude Desktop — Settings → Connectors → custom URL
3. ChatGPT — Developer Mode → Settings → Apps → Create app → URL
4. Codex (OpenAI / native) — Settings → MCP servers → Streamable HTTP → URL
5. Codex CLI — `codex mcp add robinhood-trading --url https://agent.robinhood.com/mcp/trading`
6. Cursor — Settings → Cursor Settings → Tools & MCPs → Connect → URL
7. (Banking only, also implicitly trading) OpenClaw

For "Other platforms," RH's instruction is literally "use the URL." Hermes Agent's `native-mcp` skill speaks MCP Streamable HTTP + OAuth 2.0 today, so we are in that bucket. **No new auth code is required on our side beyond what `native-mcp` already does** for OAuth-gated MCP servers.

### C2. The OAuth scope is coarse and binary

The protected-resource metadata at `https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading` returns:

```json
{
  "authorization_servers": ["https://agent.robinhood.com/mcp/trading"],
  "bearer_methods_supported": ["header"],
  "resource": "https://agent.robinhood.com/mcp/trading",
  "scopes_supported": ["internal"]
}
```

A single `internal` scope. **No granular split** between read-only and trading capabilities. We cannot ask for "just `get_portfolio`" without also being granted `place_equity_order`. This affects how we structure the reactor — see D7.8 below.

### C3. Robinhood's own General Counsel publicly flagged this as regulatorily incongruous *12 days before launch*

At the FINRA annual conference on 2026-05-15, Dan Gallagher (RH Chief Legal & Compliance Officer, also a FINRA Board of Governors member) said in a panel:

> "I'm not saying FINRA says no or the SEC says no, but if you read the rules, it's mildly incongruous. So we've got to get past that and get past that quickly, because sending American investors off into third-party sources to get investment advice to do the things they want to do in their brokerage app on the website is not good policy."

Gallagher specifically named Reg BI and Reg S-P as rules potentially in tension with third-party agentic trading. Twelve days later RH shipped exactly that flow. Reading: RH is betting first-mover status will shape the rule rather than violate it. That bet may pay off, but **we should not anchor our timeline to it**. See D7.13 below.

### C4. There is a real pre-existing third-party "RH for agents" ecosystem

Independent of the official MCP, multiple production-leaning libraries already wrap RH for agentic use. The four most relevant:

| Library | Approach | Relevance |
|---|---|---|
| `kevin1chun/robinhood-for-agents` (32★) | 18 MCP tools, Playwright-based browser login, OS-keychain token storage. **Includes options + crypto.** | Reference for what an options-capable RH wrapper looks like; potential bridge until RH MCP adds options. |
| `trayders/trayd-mcp` (mcp.trayd.ai) | Hosted MCP server, OAuth 2.1 + PKCE via Clerk, phone 2FA, in-memory tokens. | Reference for hosted-broker MCP patterns; not for our use. |
| `finlayi/rhx` | Go CLI, OS-keyring credentials, **explicit live-mode toggle + short-lived per-order confirmation token** before any order. | Pattern we should adopt at the reactor layer (see D7.10). |
| `jchappo/liljon` | Async-first Python, Pydantic models, Fernet-encrypted tokens. Wraps the unofficial REST API directly. | Reference for type-safe RH client ergonomics. |

That this ecosystem exists and has hundreds of stars across libraries is the demand signal RH responded to. It also tells us we are not alone in wanting agentic RH access — there will be downstream tooling, examples, and bug reports to learn from.

### C5. The official banking (credit-card) MCP is *not* an agentic-checkout protocol

The banking MCP at `https://banking-agent.robinhood.com/mcp/banking` (note: distinct subdomain from trading) issues virtual-card numbers and exposes spending history. It does **not** drive a checkout flow. From the support article:

> "The Robinhood Banking MCP will not browse the internet for you nor find things for you to buy. Instead, it provides the card number for your agent to complete purchases on your behalf."

This is materially less capable than Stripe Shared Payment Tokens, Google Universal Cart, or AWS Bedrock AgentCore Payments + Coinbase x402 for actual agentic commerce. For hermes-quant this is irrelevant (we don't spend, we trade), but it confirms D7.6 of ADR-0039 (skip the banking MCP) was correct and gives a cleaner reason: it doesn't even fit a generic purchase-agent workflow without us also wiring browser-use or similar.

---

## Decision additions

### D7.8. Build the reactor against the full tool surface, not a "read-only" subset

Because the OAuth scope is binary (`internal` or nothing), we cannot architecturally enforce read-only by negotiation. A read-only `RobinhoodMCPReactor.RO` mode is **policy**, not **capability** — implement it as a Python flag inside the reactor that simply refuses to invoke `place_equity_order` / `cancel_equity_order`, and wire that flag through `HERMES_QUANT_RH_REACTOR_MODE={ro|shadow|live}`. The same OAuth token works for all three modes.

### D7.9. Default to `mode=ro` for the first 30 days post-launch, regardless of feature-flag state

Even if `HERMES_QUANT_RH_REACTOR=1`, the reactor's effective mode at startup defaults to `ro` until 2026-06-26 (30 days post-launch). Override via explicit env var. This is a soft guard against an over-eager flip during initial rollout while RH burns down beta bugs.

### D7.10. Adopt the `rhx`-style live-mode pattern: short-lived confirmation token before any order

Layer **on top** of our existing HITL contract (proposal-store + `quant_approve`). Before `RobinhoodMCPReactor` will call `place_equity_order`, the reactor requires:

1. An approved proposal with `proposal_id` (existing HITL — unchanged).
2. **AND** a fresh "live confirm token" obtained via `hermes-quant rh live on --yes`, valid for ≤5 minutes.

The token is a Hermes-internal artifact, completely independent of the OAuth bearer token. It exists to add a deliberate human gesture between "shadow mode burn-in finished" and "real money is moving." Re-issuing it should require typing `--yes` interactively. The token expires automatically. This compensates for the fact that the RH MCP has **no protocol-level pre-trade approval gate** (manual approvals are advertised on the credit card, not on trading).

### D7.11. Keep the `kevin1chun/robinhood-for-agents` repo on the bookmark list as the options bridge

When/if hermes-quant wants RH options (not promised by RH), the bridge is `kevin1chun/robinhood-for-agents` (or `liljon` for direct Pydantic-typed REST). This is an **escape hatch**, not a recommendation — it carries unofficial-API risk and is explicitly out-of-scope for ADR-0039. Documenting it here so future-us doesn't re-discover it.

### D7.12. The agent's read access spans **all** RH accounts is a non-trivial blast-radius concern

Per the support article: "When you connect your AI agent to the Robinhood Trading MCP, it will have read access to **All your Robinhood accounts**, including your Robinhood account numbers, all details about your positions and balances, all details about your transactions, including your order history."

This is **not** scoped to the Agentic account. The agent — and through it, the model provider — sees positions/orders in your *primary* RH brokerage too. For prompt-injection blast radius, this is non-trivial: a malicious news article that leaks portfolio information into an attacker channel could exfiltrate a user's primary holdings, not just the Agentic-account holdings.

**Operational implication for our deployment:** the operator's RH account used for hermes-quant should be a **secondary identity**, not the operator's personal primary RH account. This is a deployment-time policy, not a code change — but it goes into the runbook.

### D7.13. Do not deposit real money for ≥30 days post-launch under any circumstance

The code can land before this. Shadow comparison can run before this (it doesn't require deposit). But the **deposit decision** is gated on:

1. ≥30 days elapsed since 2026-05-27 (i.e. ≥2026-06-26).
2. ≥4 weeks of clean shadow comparison (Decision 7.4 in ADR-0039 — unchanged).
3. **AND** at least one of: an SEC/FINRA no-action letter, formal post-launch FINRA guidance on agentic trading, or absence of enforcement action 30 days after launch.

If ADR-0039 said "swing-only first," ADR-0067 says "swing-only first, **and not before 2026-06-26**, and only after we see how the regulatory wind blows." The amendment is to the timeline, not the architecture.

---

## Risk amendments

### R3 (amended): Coarse OAuth scope means we cannot architecturally enforce read-only

ADR-0039 R3 said: "OAuth flow likely binds to a single Robinhood user." Amend with: "And the only scope is `internal`. There is no read-only token; read-only is implemented as policy in the reactor, not as a capability negotiation. A bug in the reactor's mode-check logic would be a write-capability vulnerability." Mitigation: mode check at the boundary of `RobinhoodMCPReactor._mcp_call`, fuzz-tested in unit tests, with explicit denylist of write tool names checked before tool invocation regardless of mode.

### R6 (new): Regulatory environment is unsettled and may shift mid-shadow-burn-in

If FINRA or the SEC issues guidance during our 4-week shadow period that disfavors agentic retail trading, we may need to abort the integration. Mitigation: shadow mode produces no live-trading footprint, so abort is cheap (delete the feature branch, never merge). The cost of a regulatory-driven abort is bounded to the engineering time spent on PR-2/PR-3.

### R7 (new): Read access spans all RH accounts → prompt-injection blast radius

See D7.12. Mitigation: deploy with a secondary RH account, not the operator's primary identity.

---

## Implementation update (relative to ADR-0039 §Implementation)

**No PR-2 changes.** Module structure, OAuth client choice (authlib), CLI command (`hermes-quant rh-mcp login`), unit tests — all per ADR-0039.

**PR-2 additions:**

- `RobinhoodMCPReactor.__init__` reads `HERMES_QUANT_RH_REACTOR_MODE` (default `ro`, valid: `ro|shadow|live`).
- `RobinhoodMCPReactor._mcp_call(tool, args)` checks tool name against `_WRITE_TOOLS = {"place_equity_order", "cancel_equity_order"}` and refuses unless `mode != "ro"`.
- `mode == "live"` additionally requires a fresh confirm token (D7.10). Implementation: a small `~/.hermes/.rh-live-token` file with `{token, expires_at}`, written by `hermes-quant rh live on --yes`, read by the reactor, refused if missing or expired.
- Until `2026-06-26 00:00 UTC`, the reactor's effective mode is `min(configured_mode, "ro")` — i.e. it will refuse to advance past read-only no matter what config says (D7.9). Implemented as a hard-coded date check in the reactor; remove with an ADR amendment after that date.

**PR-3 additions:**

- The shadow-mode wire-up uses `mode=shadow` (not `live`) — it calls `review_equity_order` to get fill estimates but never `place_equity_order`. We compare the **estimated** fills from RH against the **actual** fills from Alpaca paper. This is technically a weaker comparison (estimate-vs-paper-actual) but avoids any real-money exposure and is sufficient for the "are RH's previewed fills materially different from Alpaca's paper fills" question.

**Runbook additions:**

- Operator should use a **secondary** RH account, not their primary identity (D7.12).
- The 4-week burn-in starts on 2026-06-26 at the earliest, even if PR-3 lands earlier (D7.13).

---

## Alternatives reconsidered

### A1 (reconsidered): "Just use `kevin1chun/robinhood-for-agents` instead of the official MCP"

Tempting because it covers options. Rejected for the same reason ADR-0039 rejected the unofficial REST: it relies on Playwright browser-automation login, not OAuth. That's both fragile (RH front-end changes break it) and TOS-marginal. The official MCP's narrower surface is worth the gap until RH adds options natively.

### A2 (new): "Use the banking MCP as a sandbox to dry-run our auth flow before committing to trading"

Dismissed. The banking MCP is on a different subdomain (`banking-agent.robinhood.com`) and presumably has its own OAuth scope. Auth experience won't transfer cleanly. We'll dry-run trading auth against trading MCP's `initialize` call directly, which works without account creation (just returns 401, which is what we want to verify).

### A3 (new): "Wait for SEC/FINRA guidance before doing any code work at all"

Tempting given Gallagher's FINRA-conf comments. Rejected because the **code work is reversible** (feature flag, mode `ro` by default, deposit-money decision gated separately on D7.13). The cost of writing the reactor while the regulatory wind sorts itself out is low; the cost of being two months behind if/when the wind settles favorably is real.

---

## Open questions (carried forward + new)

Unchanged from ADR-0039:
- Q1. Per-symbol coverage of the `search` tool (international ADRs? OTC?).
- Q2. Behavior of `place_equity_order` during a halted symbol — does it return a structured error or a soft warning?
- Q3. OAuth refresh-token lifetime.

New from ADR-0067:
- Q4. Are there any `internal`-scope subdivisions advertised in the actual authorization-server metadata (one level deeper than the protected-resource metadata)? Worth probing the OAuth metadata endpoint once we run through the dynamic-client-registration flow in PR-2.
- Q5. Will RH publish a rate-limit policy before we hit our first 429? (Not blocking; just want to know.)
- Q6. What's the post-launch enforcement posture from FINRA / SEC? Reassess D7.13 monthly.

---

## Decision (delta from ADR-0039)

Architecture: **unchanged.** Implementation: **add D7.8–D7.13** (mode flag, default-ro through 2026-06-26, live-confirm token, secondary-account guidance, deposit-gate hardened to ≥30 days). PR sequencing: **unchanged** (PR-2 = skeleton + auth + tests; PR-3 = shadow wire-up). Real-money deposit: **gated to ≥2026-06-26 + clean shadow + at least one regulatory-clarity signal** (D7.13).

If the regulatory clarity signal in D7.13 doesn't materialize by 2026-08-01, write a follow-up ADR re-evaluating whether to ship the deposit step at all. Until that ADR, PR-2 + PR-3 ship and shadow mode runs.
