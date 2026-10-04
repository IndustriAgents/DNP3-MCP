# Security Policy

## What this server is, in security terms

DNP3 MCP is a bridge between a language model and a DNP3 outstation. It speaks
MCP over stdio to its client and DNP3 over TCP to an outstation, which puts the
trust boundary *inside* the server: whatever the model decides to call, the
outstation is asked to do.

DNP3 without Secure Authentication carries no authentication or encryption of
its own. Anyone who can reach the link can read points and operate controls.
This server does not implement DNP3 Secure Authentication.

So treat it as equipment on the control network, not as an application on the
office network.

## What the code does today

The server in `dnp3-python/` is a DNP3 master over TCP (built on the
pure-Python dnp3py stack). What that means for security:

- **Reads are real.** Each read or poll opens a TCP connection to `DNP3_HOST` /
  `DNP3_PORT` and returns what the outstation sent. Class 1-3 polls confirm
  events, which removes them from the outstation's buffer.
- **Controls are off by default.** `DNP3_WRITES_ENABLED` defaults to `false`,
  and `write_binary_output` / `write_point_by_alias` are refused until it is
  set to `true`. Only recognised true/false spellings are accepted; anything
  else stops the server at startup, so a typo cannot switch writes on.
- **Controls are real when enabled.** A control sends a CROB (LATCH_ON or
  LATCH_OFF) using select-before-operate unless `DNP3_CONTROL_MODE=direct_operate`.
  It is reported as successful only when the outstation answers with status
  `SUCCESS`. If the link fails or times out after the OPERATE (or
  DIRECT_OPERATE) was sent, the error says the output may or may not have
  changed. Controls are never retried automatically.
- **No authentication or encryption.** DNP3 Secure Authentication and TLS are
  not implemented. Serial links are not supported.

## Running it safely

- **Start against the mock.** `dnp3-mock-outstation/` exists so that an agent
  can be exercised end to end without touching real plant. Do that first,
  every time.
- **Keep it read-only until you mean otherwise.** Control relay outputs open
  breakers, start pumps and move valves. There is no undo, and a language
  model will call a tool it has been given. Leave `DNP3_WRITES_ENABLED` unset
  (or `false`) and both write tools are refused.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run beside the MCP client, not a network service.
- **Segment the network.** The server should sit where a SCADA engineering
  workstation would sit, behind whatever separates your control network from
  everything else.
- **Never point it at a safety system or protection relay.** Safety-instrumented
  and protection functions are out of scope for anything in this repository.

## Reporting a vulnerability

Please report privately, through
[GitHub private vulnerability reporting](https://github.com/IndustriAgents/DNP3-MCP/security/advisories/new),
or by email to hi@industriagents.com.

Please do not open a public issue for a vulnerability.

Include the component affected (`dnp3-python` or `dnp3-mock-outstation`), the
version or commit, what an attacker would gain, and a reproduction if you have
one. We will acknowledge within a week and keep you updated as we work on a
fix.

## Scope

In scope: the MCP server in `dnp3-python/` and the mock outstation in
`dnp3-mock-outstation/`. That includes the copy shipped inside the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite in
its `DNP3-Project/` directory.

Out of scope: vulnerabilities in the DNP3 protocol itself (the lack of
authentication without Secure Authentication is a property of DNP3), in
third-party libraries (report those upstream), and in vendor outstation
firmware. Issues in `mcp-manager-ui`, the suite or another protocol server
belong to
[IndustriConnect's security policy](https://github.com/IndustriAgents/IndustriConnect/security/policy),
or to that server's own repository once it has been split out like this one.
