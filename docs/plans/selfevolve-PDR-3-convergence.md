# PDR-3 — `ConvergenceValidator` (cross-SOURCE require_ensemble at PERCEPTION)

> **Status:** implementation-ready plan · **Date:** 2026-05-31 · **Wave:** PDR-3 (GAP-B / VALIDATE)
> **Authoritative grounding (read before building):**
> - `docs/adr/ADR-0079-perception-decision-reaction-architecture.md` — D79.3 (two-layer ensemble), D79.5 (default-OFF/eval-gated), Rollout PDR-3 row, Consequences ("Cross-source convergence depends on real producers … source-family taxonomy must be policed").
> - `docs/design/pdr-unified-architecture.md` §3.2 ("DATA CONVERGENCE validation — require_ensemble at the SOURCE level, GAP-B") + the "clean distinction to hold" callout (cross-SOURCE@perception ≠ cross-ANALYST@decision; a social-arb signal needs BOTH).
> - `docs/research/2026-05-31-r-pdr234-seams.md` §2, §4, §5 (the PDR-3 attachment points cited inline below).
>
> A fresh agent can build this with no further research. Every `file:line` is verified against HEAD (2026-05-31).

---

## 0. One-paragraph statement of the primitive

`ConvergenceValidator` is a **pure function over the set of `CatalystItem`s for one symbol** that asks the Camillo VALIDATE question — *is this trend real?* — by counting **independent SOURCE FAMILIES** (`reddit` / `google_trends` / `news_rss` / `web_traffic`). It normalizes each item's raw `CatalystItem.source` string to a family, **polices shared upstreams** (two feeds whose origin is the same publisher / aggregator do NOT both count), and emits a small evidence Mapping `{n_families, families, validated, n_items}`. When `HERMES_QUANT_CONVERGENCE=1` and a symbol's items resolve to `< 2` independent families, the packet for that symbol is **haircut or dropped** at emission time in `synthesize_packets`. The score is also stamped on `PerceptionFrame.convergence` for provenance. This is `require_ensemble` **relocated to the perception layer (cross-SOURCE)** — *complementary, never a replacement*, for BMA's cross-ANALYST `require_ensemble` (`aggregators/bma.py:498-519`). A social-arb signal must clear **BOTH** to fire. Default-OFF; with the flag absent, behavior is **byte-identical** to today (no source-count requirement).

**What it is NOT (rails restated):** not an authority, not a sizing input, not a new ladder. `PerceptionFrame` stays a container; the deterministic gate (ADR-0004) stays final. It can only **subtract** (haircut/drop a single-source packet), never amplify. It is perception-layer **evidence only**. It stamps `asof` and reads only the items present at decision time (no lookahead).

---

## 1. The source-family taxonomy (the load-bearing modeling artifact)

The whole primitive hinges on a *policed* mapping from a raw `CatalystItem.source` string → a normalized family, with shared-upstream detection. The raw strings are produced at three known sites (recon §2, §4):

| Producer | Raw `source` string example | Site |
|---|---|---|
| GN-RSS news | `"<publisher name>"` (e.g. `"Reuters"`, `"CNBC"`) | `ingest.py:130` (set from the RSS `<source>` element) |
| Reddit | `"reddit/r/<sub> (score=.. c=..)"` | `social.py:91` |
| Google Trends | `"google_trends/<geo>"` | `social.py:185` |
| sign-eval harness | `"sign-eval"`, `"phase0-label"` | `eval.py:189`, label scripts |

### 1.1 Family definitions (the four ADR-0079 families)

```
reddit        — social-discussion family   (source startswith "reddit/")
google_trends — search-interest family     (source startswith "google_trends")
news_rss      — syndicated-news family      (anything from the GN-RSS ingester)
web_traffic   — web/app-traffic family      (future producer; B08 — reserved, no live source yet)
```

`unknown` is a fifth bucket for anything unrecognized (sign-eval, manual labels). **`unknown` NEVER counts toward convergence** (silence-by-default: an unclassifiable source proves nothing).

### 1.2 The shared-upstream police (the ADR-0079 Consequences requirement)

The ADR warns: *"if two 'independent' feeds actually share an upstream, convergence is illusory."* Three policed rules, all pure and deterministic:

1. **Same family never double-counts.** Convergence counts **distinct families**, not items. Ten Reddit posts = one `reddit` family vote. This is the primary defense and is automatic from counting families, not items.
2. **GN-RSS publisher collapse.** Google-News-RSS syndicates: the *same wire story* (e.g. an AP story) appears under many publisher names. Within the `news_rss` family this is already one family vote (rule 1 covers it). A known **press-wire list** (`_SHARED_UPSTREAM`) maps publisher substrings that are actually the *same wire* (`"yahoo"`, `"msn"`, `"google news"`, `"prnewswire"`, `"businesswire"`, `"globenewswire"`, …) — these are republishers, not independent reporting; they still collapse into `news_rss` (no behavior change today) but are **flagged** in the returned Mapping (`shared_upstream_collapsed`) so an operator audit can see when convergence rested on wire-republished noise.
3. **Cross-family shared-origin guard (reserved, B08).** When a real `web_traffic` producer lands, if it derives from the *same vendor* as `google_trends` (both Google properties), the taxonomy must treat them as one. Encode this as a `_FAMILY_ORIGIN` map (`google_trends → "google"`, `web_traffic(SimilarWeb) → "similarweb"`); convergence counts **distinct origins**, not distinct families, so two Google-owned feeds = one origin vote. Today only `google_trends` exists in the Google origin, so this is a no-op until B08; encode it now so the taxonomy is correct-by-construction when the producer lands.

> **Why count origins, not families:** families are a human-readable label; *origins* are the true independence unit. `n_independent = |distinct origins among items, excluding unknown|`. With today's producers each family maps 1:1 to a distinct origin, so `n_independent == n_families`; the origin layer only bites once two same-vendor producers coexist (B08).

---

## 2. Exact files: new + modified (cite seams by `file:line`)

### 2.1 NEW — `hermes_quant/perception/convergence.py` (the pure validator)

The pure primitive. No I/O, no flag read inside the scorer (the flag is read at the *call site* in `synthesize_packets` — §3), so the scorer is trivially unit-testable and the eval harness can call it directly.

```python
"""hermes_quant.perception.convergence — cross-SOURCE require_ensemble (ADR-0079 PDR-3, GAP-B).

The Camillo VALIDATE step relocated to the PERCEPTION layer: a trend is real only
when it shows across >=2 INDEPENDENT source families. Complementary to BMA's
cross-ANALYST require_ensemble (aggregators/bma.py:498-519); a social-arb signal
must clear BOTH. PURE + evidence-only: returns a score Mapping, never gates by
itself. The flag (HERMES_QUANT_CONVERGENCE) is read by the CALLER (synthesize), so
the scorer stays deterministic and offline-testable.

Rails: PerceptionFrame is a container (this fills .convergence); the deterministic
gate stays final; it can only SUBTRACT (haircut/drop a single-source packet),
never amplify. asof honesty: it reads only the items present at decision time.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hermes_quant.catalyst.ingest import CatalystItem

# ── family taxonomy (plan §1.1) ────────────────────────────────────────────
_FAMILY_REDDIT = "reddit"
_FAMILY_TRENDS = "google_trends"
_FAMILY_NEWS = "news_rss"
_FAMILY_WEB = "web_traffic"
_FAMILY_UNKNOWN = "unknown"  # never counts

CONVERGENCE_MIN_FAMILIES = 2  # the >=2 independent-origin bar (the taxonomy home)

# raw source string -> origin (the true independence unit, plan §1.2 rule 3).
# Today each family == a distinct origin; the map only bites at B08.
_FAMILY_ORIGIN = {
    _FAMILY_REDDIT: "reddit",
    _FAMILY_TRENDS: "google",        # Google-owned
    _FAMILY_NEWS: "news_rss",        # syndicated news (collapsed within family already)
    _FAMILY_WEB: "web_traffic",      # B08 placeholder; set to "google" if a Google web-traffic feed lands
}

# press-wire / aggregator publisher substrings that are NOT independent reporting
# (plan §1.2 rule 2). They still collapse into news_rss (no behavior change) but
# are FLAGGED so an operator audit sees when convergence rested on wire noise.
_SHARED_UPSTREAM = (
    "yahoo", "msn", "google news", "prnewswire", "pr newswire",
    "businesswire", "business wire", "globenewswire", "globe newswire",
    "accesswire", "newsfile",
)


def source_family(source: str) -> str:
    """Normalize a raw CatalystItem.source string to a source FAMILY.

    reddit/  -> reddit; google_trends -> google_trends; anything from the
    GN-RSS ingester (a bare publisher name) -> news_rss; recognized non-feed
    sources (sign-eval, phase0-label) -> unknown (never counts).
    """
    s = (source or "").strip().lower()
    if not s:
        return _FAMILY_UNKNOWN
    if s.startswith("reddit/"):
        return _FAMILY_REDDIT
    if s.startswith("google_trends"):
        return _FAMILY_TRENDS
    if s.startswith("web_traffic/") or s.startswith("similarweb"):
        return _FAMILY_WEB
    # non-feed / synthetic harness sources prove nothing about real convergence
    if s in {"sign-eval", "phase0-label", "n/a"} or s.startswith("test"):
        return _FAMILY_UNKNOWN
    # everything else is a GN-RSS publisher name (ingest.py:130) -> news family
    return _FAMILY_NEWS


@dataclass(frozen=True)
class ConvergenceResult:
    """Cross-source convergence evidence for one symbol's CatalystItem set.

    A CONTAINER of evidence, not an authority. ``validated`` is True iff
    ``n_independent >= min_families``.
    """
    n_items: int
    n_families: int                 # distinct families seen (excluding unknown)
    n_independent: int              # distinct ORIGINS (the true independence unit)
    families: tuple[str, ...]       # sorted distinct families (excluding unknown)
    validated: bool
    shared_upstream_collapsed: tuple[str, ...] = ()  # wire-republisher names seen
    min_families: int = 2

    def as_evidence(self) -> dict[str, Any]:
        """The Mapping stamped on PerceptionFrame.convergence (adapter.py:53)."""
        return {
            "n_items": self.n_items,
            "n_families": self.n_families,
            "n_independent": self.n_independent,
            "families": list(self.families),
            "validated": self.validated,
            "shared_upstream_collapsed": list(self.shared_upstream_collapsed),
            "min_families": self.min_families,
        }


def validate_convergence(
    items: Sequence[CatalystItem],
    *,
    min_families: int = CONVERGENCE_MIN_FAMILIES,
) -> ConvergenceResult:
    """PURE: count independent source ORIGINS across ``items`` for one symbol.

    >=min_families distinct origins (excluding 'unknown') => validated. This is
    cross-SOURCE require_ensemble: it asks "is this trend real?" and is
    COMPLEMENTARY to BMA's cross-ANALYST guard (a social-arb signal must clear
    BOTH). Reads only the items handed in (asof honesty: the caller filters the
    item set to <= decision time before calling).
    """
    families: set[str] = set()
    origins: set[str] = set()
    wires: list[str] = []
    for it in items:
        fam = source_family(it.source)
        if fam == _FAMILY_UNKNOWN:
            continue
        families.add(fam)
        origins.add(_FAMILY_ORIGIN.get(fam, fam))
        if fam == _FAMILY_NEWS:
            low = (it.source or "").lower()
            for w in _SHARED_UPSTREAM:
                if w in low:
                    wires.append(it.source)
                    break
    n_independent = len(origins)
    return ConvergenceResult(
        n_items=len(items),
        n_families=len(families),
        n_independent=n_independent,
        families=tuple(sorted(families)),
        validated=(n_independent >= min_families),
        shared_upstream_collapsed=tuple(sorted(set(wires))),
        min_families=min_families,
    )


__all__ = ["source_family", "validate_convergence", "ConvergenceResult", "CONVERGENCE_MIN_FAMILIES"]
```

**Why a pure function over the item set, and not over `PropagationResult`:** the recon (§2, lines 42-45) confirms `PropagationResult.contributions` carries graph-edge provenance (`source` = the graph ENTITY, `relation`, `weight`) — `propagation.py:361-366` — **not** the feed family. The feed family lives only on `CatalystItem.source`. So convergence MUST be computed over the **`CatalystItem` set**, before/around synthesis, not from the propagation result.

### 2.2 MODIFIED — `hermes_quant/catalyst/synthesize.py` (gate packet EMISSION)

This is the cleanest attachment point per recon §5 (lines 88-94): *"gate packet EMISSION inside `synthesize_packets` at the per-symbol loop `synthesize.py:100-108` (before `packets.append`, line 138)."*

**The problem to solve first (item-set grouping):** `synthesize_packets` today iterates **per item** (`synthesize.py:90`) and emits packets per touched symbol. Convergence is a property of the **set of items that touch a given symbol**, so we cannot decide it inside the per-item loop. Two-pass design:

1. **Pass 1 (new):** for each item that is a catalyst with entities, run the SAME `classify_headline` + `extract_entities` + `propagate` ONCE, recording `(item, cls, results)` tuples AND building `symbol_items: dict[str, list[CatalystItem]]` (which items touch each symbol). This avoids double-propagating.
2. **Pass 2 (the existing emission loop, now convergence-aware):** when `HERMES_QUANT_CONVERGENCE=1`, for each symbol look up its item set, call `validate_convergence(symbol_items[sym])`, and:
   - **validated (`n_independent >= 2`):** emit unchanged (today's behavior).
   - **not validated (single-source):** apply the convergence policy — multiply confidence by `CONVERGENCE_SINGLE_SOURCE_HAIRCUT` (default `0.0` = DROP/abstain; configurable to a partial haircut like `0.5` if a later eval earns it). Drop when `sized_confidence <= 0.0` (the existing guard at `synthesize.py:107` already drops zero-confidence packets — reuse it).
   - Stamp the convergence evidence into `packet.metadata["convergence"] = result.as_evidence()` so it is auditable.

**Flag-gating idiom (copy `wiring.py:40` / `builder.py:175`):**

```python
import os
convergence_on = os.environ.get("HERMES_QUANT_CONVERGENCE", "0") == "1"
```

Read it **at call time** inside `synthesize_packets` (not at import) so the flag can be flipped per-process and tests can `monkeypatch.setenv`. When OFF, the new pre-pass still runs harmlessly (it only groups items) but the emission policy is the **identity** — no haircut, no drop, no metadata key written — so output is **byte-identical** to today.

**New module-level constant (mirror `CONSUMER_TREND_CONFIDENCE_HAIRCUT`, `synthesize.py:52-53`; import `CONVERGENCE_MIN_FAMILIES` from `perception.convergence`):**

```python
from hermes_quant.perception.convergence import (
    CONVERGENCE_MIN_FAMILIES, validate_convergence, ConvergenceResult,
)

# PDR-3: cross-SOURCE require_ensemble. A single-source social-arb signal has not
# been VALIDATED (Camillo: a real trend shows across >=2 independent sources). With
# HERMES_QUANT_CONVERGENCE=1, an un-validated packet is dropped (haircut 0.0) so it
# cannot fire. Set to a partial multiplier (e.g. 0.5) only if a larger labeled set
# (B09) earns a softer policy. DEFAULT-OFF: flag absent => no source-count requirement.
CONVERGENCE_SINGLE_SOURCE_HAIRCUT = 0.0  # 0.0 = drop/abstain (the require_ensemble default)
```

**Exact edit shape** (the load-bearing change; do NOT touch the `magnitude`/`confidence` derivation — PDR-3 only gates EMISSION, it is orthogonal to PDR-2's magnitude swap at `synthesize.py:116`):

```python
def synthesize_packets(items, *, horizon="1d", graph=None, aliases=None,
                       propagation_log=None, model="catalyst-sense:v1"):
    ...  # graph/aliases load unchanged (synthesize.py:84-87)
    convergence_on = os.environ.get("HERMES_QUANT_CONVERGENCE", "0") == "1"

    # ── Pass 1: compute per-item (cls, results) ONCE; group items by symbol ──
    prepared = []  # list[(item, cls, results)]
    symbol_items: dict[str, list[CatalystItem]] = {}
    for item in items:
        cls = classify_headline(item.title)
        if not cls.is_catalyst:
            continue
        sign = polarity_sign(cls.polarity)
        ents = extract_entities(item.title, aliases)
        if not ents:
            continue
        results = propagate(ents, sign, graph, log=propagation_log)
        prepared.append((item, cls, results))
        for sym, res in results.items():
            if res.stance == "neutral" or res.confidence <= 0.0:
                continue
            symbol_items.setdefault(sym, []).append(item)

    # ── Pass 1.5: convergence per symbol (only when flag ON) ──
    convergence_by_symbol: dict[str, ConvergenceResult] = {}
    if convergence_on:
        for sym, its in symbol_items.items():
            convergence_by_symbol[sym] = validate_convergence(
                its, min_families=CONVERGENCE_MIN_FAMILIES)

    # ── Pass 2: emit packets (unchanged), applying the convergence policy ──
    packets = []
    for item, cls, results in prepared:
        for sym, res in results.items():
            if res.stance == "neutral" or res.confidence <= 0.0:
                continue
            haircut = _consumer_trend_haircut(res)
            sized_confidence = round(min(1.0, res.confidence * haircut), 4)

            conv = convergence_by_symbol.get(sym)
            if convergence_on and conv is not None and not conv.validated:
                # cross-SOURCE require_ensemble: un-validated single-source => drop
                sized_confidence = round(sized_confidence * CONVERGENCE_SINGLE_SOURCE_HAIRCUT, 4)

            if sized_confidence <= 0.0:
                continue
            packet = semantic_packet_from_dict({
                ...,  # all existing keys unchanged
                "confidence": sized_confidence,
                "magnitude": round(float(cls.severity), 4),
                "metadata": {
                    ...,  # unchanged keys (synthesize.py:128-135)
                    **({"convergence": conv.as_evidence()} if (convergence_on and conv is not None) else {}),
                },
            })
            packets.append(packet)
    return packets
```

> **Byte-identity proof obligation:** with `convergence_on=False`, the only difference from today is the harmless Pass-1/Pass-2 split (same `classify`→`extract`→`propagate` calls, same order, same items). The `metadata["convergence"]` key is written **only** when the flag is on. The `propagation_log` is still appended exactly once per item (inside `propagate`, called once per item in Pass 1) — a test asserts this ordering is unchanged (§5.3).

### 2.3 MODIFIED — `hermes_quant/perception/builder.py` (stamp `frame.convergence`)

Per recon §5 (lines 92-93): *"Result stored in `frame.convergence` (`builder.py`)."* The builder's Step 5 semantic slice (`builder.py:168-185`) loads packets via `load_packets_for`. After packets are loaded, compute the per-symbol convergence evidence and attach it (frame is single-symbol, so this is the convergence for *this* symbol's loaded packets' source set).

**Minimal, flag-gated addition after `builder.py:185`** (before the `last_close` step at line 187):

```python
# ── Step 5b: PDR-3 convergence evidence (HERMES_QUANT_CONVERGENCE) ──
# Container-only: stamps frame.convergence for provenance/audit. The EMISSION
# gate lives in synthesize.py; here we record what the loaded packets converged
# on. Silence-by-default: OFF / no packets / any error -> None.
frame_convergence = None
if semantic_packets and os.environ.get("HERMES_QUANT_CONVERGENCE", "0") == "1":
    try:
        from hermes_quant.perception.convergence import (
            CONVERGENCE_MIN_FAMILIES, source_family,
        )
        # packets carry their feed family in metadata.feed_source (synthesize.py:132)
        fams = sorted({
            source_family((p.get("metadata") or {}).get("feed_source", ""))
            for p in semantic_packets
        } - {"unknown"})
        frame_convergence = {"families": fams, "n_independent": len(fams),
                             "validated": len(fams) >= CONVERGENCE_MIN_FAMILIES}
    except Exception as exc:  # noqa: BLE001 — never block frame build
        logger.debug("build_perception_frame(%s): convergence stamp failed: %s", symbol, exc)
```

Then pass `convergence=frame_convergence` in the `PerceptionFrame(...)` constructor (replacing the hardcoded `convergence=None` at `builder.py:200`).

> **Builder vs. synthesize split (why both):** the EMISSION gate (the change with rails impact) lives in `synthesize.py`, exactly where recon §5 places it — validation happens once, at packet creation. The builder stamp is **pure provenance** (a container field for audit). Loaded packets already passed through `synthesize_packets`, so when the flag is on, single-source packets were already dropped at write time; the builder stamp reflects whatever survived. Keep `CONVERGENCE_MIN_FAMILIES` in `perception/convergence.py` (the taxonomy home) and import it in both `synthesize.py` and `builder.py` — ONE source of truth.

### 2.4 NO CHANGE — `adapter.py`, `frame.py`, `bma.py`, `semantic.py`, `risk/gate.py`

- `adapter.py:53-54` **already** projects a non-None `frame.convergence` into `ctx.extras["convergence"]` — no edit needed; analysts ignore the key (recon §1, `protocol.py:16`). PDR-3 fills the slot the adapter already wires.
- `frame.py:39` already declares the `convergence` slot. No shape change.
- `bma.py:498-519` cross-ANALYST `require_ensemble` is **untouched** — PDR-3 is complementary, at a different layer (D79.3). A social-arb packet that clears PDR-3 still must find a numerical corroborator in BMA. **Both** guards remain.
- `semantic.py`, `risk/gate.py`: no change. Convergence shrinks the *packet* before it ever becomes an `AnalystView`; the view path and gate are byte-identical.

### 2.5 PROMOTE — versioned fixture (per N13 / M16)

The labeled social-arb set currently reads/writes `/tmp/phase0_labels.json` and live yfinance (`ops/scripts/quant-catalyst-socialarb-labels.py:67`; `-eval.py:80`). **N13** (`docs/research/2026-05-30-backlog-consolidated.md:157`) requires promoting it to a versioned `tests/fixtures/` path before B07/B09 can be trusted. PDR-3's eval depends on it, so promote now:

- **NEW** `tests/fixtures/socialarb/labeled_cases.json` — the 5 Camillo cases WITH realized forward returns frozen (run `quant-catalyst-socialarb-labels.py` once, commit the JSON output; the returns become a static fixture so the eval is deterministic + offline, no live yfinance).
- **NEW** `tests/fixtures/socialarb/convergence_items.json` — for each case, the **multi-source** `CatalystItem` sets PDR-3 needs (a reddit item + a google_trends item + a news item per validated case; single-source sets for the negative cases). This is the B09-shaped input the convergence eval consumes. Hand-authored from the case headlines + synthetic per-family framings (mirroring how `social.py` frames trends headlines).

---

## 3. The flag-gating idiom (copy an existing `HERMES_QUANT_*` check)

Exact precedent copied verbatim from `hermes_quant/catalyst/wiring.py:40` and `hermes_quant/perception/builder.py:175`:

```python
import os
if os.environ.get("HERMES_QUANT_CONVERGENCE", "0") == "1":
    ...  # the only branch that changes behavior
```

Rules (identical to every other `HERMES_QUANT_*` flag in the repo, verified at `advisor.py:377`, `autonomous.py:550`, `admissibility/oracle.py:406`):
- **Read at call time**, never cached at import (so per-process flip + `monkeypatch.setenv` work).
- Default-absent → `"0"` → OFF → **no source-count requirement**.
- With the flag absent, the new code paths are **identity transforms**: no haircut, no drop, no `metadata["convergence"]` key, `frame.convergence` stays `None`. Byte-identical to today.

---

## 4. Function signatures (the contract a fresh agent implements)

```python
# hermes_quant/perception/convergence.py  (NEW)
def source_family(source: str) -> str: ...
def validate_convergence(items: Sequence[CatalystItem], *, min_families: int = 2) -> ConvergenceResult: ...

@dataclass(frozen=True)
class ConvergenceResult:
    n_items: int; n_families: int; n_independent: int
    families: tuple[str, ...]; validated: bool
    shared_upstream_collapsed: tuple[str, ...] = (); min_families: int = 2
    def as_evidence(self) -> dict[str, Any]: ...
CONVERGENCE_MIN_FAMILIES: int = 2

# hermes_quant/catalyst/synthesize.py  (MODIFIED — signature UNCHANGED, body two-pass + flag-gated)
def synthesize_packets(items, *, horizon="1d", graph=None, aliases=None,
                       propagation_log=None, model="catalyst-sense:v1") -> list[SemanticPacket]: ...
CONVERGENCE_SINGLE_SOURCE_HAIRCUT: float = 0.0

# hermes_quant/catalyst/eval.py  (MODIFIED — add the convergence-aware precision runner)
def run_precision_with_convergence(
    case_item_sets: list[tuple[EvalCase, list[CatalystItem]]],
    *, min_hit_rate: float = 0.65, require_convergence: bool = True,
    graph=None, aliases=None,
) -> PrecisionResult: ...
```

> **`run_precision_with_convergence` rationale:** the existing `run_precision` (`eval.py:76-120`) synthesizes one packet per case *item* (`synthesize_packets([case.item], ...)`) — it cannot exercise multi-source convergence because each case has one item. PDR-3's eval needs the **item SET** per case. Add a sibling runner that takes `(EvalCase, list[CatalystItem])` pairs, runs with `HERMES_QUANT_CONVERGENCE=1`, synthesizes from the FULL set, and measures hit-rate on the surviving (validated) packets. The **higher bar** (`min_hit_rate=0.65` vs the 0.60 D74.7 floor) is the ADR-0079 Rollout PDR-3 promise: *"the larger labeled social-arb set (B09) clears a HIGHER bar with the ≥2-source requirement on."* Keep `run_precision` unchanged (PDR-2 reuses it per recon §4).

---

## 5. Tests — eval gate as pytest-verifiable acceptance criteria

All new tests live in `tests/perception/` (the existing PDR home, alongside `test_frame_replay.py`) and `tests/unit/` for the catalyst-layer pieces. Run with `~/.hermes/hermes-agent/venv/bin/python3 -m pytest`.

### 5.1 NEW `tests/perception/test_convergence_validator.py` — the pure-function unit eval

```python
def test_two_independent_families_validates():
    items = [_reddit_item("CELH"), _trends_item("CELH")]
    r = validate_convergence(items)
    assert r.validated and r.n_independent == 2 and "reddit" in r.families

def test_single_family_does_not_validate():
    items = [_reddit_item("CELH"), _reddit_item("CELH")]  # 10 reddit posts != convergence
    assert not validate_convergence(items).validated  # n_independent == 1

def test_unknown_source_never_counts():
    items = [_reddit_item("CELH"), _signeval_item("CELH")]  # sign-eval is unknown
    assert not validate_convergence(items).validated  # only reddit counts

def test_shared_upstream_flagged_but_news_collapses():
    # two GN-RSS items, one a PRNewswire republish -> still ONE news_rss family
    items = [_news_item("CELH", "Reuters"), _news_item("CELH", "PRNewswire")]
    r = validate_convergence(items)
    assert r.n_families == 1 and r.n_independent == 1
    assert any("PRNewswire" in s for s in r.shared_upstream_collapsed)

def test_source_family_taxonomy():
    assert source_family("reddit/r/stocks (score=5 c=2)") == "reddit"
    assert source_family("google_trends/US") == "google_trends"
    assert source_family("Reuters") == "news_rss"
    assert source_family("sign-eval") == "unknown"

def test_b08_origin_collapse_reserved(monkeypatch):
    # when web_traffic shares the Google origin it must NOT double-count.
    import hermes_quant.perception.convergence as c
    monkeypatch.setitem(c._FAMILY_ORIGIN, "web_traffic", "google")
    items = [_trends_item("CELH"), _webtraffic_item("CELH")]  # both Google origin
    assert validate_convergence(items).n_independent == 1  # one origin
```

**Acceptance:** all pass; the taxonomy is policed; `n_independent` (origins) is the gating count, not item count.

### 5.2 NEW `tests/perception/test_convergence_eval_gate.py` — the HIGHER-bar precision eval (B09)

```python
def test_convergence_clears_higher_bar_with_requirement_on(monkeypatch):
    """ADR-0079 Rollout PDR-3: the labeled set clears a HIGHER bar (>=0.65) with
    the >=2-source requirement ON. Multi-source (validated) cases survive and are
    directionally correct; single-source cases are dropped (not scored)."""
    case_item_sets = _load_fixture("tests/fixtures/socialarb/convergence_items.json")
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    res = run_precision_with_convergence(case_item_sets, min_hit_rate=0.65,
                                         graph=_CONSUMER_GRAPH, aliases=_CONSUMER_ALIASES)
    assert res.passed and res.hit_rate >= 0.65
```

**Acceptance (the eval gate):** with `HERMES_QUANT_CONVERGENCE=1`, `run_precision_with_convergence` over the versioned fixture returns `passed=True` at `min_hit_rate >= 0.65` — strictly higher than the 0.60 D74.7 floor. **This is the gate the ADR pins before any live influence.** Document explicitly that the *full* live-influence flip additionally needs B08 (real reddit/trends/web-traffic producers) + B09 (a larger labeled set) volume to clear the higher bar at scale — but the **mechanism + taxonomy + this unit eval build and pass NOW** on the committed fixture.

### 5.3 NEW `tests/perception/test_convergence_flag_off_byte_identical.py` — the rails proof

```python
@pytest.mark.parametrize("items_fixture", _ALL_CONVERGENCE_FIXTURES)
def test_flag_off_synthesize_byte_identical(monkeypatch, items_fixture):
    """HERMES_QUANT_CONVERGENCE absent => synthesize_packets output is bit-for-bit
    today's (no haircut, no drop, no metadata['convergence'] key)."""
    monkeypatch.delenv("HERMES_QUANT_CONVERGENCE", raising=False)
    pkts_off = synthesize_packets(items, graph=G, aliases=A)
    assert [p.to_dict(include_hash=True) for p in pkts_off] == _GOLDEN_OUTPUT
    assert all("convergence" not in (p.metadata or {}) for p in pkts_off)

def test_flag_off_propagation_log_order_unchanged(monkeypatch):
    """The two-pass refactor must not change propagation_log ordering/contents."""
    monkeypatch.delenv("HERMES_QUANT_CONVERGENCE", raising=False)
    log_new: list[dict] = []
    synthesize_packets(items, graph=G, aliases=A, propagation_log=log_new)
    assert log_new == _GOLDEN_PROP_LOG
```

**Acceptance:** flag-OFF output and propagation-log are byte-identical to the pre-PDR-3 baseline (capture the golden once on current HEAD before refactoring, store as a fixture). Also extend the existing `tests/perception/test_frame_replay.py` `_REPLAY_KEYS` replay with a CONVERGENCE-OFF parametrize so the full-pipeline `recommend(...)` replay proves no live-path divergence.

### 5.4 NEW `tests/perception/test_convergence_asof_no_lookahead.py` — the asof honesty test

```python
def test_convergence_only_sees_items_at_or_before_asof():
    """A future-dated source must NOT contribute to convergence. The caller
    (load_packets_for / the eval) filters items to <= decision asof; convergence
    over the filtered set must drop the future family."""
    past_reddit = _reddit_item("CELH", published_at="2024-01-01T00:00:00Z")
    future_trends = _trends_item("CELH", published_at="2099-01-01T00:00:00Z")
    asof = pd.Timestamp("2024-06-01T00:00:00Z")
    visible = [it for it in [past_reddit, future_trends] if it.published_at <= asof]
    r = validate_convergence(visible)
    assert r.n_independent == 1 and not r.validated  # future trends excluded => single-source
```

**Acceptance:** convergence is computed only over items with `published_at <= asof`. Because `CatalystItem.published_at` is the fidelity anchor that becomes `packet.asof` (recon §2, `ingest.py:33`, `synthesize.py:112`), and `load_packets_for` already validates packets `<= asof` (`synthesize.py:207-211`), the no-lookahead gate is preserved: a future source cannot manufacture convergence. The validator has no clock of its own; it scores only the handed-in (already-filtered) set.

### 5.5 NEW `tests/unit/test_convergence_complementary_to_bma.py` — the two-layer-ensemble proof

```python
def test_social_arb_must_clear_BOTH_layers(monkeypatch):
    """D79.3: cross-SOURCE (PDR-3) AND cross-ANALYST (BMA) are independent gates.
    A multi-source packet (clears PDR-3) that finds NO numerical corroborator is
    STILL silenced by BMA require_ensemble (n_distinct_analysts <= 1)."""
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    pkts = synthesize_packets([_reddit_item("CELH"), _trends_item("CELH")], graph=G, aliases=A)
    assert pkts  # PDR-3 passed (>=2 families)
    # feeding ONLY the semantic view into BMA (no TA/Kronos agreement) -> silenced
    # by require_ensemble (reuse the existing bma test scaffold, bma.py:498-519).

def test_convergence_is_complementary_not_replacement():
    """Turning PDR-3 ON does not relax BMA's cross-ANALYST guard."""
    ...
```

**Acceptance:** demonstrates the **two independent ensemble requirements**. PDR-3 ON never relaxes `bma.py:498-519`; a lone-analyst signal is still silenced even after clearing convergence. This is the explicit ADR-0079 D79.3 / design §3.2 "clean distinction" invariant, made executable.

### 5.6 Eval-gate command (the reproducible CI line)

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest \
  tests/perception/test_convergence_validator.py \
  tests/perception/test_convergence_eval_gate.py \
  tests/perception/test_convergence_flag_off_byte_identical.py \
  tests/perception/test_convergence_asof_no_lookahead.py \
  tests/unit/test_convergence_complementary_to_bma.py -q
```

Plus the full-suite regression (proves nothing else moved):
```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/ -q
```

---

## 6. The perception-layer safety frame (restate; every line is a build constraint)

- **Evidence-only / no authority.** `ConvergenceValidator` returns a Mapping and (when ON) can only **subtract** — haircut/drop a single-source packet. It never amplifies, never raises confidence, never selects direction or size. The deterministic gate (ADR-0004) stays the FINAL authority; BMA still fuses peers.
- **`PerceptionFrame` is a container, not an authority.** PDR-3 fills the pre-existing `frame.convergence` slot (`frame.py:39`) for provenance/audit; the adapter already projects it into `ctx.extras["convergence"]` (`adapter.py:53`) where analysts ignore it (`protocol.py:16`). The actual EMISSION gate lives in `synthesize.py` (one place, at packet creation), not in the frame.
- **Complementary to cross-ANALYST `require_ensemble`, NEVER a replacement.** Two layers (D79.3 / design §3.2): cross-SOURCE@perception (*is the trend real?*) + cross-ANALYST@decision (`bma.py:498-519`, *do my models concur?*). A social-arb signal must clear **BOTH**. Turning PDR-3 ON must not, and does not, relax BMA (test §5.5).
- **Source-family taxonomy is policed.** Same family never double-counts; press-wire republishers collapse within `news_rss` and are flagged; same-vendor cross-family feeds collapse by ORIGIN (B08-reserved). `unknown` never counts. This is the ADR-0079 Consequences requirement ("two 'independent' feeds sharing an upstream = illusory convergence") made code.
- **`asof` honesty / no-lookahead.** The validator has no clock; it scores only the handed-in item set, which the caller filters to `published_at <= asof` (`load_packets_for`, `synthesize.py:207-211`; `CatalystItem.published_at` = fidelity anchor, `ingest.py:33`). A future source cannot manufacture convergence (test §5.4).
- **PDR-4 silence-only boundary (restated for the sibling primitive, not built here).** PDR-4 `SaturationScore` applies a multiplier `m∈(0,1]` to the `HermesSemanticAnalyst`'s OWN `AnalystView.confidence` BEFORE BMA, with TWO property tests: (a) post-saturation conf ≤ pre, for every input (never raises); (b) for every NON-semantic view, contribution is bit-identical sat-on-vs-off (never touches another analyst). PDR-3 shares the same authority boundary (evidence can only quiet *itself*) — a single-source packet is quieted/dropped, the numerical analysts are untouched.
- **Default-OFF + eval-gated; ships now, live-influence later.** The primitive + taxonomy + unit eval **build and pass now** behind `HERMES_QUANT_CONVERGENCE` (default-OFF, byte-identical when absent). The **full live-influence** flip additionally needs B08 (real reddit/trends/web-traffic producers) and B09 (larger labeled set) data volume to clear the higher bar at scale — but the MECHANISM and its unit eval do not wait for that. Promotion follows the ADR-0079 discipline: default-OFF construction → eval-gate (§5.2, ≥0.65) → operator side-by-side audit → flip on the cron wrapper.
- **Discrete ladder untouched.** PDR-3 never touches sizing; it shrinks a *packet* before it becomes a view. The `{0, ±0.05, ±0.10, ±0.15, ±0.20}` ladder is not widened.

---

## 7. Build order (for the executing agent)

1. **Capture the flag-OFF golden** (run current HEAD `synthesize_packets` + `propagation_log` on the convergence fixtures; freeze `_GOLDEN_OUTPUT` / `_GOLDEN_PROP_LOG`) — BEFORE refactoring, so §5.3 has a true baseline.
2. **`perception/convergence.py`** (pure; §2.1) + `tests/perception/test_convergence_validator.py` (§5.1). TDD this first — it has no dependencies.
3. **Promote fixtures** to `tests/fixtures/socialarb/` (§2.5): run `quant-catalyst-socialarb-labels.py` once, freeze the JSON; author `convergence_items.json`.
4. **Two-pass refactor of `synthesize.py`** (§2.2), flag-gated. Run §5.3 byte-identical + §5.4 asof tests immediately.
5. **`run_precision_with_convergence` in `eval.py`** (§4) + `tests/perception/test_convergence_eval_gate.py` (§5.2) — confirm ≥0.65 on the fixture.
6. **Builder stamp** (`builder.py` Step 5b, §2.3) + extend the frame-replay parametrize for CONVERGENCE-OFF.
7. **Complementary-to-BMA test** (§5.5) — prove the two layers are independent.
8. **Full suite** + lint; update the ADR-0079 Rollout PDR-3 row status from "future wave" to "built, default-OFF, eval-gated (unit bar cleared; live flip pending B08/B09)".

---

## 8. Acceptance criteria (pytest-verifiable, copy into the PR)

1. `tests/perception/test_convergence_validator.py` — taxonomy + `n_independent` (origin) counting + shared-upstream police all pass.
2. `tests/perception/test_convergence_eval_gate.py` — `run_precision_with_convergence(...)` `passed=True` at `min_hit_rate>=0.65` (the HIGHER bar) on the **versioned** `tests/fixtures/socialarb/` set (NOT `/tmp`, per N13).
3. `tests/perception/test_convergence_flag_off_byte_identical.py` — flag-OFF `synthesize_packets` output AND `propagation_log` are byte-identical to the pre-PDR-3 golden; no `metadata["convergence"]` key when OFF.
4. `tests/perception/test_convergence_asof_no_lookahead.py` — a future-dated source cannot manufacture convergence; the validator scores only the handed-in (≤asof-filtered) set.
5. `tests/unit/test_convergence_complementary_to_bma.py` — a packet that clears PDR-3 but finds no numerical corroborator is STILL silenced by BMA `require_ensemble`; PDR-3 ON does not relax `bma.py:498-519`.
6. Full `pytest tests/ -q` green; `frame_replay` extended with a CONVERGENCE-OFF parametrize stays green.
