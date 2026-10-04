# dnp3-mock-outstation

Development outstation for DNP3. Instead of a full protocol stack, the current scaffold exposes a TCP JSON service that mimics point reads, controls, and class polls, as a stand-in for exercising the MCP server locally. The MCP server's master does not connect to it yet; see [Status](../README.md#status).

## Usage

```bash
uv sync
uv run dnp3-mock-outstation
```

Defaults:

- Listens on `127.0.0.1:7300` (`--host` / `--port`, or `MOCK_DNP3_HOST` / `MOCK_DNP3_PORT`)
- Nudges the simulated analog inputs every second (`--update-interval` or `MOCK_DNP3_UPDATE_INTERVAL`); `--verbose` logs each update
- Supports newline-delimited JSON commands:
  - `{ "op": "read", "type": "binary", "start": 0, "count": 8 }`
  - `{ "op": "read", "type": "analog", "start": 0, "count": 4 }`
  - `{ "op": "write_binary", "index": 5, "value": true }`
  - `{ "op": "poll_class", "class": 1 }`

Try it by hand with `printf '{"op":"read","type":"analog","start":0,"count":4}\n' | nc -w 1 127.0.0.1 7300`. The mock keeps a connection open until the client closes it, so `-w 1` makes `nc` exit after a second of silence.

Extend `dnp3_mock_outstation.py` later to hook into `pydnp3`, simulate events, unsolicited responses, time synchronization, and file transfer.
