from __future__ import annotations

from typing import Any

from dnp3_mcp.domain.capabilities import capabilities
from dnp3_mcp.domain.errors import DNP3MasterError
from dnp3_mcp.domain.model import EventClass, OutstationAddress, err, logger, ok


class DiagnosticsOperations:
    async def guarded(self, tool: str, call: Any, *, write: bool = False) -> dict[str, Any]:
        try:
            return await call()
        except DNP3MasterError as exc:
            meta = {"tool": tool, **exc.meta}
            if write and "outcome" not in meta:
                meta.update(outcome="unknown", request_sent=True)
            return err(str(exc), meta)
        except Exception as exc:
            logger.error("Unexpected error in %s (%s)", tool, type(exc).__name__)
            meta = {"tool": tool}
            message = "Unexpected error while executing tool"
            if write:
                meta.update(outcome="unknown", request_sent=True)
                message += ". The control may have been applied; read the output's state back before retrying."
            return err(message, meta)

    async def poll_class(self, outstation_address: OutstationAddress, event_class: EventClass) -> dict:
        """Poll an outstation for Class 0 static data or Class 1/2/3 events."""
        request_scope = self.cancel_scope()

        async def call() -> dict[str, Any]:
            data, meta = await self.master.poll_class(outstation_address, event_class, request_scope=request_scope)
            return ok(data, meta)

        return await self.guarded("poll_class", call)

    async def ping(self) -> dict:
        """Report the server's configuration: connection settings, write switch and point map."""
        return ok(
            {
                "connection": self.master.connection_status(),
                "capabilities": capabilities(self.master.config.writes_enabled),
                "writes_enabled": self.master.config.writes_enabled,
                "point_aliases": self.point_map.count(),
                "point_map": {
                    "path": str(self.point_map.path) if self.point_map.path else None,
                    "error": self.point_map.error,
                },
                "server_version": self.resources.server_version,
                "dnp3_library": f"dnp3py {self.resources.library_version}",
            }
        )
