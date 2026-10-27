"""
Ollama backend — uses Ollama's OpenAI-compatible /v1/chat/completions endpoint.

Requires a locally running Ollama instance and a model that supports tool calling,
e.g. qwen2.5, llama3.1, mistral-nemo.

Config (via .env):
    OLLAMA_BASE_URL  default http://localhost:11434
    OLLAMA_MODEL     default qwen2.5
"""

import json
import os
import uuid

import httpx

from .base import Backend, BackendResponse, ToolCall


class OllamaBackend(Backend):
    def __init__(self, model: str = None, base_url: str = None):
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model    = model or os.environ.get("OLLAMA_MODEL", "qwen2.5")

    def _to_openai_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name":        t["name"],
                    "description": t.get("description", ""),
                    "parameters":  t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
    ) -> BackendResponse:
        wire_messages = []
        if system:
            wire_messages.append({"role": "system", "content": system})
        wire_messages.extend(messages)

        payload = {
            "model":    self.model,
            "messages": wire_messages,
            "stream":   False,
        }
        if tools:
            payload["tools"] = self._to_openai_tools(tools)

        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{self.base_url}/v1/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()

        msg         = data["choices"][0]["message"]
        text        = msg.get("content") or ""
        raw_calls   = msg.get("tool_calls") or []
        stop_reason = "tool_use" if raw_calls else "end_turn"

        tool_calls = [
            ToolCall(
                id=tc.get("id") or str(uuid.uuid4()),
                name=tc["function"]["name"],
                args=json.loads(tc["function"]["arguments"] or "{}"),
            )
            for tc in raw_calls
        ]
        return BackendResponse(text=text, tool_calls=tool_calls, stop_reason=stop_reason)

    def append_assistant(self, messages: list[dict], response: BackendResponse) -> None:
        msg: dict = {"role": "assistant", "content": response.text or ""}
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id":   tc.id,
                    "type": "function",
                    "function": {
                        "name":      tc.name,
                        "arguments": json.dumps(tc.args),
                    },
                }
                for tc in response.tool_calls
            ]
        messages.append(msg)

    def append_tool_result(self, messages: list[dict], call: ToolCall, result: str) -> None:
        messages.append({
            "role":         "tool",
            "tool_call_id": call.id,
            "name":         call.name,
            "content":      result,
        })
