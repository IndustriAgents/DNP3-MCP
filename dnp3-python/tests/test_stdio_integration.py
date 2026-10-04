"""End to end: the mock outstation as a subprocess, the real `dnp3-mcp` server over stdio.

Covers known issues 1, 2, 3, 4, 7, 10 and 11 the way a client sees them, and
checks that nothing but JSON-RPC reaches stdout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION
from .conftest import MOCK_PATH

pytestmark = pytest.mark.anyio

LISTENING = re.compile(r"DNP3 mock outstation listening on [^:]+:(\d+)")


@pytest.fixture
def mock_process(tmp_path: Path) -> Iterator[int]:
    """Run the mock outstation script on an ephemeral port; yield the port."""
    if not MOCK_PATH.is_file():
        pytest.skip(f"mock outstation not found at {MOCK_PATH}")
    log_path = tmp_path / "mock.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, str(MOCK_PATH), "--port", "0", "--update-interval", "0"],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    try:
        deadline = time.monotonic() + 20
        port = None
        while port is None:
            match = LISTENING.search(log_path.read_text())
            if match:
                port = int(match.group(1))
            elif proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"mock outstation did not start:\n{log_path.read_text()}")
            else:
                time.sleep(0.05)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def server_command() -> tuple[str, list[str]]:
    script = Path(sys.executable).with_name("dnp3-mcp")
    if script.is_file():
        return str(script), []
    return sys.executable, ["-m", "dnp3_mcp.cli"]


def server_env(**settings: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("DNP3_") and key != "POINT_MAP_FILE"}
    env.update(settings)
    return env


def envelope_of(result: Any) -> dict[str, Any]:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


async def test_mcp_session_against_the_mock(mock_process: int, tmp_path: Path) -> None:
    point_map = tmp_path / "points.json"
    point_map.write_text(json.dumps({"trip": {"outstation": OUTSTATION, "type": "binary_output", "index": 3}}))
    command, args = server_command()
    params = StdioServerParameters(
        command=command,
        args=args,
        env=server_env(
            DNP3_HOST="127.0.0.1",
            DNP3_PORT=str(mock_process),
            DNP3_WRITES_ENABLED="true",
            DNP3_TIMEOUT="3000",
            POINT_MAP_FILE=str(point_map),
        ),
        cwd=str(tmp_path),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.serverInfo.name == "DNP3 MCP Server"
        assert init.serverInfo.version == version("dnp3-mcp")

        tools = (await session.list_tools()).tools
        assert len(tools) == 8 and all(tool.description for tool in tools)

        async def call(tool: str, **arguments: Any) -> dict[str, Any]:
            return envelope_of(await session.call_tool(tool, arguments))

        ping = await call("ping")
        assert ping["success"] and ping["data"]["writes_enabled"] is True
        assert ping["data"]["connection"]["port"] == mock_process

        binaries = await call("read_binary_inputs", outstation_address=OUTSTATION, start_index=0, count=4)
        assert binaries["success"], binaries
        assert [p["value"] for p in binaries["data"]["points"]] == [False, True, False, True]

        analogs = await call("read_analog_inputs", outstation_address=OUTSTATION, start_index=0, count=4)
        assert [p["value"] for p in analogs["data"]["points"]] == [0.0, 1.0, 2.0, 3.0]

        async def output_3() -> bool:
            static = await call("poll_class", outstation_address=OUTSTATION, event_class=0)
            assert static["success"], static
            return next(
                p["value"] for p in static["data"]["points"] if p["type"] == "binary_output" and p["index"] == 3
            )

        assert await output_3() is False
        wrote = await call("write_binary_output", outstation_address=OUTSTATION, index=3, value=True)
        assert wrote["success"] and wrote["meta"]["status"] == "SUCCESS", wrote
        assert await output_3() is True

        # The string "false" switches the output OFF, never ON.
        by_alias = await call("write_point_by_alias", alias="trip", value="false")
        assert by_alias["success"] and by_alias["meta"]["control_code"] == "LATCH_OFF", by_alias
        assert await output_3() is False

        refused = await call("write_binary_output", outstation_address=OUTSTATION, index=42, value=True)
        assert refused["success"] is False and "NOT_SUPPORTED" in refused["error"]

        missing = await call("read_binary_inputs", outstation_address=OUTSTATION, start_index=14, count=4)
        assert missing["success"] is False and "16, 17" in missing["error"]

        bad = await call("read_binary_inputs", outstation_address="ten", start_index=0, count=1)
        assert bad["success"] is False and bad["error"].startswith("Invalid arguments")


async def test_writes_are_off_unless_enabled(mock_process: int, tmp_path: Path) -> None:
    command, args = server_command()
    params = StdioServerParameters(
        command=command,
        args=args,
        env=server_env(DNP3_PORT=str(mock_process)),
        cwd=str(tmp_path),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        ping = envelope_of(await session.call_tool("ping", {}))
        assert ping["data"]["writes_enabled"] is False
        wrote = envelope_of(
            await session.call_tool(
                "write_binary_output", {"outstation_address": OUTSTATION, "index": 3, "value": True}
            )
        )
        assert wrote["success"] is False and "DNP3_WRITES_ENABLED" in wrote["error"]


def test_stdout_carries_only_json_rpc(mock_process: int, tmp_path: Path) -> None:
    """Drive the server by hand and check every stdout line, including while dnp3py logs and errors happen."""
    command, args = server_command()
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "read_analog_inputs",
                "arguments": {"outstation_address": OUTSTATION, "start_index": 0, "count": 2},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "poll_class", "arguments": {"outstation_address": 99, "event_class": 1}},
        },
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    ]
    proc = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=server_env(DNP3_PORT=str(mock_process), DNP3_TIMEOUT="300", DNP3_MAX_RETRIES="1"),
        text=True,
    )
    out_lines: list[str] = []
    err_chunks: list[str] = []
    readers = [
        threading.Thread(target=lambda: out_lines.extend(iter(proc.stdout.readline, "")), daemon=True),
        threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        assert proc.stdin is not None
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        # Keep stdin open until every request is answered: on EOF the server
        # stops and would cancel calls still in flight.
        deadline = time.monotonic() + 60
        while sum('"id"' in line for line in out_lines) < 5 and time.monotonic() < deadline:
            time.sleep(0.05)
        proc.stdin.close()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for reader in readers:
            reader.join(timeout=10)
    stdout, stderr = "".join(out_lines), "".join(err_chunks)

    lines = [line for line in stdout.splitlines() if line.strip()]
    responses = {}
    for line in lines:
        message = json.loads(line)  # a log line or print() here would fail the test
        assert message.get("jsonrpc") == "2.0", line
        if "id" in message:
            responses[message["id"]] = message
    assert set(responses) == {1, 2, 3, 4, 5}, stdout
    read = json.loads(responses[3]["result"]["content"][0]["text"])
    assert read["success"] is True
    timeout = json.loads(responses[4]["result"]["content"][0]["text"])
    assert timeout["success"] is False and "No response from outstation 99" in timeout["error"]
    # The logging did happen; it went to stderr.
    assert "Master connected" in stderr
