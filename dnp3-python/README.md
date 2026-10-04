# dnp3-python

Python MCP server for DNP3. Built with `uv`, `mcp[cli]` (FastMCP) and the
pure-Python [dnp3py](https://pypi.org/project/dnp3py/) stack, it exposes the
`dnp3-mcp` CLI, which lets MCP-compatible clients read binary and analog
inputs, operate binary outputs and poll event classes on DNP3 outstations over
TCP. Responses include the outstation's IIN flags.

Controls are refused unless `DNP3_WRITES_ENABLED=true`. See
[Status and known limitations](../README.md#status-and-known-limitations) in
the repository README.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- TCP access to the outstation (or to a serial-to-TCP gateway). No native
  libraries are needed.

## Quick Start

```bash
uv sync
DNP3_HOST=127.0.0.1 DNP3_PORT=20000 uv run dnp3-mcp
```

Set the connection settings in the real environment, in the `env` block of
your MCP client config, or in a `.env` file in the directory the server is
started from (here, `dnp3-python/.env`; see [`.env.example`](.env.example)).
Parent directories are not searched, and real environment variables take
precedence. `DNP3_WRITES_ENABLED` is ignored (with a warning) when it comes
from that implicit `.env`; put it in the real environment or in a file you
pass explicitly with `uv run dnp3-mcp --env-file PATH`. See
[Configuration](../README.md#configuration) for every setting.

## Tests and checks

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

The tests include end-to-end runs that start `../dnp3-mock-outstation` with
this project's interpreter and drive the `dnp3-mcp` console script over stdio.
`tests/interop` (opendnp3 via yadnp3, Linux x86_64 only) runs with
`uv sync --locked --group interop` and
`REQUIRE_OPENDNP3=1 uv run --locked --group interop pytest tests/interop`.

## Layout

```
dnp3-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example
├── src/dnp3_mcp
│   ├── __init__.py
│   ├── cli.py           # entry point: --env-file / .env (no gates), builds the server, runs stdio
│   ├── config.py        # environment parsing, read when the server is built
│   ├── server.py        # FastMCP wiring, envelope for argument errors, version
│   ├── tools.py         # the MCP tools and the point map
│   └── dnp3_master.py   # DNP3 master on dnp3py (TCP, own CROB encoding, guarded confirms)
└── tests/               # unit, protocol, stdio end-to-end; tests/interop needs yadnp3
```
