# ADR-0039: Robinhood Agentic Trading MCP Reactor — additive equity execution rail

**Status:** Proposed
**Date:** 2026-05-27
**Wave:** E (post Wave-D pattern backfill)
**Supersedes:** nothing
**Amends:** nothing
**Cites:**
- `hermes_quant/react/base.py` (Reactor Protocol, ADR-0015 §D6)
- ADR-0008 (signal bus), ADR-0015 (HITL propose-decide-react),
  ADR-0016 (autonomous mode), ADR-0029 (multi-leg paper reactor),
  ADR-0027/0028 (options gate + data layer)
- `~/wiki/concepts/robinhood-agentic-trading.md` (vendor surface, probed 2026-05-27)

---

## Context

On 2026-05-27 Robinhood announced **Agentic Trading**, exposing two MCP servers:

- `https://agent.robinhood.com/mcp/trading` — equity brokerage
- `https://agent.robinhood.com/mcp/banking` — virtual Gold Card (out of scope here)

The trading MCP is OAuth 2.0 bearer-protected (RFC 9728 protected-resource
metadata at `/.well-known/oauth-protected-resource/mcp/trading`), uses the
MCP Streamable HTTP transport (spec 2025-03-26), and exposes 10 tools:

- read: `get_accounts`, `get_portfolio`, `get_equity_positions`,
  `get_equity_quotes` (≤20 symbols/call), `get_equity_orders`,
  `get_equity_tradability`, `search`
- write: `review_equity_order` (preview), `place_equity_order`,
  `cancel_equity_order`

The Agentic account is a **separate self-directed individual investing
account** (one of up to 10), funds isolated. Read access spans all
Robinhood accounts; write access is scoped to the Agentic account only.
Onboarding is desktop-only.

**Scope boundaries observed in the vendor surface**:

- ✅ US equities, long-only, single-leg
- ❌ No options (any kind)
- ❌ No crypto
- ❌ No futures, no event contracts
- ❌ No shorts
- ❌ No multi-leg / spreads
- ❌ No native per-trade manual-approval gate at the MCP layer (the
  manual-approval feature applies to the credit card surface, not trading)

Robinhood's announcement says options/crypto/event-contracts/futures are
"coming soon" with no public timeline. We treat them as out-of-scope until
shipped.

### Why this matters for hermes-quant

The reactor seam (`hermes_quant.react.base.Reactor` Protocol) was designed
exactly for this — broker-specific adapters that take a `Proposal +
fill_size_pct` and emit an `ExecutionRecord`. ADR-0029 (Multi-Leg Paper
Reactor) extended the seam for options. Today the only concrete reactor is
`PaperReactor` (paper-only, decision-price fills). This ADR adds a sibling
reactor that targets a **real** broker — Robinhood — but only for the
equity-long subset of our 5-play playbook (see ADR-0035 cadence).

Plays that depend on options (covered_call, cash_secured_put, wheel,
LEAPS-options legs) are **NOT** addressed by this ADR. They remain on
whatever options-capable reactor we land for ADR-0029 (Alpaca options or
IBKR).

### Why not just use the existing equities path

Today we have one reactor: `PaperReactor`. It emits `decision_price` as
`fill_price`, with no broker round-trip. To get **real fill quality, real
slippage, real exchange-route halt behavior**, we need a reactor that
actually places orders against an exchange. Robinhood's MCP makes that
cheap because:

1. We already speak MCP (per `native-mcp` skill); no new transport layer.
2. OAuth 2.0 dynamic client registration is on the protected-resource
   metadata — no email-and-PDF API key process.
3. The Agentic account is funds-isolated by design — blast radius is
   bounded to whatever we deposit.
4. No proprietary SDK risk (cf. `robin_stocks` unofficial-API).

This is the lowest-friction way to get a real-money equity execution rail
for hermes-quant signals while leaving the architecture intact.

---

## Decision

We add **`RobinhoodMCPReactor`** as an additive equity execution rail behind
the existing Reactor Protocol, gated by an explicit feature flag, with a
mandatory shadow-mode burn-in before any execution promotion.

The decision splits into seven sub-decisions, **D1–D7**.

### D1 — New module `hermes_quant/react/robinhood_mcp.py`

Implements the `Reactor` Protocol:

```python
class RobinhoodMCPReactor:
    name = "robinhood_mcp"
    requires_credentials = True

    def __init__(
        self,
        *,
        client: RobinhoodMCPClient,
        agentic_account_number: str,
        time_in_force: str = "day",
        review_before_place: bool = True,
        live: bool = False,
    ) -> None: ...

    def execute(
        self,
        proposal: Proposal,
        *,
        fill_size_pct: float,
        approver_user_id: str | None = None,
    ) -> ExecutionRecord: ...
```

**Hard constraints inside `execute`** (fail-closed):

1. `proposal.asset_class != "equity"` → raise `RobinhoodReactorError`
   immediately. We do NOT attempt to translate options/crypto/event-contract
   proposals into equity orders. Wrong reactor for the proposal class is a
   programming error, not a recoverable runtime condition.
2. `fill_size_pct` produces a target signed dollar amount. If
   `target_dollars < 0` (short), raise `RobinhoodShortNotSupportedError` —
   the agentic surface is long-only.
3. `live=False` (default) means **read-only smoke**: `review_equity_order`
   is called, the response is logged, but `place_equity_order` is **NOT**.
   The returned `ExecutionRecord.fill_price` is the previewed/quoted
   midpoint and `reactor_metadata["mode"] = "shadow_review_only"`. This
   matches the "live reactors MUST raise NotImplementedError or a clear
   error if invoked without `--live` opt-in" guidance in `react/base.py`.
4. `live=True` requires both the reactor flag AND an environment opt-in
   (`HERMES_QUANT_RH_LIVE=1`). Two switches — config + env — to defeat a
   single drift.

### D2 — Symbol mapping and pre-trade discipline

Robinhood agentic uses standard US equity tickers. No OCC, no special
formatting. We add a thin `_resolve_symbol(proposal)` that:

1. Calls `get_equity_tradability(symbol)` exactly once on the first execute
   for that symbol per-process; caches `(tradable, fractional)` in an
   LRU(256) for the process lifetime.
2. Refuses to place if `tradable=False` (raise `SymbolNotTradableError`).
3. Routes fractional via the broker's fractional path if
   `fractional=True` AND our computed `qty` rounds to a non-integer count;
   otherwise rounds to the nearest whole share with explicit logging of
   the rounding delta.

We do NOT call `search` from inside `execute` — symbol resolution must be
deterministic by the time a `Proposal` exists. `search` is for human / cron
discovery only.

`review_equity_order` is called before every `place_equity_order` when
`review_before_place=True` (default). The returned warnings are recorded
in `reactor_metadata["review_warnings"]` and surfaced via the daemon's
audit log. They are **additive evidence** — not authoritative override of
ADR-0004's deterministic risk gate.

### D3 — `MCPClient` and OAuth flow

`hermes_quant/react/_mcp_client.py` provides:

```python
class RobinhoodMCPClient:
    def __init__(self, *, base_url: str, token_provider: TokenProvider) -> None: ...
    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...
```

Where `TokenProvider` is a pluggable abstraction with a default
implementation that:

1. On first run with no cached token: dynamic client registration via
   the metadata at `/.well-known/oauth-protected-resource/mcp/trading`,
   then OAuth 2.0 authorization code with PKCE → tokens stored in
   `~/.hermes/quant/credentials/robinhood-mcp.json` (mode 600).
2. On subsequent runs: refreshes via `refresh_token` when expiry is
   <120s away. Refresh failure raises `RobinhoodAuthExpiredError` with a
   clear human action message ("re-run `hermes-quant rh-mcp login`").
3. Every request carries `Authorization: Bearer <access_token>` and
   honors the `Mcp-Session-Id` header round-trip per spec.

The CLI subcommand `hermes-quant rh-mcp login` triggers the OAuth flow
non-interactively (opens the consent URL via `xdg-open`, runs a localhost
callback server). Required because Robinhood's onboarding flow itself is
desktop-browser-bound.

We do NOT bake a vendored OAuth library — we use `authlib` (already in
hermes-agent's dependency closure for other MCP integrations). One new
runtime dep at most. PKCE is mandatory.

### D4 — Feature flag and config posture

The reactor is **off by default**. To activate:

1. `HERMES_QUANT_RH_REACTOR=1` env var (flag visibility).
2. A `[reactors.robinhood_mcp]` block in the daemon config with at least
   `agentic_account_number` populated.
3. Optionally `HERMES_QUANT_RH_LIVE=1` to promote from shadow-review to
   real placement.

`RobinhoodMCPReactor.__init__` raises `RobinhoodReactorDisabledError` if
the env flag isn't set, even if the config block exists. This makes
"someone left the config in place" not a footgun.

### D5 — Shadow-mode burn-in (mandatory before live)

The reactor MUST run in shadow mode (`live=False`) for **at least 4
weeks** before any operator may set `HERMES_QUANT_RH_LIVE=1`. During shadow
mode:

1. Every approved equity proposal that would normally fire `PaperReactor`
   ALSO fires `RobinhoodMCPReactor.execute(live=False)` in parallel.
   Both `ExecutionRecord`s are persisted to `executions.jsonl` with
   distinct `reactor_name` keys.
2. The retro-loop labeler uses Alpaca paper as ground truth (current
   behavior, unchanged).
3. A new diagnostic, `slippage_compare.py`, joins the two streams on
   `proposal_id` and emits per-day rolling stats:
   - mean / p50 / p95 absolute price delta
   - directional consistency (sign match between paper midpoint and RH
     reviewed midpoint)
   - any `review_warnings` clusters (recurring pre-trade flags)
4. The 4-week burn-in is satisfied when:
   - `n_paired_executions >= 200`
   - p95 absolute price delta < 25 bps on a representative universe
   - no `review_warnings` cluster representing >5% of executions
5. If a burn-in window fails, we extend by another 2 weeks; we do NOT
   shorten the burn-in to compensate for low signal volume.

### D6 — Live promotion is **swing-only**, not whole playbook

When the burn-in clears, the first promotion is **swing-only** — the
single play in the 5-play playbook that is already cleanly equity-long
and short-horizon. The other equity-touching plays (LEAPS underlying,
covered_call's underlying check) remain on `PaperReactor` until the swing
play has demonstrated stable real-money operation for an additional 30
days.

This is a posture decision: we expand the live blast radius slowly, one
play at a time. ADR-0023's deliberative committee can silence (×0.0) any
play; that's our backstop if real-money behavior diverges from paper.

### D7 — Live-trading non-goals (explicit)

This ADR explicitly does NOT do the following:

1. **Replace `PaperReactor`.** Paper stays primary for at least 60 days
   post-promotion. Both run in parallel; paper is ground truth for the
   labeler.
2. **Cover options.** Wheel, CC, CSP, LEAPS-as-options stay on whatever
   options reactor lands for ADR-0029.
3. **Cover crypto / freqtrade lane.** Untouched.
4. **Replace HITL.** `quant_approve(proposal_id)` still gates execution.
   The reactor is a leg of the post-approval flow, not a replacement for
   it.
5. **Override the deterministic risk gate (ADR-0004).** RH's
   `review_equity_order` warnings are additive evidence, never
   substitutive.
6. **Wire the banking MCP** (`/mcp/banking`). Hermes-quant is trading,
   not spending. If a future project (e.g. `weather-alpha` style purchase
   agent) wants the banking MCP, it gets its own ADR.
7. **Support multiple Robinhood users.** Single-user per Hermes process.
   Concurrent-session semantics across multiple Hermes processes are
   unproven on the vendor side; we will not multiplex until the vendor
   documents the contract.

---

## Test fence

Before merging the implementation PR (this ADR is the design):

1. **Unit tests** (≥15) covering:
   - `_resolve_symbol` — tradability check, fractional routing, refusal
     on non-tradable.
   - `execute` rejection paths — wrong asset_class, short request, missing
     env flag, missing live flag.
   - `review_before_place` plumbing — warnings flow into
     `reactor_metadata`.
   - OAuth refresh logic with a mock `TokenProvider` — happy path,
     near-expiry refresh, refresh failure surfacing.
2. **Integration tests** behind `pytest -m mcp_live` (skipped by default,
   only runs when `HERMES_QUANT_RH_LIVE_TEST=1` AND a sandbox account is
   wired):
   - `initialize` handshake against the real MCP server.
   - `get_portfolio` round-trip.
   - `review_equity_order` with a known-tradable symbol; assert no
     `place_equity_order` is emitted.
3. **Shadow-mode harness test** — `slippage_compare.py` correctly joins
   two streams and emits stats on a synthetic 50-row dataset.
4. **No regressions** on the existing `PaperReactor` test surface (run
   the full pre-existing suite, expect identical pass/fail counts modulo
   new tests).

---

## Migration / rollout

**Phase 1 (this ADR + PR-1):** ADR + module skeleton + unit tests + the
CLI login command. No shadow execution yet. Ship behind
`HERMES_QUANT_RH_REACTOR=0` (default off).

**Phase 2 (PR-2, ~1 week):** Shadow-mode wire-up in the daily playbook
tick + weekly rebalance + `slippage_compare.py` diagnostic.

**Phase 3 (week 2–5):** Operator runs shadow mode against a real RH
agentic deposit (suggested $100). Slippage / warning stats reviewed
weekly via the diagnostic.

**Phase 4 (week 6+):** If burn-in passes, swing-only live promotion via
`HERMES_QUANT_RH_LIVE=1`. Weekly review continues.

**Phase 5 (week 10+):** If swing-only is stable, expand to LEAPS-underlying
shadow→live. Options-bearing plays remain on options reactor (ADR-0029)
indefinitely until / unless RH adds options support.

---

## Risks

1. **Vendor SLA / rate limits unknown.** RH agentic is brand new beta. We
   add exponential backoff with jitter on transient errors and a circuit
   breaker that disables the reactor for 30 minutes after 5 consecutive
   failures within 5 minutes. Failures are surfaced via the existing
   audit log + halt machine.
2. **OAuth single-binding.** If two Hermes processes use the same RH
   account concurrently, behavior is unproven. Mitigation: a process-local
   advisory lock at `~/.hermes/quant/credentials/robinhood-mcp.lock`. The
   daemon is the authoritative process; ad-hoc `quant_approve` calls
   acquire the lock with timeout 5s and surface a clear error on failure.
3. **Data-egress disclosure**. RH's announcement is explicit: "once your
   data is shared with an AI provider of your choice, it leaves
   Robinhood's security environment". Operators MUST configure a model
   provider with a no-train guarantee (Anthropic / OpenAI enterprise) or
   accept that portfolio + position data will be in training corpora.
   Documented in the README; not enforceable by the reactor.
4. **Read-on-all-accounts blast radius.** RH gives the reactor read across
   all Robinhood accounts even though writes are isolated to the agentic
   account. Prompt injection on a `get_portfolio` response could leak
   primary brokerage state into the model context. Mitigation: the
   reactor's `_mcp_client` strips all account fields from `get_portfolio`
   responses except those matching `agentic_account_number` before they
   surface to the audit log or any caller. Defense in depth: never log
   raw responses; always log the post-strip projection.
5. **Hidden vendor cost / surcharge later.** If RH announces an
   agentic-specific surcharge or fee, the slippage_compare diagnostic
   will surface it; we document the response action as "escalate to
   operator, do not auto-disable" because cost vs. fill quality is a
   decision, not a reflex.

---

## Alternatives considered

### A. Don't integrate; stay Alpaca-paper-only

Rejected. We get no real-fill ground truth and we leave a low-friction,
official-API equity execution rail on the table. Real-money small-stakes
execution is a net positive for the retro-loop labeler.

### B. Replace Alpaca paper with RH

Rejected. Alpaca covers options-paper today (used in ADR-0029); RH does
not. Replacing paper would lose options-side coverage and would jump
real-money exposure from $0 to whatever-we-deposit overnight, with no
shadow burn-in.

### C. Wire RH and `weather-alpha` banking MCP at the same time

Rejected for this ADR. Banking MCP is purchasing, not trading. Different
project, different ADR. Bundling them couples two unrelated risks; we
keep them separate.

### D. Use `robin_stocks` (unofficial API) instead of the MCP

Rejected. `robin_stocks` is unofficial, non-warranted, and historically
prone to ToS-related breakage. The MCP is the official, OAuth-protected
surface; using it removes the ToS risk entirely.

### E. Skip the OAuth flow; require operator to paste a token

Rejected. Robinhood's tokens are short-lived and refresh-rotated. Paste
flow guarantees a token expiry surprise inside a cron tick; OAuth refresh
is the safe path.

---

## Acceptance criteria (this ADR)

This ADR is "accepted" when:

1. The seven sub-decisions D1–D7 are merged to `main` as design.
2. The skeleton module + unit tests + CLI login command land in PR-2.
3. The shadow-mode wire-up + diagnostic land in PR-3.

Live promotion (Phase 4) requires a separate operator decision recorded as
an ADR amendment; it is NOT auto-promoted by satisfying the burn-in
metrics. Burn-in is a necessary, not sufficient, condition for live.

---

## Sources

- Robinhood Newsroom 2026-05-27 — *Robinhood is Now Open to Agents*
- Robinhood Support — *Agentic Trading overview* (covers MCP setup,
  account model, scope)
- Robinhood Support — *Trading with your agent* (10-tool surface)
- Wire-level probe 2026-05-27 18:35 UTC — confirmed Streamable HTTP
  transport, OAuth 2.0 bearer, RFC 9728 protected-resource metadata,
  `Mcp-Session-Id` round-trip
- `~/wiki/concepts/robinhood-agentic-trading.md` — full vendor surface
  notes (private)
