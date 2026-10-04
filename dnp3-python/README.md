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
started from (here, `dnp3-python/.env`). Parent directories are not searched,
and real environment variables take precedence. See
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

## Layout

```
dnp3-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── src/dnp3_mcp
│   ├── __init__.py
│   ├── cli.py           # entry point: loads .env, builds the server, runs stdio
│   ├── config.py        # environment parsing, read when the server is built
│   ├── server.py        # FastMCP wiring, envelope for argument errors, version
│   ├── tools.py         # the MCP tools and the point map
│   └── dnp3_master.py   # DNP3 master on dnp3py (TCP, CROB status decoding)
└── tests/
```
