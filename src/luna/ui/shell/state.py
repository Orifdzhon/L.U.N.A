"""Pure shell state: icon state derivation, conversation model, tray menu state (no Qt)."""

from __future__ import annotations

import enum
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from luna.core.events import Status
from luna.tools.base import ToolResult

MAX_SUMMARY_CHARS = 120


class IconState(enum.Enum):
    IDLE = "idle"
    WAKE = "wake"
    RECORDING = "recording"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


_TOOLTIPS = {
    IconState.IDLE: "L.U.N.A. - idle",
    IconState.WAKE: "L.U.N.A. - listening for the wake word",
    IconState.RECORDING: "L.U.N.A. - listening",
    IconState.THINKING: "L.U.N.A. - thinking",
    IconState.SPEAKING: "L.U.N.A. - speaking",
    IconState.ERROR: "L.U.N.A. - error",
}


def tooltip_for(state: IconState) -> str:
    return _TOOLTIPS[state]


def derive_icon_state(
    status: Status | None, wake_state: str, error_active: bool, conversation: bool = False
) -> IconState:
    """Pick the tray icon. ``error_active`` covers the transient post-ERROR window and FAILED."""
    wake = wake_state.upper()
    if error_active or wake == "FAILED" or status is Status.ERROR:
        return IconState.ERROR
    if status is Status.LISTENING:
        return IconState.RECORDING
    if status is Status.SPEAKING:
        return IconState.SPEAKING
    if status is Status.THINKING:
        return IconState.THINKING
    if conversation and wake == "ARMED":
        return IconState.RECORDING  # the follow-up window is open: listening without the keyword
    if wake in ("ARMED", "HELD"):
        return IconState.WAKE
    return IconState.IDLE


@dataclass(frozen=True)
class ConversationLine:
    kind: Literal["user", "assistant", "action", "system"]
    text: str


def format_action_line(result: ToolResult) -> ConversationLine:
    """``"open_website: ok"`` plus the summary (cut to 120 chars; never for untrusted results)."""
    text = f"{result.tool}: {'ok' if result.ok else 'failed'}"
    if not result.untrusted:
        summary = " ".join(result.summary.split())
        if summary:
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
            text = f"{text} - {summary}"
    return ConversationLine("action", text)


class Conversation:
    """Bounded in-memory list of lines; the oldest line is dropped beyond ``max_lines``."""

    def __init__(self, max_lines: int) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        self._lines: deque[ConversationLine] = deque(maxlen=max_lines)

    def add(self, line: ConversationLine) -> ConversationLine:
        self._lines.append(line)
        return line

    def add_action(self, result: ToolResult) -> ConversationLine:
        return self.add(format_action_line(result))

    @property
    def lines(self) -> tuple[ConversationLine, ...]:
        return tuple(self._lines)

    def __len__(self) -> int:
        return len(self._lines)

    def __iter__(self) -> Iterator[ConversationLine]:
        return iter(self.lines)


@dataclass(frozen=True)
class TrayMenuState:
    wake_checked: bool
    wake_enabled: bool  # False while enable/disable runs or wake is unavailable
    wake_label: str  # "Listen for «Луна»" / "Starting wake word…"
    voice_checked: bool  # not muted
    voice_enabled: bool  # False when [tts] enabled = false (no Speaker)
    autostart_checked: bool
    autostart_enabled: bool  # False while toggling or on unsupported platforms
