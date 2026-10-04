"""Client cancellation: a control that was not sent yet is never sent afterwards.

Clarification of frozen rules 1 and 12. An MCP client's notifications/cancelled
reaches the tool as a cancellation of its task. Whether the call is queued
behind the lock, connecting, or between SELECT and OPERATE, no OPERATE or
DIRECT_OPERATE may leave afterwards. dnp3py writes a frame before its first
await, so the master checks for cancellation explicitly before every control.
"""

from __future__ import annotations

import time
from typing import Any

import anyio
import pytest
from dnp3.core.enums import FunctionCode

from dnp3_mcp import dnp3_master

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION
from .conftest import Received, binary_statics, crob_echo, make_master, response

pytestmark = pytest.mark.anyio


def _held_select(release: anyio.Event, seen: anyio.Event, *, connection: int | None = None) -> Any:
    """Script: answer every control, but hold the answer to a SELECT until `release` is set."""

    async def script(request: Received) -> list[bytes]:
        if request.function == FunctionCode.SELECT and connection in (None, request.connection):
            seen.set()
            await release.wait()
        return [crob_echo(request)]

    return script


async def test_cancel_while_the_select_is_unanswered_sends_no_operate(scripted: Any) -> None:
    release, seen = anyio.Event(), anyio.Event()
    outstation = await scripted(_held_select(release, seen))
    master = make_master(outstation.port, timeout_ms=5000)
    scope = anyio.CancelScope()

    async def control() -> None:
        with scope:
            await master.write_binary_output(OUTSTATION, 3, True)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(control)
        await seen.wait()
        scope.cancel()  # the client gives up while the outstation sits on the SELECT
    assert scope.cancelled_caught
    release.set()  # now the outstation answers the SELECT
    await anyio.sleep(0.5)
    assert outstation.functions() == [FunctionCode.SELECT]


async def test_cancel_while_queued_sends_nothing(scripted: Any) -> None:
    release, seen = anyio.Event(), anyio.Event()
    outstation = await scripted(_held_select(release, seen, connection=1))
    master = make_master(outstation.port, timeout_ms=5000)
    queued = anyio.CancelScope()
    done: list[dict[str, Any]] = []

    async def first() -> None:
        done.append(await master.write_binary_output(OUTSTATION, 3, True))

    async def second() -> None:
        with queued:
            await master.write_binary_output(OUTSTATION, 4, True)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(first)
        await seen.wait()
        tasks.start_soon(second)
        await anyio.sleep(0.2)  # the second control is waiting for the first to finish
        queued.cancel()
        release.set()
    assert queued.cancelled_caught
    assert done and done[0]["outcome"] == "operated"
    # Only the first control reached the outstation.
    assert outstation.connections == 1
    assert outstation.functions() == [FunctionCode.SELECT, FunctionCode.OPERATE]
    assert all(r.data[-12] == 3 for r in outstation.received)  # index 3 only


async def test_cancel_between_select_and_operate_stops_before_operate(
    scripted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port)
    scope = anyio.CancelScope()
    attempted: list[FunctionCode] = []

    original_request = dnp3_master._Session.request

    async def spy(self: Any, fragment: Any) -> Any:
        attempted.append(fragment.header.function)
        return await original_request(self, fragment)

    original_step = master._control_step

    async def step(session: Any, function: str, *args: Any) -> Any:
        iin = await original_step(session, function, *args)
        if function == "SELECT":
            scope.cancel()  # the client's cancel lands just as the SELECT answer is processed
        return iin

    monkeypatch.setattr(dnp3_master._Session, "request", spy)
    monkeypatch.setattr(master, "_control_step", step)
    with scope:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert scope.cancelled_caught
    # The OPERATE was not even attempted (on Python 3.12+ an attempt could put it on the wire).
    assert attempted == [FunctionCode.SELECT]
    await anyio.sleep(0.2)
    assert outstation.functions() == [FunctionCode.SELECT]


@pytest.mark.parametrize("mode", ["select_before_operate", "direct_operate"])
async def test_cancel_while_connecting_sends_nothing(scripted: Any, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port, control_mode=mode)
    connecting = anyio.Event()
    steps: list[str] = []

    async def slow_open(self: Any) -> None:
        connecting.set()
        await anyio.sleep(3600)

    original_step = master._control_step

    async def step(session: Any, function: str, *args: Any) -> Any:
        steps.append(function)
        return await original_step(session, function, *args)

    monkeypatch.setattr(dnp3_master._Session, "open", slow_open)
    monkeypatch.setattr(master, "_control_step", step)
    scope = anyio.CancelScope()

    async def control() -> None:
        with scope:
            await master.write_binary_output(OUTSTATION, 3, True)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(control)
        await connecting.wait()
        scope.cancel()
    assert scope.cancelled_caught
    assert steps == []
    assert outstation.received == []


def test_default_deadlines_stay_under_fifty_seconds() -> None:
    master = make_master(1, timeout_ms=5000, max_retries=3)  # the defaults
    assert master.read_deadline_s < 50
    assert master.control_deadline_s < 50


async def test_closing_stops_a_control_waiting_for_its_select(scripted: Any) -> None:
    release, seen = anyio.Event(), anyio.Event()
    outstation = await scripted(_held_select(release, seen))
    master = make_master(outstation.port, timeout_ms=5000)
    errors: list[Exception] = []

    async def control() -> None:
        try:
            await master.write_binary_output(OUTSTATION, 3, True)
        except dnp3_master.DNP3ControlError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(control)
        await seen.wait()
        master.shutdown("the MCP client disconnected (stdin closed)")
    release.set()
    await anyio.sleep(0.5)
    assert outstation.functions() == [FunctionCode.SELECT]
    (error,) = errors
    assert (error.meta["outcome"], error.meta["request_sent"], error.meta["closing"]) == ("rejected", True, True)


async def test_nothing_is_sent_after_closing(scripted: Any) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port)
    master.shutdown("the MCP client disconnected (stdin closed)")
    with pytest.raises(dnp3_master.DNP3ControlError, match="No control was sent") as excinfo:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("not_sent", False)
    with pytest.raises(dnp3_master.DNP3ConnectionError, match="abandoned"):
        await master.read_binary_inputs(OUTSTATION, 0, 1)
    await anyio.sleep(0.2)
    assert outstation.connections == 0 and outstation.received == []


# -- the overall deadline includes waiting for the lock (rule 12 clarification) -------------------


def _slow_reads(delay: float) -> Any:
    """Script: answer READs after `delay` seconds, controls at once."""

    async def script(request: Received) -> list[bytes]:
        if request.function == FunctionCode.READ:
            await anyio.sleep(delay)
            return [response(binary_statics(0, [True]), seq=request.seq)]
        return [crob_echo(request)]

    return script


async def _hold_the_lock(master: Any, seconds: float, held: anyio.Event) -> None:
    """Stands in for slow earlier calls: keeps the master busy for `seconds`."""
    async with master._lock:
        held.set()
        await anyio.sleep(seconds)


@pytest.mark.parametrize("mode", ["direct_operate", "select_before_operate"])
async def test_control_queued_past_its_deadline_is_never_sent(scripted: Any, mode: str) -> None:
    # The MCP SDK client sends no notifications/cancelled when its own timeout fires,
    # so the server's deadline is the only thing that stops a control queued behind slow calls.
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port, timeout_ms=200, control_mode=mode)
    master.deadline_slack_s = 0.1  # control deadline 0.7 s
    held = anyio.Event()
    errors: list[dnp3_master.DNP3ControlError] = []
    finished: list[float] = []

    async def control() -> None:
        started = time.monotonic()
        try:
            await master.write_binary_output(OUTSTATION, 3, True)
        except dnp3_master.DNP3ControlError as exc:
            errors.append(exc)
        finished.append(time.monotonic() - started)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_hold_the_lock, master, 2.0, held)
        await held.wait()
        tasks.start_soon(control)
    await anyio.sleep(0.5)  # the lock is free again: anything still pending would go out now

    (error,) = errors
    assert (error.meta["outcome"], error.meta["request_sent"], error.meta["queued"]) == ("not_sent", False, True)
    assert "will not be sent later" in str(error)
    assert finished[0] < 1.5  # it gave up at its own deadline, not when the lock came free
    assert outstation.connections == 0 and outstation.received == []


async def test_read_queued_past_its_deadline_fails_cleanly(scripted: Any) -> None:
    outstation = await scripted(lambda request: [response(binary_statics(0, [True]), seq=request.seq)])
    master = make_master(outstation.port, timeout_ms=200, max_retries=0)
    master.deadline_slack_s = 0.1  # read deadline 0.5 s
    held = anyio.Event()
    errors: list[Exception] = []

    async def read() -> None:
        try:
            await master.read_binary_inputs(OUTSTATION, 0, 1)
        except dnp3_master.DNP3TimeoutError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_hold_the_lock, master, 1.5, held)
        await held.wait()
        tasks.start_soon(read)
    (error,) = errors
    assert error.meta["queued"] is True and "did not start" in str(error)
    assert outstation.connections == 0


async def test_queued_calls_behind_slow_reads_end_on_time(scripted: Any) -> None:
    # The reviewer's scenario: several reads queued, then a control. Every call ends
    # within its own deadline, counted from when it was made, and the control is
    # either sent within its deadline or not at all.
    outstation = await scripted(_slow_reads(0.8))
    master = make_master(outstation.port, timeout_ms=1000, max_retries=0, control_mode="direct_operate")
    master.deadline_slack_s = 0.1
    durations: dict[str, float] = {}
    outcome: list[str] = []
    began = time.monotonic()

    async def read(name: str) -> None:
        started = time.monotonic()
        try:
            await master.read_binary_inputs(OUTSTATION, 0, 1)
        except dnp3_master.DNP3TimeoutError:
            pass
        durations[name] = time.monotonic() - started

    async def control() -> None:
        started = time.monotonic()
        try:
            outcome.append((await master.write_binary_output(OUTSTATION, 3, True))["outcome"])
        except dnp3_master.DNP3ControlError as exc:
            outcome.append(exc.meta["outcome"])
        durations["control"] = time.monotonic() - started

    async with anyio.create_task_group() as tasks:
        for number in range(3):
            tasks.start_soon(read, f"read{number}")
            await anyio.sleep(0.01)
        tasks.start_soon(control)
    await anyio.sleep(0.5)
    for name, took in durations.items():
        limit = master.control_deadline_s if name == "control" else master.read_deadline_s
        assert took <= limit + 0.3, (name, took, limit)
    operates = [r for r in outstation.received if r.function == FunctionCode.DIRECT_OPERATE]
    if outcome == ["not_sent"]:
        assert operates == []
    else:
        assert outcome == ["operated"] and len(operates) == 1
    assert time.monotonic() - began < master.control_deadline_s + 1.5


async def test_cancelled_operate_is_logged_as_unknown(scripted: Any, caplog: pytest.LogCaptureFixture) -> None:
    release, seen = anyio.Event(), anyio.Event()

    async def script(request: Received) -> list[bytes]:
        if request.function == FunctionCode.DIRECT_OPERATE:
            seen.set()
            await release.wait()
        return [crob_echo(request)]

    outstation = await scripted(script)
    master = make_master(outstation.port, timeout_ms=5000, control_mode="direct_operate")
    scope = anyio.CancelScope()

    async def control() -> None:
        with scope:
            await master.write_binary_output(OUTSTATION, 3, True)

    with caplog.at_level("WARNING", logger="dnp3_mcp.dnp3_master"):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(control)
            await seen.wait()
            scope.cancel()  # the DIRECT_OPERATE is already on the wire
        release.set()
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "outcome unknown, request_sent true" in messages
    assert "may have been applied" in messages


async def test_cancelled_while_queued_is_logged_as_not_sent(scripted: Any, caplog: pytest.LogCaptureFixture) -> None:
    outstation = await scripted(_slow_reads(0.5))
    master = make_master(outstation.port, timeout_ms=5000)
    scope = anyio.CancelScope()

    async def control() -> None:
        with scope:
            await master.write_binary_output(OUTSTATION, 3, True)

    with caplog.at_level("WARNING", logger="dnp3_mcp.dnp3_master"):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(master.read_binary_inputs, OUTSTATION, 0, 1)
            await anyio.sleep(0.05)
            tasks.start_soon(control)
            await anyio.sleep(0.1)
            scope.cancel()
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "during queued" in messages and "outcome not_sent, request_sent false" in messages
    assert outstation.functions() == [FunctionCode.READ]


# -- one absolute deadline per tool call ----------------------------------------------------------


async def test_operate_after_the_call_deadline_is_refused(scripted: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port, timeout_ms=300)
    master.deadline_slack_s = 0.1  # control deadline 1.0 s
    original_step = master._control_step

    async def step(session: Any, function: str, *args: Any) -> Any:
        iin = await original_step(session, function, *args)
        if function == "SELECT":
            # Block the event loop past the deadline, so its timer cannot fire before the
            # OPERATE is attempted: only an explicit check of the clock can stop it.
            time.sleep(master.control_deadline_s + 0.1)
        return iin

    monkeypatch.setattr(master, "_control_step", step)
    with pytest.raises(dnp3_master.DNP3ControlError, match="OPERATE was not sent") as excinfo:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("rejected", True)
    await anyio.sleep(0.3)
    assert outstation.functions() == [FunctionCode.SELECT]


async def test_one_deadline_covers_lock_wait_and_select(scripted: Any) -> None:
    # 0.6 s waiting for the lock plus a 0.6 s SELECT answer exceed the 1.0 s deadline:
    # had either step started a fresh deadline, the OPERATE would have gone out.
    async def script(request: Received) -> list[bytes]:
        if request.function == FunctionCode.SELECT:
            await anyio.sleep(0.6)
        return [crob_echo(request)]

    outstation = await scripted(script)
    master = make_master(outstation.port, timeout_ms=1000)
    master.deadline_slack_s = -2.0  # control deadline 3 x 1.0 - 2.0 = 1.0 s
    held = anyio.Event()
    errors: list[dnp3_master.DNP3ControlError] = []
    took: list[float] = []

    async def control() -> None:
        started = time.monotonic()
        try:
            await master.write_binary_output(OUTSTATION, 3, True)
        except dnp3_master.DNP3ControlError as exc:
            errors.append(exc)
        took.append(time.monotonic() - started)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_hold_the_lock, master, 0.6, held)
        await held.wait()
        tasks.start_soon(control)
    await anyio.sleep(0.5)
    (error,) = errors
    assert (error.meta["outcome"], error.meta["request_sent"]) == ("rejected", True)
    assert took[0] < 1.3
    assert outstation.functions() == [FunctionCode.SELECT]


async def test_one_deadline_covers_every_read_retry(scripted: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    silent = await scripted(None)
    master = make_master(silent.port, timeout_ms=300, max_retries=5)
    monkeypatch.setattr(type(master), "read_deadline_s", property(lambda self: 0.5))
    started = time.monotonic()
    with pytest.raises(dnp3_master.DNP3TimeoutError, match="overall deadline of 0.5 s"):
        await master.read_binary_inputs(OUTSTATION, 0, 1)
    assert time.monotonic() - started < 0.9  # six 0.3 s attempts would take 1.8 s
    assert silent.connections <= 2


async def test_one_deadline_covers_lock_wait_and_read(scripted: Any) -> None:
    # 0.3 s waiting for the lock plus a 0.3 s answer exceed the 0.5 s deadline.
    outstation = await scripted(_slow_reads(0.3))
    master = make_master(outstation.port, timeout_ms=1000, max_retries=0)
    master.deadline_slack_s = -1.5  # read deadline 2 x 1.0 - 1.5 = 0.5 s
    held = anyio.Event()
    errors: list[Exception] = []

    async def read() -> None:
        try:
            await master.read_binary_inputs(OUTSTATION, 0, 1)
        except dnp3_master.DNP3TimeoutError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_hold_the_lock, master, 0.3, held)
        await held.wait()
        tasks.start_soon(read)
    (error,) = errors
    assert "overall deadline of 0.5 s" in str(error)
