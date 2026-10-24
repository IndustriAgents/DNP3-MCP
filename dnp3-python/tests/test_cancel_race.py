"""The cancel race: a client cancellation that lands in the same loop step as a wait completing.

The MCP SDK's RequestResponder.cancel() cancels the request's scope synchronously.
anyio does not interrupt a task whose wait has just completed (a lock was granted,
a connect finished, an answer arrived), so the task runs one more step, and dnp3py
writes a frame before its first await. The master therefore checks the request's
scope explicitly right before every send (`_before_send`). These tests cancel in
exactly that step, many times over, and require that nothing is sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from dnp3.core.enums import FunctionCode
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.session import BaseSession, RequestResponder

from dnp3_mcp import dnp3_master, tools
from dnp3_mcp.config import DNP3Config, ServerConfig
from dnp3_mcp.server import DNP3MCPServer
from dnp3_mcp.tools import request_cancel_scope

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION
from .conftest import crob_echo, make_master
from .test_tools import FakeMaster

pytestmark = pytest.mark.anyio

ROUNDS = 40
CONTROLS = {FunctionCode.SELECT, FunctionCode.OPERATE, FunctionCode.DIRECT_OPERATE}


# -- the mcp internals this relies on (pinned mcp<2) -------------------------------------------------


def test_mcp_internals_are_where_the_server_expects_them() -> None:
    # Fails, rather than silently losing the protection, if a new mcp release moves them.
    assert "_in_flight" in BaseSession.__init__.__code__.co_names
    assert "_cancel_scope" in RequestResponder.__init__.__code__.co_names
    assert "_cancel_scope" in RequestResponder.cancel.__code__.co_names


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_binary_output", {"outstation_address": 10, "index": 3, "value": True}),
        ("read_binary_inputs", {"outstation_address": 10, "start_index": 0, "count": 1}),
        ("read_analog_inputs", {"outstation_address": 10, "start_index": 0, "count": 1}),
        ("poll_class", {"outstation_address": 10, "event_class": 1}),
    ],
)
async def test_each_device_tool_hands_the_request_scope_to_the_master(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    config = ServerConfig(dnp3=DNP3Config(writes_enabled=True))
    master = FakeMaster(config.dnp3)
    server = DNP3MCPServer(config, master=master)
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool(tool, arguments)
    assert json.loads(result.content[0].text)["success"] is True
    (scope,) = master.request_scopes
    assert isinstance(scope, anyio.CancelScope), "the MCP request's cancel scope was not found"


async def test_alias_writes_hand_the_request_scope_to_the_master(tmp_path: Path) -> None:
    points = tmp_path / "points.json"
    points.write_text(json.dumps({"trip": {"outstation": 10, "type": "binary_output", "index": 3}}))
    config = ServerConfig(dnp3=DNP3Config(writes_enabled=True), point_map_path=points)
    master = FakeMaster(config.dnp3)
    server = DNP3MCPServer(config, master=master)
    async with create_connected_server_and_client_session(server.mcp) as session:
        await session.call_tool("write_point_by_alias", {"alias": "trip", "value": True})
    (scope,) = master.request_scopes
    assert isinstance(scope, anyio.CancelScope)


def test_no_request_scope_outside_a_request() -> None:
    server = DNP3MCPServer(ServerConfig(dnp3=DNP3Config()), master=FakeMaster(DNP3Config()))
    assert request_cancel_scope(server.mcp) is None


# -- same-step cancellations --------------------------------------------------------------------------


async def _control(master: dnp3_master.DNP3Master, request_scope: anyio.CancelScope, index: int = 3) -> None:
    """A control as a tool call would run it: inside its request's cancel scope."""
    with request_scope:
        await master.write_binary_output(OUTSTATION, index, True, request_scope=request_scope)


async def _wait_for_waiter(lock: anyio.Lock) -> None:
    with anyio.fail_after(5):
        while lock.statistics().tasks_waiting == 0:
            await anyio.sleep(0)


@pytest.mark.parametrize("mode", ["select_before_operate", "direct_operate"])
async def test_cancel_in_the_step_the_lock_is_granted(scripted: Any, mode: str) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port, control_mode=mode)
    for _ in range(ROUNDS):
        request_scope = anyio.CancelScope()
        async with anyio.create_task_group() as tasks:
            await master._lock.acquire()
            tasks.start_soon(_control, master, request_scope)
            await _wait_for_waiter(master._lock)
            master._lock.release()  # grants the lock to the queued control...
            request_scope.cancel()  # ...and the client's cancel lands in the same step
        assert request_scope.cancelled_caught
    await anyio.sleep(0.2)
    assert not CONTROLS & set(outstation.functions())


@pytest.mark.parametrize("mode", ["select_before_operate", "direct_operate"])
async def test_cancel_in_the_step_the_connect_completes(
    scripted: Any, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port, control_mode=mode)
    current: list[anyio.CancelScope] = []
    original_open = dnp3_master._Session.open

    async def open_then_cancel(self: Any) -> None:
        await original_open(self)
        current[-1].cancel()  # the connect has completed; no await before the first send

    monkeypatch.setattr(dnp3_master._Session, "open", open_then_cancel)
    for _ in range(ROUNDS):
        current.append(anyio.CancelScope())
        await _control(master, current[-1])
        assert current[-1].cancelled_caught
    await anyio.sleep(0.2)
    assert outstation.connections == ROUNDS
    assert not CONTROLS & set(outstation.functions())


async def test_cancel_in_the_step_the_select_is_answered(scripted: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port)
    current: list[anyio.CancelScope] = []
    original_step = master._control_step

    async def step_then_cancel(session: Any, function: str, *args: Any) -> Any:
        iin = await original_step(session, function, *args)
        if function == "SELECT":
            current[-1].cancel()  # the SELECT answer has been processed; OPERATE is next
        return iin

    monkeypatch.setattr(master, "_control_step", step_then_cancel)
    for _ in range(ROUNDS):
        current.append(anyio.CancelScope())
        await _control(master, current[-1])
        assert current[-1].cancelled_caught
    await anyio.sleep(0.2)
    functions = outstation.functions()
    assert functions.count(FunctionCode.SELECT) == ROUNDS
    assert FunctionCode.OPERATE not in functions


async def test_cancelled_request_scope_alone_stops_the_send(scripted: Any) -> None:
    # The explicit check on its own: the request scope is already cancelled when the
    # control reaches its first send, but it does not enclose the call, so no
    # checkpoint would ever raise. Only the cancel_called check stops it.
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port)
    detached = anyio.CancelScope()
    detached.cancel()
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await master.write_binary_output(OUTSTATION, 3, True, request_scope=detached)
    await anyio.sleep(0.2)
    assert outstation.connections == 0 and outstation.received == []


# -- through the real SDK: RequestResponder.cancel() in the same step ---------------------------------


async def test_real_responder_cancel_in_the_lock_grant_step_keeps_the_server_alive(
    scripted: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The SDK has already answered "Request cancelled", so the tool must end as a cancellation.

    Returning an envelope instead would make the SDK answer the request a second
    time, which fails its `assert not self._completed` and can take the server down.
    """
    outstation = await scripted(lambda request: [crob_echo(request)])
    config = ServerConfig(
        dnp3=DNP3Config(host="127.0.0.1", port=outstation.port, timeout_ms=2000, max_retries=0, writes_enabled=True)
    )
    server = DNP3MCPServer(config)
    master = server.master
    responders: list[Any] = []
    find_scope = tools.request_cancel_scope

    def capture(mcp: Any) -> Any:
        context = mcp._mcp_server.request_context
        responders.append(context.session._in_flight[context.request_id])
        return find_scope(mcp)

    from dnp3_mcp.protocol import tools as protocol_tools

    monkeypatch.setattr(protocol_tools, "request_cancel_scope", capture)
    arguments = {"outstation_address": OUTSTATION, "index": 3, "value": True}
    with caplog.at_level("WARNING", logger="dnp3_mcp.dnp3_master"):
        async with create_connected_server_and_client_session(server.mcp) as session:
            for _ in range(20):
                responders.clear()
                outcome: list[str] = []

                async def call(outcome: list[str]) -> None:
                    try:
                        await session.call_tool("write_binary_output", arguments)
                        outcome.append("answered")
                    except McpError as exc:
                        outcome.append(exc.error.message)

                with anyio.fail_after(10):
                    await master._lock.acquire()
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(call, outcome)
                        await _wait_for_waiter(master._lock)
                        master._lock.release()  # the queued control is granted the lock...
                        # ...and the SDK's own cancel() runs its first, synchronous step now:
                        # it cancels the request's scope and marks the request answered.
                        await responders[0].cancel()
                assert outcome == ["Request cancelled"]
                ping = await session.call_tool("ping", {})  # the server is still alive
                assert json.loads(ping.content[0].text)["success"] is True
    await anyio.sleep(0.2)
    assert not CONTROLS & set(outstation.functions())
    assert "nothing more will be sent (outcome not_sent, request_sent false)" in caplog.text


@pytest.mark.parametrize("operation", ["static", "event_poll"])
async def test_read_cancelled_when_connection_completes_never_sends(operation, monkeypatch):
    master = make_master(1)
    scope = anyio.CancelScope()
    sent = []
    closed = []

    async def open_then_cancel(self):
        scope.cancel()

    async def request(self, fragment):
        # dnp3py can send before its next checkpoint. Observe invocation itself.
        sent.append(fragment)
        await anyio.lowlevel.checkpoint()

    async def close(self):
        closed.append(True)

    monkeypatch.setattr(dnp3_master._Session, "open", open_then_cancel)
    monkeypatch.setattr(dnp3_master._Session, "request", request)
    monkeypatch.setattr(dnp3_master._Session, "close", close)
    with scope:
        if operation == "static":
            await master.read_binary_inputs(OUTSTATION, 0, 1, request_scope=scope)
        else:
            await master.poll_class(OUTSTATION, 1, request_scope=scope)
    assert scope.cancelled_caught
    assert closed == [True]
    assert sent == []
    assert master._active == set()
