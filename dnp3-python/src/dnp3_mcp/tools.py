"""DNP3 MCP tool definitions.

Every tool returns the suite's envelope: {success, data, error, meta}.
Failures, including bad arguments, come back as success: false with an
error message; tools never raise to the client.
"""

import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator, Field, StrictBool, StrictInt

from .config import MAX_LINK_ADDRESS
from .dnp3_master import MAX_POINT_INDEX, MAX_READ_COUNT, DNP3Master, DNP3MasterError

logger = logging.getLogger(__name__)

POINT_TYPES = ("binary_input", "analog_input", "binary_output")


def envelope(
    success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


def ok(data: Any = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return envelope(True, data=data, meta=meta)


def err(message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return envelope(False, error=message, meta=meta)


# Strict integers: JSON booleans, numeric strings ("10") and floats (10.0) are
# rejected rather than coerced into an address or index.
OutstationAddress = Annotated[
    StrictInt,
    Field(ge=0, le=MAX_LINK_ADDRESS, description=f"DNP3 link address of the outstation (0-{MAX_LINK_ADDRESS})."),
]
StartIndex = Annotated[StrictInt, Field(ge=0, le=MAX_POINT_INDEX, description="First point index to read.")]
PointIndex = Annotated[StrictInt, Field(ge=0, le=MAX_POINT_INDEX, description="Point index of the binary output.")]
Count = Annotated[
    StrictInt,
    Field(ge=1, le=MAX_READ_COUNT, description=f"Number of consecutive points to read (1-{MAX_READ_COUNT})."),
]
EventClass = Annotated[
    StrictInt,
    Field(ge=0, le=3, description="0 reads all static data (Class 0); 1, 2 or 3 reads that event class."),
]


def _output_value(value: Any) -> Any:
    # Only a JSON boolean or the words "true"/"false" (any case). Numbers and
    # other words ("off", "yes", 1) are rejected rather than guessed: this
    # value switches physical equipment.
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError('expected a JSON boolean, or the string "true" or "false"')


OutputValue = Annotated[
    StrictBool,
    Field(
        description=(
            "true latches the output ON, false latches it OFF. Pass a JSON boolean; "
            'the strings "true" and "false" are also accepted. Anything else is rejected.'
        )
    ),
    BeforeValidator(_output_value),
]
Alias = Annotated[str, Field(min_length=1, max_length=256, description="Alias defined in the point map.")]


@dataclass(frozen=True, slots=True)
class PointSpec:
    alias: str
    type: str | None
    outstation: int | None
    index: int | None
    description: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "alias": self.alias,
            "outstation": self.outstation,
            "type": self.type,
            "index": self.index,
            "description": self.description,
        }
        if self.error:
            out["error"] = self.error
        return out


def _int_field(spec: dict[str, Any], key: str, maximum: int) -> tuple[int | None, str | None]:
    raw = spec.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, f"'{key}' must be an integer, got {raw!r}"
    if not 0 <= raw <= maximum:
        return None, f"'{key}' must be 0-{maximum}, got {raw}"
    return raw, None


def parse_point_spec(alias: str, spec: Any) -> PointSpec:
    """Validate one point-map entry. Problems are recorded, not raised."""
    if not isinstance(spec, dict):
        return PointSpec(alias, None, None, None, None, f"entry must be a JSON object, got {type(spec).__name__}")
    problems: list[str] = []
    p_type = spec.get("type")
    if p_type not in POINT_TYPES:
        problems.append(f"'type' must be one of {', '.join(POINT_TYPES)}, got {p_type!r}")
    outstation, problem = _int_field(spec, "outstation", MAX_LINK_ADDRESS)
    if problem:
        problems.append(problem)
    index, problem = _int_field(spec, "index", MAX_POINT_INDEX)
    if problem:
        problems.append(problem)
    description = spec.get("description")
    if description is not None and not isinstance(description, str):
        problems.append("'description' must be a string")
        description = None
    return PointSpec(
        alias=alias,
        type=p_type if isinstance(p_type, str) else None,
        outstation=outstation,
        index=index,
        description=description,
        error="; ".join(problems) or None,
    )


class PointMap:
    """Alias -> point definitions from a JSON file, re-read when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._points: dict[str, PointSpec] = {}
        self._stamp: tuple[int, int] | None = None
        self.error: str | None = None
        self.refresh()
        if self.error:
            logger.warning("%s", self.error)

    def refresh(self) -> None:
        if self.path is None:
            self._points, self._stamp, self.error = {}, None, None
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._points, self._stamp = {}, None
            self.error = f"Point map {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._points = {}
            self.error = f"Point map {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._points = {}
            self.error = f"Point map {self.path} must be a JSON object mapping aliases to points"
            return
        self._points = {str(alias): parse_point_spec(str(alias), spec) for alias, spec in data.items()}
        self.error = None

    def get(self, alias: str) -> PointSpec | None:
        self.refresh()
        return self._points.get(alias)

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        return [spec.as_dict() for spec in self._points.values()]

    def count(self) -> int:
        self.refresh()
        return len(self._points)


@dataclass(slots=True)
class ToolResources:
    master: DNP3Master
    point_map: PointMap
    server_version: str = "unknown"
    library_version: str = "unknown"


# Rule: nothing that changes a device may advertise itself as idempotent or harmless.
# Device reads change no point, but any unsolicited data that arrives while they run is
# confirmed (and so deleted on the outstation, returned in meta.unsolicited): not idempotent.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
LOCAL_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
# A Class 1-3 poll confirms events, and the outstation then deletes them.
CONSUMES_EVENTS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

NOT_SENT = {"outcome": "not_sent", "request_sent": False}


def request_cancel_scope(server: FastMCP) -> anyio.CancelScope | None:
    """The cancel scope of the MCP request now being handled, or None outside one.

    RequestResponder.cancel() cancels this scope synchronously when the client
    sends notifications/cancelled, before anyio interrupts the task; the master
    checks it right before every send. This reads private mcp<2 attributes
    (`session._in_flight`, `RequestResponder._cancel_scope`), hence the getattr
    guards and tests/test_cancel_race.py, which fails if they move.
    """
    try:
        context = server._mcp_server.request_context
    except (LookupError, AttributeError):
        return None
    in_flight = getattr(context.session, "_in_flight", None)
    responder = in_flight.get(context.request_id) if isinstance(in_flight, dict) else None
    scope = getattr(responder, "_cancel_scope", None)
    return scope if isinstance(scope, anyio.CancelScope) else None


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    master = resources.master
    point_map = resources.point_map

    def writes_refused(tool: str) -> dict[str, Any] | None:
        if not master.config.writes_enabled:
            return err(
                "Write operations are disabled. Set DNP3_WRITES_ENABLED=true to allow controls.",
                {"tool": tool, "writes_enabled": False, **NOT_SENT},
            )
        return None

    async def guarded(tool: str, call: Any, *, write: bool = False) -> dict[str, Any]:
        try:
            return await call()
        except DNP3MasterError as exc:
            meta = {"tool": tool, **exc.meta}
            if write and "outcome" not in meta:
                # Not classified where it was raised: assume the worst.
                meta.update(outcome="unknown", request_sent=True)
            return err(str(exc), meta)
        except Exception as exc:  # never let an exception reach the client
            logger.exception("Unexpected error in %s", tool)
            meta = {"tool": tool}
            message = f"Unexpected error: {type(exc).__name__}: {exc}"
            if write:
                # It cannot be known how far the control got.
                meta.update(outcome="unknown", request_sent=True)
                message += ". The control may have been applied; read the output's state back before retrying."
            return err(message, meta)

    def tool(annotations: ToolAnnotations) -> Any:
        def register(fn: Any) -> Any:
            # Docstrings become the tool descriptions in tools/list.
            return server.tool(description=inspect.cleandoc(fn.__doc__ or ""), annotations=annotations)(fn)

        return register

    def resolve(alias: str, tool: str, *, write: bool = False) -> PointSpec | dict[str, Any]:
        extra = dict(NOT_SENT) if write else {}
        spec = point_map.get(alias)
        if spec is None:
            if point_map.error:
                return err(f"Unknown alias '{alias}'. {point_map.error}", {"tool": tool, **extra})
            return err(f"Unknown alias '{alias}'", {"tool": tool, **extra})
        if spec.error:
            return err(f"Point map entry '{alias}' is invalid: {spec.error}", {"tool": tool, "alias": alias, **extra})
        return spec

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
        request_scope = request_cancel_scope(server)

        async def call() -> dict[str, Any]:
            points, meta = await master.read_binary_inputs(
                outstation_address, start_index, count, request_scope=request_scope
            )
            return ok({"outstation": outstation_address, "points": points}, meta)

        return await guarded("read_binary_inputs", call)

    @tool(READ_ONLY)
    async def read_analog_inputs(outstation_address: OutstationAddress, start_index: StartIndex, count: Count) -> dict:
        """Read analog inputs (DNP3 group 30) from an outstation.

        Reads indexes start_index to start_index + count - 1 over a fresh TCP
        connection to DNP3_HOST:DNP3_PORT. Values are returned as the device
        encodes them (integer or floating point variations, unscaled). Fails if
        the outstation does not return every requested index. Unsolicited data
        received meanwhile is returned in meta.unsolicited.
        """
        request_scope = request_cancel_scope(server)

        async def call() -> dict[str, Any]:
            points, meta = await master.read_analog_inputs(
                outstation_address, start_index, count, request_scope=request_scope
            )
            return ok({"outstation": outstation_address, "points": points}, meta)

        return await guarded("read_analog_inputs", call)

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
        request_scope = request_cancel_scope(server)
        refused = writes_refused("write_binary_output")
        if refused:
            return refused

        async def call() -> dict[str, Any]:
            meta = await master.write_binary_output(outstation_address, index, value, request_scope=request_scope)
            return ok({"outstation": outstation_address, "index": index, "value": value}, meta)

        return await guarded("write_binary_output", call, write=True)

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
        request_scope = request_cancel_scope(server)

        async def call() -> dict[str, Any]:
            data, meta = await master.poll_class(outstation_address, event_class, request_scope=request_scope)
            return ok(data, meta)

        return await guarded("poll_class", call)

    @tool(LOCAL_ONLY)
    async def list_points() -> dict:
        """List the aliases defined in the point map (POINT_MAP_FILE).

        Invalid entries are listed with an 'error' field. Does not contact any
        outstation.
        """
        points = point_map.list()
        meta: dict[str, Any] = {"path": str(point_map.path) if point_map.path else None}
        if point_map.error:
            return err(point_map.error, meta)
        return ok({"points": points, "count": len(points)}, meta)

    @tool(READ_ONLY)
    async def read_point_by_alias(alias: Alias) -> dict:
        """Read one binary_input or analog_input point by its point-map alias.

        Looks the alias up in POINT_MAP_FILE and reads that single point from
        its outstation, returning the same envelope as read_binary_inputs or
        read_analog_inputs.
        """
        spec = resolve(alias, "read_point_by_alias")
        if isinstance(spec, dict):
            return spec
        if spec.type == "binary_input":
            return await read_binary_inputs(spec.outstation, spec.index, 1)
        if spec.type == "analog_input":
            return await read_analog_inputs(spec.outstation, spec.index, 1)
        return err(
            f"Alias '{alias}' is a {spec.type}; reading it by alias is not supported. "
            "Use poll_class with event_class 0 to see output status.",
            {"tool": "read_point_by_alias", "alias": alias},
        )

    @tool(WRITES)
    async def write_point_by_alias(alias: Alias, value: OutputValue) -> dict:
        """Operate a binary_output point by its point-map alias.

        Same as write_binary_output for the outstation and index the alias
        names, including the DNP3_WRITES_ENABLED gate. Only binary_output
        aliases can be written.
        """
        refused = writes_refused("write_point_by_alias")
        if refused:
            return refused
        spec = resolve(alias, "write_point_by_alias", write=True)
        if isinstance(spec, dict):
            return spec
        if spec.type != "binary_output":
            return err(
                f"Alias '{alias}' is a {spec.type}, which cannot be written",
                {"tool": "write_point_by_alias", "alias": alias, **NOT_SENT},
            )
        return await write_binary_output(spec.outstation, spec.index, value)

    @tool(LOCAL_ONLY)
    async def ping() -> dict:
        """Report the server's configuration: connection settings, write switch and point map.

        Does not contact any outstation; use a read tool to check that a
        device answers.
        """
        return ok(
            {
                "connection": master.connection_status(),
                "writes_enabled": master.config.writes_enabled,
                "point_aliases": point_map.count(),
                "point_map": {
                    "path": str(point_map.path) if point_map.path else None,
                    "error": point_map.error,
                },
                "server_version": resources.server_version,
                "dnp3_library": f"dnp3py {resources.library_version}",
            }
        )
