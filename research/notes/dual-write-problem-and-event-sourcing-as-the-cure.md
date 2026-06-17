---
title: Dual-write problem and event sourcing as the cure
id: dual-write-problem-and-event-sourcing-as-the-cure
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:12:19.205280Z'
updated: '2026-06-17T20:28:23.188940Z'
source: https://www.confluent.io/blog/dual-write-problem/
status: evergreen
type: note
tier: institutional
content_type: article
deprecated: false
summary: 'Confluent 2024: dual-write = hermes bug class; cure = single append-only
  log + derive consumers; banking is naturally event-sourced'
---

# Dual-write problem + event sourcing as the cure (Confluent, 2024)

**Source:** Wade Waldron (Confluent). "Understanding the Dual-Write Problem and Its Solutions." 2024-05-29. https://www.confluent.io/blog/dual-write-problem/

## The problem (this IS hermes' bug class, named)
- Dual-write = two external systems must update atomically; if one write succeeds and the other fails, inconsistent state. "Occurs anytime you try to write to two separate systems and only one of those writes succeeds." Happens even in a monolith (DB + email = distributed system).
- Anti-patterns that DON'T solve it: reorder operations (just moves the inconsistency); wrap in a DB transaction (transactions don't extend to external systems — failure after emit but before commit still diverges); in-memory retry (lost on crash); durable retry (re-introduces a second write).

## The cure (validates ADR-0092 single-fold + ADR-0091 lesson)
- "The key is to SEPARATE the two writes and introduce a DEPENDENCY between them": one authoritative write, then a separate process derives the rest.
- **Event sourcing**: "Each time the DepositFunds command is issued, a FundsDeposited event is written to a single row in a single table. A transaction is unnecessary because the event is written to a single row in a single table. When the application needs state, it reads the events back and uses them to rebuild it." Projections/consumers derive from the one log.
- **"This model works well for banking because accounts are NATURALLY event-sourced. Banks keep a full history of every deposit and withdrawal."**
- Transactional outbox = alt (one DB txn writes state + outbox row; CDC ships events). Listen-to-yourself = write to log first, derive DB later (eventually consistent).

## Relevance to ADR-0092
- The hermes dual-ledger divergence (two reconstructors reading executions.jsonl with incompatible semantics) is literally a dual-read divergence; the structural cure is exactly ADR-0092's: ONE append-only log (intent), ONE fold (Ledger.portfolio()), all consumers DERIVE from it (single source of truth). The ADR-0091 lesson ("don't make a producer read derived state to write the log") = keep the log a pure record of intent, write a single row, never couple the write to derived state — directly endorsed here. Materialized projections are fine ONLY if rebuildable from the log (CQRS).
