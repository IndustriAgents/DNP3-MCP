"""CLI entry point for the DNP3 MCP server."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError
from .server import DNP3MCPServer


def load_env_file(directory: Path | None = None) -> Path | None:
    """Load `.env` from `directory` (default: the working directory) only.

    Parent directories are not searched, so a `.env` belonging to some other
    project higher up the tree (a suite checkout, a home directory) is never
    picked up. Variables already set in the environment win.
    """
    env_file = (directory or Path.cwd()) / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)
        return env_file
    return None


def main() -> None:
    load_env_file()
    try:
        server = DNP3MCPServer()
    except ConfigError as exc:
        # stdout belongs to the MCP protocol; diagnostics go to stderr.
        print(f"dnp3-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
