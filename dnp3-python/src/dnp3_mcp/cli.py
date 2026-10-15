"""CLI entry point for the DNP3 MCP server."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from .config import ConfigError
from .server import DNP3MCPServer

# Variables that switch on something that changes a device. They are honoured
# only from the real process environment or an explicit --env-file, never from
# the implicit .env.
GATE_VARIABLES = ("DNP3_WRITES_ENABLED",)
_GATE_PATTERN = re.compile(r"_(WRITES|CMDS|COMMANDS|CONTROLS?|STATE_CHANGE)_ENABLED$")

# What the implicit .env may set: this server's own settings, nothing else
# (no PATH, PYTHON*, DYLD_*, proxies or certificate paths).
OWN_PREFIX = "DNP3_"
OWN_EXTRAS = ("POINT_MAP_FILE",)

_PROJECT_NAME = "industriagents-dnp3-mcp"


def normalise(name: str) -> str:
    """The name as Windows would see it: trimmed, upper case, without an `export ` prefix."""
    name = name.strip()
    if name[:7].lower() == "export ":
        name = name[7:].strip()
    return name.upper()


def is_gate(name: str) -> bool:
    name = normalise(name)
    return name in GATE_VARIABLES or bool(_GATE_PATTERN.search(name))


def is_own_setting(name: str) -> bool:
    name = normalise(name)
    return name.startswith(OWN_PREFIX) or name in OWN_EXTRAS


def _warn(message: str) -> None:
    # stdout belongs to the MCP protocol.
    print(f"dnp3-mcp: {message}", file=sys.stderr)


def project_env_file(module_file: Path | None = None) -> Path | None:
    """Where the implicit .env may live: `dnp3-python/.env` of a source checkout.

    It is found relative to this package's source (src/dnp3_mcp/ inside the
    dnp3-mcp project), never relative to the working directory, which may be an
    MCP client's untrusted project. An installed package (site-packages) has no
    implicit .env: use the environment or --env-file.
    """
    package_dir = (module_file or Path(__file__)).resolve().parent
    src_dir = package_dir.parent
    project_dir = src_dir.parent
    if package_dir.name != "dnp3_mcp" or src_dir.name != "src":
        return None
    pyproject = project_dir / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if not re.search(rf'^name\s*=\s*"{re.escape(_PROJECT_NAME)}"', text, re.MULTILINE):
        return None
    return project_dir / ".env"


_DEFAULT = object()


def load_env_file(env_file: Path | None | object = _DEFAULT) -> Path | None:
    """Load the implicit .env (default: `project_env_file()`), keeping only this server's own settings.

    Gate variables (DNP3_WRITES_ENABLED, in any case or spelling) and anything
    that is not a DNP3_* setting or POINT_MAP_FILE are ignored with a warning on
    stderr. Variables already set in the environment win.
    """
    path = project_env_file() if env_file is _DEFAULT else env_file
    if not isinstance(path, Path) or not path.is_file():
        return None
    for raw_name, value in dotenv_values(path).items():
        if value is None:
            continue
        name = normalise(raw_name)
        if is_gate(name):
            _warn(
                f"ignoring {raw_name.strip()} from {path}: safety switches are only read from the real environment "
                "or an explicit --env-file"
            )
            continue
        if not is_own_setting(name):
            _warn(f"ignoring {raw_name.strip()} from {path}: the implicit .env may only set DNP3_* and POINT_MAP_FILE")
            continue
        if name not in os.environ:
            os.environ[name] = value
    return path


def load_explicit_env_file(path: Path) -> Path:
    """Load an --env-file the operator named. All of its variables apply, gates included."""
    if not path.is_file():
        raise ConfigError(f"--env-file {path} does not exist or is not a file")
    load_dotenv(path, override=False)
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dnp3-mcp",
        description="DNP3 MCP server (speaks MCP over stdio).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "load settings from this file, including DNP3_WRITES_ENABLED. Without it, only dnp3-python/.env of a "
            "source checkout is read, and only for DNP3_* connection settings and POINT_MAP_FILE."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        if args.env_file is not None:
            load_explicit_env_file(args.env_file)
        else:
            load_env_file()
        server = DNP3MCPServer()
    except ConfigError as exc:
        _warn(f"configuration error: {exc}")
        raise SystemExit(2) from None
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
