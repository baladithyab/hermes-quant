# Spike 002 — free-feed ingest (Google News RSS, stdlib only)

## Question
Can we pull timestamped, deduped, parseable catalyst items for a sector/symbol
query at acceptable latency/coverage with **zero paid API**? Would it have
surfaced the Blue Origin explosion?

## Approach
Python stdlib only (`urllib` + `xml.etree`, no feedparser, no keys). Google News
RSS search endpoint with 4 queries: space-sector, RKLB-symbol, energy-sector,
semis-sector. Jaccard dedup on titles. RFC-822 pubDate parsing.

## Result

```
space-sector:  100 fetched -> 89 deduped in 1.14s   (83 Blue Origin/New Glenn items!)
rklb-symbol:   100 fetched -> 87 deduped in 0.62s
energy-sector: 100 fetched -> 95 deduped in 0.67s
semis-sector:  100 fetched -> 93 deduped in 0.66s

total deduped: 364   latency avg 0.77s   paid APIs: 0
```

Surfaced headlines included Reuters, Barron's, NYT, Time on the Blue Origin
explosion — and a Barron's headline that **pre-correlates the move for us**:
*"Blue Origin Rocket Blows Up. AST SpaceMobile and Rocket Lab Stocks Fall."*

## Verdict: VALIDATED

Google News RSS is a sufficient, free, low-latency catalyst backbone. The no-X-API
bet holds: the explosion was overwhelmingly covered (83 items) without any paid
feed. Query-driven pulls (per-sector + per-symbol) give both broad sweep and
targeted depth.

### What worked
- Stdlib-only parsing — no new dependency for v1 ingest.
- Sub-second latency per query; trivially parallelizable across feeds.
- pubDate is present and RFC-822 parseable → gives us the `asof` the lookahead
  gate needs (spike 003).
- Jaccard dedup collapsed ~12% near-duplicate syndicated copies.

### What didn't / caveats
- **Google News RSS `link` is a redirect/encoded URL**, not the publisher's direct
  link — fine for provenance, but resolving to the real article for full-text needs
  an extra hop (worldmonitor uses a relay for blocked domains).
- **pubDate is when Google indexed it**, which can lag the true publication by
  minutes-to-hours. For the lookahead gate this is the SAFE direction (later asof =
  more conservative), but means our latency-to-signal is GN's indexing latency, not
  the wire's. Fast-breaking social-first stories may lag — the case where direct X
  could help later (deferred).
- `when:Nd` recency operator works but is coarse (day granularity).
- No rate-limit hit in the spike, but production polling of many queries needs
  per-feed timeout + backoff + caching (worldmonitor's pattern).
- Title-only classification loses nuance; full-text fetch is a quality lever for
  the LLM synthesis stage.

### Recommendation for the real build
- v1 ingest: stdlib `urllib` + `xml.etree`, query-driven GN RSS + a curated direct
  RSS set (reuse worldmonitor's feed catalog as the seed list — facts, reimplement
  the fetcher). No feedparser dependency needed.
- Use GN `pubDate` as the packet `asof` (conservative for lookahead).
- Per-query timeout + result cache + concurrent fetch; degrade gracefully per feed.
- Defer direct-X and full-text-resolution to a later phase; GN RSS covers v1.
