"""Small RGB PNG frame writer; no display server or imaging dependency required."""

from pathlib import Path
import struct
import zlib


def write_frame(game, path: str | Path, scale: int = 16) -> None:
    width, height = int(game.width * scale), int(game.height * scale)
    pixels = bytearray(bytes((23, 29, 34)) * width * height)

    def rectangle(x0, y0, x1, y1, color):
        for y in range(max(0, int(y0 * scale)), min(height, int(y1 * scale))):
            left, right = max(0, int(x0 * scale)), min(width, int(x1 * scale))
            if right > left:
                pixels[(y * width + left) * 3:(y * width + right) * 3] = bytes(color) * (right - left)

    for wall in game.walls:
        rectangle(*wall, (83, 95, 106))
    if not game.door["open"]:
        rectangle(11, 7, 12, 10, (184, 132, 68))
    ex, ey = game.exit_pos
    rectangle(ex - 1, ey - 1, ex + 1, ey + 1, (40, 110, 84))
    for crate in game.crates.values():
        rectangle(crate["x"] - 0.3, crate["y"] - 0.3, crate["x"] + 0.3, crate["y"] + 0.3,
                  (60, 60, 60) if crate["looted"] else (222, 181, 92))
    for guard in game.guards:
        rectangle(guard.x - 0.2, guard.y - 0.2, guard.x + 0.2, guard.y + 0.2,
                  (210, 74, 72) if guard.hp > 0 else (88, 43, 42))
    for grenade in game.grenades:
        rectangle(grenade["x"] - 0.15, grenade["y"] - 0.15,
                  grenade["x"] + 0.15, grenade["y"] + 0.15, (255, 139, 52))
    p = game.player
    rectangle(p.x - 0.22, p.y - 0.22, p.x + 0.22, p.y + 0.22, (88, 186, 232))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    payload = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(payload)
