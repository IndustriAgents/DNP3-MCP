"""Output readback and bounded static groups through real DNP3/TCP and MCP."""

import json

import pytest
from dnp3.core.enums import FunctionCode

from dnp3_mcp.domain.policy import Policy
from dnp3_mcp.protocol.contract import envelope_of

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION
from .conftest import make_master, response
from .test_architecture_contract import boundary, run
from .test_tools import build, call


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind,group,value",
    [
        ("binary_input", 1, False),
        ("analog_input", 30, 0.0),
        ("binary_output", 10, True),
        ("analog_output", 40, 12.0),
        ("counter", 20, 123),
        ("frozen_counter", 21, 77),
    ],
)
async def test_static_groups_over_tcp(mock_outstation, kind, group, value):
    db = mock_outstation.database
    db.update_binary_output(0, True)
    db.add_analog_output(0, value=12.0)
    db.add_counter(0, value=123)
    db.add_frozen_counter(0, value=77)
    master = make_master(mock_outstation.port)
    points, meta = await master.read_points(kind, OUTSTATION, 0, 1)
    assert points[0]["value"] == value
    assert points[0]["index"] == 0
    assert "quality" in points[0] and "flags" in points[0]
    assert meta["attempts"] == 1


@pytest.mark.anyio
async def test_double_bit_group_from_independent_wire_fixture(scripted):
    # g3v2, qualifier 0x00 (8-bit range), index 0, ONLINE + determined ON (2 << 6).
    outstation = await scripted(lambda request: [response(bytes.fromhex("03 02 00 00 00 81"), seq=request.seq)])
    points, _ = await make_master(outstation.port).read_points("double_bit_input", OUTSTATION, 0, 1)
    assert points[0]["value"] == "ON"
    read = next(req for req in outstation.received if req.function == FunctionCode.READ)
    assert read.data[2:4] == bytes([3, 0])


@pytest.mark.anyio
async def test_control_then_targeted_output_readback(mock_outstation):
    master = make_master(mock_outstation.port)
    await master.write_binary_output(OUTSTATION, 3, True)
    points, _ = await master.read_points("binary_output", OUTSTATION, 3, 1)
    assert points[0]["value"] is True


@pytest.mark.anyio
async def test_new_mcp_route_and_capabilities(tmp_path):
    server, master = build(tmp_path)
    result = await call(
        server,
        "read_points",
        {
            "kind": "binary_output",
            "outstation_address": 10,
            "start_index": 3,
            "count": 1,
        },
    )
    assert result["success"]
    assert master.calls == [("read_points", ("binary_output", 10, 3, 1))]
    info = (await call(server, "ping", {}))["data"]["capabilities"]
    assert info["scope"] == "backend_support" and info["device_verified"] is False
    assert info["operations"]["read_points"] == {"supported": True, "enabled": True}
    assert info["operations"]["control_binary_output"]["enabled"] is False


def test_read_only_policy_denies_implicit_event_consumption():
    b, calls = boundary(Policy(read_only=True))
    for name, args in [
        ("read_inputs", {"source": "binary", "outstation_address": 10, "start_index": 0, "count": 1}),
        ("read_binary_inputs", {"outstation_address": 10, "start_index": 0, "count": 1}),
        ("read_points", {"kind": "counter", "outstation_address": 10, "start_index": 0, "count": 1}),
    ]:
        result = envelope_of(run(b.call_tool(name, args)))
        assert result["success"] is False and result["meta"]["outcome"] == "not_sent"
    assert calls == []


@pytest.mark.anyio
async def test_unexpected_failure_redacts_logs_and_result(tmp_path, caplog):
    server, _ = build(tmp_path, fail=RuntimeError("private-payload-123"))
    result = await call(server, "poll_class", {"outstation_address": 10, "event_class": 1})
    assert "private-payload-123" not in json.dumps(result) + caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("kind,group,variation", [("analog_input", 30, 5), ("analog_output", 40, 3)])
@pytest.mark.parametrize(
    "value,encoded", [(float("nan"), "NaN"), (float("inf"), "Infinity"), (-float("inf"), "-Infinity")]
)
async def test_nonfinite_static_values_are_lossless_json_strings(scripted, kind, group, variation, value, encoded):
    import struct

    data = bytes([group, variation, 0, 0, 0, 1]) + struct.pack("<f", value)
    outstation = await scripted(lambda request: [response(data, seq=request.seq)])
    points, meta = await make_master(outstation.port).read_points(kind, OUTSTATION, 0, 1)
    assert points[0]["value"] == encoded and points[0]["flags"] == 1
    json.dumps({"points": points, "meta": meta}, allow_nan=False)


@pytest.mark.anyio
async def test_nonfinite_confirmed_event_and_unsolicited_values_survive_json(scripted):
    import struct

    # g32v5, 8-bit count/index, flags ONLINE, IEEE NaN.
    event = bytes([32, 5, 0x17, 1, 0, 1]) + struct.pack("<f", float("nan"))
    outstation = await scripted(lambda request: [response(event, seq=request.seq, con=True)])
    data, meta = await make_master(outstation.port).poll_class(OUTSTATION, 1)
    assert data["points"][0]["value"] == "NaN"
    assert outstation.confirms()
    json.dumps({"data": data, "meta": meta}, allow_nan=False)

    from .conftest import binary_statics

    def unsolicited(request):
        return [response(event, seq=3, uns=True, con=True), response(binary_statics(0, [True]), seq=request.seq)]

    outstation2 = await scripted(unsolicited)
    _, meta = await make_master(outstation2.port).read_binary_inputs(OUTSTATION, 0, 1)
    assert meta["unsolicited"][0]["value"] == "NaN" and meta["unsolicited"][0]["confirmed"]
    json.dumps(meta, allow_nan=False)
