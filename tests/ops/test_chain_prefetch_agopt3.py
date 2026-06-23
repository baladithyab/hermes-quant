"""agopt3: the options-chain pre-fetch script (aegis-chain-prefetch.py).

The WRITER half P8 found agent-codeable (analogous to bf76b). Tested OFFLINE with an
injected reader — the live fetch (creds+network) and the N>=100 eval window remain
operator/run-time. Pins: (1) it drives fetch_chain_live per optionable symbol;
(2) LiveChainDisabled => whole batch inert (silence-by-default, exit 0);
(3) one bad symbol is SKIPPED, never aborts the batch (fail-soft).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes_quant.options.data import LiveChainDisabled

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "aegis-chain-prefetch.py"


def _load():
    spec = importlib.util.spec_from_file_location("aegis_chain_prefetch_x", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _OkReader:
    def __init__(self):
        self.calls = []

    def fetch_chain_live(self, underlying: str):
        self.calls.append(underlying)
        return object()  # agperc3 writes the parquet; the script only counts the call


class _DisabledReader:
    def fetch_chain_live(self, underlying: str):
        raise LiveChainDisabled("HERMES_QUANT_OPTIONS_LIVE_CHAIN!=1")


class _FlakyReader:
    """Fails on BBB, succeeds otherwise — proves one bad symbol doesn't abort the batch."""

    def __init__(self):
        self.calls = []

    def fetch_chain_live(self, underlying: str):
        self.calls.append(underlying)
        if underlying == "BBB":
            raise RuntimeError("provider 500 on BBB")
        return object()


# ---------------------------------------------------------------------------
# aegis-ra-home2 (ADR-0092 home-decouple residue): the prefetch WRITER must
# target the SAME option_chains dir the replay READER reads. The reader defaults
# its chains_dir to hermes_quant.home.quant_home() / "option_chains"
# (= <home>/quant/option_chains). Pre-fix, the prefetch's _home_path returned
# <home>/.hermes (or a bare $HERMES_HOME) and appended option_chains, writing to
# a DIFFERENT dir the monitor sweeps never read. RED-proof: under an injected
# HERMES_QUANT_HOME the writer dir resolved to ~/.hermes/option_chains, NOT the
# injected <home>/option_chains the reader uses.
# ---------------------------------------------------------------------------


def test_prefetch_chains_dir_honors_hermes_quant_home(monkeypatch, tmp_path):
    """An injected HERMES_QUANT_HOME redirects the writer's chains dir to the
    SAME quant root the replay reader resolves, NOT ~/.hermes."""
    from hermes_quant.home import quant_home

    monkeypatch.delenv("HERMES_HOME", raising=False)
    inj = tmp_path / "injected_quant_root"
    monkeypatch.setenv("HERMES_QUANT_HOME", str(inj))

    mod = _load()
    writer_dir = mod._chains_dir(None)
    assert writer_dir == inj / "option_chains"
    assert (Path.home() / ".hermes") not in writer_dir.parents


def test_prefetch_writer_dir_equals_reader_default_formula(monkeypatch, tmp_path):
    """The whole point of the fix: the writer dir equals the reader's DOCUMENTED
    default formula (quant_home() / "option_chains") under the SAME home, so a
    successful prefetch is visible to the replay/monitor sweeps. We compare the
    writer dir against the live quant_home() formula the reader's default uses
    (data.py binds its default at import, so a fresh process gives both the same
    env-resolved root — this asserts the FORMULAS match, not a stale instance)."""
    from hermes_quant.home import quant_home

    monkeypatch.delenv("HERMES_HOME", raising=False)
    inj = tmp_path / "shared_root"
    monkeypatch.setenv("HERMES_QUANT_HOME", str(inj))

    mod = _load()
    writer_dir = mod._chains_dir(None)
    reader_default_formula = quant_home() / "option_chains"
    assert writer_dir == reader_default_formula
    assert writer_dir == inj / "option_chains"


def test_prefetch_chains_dir_honors_hermes_home(monkeypatch, tmp_path):
    """HERMES_HOME points at the hermes home; the chains dir is
    <HERMES_HOME>/quant/option_chains (quant root under the hermes home)."""
    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    hhome = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hhome))

    mod = _load()
    assert mod._chains_dir(None) == hhome / "quant" / "option_chains"


def test_prefetch_chains_dir_byte_identical_without_env(monkeypatch):
    """Parity: no env -> EXACTLY ~/.hermes/quant/option_chains."""
    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    mod = _load()
    assert mod._chains_dir(None) == Path.home() / ".hermes" / "quant" / "option_chains"


def test_prefetch_drives_fetch_per_symbol(tmp_path):
    mod = _load()
    reader = _OkReader()
    summary = mod.prefetch_chains(home=tmp_path, reader=reader, symbols=["AAA", "BBB", "CCC"])
    assert reader.calls == ["AAA", "BBB", "CCC"]
    assert summary["fetched"] == 3
    assert summary["skipped"] == 0
    assert summary["disabled"] is False


def test_live_chain_disabled_is_inert(tmp_path):
    mod = _load()
    summary = mod.prefetch_chains(home=tmp_path, reader=_DisabledReader(), symbols=["AAA", "BBB"])
    assert summary["disabled"] is True
    assert summary["fetched"] == 0
    # exit 0 (not an error) via main()
    rc = mod.main(["--home", str(tmp_path), "--symbols", "AAA,BBB"])
    # NB: main() with no injected reader builds a real ChainSnapshotReader, which is
    # disabled (no flag/creds in the test env) -> also inert, exit 0.
    assert rc == 0


def test_one_bad_symbol_is_skipped_not_aborted(tmp_path):
    mod = _load()
    reader = _FlakyReader()
    summary = mod.prefetch_chains(home=tmp_path, reader=reader, symbols=["AAA", "BBB", "CCC"])
    # BBB failed but AAA + CCC still fetched (batch not aborted).
    assert reader.calls == ["AAA", "BBB", "CCC"]
    assert summary["fetched"] == 2
    assert summary["skipped"] == 1
    assert any("BBB" in e for e in summary["errors"])
