# 0.3.0 local release verification

Verified on 2026-10-24. Package: `industriagents-dnp3-mcp`.

These are local development builds from the current working tree, using the
existing 0.3.0 package manifests. Nothing was published to a registry.

## Checks

- Full Python regression run: 265 passed, 1 skipped.
- DNP3 mock outstation regression suite: 14 passed.
- Ruff lint and formatting checks passed for source and tests, including mock sources.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported the manifest version and listed 5 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- Workspace artifact verification matched every wheel Python source to the
  current checkout and the packaged contract to its reviewed copy. The npm
  tarball contains exactly the same wheel bytes as the Python artifact.
- `git diff --check` passed.

Optional OpenDNP3 interoperability test skipped because its dependency is unavailable on this host.

New regressions cover request depth/size/JSON bounds before dispatch, unknown
arguments on legacy/direct routes, cancellation cleanup, audit-result failures
for structured and text-only results, and runtime/validation exceptions after
handler dispatch. The latter preserve unknown control outcomes without retries.
Application/domain imports are checked against native driver and MCP dependencies.

The follow-up review added two same-step cancellation cases for static reads
and event polls. Both prove that a cancellation delivered when connection
establishment finishes cannot invoke the native request, and the session is
closed. The final suite also includes nine regular-file audit and bounded-policy
loading checks.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_dnp3_mcp-0.3.0-py3-none-any.whl` | `2b5ddd5df93cd11b84521a998faed9350a621e011ab292384c45519c4cee25e7` |
| `industriagents_dnp3_mcp-0.3.0.tar.gz` | `15f0644dd9afaebc7fc5cb8233da4f9935fed133128b73a78039f5cb7e6ff2be` |
| `industriagents-dnp3-mcp-0.3.0.tgz` | `f4490e008bd3c1174deccbdd019430fbe8a813e371f1e453ec9f1ed913462ed4` |

See [release instructions](release.md) for independent registry publication.
