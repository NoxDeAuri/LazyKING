from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class BackendResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # "tool_use" | "end_turn" | "stop"


class Backend(ABC):
    """
    Common interface for LLM backends.

    Tools are passed in MCP inputSchema format:
        [{"name": ..., "description": ..., "inputSchema": {...}}, ...]

    Each backend converts to its native wire format internally.
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
    ) -> BackendResponse:
        """Send messages and return the model's response."""
        ...

    @abstractmethod
    def append_assistant(self, messages: list[dict], response: BackendResponse) -> None:
        """Append the assistant's response to the message list in-place."""
        ...

    @abstractmethod
    def append_tool_result(
        self,
        messages: list[dict],
        call: ToolCall,
        result: str,
    ) -> None:
        """Append a tool result to the message list in-place."""
        ...
