from __future__ import annotations

from typing import Any

from dnp3_mcp.domain.model import err, ok


class MetadataOperations:
    async def list_points(self) -> dict:
        """List the aliases defined in the point map (POINT_MAP_FILE)."""
        points = self.point_map.list()
        meta: dict[str, Any] = {"path": str(self.point_map.path) if self.point_map.path else None}
        if self.point_map.error:
            return err(self.point_map.error, meta)
        return ok({"points": points, "count": len(points)}, meta)
