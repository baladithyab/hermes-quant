---
title: Hexagonal driven ports leaking infrastructure
id: hexagonal-driven-ports-leaking-infrastructure
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:13:52.525352Z'
updated: '2026-06-17T15:42:46.615235Z'
source: https://journal.optivem.com/p/hexagonal-architecture-your-driven-ports-are-leaking-infrastructure
status: review
type: note
tier: practitioner
content_type: blog
deprecated: false
summary: Leaky port exposes HOW (SQL/HTTP/host types) not WHAT domain needs -> domain
  coupled to infra; the AnalystView leak test
---

# Hexagonal: driven ports leaking infrastructure (Optivem, 2026)

**Source:** Valentina Jemuovic, Optivem Journal, "Hexagonal Architecture: Your Driven Ports Are Leaking Infrastructure", 2026-04-23. https://journal.optivem.com/p/hexagonal-architecture-your-driven-ports-are-leaking-infrastructure

## The leak failure mode (sub-Q1, directly answers "what happens when the boundary leaks")
- The mistake is NOT forgetting the interface; it's "adding them at the WRONG LEVEL OF ABSTRACTION."
- A driven port is NOT: a wrapper around a framework class; a thin layer over a DB driver; a generic interface exposing HOW things work underneath.
- A driven port IS: "something the domain needs to get its work done; a boundary expressed in DOMAIN LANGUAGE; a contract that hides how things are implemented."
- THE diagnostic question: "does this interface expose WHAT the domain needs, or HOW the infrastructure works?"
- Concrete leaks: OrderRepository.executeQuery(String sql) leaks SQL (domain now knows it's relational); PaymentGateway.post(url, headers, json) leaks HTTP/REST; NotificationService.sendRawMessage(host, port, payload) leaks transport; ProductCatalog returning AttributeValue leaks DynamoDB. "Your domain is shaped by your infrastructure. Change the database/API/broker and your domain code has to change too. That's the OPPOSITE of decoupling."

## Relevance to ADR-0092 (the AnalystView leak hazard)
- This is the precise risk for ADR-0092's AnalystView seam. AnalystView must express WHAT the decision core needs (a peer view: direction, confidence in [0,1], asof publication time, evidence refs) in DOMAIN language — NOT leak HOW the host produced it (no MCP tool-call shapes, no Claude-subagent message structs, no cron-tick metadata, no Python-class internals). The moment AnalystView carries a host-shaped field, the core is coupled to a host and the "host-blind seam" claim fails. The leak test: could you swap Hermes for Cowork (or add a third host) without touching the core's contract? If a field answers "how did this host produce it," it leaks. ADR-0092 is RIGHT that AnalystView is the seam; it is UNDER-SPECIFIED on the discipline that keeps the port at domain abstraction. Name an explicit "no host/infra types in the contract" invariant + a fitness test.
