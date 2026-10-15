from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, StrictBool, StrictInt

from dnp3_mcp.config import MAX_LINK_ADDRESS
from dnp3_mcp.domain.errors import MAX_POINT_INDEX, MAX_READ_COUNT

logger = logging.getLogger(__name__)
POINT_TYPES = ("binary_input", "analog_input", "binary_output")


def envelope(
    success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


def ok(data: Any = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return envelope(True, data=data, meta=meta)


def err(message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return envelope(False, error=message, meta=meta)


OutstationAddress = Annotated[
    StrictInt,
    Field(ge=0, le=MAX_LINK_ADDRESS, description=f"DNP3 link address of the outstation (0-{MAX_LINK_ADDRESS})."),
]
StartIndex = Annotated[StrictInt, Field(ge=0, le=MAX_POINT_INDEX, description="First point index to read.")]
PointIndex = Annotated[StrictInt, Field(ge=0, le=MAX_POINT_INDEX, description="Point index of the binary output.")]
Count = Annotated[
    StrictInt, Field(ge=1, le=MAX_READ_COUNT, description=f"Number of consecutive points to read (1-{MAX_READ_COUNT}).")
]
EventClass = Annotated[
    StrictInt, Field(ge=0, le=3, description="0 reads all static data (Class 0); 1, 2 or 3 reads that event class.")
]


def _output_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError('expected a JSON boolean, or the string "true" or "false"')


OutputValue = Annotated[
    StrictBool,
    Field(
        description=(
            "true latches the output ON, false latches it "
            'OFF. Pass a JSON boolean; the strings "true" and'
            ' "false" are also accepted. Anything else is '
            "rejected."
        )
    ),
    BeforeValidator(_output_value),
]
Alias = Annotated[str, Field(min_length=1, max_length=256, description="Alias defined in the point map.")]


@dataclass(frozen=True, slots=True)
class PointSpec:
    alias: str
    type: str | None
    outstation: int | None
    index: int | None
    description: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "alias": self.alias,
            "outstation": self.outstation,
            "type": self.type,
            "index": self.index,
            "description": self.description,
        }
        if self.error:
            out["error"] = self.error
        return out


def _int_field(spec: dict[str, Any], key: str, maximum: int) -> tuple[int | None, str | None]:
    raw = spec.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return (None, f"'{key}' must be an integer, got {raw!r}")
    if not 0 <= raw <= maximum:
        return (None, f"'{key}' must be 0-{maximum}, got {raw}")
    return (raw, None)


def parse_point_spec(alias: str, spec: Any) -> PointSpec:
    """Validate one point-map entry. Problems are recorded, not raised."""
    if not isinstance(spec, dict):
        return PointSpec(alias, None, None, None, None, f"entry must be a JSON object, got {type(spec).__name__}")
    problems: list[str] = []
    p_type = spec.get("type")
    if p_type not in POINT_TYPES:
        problems.append(f"'type' must be one of {', '.join(POINT_TYPES)}, got {p_type!r}")
    outstation, problem = _int_field(spec, "outstation", MAX_LINK_ADDRESS)
    if problem:
        problems.append(problem)
    index, problem = _int_field(spec, "index", MAX_POINT_INDEX)
    if problem:
        problems.append(problem)
    description = spec.get("description")
    if description is not None and (not isinstance(description, str)):
        problems.append("'description' must be a string")
        description = None
    return PointSpec(
        alias=alias,
        type=p_type if isinstance(p_type, str) else None,
        outstation=outstation,
        index=index,
        description=description,
        error="; ".join(problems) or None,
    )


NOT_SENT = {"outcome": "not_sent", "request_sent": False}
