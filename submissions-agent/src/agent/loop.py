"""
Shared agent loop — runs a backend through tool calls until it returns a final answer.
Used by both the orchestrator and each specialist agent.
"""

import asyncio
import json
import re
from typing import Callable, Awaitable

from .backends.base import Backend, BackendResponse
from .tools import call_tool
from ..telemetry.spans import span, record_event
from ..guardrails import input_fence, tool_validator, reasoning_trace

MAX_ITERATIONS = 20

# ---------------------------------------------------------------------------
# Security: data fence for external tool results (LLM01)
# Input fence module adds active injection scanning on top of the basic wrap.
# ---------------------------------------------------------------------------

_EXTERNAL_TOOLS = {
    "search_calls", "find_boutique", "match_works",
}


def _wrap_tool_result(tool_name: str, result: str) -> str:
    if tool_name in _EXTERNAL_TOOLS:
        fenced, fence_result = input_fence.wrap_external(tool_name, result)
        if not fence_result.clean:
            _log_fence_warning(tool_name, fence_result)
        return fenced
    return result


def _log_fence_warning(tool_name: str, fence_result) -> None:
    if fence_result.injections:
        print(
            f"\n[GUARDRAIL] Input fence blocked injection in {tool_name} result: "
            + ", ".join(fence_result.injections)
        )
    if fence_result.manuscript_refs:
        print(
            f"\n[GUARDRAIL] Input fence redacted manuscript path in {tool_name} result."
        )


# ---------------------------------------------------------------------------
# Security: write-confirmation gate with reasoning trace (LLM06)
# ---------------------------------------------------------------------------

_WRITE_TOOLS = {
    "upsert_work",
    "record_submission",
    "log_instagram_post",
    "create_timeline",
    "adjust_timeline",
    "record_response",
}


async def _confirm_write(tool_name: str, args: dict, messages: list[dict]) -> bool:
    trace = reasoning_trace.verify(tool_name, args, messages)

    # Auto-deny without prompting when reasoning is flagged as suspicious
    if trace.verdict == "SUSPICIOUS":
        print(
            f"\n[GUARDRAIL] Reasoning trace SUSPICIOUS for {tool_name} — action auto-denied.\n"
            f"  Patterns: {', '.join(trace.suspicious)}"
        )
        return False

    block = reasoning_trace.format_confirmation(tool_name, args, trace)
    loop = asyncio.get_event_loop()

    def _ask() -> bool:
        print(block)
        return input("\n  Allow this action? [y/N]: ").strip().lower() == "y"

    return await loop.run_in_executor(None, _ask)


# ---------------------------------------------------------------------------
# Security: response leakage filter (LLM07)
# ---------------------------------------------------------------------------

_LEAK_PATTERNS = [
    "=== EXTERNAL DATA ===",
    "BW_CLIENTID",
    "BW_CLIENTSECRET",
    "BW_SESSION",
    "ANTHROPIC_API_KEY",
    "TWEEK_API_KEY",
    "TWEEK_CALENDAR_ID",
    "AGENT_SECRET",
]


def _check_for_leakage(text: str) -> bool:
    return any(p in text for p in _LEAK_PATTERNS)


async def agent_loop(
    backend: Backend,
    messages: list[dict],
    tools: list[dict],
    system: str,
    tool_executor: Callable[[str, dict], Awaitable[str]] = call_tool,
    verbose: bool = False,
    label: str = "",
    confirm_fn: Callable | None = None,
) -> str:
    """
    Run the backend until it stops calling tools. Returns the final text response.

    tool_executor: async (name, args) -> str
        Defaults to the shared MCP tool dispatcher.
        Pass a custom executor to give the orchestrator sub-agent delegation.
    """
    prefix = f"[{label}] " if label else ""
    model  = getattr(backend, "model", "unknown")
    btype  = type(backend).__name__.replace("Backend", "").lower()

    with span(
        "agent.loop",
        agent_label=label or "unnamed",
        agent_backend=btype,
        agent_model=model,
        agent_tool_count=len(tools),
    ) as loop_span:
        iteration = 0
        total_tool_calls = 0

        for _ in range(MAX_ITERATIONS):
            iteration += 1
            response: BackendResponse = await backend.chat(messages, tools, system=system)

            if not response.tool_calls:
                text = response.text or ""
                if _check_for_leakage(text):
                    text = "[Response filtered: contained internal configuration details.]"
                loop_span.set_attribute("agent.iterations", iteration)
                loop_span.set_attribute("agent.total_tool_calls", total_tool_calls)
                loop_span.set_attribute("agent.response_chars", len(text))
                return text

            # Only print intermediate text (when there are also tool calls)
            if response.text and verbose:
                print(f"\n{prefix}{response.text}")

            backend.append_assistant(messages, response)

            # Execute all tool calls, gathering parallel-safe ones
            # (tool calls within one response are independent so gather is safe)
            async def _exec(tc):
                nonlocal total_tool_calls
                is_write = tc.name in _WRITE_TOOLS

                with span(
                    "agent.tool",
                    tool_name=tc.name,
                    tool_is_write=is_write,
                    agent_label=label or "unnamed",
                ) as tool_span:
                    # Tool argument validation guardrail (blocks malformed/adversarial args)
                    arg_ok, arg_error = tool_validator.validate_args(tc.name, tc.args)
                    if not arg_ok:
                        tool_span.set_attribute("tool.validation_failed", True)
                        record_event(tool_span, "guardrail.tool_validation_failed",
                                     tool_name=tc.name, error=arg_error)
                        if verbose:
                            print(f"\n{prefix}  [GUARDRAIL] {arg_error}")
                        return tc, json.dumps({"error": arg_error})

                    # Write-confirmation gate with reasoning trace (LLM06)
                    if is_write:
                        _confirm = confirm_fn if confirm_fn is not None else _confirm_write
                        approved = await _confirm(tc.name, tc.args, messages)
                        record_event(tool_span, "write.gate", tool_approved=approved)
                        if not approved:
                            tool_span.set_attribute("tool.denied", True)
                            return tc, json.dumps({
                                "error": "Action denied by user. Do not retry without explicit approval."
                            })

                    if verbose:
                        print(f"\n{prefix}  → {tc.name}({tc.args})")
                    result = await tool_executor(tc.name, tc.args)
                    total_tool_calls += 1

                    # Keep results under 8 KB so Ollama doesn't time out summarising them
                    if len(result) > 8000:
                        result = result[:8000] + "\n... [truncated — full result too large for context]"
                        tool_span.set_attribute("tool.truncated", True)

                    tool_span.set_attribute("tool.result_chars", len(result))

                    # Wrap external content in a data fence (LLM01)
                    result = _wrap_tool_result(tc.name, result)
                    if verbose:
                        preview = result[:200] + ("..." if len(result) > 200 else "")
                        print(f"{prefix}  ← {tc.name}: {preview}")
                    return tc, result

            pairs = await asyncio.gather(*[_exec(tc) for tc in response.tool_calls])

            for tc, result in pairs:
                backend.append_tool_result(messages, tc, result)

        loop_span.set_attribute("agent.iterations", MAX_ITERATIONS)
        loop_span.set_attribute("agent.total_tool_calls", total_tool_calls)
        return "[Agent reached iteration limit without a final answer]"
