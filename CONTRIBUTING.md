# Contributing to DNP3 MCP

Thanks for taking an interest. This repository holds one MCP server for the
DNP3 SCADA protocol and a mock outstation to exercise it against.

## Where changes land

DNP3 MCP also ships inside the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite as
the `DNP3-Project` git submodule. That folder is only a pointer to this
repository, so changes to the DNP3 server or its mock are made **here**: open
issues and pull requests against `IndustriAgents/DNP3-MCP`, not
IndustriConnect. When `main` moves, a workflow
(`.github/workflows/notify-industriconnect.yml`) tells IndustriConnect so it
can bump its submodule pointer.

Changes to the suite itself (`mcp-manager-ui`, the suite README, the submodule
pins) belong in IndustriConnect. Each other protocol server has its own
repository; see
[IndustriConnect's CONTRIBUTING.md](https://github.com/IndustriAgents/IndustriConnect/blob/main/CONTRIBUTING.md#where-a-change-goes).

## Repository layout

```text
DNP3-MCP/
├── dnp3-python/             # the MCP server: pyproject.toml + src/dnp3_mcp/, console script `dnp3-mcp`
├── dnp3-mock-outstation/    # the mock outstation: one module, console script `dnp3-mock-outstation`
└── README.md                # quickstart, tools, configuration, status
```

Each of the two folders is its own [`uv`](https://docs.astral.sh/uv/) project
with a committed `uv.lock`.

## What keeps it consistent with the suite

The same three things hold every IndustriConnect server together, and a change
that breaks one of them needs a reason in the pull request:

1. **One response envelope.** Every tool returns `{ success, data, error, meta }`.
   A client that can read one server's output can read all of them.
2. **Read-only by default.** Tools that operate a control change physical
   equipment. They are kept separate from the read tools and gated by
   `DNP3_WRITES_ENABLED`. That variable currently defaults to `true`, which
   does not match the suite's rule. Changing the default is a welcome
   contribution.
3. **A mock for the protocol.** If you add a tool, the mock has to be able to
   answer it, or nobody can test it without a plant.

## Getting set up

```bash
git clone https://github.com/IndustriAgents/DNP3-MCP.git
cd DNP3-MCP
```

Run the mock outstation:

```bash
cd dnp3-mock-outstation
uv sync
uv run dnp3-mock-outstation        # 127.0.0.1:7300; --port or MOCK_DNP3_PORT to change
```

In a second terminal, from the repository root, run the MCP server (it speaks
MCP over stdio and waits for a client):

```bash
cd dnp3-python
uv sync
uv run dnp3-mcp
```

The server's DNP3 master is still a scaffold. It does not connect to the mock,
or to anything else, yet; see [Status](README.md#status). Wiring the two
together is the main open piece of work.

## Testing a change

There is no test suite yet. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml))
checks that both projects install from their lockfiles and compile, that the
server package imports and builds, and that the server starts and answers an
MCP `initialize`, `tools/list` and `ping`. Run the install, compile and import
checks locally before opening a pull request. From the repository root:

```bash
cd dnp3-python
uv sync --locked
uv run --locked python -m compileall -q src
uv run --locked python -c "from dnp3_mcp import DNP3MCPServer; DNP3MCPServer()"
cd ..

cd dnp3-mock-outstation
uv sync --locked
uv run --locked python -m compileall -q dnp3_mock_outstation.py
uv run --locked dnp3-mock-outstation --help
cd ..
```

CI's last server check, the MCP handshake, is a short Python script written
inline in the `server` job of `ci.yml`. To run it locally, save it to a file
and run `uv run --locked python <file>` from `dnp3-python/`.

Then drive the server the way a user would, through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a real
client. From the repository root:

```bash
cd dnp3-python
npx @modelcontextprotocol/inspector uv run dnp3-mcp
```

Call the tool you changed, and check the envelope, not just the value.

If you change dependencies, run `uv lock` in that project and commit the
updated `uv.lock`. CI installs with `--locked` and fails on a stale lockfile.

**Do not test against production equipment.** If a change genuinely cannot be
verified against the mock, say so in the pull request and describe the bench
you used.

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. A stray `print()` corrupts the session.
- Fail at startup, not at the first tool call, when configuration is unusable.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server. The model needs to be told what went wrong.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

## Pull requests

Branch off `main` and keep each pull request to one concern. Say how you
tested, and whether the change touches anything that can write to a device.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
