# Contributing to DNP3 MCP

Thanks for taking an interest. This repository holds one MCP server for the
DNP3 SCADA protocol and a mock outstation to exercise it against.

## Where changes land

DNP3 MCP also ships inside the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite, in
its `DNP3-Project/` directory, which is being switched to a git submodule that
points at this repository. Changes to the DNP3 server or its mock are made
**here**: open issues and pull requests against `IndustriAgents/DNP3-MCP`, not
IndustriConnect. When `main` moves, a workflow
(`.github/workflows/notify-industriconnect.yml`) tells IndustriConnect so it
can bump its submodule pointer.

Changes to the suite itself (`mcp-manager-ui`, the suite README, the submodule
pins) belong in IndustriConnect. The other protocol servers are being split
out into their own repositories the same way; see
[IndustriConnect's CONTRIBUTING.md](https://github.com/IndustriAgents/IndustriConnect/blob/main/CONTRIBUTING.md)
for where a change to one of them goes.

## Repository layout

```text
DNP3-MCP/
├── dnp3-python/             # the MCP server: pyproject.toml + src/dnp3_mcp/ + tests/, console script `dnp3-mcp`
├── dnp3-mock-outstation/    # the mock outstation (real DNP3 over TCP): one module + tests/, console script `dnp3-mock-outstation`
└── README.md                # quickstart, tools, configuration, status
```

Each of the two folders is its own [`uv`](https://docs.astral.sh/uv/) project
with a committed `uv.lock`. Both use the pure-Python
[dnp3py](https://pypi.org/project/dnp3py/) stack: the server as a master, the
mock as an outstation.

## What keeps it consistent with the suite

The same three things hold every IndustriConnect server together, and a change
that breaks one of them needs a reason in the pull request:

1. **One response envelope.** Every tool returns `{ success, data, error, meta }`,
   including for bad arguments. A client that can read one server's output can
   read all of them.
2. **Read-only by default.** Tools that operate a control change physical
   equipment. They are kept separate from the read tools and refused unless
   `DNP3_WRITES_ENABLED=true`, which is off by default.
3. **A mock for the protocol.** If you add a tool, the mock has to be able to
   answer it, or nobody can test it without a plant.

## Getting set up

```bash
git clone https://github.com/IndustriAgents/DNP3-MCP.git
cd DNP3-MCP
```

Run the mock outstation (DNP3 on `127.0.0.1:20000`, outstation address `10`):

```bash
cd dnp3-mock-outstation
uv sync
uv run dnp3-mock-outstation
```

In a second terminal, from the repository root, run the MCP server (it speaks
MCP over stdio and waits for a client). Its defaults point at the mock:

```bash
cd dnp3-python
uv sync
uv run dnp3-mcp
```

## Testing a change

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same four
steps in each project: install from the lockfile, `ruff check`,
`ruff format --check` and `pytest`. Run them locally before opening a pull
request. From the repository root:

```bash
cd dnp3-python
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
cd ..

cd dnp3-mock-outstation
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
uv run --locked dnp3-mock-outstation --help
cd ..
```

The server's tests include end-to-end runs: they start
`dnp3-mock-outstation/dnp3_mock_outstation.py` with the server project's own
interpreter (it only needs dnp3py, which the server depends on too) and drive
the `dnp3-mcp` console script over stdio with the MCP client SDK. One of them
runs the server unbuffered, calls every tool and checks that nothing but
JSON-RPC is written to stdout. `tests/test_protocol.py` uses a scripted
outstation to pin down the wire behaviour (control encoding, confirmations,
unsolicited data).

CI also runs two more jobs:

- **Interop with opendnp3.** `tests/interop` runs the master against an
  opendnp3 outstation (the `yadnp3` bindings) and an opendnp3 master against the
  mock. yadnp3 has wheels for Linux x86_64 only, so locally these tests are
  skipped elsewhere. On Linux x86_64 (or in a container):

  ```bash
  cd dnp3-python
  uv sync --locked --group interop
  REQUIRE_OPENDNP3=1 uv run --locked --group interop pytest tests/interop
  ```

- **Lowest declared dependencies.** Each project is installed with every
  direct dependency at exactly its declared lower bound, and the tests run
  again. If you raise or add a dependency, keep its floor at a version the
  tests pass on.

Safety tests must be able to fail: when you add one, check that it fails with
the protection removed.

Then drive the server the way a user would, through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a real
client, with the mock running. From the repository root:

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
- Read configuration when the server is built (after `.env` is loaded), never
  at import time. The implicit `.env` is `dnp3-python/.env` of a checkout only
  (never the working directory), may set only `DNP3_*` and `POINT_MAP_FILE`,
  and never a safety switch (`DNP3_WRITES_ENABLED`, in any case); those come
  from the real environment or an explicit `--env-file`.
- A write reaches the device at most once. Only (re)connecting may be retried;
  once a control may have gone out, a lost reply is reported with
  `meta.outcome: "unknown"`, not retried. Every call has an overall deadline.
  Check for cancellation and for the client having gone (`DNP3Master.closing`)
  immediately before every send; dnp3py writes a frame before its first
  await, so a pending cancellation alone does not stop it.
- A Class 1-3 poll deletes the events it confirms: never retry it after a
  confirm, and never drop values from a confirmed fragment.
- Tools carry annotations: anything that changes a device or deletes events is
  `destructiveHint=True, idempotentHint=False`; read-only tools are
  `readOnlyHint=True`.
- Mock subprocesses started by tests use `--watch-stdin` with a pipe, so a
  killed test run leaves no mock behind.
- Fail at startup, not at the first tool call, when configuration is unusable.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server. The model needs to be told what went wrong.
- Never report success for something that did not happen, and never return
  values that did not come from the device.
- Every tool needs a docstring: it is the description clients see in
  `tools/list`.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

## Pull requests

Branch off `main` and keep each pull request to one concern. Say how you
tested, and whether the change touches anything that can write to a device.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
