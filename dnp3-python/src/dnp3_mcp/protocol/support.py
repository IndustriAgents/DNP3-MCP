from __future__ import annotations

import anyio as anyio
from mcp.server.fastmcp import FastMCP as FastMCP
from mcp.types import ToolAnnotations as ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
LOCAL_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
CONSUMES_EVENTS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


def request_cancel_scope(server: FastMCP) -> anyio.CancelScope | None:
    """The cancel scope of the MCP request now being handled, or None outside one.

    RequestResponder.cancel() cancels this scope synchronously when the client
    sends notifications/cancelled, before anyio interrupts the task; the master
    checks it right before every send. This reads private mcp<2 attributes
    (`session._in_flight`, `RequestResponder._cancel_scope`), hence the getattr
    guards and tests/test_cancel_race.py, which fails if they move.
    """
    try:
        context = server._mcp_server.request_context
    except (LookupError, AttributeError):
        return None
    in_flight = getattr(context.session, "_in_flight", None)
    responder = in_flight.get(context.request_id) if isinstance(in_flight, dict) else None
    scope = getattr(responder, "_cancel_scope", None)
    return scope if isinstance(scope, anyio.CancelScope) else None
