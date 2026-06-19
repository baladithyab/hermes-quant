"""Cross-account / cross-unit bus-pooling guard for the weekly playbook readers.

The weekly playbook (scripts/quant-playbook-weekly.py) reconstructs the positions it
manages from EXACTLY the paper-default+equity partition (load_portfolio ->
reconstruct_portfolio(account_id="paper-default", asset_class="equity")). But the
per-asset raw-executions readers that decide the close SIZE and the exit RULESET
(_held_nav_fraction, _establishing_leg -> find_entry_record / infer_play_tag) scan the
SHARED executions.jsonl bus, which is ALSO written by other accounts:

  - the alpaca-paper SHADOW reactor (account_id="alpaca-paper", equity — the SAME tickers
    can collide, e.g. AAPL on both books), and
  - the freqtrade crypto consumer (account_id="freqtrade", qty in RAW COIN units).

Before the fix these readers filtered ONLY by `asset`, so a DIFFERENT account's later
record for the same ticker overrode the paper-default decision:

  * _held_nav_fraction returned the wrong account's NAV fraction -> the armed close fired
    fill_size_pct = -(wrong fraction); the live state.db delta fold (new_qty = held +
    (-wrong)) left a position the weekly reported CLOSED still OPEN (or flipped its sign)
    — a money fail-OPEN on the live close path.
  * find_entry_record / infer_play_tag picked the wrong account's opening leg -> wrong
    days_held (the swing 60d stop input) and wrong play classification (leaps vs swing ->
    wrong exit ruleset).

This is the ar34/ar35/ar25 cross-account/cross-unit bus-aggregation family. The fix
filters every raw-executions reader to the weekly's account partition, resolving each
record's account the SAME way the loader does (portfolio.state._record_account ladder:
top-level account_id -> reactor_metadata.account_id -> "paper-default" sentinel).

These tests are self-contained (pure, no I/O) and would FAIL on the pre-fix code.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "quant-playbook-weekly.py"
if not SCRIPT_PATH.exists():
    SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "quant-playbook-weekly.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT_PATH.exists(),
    reason=f"quant-playbook-weekly.py not found at {SCRIPT_PATH}",
)


@pytest.fixture(scope="module")
def mod():
    import sys

    spec = importlib.util.spec_from_file_location("qpw_xacct", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["qpw_xacct"] = m  # register before exec so dataclass annotations resolve
    spec.loader.exec_module(m)
    return m


def _rec(asset, acct, target, asof, **extra):
    rec = {
        "asset": asset,
        "asset_class": extra.pop("asset_class", "equity"),
        "account_id": acct,
        "target_position_pct": target,
        "fill_size_pct": target,
        "fill_price": 200.0,
        "asof_execution": asof,
        "reactor_name": extra.pop("reactor_name", "paper"),
    }
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# _held_nav_fraction — the close-size sizing read (live money path)
# ---------------------------------------------------------------------------

def test_held_fraction_ignores_later_alpaca_paper_record(mod):
    """A later alpaca-paper AAPL target must NOT override the paper-default held size.

    Pre-fix: _held_nav_fraction returned 0.10 (alpaca, latest by asof) -> the armed close
    fired -0.10 and left the real +0.30 paper-default long at +0.20 (NOT flat).
    """
    execs = [
        _rec("AAPL", "paper-default", 0.30, "2026-06-01T14:00:00Z"),
        _rec("AAPL", "alpaca-paper", 0.10, "2026-06-10T14:00:00Z", reactor_name="alpaca_paper"),
    ]
    held = mod._held_nav_fraction(execs, "AAPL")
    assert held == 0.30, "must read paper-default's 0.30, not alpaca-paper's later 0.10"
    # The close that flattens the live state.db delta fold (new_qty = 0.30 + (-held)).
    assert round(0.30 + (-held), 10) == 0.0, "fill_size_pct=-held must flatten the book"


def test_held_fraction_ignores_freqtrade_raw_coin_record(mod):
    """A freqtrade crypto record (raw-COIN qty) must not pool into the NAV-fraction read."""
    execs = [
        _rec("AAPL", "paper-default", 0.30, "2026-06-01T14:00:00Z"),
        # freqtrade shape: account_id="freqtrade", side/qty in RAW COINS, no reactor_name.
        {
            "asset": "AAPL",
            "asset_class": "crypto",
            "account_id": "freqtrade",
            "side": "buy",
            "qty": 0.5,
            "fill_price": 201.0,
            "asof_execution": "2026-06-10T14:00:00Z",
        },
    ]
    assert mod._held_nav_fraction(execs, "AAPL") == 0.30


def test_held_fraction_resolves_account_via_reactor_metadata(mod):
    """An alpaca record carrying its account ONLY in reactor_metadata is still excluded."""
    execs = [
        _rec("AAPL", "paper-default", 0.30, "2026-06-01T14:00:00Z"),
        {
            "asset": "AAPL",
            "asset_class": "equity",
            # No top-level account_id; account lives in reactor_metadata (the real shape).
            "reactor_metadata": {"account_id": "alpaca-paper"},
            "reactor_name": "alpaca_paper",
            "target_position_pct": 0.10,
            "fill_size_pct": 0.10,
            "fill_price": 201.0,
            "asof_execution": "2026-06-10T14:00:00Z",
        },
    ]
    assert mod._held_nav_fraction(execs, "AAPL") == 0.30


# ---------------------------------------------------------------------------
# find_entry_record / infer_play_tag — the entry leg + exit-ruleset reads
# ---------------------------------------------------------------------------

def test_find_entry_record_ignores_other_account_opener(mod):
    """The establishing leg must come from THIS account, not an earlier alpaca opener.

    Pre-fix: the earlier alpaca-paper AAPL fill (file-order first) was picked as the
    entry -> wrong days_held + wrong play classification.
    """
    execs = [
        _rec("AAPL", "alpaca-paper", 0.10, "2026-05-01T14:00:00Z",
             reactor_name="alpaca_paper", signal_id="leaps-alpaca"),
        _rec("AAPL", "paper-default", 0.30, "2026-06-01T14:00:00Z",
             reactor_name="paper", signal_id="swing-paper"),
    ]
    entry = mod.find_entry_record(execs, "AAPL", 100.0)
    assert entry is not None
    assert entry.get("account_id") == "paper-default"
    assert entry.get("asof_execution") == "2026-06-01T14:00:00Z"


def test_infer_play_tag_ignores_other_account_opener(mod):
    """Play classification must read THIS account's opener (swing), not alpaca's leaps."""
    execs = [
        _rec("AAPL", "alpaca-paper", 0.10, "2026-05-01T14:00:00Z",
             reactor_name="alpaca_paper", signal_id="leaps-alpaca"),
        _rec("AAPL", "paper-default", 0.30, "2026-06-01T14:00:00Z",
             reactor_name="paper", signal_id="swing-paper"),
    ]
    assert mod.infer_play_tag(execs, "AAPL", 100.0) == "swing"


# ---------------------------------------------------------------------------
# Byte-identical: single-account / unstamped records (the common live + test case)
# ---------------------------------------------------------------------------

def test_unstamped_record_resolves_to_paper_default_byte_identical(mod):
    """A record with NO account stamp resolves to paper-default -> unchanged behavior."""
    execs = [{"asset": "MSFT", "side": "buy", "play_tag": "leaps"}]
    assert mod.infer_play_tag(execs, "MSFT") == "leaps"

    execs2 = [
        {
            "asset": "AAPL",
            "asset_class": "equity",
            "target_position_pct": 0.20,
            "fill_size_pct": 0.20,
            "fill_price": 200.0,
            "asof_execution": "2026-06-08T13:31:05+00:00",
            "reactor_name": "paper",
        }
    ]
    assert mod._held_nav_fraction(execs2, "AAPL") == 0.20
    assert mod.find_entry_record(execs2, "AAPL") is not None
