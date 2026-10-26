"""
Works profile builder — interactive CLI.

Usage:
    python -m src.profile.builder
    # or after install:
    build-profile

Asks one question at a time, shows the record before saving, and never
invents synopsis text. Re-run to update an existing record.
"""

import fcntl
import json
import sys
from pathlib import Path

WORKS_FILE = Path(__file__).parent / "works.json"

FIELD_PROMPTS = [
    ("id",            "Short ID (e.g. 'the-cliffs', lowercase-hyphenated): "),
    ("title",         "Title: "),
    ("type",          "Type (short story / novel): "),
    ("status",        "Status (drafting / complete / submitted / accepted / published): "),
    ("word_count",    "Word count (exact number): "),
    ("genre",         "Genre (e.g. literary, literary fiction, horror): "),
    ("themes",        "Themes (comma-separated): "),
    ("synopsis_short","Short synopsis (1-2 sentences — your words, not mine): "),
    ("synopsis_long", "Long synopsis (paragraph — your words, not mine): "),
    ("comps",         "Comps (comma-separated, or leave blank): "),
    ("content_flags", "Content flags anything a call might exclude (comma-separated, or blank): "),
]


def load_works() -> dict:
    if WORKS_FILE.exists():
        return json.loads(WORKS_FILE.read_text())
    return {"works": []}


def save_works(data: dict) -> None:
    """Atomically write works.json with an exclusive file lock (cross-process safe)."""
    WORKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = WORKS_FILE.with_suffix(".lock")
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            WORKS_FILE.write_text(json.dumps(data, indent=2))
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _ask(prompt: str, current: str = "") -> str:
    """Prompt with optional current value shown in brackets. Enter keeps current."""
    if current:
        val = input(f"{prompt}[{current}] ").strip()
        return val if val else current
    return input(prompt).strip()


def build_work(existing: dict = None) -> dict:
    work = existing.copy() if existing else {}

    for field, prompt in FIELD_PROMPTS:
        if field == "synopsis_short":
            current = (work.get("synopsis") or {}).get("short", "")
        elif field == "synopsis_long":
            current = (work.get("synopsis") or {}).get("long", "")
        elif field in ("themes", "comps", "content_flags"):
            current = ", ".join(work.get(field) or [])
        else:
            current = str(work.get(field, "") or "")

        val = _ask(prompt, current)

        if field == "synopsis_short":
            work.setdefault("synopsis", {})["short"] = val
        elif field == "synopsis_long":
            work.setdefault("synopsis", {})["long"] = val
        elif field in ("themes", "comps", "content_flags"):
            work[field] = [v.strip() for v in val.split(",") if v.strip()]
        elif field == "word_count":
            try:
                work[field] = int(val)
            except ValueError:
                print(f"  (Could not parse as integer, stored as-is: {val})")
                work[field] = val
        else:
            work[field] = val

    # Preserve fields not covered by prompts
    for preserved in ("submission_history", "accepted_on", "timeline_id",
                       "target_word_count", "target_word_count_note",
                       "word_count_note", "call_requirements"):
        if existing and preserved in existing:
            work.setdefault(preserved, existing[preserved])
        else:
            work.setdefault(preserved, [] if preserved == "submission_history" else None)

    return work


def confirm_and_save(work: dict, data: dict) -> bool:
    print("\n--- Record preview ---")
    print(json.dumps(work, indent=2))
    print("----------------------")
    answer = input("Save this record? (yes/no): ").strip().lower()
    if answer != "yes":
        print("Discarded.")
        return False

    works = data["works"]
    idx = next((i for i, w in enumerate(works) if w["id"] == work["id"]), None)
    if idx is not None:
        works[idx] = work
        print(f"Updated '{work['title']}'.")
    else:
        works.append(work)
        print(f"Added '{work['title']}'.")

    save_works(data)
    return True


def main():
    data = load_works()
    works = data["works"]

    print("Submissions Agent — Works Profile Builder")
    print("==========================================")

    if works:
        print(f"\nWorks on file: {', '.join(w['title'] for w in works)}")

    print("\nOptions:")
    print("  1. Add a new work")
    print("  2. Update an existing work")
    print("  3. Exit")

    choice = input("\nChoice: ").strip()

    if choice == "1":
        work = build_work()
        confirm_and_save(work, data)

    elif choice == "2":
        if not works:
            print("No works on file.")
            return
        for i, w in enumerate(works):
            print(f"  {i + 1}. {w['title']}  ({w['id']})  [{w.get('status', '?')}]")
        raw = input("Enter number: ").strip()
        try:
            selected = works[int(raw) - 1]
        except (ValueError, IndexError):
            print("Invalid selection.")
            return
        work = build_work(existing=selected)
        confirm_and_save(work, data)

    else:
        print("Exiting.")


if __name__ == "__main__":
    main()
