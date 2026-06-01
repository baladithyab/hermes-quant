# tests/fixtures/catalyst_onboarding — ADR-0075 admission-precision eval set (B05)

Committed, offline, deterministic eval input for the **admission-precision** axis
(`hermes_quant.catalyst.eval.run_admission_precision`). **Versioned fixture, NEVER `/tmp`**
so the unit test runs with no network and no live price fetch.

## Why this axis exists (and how it differs from directional precision)

`run_precision` measures whether a synthesized PACKET's stance matches the realized move.
That is necessary but it is **not** the question ADR-0075 onboarding raises. Onboarding only
ever TRADES the names it **admits** — out-of-universe, fresh, `confidence >= TAU_CONF (0.60)`,
`magnitude >= TAU_MAG (0.04)`, non-neutral, broker-tradeable, capped at 3. The gate-relevant
precision is therefore **conditional on admission**: *of the names catalyst onboarding would
actually admit, what fraction moved in the packet's stance direction?*

`run_admission_precision` replays that exact admission gate (same `TAU_CONF`/`TAU_MAG`,
out-of-universe, fail-closed tradeability, non-neutral) **deterministically from the episode
fields** — it does not synthesize or touch the network. Only **admitted** episodes are scored;
non-admitted ("benign") episodes are excluded from the denominator so they cannot inflate
precision, while an admitted-but-wrong episode IS a miss and drags the rate down.

## Files

### `admission_episodes.v1.json`

Curated catalyst-admission episodes. Each carries the fields the admission gate reads
(`symbol, stance, confidence, magnitude, in_universe, tradeable, horizon`) plus the
**REAL captured forward return** (`realized_forward_return`, signed % over the horizon,
captured ONCE offline and committed). The space-launch basket (LUNR/RKLB/ASTS) reuses the
documented Blue-Origin returns from `test_catalyst.py::test_precision_blue_origin_case`.

| episode | admitted? | scored? | why |
|---|---|---|---|
| LUNR / RKLB / ASTS (bearish, Blue-Origin) | yes | HIT | ADR-0075 seed: out-of-universe space reactors, down moves match bearish |
| LCID (bullish) | yes | HIT | out-of-universe EV reactor, up move matches bullish |
| SPR (bullish) | yes | **MISS** | documented false positive — bullish admit that faded; keeps the set honest |
| PLUG | no | — | NEG-CONTROL: `confidence 0.55 < TAU_CONF`; wrong-direction, would tank rate if leaked |
| JOBY | no | — | NEG-CONTROL: `magnitude 0.02 < TAU_MAG`; correct-direction, would inflate rate if leaked |
| AMD | no | — | NEG-CONTROL: `in_universe` (transient screen artifact, already recommended) |
| SOFI | no | — | NEG-CONTROL: tradeability fail-closed; correct-direction, would inflate rate if leaked |
| RIVN | no | — | NEG-CONTROL: neutral stance, no tradeable direction |

Result over this set: **5 admitted, 5 scored, 4 hits → hit_rate 0.80 ≥ 0.60 bar (PASS)**,
with the SPR miss in the denominator. The negative-control episodes are split deliberately:
two are directionally CORRECT (JOBY, SOFI) so a leak would *inflate* precision, two are
directionally WRONG (PLUG, AMD) so a leak would *tank* it — together they prove the gate's
admission filter is what determines the scored set, not the labels.

## Gate linkage (the point of the axis)

This is the eval **GATE** for the `HERMES_QUANT_CATALYST_ONBOARDING` flip. Per ADR-0075
*Verification* and *Implementation status*: the ADR stays **Proposed** and the flag stays
**OFF** until `run_admission_precision` over a curated admission-episode set (this fixture,
seeded by the real LUNR Blue-Origin move) is **green** at the stated bar. The **flag-flip is
an operator action** — this code and fixture only *measure* whether the bar is cleared. They
never flip the flag and never touch the ADR-0004 risk gate / sizing ladder / kill switch
(admission is perception-layer admissibility; the deterministic gate remains final authority).
