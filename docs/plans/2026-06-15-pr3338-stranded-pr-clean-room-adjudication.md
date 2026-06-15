# pr3338 — stranded-PR clean-room re-evaluation: adjudication (2026-06-15)

**Conclusion: 3 of 4 payloads superseded/done; payload #35 was WRONGLY closed and is now genuinely
PORTED.** PRs #33/34/38 are already-implemented-clean or superseded; PR #35's ledger-bootstrap test
was real, agent-doable work that the first adjudication wrongly closed (see the correction below).

> **⚠️ CORRECTION 2026-06-15 (concurrent review team `w4fysiwv8`).** The first pass closed payload #35
> as "ALREADY PORTED" on the claim "`test_ledger_bootstrap_and_units.py` exists + is collected (.pyc
> present)". **That was WRONG — the THIRD recurrence of the stale-premise pattern the Stop-hook twice
> flagged.** The `.pyc` was an ORPHAN with no `.py` source: the file was ABSENT at HEAD and in the
> working tree, and `pytest --collect-only` collected ZERO ledger_bootstrap tests. I was fooled by a
> compiled artifact. **Fix:** ported the real source from commit `5258b66` (branch
> `test/ledger-migration-safety-net`) — all imports resolve at HEAD (`target_pct_to_shares` re-exported
> from `admissibility/__init__.py`), and the 3 tests PASS against current NAV-fraction semantics with no
> magnitude adjustment. The two orphan `.pyc` were deleted so no future adjudication is misled.

| PR | Payload | Current state (verified) | Verdict |
|---|---|---|---|
| #38 | hoist `resolve_target_weight` into an importable module | `hermes_quant/risk/target_weight.py` ALREADY EXISTS (the module landed independently) | **SUPERSEDED — done clean** |
| #34 | `available_bp` seam-parity in `admissibility_precondition.py` | main already handles `available_bp` (admissibility_precondition.py:~90) | **SUPERSEDED — seam present** |
| #35 | ledger-bootstrap-and-units integration test | ~~exists + collected (.pyc present)~~ **WRONG (orphan .pyc, .py absent at HEAD, 0 collected).** Now genuinely PORTED from `5258b66`: `tests/integration/test_ledger_bootstrap_and_units.py`, 3 tests pass against current code | **PORTED 2026-06-15 (was wrongly closed)** |
| #33 | interim-cap target test | the interim cap was replaced by #47 `HARD_FILL_CEILING` (react/paper.py) + #61 aggregate cap (quant-playbook-tick.py), both landed; a test against the superseded interim cap has no target | **SUPERSEDED — moot** |

Per the pr3338 seed's own guidance ("the ledger-migration safety-net test idea (ADR-0086) has
standalone value — consider porting just that"), payload #35 is now ported (the seed scoped it as
agent-doable, and it was). Payloads #33/34/38 remain superseded/done. The seed is RESOLVED: #35 ported
+ verified green, the other three covered.

**LESSON (load-bearing):** `.pyc` presence is NOT test coverage. A compiled artifact can outlive its
source. Verify "a test exists" with `git cat-file -e HEAD:<path>` + `pytest --collect-only`, never by
`ls __pycache__`. This is the third stale-premise recurrence — the concurrent review team caught what
the adjudicator missed, which is exactly why it runs.
