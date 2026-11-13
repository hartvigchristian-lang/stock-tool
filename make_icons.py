"""Draw the home-screen icon (rising chart line on a dark blue tile) as PNGs, stdlib only.

    python3 make_icons.py   → icons/icon-180.png, icons/icon-192.png, icons/icon-512.png
"""
import math
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BG_TOP, BG_BOTTOM = (22, 52, 104), (11, 27, 58)
LINE, FILL, DOT = (86, 204, 132), (86, 204, 132), (255, 255, 255)
# Chart points in 0..1 (x right, y up).
PTS = [(0.18, 0.30), (0.34, 0.42), (0.48, 0.36), (0.62, 0.55), (0.82, 0.74)]


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def line_y(x):
    for (ax, ay), (bx, by) in zip(PTS, PTS[1:]):
        if ax <= x <= bx:
            return ay + (by - ay) * (x - ax) / (bx - ax)
    return None


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size):
    ss = 3  # supersampling for smooth edges
    width = 0.045
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    u = (x + (sx + .5) / ss) / size
                    v = 1 - (y + (sy + .5) / ss) / size
                    c = mix(BG_TOP, BG_BOTTOM, 1 - v)
                    ly = line_y(u)
                    if ly is not None and 0.16 < v < ly:  # soft area under the line
                        c = mix(c, FILL, 0.10 + 0.18 * (v - 0.16) / max(ly - 0.16, 1e-6))
                    d = min(seg_dist(u, v, *a, *b) for a, b in zip(PTS, PTS[1:]))
                    if d < width / 2:
                        c = LINE
                    ex, ey = PTS[-1]
                    if math.hypot(u - ex, v - ey) < 0.055:
                        c = DOT
                    for i in range(3):
                        acc[i] += c[i]
            row += bytes(round(a / (ss * ss)) for a in acc)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = HERE / "icons"
    out.mkdir(exist_ok=True)
    for s in (180, 192, 512):
        (out / f"icon-{s}.png").write_bytes(render(s))
        print("wrote", f"icons/icon-{s}.png")
