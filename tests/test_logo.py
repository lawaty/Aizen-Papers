"""Tests for the bundled-logo loader and the stdlib PNG decoder behind it.

Two halves, like ``tests/test_truetype.py``:

- the **committed asset**, decoded for real: the facts the PDF writer depends
  on — 360×360, black artwork on a transparent ground — asserted from the
  decoded samples rather than from the file's header;
- **hand-built PNGs** for the paths the committed asset never takes: every row
  filter, the RGB / greyscale / palette colour types, and the malformed-input
  errors. Those branches exist in the decoder and would otherwise be untested
  code that only runs when it is already broken.
"""

from __future__ import annotations

import binascii
import struct
import zlib

import pytest

from sender.infrastructure import logo
from sender.infrastructure.logo import LOGO_PATH, load_logo
from sender.infrastructure.png import decode_png

LOGO = load_logo()


def test_the_logo_is_the_committed_file() -> None:
    """The loader embeds *this* file, and decodes it once per process."""
    assert LOGO_PATH.name == "logo.png"
    assert LOGO_PATH.parent.name == "assets"
    assert LOGO_PATH.is_file()
    assert load_logo() is LOGO
    assert load_logo("/nonexistent/logo.png") is LOGO  # cache hit, no read


def test_the_committed_logo_decodes_to_360x360_samples() -> None:
    """A DeviceRGB XObject of the logo draws 388,800 bytes; its mask 129,600."""
    assert (LOGO.width, LOGO.height) == (360, 360)
    assert len(LOGO.rgb) == 360 * 360 * 3
    assert len(LOGO.alpha) == 360 * 360
    # Black artwork on a transparent ground: both are really there, and an
    # opaque sample is black — anti-aliased edges included, every channel
    # within 32 of zero — a monochrome brand that survives a grayscale print,
    # never a colour that would collapse in one.
    assert 0 in LOGO.alpha and 255 in LOGO.alpha
    opaque = [i for i in range(0, len(LOGO.alpha), 97) if LOGO.alpha[i] == 255]
    assert opaque
    for i in opaque:
        assert max(LOGO.rgb[3 * i : 3 * i + 3]) <= 32


def test_a_missing_logo_file_is_reported_with_its_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one failure an operator has to act on names the file it could not open."""
    monkeypatch.setattr(logo, "_logo", None)
    with pytest.raises(ValueError, match="cannot read .*logo\\.png"):
        load_logo("/nonexistent/logo.png")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not a png at all", "not a PNG"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 5, "truncated chunk header"),
        (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + b"\x00" * 13
            + b"\x00" * 4,  # a CRC field that cannot match the body above it
            "bad CRC",
        ),
    ],
)
def test_a_corrupt_logo_file_is_refused_at_load_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path, payload: bytes, message: str
) -> None:
    """A corrupt asset is a loud failure, not a wrong logo on every invoice."""
    monkeypatch.setattr(logo, "_logo", None)
    broken = tmp_path / "logo.png"
    broken.write_bytes(payload)
    with pytest.raises(ValueError, match=message):
        load_logo(broken)


# --- hand-built PNGs --------------------------------------------------------------


def _chunk(kind: bytes, body: bytes) -> bytes:
    """One PNG chunk: length, type, body, CRC — the CRC the decoder verifies."""
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", binascii.crc32(kind + body) & 0xFFFFFFFF)
    )


def _bare(chunks: list[bytes]) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _png(
    width: int,
    height: int,
    colour: int,
    rows: list[bytes],
    palette: bytes = b"",
    transparency: bytes = b"",
) -> bytes:
    """A minimal valid PNG. *rows* are raw scanlines *with* their filter bytes."""
    chunks = [_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))]
    if palette:
        chunks.append(_chunk(b"PLTE", palette))
    if transparency:
        chunks.append(_chunk(b"tRNS", transparency))
    chunks.append(_chunk(b"IDAT", zlib.compress(b"".join(rows))))
    chunks.append(_chunk(b"IEND", b""))
    return _bare(chunks)


def _paeth(left: int, above: int, corner: int) -> int:
    """The predictor, restated here so a wrong one in the module fails loudly."""
    estimate = left + above - corner
    to_left = abs(estimate - left)
    to_above = abs(estimate - above)
    to_corner = abs(estimate - corner)
    if to_left <= to_above and to_left <= to_corner:
        return left
    if to_above <= to_corner:
        return above
    return corner


def _filtered(raw: bytes, previous: bytes, kind: int, bpp: int) -> bytes:
    """One scanline filtered *forward*, the way a PNG encoder would."""
    if kind == 0:
        return raw
    out = bytearray(len(raw))
    for i in range(len(raw)):
        left = raw[i - bpp] if i >= bpp else 0
        above = previous[i]
        corner = previous[i - bpp] if i >= bpp else 0
        if kind == 1:
            out[i] = (raw[i] - left) & 0xFF
        elif kind == 2:
            out[i] = (raw[i] - above) & 0xFF
        elif kind == 3:
            out[i] = (raw[i] - ((left + above) >> 1)) & 0xFF
        else:
            out[i] = (raw[i] - _paeth(left, above, corner)) & 0xFF
    return bytes(out)


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4])
def test_every_row_filter_round_trips(kind: int) -> None:
    """None/Sub/Up/Average/Paeth reversed exactly: the decoder's core arithmetic.

    Each PNG is built with the filter applied forward, row by row, over
    non-trivial samples — so a decoder that predicts from the wrong neighbour,
    or filters on sample instead of pixel boundaries, returns wrong bytes.
    """
    raw_rows = [
        bytes((10, 20, 30, 0, 40, 50, 60, 128)),
        bytes((70, 80, 90, 255, 100, 110, 120, 200)),
        bytes((5, 5, 5, 5, 250, 200, 150, 100)),
    ]
    rows = []
    previous = bytes(8)
    for raw in raw_rows:
        rows.append(bytes([kind]) + _filtered(raw, previous, kind, 4))
        previous = raw
    image = decode_png(_png(2, 3, 6, rows), source="<filters>")
    flat = b"".join(raw_rows)
    assert image.rgb == bytes(flat[i] for i in range(len(flat)) if i % 4 != 3)
    assert image.alpha == flat[3::4]


def test_rgb_and_greyscale_images_expand_to_opaque_device_rgb() -> None:
    """Colour types 2 and 0: the samples as RGB, and a fully opaque alpha."""
    rgb = decode_png(_png(1, 1, 2, [b"\x00" + bytes((18, 52, 86))]), source="<rgb>")
    assert rgb.rgb == bytes((18, 52, 86))
    assert rgb.alpha == b"\xff"
    grey = decode_png(_png(1, 2, 0, [b"\x00\x00", b"\x00\xff"]), source="<grey>")
    assert grey.rgb == bytes((0, 0, 0, 255, 255, 255))
    assert grey.alpha == b"\xff\xff"


def test_a_palette_image_expands_through_plte_and_trns() -> None:
    """Colour type 3: indices through PLTE, alpha through tRNS — and an
    image with no tRNS decodes fully opaque."""
    palette = bytes((255, 0, 0, 0, 0, 255))  # red, blue
    image = decode_png(
        _png(1, 2, 3, [b"\x00\x00", b"\x00\x01"], palette=palette, transparency=b"\x80"),
        source="<palette>",
    )
    assert image.rgb == bytes((255, 0, 0, 0, 0, 255))
    assert image.alpha == bytes((0x80, 0xFF))
    plain = decode_png(_png(1, 1, 3, [b"\x00\x00"], palette=palette), source="<plain>")
    assert plain.alpha == b"\xff"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 5, "truncated chunk header"),
        (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + b"\x00" * 13
            + b"\x00" * 4,  # a CRC field that cannot match the body above it
            "bad CRC",
        ),
        (
            # The chunk is *built* as IDAx, so its CRC is right for the wrong
            # type: the decoder sees a well-formed unknown chunk, not corruption.
            _bare(
                [
                    _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0)),
                    _chunk(b"IDAx", zlib.compress(b"\x00" * 9)),
                    _chunk(b"IEND", b""),
                ]
            ),
            "no IDAT chunk",
        ),
        (
            _bare(
                [
                    _chunk(b"IDAT", zlib.compress(b"\x00" * 9)),
                    _chunk(b"IEND", b""),
                ]
            ),
            "no IHDR chunk",
        ),
        (
            # Colour 3, not 6: the decoder ignores PLTE for a truecolour image,
            # as the spec says, so only a palette image reaches the check.
            _png(2, 1, 3, [b"\x00" * 3], palette=bytes(4)),
            "PLTE is 4 bytes",
        ),
        (
            _bare(
                [
                    _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 16, 6, 0, 0, 0)),
                    _chunk(b"IDAT", zlib.compress(b"\x00" * 9)),
                    _chunk(b"IEND", b""),
                ]
            ),
            "bit depth 16",
        ),
        (
            _png(2, 1, 4, [b"\x00" * 3]),
            "colour type 4",
        ),
        (
            # The IHDR is built with the interlace byte set, CRC and all: a
            # rewritten body would fail the CRC check before reaching the test.
            _bare(
                [
                    _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 1)),
                    _chunk(b"IDAT", zlib.compress(b"\x00" * 9)),
                    _chunk(b"IEND", b""),
                ]
            ),
            "interlaced",
        ),
        (
            _png(2, 1, 6, [b"\x00" * 5]),
            "sample bytes",
        ),
        (
            # The junk is a correctly framed IDAT: same length field, CRC to
            # match, so only zlib itself can be what objects.
            _bare(
                [
                    _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0)),
                    _chunk(b"IDAT", b"not zlib at all"),
                    _chunk(b"IEND", b""),
                ]
            ),
            "does not inflate",
        ),
        (
            _png(1, 1, 6, [b"\x05\x00\x00\x00\x00"]),
            "filter 5",
        ),
        (
            _png(1, 1, 3, [b"\x00\x01"], palette=bytes((1, 2, 3))),
            "palette index",
        ),
        (
            _png(1, 1, 3, [b"\x00\x00"], palette=bytes((1, 2, 3)), transparency=b"\x00\xff"),
            "tRNS has 2 entries",
        ),
    ],
)
def test_malformed_pngs_raise_value_error_with_a_reason(data: bytes, message: str) -> None:
    """A truncated or wrong file is a ``ValueError`` that says why, never an index error."""
    with pytest.raises(ValueError, match=message):
        decode_png(data, source="<malformed>")
