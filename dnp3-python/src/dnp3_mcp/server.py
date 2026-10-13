"""FastMCP wiring for the DNP3 implementation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import ContentBlock
from pydantic import ValidationError

from .config import ServerConfig
from .dnp3_master import DNP3Master
from .tools import PointMap, ToolResources, err, register_tools

logger = logging.getLogger(__name__)

# Errors from writing a reply to a client that has gone.
_UNDELIVERABLE = (anyio.ClosedResourceError, anyio.BrokenResourceError, BrokenPipeError, ConnectionResetError)


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


__version__ = _package_version("dnp3-mcp")

WRITE_TOOLS = frozenset({"write_binary_output", "write_point_by_alias"})

INSTRUCTIONS = (
    "Tools for a DNP3 (IEEE 1815) master over TCP. Every tool returns {success, data, error, meta}. "
    "Controls are refused unless DNP3_WRITES_ENABLED=true."
)


def describe_validation_error(tool: str, exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        problems.append(f"{where}: {error.get('msg', 'invalid')}")
    return f"Invalid arguments for {tool}: " + "; ".join(problems)


class EnvelopeFastMCP(FastMCP):
    """FastMCP that reports argument validation errors in the response envelope.

    FastMCP validates arguments against the tool signature before the tool
    runs, and by default answers a bad argument with a bare protocol error.
    Here it becomes a normal {success: false} envelope like any other failure.
    """

    # Called once when the client disconnects (stdin reaches end of file).
    on_client_gone: Callable[[], None] | None = None
    # True once the client's stdin has reached end of file.
    client_gone: bool = False

    async def run_stdio_async(self) -> None:
        """Run over stdio, and stop pending DNP3 requests as soon as the client disconnects.

        FastMCP stops reading at end of file but lets tool calls in flight run
        to completion, so a control could still go out after the client is
        gone. The input is relayed through a stream of our own so that end of
        file is seen here first and `on_client_gone` can stop them.
        """
        self.client_gone = False
        try:
            async with stdio_server() as (read_stream, write_stream):
                relay_send, relay_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self._relay_until_eof, read_stream, relay_send)
                    await self._mcp_server.run(
                        relay_receive,
                        write_stream,
                        self._mcp_server.create_initialization_options(),
                    )
                    tasks.cancel_scope.cancel()
        except BaseExceptionGroup as group:
            # Replies to calls that were still running cannot be delivered once
            # the client is gone; that is a normal end, not a crash.
            _, rest = group.split(_UNDELIVERABLE)
            if not self.client_gone or rest is not None:
                raise
            logger.info("Dropped replies for the disconnected client")

    async def _relay_until_eof(self, source: Any, sink: Any) -> None:
        try:
            async with sink:
                async for message in source:
                    await sink.send(message)
        finally:
            self.client_gone = True
            if self.on_client_gone is not None:
                self.on_client_gone()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                meta: dict[str, Any] = {"tool": name}
                if name in WRITE_TOOLS:
                    meta.update(outcome="not_sent", request_sent=False)
                return err(describe_validation_error(name, exc.__cause__), meta)
            raise


class DNP3MCPServer:
    def __init__(self, config: ServerConfig | None = None, *, master: DNP3Master | None = None) -> None:
        # Read the environment here, not at import time, so .env values loaded
        # by cli.main() apply.
        self.config = config or ServerConfig.from_env()
        self.master = master or DNP3Master(self.config.dnp3)
        self._server = EnvelopeFastMCP(name="DNP3 MCP Server", instructions=INSTRUCTIONS)
        self._server.on_client_gone = lambda: self.master.shutdown("the MCP client disconnected (stdin closed)")
        # FastMCP has no version argument and would report the mcp SDK's own
        # version as serverInfo.version.
        self._server._mcp_server.version = __version__
        self.point_map = PointMap(self.config.point_map_path)
        register_tools(
            self._server,
            ToolResources(
                master=self.master,
                point_map=self.point_map,
                server_version=__version__,
                library_version=_package_version("dnp3py"),
            ),
        )

    @property
    def mcp(self) -> FastMCP:
        return self._server

    def run(self) -> None:
        self._server.run()
