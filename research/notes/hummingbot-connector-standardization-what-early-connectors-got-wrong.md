---
title: hummingbot connector standardization what early connectors got wrong
id: hummingbot-connector-standardization-what-early-connectors-got-wrong
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:07:00.312392Z'
updated: '2026-06-17T20:28:23.178932Z'
source: https://deepwiki.com/hummingbot/hummingbot
status: evergreen
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'hummingbot: early per-connector duplicated order-tracking/throttling ->
  divergence; fix=pull state machinery into shared base (mirrors hermes dual-ledger
  fix)'
---

# hummingbot — connector standardization, what early connectors got wrong (deepwiki, 2026-06)

**Source:** DeepWiki Q&A over hummingbot/hummingbot, https://deepwiki.com/hummingbot/hummingbot

## Core/adapter factoring
- Hierarchy: ConnectorBase (event reporting, logging, balances, buy/sell/cancel) -> ExchangeBase (order-book trackers, budget checking, trading-pair format conversion) -> ExchangePyBase (modern connectors; lifecycle of trackers + polling for trading rules/fees; TimeSynchronizer, AsyncThrottler).
- InFlightOrder tracks mutable live-order state through OrderState: PENDING_CREATE/OPEN/PARTIALLY_FILLED/FILLED/PENDING_CANCEL/CANCELED/FAILED. ClientOrderTracker manages the collection + handles "lost orders" (orders that vanish from venue API).

## WHAT EARLY CONNECTORS GOT WRONG (direct answer to "what did they get wrong first")
- **Duplicated logic**: each connector re-implemented its own order tracking, throttling, time-sync -> redundant, divergent.
- **Inconsistent error handling** across connectors -> strategies less robust.
- **"Lost orders" hard to manage** without a dedicated ClientOrderTracker (re-query + mark-failed).
- **Ad-hoc per-connector rate limiting** -> API bans / inefficiency.
- The fix = STANDARDIZED framework: ClientOrderTracker (decoupled order tracking), WebAssistantsFactory (centralized REST/WS auth+rate-limit), AsyncThrottler (uniform rate limits), TimeSynchronizer. i.e. they pulled per-adapter duplicated state machinery UP into the shared base.

## Relevance to ADR-0092
- THE cautionary tale that maps onto hermes-quant's confirmed defects: when adapters each re-implement order/position tracking, you get divergence (hermes' dual-ledger), inconsistent rails (hermes' 2-of-4 cap-bypass), and lost-order/state bugs. Hummingbot's remedy — pull the order-state machine + tracking + rate-limit + time-sync OUT of per-venue adapters INTO a shared base — is precisely ADR-0092's "core owns all money-state machinery; adapters only translate." Strong CONFIRMATION that the failure mode ADR-0092 targets is real and recurrent, and that the fix direction (centralize state, thin adapters) is the proven remedy.
