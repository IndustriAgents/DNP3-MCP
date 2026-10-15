# 0.3.0 local release verification

Verified on 2026-10-15. Package: `industriagents-dnp3-mcp`.

This records local build verification, not registry publication. Check the
GitHub Actions runs and registry version pages for publication status. Registry
authentication/trusted publishers must be configured before release; GitHub
repository access alone does not authorize registry publication.

## Checks

- Full Python regression run: 238 passed, 1 skipped.
- Final architecture/contract tests: 15 passed (a separate run, not additional unique tests).
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported version 0.3.0 and listed 5 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- The npm tarball contains exactly the same wheel bytes as the PyPI artifact.
- `git diff --check` passed.

Skipped test: Optional opendnp3 interoperability dependency unavailable.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_dnp3_mcp-0.3.0-py3-none-any.whl` | `06e501cd962defc53c577f3baab2dcfff8076b137f80ca156df54bad9aaed5d9` |
| `industriagents_dnp3_mcp-0.3.0.tar.gz` | `d95d4cab2959659b62ceedc07534e6ebc3bd456da4ac0da90082b509e394d9b9` |
| `industriagents-dnp3-mcp-0.3.0.tgz` | `f36d17f871b1a91c36952dd61e3a80498a8d94ef9cbb23aeaa64654dffabeead` |

See [release instructions](release.md) for independent registry publication.
