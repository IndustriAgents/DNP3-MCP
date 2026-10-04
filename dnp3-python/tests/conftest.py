"""Shared fixtures.

The mock outstation lives in ../dnp3-mock-outstation. It only needs dnp3py,
which is also a dependency of this package, so the tests import it (or run it
as a subprocess) with this project's interpreter.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dnp3.application.parser import parse_request
from dnp3.core.enums import FunctionCode, LinkFunctionCode
from dnp3.datalink.builder import build_ack, build_unconfirmed_user_data
from dnp3.datalink.parser import FrameParser
from dnp3.transport.reassembler import Reassembler
from dnp3.transport.segment import TransportSegment
from dnp3.transport.segmenter import Segmenter

from dnp3_mcp.config import DNP3Config
from dnp3_mcp.dnp3_master import CROB_SIZE, DNP3Master

MOCK_PATH = Path(__file__).resolve().parents[2] / "dnp3-mock-outstation" / "dnp3_mock_outstation.py"
MOCK_OUTSTATION_ADDRESS = 10

ENV_VARS = (
    "DNP3_CONNECTION_TYPE",
    "DNP3_HOST",
    "DNP3_PORT",
    "DNP3_SERIAL_PORT",
    "DNP3_MASTER_ADDRESS",
    "DNP3_LOCAL_ADDRESS",
    "DNP3_TIMEOUT",
    "DNP3_MAX_RETRIES",
    "DNP3_CONTROL_MODE",
    "DNP3_WRITES_ENABLED",
    "POINT_MAP_FILE",
)


@pytest.fixture
def anyio_backend() -> str:
    # dnp3py is built on asyncio.
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own DNP3_* settings out of every test."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


_mock_module: ModuleType | None = None


def load_mock() -> ModuleType:
    global _mock_module
    if _mock_module is None:
        if not MOCK_PATH.is_file():
            pytest.skip(f"mock outstation not found at {MOCK_PATH}")
        spec = importlib.util.spec_from_file_location("dnp3_mock_outstation", MOCK_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["dnp3_mock_outstation"] = module
        spec.loader.exec_module(module)
        _mock_module = module
    return _mock_module


@dataclass
class RunningMock:
    mock: Any  # DNP3MockOutstation
    port: int

    @property
    def database(self) -> Any:
        return self.mock.database


@pytest.fixture
async def mock_outstation() -> AsyncIterator[RunningMock]:
    """The repository's mock outstation, in process, on an ephemeral port, with no random walk."""
    module = load_mock()
    mock = module.DNP3MockOutstation(
        module.MockConfig(host="127.0.0.1", port=0, outstation_address=MOCK_OUTSTATION_ADDRESS, update_interval=0)
    )
    await mock.start()
    try:
        assert mock.local_address is not None
        yield RunningMock(mock=mock, port=mock.local_address[1])
    finally:
        await mock.stop()


@dataclass(frozen=True)
class Received:
    """One application request a ScriptedOutstation received."""

    connection: int
    function: FunctionCode
    seq: int
    uns: bool
    data: bytes  # the whole application fragment


Script = Callable[[Received], Iterable[bytes]]


class ScriptedOutstation:
    """A hand-driven DNP3 outstation over TCP, for protocol edge cases.

    It answers RESET_LINK_STATE, reassembles each request, records it, and
    sends back whatever application fragments `script` returns (none: stays
    silent). CONFIRMs are recorded and never passed to the script.
    """

    def __init__(self, script: Script | None = None, *, address: int = MOCK_OUTSTATION_ADDRESS) -> None:
        self.script = script
        self.address = address
        self.connections = 0
        self.received: list[Received] = []
        self._server: asyncio.base_events.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    def functions(self) -> list[FunctionCode]:
        return [r.function for r in self.received]

    def confirms(self) -> list[Received]:
        return [r for r in self.received if r.function == FunctionCode.CONFIRM]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        connection = self.connections
        parser, reassembler, segmenter = FrameParser(), Reassembler(max_fragment_size=4096), Segmenter()
        try:
            while data := await reader.read(4096):
                for frame in parser.feed(data):
                    if frame.header.destination != self.address or not frame.header.control.prm:
                        continue
                    master = frame.header.source
                    if frame.header.control.function_code == LinkFunctionCode.PRI_RESET_LINK_STATE:
                        writer.write(build_ack(master, self.address, False).to_bytes())
                        continue
                    if not frame.user_data:
                        continue
                    result = reassembler.add(TransportSegment.from_bytes(frame.user_data))
                    if result is None:
                        continue
                    request = parse_request(result.data)
                    received = Received(
                        connection=connection,
                        function=request.header.function,
                        seq=request.header.control.seq,
                        uns=request.header.control.uns,
                        data=bytes(result.data),
                    )
                    self.received.append(received)
                    if received.function == FunctionCode.CONFIRM or self.script is None:
                        continue
                    for fragment in self.script(received):
                        for segment in segmenter.segment(fragment):
                            frame_out = build_unconfirmed_user_data(master, self.address, False, segment.to_bytes())
                            writer.write(frame_out.to_bytes())
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def scripted() -> AsyncIterator[Callable[[Script | None], Awaitable[ScriptedOutstation]]]:
    """Factory for started ScriptedOutstations, stopped after the test."""
    started: list[ScriptedOutstation] = []

    async def make(script: Script | None = None) -> ScriptedOutstation:
        outstation = ScriptedOutstation(script)
        await outstation.start()
        started.append(outstation)
        return outstation

    try:
        yield make
    finally:
        for outstation in started:
            await outstation.stop()


@pytest.fixture
async def silent_server(scripted: Any) -> ScriptedOutstation:
    """An outstation that records what it receives and never answers."""
    return await scripted(None)


# -- application fragments for scripted outstations ---------------------------


def response(objects: bytes = b"", *, seq: int, con: bool = False, uns: bool = False, fin: bool = True) -> bytes:
    """A RESPONSE (or UNSOLICITED_RESPONSE) fragment, FIR set, IIN clear."""
    control = 0x80 | (0x40 if fin else 0) | (0x20 if con else 0) | (0x10 if uns else 0) | (seq & 0x0F)
    return bytes([control, 0x82 if uns else 0x81, 0, 0]) + objects


def binary_events(*events: tuple[int, bool]) -> bytes:
    """g2v1 binary input events, qualifier 0x17."""
    body = b"".join(bytes([index, 0x81 if value else 0x01]) for index, value in events)
    return bytes([2, 1, 0x17, len(events)]) + body


def binary_statics(start: int, values: list[bool]) -> bytes:
    """g1v2 binary inputs with flags, qualifier 0x00 (start-stop)."""
    body = bytes(0x81 if v else 0x01 for v in values)
    return bytes([1, 2, 0x00, start, start + len(values) - 1]) + body


# A g50v4 block, which dnp3py 0.6.0 cannot size: it stops decoding there
# (opendnp3 includes such a block in its Class 0 response).
UNDECODABLE = bytes([50, 4, 0x00, 0, 0]) + bytes(11)


def crob_echo(request: Received, status: int = 0, *, tamper: bool = False) -> bytes:
    """Echo a single-CROB request's objects with `status`, optionally changing its control code."""
    objects = bytearray(request.data[2:])
    objects[-1] = status
    if tamper:
        objects[-CROB_SIZE] ^= 0x07  # LATCH_ON <-> LATCH_OFF
    return response(bytes(objects), seq=request.seq)


@pytest.fixture
def closed_port() -> Iterator[int]:
    """A port with nothing listening on it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    yield port


def make_master(port: int, **overrides: Any) -> DNP3Master:
    settings: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "timeout_ms": 2000,
        "max_retries": 0,
        "writes_enabled": True,
    }
    settings.update(overrides)
    return DNP3Master(DNP3Config(**settings))


LISTENING = re.compile(r"DNP3 mock outstation listening on [^:]+:(\d+)")


@pytest.fixture
def mock_process(tmp_path: Path) -> Iterator[int]:
    """Run the mock outstation script on an ephemeral port; yield the port.

    The mock watches its stdin pipe: if pytest is killed, the pipe closes and
    the mock exits instead of being left running.
    """
    if not MOCK_PATH.is_file():
        pytest.skip(f"mock outstation not found at {MOCK_PATH}")
    log_path = tmp_path / "mock.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, str(MOCK_PATH), "--port", "0", "--update-interval", "0", "--watch-stdin"],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
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
        if proc.stdin is not None:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
