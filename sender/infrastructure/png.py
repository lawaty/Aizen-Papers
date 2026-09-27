"""A minimal pure-Python PNG decoder — the stdlib half of the brand logo.

The invoice PDF has to carry the brand logo, and a PDF 1.4 image XObject wants
*raw samples* — a byte array of RGB triples plus a separate gray array for the
alpha — not a PNG. So the PNG is decoded here, at load time, with ``struct``
and ``zlib`` and nothing else: the project's whole design pitch is a
two-dependency footprint (see ``docs/design.md`` #7), so an image library is
not an option the way a decoder written in place is.

What it supports is exactly what a committed brand asset needs:

- 8-bit, non-interlaced images;
- colour type 6 (RGBA) — the committed logo; 2 (RGB), 3 (palette, with an
  optional ``tRNS``) and 0 (greyscale) cost a few lines each and keep the
  decoder from being a single-asset trap;
- all five row filters (None/Sub/Up/Average/Paeth), reversed with the Paeth
  predictor exactly as the spec writes it.

Everything else — 16-bit depth, interlacing, colour type 4 — is refused with a
:class:`ValueError` naming the problem, the same fail-fast contract as
:mod:`sender.infrastructure.truetype`: a wrong sample is a wrong logo on every
invoice, and a loud failure at load time is the only cheap way to notice.
"""

from __future__ import annotations

import binascii
import struct
import typing
import zlib

#: The file signature every PNG starts with.
_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Channels per pixel, by colour type. 4 (greyscale+alpha) is deliberately
#: absent: nothing produces it here, and half-supporting a format is how a
#: decoder ends up quietly wrong about it.
_CHANNELS: dict[int, int] = {0: 1, 2: 3, 3: 1, 6: 4}


class Image(typing.NamedTuple):
    """One decoded image: its dimensions and its two raw sample arrays."""

    width: int
    height: int
    #: RGB triples, ``width * height * 3`` bytes — what a ``/DeviceRGB``
    #: image XObject draws.
    rgb: bytes
    #: Alpha as ``/DeviceGray`` samples (255 = opaque), ``width * height``
    #: bytes — always present: an image with no alpha channel decodes to a
    #: fully opaque one, so the PDF side has exactly one shape to emit.
    alpha: bytes


def decode_png(data: bytes, source: str = "<png>") -> Image:
    """Decode *data* — a complete PNG file — into raw samples.

    Raises :class:`ValueError`, naming *source* and the problem, on anything
    malformed or unsupported. CRCs are verified, because a corrupt asset is
    exactly the failure this decoder exists to catch loudly.
    """
    if len(data) < 8 or data[:8] != _SIGNATURE:
        raise ValueError(f"PNG {source}: not a PNG (bad signature)")
    pos = 8
    header: bytes | None = None
    palette = b""
    transparency = b""
    compressed = bytearray()
    while pos < len(data):
        if pos + 12 > len(data):
            raise ValueError(f"PNG {source}: truncated chunk header at {pos}")
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4 : pos + 8]
        end = pos + 8 + length
        if end + 4 > len(data):
            raise ValueError(
                f"PNG {source}: chunk {kind!r} claims {length} bytes, past the end of the file"
            )
        body = data[pos + 8 : end]
        (crc,) = struct.unpack_from(">I", data, end)
        if binascii.crc32(data[pos + 4 : end]) & 0xFFFFFFFF != crc:
            raise ValueError(f"PNG {source}: chunk {kind!r} has a bad CRC")
        pos = end + 4
        if kind == b"IHDR":
            header = body
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError(f"PNG {source}: no IHDR chunk")
    if not compressed:
        raise ValueError(f"PNG {source}: no IDAT chunk")
    if len(header) < 13:
        raise ValueError(f"PNG {source}: IHDR is {len(header)} bytes, need 13")
    width, height, depth, colour, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if not width or not height:
        raise ValueError(f"PNG {source}: {width}x{height} image")
    if depth != 8:
        raise ValueError(f"PNG {source}: bit depth {depth} is not supported (need 8)")
    if colour not in _CHANNELS:
        raise ValueError(
            f"PNG {source}: colour type {colour} is not supported (need 0, 2, 3 or 6)"
        )
    if compression or filter_method or interlace:
        raise ValueError(f"PNG {source}: interlaced or non-standard compression/filter method")
    channels = _CHANNELS[colour]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ValueError(f"PNG {source}: IDAT does not inflate: {exc}") from exc
    expected = (stride + 1) * height
    if len(raw) != expected:
        raise ValueError(
            f"PNG {source}: decoded {len(raw)} sample bytes, expected {expected}"
        )
    pixels = _unfilter(raw, stride, height, channels, source)
    return _split(pixels, width, height, colour, palette, transparency, source)


def _unfilter(raw: bytes, stride: int, height: int, channels: int, source: str) -> bytearray:
    """Reverse the per-row filters (0-4) and return the raw scanlines.

    Each row carries one filter byte before its samples, and every filter but
    None predicts each byte from its neighbours — the byte *bpp* places to its
    left in the same row and the byte above it — so reversing is the same
    arithmetic with the prediction added back. ``bpp`` is bytes per pixel, not
    per sample: the spec filters on pixel boundaries.
    """
    bpp = channels
    out = bytearray(stride * height)
    previous = bytes(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        encoded = raw[start + 1 : start + 1 + stride]
        line = bytearray(stride)
        if kind == 0:  # None: the samples are the row as it is
            line[:] = encoded
        elif kind == 1:  # Sub: each byte plus the byte bpp places to its left
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (encoded[i] + left) & 0xFF
        elif kind == 2:  # Up: each byte plus the byte above it
            for i in range(stride):
                line[i] = (encoded[i] + previous[i]) & 0xFF
        elif kind == 3:  # Average: each byte plus the mean of left and above
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (encoded[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:  # Paeth: each byte plus the Paeth predictor of its neighbours
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                above = previous[i]
                corner = previous[i - bpp] if i >= bpp else 0
                line[i] = (encoded[i] + _paeth(left, above, corner)) & 0xFF
        else:
            raise ValueError(f"PNG {source}: row {row} has filter {kind}, not 0-4")
        out[row * stride : (row + 1) * stride] = line
        previous = line
    return out


def _paeth(left: int, above: int, corner: int) -> int:
    """The Paeth predictor, as the PNG spec writes it: nearest of the three."""
    estimate = left + above - corner
    to_left = abs(estimate - left)
    to_above = abs(estimate - above)
    to_corner = abs(estimate - corner)
    if to_left <= to_above and to_left <= to_corner:
        return left
    if to_above <= to_corner:
        return above
    return corner


def _split(
    pixels: bytearray,
    width: int,
    height: int,
    colour: int,
    palette: bytes,
    transparency: bytes,
    source: str,
) -> Image:
    """The sample arrays a PDF image XObject wants: RGB triples and alpha.

    Greyscale and palette images are expanded to RGB here — a ``/DeviceRGB``
    XObject is the one shape the PDF side emits — and an image with no alpha
    channel decodes to a fully opaque one, so the alpha array is always there.
    """
    count = width * height
    if colour == 6:
        rgb = bytearray(count * 3)
        alpha = bytearray(count)
        for pixel in range(count):
            at = pixel * 4
            rgb[pixel * 3 : pixel * 3 + 3] = pixels[at : at + 3]
            alpha[pixel] = pixels[at + 3]
        return Image(width, height, bytes(rgb), bytes(alpha))
    if colour == 2:
        return Image(width, height, bytes(pixels), b"\xff" * count)
    if colour == 0:
        rgb = bytearray(count * 3)
        for pixel in range(count):
            grey = pixels[pixel]
            rgb[pixel * 3 : pixel * 3 + 3] = bytes((grey, grey, grey))
        return Image(width, height, bytes(rgb), b"\xff" * count)
    # Palette: the samples are indices into PLTE, with tRNS holding one alpha
    # byte per index.
    if not palette or len(palette) % 3:
        raise ValueError(
            f"PNG {source}: PLTE is {len(palette)} bytes, not a 3-byte-per-entry table"
        )
    entries = len(palette) // 3
    if len(transparency) > entries:
        raise ValueError(
            f"PNG {source}: tRNS has {len(transparency)} entries but PLTE has {entries}"
        )
    alphas = bytearray(b"\xff" * entries)
    alphas[: len(transparency)] = transparency
    rgb = bytearray(count * 3)
    alpha = bytearray(count)
    for pixel in range(count):
        index = pixels[pixel]
        if index >= entries:
            raise ValueError(
                f"PNG {source}: palette index {index} is past PLTE's {entries} entries"
            )
        at = index * 3
        rgb[pixel * 3 : pixel * 3 + 3] = palette[at : at + 3]
        alpha[pixel] = alphas[index]
    return Image(width, height, bytes(rgb), bytes(alpha))
