"""End to end: the mock outstation as a subprocess, the real `dnp3-mcp` server over stdio.

Covers known issues 1, 2, 3, 4, 7, 10 and 11 the way a client sees them, and
checks that nothing but JSON-RPC reaches stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION

pytestmark = pytest.mark.anyio


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
        assert (wrote["meta"]["outcome"], wrote["meta"]["request_sent"]) == ("operated", True)
        assert await output_3() is True

        # The string "false" switches the output OFF, never ON.
        by_alias = await call("write_point_by_alias", alias="trip", value="false")
        assert by_alias["success"] and by_alias["meta"]["control_code"] == "LATCH_OFF", by_alias
        assert await output_3() is False

        refused = await call("write_binary_output", outstation_address=OUTSTATION, index=42, value=True)
        assert refused["success"] is False and "NOT_SUPPORTED" in refused["error"]
        assert refused["meta"]["outcome"] == "rejected"

        # Above index 255 the request uses qualifier 0x28; the mock must see a well-formed CROB
        # and answer with its own status for an output it does not have, not IIN PARAMETER_ERROR.
        high = await call("write_binary_output", outstation_address=OUTSTATION, index=300, value=True)
        assert high["success"] is False and high["meta"]["status"] == "NOT_SUPPORTED", high
        assert "PARAMETER_ERROR" not in high["meta"]["iin"]

        loose = await call("write_binary_output", outstation_address=OUTSTATION, index=3, value="off")
        assert loose["success"] is False and loose["error"].startswith("Invalid arguments"), loose

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


ALL_TOOL_CALLS = [
    ("ping", {}),
    ("list_points", {}),
    ("read_binary_inputs", {"outstation_address": OUTSTATION, "start_index": 0, "count": 2}),
    ("read_analog_inputs", {"outstation_address": OUTSTATION, "start_index": 0, "count": 2}),
    ("poll_class", {"outstation_address": OUTSTATION, "event_class": 0}),
    ("poll_class", {"outstation_address": 99, "event_class": 1}),  # no such outstation: timeout path
    ("write_binary_output", {"outstation_address": OUTSTATION, "index": 3, "value": True}),
    ("read_point_by_alias", {"alias": "breaker"}),
    ("write_point_by_alias", {"alias": "trip", "value": False}),
    ("read_binary_inputs", {"outstation_address": "ten", "start_index": 0, "count": 1}),  # invalid arguments
    ("nope", {}),  # unknown tool
]


def test_stdout_carries_only_json_rpc(mock_process: int, tmp_path: Path) -> None:
    """Drive the server by hand, unbuffered, through every tool, and check every stdout line.

    PYTHONUNBUFFERED=1 matters: with a block-buffered stdout a stray print() can
    sit in the buffer and be lost at exit, and the test would pass anyway.
    """
    point_map = tmp_path / "points.json"
    point_map.write_text(
        json.dumps(
            {
                "breaker": {"outstation": OUTSTATION, "type": "binary_input", "index": 1},
                "trip": {"outstation": OUTSTATION, "type": "binary_output", "index": 3},
            }
        )
    )
    messages: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ]
    for number, (name, arguments) in enumerate(ALL_TOOL_CALLS, start=2):
        messages.append(
            {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
    expected_ids = set(range(len(ALL_TOOL_CALLS) + 2))

    command, args = server_command()
    proc = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=server_env(
            PYTHONUNBUFFERED="1",
            DNP3_PORT=str(mock_process),
            DNP3_TIMEOUT="300",
            DNP3_MAX_RETRIES="1",
            DNP3_WRITES_ENABLED="true",
            POINT_MAP_FILE=str(point_map),
        ),
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
        while sum('"id"' in line for line in out_lines) < len(expected_ids) and time.monotonic() < deadline:
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

    responses = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            pytest.fail(f"non-JSON-RPC output on stdout: {line!r}")
        assert isinstance(message, dict) and message.get("jsonrpc") == "2.0", line
        if "id" in message:
            responses[message["id"]] = message
    assert set(responses) == expected_ids, stdout

    def envelope(number: int) -> dict[str, Any]:
        return json.loads(responses[number]["result"]["content"][0]["text"])

    called = {name for name, _ in ALL_TOOL_CALLS if name != "nope"}
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == called  # every tool was called
    by_name = {number: name for number, (name, _) in enumerate(ALL_TOOL_CALLS, start=2)}
    for number, name in by_name.items():
        if name == "nope":
            assert responses[number]["result"]["isError"] is True
            continue
        assert set(envelope(number)) == {"success", "data", "error", "meta"}, name
    assert envelope(4)["success"] is True  # a real read went through
    assert "No response from outstation 99" in envelope(7)["error"]
    assert envelope(8)["meta"]["outcome"] == "operated"
    # The logging did happen; it went to stderr.
    assert "Master connected" in stderr


async def test_starts_and_answers_without_a_device(closed_port: int, tmp_path: Path) -> None:
    command, args = server_command()
    params = StdioServerParameters(
        command=command,
        args=args,
        env=server_env(DNP3_PORT=str(closed_port), DNP3_MAX_RETRIES="0"),
        cwd=str(tmp_path),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert len((await session.list_tools()).tools) == 8
        assert envelope_of(await session.call_tool("ping", {}))["success"] is True
        read_result = envelope_of(
            await session.call_tool("read_binary_inputs", {"outstation_address": 10, "start_index": 0, "count": 1})
        )
        assert read_result["success"] is False and "Could not connect" in read_result["error"]


# Runs the real CLI with the implicit .env location moved to a test directory,
# so the test never needs to write dnp3-python/.env itself.
PROJECT_ENV_WRAPPER = """
import sys
from pathlib import Path
import dnp3_mcp.cli as cli
location = Path(sys.argv[1])
cli.project_env_file = lambda module_file=None: location
cli.main(sys.argv[2:])
"""


async def _ping_and_stderr(params: StdioServerParameters, errlog_path: Path) -> tuple[dict[str, Any], str]:
    with errlog_path.open("w+") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            ping = envelope_of(await session.call_tool("ping", {}))
        errlog.seek(0)
        return ping, errlog.read()


async def test_implicit_env_file_cannot_enable_writes(mock_process: int, tmp_path: Path) -> None:
    project = tmp_path / "dnp3-python"
    project.mkdir()
    (project / ".env").write_text(f"DNP3_PORT={mock_process}\ndnp3_writes_enabled=true\nPYTHONWARNINGS=error\n")

    # The project .env sets the port, but neither the gate (any case) nor a foreign variable.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", PROJECT_ENV_WRAPPER, str(project / ".env")],
        env=server_env(),
        cwd=str(tmp_path),
    )
    ping, stderr = await _ping_and_stderr(params, tmp_path / "a.err")
    assert ping["data"]["connection"]["port"] == mock_process
    assert ping["data"]["writes_enabled"] is False
    assert "ignoring dnp3_writes_enabled" in stderr and "ignoring PYTHONWARNINGS" in stderr

    # A .env in the working directory (an MCP client's project) is not read at all.
    client_project = tmp_path / "client"
    client_project.mkdir()
    (client_project / ".env").write_text(f"DNP3_PORT={mock_process}\nDNP3_WRITES_ENABLED=true\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", PROJECT_ENV_WRAPPER, str(tmp_path / "nowhere" / ".env")],
        env=server_env(),
        cwd=str(client_project),
    )
    ping, _ = await _ping_and_stderr(params, tmp_path / "b.err")
    assert ping["data"]["connection"]["port"] == 20000
    assert ping["data"]["writes_enabled"] is False

    # An explicit --env-file may switch writes on.
    explicit = tmp_path / "operator.env"
    explicit.write_text(f"DNP3_PORT={mock_process}\nDNP3_WRITES_ENABLED=true\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", PROJECT_ENV_WRAPPER, str(project / ".env"), "--env-file", str(explicit)],
        env=server_env(),
        cwd=str(client_project),
    )
    ping, _ = await _ping_and_stderr(params, tmp_path / "c.err")
    assert ping["data"]["writes_enabled"] is True
