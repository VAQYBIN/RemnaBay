"""Файлы логотипов для тестов: настоящие PNG и SVG нужного размера."""

import struct
import zlib


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def make_png(width: int, height: int, *, alpha: bool = True) -> bytes:
    """PNG одного цвета: RGBA (с прозрачностью) или RGB."""
    color_type, pixel = (6, b"\x1f\xa4\xa0\x00") if alpha else (2, b"\x1f\xa4\xa0")
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    rows = b"".join(b"\x00" + pixel * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


def make_svg(width: float = 64, height: float = 64) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="#1fa4a0"/></svg>'
    ).encode()
