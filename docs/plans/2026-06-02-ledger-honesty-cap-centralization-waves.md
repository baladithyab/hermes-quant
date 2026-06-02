# Wave plan — post-incident ledger-honesty + cap-centralization (ship-now scope)

ADRs: ADR-0086 (Phase 1 only — read-time MTM, no schema change) + ADR-0087 (centralize cap at reactor seam).
Baseline commit: 5745ad0. Repo: /mnt/e/CS/github/hermes-quant (editable-installed into ~/.hermes/hermes-agent/venv).
Scope DEFERRED out of this plan: ADR-0086 Phase 2 (full share-quantity migration) — separate arc, gated by the pre-mortem.

## Units reality (the constraint every wave obeys)

`state.db.positions.quantity` is a SIGNED NAV-FRACTION (e.g. -0.2 = 20% short), NOT shares.
`avg_entry_price` is the entry mark. So marked equity in the CURRENT (fraction) model is:

```
unrealized_i = weight_i * NAV_ref * (mark_i / entry_i - 1)     # weight carries sign
marked_equity = cost_basis_equity + Σ unrealized_i
```

where `NAV_ref` is the equity basis the weights were sized against (the account's nominal NAV,
default _default_initial_cash() = $100k unless a better basis is available). This is EXACTLY the
formula that produced the verified −$30k incident number, so it is the regression lock.

## Waves

### Wave 1 — `get_marked_equity` read API + cost-basis cash/equity coherence  (owner: subagent A)

Files OWNED (no other wave writes these):
- `hermes_quant/state/portfolio_state.py`
- `tests/unit/test_portfolio_state_accounting.py` (NEW)

Changes:
1. Add `PortfolioState.get_marked_equity(account_id, mark_prices: dict[str,float], *, nav_ref: float | None = None) -> MarkedEquity`.
   - Reads positions + cash (existing get_positions/get_cash).
   - `nav_ref` defaults to `cash.equity_total if cash else _default_initial_cash()`.
   - For each position: `mark = mark_prices.get(symbol)`; if absent → fall back to `avg_entry_price` and set a per-result `equity_basis="entry|mark|mixed"` flag.
   - `unrealized_i = weight_i * nav_ref * (mark/entry - 1)` (signed; shorts profit when mark<entry).
   - Returns a small frozen dataclass `MarkedEquity(account_id, cost_basis_equity, marked_equity, total_unrealized, equity_basis, n_positions, n_marked)`.
   - **No network call** — marks are injected.
2. Do NOT change the firing/cap path or executions schema. The existing `equity_total` write stays (cost-basis); the new API is read-time MTM layered on top.
   (Optional, low-risk: fix the `delta_cash` comment to stop claiming dollar semantics it doesn't have — doc-only, no behavior change, to avoid misleading the Phase-2 implementer.)

Acceptance tests (ADR-0086 Phase-1 gate):
- `test_marked_equity_signed_mtm` — mixed long/short book + incident marks → marked_equity ≈ cost_basis − $30,657 (±$50). REGRESSION LOCK.
- `test_marked_equity_short_reduces_equity` — adverse mark on a short reduces marked_equity.
- `test_marked_equity_falls_back_when_mark_absent` — missing mark → equity_basis != "mark".
- `test_get_marked_equity_no_network` — monkeypatch socket to raise; call still succeeds.

### Wave 2 — centralize portfolio cap at PaperReactor.execute()  (owner: subagent B)

Files OWNED:
- `hermes_quant/react/paper.py`
- `tests/unit/test_paper_reactor_cap.py` (NEW)

Changes:
1. In `PaperReactor.execute()`, after the existing ADR-0077/0079 admissibility precondition and BEFORE appending the fill, when `os.environ.get("HERMES_QUANT_PORTFOLIO_CAPS")=="1"`:
   - Reconstruct current book weights from `state.db` (read `get_positions(account)` → {symbol: weight}).
   - Build `PortfolioState`(risk) + `PortfolioCaps.standard()`; call `clip_one_to_remaining_headroom(symbol, fill_size_pct, state, caps)`.
   - clipped→0 or not fired: return an ExecutionRecord with `reactor_metadata.silenced=True, silence_reason="portfolio_cap_<reason>"`; do NOT append a position-moving fill.
   - scaled: execute at clipped `fill_size_pct`; record `reactor_metadata.cap_scaled_from/to`.
2. **Flag DEFAULT-OFF.** With `HERMES_QUANT_PORTFOLIO_CAPS` unset, `execute()` is byte-identical to today (no extra state read, no clip).
3. Do NOT remove the per-layer clips yet (autonomous in-package, advisor deployed script) — that's Wave 3, sequenced to avoid double-clip while the flag is OFF this session.

Acceptance tests (ADR-0087 gate):
- `test_flag_off_is_bit_identical` — flag unset → no clip path taken (assert clip fn not called).
- `test_cap_silences_over_gross` — flag on, book at 200% gross → silenced record, no fill appended.
- `test_cap_scales_partial_headroom` — partial fit → scaled, cap_scaled_to recorded.

### Wave 3 — reporting integration + per-layer clip removal note  (owner: subagent C, AFTER waves 1+2 land)

Files OWNED:
- deployed `~/.hermes/scripts/quant-portfolio-daily.py` (reporting reads get_marked_equity)
- deployed `~/.hermes/scripts/quant-daily-interim.py` (remove the 2026-06-02 hot-patch per-layer clip ONLY IF Wave-2 seam flag is to be turned on; otherwise leave + document)
- `hermes_quant/autonomous.py` (same — document, don't remove unless flag flips)

Decision for THIS session: keep `HERMES_QUANT_PORTFOLIO_CAPS` semantics as-is (advisor cron sets it → advisor clips per-layer). Wave 2 adds the seam clip default-OFF so it does NOT double-clip. Turning the flag into "reactor-seam owns it, per-layer clips removed" is a flag-flip + clip-removal that lands in the DEFERRED arc with the share migration, because the cap reads weights and Phase-2 changes weight units. So Wave 3 is: wire reporting to get_marked_equity (real value now) + leave a `# TODO(ADR-0087 Wave-final)` marker where the per-layer clips will be removed.

## Concurrency / file-ownership

Waves 1 and 2 touch disjoint files → parallelizable. Wave 3 is sequential (depends on 1+2 + needs deployed-script edits). Reviewers run concurrent with execution (Phase 7).

## Rollback

All three waves are additive + default-OFF. Rollback = `git revert` the wave commit; the cap seam is dormant until the env flag is set; get_marked_equity is a new read API with no writer dependency.
