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
  dnp3py is young (a 0.x beta from a single maintainer); see
  [About dnp3py](#about-dnp3py) for what that means and how it is contained.
- Tools for binary and analog input reads, binary output controls
  (select-before-operate or direct operate), Class 0/1/2/3 polls and a `ping`
  diagnostic, all returning the suite's `{ success, data, error, meta }`
  envelope. `meta.iin` carries the outstation's internal indications.
- Honest results: values come from the outstation during the call; a read that
  is missing an index, a control the outstation rejects, a timeout or a broken
  link all come back as `success: false` with a reason. Bad arguments come
  back the same way. Controls also report `meta.outcome` (`operated`,
  `rejected`, `not_sent` or `unknown`) and `meta.request_sent`.
- Point-map aliasing: give points friendly names in a JSON file and read or
  write them by alias.
- A mock outstation with 16 binary inputs, 8 analog inputs and 8 binary
  outputs that answers real DNP3 requests, including controls and event polls.

Not implemented: serial links (use a serial-to-TCP gateway), DNP3 Secure
Authentication, TLS, counters and analog outputs as dedicated tools (they do
appear in Class 0 polls), time synchronisation, file transfer, and enabling
unsolicited reporting.

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
| `write_binary_output` | `outstation_address`, `index`, `value` | CROB LATCH_ON (`true`) or LATCH_OFF (`false`) on index 0-65535; refused unless `DNP3_WRITES_ENABLED=true`; succeeds only when the outstation echoes the control unchanged with status `SUCCESS` |
| `poll_class` | `outstation_address`, `event_class` | `0`: all static data; `1`-`3`: that event class. **Destructive**: the events are confirmed, so the outstation deletes them (see below) |
| `list_points` | none | Lists the point-map aliases, flagging invalid entries |
| `read_point_by_alias` | `alias` | Reads a `binary_input` or `analog_input` alias |
| `write_point_by_alias` | `alias`, `value` | Operates a `binary_output` alias; same gate and checks as `write_binary_output` |
| `ping` | none | Configuration only (connection settings, write switch, point map, versions); does not contact an outstation |

Arguments are strict. Addresses, indexes, counts and classes must be JSON
integers (not booleans, strings or floats); `count` is 1-1000. `value` must be
a JSON boolean or the string `"true"` or `"false"` (any case); anything else,
such as `"off"`, `1` or `"yes"`, is rejected rather than guessed. Each point
comes back as `{ index, value, flags, quality, timestamp }`, where `quality`
lists the flag names (for example `ONLINE`) and `timestamp` is set only when
the device sent one.

Tool annotations say which tools change something: the two write tools and
`poll_class` are marked destructive and not idempotent; the read tools,
`list_points` and `ping` are marked read-only. The device read tools
(`read_binary_inputs`, `read_analog_inputs`, `read_point_by_alias`) are also
marked not idempotent: they change no point, but any unsolicited data the
outstation pushes while they run is confirmed, which deletes those events on
the outstation (they are returned in `meta.unsolicited`).

Controls put the result in `meta` as well as in the text:

| `meta.outcome` | Meaning |
|----------------|---------|
| `operated` | The outstation echoed the OPERATE / DIRECT_OPERATE unchanged with status `SUCCESS` and no IIN error |
| `rejected` | Nothing was operated. Either the echoed control carried a refusal status (for example `NOT_SUPPORTED`), or the SELECT failed in any way (refused, timed out, garbled or mismatched), in which case OPERATE is never sent and a SELECT alone operates nothing |
| `not_sent` | No control request left the server: writes disabled, invalid arguments or alias, or no connection |
| `unknown` | An OPERATE / DIRECT_OPERATE may have reached the outstation, but its success was not confirmed: no answer, a dropped link, a garbled or mismatched echo, or a `SUCCESS` echo together with an IIN error. It may have been applied: read the output back before retrying |

`meta.request_sent` is `true` whenever any control request (a SELECT
included) may have reached the outstation. Controls are never retried, and
every call has an overall deadline (see `DNP3_TIMEOUT`).

**Cancelling and disconnecting.** If the MCP client cancels a control
(`notifications/cancelled`) or disconnects (closes the server's stdin) while
the control is queued, connecting, or between SELECT and OPERATE, nothing more
is sent for it: the server checks for both right before every control request.
That check reads the MCP request's own cancel flag directly, because the SDK
marks a request cancelled at once but anyio only interrupts the call at its
next wait: a cancel that lands just as the call is granted the lock, finishes
connecting or receives the SELECT answer would otherwise still let one request
out. A cancelled call ends as a cancellation (the SDK has already answered
"Request cancelled"), and its outcome is written to stderr.
On a disconnect, every call in flight is stopped, nothing further is sent, and
the outcome of each abandoned control is written to stderr (no OPERATE after a
SELECT: `rejected`; nothing sent yet: `not_sent`; an OPERATE already on the
wire: `unknown`).

**Event polls delete events.** A Class 1-3 poll confirms each response
fragment, and the outstation then deletes those events, so other masters on
the same association will not see them and a second poll does not return them.
A Class 1-3 poll is never retried once any fragment has been confirmed. If a
later fragment fails (timeout, dropped link, or a fragment the library cannot
fully decode), the call fails, and the events of the fragments already
confirmed are returned in `meta.confirmed_events`. Fragments the server
never confirmed (an undecodable one, or one that never arrived) stay on an
outstation that waits for confirmation.

The outstation may also push unsolicited responses while a call is in
progress. The server does not enable unsolicited reporting, but any
unsolicited data that arrives is returned in `meta.unsolicited`, once per
response (retransmissions are dropped), each value with `confirmed: true`
when the server confirmed it (so the outstation deleted it) or `false` when it
was withheld because the library could not fully decode it (see
`meta.warnings`).

## Configuration

Set these in the environment, in the `env` block of your MCP client config, or
in a `.env` file (see below; `DNP3_WRITES_ENABLED` is never read from the
implicit `.env`). A malformed value stops the server at startup with a message
on stderr; problems inside the point-map file are reported by the tools
instead.
[`dnp3-python/.env.example`](dnp3-python/.env.example) lists them all.

| Variable | Default | Notes |
|----------|---------|-------|
| `DNP3_CONNECTION_TYPE` | `tcp` | Only `tcp` is implemented; `serial` is rejected at startup |
| `DNP3_SERIAL_PORT` | unset | Serial is not implemented: setting it stops the server at startup rather than quietly using TCP |
| `DNP3_HOST` | `127.0.0.1` | Outstation (or gateway) host |
| `DNP3_PORT` | `20000` | Outstation TCP port |
| `DNP3_MASTER_ADDRESS` | `1` | This master's DNP3 link address (0-65519). `1` is a common production value too: against real equipment, use an address of its own (see [SECURITY.md](SECURITY.md)) |
| `DNP3_LOCAL_ADDRESS` | unset | Old name for `DNP3_MASTER_ADDRESS`; used only when that is unset, and an error if the two differ |
| `DNP3_TIMEOUT` | `5000` | Milliseconds; bounds the TCP connect and each request/response exchange. A whole call is also bounded by one absolute deadline, set when the call is made and shared by everything the call does: waiting for earlier calls to finish (calls run one at a time), connecting, SELECT and OPERATE, and every retry of a read or poll; it is never restarted per step, and a request due after it is refused rather than sent: reads and polls by 2 x timeout x (retries + 1) + 2 s, controls by 3 x timeout + 2 s (42 s and 17 s with the defaults, so the server rather than a client's 60 s timeout decides the outcome; the MCP SDK client sends no cancellation when its own timeout fires). Past that, a read fails cleanly, and a control reports `not_sent` if it was still waiting or connecting (it is then never sent later), `rejected` during SELECT, or `unknown` once an OPERATE may have left |
| `DNP3_MAX_RETRIES` | `3` | Extra attempts for reads and polls after a connection failure or timeout (0-10). Controls are never retried |
| `DNP3_CONTROL_MODE` | `select_before_operate` | Or `direct_operate` |
| `DNP3_WRITES_ENABLED` | `false` | `true` allows `write_binary_output` and `write_point_by_alias`. Only true/false spellings (`true`, `1`, `yes`, `on` / `false`, `0`, `no`, `off`) are accepted. Read only from the real environment or an explicit `--env-file`, never from the implicit `.env` |
| `POINT_MAP_FILE` | unset | Path to a JSON point map (see below) |

**The implicit `.env`.** When run from a source checkout, the server loads
`dnp3-python/.env`, found next to its own source, never the `.env` of the
directory it is started in or of any parent: an MCP client may start the
server inside its own project, which you may not control, and settings such as
the host and the point map decide where a control goes. An installed package
has no implicit `.env` at all. That file may set only this server's own
settings, `DNP3_*` and `POINT_MAP_FILE`, and never `DNP3_WRITES_ENABLED`. Names
are compared case-insensitively (Windows upper-cases them), with or without an
`export ` prefix. Anything else, such as `PATH`, `PYTHONWARNINGS`,
`DYLD_INSERT_LIBRARIES` or a proxy setting, and any spelling of the write
switch is ignored with a warning on stderr. Variables already set in the
environment win.

To keep the write switch in a file, name the file explicitly:

```bash
uv run dnp3-mcp --env-file /etc/dnp3-mcp/bench.env
```

An `--env-file` replaces the implicit `.env` and may set every variable,
`DNP3_WRITES_ENABLED` included. All settings are read after the file is
loaded.

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
- **One connection per call.** There is no persistent session. Some
  outstations accept one master connection at a time and will refuse this one,
  or drop the connection they already have, while a call runs. The server never
  enables unsolicited reporting; unsolicited data that arrives anyway is
  returned in `meta.unsolicited`. The outstation's `DEVICE_RESTART` indication
  is not cleared by the server (it shows up in `meta.iin`).
- **Every call is bounded.** Besides dnp3py's own per-exchange timeout, each
  call has an overall deadline (see `DNP3_TIMEOUT`) that also covers waiting
  behind earlier calls, so neither a peer that answers garbage nor a queue of
  slow calls can leave a call hanging without an outcome, and a control whose
  deadline passes while it waits is never sent.
- **Library coverage.** Responses are decoded by dnp3py. If a response holds an
  object it cannot size (opendnp3 includes group 50 variation 4 in its Class 0
  response, for example), the rest of that fragment is skipped. For Class 0 the
  result carries `meta.incomplete: true` and a warning; for an event poll that
  fragment is not confirmed and the call fails (events of earlier, confirmed
  fragments are in `meta.confirmed_events`). Range reads fail if any requested
  index is missing.
- **An undecodable event can block its class.** If the oldest event in a class
  is one dnp3py cannot decode, every poll of that class stops at it, is left
  unconfirmed and fails the same way, so the events queued behind it cannot be
  collected by this server. To recover, collect that class with another master
  that can decode the object (the production SCADA master, or an opendnp3-based
  tool), clear the outstation's event buffer from its own configuration tool,
  or reassign that point to a class this server does not poll.
- **Mock analogs are integers.** dnp3py's outstation encodes analog inputs as
  32-bit integers (group 30 variation 1), so the mock's values are whole
  numbers. The master decodes floating-point variations from real devices.
- The master is tested against the mock and, in a separate CI job, against an
  opendnp3 outstation (the `yadnp3` bindings), but not against vendor hardware.
  Treat a first connection to a real device as a test.

### About dnp3py

[dnp3py](https://pypi.org/project/dnp3py/) was chosen because it is
maintained, pure Python (it installs on macOS and Linux without native
libraries) and provides both a master and an outstation. The alternatives on
PyPI are master-only, unmaintained, Linux/Windows-only or commercial. It is
also young: a 0.x beta from one maintainer's repository, with no author or
project links in its PyPI metadata and open protocol bugs. This server
contains that risk as follows:

- It is pinned to exactly `dnp3py==0.6.0`, because the server relies on some of
  its internals (it subclasses its master and runner to capture fragments and
  withhold confirmations).
- Two defects are worked around here. dnp3py's own control builder writes a
  one-octet object count with qualifier 0x28, which makes every control on an
  index above 255 malformed, so this server encodes controls itself. dnp3py
  confirms response fragments it could only partly decode, which would delete
  events the server never saw, so the server withholds that confirmation for
  event data.
- Some object variations have no decoder in dnp3py; responses containing them
  are flagged as incomplete (see above).
- Besides the dnp3py-against-dnp3py tests, CI runs the master against an
  opendnp3 outstation, the de facto reference implementation, covering reads,
  event polls with confirmation and controls up to index 65535.

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
