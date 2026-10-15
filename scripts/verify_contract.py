"""Reject drift between the reviewed contract and its wheel resource."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = json.loads((ROOT / "contract/tools.json").read_text())
bundled = ROOT / "dnp3-python/src/dnp3_mcp/_contract/tools.json"
assert source == json.loads(bundled.read_text()), (
    "Update the packaged contract from contract/tools.json"
)

import os

import tomllib

package = json.loads((ROOT / "npm/package.json").read_text())
project = tomllib.loads((ROOT / "dnp3-python/pyproject.toml").read_text())["project"]
assert package["name"] == project["name"]
assert package["version"] == project["version"]
ref = os.environ.get("GITHUB_REF", "")
if ref.startswith("refs/tags/"):
    assert ref == "refs/tags/v" + project["version"], (
        "Release tag must match both package versions"
    )
