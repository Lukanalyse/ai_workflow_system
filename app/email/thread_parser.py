from __future__ import annotations

import re

THREAD_BREAK_PATTERNS = [
    r"^On .+ wrote:$",
    r"^From:\s+.+$",
    r"^Sent:\s+.+$",
    r"^-----Original Message-----$",
    r"^Le .+ a écrit :$",
]


def trim_thread_history(text: str) -> str:
    """Keep only the top-most message body and remove reply chains."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        for pattern in THREAD_BREAK_PATTERNS:
            if re.match(pattern, line.strip(), flags=re.IGNORECASE):
                return "\n".join(lines[:index]).strip()
    return text.strip()

