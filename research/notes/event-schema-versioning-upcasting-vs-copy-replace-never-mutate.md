---
title: Event schema versioning upcasting vs copy-replace never mutate
id: event-schema-versioning-upcasting-vs-copy-replace-never-mutate
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:12:20.436306Z'
updated: '2026-06-17T20:28:23.204361Z'
source: https://event-driven.io/en/simple_events_versioning_patterns/
status: evergreen
type: note
tier: practitioner
content_type: article
deprecated: false
summary: 'ES versioning: never mutate events; upcast at READ time (=version-discriminated
  carry-forward fold = ADR-0091-C); copy-replace only for clean log; keep streams
  short'
---

# Event schema versioning — upcasting vs copy-replace, never mutate (Dudycz/Young)

**Source:** Oskar Dudycz. "Simple patterns for events schema versioning." event-driven.io. https://event-driven.io/en/simple_events_versioning_patterns/ (+ Greg Young, "Versioning in an Event Sourced System")

## The choice (directly answers sub-Q4 + ADR-0091 interpretation-vs-mutation)
- Rule: "typically you should NOT change the past. Having precise information, even including bugs, is a valid scenario." Events are immutable.
- **Simple mapping** (non-breaking): new optional/nullable property; new required property WITH a default value; renamed property via serialization mapping (keep old JSON name, map on (de)serialize) -> backward+forward compatible.
- **Upcasting** = "plug a middleware between deserialisation and application logic": read the raw/old event and transform to the new schema AT READ TIME. Old events stay on disk in old shape; the reader produces the new shape. (This is exactly a version-discriminated carry-forward FOLD.)
- **Downcasting** = the reverse, for old readers/listeners.
- **Stream transformation** (N:M) = collapse/expand multiple old events into new ones at read, keyed by correlation id.
- **Copy-and-replace / migration** = read the old stream, write a new stream (or new DB), switch once caught up — only if you pragmatically want a "clean" log. Heavyweight; avoid unless necessary.
- Keep streams SHORT-LIVED to make versioning trivial (two-phase deploy: support both schemas, then drop old once no old aggregates remain). Best of all: avoid versioning by modelling well upfront ("the best approach is to not need to version at all").

## Relevance to ADR-0092 + ADR-0091
- ADR-0091's option C ("version-discriminated carry-forward fold") = textbook UPCASTING at read time. This is the canonical, low-risk ES practice and it is INTERPRETATION (read-time), not MUTATION (rewriting the log) — confirms ADR-0091-C over option B (producer-emits-delta = write-time coupling, which ES practice warns against: never make the write depend on derived/old state). For two plugins on different cadences: each shell's pdr-core version registers its upcasters; old log records are interpreted forward; no shell ever rewrites the shared log. The shared core ships the upcaster registry as part of its versioned contract.
