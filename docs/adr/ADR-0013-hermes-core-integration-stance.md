# ADR-0013: Hermes-core integration stance + dual-surface architecture

**Status:** Accepted (2026-05-13), implemented
**Supersedes:** none
**Amends:** none (locks integration boundary for ADR-0001, ADR-0007)
**Cross-cuts:** ADR-0002 (Analyst Protocol), ADR-0003 (calibrators), ADR-0004 (risk gate), ADR-0005 (data layer), ADR-0007 (plugin shape / canonical CLI), ADR-0010 (settlement journal), ADR-0011 (portfolio reconstruction), ADR-0012 (LLMAnalyst deferred)

---

## Context

hermes-quant is a Hermes Agent plugin distributed via `hermes plugins install`. Per the user directive of 2026-05-13, it must be installable by "anyone using Hermes" with minimum friction. That requirement, combined with the existing daemon architecture (ADR-0001), surfaces two distinct operator-facing flows that share core code but differ sharply in setup cost and money-software risk:

1. **Autopilot surface** — the long-running daemon → JSONL signal bus → freqtrade flow. Money-software discipline (ADR-0012 §LLMs out of action path; AGENTS.md §Project posture). Requires `systemd` (or equivalent supervisor), broker API key, profile setup, and explicit operator opt-in. This is the existing v0.1.1 surface.

2. **Advisor surface** — a synchronous, in-process "what does the system say about X" tool callable from the Hermes chat session and `hermes quant <verb>` CLI. No daemon, no broker, no portfolio state, no actuator. Read-only consumer of the analyst pool, the calibrator framework, and (when present) the settlement journal. The bootstrap path uses `yfinance` and requires zero credentials.

The architecture review at [`docs/reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md`](../reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md) walked the v0.1.2 work plan against (a) Hermes-core's documented `PluginContext` API and (b) the existing monkeypatch-plugin prior art (`hermes-mission-control-bootstrap`). It found **zero monkeypatch candidates** and **five LEVERAGE adoptions**. This ADR locks both stances and the dual-surface split before v0.1.2 lands so that subsequent PRs ship against a fixed integration boundary rather than negotiating it case by case.

## Decision

### D1: Dual-surface architecture

hermes-quant ships **two operator surfaces** from v0.1.2 onward:

- **Autopilot** (existing): `hermes quant start` → daemon → JSONL → freqtrade. Per ADR-0007. Unchanged from v0.1.1; remains the canonical money-software path.
- **Advisor** (new in v0.1.2): chat-mode tools `quant_recommend`, `quant_show_signals`, `quant_show_views`, `quant_show_lessons`, `quant_doctor`, all usable **without a running daemon**. The yfinance bootstrap path is the supported zero-setup configuration.

Both surfaces share the same Analyst Protocol implementations (ADR-0002), the same `RiskGate` (ADR-0004), the same calibrator framework (ADR-0003), and the same settlement journal format (ADR-0010). The difference is operational, not algorithmic:

| Concern | Autopilot | Advisor |
|---|---|---|
| Execution model | Async daemon, persistent | Sync, in-process per call |
| State store | `state.db` watermark, `signals.jsonl` bus | None — pure function of the call args |
| Portfolio state | Yes (ADR-0011) | No |
| Actuator | freqtrade strategy via JSONL | None — returns a recommendation dict |
| Settlement journal | Writer + reader | Reader only (`get_recent_lessons`) |
| Setup cost | Broker key + supervisor + profile | `pip install -e '.[yfinance]'` |

Both surfaces are first-class. Neither is a "demo mode" of the other.

### D2: Hermes-core integration via PUBLIC plugin contract only

Hermes-core is consumed exclusively through documented `PluginContext` APIs. The full v0.1.2 surface is:

- `register_tool(name, toolset, schema, handler)` — read-only `quant_*` tools (advisor surface)
- `register_command(name, handler, description)` — `/quant` slash command
- `register_cli_command(name, help, setup_fn, handler_fn)` — `hermes quant <verb>` subcommands (per ADR-0007)
- `register_hook("pre_gateway_dispatch", fn)` — Discord `/quant` slash deferred install
- `register_skill(name, path)` — bundled `hermes-quant` SKILL.md

There are **no private-attribute reads** of `adapter._client`, `adapter.session_store._entries`, `hermes_state.SessionDB()`, or any other underscore-prefixed Hermes-core symbol for v0.1.2. Future advisor enhancements (e.g. surfacing the last `quant_recommend` call into session memory automatically) may need such reads; if so, they will be tracked under a separate ADR that explicitly motivates the boundary breach.

### D3: Zero monkeypatches for v0.1.2

No `apply()` / `revert()` patch modules are introduced in v0.1.2. Per `plugin-as-monkeypatch.md` §When NOT to monkeypatch, none of the v0.1.2 work items meet the bar:

- The advisor surface lives entirely inside `hermes_quant.tools` and registers through public APIs.
- The daemon lives in its own process and is not coupled to Hermes-core's runtime at all (ADR-0001).
- The CLI, slash command, and skill all have first-class `register_*` entry points.

If a defect emerges during dogfooding that genuinely cannot be worked around through the public contract, the prior-art template is `hermes-mission-control-bootstrap/mc_bootstrap/` (two-patch shape). The fix would ship as a **separate** plugin named `hermes-quant-bootstrap`, leaving hermes-quant itself patch-free. This separation matters: a money-software plugin must not also be the thing reaching into Hermes-core's internals.

### D4: LEVERAGE adoptions (5 items)

The architecture review identified five Hermes-core capabilities to adopt rather than rebuild:

1. **Profiles = trading-account isolation.** `~/.hermes/profiles/<name>/quant/{state.db, signals.jsonl, journal.md}`. The daemon respects `HERMES_PROFILE` env. One profile per broker / per strategy / per environment (paper vs live). This replaces a hand-rolled `--account` flag we would otherwise have built.
2. **Credential pools** for ccxt API key rotation per ADR-0005. The daemon pulls keys from the Hermes credential pool by name; rotation, masking, and revocation are Hermes-core's problem.
3. **Hermes cron for OPERATOR tasks ONLY** (daily digest emit, weekly journal compaction, end-of-week pnl audit). Hermes cron's 3-minute hard-interrupt budget is fine for these; it is **not** acceptable for the daemon (see D5).
4. **MEMORY.md stays separate** from the settlement journal. ADR-0010 §9 reproducibility forbids merging — MEMORY.md is human-edited, the settlement journal is Pydantic-rendered. Conflating them would break replay-from-bars.
5. **CLI seams** are already wired correctly in v0.1.1 via `register_cli_command(name='quant', ...)`. No change required; flagged so future PRs do not regress.

### D5: REJECTIONS with rationale

The review surfaced five anti-patterns that would have looked superficially attractive. Each is rejected, on the record:

| Anti-pattern | Why rejected |
|---|---|
| Cron for the daemon | Hermes cron's 3-minute hard-interrupt violates the daemon's required uptime; the tick loop and settlement loop both need persistent state across minute boundaries. |
| `delegate_task` for parallel analyst fan-out | Injects an LLM in the action path → violates ADR-0012 §LLMs out of action path. Analyst parallelism, when needed, is `asyncio.gather` over deterministic Analyst Protocol implementations. |
| `session_search` for journal indexing | Re-introduces the ChromaDB-class regression that ADR-0010 §Provenance explicitly avoids. The journal's only retrieval surface is `get_recent_lessons` (flat tail-N). |
| Filesystem checkpoints for daemon recovery | Hermes-core's checkpoint mechanism does not cover `state.db` / `signals.jsonl`; using it for the daemon would silently desync the bus from the SQLite watermark. The daemon's atomic-rename pattern (ADR-0001) is the only sanctioned recovery path. |
| `_voice_input_callback`-style takeover | The daemon does not touch voice; not applicable. Listed for completeness so future agents do not propose it. |

## Consequences

### Positive

- **Friction-free install.** `hermes plugins install baladithyab/hermes-quant` followed by `pip install -e '.[yfinance]'` yields a working advisor surface on first command. No broker account, no API key, no supervisor.
- **One plugin, two configs.** The operator's mental model is a single plugin with chat-mode and daemon-mode wings; they pick the wing per use case rather than per install.
- **No fork tax.** Every defect we hit in Hermes-core is upstreamable, because the integration surface is the public `PluginContext` contract. There is no private-attribute coupling to negotiate during a Hermes-core upgrade.
- **Profile isolation gives multi-account out of the box.** Paper-vs-live, BTC-vs-equity, dev-vs-prod — all separate profiles, no code change.
- **Zero-monkeypatch posture is auditable.** A reviewer can `grep -r 'apply()\|revert()' hermes_quant/` and confirm absence in CI.

### Negative

- **Two surfaces means two test matrices.** The advisor sync path and the daemon async path each need their own integration tests. Mitigated: the shared analyst + aggregator + calibrator code is exercised by both, so the duplication is at the wiring layer (call adapters, fixture loaders) rather than the algorithmic core.
- **Advisor surface returns a snapshot-in-time recommendation.** A user MAY interpret it as a guarantee or as a live signal. The README must be explicit that advisor output is informational, not a tradeable signal, and that only the autopilot surface emits tradeable signals (via `signals.jsonl`).
- **Zero monkeypatches means we cannot hot-fix Hermes-core defects out-of-band.** If Hermes-core ships a regression that affects hermes-quant, we are blocked on an upstream release or on shipping `hermes-quant-bootstrap`. Mitigated: the bootstrap-plugin path is well-understood prior art; it can be added later without architectural change to hermes-quant itself.

## Cross-references

- **ADR-0007** (canonical CLI surface) — autopilot side already locked; this ADR confirms the advisor surface uses the same `register_cli_command` seam.
- **ADR-0012** (LLMAnalyst deferred) — the chat-mode advisor's analyst implementations remain non-LLM in v0.1.2; LLMAnalyst lands in v0.3.0 and at that point both surfaces gain it simultaneously through the shared Analyst Protocol.
- **ADR-0010** (settlement journal) — the advisor surface reads via `get_recent_lessons(symbol, n_same, n_cross)`. The journal is written only by the autopilot surface; advisor mode against a profile that has never run the daemon simply returns an empty lesson list, which is correct.
- **ADR-0001** (sidecar architecture) — confirmed unchanged. The advisor surface is in-process and does not violate the sidecar boundary because it does not run a tick loop, does not write the bus, and does not actuate.
- [`docs/reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md`](../reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md) — full BUILD vs LEVERAGE vs MONKEYPATCH analysis from which D2–D5 are derived.

## Implementation order in v0.1.2

The dependencies between v0.1.2 work items dictate a specific order. Lowest-risk / highest-UX-win first; gating items pulled forward:

1. **Advisor surface MVP** — `quant_recommend` tool, sync path, yfinance bootstrap. Lowest risk, highest UX win. Ships the "anyone using Hermes can try it" promise on day one.
2. **Settlement journal writer** (ADR-0010) — needed by the advisor surface for lesson retrieval; also needed by the autopilot surface for operator legibility.
3. **`as_of` plumbing** (ADR-0005 amendment) — gates the no-lookahead test (`tests/test_no_lookahead.py`). Both surfaces depend on this.
4. **`portfolio_loader` rewrite** (ADR-0011) — gates the calibrator gate lift; required for the autopilot surface only, but blocks step 5.
5. **Calibrator gate lift** (ADR-0003 amendment) — closes the v0.1.2 contract.

Remaining v0.1.2 items (`KronosAnalyst`, OHLCV cache, ccxt provider) follow as previously planned and are not gated by the dual-surface split.

## Provenance

- User directive 2026-05-13: hermes-quant must be installable by "anyone using Hermes" with minimum friction.
- [`docs/reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md`](../reviews/2026-05-13-v0.1.2-architecture/build-vs-leverage-vs-monkeypatch.md): zero monkeypatch candidates, five LEVERAGE adoptions, five rejected anti-patterns.
- `hermes-mission-control-bootstrap/mc_bootstrap/` — two-patch shape cited as the prior-art template should a future `hermes-quant-bootstrap` become necessary.
- ADR-0007 §plugin shape — `register_cli_command(name='quant', ...)` seam, confirmed retained.
- ADR-0010 §Provenance — flat-tail recency retrieval as the only sanctioned journal-indexing mechanism.
