from __future__ import annotations

from typing import Any

from dnp3_mcp.domain.model import NOT_SENT, PointSpec, err


class CommonOperations:
    def __init__(self, resources, cancel_scope):
        self.resources = resources
        self.cancel_scope = cancel_scope
        self.master = self.resources.master
        self.point_map = self.resources.point_map

    def writes_refused(self, tool: str) -> dict[str, Any] | None:
        if not self.master.config.writes_enabled:
            return err(
                "Write operations are disabled. Set DNP3_WRITES_ENABLED=true to allow controls.",
                {"tool": tool, "writes_enabled": False, **NOT_SENT},
            )
        return None

    def resolve(self, alias: str, tool: str, *, write: bool = False) -> PointSpec | dict[str, Any]:
        extra = dict(NOT_SENT) if write else {}
        spec = self.point_map.get(alias)
        if spec is None:
            if self.point_map.error:
                return err(f"Unknown alias '{alias}'. {self.point_map.error}", {"tool": tool, **extra})
            return err(f"Unknown alias '{alias}'", {"tool": tool, **extra})
        if spec.error:
            return err(f"Point map entry '{alias}' is invalid: {spec.error}", {"tool": tool, "alias": alias, **extra})
        return spec
