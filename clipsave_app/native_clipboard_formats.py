from __future__ import annotations

import struct

from PySide6.QtGui import QImage

from .constants import MAX_CLIPBOARD_IMAGE_BYTES, MAX_IMAGE_PIXELS


REGISTERED_IMAGE_FORMATS = frozenset({"PNG", "image/png"})


def validate_registered_image_header(name: str, size: int, header: bytes) -> None:
    if size > MAX_CLIPBOARD_IMAGE_BYTES:
        raise ValueError("Clipboard image payload is too large")
    if name in REGISTERED_IMAGE_FORMATS:
        if (
            len(header) < 24
            or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[12:16] != b"IHDR"
        ):
            raise ValueError("Invalid registered PNG clipboard data")
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        pixels = width * height
        if width <= 0 or height <= 0:
            raise ValueError("Invalid clipboard image dimensions")
        if pixels > MAX_IMAGE_PIXELS or pixels * 4 > MAX_CLIPBOARD_IMAGE_BYTES:
            raise ValueError("Clipboard image dimensions are too large")


def dib_as_bmp(name: str, payload: bytes) -> bytes:
    if len(payload) < 12:
        raise ValueError("Invalid clipboard DIB data")
    header_size = int.from_bytes(payload[:4], "little")
    if name == "DIBV5" and header_size != 124:
        raise ValueError("Invalid clipboard DIBV5 header")
    if header_size == 12:
        width = int.from_bytes(payload[4:6], "little")
        height = int.from_bytes(payload[6:8], "little")
        planes = int.from_bytes(payload[8:10], "little")
        bits_per_pixel = int.from_bytes(payload[10:12], "little")
        compression = 0
        palette_entry_size = 3
        colors_used = 1 << bits_per_pixel if bits_per_pixel <= 8 else 0
        masks_size = 0
    elif header_size in {40, 52, 56, 108, 124} and len(payload) >= header_size:
        width = int.from_bytes(payload[4:8], "little", signed=True)
        height = abs(int.from_bytes(payload[8:12], "little", signed=True))
        planes = int.from_bytes(payload[12:14], "little")
        bits_per_pixel = int.from_bytes(payload[14:16], "little")
        compression = int.from_bytes(payload[16:20], "little")
        colors_used = int.from_bytes(payload[32:36], "little")
        palette_entry_size = 4
        masks_size = 12 if header_size == 40 and compression == 3 else 0
        if header_size == 40 and compression == 6:
            masks_size = 16
        if not colors_used and bits_per_pixel <= 8:
            colors_used = 1 << bits_per_pixel
    else:
        raise ValueError("Unsupported clipboard DIB header")
    if width <= 0 or height <= 0 or planes != 1:
        raise ValueError("Invalid clipboard image dimensions")
    if bits_per_pixel not in {1, 4, 8, 16, 24, 32}:
        raise ValueError("Unsupported clipboard DIB bit depth")
    if compression not in {0, 3, 6}:
        raise ValueError("Unsupported clipboard DIB compression")
    if compression in {3, 6} and bits_per_pixel not in {16, 32}:
        raise ValueError("Invalid clipboard DIB bitfields")
    if compression == 6 and header_size == 52:
        raise ValueError("Invalid clipboard DIB alpha bitfields")
    pixels = width * height
    if pixels > MAX_IMAGE_PIXELS or pixels * 4 > MAX_CLIPBOARD_IMAGE_BYTES:
        raise ValueError("Clipboard image dimensions are too large")
    pixel_offset = header_size + masks_size + colors_used * palette_entry_size
    row_bytes = ((width * bits_per_pixel + 31) // 32) * 4
    if pixel_offset > len(payload) or row_bytes * height > len(payload) - pixel_offset:
        raise ValueError("Truncated clipboard DIB data")
    file_size = len(payload) + 14
    bitmap_header = struct.pack(
        "<2sIHHI",
        b"BM",
        file_size,
        0,
        0,
        pixel_offset + 14,
    )
    return bitmap_header + payload


def decode_native_image(name: str, payload: bytes, *, validate_image) -> QImage:
    if name in REGISTERED_IMAGE_FORMATS:
        validate_registered_image_header(name, len(payload), payload[:32])
        image = QImage.fromData(payload, "PNG")
    elif name in {"DIB", "DIBV5"}:
        image = QImage.fromData(dib_as_bmp(name, payload), "BMP")
    else:
        raise ValueError("Unsupported native clipboard image format")
    if image.isNull():
        raise ValueError("Invalid native clipboard image data")
    validate_image(image)
    return image
