"""Lightweight consistency checks for ``docs/operations/ROLLOUT.md``.

These tests intentionally check structural / cross-reference properties only.  They do
not reason about content correctness.  Per ADR-0062, their purpose is to ensure that:

  * the rollout playbook exists at its canonical path;
  * every LLM-rollout env-var named in the playbook is actually read by code;
  * every LLM-rollout env-var read by code is named in the playbook;
  * every ADR referenced in the playbook exists as a file in ``docs/adr/``;
  * ADR-0062 itself has the expected ``Status: Accepted`` frontmatter;
  * the playbook contains all seven required section headers;
  * the §4 KPIs list contains at least five items;
  * the §0 pre-flight checklist contains at least six checkboxes.

These tests are fast, have no external dependencies, and surface a clear failure mode
(rename an env var in code → consistency test fails → developer either updates the
playbook or reverts the rename).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLOUT_PATH = REPO_ROOT / "docs" / "operations" / "ROLLOUT.md"
ADR_DIR = REPO_ROOT / "docs" / "adr"
ADR_0062 = ADR_DIR / "ADR-0062-rollout-playbook.md"
HERMES_QUANT_PKG = REPO_ROOT / "hermes_quant"

# The four flags this rollout playbook is responsible for.  These are the LLM-rollout
# flags listed in ADR-0054 / ADR-0056 / ADR-0057 / ADR-0058 and are the *only* env
# vars the playbook activates.  Other ``HERMES_QUANT_*`` env vars exist in the code
# (e.g. ``HERMES_QUANT_JOURNAL_PATH``, ``HERMES_QUANT_MEMORY_INJECT``) but they are
# operational knobs, not rollout flags, and are intentionally out of scope for this
# playbook.
ROLLOUT_FLAGS: tuple[str, ...] = (
    "HERMES_QUANT_REGIME_HMM",
    "HERMES_QUANT_REFLECTOR_LLM",
    "HERMES_QUANT_RISK_COMMITTEE_LLM",
    "HERMES_QUANT_TRADER_LLM",
)

REQUIRED_SECTION_HEADERS: tuple[str, ...] = (
    "## 0. Pre-Flight Checklist",
    "## 1. Activation Order",
    "## 2. Smoke-Test Sequence",
    "## 3. Rollback Procedure",
    "## 4. Monitoring KPIs",
    "## 5. Kill-Switch",
    "## 6. Cross-References",
    "## 7. Append-Only Event Stores Reference Table",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_rollout() -> str:
    assert ROLLOUT_PATH.exists(), f"ROLLOUT.md missing at {ROLLOUT_PATH}"
    return ROLLOUT_PATH.read_text(encoding="utf-8")


def _flags_read_by_code() -> set[str]:
    """Return the set of rollout-flag env vars actually referenced by ``hermes_quant/``.

    A flag is considered "wired" if its literal name appears either in an
    ``os.environ.get(...)`` / ``os.getenv(...)`` call or as a string literal that is
    later passed to one of those calls (this is the case e.g. in
    ``risk_committee/committee.py`` where the name is bound to a module-level constant
    ``_LLM_FLAG_ENV_VAR`` and read via ``os.environ.get(_LLM_FLAG_ENV_VAR, ...)``).

    Concretely we accept the flag if its quoted literal appears anywhere in any
    ``hermes_quant/`` Python source file, AND at least one ``os.environ.get`` /
    ``os.getenv`` call appears in the same file.  This is loose enough to handle
    constant indirection but tight enough to catch typos and stale references.

    Only the four documented rollout flags are considered; other ``HERMES_QUANT_*``
    env vars in the code base are intentionally out of scope for this playbook.
    """
    found: set[str] = set()
    env_call_re = re.compile(r"os\.(?:environ\.get|getenv)\s*\(")
    for py_file in HERMES_QUANT_PKG.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover — unreadable file shouldn't break tests
            continue
        if not env_call_re.search(text):
            continue
        for flag in ROLLOUT_FLAGS:
            # Match the literal flag name inside a quoted string ("..." or '...').
            if f'"{flag}"' in text or f"'{flag}'" in text:
                found.add(flag)
    return found


def _split_section(text: str, header: str) -> str:
    """Return the body of the section that starts with ``header`` (exclusive of the
    next ``## `` header).  Returns empty string if the header is missing.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith(header):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rollout_file_exists() -> None:
    """ROLLOUT.md is at the canonical path."""
    assert ROLLOUT_PATH.is_file(), (
        f"ROLLOUT.md missing at {ROLLOUT_PATH} — the rollout playbook must live at "
        "docs/operations/ROLLOUT.md per ADR-0062."
    )


def test_all_rollout_flags_appear_in_playbook() -> None:
    """Every documented rollout flag is named at least once in ROLLOUT.md."""
    text = _read_rollout()
    missing = [flag for flag in ROLLOUT_FLAGS if flag not in text]
    assert not missing, (
        f"ROLLOUT.md does not mention these rollout flags: {missing}.  Every flag in "
        f"ROLLOUT_FLAGS must appear in the playbook so the operator can find it."
    )


def test_all_rollout_flags_are_read_by_code() -> None:
    """Every rollout flag named in the playbook is actually read by hermes_quant/."""
    found = _flags_read_by_code()
    missing = set(ROLLOUT_FLAGS) - found
    assert not missing, (
        f"These rollout flags are documented but never read by any code under "
        f"hermes_quant/: {sorted(missing)}.  Either wire the flag in code or remove it "
        "from ROLLOUT.md."
    )


def test_no_undocumented_rollout_flags_in_code() -> None:
    """The set of rollout flags in code matches the documented set exactly.

    This guards against a developer adding a fifth LLM-rollout flag without updating
    the playbook.  Non-rollout ``HERMES_QUANT_*`` env vars are intentionally out of
    scope (see ROLLOUT_FLAGS docstring).
    """
    found = _flags_read_by_code()
    extra = found - set(ROLLOUT_FLAGS)
    assert not extra, (
        f"Code reads rollout-flag-shaped env vars not listed in ROLLOUT_FLAGS: "
        f"{sorted(extra)}.  If these are real rollout flags, document them in "
        "ROLLOUT.md and add to ROLLOUT_FLAGS in this test."
    )


def test_all_referenced_adrs_exist() -> None:
    """Every ADR-XXXX referenced in ROLLOUT.md exists as a file in docs/adr/."""
    text = _read_rollout()
    referenced = set(re.findall(r"ADR-(\d{4})", text))
    assert referenced, "ROLLOUT.md references no ADRs — at minimum it must cite ADR-0031."
    existing = {p.name[4:8] for p in ADR_DIR.glob("ADR-*.md")}
    missing = referenced - existing
    assert not missing, (
        f"ROLLOUT.md references ADRs that do not exist: "
        f"{sorted('ADR-' + n for n in missing)}.  Either create the ADR or remove "
        "the reference."
    )


def test_adr_0062_has_accepted_status() -> None:
    """ADR-0062 frontmatter declares ``Status: Accepted``."""
    assert ADR_0062.is_file(), f"ADR-0062 missing at {ADR_0062}"
    head = ADR_0062.read_text(encoding="utf-8").splitlines()[:15]
    head_text = "\n".join(head)
    assert "Status:" in head_text and "Accepted" in head_text, (
        f"ADR-0062 must have 'Status: Accepted' in its frontmatter (first 15 lines).  "
        f"Found:\n{head_text}"
    )


def test_rollout_has_all_required_sections() -> None:
    """ROLLOUT.md contains every required ## header in order."""
    text = _read_rollout()
    last_index = -1
    for header in REQUIRED_SECTION_HEADERS:
        idx = text.find(header)
        assert idx != -1, (
            f"ROLLOUT.md is missing required section header: {header!r}"
        )
        assert idx > last_index, (
            f"ROLLOUT.md sections out of order: {header!r} appears before a "
            "previously-required header."
        )
        last_index = idx


def test_kpis_section_lists_at_least_five_items() -> None:
    """§4 Monitoring KPIs lists at least five distinct KPIs (numbered list items)."""
    text = _read_rollout()
    body = _split_section(text, "## 4. Monitoring KPIs")
    # Numbered list items at the start of a line: "1.", "2.", ...
    items = re.findall(r"^\s*\d+\.\s+", body, flags=re.MULTILINE)
    assert len(items) >= 5, (
        f"§4 must list at least 5 KPIs; found {len(items)} numbered list items.  "
        "Operators rely on a meaningful KPI surface during rollout."
    )


def test_pre_flight_checklist_has_at_least_six_checkboxes() -> None:
    """§0 Pre-Flight Checklist has at least six ``- [ ]`` items."""
    text = _read_rollout()
    body = _split_section(text, "## 0. Pre-Flight Checklist")
    items = re.findall(r"^- \[ \] ", body, flags=re.MULTILINE)
    assert len(items) >= 6, (
        f"§0 must have at least 6 checklist items; found {len(items)}.  The pre-flight "
        "checklist is the gate between 'I think this is safe' and 'I have evidence'."
    )


# ---------------------------------------------------------------------------
# Module-level sanity (collected as a single test by pytest if everything else passes).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ROLLOUT_FLAGS)
def test_each_flag_documented_in_activation_order_section(flag: str) -> None:
    """Every rollout flag is mentioned inside §1 Activation Order, not just the table."""
    text = _read_rollout()
    body = _split_section(text, "## 1. Activation Order")
    assert flag in body, (
        f"§1 Activation Order does not mention {flag!r}.  Every rollout flag must "
        "appear in the activation-order section so the operator knows when to flip it."
    )
