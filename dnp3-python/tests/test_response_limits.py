"""Per-call DNP3 budgets stop before CONFIRM without losing confirmed events."""

import pytest
from dnp3.core.enums import FunctionCode

from dnp3_mcp.adapters import dnp3_master as native
from dnp3_mcp.dnp3_master import DNP3ControlError, DNP3ProtocolError

from .conftest import MOCK_OUTSTATION_ADDRESS as OUTSTATION
from .conftest import binary_events, binary_statics, crob_echo, make_master, response
from .test_protocol import _fragment

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("resource", ["MAX_CALL_FRAGMENTS", "MAX_CALL_RAW_BYTES", "MAX_CALL_POINTS"])
async def test_event_budget_keeps_confirmed_prefix_and_withholds_overflow(scripted, monkeypatch, resource):
    def script(request):
        first = _fragment(binary_events((1, True)), seq=request.seq, fir=True, fin=False, con=True)
        second = _fragment(binary_events((2, False)), seq=(request.seq + 1) % 16, fir=False, fin=True, con=True)
        return [first, second]

    sample = response(binary_events((1, True)), seq=0)
    monkeypatch.setattr(native, resource, len(sample) if resource == "MAX_CALL_RAW_BYTES" else 1)
    outstation = await scripted(script)
    with pytest.raises(DNP3ProtocolError, match="budget exhausted") as caught:
        await make_master(outstation.port, max_retries=2).poll_class(OUTSTATION, 1)
    meta = caught.value.meta
    assert meta["code"] == "response_limit" and meta["incomplete"] and meta["retried"] is False
    assert [(p["index"], p["value"]) for p in meta["confirmed_events"]] == [(1, True)]
    assert len(outstation.confirms()) == 1 and outstation.connections == 1
    assert meta["response_budget"]["points"] <= 1


async def test_overfull_first_fragment_is_not_confirmed(scripted, monkeypatch):
    monkeypatch.setattr(native, "MAX_CALL_POINTS", 1)
    outstation = await scripted(lambda req: [response(binary_events((1, True), (2, False)), seq=req.seq, con=True)])
    with pytest.raises(DNP3ProtocolError) as caught:
        await make_master(outstation.port).poll_class(OUTSTATION, 1)
    assert caught.value.meta["code"] == "response_limit"
    assert "confirmed_events" not in caught.value.meta
    assert outstation.confirms() == []


async def test_unsolicited_budget_also_bounds_static_calls_and_preserves_confirmed_data(scripted, monkeypatch):
    monkeypatch.setattr(native, "MAX_CALL_FRAGMENTS", 1)

    def script(req):
        return [
            response(binary_events((7, True)), seq=0, uns=True, con=True),
            response(binary_statics(0, [True]), seq=req.seq),
        ]

    outstation = await scripted(script)
    with pytest.raises(DNP3ProtocolError) as caught:
        await make_master(outstation.port, max_retries=2).read_binary_inputs(OUTSTATION, 0, 1)
    assert caught.value.meta["code"] == "response_limit"
    assert caught.value.meta["unsolicited"][0]["confirmed"] is True
    assert caught.value.meta["unsolicited"][0]["index"] == 7
    assert outstation.connections == 1 and len(outstation.confirms()) == 1


@pytest.mark.parametrize("control_mode,outcome", [("select_before_operate", "rejected"), ("direct_operate", "unknown")])
async def test_control_budget_preserves_dispatch_outcome(scripted, monkeypatch, control_mode, outcome):
    monkeypatch.setattr(native, "MAX_CALL_FRAGMENTS", 1)

    def script(req):
        return [response(binary_events((7, True)), seq=0, uns=True, con=True), crob_echo(req)]

    outstation = await scripted(script)
    with pytest.raises(DNP3ControlError) as caught:
        await make_master(outstation.port, control_mode=control_mode).write_binary_output(OUTSTATION, 3, True)
    assert caught.value.meta["outcome"] == outcome and caught.value.meta["request_sent"]
    assert caught.value.meta["code"] == "response_limit"
    assert caught.value.meta["unsolicited"][0]["confirmed"]
    assert not any(req.function == FunctionCode.OPERATE for req in outstation.received)
    assert len(outstation.confirms()) == 1 and outstation.connections == 1


async def test_byte_budget_rejects_before_owned_parse_or_retention():
    master = native._CapturingMaster()
    master.log.raw_bytes = native.MAX_CALL_RAW_BYTES
    with pytest.raises(DNP3ProtocolError):
        master.process_response(b"anything")
    assert master.fragments == [] and master.log.fragments == [] and master.log.values == []


async def test_operate_budget_exhaustion_after_successful_select_remains_unknown(scripted, monkeypatch):
    monkeypatch.setattr(native, "MAX_CALL_FRAGMENTS", 1)
    outstation = await scripted(lambda request: [crob_echo(request)])
    with pytest.raises(DNP3ControlError) as caught:
        await make_master(outstation.port, max_retries=2).write_binary_output(OUTSTATION, 3, True)
    assert caught.value.meta["outcome"] == "unknown" and caught.value.meta["request_sent"]
    assert caught.value.meta["code"] == "response_limit"
    assert caught.value.meta["failed_step"] == "OPERATE"
    assert outstation.functions() == [FunctionCode.SELECT, FunctionCode.OPERATE]
    assert outstation.connections == 1 and outstation.confirms() == []


async def test_retry_cannot_reset_the_call_budget_or_lose_confirmed_unsolicited(scripted, monkeypatch):
    monkeypatch.setattr(native, "MAX_CALL_FRAGMENTS", 1)

    def script(request):
        if request.connection == 1:
            # Confirm this unsolicited event, then time out waiting for the read.
            return [response(binary_events((7, True)), seq=0, uns=True, con=True)]
        return [response(binary_statics(0, [True]), seq=request.seq)]

    outstation = await scripted(script)
    with pytest.raises(DNP3ProtocolError) as caught:
        await make_master(outstation.port, max_retries=2).read_binary_inputs(OUTSTATION, 0, 1)
    assert caught.value.meta["code"] == "response_limit"
    assert caught.value.meta["response_budget"]["fragments"] == 1
    assert caught.value.meta["unsolicited"][0]["confirmed"]
    assert outstation.connections == 2 and len(outstation.confirms()) == 1
