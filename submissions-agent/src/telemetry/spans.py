"""
Span helpers — thin wrappers around the OTel SDK.

All helpers are safe to call when OTel is disabled: the SDK's default
no-op tracer means span() runs the body with negligible overhead and
records nothing. No feature flags needed in calling code.

Usage:
    with span("agent.loop", agent_label="matching", agent_model="claude-opus-5-5"):
        ...

    with span("agent.tool", tool_name="match_works", tool_is_write=False) as s:
        result = await tool_executor(...)
        s.set_attribute("tool.result_chars", len(result))

Attribute naming: kwargs use underscores (agent_label); they are stored
as dotted OTel keys (agent.label) by replacing the first underscore
in each segment with a dot. For multi-word segments, use double
underscore as separator: tool_is_write → tool.is_write.
"""

from contextlib import contextmanager
from typing import Generator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_TRACER_NAME = "submissions-agent"


def get_tracer() -> trace.Tracer:
    """Return the global tracer. Works as a no-op when OTel is not configured."""
    return trace.get_tracer(_TRACER_NAME)


def _to_otel_key(kwarg_key: str) -> str:
    """Convert a Python kwarg name to an OTel attribute key.

    Rules:
      agent_label   → agent.label
      tool_is_write → tool.is_write
      rag_k         → rag.k
    First underscore after the namespace prefix becomes a dot;
    subsequent underscores stay as underscores.
    """
    parts = kwarg_key.split("_", 1)
    return ".".join(parts) if len(parts) == 2 else kwarg_key


@contextmanager
def span(name: str, /, **attributes) -> Generator[Span, None, None]:
    """
    Context manager that wraps a block in an OTel span.

    Keyword arguments become span attributes (underscore → dot conversion).
    Exceptions are recorded on the span and re-raised.

    Example:
        with span("rag.search", rag_k=5) as s:
            results = store.search(...)
            s.set_attribute("rag.result_count", len(results))
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as s:
        for key, value in attributes.items():
            if value is not None:
                try:
                    s.set_attribute(_to_otel_key(key), value)
                except Exception:
                    pass  # never let telemetry break the app
        try:
            yield s
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def record_event(span_obj: Span, name: str, **attributes) -> None:
    """Add a timestamped event to an existing span (e.g. 'tool.approved')."""
    try:
        attrs = {_to_otel_key(k): v for k, v in attributes.items() if v is not None}
        span_obj.add_event(name, attributes=attrs)
    except Exception:
        pass
