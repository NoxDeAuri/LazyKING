"""
Submissions Agent — CLI entry point.

Routes every prompt through the orchestrator, which delegates to:
  ListingAgent  — searches submission calls
  MatchingAgent — matches works to listings
  ScheduleAgent — builds/adjusts timelines in Tweek

Backend switches between Ollama (local) and Claude (Anthropic API).

Usage:
    run-agent "find boutique calls for my short stories"
    run-agent --backend claude "what are my complete works?"
    run-agent --backend ollama --model llama3.1 "create a timeline for The Cliffs"
    run-agent          # interactive mode

.env keys:
    AGENT_BACKEND    ollama | claude  (default: ollama)
    OLLAMA_BASE_URL  http://localhost:11434
    OLLAMA_MODEL     qwen2.5
    ANTHROPIC_API_KEY
    CLAUDE_MODEL     claude-opus-5-5
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv

from .agents.orchestrator import run as orchestrate
from .backends.base import Backend
from ..telemetry.setup import setup_telemetry

load_dotenv()
setup_telemetry()


def _make_backend(backend_name: str, model: str = None) -> Backend:
    name = backend_name.lower()
    if name == "claude":
        from .backends.claude_backend import ClaudeBackend
        return ClaudeBackend(model=model)
    if name == "ollama":
        from .backends.ollama_backend import OllamaBackend
        return OllamaBackend(model=model)
    raise ValueError(f"Unknown backend '{backend_name}'. Choose 'ollama' or 'claude'.")


async def run(
    prompt: str,
    backend_name: str,
    model: str = None,
    verbose: bool = False,
) -> str:
    backend = _make_backend(backend_name, model)
    return await orchestrate(prompt, backend, verbose=verbose)


def main():
    parser = argparse.ArgumentParser(description="Submissions Agent")
    parser.add_argument("prompt", nargs="?", help="What to ask")
    parser.add_argument(
        "--backend",
        default=os.environ.get("AGENT_BACKEND", "ollama"),
        choices=["ollama", "claude"],
        help="LLM backend (default: ollama, or AGENT_BACKEND env var)",
    )
    parser.add_argument("--model", default=None, help="Model name override")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show tool calls")
    args = parser.parse_args()

    if not args.prompt:
        print("Submissions Agent — interactive mode")
        print(f"Backend: {args.backend}" + (f"  model: {args.model}" if args.model else ""))
        print("Ctrl-C to exit.\n")
        try:
            while True:
                prompt = input("You: ").strip()
                if not prompt:
                    continue
                answer = asyncio.run(run(prompt, args.backend, args.model, verbose=args.verbose))
                print(f"\nAgent: {answer}\n")
        except KeyboardInterrupt:
            print("\nGoodbye.")
    else:
        answer = asyncio.run(run(args.prompt, args.backend, args.model, verbose=args.verbose))
        print(answer)


if __name__ == "__main__":
    main()
