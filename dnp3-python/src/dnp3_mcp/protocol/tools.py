from __future__ import annotations

import inspect
from typing import Any

from dnp3_mcp.application.resources import ToolResources
from dnp3_mcp.domain.model import Alias, Count, EventClass, OutputValue, OutstationAddress, PointIndex, StartIndex

from ..application.operations import Operations
from .support import CONSUMES_EVENTS, LOCAL_ONLY, READ_ONLY, WRITES, FastMCP, ToolAnnotations, request_cancel_scope


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    operations = Operations(resources, lambda: request_cancel_scope(server))

    def tool(annotations: ToolAnnotations) -> Any:

        def register(fn: Any) -> Any:
            return server.tool(description=inspect.cleandoc(fn.__doc__ or ""), annotations=annotations)(fn)

        return register

    @tool(READ_ONLY)
    async def read_binary_inputs(outstation_address: OutstationAddress, start_index: StartIndex, count: Count) -> dict:
        """Read binary inputs (DNP3 group 1) from an outstation.

        Reads indexes start_index to start_index + count - 1 over a fresh TCP
        connection to DNP3_HOST:DNP3_PORT. Each point has its value, its flag
        octet, the decoded quality flags and a timestamp when the device sent
        one. Fails if the outstation does not return every requested index.
        Any unsolicited data the outstation pushes meanwhile is confirmed (so it
        is removed from the outstation) and returned in meta.unsolicited.
        """
        return await operations.read_binary_inputs(outstation_address, start_index, count)

    @tool(READ_ONLY)
    async def read_analog_inputs(outstation_address: OutstationAddress, start_index: StartIndex, count: Count) -> dict:
        """Read analog inputs (DNP3 group 30) from an outstation.

        Reads indexes start_index to start_index + count - 1 over a fresh TCP
        connection to DNP3_HOST:DNP3_PORT. Values are returned as the device
        encodes them (integer or floating point variations, unscaled). Fails if
        the outstation does not return every requested index. Unsolicited data
        received meanwhile is returned in meta.unsolicited.
        """
        return await operations.read_analog_inputs(outstation_address, start_index, count)

    @tool(WRITES)
    async def write_binary_output(outstation_address: OutstationAddress, index: PointIndex, value: OutputValue) -> dict:
        """Operate a binary output with a DNP3 control relay output block (CROB, group 12).

        value=true sends LATCH_ON, value=false sends LATCH_OFF, using
        select-before-operate unless DNP3_CONTROL_MODE=direct_operate. Refused
        unless DNP3_WRITES_ENABLED=true. Succeeds only when the outstation
        answers with control status SUCCESS and echoes the control unchanged;
        any other status, a mismatched echo, a timeout or a dropped link is
        reported as a failure, and the error says whether the output may have
        changed. meta.outcome is "operated", "rejected", "not_sent" (no OPERATE
        or DIRECT_OPERATE was sent) or "unknown" (it may have acted; read the
        output before retrying), and meta.request_sent says whether any control
        request may have reached the outstation. Unsolicited data received
        meanwhile is in meta.unsolicited.
        """
        return await operations.write_binary_output(outstation_address, index, value)

    @tool(CONSUMES_EVENTS)
    async def poll_class(outstation_address: OutstationAddress, event_class: EventClass) -> dict:
        """Poll an outstation for Class 0 static data or Class 1/2/3 events.

        event_class=0 returns every static point the device reports (inputs,
        output status, counters). 1, 2 or 3 returns the buffered events of that
        class and DELETES them on the outstation (they are confirmed), so other
        masters sharing the association will not see them; repeating the call
        does not return them again. Each entry has its point type, index, value,
        quality flags and timestamp if sent. If an event response cannot be
        fully decoded that fragment is not confirmed and the call fails, with
        the events of fragments already confirmed in meta.confirmed_events; for
        Class 0, meta.incomplete is true and meta.warnings says where decoding
        stopped.
        Unsolicited data received meanwhile is returned in meta.unsolicited.
        """
        return await operations.poll_class(outstation_address, event_class)

    @tool(LOCAL_ONLY)
    async def list_points() -> dict:
        """List the aliases defined in the point map (POINT_MAP_FILE).

        Invalid entries are listed with an 'error' field. Does not contact any
        outstation.
        """
        return await operations.list_points()

    @tool(READ_ONLY)
    async def read_point_by_alias(alias: Alias) -> dict:
        """Read one binary_input or analog_input point by its point-map alias.

        Looks the alias up in POINT_MAP_FILE and reads that single point from
        its outstation, returning the same envelope as read_binary_inputs or
        read_analog_inputs.
        """
        return await operations.read_point_by_alias(alias)

    @tool(WRITES)
    async def write_point_by_alias(alias: Alias, value: OutputValue) -> dict:
        """Operate a binary_output point by its point-map alias.

        Same as write_binary_output for the outstation and index the alias
        names, including the DNP3_WRITES_ENABLED gate. Only binary_output
        aliases can be written.
        """
        return await operations.write_point_by_alias(alias, value)

    @tool(LOCAL_ONLY)
    async def ping() -> dict:
        """Report the server's configuration: connection settings, write switch and point map.

        Does not contact any outstation; use a read tool to check that a
        device answers.
        """
        return await operations.ping()

    from .contract import install_contract

    install_contract(server)
