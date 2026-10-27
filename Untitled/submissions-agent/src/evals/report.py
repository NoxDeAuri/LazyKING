"""
Terminal report for eval results.

Prints a colored summary and exits with code 0 (all pass) or 1 (any fail).
"""

import sys
from .case import CaseResult

# ANSI colors
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_PASS = f"{_GREEN}✓{_RESET}"
_FAIL = f"{_RED}✗{_RESET}"
_SKIP = f"{_YELLOW}–{_RESET}"


def _icon(passed: bool) -> str:
    return _PASS if passed else _FAIL


def print_report(results: list[CaseResult], skipped: list, verbose: bool = False) -> None:
    print()
    print(f"{_BOLD}{'─' * 60}{_RESET}")
    print(f"{_BOLD}  Eval Results{_RESET}")
    print(f"{'─' * 60}")

    for r in results:
        status = _icon(r.passed)
        label = f"{_BOLD}{r.case.id}{_RESET}"
        print(f"  {status} {label}  {_DIM}{r.case.description}{_RESET}")

        if r.error:
            print(f"       {_RED}ERROR: {r.error}{_RESET}")

        for ar in r.assertion_results:
            a_icon = _icon(ar.passed)
            a_name = f"{_DIM}{ar.name}{_RESET}"
            print(f"       {a_icon} {a_name}", end="")
            if not ar.passed and ar.detail:
                print(f"  {_RED}← {ar.detail}{_RESET}", end="")
            print()

        if verbose and r.output:
            print(f"\n       {_CYAN}Output:{_RESET}")
            for line in r.output.splitlines():
                print(f"         {line}")
            if r.tool_calls:
                print(f"\n       {_CYAN}Tool calls:{_RESET}")
                for tc in r.tool_calls:
                    print(f"         → {tc.name}")
            print()

    for case, reason in skipped:
        print(f"  {_SKIP} {_BOLD}{case.id}{_RESET}  {_DIM}SKIPPED: {reason}{_RESET}")

    print(f"{'─' * 60}")

    total   = len(results)
    passed  = sum(1 for r in results if r.passed)
    failed  = total - passed
    n_skip  = len(skipped)

    pass_color = _GREEN if failed == 0 else _RED
    print(
        f"  {pass_color}{_BOLD}{passed}/{total} passed{_RESET}"
        + (f"  {_RED}{failed} failed{_RESET}" if failed else "")
        + (f"  {_YELLOW}{n_skip} skipped{_RESET}" if n_skip else "")
    )
    print(f"{'─' * 60}")
    print()


def exit_with_code(results: list[CaseResult]) -> None:
    failed = any(not r.passed for r in results)
    sys.exit(1 if failed else 0)
