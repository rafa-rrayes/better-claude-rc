"""Generate PWA icons (pure python, no PIL): warm dark rounded square,
terracotta ❯ prompt glyph + underscore, 2x supersampled for soft edges."""

import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"

BG = (20, 18, 16)
TILE = (30, 27, 24)
ACCENT = (232, 130, 94)
TEXT = (237, 230, 222)


def render(size: int) -> bytes:
    ss = 2
    n = size * ss
    px = [[BG] * n for _ in range(n)]

    def rounded(cx0, cy0, cx1, cy1, r, color):
        for y in range(int(cy0), int(cy1)):
            for x in range(int(cx0), int(cx1)):
                dx = max(cx0 + r - x, x - (cx1 - r), 0)
                dy = max(cy0 + r - y, y - (cy1 - r), 0)
                if dx * dx + dy * dy <= r * r:
                    px[y][x] = color

    def thick_line(x0, y0, x1, y1, w, color):
        length2 = (x1 - x0) ** 2 + (y1 - y0) ** 2 or 1
        minx = int(min(x0, x1) - w - 1); maxx = int(max(x0, x1) + w + 1)
        miny = int(min(y0, y1) - w - 1); maxy = int(max(y0, y1) + w + 1)
        for y in range(max(0, miny), min(n, maxy)):
            for x in range(max(0, minx), min(n, maxx)):
                t = ((x - x0) * (x1 - x0) + (y - y0) * (y1 - y0)) / length2
                t = max(0.0, min(1.0, t))
                dx = x - (x0 + t * (x1 - x0))
                dy = y - (y0 + t * (y1 - y0))
                if dx * dx + dy * dy <= w * w:
                    px[y][x] = color

    # tile
    m = n * 0.06
    rounded(m, m, n - m, n - m, n * 0.20, TILE)

    # ❯ glyph
    w = n * 0.055
    gx = n * 0.30
    gy = n * 0.34
    seg = n * 0.14
    thick_line(gx, gy, gx + seg, gy + seg, w, ACCENT)
    thick_line(gx + seg, gy + seg, gx, gy + 2 * seg, w, ACCENT)
    # underscore
    ux = n * 0.52
    uy = gy + 2 * seg
    thick_line(ux, uy, ux + n * 0.18, uy, w, TEXT)

    # downsample
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = 0
            for dy in range(ss):
                for dx in range(ss):
                    c = px[y * ss + dy][x * ss + dx]
                    r += c[0]; g += c[1]; b += c[2]
            k = ss * ss
            row += bytes((r // k, g // k, b // k))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))

    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


for size in (180, 512):
    out = WEB / f"icon-{size}.png"
    out.write_bytes(render(size))
    print(f"wrote {out} ({out.stat().st_size} bytes)")
