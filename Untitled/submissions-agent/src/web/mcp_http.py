"""
MCP HTTP/SSE transport — mounts the MCP server onto the FastAPI app.

Activated when DEPLOY=true. Endpoints:
  GET  /mcp/sse        — SSE stream; MCP clients connect here
  POST /mcp/messages/  — MCP client message inbox

Auth is handled by the existing Bearer-token middleware in app.py.
"""

from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from ..server import app as _mcp_server

_transport = SseServerTransport("/mcp/messages/")


async def _sse_handler(scope, receive, send) -> None:
    async with _transport.connect_sse(scope, receive, send) as streams:
        read_stream, write_stream = streams
        await _mcp_server.run(
            read_stream,
            write_stream,
            _mcp_server.create_initialization_options(),
        )


def create_mcp_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/sse", _sse_handler),
            Mount("/messages/", app=_transport.handle_post_message),
        ]
    )
