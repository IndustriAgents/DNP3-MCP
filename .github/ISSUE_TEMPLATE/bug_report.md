---
name: Bug report
about: Report a problem with the DNP3 MCP server or the mock outstation
title: "[Bug] "
labels: bug
assignees: ""
---

**Which component?**
- [ ] MCP server (`dnp3-python`, `dnp3-mcp`)
- [ ] Mock outstation (`dnp3-mock-outstation`)

**Describe the bug**
What happens, and what you expected instead.

**To reproduce**
The MCP client you used (Claude Desktop / Claude Code / Cursor / Inspector /
mcp-manager-ui), the tool you called, and the arguments you passed.

1.
2.
3.

**Tool output**
```json
paste the { success, data, error, meta } envelope, or the error
```

**What was on the other end?**
- [ ] The mock outstation from this repo
- [ ] Real equipment — vendor and model:

**Environment**
- OS:
- Python version:
- Commit SHA:
- Run from: this repo / the IndustriConnect suite (`DNP3-Project` submodule)

**Additional context**
Outstation address, point types and indexes, `DNP3_*` settings (no secrets),
your point map, stderr logs — whatever would let someone else reproduce it
against the mock.
