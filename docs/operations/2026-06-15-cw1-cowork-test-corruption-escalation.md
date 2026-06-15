# cw1 — cowork-quant test_mask.py corruption: verified escalation (2026-06-15)

**Status:** ESCALATION (agent cannot fix — `cowork-quant/` is out of this agent's edit scope).

## Verified finding
`cowork-quant/scripts/quantcore/tests/test_mask.py` is **3713 bytes of binary `data`**, NOT a
Python source file. Verified 2026-06-15: `file(1)` reports `data`; the first bytes are
non-printable garbage (`$^ZM-^\M-^R...`), not UTF-8/ASCII Python. The file cannot be collected or
run by pytest — it would raise a `SyntaxError`/`UnicodeDecodeError` at import.

## Why it matters
Any "212 tests green" (or similar) claim for the cowork-quant `quantcore` suite that *counts* this
file is **unverifiable / false** — a corrupted test file is silently skipped or errors out, so the
green count does not reflect this module's coverage. A passing-suite claim over a corrupt test is a
silence-by-default violation (the test that should guard `mask` behaviour is not running).

## Recommended action (operator / cowork-quant owner)
1. `git -C cowork-quant log --oneline -- scripts/quantcore/tests/test_mask.py` — find the last
   non-corrupt revision; `git -C cowork-quant show <sha>:scripts/quantcore/tests/test_mask.py` to
   recover the source, OR regenerate the test from the `mask` module's contract.
2. Re-run the cowork-quant `quantcore` suite and confirm the real (non-inflated) green count.
3. Add a CI guard: a tiny "every tests/*.py is importable" smoke check so a binary-corrupt test file
   fails loudly rather than inflating a green count.

## Scope note
This agent is instructed not to edit `cowork-quant/`. The fix belongs to the cowork-quant repo
owner. This note is the agent-side deliverable: verified the corruption, explained the integrity
risk, and handed off the exact recovery steps.
