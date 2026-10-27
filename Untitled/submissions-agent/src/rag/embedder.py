"""
Text embedder — calls ollama's embedding endpoint.

Requires:
  - ollama running locally (default: http://localhost:11434)
  - the embedding model pulled: `ollama pull nomic-embed-text`

Configure via .env:
  OLLAMA_HOST       default http://localhost:11434
  EMBED_MODEL       default nomic-embed-text
"""

import os
import httpx
from dotenv import load_dotenv
from ..telemetry.spans import span as _span

load_dotenv()

_HOST  = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
_MODEL = os.getenv("EMBED_MODEL",  "nomic-embed-text")
_TIMEOUT = 30.0


async def embed(text: str) -> list[float]:
    """
    Embed text using ollama. Returns a float vector.
    Raises RuntimeError if ollama is unreachable or the model isn't available.
    """
    url = f"{_HOST}/api/embeddings"
    payload = {"model": _MODEL, "prompt": text}

    with _span("rag.embed", rag_model=_MODEL, rag_text_chars=len(text)) as s:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.ConnectError:
            raise RuntimeError(
                f"Cannot reach ollama at {_HOST}. "
                "Make sure ollama is running: `ollama serve`"
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Ollama embedding failed ({e.response.status_code}). "
                f"Is '{_MODEL}' pulled? Run: `ollama pull {_MODEL}`"
            )

        embedding = data.get("embedding")
        if not embedding:
            raise RuntimeError(f"Ollama returned no embedding for model '{_MODEL}'")

        s.set_attribute("rag.vector_dims", len(embedding))
        return embedding


def embed_sync(text: str) -> list[float]:
    """Synchronous wrapper for use in non-async contexts (e.g. handlers)."""
    import asyncio
    return asyncio.run(embed(text))
