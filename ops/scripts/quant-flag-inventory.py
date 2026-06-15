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

# os.environ.get("HERMES_QUANT_X", "default") / getenv(...)  — flag named inline, WITH a default
_INLINE = re.compile(
    r"""(?:environ\.get|getenv)\(\s*['"]?(HERMES_QUANT_[A-Z_]+)['"]?\s*,\s*('[^']*'|"[^"]*"|None)"""
)
# os.environ.get("HERMES_QUANT_X")  — flag named inline, NO default (e.g. `== "1"` / `or ""`).
# rt03: these default-OFF boolean reads were silently missed; capture them with an empty default.
_INLINE_NODEF = re.compile(r"""(?:environ\.get|getenv)\(\s*['"](HERMES_QUANT_[A-Z_]+)['"]\s*\)""")
# _FOO_FLAG = "HERMES_QUANT_X"  — flag bound to a module constant
_CONST = re.compile(r"""([A-Z_]*(?:FLAG|ENV)[A-Z_]*)\s*=\s*['"](HERMES_QUANT_[A-Z_]+)['"]""")
# os.environ.get(CONST, "default") — flag referenced via the constant, WITH a default
_VIA_CONST = re.compile(r"""(?:environ\.get|getenv)\(\s*([A-Z_]+)\s*,\s*('[^']*'|"[^"]*"|None)""")
# os.environ.get(CONST)  — flag referenced via the constant, NO default
_VIA_CONST_NODEF = re.compile(r"""(?:environ\.get|getenv)\(\s*([A-Z_]+)\s*\)""")


def scan() -> dict[str, tuple[str, str]]:
    """Return {flag: (default, 'relpath:line')}. Deterministic by path sort.

    rt03 correctness: (a) constants are resolved PER-FILE (a single global consts dict made
    generic names like ENV_FLAG/_FLAG collide last-write-wins, silently dropping flags); (b)
    no-inline-default reads (``environ.get("X")`` / ``environ.get(CONST)``) are captured with an
    empty default so money-path toggles read as ``== "1"`` (PORTFOLIO_CAPS, DISSENT_CAP) are not
    missed. A WITH-default read always WINS over a no-default one for the same flag (so a real
    literal default is never overwritten by an empty string)."""
    flags: dict[str, tuple[str, str]] = {}
    has_default: set[str] = set()

    def _record(flag: str, default: str, loc: str, *, with_default: bool) -> None:
        if flag in flags:
            # A with-default reading upgrades a previously-recorded no-default one.
            if with_default and flag not in has_default:
                flags[flag] = (default, loc)
                has_default.add(flag)
            return
        flags[flag] = (default, loc)
        if with_default:
            has_default.add(flag)

    for f in sorted(SRC.rglob("*.py")):
        txt = f.read_text(errors="ignore")
        rel = f.relative_to(REPO)
        # Constants are resolved PER-FILE (no cross-file collision).
        consts = {m.group(1): m.group(2) for m in _CONST.finditer(txt)}

        def _line(start: str, _rel: Path = rel, _txt: str = txt) -> str:
            return f"{_rel}:{_txt[:start].count(chr(10)) + 1}"

        for m in _INLINE.finditer(txt):
            _record(m.group(1), m.group(2).strip("'\""), _line(m.start()), with_default=True)
        for m in _VIA_CONST.finditer(txt):
            if m.group(1) in consts:
                _record(consts[m.group(1)], m.group(2).strip("'\""), _line(m.start()), with_default=True)
        for m in _INLINE_NODEF.finditer(txt):
            _record(m.group(1), "", _line(m.start()), with_default=False)
        for m in _VIA_CONST_NODEF.finditer(txt):
            if m.group(1) in consts:
                _record(consts[m.group(1)], "", _line(m.start()), with_default=False)
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
