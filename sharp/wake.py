"""Wake-word filtering for voice input.

Typed messages are intentionally not filtered.  Only microphone transcripts pass
through this module, so ambient speech never reaches the assistant unless it
contains the name "Шарп" (or its English spelling, which STT sometimes returns).
"""
from __future__ import annotations

import re

WAKE_WORD = "Шарп"
_WAKE_RE = re.compile(r"(?iu)(?<![\w-])(?:шарп|sharp)(?![\w-])")
_SEPARATORS_RE = re.compile(r"^[\s,.:;!?—–-]+|[\s,.:;!?—–-]+$")


def extract_command(transcript: str) -> str | None:
    """Return the spoken command after the wake word, or ``None`` if not addressed.

    Speech recognizers occasionally place the name at the end ("включи музыку,
    Шарп"), therefore text on both sides is retained.
    """
    match = _WAKE_RE.search(transcript)
    if not match:
        return None

    before = _SEPARATORS_RE.sub("", transcript[: match.start()])
    after = _SEPARATORS_RE.sub("", transcript[match.end() :])
    command = " ".join(part for part in (before, after) if part).strip()
    return command or "Слушай меня"
