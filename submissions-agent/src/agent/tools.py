"""
Tool definitions and executor for the agent runner.

Reuses the same tool schemas registered in server.py and calls the same
dispatch function, so the agent and the MCP server always stay in sync.
"""

import asyncio
import json

import mcp.types as types

from ..server import handle_list_tools, _dispatch


async def get_tool_schemas() -> list[dict]:
    """Return tool definitions in the flat dict format backends expect."""
    mcp_tools: list[types.Tool] = await handle_list_tools()
    return [
        {
            "name":        t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in mcp_tools
    ]


async def call_tool(name: str, args: dict) -> str:
    """Execute a tool by name and return its result as a JSON string."""
    try:
        result = await _dispatch(name, args)
    except Exception as exc:
        result = {"error": str(exc)}
    return json.dumps(result, indent=2, default=str)
