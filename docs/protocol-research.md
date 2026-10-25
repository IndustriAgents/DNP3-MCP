# DNP3 protocol research and capability assessment

Reviewed 2026-10-25 against dnp3py **0.6.0**, this repository, and OPCUA-MCP's
contract/execution reference. Support claims below are narrower than IEEE 1815.

## Primary evidence

| Source | Sections used | Implementation consequence |
|---|---|---|
| [DNP Users Group overview](https://www.dnp.org/About/Overview-of-DNP3-Protocol) | Protocol ownership, IEEE adoption, document access | The public overview does not replace IEEE 1815 or vendor profiles. Full membership/purchased normative documents were unavailable during this review. |
| [DNP3 primer, revision A](https://www.dnp.org/Portals/0/AboutUs/DNP3%20Primer%20Rev%20A.pdf) | Master/outstation model, classes, objects and event handling | Preserve point identity, quality and timestamps; distinguish static reads from event acknowledgement. |
| [dnp3py pinned release source](https://raw.githubusercontent.com/craigpnnl/dnp3py/7275b367dd8c48caf74f27d0e8269846f6cd622a/README.md) and [0.6.0 distribution](https://pypi.org/project/dnp3py/0.6.0/) | Features and conformance limits; installed master/handler/decoder modules | Reuse existing range requests and seven existing static decoders; preserve pinned CROB and CONFIRM workarounds. |
| [opendnp3 3.0 measurement types](https://dnp3.github.io/docs/cpp/3.0.0/annotated.html) | Binary/output, double-bit, analog, counter and frozen-counter types | Add independent-stack tests for targeted reads, including output readback. |

Accessed 2026-10-25. Group mapping and response decoding were also checked against
the pinned source and hand-assembled TCP response fixtures. A library's broader
feature list is not a claim that this MCP exposes those features.

DNP3 is used for SCADA master/outstation exchanges. A control response describes
protocol acceptance, not completion of a plant process. Direct output status
readback was missing: only a potentially large Class 0 poll exposed outputs.
`read_points` now reads bounded static ranges of binary inputs (g1), double-bit
inputs (g3), binary output status (g10), counters (g20), frozen counters (g21),
analog inputs (g30) and analog output status (g40). The outstation chooses the
variation. No unit or engineering scaling is inferred.

## Capability matrix

| Capability / tool | Status and contract | Evidence / remaining work |
|---|---|---|
| Input reads / `read_inputs` | Existing binary/analog/alias modes retained | Existing TCP, wire and stdio suites |
| Static range/readback / `read_points` | Implemented; seven point kinds, 1–1000 values, valid range required, missing indexes fail | `test_read_points.py`: native TCP, independent byte fixtures, output readback and non-finite analogs; extended Linux opendnp3 suite |
| Alias reads | Extended to all supported static types; output aliases can now be read | MCP routing and point-map tests |
| Binary command / `control_binary_output` | Existing latch ON/OFF, SBO or direct operate, single attempt, matching echo/status required | Existing CROB boundaries, mismatch, lost reply, cancellation and independent-stack tests |
| Class polls / `poll_class` | Existing Class 0 or 1/2/3, confirmed event preservation and incomplete decoding notices | Event/unsolicited fault fixtures and opendnp3 CI |
| Local inventory/status / `list_points`, `ping` | Typed outputs and backend capability inventory; no implicit network scan | Contract and installed-package smoke |
| Analog output commands, pulse/trip/close CROBs, time sync, freeze, restart, file transfer | Not exposed | Separate control design and native interoperability evidence are still needed; raw-service escape hatches are intentionally absent |
| Persistent unsolicited monitoring | Not exposed; unsolicited responses during calls are preserved | Needs a persistent session/event ownership design; polling is not a durable subscription |
| Serial, TLS, Secure Authentication | Not implemented by this MCP | Requires transport/authentication work and authorized integration infrastructure; TCP alone provides none of these guarantees |
| Physical-device conformance | Not performed | Requires authorized outstations and vendor profiles |

## Policy and compatibility changes

The existing static-read transport confirms unsolicited events received during a
call. Such confirmation can remove events from the outstation. Consequently
`read_inputs`, its aliases and `read_points` now advertise `readOnlyHint:false`
and explicit policy `read_only:true` denies them, just as it denies event polls.
To permit observations, use an operation/tool allowlist with `read_only:false`
while leaving `DNP3_WRITES_ENABLED=false` to block physical output controls.
This is a policy correction; the native control gate is unchanged.

`read_inputs` remains available. New workflows should use `read_points` when
reading output/counter/double-bit types. Non-finite IEEE analog values are JSON
strings `NaN`, `Infinity` or `-Infinity`, including confirmed event metadata;
quality flags are preserved. Unexpected exception payloads are redacted from
MCP errors and generic logs.

Each canonical tool now has a concrete output data schema. MCP and stdio tests
validate observed results; package smoke checks metadata parity and capabilities
in an isolated loopback configuration. Local macOS skips the opendnp3 module
because yadnp3 wheels are unavailable here; the existing required Linux CI job
runs it with `REQUIRE_OPENDNP3=1` and the new static-group case.

## Second review: response budgets (2026-10-25)

The [pinned dnp3py TCP runner](https://raw.githubusercontent.com/craigpnnl/dnp3py/7275b367dd8c48caf74f27d0e8269846f6cd622a/src/dnp3/master/tcp_runner.py)
provides native fragment/reassembly and solicited-burst limits, but our call log
also spans unsolicited traffic and retries. The adapter now bounds that entire
call to 128 retained fragments, 256 KiB of raw fragment data and 4096 decoded
points. Fragment admission precedes owned retention and parsing; point admission
precedes callback retention and CONFIRM. Upstream transport/parser allocations
remain outside this retained-state budget; it is not a total heap bound.

Exhaustion produces `meta.code: response_limit`, `incomplete:true`, budget details
and no retry of that failure. The overflowing fragment is not confirmed. Earlier
confirmed solicited events remain in `meta.confirmed_events`, and confirmed
unsolicited data remains in `meta.unsolicited`. Control errors preserve the sent
stage: SELECT exhaustion prevents OPERATE; exhaustion after DIRECT_OPERATE or
OPERATE remains unknown. The limits are visible through local ping connection
metadata. Wire regressions verify each budget, confirmation counts, preserved
event prefixes and control outcomes.

Prioritized remaining roadmap:

1. Add explicit timestamp validity and time-quality metadata for observations.
2. Expand independently verified static/event variation coverage before adding controls.
3. Design persistent unsolicited ownership and durable delivery before subscription tools.

Analog controls, Secure Authentication, TLS/serial and physical-device profiles
remain outside this implementation's supported scope.
