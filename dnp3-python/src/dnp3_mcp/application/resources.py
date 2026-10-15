from __future__ import annotations

from dataclasses import dataclass

from dnp3_mcp.domain.ports import DNP3MasterPort
from dnp3_mcp.infrastructure.maps import PointMap


@dataclass(slots=True)
class ToolResources:
    master: DNP3MasterPort
    point_map: PointMap
    server_version: str = "unknown"
    library_version: str = "unknown"
