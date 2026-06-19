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
