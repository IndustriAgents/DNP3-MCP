from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dnp3_mcp.domain.model import PointSpec, logger, parse_point_spec


class PointMap:
    """Alias -> point definitions from a JSON file, re-read when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._points: dict[str, PointSpec] = {}
        self._stamp: tuple[int, int] | None = None
        self.error: str | None = None
        self.refresh()
        if self.error:
            logger.warning("%s", self.error)

    def refresh(self) -> None:
        if self.path is None:
            self._points, self._stamp, self.error = ({}, None, None)
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._points, self._stamp = ({}, None)
            self.error = f"Point map {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._points = {}
            self.error = f"Point map {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._points = {}
            self.error = f"Point map {self.path} must be a JSON object mapping aliases to points"
            return
        self._points = {str(alias): parse_point_spec(str(alias), spec) for alias, spec in data.items()}
        self.error = None

    def get(self, alias: str) -> PointSpec | None:
        self.refresh()
        return self._points.get(alias)

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        return [spec.as_dict() for spec in self._points.values()]

    def count(self) -> int:
        self.refresh()
        return len(self._points)
