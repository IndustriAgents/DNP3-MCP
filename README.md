# DNP3 MCP Server

[![CI](https://github.com/IndustriAgents/DNP3-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/DNP3-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

Model Context Protocol tooling for the DNP3 SCADA protocol (IEEE 1815): a
Python MCP server plus a mock outstation. The goal is to let AI agents poll
points, issue controls and inspect diagnostics safely. The repository follows
the same layout as the Modbus, S7comm and EtherCAT servers in the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite.

> **Status: early scaffold.** The MCP server, its tools and point-map aliasing
> work, but the DNP3 master behind them is not implemented yet. Reads return
> synthetic values, controls are acknowledged without being sent, and no
> connection is opened to `DNP3_HOST` / `DNP3_PORT`. See [Status](#status)
> before pointing it at anything.

## Layout

```text
DNP3-MCP/                    # mirrored into IndustriConnect as DNP3-Project/
├── README.md
├── LICENSE
├── CONTRIBUTING.md          # also CODE_OF_CONDUCT.md, SECURITY.md
├── .github/                 # CI, issue/PR templates, IndustriConnect notify workflow
├── dnp3-python/             # Python MCP server (uv project, console script `dnp3-mcp`)
└── dnp3-mock-outstation/    # Mock outstation (uv project, JSON over TCP)
```

## Highlights

What works today:

- An MCP server over stdio with tools for binary and analog input reads,
  binary output writes, class polls and a `ping` diagnostic, all returning the
  suite's `{ success, data, error, meta }` envelope
- Point-map aliasing: give points friendly names in a JSON file and read or
  write them by alias
- A write switch (`DNP3_WRITES_ENABLED`) that refuses both write tools when off
- A mock outstation with simulated binary and analog points for local
  development

Planned, not implemented yet: a real DNP3 master over TCP and serial, counters,
event data from class polls, time sync, security hooks, link/IIN diagnostics,
and a mock that speaks DNP3 on the wire.

## Install

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/IndustriAgents/DNP3-MCP.git
cd DNP3-MCP
```

The server and the mock are separate uv projects. Each has its own `uv.lock`.

## Quick start

1. **Run the MCP server**

   ```bash
   cd dnp3-python
   uv sync
   DNP3_CONNECTION_TYPE=tcp DNP3_HOST=127.0.0.1 DNP3_PORT=20000 DNP3_WRITES_ENABLED=false uv run dnp3-mcp
   ```

   It speaks MCP over stdio, so on its own it simply waits for a client.

2. **Wire it into Claude Desktop (or another MCP client)**

   ```json
   {
     "mcpServers": {
       "DNP3": {
         "command": "uv",
         "args": ["--directory", "/absolute/path/to/DNP3-MCP/dnp3-python", "run", "dnp3-mcp"],
         "env": {
           "DNP3_CONNECTION_TYPE": "tcp",
           "DNP3_HOST": "127.0.0.1",
           "DNP3_PORT": "20000",
           "DNP3_WRITES_ENABLED": "false"
         }
       }
     }
   }
   ```

3. **Try it**: "Ping the DNP3 server", "Read analog inputs 0 to 3 from
   outstation 10". Remember that the values are synthetic for now.

To poke at the server without a chat client, use the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector). From
`dnp3-python/`, where step 1 left you:

```bash
npx @modelcontextprotocol/inspector uv run dnp3-mcp
```

## Tools

| Tool | Arguments | What it does today |
|------|-----------|--------------------|
| `read_binary_inputs` | `outstation_address`, `start_index`, `count` | Returns synthetic points (value = index is odd) |
| `read_analog_inputs` | `outstation_address`, `start_index`, `count` | Returns synthetic points (value = index as a float) |
| `write_binary_output` | `outstation_address`, `index`, `value` | Gated by `DNP3_WRITES_ENABLED`; acknowledged, but no control (CROB) is sent |
| `poll_class` | `outstation_address`, `event_class` | Returns timing metadata only; no events |
| `list_points` | none | Lists the aliases in the point map |
| `read_point_by_alias` | `alias` | Reads a `binary_input` or `analog_input` alias through the tools above |
| `write_point_by_alias` | `alias`, `value` | Writes a `binary_output` alias; gated by `DNP3_WRITES_ENABLED` |
| `ping` | none | Configured connection settings, write flag and alias count |

## Configuration

Set these in the environment, or in the `env` block of your MCP client config.

| Variable | Default | Notes |
|----------|---------|-------|
| `DNP3_CONNECTION_TYPE` | `tcp` | Reported by `ping`; not used to connect yet |
| `DNP3_HOST` | `127.0.0.1` | Reported by `ping`; not used to connect yet |
| `DNP3_PORT` | `20000` | Reported by `ping`; not used to connect yet |
| `DNP3_SERIAL_PORT` | `/dev/ttyUSB0` | Reported by `ping`; not used to connect yet |
| `DNP3_MASTER_ADDRESS` | `1` | Reported by `ping` |
| `DNP3_LOCAL_ADDRESS` | `1` | Parsed, not used yet |
| `DNP3_TIMEOUT` | `5000` | Milliseconds; parsed, not used yet |
| `DNP3_MAX_RETRIES` | `3` | Parsed, not used yet |
| `DNP3_WRITES_ENABLED` | `true` | `false` refuses `write_binary_output` and `write_point_by_alias`. Set it to `false` unless you are working on the write path |
| `POINT_MAP_FILE` | unset | Path to a JSON point map (see below) |

At startup the server also loads the nearest `.env` file, searching upward
from `dnp3-python/src/dnp3_mcp/` (in a checkout, `dnp3-python/.env`). Today
only `DNP3_WRITES_ENABLED` and `POINT_MAP_FILE` take effect from it, because
the connection settings are read before the file is loaded. Put the others in
the real environment.

### Point map

`POINT_MAP_FILE` points at a JSON object mapping an alias to a point. `type` is
one of `binary_input`, `analog_input` or `binary_output`. The file is re-read
when it changes.

```json
{
  "feeder1_breaker_closed": { "outstation": 10, "type": "binary_input", "index": 0, "description": "Feeder 1 breaker closed" },
  "feeder1_current": { "outstation": 10, "type": "analog_input", "index": 3, "description": "Feeder 1 phase A current" },
  "feeder1_trip": { "outstation": 10, "type": "binary_output", "index": 0, "description": "Feeder 1 trip relay" }
}
```

## Mock outstation

From the repository root:

```bash
cd dnp3-mock-outstation
uv sync
uv run dnp3-mock-outstation        # 127.0.0.1:7300; --port or MOCK_DNP3_PORT to change
```

The mock is a stand-in, not a DNP3 stack. It answers newline-delimited JSON
commands over TCP (point reads, binary output writes, class polls), and the
MCP server does not connect to it yet. See
[dnp3-mock-outstation/README.md](dnp3-mock-outstation/README.md) for the
command format.

## Status

This is scaffolding, kept in step with the rest of the suite so that the
protocol work can follow:

- `dnp3-python/src/dnp3_mcp/dnp3_master.py` is a placeholder. It never opens a
  channel, so every read is generated and every write is a no-op that reports
  success. `pydnp3` is not a declared dependency. If it is installed anyway,
  the master tries to create an OpenDNP3 manager at startup, but it still
  opens no channel, and that path is untested.
- The mock speaks JSON, not DNP3, so even a finished master will need the mock
  extended (for example with an OpenDNP3 outstation) before the two can talk.
- There is no test suite yet. CI checks that both projects install from their
  lockfiles and compile, and that the server answers an MCP `initialize`,
  `tools/list` and `ping`.

Contributions that move any of this forward are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md).

## Part of IndustriConnect

This server is one of the protocol servers in
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite
of MCP servers for industrial automation (Modbus, MQTT/Sparkplug B, OPC UA,
BACnet, DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET and S7comm), each with
a mock device and the same response envelope.

IndustriConnect includes this repository as its `DNP3-Project` git submodule,
tracking `main`. Development happens here: open issues and pull requests
against `IndustriAgents/DNP3-MCP`. To get the whole suite instead:

```bash
git clone --recurse-submodules https://github.com/IndustriAgents/IndustriConnect.git
```

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, the checks CI runs and the
conventions shared with the rest of the suite. Please also read the
[Code of Conduct](CODE_OF_CONDUCT.md).

This server is meant to sit between a language model and SCADA equipment.
[SECURITY.md](SECURITY.md) explains how to run it safely and how to report a
vulnerability privately. Please do not open a public issue for one.

## License

MIT, see [LICENSE](LICENSE).
