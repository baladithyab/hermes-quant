"""Registry-open play loader tests (ADR-0082 Part A continuation).

After seed 0878 the playbook's derived surfaces — ``score_all`` / ``PLAY_NAMES``
/ the per-play wrappers / ``direction_bias`` — are all computed FROM
``profiles.PROFILES``. This loader adds the discovery seam: a play can be declared
externally (YAML under ``~/.hermes/quant/plays/*.yaml`` or a ``hermes_quant.plays``
entry point) and merged into ``PROFILES`` at registry-build time, mirroring the
analyst/aggregator discovery in ``daemon.discovery`` / ``recipes``.

Contract under test (all offline / deterministic — no network, no entry points
installed; YAML is read from a tmp dir):

1. Default-OFF: with ``HERMES_QUANT_PLAYS_OPEN`` unset/!="1", discovery returns
   ``{}`` and ``build_play_registry`` is byte-identical to the built-in 5.
2. A valid YAML play is discovered, scored via the PROFILES-derived ``score_all``,
   and appears in the refreshed ``PLAY_NAMES`` after install.
3. A malformed YAML file is SKIPPED (silence-by-default), not crashed.
4. A play missing ``bias`` is FAIL-CLOSED (skipped), never default-filled.

Each test that mutates the live ``PROFILES`` / ``PLAY_NAMES`` restores them in a
fixture so no state leaks across tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant.playbook import PROFILES, PlayProfile
from hermes_quant.playbook import play_loader as pl
from hermes_quant.playbook import scorers as scorers_mod
from hermes_quant.playbook import watchlist_evolution as we_mod
from hermes_quant.playbook.direction_bias import compatible_plays

_BUILTIN_NAMES: tuple[str, ...] = ("covered_call", "csp", "wheel", "leaps", "swing")


@pytest.fixture
def restore_registry():
    """Snapshot PROFILES + PLAY_NAMES; restore after the test (no leakage)."""
    saved_profiles = dict(PROFILES)
    saved_scorer_names = scorers_mod.PLAY_NAMES
    saved_we_names = we_mod.PLAY_NAMES
    try:
        yield
    finally:
        PROFILES.clear()
        PROFILES.update(saved_profiles)
        scorers_mod.PLAY_NAMES = saved_scorer_names
        we_mod.PLAY_NAMES = saved_we_names


def _valid_bearish_yaml() -> str:
    return (
        "name: bearish_breakdown\n"
        "bias: bearish\n"
        "hard_rules:\n"
        "  quote_type: [eq, EQUITY]\n"
        "  avg_dollar_volume_30d: [ge, 10000000.0]\n"
        "  last_close: [between, 10.0, 500.0]\n"
        "soft_rules:\n"
        "  rsi_14: [gt, 70.0]\n"
        "eviction_rules:\n"
        "  non_equity: [ne_field, quote_type, EQUITY]\n"
        "regime_gates:\n"
        "  bull: deny\n"
        "  bear: allow\n"
    )


def _eligible_short_snapshot() -> dict:
    return {
        "symbol": "BRKDN",
        "quote_type": "EQUITY",
        "avg_dollar_volume_30d": 2e7,
        "last_close": 50.0,
        "rsi_14": 75.0,
    }


# --------------------------------------------------------------------------- #
# 1. Default-OFF: no external plays => built-in 5 unchanged (byte-identical)
# --------------------------------------------------------------------------- #


def test_flag_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pl.PLAYS_OPEN_ENV, raising=False)
    assert pl.plays_open_enabled() is False


def test_off_state_discovery_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(pl.PLAYS_OPEN_ENV, raising=False)
    # Even with a perfectly valid YAML on disk, the OFF flag means we never read.
    (tmp_path / "valid.yaml").write_text(_valid_bearish_yaml(), encoding="utf-8")
    assert pl.discover_external_plays(user_root=tmp_path) == {}


def test_off_state_registry_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pl.PLAYS_OPEN_ENV, raising=False)
    reg = pl.build_play_registry()
    assert list(reg.keys()) == list(PROFILES.keys()) == list(_BUILTIN_NAMES)
    # Same profile objects, not copies — truly byte-identical to today.
    assert all(reg[name] is PROFILES[name] for name in PROFILES)


def test_off_state_install_is_noop(
    monkeypatch: pytest.MonkeyPatch, restore_registry: None
) -> None:
    monkeypatch.delenv(pl.PLAYS_OPEN_ENV, raising=False)
    newly = pl.install_external_plays()
    assert newly == ()
    assert tuple(PROFILES.keys()) == _BUILTIN_NAMES
    assert scorers_mod.PLAY_NAMES == _BUILTIN_NAMES
    # install always re-derives PLAY_NAMES from PROFILES, keeping the re-export
    # identity invariant from seed 0878.
    assert we_mod.PLAY_NAMES is scorers_mod.PLAY_NAMES


# --------------------------------------------------------------------------- #
# 2. A valid YAML play is discovered + scored + appears in PLAY_NAMES
# --------------------------------------------------------------------------- #


def test_valid_yaml_discovered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "bearish.yaml").write_text(_valid_bearish_yaml(), encoding="utf-8")
    loaded = pl.load_user_plays(root=tmp_path)
    assert set(loaded) == {"bearish_breakdown"}
    prof = loaded["bearish_breakdown"]
    assert isinstance(prof, PlayProfile)
    assert prof.bias == "bearish"
    # YAML lists were normalized to tuples to match the in-code rule grammar.
    assert prof.hard_rules["quote_type"] == ("eq", "EQUITY")
    assert isinstance(prof.hard_rules["avg_dollar_volume_30d"], tuple)
    # regime_gates stay a label->action string map (not tuple rules).
    assert prof.regime_gates == {"bull": "deny", "bear": "allow"}


def test_valid_yaml_installed_scored_and_in_play_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_registry: None
) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "bearish.yaml").write_text(_valid_bearish_yaml(), encoding="utf-8")

    newly = pl.install_external_plays(user_root=tmp_path)
    assert newly == ("bearish_breakdown",)

    # Flows into PROFILES (single source of truth) ...
    assert "bearish_breakdown" in PROFILES
    # ... and through the PROFILES-derived PLAY_NAMES (refreshed, last position).
    assert scorers_mod.PLAY_NAMES[-1] == "bearish_breakdown"
    assert scorers_mod.PLAY_NAMES == tuple(PROFILES.keys())
    assert we_mod.PLAY_NAMES is scorers_mod.PLAY_NAMES

    # ... and is scored by score_all with NO further code edit (seed 0878 wiring).
    out = scorers_mod.score_all(_eligible_short_snapshot())
    assert "bearish_breakdown" in out
    fit = out["bearish_breakdown"]
    assert fit.play == "bearish_breakdown"
    assert fit.pass_hard is True
    assert fit.eligible is True

    # The built-in 5 are UNAFFECTED by the addition (still present, same order).
    assert tuple(scorers_mod.PLAY_NAMES[:5]) == _BUILTIN_NAMES

    # Bias is honored: a SHORT signal may now route through the bearish play,
    # while LONG must not (silence-by-default direction routing).
    assert "bearish_breakdown" in compatible_plays(-1, scorers_mod.PLAY_NAMES)
    assert "bearish_breakdown" not in compatible_plays(1, scorers_mod.PLAY_NAMES)


def test_install_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_registry: None
) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "bearish.yaml").write_text(_valid_bearish_yaml(), encoding="utf-8")
    first = pl.install_external_plays(user_root=tmp_path)
    second = pl.install_external_plays(user_root=tmp_path)
    assert first == ("bearish_breakdown",)
    assert second == ()  # already present -> not re-installed
    # No duplicate keys.
    assert list(PROFILES.keys()).count("bearish_breakdown") == 1


# --------------------------------------------------------------------------- #
# 3. Malformed YAML is skipped (silence-by-default), not crashed
# --------------------------------------------------------------------------- #


def test_malformed_yaml_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "broken.yaml").write_text("name: oops\n  bad: : :\n - [\n", encoding="utf-8")
    # Does NOT raise — returns empty (skipped).
    loaded = pl.load_user_plays(root=tmp_path)
    assert loaded == {}


def test_malformed_among_valid_only_drops_bad(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "a_broken.yaml").write_text(": : not yaml [", encoding="utf-8")
    (tmp_path / "b_valid.yaml").write_text(_valid_bearish_yaml(), encoding="utf-8")
    loaded = pl.load_user_plays(root=tmp_path)
    # The good one survives; the bad one is silently dropped.
    assert set(loaded) == {"bearish_breakdown"}


def test_non_mapping_yaml_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    # A YAML scalar / list at top level is not a play mapping.
    (tmp_path / "scalar.yaml").write_text("just a string\n", encoding="utf-8")
    (tmp_path / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    assert pl.load_user_plays(root=tmp_path) == {}


# --------------------------------------------------------------------------- #
# 4. A play missing bias is fail-closed (skipped, never default-bullish)
# --------------------------------------------------------------------------- #


def test_missing_bias_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "nobias.yaml").write_text(
        "name: no_bias_play\nhard_rules:\n  quote_type: [eq, EQUITY]\n",
        encoding="utf-8",
    )
    loaded = pl.load_user_plays(root=tmp_path)
    assert "no_bias_play" not in loaded
    assert loaded == {}


def test_invalid_bias_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "weirdbias.yaml").write_text(
        "name: weird\nbias: sideways\nhard_rules: {}\n", encoding="utf-8"
    )
    assert pl.load_user_plays(root=tmp_path) == {}


def test_missing_bias_mapping_raises_value_error() -> None:
    # The pure converter raises (the loader converts the raise into a skip+log).
    with pytest.raises(ValueError, match="bias is required"):
        pl.play_profile_from_mapping({"name": "x", "hard_rules": {}})


def test_builtin_name_collision_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    # An external play that reuses a built-in name must NOT shadow the built-in.
    (tmp_path / "collide.yaml").write_text(
        "name: swing\nbias: agnostic\nhard_rules: {}\n", encoding="utf-8"
    )
    assert pl.load_user_plays(root=tmp_path) == {}


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        pl.play_profile_from_mapping(
            {"name": "x", "bias": "agnostic", "not_a_field": 1}
        )


def test_example_user_play_is_loadable() -> None:
    # The operator-facing template must itself be a valid play.
    prof = pl.play_profile_from_mapping(pl.example_user_play())
    assert isinstance(prof, PlayProfile)
    assert prof.bias in {"bullish", "bearish", "agnostic"}


def test_missing_play_dir_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    missing = tmp_path / "does_not_exist"
    assert pl.load_user_plays(root=missing) == {}


# --------------------------------------------------------------------------- #
# 5. Fail-closed rule-op / arity / regime-action validation (seed d45d).
#
# Before this seed a play with an unknown rule op (or wrong arity, or an unknown
# regime_gates action) LOADED — then ``score_all`` (which iterates EVERY play)
# raised ``unknown rule op`` for the whole batch, and ``score_symbol``'s blanket
# ``except -> 0.0`` silently zeroed the built-in 5, mass-evicting the watchlist.
# We now refuse such a play at LOAD (fail-closed), mirroring the bias check.
# --------------------------------------------------------------------------- #


def _malformed_rule_yaml() -> str:
    # rsi_14 uses an op the scorers (_eval_rule) do not know.
    return (
        "name: bad_op_play\n"
        "bias: bullish\n"
        "hard_rules:\n"
        "  rsi_14: [bogusop, 5]\n"
    )


def _bad_action_yaml() -> str:
    # regime_gates action 'nuke' is not in {allow,warn,deny}; _eval_regime_gate
    # would treat it as 'allow' (fails OPEN) — so we must refuse it at load.
    return (
        "name: bad_action_play\n"
        "bias: bullish\n"
        "regime_gates:\n"
        "  bull: nuke\n"
    )


def test_malformed_rule_play_rejected_at_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "badop.yaml").write_text(_malformed_rule_yaml(), encoding="utf-8")
    # Rejected at LOAD (skip+log), never installed, never reaches a scorer.
    assert pl.load_user_plays(root=tmp_path) == {}


def test_bad_regime_action_play_rejected_at_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "badact.yaml").write_text(_bad_action_yaml(), encoding="utf-8")
    assert pl.load_user_plays(root=tmp_path) == {}


def test_malformed_rule_mapping_raises_value_error() -> None:
    # The pure converter raises at LOAD (the loader turns this into a skip+log) —
    # NOT later at scoring. This is the whole point of the fix.
    with pytest.raises(ValueError, match="unknown rule op"):
        pl.play_profile_from_mapping(
            {"name": "x", "bias": "bullish", "hard_rules": {"rsi_14": ["bogusop", 5]}}
        )


def test_wrong_arity_rule_rejected() -> None:
    # 'between' needs exactly 2 args; one is a fail-closed reject at load.
    with pytest.raises(ValueError, match="op 'between' takes 2 arg"):
        pl.play_profile_from_mapping(
            {"name": "x", "bias": "bullish", "hard_rules": {"last_close": ["between", 10.0]}}
        )


def test_eviction_op_misuse_rejected() -> None:
    # A value-rule op ('lt') in an eviction field is not a valid eviction op
    # (_eval_eviction only knows *_field ops); reject at load.
    with pytest.raises(ValueError, match="eviction_rules has unknown op"):
        pl.play_profile_from_mapping(
            {
                "name": "x",
                "bias": "bullish",
                "eviction_rules": {"e": ["lt", "market_cap_usd", 5]},
            }
        )


def test_bad_regime_action_mapping_raises_value_error() -> None:
    with pytest.raises(ValueError, match="action must be one of"):
        pl.play_profile_from_mapping(
            {"name": "x", "bias": "bullish", "regime_gates": {"bull": "nuke"}}
        )


def test_composite_rules_still_validate_and_load() -> None:
    # 'or' / 'any_of' / 'in' are valid ops; their sub-rules are validated
    # recursively — a valid composite must still LOAD unchanged.
    prof = pl.play_profile_from_mapping(
        {
            "name": "composite_ok",
            "bias": "agnostic",
            "soft_rules": {
                "rsi_14": ["or", ["lt", 30.0], ["gt", 70.0]],
                "five_d_return_pct": ["any_of", ["lt", -0.1], ["gt", 0.1]],
            },
        }
    )
    assert prof.soft_rules["rsi_14"] == ("or", ("lt", 30.0), ("gt", 70.0))
    assert prof.soft_rules["five_d_return_pct"][0] == "any_of"


def test_composite_rule_with_bad_subrule_rejected() -> None:
    # A bad op nested inside an 'or' must also be caught (recursive validation).
    with pytest.raises(ValueError, match="unknown rule op"):
        pl.play_profile_from_mapping(
            {
                "name": "x",
                "bias": "bullish",
                "soft_rules": {"rsi_14": ["or", ["lt", 30.0], ["bogus", 70.0]]},
            }
        )


def test_builtin_profiles_pass_new_validation() -> None:
    # The validators are derived from the scorers the built-ins already use, so
    # every built-in profile MUST pass — a regression here means we tightened
    # past the in-code grammar (would break byte-identity of the off path).
    for name, prof in PROFILES.items():
        for field_name in ("hard_rules", "soft_rules"):
            for rule in getattr(prof, field_name).values():
                pl._validate_value_rule(rule, field_name=field_name, play_name=name)
        for rule in prof.eviction_rules.values():
            pl._validate_eviction_rule(rule, play_name=name)
        for action in prof.regime_gates.values():
            assert action in pl._VALID_REGIME_ACTIONS


def test_bad_play_on_disk_does_not_zero_builtins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_registry: None
) -> None:
    """End-to-end rail: a malformed-rule + bad-action play on disk are rejected
    at install, and ``score_all`` / ``score_symbol`` over the built-in 5 never
    raise or get silently zeroed because of an external play.
    """
    monkeypatch.setenv(pl.PLAYS_OPEN_ENV, "1")
    (tmp_path / "badop.yaml").write_text(_malformed_rule_yaml(), encoding="utf-8")
    (tmp_path / "badact.yaml").write_text(_bad_action_yaml(), encoding="utf-8")

    newly = pl.install_external_plays(user_root=tmp_path)
    assert newly == ()  # both rejected — nothing installed
    assert tuple(PROFILES.keys()) == _BUILTIN_NAMES
    assert scorers_mod.PLAY_NAMES == _BUILTIN_NAMES

    # An eligible large-cap snapshot scores covered_call fine (not zeroed).
    snap = {
        "symbol": "AAPL",
        "quote_type": "EQUITY",
        "market_cap_usd": 3e10,
        "avg_dollar_volume_30d": 2e10,
        "last_close": 190.0,
        "days_since_earnings": 30,
        "realized_vol_30d": 0.3,
        "rsi_14": 55.0,
        "distance_from_52w_high_pct": -0.05,
    }
    out = scorers_mod.score_all(snap)  # must not raise
    assert set(out) == set(_BUILTIN_NAMES)
    assert out["covered_call"].eligible is True
    assert out["covered_call"].score > 0.0
