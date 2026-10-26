"""
rag-index CLI — rebuild the vector store from the full event log.

Run this once after first install, or any time you want to re-sync
the vector store with the current event log (e.g. after importing
old submission history).

Usage:
    rag-index          # index all completed submissions
    rag-index --stats  # show vector store stats and exit
"""

import argparse
import asyncio

from dotenv import load_dotenv


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rag-index")
    p.add_argument("--stats", action="store_true", help="Print store stats and exit")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> None:
    load_dotenv()

    from . import store
    from ..events.store import replay

    if args.stats:
        count  = store.count()
        events = replay()
        print(f"\nVector store: {count} documents")
        print(f"Event log:    {len(events)} events")
        print(f"  → {sum(1 for e in events if e.type == 'submission_sent')} submissions sent")
        print(f"  → {sum(1 for e in events if e.type == 'response_received')} responses received")
        print()
        return

    print("\nIndexing submission history…\n")
    from .indexer import index_all
    n = await index_all()
    print(f"\nDone. {n} documents in vector store.\n")


def main() -> None:
    asyncio.run(_run(_parse_args()))
