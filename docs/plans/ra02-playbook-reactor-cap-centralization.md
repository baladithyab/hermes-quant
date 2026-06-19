# ra02 — Playbook fire-path → reactor cap centralization (scoped recommendation)

Date: 2026-06-15 · Lane: `inc/ra02-playbook` (half-2 of ra02) · Status: RECOMMENDATION (no behavior shipped)

Related: ADR-0087 (centralize cap at reactor seam), ADR-0029 (reactor dispatch),
`docs/plans/2026-06-02-ledger-honesty-cap-centralization-waves.md` (covers the reactor-seam
cap; does NOT cover the playbook raw-POST path — THIS doc extends it).

> hermes-quant is MONEY-SOFTWARE. ra02 targets the "final authority" portfolio cap being
> PATH-DEPENDENT — the 2026-06-02 41.6x-gross incident mechanism class. Half-1
> (`autonomous._react -> select_reactor`) landed byte-identical. This doc covers half-2: the
> playbook-tick FIRE path, which does NOT route through the reactor and therefore does NOT
> share the reactor's cap accumulator. **Conclusion: there is no safe additive byte-identical
> slice that advances the goal; the cutover is inherently behavioral. This is the design +
> gate + migration order to do it safely later.**

## 1. The problem ra02 half-2 targets

The portfolio gross cap is the "final authority," but it is enforced by TWO independent
in-tick accumulators that do not observe each other within a firing window:

- **Reactor path** (autonomous + HITL approve): `react/paper.py::PaperReactor.execute(proposal,
  fill_size_pct=...)` clips against a per-fire re-read of the book
  (`_portfolio_cap_clip`, `react/paper.py:652`), in **NAV-fraction** units, behind
  `HERMES_QUANT_PORTFOLIO_CAPS`. It writes `executions.jsonl` + `state.db`.
- **Playbook path** (`ops/scripts/quant-playbook-tick.py`): fires a raw USD-notional
  market order directly to Alpaca `/orders` (`:248`, via `place_paper_market_order` `:225`),
  gated by its OWN tick-local **USD** accumulator `AggregateTickBudget` (`:359`) behind
  `HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP`. It writes NO bus and NO `state.db`.

Because the playbook never writes the bus, the reactor's per-fire book-read can never see a
same-tick playbook fire (and vice-versa). Two armed crons firing in the same window each see
the same pre-fire headroom and both pass the cap — the path-dependence / cross-writer
non-atomicity that produced the 41.6x-gross class. The reactor side has a partial guard
(`HERMES_QUANT_ACCOUNT_LOCK`, `react/paper.py:323`, default-OFF) but it cannot serialize the
playbook because the playbook holds no lock and writes no bus.

## 2. What is ALREADY shared (do NOT re-fold)

The cap **math** is already centralized at the correct layer. Both paths derive their gross
cap from the identical canonical source:

```
hermes_quant/risk/portfolio_normalize.py
  headroom_summary(PortfolioState, PortfolioCaps.standard())["gross_headroom"]
    = caps.max_gross_exposure_pct - Σ|Position.quantity|        (:148)
```

- Playbook: `build_aggregate_tick_budget` reads exactly this (`:469-471`), then denominates
  the ceiling in USD as `equity_usd * gross_headroom` (`:484`) and seeds the consumed side
  with `equity_usd * Σ|Position.quantity|` (`read_real_open_positions_gross_usd:350-351`).
- Reactor: `_portfolio_cap_clip` builds `RiskPortfolioState(pos_map)` from the same book and
  calls `clip_one_to_remaining_headroom(...)` against the same `PortfolioCaps.standard()`
  (`react/paper.py:694-753`).

There is no second, divergent cap formula to fold. ADR-0087's "centralize the cap math" goal
is substantially met. A shared helper that merely re-wraps `headroom_summary` would add
indirection on a live money fire-path without changing behavior — explicitly NOT recommended.

## 3. What is NOT shared (the residual, and why it is behavioral)

The unshared piece is the in-tick ACCUMULATOR and its STATE MODEL:

| Axis | Playbook fire (raw POST) | Reactor `execute()` |
|---|---|---|
| Size unit | USD notional | signed NAV-fraction `fill_size_pct` |
| Order mechanism | direct Alpaca `/orders` POST (`:248`) | bus append + `state.db` (synthetic) or backend |
| Cap unit | USD (`equity*gross_headroom`) | NAV-fraction (`clip_one_to_remaining_headroom`) |
| In-tick state | tick-local `consumed_usd` accumulator | per-fire re-read of `state.db` book |
| Bus / state.db writes | NONE | YES |
| Input object | none (symbol + notional) | `Proposal` |

Making the two caps mutually aware requires EITHER the playbook to start writing the bus (so
the reactor's book-read observes it) OR a new durable in-tick reservation store both consult.
Both are behavioral changes to a live cron. Neither is additive/byte-identical. Therefore no
safe slice ships this lane; the work below is the recommended (gated) cutover.

## 4. Recommended cutover: `HERMES_QUANT_PLAYBOOK_VIA_REACTOR` (default-OFF route flag)

Route the playbook FIRE through `react.dispatch.select_reactor + reactor.execute(...)` when
the flag is ON; raw POST when OFF (byte-identical to today). Mirrors the half-1 dispatch
pattern. This is a 4-change COUPLED cutover — sequence it, do not land it as one diff:

1. **USD → NAV-fraction conversion.** `fill_size_pct = notional_usd / equity_usd`, with the
   SAME fail-closed equity read the aggregate cap uses (`read_alpaca_account_equity`, `:270`,
   returns `None` on any uncertainty → silence). Guard the dimensional trap documented in
   `read_real_open_positions_gross_usd` (`:316-331`): `Position.quantity` is a NAV-fraction,
   never a share count. A unit error here re-introduces the cap2 under-count.
2. **`Proposal` synthesis.** Build an equity `Proposal` (proposal_id, symbol, asset_class,
   decision_price, signal_id, advisor_result, play_tag="playbook") from the symbol + advisor
   result the playbook already has. Reuse `_extract_decision_price` semantics so the reactor's
   `cr05` zero-price reject (`react/paper.py:208`) sees a real price.
3. **Bus + state.db writes BEGIN.** When ON, `execute()` writes `executions.jsonl` and calls
   `apply_execution` (`react/paper.py:519`). This is the NEW durable side-effect — it is what
   finally lets the reactor cap and (after this cutover) any other writer observe playbook
   fires in the same book. It MUST be paper-soaked before trust.
4. **Cap unit flips USD → NAV-fraction.** When ON, the reactor's `_portfolio_cap_clip`
   (`HERMES_QUANT_PORTFOLIO_CAPS`) becomes the authority; the playbook's `AggregateTickBudget`
   is bypassed on the ON path. The two flags must be set together for the ON path to be capped.

OFF path (flag unset): the `kelly_to_notional → AggregateTickBudget.check →
place_paper_market_order → record_placed` sequence is bit-for-bit today. This is the
byte-identical invariant the parity grid enforces.

### Closing the cross-writer race (the actual ra02 goal)

Routing the playbook through the reactor is NECESSARY but not SUFFICIENT to close the
path-dependence. Even ON, the reactor's per-fire book re-read is still TOCTOU across crons.
The durable fix is a shared in-tick reservation: extend `HERMES_QUANT_ACCOUNT_LOCK`
(`react/paper.py:323`) to span the playbook's fire once it routes through `execute()`, so the
account-outer lock serializes the cap-read→fire→state.db window across BOTH the autonomous and
playbook crons. This is only reachable AFTER step 3 (the playbook writes the bus under the
reactor) — which is why the route flag is the prerequisite, not the fix on its own.

## 5. Parity-grid acceptance gate

The cutover is accepted only when BOTH hold:

- **OFF byte-identical.** Re-run the existing `tests/unit/test_playbook_aggregate_cap.py`
  suite (16 cases incl. cap2 NAV-fraction dimensional tests) with `VIA_REACTOR` unset — every
  assertion unchanged (fires/silenced counts, journal rows, `aggregate_cap_*` fields,
  fail-closed on bad equity/notional/unreadable book). `test_flag_unset_is_byte_identical_to_
  explicit_off` is the keystone.
- **ON cap-equivalent.** A new grid re-expresses each USD scenario in NAV-fraction terms and
  asserts the reactor cap admits/silences the SAME set of fires the USD cap would, across:
  empty book, partial-headroom book (`test_flag_on_real_book_partial_headroom_admits_then_binds`),
  over-cap book (`test_flag_on_counts_real_open_book_against_ceiling`), unreadable book
  (fail-closed), non-finite equity/notional. Plus the cross-writer case: an autonomous fire +
  a playbook fire in the same tick must, under `HERMES_QUANT_ACCOUNT_LOCK=1`, consume one
  shared headroom (the loser silenced) — the regression the whole lane exists to prevent.

## 6. Migration order (each step gated, default-OFF until the prior soaks)

1. **Land `VIA_REACTOR` default-OFF + parity grid** (steps 1-4 above, flag unset == today).
   Gate: OFF byte-identical grid green + ruff clean.
2. **Dry-run parity soak.** Run the playbook `--dry-run` with `VIA_REACTOR=1` against a
   recorded tick; diff the synthesized `Proposal` + would-be `fill_size_pct` against the raw
   notional. Gate: USD→NAV conversion matches `notional/equity` to tolerance, no Proposal
   field missing.
3. **Flag-ON paper soak.** Enable `VIA_REACTOR=1 + HERMES_QUANT_PORTFOLIO_CAPS=1` on the PAPER
   account for ≥1 week. Gate: bus + state.db writes correct, no cap breach, reconciliation
   against Alpaca paper positions clean.
4. **Extend account-lock across the playbook + close the race.** Gate: cross-writer grid case
   green; the 41.6x-class mechanism is provably unreachable with both crons armed.
5. **Cutover.** Default `VIA_REACTOR` ON; deprecate the raw-POST + `AggregateTickBudget` path.
   Gate: operator sign-off (money seam).

## 7. Out of scope for the ra02 lane (explicitly NOT done here)

- No edit to `ops/scripts/quant-playbook-tick.py`, `react/dispatch.py`, `react/paper.py`, or
  any test. The existing playbook/cap tests stay green because nothing was touched.
- No new flag wired, no order mechanism change, no bus writes from the playbook, no commit.
- This doc is the deliverable. Implementation is the gated sequence above, in a later lane.
