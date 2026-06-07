# Post-mortem: $4,247 paper loss on ASTS (2026-06-07)

**Status:** Blameless post-mortem. Paper capital only. No code bug caused the
loss directly — but the deep review (same day) found the *class* of latent bug
that could cause a real one. Both documented here.

## What happened

Two ASTS long positions were flattened on 2026-06-07 during a post-restart
validation session:

| Position | Source | Entry | Exit | Return | Realized |
|---|---|---|---|---|---|
| June-4 advisor (HITL) | `prop_20260603T193506_ASTS_371e27` | $118.17 | $93.44 | −20.93% | **−$4,186** |
| Jun-7 autonomous test | `prop_20260607T174333_ASTS_a37e65` | $93.72 | $93.44 | −0.30% | −$61 |
| | | | | **TOTAL** | **−$4,247** |

The $61 was a deliberate plumbing-test fire (Sunday, off Friday's bar). The
$4,186 is the real story.

## Why the June-4 ASTS trade lost (root cause)

The trade was a **known-weak, internally-contradictory signal fired at full
size with no stop**. From the advisor record:

1. **The analyst panel disagreed; the BMA hid it.** Four analysts:
   - classical-ta: +1 long, weak (raw 0.47, only 2/4 sub-signals)
   - microstructure_lite: +1 long, very weak (raw 0.33, 1 sub-signal)
   - **kronos: −1 SHORT, HIGHEST conviction (raw 0.85, 25/30 paths agreed)**
   - hermes_semantic: +1 long (raw 0.895)

   The BMA used equal 0.5 weights and outvoted the single highest-conviction
   analyst (Kronos, short) 3:1, emitting `confidence=0.688, direction=+1`.

2. **The bullish semantic signal was sector contagion, not ASTS news.** The
   only catalyst was *"Blue Origin seeks to resume New Glenn launches"* —
   propagated to ASTS via a `sector_member` graph edge. A competitor's launch
   news read as bullish for ASTS. Thin evidence weighted as a primary voice.

3. **The risk committee tried to stop it and was overruled.** The conservative
   persona voted **SILENCE** (0.85): *"There is no stop_loss on this proposal.
   We are not in the business of taking unbounded losses."* The committee only
   reduced size 0.5×. The trade fired at **20% NAV with `stop_loss: None`** —
   so when ASTS fell $118→$93 there was no invalidation level to cut it.

4. **Edge was thin from the start:** `edge=0.0114` (1.1%) drove a Kelly 0.20
   position.

## Contributing latent defects (from the same-day deep review)

The deep review (`synthesis.md`) found the BMA over-confidence mechanism is
real and unguarded for correlated/unanimous voices (`bma.py:1049`), and a
**NaN-fail-open defect class** across 5 sites that could let unknowable account
state bypass the risk gate entirely. Those NaN sites were FIXED on branch
`fix/nan-fail-open-and-signal-guards` (this commit). The BMA-confidence and
no-stop-loss issues are NOT yet fixed — see Action items.

## Action items

| # | Action | Owner | Status |
|---|---|---|---|
| 1 | Fix 5 NaN-fail-open sites (protocol/slippage/silence_bias/oracle) | done | ✅ `f22b6b1` |
| 2 | **Block full-size fires with `stop_loss: None`** in autonomous/HITL path | done | ✅ `660d853` (root-cause trader pct-stop fallback + flag-gated tick backstop) |
| 3 | **BMA: down-weight correlated same-direction voices; cap unanimity confidence** (`bma.py:1049`) | done | ✅ `c3db1b3` (dissent-aware confidence cap, `HERMES_QUANT_DISSENT_CAP`) |
| 4 | **Confidence-weight the BMA, don't equal-weight** — a 0.85-conviction contrarian shouldn't be averaged into noise | open | P2 — deferred (higher-blast-radius; the dissent-cap addresses the confidence symptom; conviction-weighting would change *direction* selection and wants its own backtest) |
| 5 | Semantic propagation: discount `sector_member`-only catalysts vs direct-ticker news | open | P2 |
| 6 | Enforce `max_concurrent_positions` (currently dead) | done | ✅ `dea6d27` |
| 7 | Wire `kill_switch_pct` cumulative-PnL live (currently only honors pre-tripped file) | done | ✅ `dea6d27` |

## Lesson

The loss wasn't a bug — it was the system doing exactly what its (flawed)
aggregation + sizing rules told it to, over the explicit objection of its own
risk committee. The fix is not "don't trade ASTS" — it's **don't let an
equal-weighted vote bury a high-conviction contrarian, and never fire at full
size without a stop.**
