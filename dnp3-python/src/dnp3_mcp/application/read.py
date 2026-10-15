from __future__ import annotations

from typing import Any

from dnp3_mcp.domain.model import Alias, Count, OutstationAddress, StartIndex, err, ok


class ReadOperations:
    async def read_binary_inputs(
        self, outstation_address: OutstationAddress, start_index: StartIndex, count: Count
    ) -> dict:
        """Read binary inputs (DNP3 group 1) from an outstation."""
        request_scope = self.cancel_scope()

        async def call() -> dict[str, Any]:
            points, meta = await self.master.read_binary_inputs(
                outstation_address, start_index, count, request_scope=request_scope
            )
            return ok({"outstation": outstation_address, "points": points}, meta)

        return await self.guarded("read_binary_inputs", call)

    async def read_analog_inputs(
        self, outstation_address: OutstationAddress, start_index: StartIndex, count: Count
    ) -> dict:
        """Read analog inputs (DNP3 group 30) from an outstation."""
        request_scope = self.cancel_scope()

        async def call() -> dict[str, Any]:
            points, meta = await self.master.read_analog_inputs(
                outstation_address, start_index, count, request_scope=request_scope
            )
            return ok({"outstation": outstation_address, "points": points}, meta)

        return await self.guarded("read_analog_inputs", call)

    async def read_point_by_alias(self, alias: Alias) -> dict:
        """Read one binary_input or analog_input point by its point-map alias."""
        spec = self.resolve(alias, "read_point_by_alias")
        if isinstance(spec, dict):
            return spec
        if spec.type == "binary_input":
            return await self.read_binary_inputs(spec.outstation, spec.index, 1)
        if spec.type == "analog_input":
            return await self.read_analog_inputs(spec.outstation, spec.index, 1)
        return err(
            (
                "Alias '"
                f"{alias}"
                "' is a "
                f"{spec.type}"
                "; reading it by alias is not supported. Use "
                "poll_class with event_class 0 to see output "
                "status."
            ),
            {"tool": "read_point_by_alias", "alias": alias},
        )
