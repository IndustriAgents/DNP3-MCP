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
# an implicitly discovered .env: when an MCP client launches the server, the
# working directory may be the client's (untrusted) project.
GATE_VARIABLES = ("DNP3_WRITES_ENABLED",)
_GATE_PATTERN = re.compile(r"_(WRITES|CMDS|COMMANDS|CONTROLS?|STATE_CHANGE)_ENABLED$")


def is_gate(name: str) -> bool:
    return name in GATE_VARIABLES or bool(_GATE_PATTERN.search(name))


def _warn(message: str) -> None:
    # stdout belongs to the MCP protocol.
    print(f"dnp3-mcp: {message}", file=sys.stderr)


def load_env_file(directory: Path | None = None) -> Path | None:
    """Load `.env` from `directory` (default: the working directory) only, without its gates.

    Parent directories are not searched, so a `.env` belonging to some other
    project higher up the tree is never picked up. Variables already set in
    the environment win. Gate variables (DNP3_WRITES_ENABLED) found here are
    ignored with a warning on stderr.
    """
    env_file = (directory or Path.cwd()) / ".env"
    if not env_file.is_file():
        return None
    for name, value in dotenv_values(env_file).items():
        if value is None or name in os.environ:
            continue
        if is_gate(name):
            _warn(
                f"ignoring {name} from {env_file}: safety switches are only read from the real environment "
                "or an explicit --env-file"
            )
            continue
        os.environ[name] = value
    return env_file


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
            "load settings from this file, including DNP3_WRITES_ENABLED. Without it, ./.env is read "
            "for connection settings only."
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
