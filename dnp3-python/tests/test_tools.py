"""Tool layer: descriptions, envelopes, validation and the write gate (issues 2, 3, 4, 9, 11)."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from dnp3_mcp.config import DNP3Config, ServerConfig
from dnp3_mcp.dnp3_master import DNP3Master, DNP3MasterError
from dnp3_mcp.server import DNP3MCPServer

pytestmark = pytest.mark.anyio

TOOLS = {
    "read_binary_inputs",
    "read_analog_inputs",
    "write_binary_output",
    "poll_class",
    "list_points",
    "read_point_by_alias",
    "write_point_by_alias",
    "ping",
}


class FakeMaster(DNP3Master):
    """Records calls instead of talking DNP3."""

    def __init__(self, config: DNP3Config, fail: Exception | None = None) -> None:
        super().__init__(config)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.fail:
            raise self.fail

    async def read_binary_inputs(self, outstation: int, start: int, count: int) -> Any:
        self._record("read_binary_inputs", outstation, start, count)
        return [{"index": start, "value": True}], {"outstation": outstation}

    async def read_analog_inputs(self, outstation: int, start: int, count: int) -> Any:
        self._record("read_analog_inputs", outstation, start, count)
        return [{"index": start, "value": 1.5}], {"outstation": outstation}

    async def write_binary_output(self, outstation: int, index: int, value: bool) -> Any:
        self._record("write_binary_output", outstation, index, value)
        return {"status": "SUCCESS"}

    async def poll_class(self, outstation: int, event_class: int) -> Any:
        self._record("poll_class", outstation, event_class)
        return {"outstation": outstation, "class": event_class, "count": 0, "points": []}, {}


def build(
    tmp_path: Path, *, writes: bool = False, point_map: Any = None, fail: Exception | None = None
) -> tuple[DNP3MCPServer, FakeMaster]:
    path = None
    if point_map is not None:
        path = tmp_path / "points.json"
        path.write_text(json.dumps(point_map))
    config = ServerConfig(dnp3=DNP3Config(writes_enabled=writes), point_map_path=path)
    master = FakeMaster(config.dnp3, fail=fail)
    return DNP3MCPServer(config, master=master), master


async def call(server: DNP3MCPServer, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool(tool, args)
    assert not result.isError, result.content
    envelope = json.loads(result.content[0].text)
    assert set(envelope) == {"success", "data", "error", "meta"}
    return envelope


POINTS = {
    "breaker": {"outstation": 10, "type": "binary_input", "index": 2},
    "current": {"outstation": 10, "type": "analog_input", "index": 1},
    "trip": {"outstation": 10, "type": "binary_output", "index": 3},
    "broken": {"outstation": "ten", "type": "binary_output", "index": 3},
}


async def test_every_tool_has_a_description_and_typed_schema(tmp_path: Path) -> None:
    server, _ = build(tmp_path)
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.description and tool.description.strip(), tool.name
        assert "\n    " not in tool.description, tool.name  # docstring indentation is cleaned
    schema = tools["read_binary_inputs"].inputSchema
    address = schema["properties"]["outstation_address"]
    assert (address["type"], address["minimum"], address["maximum"]) == ("integer", 0, 65519)
    assert "ge" not in address and "le" not in address  # standard JSON Schema keywords only
    assert (schema["properties"]["count"]["minimum"], schema["properties"]["count"]["maximum"]) == (1, 1000)
    assert set(schema["required"]) == {"outstation_address", "start_index", "count"}
    assert tools["write_binary_output"].inputSchema["properties"]["value"]["type"] == "boolean"
    assert tools["write_point_by_alias"].inputSchema["properties"]["value"]["type"] == "boolean"


async def test_server_info_reports_the_package_version(tmp_path: Path) -> None:
    server, _ = build(tmp_path)
    assert server.mcp._mcp_server.version == version("dnp3-mcp")
    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_version == version("dnp3-mcp") != version("mcp")


async def test_writes_are_refused_by_default(tmp_path: Path) -> None:
    server, master = build(tmp_path, point_map=POINTS)
    for tool, args in (
        ("write_binary_output", {"outstation_address": 10, "index": 3, "value": True}),
        ("write_point_by_alias", {"alias": "trip", "value": True}),
    ):
        envelope = await call(server, tool, args)
        assert envelope["success"] is False
        assert "DNP3_WRITES_ENABLED" in envelope["error"]
        assert (envelope["meta"]["outcome"], envelope["meta"]["request_sent"]) == ("not_sent", False)
    assert master.calls == []
    ping = await call(server, "ping", {})
    assert ping["data"]["writes_enabled"] is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("false", False), ("False", False), (" TRUE ", True), ("true", True)],
)
async def test_write_value_is_parsed_strictly(tmp_path: Path, raw: Any, expected: bool) -> None:
    server, master = build(tmp_path, writes=True, point_map=POINTS)
    direct = await call(server, "write_binary_output", {"outstation_address": 10, "index": 3, "value": raw})
    by_alias = await call(server, "write_point_by_alias", {"alias": "trip", "value": raw})
    assert direct["success"] and by_alias["success"]
    assert master.calls == [("write_binary_output", (10, 3, expected))] * 2


@pytest.mark.parametrize("raw", ["banana", "", "off", "on", "yes", "no", "0", "1", 0, 1, 2, 1.0, None, [True]])
async def test_unrecognised_write_value_is_rejected(tmp_path: Path, raw: Any) -> None:
    server, master = build(tmp_path, writes=True, point_map=POINTS)
    for tool, args in (
        ("write_binary_output", {"outstation_address": 10, "index": 3, "value": raw}),
        ("write_point_by_alias", {"alias": "trip", "value": raw}),
    ):
        envelope = await call(server, tool, args)
        assert envelope["success"] is False
        assert envelope["error"].startswith(f"Invalid arguments for {tool}: value")
    assert master.calls == []


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ({"outstation_address": "abc", "start_index": 0, "count": 1}, "outstation_address"),
        ({"outstation_address": True, "start_index": 0, "count": 1}, "outstation_address"),
        ({"outstation_address": -1, "start_index": 0, "count": 1}, "outstation_address"),
        ({"outstation_address": 65520, "start_index": 0, "count": 1}, "outstation_address"),
        ({"outstation_address": 10, "start_index": 0, "count": 0}, "count"),
        ({"outstation_address": 10, "start_index": 0, "count": 1001}, "count"),
        ({"outstation_address": 10, "start_index": 1.5, "count": 1}, "start_index"),
        ({"outstation_address": 10, "start_index": 0}, "count"),
    ],
)
async def test_bad_read_arguments_come_back_as_envelopes(tmp_path: Path, args: dict[str, Any], problem: str) -> None:
    server, master = build(tmp_path)
    envelope = await call(server, "read_binary_inputs", args)
    assert envelope["success"] is False
    assert envelope["error"].startswith("Invalid arguments for read_binary_inputs")
    assert problem in envelope["error"]
    assert envelope["meta"] == {"tool": "read_binary_inputs"}
    assert master.calls == []


async def test_bad_event_class_is_rejected(tmp_path: Path) -> None:
    server, master = build(tmp_path)
    envelope = await call(server, "poll_class", {"outstation_address": 10, "event_class": 4})
    assert envelope["success"] is False and "event_class" in envelope["error"]
    assert master.calls == []


@pytest.mark.parametrize("raw", ["10", 10.0, True, "ten"])
async def test_integers_are_strict(tmp_path: Path, raw: Any) -> None:
    server, master = build(tmp_path)
    envelope = await call(server, "read_analog_inputs", {"outstation_address": raw, "start_index": 2, "count": 1})
    assert envelope["success"] is False
    assert envelope["error"].startswith("Invalid arguments for read_analog_inputs: outstation_address")
    assert master.calls == []


async def test_aliases_route_to_the_right_point(tmp_path: Path) -> None:
    server, master = build(tmp_path, writes=True, point_map=POINTS)
    assert (await call(server, "read_point_by_alias", {"alias": "breaker"}))["success"]
    assert (await call(server, "read_point_by_alias", {"alias": "current"}))["success"]
    assert master.calls == [("read_binary_inputs", (10, 2, 1)), ("read_analog_inputs", (10, 1, 1))]


async def test_malformed_alias_entries_return_envelopes(tmp_path: Path) -> None:
    server, master = build(tmp_path, writes=True, point_map=POINTS)
    for tool, args in (
        ("read_point_by_alias", {"alias": "broken"}),
        ("write_point_by_alias", {"alias": "broken", "value": True}),
    ):
        envelope = await call(server, tool, args)
        assert envelope["success"] is False
        assert "Point map entry 'broken' is invalid" in envelope["error"]
    assert master.calls == []


async def test_alias_type_mismatches(tmp_path: Path) -> None:
    server, master = build(tmp_path, writes=True, point_map=POINTS)
    unknown = await call(server, "read_point_by_alias", {"alias": "nope"})
    assert unknown["success"] is False and "Unknown alias 'nope'" in unknown["error"]
    read_output = await call(server, "read_point_by_alias", {"alias": "trip"})
    assert read_output["success"] is False and "poll_class" in read_output["error"]
    write_input = await call(server, "write_point_by_alias", {"alias": "breaker", "value": True})
    assert write_input["success"] is False and "cannot be written" in write_input["error"]
    assert master.calls == []


async def test_list_points_reports_invalid_entries_and_file_errors(tmp_path: Path) -> None:
    server, _ = build(tmp_path, point_map=POINTS)
    listed = await call(server, "list_points", {})
    assert listed["success"] is True and listed["data"]["count"] == 4
    assert [p["alias"] for p in listed["data"]["points"] if p.get("error")] == ["broken"]

    config = ServerConfig(dnp3=DNP3Config(), point_map_path=tmp_path / "missing.json")
    missing = DNP3MCPServer(config, master=FakeMaster(config.dnp3))
    envelope = await call(missing, "list_points", {})
    assert envelope["success"] is False and "cannot be read" in envelope["error"]


async def test_master_errors_become_envelopes(tmp_path: Path) -> None:
    error = DNP3MasterError("Outstation 10 returned no binary input value for index 99", {"iin": ["DEVICE_RESTART"]})
    server, _ = build(tmp_path, fail=error)
    envelope = await call(server, "read_binary_inputs", {"outstation_address": 10, "start_index": 99, "count": 1})
    assert envelope == {
        "success": False,
        "data": None,
        "error": "Outstation 10 returned no binary input value for index 99",
        "meta": {"tool": "read_binary_inputs", "iin": ["DEVICE_RESTART"]},
    }


async def test_unexpected_exceptions_become_envelopes(tmp_path: Path) -> None:
    server, _ = build(tmp_path, fail=RuntimeError("boom"))
    envelope = await call(server, "poll_class", {"outstation_address": 10, "event_class": 1})
    assert envelope["success"] is False
    assert envelope["error"] == "Unexpected error: RuntimeError: boom"


async def test_ping_reports_one_configuration(tmp_path: Path) -> None:
    server, master = build(tmp_path, writes=True, point_map=POINTS)
    envelope = await call(server, "ping", {})
    data = envelope["data"]
    assert data["writes_enabled"] is True
    assert "writes_enabled" not in data["connection"]
    assert data["connection"]["connection_type"] == "tcp"
    assert data["point_aliases"] == 4
    assert data["server_version"] == version("dnp3-mcp")
    assert data["dnp3_library"] == f"dnp3py {version('dnp3py')}"
    assert master.calls == []
