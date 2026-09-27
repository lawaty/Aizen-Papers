"""The bundled brand logo, decoded once per process and cached.

Mirrors :func:`sender.infrastructure.truetype.load_font`: the PDF is rendered
inside a poll loop, so the asset is read once and reused, and a missing or
corrupt file is a loud :class:`ValueError` at load time rather than a wrong
logo on every invoice at send time. The decode itself lives in
:mod:`sender.infrastructure.png` — this module is only the asset's address
and its cache.
"""

from __future__ import annotations

from pathlib import Path

from sender.infrastructure.png import Image, decode_png

#: The brand logo: black artwork on a transparent ground, 360×360 8-bit RGBA —
#: a downscaled copy of the master mark, committed at a size the header draws
#: it, so no rescaling happens at render time.
LOGO_PATH = Path(__file__).with_name("assets") / "logo.png"

_logo: Image | None = None


def load_logo(path: Path | str = LOGO_PATH) -> Image:
    """The decoded brand logo, read once per process and cached.

    Raises :class:`ValueError` when the asset is missing or not a decodable
    PNG — the invoice PDF cannot carry the brand without it. Like the font
    loader, the *path* argument exists for tests and is ignored once the
    cache is filled: the asset never changes while the process runs.
    """
    global _logo
    if _logo is None:
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"Brand logo: cannot read {path}: {exc}; the invoice PDF cannot "
                "carry the brand without it"
            ) from exc
        _logo = decode_png(data, source=str(path))
    return _logo
