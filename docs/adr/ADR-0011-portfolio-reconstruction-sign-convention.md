# ADR-0011: Portfolio reconstruction sign convention

**Status:** Accepted (2026-05-13), target v0.1.2 implementation
**Supersedes:** none
**Amends:** ADR-0008 (settlement loop), ADR-0009 §P0-3 (broker-reality portfolio)

## Context

`portfolio_loader.reconstruct_portfolio` rebuilds positions, cash, realized
PnL, and fees from the append-only execution log so the daemon's view of the
book matches broker reality (per ADR-0009 §P0-3). It is the spine of equity
and drawdown computation, and feeds the calibrator's exit-fill join
(ADR-0003 calibration_quality lifecycle).

Phase-8 review of the v0.1.1 cut found two distinct sign-convention bugs in
the partial-close and direction-flip branches:

- **Claude P1**: partial close of a short position computed `closed_qty`
  with the wrong sign, leaving the residual position with corrupted average
  cost basis after the next add.
- **DeepSeek P0**: direction-flip realized PnL multiplied by the trade
  `side` ('buy' = +1 / 'sell' = -1) instead of by `sign(old_qty)`. For a
  long-to-short flip (sell more than long), this flipped the realized PnL
  sign — a profitable exit was logged as a loss and vice versa.

Because both bugs silently corrupt equity rather than failing loudly, v0.1.1
ships a `NotImplementedError` gate (see `hermes_quant/daemon/portfolio_loader.py:105-137`)
that refuses to process partial closes and direction flips at all. The error
message documents an operator workaround (one-position-per-pair, no
scale-out, no implicit flips). v0.1.2 must lift this gate with an explicit,
test-fenced rewrite.

This ADR pins the sign convention, the case enumeration, and the test fence
that gates the rewrite.

## Decision

`reconstruct_portfolio` dispatches on a strict four-case enumeration over
`(old_qty, signed_qty)`. Each case has a single canonical formula. Average
cost basis is used (not FIFO). Realized PnL uses `sign(old_qty)` — never
trade `side` — to determine the profit direction. The implementation is
gated by a frozen 11-test fence; the v0.1.1 `NotImplementedError` block is
removed in the same PR that lands the rewrite.

## The four canonical cases

Let `old_qty` be the signed position before the fill, `signed_qty = ±qty`
where the sign is +1 for buy / −1 for sell, and `new_qty = old_qty + signed_qty`.

| Case | Predicate | Action |
|------|-----------|--------|
| (a) OPEN / ADD | `old_qty == 0` OR `old_qty * signed_qty > 0` | Update averaged cost basis, no realized PnL |
| (b) PARTIAL CLOSE | `old_qty * signed_qty < 0` AND `abs(signed_qty) < abs(old_qty)` | Realize on `closed_qty = -signed_qty`, retain residual at unchanged `avg_old` |
| (c) FULL CLOSE | `old_qty * signed_qty < 0` AND `abs(new_qty) < 1e-12` | Realize on entire `old_qty`, position cleared |
| (d) DIRECTION FLIP | `old_qty * signed_qty < 0` AND `abs(signed_qty) > abs(old_qty)` | Realize on `closed_qty = -old_qty`, reopen at `fill` for `new_qty` |

Worked examples (units = contracts/shares; signs explicit):

| Case | old_qty | signed_qty | new_qty | closed_qty | Effect on avg cost |
|------|---------|------------|---------|------------|--------------------|
| (a) open long | 0 | +10 | +10 | — | new_avg = fill |
| (a) scale-in long | +10 @ 100 | +5 @ 110 | +15 | — | new_avg = (10·100 + 5·110)/15 = 103.33 |
| (a) scale-in short | −10 @ 100 | −5 @ 90 | −15 | — | new_avg = (−10·100 + −5·90)/(−15) = 96.67 |
| (b) partial close long | +10 @ 100 | −3 | +7 | +3 | avg unchanged at 100 |
| (b) partial close short | −10 @ 100 | +4 | −6 | +4 (magnitude) | avg unchanged at 100 |
| (c) full close long | +10 @ 100 | −10 | 0 | +10 | avg → 0, qty → 0 |
| (c) full close short | −10 @ 100 | +10 | 0 | +10 (magnitude) | avg → 0, qty → 0 |
| (d) flip long→short | +10 @ 100 | −15 | −5 | +10 (magnitude) | new_avg = fill (new short opened at fill) |
| (d) flip short→long | −10 @ 100 | +15 | +5 | +10 (magnitude) | new_avg = fill |

Dispatch in pseudo-code:

```python
EPS = 1e-12
old_qty = positions_qty[asset]
old_avg = (positions_cost[asset] / old_qty) if old_qty != 0 else 0.0
new_qty = old_qty + signed_qty

same_direction = (old_qty == 0) or (old_qty * signed_qty > 0)
opposite       = old_qty * signed_qty < 0
full_close     = opposite and abs(new_qty) < EPS
partial_close  = opposite and abs(signed_qty) < abs(old_qty) - EPS
flip           = opposite and abs(signed_qty) > abs(old_qty) + EPS

if same_direction:                                          # case (a)
    new_avg = (old_qty * old_avg + signed_qty * fill) / new_qty
    positions_qty[asset]  = new_qty
    positions_cost[asset] = new_qty * new_avg

elif partial_close:                                         # case (b)
    closed_qty = abs(signed_qty)                            # always positive
    realized   = (fill - old_avg) * closed_qty * sign(old_qty)
    realized_pnl_total   += realized
    positions_qty[asset]  = new_qty
    positions_cost[asset] = new_qty * old_avg               # avg unchanged

elif full_close:                                            # case (c)
    closed_qty = abs(old_qty)
    realized   = (fill - old_avg) * closed_qty * sign(old_qty)
    realized_pnl_total   += realized
    positions_qty[asset]  = 0.0
    positions_cost[asset] = 0.0

elif flip:                                                  # case (d)
    closed_qty = abs(old_qty)
    realized   = (fill - old_avg) * closed_qty * sign(old_qty)
    realized_pnl_total   += realized
    positions_qty[asset]  = new_qty                         # new direction
    positions_cost[asset] = new_qty * fill                  # reopened at fill

else:
    raise AssertionError(f"unreachable: old={old_qty} signed={signed_qty}")

cash -= signed_qty * fill + fees
realized_fees_total += fees
```

The four predicates are **mutually exclusive and exhaustive** modulo
`EPS`; the `else` branch is an unreachable assertion, not a silent fall-through.

## Realized PnL sign convention

The single canonical formula is:

```
realized = (fill - avg_old) * closed_qty * sign(old_qty)
```

where `closed_qty` is a **positive magnitude** and `sign(old_qty)` is the
direction of the position being closed (not the trade side).

Worked examples:

| Direction | old_qty | avg_old | side | fill | closed_qty | realized | Profitable when |
|-----------|---------|---------|------|------|------------|----------|-----------------|
| LONG closed | +10 | 100 | sell | 110 | 10 | (110−100)·10·(+1) = **+100** | fill > avg_old |
| LONG closed at loss | +10 | 100 | sell | 90 | 10 | (90−100)·10·(+1) = **−100** | — |
| SHORT closed | −10 | 100 | buy | 90 | 10 | (90−100)·10·(−1) = **+100** | fill < avg_old |
| SHORT closed at loss | −10 | 100 | buy | 110 | 10 | (110−100)·10·(−1) = **−100** | — |
| FLIP long→short | +10 | 100 | sell @ 110, qty 15 | 110 | 10 | (110−100)·10·(+1) = **+100** | + new short opened at 110 |

The DeepSeek P0 bug used `sign(side)` — i.e. +1 for buy, −1 for sell — in
place of `sign(old_qty)`. For a long-to-short flip (`side='sell'`), this
multiplied a profitable close by −1 and logged a profitable trade as a
loss of equal magnitude. The two coincide for the simple long-close /
short-cover paths but diverge precisely on flips, which is why the unit
tests for cases (b)–(d) are non-negotiable.

## Average cost basis vs FIFO

v0.1.2 pins **average cost basis**:

- The same-direction branch already uses average cost in v0.1.1, so the
  partial-close and flip branches must match to preserve a single source
  of truth for `avg_old`.
- Average cost matches freqtrade's default profit accounting, which is
  what the settlement loop reconciles against (ADR-0008).
- Partial closes leave `avg_old` unchanged on the residual, which is the
  intuitive and documentable behavior.

**FIFO / lot accounting is explicitly out of scope for v0.1.x.** It is
deferred to v0.2 if and only if a tax-lot reporting requirement materializes
(e.g. wash-sale tracking for a US taxable account). A v0.2 ADR amendment is
required before any FIFO code lands; do not bolt lot tracking onto the
average-cost dispatch.

## Test fence

The v0.1.2 PR MUST NOT merge until all 11 of the following tests are green
in `tests/unit/daemon/test_portfolio_loader_reconstruction.py`:

| # | Test | Case | What it pins |
|---|------|------|--------------|
| 1 | `test_open_long` | (a) | buy from flat → qty=+q, avg=fill, realized=0 |
| 2 | `test_open_short` | (a) | sell from flat → qty=−q, avg=fill, realized=0 |
| 3 | `test_scale_in_long` | (a) | weighted avg formula on long add |
| 4 | `test_scale_in_short` | (a) | weighted avg formula on short add |
| 5 | `test_partial_close_long` | (b) | residual qty + avg unchanged + realized sign |
| 6 | `test_partial_close_short` | (b) | mirror of #5 |
| 7 | `test_full_close_long` | (c) | qty=0, cost=0, realized > 0 on profitable exit |
| 8 | `test_full_close_short_with_loss` | (c) | qty=0, cost=0, realized < 0 when fill > avg_old |
| 9 | `test_direction_flip_long_to_short` | (d) | realized on closed leg + new short avg = fill |
| 10 | `test_direction_flip_short_to_long` | (d) | mirror of #9 |
| 11 | `test_long_to_flat_to_short_separate_executions` | (c)+(a) | two exec records that pass through zero are NOT a flip — they dispatch as (c) then (a), realized PnL is on the close leg only |

Test #11 is the canary that prevents collapsing the (c)+(a) sequence into
case (d) optimization later. Cases (c) and (d) are distinct because (d)
realizes and reopens in a single execution, while (c)+(a) realizes,
zeroes, then opens — the cash and fees ledger entries differ.

Each test asserts the full triple `(positions_qty, positions_cost,
realized_pnl_total)` after the fill, not just one field.

## Cross-cuts

- **ADR-0008** (settlement loop): the loop reads exec records and calls
  `reconstruct_portfolio` to derive equity and drawdown. Sign bugs here
  silently feed the drawdown circuit breaker bad numbers, which can either
  trip prematurely (lost opportunity) or fail to trip (lost capital).
- **ADR-0009 §P0-3** (broker-reality portfolio): the daemon's portfolio
  must equal what `reconstruct_portfolio` computes from the exec log. Any
  divergence is a `quant_doctor` red flag.
- **ADR-0003** (calibration_quality lifecycle): the calibrator's gate to
  lift from `slippage_only` to `horizon_return` requires entry+exit fill
  joining. Case (c) FULL CLOSE is the canonical join point — exit fill is
  matched to the open's entry fill at the moment the position clears. The
  flip case (d) joins on the closed leg only; the reopened leg starts a
  new calibration window.

## Migration from v0.1.1 NotImplementedError gate

The v0.1.1 gate at `hermes_quant/daemon/portfolio_loader.py:105-137` raises
`NotImplementedError` for any fill that is neither pure same-direction nor
exact full close. The error message points operators at the workaround
(one-position-per-pair, no scale-out, no implicit flips). This is a
deliberately loud failure: silence-by-default + money-software discipline
demand a refused fill over a corrupted ledger.

Migration order for the v0.1.2 PR:

1. Land the new dispatch in `reconstruct_portfolio` **with the
   `NotImplementedError` gate still in place above it.** The gate is
   unreachable but provides a safety net during code review.
2. Add all 11 tests in
   `tests/unit/daemon/test_portfolio_loader_reconstruction.py`. CI must be
   green.
3. Remove the gate (lines 105–129 of v0.1.1) in a **separate commit** on
   the same PR titled `refactor(portfolio): remove v0.1.1 NotImplementedError gate`.
   The diff for that commit must be exactly the gate block + its docstring
   markers — nothing else.
4. Update `CHANGELOG.md` under v0.1.2: "Lifted partial-close and direction-flip
   gate; see ADR-0011."
5. Run a full settlement-loop replay on the existing v0.1.1 fixture exec
   logs and confirm equity curves match the freqtrade-side reconciliation
   to within fee precision.

Rollback: if step 5 fails, revert the gate-removal commit only. The new
dispatch logic stays (it's strictly more capable than the old code) but
the gate re-engages until the discrepancy is diagnosed.

## Provenance

- Phase-8 synthesis: `docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md` §P1-α
  - Reviewer attribution: Claude (P1, partial-close residual avg cost) +
    DeepSeek (P0, flip realized-PnL sign via `side` instead of `sign(old_qty)`)
- v0.1.1 gate code: `hermes_quant/daemon/portfolio_loader.py:105-137`
- Target: v0.1.2 implementation + test fence merged together; gate removed
  in same PR on a separate commit.
