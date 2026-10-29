"""Pure orb animation model (ADR 0011 Feature C): colours, timing and per-frame geometry.

No Qt here, so the motion is unit tested. ``orb_frame(style, t)`` is a pure function of the
seconds ``t`` since the state started. Lengths are relative to the moon radius (1.0 = the
resting moon); the painter scales them to pixels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from luna.ui.shell.state import IconState

OrbMode = Literal["breathe", "pulse", "spin", "ripple", "flash"]

CROSSFADE_S = 0.25  # colour cross-fade when the state changes
MOON_CORE = ("#E8F0FF", "#B8C7E6")  # radial gradient centre -> edge, same in all states


@dataclass(frozen=True)
class OrbStyle:
    color: str  # glow colour, "#RRGGBB"
    period_s: float  # one cycle (breath, pulse, turn, ripple)
    mode: OrbMode
    fps: int  # animation clock rate while visible


@dataclass(frozen=True)
class OrbFrame:
    radius: float  # moon radius scale, ~1.0
    glow_alpha: float  # 0..1 strength of the glow rings
    arc_angle: float  # degrees, start of the THINKING arc (0 for other modes)
    ripple_radii: tuple[float, ...]  # 0..1 progress of each outward ripple (empty if none)


_STYLES = {
    IconState.IDLE: OrbStyle("#8FA3C8", 6.0, "breathe", 15),
    IconState.WAKE: OrbStyle("#6EA8FE", 3.0, "breathe", 15),
    IconState.RECORDING: OrbStyle("#FF5C7A", 0.9, "pulse", 30),
    IconState.THINKING: OrbStyle("#F5B84B", 1.2, "spin", 30),
    IconState.SPEAKING: OrbStyle("#5EE6A8", 1.5, "ripple", 30),
    IconState.ERROR: OrbStyle("#FF4D4F", 0.3, "flash", 30),
}

_RIPPLES = 3
_FLASHES = 2
_ERROR_STEADY_ALPHA = 0.35


def orb_style(state: IconState) -> OrbStyle:
    return _STYLES[state]


def _wave(t: float, period_s: float) -> float:
    """Smooth 0..1..0 oscillation starting at 0 (t = 0)."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * t / period_s)


def orb_frame(style: OrbStyle, t: float) -> OrbFrame:
    """Geometry at ``t`` seconds after the state started (``t`` < 0 is treated as 0)."""
    t = max(t, 0.0)
    mode = style.mode
    if mode == "breathe":
        w = _wave(t, style.period_s)
        return OrbFrame(1.0 + 0.02 * w, 0.30 + 0.30 * w, 0.0, ())
    if mode == "pulse":
        w = _wave(t, style.period_s)
        return OrbFrame(1.0 + 0.06 * w, 0.40 + 0.60 * w, 0.0, ())
    if mode == "spin":
        return OrbFrame(1.0, 0.55, (360.0 * t / style.period_s) % 360.0, ())
    if mode == "ripple":
        base = (t / style.period_s) % 1.0
        radii = tuple((base + k / _RIPPLES) % 1.0 for k in range(_RIPPLES))
        return OrbFrame(1.0, 0.45, 0.0, radii)
    # flash: two quick flashes (one period each: on for the first half), then steady dim red
    if t < _FLASHES * style.period_s:
        on = (t % style.period_s) < style.period_s / 2
        return OrbFrame(1.0, 1.0 if on else 0.15, 0.0, ())
    return OrbFrame(1.0, _ERROR_STEADY_ALPHA, 0.0, ())


def mix_color(a: str, b: str, k: float) -> str:
    """Linear blend of two ``#RRGGBB`` colours (``k`` = 0 gives ``a``, 1 gives ``b``)."""
    k = min(max(k, 0.0), 1.0)
    ca = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * k):02X}" for x, y in zip(ca, cb, strict=True))
