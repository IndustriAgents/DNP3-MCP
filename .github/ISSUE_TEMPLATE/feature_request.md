---
name: Feature request
about: Suggest a new tool or an improvement to the DNP3 server or mock
title: "[Feature] "
labels: enhancement
assignees: ""
---

**What problem does this solve?**
The use case, from the point of view of someone asking an agent to do something.

**Which component?**
- [ ] MCP server (`dnp3-python`)
- [ ] Mock outstation (`dnp3-mock-outstation`)

**Proposed solution**
If it is a new tool: its name, arguments, and what it returns. Tool names and
arguments are kept consistent across the IndustriConnect servers, so say how it
lines up with the equivalent tool in the other protocol servers.

**Does it write to a device?**
- [ ] No — read-only
- [ ] Yes — it operates a control or otherwise changes the outstation

**Can the mock answer it?**
What the mock outstation would need to be able to simulate for this to be
testable without real plant.

**Additional context**
Links to the relevant IEEE 1815 (DNP3) sections, example devices, or related
issues.
