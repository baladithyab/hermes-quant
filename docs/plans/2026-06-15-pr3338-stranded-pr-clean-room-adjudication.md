# pr3338 — stranded-PR clean-room re-evaluation: adjudication (2026-06-15)

**Conclusion: NOTHING TO REVIVE.** All 4 stranded payloads from closed PRs #33/34/35/38 are either
already-implemented-clean on the current branch or superseded by the cap rework that obsoleted them.
Verified against current code 2026-06-15.

| PR | Payload | Current state (verified) | Verdict |
|---|---|---|---|
| #38 | hoist `resolve_target_weight` into an importable module | `hermes_quant/risk/target_weight.py` ALREADY EXISTS (the module landed independently) | **SUPERSEDED — done clean** |
| #34 | `available_bp` seam-parity in `admissibility_precondition.py` | main already handles `available_bp` (admissibility_precondition.py:~90) | **SUPERSEDED — seam present** |
| #35 | ledger-bootstrap-and-units integration test | `tests/.../test_ledger_bootstrap_and_units.py` exists + is collected (.pyc present) | **ALREADY PORTED** |
| #33 | interim-cap target test | the interim cap was replaced by #47 `HARD_FILL_CEILING` (react/paper.py) + #61 aggregate cap (quant-playbook-tick.py), both landed; a test against the superseded interim cap has no target | **SUPERSEDED — moot** |

Per the pr3338 seed's own guidance ("reimplement clean-room against current paper.py rather than
reviving the stranded branches"), and given the above, there is no clean-room reimplementation owed:
the target-weight module exists, the admissibility seam exists, the ledger-units test exists, and the
interim-cap test's subject is superseded. The seed is RESOLVED by adjudication (the investigation
concludes the work is already covered / moot), not by reviving entangled branches.
