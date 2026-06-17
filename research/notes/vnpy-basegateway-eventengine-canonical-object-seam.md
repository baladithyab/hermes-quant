---
title: vn.py BaseGateway EventEngine canonical-object seam
id: vnpy-basegateway-eventengine-canonical-object-seam
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:06:59.771608Z'
updated: '2026-06-17T20:28:23.173540Z'
source: https://deepwiki.com/vnpy/vnpy
status: evergreen
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'vn.py: BaseGateway + canonical BaseData + pub/sub; risk is a SUBSCRIBER
  not an inline chokepoint (cautionary contrast)'
---

# vn.py — BaseGateway + EventEngine canonical-object seam (deepwiki, 2026-06)

**Source:** DeepWiki Q&A over vnpy/vnpy, https://deepwiki.com/vnpy/vnpy

## Core/adapter factoring (event-driven)
- `BaseGateway` = abstract base ALL gateways inherit: connect/close/subscribe/send_order/cancel_order/query_account/query_position. Uniform interface MainEngine uses for any venue.
- `EventEngine` = central pub/sub backbone. Gateways publish via on_tick/on_order/on_trade/on_position/on_account/on_contract/on_quote/on_log -> Event objects.
- MainEngine orchestrates gateways + engines (OmsEngine, RiskManager). OmsEngine subscribes to ALL trading events, maintains in-memory caches of orders/trades/positions/accounts. RiskManager subscribes to EVENT_ORDER/EVENT_TRADE to enforce risk.

## Canonical objects (the seam)
- All inherit BaseData: TickData, OrderData, TradeData, PositionData, AccountData, ContractData, QuoteData. Gateways translate venue formats <-> these. OrderRequest -> OrderData on send.

## Known pain points (the boundary's failure surface)
- Thread-safety / non-blocking gateway methods are hard when venue APIs aren't designed for it.
- Robust automatic reconnection across heterogeneous venue APIs.
- **Data immutability: callbacks must pass CONSTANT objects, requiring copy.copy() if internal caches exist** (a real mutation-leak hazard — relevant to ADR-0092's immutable-log discipline).
- The connect() method must query+publish a large initial-state snapshot (contracts/account/positions/orders/trades) — a complexity/failure concentration point.
- Error handling: gateways must swallow+log, never raise into the main engine.

## Relevance to ADR-0092
- vn.py's seam = abstract BaseGateway + canonical BaseData objects + pub/sub EventEngine. Risk is a subscriber, NOT inline on the order path -> WEAKER than nautilus (RiskEngine before ExecutionEngine) and weaker than ADR-0092's "one gate node on every path." This is a cautionary contrast: an event-subscriber risk module can race/miss (no single synchronous chokepoint), exactly the class ADR-0092's single execute() chokepoint avoids.
