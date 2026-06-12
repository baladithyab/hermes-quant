# algoxaustin "Claude Code scans the whole market" — Methodology Extraction + Feasibility Verdict

> **Class:** influencer options-flow reel → screener-spec extraction + honest backtest of the
> reconstructable kernel. Companion to `socalminh-covered-call-screener.md`.
> **Verdict:** the backtestable kernel is **dead** (loses to random); the only legitimate leg
> (GEX/flow) is **paywalled-token-only** and incompatible with our no-auth feed tier.
> See the paired research verdict: `docs/research/2026-06-11-algoxaustin-call-kernel-verdict.md`.

## Source

| Field | Value |
| --- | --- |
| URL | https://www.instagram.com/reel/DYk6vQFjoBT/ |
| Author | Austin — `@algoxaustin` ("Options Alerts & Setups") |
| Upload date | 2026-05-20 |
| Duration | 40.97 s |
| Likes | 1,228 |
| Caption | "Claude code is ACTUALLY insane. #investing #trading #stocks #makemoney #stockmarket" |
| Modality | Talking-head; methodology entirely in audio (caption is pure CTA) |
| Pipeline | yt-dlp audio → faster-whisper large-v3 (CUDA, RTX 5090) |

## Verbatim transcript (large-v3)

> we just gave cloud code [Claude Code] access to the entire option and stock market. the coolest
> thing i've learned it can do is scan the entire market for the best live contracts. it does this
> all scored on a **point system** based off **unusual flow, volume, put/call ratio, GEX levels,
> momentum, and theta burn**. it then spits back the top setups in seconds. like dude this morning
> it literally flagged SMCI calls — so we alerted this play at open in the discord, **120%** on the
> contracts. small account challenge, **500 to 3,800 in the past week**. this is like the most unfair
> edge retail has ever had. if you want to see exactly how i do this live in the discord click the
> link in my bio.

Community fact-checks in the IG comments (worth recording — they independently flagged the grift):
- `quant.tradez`: "Don't do this, financial data is non-IID and AI models overfit this 🤦‍♂️"
- `erzconnector`: "Api costs 50usd per week (lowest tier...)"
- `salesstacks`: "Why can't you use this to just make the trades programmatically, set it and forget it?"

## The "point system" (6 factors) — decode

The mechanism is **Claude Code writing a script that calls a flow-data API and ranks contracts** —
NOT an LLM "predicting" trades. The "scan the market in seconds" claim is honest (it's a ranking
loop); the "unfair edge / AI predicts" framing is the lie. Almost certainly backed by the
**Unusual Whales API / MCP server** (the 6 factors map 1:1 onto UW endpoints — see auth finding below).

| Factor | Maps to | Reconstructable on our (Alpaca) data? |
| --- | --- | --- |
| Unusual flow | UOA / sweep detection vs OI | ❌ needs historical OI time series (Alpaca = none) |
| Volume | vol / OI ratio | ✅ (volume) / ❌ (OI history) |
| Put/Call ratio | directional sentiment | ❌ historical chain P/C not retrievable |
| **GEX levels** | dealer gamma exposure | ❌ **needs full-chain OI+greeks at each past date** |
| Momentum | price technical | ✅ trivially |
| Theta burn | greeks decay | ⚠️ live only (no historical greeks series) |

Only **momentum** and **(current) volume** are honestly backtestable for free. GEX — the one leg
with a real mechanistic thesis (see `options-microstructure-regimes` skill: dealer hedging is a
mechanical force) — is **not reconstructable on Alpaca** (no historical OI/greeks series).

## Unusual Whales integration — auth finding (tested 2026-06-11)

**Question:** can the UW MCP server be packaged under hermes-quant's no-auth feed tier
(the `quant-catalyst-ingest.py` pattern: Google News RSS / Reddit .rss / Google Trends, all
tokenless, "a dead producer contributes zero items, NEVER raises")?

**Answer: No. UW is token-gated with no free tier and no degraded mode. Fails closed at every layer.**

| Layer | Test | Result |
| --- | --- | --- |
| API | `GET /api/market/market-tide`, `/stock/AAPL/greek-exposure`, `/flow-alerts` — no token | **401 `authentication_required`** on every route |
| API | same with a malformed token | **401** — demands UUID-format token |
| MCP pkg | `npx -y unusualwhales-mcp@0.1.8` with no `UNUSUAL_WHALES_API_KEY` | **throws `UNUSUAL_WHALES_API_KEY is required` → `process.exit(1)`** before registering any tool |

The package (`unusualwhales-mcp@0.1.8`, MIT, npm) reads exactly one secret —
`UNUSUAL_WHALES_API_KEY` — and constructs its API client eagerly at boot. No key ⇒ the server dies
on startup. This is **architecturally incompatible** with the no-auth catalyst tier, whose invariant
is that a keyless/dead producer yields zero items rather than crashing. Dropping a hard-exit(1)
server into that tier would break that guarantee.

UW pricing: paid API token, **$50/wk floor** (confirmed by the IG comment + pricing page).

### Config block (for if/when a token is purchased — NOT applied)

```yaml
mcp_servers:
  unusualwhales:
    command: "npx"
    args: ["-y", "unusualwhales-mcp"]
    env:
      UNUSUAL_WHALES_API_KEY: "<uuid-token>"   # prefer a shell-env ref, not inline (config.yaml is world-readable + hot-reload strips comments)
    timeout: 60
    sampling:
      enabled: false   # untrusted 3rd-party server — no agent-in-loop LLM calls
```

Tools would register as `mcp_unusualwhales_get_stock_greek_exposure`,
`mcp_unusualwhales_get_stock_flow_alerts`, `mcp_unusualwhales_get_market_tide`, etc. (81 endpoints,
12 categories). `sampling.enabled: false` is deliberate for a third-party server.

## Disposition

- **Do NOT** package UW under the no-auth tier — it cannot satisfy the tier's fail-open invariant.
- **Do NOT** build the call-buying bot from the reconstructable kernel — it's a measured loser
  (see paired verdict note).
- The only genuinely interesting thread is a **forward-only paper GEX+flow ranking via the UW MCP
  server**, gated on buying the $50/wk token, treating GEX as a *silence-bias gate dimension* (not a
  fire signal) per the `options-microstructure-regimes` framing. Staged but not activated.
