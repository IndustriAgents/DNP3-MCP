# dnp3-python

Python MCP server for DNP3. Built with `uv` and `mcp[cli]` (FastMCP), it exposes the `dnp3-mcp` CLI so MCP-compatible clients can poll points, issue controls, and inspect DNP3 diagnostics.

> The DNP3 master is still a scaffold: reads return synthetic values, writes are acknowledged without sending a control, and no connection is opened. See [Status](../README.md#status) in the repository README.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Once the master is implemented: DNP3 network access (TCP or serial) and the native `pydnp3` dependencies. Neither is needed, or used, today.

## Quick Start

```bash
uv sync
DNP3_CONNECTION_TYPE=tcp DNP3_HOST=127.0.0.1 DNP3_PORT=20000 DNP3_WRITES_ENABLED=false uv run dnp3-mcp
```

Set the connection settings (connection type, host, port, addresses, timeouts) in the real environment or in the `env` block of your MCP client config. A `.env` file in `dnp3-python/` is also loaded at startup, but today only `DNP3_WRITES_ENABLED` and `POINT_MAP_FILE` take effect from it. See [Configuration](../README.md#configuration) for the full list and what each one does today.

## Layout

```
dnp3-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
└── src/dnp3_mcp
    ├── __init__.py
    ├── cli.py
    ├── server.py
    ├── tools.py
    └── dnp3_master.py
```

This scaffold wires a master wrapper (`dnp3_master.py`), FastMCP server, point-map loader, and tools for reading binary/analog points, writing outputs, polling classes, alias-based access, and ping diagnostics. The protocol-specific logic (OpenDNP3 channels, event handling, time sync, file transfer, etc.) is still to be filled in; see [Status](../README.md#status).
