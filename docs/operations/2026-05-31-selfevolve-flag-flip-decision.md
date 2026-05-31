# Self-evolution / PDR flag-flip decision — 2026-05-31

**Author:** agent (autonomous, operator-authorized to flip `.env` flags this session)
**Context:** `/goal` "keep going, flip the env flags yourself." `.env` is writable; W1
(`HERMES_QUANT_REFLECTION` + `HERMES_QUANT_MEMORY_INJECT`) is already live (line 438-439).
**Method:** for each of the 9 self-evolution/PDR flags, verify (a) the eval gate is green NOW
and (b) flipping it CHANGES LIVE BEHAVIOR SAFELY given the *current* runtime state — not just
that the unit gate passes. A flag whose real-world precondition is unmet is inert (theater) or
harmful; flipping it is not "progress." Backup taken: `~/.hermes/.env.pre-selfevolve-enable`.

## Live runtime ground truth (the gating reality, measured this session)
- **Catalyst feed is 100% `news_rss`** (last 200 packets; no Reddit/Trends producers — that is
  **B08, not built**). Packet store = 2499 rows.
- **`RESEARCH_DEBATE` is OFF** and the committee-LLM paths (`RISK_COMMITTEE_LLM`/`TRADER_LLM`) are OFF.
- **W2–W6 cron scripts are NOT deployed** to `~/.hermes/scripts/` (all 5 MISSING).
- **No `beliefs.jsonl`** exists yet (W2 has produced nothing).
- The Hermes `cronjob` registration mechanism (cron.db) is operator-gated; the session `CronCreate`
  tool is a *different*, session-only scheduler — NOT a substitute for production cron registration.

## Per-flag verdict

| Flag | Unit gate | Flip NOW? | Reason (live state) |
|------|-----------|-----------|----------------------|
| `REFLECTION`+`MEMORY_INJECT` (W1) | ✅ 4/4 | **already on** | keystone, live since .env:438 |
| `WEEKLY_RETRO` (W2) | ✅ 6/6 | ❌ inert | cron undeployed+unregistered; no beliefs.jsonl → inline injection no-ops |
| `MONTHLY_META_RETRO` (W3) | n/a | ❌ | depends on a month of W2 output |
| `RESEARCH_LOOP` (W6) | ✅ 7/7 | ❌ | depends on W3 candidate hypotheses |
| `FACTOR_WEIGHT_PROPOSER` (W4) | ✅ 6/6 | ❌ inert | cron undeployed+unregistered |
| `GRAPH_MINING` (W5) | ✅ | ❌ inert | cron undeployed; needs corpus volume |
| `REDTEAM_TURN` (W7) | ✅ 3/3 | ❌ inert | fires only inside research debate; `RESEARCH_DEBATE` OFF |
| `TREND_VELOCITY` (PDR-2) | ✅ | ❌ decision-inert + costly | ingest doesn't pass `velocity_by_symbol` (magnitude swap is eval-only); `frame.trend_velocity` is observability-only (neither semantic nor BMA reads it). Flipping adds a full 2499-row store read per recommend for zero decision effect. |
| `CONVERGENCE` (PDR-3) | ✅ | 🛑 **HARMFUL** | gate fires at INGEST (`quant-catalyst-ingest.py`); with a 100%-single-source feed it would DROP every newly-ingested packet → semantic signal goes dark. The exact "live-influence waits on B08" caveat. |
| `SATURATION` (PDR-4) | ✅ 19/19 | 🛑 **live-influencing, unvetted** | builder computes saturation from loaded packets (packet-age basis) → would decay live semantic confidence on the next recommend, with no B09 side-by-side audit on live data. The plan gates the live flip on B09. |

## Decision

**Flip nothing beyond W1 this session.** Of the 8 not-yet-on flags, every one is blocked by a
missing real-world precondition: 4 are inert (undeployed crons / no debate / observability-only),
2 are harmful-or-unvetted-live (PDR-3 drops live packets; PDR-4 decays live confidence without the
B09 audit), and the inert ones cannot be made live by the agent (cron registration + script deploy
are operator/Hermes-gated). Appending the lines would be theater at best, degradation at worst —
which violates the same money-software discipline the build held to (a flag flip is a change to the
running system and must clear its gate AND not degrade current behavior).

**What WOULD unlock each (the real next actions, mostly operator-side):**
- W2/W4/W5: `cp ops/scripts/quant-{weekly-retro,factor-weight-propose,catalyst-graph-mine}.py ~/.hermes/scripts/`
  → `python ops/deploy/quant-deploy-audit.py` (expect SAME) → register via Hermes `cronjob action='create'`
  (schedules in `SELFEVOLVE-ENABLEMENT.md`) → THEN set the `.env` flag. Observe ≥1 weekly cadence.
- W3/W6: unlock after W2/W4 have produced ≥1 month of advisory-plane output.
- W7: set `HERMES_QUANT_RESEARCH_DEBATE=1` (operator cost decision: it engages committee-LLM calls)
  first; then `REDTEAM_TURN=1`.
- PDR-2/3/4: gated on **B08** (build the real Reddit/Trends producers so the feed is multi-source)
  and **B09** (a larger labeled set + side-by-side audit) before any live flip. PDR-3 in particular
  must NOT be flipped until the feed is multi-source, or it silences everything.

**Net:** the architecture is complete and correct; the blocker to *activation* is data/deploy
plumbing (B08 producers, deployed+registered crons), not code. The honest agent move is to BUILD
the next unblock (B08 real producers) rather than flip inert/harmful flags. See the B08 follow-up.

---

## Addendum — B08 social-ingest seam wired + the REAL blocker found (2026-05-31)

Acting on "advance the goal," I traced the PDR-3 precondition (multi-source feed) to its root:
`hermes_quant/catalyst/social.py` (Reddit + Google-Trends producers) is **built and committed**,
uses **public unauthenticated endpoints**, never raises, and emits `reddit/...` + `google_trends/...`
source tags the PDR-3 taxonomy keys on — but it is **NOT wired into the live ingest cron**
(`quant-catalyst-ingest.py` only called `ingest_queries` = news RSS). That wiring is the B08 gap.

**Done:** wired `ingest_social` into the cron behind a new **`HERMES_QUANT_SOCIAL_INGEST`**
flag (default-OFF → byte-identical news-only path; verified `_social_on()` False unless `=1`).
Social items flow through the SAME classify→propagate→synthesize pipeline as news, so a brand only
emits a packet if it is already a graph entity (evidence-only, no new authority). Query set covers
the graph's consumer brands (Crocs/Tesla/Celsius/Coach-Tapestry/Boeing/TSMC + space).

**The real blocker, found by LIVE-TESTING the producers (not trusting the code):**
- **Reddit `.json` → HTTP 403 Blocked** — Reddit closed unauthenticated JSON access; needs OAuth
  (a registered app + `client_id`/`secret`, operator-provided).
- **Google Trends `dailytrends` → HTTP 404** — the endpoint moved/changed; needs a current API or a
  replacement source.
- `social.py`'s unit tests pass because they inject MOCK fetchers (parser logic is sound); the live
  HTTP layer is what's dead.

**So B08 is NOT unblocked by wiring alone.** The seam is ready; making it *produce* needs working
producers — Reddit OAuth creds (operator) and a live Trends/web-traffic endpoint (small build).
Until then, flipping `HERMES_QUANT_SOCIAL_INGEST=1` is safe but yields 0 social items (silence-by-
default), so the feed stays single-source and PDR-3 must remain OFF. **Net unchanged: PDR-3/PDR-4
live flips remain correctly blocked — now with the precise reason (producer auth), not a vague "B08".**

### Updated next actions (precise)
1. **Operator:** register a Reddit OAuth app → add `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` to `.env`;
   then a small build adds an authed fetcher to `social.py` (the parser is already correct).
2. **Build (agent-doable):** replace the dead Google-Trends `dailytrends` URL with a working endpoint
   (or a web-traffic alternative), keeping the injected-fetcher seam + offline tests.
3. Once producers return items live → flip `HERMES_QUANT_SOCIAL_INGEST=1`, observe the feed go
   multi-source in the packet store → THEN PDR-3 (`CONVERGENCE`) becomes safe to flip.
