# DNP3 MCP Server

[![CI](https://github.com/IndustriAgents/DNP3-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/DNP3-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

Model Context Protocol tooling for the DNP3 SCADA protocol (IEEE 1815): a
Python MCP server that acts as a DNP3 master over TCP, plus a mock outstation
that speaks DNP3 so the whole path can be exercised without a plant. The
repository follows the same layout as the Modbus, S7comm and EtherCAT servers
in the [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect)
suite.

> **Read-only by default.** Controls (CROBs) are refused unless you set
> `DNP3_WRITES_ENABLED=true`. When they are enabled, a control is reported as
> successful only if the outstation answers with status `SUCCESS`. See
> [SECURITY.md](SECURITY.md) before pointing the server at real equipment.

## Layout

```text
DNP3-MCP/                    # shipped inside IndustriConnect as DNP3-Project/
├── README.md
├── LICENSE
├── CONTRIBUTING.md          # also CODE_OF_CONDUCT.md, SECURITY.md
├── .github/                 # CI, issue/PR templates, IndustriConnect notify workflow
├── dnp3-python/             # Python MCP server (uv project, console script `dnp3-mcp`)
└── dnp3-mock-outstation/    # Mock outstation (uv project, DNP3 over TCP)
```

## Highlights

- A DNP3 master over TCP built on [dnp3py](https://pypi.org/project/dnp3py/),
  a pure-Python IEEE 1815-2012 stack (no native dependencies). Every tool call
  opens a connection, resets the link, runs its exchange and closes again.
- Tools for binary and analog input reads, binary output controls
  (select-before-operate or direct operate), Class 0/1/2/3 polls and a `ping`
  diagnostic, all returning the suite's `{ success, data, error, meta }`
  envelope. `meta.iin` carries the outstation's internal indications.
- Honest results: values come from the outstation during the call; a read that
  is missing an index, a control the outstation rejects, a timeout or a broken
  link all come back as `success: false` with a reason. Bad arguments come
  back the same way.
- Point-map aliasing: give points friendly names in a JSON file and read or
  write them by alias.
- A mock outstation with 16 binary inputs, 8 analog inputs and 8 binary
  outputs that answers real DNP3 requests, including controls and event polls.

Not implemented: serial links (use a serial-to-TCP gateway), DNP3 Secure
Authentication, TLS, counters and analog outputs as dedicated tools (they do
appear in Class 0 polls), time synchronisation, file transfer and unsolicited
reporting.

## Install

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/IndustriAgents/DNP3-MCP.git
cd DNP3-MCP
```

The server and the mock are separate uv projects. Each has its own `uv.lock`.

## Quick start

1. **Start the mock outstation** (DNP3 on `127.0.0.1:20000`, outstation
   address `10`):

   ```bash
   cd dnp3-mock-outstation
   uv sync
   uv run dnp3-mock-outstation
   ```

2. **Run the MCP server** in a second terminal, from the repository root:

   ```bash
   cd dnp3-python
   uv sync
   DNP3_HOST=127.0.0.1 DNP3_PORT=20000 uv run dnp3-mcp
   ```

   It speaks MCP over stdio, so on its own it simply waits for a client.

3. **Wire it into Claude Desktop (or another MCP client)**

   ```json
   {
     "mcpServers": {
       "DNP3": {
         "command": "uv",
         "args": ["--directory", "/absolute/path/to/DNP3-MCP/dnp3-python", "run", "dnp3-mcp"],
         "env": {
           "DNP3_HOST": "127.0.0.1",
           "DNP3_PORT": "20000"
         }
       }
     }
   }
   ```

   Add `"DNP3_WRITES_ENABLED": "true"` only when you want the agent to be able
   to operate outputs.

4. **Try it**: "Ping the DNP3 server", "Read binary inputs 0 to 3 from
   outstation 10", "Poll class 2 events from outstation 10".

To poke at the server without a chat client, use the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector). From
`dnp3-python/`, where step 2 left you:

```bash
npx @modelcontextprotocol/inspector uv run dnp3-mcp
```

## Tools

| Tool | Arguments | What it does |
|------|-----------|--------------|
| `read_binary_inputs` | `outstation_address`, `start_index`, `count` | READ group 1 for the range; fails if any index is missing |
| `read_analog_inputs` | `outstation_address`, `start_index`, `count` | READ group 30 for the range; fails if any index is missing |
| `write_binary_output` | `outstation_address`, `index`, `value` | CROB LATCH_ON (`true`) or LATCH_OFF (`false`); refused unless `DNP3_WRITES_ENABLED=true`; succeeds only on status `SUCCESS` |
| `poll_class` | `outstation_address`, `event_class` | `0`: all static data; `1`-`3`: that event class (events are confirmed and cleared on the outstation) |
| `list_points` | none | Lists the point-map aliases, flagging invalid entries |
| `read_point_by_alias` | `alias` | Reads a `binary_input` or `analog_input` alias |
| `write_point_by_alias` | `alias`, `value` | Operates a `binary_output` alias; same gate and checks as `write_binary_output` |
| `ping` | none | Configuration only (connection settings, write switch, point map, versions); does not contact an outstation |

`count` is 1-1000. `value` must be a JSON boolean; the strings `"true"` and
`"false"` are also understood, and anything else is rejected rather than
guessed. Each point comes back as `{ index, value, flags, quality, timestamp }`,
where `quality` lists the flag names (for example `ONLINE`) and `timestamp` is
set only when the device sent one.

## Configuration

Set these in the environment, in the `env` block of your MCP client config, or
in a `.env` file (see below). A malformed value stops the server at startup
with a message on stderr; problems inside the point-map file are reported by
the tools instead.

| Variable | Default | Notes |
|----------|---------|-------|
| `DNP3_CONNECTION_TYPE` | `tcp` | Only `tcp` is implemented; `serial` is rejected at startup |
| `DNP3_HOST` | `127.0.0.1` | Outstation (or gateway) host |
| `DNP3_PORT` | `20000` | Outstation TCP port |
| `DNP3_MASTER_ADDRESS` | `1` | This master's DNP3 link address (0-65519) |
| `DNP3_LOCAL_ADDRESS` | unset | Old name for `DNP3_MASTER_ADDRESS`; used only when that is unset, and an error if the two differ |
| `DNP3_TIMEOUT` | `5000` | Milliseconds; bounds the TCP connect and each request/response exchange |
| `DNP3_MAX_RETRIES` | `3` | Extra attempts for reads and polls after a connection failure or timeout (0-10). Controls are never retried |
| `DNP3_CONTROL_MODE` | `select_before_operate` | Or `direct_operate` |
| `DNP3_WRITES_ENABLED` | `false` | `true` allows `write_binary_output` and `write_point_by_alias`. Only true/false spellings (`true`, `1`, `yes`, `on` / `false`, `0`, `no`, `off`) are accepted |
| `POINT_MAP_FILE` | unset | Path to a JSON point map (see below) |

At startup the server loads a `.env` file from the directory it is started in
(with `uv --directory .../dnp3-python run dnp3-mcp`, that is
`dnp3-python/.env`). Parent directories are not searched, and variables that
are already set in the environment win. All settings are read after that file
is loaded.

### Point map

`POINT_MAP_FILE` points at a JSON object mapping an alias to a point. `type` is
one of `binary_input`, `analog_input` or `binary_output`; `outstation` and
`index` must be integers. The file is re-read when it changes. Invalid entries
are reported by `list_points` and refused by the alias tools.

```json
{
  "feeder1_breaker_closed": { "outstation": 10, "type": "binary_input", "index": 0, "description": "Feeder 1 breaker closed" },
  "feeder1_current": { "outstation": 10, "type": "analog_input", "index": 3, "description": "Feeder 1 phase A current" },
  "feeder1_trip": { "outstation": 10, "type": "binary_output", "index": 0, "description": "Feeder 1 trip relay" }
}
```

## Mock outstation

The mock is a real DNP3 outstation over TCP (dnp3py), listening on
`127.0.0.1:20000` as outstation `10` by default, which matches the server's
defaults. It has 16 binary inputs (odd indexes are on), 8 analog inputs that
random-walk and raise Class 2 events, and 8 binary outputs that accept
LATCH_ON/LATCH_OFF. See
[dnp3-mock-outstation/README.md](dnp3-mock-outstation/README.md) for options.

## Status and known limitations

- **TCP only.** Serial (`DNP3_CONNECTION_TYPE=serial`) is not implemented and
  is refused at startup.
- **Latching controls only.** `write_binary_output` sends LATCH_ON/LATCH_OFF.
  Devices that only accept pulse or trip/close codes will answer
  `NOT_SUPPORTED`, which is reported as a failure.
- **One connection per call.** There is no persistent session, so unsolicited
  reporting is not used and the outstation's `DEVICE_RESTART` indication is not
  cleared by the server (it shows up in `meta.iin`).
- **Library coverage.** Responses are decoded by dnp3py. If a response holds an
  object it cannot size (opendnp3 includes group 50 variation 4 in its Class 0
  response, for example), the rest of that fragment is skipped and the result
  carries `meta.incomplete: true` with a warning. Range reads still fail if any
  requested index is missing.
- **Mock analogs are integers.** dnp3py's outstation encodes analog inputs as
  32-bit integers (group 30 variation 1), so the mock's values are whole
  numbers. The master decodes floating-point variations from real devices.
- The master is tested against the mock by the test suite, and was checked by
  hand against an opendnp3 outstation (the `yadnp3` bindings), but not against
  vendor hardware. Treat a first connection to a real device as a test.

## Part of IndustriConnect

This server is one of the protocol servers in
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite
of MCP servers for industrial automation (Modbus, MQTT/Sparkplug B, OPC UA,
BACnet, DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET and S7comm), each with
a mock device and the same response envelope.

IndustriConnect ships this server in its `DNP3-Project/` directory, which is
being switched to a git submodule that tracks this repository's `main`.
Development happens here: open issues and pull requests against
`IndustriAgents/DNP3-MCP`. To get the whole suite instead:

```bash
git clone --recurse-submodules https://github.com/IndustriAgents/IndustriConnect.git
```

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, the checks CI runs and the
conventions shared with the rest of the suite. Please also read the
[Code of Conduct](CODE_OF_CONDUCT.md).

This server sits between a language model and SCADA equipment.
[SECURITY.md](SECURITY.md) explains how to run it safely and how to report a
vulnerability privately. Please do not open a public issue for one.

## License

MIT, see [LICENSE](LICENSE).
