"""tests/ops/test_flag_inventory_drift.py — the committed flag inventory must not drift.

`docs/operations/FLAG-INVENTORY.md` is the GENERATED source-of-truth for every
`HERMES_QUANT_*` flag read in `hermes_quant/` with its CODE default. It is the
authoritative inventory that `docs/FLAGS.md` (the human decision-sheet) points at.
The risk this gate closes: an operator of money-software reads a STALE committed
inventory and mis-judges what is live (audit seed ra09 — the committed mirror had
drifted 41→55 flags, including money-path flags like ALPACA_PAPER / DETERMINISTIC_EQUITY
and the SEMANTIC_ENABLED/REFLECTION/MEMORY_INJECT/WEEKLY_RETRO default flips).

`quant-flag-inventory.py --check` exits 1 when the committed file disagrees with a
fresh code scan. This test runs that exact gate and asserts exit 0, so a future flag
add or default flip that forgets to regenerate the doc fails the build.

Regenerate with: `python ops/scripts/quant-flag-inventory.py --write`
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-flag-inventory.py"
)


def _load_inventory():
    spec = importlib.util.spec_from_file_location("quant_flag_inventory_x", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flag_inventory_doc_is_not_stale():
    """`--check` must pass: the committed FLAG-INVENTORY.md == a fresh code scan.

    This is the drift gate. If it fails, run:
        python ops/scripts/quant-flag-inventory.py --write
    """
    mod = _load_inventory()
    flags = mod.scan()
    expected = mod.render(flags)
    committed = mod.DOC.read_text() if mod.DOC.exists() else ""
    assert committed == expected, (
        "docs/operations/FLAG-INVENTORY.md is STALE vs the code scan "
        f"({len(flags)} flags). Regenerate: "
        "python ops/scripts/quant-flag-inventory.py --write"
    )


def test_check_mode_exits_zero(monkeypatch):
    """Drive the real CLI `--check` path end-to-end and assert exit code 0.

    Belt-and-suspenders over the content assert above: this exercises main()'s
    actual --check branch (the thing CI / the deploy gate runs), so the gate that
    the operator relies on can't silently regress to always-pass.
    """
    mod = _load_inventory()
    monkeypatch.setattr(mod.sys, "argv", ["quant-flag-inventory.py", "--check"])
    assert mod.main() == 0
