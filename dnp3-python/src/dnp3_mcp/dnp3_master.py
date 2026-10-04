"""DNP3 master built on dnp3py, a pure-Python IEEE 1815-2012 stack.

Every operation opens a TCP connection to the configured host and port,
resets the data link, runs its exchange (one READ, or SELECT then OPERATE),
and closes the connection again. Operations are serialised, so only one
connection is open at a time. Nothing is cached: every value returned was
read from the outstation during the call that returns it.

Only TCP is implemented. Serial is rejected when the configuration is read.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

import anyio
from dnp3.application.builder import build_all_objects_request
from dnp3.application.fragment import RequestFragment
from dnp3.application.parser import ParseError, parse_response
from dnp3.core.enums import CommandStatus, FunctionCode
from dnp3.core.flags import IIN, AnalogQuality, BinaryQuality, CounterQuality, DoubleBitQuality
from dnp3.master import Master, MasterConfig, MasterRunnerError, MasterTcpRunner, ResponseTimeoutError
from dnp3.master.commands import CommandBuilder
from dnp3.master.handler import ResponseInfo
from dnp3.transport_io import ChannelError, TcpClientChannel, TcpConfig

from .config import MAX_LINK_ADDRESS, DNP3Config

logger = logging.getLogger(__name__)

MAX_POINT_INDEX = 0xFFFF
MAX_READ_COUNT = 1000
CLASS_DATA_GROUP = 60

# IIN2 bits that mean the outstation did not carry out the request.
REQUEST_ERROR_IIN = IIN.NO_FUNC_CODE_SUPPORT | IIN.OBJECT_UNKNOWN | IIN.PARAMETER_ERROR | IIN.ALREADY_EXECUTING

_QUALITY_FLAGS: dict[str, type] = {
    "binary_input": BinaryQuality,
    "binary_output": BinaryQuality,
    "double_bit_input": DoubleBitQuality,
    "analog_input": AnalogQuality,
    "analog_output": AnalogQuality,
    "counter": CounterQuality,
    "frozen_counter": CounterQuality,
}
# Bits that carry the value (or nothing), not a quality.
_NOT_QUALITY = {"STATE", "STATE_BIT_0", "STATE_BIT_1", "RESERVED"}

# g12v1 control relay output block: control code, count, on-time (4),
# off-time (4), status. The status octet is the last of the 11.
_CROB_GROUP_VARIATION = (12, 1)
_CROB_SIZE = 11
_CROB_QUALIFIERS = {0x17: (1, 1), 0x28: (2, 2)}  # qualifier -> (count octets, index octets)

T = TypeVar("T")


class DNP3MasterError(RuntimeError):
    """A DNP3 operation did not complete. `meta` carries diagnostics."""

    def __init__(self, message: str, meta: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.meta: dict[str, Any] = meta or {}


class DNP3WritesDisabledError(DNP3MasterError):
    """A control was requested while DNP3_WRITES_ENABLED is false."""


class DNP3ConnectionError(DNP3MasterError):
    """The TCP connection or the link to the outstation failed."""


class DNP3TimeoutError(DNP3MasterError):
    """The outstation did not answer in time."""


class DNP3ProtocolError(DNP3MasterError):
    """The outstation answered, but refused the request or left data out."""


class DNP3ControlError(DNP3MasterError):
    """A control (CROB) was not confirmed as executed."""


def iin_names(iin: IIN | int) -> list[str]:
    """Names of the IIN bits that are set."""
    value = int(iin)
    return [bit.name for bit in IIN if bit.name and value & bit]


def quality_names(kind: str, flags: int) -> list[str]:
    """Names of the quality flags set in a point's flag octet."""
    enum = _QUALITY_FLAGS.get(kind)
    if enum is None:
        return []
    return [bit.name for bit in enum if bit.name not in _NOT_QUALITY and flags & bit]


def command_status_name(code: int) -> str:
    try:
        return CommandStatus(code).name
    except ValueError:
        return f"STATUS_{code}"


@dataclass(frozen=True, slots=True)
class PointValue:
    """One value as the outstation reported it."""

    kind: str
    index: int
    value: Any
    flags: int
    timestamp: datetime | None
    unsolicited: bool

    def as_dict(self, *, with_kind: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if with_kind:
            out["type"] = self.kind
        out.update(
            {
                "index": self.index,
                "value": self.value,
                "flags": self.flags,
                "quality": quality_names(self.kind, self.flags),
                "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            }
        )
        if with_kind:
            out["unsolicited"] = self.unsolicited
        return out


class _Collector:
    """SOE handler that records every value delivered during a session."""

    def __init__(self) -> None:
        self.values: list[PointValue] = []

    def _add(self, kind: str, values: list[Any], info: ResponseInfo, convert: Callable[[Any], Any]) -> None:
        for v in values:
            self.values.append(
                PointValue(
                    kind=kind,
                    index=int(v.index),
                    value=convert(v),
                    flags=int(v.quality),
                    timestamp=v.timestamp,
                    unsolicited=bool(info.is_unsolicited),
                )
            )

    def on_binary_input(self, values: list[Any], info: ResponseInfo) -> None:
        self._add("binary_input", values, info, lambda v: bool(v.value))

    def on_binary_output(self, values: list[Any], info: ResponseInfo) -> None:
        self._add("binary_output", values, info, lambda v: bool(v.value))

    def on_double_bit_input(self, values: list[Any], info: ResponseInfo) -> None:
        self._add("double_bit_input", values, info, lambda v: v.state.name)

    def on_analog_input(self, values: list[Any], info: ResponseInfo) -> None:
        self._add("analog_input", values, info, lambda v: float(v.value))

    def on_analog_output(self, values: list[Any], info: ResponseInfo) -> None:
        self._add("analog_output", values, info, lambda v: float(v.value))

    def on_counter(self, values: list[Any], info: ResponseInfo) -> None:
        self._add("counter", values, info, lambda v: int(v.value))

    def on_frozen_counter(self, values: list[Any], info: ResponseInfo) -> None:
        self._add("frozen_counter", values, info, lambda v: int(v.value))


@dataclass
class _CapturingMaster(Master):
    """dnp3py Master that keeps the raw response fragments.

    dnp3py does not hand control-status echoes to the SOE handler, so the
    g12v1 status octets are decoded from these fragments instead.
    """

    fragments: list[bytes] = field(default_factory=list, init=False)

    def process_response(self, data: bytes) -> ResponseInfo | None:
        self.fragments.append(bytes(data))
        return super().process_response(data)


@dataclass(frozen=True, slots=True)
class _Reply:
    infos: list[ResponseInfo]
    fragments: list[bytes]
    values: list[PointValue]

    @property
    def iin(self) -> IIN:
        combined = IIN(0)
        for info in self.infos:
            combined |= info.iin
        return combined

    @property
    def warnings(self) -> list[str]:
        """Fragments the library could only partly decode. Values after the stopping point were not delivered."""
        out = []
        for info in self.infos:
            cut = info.truncation
            if cut is None:
                continue
            where = f"group {cut.group} variation {cut.variation}" if cut.group is not None else "trailing octets"
            out.append(
                f"Response fragment {info.sequence} could not be fully decoded ({cut.reason.value} at {where}); "
                "objects from that point on in the fragment were not returned"
            )
        return out


class _Session:
    def __init__(self, master: DNP3Master, outstation: int) -> None:
        cfg = master.config
        self._describe = f"outstation {outstation} at {cfg.host}:{cfg.port}"
        self._timeout_s = cfg.timeout_s
        self.collector = _Collector()
        self.master = _CapturingMaster(
            handler=self.collector,
            config=MasterConfig(
                address=cfg.master_address,
                outstation_address=outstation,
                response_timeout=cfg.timeout_s,
            ),
        )
        self.channel = TcpClientChannel(config=TcpConfig(host=cfg.host, port=cfg.port, connect_timeout=cfg.timeout_s))
        self.runner = MasterTcpRunner(
            master=self.master,
            host=cfg.host,
            port=cfg.port,
            response_timeout=cfg.timeout_s,
            channel=self.channel,
        )

    async def open(self) -> None:
        try:
            await self.runner.open()
        except (ChannelError, MasterRunnerError, OSError) as exc:
            raise DNP3ConnectionError(f"Could not connect to {self._describe}: {exc or type(exc).__name__}") from exc

    async def close(self) -> None:
        try:
            await self.runner.close()
        finally:
            await self.channel.close()

    async def request(self, fragment: RequestFragment) -> _Reply:
        self.master.fragments.clear()
        first = len(self.collector.values)
        try:
            infos = await self.runner.request(fragment)
        except ResponseTimeoutError as exc:
            raise DNP3TimeoutError(f"No response from {self._describe} within {self._timeout_s:g} s") from exc
        except (ChannelError, MasterRunnerError, OSError) as exc:
            raise DNP3ConnectionError(f"Link to {self._describe} failed: {exc or type(exc).__name__}") from exc
        return _Reply(infos=list(infos), fragments=list(self.master.fragments), values=self.collector.values[first:])


def crob_statuses(fragments: list[bytes]) -> dict[int, int]:
    """Map point index -> status octet for every g12v1 object echoed in solicited fragments."""
    statuses: dict[int, int] = {}
    for raw in fragments:
        try:
            response = parse_response(raw)
        except (ParseError, ValueError, IndexError):
            continue
        if response.header.control.uns:
            continue
        for block in response.objects:
            header = block.header
            if (header.group, header.variation) != _CROB_GROUP_VARIATION:
                continue
            sizes = _CROB_QUALIFIERS.get(header.qualifier)
            if sizes is None:
                continue
            count_size, index_size = sizes
            data = block.data
            if len(data) < count_size:
                continue
            count = int.from_bytes(data[:count_size], "little")
            pos = count_size
            for _ in range(count):
                end = pos + index_size + _CROB_SIZE
                if end > len(data):
                    break
                index = int.from_bytes(data[pos : pos + index_size], "little")
                statuses[index] = data[end - 1] & 0x7F
                pos = end
    return statuses


def _check_outstation(outstation: int) -> None:
    if isinstance(outstation, bool) or not isinstance(outstation, int) or not 0 <= outstation <= MAX_LINK_ADDRESS:
        raise DNP3MasterError(f"outstation address must be an integer 0-{MAX_LINK_ADDRESS}, got {outstation!r}")


def _check_index(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_POINT_INDEX:
        raise DNP3MasterError(f"{name} must be an integer 0-{MAX_POINT_INDEX}, got {value!r}")


def _format_indexes(indexes: list[int]) -> str:
    shown = ", ".join(str(i) for i in indexes[:20])
    return shown + (f" and {len(indexes) - 20} more" if len(indexes) > 20 else "")


class DNP3Master:
    """Async DNP3 master over TCP."""

    def __init__(self, config: DNP3Config | None = None) -> None:
        self.config = config or DNP3Config()
        self._lock = anyio.Lock()

    # -- public operations --------------------------------------------------

    async def read_binary_inputs(
        self, outstation: int, start: int, count: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """READ binary inputs (g1, any variation) start..start+count-1."""
        return await self._read_static("binary_input", 1, outstation, start, count)

    async def read_analog_inputs(
        self, outstation: int, start: int, count: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """READ analog inputs (g30, any variation) start..start+count-1."""
        return await self._read_static("analog_input", 30, outstation, start, count)

    async def poll_class(self, outstation: int, event_class: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """READ class 0 (static data) or one event class (1-3)."""
        _check_outstation(outstation)
        if isinstance(event_class, bool) or event_class not in (0, 1, 2, 3):
            raise DNP3MasterError(f"event_class must be 0, 1, 2 or 3, got {event_class!r}")

        def build(session: _Session) -> RequestFragment:
            if event_class == 0:
                return build_all_objects_request(
                    FunctionCode.READ, CLASS_DATA_GROUP, 1, seq=session.master.next_request_sequence()
                )
            return session.master.build_class_poll(
                class_1=event_class == 1, class_2=event_class == 2, class_3=event_class == 3
            )

        started = time.perf_counter()
        reply, attempts = await self._read_with_retries(outstation, build)
        meta = self._meta(outstation, started, attempts, reply)
        self._raise_for_iin(reply.iin, "READ", meta)
        points = [v.as_dict(with_kind=True) for v in reply.values]
        return {"outstation": outstation, "class": event_class, "count": len(points), "points": points}, meta

    async def write_binary_output(self, outstation: int, index: int, value: bool) -> dict[str, Any]:
        """Latch a binary output on or off with a CROB and confirm the outstation accepted it."""
        if not self.config.writes_enabled:
            raise DNP3WritesDisabledError("Write operations are disabled (DNP3_WRITES_ENABLED is false)")
        _check_outstation(outstation)
        _check_index("index", index)
        if not isinstance(value, bool):
            raise DNP3MasterError(f"value must be a boolean, got {value!r}")

        code = "LATCH_ON" if value else "LATCH_OFF"
        builder = CommandBuilder()
        if value:
            builder.latch_on(index)
        else:
            builder.latch_off(index)
        mode = self.config.control_mode
        started = time.perf_counter()
        base_meta = {"outstation": outstation, "index": index, "control_code": code, "mode": mode}

        async with self._lock:
            session = _Session(self, outstation)
            try:
                await session.open()
            except DNP3ConnectionError as exc:
                raise DNP3ControlError(f"{exc}. No control was sent.", base_meta) from exc
            try:
                iin = IIN(0)
                if mode == "select_before_operate":
                    iin |= await self._control_step(
                        session, session.master.build_select(builder.build_select()), "SELECT", index, base_meta
                    )
                    iin |= await self._control_step(
                        session, session.master.build_operate(builder.build_operate()), "OPERATE", index, base_meta
                    )
                else:
                    iin |= await self._control_step(
                        session,
                        session.master.build_direct_operate(builder.build_direct_operate()),
                        "DIRECT_OPERATE",
                        index,
                        base_meta,
                    )
            finally:
                await session.close()

        meta = {**base_meta, **self._meta(outstation, started, 1), "iin": iin_names(iin), "status": "SUCCESS"}
        return meta

    def connection_status(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "connection_type": cfg.connection_type,
            "host": cfg.host,
            "port": cfg.port,
            "master_address": cfg.master_address,
            "timeout_ms": cfg.timeout_ms,
            "max_retries": cfg.max_retries,
            "control_mode": cfg.control_mode,
        }

    async def close(self) -> None:
        """Nothing to release: connections only live for one operation."""

    # -- internals ----------------------------------------------------------

    async def _read_static(
        self, kind: str, group: int, outstation: int, start: int, count: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        _check_outstation(outstation)
        _check_index("start_index", start)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_READ_COUNT:
            raise DNP3MasterError(f"count must be an integer 1-{MAX_READ_COUNT}, got {count!r}")
        stop = start + count - 1
        if stop > MAX_POINT_INDEX:
            raise DNP3MasterError(f"start_index + count - 1 must not exceed {MAX_POINT_INDEX}, got {stop}")

        started = time.perf_counter()
        reply, attempts = await self._read_with_retries(
            outstation, lambda session: session.master.build_range_poll(group, 0, start, stop)
        )
        meta = self._meta(outstation, started, attempts, reply)
        self._raise_for_iin(reply.iin, "READ", meta)

        by_index: dict[int, PointValue] = {}
        for value in reply.values:
            if value.unsolicited or value.kind != kind or not start <= value.index <= stop:
                continue
            by_index[value.index] = value
        missing = [i for i in range(start, stop + 1) if i not in by_index]
        if missing:
            meta["returned_indexes"] = sorted(by_index)
            label = kind.replace("_", " ")
            raise DNP3ProtocolError(
                f"Outstation {outstation} returned no {label} value for index {_format_indexes(missing)}",
                meta,
            )
        return [by_index[i].as_dict() for i in range(start, stop + 1)], meta

    async def _read_with_retries(
        self, outstation: int, build: Callable[[_Session], RequestFragment]
    ) -> tuple[_Reply, int]:
        async def attempt() -> _Reply:
            session = _Session(self, outstation)
            await session.open()
            try:
                return await session.request(build(session))
            finally:
                await session.close()

        async with self._lock:
            return await self._with_retries(attempt)

    async def _with_retries(self, operation: Callable[[], Awaitable[T]]) -> tuple[T, int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                return await operation(), attempts
            except (DNP3ConnectionError, DNP3TimeoutError) as exc:
                if attempts > self.config.max_retries:
                    exc.meta.setdefault("attempts", attempts)
                    raise
                logger.warning("%s; retrying (%d of %d)", exc, attempts, self.config.max_retries)

    async def _control_step(
        self, session: _Session, fragment: RequestFragment, function: str, index: int, meta: dict[str, Any]
    ) -> IIN:
        acting = function != "SELECT"
        unknown = (
            "The outstation may or may not have operated the output; read its state before trying again."
            if acting
            else "Nothing was operated."
        )
        try:
            reply = await session.request(fragment)
        except (DNP3TimeoutError, DNP3ConnectionError) as exc:
            raise DNP3ControlError(f"{function} for binary output {index}: {exc}. {unknown}", dict(meta)) from exc
        except Exception as exc:  # e.g. a response the library could not parse
            raise DNP3ControlError(
                f"{function} for binary output {index} failed: {type(exc).__name__}: {exc}. {unknown}", dict(meta)
            ) from exc

        step_meta = {**meta, "iin": iin_names(reply.iin), "failed_step": function}
        if reply.iin & REQUEST_ERROR_IIN:
            rejected = iin_names(reply.iin & REQUEST_ERROR_IIN)
            raise DNP3ControlError(
                f"Outstation rejected {function} for binary output {index} (IIN {', '.join(rejected)}). "
                "Nothing was operated.",
                step_meta,
            )
        status = crob_statuses(reply.fragments).get(index)
        if status is None:
            raise DNP3ControlError(
                f"{function} response for binary output {index} carried no control status. {unknown}", step_meta
            )
        if status != CommandStatus.SUCCESS:
            name = command_status_name(status)
            step_meta["status"] = name
            raise DNP3ControlError(
                f"Outstation rejected {function} for binary output {index} with status {name}. Nothing was operated.",
                step_meta,
            )
        return reply.iin

    def _raise_for_iin(self, iin: IIN, function: str, meta: dict[str, Any]) -> None:
        if iin & REQUEST_ERROR_IIN:
            rejected = iin_names(iin & REQUEST_ERROR_IIN)
            raise DNP3ProtocolError(f"Outstation rejected the {function} request (IIN {', '.join(rejected)})", meta)

    def _meta(self, outstation: int, started: float, attempts: int, reply: _Reply | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "outstation": outstation,
            "host": self.config.host,
            "port": self.config.port,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "attempts": attempts,
        }
        if reply is not None:
            meta["iin"] = iin_names(reply.iin)
            warnings = reply.warnings
            if warnings:
                # Say so rather than presenting a partial result as complete.
                meta["incomplete"] = True
                meta["warnings"] = warnings
        return meta
