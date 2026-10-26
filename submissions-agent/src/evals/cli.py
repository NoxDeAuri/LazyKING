"""
run-evals CLI entry point.

Usage:
    run-evals                        # run all cases
    run-evals --case matching/basic  # run one case by id
    run-evals --suite security       # run all cases whose id starts with "security/"
    run-evals --verbose              # print agent output and tool calls
    run-evals --list                 # list all available case ids

Exit code: 0 if all pass, 1 if any fail.
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from .cases import ALL_CASES
from .runner import run_case
from .report import print_report, exit_with_code


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run-evals",
        description="Run the submissions-agent eval suite",
    )
    p.add_argument("--case",    metavar="ID",    help="Run a single case by exact id")
    p.add_argument("--suite",   metavar="PREFIX", help='Run all cases whose id starts with PREFIX (e.g. "security")')
    p.add_argument("--verbose", action="store_true", help="Print agent output and tool call log")
    p.add_argument("--list",    action="store_true", help="List all available case ids and exit")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> None:
    load_dotenv()

    if args.list:
        print("\nAvailable eval cases:\n")
        for case in ALL_CASES:
            skip_note = f"  [SKIP: {case.skip_reason}]" if case.skip else ""
            print(f"  {case.id:<45} {case.description}{skip_note}")
        print()
        sys.exit(0)

    # Filter cases
    if args.case:
        cases = [c for c in ALL_CASES if c.id == args.case]
        if not cases:
            print(f"Error: no case with id '{args.case}'", file=sys.stderr)
            sys.exit(2)
    elif args.suite:
        prefix = args.suite.rstrip("/") + "/"
        cases = [c for c in ALL_CASES if c.id.startswith(prefix)]
        if not cases:
            print(f"Error: no cases matching suite '{args.suite}'", file=sys.stderr)
            sys.exit(2)
    else:
        cases = ALL_CASES

    results = []
    skipped = []

    for case in cases:
        if case.skip:
            skipped.append((case, case.skip_reason))
            continue

        print(f"  Running {case.id}…", end="\r", flush=True)
        result = await run_case(case, verbose=args.verbose)
        results.append(result)

        # Live indicator
        icon = "✓" if result.passed else "✗"
        print(f"  {icon} {case.id}" + (" " * 20))

    print_report(results, skipped, verbose=args.verbose)
    exit_with_code(results)


def main() -> None:
    asyncio.run(_run(_parse_args()))
