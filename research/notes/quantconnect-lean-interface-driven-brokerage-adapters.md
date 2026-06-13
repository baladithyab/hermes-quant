---
title: QuantConnect Lean interface-driven brokerage adapters
id: quantconnect-lean-interface-driven-brokerage-adapters
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:03:40.533232Z'
source: https://deepwiki.com/QuantConnect/Lean
status: draft
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'Lean: IBrokerage contract + canonical Order + IBrokerageModel per-venue
  rules + BrokerageTransactionHandler intermediary'
---

# QuantConnect Lean — interface-driven brokerage/data adapters (deepwiki, 2026-06)

**Source:** DeepWiki Q&A over QuantConnect/Lean, https://deepwiki.com/QuantConnect/Lean

## Core/adapter factoring (interface-driven)
- `IBrokerage` interface = the contract for ALL brokerage implementations: Connect/Disconnect/IsConnected; PlaceOrder/UpdateOrder/CancelOrder; GetOpenOrders/GetAccountHoldings/GetCashBalance; events OrdersStatusChanged/AccountChanged/Message.
- `IDataQueueHandler` = real-time data feed contract; in live trading the brokerage often implements both.
- Abstract `Brokerage` base class provides protected OnOrderEvent/OnAccountChanged for firing events (template-method shared behavior).

## Canonical order model + brokerage models (the seam + the per-venue rules)
- Canonical model = the `Order` class.
- `IBrokerageModel` defines brokerage-SPECIFIC rules WITHOUT polluting the core: supported order types, fees, leverage, and **`CanSubmitOrder()` validation against per-brokerage constraints**. e.g. DefaultBrokerageModel blocks MarketOnOpen for Futures/FutureOptions.
- `IBrokerageFactory` instantiates the right model; BrokerageFactoryAttribute associates class<->factory.
- KEY: per-venue idiosyncrasy is isolated in IBrokerageModel (an adapter concern), so the canonical Order stays clean. This is the "where does venue-specific knowledge live" answer.

## Risk/portfolio vs brokerage separation
- `BrokerageTransactionHandler` is the intermediary: receives order requests from QCAlgorithm, interacts with IBrokerage to place/update/cancel, subscribes to IBrokerage events to update algorithm state. Maintains _completeOrders/_completeOrderTickets.
- This decouples portfolio/risk logic from brokerage implementation details — the algorithm core never touches a venue API.

## Relevance to ADR-0092
- Lean's split — canonical `Order` (core) + `IBrokerageModel.CanSubmitOrder` (adapter-side per-venue validation) + `BrokerageTransactionHandler` (intermediary) — is a mature answer to "what does the adapter own vs the core." The core owns the canonical contract + the transaction handler; adapters own venue rules. Maps to ADR-0092's "shells produce AnalystView[] and feed Fills back; core returns authorized sized Proposal."
