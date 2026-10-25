"""Point map loading and validation (known issue 9)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dnp3_mcp.tools import PointMap, parse_point_spec


def write_map(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_valid_entries(tmp_path: Path) -> None:
    path = write_map(
        tmp_path / "points.json",
        {
            "breaker": {"outstation": 10, "type": "binary_input", "index": 0, "description": "Breaker closed"},
            "trip": {"outstation": 10, "type": "binary_output", "index": 3},
        },
    )
    points = PointMap(path)
    assert points.error is None
    assert points.count() == 2
    spec = points.get("trip")
    assert spec is not None and spec.error is None
    assert (spec.outstation, spec.type, spec.index) == (10, "binary_output", 3)


def test_malformed_entries_are_recorded_not_raised(tmp_path: Path) -> None:
    path = write_map(
        tmp_path / "points.json",
        {
            "words": {"outstation": "ten", "type": "binary_input", "index": 0},
            "no_index": {"outstation": 10, "type": "analog_input"},
            "bool_index": {"outstation": 10, "type": "analog_input", "index": True},
            "bad_type": {"outstation": 10, "type": "unsupported_type", "index": 1},
            "too_big": {"outstation": 70000, "type": "binary_output", "index": 1},
            "not_object": 5,
        },
    )
    points = PointMap(path)
    assert points.error is None
    listed = {entry["alias"]: entry for entry in points.list()}
    assert set(listed) == {"words", "no_index", "bool_index", "bad_type", "too_big", "not_object"}
    for alias, entry in listed.items():
        assert entry.get("error"), alias
    assert "'outstation' must be an integer" in listed["words"]["error"]
    assert "'index' must be an integer" in listed["no_index"]["error"]
    assert "'index' must be an integer" in listed["bool_index"]["error"]
    assert "'type' must be one of" in listed["bad_type"]["error"]
    assert "'outstation' must be 0-65519" in listed["too_big"]["error"]
    assert "must be a JSON object" in listed["not_object"]["error"]


def test_file_problems_are_reported(tmp_path: Path) -> None:
    missing = PointMap(tmp_path / "missing.json")
    assert missing.count() == 0 and missing.error and "cannot be read" in missing.error

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert "not valid JSON" in (PointMap(broken).error or "")

    array = write_map(tmp_path / "array.json", [1, 2])
    assert "must be a JSON object" in (PointMap(array).error or "")


def test_reloads_when_the_file_changes(tmp_path: Path) -> None:
    path = write_map(tmp_path / "points.json", {"a": {"outstation": 1, "type": "binary_input", "index": 0}})
    points = PointMap(path)
    assert points.count() == 1
    write_map(path, {"a": {"outstation": 1, "type": "binary_input", "index": 0}, "b": {"x": 1}})
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert points.count() == 2


def test_no_path_means_an_empty_map() -> None:
    points = PointMap(None)
    assert points.error is None and points.list() == []


def test_parse_point_spec_description_must_be_text() -> None:
    spec = parse_point_spec("x", {"outstation": 1, "type": "binary_input", "index": 0, "description": 5})
    assert spec.error == "'description' must be a string"
