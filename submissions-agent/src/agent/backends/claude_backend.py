"""
Claude backend — uses the Anthropic SDK.

Config (via .env):
    ANTHROPIC_API_KEY  required
    CLAUDE_MODEL       default claude-opus-5-5
"""

import json
import os

import anthropic

from .base import Backend, BackendResponse, ToolCall


class ClaudeBackend(Backend):
    def __init__(self, model: str = None):
        self.model  = model or os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
        self.client = anthropic.AsyncAnthropic()

    def _to_anthropic_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "name":         t["name"],
                "description":  t.get("description", ""),
                "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
    ) -> BackendResponse:
        kwargs: dict = {
            "model":      self.model,
            "max_tokens": 4096,
            "messages":   messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        resp = await self.client.messages.create(**kwargs)

        text       = ""
        tool_calls = []

        for block in resp.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, args=block.input))

        stop_reason = "tool_use" if tool_calls else "end_turn"
        return BackendResponse(text=text, tool_calls=tool_calls, stop_reason=stop_reason)

    def append_assistant(self, messages: list[dict], response: BackendResponse) -> None:
        content = []
        if response.text:
            content.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            content.append({
                "type":  "tool_use",
                "id":    tc.id,
                "name":  tc.name,
                "input": tc.args,
            })
        messages.append({"role": "assistant", "content": content})

    def append_tool_result(self, messages: list[dict], call: ToolCall, result: str) -> None:
        # Anthropic requires tool results in a user message
        if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
            messages[-1]["content"].append({
                "type":        "tool_result",
                "tool_use_id": call.id,
                "content":     result,
            })
        else:
            messages.append({
                "role": "user",
                "content": [{
                    "type":        "tool_result",
                    "tool_use_id": call.id,
                    "content":     result,
                }],
            })
