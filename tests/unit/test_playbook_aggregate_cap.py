"""Regression coverage for the playbook/hourly aggregate cap bypass.

The live playbook script is a cron artifact under ops/scripts rather than an
importable package module, so these tests load it with importlib and fake HOME.
They exercise the direct Alpaca order path without network calls.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-playbook-tick.py"

FIRE_SYMBOLS = ("AAPL", "MSFT", "NVDA", "TSLA", "GOOGL")
FIXED_TS = "2026-06-04T13:00:00Z"
FIXED_DATE_ET = "2026-06-04"


def _load_tick_module(monkeypatch: pytest.MonkeyPatch, root: Path, *, name: str):
    fake_home = root / "home"
    fake_home.mkdir(parents=True)
    (fake_home / ".hermes" / "quant" / "watchlist").mkdir(parents=True)
    (fake_home / ".hermes" / "quant" / "playbook").mkdir(parents=True)
    (fake_home / ".hermes" / "secrets").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_TICK_MOCK", "1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    mod.HERMES_HOME = fake_home / ".hermes"
    mod.QUANT_HOME = mod.HERMES_HOME / "quant"
    mod.WATCHLIST_PATH = mod.QUANT_HOME / "watchlist" / "play-fit.json"
    mod.HALT_MIRROR_PATH = mod.QUANT_HOME / "halt_state.json"
    mod.PLAYBOOK_DIR = mod.QUANT_HOME / "playbook"
    mod.JOURNAL_PATH = mod.PLAYBOOK_DIR / "tick-journal.jsonl"
    mod.SECRETS_PATH = mod.HERMES_HOME / "secrets" / "alpaca.env"
    mod.utcnow_iso = lambda: FIXED_TS
    mod.today_et_date = lambda: FIXED_DATE_ET
    return mod


def _write_fire_watchlist(mod: Any, symbols: tuple[str, ...] = FIRE_SYMBOLS) -> None:
    mod.WATCHLIST_PATH.write_text(
        json.dumps(
            {
                "as_of": FIXED_TS,
                "plays": {
                    "swing": [
                        {
                            "symbol": symbol,
                            "play": "swing",
                            "state": "active",
                            "last_score": 0.9,
                            "consecutive_days_above_floor": 1,
                            "consecutive_days_below_onboard": 0,
                            "extras": {},
                            "last_seen_at": FIXED_TS,
                            "onboarded_at": FIXED_TS,
                            "eviction_reason": None,
                        }
                        for symbol in symbols
                    ]
                },
            }
        )
    )


def _fire_result(kelly_fraction: float = 0.05) -> dict[str, Any]:
    return {
        "risk_gate": {
            "pass": True,
            "recommended_action": "long",
            "kelly_fraction": kelly_fraction,
            "gated_reason": None,
        },
        "aggregated_signal": {
            "direction": 1,
            "magnitude": 0.03,
            "confidence": 0.7,
            "horizon": "1d",
            "aggregator": "test",
        },
        "as_of": FIXED_TS,
        "caveats": [],
    }


def _force_all_fire(monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    monkeypatch.setattr(mod, "call_advisor", lambda symbol: _fire_result())


def _install_order_spy(monkeypatch: pytest.MonkeyPatch, mod: Any) -> list[dict[str, Any]]:
    placed: list[dict[str, Any]] = []

    def fake_order(
        symbol: str,
        notional_usd: float,
        *,
        side: str = "buy",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        placed.append({
            "symbol": symbol, "notional_usd": notional_usd, "side": side,
            "client_order_id": client_order_id,
        })
        idx = len(placed)
        return {
            "id": f"order-{idx}",
            "client_order_id": client_order_id or f"client-{idx}",
            "submitted_at": FIXED_TS,
        }

    monkeypatch.setattr(mod, "place_paper_market_order", fake_order)
    return placed


def _journal_rows(mod: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in mod.JOURNAL_PATH.read_text().splitlines() if line.strip()]


def _decision_rows(mod: Any) -> list[dict[str, Any]]:
    return [row for row in _journal_rows(mod) if row.get("decision")]


def _ready_fire_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, module_name: str):
    mod = _load_tick_module(monkeypatch, tmp_path / module_name, name=module_name)
    _write_fire_watchlist(mod)
    _force_all_fire(monkeypatch, mod)
    placed = _install_order_spy(monkeypatch, mod)
    return mod, placed


def test_flag_off_documents_current_per_fire_only_bypass(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", raising=False)
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_off")
    monkeypatch.setattr(
        mod,
        "read_alpaca_account_equity",
        lambda: (_ for _ in ()).throw(AssertionError("OFF path must not read account equity")),
    )

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 5
    assert summary["silenced"] == 0
    assert len(placed) == 5
    assert sum(order["notional_usd"] for order in placed) == pytest.approx(5000.0)


def test_flag_on_silences_fires_that_would_breach_aggregate_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_on")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 1250.0)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 2
    assert summary["silenced"] == 3
    assert len(placed) == 2
    assert sum(order["notional_usd"] for order in placed) == pytest.approx(2000.0)
    assert sum(order["notional_usd"] for order in placed) <= 2500.0

    silenced = [row for row in _decision_rows(mod) if row["decision"] == "silenced"]
    assert len(silenced) == 3
    for row in silenced:
        assert "portfolio_cap" in row["reason"]
        assert "aggregate" in row["reason"]
        assert row["aggregate_cap_ceiling_usd"] == pytest.approx(2500.0)
        assert row["aggregate_cap_consumed_usd"] == pytest.approx(2000.0)


def test_flag_on_unreadable_equity_silences_every_fire(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_no_equity")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: None)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 0
    assert summary["silenced"] == 5
    assert placed == []
    assert all("account_equity" in row["reason"] for row in _decision_rows(mod))


def test_flag_unset_is_byte_identical_to_explicit_off(monkeypatch, tmp_path):
    def run_case(module_name: str, flag_value: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if flag_value is None:
            monkeypatch.delenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", raising=False)
        else:
            monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", flag_value)
        mod, _placed = _ready_fire_run(monkeypatch, tmp_path, module_name=module_name)
        summary = mod.run_tick(dry_run=False)
        return summary, _journal_rows(mod)

    baseline_summary, baseline_rows = run_case("qpt_aggregate_unset", None)
    explicit_off_summary, explicit_off_rows = run_case("qpt_aggregate_zero", "0")

    assert explicit_off_summary == baseline_summary
    assert explicit_off_rows == baseline_rows


@pytest.mark.parametrize("bad_equity", [math.nan, math.inf, -math.inf])
def test_flag_on_non_finite_equity_silences_every_fire(monkeypatch, tmp_path, bad_equity):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name=f"qpt_bad_equity_{bad_equity}")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: bad_equity)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 0
    assert summary["silenced"] == 5
    assert placed == []
    assert all("portfolio_cap_aggregate_breach" in row["reason"] for row in _decision_rows(mod))


def test_flag_on_non_finite_notional_silences_without_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_bad_notional")
    _write_fire_watchlist(mod, symbols=("AAPL",))
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 100_000.0)
    monkeypatch.setattr(mod, "kelly_to_notional", lambda advisor_result: math.nan)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 0
    assert summary["silenced"] == 1
    assert placed == []
    row = _decision_rows(mod)[0]
    assert row["decision"] == "silenced"
    assert "non_finite_notional" in row["reason"]


def test_flag_on_dry_run_path_does_not_fetch_equity_or_place(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_aggregate_dry_run")
    monkeypatch.setattr(
        mod,
        "read_alpaca_account_equity",
        lambda: (_ for _ in ()).throw(AssertionError("dry-run must not build budget")),
    )

    summary = mod.run_tick(dry_run=True)

    assert summary["fired"] == 5
    assert summary["silenced"] == 0
    assert placed == []


# ---------------------------------------------------------------------------
# cap2: the aggregate cap must count the REAL open book, not just this tick's
# own fires. The ceiling is denominated in USD (equity × gross_headroom); the
# consumed side must seed from the canonical PortfolioState's open positions.
#
# DIMENSIONAL TRUTH (verified against the canonical money-state code):
#   * For an equity fill the PaperReactor persists fill_size_pct — a SIGNED
#     NAV-FRACTION — straight into Position.quantity (state.portfolio_state §3,
#     react/paper.py:69 + reactor_metadata.quantity ABSENT for equity). So
#     Position.quantity is a NAV-fraction, NOT a share count.
#   * The canonical ADR-0071 gross cap measures the existing book as
#     Σ |Position.quantity| (react/multileg.py:461-466 feeds bare quantity into
#     RiskPortfolioState whose gross_exposure_pct = Σ|p|; portfolio_normalize.py:
#     116-117) and compares against caps.max_gross_exposure_pct (a NAV-fraction).
#   * The playbook cap CEILING is equity_usd × gross_headroom (USD). The matching
#     USD gross of the real book is therefore equity_usd × Σ|quantity|.
#   * Multiplying the NAV-fraction quantity by avg_entry_price (a per-share USD
#     price) is a UNIT ERROR — it returns a NAV-fraction × price, ~hundreds× too
#     small, silently re-introducing the cap2 under-count.
# ---------------------------------------------------------------------------


def test_real_open_positions_gross_usd_is_equity_times_gross_nav_fraction(monkeypatch, tmp_path):
    """The real-book gross-USD reader returns equity × Σ|quantity|.

    Position.quantity is a SIGNED NAV-FRACTION (canonical state.db semantics for
    equity fills). NVDA +1.0 (long) + TSLA -0.8 (short) → gross NAV-fraction 1.8;
    with equity 100_000 USD the TRUE gross exposure is 180_000 USD. The legacy
    |qty|×avg_entry_price×mult formula returns 1.0*350 + 0.8*250 = 550 (a
    NAV-fraction times a per-share price = not USD, ~327× too low) — this asserts
    the correct equity×Σ|quantity| value, so it is RED before the unit fix."""
    mod = _load_tick_module(monkeypatch, tmp_path / "qpt_real_gross", name="qpt_real_gross")

    class _Pos:
        def __init__(self, asset_class, symbol, quantity, avg_entry_price):
            self.asset_class = asset_class
            self.symbol = symbol
            self.quantity = quantity
            self.avg_entry_price = avg_entry_price

    class _State:
        def get_positions(self, account_id):
            assert account_id == "paper-default"
            return {
                ("equity", "NVDA"): _Pos("equity", "NVDA", 1.0, 350.0),   # +1.0 NAV-frac long
                ("equity", "TSLA"): _Pos("equity", "TSLA", -0.8, 250.0),  # -0.8 NAV-frac short
            }

    import hermes_quant.state.portfolio_state as ps_mod
    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: _State())
    gross = mod.read_real_open_positions_gross_usd(100_000.0)
    # gross NAV-fraction = |1.0| + |-0.8| = 1.8; × equity 100_000 = 180_000 USD.
    assert gross == pytest.approx(180_000.0)


def test_real_open_positions_gross_usd_true_unit_equity_normalized(monkeypatch, tmp_path):
    """ar118: a det-equity true_unit EQUITY row holds REAL SHARES, not a NAV-fraction.
    read_real_open_positions_gross_usd must normalize it via the unit_kind-aware seam
    (position_gross_fraction: qty*avg_price*mult/nav), NOT Σ|raw quantity|.

    100 shares of AAPL @ $150, NAV $100k → NAV-fraction 100*150/100_000 = 0.15 → gross
    USD = 100_000 × 0.15 = $15,000. The pre-fix Σ|quantity| read 100 as a 10000%
    NAV-fraction → 100_000 × 100 = $10,000,000 (~667× over-count) → blows the USD ceiling
    and over-silences every fire (fail-CLOSED). DETERMINISTIC_EQUITY=1 is live.
    """
    mod = _load_tick_module(monkeypatch, tmp_path / "qpt_real_tu", name="qpt_real_tu")

    class _TrueUnitPos:
        asset_class = "equity"
        symbol = "AAPL"
        quantity = 100.0  # REAL signed shares (det-equity), not a NAV-fraction
        avg_entry_price = 150.0
        unit_kind = "true_unit"

    class _State:
        def get_positions(self, account_id):
            assert account_id == "paper-default"
            return {("equity", "AAPL"): _TrueUnitPos()}

    import hermes_quant.state.portfolio_state as ps_mod
    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: _State())
    gross = mod.read_real_open_positions_gross_usd(100_000.0)
    # 100sh × $150 / 100_000 = 0.15 NAV-fraction; × equity 100_000 = $15,000.
    assert gross == pytest.approx(15_000.0), (
        f"ar118: true_unit equity gross must be equity×(shares*price/nav)=$15,000, not "
        f"the Σ|quantity| phantom $10,000,000; got {gross}"
    )
    # Explicitly NOT the ~667× phantom the bare-Σ|quantity| produced.
    assert gross != pytest.approx(100_000.0 * 100.0)


def test_real_open_positions_gross_usd_empty_book_is_zero(monkeypatch, tmp_path):
    """A book with no open positions → 0.0 (byte-identical to today's behavior)."""
    mod = _load_tick_module(monkeypatch, tmp_path / "qpt_real_empty", name="qpt_real_empty")

    class _State:
        def get_positions(self, account_id):
            return {}

    import hermes_quant.state.portfolio_state as ps_mod
    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: _State())
    # equity × Σ|quantity| = equity × 0 = 0.0 regardless of the equity value.
    assert mod.read_real_open_positions_gross_usd(100_000.0) == 0.0


def test_real_open_positions_gross_usd_failure_is_fail_closed_none(monkeypatch, tmp_path):
    """Any failure reading the canonical book → None (fail-closed input)."""
    mod = _load_tick_module(monkeypatch, tmp_path / "qpt_real_fail", name="qpt_real_fail")

    def _boom():
        raise RuntimeError("state.db unreadable")

    import hermes_quant.state.portfolio_state as ps_mod
    monkeypatch.setattr(ps_mod, "get_portfolio_state", _boom)
    assert mod.read_real_open_positions_gross_usd(100_000.0) is None


def test_flag_on_counts_real_open_book_against_ceiling(monkeypatch, tmp_path):
    """RED→GREEN cap2 core: the cap ceiling is equity(1250)×gross_headroom(2.0)
    = 2500 USD. An EXISTING open book of 2000 USD already consumes most of it,
    so only ONE more 1000-USD fire (→3000) breaches but the FIRST fire fits
    (2000+1000=3000 > 2500 → breach). With the real book counted, the budget
    starts at consumed=2000, so EVERY new 1000-USD fire breaches (2000+1000>2500)
    and all 5 are silenced. Before the fix consumed started at 0 and 2 fires
    were admitted (the empty-stub under-count)."""
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_real_book_breach")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 1250.0)  # ceiling 2500
    monkeypatch.setattr(mod, "read_real_open_positions_gross_usd", lambda _equity: 2000.0)

    summary = mod.run_tick(dry_run=False)

    # Real book (2000) + any 1000 fire (=3000) > 2500 → every fire silenced.
    assert summary["fired"] == 0
    assert summary["silenced"] == 5
    assert placed == []

    silenced = [row for row in _decision_rows(mod) if row["decision"] == "silenced"]
    assert len(silenced) == 5
    for row in silenced:
        assert "portfolio_cap_aggregate_breach" in row["reason"]
        assert row["aggregate_cap_ceiling_usd"] == pytest.approx(2500.0)
        # consumed reflects the REAL book (2000), not an empty 0-stub.
        assert row["aggregate_cap_consumed_usd"] == pytest.approx(2000.0)


def test_flag_on_real_book_partial_headroom_admits_then_binds(monkeypatch, tmp_path):
    """With equity 2000 → ceiling 4000, and a real book of 1500 USD already open,
    remaining headroom is 2500 USD: the first two 1000-USD fires fit
    (1500+1000=2500, +1000=3500 ≤ 4000), the third (→4500) breaches. So 2 fire,
    3 silenced — the real book correctly eats 1.5 fires worth of headroom."""
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_real_book_partial")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 2000.0)  # ceiling 4000
    monkeypatch.setattr(mod, "read_real_open_positions_gross_usd", lambda _equity: 1500.0)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 2
    assert summary["silenced"] == 3
    assert len(placed) == 2
    # 1500 (book) + 2*1000 (fires) = 3500 ≤ 4000; a 3rd would be 4500 > 4000.
    assert 1500.0 + sum(o["notional_usd"] for o in placed) <= 4000.0


def test_flag_on_empty_book_byte_identical_to_legacy_consumed_zero(monkeypatch, tmp_path):
    """An empty real book must reproduce the legacy behavior exactly: consumed
    starts at 0, equity 1250 → ceiling 2500, two 1000-USD fires fit (→2000),
    the third would breach (→3000). 2 fire, 3 silenced — same as the original
    test_flag_on_silences_fires_that_would_breach_aggregate_cap."""
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_real_book_empty")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 1250.0)
    monkeypatch.setattr(mod, "read_real_open_positions_gross_usd", lambda _equity: 0.0)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 2
    assert summary["silenced"] == 3
    assert sum(o["notional_usd"] for o in placed) == pytest.approx(2000.0)
    silenced = [row for row in _decision_rows(mod) if row["decision"] == "silenced"]
    for row in silenced:
        assert row["aggregate_cap_consumed_usd"] == pytest.approx(2000.0)


def test_flag_on_real_book_unreadable_silences_every_fire(monkeypatch, tmp_path):
    """If the canonical open book is unreadable (None), the budget fails closed —
    silence every would-be fire rather than assuming an empty book and breaching."""
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_real_book_unreadable")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 100_000.0)
    monkeypatch.setattr(mod, "read_real_open_positions_gross_usd", lambda _equity: None)

    summary = mod.run_tick(dry_run=False)

    assert summary["fired"] == 0
    assert summary["silenced"] == 5
    assert placed == []
    assert all(
        "open_book" in row["reason"] or "portfolio_cap_aggregate_breach" in row["reason"]
        for row in _decision_rows(mod)
    )


def test_flag_on_real_book_end_to_end_no_reader_stub_counts_true_gross(monkeypatch, tmp_path):
    """End-to-end cap2: monkeypatch ONLY the canonical get_portfolio_state (NOT the
    reader), so the real read_real_open_positions_gross_usd unit math runs.

    This is the test that would have caught the dimensional under-count: with
    Position.quantity as a SIGNED NAV-FRACTION (NVDA +1.0 / TSLA -0.8 → gross 1.8)
    and equity 1250 (ceiling 2500), the TRUE gross USD = 1250 × 1.8 = 2250. Every
    1000-USD fire (2250+1000=3250 > 2500) breaches → 0 fired / 5 silenced. The
    legacy |qty|×avg×mult formula would have returned ~1.0*350+0.8*250 = 550
    (way under-counting), admitting 1 fire (550+1000=1550 < 2500, 550+2000=2550 >
    2500) — breaching the true gross ceiling."""
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP", "1")
    mod, placed = _ready_fire_run(monkeypatch, tmp_path, module_name="qpt_real_e2e")
    monkeypatch.setattr(mod, "read_alpaca_account_equity", lambda: 1250.0)  # ceiling 2500

    class _Pos:
        def __init__(self, asset_class, symbol, quantity, avg_entry_price):
            self.asset_class = asset_class
            self.symbol = symbol
            self.quantity = quantity
            self.avg_entry_price = avg_entry_price

    class _State:
        def get_positions(self, account_id):
            assert account_id == "paper-default"
            return {
                ("equity", "NVDA"): _Pos("equity", "NVDA", 1.0, 350.0),   # +1.0 NAV-frac
                ("equity", "TSLA"): _Pos("equity", "TSLA", -0.8, 250.0),  # -0.8 NAV-frac
            }

    import hermes_quant.state.portfolio_state as ps_mod
    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: _State())

    summary = mod.run_tick(dry_run=False)

    # True gross = 1250 × (|1.0| + |-0.8|) = 1250 × 1.8 = 2250; every fire breaches.
    assert summary["fired"] == 0
    assert summary["silenced"] == 5
    assert placed == []
    silenced = [row for row in _decision_rows(mod) if row["decision"] == "silenced"]
    assert len(silenced) == 5
    for row in silenced:
        assert "portfolio_cap_aggregate_breach" in row["reason"]
        assert row["aggregate_cap_ceiling_usd"] == pytest.approx(2500.0)
        assert row["aggregate_cap_consumed_usd"] == pytest.approx(2250.0)
