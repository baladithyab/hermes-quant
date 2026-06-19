---
title: nautilus_trader ports-and-adapters venue integration
id: nautilus_trader-ports-and-adapters-venue-integration
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:03:09.242247Z'
updated: '2026-06-17T20:28:23.146846Z'
source: https://deepwiki.com/nautechsystems/nautilus_trader
status: evergreen
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'nautilus_trader: ports-and-adapters, normalized domain model, RiskEngine-before-ExecutionEngine
  chokepoint, fixed-point money'
---

# nautilus_trader — ports-and-adapters venue integration (deepwiki, 2026-06)

**Source:** DeepWiki Q&A over nautechsystems/nautilus_trader, https://deepwiki.com/nautechsystems/nautilus_trader

## Core/adapter factoring
- Explicitly a "ports and adapters" architectural style: adapters translate native venue APIs into a UNIFIED internal domain model. Modular components plug into the core.
- Rust core (performance/safety) is the engine; Python is the control plane for strategy logic + orchestration.
- Adapter = Rust core (HTTP client w/ request signing + rate limiting, WebSocket client, parsing venue->Nautilus domain models, PyO3 bindings) + Python layer (InstrumentProvider, DataClient, ExecutionClient, Factories, Configuration).

## Canonical internal model (the seam)
- Adapters translate raw venue APIs into Nautilus's normalized domain model: instrument types (spot/perp/future/option), QuoteTick objects for market data.
- **Fixed-point precision system for prices, quantities, money to avoid floating-point errors** (directly relevant to money-state correctness).
- Orders: Nautilus order commands -> venue-specific API calls; execution reports -> Nautilus events (OrderAccepted, OrderFilled).

## Engine topology (gate placement)
- Execution flow: strategy submits order -> **RiskEngine pre-trade checks/validation** -> ExecutionEngine routes to the venue's ExecutionClient -> adapter places order, receives events -> back to ExecutionEngine -> strategy.
- **The RiskEngine sits BEFORE the ExecutionEngine on every path.** Pre-trade risk is a chokepoint between decision and venue. This is structural gate-as-chokepoint prior art.

## What changed / redesign (v1->v2)
- Migrating from legacy Cython (v1) to pure Rust/PyO3 (v2) for performance/safety. v2 Rust adapters run without a Python runtime; v2 PyO3 lets Python user-components run on the Rust core.
- Adapter-developer-guide additions over time reveal the boundary's real failure surface: WebSocket unit tests, close/stream patterns, split-client architecture, symbol normalization, status diffing, task management, data event emission, AuthTracker, credential zeroization + secret redaction. These are the leak points a venue adapter must handle to keep the core clean.

## Relevance to ADR-0092
- Confirms ports-and-adapters + a normalized canonical domain model is the proven seam for multi-venue. The RiskEngine-before-ExecutionEngine topology is direct prior art for "gate as a single chokepoint before reaction." Fixed-point money type is a concrete money-correctness practice the pdr-core should adopt.
