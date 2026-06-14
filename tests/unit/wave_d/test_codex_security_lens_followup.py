"""Lock-in tests for codex security+invariant lens findings (2026-05-26).

Three P2 findings landed in [hash-tbd]; these tests guard against
regression.

Vestigial-daemon-spine deletion: the deterministic-signal_id findings
(``tick_loop._build_signal_record`` + the end-to-end ``run_one_tick`` replay
parity) were pinned here. ``daemon/tick_loop.py`` drove the vestigial daemon →
signals.jsonl → freqtrade spine (the live spine is cron scripts calling
advisor.recommend + reactors directly), so that module — and the two test
classes that exercised it — were removed. The DeliberativeConfig-keyword guard
(which pins the LIVE ``quant-playbook-tick.py`` cron) and the quant_doctor
cold-start halt-visibility guard (which pins the KEPT halt_state + tools mirror)
stay.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.tools import _compute_daemon_state_mirror

# ---------------------------------------------------------------------------
# Fix #1 — DeliberativeConfig keyword: enable_risk_mgmt (NOT include_risk_mgmt)
# ---------------------------------------------------------------------------


class TestDeliberativeConfigKeyword:
    """Codex P2 finding: quant-playbook-tick.py was passing
    `include_risk_mgmt=...` to `DeliberativeConfig`, but the dataclass
    field is `enable_risk_mgmt`. Calling with the wrong kwarg raises
    TypeError, breaking the deliberative path silently before
    `run_llm_committee()` could run.

    These tests pin the canonical field name so a future rename forces
    every caller to update.
    """

    def test_canonical_field_name_is_enable_risk_mgmt(self):
        from hermes_quant.aggregators.deliberative import DeliberativeConfig

        # Must accept enable_risk_mgmt without error.
        cfg = DeliberativeConfig(enable_llm_turns=True, enable_risk_mgmt=True)
        assert cfg.enable_risk_mgmt is True

    def test_include_risk_mgmt_is_NOT_a_field(self):  # noqa: N802 — capital NOT is deliberate emphasis
        """If a future contributor reintroduces an `include_risk_mgmt`
        alias, they must add this test deliberately. The dataclass is
        frozen + slots-strict, so unknown kwargs raise TypeError."""
        from hermes_quant.aggregators.deliberative import DeliberativeConfig

        with pytest.raises(TypeError, match="include_risk_mgmt"):
            DeliberativeConfig(  # type: ignore[call-arg]
                enable_llm_turns=True, include_risk_mgmt=True
            )

    def test_playbook_tick_uses_canonical_keyword(self):
        """Static check: ops/scripts/quant-playbook-tick.py must spell
        the keyword `enable_risk_mgmt`. The runtime copy at
        ~/.hermes/scripts/ is exercised by the cron and is synced by
        the same commits, so we only check the source-of-truth path
        here."""
        repo_root = Path(__file__).resolve().parents[3]
        path = repo_root / "ops" / "scripts" / "quant-playbook-tick.py"
        text = path.read_text()
        assert "enable_risk_mgmt=" in text, (
            "playbook-tick.py must pass enable_risk_mgmt=... to "
            "DeliberativeConfig"
        )
        assert "include_risk_mgmt=" not in text, (
            "playbook-tick.py must NOT use include_risk_mgmt= — that "
            "kwarg does not exist on DeliberativeConfig and raises "
            "TypeError"
        )


# ---------------------------------------------------------------------------
# Fix #4 — quant_doctor cold-start halt visibility
# ---------------------------------------------------------------------------


class TestQuantDoctorColdStartHaltVisibility:
    """Codex P2 finding: when the daemon hasn't created signals.jsonl yet
    but state.db already contains an active halt, the early return at
    tools.py:1139 reported `halts: []` — hiding the safety state in
    exactly the cold-start scenario the diagnostic is supposed to cover.

    Fixed by replacing the hard early-return with a `bus_present` flag:
    when bus is absent, per_symbol/heartbeat probes are skipped (they
    need bus rows) but halt + pending probes still run.
    """

    def test_halts_visible_even_when_bus_absent(self, tmp_path: Path):
        # Set up: no signal bus, but state.db has an active halt.
        bus = tmp_path / "signals.jsonl"
        assert not bus.exists()
        state_db = tmp_path / "state.db"
        halt_mirror = tmp_path / "halt.json"

        # Create a halt registry with one active halt.
        hs = HaltStateSQLite(db_path=state_db, mirror_path=halt_mirror)
        hs.add_halt(
            account_id="acct-test",
            asset_class="crypto",
            asset=None,  # account-wide halt
            reason="operator_emergency_stop",
            halted_until=None,
        )
        active = hs.active_halts()
        assert len(active) == 1, "fixture: halt registry should have 1 row"

        # Run quant_doctor — should surface the halt despite missing bus.
        out = _compute_daemon_state_mirror(
            signal_bus_path=bus,
            state_db_path=state_db,
            halt_mirror_path=halt_mirror,
        )

        # Per-symbol/heartbeat are empty (correct — no bus rows).
        assert out["per_symbol"] == {}
        assert out["last_heartbeat_age_s"] is None
        # Halts are NOT empty.
        assert len(out["halts"]) == 1
        assert out["halts"][0]["reason"] == "operator_emergency_stop"
        assert out["halts"][0]["account_id"] == "acct-test"
        # Cold-start note is still surfaced for human callers.
        assert out.get("note") == "signal bus does not exist yet"

    def test_no_halts_no_bus_returns_empty_halts_with_note(self, tmp_path: Path):
        """Symmetry: no bus AND no halt registry → empty halts AND note."""
        bus = tmp_path / "signals.jsonl"
        out = _compute_daemon_state_mirror(
            signal_bus_path=bus,
            state_db_path=tmp_path / "nonexistent.db",
            halt_mirror_path=tmp_path / "nonexistent.json",
        )
        assert out["halts"] == []
        assert out.get("note") == "signal bus does not exist yet"
