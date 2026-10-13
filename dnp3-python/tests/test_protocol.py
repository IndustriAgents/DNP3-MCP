"""Wire-level behaviour against a scripted outstation: CROB encoding, CONFIRMs, unsolicited data, echoes.

These pin down the places where the server works around dnp3py 0.6.0 (review findings 1, 2, 3, 5).
"""

from __future__ import annotations

import time
from typing import Any

import anyio
import pytest
from dnp3.core.enums import FunctionCode

from dnp3_mcp.dnp3_master import (
    DNP3ConnectionError,
    DNP3ControlError,
    DNP3ProtocolError,
    DNP3TimeoutError,
    build_crob_request,
    crob_echoes,
)

from .conftest import (
    MOCK_OUTSTATION_ADDRESS as OUTSTATION,
)
from .conftest import (
    UNDECODABLE,
    Received,
    binary_events,
    binary_statics,
    crob_echo,
    make_master,
    response,
)

pytestmark = pytest.mark.anyio

ZEROS = "00 00 00 00 00 00 00 00"  # on-time and off-time


# -- CROB encoding (finding 1) ------------------------------------------------


@pytest.mark.parametrize(
    ("function", "code"),
    [("SELECT", "03"), ("OPERATE", "04"), ("DIRECT_OPERATE", "05")],
)
def test_crob_golden_bytes_index_3(function: str, code: str) -> None:
    on = build_crob_request(function, 3, True, seq=0).to_bytes()
    off = build_crob_request(function, 3, False, seq=1).to_bytes()
    # control, function | g12v1, qualifier 0x17, count 1 | index 3 | LATCH_ON/OFF, count 1, times, status
    assert on == bytes.fromhex(f"c0 {code} 0c 01 17 01 03 03 01 {ZEROS} 00")
    assert off == bytes.fromhex(f"c1 {code} 0c 01 17 01 03 04 01 {ZEROS} 00")


@pytest.mark.parametrize(
    ("function", "code"),
    [("SELECT", "03"), ("OPERATE", "04"), ("DIRECT_OPERATE", "05")],
)
def test_crob_golden_bytes_index_300(function: str, code: str) -> None:
    on = build_crob_request(function, 300, True, seq=0).to_bytes()
    # qualifier 0x28: two-octet count (01 00) and two-octet index (2c 01 = 300)
    assert on == bytes.fromhex(f"c0 {code} 0c 01 28 01 00 2c 01 03 01 {ZEROS} 00")


def test_crob_qualifier_boundaries() -> None:
    assert build_crob_request("SELECT", 255, True, seq=0).to_bytes()[4] == 0x17
    assert build_crob_request("SELECT", 256, True, seq=0).to_bytes()[4:9] == bytes.fromhex("28 01 00 00 01")
    assert build_crob_request("SELECT", 65535, True, seq=0).to_bytes()[4:9] == bytes.fromhex("28 01 00 ff ff")
    with pytest.raises(ValueError):
        build_crob_request("SELECT", 65536, True, seq=0)


def test_crob_echoes_round_trip() -> None:
    for index in (3, 300):
        request = build_crob_request("SELECT", index, True, seq=0).to_bytes()
        echo = response(request[2:], seq=0)
        assert crob_echoes([echo]) == {index: request[-11:]}


async def test_sbo_on_the_wire_at_index_300(scripted: Any) -> None:
    outstation = await scripted(lambda request: [crob_echo(request)])
    master = make_master(outstation.port)
    meta = await master.write_binary_output(OUTSTATION, 300, True)
    assert meta["status"] == "SUCCESS"
    select, operate = outstation.received
    assert select.data == bytes.fromhex(f"c0 03 0c 01 28 01 00 2c 01 03 01 {ZEROS} 00")
    assert operate.data == bytes.fromhex(f"c1 04 0c 01 28 01 00 2c 01 03 01 {ZEROS} 00")


# -- echo comparison (finding 5) ----------------------------------------------


async def test_select_echo_mismatch_stops_before_operate(scripted: Any) -> None:
    outstation = await scripted(lambda request: [crob_echo(request, tamper=True)])
    master = make_master(outstation.port)
    with pytest.raises(DNP3ControlError, match="Nothing was operated; OPERATE was not sent") as excinfo:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert excinfo.value.meta["status"] == "SELECT_MISMATCH"
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("rejected", True)
    assert outstation.functions() == [FunctionCode.SELECT]


async def test_direct_operate_echo_mismatch_is_reported_as_unknown(scripted: Any) -> None:
    outstation = await scripted(lambda request: [crob_echo(request, tamper=True)])
    master = make_master(outstation.port, control_mode="direct_operate")
    with pytest.raises(
        DNP3ControlError, match="may have been applied; read the output.s state back before retrying"
    ) as excinfo:
        await master.write_binary_output(OUTSTATION, 3, False)
    assert excinfo.value.meta["status"] == "ECHO_MISMATCH"
    assert outstation.functions() == [FunctionCode.DIRECT_OPERATE]


# -- CONFIRM handling (finding 3) ---------------------------------------------


def _answer_reads(objects: bytes, *, con: bool = True) -> Any:
    def script(request: Received) -> list[bytes]:
        return [response(objects, seq=request.seq, con=con)] if request.function == FunctionCode.READ else []

    return script


async def test_event_poll_confirms_exactly_once(scripted: Any) -> None:
    outstation = await scripted(_answer_reads(binary_events((2, True), (5, False))))
    master = make_master(outstation.port)
    data, meta = await master.poll_class(OUTSTATION, 1)
    assert [(p["type"], p["index"], p["value"]) for p in data["points"]] == [
        ("binary_input", 2, True),
        ("binary_input", 5, False),
    ]
    read = outstation.received[0]
    confirms = outstation.confirms()
    # Fails if dnp3py's CONFIRM is suppressed: events would then stay on the outstation.
    assert [(c.seq, c.uns) for c in confirms] == [(read.seq, False)]
    assert "incomplete" not in meta


async def test_partly_decoded_event_poll_is_not_confirmed(scripted: Any) -> None:
    # The event after the undecodable block would be lost if this fragment were confirmed.
    outstation = await scripted(_answer_reads(UNDECODABLE + binary_events((2, True))))
    master = make_master(outstation.port)
    with pytest.raises(DNP3ProtocolError, match="not confirmed") as excinfo:
        await master.poll_class(OUTSTATION, 2)
    assert outstation.confirms() == []
    assert excinfo.value.meta["incomplete"] is True
    assert "group 50 variation 4" in excinfo.value.meta["warnings"][0]


async def test_partly_decoded_first_fragment_ends_the_poll_at_once(scripted: Any) -> None:
    def script(request: Received) -> list[bytes]:
        if request.function != FunctionCode.READ:
            return []
        return [response(UNDECODABLE + binary_events((2, True)), seq=request.seq, con=True, fin=False)]

    outstation = await scripted(script)
    master = make_master(outstation.port, timeout_ms=3000)
    started = time.monotonic()
    with pytest.raises(DNP3ProtocolError, match="not confirmed"):
        await master.poll_class(OUTSTATION, 3)
    assert time.monotonic() - started < 2.0  # did not sit out the response timeout
    assert outstation.confirms() == []


async def test_class_0_truncation_is_confirmed_and_flagged(scripted: Any) -> None:
    # Static data is not deleted by a CONFIRM, so a partial Class 0 response is confirmed and flagged.
    outstation = await scripted(_answer_reads(binary_statics(0, [True, False]) + UNDECODABLE))
    master = make_master(outstation.port)
    data, meta = await master.poll_class(OUTSTATION, 0)
    assert data["count"] == 2
    assert meta["incomplete"] is True
    assert len(outstation.confirms()) == 1


# -- unsolicited data (finding 2) ---------------------------------------------


def _push_unsolicited_then(answer: Any, unsolicited: bytes) -> Any:
    def script(request: Received) -> list[bytes]:
        return [response(unsolicited, seq=0, uns=True, con=True), *answer(request)]

    return script


async def test_unsolicited_events_during_a_read_are_returned(scripted: Any) -> None:
    def statics(request: Received) -> list[bytes]:
        return [response(binary_statics(0, [False, True]), seq=request.seq)]

    outstation = await scripted(_push_unsolicited_then(statics, binary_events((7, True))))
    master = make_master(outstation.port)
    points, meta = await master.read_binary_inputs(OUTSTATION, 0, 2)
    assert [p["value"] for p in points] == [False, True]
    assert [(u["type"], u["index"], u["value"], u["confirmed"]) for u in meta["unsolicited"]] == [
        ("binary_input", 7, True, True)
    ]
    # It was confirmed, which is why it must not be dropped.
    assert [(c.seq, c.uns) for c in outstation.confirms()] == [(0, True)]


async def test_unsolicited_events_during_a_control_are_returned(scripted: Any) -> None:
    outstation = await scripted(_push_unsolicited_then(lambda r: [crob_echo(r)], binary_events((1, False))))
    master = make_master(outstation.port)
    meta = await master.write_binary_output(OUTSTATION, 3, True)
    assert meta["status"] == "SUCCESS"
    # Pushed before SELECT and again before OPERATE with the same sequence number and content:
    # a retransmission, reported once.
    assert [(u["index"], u["value"], u["confirmed"]) for u in meta["unsolicited"]] == [(1, False, True)]


async def test_unsolicited_events_survive_a_failed_call(scripted: Any) -> None:
    outstation = await scripted(_push_unsolicited_then(lambda r: [], binary_events((4, True))))
    master = make_master(outstation.port, timeout_ms=300)
    with pytest.raises(DNP3TimeoutError) as excinfo:
        await master.read_binary_inputs(OUTSTATION, 0, 1)
    assert [(u["index"], u["value"]) for u in excinfo.value.meta["unsolicited"]] == [(4, True)]


async def test_partly_decoded_unsolicited_is_not_confirmed(scripted: Any) -> None:
    def statics(request: Received) -> list[bytes]:
        return [response(binary_statics(0, [True]), seq=request.seq)]

    outstation = await scripted(_push_unsolicited_then(statics, binary_events((4, True)) + UNDECODABLE))
    master = make_master(outstation.port)
    points, meta = await master.read_binary_inputs(OUTSTATION, 0, 1)
    assert points[0]["value"] is True
    assert outstation.confirms() == []
    assert any("Unsolicited" in w and "not confirmed" in w for w in meta["warnings"])
    # What was decoded is reported, marked as not confirmed (the outstation keeps it).
    assert [(u["index"], u["value"], u["confirmed"]) for u in meta["unsolicited"]] == [(4, True, False)]


async def test_unsolicited_retransmissions_are_reported_once(scripted: Any) -> None:
    pushed = response(binary_events((6, True)), seq=3, uns=True, con=True)

    def script(request: Received) -> list[bytes]:
        if request.function != FunctionCode.READ:
            return []
        changed = response(binary_events((6, False)), seq=4, uns=True, con=True)  # a new one: next sequence
        return [pushed, pushed, changed, response(binary_statics(0, [True]), seq=request.seq)]

    outstation = await scripted(script)
    _, meta = await make_master(outstation.port).read_binary_inputs(OUTSTATION, 0, 1)
    assert [(u["index"], u["value"], u["confirmed"]) for u in meta["unsolicited"]] == [
        (6, True, True),
        (6, False, True),
    ]


# -- confirmed events are never lost (review 2, finding 1) ---------------------


def _fragment(objects: bytes, *, seq: int, fir: bool, fin: bool, con: bool) -> bytes:
    control = (0x80 if fir else 0) | (0x40 if fin else 0) | (0x20 if con else 0) | (seq & 0x0F)
    return bytes([control, 0x81, 0, 0]) + objects


async def test_events_of_confirmed_fragments_survive_a_later_undecodable_fragment(scripted: Any) -> None:
    def script(request: Received) -> list[bytes]:
        if request.function != FunctionCode.READ:
            return []
        first = _fragment(
            binary_events((1, True), (2, False), (3, True)), seq=request.seq, fir=True, fin=False, con=True
        )
        second = _fragment(UNDECODABLE + binary_events((9, True)), seq=request.seq + 1, fir=False, fin=True, con=True)
        return [first, second]

    outstation = await scripted(script)
    with pytest.raises(DNP3ProtocolError, match="3 event.* already confirmed") as excinfo:
        await make_master(outstation.port).poll_class(OUTSTATION, 1)
    confirmed = [(e["index"], e["value"]) for e in excinfo.value.meta["confirmed_events"]]
    assert confirmed == [(1, True), (2, False), (3, True)]
    # Fragment 1 was confirmed (the outstation deletes those events); fragment 2 was not.
    assert len(outstation.confirms()) == 1


async def test_event_poll_is_not_retried_after_a_confirm(scripted: Any) -> None:
    def script(request: Received) -> list[bytes]:
        if request.function != FunctionCode.READ:
            return []
        if request.connection == 1:  # fragment 1 arrives and is confirmed, then the link goes quiet
            return [
                _fragment(
                    binary_events((1, True), (2, False), (3, True)), seq=request.seq, fir=True, fin=False, con=True
                )
            ]
        return [_fragment(binary_events((9, True)), seq=request.seq, fir=True, fin=True, con=True)]

    outstation = await scripted(script)
    master = make_master(outstation.port, timeout_ms=400, max_retries=2)
    with pytest.raises(DNP3TimeoutError) as excinfo:
        await master.poll_class(OUTSTATION, 1)
    assert outstation.connections == 1  # not retried: a retry would only fetch what is left
    assert excinfo.value.meta["retried"] is False
    assert [(e["index"], e["value"]) for e in excinfo.value.meta["confirmed_events"]] == [
        (1, True),
        (2, False),
        (3, True),
    ]


async def test_static_reads_are_still_retried(scripted: Any) -> None:
    def script(request: Received) -> list[bytes]:
        if request.connection == 1:
            return []  # silent the first time
        return [response(binary_statics(0, [True]), seq=request.seq)]

    outstation = await scripted(script)
    points, meta = await make_master(outstation.port, timeout_ms=300, max_retries=1).read_binary_inputs(
        OUTSTATION, 0, 1
    )
    assert points[0]["value"] is True and meta["attempts"] == 2


# -- overall deadline and garbage peers (rule 12) -------------------------------


@pytest.fixture
async def garbage_peer() -> Any:
    """A TCP peer that answers everything with random bytes, sprinkled with DNP3 start octets."""
    import asyncio
    import os

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                writer.write(os.urandom(61) + bytes.fromhex("0564 ff"))
                await writer.drain()
                await asyncio.sleep(0.01)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


async def test_garbage_peer_read_is_a_clean_error(garbage_peer: int) -> None:
    master = make_master(garbage_peer, timeout_ms=500, max_retries=1)
    started = time.monotonic()
    with pytest.raises((DNP3TimeoutError, DNP3ConnectionError, DNP3ProtocolError)):
        await master.read_binary_inputs(OUTSTATION, 0, 1)
    assert time.monotonic() - started <= master.read_deadline_s


async def test_garbage_peer_control_is_unknown(garbage_peer: int) -> None:
    master = make_master(garbage_peer, timeout_ms=500, control_mode="direct_operate")
    started = time.monotonic()
    with pytest.raises(DNP3ControlError, match="may have been applied") as excinfo:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == ("unknown", True)
    assert time.monotonic() - started <= master.control_deadline_s


async def _hang(*args: Any, **kwargs: Any) -> Any:
    import anyio

    await anyio.sleep(3600)


async def test_overall_deadline_bounds_a_hung_read(scripted: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from dnp3_mcp import dnp3_master

    outstation = await scripted(None)
    master = make_master(outstation.port, timeout_ms=100, max_retries=1)
    master.deadline_slack_s = 0.2
    monkeypatch.setattr(dnp3_master._Session, "request", _hang)  # a stack that never gives up
    started = time.monotonic()
    with anyio.fail_after(5), pytest.raises(DNP3TimeoutError, match="overall deadline") as excinfo:
        await master.read_binary_inputs(OUTSTATION, 0, 1)
    assert time.monotonic() - started < 2.0
    assert excinfo.value.meta["deadline_s"] == pytest.approx(master.read_deadline_s)


@pytest.mark.parametrize(("mode", "outcome"), [("direct_operate", "unknown"), ("select_before_operate", "rejected")])
async def test_overall_deadline_bounds_a_hung_control(
    scripted: Any, monkeypatch: pytest.MonkeyPatch, mode: str, outcome: str
) -> None:
    from dnp3_mcp import dnp3_master

    outstation = await scripted(None)
    master = make_master(outstation.port, timeout_ms=100, control_mode=mode)
    master.deadline_slack_s = 0.2
    monkeypatch.setattr(dnp3_master._Session, "request", _hang)
    started = time.monotonic()
    with anyio.fail_after(5), pytest.raises(DNP3ControlError, match="overall deadline") as excinfo:
        await master.write_binary_output(OUTSTATION, 3, True)
    assert time.monotonic() - started < 2.0
    assert (excinfo.value.meta["outcome"], excinfo.value.meta["request_sent"]) == (outcome, True)
