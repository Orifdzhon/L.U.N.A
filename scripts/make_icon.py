"""Generate packaging/luna.ico (a white crescent moon on a dark disc). Stdlib only.

Run once; the result is committed: ``python scripts/make_icon.py``.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
BACKGROUND = (30, 34, 54)
MOON = (240, 240, 255)
SAMPLES = 3  # supersampling for smooth edges


def _pixel(x: float, y: float) -> tuple[int, int, int, int]:
    """Colour at the unit-square point (x, y); crescent = big disc minus a shifted disc."""
    if (x - 0.5) ** 2 + (y - 0.5) ** 2 > 0.48**2:
        return (0, 0, 0, 0)
    in_moon = (x - 0.5) ** 2 + (y - 0.5) ** 2 <= 0.30**2
    in_bite = (x - 0.62) ** 2 + (y - 0.42) ** 2 <= 0.26**2
    if in_moon and not in_bite:
        return (*MOON, 255)
    return (*BACKGROUND, 255)


def _png(size: int) -> bytes:
    rows = bytearray()
    for j in range(size):
        rows.append(0)  # filter type: none
        for i in range(size):
            red = green = blue = alpha = 0
            for sj in range(SAMPLES):
                for si in range(SAMPLES):
                    r, g, b, a = _pixel(
                        (i + (si + 0.5) / SAMPLES) / size, (j + (sj + 0.5) / SAMPLES) / size
                    )
                    red += r * a
                    green += g * a
                    blue += b * a
                    alpha += a
            if alpha == 0:
                rows += bytes((0, 0, 0, 0))
            else:
                rows += bytes((red // alpha, green // alpha, blue // alpha, alpha // SAMPLES**2))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def build_ico() -> bytes:
    images = [_png(size) for size in SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, data in zip(SIZES, images, strict=True):
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data
    return bytes(out)


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "packaging" / "luna.ico"
    target.write_bytes(build_ico())
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
