from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from hermes_quant.pdr_core.aegis_contract_bridge import (
    ENABLE_ENV,
    SRC_ENV,
    probe_aegis_contracts,
)


def test_aegis_contract_bridge_default_off_does_not_import(monkeypatch) -> None:
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    sys.modules.pop("aegis", None)
    sys.modules.pop("aegis.contracts", None)

    status = probe_aegis_contracts({})

    assert status.enabled is False
    assert status.loaded is False
    assert "not enabled" in status.reason
    assert "aegis.contracts" not in sys.modules


def test_aegis_contract_bridge_reports_missing_package_when_enabled(monkeypatch) -> None:
    from hermes_quant.pdr_core import aegis_contract_bridge as bridge

    monkeypatch.delenv(SRC_ENV, raising=False)
    sys.modules.pop("aegis", None)
    sys.modules.pop("aegis.contracts", None)
    monkeypatch.setattr(
        bridge.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )

    status = probe_aegis_contracts({ENABLE_ENV: "1"})

    assert status.enabled is True
    assert status.loaded is False
    assert status.reason == "aegis.contracts import failed"
    assert status.errors


def test_aegis_contract_bridge_can_load_explicit_source_checkout(tmp_path) -> None:
    package_root = tmp_path / "src"
    package = package_root / "aegis"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    source_contracts = Path(__file__).resolve().parents[2] / "hermes_quant" / "pdr_core" / "contracts.py"
    shutil.copy2(source_contracts, package / "contracts.py")
    sys.modules.pop("aegis", None)
    sys.modules.pop("aegis.contracts", None)

    status = probe_aegis_contracts({ENABLE_ENV: "1", SRC_ENV: str(package_root)})

    assert status.enabled is True
    assert status.loaded is True
    assert status.reason == "ok"
    assert status.module == "aegis.contracts"
    assert status.source and str(package_root) in status.source
    assert status.errors == ()


def test_aegis_contract_bridge_detects_contract_drift(tmp_path) -> None:
    package_root = tmp_path / "src"
    package = package_root / "aegis"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    source_contracts = Path(__file__).resolve().parents[2] / "hermes_quant" / "pdr_core" / "contracts.py"
    text = source_contracts.read_text(encoding="utf-8")
    text = text.replace(
        'POSITION_LADDER: frozenset[float] = frozenset(\n    {0.0, 0.05, -0.05, 0.10, -0.10, 0.15, -0.15, 0.20, -0.20}\n)',
        "POSITION_LADDER: frozenset[float] = frozenset({0.0})",
    )
    (package / "contracts.py").write_text(text, encoding="utf-8")
    sys.modules.pop("aegis", None)
    sys.modules.pop("aegis.contracts", None)

    status = probe_aegis_contracts({ENABLE_ENV: "1", SRC_ENV: str(package_root)})

    assert status.enabled is True
    assert status.loaded is False
    assert status.reason == "contract parity mismatch"
    assert any("POSITION_LADDER differs" in error for error in status.errors)


def test_aegis_contract_bridge_loads_sibling_repo_when_present() -> None:
    sibling_src = Path(__file__).resolve().parents[3] / "aegis" / "src"
    if not sibling_src.is_dir():
        pytest.skip("sibling aegis checkout is not present")
    sys.modules.pop("aegis", None)
    sys.modules.pop("aegis.contracts", None)

    status = probe_aegis_contracts({ENABLE_ENV: "1", SRC_ENV: str(sibling_src)})

    assert status.enabled is True
    assert status.loaded is True
    assert status.reason == "ok"
