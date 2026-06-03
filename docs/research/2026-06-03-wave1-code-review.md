# Wave-1 Code Review — hermes-quant (advisor-cap target-weight + admissibility seam parity)

Reviewer: subagent (review-only, no code modified). Repo: `/mnt/e/CS/github/hermes-quant`.

## TL;DR / P0 banner

**No P0 (release-blocking) defects found in either commit.** Both fixes are correct,
fail-closed, well-tested, and (importantly) gated behind existing env flags so neither
changes default behavior. There is **one cross-cutting P1 process finding**: the two
commits live on **divergent branches**, not a stack — `a524843` is NOT an ancestor of
`HEAD` (`5cf5ff2`); their merge-base is `ae8cbea`. They must be merged independently and
will need conflict-free integration verification together.

| Commit | Branch | Verdict |
|---|---|---|
| 1 — `a524843` advisor-cap target-weight | `fix/advisor-cap-target-weight-resolution` | **APPROVE-WITH-NITS** |
| 2 — `5cf5ff2` seam BP parity | `fix/seam-divergence-available-bp-parity` | **APPROVE** |

### Test evidence (all green)
- Commit 1 (run in a `git worktree` at `a524843`, since the file is absent from current HEAD):
  `tests/ops/test_quant_daily_interim_cap_target.py` → **9 passed**.
- Commit 2 (current HEAD): `tests/unit/test_admissibility_account_context_unified.py` +
  `tests/unit/test_paper_reactor_cap.py` → **12 passed**.
- Regression baseline: `tests/unit/test_portfolio_normalize.py` → **24 passed** (unchanged cap math).

---

## COMMIT 1 — `a524843` advisor-cap target-weight resolution — APPROVE-WITH-NITS

Fixes a real, severe bug: the advisor cap read `actionable["target_position_pct"]` — a
key the actionable-builder never sets — so every fire silenced as `zero_target`
(advisor fired 0 trades ~24h while reporting healthy). The new `_resolve_target_weight()`
resolves the signed weight from fields that actually exist, and a loud
`size_field_missing` guard prevents a future plumbing break from being laundered into a
benign cap silence. The fix is sound.

### P0
None.

### P1
None.

### P2 (nits / hardening)

- **P2-1 — Explicit `target_position_pct == 0.0` short-circuits to a genuine zero, bypassing the missing-field guard.**
  `ops/scripts/quant-daily-interim.py` `_resolve_target_weight()`, the
  `if explicit is not None:` branch (≈ line 297). The task flagged this as a potential
  footgun. **Assessment: NOT a footgun in current usage, but worth a one-line note.**
  Reasoning: an explicit `0.0` returns `(0.0, "explicit")` — a *non-None* source — so the
  caller does NOT raise `size_field_missing`; instead it flows to
  `clip_one_to_remaining_headroom`, which silences it as `zero_target`. That is the
  *correct* semantic distinction: a caller who *explicitly* sets `0.0` is genuinely asking
  for a zero-size order (legitimately benign), whereas a *missing* field (`None` source) is
  a plumbing break (loud). The guard logic correctly distinguishes the two. The only nit:
  the actionable-builder doesn't currently set this key at all, so this branch is presently
  dead/defensive; if a future caller ever sets `0.0` *unintentionally* it would silence
  quietly. Suggested fix (optional): add a comment on the explicit branch noting "explicit
  0.0 is an intentional zero, NOT the missing-field guard — by design." No code change required.

- **P2-2 — `_running` is seeded from `state.db.positions.quantity` and treated as a NAV-fraction weight.**
  `ops/scripts/quant-daily-interim.py` in `auto_approve_actionables()` (the `_running[_sym] = float(_qty)`
  seeding, ≈ lines 393-401). The comment claims `quantity` "is the net cumulative target
  weight per (asset_class, symbol)." The commit message itself (and the INCIDENT doc)
  acknowledges the Phase-2 share-migration will change `quantity` from a weight to a share
  count — at which point this seed becomes dimensionally wrong (shares summed as if they were
  NAV fractions) and the cap would mis-clip. This is **out of scope for this commit** (the
  cap is default-OFF behind `HERMES_QUANT_PORTFOLIO_CAPS=1`, and the doc explicitly sequences
  cap-removal with Phase-2), but it is a latent coupling that should be pinned by a test or an
  assertion before Phase-2 lands. Not introduced by this commit; flagging for the migration owner.

- **P2-3 — Resolver lives only in the deployed/vendored ops script, not in an importable package module.**
  `_resolve_target_weight` is defined in `ops/scripts/quant-daily-interim.py`. The test
  imports it via `importlib.util.spec_from_file_location` and a `sys.executable` swap to
  neutralize the script's execv guard — a clever but fragile harness. The commit explicitly
  closes the deployed-script-drift gap (Issue #23) by vendoring, which is the right call for
  now. P2 suggestion: a future refactor should hoist the resolver + cap-gate into
  `hermes_quant.risk` so prod and tests import the identical object with no execv dance.
  (This is exactly the test/prod-divergence class of bug the commit is fixing — the harness
  itself reintroduces a thinner version of that risk surface.)

### Correctness confirmations (positive evidence)
- Risk multiplier clamp to ≤1.0 IS present and correct: `if mult < 0.0: mult = 0.0; elif mult > 1.0: mult = 1.0`
  (committee can only silence, never amplify — ADR-0043). Pinned by `test_risk_mult_clamped_to_silence_only`
  (3.0 → clamped to 1.0×, asserts 0.2 not 0.6). ✓
- Resolution order is correct: explicit → signed `kelly_fraction × mult` → `sign(direction) × |trader_size| × mult`.
  Zero-kelly with no fallback correctly returns `(0.0, None)` → loud (`test_zero_kelly_with_no_fallback_is_missing`). ✓
- All numeric coercion is `try/except (TypeError, ValueError)`-guarded; garbage never raises
  (`test_garbage_size_values_dont_crash`). ✓
- The missing-field guard in the caller (`if tgt_src is None:`) stamps `size_field_missing ... NOT a cap silence`
  and `continue`s — the exact anti-laundering behavior the incident demanded. ✓

---

## COMMIT 2 — `5cf5ff2` admissibility seam BP parity — APPROVE

Closes a genuine seam divergence (issue #32): `72e3d8b` plumbed live `available_bp` into
the autonomous seam only, so with live BP known the autonomous seam ADMITTED a short while
the PaperReactor seam REJECTED the same short as `MISSING_ACCOUNT_CONTEXT`. The fix threads
a `bp_provider` kwarg through `admissibility_reject_equity` and has `PaperReactor` pass the
SAME `live_buying_power()` oracle helper the autonomous seam uses. Clean and minimal.

### P0
None.

### P1
None.

### P2 (nits)

- **P2-1 — Lazy `from hermes_quant.admissibility.oracle import live_buying_power` inside the method.**
  `hermes_quant/react/paper.py` ≈ line 334. Import-inside-method is intentional (avoids an
  import cycle / keeps the flag-OFF path IO-free per the docstring), and `live_buying_power`
  is resolved lazily only inside the flag-ON short branch in the precondition. Acceptable;
  the only nit is that monkeypatch-based tests must patch `oracle.live_buying_power` (the
  module attr), which the new tests correctly do. No change needed.

- **P2-2 — RR15 reason-code conflation (documented, intentional).**
  `oracle.live_buying_power` (oracle.py ≈ lines 430-448) collapses genuinely-zero/negative BP
  and unknown/failed-fetch BP both to `None` → both label `MISSING_ACCOUNT_CONTEXT` rather than
  the more precise `INSUFFICIENT_BPR`. The fail-closed *direction* is identical (a zero-BP short
  never admits either way), and it's pinned by `test_admissibility_bp.py`'s `bp if bp > 0 else None`
  contract. Documented design choice, not a defect.

### The three task-specified scrutiny questions — answered with evidence

1. **Does `bp_provider` default to None, leaving the third (MultiLegPaperReactor) seam unchanged?**
   **YES.** Signature: `bp_provider: Callable[[], float | None] | None = None` (keyword-only,
   `admissibility_precondition.py:63`). `multileg.py:183-207` calls `admissibility_reject_equity(...)`
   and closes its kwargs at `extra_metadata=...` (line 202-206) — it never passes `bp_provider`,
   so the collar-leg seam keeps its documented gap (available_bp=None, fails-closed). The
   `Callable` import is present (`from collections.abc import Callable`, line 23) — no NameError
   regression. ✓

2. **Is the fail-closed parity correct (both seams → MISSING_ACCOUNT_CONTEXT when BP unavailable)?**
   **YES.** `live_buying_power()` is fail-closed by construction: returns `None` on missing
   alpaca-py / missing creds / network error / non-positive BP, and `except Exception` never
   re-raises (oracle.py:443-451). Both seams call the *same* helper, so identical `None` input →
   identical `MISSING_ACCOUNT_CONTEXT` verdict. This is explicitly pinned by the NEW test
   `test_autonomous_and_reactor_seams_both_reject_when_bp_missing`, which asserts
   `gme.details["reason"] == rec.reactor_metadata["admissibility_reason"] == "MISSING_ACCOUNT_CONTEXT"`
   and that nothing is written to the bus. ✓

3. **Could plumbing live BP into the paper seam now ADMIT a short that was previously (correctly) REJECTED — does it need a flag?**
   **It CAN change the paper seam from reject→admit — and that is the intended bug fix, not a
   regression — AND it is already flag-gated, so no new flag is required.** Evidence:
   - The change only takes effect when `HERMES_QUANT_ADMISSIBILITY=1`. With the flag unset,
     `admissibility_reject_equity` short-circuits to `None` *before* consulting the oracle or
     `bp_provider` (precondition.py rails, lines 12-13; the lazy `bp_provider()` call is inside
     the flag-ON short branch only). Flag-OFF is a bit-for-bit no-op. ✓
   - When the flag IS on, the *previous* paper-seam behavior (reject a BP-sufficient short as
     `MISSING_ACCOUNT_CONTEXT`) was the **bug** — the autonomous seam already admitted it. So the
     new admit is *restoring correctness / parity*, not loosening a control: the short still must
     pass the real BP hard check (step 8b) against live BP; a BP-insufficient short still rejects
     at BOTH seams. This is asserted by the rewritten
     `test_autonomous_and_reactor_seams_produce_identical_verdict` (both ADMIT under generous live BP;
     a real fill is written) paired with the reject-parity test above.
   - Net: the behavior change is (a) gated behind the existing admissibility flag, (b) directionally
     safe (it can only admit shorts that genuinely have sufficient live BP — never fabricates
     sufficiency), and (c) makes the paper seam MORE consistent with the already-live autonomous seam.
     A separate flag would be over-engineering; the existing `HERMES_QUANT_ADMISSIBILITY` flag is the
     correct gate. ✓

### Test sufficiency
Strong. The suite was upgraded from *pinning the divergence* to *asserting parity* in BOTH
directions (admit-parity under generous BP; reject-parity under None BP), and the docstrings were
rewritten to reflect restored parity rather than a known gap. The one residual gap is that the
**MultiLegPaperReactor third seam** has no parity test (it intentionally still passes None) —
acceptable since the commit explicitly scopes it out, but a future "wire the collar leg" PR should
add the symmetric multileg parity test.

---

## Cross-commit / integration notes (P1 process)

- **P1 — Divergent branches, not a stack.** `git merge-base --is-ancestor a524843 HEAD` → false;
  merge-base is `ae8cbea`. The two fixes touch disjoint files (ops script + cap tests vs.
  admissibility seam + admissibility tests), so a textual merge conflict is unlikely, but they have
  NOT been validated together on one tree. Recommend: after merging both to the integration branch,
  re-run the full combined command
  `pytest tests/ops/test_quant_daily_interim_cap_target.py tests/unit/test_admissibility_account_context_unified.py tests/unit/test_paper_reactor_cap.py -q`
  on the merged tree (it cannot pass today because the C1 test file does not exist on the C2 branch —
  this is the only reason the originally-specified single command 404s).

## Recommendation
- Commit 1 (`a524843`): **APPROVE-WITH-NITS** — merge as-is; track P2-2 (Phase-2 weight/share
  dimensional coupling) and P2-3 (hoist resolver into an importable module) as follow-ups.
- Commit 2 (`5cf5ff2`): **APPROVE** — merge as-is. Clean, fail-closed, flag-gated, parity proven
  in both directions.
