# Study — Financial / Economic Calendar as a Risk + Perception Data Point

- Date: 2026-05-31
- Type: research → architecture study (decisive)
- Status: recommendations + dependency-ordered seed specs; one ADR proposed (ADR-0084)
- Operator question: *Should hermes-quant add a financial/economic calendar (FOMC / ADP / CPI / NFP + earnings) as a risk + perception data point, and can the pipeline's agents deliberate on it?*

## Verdict (one line)

**Yes — add it, as a default-OFF, eval-gated, asof-honest capability with three layers:**
1. a **stdlib calendar adapter** that emits two-timestamp `CalendarEvent`s (the FOMC seed YAML + BLS iCal are the no-key primary; FRED + yfinance are keyed/best-effort fallbacks),
2. a **rails-safe additive REJECT/abstain guard** in the immutable deterministic gate (a pre-event blackout that can only *shrink* a target, never resize the ladder), **plus** an options earnings-proximity (O8) check that fires only on net-theta-paying / net-vega-long structures,
3. a **read-only `EventRisk` evidence field** on `PerceptionFrame.extras` that analysts + bull/bear/judge **may read** to color conviction — with the deterministic guard as the **independent backstop** that does not depend on any agent reading the field.

The deliberation layer *can* reason about scheduled-event risk and it is *useful* that it does (it shapes conviction and produces an auditable rationale), **but it must never be the only thing standing between the book and an event** — that is the deterministic guard's job. This is the ESMA "hard block independent of strategy logic" principle, which is exactly hermes-quant's immutable REJECT-only gate (ADR-0004).

---

## 1. Current gap (file-grounded)

hermes-quant has **zero** scheduled-event awareness. Verified this session:

- `hermes_quant/catalyst/ingest.py` ingests **news that OCCURRED** — `CatalystItem.published_at` (ingest.py:33) is "tz-aware UTC — the fidelity anchor (becomes packet.asof)". There is exactly ONE timestamp, and it is a *past* event time. There is no notion of a *forward scheduled* event.
- `hermes_quant/risk/gate.py` enforces an 8-rule sequence with silence counters for halt / drawdown / daily-loss / flat / cooldown / cost / min-trade / **lookahead** (gate.py:31-38). None of these is event-aware. Rule 0.5 `check_view_lookahead` silences any view whose evidence `available_at > signal.asof` via an `EvidenceStore.get(evidence_id).available_at` lookup (gate.py docstring lines 17-22, body ~94-107) — **this is the exact asof-honesty primitive a calendar event plugs into.**
- `hermes_quant/risk/options_gate.py` runs O1-O7 (classify → max-loss → no-naked → gamma → theta → vega → BPR → pin-risk; options_gate.py:370-375) and computes `net_greeks` (theta/vega sign) with `min_dte_for_new_entry=7` and `pin_risk_dte_threshold=3` (options_gate.py:78-79). It is **earnings-DATE blind** — a 30-DTE long straddle that spans earnings in 3 days passes O1-O7 untouched. The IV-crush trap is unmodeled.
- `hermes_quant/playbook/scorers.py` is the *only* place that touches earnings dates: it calls `tk.earnings_dates` and `tk.calendar` under stderr-suppression + try/except (scorers.py:560-619), uses **`days_since_earnings`** (PAST), and defaults to `30` when missing (scorers.py:618-619). The `covered_call days_since_earnings>=5` convention there is the existing precedent for **N=5**.
- `hermes_quant/perception/frame.py` `PerceptionFrame` is add-only versioned with `extras` as a "forward-compat escape hatch" (frame.py:42-46) and the documented rule "consumers ignore unknown fields" (frame.py:28-29). Analysts already read `ctx.extras` for `regime`, `semantic_packets`, `saturation`, `ground_truth_block` (semantic.py:96-181, classical_ta.py:304, kronos.py:562, microstructure.py:326) — the carrier for an `EventRisk` field already exists and is already consumed.
- `hermes_quant/aggregators/deliberative.py` builds bull/bear turns from weighted views (deliberative.py:286-304); `committee_runner.py:build_committee_turns_from_packets` builds turns from packet evidence. The debate layer is already structured to read evidence into rationales — so an `EventRisk` field is naturally readable there.
- `hermes_quant/catalyst/propagation_graph.seed.yaml` is a vendored, operator-editable, version-controlled seed with a strong "review the highest-risk field" discipline — the **exact precedent** for a vendored `fomc_calendar.seed.yaml`.

Conclusion: the seams are all present; the gap is a missing *forward, two-timestamp* event type and the small amount of plumbing to feed it into (a) the gate as a silence rule and (b) the perception frame as a read-only field.

---

## 2. Recommended DATA source(s)

**Decisive ranking (no-key + government-primary first):**

### Tier 1 — Macro (build first, highest leverage, lowest risk)
- **FOMC → vendor `fomc_calendar.seed.yaml`.** 8 meeting windows/yr + the 8 official blackout windows, each row carrying BOTH `scheduled_for` and `announced_at`. Source of truth: federalreserve.gov + chicagofed.org federal-reserve-calendars. FOMC dates are published ~1 year ahead and rarely revised, so a vendored seed is more robust and more asof-honest than any live fetch (the `announced_at` is a hard fact, not a guess). Mirror `propagation_graph.seed.yaml`. Refresh annually via an `ops/scripts/quant-*` script with a freshness assertion (warn if the latest seeded meeting is in the past).
- **BLS macro (CPI / PPI / NFP / ECI / JOLTS) → the no-key iCal** `https://www.bls.gov/schedule/news_release/bls.ics`, parsed stdlib (line-based `BEGIN:VEVENT` / `DTSTART` / `SUMMARY` / `LAST-MODIFIED`). `DTSTART` = `scheduled_for`; `LAST-MODIFIED` (or the fetch timestamp clamped to BLS's weekly-Friday refresh) = `announced_at`. No new dependency — an `.ics` is trivially line-parsed, same posture as ingest.py's xml.etree.

### Tier 1 fallback (keyed, default-OFF)
- **FRED** `fred/releases/dates?include_release_dates_with_no_data=true` (MUST pass that flag or future dates are silently excluded). Needs a free key → **key-absent must mean "source unavailable → silence-by-default", never a crash.** Strongest *vintage* provenance (`release_last_updated` + realtime period) but cannot be the only macro source.

### Tier 2 — Earnings (behind its own sub-flag; lower reliability)
- **Forward per-ticker earnings → `yfinance.Ticker.calendar`, NOT `.earnings_dates`.** The `.earnings_dates` future-date path is **confirmed broken upstream** (Yahoo retagged future events `eventtype==11`; GH #2552/#2566; maintainer says use `.calendar`). `.calendar` gives a date-only next earnings date → clamp the blackout to a conservative full-day band. yfinance is already an optional extra in `pyproject.toml`. Because a yfinance forward date has **no reliable `announced_at`**, it is allowed to ONLY widen risk (fail-closed silence), **never** to inform direction.
- **OCCURRED earnings → SEC EDGAR 8-K Item 2.02** (data.sec.gov, no key, UA header, ≤10 req/s), `asof = filing_date`. Use it to *end* a blackout deterministically and confirm an event fired. Filter on Item 2.02 + EX-99.1 — not every 8-K is earnings.

### Explicitly do NOT
- Do not hard-depend on Finnhub / FMP / Trading Economics (all need keys → violates no-live-key-friendly). Leave a keyed adapter as an optional extra mirroring the `[yfinance]`/`alphavantage` extras pattern, default-OFF.
- Do not promise **ADP** coverage from the no-key tier — ADP National Employment Report is private (ADP Research Institute), in no free no-key source. Document it as keyed-only / out-of-scope for v0.

**The load-bearing rule (both timestamps, always):** every `CalendarEvent` needs `scheduled_for` (when it happens, forward payload that must be gated) AND `announced_at` (when that schedule first became public — the asof anchor, the analogue of `CatalystItem.published_at`). A parser **drops any row lacking either** — same discipline as ingest.py dropping items with no parseable pubDate. An event's **OUTCOME** (actual print, FOMC decision, reported EPS) is **never populated pre-event** (`outcome: None`).

---

## 3. RISK integration (rails-safe, ADDITIVE — NOT a sizing change)

**Posture:** event risk is a textbook *hard pre-trade control* (ESMA hard-block; FIA / NexusFi "risk layer independent of strategy"). It maps 1:1 onto hermes-quant's immutable REJECT/abstain-only gate. It **ADDS a reject condition; it never touches the `{0,±0.05,±0.10,±0.15,±0.20}` ladder, never amplifies, never flips a side.** `abs(adjusted) <= abs(target)` holds — identical posture to `admissibility/gate_order.py::admit_or_reject`. ADR-0004 stays immutable.

### Guard 1 — pre-event blackout (equity/crypto main path)
A **pure predicate** `in_event_blackout(symbol, asof, events, n_earnings, n_macro) -> reason|None` consumed as a SILENCE rule (same shape as the existing halt/cooldown/drawdown silences in gate.py). Fires only when:
- a **HIGH-tier** event for this symbol's market (own earnings; or US Tier-1 macro = FOMC/CPI/NFP/Core-PCE) falls within the window, **AND**
- the action would **OPEN or INCREASE** directional exposure (`abs(target)` increasing).

It does **NOT** block REDUCING/flattening trades — de-risking into an event must always be allowed. Windows: `N_earnings = 5 trading days` (config 3-7, default 5 to match the `covered_call days_since_earnings>=5` convention); `N_macro = 1 calendar day` (T-1 through T-0). Reason strings in the existing audit vocabulary, e.g. `event_blackout_earnings_T-3`, `event_blackout_fomc_T-1`.

### Guard 2 — options earnings-proximity (O8 in options_gate.py)
Add rule **O8** after O7. Fire **only** when (`candidate_net.theta < 0`, i.e. net-theta-PAYING) OR net-vega-LONG, **AND** (an earnings date falls within `N_earnings` days OR within the longest leg's DTE). Silence with reason `earnings_iv_crush_long_premium`. **Premium-selling buckets (CC/CSP/credit verticals — net-theta-collecting) are EXEMPT** — the literature (VolRadar, ApexVol; Dubinsky 2019 RFS; Alexiou et al. 2025) shows they *harvest* the crush; blocking them would over-reject the wheel strategies the options gate exists to enable. O8 is orthogonal to `min_dte_for_new_entry` (it keys off the earnings date *within the leg's DTE*, the case min-DTE misses).

### IV-awareness (earnings IV crush)
The guard is **sign-aware, not IV-magnitude-aware** by design. It does not read or predict an IV level (that would drift toward outcome-modeling). It keys off (a) the scheduled earnings DATE and (b) the candidate structure's net theta/vega *sign* — both known at `asof`, neither an outcome. This is the safe slice of the IV-crush literature.

### asof-honesty (non-negotiable)
- The guard uses ONLY the scheduled DATE, known-at `<= asof`. Store `scheduled_for` AND `announced_at`; honor only schedule revisions published `<= asof` (mirror the `EvidenceStore.available_at` check). Optionally register each event as an EvidenceStore row (`available_at = announced_at`) so `check_view_lookahead` enforces honesty universally with **zero gate change**.
- Two traps: (a) consuming an OUTCOME before `scheduled_for` = classic lookahead; (b) the subtler one — using an event's mere EXISTENCE before `announced_at`. Mitigation: require both timestamps; for sources with weak `announced_at` (yfinance forward), only ever WIDEN risk, never inform direction.

### Fail posture (explicit)
- Macro Tier-1 dates are deterministic (seed/BLS) → reliable, not subject to a data gap.
- Forward-earnings data missing (yfinance 404/None) → treat as **NO known event** (do NOT fabricate a blackout — fabricating one from missing data is itself non-asof-honest and over-rejecting). **Log it as a coverage gap.**

### Eval-gating
Ship behind `HERMES_QUANT_EVENT_RISK=0` (NullCalendar → everything ACCEPTED → byte-identical to today). Add a flag-off byte-identical test (mirror `tests/perception/test_convergence_flag_off_byte_identical.py`). Promote only via the existing promotion-gate harness, proving: (a) byte-identical when OFF; (b) when ON, only ever converts actions to abstain (`abs(adjusted) <= abs(target)`); (c) no event consumed before its `announced_at`; (d) non-degradation/improvement on event-adjacent trades. Start NARROW (earnings only, one tier, a handful of liquid symbols).

### Seam-coverage warning (carried from gate_order.py)
The admissibility seam is documented as wired ONLY at the autonomous tick — the daily brief, HITL `quant_approve`, and `PaperReactor.execute` are NOT admissibility-aware. The event-risk guard MUST be wired at ALL the same seams (reuse the single predicate from every caller) or it inherits the identical hole.

---

## 4. DELIBERATION answer

**Can the bull/bear/judge + risk-committee reason about scheduled-event risk if it is in `PerceptionFrame.extras` / `ctx.extras`? Yes — and the minimal rails-safe surface is a read-only `EventRisk` evidence field, with the deterministic guard as the independent backstop.**

### Surface
Add a single optional read-only key, e.g. `PerceptionFrame.extras["event_risk"]` (or a typed `event_risk` slot following the add-only `convergence`/`saturation` precedent in frame.py:38-40), populated ONLY when `HERMES_QUANT_EVENT_RISK=1`, carrying the **asof-honest, outcome-free** view of upcoming events:

```
event_risk = {
  "symbol": "AMZN",
  "decision_asof": "2026-05-31T...Z",
  "next_earnings": {"scheduled_for": "...", "announced_at": "...", "days_to": 3,
                    "precision": "date", "source": "yfinance_calendar"},
  "next_macro": [{"kind": "fomc", "scheduled_for": "...", "announced_at": "...",
                  "days_to": 1, "tier": "high"}],
  "in_blackout": true, "blackout_reason": "event_blackout_fomc_T-1"
}
```

It is filtered to `announced_at <= decision_asof` BEFORE it ever reaches the frame (the `known_events_asof` gate), and it carries `scheduled_for` / `days_to` / `tier` only — **never any outcome**.

### Who reads it and how
- **Analysts** (ClassicalTA/Kronos/Semantic) already `ctx.extras.get(...)` and ignore unknown keys (frame.py:28-29). They MAY read `event_risk` to *attenuate conviction* into a known event (e.g. semantic.py-style — widen the abstain band, lower confidence), exactly as they already read `regime` and `saturation`. This is OPTIONAL and additive.
- **Bull/bear/judge + risk-committee** (`deliberative.py`, `committee_runner.py`) MAY incorporate `event_risk` into rationale strings ("bear: FOMC in T-1, prefer flat into the print") — the debate is already structured to fold evidence into turns (deliberative.py:286-304, committee_runner.py:21-95). This makes the system's event-awareness *auditable and explainable*.

### Why a field AND a deterministic guard (not one or the other)
- A **pure deterministic guard alone** is safe but mute — the debate can't explain *why* it abstained, and analysts can't pre-emptively soften conviction (you'd see whipsaw: full-size proposal → hard reject).
- A **deliberation-only signal alone** is NOT safe — agents are non-deterministic and can be talked out of caution; an LLM bull can rationalize trading into earnings. The literature is unanimous that event risk must be a hard control independent of strategy logic.
- **The decisive design: both, with strict separation of authority.** The `EventRisk` field is *advisory* (shapes conviction, produces rationale). The deterministic `in_event_blackout` / O8 guard is the *authority* (can only reject/abstain) and **does not depend on any agent having read the field** — if every analyst and the whole committee ignore `event_risk`, the gate still silences a would-be open-into-FOMC trade. The field improves explainability and reduces whipsaw; the guard is the backstop that cannot be argued with.

**Verdict: NOT pure-deterministic-only. Both layers — advisory field + independent deterministic backstop — is strictly safer and more useful, and respects the immutable gate (the field carries no authority, the guard adds only a reject condition).**

---

## 5. ADR

A durable architectural decision is warranted (a new forward-event data type + a new gate reject condition + a new perception field). **ADR-0084 (proposed)** is drafted alongside this study: *"Scheduled-event calendar as asof-honest perception + a default-OFF pre-event REJECT/abstain guard."* MADR, status proposed, ≥2 options, negative consequences enumerated. See `docs/adr/ADR-0084-scheduled-event-calendar-and-pre-event-guard.md`.

---

## 6. Sources

See the research bundle (calendar-data-sources, event-risk-management) for full citations — FOMC/BLS/FRED/SEC primary sources, yfinance bug threads (#2552/#2566), ESMA algorithmic-trading supervisory briefing, FIA automated-trading risk controls, NexusFi event-driven automation, and the academic earnings-event-risk papers (Dubinsky/Johannes/Kaeck/Seeger 2019 RFS; Alexiou et al. 2025; Gao/Xing/Zhang).
