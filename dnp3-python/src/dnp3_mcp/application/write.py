from __future__ import annotations

from typing import Any

from dnp3_mcp.domain.model import NOT_SENT, Alias, OutputValue, OutstationAddress, PointIndex, err, ok


class WriteOperations:
    async def write_binary_output(
        self, outstation_address: OutstationAddress, index: PointIndex, value: OutputValue
    ) -> dict:
        """Operate a binary output with a DNP3 control relay output block (CROB, group 12)."""
        request_scope = self.cancel_scope()
        refused = self.writes_refused("write_binary_output")
        if refused:
            return refused

        async def call() -> dict[str, Any]:
            meta = await self.master.write_binary_output(outstation_address, index, value, request_scope=request_scope)
            return ok({"outstation": outstation_address, "index": index, "value": value}, meta)

        return await self.guarded("write_binary_output", call, write=True)

    async def write_point_by_alias(self, alias: Alias, value: OutputValue) -> dict:
        """Operate a binary_output point by its point-map alias."""
        refused = self.writes_refused("write_point_by_alias")
        if refused:
            return refused
        spec = self.resolve(alias, "write_point_by_alias", write=True)
        if isinstance(spec, dict):
            return spec
        if spec.type != "binary_output":
            return err(
                f"Alias '{alias}' is a {spec.type}, which cannot be written",
                {"tool": "write_point_by_alias", "alias": alias, **NOT_SENT},
            )
        return await self.write_binary_output(spec.outstation, spec.index, value)
