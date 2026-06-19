"""Tests for the autonomous-mode cron-writer (V03-4 / ADR-0016 §D4).

The CLI's `_autonomous_start --no-cron` path is the safe default for these
tests; the cron-creation path is tested by mocking shutil.which and
subprocess.run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from hermes_quant.cli import _autonomous_start, _create_autonomous_cron_job


@pytest.fixture
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "hermes_quant.cli.Path",
        Path,  # noop sanity
    )
    # Redirect ~/.hermes/config.yaml to tmpdir
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".hermes").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home / ".hermes" / "config.yaml"


# ---------------------------------------------------------------------------
# _create_autonomous_cron_job — happy + sad paths
# ---------------------------------------------------------------------------


def test_cron_creation_succeeds_when_hermes_on_path():
    with mock.patch("shutil.which", return_value="/usr/bin/hermes"):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="Created cron job abc123def\n",
                stderr="",
            )
            result = _create_autonomous_cron_job(cadence="15m")

    assert result["created"] is True
    assert "15m" in result["summary"]
    assert result["job_id"] == "abc123def"

    # Verify the command shape
    args, _ = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "/usr/bin/hermes"
    assert cmd[1:4] == ["cron", "create", "15m"]
    assert "--no-agent" in cmd
    assert "--script" in cmd
    script_idx = cmd.index("--script") + 1
    assert "quant autonomous tick" in cmd[script_idx]
    assert "--no-dry-run" in cmd[script_idx]


def test_cron_creation_fails_when_hermes_not_on_path():
    with mock.patch("shutil.which", return_value=None):
        result = _create_autonomous_cron_job(cadence="15m")

    assert result["created"] is False
    assert "not found on PATH" in result["reason"]


def test_cron_creation_fails_when_hermes_cron_returns_nonzero():
    with mock.patch("shutil.which", return_value="/usr/bin/hermes"):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=2,
                stdout="",
                stderr="error: invalid schedule '15m'",
            )
            result = _create_autonomous_cron_job(cadence="15m")

    assert result["created"] is False
    assert "exited 2" in result["reason"]
    assert "invalid schedule" in result["reason"]


def test_cron_creation_handles_subprocess_timeout():
    with mock.patch("shutil.which", return_value="/usr/bin/hermes"):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=15),
        ):
            result = _create_autonomous_cron_job(cadence="15m")

    assert result["created"] is False
    assert "TimeoutExpired" in result["reason"] or "Command 'hermes' timed out" in result["reason"]


def test_cron_creation_handles_oserror():
    with mock.patch("shutil.which", return_value="/usr/bin/hermes"):
        with mock.patch(
            "subprocess.run",
            side_effect=OSError("fork failed"),
        ):
            result = _create_autonomous_cron_job(cadence="15m")

    assert result["created"] is False
    assert "fork failed" in result["reason"]


def test_cron_creation_extracts_job_id_from_various_formats():
    """The cron job_id parser handles a few output shapes."""
    test_cases = [
        ("Created cron job ABC1234\n", "ABC1234"),
        ("job_id: deadbeef\n", "deadbeef"),
    ]
    for stdout, expected_id in test_cases:
        with mock.patch("shutil.which", return_value="/usr/bin/hermes"):
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(
                    returncode=0,
                    stdout=stdout,
                    stderr="",
                )
                result = _create_autonomous_cron_job(cadence="15m")
        assert result["created"] is True
        assert result["job_id"] == expected_id, f"failed for stdout={stdout!r}"


# ---------------------------------------------------------------------------
# _autonomous_start --no-cron path (the safe default for tests)
# ---------------------------------------------------------------------------


def test_autonomous_start_no_cron_skips_creation(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with mock.patch("subprocess.run") as mock_run:
        rc = _autonomous_start(
            cadence="15m",
            watchlist_str="AAPL:equity:1d",
            no_cron=True,
        )

    assert rc == 0
    # subprocess was NEVER called when --no-cron
    assert not mock_run.called
    out = capsys.readouterr().out
    assert "autonomous mode ENABLED" in out
    assert "skipped cron job creation per --no-cron" in out


def test_autonomous_start_with_cron_writes_config_and_calls_subprocess(
    tmp_path,
    monkeypatch,
    capsys,
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # ADR-0092 home-decouple (4aafaf3): get_config_path() now resolves via
    # hermes_home(), which honors HERMES_HOME BEFORE Path.home(). Under a CI home
    # that exports HERMES_HOME the monkeypatched Path.home() no longer controls the
    # config path, so pin HERMES_HOME to the same fake home the assertions expect.
    monkeypatch.setenv("HERMES_HOME", str(fake_home / ".hermes"))
    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    with mock.patch("shutil.which", return_value="/usr/bin/hermes"):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="Created cron job xyz789\n",
                stderr="",
            )
            rc = _autonomous_start(
                cadence="15m",
                watchlist_str="BTC/USDT:crypto:1h,ETH/USDT:crypto:1h",
                no_cron=False,
            )

    assert rc == 0
    # config written
    cfg_path = fake_home / ".hermes" / "config.yaml"
    assert cfg_path.exists()
    cfg_text = cfg_path.read_text()
    assert "mode: autonomous" in cfg_text
    assert "BTC/USDT" in cfg_text
    assert "ETH/USDT" in cfg_text

    # cron actually called
    assert mock_run.called
    out = capsys.readouterr().out
    assert "cron job created" in out
    assert "15m" in out


def test_autonomous_start_handles_cron_failure_gracefully(
    tmp_path,
    monkeypatch,
    capsys,
):
    """When `hermes cron create` fails, the config STILL gets written
    (autonomous mode is set; only the cron wiring is missing) and the
    operator gets clear instructions to wire it manually."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # ADR-0092 home-decouple (4aafaf3): get_config_path() now resolves via
    # hermes_home(), which honors HERMES_HOME BEFORE Path.home() — pin it to the
    # same fake home the assertions expect (see the sibling test above).
    monkeypatch.setenv("HERMES_HOME", str(fake_home / ".hermes"))
    monkeypatch.delenv("HERMES_QUANT_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    with mock.patch("shutil.which", return_value=None):  # no hermes
        rc = _autonomous_start(
            cadence="15m",
            watchlist_str="AAPL:equity:1d",
            no_cron=False,
        )

    assert rc == 0  # don't fail just because cron creation failed
    cfg_path = fake_home / ".hermes" / "config.yaml"
    assert cfg_path.exists()
    out = capsys.readouterr().out
    assert "autonomous mode ENABLED" in out
    assert "cron job creation skipped" in out
    assert "manually" in out
