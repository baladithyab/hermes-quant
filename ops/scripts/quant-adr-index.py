#!/usr/bin/env python3
"""Regenerate docs/adr/README.md from the ADR files (audit seed b3d6).

The index had drifted to 54 of 82 ADRs (28 missing, incl. money-critical 0027/0033/0051/
0058). Per adr-methodology: rebuild the index, never hand-maintain it. ADRs encode status
two ways — YAML frontmatter (`status:`) or bold-markdown (`**Status**: proposed`) — and some
carry a compound status ("Part A accepted; Part B proposed"); this reads both and preserves
compound statuses verbatim.

Usage:
    python ops/scripts/quant-adr-index.py            # print the index to stdout
    python ops/scripts/quant-adr-index.py --write     # rewrite docs/adr/README.md
    python ops/scripts/quant-adr-index.py --check      # exit 1 if the committed index is stale
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADR = REPO / "docs" / "adr"
INDEX = ADR / "README.md"


def _field(txt: str, name: str) -> str:
    """Read `name` from YAML frontmatter or a `**Name**: value` / `Name: value` line."""
    fm = re.match(r"^---\n(.*?)\n---", txt, re.S)
    if fm:
        for line in fm.group(1).splitlines():
            if line.strip().lower().startswith(name + ":"):
                v = line.split(":", 1)[1].strip()
                if v:
                    return v
    # Match the many in-body forms, anchored to the line start, tolerating leading
    # list bullets ("- "), table pipes ("| "), and bold markers wherever they fall:
    #   **Status**: proposed   |   - **Status:** proposed   |   | Status | **Accepted** |
    for line in txt.splitlines():
        # normalize: drop leading bullet/pipe, strip bold + surrounding pipes
        norm = line.strip().lstrip("-|").strip()
        norm_nb = norm.replace("**", "").replace("*", "")
        m = re.match(rf"(?i)^{name}\s*[:|]\s*(.+)$", norm_nb)
        if m:
            v = m.group(1).strip().strip("|").strip()
            if v:
                return v
    return "?"


def collect() -> list[tuple[str, str, str, str, str]]:
    rows = []
    for f in sorted(ADR.glob("ADR-[0-9]*.md")):
        m = re.match(r"ADR-(\d{4})-", f.name)
        if not m:
            continue
        txt = f.read_text(errors="ignore")
        num = m.group(1)
        status = _field(txt, "status")
        date = _field(txt, "date")
        h1 = re.search(r"^#\s+(.+)$", txt, re.M)
        raw = h1.group(1).strip() if h1 else f.stem
        title = re.sub(r"^ADR[- ]?\d+\s*[:—\-]?\s*", "", raw).strip()
        rows.append((num, f.name, title.replace("|", "\\|"), status.replace("|", "\\|"), date))
    return rows


def render(rows) -> str:
    out = [
        "# Architecture Decision Records",
        "",
        f"{len(rows)} ADRs — **generated** index (regenerate with "
        "`python ops/scripts/quant-adr-index.py --write`; do not hand-maintain).",
        "",
        "Status vocabulary: proposed | accepted | rejected | deprecated | superseded by ADR-NNNN. "
        "A compound status (e.g. \"Part A accepted; Part B proposed\") is the ADR's own — see the file.",
        "",
        "| # | Title | Status | Date |",
        "|---|---|---|---|",
    ]
    for num, fn, title, status, date in rows:
        out.append(f"| [ADR-{num}]({fn}) | {title} | {status} | {date} |")
    out.append("")
    return "\n".join(out)


def main() -> int:
    rows = collect()
    text = render(rows)
    if "--write" in sys.argv:
        INDEX.write_text(text)
        print(f"wrote {INDEX.relative_to(REPO)} ({len(rows)} ADRs)")
    elif "--check" in sys.argv:
        if not INDEX.exists() or INDEX.read_text() != text:
            print("docs/adr/README.md is STALE — run: python ops/scripts/quant-adr-index.py --write")
            return 1
        print(f"docs/adr/README.md is current ({len(rows)} ADRs)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
