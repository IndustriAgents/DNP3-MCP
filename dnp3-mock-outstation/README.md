# dnp3-mock-outstation

Development outstation for the DNP3 MCP server. It is a real DNP3
(IEEE 1815-2012) outstation over TCP, built on the pure-Python
[dnp3py](https://pypi.org/project/dnp3py/) stack, so the server's master talks
to it exactly as it would to a device.

## Usage

```bash
uv sync
uv run dnp3-mock-outstation
```

Options (each also readable from the environment when the command starts):

| Option | Environment | Default |
|--------|-------------|---------|
| `--host` | `MOCK_DNP3_HOST` | `127.0.0.1` |
| `--port` | `MOCK_DNP3_PORT` | `20000` (the server's default `DNP3_PORT`) |
| `--outstation-address` | `MOCK_DNP3_OUTSTATION_ADDRESS` | `10` |
| `--update-interval` | `MOCK_DNP3_UPDATE_INTERVAL` | `1.0` seconds; `0` freezes the analogs |
| `--verbose` | `MOCK_DNP3_VERBOSE` | off; logs every analog update |
| `--watch-stdin` | | off; stop when stdin reaches end of file, so a test harness that dies (closing the pipe) takes the mock with it |

It accepts any master address. Logs go to stderr. `Ctrl-C` or `SIGTERM` stops
it, also while a master is connected.

## Points

| Type | Indexes | Behaviour |
|------|---------|-----------|
| Binary inputs (group 1) | 0-15 | `true` for odd indexes. They do not change on their own (assigned to Class 1) |
| Analog inputs (group 30) | 0-7 | Start at their index, then step by -1, 0 or +1 every update interval. Class 2 events (deadband 1). Sent as 32-bit integers (g30v1/g32v1), which is how dnp3py encodes analogs |
| Binary outputs (group 10) | 0-7 | Start off. LATCH_ON / LATCH_OFF controls (group 12, select-before-operate or direct operate) set them. Other control codes and indexes are answered `NOT_SUPPORTED`. No output events; read their status with a Class 0 poll |

Unsolicited responses are disabled and the outstation does not ask for time
synchronisation.

## Try it

With the mock running, start the MCP server from `../dnp3-python` with its
defaults (`DNP3_HOST=127.0.0.1`, `DNP3_PORT=20000`) and call, for example,
`read_binary_inputs` with `outstation_address=10, start_index=0, count=4`, or
`poll_class` with `event_class=2` to collect the analog events.

## Tests and checks

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```
