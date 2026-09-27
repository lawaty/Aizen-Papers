# Embedded fonts

**Breadcrumb:** [Home](../../../docs/index.md) / [Architecture](../../../docs/architecture.md) / [Infrastructure layer](../../../docs/layers/infrastructure.md)

---

`NotoNaskhArabic-Regular.subset.ttf` is the only font program this project
ships. It exists for one reason: **the invoice PDF has to be in Arabic**, and the
standard-14 fonts it used to draw with are Latin-1 only (see
[design.md](../../../docs/design.md) #8).

## The file

| | |
|---|---|
| Source | `/usr/share/fonts/noto/NotoNaskhArabic-Regular.ttf` (the distro's `noto-fonts` package) |
| Version | `2.021` (name ID 5 of the source: `Version 2.021; ttfautohint (v1.8.4.16-eb64)`) |
| Subset | 87,368 bytes, 1,011 glyphs, `unitsPerEm` 1000 |
| Licence | SIL Open Font License 1.1 — the full text is in [`OFL.txt`](OFL.txt) |
| Licence holder | Copyright 2022 The Noto Project Authors (<https://github.com/notofonts/arabic>) |

A subset, not the original (the original is 247,336 bytes / 1,415 glyphs): the
PDF embeds the whole file on every invoice, and the only characters it can ever
need are Latin, Arabic, and the Arabic Presentation Forms the shaper emits.

## How it was produced

The subsetting tool is **not** a project dependency — it is a one-off build step,
run by hand with a fontTools install:

```
/home/lawaty/.pyenv/versions/3.11.9/bin/pyftsubset \
  /usr/share/fonts/noto/NotoNaskhArabic-Regular.ttf \
  --output-file="sender/infrastructure/fonts/NotoNaskhArabic-Regular.subset.ttf" \
  --unicodes="U+0020-007E,U+00A0,U+0600-06FF,U+FB50-FDFF,U+FE70-FEFF" \
  --layout-features="" --no-hinting --name-IDs="1,2,3,4,6" --drop-tables+=DSIG
```

Done on **2026-09-26**. The flags are load-bearing:

- `U+FE70-FEFF` — the **Presentation Forms-B** block. This is the whole trick:
  the shaper in [`../arabic.py`](../arabic.py) resolves every Arabic character to
  its contextual form itself and then asks the font's `cmap` for that form, so the
  font has to be able to answer for those code points.
- `--layout-features=""` — the shaper does the contextual work itself, and a viewer
  runs no shaping engine, so the file needs no OpenType layout *features*. Note this
  empties GSUB (48 bytes) and GPOS (32 bytes) but does not remove the tables
  themselves: `pyftsubset` only drops a table when told to with `--drop-tables`.
  They now cost 392 bytes with GDEF, 0.4% of the file, and are inert.
- `--no-hinting` — hinting is a raster-time concern; a PDF viewer rasterizes
  without the original hinting program being meaningful.
- `--name-IDs="1,2,3,4,6"` — keeps identity and licence-relevant metadata. Note this
  **drops name ID 5** (the version string), so the version above is read from the
  source font, not from the subset, and the subset's name ID 3 reads
  `2.021;GOOG;NotoNaskhArabic-Regular`.
- `--drop-tables+=DSIG` — the source signature no longer covers the subset.

### Coverage

| Block | Assigned code points present | Missing |
|---|---:|---|
| U+0600–U+06FF (Arabic) | 256 of 256 | none |
| U+FB50–U+FDFF (Presentation Forms-A) | 631 of 631 | none |
| U+FE70–U+FEFF (Presentation Forms-B) | 141 of 144 | U+FE75, U+FEFD, U+FEFE |
| U+00A0 | 1 of 1 | none |
| U+0020–U+007E (ASCII) | 15 of 95 | see below |

The three absent presentation forms — U+FE75, U+FEFD and U+FEFE — are all
**unassigned in Unicode** (they have no character name), so no shaper can ever select
them and there is nothing to fall back for. Every real ligature is present, including
the whole U+FEF5–U+FEFC lam-alef family: U+FEF5 (isolated) is glyph 873 and U+FEFC
(final) is glyph 880.

**ASCII is deliberately nearly all absent.** Only ` !,.0123456789:` — 15 of the 95
printable ASCII code points — are in the subset. That is not an oversight, it is the
reason [`../pdf.py`](../pdf.py) still draws Latin text in the standard-14 Helvetica:
the invoice's digits, dates, invoice numbers and currency codes are Latin-1 text, and
Helvetica is 14 PDF bytes of dictionary instead of 87 KB of font program. A Latin
letter or an accented character therefore reaches Helvetica by necessity, which is
exactly the split [`../arabic.py`](../arabic.py) makes per character.

## Re-licensing is a non-issue, redistribution is fine

The OFL explicitly permits **embedding and redistributing** the font inside other
software ("may be bundled, embedded, redistribute, and sell modified and unmodified
copies of the Font Software"), on one condition: **the licence must accompany every
copy** — hence `OFL.txt` sitting next to the font in this directory. The font is
also never sold by itself; it ships inside a PDF the customer is given. What the
OFL forbids (selling the font alone, relicensing it, using a Reserved Font Name for
a *modified* version) does not apply to an unmodified subset used as an embedded
PDF resource.

## Who reads it

- [`../truetype.py`](../truetype.py) — the pure-stdlib `sfnt` reader that pulls
  `cmap`/`hmtx`/`head`/`hhea`/`maxp` out of this file.
- [`../arabic.py`](../arabic.py) — the shaper and the bidi pass that decide *which*
  code points get asked for.
- [`../pdf.py`](../pdf.py) — embeds the file as a zlib-compressed `FontFile2` under
  an `Identity-H` `CIDFontType2`, so the viewer draws the glyph ids directly and does
  no font lookup of its own.

Nothing else in the project reads fonts, and nothing outside this directory is a
font.
