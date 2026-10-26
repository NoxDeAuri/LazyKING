"""
Event handlers — side effects that fire when events are appended.

Handlers are registered at module import time. Import this module once
at app startup (app.py and server.py both do this) to activate them.

Design rule: a handler must never raise — errors are logged and swallowed
so that the caller's write path is never interrupted.
"""

from .schema import Event, EventType
from .store import subscribe


def _on_submission_sent(event: Event) -> None:
    """Log new submissions to stdout for visibility. Works.json is updated by the caller."""
    p = event.payload
    print(
        f"[event] submission_sent  {p['work_title']!r} → {p['call']!r}  "
        f"({p.get('date', '')})"
    )


def _on_response_received(event: Event) -> None:
    """
    When a response arrives:
      1. Log it.
      2. Trigger RAG indexing of the completed work+call pair — this is
         what lets future match recommendations learn from past outcomes.
    """
    p = event.payload
    rt = p.get("response_type", "?")
    print(
        f"[event] response_received  {p['work_title']!r} ← {p['call']!r}  "
        f"result={rt}  ({p.get('date', '')})"
    )
    _index_for_rag(event)


def _on_withdrawal_sent(event: Event) -> None:
    p = event.payload
    print(
        f"[event] withdrawal_sent  {p['work_title']!r} ← {p['call']!r}  "
        f"({p.get('date', '')})"
    )


def _index_for_rag(event: Event) -> None:
    """
    Index the completed submission in the vector store so future retrieval
    can surface it as a past-outcome example.

    Import is deferred so the RAG module is optional — if chromadb isn't
    installed or ollama isn't running, the handler logs and continues.
    """
    try:
        from ..rag.indexer import index_event
        index_event(event)
    except ImportError:
        pass  # chromadb not installed — RAG is optional
    except Exception as exc:
        print(f"[event] rag index failed: {exc}")


# ---------------------------------------------------------------------------
# Registration — happens once at import time
# ---------------------------------------------------------------------------

def _register() -> None:
    subscribe(EventType.SUBMISSION_SENT,   _on_submission_sent)
    subscribe(EventType.RESPONSE_RECEIVED, _on_response_received)
    subscribe(EventType.WITHDRAWAL_SENT,   _on_withdrawal_sent)


_register()
