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

---

## Addendum 2 — B08 fully unblocked (agent-side), SOCIAL_INGEST FLIPPED, CONVERGENCE held (2026-05-31)

Both social producers were fixed to public no-auth feeds (no operator OAuth needed after all):
- **Trends:** `trends/api/dailytrends` (404) → `trending/rss` RSS-2.0 (commit cac3af0). Live: 6 items.
- **Reddit:** `.json` (403) → public Atom `.rss` `/new.rss` + `/search.rss` (commit 42f6cb4). Live:
  49 items from /r/stocks/new.rss, 50 from search. source_family('reddit/r/stocks (rss)') = 'reddit'.
- **Cron reconciled + deployed** (commit 4a29cc3): the deployed copy had diverged features
  (consumer-trend sweeps + per-item log_propagations) the repo lacked; merged both with the social
  wiring so a redeploy wouldn't regress live behavior, then deployed (DEPLOYED == REPO).

### FLIPPED: `HERMES_QUANT_SOCIAL_INGEST=1` (.env:440)
Verified safe + effective: the deployed cron with the flag ON produced a genuinely MULTI-SOURCE feed
(508 items = 148 reddit + 360 news → 309 packets, 356 propagations). Live store now has TSLA/RIVN/LCID
with packets from BOTH news_rss AND reddit. A live TSLA recommend runs clean (no crash; the
deterministic gate + consumer-trend haircut + BMA require_ensemble still govern). This flag is pure
upside — it only ADDS evidence; it drops nothing. Backup: ~/.hermes/.env.pre-selfevolve-enable.

### HELD OFF: `HERMES_QUANT_CONVERGENCE` (PDR-3) — precondition met, but value-gate not yet
The structural precondition (multi-source feed) is now satisfied, BUT with SEMANTIC_ENABLED=1 the
ingest-time drop is consequential, and a measured impact check shows flipping CONVERGENCE now would:
- **KEEP** 3 symbols (TSLA/RIVN/LCID — mega-cap EV, the only names with Reddit chatter this pull)
- **DROP** 16 single-source symbols — *including CELH and CROX*, the exact Camillo consumer-trend
  names the social-arb thesis is built on. They appear in news + consumer-sweeps but not in the
  current 3-sub Reddit query set, so PDR-3 would silence its own best signals.

That is not PDR-3 being wrong — it is the Reddit coverage being too narrow (3 subs, one thin pull)
for mid-cap consumer brands to reliably show up. **The honest gate for the CONVERGENCE flip:** Reddit
coverage broad enough (more subreddits + accumulated over time) that consumer-brand trends (CELH/CROX/
TPR) reliably appear in ≥2 families — verified by re-running this kept-vs-dropped check and seeing the
consumer names in the KEPT set, not dropped. Until then CONVERGENCE stays OFF (flipping it would
silence the thesis). Next concrete step: widen SOCIAL_REDDIT_QUERIES (add r/CrocsCrocs-style brand
subs, r/StockMarket, r/options) and let the store accumulate, then re-measure.

### Net flag state after this session
- **ON:** REFLECTION, MEMORY_INJECT (W1, pre-existing), SEMANTIC_ENABLED (pre-existing), **SOCIAL_INGEST (NEW)**.
- **OFF (correctly, precondition-gated):** CONVERGENCE (needs broader Reddit coverage), SATURATION
  (needs B09 audit), TREND_VELOCITY (decision-inert + costly), W2-W7 crons (undeployed/RESEARCH_DEBATE off).

---

## Addendum 3 — CONVERGENCE flip blocked by a MEASURED freshness-window mismatch (2026-05-31)

Pushed to actually flip CONVERGENCE. Accumulated the live multi-source store (SOCIAL_INGEST=1,
multiple cron runs → 117 reddit packets present) and MEASURED the kept-vs-dropped set within PDR-3's
24h freshness window. Result: ZERO multi-source symbols — every symbol reads news_rss-only in-window.

Root cause (data, not assumption): packet AGE distribution by family in the live store —
- **news_rss:** n=4238, median age 47h, 1583 within 24h (intraday `when:1d` queries).
- **reddit:** n=117, **median age 1494h (62 days), min 939h (39 days), 0 within 24h.**

The Reddit public `new.rss` feed for these brand/ticker queries returns mostly weeks-to-months-old
posts (low-volume subs + stale search results), so a reddit packet's `published_at` (the honest
fidelity anchor) is always far outside the 24h window the news packets live in. A genuinely
convergent trend (CROX discussed on Reddit + a CROX news story) never has BOTH packets
SIMULTANEOUSLY fresh — so PDR-3, which validates convergence over `load_packets_for`'s uniform 24h
`max_age_minutes`, can't see the overlap. Flipping CONVERGENCE now would drop ~everything.

**This is NOT "needs more accumulation" — it is two real, code-level design gaps:**
1. **Stale Reddit feed:** `new.rss` surfaces old content; needs a recency filter (drop entries older
   than N days at ingest) or a higher-volume/real-time social source so reddit produces RECENT signal.
2. **Uniform convergence window:** PDR-3 validates within a single 24h window, but source families
   have wildly different freshness distributions. Convergence-over-a-rolling-window needs a
   FAMILY-AWARE lookback (a longer window for slow social families than for intraday news), or a
   convergence notion that doesn't require simultaneous freshness (e.g. "both families touched the
   symbol within the trend's lifetime", not "both fresh in the same 24h").

Both are reviewed-wave work (a PDR-3 windowing change is a money-path validation-semantics change),
NOT a flag flip. Tracked in #35. CONVERGENCE stays OFF until one of them lands and the kept-vs-dropped
re-measure shows CELH/CROX/TPR in the KEPT set. This is the precise, measured blocker — found by
digging to the data, not by assuming.

---

## Addendum 4 — freshness FIXED; CONVERGENCE now coverage-bound, not freshness-bound (2026-05-31)

Shipped the recency fix (13ca85a): cron now pulls new.rss (live: 244 social items, all ≤7d,
vs the old search.rss median 62d) + a max_age_days=7 backstop. Re-measured CONVERGENCE
flippability on the fresh store. Still zero in-window multi-source overlap — but the binding
constraint has MOVED, and measuring showed exactly where:

Of 244 FRESH social items: 26 classify as a catalyst, 20 hit a graph entity, but only **2 do
BOTH** → 8 reddit packets synthesized (all space names RKLB/ASTS/LUNR/RDW). The convergence
funnel is three sequential filters, each multiplying down:
  1. endpoint freshness — FIXED (new.rss + 7d gate).
  2. catalyst-classify + graph-entity — THE NEW BOTTLENECK: the catalyst lexicon is tuned for
     NEWS headlines ("beats earnings", "recall", "bankruptcy"), not retail-chat phrasing
     ("YOLO", "to the moon", "$CROX upside", "bag holder"), so fresh on-topic Reddit chatter
     mostly produces no packet (2/244 yield).
  3. same-symbol news∩reddit overlap in-window — needs (2) to yield enough reddit packets that
     some land on a symbol news also covers.

So CONVERGENCE is now FRESHNESS-unblocked but COVERAGE-bound. Flipping it today would still
over-drop (news-heavy symbols have no same-symbol reddit packet). The next real lever is lifting
the social→packet yield — a catalyst lexicon (classify.py) tuned for social phrasing + possibly a
social-specific entity/cashtag extractor ($TICKER). That is a substantive classifier wave (its own
eval: does social-tuned classification keep precision while lifting recall?), NOT a flag flip.

Tracked in #35. Every freshness/plumbing layer toward CONVERGENCE is now done and committed
(producers live, fresh, source-tagged, recency-gated); the remaining work is classifier coverage,
which is honestly its own reviewed wave. CONVERGENCE stays OFF until social→packet yield is high
enough that the kept-vs-dropped re-measure shows real same-symbol cross-source overlap.

---

## Addendum 5 — CONVERGENCE: the honest terminal call (do NOT force the flip) (2026-05-31)

Inspected WHY only 2/244 fresh social items become packets. The answer kills the "loosen the
lexicon" reflex: most non-classifications are CORRECT. The live fresh Reddit chatter is dominated
by "SpaceX IPO" / "SpaceX share unlock" / "merger chatter" / "bag holder" / "$SPCE pump" — and
SpaceX is PRIVATE (not a tradeable ticker; the graph maps it to suppliers like RKLB, for which an
IPO rumor is NOT a clean directional catalyst). The classifier returning is_catalyst=False on these
is right, not a miss. Loosening the lexicon to fire on "IPO"/"pump"/"merger" would MANUFACTURE
false-positive packets — the exact cry-wolf failure the catalyst eval's negative-control guards
against — degrading precision to chase a convergence count.

**Terminal determination:** CONVERGENCE stays OFF, and that is the CORRECT state right now — not a
dodged code gap. Every plumbing layer is done and committed: producers live (Reddit Atom + Trends
RSS, no OAuth), fresh (new.rss, ≤7d recency-gated), correctly source-tagged (PDR-3 taxonomy intact),
flowing through the same classify→propagate→synthesize pipeline. The remaining gap is not code —
it is that the social-arb edge requires an ORGANIC same-symbol social+news convergence on a
tradeable name, which the current watchlist (space/EV/semis — institutional, not retail-meme names)
+ this week's flow simply isn't producing. The Camillo edge is in CONSUMER names (CELH/CROX) during
ACTUAL viral moments; you cannot fabricate a trend that isn't there.

The flip is therefore correctly DATA-gated (on a real convergent event occurring), not engineering-
gated. When a genuine consumer-trend moment hits (CELH/CROX trending on Reddit AND in the news on the
same day), the kept-vs-dropped re-measure will show it, and CONVERGENCE becomes a safe, value-adding
flip. Until then, flipping it would silence real single-source news signal to chase a convergence the
market isn't offering. Forcing it would be the un-rigorous move. #35 mechanism = DONE; #35 live-flip
= correctly awaiting an organic convergent event (operator/market-gated, not agent-doable by fiat).
