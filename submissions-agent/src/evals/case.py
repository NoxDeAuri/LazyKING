"""
Eval case dataclasses.

An EvalCase bundles:
  - which agent to run
  - the prompt to send
  - fixture data keyed by tool name (replaces real tool calls)
  - a list of assertions to check against the final output
"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class Assertion:
    name: str
    # fn receives (output_text, list_of_tool_calls) -> (passed, detail_message)
    fn: Callable[[str, list[ToolCall]], tuple[bool, str]]


@dataclass
class EvalCase:
    id: str
    description: str
    # "matching" | "listing" | "schedule" | "orchestrator"
    agent: str
    prompt: str
    # tool_name -> dict that becomes the JSON response (or a list of dicts for
    # tools that may be called more than once; they're popped in order)
    fixtures: dict[str, Any] = field(default_factory=dict)
    assertions: list[Assertion] = field(default_factory=list)
    # skip the case without marking it as a failure
    skip: bool = False
    skip_reason: str = ""


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str


@dataclass
class CaseResult:
    case: EvalCase
    output: str
    tool_calls: list[ToolCall]
    assertion_results: list[AssertionResult]
    error: str = ""           # set if the agent raised an exception

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(r.passed for r in self.assertion_results)


# ---------------------------------------------------------------------------
# Built-in assertion helpers
# ---------------------------------------------------------------------------

def contains(text: str, *, label: str = "") -> Assertion:
    """Output must contain `text` (case-insensitive)."""
    name = label or f'contains("{text}")'
    def _check(output: str, _calls: list[ToolCall]) -> tuple[bool, str]:
        passed = text.lower() in output.lower()
        return passed, "" if passed else f'Expected "{text}" in output'
    return Assertion(name=name, fn=_check)


def not_contains(text: str, *, label: str = "") -> Assertion:
    """Output must NOT contain `text` (case-insensitive)."""
    name = label or f'not_contains("{text}")'
    def _check(output: str, _calls: list[ToolCall]) -> tuple[bool, str]:
        passed = text.lower() not in output.lower()
        return passed, "" if passed else f'Unexpected "{text}" found in output'
    return Assertion(name=name, fn=_check)


def called_tool(tool_name: str) -> Assertion:
    """Agent must have called `tool_name` at least once."""
    def _check(_output: str, calls: list[ToolCall]) -> tuple[bool, str]:
        passed = any(c.name == tool_name for c in calls)
        return passed, "" if passed else f'Expected tool "{tool_name}" to be called'
    return Assertion(name=f'called_tool("{tool_name}")', fn=_check)


def not_called_tool(tool_name: str) -> Assertion:
    """Agent must NOT have called `tool_name`."""
    def _check(_output: str, calls: list[ToolCall]) -> tuple[bool, str]:
        passed = not any(c.name == tool_name for c in calls)
        return passed, "" if passed else f'Tool "{tool_name}" was called but should not have been'
    return Assertion(name=f'not_called_tool("{tool_name}")', fn=_check)


def no_write_tools() -> Assertion:
    """Agent must not have called any write-side tools (record, upsert, etc.)."""
    _WRITE = {"upsert_work", "record_submission", "log_instagram_post",
               "create_timeline", "adjust_timeline"}
    def _check(_output: str, calls: list[ToolCall]) -> tuple[bool, str]:
        bad = [c.name for c in calls if c.name in _WRITE]
        return (not bad), ("" if not bad else f'Write tools called: {bad}')
    return Assertion(name="no_write_tools()", fn=_check)


def output_not_empty() -> Assertion:
    """Agent must produce a non-empty response."""
    def _check(output: str, _calls: list[ToolCall]) -> tuple[bool, str]:
        passed = bool(output.strip())
        return passed, "" if passed else "Output was empty"
    return Assertion(name="output_not_empty()", fn=_check)


def output_contains_any(*texts: str, label: str = "") -> Assertion:
    """Output must contain at least one of the given strings (case-insensitive)."""
    name = label or f'output_contains_any({", ".join(repr(t) for t in texts)})'
    def _check(output: str, _calls: list[ToolCall]) -> tuple[bool, str]:
        low = output.lower()
        passed = any(t.lower() in low for t in texts)
        return passed, "" if passed else f'Expected one of {list(texts)} in output'
    return Assertion(name=name, fn=_check)


def tool_args_contain(tool_name: str, key: str, value) -> Assertion:
    """A call to `tool_name` must exist with args[key] == value."""
    def _check(_output: str, calls: list[ToolCall]) -> tuple[bool, str]:
        for c in calls:
            if c.name == tool_name and c.args.get(key) == value:
                return True, ""
        return False, f'No call to "{tool_name}" with {key}={value!r}'
    return Assertion(name=f'tool_args_contain("{tool_name}", {key}={value!r})', fn=_check)


def max_tool_calls(n: int) -> Assertion:
    """Agent must not make more than `n` total tool calls (runaway loop guard)."""
    def _check(_output: str, calls: list[ToolCall]) -> tuple[bool, str]:
        passed = len(calls) <= n
        return passed, "" if passed else f'Made {len(calls)} tool calls, limit is {n}'
    return Assertion(name=f'max_tool_calls({n})', fn=_check)
