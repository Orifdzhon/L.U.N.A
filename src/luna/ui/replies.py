"""Reply helpers shared by the CLI and the desktop shell (no toolkit imports)."""

from __future__ import annotations

from luna.core.models import Reply
from luna.core.prompts import fallback_text

# Apologies for a cancelled / timed-out turn are shown but not spoken.
_SILENT_KINDS = ("cancelled", "timeout")


def is_silent(reply: Reply) -> bool:
    return any(
        reply.text == fallback_text(kind, lang) for kind in _SILENT_KINDS for lang in ("ru", "en")
    )
