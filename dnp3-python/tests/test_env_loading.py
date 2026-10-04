"""When and where configuration is read (known issues 5 and 6)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import dnp3_mcp
from dnp3_mcp import cli
from dnp3_mcp.server import DNP3MCPServer


def test_environment_is_read_when_the_server_is_built_not_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    # dnp3_mcp (and dnp3_master) are already imported at this point.
    assert dnp3_mcp.DNP3MCPServer is DNP3MCPServer
    monkeypatch.setenv("DNP3_HOST", "192.0.2.10")
    monkeypatch.setenv("DNP3_PORT", "20002")
    monkeypatch.setenv("DNP3_WRITES_ENABLED", "true")
    server = DNP3MCPServer()
    assert server.master.config.host == "192.0.2.10"
    assert server.master.config.port == 20002
    assert server.master.config.writes_enabled is True


def test_env_file_in_working_directory_applies_to_every_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("DNP3_PORT=7301\nDNP3_HOST=192.0.2.20\nDNP3_WRITES_ENABLED=true\n")
    monkeypatch.chdir(tmp_path)
    assert cli.load_env_file() == tmp_path / ".env"
    server = DNP3MCPServer()
    # Connection settings and the write switch come from the same read.
    assert (server.master.config.host, server.master.config.port) == ("192.0.2.20", 7301)
    assert server.master.config.writes_enabled is True
    assert server.config.writes_enabled is True


def test_env_file_in_a_parent_directory_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("DNP3_WRITES_ENABLED=true\nDNP3_PORT=7302\n")
    child = tmp_path / "dnp3-python"
    child.mkdir()
    monkeypatch.chdir(child)
    assert cli.load_env_file() is None
    assert "DNP3_WRITES_ENABLED" not in os.environ
    assert DNP3MCPServer().master.config.writes_enabled is False


def test_real_environment_wins_over_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("DNP3_WRITES_ENABLED=true\n")
    monkeypatch.setenv("DNP3_WRITES_ENABLED", "false")
    cli.load_env_file(tmp_path)
    assert DNP3MCPServer().master.config.writes_enabled is False


def test_bad_configuration_exits_with_a_message_on_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DNP3_WRITES_ENABLED", "maybe")
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DNP3_WRITES_ENABLED" in captured.err
