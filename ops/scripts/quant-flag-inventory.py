#!/usr/bin/env python3
"""Generate the canonical HERMES_QUANT_* flag inventory from source-of-truth: the code.

Closes the doc-drift class where the enablement docs hand-list flags and go stale (audit
seed 08c6). Scans hermes_quant/ for every `os.environ.get(FLAG, default)` /
`getenv(FLAG, default)` — directly or via a `_FLAG`/`_ENV` constant — and emits a Markdown
table (full prefixed name, code default, source file:line). Run it to refresh
docs/operations/FLAG-INVENTORY.md; a CI check can diff its output against the committed file
to fail the build when a new flag is added without documenting it.

Usage:
    python ops/scripts/quant-flag-inventory.py            # print the table to stdout
    python ops/scripts/quant-flag-inventory.py --write    # rewrite docs/operations/FLAG-INVENTORY.md
    python ops/scripts/quant-flag-inventory.py --check     # exit 1 if the committed file is stale
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "hermes_quant"
DOC = REPO / "docs" / "operations" / "FLAG-INVENTORY.md"

# os.environ.get("HERMES_QUANT_X", "default") / getenv(...)  — flag named inline
_INLINE = re.compile(
    r"""(?:environ\.get|getenv)\(\s*['"]?(HERMES_QUANT_[A-Z_]+)['"]?\s*,\s*('[^']*'|"[^"]*"|None)"""
)
# _FOO_FLAG = "HERMES_QUANT_X"  — flag bound to a module constant
_CONST = re.compile(r"""([A-Z_]*(?:FLAG|ENV)[A-Z_]*)\s*=\s*['"](HERMES_QUANT_[A-Z_]+)['"]""")
# os.environ.get(CONST, "default") — flag referenced via the constant
_VIA_CONST = re.compile(r"""(?:environ\.get|getenv)\(\s*([A-Z_]+)\s*,\s*('[^']*'|"[^"]*"|None)""")


def scan() -> dict[str, tuple[str, str]]:
    """Return {flag: (default, 'relpath:line')}. First occurrence wins (deterministic by path sort)."""
    flags: dict[str, tuple[str, str]] = {}
    consts: dict[str, str] = {}
    files = sorted(SRC.rglob("*.py"))
    for f in files:
        consts.update({m.group(1): m.group(2) for m in _CONST.finditer(f.read_text(errors="ignore"))})
    for f in files:
        txt = f.read_text(errors="ignore")
        rel = f.relative_to(REPO)
        for m in _INLINE.finditer(txt):
            flag, default = m.group(1), m.group(2).strip("'\"")
            flags.setdefault(flag, (default, f"{rel}:{txt[:m.start()].count(chr(10)) + 1}"))
        for m in _VIA_CONST.finditer(txt):
            cname, default = m.group(1), m.group(2).strip("'\"")
            if cname in consts:
                flag = consts[cname]
                flags.setdefault(flag, (default, f"{rel}:{txt[:m.start()].count(chr(10)) + 1}"))
    return flags


def render(flags: dict[str, tuple[str, str]]) -> str:
    lines = [
        "# HERMES_QUANT_* flag inventory (GENERATED — do not hand-edit)",
        "",
        "> Regenerate with `python ops/scripts/quant-flag-inventory.py --write`. This is the",
        "> authoritative list of every flag READ in `hermes_quant/` with its CODE default. The",
        "> enablement runbooks (FEATURE-ENABLEMENT.md, SELFEVOLVE-ENABLEMENT.md) explain the",
        "> eval-gate-to-flip for the capability flags; this table is just the source-of-truth",
        "> defaults so the docs can't silently drift. Empty default = required/path-style (not a",
        "> boolean capability toggle). Every capability flag defaults `'0'` (default-OFF rail).",
        "",
        f"**{len(flags)} flags** (resolvable default).",
        "",
        "| Flag | Code default | Source |",
        "|---|---|---|",
    ]
    for flag in sorted(flags):
        d, loc = flags[flag]
        lines.append(f"| `{flag}` | `{d}` | `{loc}` |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    flags = scan()
    out = render(flags)
    if "--write" in sys.argv:
        DOC.write_text(out)
        print(f"wrote {DOC.relative_to(REPO)} ({len(flags)} flags)")
    elif "--check" in sys.argv:
        if not DOC.exists() or DOC.read_text() != out:
            print("FLAG-INVENTORY.md is STALE — run: python ops/scripts/quant-flag-inventory.py --write")
            return 1
        print(f"FLAG-INVENTORY.md is current ({len(flags)} flags)")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
