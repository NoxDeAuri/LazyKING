"""
Pre-write Reasoning Trace — verifies the agent has a coherent reason before
any write tool fires.

When the agent is about to call a write tool, this module:
  1. Extracts the most recent assistant text from the conversation history
     (the model's explanation of what it's about to do).
  2. Applies rules-based checks to verify the reasoning is:
       - Present and non-trivial (not just a placeholder sentence)
       - Grounded in the conversation (mentions the specific work/call/date)
       - Free of injection markers
       - Not circumventing the write gate (doesn't claim pre-approval)
  3. Returns a formatted block that replaces the raw confirmation prompt,
     so the user sees the agent's reasoning alongside the action.

A failing trace does NOT automatically block the action — that remains the
user's decision at the confirmation prompt. Instead, it surfaces a NOTICE
so the user can make an informed choice.
"""

import re
from dataclasses import dataclass, field

# ── Minimum reasoning thresholds ─────────────────────────────────────────────

_MIN_REASONING_CHARS = 30
_MIN_REASONING_WORDS = 8

# ── Suspicious reasoning patterns ────────────────────────────────────────────

_SUSPICIOUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\byou (already |previously )?(approved|authorized|confirmed)\b", re.I),
     "claims pre-authorization"),
    (re.compile(r"\bTrevor (said|asked|told me|instructed|confirmed)\b", re.I),
     "fabricates user instruction"),
    (re.compile(r"\bautomatically\b.{0,30}\bsubmit\b", re.I),
     "attempts auto-submission"),
    (re.compile(r"\bwithout (your |asking |confirmation |approval )?permission\b", re.I),
     "permission bypass language"),
    (re.compile(r"\bjust (do|execute|run|call) it\b", re.I),
     "dismisses confirmation requirement"),
    (re.compile(r"\bignore (the |this )?(confirm|gate|check|guardrail)\b", re.I),
     "attempts to bypass guardrail"),
]

# Anchor terms: reasoning should mention something specific to the action
_WRITE_ANCHORS: dict[str, list[str]] = {
    "record_submission": ["submiss", "submit", "sent", "call", "work", "story"],
    "upsert_work":       ["work", "title", "story", "update", "add", "creat"],
    "create_timeline":   ["timeline", "draft", "schedule", "start", "work"],
    "adjust_timeline":   ["timeline", "adjust", "faster", "slower", "pace", "session"],
    "log_instagram_post":["instagram", "post", "caption", "reel", "carousel"],
    "record_response":   ["response", "reject", "accept", "waitlist", "withdraw", "result"],
}


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_reasoning(messages: list[dict]) -> str:
    """
    Find the most recent assistant message text that came before tool calls.

    Backends represent the assistant turn differently depending on whether
    tool calls were batched with a text preamble. We walk backwards and
    return the first non-empty assistant text we find.
    """
    for msg in reversed(messages):
        role = msg.get("role", "")
        if role != "assistant":
            continue

        # Claude-style: content is a list of blocks
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        return text
        # Ollama-style: content is a plain string
        elif isinstance(content, str):
            text = content.strip()
            if text:
                return text

    return ""


# ── Verification ─────────────────────────────────────────────────────────────

@dataclass
class TraceResult:
    reasoning: str
    issues: list[str] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)
    anchored: bool = True        # mentions something relevant to the action
    verdict: str = "PASS"        # PASS | WARN | SUSPICIOUS


def verify(tool_name: str, args: dict, messages: list[dict]) -> TraceResult:
    """
    Extract and verify agent reasoning before a write tool fires.
    """
    reasoning = extract_reasoning(messages)
    result = TraceResult(reasoning=reasoning)

    # 1. Presence check
    if not reasoning:
        result.issues.append("No reasoning found in last assistant message.")
        result.verdict = "WARN"
        return result

    word_count = len(reasoning.split())
    if len(reasoning) < _MIN_REASONING_CHARS or word_count < _MIN_REASONING_WORDS:
        result.issues.append(
            f"Reasoning too brief ({word_count} words). "
            "Agent should explain what it's doing and why."
        )
        result.verdict = "WARN"

    # 2. Suspicious pattern scan
    for pattern, label in _SUSPICIOUS_PATTERNS:
        if pattern.search(reasoning):
            result.suspicious.append(label)

    if result.suspicious:
        result.verdict = "SUSPICIOUS"

    # 3. Anchor check — reasoning should mention entities from the args or tool context
    anchors = _WRITE_ANCHORS.get(tool_name, [])
    if anchors:
        combined = (reasoning + " " + " ".join(str(v) for v in args.values())).lower()
        # Check if reasoning alone (not args) mentions at least one anchor
        reasoning_lower = reasoning.lower()
        if not any(a in reasoning_lower for a in anchors):
            result.issues.append(
                f"Reasoning doesn't mention the subject of this action "
                f"(expected one of: {', '.join(anchors[:4])}, …)."
            )
            if result.verdict == "PASS":
                result.verdict = "WARN"

    return result


# ── Formatted output ──────────────────────────────────────────────────────────

def format_confirmation(
    tool_name: str,
    args: dict,
    trace: TraceResult,
) -> str:
    """
    Build the text block displayed to the user at the write-gate prompt.
    Replaces the plain "Agent wants to call X" message.
    """
    import json

    lines = ["", "╔═══════════════════════════════════════════╗"]

    if trace.verdict == "SUSPICIOUS":
        lines.append("║  ⚠  WRITE GATE  —  SUSPICIOUS REASONING  ║")
    elif trace.verdict == "WARN":
        lines.append("║  ⚠  WRITE GATE  —  VERIFY REASONING      ║")
    else:
        lines.append("║     WRITE GATE  —  CONFIRMATION REQUIRED  ║")

    lines.append("╚═══════════════════════════════════════════╝")
    lines.append(f"\nTool   : {tool_name}")
    lines.append(f"Args   : {json.dumps(args, indent=2)}")
    lines.append(f"\nAgent's reasoning:")

    if trace.reasoning:
        # Indent each line of the reasoning
        for ln in trace.reasoning.splitlines():
            lines.append(f"  {ln}")
    else:
        lines.append("  (none — agent gave no explanation)")

    if trace.issues:
        lines.append("\n[TRACE ISSUES]")
        for issue in trace.issues:
            lines.append(f"  • {issue}")

    if trace.suspicious:
        lines.append("\n[SUSPICIOUS PATTERNS DETECTED]")
        for s in trace.suspicious:
            lines.append(f"  ⚠ {s}")

    return "\n".join(lines)
