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
from typing import Any, NoReturn, TypeVar

import anyio
import anyio.lowlevel
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

from ..config import MAX_LINK_ADDRESS, DNP3Config
from ..domain.errors import MAX_POINT_INDEX as MAX_POINT_INDEX
from ..domain.errors import MAX_READ_COUNT as MAX_READ_COUNT
from ..domain.errors import DNP3MasterError as DNP3MasterError

logger = logging.getLogger(__name__)
CLASS_DATA_GROUP = 60
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
_NOT_QUALITY = {"STATE", "STATE_BIT_0", "STATE_BIT_1", "RESERVED"}
CROB_GROUP = 12
CROB_VARIATION = 1
CROB_SIZE = 11
QUALIFIER_8BIT_INDEX = 23
QUALIFIER_16BIT_INDEX = 40
_CROB_QUALIFIERS = {QUALIFIER_8BIT_INDEX: (1, 1), QUALIFIER_16BIT_INDEX: (2, 2)}
LATCH_ON = 3
LATCH_OFF = 4
_CONTROL_BUILDERS = {
    "SELECT": build_select_request,
    "OPERATE": build_operate_request,
    "DIRECT_OPERATE": build_direct_operate_request,
}
OUTCOME_OPERATED = "operated"
OUTCOME_REJECTED = "rejected"
OUTCOME_NOT_SENT = "not_sent"
OUTCOME_UNKNOWN = "unknown"
T = TypeVar("T")


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
        f"{kind}"
        " fragment "
        f"{info.sequence}"
        " could not be fully decoded ("
        f"{cut.reason.value}"
        " at "
        f"{where}"
        "); objects from that point on in the fragment "
        "were not returned"
    )


def crob_object(code: int, *, count: int = 1, on_time: int = 0, off_time: int = 0, status: int = 0) -> bytes:
    """The 11 octets of one g12v1 object."""
    return bytes([code, count]) + on_time.to_bytes(4, "little") + off_time.to_bytes(4, "little") + bytes([status & 127])


def crob_object_block(index: int, code: int) -> ObjectBlock:
    """One g12v1 object for `index`, with the qualifier and count width IEEE 1815-2012 requires.

    Qualifier 0x17 (one-octet count and index) up to index 255, 0x28 (two-octet
    count and index) above it.
    """
    if not 0 <= index <= MAX_POINT_INDEX:
        raise ValueError(f"CROB index must be 0-{MAX_POINT_INDEX}, got {index}")
    if index <= 255:
        qualifier, prefix = (QUALIFIER_8BIT_INDEX, bytes([1, index]))
    else:
        qualifier, prefix = (QUALIFIER_16BIT_INDEX, (1).to_bytes(2, "little") + index.to_bytes(2, "little"))
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
    return {index: echo[-1] & 127 for index, echo in crob_echoes(fragments).items()}


@dataclass(frozen=True, slots=True)
class PointValue:
    """One value as the outstation reported it."""

    kind: str
    index: int
    value: Any
    flags: int
    timestamp: datetime | None
    unsolicited: bool
    fragment: int = 0

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
class _Fragment:
    """One response fragment received during a call."""

    number: int
    raw: bytes
    info: ResponseInfo | None = None
    confirmed: bool = False
    duplicate: bool = False

    @property
    def unsolicited(self) -> bool:
        return self.info is not None and bool(self.info.is_unsolicited)


@dataclass
class _CallLog:
    """Everything one tool call received, across its connections and attempts.

    A CONFIRM tells the outstation it may delete the events in that fragment,
    so any value from a confirmed fragment must reach the client, whatever
    happens to the rest of the call.
    """

    values: list[PointValue] = field(default_factory=list)
    fragments: list[_Fragment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def current(self) -> int:
        return self.fragments[-1].number if self.fragments else 0

    def begin_fragment(self, raw: bytes) -> _Fragment:
        fragment = _Fragment(number=len(self.fragments) + 1, raw=raw)
        self.fragments.append(fragment)
        return fragment

    def finish_fragment(self, fragment: _Fragment, info: ResponseInfo | None) -> None:
        fragment.info = info
        if info is None or not info.is_unsolicited:
            return
        for earlier in self.fragments[:-1]:
            if earlier.unsolicited and earlier.info.sequence == info.sequence and (earlier.raw == fragment.raw):
                fragment.duplicate = True
                break
        if info.truncation is not None and (not fragment.duplicate):
            self.warnings.append(describe_truncation(info) + "; it was not confirmed")

    def mark_confirmed(self, sequence: int, uns: bool) -> None:
        for fragment in reversed(self.fragments):
            info = fragment.info
            if info is not None and info.sequence == sequence and (bool(info.is_unsolicited) == uns):
                fragment.confirmed = True
                return

    def confirmed_solicited(self) -> bool:
        return any(f.confirmed and (not f.unsolicited) for f in self.fragments)

    def confirmed_events(self) -> list[PointValue]:
        numbers = {f.number for f in self.fragments if f.confirmed and (not f.unsolicited)}
        return [v for v in self.values if v.fragment in numbers]

    def unsolicited_entries(self) -> list[dict[str, Any]]:
        by_number = {f.number: f for f in self.fragments}
        entries = []
        for value in self.values:
            fragment = by_number.get(value.fragment)
            if fragment is None or not fragment.unsolicited or fragment.duplicate:
                continue
            copies = [
                f
                for f in self.fragments
                if f.unsolicited and f.info.sequence == fragment.info.sequence and (f.raw == fragment.raw)
            ]
            entries.append({**value.as_dict(with_kind=True), "confirmed": any(f.confirmed for f in copies)})
        return entries

    def annotate(self, meta: dict[str, Any]) -> dict[str, Any]:
        unsolicited = self.unsolicited_entries()
        if unsolicited:
            meta["unsolicited"] = unsolicited
        if self.warnings:
            meta.setdefault("warnings", []).extend(w for w in self.warnings if w not in meta["warnings"])
        return meta


class _Collector:
    """SOE handler that records every value delivered, tagged with its fragment."""

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
                fragment=self._log.current,
            )
            self.values.append(point)
            self._log.values.append(point)

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
    """dnp3py Master that logs every fragment it parses.

    dnp3py does not hand control-status echoes to the SOE handler, so the
    g12v1 objects are decoded from the raw fragments instead.
    """

    fragments: list[bytes] = field(default_factory=list, init=False)
    last_info: ResponseInfo | None = field(default=None, init=False)
    log: _CallLog = field(default_factory=_CallLog, init=False)

    def process_response(self, data: bytes) -> ResponseInfo | None:
        raw = bytes(data)
        self.fragments.append(raw)
        fragment = self.log.begin_fragment(raw)
        info = super().process_response(data)
        self.last_info = info
        self.log.finish_fragment(fragment, info)
        return info


class _UnconfirmedFragment(DNP3ProtocolError):
    """A partly decoded event fragment was left unconfirmed, so the exchange cannot go on."""


@dataclass
class _GuardedRunner(MasterTcpRunner):
    """MasterTcpRunner that does not CONFIRM event data it could only partly decode.

    A CONFIRM tells the outstation it may delete the events in that fragment.
    dnp3py 0.6.0 sends it even when it stopped decoding part way, so events
    after the stopping point would be lost. Unsolicited fragments always carry
    events; solicited ones do for Class 1-3 polls (`protect_solicited`).
    Every CONFIRM that is sent is recorded in the call log.
    """

    protect_solicited: bool = False
    withheld: list[tuple[int, bool]] = field(default_factory=list, init=False, repr=False)

    async def _send_confirm(self, sequence: int, *, uns: bool, deadline: float) -> None:
        info = getattr(self.master, "last_info", None)
        partial = (
            info is not None
            and info.truncation is not None
            and (info.sequence == sequence)
            and (bool(info.is_unsolicited) == uns)
        )
        if partial and (uns or self.protect_solicited):
            self.withheld.append((sequence, uns))
            logger.warning("Not confirming %s", describe_truncation(info))
            if not uns and (not info.fin):
                raise _UnconfirmedFragment(describe_truncation(info))
            return
        self.master.log.mark_confirmed(sequence, uns)
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
                address=cfg.master_address, outstation_address=outstation, response_timeout=cfg.timeout_s
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
        with anyio.CancelScope(shield=True):
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
    if isinstance(outstation, bool) or not isinstance(outstation, int) or (not 0 <= outstation <= MAX_LINK_ADDRESS):
        raise DNP3MasterError(f"outstation address must be an integer 0-{MAX_LINK_ADDRESS}, got {outstation!r}")


def _check_index(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or (not 0 <= value <= MAX_POINT_INDEX):
        raise DNP3MasterError(f"{name} must be an integer 0-{MAX_POINT_INDEX}, got {value!r}")


def _format_indexes(indexes: list[int]) -> str:
    shown = ", ".join(str(i) for i in indexes[:20])
    return shown + (f" and {len(indexes) - 20} more" if len(indexes) > 20 else "")


def _abandoned_outcome(stage: str) -> tuple[str, bool]:
    """(outcome, request_sent) for a control abandoned during `stage`."""
    if stage in ("queued", "connect"):
        return (OUTCOME_NOT_SENT, False)
    if stage == "SELECT":
        return (OUTCOME_REJECTED, True)
    return (OUTCOME_UNKNOWN, True)


def _with_confirmed_events(exc: DNP3MasterError, log: _CallLog) -> DNP3MasterError:
    """Attach the events of already-confirmed fragments to a failed event poll."""
    events = log.confirmed_events()
    if not events:
        return exc
    exc.meta["confirmed_events"] = [v.as_dict(with_kind=True) for v in events]
    message = (
        f"{exc}"
        " "
        f"{len(events)}"
        " event(s) from fragments that were already "
        "confirmed (so the outstation has deleted them) "
        "are in meta.confirmed_events."
    )
    replacement = type(exc)(message, exc.meta)
    replacement.__cause__ = exc
    return replacement


class DNP3Master:
    """Async DNP3 master over TCP."""

    deadline_slack_s: float = 2.0

    def __init__(self, config: DNP3Config | None = None) -> None:
        self.config = config or DNP3Config()
        self._lock = anyio.Lock()
        self.closing: str | None = None
        self._active: set[anyio.CancelScope] = set()

    def shutdown(self, reason: str) -> None:
        """Stop for good: cancel every operation in flight and refuse to send anything more.

        Called when the MCP client disconnects. A control that has not been sent
        yet is never sent afterwards.
        """
        if self.closing is None:
            self.closing = reason
            logger.warning("Shutting down DNP3 operations: %s", reason)
        for scope in list(self._active):
            scope.cancel()

    @property
    def read_deadline_s(self) -> float:
        """Upper bound for a whole read or poll: connect plus exchange, for every attempt."""
        return 2 * self.config.timeout_s * (self.config.max_retries + 1) + self.deadline_slack_s

    @property
    def control_deadline_s(self) -> float:
        """Upper bound for a whole control: connect plus SELECT and OPERATE (never retried)."""
        return 3 * self.config.timeout_s + self.deadline_slack_s

    async def read_binary_inputs(
        self, outstation: int, start: int, count: int, *, request_scope: anyio.CancelScope | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """READ binary inputs (g1, any variation) start..start+count-1."""
        return await self._read_static("binary_input", 1, outstation, start, count, request_scope)

    async def read_analog_inputs(
        self, outstation: int, start: int, count: int, *, request_scope: anyio.CancelScope | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """READ analog inputs (g30, any variation) start..start+count-1."""
        return await self._read_static("analog_input", 30, outstation, start, count, request_scope)

    async def poll_class(
        self, outstation: int, event_class: int, *, request_scope: anyio.CancelScope | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """READ class 0 (static data) or one event class (1-3).

        A Class 1-3 poll is not retried once any of its fragments has been
        confirmed, and if it fails after that, the confirmed events are
        returned in meta.confirmed_events.
        """
        _check_outstation(outstation)
        if isinstance(event_class, bool) or event_class not in (0, 1, 2, 3):
            raise DNP3MasterError(f"event_class must be 0, 1, 2 or 3, got {event_class!r}")
        events = event_class > 0

        def build(session: _Session) -> RequestFragment:
            if event_class == 0:
                return build_all_objects_request(FunctionCode.READ, CLASS_DATA_GROUP, 1, seq=session.next_sequence())
            return session.master.build_class_poll(
                class_1=event_class == 1, class_2=event_class == 2, class_3=event_class == 3
            )

        log = _CallLog()
        started = time.perf_counter()
        try:
            try:
                reply, attempts = await self._read_with_retries(
                    outstation,
                    build,
                    log,
                    protect_solicited=events,
                    retry_allowed=(lambda: not log.confirmed_solicited()) if events else None,
                    request_scope=request_scope,
                )
            except _UnconfirmedFragment as exc:
                raise DNP3ProtocolError(
                    (
                        "The class "
                        f"{event_class}"
                        " poll response could not be fully decoded: "
                        f"{exc}"
                        ". That fragment was not confirmed, so an "
                        "outstation that waits for confirmation keeps its"
                        " events."
                    ),
                    log.annotate({**self._meta(outstation, started, 1), "incomplete": True, "warnings": [str(exc)]}),
                ) from exc
            meta = log.annotate(self._meta(outstation, started, attempts, reply))
            self._raise_for_iin(reply.iin, "READ", meta)
            if events and reply.warnings:
                raise DNP3ProtocolError(
                    (
                        "The class "
                        f"{event_class}"
                        " poll response could not be fully decoded, so "
                        "the fragment concerned was not confirmed and the"
                        " poll is not reported as complete. An outstation"
                        " that waits for confirmation keeps the events of"
                        " that fragment. "
                    )
                    + " ".join(reply.warnings),
                    meta,
                )
        except DNP3MasterError as exc:
            if events:
                raise _with_confirmed_events(exc, log) from exc.__cause__
            raise
        points = [v.as_dict(with_kind=True) for v in reply.values if not v.unsolicited]
        return ({"outstation": outstation, "class": event_class, "count": len(points), "points": points}, meta)

    async def write_binary_output(
        self, outstation: int, index: int, value: bool, *, request_scope: anyio.CancelScope | None = None
    ) -> dict[str, Any]:
        """Latch a binary output on or off with a CROB and confirm the outstation accepted it."""
        not_sent = {"outcome": OUTCOME_NOT_SENT, "request_sent": False}
        if not self.config.writes_enabled:
            raise DNP3WritesDisabledError("Write operations are disabled (DNP3_WRITES_ENABLED is false)", not_sent)
        try:
            _check_outstation(outstation)
            _check_index("index", index)
            if not isinstance(value, bool):
                raise DNP3MasterError(f"value must be a boolean, got {value!r}")
        except DNP3MasterError as exc:
            exc.meta.update(not_sent)
            raise
        code = "LATCH_ON" if value else "LATCH_OFF"
        mode = self.config.control_mode
        started = time.perf_counter()
        base_meta = {"outstation": outstation, "index": index, "control_code": code, "mode": mode}
        steps = ("SELECT", "OPERATE") if mode == "select_before_operate" else ("DIRECT_OPERATE",)
        log = _CallLog()
        stage = "queued"
        iin = IIN(0)
        deadline = self.control_deadline_s
        try:
            with anyio.move_on_after(deadline) as scope:
                self._active.add(scope)
                try:
                    async with self._lock:
                        stage = "connect"
                        await self._before_send(scope, request_scope)
                        session = _Session(self, outstation, log)
                        try:
                            try:
                                await session.open()
                            except DNP3ConnectionError as exc:
                                raise DNP3ControlError(
                                    f"{exc}. No control was sent.", {**base_meta, **not_sent}
                                ) from exc
                            for function in steps:
                                await self._before_send(scope, request_scope)
                                stage = function
                                iin |= await self._control_step(session, function, index, value, base_meta)
                            stage = "done"
                        finally:
                            await session.close()
                finally:
                    self._active.discard(scope)
            if scope.cancelled_caught and stage != "done":
                raise self._control_abort_error(stage, index, deadline, base_meta)
        except DNP3MasterError as exc:
            log.annotate(exc.meta)
            raise
        except BaseException:
            outcome, sent = _abandoned_outcome(stage)
            logger.warning(
                (
                    "Control on binary output %d of outstation %d "
                    "cancelled by the client during %s; nothing more "
                    "will be sent (outcome %s, request_sent %s)%s"
                ),
                index,
                outstation,
                stage,
                outcome,
                str(sent).lower(),
                "; the control may have been applied, read the output's state back before retrying"
                if outcome == OUTCOME_UNKNOWN
                else "",
            )
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

    async def _read_static(
        self,
        kind: str,
        group: int,
        outstation: int,
        start: int,
        count: int,
        request_scope: anyio.CancelScope | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        _check_outstation(outstation)
        _check_index("start_index", start)
        if isinstance(count, bool) or not isinstance(count, int) or (not 1 <= count <= MAX_READ_COUNT):
            raise DNP3MasterError(f"count must be an integer 1-{MAX_READ_COUNT}, got {count!r}")
        stop = start + count - 1
        if stop > MAX_POINT_INDEX:
            raise DNP3MasterError(f"start_index + count - 1 must not exceed {MAX_POINT_INDEX}, got {stop}")
        log = _CallLog()
        started = time.perf_counter()
        reply, attempts = await self._read_with_retries(
            outstation,
            lambda session: session.master.build_range_poll(group, 0, start, stop),
            log,
            request_scope=request_scope,
        )
        meta = log.annotate(self._meta(outstation, started, attempts, reply))
        self._raise_for_iin(reply.iin, "READ", meta)
        by_index: dict[int, PointValue] = {}
        for value in reply.values:
            if value.unsolicited or value.kind != kind or (not start <= value.index <= stop):
                continue
            by_index[value.index] = value
        missing = [i for i in range(start, stop + 1) if i not in by_index]
        if missing:
            meta["returned_indexes"] = sorted(by_index)
            label = kind.replace("_", " ")
            raise DNP3ProtocolError(
                f"Outstation {outstation} returned no {label} value for index {_format_indexes(missing)}", meta
            )
        return ([by_index[i].as_dict() for i in range(start, stop + 1)], meta)

    async def _read_with_retries(
        self,
        outstation: int,
        build: Callable[[_Session], RequestFragment],
        log: _CallLog,
        *,
        protect_solicited: bool = False,
        retry_allowed: Callable[[], bool] | None = None,
        request_scope: anyio.CancelScope | None = None,
    ) -> tuple[_Reply, int]:

        async def attempt() -> _Reply:
            session = _Session(self, outstation, log, protect_solicited=protect_solicited)
            try:
                await session.open()
                # A cancellation/deadline may arrive in the connection-complete
                # step, before dnp3py's next await. Polls can consume events too.
                await self._before_send(scope, request_scope)
                return await session.request(build(session))
            finally:
                await session.close()

        deadline = self.read_deadline_s
        started = False
        try:
            with anyio.move_on_after(deadline) as scope:
                self._active.add(scope)
                try:
                    async with self._lock:
                        started = True
                        await self._before_send(scope, request_scope)
                        return await self._with_retries(attempt, retry_allowed, scope, request_scope)
                finally:
                    self._active.discard(scope)
            if self.closing is not None:
                raise DNP3ConnectionError(f"Request abandoned: {self.closing}", {"closing": True})
            where = f"Outstation {outstation} at {self.config.host}:{self.config.port}"
            if not started:
                raise DNP3TimeoutError(
                    (
                        "The request to "
                        f"{where.lower()[0] + where[1:]}"
                        " did not start within the overall deadline of "
                        f"{deadline:g}"
                        " s: earlier DNP3 calls were still running. "
                        "Nothing was sent."
                    ),
                    {"deadline_s": deadline, "queued": True},
                )
            raise DNP3TimeoutError(
                f"{where} did not complete the request within the overall deadline of {deadline:g} s",
                {"deadline_s": deadline},
            )
        except DNP3MasterError as exc:
            log.annotate(exc.meta)
            raise

    async def _before_send(self, scope: anyio.CancelScope, request_scope: anyio.CancelScope | None = None) -> None:
        """Last stop before anything goes on the wire: honour a client cancellation, a disconnect or the deadline.

        Each condition is checked explicitly, not only through a checkpoint.
        anyio does not interrupt a task whose wait has just completed (a lock
        was granted, a connect finished, an answer arrived): the MCP SDK's
        RequestResponder.cancel() marks the request's scope cancelled at once,
        but the task still runs one more step, and dnp3py writes a frame
        before its first await. Likewise the event loop may resume the task
        before it runs the deadline's own timer.
        """
        if request_scope is not None and request_scope.cancel_called:
            await self._await_cancellation(request_scope)
        if self.closing is not None or anyio.current_time() >= scope.deadline:
            scope.cancel()
        await anyio.lowlevel.checkpoint()

    @staticmethod
    async def _await_cancellation(request_scope: anyio.CancelScope) -> NoReturn:
        """The client cancelled this call: send nothing, and let anyio deliver that cancellation.

        The SDK has already answered "Request cancelled", so the call must end
        with the cancellation itself rather than with a result. anyio delivers
        it at one of the next checkpoints.
        """
        for _ in range(1000):
            await anyio.sleep(0)
        raise anyio.get_cancelled_exc_class()(f"request cancelled by the MCP client ({id(request_scope):x})")

    async def _with_retries(
        self,
        operation: Callable[[], Awaitable[T]],
        retry_allowed: Callable[[], bool] | None = None,
        scope: anyio.CancelScope | None = None,
        request_scope: anyio.CancelScope | None = None,
    ) -> tuple[T, int]:
        attempts = 0
        while True:
            attempts += 1
            if scope is not None and attempts > 1:
                await self._before_send(scope, request_scope)
            try:
                return (await operation(), attempts)
            except (DNP3ConnectionError, DNP3TimeoutError) as exc:
                exc.meta.setdefault("attempts", attempts)
                if attempts > self.config.max_retries:
                    raise
                if retry_allowed is not None and (not retry_allowed()):
                    exc.meta["retried"] = False
                    raise
                logger.warning("%s; retrying (%d of %d)", exc, attempts, self.config.max_retries)

    def _control_abort_error(self, stage: str, index: int, deadline: float, meta: dict[str, Any]) -> DNP3ControlError:
        """A control cut short by the overall deadline or by the client disconnecting.

        `stage` is how far the control got: "queued" (waiting for the lock) or
        "connect" (nothing sent), "SELECT" (OPERATE never sent) or "OPERATE" /
        "DIRECT_OPERATE" (may have been applied).
        """
        if self.closing is not None:
            message = f"The control on binary output {index} was abandoned ({self.closing})"
            extra: dict[str, Any] = {"closing": True}
        else:
            message = (
                f"The control on binary output {index} did not complete within the overall deadline of {deadline:g} s"
            )
            extra = {"deadline_s": deadline}
        if stage == "queued":
            error = DNP3ControlError(
                (
                    f"{message}"
                    " while waiting for earlier DNP3 calls to finish."
                    " No control was sent, and it will not be sent "
                    "later."
                ),
                {**meta, "outcome": OUTCOME_NOT_SENT, "request_sent": False, "queued": True, **extra},
            )
        elif stage == "connect":
            error = DNP3ControlError(
                f"{message} before anything was sent. No control was sent.",
                {**meta, "outcome": OUTCOME_NOT_SENT, "request_sent": False, **extra},
            )
        elif stage == "SELECT":
            error = DNP3ControlError(
                f"{message} after the SELECT. Nothing was operated; OPERATE was not sent.",
                {**meta, "outcome": OUTCOME_REJECTED, "request_sent": True, "failed_step": stage, **extra},
            )
        else:
            error = DNP3ControlError(
                (
                    f"{message}"
                    " during "
                    f"{stage}"
                    ". The control may have been applied; read the "
                    "output's state back before retrying."
                ),
                {**meta, "outcome": OUTCOME_UNKNOWN, "request_sent": True, "failed_step": stage, **extra},
            )
        if self.closing is not None:
            logger.warning("%s (outcome %s)", error, error.meta["outcome"])
        return error

    async def _control_step(
        self, session: _Session, function: str, index: int, value: bool, meta: dict[str, Any]
    ) -> IIN:
        """Send one control request and require a matching SUCCESS echo with no IIN error.

        Every failure carries meta.outcome and meta.request_sent. A SELECT
        operates nothing and OPERATE is never sent after a failed one, so any
        SELECT failure once the SELECT was sent is "rejected". After an OPERATE
        or DIRECT_OPERATE, only a matching echo with a non-SUCCESS status is
        "rejected"; everything else that is not a clean success is "unknown".
        """
        acting = function != "SELECT"
        tail = (
            "The control may have been applied; read the output's state back before retrying."
            if acting
            else "Nothing was operated; OPERATE was not sent."
        )
        unanswered = OUTCOME_UNKNOWN if acting else OUTCOME_REJECTED
        try:
            fragment = build_crob_request(function, index, value, session.next_sequence())
        except Exception as exc:
            raise DNP3ControlError(
                f"Could not encode {function} for binary output {index}: {exc}. Nothing was sent.",
                {**meta, "outcome": OUTCOME_NOT_SENT, "request_sent": False, "failed_step": function},
            ) from exc
        sent = fragment.objects[0].data[-CROB_SIZE:]
        sent_meta = {**meta, "request_sent": True, "failed_step": function}
        try:
            reply = await session.request(fragment)
        except (DNP3TimeoutError, DNP3ConnectionError) as exc:
            raise DNP3ControlError(
                f"{function} for binary output {index}: {exc}. {tail}", {**sent_meta, "outcome": unanswered}
            ) from exc
        except Exception as exc:
            raise DNP3ControlError(
                f"{function} for binary output {index} failed: {type(exc).__name__}: {exc}. {tail}",
                {**sent_meta, "outcome": unanswered},
            ) from exc
        step_meta = {**sent_meta, "iin": iin_names(reply.iin)}
        iin_error = iin_names(reply.iin & REQUEST_ERROR_IIN)
        echo = crob_echoes(reply.fragments).get(index)
        if echo is None:
            detail = f" (IIN {', '.join(iin_error)})" if iin_error else ""
            raise DNP3ControlError(
                f"{function} response for binary output {index} carried no control status{detail}. {tail}",
                {**step_meta, "outcome": unanswered},
            )
        if echo[:-1] != sent[:-1]:
            raise DNP3ControlError(
                f"{function} response for binary output {index} does not echo the control that was sent. {tail}",
                {
                    **step_meta,
                    "status": "SELECT_MISMATCH" if function == "SELECT" else "ECHO_MISMATCH",
                    "sent": sent.hex(),
                    "echoed": echo.hex(),
                    "outcome": unanswered,
                },
            )
        status = echo[-1] & 127
        if status != CommandStatus.SUCCESS:
            name = command_status_name(status)
            follow = "Nothing was operated; OPERATE was not sent." if not acting else "Nothing was operated."
            raise DNP3ControlError(
                f"Outstation rejected {function} for binary output {index} with status {name}. {follow}",
                {**step_meta, "status": name, "outcome": OUTCOME_REJECTED},
            )
        if iin_error:
            raise DNP3ControlError(
                (
                    f"{function}"
                    " for binary output "
                    f"{index}"
                    " was answered with status SUCCESS but IIN "
                    f"{', '.join(iin_error)}"
                    ". "
                    f"{tail}"
                ),
                {**step_meta, "status": "SUCCESS", "outcome": unanswered},
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
                meta["incomplete"] = True
                meta["warnings"] = list(warnings)
        return meta
