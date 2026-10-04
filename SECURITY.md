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
  `DNP3_PORT` and returns what the outstation sent.
- **Event polls are destructive.** A Class 1-3 poll confirms each response
  fragment, and the outstation then deletes those events, so another master on
  the same association never sees them. The tool is annotated as destructive.
  A poll is not retried once any fragment was confirmed, and if it fails after
  that, the confirmed events are returned in `meta.confirmed_events`. Only a
  fragment the library cannot fully decode is left unconfirmed.
- **Unsolicited data is confirmed.** The server does not enable unsolicited
  reporting, but any unsolicited response the outstation sends during a call is
  confirmed (removing those events from the outstation) unless it cannot be
  fully decoded, and is returned in `meta.unsolicited` with a `confirmed` flag.
  It does not send DISABLE_UNSOLICITED or anything else that changes the
  outstation's configuration.
- **Controls are off by default.** `DNP3_WRITES_ENABLED` defaults to `false`,
  and `write_binary_output` / `write_point_by_alias` are refused until it is
  set to `true`. Only recognised true/false spellings are accepted; anything
  else stops the server at startup, so a typo cannot switch writes on. The
  switch is read only from the real process environment or an explicit
  `--env-file`, never from the implicit `.env`, in any spelling (names are
  compared case-insensitively, with or without `export `).
- **The implicit `.env` is narrow.** It is read only from `dnp3-python/` of a
  source checkout, found next to the server's own source; never from the
  working directory or a parent, because an MCP client may start the server in
  its own project, which may not be trusted, and the host and point map decide
  where a control goes. An installed package has no implicit `.env`. The file
  may set only `DNP3_*` settings and `POINT_MAP_FILE`; the write switch and
  anything else (`PATH`, `PYTHON*`, `DYLD_*`, `LD_PRELOAD`, proxy or
  certificate settings) are ignored with a warning on stderr.
- **Controls are real when enabled.** A control sends a CROB (LATCH_ON or
  LATCH_OFF) using select-before-operate unless `DNP3_CONTROL_MODE=direct_operate`.
  It is reported as successful only when the outstation echoes the control
  unchanged with status `SUCCESS` and no IIN error. If the link fails, times
  out or answers garbage after the OPERATE (or DIRECT_OPERATE) was sent, or
  answers `SUCCESS` with an IIN error, the call reports `meta.outcome:
  "unknown"` and says the control may have been applied. Only a refusal status
  in the echo counts as `rejected`. Controls are never retried automatically,
  a SELECT that is not answered correctly is never followed by an OPERATE, and
  every call has an overall deadline that starts when the call is made and
  covers waiting behind earlier calls, so none can hang without an outcome,
  and a control whose deadline passes while it waits is reported `not_sent`
  and never sent afterwards (an MCP client that times out sends no
  cancellation, so this deadline is what stops it).
- **A cancelled or abandoned control stays unsent.** If the MCP client cancels
  a control, or disconnects (closes stdin), while it is queued, connecting or
  between SELECT and OPERATE, no OPERATE or DIRECT_OPERATE is sent afterwards:
  the server checks for both immediately before every control request, and on
  a disconnect it stops every call in flight and logs each abandoned control's
  outcome on stderr.
- **No authentication or encryption.** DNP3 Secure Authentication and TLS are
  not implemented. Serial links are not supported.
- **The protocol stack is young.** DNP3 is handled by dnp3py, a 0.x beta from a
  single maintainer, pinned to one release. The server encodes controls itself
  and withholds confirmations for partly decoded event data because of known
  dnp3py defects, and CI checks it against opendnp3. See
  [About dnp3py](README.md#about-dnp3py).

## Running it safely

- **Start against the mock.** `dnp3-mock-outstation/` exists so that an agent
  can be exercised end to end without touching real plant. Do that first,
  every time.
- **Keep it read-only until you mean otherwise.** Control relay outputs open
  breakers, start pumps and move valves. There is no undo, and a language
  model will call a tool it has been given. Leave `DNP3_WRITES_ENABLED` unset
  (or `false`) and both write tools are refused. Set it only in the MCP
  client's `env` block or an explicit `--env-file`.
- **Give it an association of its own.** Configure the outstation with a
  dedicated master address and association for this server, and set
  `DNP3_MASTER_ADDRESS` to it. Never reuse the production SCADA master's
  address: the default, `1`, is a common production value (it is kept as the
  default because it is what the mock and most test outstations expect, not
  because it is safe on a live system). Sharing an address lets this server
  consume the production master's events and confuse its sequence numbers.
- **Expect connection contention.** The server opens one TCP connection per
  call. An outstation that allows a single master connection may refuse it, or
  drop the connection the production master already has. Check how your
  device behaves before connecting.
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
