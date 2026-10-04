"""DNP3 master built on dnp3py, a pure-Python IEEE 1815-2012 stack.

Every operation opens a TCP connection to the configured host and port,
resets the data link, runs its exchange (one READ, or SELECT then OPERATE),
and closes the connection again. Operations are serialised, so only one
connection is open at a time. Nothing is cached: every value returned was
read from the outstation during the call that returns it.

dnp3py is pinned to one release because this module relies on some of its
internals and works around two of its defects:

- Its CommandBuilder writes a one-octet object count with qualifier 0x28, so
  every CROB on an index above 255 is malformed. Control requests are encoded
  here instead (`crob_object_block`).
- Its runner confirms a response fragment even when it could only partly
  decode it, which tells the outstation to delete events this server never
  saw. `_GuardedRunner` withholds that CONFIRM for event data.

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
from dnp3.application.builder import (
    build_all_objects_request,
    build_direct_operate_request,
    build_operate_request,
    build_select_request,
)
from dnp3.application.fragment import ObjectBlock, RequestFragment
from dnp3.application.parser import ParseError, parse_response
from dnp3.application.qualifiers import ObjectHeader
from dnp3.core.enums import CommandStatus, FunctionCode
from dnp3.core.flags import IIN, AnalogQuality, BinaryQuality, CounterQuality, DoubleBitQuality
from dnp3.master import Master, MasterConfig, MasterRunnerError, MasterTcpRunner, ResponseTimeoutError
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

# g12v1 control relay output block (IEEE 1815-2012 A.8.1): control code (1),
# count (1), on-time (4), off-time (4), status (1).
CROB_GROUP = 12
CROB_VARIATION = 1
CROB_SIZE = 11
QUALIFIER_8BIT_INDEX = 0x17  # 1-octet count, 1-octet index prefix
QUALIFIER_16BIT_INDEX = 0x28  # 2-octet count, 2-octet index prefix
_CROB_QUALIFIERS = {QUALIFIER_8BIT_INDEX: (1, 1), QUALIFIER_16BIT_INDEX: (2, 2)}
LATCH_ON = 0x03
LATCH_OFF = 0x04

_CONTROL_BUILDERS = {
    "SELECT": build_select_request,
    "OPERATE": build_operate_request,
    "DIRECT_OPERATE": build_direct_operate_request,
}

# meta.outcome of a control, so a client can act on it without parsing text.
OUTCOME_OPERATED = "operated"  # the outstation confirmed the OPERATE / DIRECT_OPERATE with SUCCESS
OUTCOME_REJECTED = "rejected"  # the outstation refused it (status or IIN); nothing operated
OUTCOME_NOT_SENT = "not_sent"  # no OPERATE / DIRECT_OPERATE was sent; nothing operated
OUTCOME_UNKNOWN = "unknown"  # an OPERATE / DIRECT_OPERATE may have been sent but was not confirmed

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


def describe_truncation(info: ResponseInfo) -> str:
    cut = info.truncation
    if cut is None:
        return ""
    where = f"group {cut.group} variation {cut.variation}" if cut.group is not None else "trailing octets"
    kind = "Unsolicited response" if info.is_unsolicited else "Response"
    return (
        f"{kind} fragment {info.sequence} could not be fully decoded ({cut.reason.value} at {where}); "
        "objects from that point on in the fragment were not returned"
    )


# -- CROB encoding ------------------------------------------------------------


def crob_object(code: int, *, count: int = 1, on_time: int = 0, off_time: int = 0, status: int = 0) -> bytes:
    """The 11 octets of one g12v1 object."""
    return (
        bytes([code, count]) + on_time.to_bytes(4, "little") + off_time.to_bytes(4, "little") + bytes([status & 0x7F])
    )


def crob_object_block(index: int, code: int) -> ObjectBlock:
    """One g12v1 object for `index`, with the qualifier and count width IEEE 1815-2012 requires.

    Qualifier 0x17 (one-octet count and index) up to index 255, 0x28 (two-octet
    count and index) above it.
    """
    if not 0 <= index <= MAX_POINT_INDEX:
        raise ValueError(f"CROB index must be 0-{MAX_POINT_INDEX}, got {index}")
    if index <= 0xFF:
        qualifier, prefix = QUALIFIER_8BIT_INDEX, bytes([1, index])
    else:
        qualifier, prefix = QUALIFIER_16BIT_INDEX, (1).to_bytes(2, "little") + index.to_bytes(2, "little")
    header = ObjectHeader(group=CROB_GROUP, variation=CROB_VARIATION, qualifier=qualifier)
    return ObjectBlock(header=header, data=prefix + crob_object(code))


def build_crob_request(function: str, index: int, value: bool, seq: int) -> RequestFragment:
    """SELECT, OPERATE or DIRECT_OPERATE latching binary output `index` on (True) or off (False)."""
    block = crob_object_block(index, LATCH_ON if value else LATCH_OFF)
    return _CONTROL_BUILDERS[function](objects=(block,), seq=seq)


def crob_echoes(fragments: list[bytes]) -> dict[int, bytes]:
    """Map point index -> the 11-octet g12v1 object echoed for it in solicited fragments."""
    echoes: dict[int, bytes] = {}
    for raw in fragments:
        try:
            response = parse_response(raw)
        except (ParseError, ValueError, IndexError):
            continue
        if response.header.control.uns:
            continue
        for block in response.objects:
            header = block.header
            if (header.group, header.variation) != (CROB_GROUP, CROB_VARIATION):
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
                end = pos + index_size + CROB_SIZE
                if end > len(data):
                    break
                index = int.from_bytes(data[pos : pos + index_size], "little")
                echoes[index] = bytes(data[pos + index_size : end])
                pos = end
    return echoes


def crob_statuses(fragments: list[bytes]) -> dict[int, int]:
    """Map point index -> status code for every g12v1 object echoed in solicited fragments."""
    return {index: echo[-1] & 0x7F for index, echo in crob_echoes(fragments).items()}


# -- dnp3py plumbing ----------------------------------------------------------


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
        return out


@dataclass
class _CallLog:
    """What one tool call received outside the replies it asked for."""

    unsolicited: list[PointValue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def annotate(self, meta: dict[str, Any]) -> dict[str, Any]:
        if self.unsolicited:
            # These were confirmed, so the outstation has deleted them: they must not be dropped.
            meta["unsolicited"] = [v.as_dict(with_kind=True) for v in self.unsolicited]
        if self.warnings:
            meta.setdefault("warnings", []).extend(w for w in self.warnings if w not in meta["warnings"])
        return meta


class _Collector:
    """SOE handler that records every value delivered during a session."""

    def __init__(self, log: _CallLog) -> None:
        self.values: list[PointValue] = []
        self._log = log

    def _add(self, kind: str, values: list[Any], info: ResponseInfo, convert: Callable[[Any], Any]) -> None:
        for v in values:
            point = PointValue(
                kind=kind,
                index=int(v.index),
                value=convert(v),
                flags=int(v.quality),
                timestamp=v.timestamp,
                unsolicited=bool(info.is_unsolicited),
            )
            self.values.append(point)
            if point.unsolicited:
                self._log.unsolicited.append(point)

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
    """dnp3py Master that keeps the raw fragments and the last parsed fragment's info.

    dnp3py does not hand control-status echoes to the SOE handler, so the
    g12v1 objects are decoded from these fragments instead.
    """

    fragments: list[bytes] = field(default_factory=list, init=False)
    last_info: ResponseInfo | None = field(default=None, init=False)
    log: _CallLog = field(default_factory=_CallLog, init=False)

    def process_response(self, data: bytes) -> ResponseInfo | None:
        self.fragments.append(bytes(data))
        info = super().process_response(data)
        self.last_info = info
        if info is not None and info.is_unsolicited and info.truncation is not None:
            self.log.warnings.append(describe_truncation(info) + "; it was not confirmed")
        return info


class _UnconfirmedFragment(DNP3ProtocolError):
    """A partly decoded event fragment was left unconfirmed, so the exchange cannot go on."""


@dataclass
class _GuardedRunner(MasterTcpRunner):
    """MasterTcpRunner that does not CONFIRM event data it could only partly decode.

    A CONFIRM tells the outstation it may delete the events in that fragment.
    dnp3py 0.6.0 sends it even when it stopped decoding part way, so events
    after the stopping point would be lost. Unsolicited
    fragments always carry events; solicited ones do for Class 1-3 polls
    (`protect_solicited`).
    """

    protect_solicited: bool = False
    withheld: list[tuple[int, bool]] = field(default_factory=list, init=False, repr=False)

    async def _send_confirm(self, sequence: int, *, uns: bool, deadline: float) -> None:
        info = getattr(self.master, "last_info", None)
        partial = (
            info is not None
            and info.truncation is not None
            and info.sequence == sequence
            and bool(info.is_unsolicited) == uns
        )
        if partial and (uns or self.protect_solicited):
            self.withheld.append((sequence, uns))
            logger.warning("Not confirming %s", describe_truncation(info))
            if not uns and not info.fin:
                # The outstation will not send the next fragment until this
                # one is confirmed, so the exchange ends here.
                raise _UnconfirmedFragment(describe_truncation(info))
            return
        await super()._send_confirm(sequence, uns=uns, deadline=deadline)


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
        """Solicited fragments the library could only partly decode."""
        return [describe_truncation(info) for info in self.infos if info.truncation is not None]


class _Session:
    def __init__(self, master: DNP3Master, outstation: int, log: _CallLog, *, protect_solicited: bool = False) -> None:
        cfg = master.config
        self._describe = f"outstation {outstation} at {cfg.host}:{cfg.port}"
        self._timeout_s = cfg.timeout_s
        self.collector = _Collector(log)
        self.master = _CapturingMaster(
            handler=self.collector,
            config=MasterConfig(
                address=cfg.master_address,
                outstation_address=outstation,
                response_timeout=cfg.timeout_s,
            ),
        )
        self.master.log = log
        self.channel = TcpClientChannel(config=TcpConfig(host=cfg.host, port=cfg.port, connect_timeout=cfg.timeout_s))
        self.runner = _GuardedRunner(
            master=self.master,
            host=cfg.host,
            port=cfg.port,
            response_timeout=cfg.timeout_s,
            channel=self.channel,
            protect_solicited=protect_solicited,
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

    def next_sequence(self) -> int:
        return self.master.next_request_sequence()


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
                return build_all_objects_request(FunctionCode.READ, CLASS_DATA_GROUP, 1, seq=session.next_sequence())
            return session.master.build_class_poll(
                class_1=event_class == 1, class_2=event_class == 2, class_3=event_class == 3
            )

        log = _CallLog()
        started = time.perf_counter()
        try:
            reply, attempts = await self._read_with_retries(outstation, build, log, protect_solicited=event_class > 0)
        except _UnconfirmedFragment as exc:
            raise DNP3ProtocolError(
                f"The class {event_class} poll response could not be fully decoded: {exc}. It was not confirmed, "
                "so an outstation that waits for confirmation keeps those events.",
                log.annotate({**self._meta(outstation, started, 1), "incomplete": True, "warnings": [str(exc)]}),
            ) from exc
        meta = log.annotate(self._meta(outstation, started, attempts, reply))
        self._raise_for_iin(reply.iin, "READ", meta)
        if event_class > 0 and reply.warnings:
            raise DNP3ProtocolError(
                f"The class {event_class} poll response could not be fully decoded, so it was not confirmed and "
                "nothing is returned as complete. An outstation that waits for confirmation keeps those events. "
                + " ".join(reply.warnings),
                meta,
            )
        points = [v.as_dict(with_kind=True) for v in reply.values if not v.unsolicited]
        return {"outstation": outstation, "class": event_class, "count": len(points), "points": points}, meta

    async def write_binary_output(self, outstation: int, index: int, value: bool) -> dict[str, Any]:
        """Latch a binary output on or off with a CROB and confirm the outstation accepted it."""
        if not self.config.writes_enabled:
            raise DNP3WritesDisabledError(
                "Write operations are disabled (DNP3_WRITES_ENABLED is false)",
                {"outcome": OUTCOME_NOT_SENT, "request_sent": False},
            )
        _check_outstation(outstation)
        _check_index("index", index)
        if not isinstance(value, bool):
            raise DNP3MasterError(f"value must be a boolean, got {value!r}")

        code = "LATCH_ON" if value else "LATCH_OFF"
        mode = self.config.control_mode
        started = time.perf_counter()
        base_meta = {"outstation": outstation, "index": index, "control_code": code, "mode": mode}
        steps = ("SELECT", "OPERATE") if mode == "select_before_operate" else ("DIRECT_OPERATE",)
        log = _CallLog()

        async with self._lock:
            session = _Session(self, outstation, log)
            try:
                try:
                    await session.open()
                except DNP3ConnectionError as exc:
                    raise DNP3ControlError(
                        f"{exc}. No control was sent.",
                        {**base_meta, "outcome": OUTCOME_NOT_SENT, "request_sent": False},
                    ) from exc
                iin = IIN(0)
                try:
                    for function in steps:
                        iin |= await self._control_step(session, function, index, value, base_meta)
                finally:
                    await session.close()
            except DNP3MasterError as exc:
                log.annotate(exc.meta)
                raise

        meta = {
            **base_meta,
            **self._meta(outstation, started, 1),
            "iin": iin_names(iin),
            "status": "SUCCESS",
            "outcome": OUTCOME_OPERATED,
            "request_sent": True,
        }
        return log.annotate(meta)

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

        log = _CallLog()
        started = time.perf_counter()
        reply, attempts = await self._read_with_retries(
            outstation, lambda session: session.master.build_range_poll(group, 0, start, stop), log
        )
        meta = log.annotate(self._meta(outstation, started, attempts, reply))
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
        self,
        outstation: int,
        build: Callable[[_Session], RequestFragment],
        log: _CallLog,
        *,
        protect_solicited: bool = False,
    ) -> tuple[_Reply, int]:
        async def attempt() -> _Reply:
            session = _Session(self, outstation, log, protect_solicited=protect_solicited)
            await session.open()
            try:
                return await session.request(build(session))
            finally:
                await session.close()

        async with self._lock:
            try:
                return await self._with_retries(attempt)
            except DNP3MasterError as exc:
                log.annotate(exc.meta)
                raise

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
        self, session: _Session, function: str, index: int, value: bool, meta: dict[str, Any]
    ) -> IIN:
        """Send one control request and require a matching SUCCESS echo.

        Every failure carries meta.outcome and meta.request_sent. A SELECT never
        operates anything, so a failed SELECT is "rejected" when the outstation
        refused it and "not_sent" otherwise (OPERATE is then not sent). A failed
        OPERATE / DIRECT_OPERATE is "rejected" when the outstation refused it and
        "unknown" when it may have acted.
        """
        acting = function != "SELECT"
        unknown = (
            "The control may have been applied; read the output's state back before retrying."
            if acting
            else "Nothing was operated."
        )
        unanswered = OUTCOME_UNKNOWN if acting else OUTCOME_NOT_SENT
        fragment = build_crob_request(function, index, value, session.next_sequence())
        sent = fragment.objects[0].data[-CROB_SIZE:]
        # True whenever the request may have reached the outstation.
        sent_meta = {**meta, "request_sent": True, "failed_step": function}
        try:
            reply = await session.request(fragment)
        except (DNP3TimeoutError, DNP3ConnectionError) as exc:
            raise DNP3ControlError(
                f"{function} for binary output {index}: {exc}. {unknown}", {**sent_meta, "outcome": unanswered}
            ) from exc
        except Exception as exc:  # e.g. a response the library could not parse
            raise DNP3ControlError(
                f"{function} for binary output {index} failed: {type(exc).__name__}: {exc}. {unknown}",
                {**sent_meta, "outcome": unanswered},
            ) from exc

        step_meta = {**sent_meta, "iin": iin_names(reply.iin)}
        if reply.iin & REQUEST_ERROR_IIN:
            rejected = iin_names(reply.iin & REQUEST_ERROR_IIN)
            raise DNP3ControlError(
                f"Outstation rejected {function} for binary output {index} (IIN {', '.join(rejected)}). "
                "Nothing was operated.",
                {**step_meta, "outcome": OUTCOME_REJECTED},
            )
        echo = crob_echoes(reply.fragments).get(index)
        if echo is None:
            raise DNP3ControlError(
                f"{function} response for binary output {index} carried no control status. {unknown}",
                {**step_meta, "outcome": unanswered},
            )
        if echo[:-1] != sent[:-1]:
            # IEEE 1815-2012 4.4.3: the response must echo the request's objects.
            follow = "OPERATE was not sent; nothing was operated." if function == "SELECT" else unknown
            raise DNP3ControlError(
                f"{function} response for binary output {index} does not echo the control that was sent. {follow}",
                {
                    **step_meta,
                    "status": "SELECT_MISMATCH" if function == "SELECT" else "ECHO_MISMATCH",
                    "sent": sent.hex(),
                    "echoed": echo.hex(),
                    "outcome": unanswered,
                },
            )
        status = echo[-1] & 0x7F
        if status != CommandStatus.SUCCESS:
            name = command_status_name(status)
            raise DNP3ControlError(
                f"Outstation rejected {function} for binary output {index} with status {name}. Nothing was operated.",
                {**step_meta, "status": name, "outcome": OUTCOME_REJECTED},
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
                meta["warnings"] = list(warnings)
        return meta
