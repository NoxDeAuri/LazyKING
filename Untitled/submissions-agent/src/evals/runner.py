"""
Eval runner.

Loads fixture data, injects a fake tool_executor into agent_loop, runs the
agent, captures tool calls, and evaluates assertions.

The write-confirmation gate in agent_loop is bypassed in eval mode — write
calls are blocked outright (they return a fixture error) rather than
prompting stdin, so evals are non-interactive.
"""

import json
from pathlib import Path
from typing import Any

from ..agent.backends.claude_backend import ClaudeBackend
from ..agent.agents import listing_agent, matching_agent, schedule_agent, orchestrator
from ..agent.loop import agent_loop
from ..server import handle_list_tools  # reuse tool schema definitions

from .case import EvalCase, CaseResult, AssertionResult, ToolCall as EvalToolCall

FIXTURES_DIR = Path(__file__).parent.parent.parent / "evals" / "fixtures"

_WRITE_TOOLS = {
    "upsert_work", "record_submission", "log_instagram_post",
    "create_timeline", "adjust_timeline", "record_response",
}

AGENTS = {
    "matching":    matching_agent,
    "listing":     listing_agent,
    "schedule":    schedule_agent,
    "orchestrator": orchestrator,
}


def load_fixture_file(name: str) -> Any:
    """Load a JSON fixture from evals/fixtures/<name>."""
    path = FIXTURES_DIR / name
    return json.loads(path.read_text())


def _make_executor(fixtures: dict[str, Any], call_log: list[EvalToolCall]):
    """
    Return an async tool_executor backed by fixture data.

    fixtures: { tool_name: response_dict | [response_dict, ...] }
      - dict  → always return the same response
      - list  → pop responses in order (simulates multiple calls)

    Write tools always return an error — evals are read-only.
    """
    # Mutable copy so we can pop from lists
    pool: dict[str, Any] = {}
    for name, val in fixtures.items():
        pool[name] = list(val) if isinstance(val, list) else val

    async def executor(name: str, args: dict) -> str:
        call_log.append(EvalToolCall(name=name, args=args))

        if name in _WRITE_TOOLS:
            return json.dumps({
                "error": f"[EVAL] Write tool '{name}' blocked — evals are read-only."
            })

        if name not in pool:
            return json.dumps({
                "error": f"[EVAL] No fixture for tool '{name}'. Add one to the EvalCase."
            })

        data = pool[name]
        if isinstance(data, list):
            if not data:
                return json.dumps({"error": f"[EVAL] Fixture list for '{name}' exhausted."})
            return json.dumps(data.pop(0), default=str)

        return json.dumps(data, default=str)

    return executor


async def _get_tool_schemas() -> list[dict]:
    """Pull tool schemas from the MCP server definition."""
    tools = await handle_list_tools()
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in tools
    ]


async def _auto_deny(tool_name: str, args: dict, messages: list) -> bool:
    """Write gate for eval mode — always denies without prompting stdin."""
    return False


async def run_case(case: EvalCase, verbose: bool = False) -> CaseResult:
    """Run a single EvalCase and return its CaseResult."""
    call_log: list[EvalToolCall] = []
    output = ""
    error = ""

    try:
        backend = ClaudeBackend()
        all_tool_schemas = await _get_tool_schemas()
        executor = _make_executor(case.fixtures, call_log)

        agent_mod = AGENTS[case.agent]
        tools = [t for t in all_tool_schemas if t["name"] in agent_mod.TOOLS]

        output = await agent_loop(
            backend=backend,
            messages=[{"role": "user", "content": case.prompt}],
            tools=tools,
            system=agent_mod.SYSTEM,
            tool_executor=executor,
            verbose=verbose,
            label=f"eval:{case.id}",
            confirm_fn=_auto_deny,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    # Evaluate assertions
    assertion_results = []
    for assertion in case.assertions:
        try:
            passed, detail = assertion.fn(output, call_log)
        except Exception as exc:
            passed, detail = False, f"Assertion raised: {exc}"
        assertion_results.append(AssertionResult(
            name=assertion.name,
            passed=passed,
            detail=detail,
        ))

    return CaseResult(
        case=case,
        output=output,
        tool_calls=call_log,
        assertion_results=assertion_results,
        error=error,
    )
