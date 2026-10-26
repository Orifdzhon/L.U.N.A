"""Half-duplex wiring: mute the wake detector while L.U.N.A. speaks (ADR 0004 amendment)."""

from __future__ import annotations

from collections.abc import Callable

from luna.config.settings import WakeWordSettings
from luna.tts.speaker import Speaker
from luna.wakeword.listener import SPEAKING_HOLD, WakeListener

__all__ = ["bind_speaker"]


def _noop() -> None:
    return None


def bind_speaker(
    speaker: Speaker, listener: WakeListener, settings: WakeWordSettings
) -> Callable[[], None]:
    """Hold the ``speaking`` reason on ``listener`` while ``speaker`` is active.

    Returns an idempotent ``unbind``; it also releases the hold, so a stale hold cannot
    outlive the binding. With ``mute_while_speaking = false`` (headset use) nothing is
    bound and ``unbind`` is a no-op. The callback runs under the Speaker lock and only
    takes the listener lock (order Speaker -> WakeListener, never reversed).
    """
    if not settings.mute_while_speaking:
        return _noop

    def on_activity(active: bool) -> None:
        listener.set_hold(SPEAKING_HOLD, active)

    remove = speaker.add_activity_listener(on_activity)
    if speaker.active:
        on_activity(True)  # bound in the middle of an utterance

    def unbind() -> None:
        remove()
        listener.set_hold(SPEAKING_HOLD, False)

    return unbind
