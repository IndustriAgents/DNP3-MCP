"""Client cancellation: a control that was not sent yet is never sent afterwards.

Clarification of frozen rules 1 and 12. An MCP client's notifications/cancelled
reaches the tool as a cancellation of its task. Whether the call is queued
behind the lock, connecting, or between SELECT and OPERATE, no OPERATE or
DIRECT_OPERATE may leave afterwards. dnp3py writes a frame before its first
await, so the master checks for cancellation explicitly before every control.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from dnp3.core.enums import FunctionCode

from dnp3_mcp import dnp3_master

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION
from .conftest import Received, crob_echo, make_master

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
