"""When and where configuration is read (known issues 5 and 6; suite rules 3, 3a, 3b, 3c)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import dnp3_mcp
from dnp3_mcp import cli
from dnp3_mcp.server import DNP3MCPServer

PROJECT_DIR = Path(__file__).resolve().parents[1]  # dnp3-python/

# Keys the tests may write to os.environ through load_env_file; cleared and restored around each test.
TOUCHED = (
    "DNP3_HOST",
    "DNP3_PORT",
    "DNP3_TIMEOUT",
    "DNP3_WRITES_ENABLED",
    "POINT_MAP_FILE",
    "PYTHONWARNINGS",
    "DYLD_INSERT_LIBRARIES",
    "LD_PRELOAD",
    "SSL_CERT_FILE",
    "HTTPS_PROXY",
    "SOMETHING_ELSE",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in TOUCHED:
        monkeypatch.delenv(name, raising=False)
    path = os.environ.get("PATH", "")
    yield
    os.environ["PATH"] = path


@pytest.fixture
def no_project_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the implicit .env location at an empty directory, so a developer's own file cannot interfere."""
    location = tmp_path / "project" / ".env"
    monkeypatch.setattr(cli, "project_env_file", lambda module_file=None: location)
    return location


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


# -- where the implicit .env may be (rule 3c) ----------------------------------


def test_implicit_env_file_belongs_to_the_source_checkout() -> None:
    assert cli.project_env_file() == PROJECT_DIR / ".env"


def test_installed_package_has_no_implicit_env_file(tmp_path: Path) -> None:
    package = tmp_path / "lib" / "python3.11" / "site-packages" / "dnp3_mcp"
    package.mkdir(parents=True)
    # Even with a dnp3-mcp pyproject.toml where a checkout would have one, site-packages is not src/.
    for directory in (package.parent, package.parent.parent):
        (directory / "pyproject.toml").write_text('[project]\nname = "industriagents-dnp3-mcp"\n')
    assert cli.project_env_file(package / "cli.py") is None


def test_other_projects_are_not_mistaken_for_the_checkout(tmp_path: Path) -> None:
    package = tmp_path / "src" / "dnp3_mcp"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "something-else"\n')
    assert cli.project_env_file(package / "cli.py") is None
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "industriagents-dnp3-mcp"\n')
    assert cli.project_env_file(package / "cli.py") == tmp_path / ".env"


def test_env_file_in_the_working_directory_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_project_env: Path
) -> None:
    work = tmp_path / "client-project"
    work.mkdir()
    (work / ".env").write_text("DNP3_HOST=192.0.2.66\nDNP3_PORT=7301\n")
    monkeypatch.chdir(work)
    assert cli.load_env_file() is None
    assert "DNP3_HOST" not in os.environ
    assert DNP3MCPServer().master.config.host == "127.0.0.1"


def test_env_file_in_a_parent_directory_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_project_env: Path
) -> None:
    (tmp_path / ".env").write_text("DNP3_HOST=192.0.2.30\nDNP3_PORT=7302\n")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    assert cli.load_env_file() is None
    assert "DNP3_HOST" not in os.environ
    assert DNP3MCPServer().master.config.host == "127.0.0.1"


def test_project_env_file_is_used_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_project_env: Path
) -> None:
    no_project_env.parent.mkdir(parents=True)
    no_project_env.write_text("DNP3_PORT=7305\n")
    monkeypatch.chdir(tmp_path)
    assert cli.load_env_file() == no_project_env
    assert DNP3MCPServer().master.config.port == 7305


# -- what the implicit .env may set (rules 3, 3a, 3b) --------------------------


def test_implicit_env_file_sets_connection_settings_but_not_the_write_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DNP3_PORT=7301\nDNP3_HOST=192.0.2.20\nDNP3_WRITES_ENABLED=true\n")
    assert cli.load_env_file(env_file) == env_file
    server = DNP3MCPServer()
    assert (server.master.config.host, server.master.config.port) == ("192.0.2.20", 7301)
    assert server.master.config.writes_enabled is False
    assert "DNP3_WRITES_ENABLED" not in os.environ
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ignoring DNP3_WRITES_ENABLED" in captured.err


@pytest.mark.parametrize(
    "line",
    [
        "dnp3_writes_enabled=true",
        "Dnp3_Writes_Enabled=true",
        "export DNP3_WRITES_ENABLED=true",
        "export dnp3_writes_enabled=1",
        'DNP3_WRITES_ENABLED="true"',
        "DNP3_WRITES_ENABLED='yes'",
        "  DNP3_WRITES_ENABLED = on  ",
    ],
)
def test_gate_spellings_are_all_ignored(tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str) -> None:
    # Windows upper-cases environment variable names, so case must not matter.
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\nDNP3_PORT=7306\n")
    cli.load_env_file(env_file)
    assert not any(name.upper() == "DNP3_WRITES_ENABLED" for name in os.environ)
    server = DNP3MCPServer()
    assert server.master.config.writes_enabled is False
    assert server.master.config.port == 7306
    assert "ignoring" in capsys.readouterr().err


def test_own_settings_are_normalised(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("dnp3_host=192.0.2.44\nexport DNP3_TIMEOUT=1500\n")
    cli.load_env_file(env_file)
    config = DNP3MCPServer().master.config
    assert (config.host, config.timeout_ms) == ("192.0.2.44", 1500)


def test_foreign_variables_are_ignored(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / ".env"
    points = tmp_path / "points.json"
    env_file.write_text(
        "PYTHONWARNINGS=error\n"
        "DYLD_INSERT_LIBRARIES=/tmp/evil.dylib\n"
        "LD_PRELOAD=/tmp/evil.so\n"
        "SSL_CERT_FILE=/tmp/ca.pem\n"
        "HTTPS_PROXY=http://proxy.invalid:3128\n"
        "PATH=/tmp/evil\n"
        "SOMETHING_ELSE=1\n"
        f"POINT_MAP_FILE={points}\n"
    )
    cli.load_env_file(env_file)
    for name in ("PYTHONWARNINGS", "DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "SSL_CERT_FILE", "HTTPS_PROXY"):
        assert name not in os.environ, name
    assert os.environ.get("PATH") != "/tmp/evil"
    assert os.environ["POINT_MAP_FILE"] == str(points)  # a documented extra
    err = capsys.readouterr().err
    for name in ("PYTHONWARNINGS", "DYLD_INSERT_LIBRARIES", "SSL_CERT_FILE", "HTTPS_PROXY", "PATH", "SOMETHING_ELSE"):
        assert f"ignoring {name} " in err, name


@pytest.mark.parametrize("name", ["DNP3_WRITES_ENABLED", "dnp3_writes_enabled", "DNP3_SYSTEM_CMDS_ENABLED"])
def test_gate_variables_are_recognised(name: str) -> None:
    assert cli.is_gate(name)
    assert not cli.is_gate("DNP3_PORT") and not cli.is_gate("DNP3_CONTROL_MODE")


def test_real_environment_wins_over_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DNP3_PORT=7304\n")
    monkeypatch.setenv("DNP3_PORT", "20005")
    cli.load_env_file(env_file)
    assert DNP3MCPServer().master.config.port == 20005


# -- explicit --env-file --------------------------------------------------------


def test_explicit_env_file_may_enable_writes(tmp_path: Path) -> None:
    env_file = tmp_path / "dnp3.env"
    env_file.write_text("DNP3_WRITES_ENABLED=true\nDNP3_PORT=7303\n")
    assert cli.parse_args(["--env-file", str(env_file)]).env_file == env_file
    cli.load_explicit_env_file(env_file)
    server = DNP3MCPServer()
    assert server.master.config.writes_enabled is True
    assert server.master.config.port == 7303


def test_missing_explicit_env_file_is_a_configuration_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--env-file", str(tmp_path / "nope.env")])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--env-file" in captured.err


def test_bad_configuration_exits_with_a_message_on_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_project_env: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DNP3_WRITES_ENABLED", "maybe")
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DNP3_WRITES_ENABLED" in captured.err
