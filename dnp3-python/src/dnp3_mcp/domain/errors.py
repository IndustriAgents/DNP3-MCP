from __future__ import annotations

from typing import Any

MAX_POINT_INDEX = 65535
MAX_READ_COUNT = 1000


class DNP3MasterError(RuntimeError):
    """A DNP3 operation did not complete. `meta` carries diagnostics."""

    def __init__(self, message: str, meta: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.meta: dict[str, Any] = meta or {}
