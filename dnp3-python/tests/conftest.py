"""Shared fixtures.

The mock outstation lives in ../dnp3-mock-outstation. It only needs dnp3py,
which is also a dependency of this package, so the tests import it (or run it
as a subprocess) with this project's interpreter.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dnp3_mcp.config import DNP3Config
from dnp3_mcp.dnp3_master import DNP3Master

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


@pytest.fixture
async def silent_server() -> AsyncIterator[int]:
    """A TCP server that accepts connections and never answers."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(1024):
                pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


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
