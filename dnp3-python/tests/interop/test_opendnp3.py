"""Interop with opendnp3, the reference DNP3 implementation (yadnp3 bindings).

Every other test runs dnp3py against dnp3py. These run this server's master
against an opendnp3 outstation, and an opendnp3 master against the mock, so a
shared misreading of the standard cannot pass unnoticed.

yadnp3 has wheels for Linux x86_64 (and Windows) only; install it with
`uv sync --group interop`. Elsewhere these tests are skipped, unless
REQUIRE_OPENDNP3=1 (set in CI) turns a missing yadnp3 into a failure.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

if os.environ.get("REQUIRE_OPENDNP3") == "1":
    import opendnp3
else:
    opendnp3 = pytest.importorskip("opendnp3")

from dnp3_mcp.dnp3_master import DNP3ControlError, DNP3ProtocolError  # noqa: E402

from ..conftest import make_master  # noqa: E402

OUTSTATION = 10
REJECTED_INDEX = 9  # the handler answers NOT_SUPPORTED for this output


class CommandHandler(opendnp3.ICommandHandler):
    """Latches any binary output except REJECTED_INDEX, and records what it operated."""

    def __init__(self) -> None:
        super().__init__()
        self.operated: list[tuple[int, str, str]] = []

    def Begin(self) -> None:  # opendnp3 interface
        pass

    def End(self) -> None:
        pass

    def _accepts(self, command: Any, index: int) -> bool:
        latch = (opendnp3.OperationType.LATCH_ON, opendnp3.OperationType.LATCH_OFF)
        return (
            isinstance(command, opendnp3.ControlRelayOutputBlock)
            and command.opType in latch
            and index != REJECTED_INDEX
        )

    def Select(self, command: Any, index: int) -> Any:
        return opendnp3.CommandStatus.SUCCESS if self._accepts(command, index) else opendnp3.CommandStatus.NOT_SUPPORTED

    def Operate(self, command: Any, index: int, handler: Any, op_type: Any) -> Any:
        if not self._accepts(command, index):
            return opendnp3.CommandStatus.NOT_SUPPORTED
        self.operated.append((index, command.opType.name, op_type.name))
        return opendnp3.CommandStatus.SUCCESS


class OutstationApp(opendnp3.IOutstationApplication):
    def __init__(self) -> None:
        super().__init__()

    def SupportsWriteAbsoluteTime(self) -> bool:
        return False

    def WriteAbsoluteTime(self, timestamp: Any) -> bool:
        return False

    def SupportsAssignClass(self) -> bool:
        return False

    def GetApplicationIIN(self) -> Any:
        return opendnp3.ApplicationIIN()

    def ColdRestartSupport(self) -> Any:
        return opendnp3.RestartMode.UNSUPPORTED

    def WarmRestartSupport(self) -> Any:
        return opendnp3.RestartMode.UNSUPPORTED

    def ColdRestart(self) -> int:
        return 0

    def WarmRestart(self) -> int:
        return 0

    def Now(self) -> Any:
        return opendnp3.DNPTime(int(time.time() * 1000))


@dataclass
class OpenDnp3Outstation:
    port: int
    stack: Any
    handler: CommandHandler

    def update(self, *updates: tuple[Any, int]) -> None:
        builder = opendnp3.UpdateBuilder()
        for value, index in updates:
            builder.Update(value, index)
        self.stack.Apply(builder.Build())
        time.sleep(0.1)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_listening(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"opendnp3 outstation is not listening on {port}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def opendnp3_outstation() -> Iterator[OpenDnp3Outstation]:
    """An opendnp3 outstation (10 points of each type) on 127.0.0.1, outstation address 10."""
    port = _free_port()
    manager = opendnp3.DNP3Manager(1)
    try:
        channel = manager.AddTCPServer(
            "server",
            opendnp3.levels.NOTHING,
            opendnp3.ServerAcceptMode.CloseExisting,
            opendnp3.IPEndpoint("127.0.0.1", port),
            None,
        )
        handler = CommandHandler()
        config = opendnp3.OutstationStackConfig(opendnp3.DatabaseConfig(10))
        config.outstation.params.allowUnsolicited = False
        config.outstation.eventBufferConfig = opendnp3.EventBufferConfig.AllTypes(100)
        config.link.LocalAddr = OUTSTATION
        config.link.RemoteAddr = 1
        stack = channel.AddOutstation("outstation", handler, OutstationApp(), config)
        stack.Enable()
        outstation = OpenDnp3Outstation(port=port, stack=stack, handler=handler)
        outstation.update(
            *[(opendnp3.Binary(bool(i % 2)), i) for i in range(10)],
            *[(opendnp3.Analog(float(i * 10)), i) for i in range(10)],
        )
        _wait_listening(port)
        yield outstation
    finally:
        manager.Shutdown()


# -- this server's master -> opendnp3 outstation ------------------------------


@pytest.mark.anyio
async def test_reads(opendnp3_outstation: OpenDnp3Outstation) -> None:
    master = make_master(opendnp3_outstation.port)
    binaries, _ = await master.read_binary_inputs(OUTSTATION, 0, 4)
    assert [p["value"] for p in binaries] == [False, True, False, True]
    analogs, _ = await master.read_analog_inputs(OUTSTATION, 0, 3)
    assert [p["value"] for p in analogs] == [0.0, 10.0, 20.0]
    with pytest.raises(DNP3ProtocolError, match="PARAMETER_ERROR"):
        await master.read_analog_inputs(OUTSTATION, 8, 4)


@pytest.mark.anyio
async def test_class_0_poll_is_flagged_incomplete(opendnp3_outstation: OpenDnp3Outstation) -> None:
    # opendnp3's Class 0 response contains g50v4, which dnp3py 0.6.0 cannot size.
    data, meta = await make_master(opendnp3_outstation.port).poll_class(OUTSTATION, 0)
    assert {"binary_input", "analog_input", "counter", "binary_output"} <= {p["type"] for p in data["points"]}
    assert meta["incomplete"] is True
    assert any("group 50 variation 4" in w for w in meta["warnings"])


@pytest.mark.anyio
async def test_event_polls_are_confirmed(opendnp3_outstation: OpenDnp3Outstation) -> None:
    master = make_master(opendnp3_outstation.port)
    first, _ = await master.poll_class(OUTSTATION, 1)
    assert first["count"] > 0  # the startup updates raised Class 1 events
    # opendnp3 deletes events only when the master confirms them.
    again, _ = await master.poll_class(OUTSTATION, 1)
    assert again["count"] == 0
    opendnp3_outstation.update((opendnp3.Binary(True), 2))
    changed, _ = await master.poll_class(OUTSTATION, 1)
    assert [(p["type"], p["index"], p["value"]) for p in changed["points"]] == [("binary_input", 2, True)]


@pytest.mark.anyio
@pytest.mark.parametrize("index", [3, 255, 256, 300, 65535])
@pytest.mark.parametrize("mode", ["select_before_operate", "direct_operate"])
async def test_controls(opendnp3_outstation: OpenDnp3Outstation, index: int, mode: str) -> None:
    master = make_master(opendnp3_outstation.port, control_mode=mode)
    meta = await master.write_binary_output(OUTSTATION, index, True)
    assert meta["status"] == "SUCCESS"
    expected = "SelectBeforeOperate" if mode == "select_before_operate" else "DirectOperate"
    assert opendnp3_outstation.handler.operated == [(index, "LATCH_ON", expected)]


@pytest.mark.anyio
async def test_rejected_control_operates_nothing(opendnp3_outstation: OpenDnp3Outstation) -> None:
    master = make_master(opendnp3_outstation.port)
    with pytest.raises(DNP3ControlError, match="Nothing was operated"):
        await master.write_binary_output(OUTSTATION, REJECTED_INDEX, True)
    assert opendnp3_outstation.handler.operated == []


# -- opendnp3 master -> the repository's mock outstation -----------------------


class SOEHandler(opendnp3.ISOEHandler):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[tuple[str, int], Any] = {}
        self.lock = threading.Lock()

    def BeginFragment(self, info: Any) -> None:
        pass

    def EndFragment(self, info: Any) -> None:
        pass

    def Process(self, info: Any, values: Any) -> None:
        with self.lock:
            for v in values:
                self.values[(type(v.value).__name__, v.index)] = v.value.value


class MasterApp(opendnp3.IMasterApplication):
    def __init__(self) -> None:
        super().__init__()

    def OnReceiveIIN(self, iin: Any) -> None:
        pass

    def OnTaskStart(self, type: Any, id: Any) -> None:
        pass

    def OnTaskComplete(self, info: Any) -> None:
        pass

    def OnOpen(self) -> None:
        pass

    def OnClose(self) -> None:
        pass

    def AssignClassDuringStartup(self) -> bool:
        return False

    def Now(self) -> Any:
        return opendnp3.UTCTimestamp(int(time.time() * 1000))


@dataclass
class _Result:
    done: threading.Event = field(default_factory=threading.Event)
    summaries: list[Any] = field(default_factory=list)

    def __call__(self, result: Any) -> None:
        self.summaries.append(result.summary)
        self.done.set()


def _wait_for(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_opendnp3_master_against_the_mock(mock_process: int) -> None:
    manager = opendnp3.DNP3Manager(1)
    try:
        channel = manager.AddTCPClient(
            "client",
            opendnp3.levels.NOTHING,
            opendnp3.ChannelRetry.Default(),
            [opendnp3.IPEndpoint("127.0.0.1", mock_process)],
            "0.0.0.0",
            None,
        )
        soe = SOEHandler()
        config = opendnp3.MasterStackConfig()
        config.master.disableUnsolOnStartup = True
        config.link.LocalAddr = 1
        config.link.RemoteAddr = OUTSTATION
        master = channel.AddMaster("master", soe, MasterApp(), config)
        master.Enable()

        # Startup integrity poll.
        assert _wait_for(lambda: len([k for k in soe.values if k[0] == "Binary"]) == 16)
        assert [soe.values[("Binary", i)] for i in range(16)] == [bool(i % 2) for i in range(16)]
        assert [soe.values[("Analog", i)] for i in range(8)] == [float(i) for i in range(8)]

        result = _Result()
        master.SelectAndOperate(opendnp3.ControlRelayOutputBlock(opendnp3.OperationType.LATCH_ON), 6, result)
        assert result.done.wait(10)
        assert result.summaries == [opendnp3.TaskCompletion.SUCCESS]

        soe.values.clear()
        master.ScanClasses(opendnp3.ClassField.AllClasses(), soe)
        assert _wait_for(lambda: ("BinaryOutputStatus", 6) in soe.values)
        assert soe.values[("BinaryOutputStatus", 6)] is True
    finally:
        manager.Shutdown()


@pytest.mark.anyio
async def test_read_points_interoperates_with_independent_static_groups(opendnp3_outstation):
    """New point tool uses real independently encoded output/counter/double-bit data."""
    station = opendnp3_outstation
    station.update(
        (opendnp3.BinaryOutputStatus(True), 3),
        (opendnp3.AnalogOutputStatus(123.0), 3),
        (opendnp3.Counter(7654), 3),
        (opendnp3.DoubleBitBinary(opendnp3.DoubleBit.DETERMINED_ON), 3),
    )
    master = make_master(station.port)
    expected = {
        "binary_input": True,
        "analog_input": 30.0,
        "binary_output": True,
        "analog_output": 123.0,
        "counter": 7654,
        "double_bit_input": "ON",
    }
    for kind, value in expected.items():
        points, meta = await master.read_points(kind, OUTSTATION, 3, 1)
        assert points[0]["value"] == value, (kind, points, meta)
        assert points[0]["index"] == 3 and meta["attempts"] == 1
    # Frozen counter values start at zero in the independent database.
    frozen, _ = await master.read_points("frozen_counter", OUTSTATION, 3, 1)
    assert frozen[0]["value"] == 0
