# Security Policy

## What this server is, in security terms

DNP3 MCP is a bridge between a language model and a DNP3 outstation. It speaks
MCP over stdio to its client and is meant to speak DNP3 to an outstation, which
puts the trust boundary *inside* the server: whatever the model decides to call,
the outstation is asked to do.

DNP3 without Secure Authentication carries no authentication or encryption of
its own. Anyone who can reach the link can read points and operate controls.
This server does not implement DNP3 Secure Authentication.

So treat it as equipment on the control network, not as an application on the
office network.

## What the code does today

The DNP3 master in `dnp3-python/` is still a scaffold (see
[Status](README.md#status)). Two consequences matter for security:

- **Reads are synthetic.** No connection is opened to `DNP3_HOST` /
  `DNP3_PORT`, and the values returned are generated, not read from an
  outstation. Nothing an agent sees today reflects a real device.
- **Writes are enabled by default and report success without sending
  anything.** `DNP3_WRITES_ENABLED` defaults to `true`, and
  `write_binary_output` / `write_point_by_alias` return `success: true`
  although no control (CROB) is sent. Set `DNP3_WRITES_ENABLED=false` unless
  you are deliberately working on the write path.

The guidance below is written for the day the master talks to real equipment.

## Running it safely

- **Start against the mock.** `dnp3-mock-outstation/` exists so that an agent
  can be exercised end to end without touching real plant. Do that first,
  every time.
- **Keep it read-only until you mean otherwise.** Control relay outputs open
  breakers, start pumps and move valves. There is no undo, and a language
  model will call a tool it has been given. `DNP3_WRITES_ENABLED=false`
  refuses both write tools.
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
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite as
the `DNP3-Project` submodule, which is this repository.

Out of scope: vulnerabilities in the DNP3 protocol itself (the lack of
authentication without Secure Authentication is a property of DNP3), in
third-party libraries (report those upstream), and in vendor outstation
firmware. Issues in `mcp-manager-ui` or the suite belong to
[IndustriConnect's security policy](https://github.com/IndustriAgents/IndustriConnect/security/policy);
the other protocol servers each have their own repository and SECURITY.md.
