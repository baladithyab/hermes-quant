---
title: TigerBeetle debit-credit immutability in-ledger invariants
id: tigerbeetle-debit-credit-immutability-in-ledger-invariants
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:12:19.615200Z'
updated: '2026-06-17T15:42:46.601495Z'
source: https://docs.tigerbeetle.com/concepts/debit-credit
status: review
type: note
tier: institutional
content_type: docs
deprecated: false
summary: 'TigerBeetle: double-entry, append-only immutable, reversals-as-new-entries,
  invariants enforced IN ledger, don''t roll your own (Uber/Airbnb/Stripe)'
---

# TigerBeetle — debit/credit, immutability, in-ledger invariants, don't roll your own (2024-25)

**Source:** TigerBeetle docs. "Debit/Credit: The Schema for OLTP." https://docs.tigerbeetle.com/concepts/debit-credit

## Money-state model best practice (sub-Q2)
- Double-entry debit/credit is "minimal and complete: two entities (accounts, transfers) and one invariant (every debit has an equal and opposite credit) model any exchange of value, in any domain." Money never appears/disappears => all money accounted for.
- **Immutability is essential**: "once transfers are recorded, they cannot be erased. Reversals are implemented with SEPARATE transfers to provide a full and auditable log." TigerBeetle enforces append-only immutability where SQL allows destructive UPDATE/DELETE. "ensuring effortless reconciliation and audit success."
- **Invariants enforced IN the ledger**: "accounting invariants such as balance limits are enforced WITHIN the database, avoiding round-trips between database and application logic." (=> the gate/limits belong with the ledger, not scattered in callers — maps to ADR-0092 "core owns the gate.")
- Two-phase transfers + linked events (atomic chains) built in.

## "Don't roll your own ledger" (validates extracting a dedicated money-core)
- Uber (2018, 2-year/40-engineer migration), Airbnb (2012-16 rebuilt financial reporting), Stripe — all converged on double-entry + immutable event log after their roll-your-own systems failed to scale/audit. "Many companies start out building their own system... then realize they need a proper ledger and come back to debits and credits."

## Relevance to ADR-0092
- Strong CONFIRMATION of the money-state choices: append-only immutable log, reversals-as-new-entries (never mutate — same as ADR-0091 lesson), invariants/limits enforced AT the ledger (the gate co-located with money state, the ADR-0092 "core owns gate + state"). The "don't roll your own ledger / big companies converge on double-entry" history is the industry argument FOR extracting one audited money-core rather than two plugins each re-deriving it. Consider: should pdr-core's ledger be double-entry (cash account + position accounts) rather than a flat position log? That would make "money never appears from nowhere" a structural invariant — a concrete improvement to name.
