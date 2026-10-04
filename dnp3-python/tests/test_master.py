"""The DNP3 master against the repository's mock outstation, over real DNP3/TCP (issues 1, 2, 7)."""

from __future__ import annotations

from typing import Any

import pytest
from dnp3.application.fragment import Truncation, TruncationReason
from dnp3.core.enums import FunctionCode
from dnp3.core.flags import IIN
from dnp3.master.handler import ResponseInfo

from dnp3_mcp.dnp3_master import (
    DNP3ConnectionError,
    DNP3ControlError,
    DNP3MasterError,
    DNP3ProtocolError,
    DNP3TimeoutError,
    DNP3WritesDisabledError,
    PointValue,
    _Reply,
    crob_statuses,
    iin_names,
    quality_names,
)

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION
from .conftest import RunningMock, make_master

pytestmark = pytest.mark.anyio


async def test_reads_values_from_the_outstation(mock_outstation: RunningMock) -> None:
    master = make_master(mock_outstation.port)
    points, meta = await master.read_binary_inputs(OUTSTATION, 0, 4)
    assert [p["value"] for p in points] == [False, True, False, True]
    assert [p["index"] for p in points] == [0, 1, 2, 3]
    assert all(p["quality"] == ["ONLINE"] and p["flags"] == 1 for p in points)
    assert meta["outstation"] == OUTSTATION and meta["attempts"] == 1
    assert meta["port"] == mock_outstation.port

    analogs, _ = await master.read_analog_inputs(OUTSTATION, 2, 3)
    assert [(p["index"], p["value"]) for p in analogs] == [(2, 2.0), (3, 3.0), (4, 4.0)]


async def test_reads_follow_the_outstation_not_a_formula(mock_outstation: RunningMock) -> None:
    mock_outstation.database.update_binary_input(1, False)
    mock_outstation.database.update_analog_input(0, 1234.0)
    master = make_master(mock_outstation.port)
    binaries, _ = await master.read_binary_inputs(OUTSTATION, 1, 1)
    analogs, _ = await master.read_analog_inputs(OUTSTATION, 0, 1)
    assert binaries[0]["value"] is False
    assert analogs[0]["value"] == 1234.0


async def test_missing_indexes_are_an_error(mock_outstation: RunningMock) -> None:
    master = make_master(mock_outstation.port)
    with pytest.raises(DNP3ProtocolError, match="no analog input value for index 8, 9") as excinfo:
        await master.read_analog_inputs(OUTSTATION, 6, 4)
    assert excinfo.value.meta["returned_indexes"] == [6, 7]


@pytest.mark.parametrize("mode", ["select_before_operate", "direct_operate"])
async def test_control_operates_the_output(mock_outstation: RunningMock, mode: str) -> None:
    master = make_master(mock_outstation.port, control_mode=mode)
    outputs = mock_outstation.database.binary_outputs
    assert outputs[3].value is False

    meta = await master.write_binary_output(OUTSTATION, 3, True)
    assert outputs[3].value is True
    assert meta["status"] == "SUCCESS" and meta["control_code"] == "LATCH_ON" and meta["mode"] == mode
    assert (meta["outcome"], meta["request_sent"]) == ("operated", True)

    meta = await master.write_binary_output(OUTSTATION, 3, False)
    assert outputs[3].value is False
    assert meta["control_code"] == "LATCH_OFF"


@pytest.mark.parametrize("mode", ["select_before_operate", "direct_operate"])
async def test_control_above_index_255_reaches_the_outstation(mock_outstation: RunningMock, mode: str) -> None:
    # Qualifier 0x28 needs a two-octet count; dnp3py's own builder writes one octet,
    # which an outstation answers with IIN PARAMETER_ERROR instead of acting.
    mock_outstation.database.add_binary_output(300, value=False)
    master = make_master(mock_outstation.port, control_mode=mode)
    meta = await master.write_binary_output(OUTSTATION, 300, True)
    assert meta["status"] == "SUCCESS"
    assert mock_outstation.database.binary_outputs[300].value is True
    assert "PARAMETER_ERROR" not in meta["iin"]

    # An index the outstation does not have gets the outstation's real answer.
    with pytest.raises(DNP3ControlError, match="status NOT_SUPPORTED") as excinfo:
        await master.write_binary_output(OUTSTATION, 301, True)
    assert "PARAMETER_ERROR" not in excinfo.value.meta["iin"]


async def test_rejected_control_is_a_failure(mock_outstation: RunningMock) -> None:
    master = make_master(mock_outstation.port)
    with pytest.raises(DNP3ControlError, match="SELECT .* status NOT_SUPPORTED. Nothing was operated") as excinfo:
        await master.write_binary_output(OUTSTATION, 42, True)
    assert excinfo.value.meta["status"] == "NOT_SUPPORTED"
    assert excinfo.value.meta["failed_step"] == "SELECT"
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("rejected", True)

    direct = make_master(mock_outstation.port, control_mode="direct_operate")
    with pytest.raises(DNP3ControlError, match="DIRECT_OPERATE .* NOT_SUPPORTED"):
        await direct.write_binary_output(OUTSTATION, 42, True)


async def test_writes_disabled_sends_nothing(closed_port: int) -> None:
    master = make_master(closed_port, writes_enabled=False)
    # A closed port would give DNP3ControlError if a connection were attempted.
    with pytest.raises(DNP3WritesDisabledError) as excinfo:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("not_sent", False)


async def test_class_0_poll_returns_static_data_including_outputs(mock_outstation: RunningMock) -> None:
    master = make_master(mock_outstation.port)
    await master.write_binary_output(OUTSTATION, 5, True)
    data, meta = await master.poll_class(OUTSTATION, 0)
    kinds = {p["type"] for p in data["points"]}
    assert {"binary_input", "analog_input", "binary_output"} <= kinds
    outputs = {p["index"]: p["value"] for p in data["points"] if p["type"] == "binary_output"}
    assert outputs[5] is True and outputs[4] is False
    assert data["count"] == len(data["points"]) == 16 + 8 + 8
    assert "iin" in meta


async def test_event_poll_returns_and_clears_events(mock_outstation: RunningMock) -> None:
    master = make_master(mock_outstation.port)
    empty, _ = await master.poll_class(OUTSTATION, 2)
    assert empty["points"] == []

    mock_outstation.database.update_analog_input(4, 40.0)
    mock_outstation.database.update_binary_input(0, True)
    class2, meta = await master.poll_class(OUTSTATION, 2)
    assert [(p["type"], p["index"], p["value"]) for p in class2["points"]] == [("analog_input", 4, 40.0)]
    assert "CLASS_1_EVENTS" in meta["iin"]

    class1, _ = await master.poll_class(OUTSTATION, 1)
    assert [(p["type"], p["index"], p["value"]) for p in class1["points"]] == [("binary_input", 0, True)]
    again, _ = await master.poll_class(OUTSTATION, 2)
    assert again["points"] == []


async def test_connection_refused_is_retried_then_reported(closed_port: int) -> None:
    master = make_master(closed_port, max_retries=2)
    with pytest.raises(
        DNP3ConnectionError, match=f"Could not connect to outstation 10 at 127.0.0.1:{closed_port}"
    ) as e:
        await master.read_binary_inputs(OUTSTATION, 0, 1)
    assert e.value.meta["attempts"] == 3


async def test_wrong_outstation_address_times_out(mock_outstation: RunningMock) -> None:
    master = make_master(mock_outstation.port, timeout_ms=300, max_retries=1)
    with pytest.raises(DNP3TimeoutError, match="No response from outstation 11") as excinfo:
        await master.read_binary_inputs(11, 0, 1)
    assert excinfo.value.meta["attempts"] == 2


async def test_control_timeout_is_never_retried(scripted: Any) -> None:
    # A silent outstation: the DIRECT_OPERATE may have acted, so it must be sent exactly once,
    # on one connection, even with retries configured.
    silent = await scripted(None)
    direct = make_master(silent.port, timeout_ms=300, control_mode="direct_operate", max_retries=3)
    with pytest.raises(
        DNP3ControlError, match="may have been applied; read the output.s state back before retrying"
    ) as excinfo:
        await direct.write_binary_output(OUTSTATION, 3, True)
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("unknown", True)
    assert silent.connections == 1
    assert silent.functions() == [FunctionCode.DIRECT_OPERATE]

    # A SELECT that times out is not followed by an OPERATE.
    silent = await scripted(None)
    sbo = make_master(silent.port, timeout_ms=300, max_retries=3)
    with pytest.raises(DNP3ControlError, match="SELECT .* Nothing was operated") as excinfo:
        await sbo.write_binary_output(OUTSTATION, 3, True)
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("not_sent", True)
    assert silent.connections == 1
    assert silent.functions() == [FunctionCode.SELECT]


async def test_reads_are_retried(scripted: Any) -> None:
    silent = await scripted(None)
    master = make_master(silent.port, timeout_ms=200, max_retries=2)
    with pytest.raises(DNP3TimeoutError):
        await master.read_binary_inputs(OUTSTATION, 0, 1)
    assert silent.connections == 3
    assert silent.functions() == [FunctionCode.READ] * 3


async def test_control_with_nothing_listening_says_nothing_was_sent(closed_port: int) -> None:
    master = make_master(closed_port)
    with pytest.raises(DNP3ControlError, match="No control was sent") as excinfo:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("not_sent", False)


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("read_binary_inputs", (OUTSTATION, 0, 0)),
        ("read_binary_inputs", (OUTSTATION, 65535, 2)),
        ("read_analog_inputs", (65520, 0, 1)),
        ("read_analog_inputs", (True, 0, 1)),
        ("poll_class", (OUTSTATION, 4)),
        ("write_binary_output", (OUTSTATION, -1, True)),
        ("write_binary_output", (OUTSTATION, 1, "false")),
    ],
)
async def test_master_validates_its_own_arguments(closed_port: int, call: str, args: tuple[Any, ...]) -> None:
    master = make_master(closed_port)
    with pytest.raises(DNP3MasterError) as excinfo:
        await getattr(master, call)(*args)
    assert type(excinfo.value) is DNP3MasterError


# -- control-step edge cases, with a stub session ---------------------------


NO_IIN = IIN(0)


def _info(iin: IIN = NO_IIN) -> ResponseInfo:
    return ResponseInfo(function=FunctionCode.RESPONSE, iin=iin, sequence=0)


def _crob_response(index: int, status: int, *, qualifier: int = 0x17, uns: bool = False) -> bytes:
    control = 0xC0 | (0x10 if uns else 0)  # FIR | FIN | (UNS)
    header = bytes([control, 0x81, 0x00, 0x00])  # control, RESPONSE, IIN
    if qualifier == 0x17:
        objects = bytes([12, 1, 0x17, 1, index])
    else:
        objects = bytes([12, 1, 0x28, 1, 0]) + index.to_bytes(2, "little")
    return header + objects + bytes([0x03, 1, 0, 0, 0, 0, 0, 0, 0, 0, status])


class StubSession:
    def __init__(self, reply: _Reply | Exception) -> None:
        self.reply = reply

    def next_sequence(self) -> int:
        return 0

    async def request(self, fragment: Any) -> _Reply:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


async def _step(reply: _Reply | Exception, function: str = "OPERATE", value: bool = True) -> IIN:
    master = make_master(1)
    return await master._control_step(StubSession(reply), function, 3, value, {"index": 3})  # type: ignore[arg-type]


async def test_control_step_requires_a_success_echo() -> None:
    assert await _step(_Reply([_info()], [_crob_response(3, 0)], [])) == IIN(0)
    with pytest.raises(DNP3ControlError, match="status TIMEOUT"):
        await _step(_Reply([_info()], [_crob_response(3, 1)], []))
    with pytest.raises(DNP3ControlError, match="carried no control status. The control may have been applied") as e:
        await _step(_Reply([_info()], [], []))
    assert e.value.meta["outcome"] == "unknown"
    # An echo for some other point is not an answer for this one.
    with pytest.raises(DNP3ControlError, match="carried no control status"):
        await _step(_Reply([_info()], [_crob_response(4, 0)], []))
    # Nor is an unsolicited fragment.
    with pytest.raises(DNP3ControlError, match="carried no control status"):
        await _step(_Reply([_info()], [_crob_response(3, 0, uns=True)], []))


async def test_control_step_iin_errors_and_link_failures() -> None:
    with pytest.raises(DNP3ControlError, match=r"rejected OPERATE .*IIN NO_FUNC_CODE_SUPPORT") as e:
        await _step(_Reply([_info(IIN.NO_FUNC_CODE_SUPPORT)], [_crob_response(3, 0)], []))
    assert e.value.meta["outcome"] == "rejected"
    with pytest.raises(DNP3ControlError, match="may have been applied; read the output.s state back before retrying"):
        await _step(DNP3ConnectionError("Link to outstation 10 failed"))
    with pytest.raises(DNP3ControlError, match="Nothing was operated"):
        await _step(DNP3TimeoutError("No response"), function="SELECT")
    with pytest.raises(DNP3ControlError, match="failed: ValueError: bad frame. The control may have been applied"):
        await _step(ValueError("bad frame"))


async def test_control_step_compares_the_echo_with_the_request() -> None:
    # The stub echoes LATCH_ON (0x03); a LATCH_OFF request must not accept it.
    echo_on = _Reply([_info()], [_crob_response(3, 0)], [])
    with pytest.raises(DNP3ControlError, match="does not echo the control that was sent. OPERATE was not sent") as e:
        await _step(echo_on, function="SELECT", value=False)
    assert e.value.meta["status"] == "SELECT_MISMATCH" and e.value.meta["outcome"] == "not_sent"
    with pytest.raises(DNP3ControlError, match="does not echo .* may have been applied") as e:
        await _step(echo_on, function="DIRECT_OPERATE", value=False)
    assert e.value.meta["status"] == "ECHO_MISMATCH" and e.value.meta["outcome"] == "unknown"
    assert await _step(echo_on, function="SELECT", value=True) == IIN(0)


async def test_partially_decoded_responses_are_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    # opendnp3 puts g50v4 objects in its Class 0 response, which dnp3py cannot size.
    cut = Truncation(reason=TruncationReason.UNKNOWN_WIDTH, offset=265, group=50, variation=4, qualifier=0)
    info = ResponseInfo(function=FunctionCode.RESPONSE, iin=NO_IIN, sequence=2, truncation=cut)
    reply = _Reply([info], [], [PointValue("binary_input", 0, True, 1, None, False)])
    master = make_master(1)

    async def fake_read(*args: Any, **kwargs: Any) -> tuple[_Reply, int]:
        return reply, 1

    monkeypatch.setattr(master, "_read_with_retries", fake_read)
    data, meta = await master.poll_class(OUTSTATION, 0)
    assert data["count"] == 1
    assert meta["incomplete"] is True
    assert "unknown_width at group 50 variation 4" in meta["warnings"][0]

    # Event data that was only partly decoded is a failure, not a partial success.
    with pytest.raises(DNP3ProtocolError, match="class 1 poll response could not be fully decoded") as excinfo:
        await master.poll_class(OUTSTATION, 1)
    assert excinfo.value.meta["incomplete"] is True

    complete = _Reply([_info()], [], [])
    monkeypatch.setattr(master, "_read_with_retries", lambda *a, **k: _async(complete))
    _, meta = await master.poll_class(OUTSTATION, 0)
    assert "incomplete" not in meta and "warnings" not in meta


async def _async(value: Any) -> tuple[Any, int]:
    return value, 1


def test_crob_statuses_decodes_both_qualifiers() -> None:
    assert crob_statuses([_crob_response(7, 4)]) == {7: 4}
    assert crob_statuses([_crob_response(300, 0, qualifier=0x28)]) == {300: 0}
    assert crob_statuses([b"\x00"]) == {}


def test_flag_names() -> None:
    assert iin_names(IIN.DEVICE_RESTART | IIN.PARAMETER_ERROR) == ["DEVICE_RESTART", "PARAMETER_ERROR"]
    assert quality_names("binary_input", 0x81) == ["ONLINE"]
    assert quality_names("analog_input", 0x25) == ["ONLINE", "COMM_LOST", "OVER_RANGE"]
    assert quality_names("unknown", 0xFF) == []
