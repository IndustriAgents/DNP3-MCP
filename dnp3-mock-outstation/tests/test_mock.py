"""Mock outstation: configuration, points, controls, DNP3 on the wire, shutdown (known issue 10)."""

from __future__ import annotations

import asyncio
import random
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from dnp3.core.enums import ControlCode
from dnp3.master import DefaultSOEHandler, Master, MasterConfig, MasterTcpRunner

import dnp3_mock_outstation as mock_module
from dnp3_mock_outstation import DNP3MockOutstation, MockCommandHandler, MockConfig, build_database, parse_args

MOCK_PATH = Path(mock_module.__file__).resolve()


def test_defaults_match_the_server_defaults() -> None:
    config = parse_args([], env={})
    # The MCP server defaults to DNP3_PORT=20000; tools/docs use outstation 10.
    assert (config.host, config.port, config.outstation_address) == ("127.0.0.1", 20000, 10)
    assert config.update_interval == 1.0 and config.verbose is False


def test_environment_is_read_when_parsing_not_at_import() -> None:
    env = {
        "MOCK_DNP3_HOST": "0.0.0.0",
        "MOCK_DNP3_PORT": "20500",
        "MOCK_DNP3_OUTSTATION_ADDRESS": "4",
        "MOCK_DNP3_UPDATE_INTERVAL": "0",
        "MOCK_DNP3_VERBOSE": "true",
    }
    config = parse_args([], env=env)
    assert (config.host, config.port, config.outstation_address, config.update_interval, config.verbose) == (
        "0.0.0.0",
        20500,
        4,
        0.0,
        True,
    )
    assert parse_args(["--port", "20600"], env=env).port == 20600


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        ([], {"MOCK_DNP3_PORT": "abc"}),
        ([], {"MOCK_DNP3_VERBOSE": "maybe"}),
        (["--port", "70000"], {}),
        (["--outstation-address", "65520"], {}),
        (["--update-interval", "-1"], {}),
    ],
)
def test_invalid_settings_exit(argv: list[str], env: dict[str, str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv, env=env)


def test_points() -> None:
    db = build_database()
    assert [db.binary_inputs[i].value for i in range(16)] == [bool(i % 2) for i in range(16)]
    assert [db.analog_inputs[i].value for i in range(8)] == [float(i) for i in range(8)]
    assert [db.binary_outputs[i].value for i in range(8)] == [False] * 8
    assert all(p.is_online for p in db.binary_inputs.values())
    assert db.event_buffer.total_count == 0  # no spurious events at start


def test_command_handler() -> None:
    db = build_database()
    handler = MockCommandHandler(db)
    assert handler.select_binary_output(2, ControlCode.LATCH_ON, 1, 0, 0).is_success
    assert db.binary_outputs[2].value is False  # SELECT does not act
    assert handler.operate_binary_output(2, ControlCode.LATCH_ON, 1, 0, 0, 0).is_success
    assert db.binary_outputs[2].value is True
    assert handler.direct_operate_binary_output(2, ControlCode.LATCH_OFF, 1, 0, 0).is_success
    assert db.binary_outputs[2].value is False
    assert not handler.direct_operate_binary_output(42, ControlCode.LATCH_ON, 1, 0, 0).is_success
    assert not handler.direct_operate_binary_output(2, ControlCode.PULSE_ON, 1, 100, 100).is_success
    assert db.binary_outputs[2].value is False


def test_random_walk_steps_by_at_most_one() -> None:
    mock = DNP3MockOutstation(MockConfig(port=0, update_interval=0), rng=random.Random(1))
    before = {i: p.value for i, p in mock.database.analog_inputs.items()}
    for _ in range(5):
        previous = {i: p.value for i, p in mock.database.analog_inputs.items()}
        mock.step()
        for i, point in mock.database.analog_inputs.items():
            assert abs(point.value - previous[i]) <= 1.0
    assert before != {i: p.value for i, p in mock.database.analog_inputs.items()}
    assert mock.database.event_buffer.total_count > 0  # changes queue Class 2 events


def test_speaks_dnp3_and_keeps_its_update_task() -> None:
    async def scenario() -> None:
        mock = DNP3MockOutstation(MockConfig(port=0, outstation_address=10, update_interval=0.05))
        await mock.start()
        try:
            assert mock._update_task is not None and not mock._update_task.done()
            port = mock.local_address[1]
            handler = DefaultSOEHandler()
            master = Master(handler=handler, config=MasterConfig(address=1, outstation_address=10))
            async with MasterTcpRunner(master=master, host="127.0.0.1", port=port, response_timeout=3) as runner:
                await runner.integrity_poll()
            assert len(handler.binary_inputs) == 16 and handler.binary_inputs[3].value is True
            assert len(handler.analog_inputs) == 8
            assert len(handler.binary_outputs) == 8
        finally:
            update_task = mock._update_task
            await mock.stop()
        assert update_task is not None and update_task.done()
        assert not mock.runner.is_running

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_stops_promptly_with_a_client_connected(tmp_path: Path) -> None:
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
            match = re.search(r"DNP3 mock outstation listening on [^:]+:(\d+)", log_path.read_text())
            if match:
                port = int(match.group(1))
            elif proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail(log_path.read_text())
            else:
                time.sleep(0.05)
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            time.sleep(0.3)  # let the outstation accept it
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0, log_path.read_text()
        assert "Stopping DNP3 mock outstation" in log_path.read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipes")
def test_watch_stdin_stops_the_mock_when_its_parent_goes_away(tmp_path: Path) -> None:
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
        while "listening on" not in log_path.read_text():
            assert proc.poll() is None and time.monotonic() < deadline, log_path.read_text()
            time.sleep(0.05)
        assert proc.poll() is None
        assert proc.stdin is not None
        proc.stdin.close()  # what the kernel does when the parent dies
        assert proc.wait(timeout=10) == 0, log_path.read_text()
        assert "stdin closed" in log_path.read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_watch_stdin_is_off_by_default() -> None:
    assert parse_args([], env={}).watch_stdin is False
    assert parse_args(["--watch-stdin"], env={}).watch_stdin is True
