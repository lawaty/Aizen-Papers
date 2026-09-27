"""Arabic shaping and bidi reordering, at PDF-generation time.

**Why this module exists.** Three facts, in order:

1. **A PDF viewer does not shape text.** It has no HarfBuzz and no Unicode
   engine: it takes the bytes a content stream gives it, maps them to glyph ids
   through the font's ``cmap``, and draws them left to right. The
   ``init``/``medi``/``fina`` joining behaviour an Arabic reader expects simply
   does not happen.
2. **The standard-14 fonts cannot draw Arabic at all.** They are Latin-1 only,
   so an Arabic customer name used to be dropped from the PDF entirely (see
   ``docs/design.md`` #8).
3. **So the shaping has to be done here, before the bytes are written.** This
   module resolves every Arabic character to its **contextual presentation form**
   (U+FE70–U+FEFF), and :mod:`sender.infrastructure.pdf` maps those code points
   to glyph ids and emits the ids as a hex string through an ``Identity-H``
   ``CIDFontType2`` — so the viewer performs no lookup and no shaping, and the
   joins are already correct in the file.

That is the whole trick, and it is why the font subset keeps the Presentation
Forms-B block (see ``fonts/README.md``).

The tables here are **derived from ``unicodedata.name`` at import time** rather
than hardcoded. A 150-entry literal table of code points is unreviewable and
rots silently when Unicode is updated; the Unicode character database is the
authority, it ships with Python, and a wrong answer is a wrong *name* that
raises at import instead of a wrong invoice. The joining type of a letter is
derived from *which forms exist*, which is also the only definition that is
honest here: a letter that has no initial/medial form cannot join on the left.

What is deliberately **not** implemented, because it needs a real shaping engine:

- **GPOS mark positioning.** Harakat (U+064B–U+065F, U+0670) are stripped: a
  combining mark needs a glyph-positioning table to be placed over its base, and
  drawing the mark on its own would put it at the wrong offset. Dropping the
  mark and keeping the letter is the lesser evil for an invoice, and it is why
  ``شَراب`` prints as ``شرب`` rather than as a mark in the margin.
- **Ligatures beyond lam-alef.** The alef+hamza, and the many Quranic ligatures,
  are left unshaped. Lam-alef is implemented because it is the one ligature
  Arabic readers notice immediately when it is missing (see :func:`shape`).
- **Persian/Urdu letters** (پ 067E, گ 06AF, …) have no presentation forms at
  all, so they pass through unshaped: they still *render* (the subset covers the
  whole Arabic block) but never join. :func:`unshaped_arabic` reports them so the
  caller can say so in the log instead of shipping a silently unjoined name.
"""

from __future__ import annotations

import unicodedata
from typing import Callable

from sender.infrastructure.truetype import load_font

# --- joining classes --------------------------------------------------------------

#: Joins on **both** sides (ب، ت، م، ل …). Takes a connection from the previous
#: letter and offers one to the next.
DUAL = "dual"
#: Joins only to the letter **before** it (ا، د، ر، و …): it accepts a connection
#: and then the chain ends, so it is always in a final or isolated form.
RIGHT = "right"
#: Never joins (ء، Arabic-Indic digits, punctuation, and every letter with no
#: presentation forms).
NON_JOINING = "non-joining"

#: The four contextual forms, in the order the Presentation Forms-B block lists
#: them for a given letter.
FORMS = ("ISOLATED", "INITIAL", "MEDIAL", "FINAL")

#: The one letter whose joining type cannot be read off the presentation forms:
#: tatweel (ـ) is the pure connector — it *is* the connection, it has no forms of
#: its own in U+FE70–U+FEFF, and treating it as non-joining would break every
#: word that uses it to stop a join ("رواــم" vs "روا م").
_TATWEEL = 0x0640

#: Ranges the shaper and the bidi pass both treat as Arabic. The last two are the
#: Arabic Presentation Forms: shaping output, and input to a second shaping pass
#: (which passes it through unchanged, so :func:`shape` is idempotent).
_ARABIC_RANGES = ((0x0600, 0x06FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))

#: Harakat and the superscript alef: base letters minus the marks above them.
#: See the module docstring for why they are dropped rather than drawn.
_TRANSPARENT_RANGES = ((0x064B, 0x065F), (0x0670, 0x0670))


def _in_ranges(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= codepoint <= end for start, end in ranges)


def is_arabic(char: str) -> bool:
    """True for anything in the Arabic blocks, presentation forms included."""
    return _in_ranges(ord(char), _ARABIC_RANGES)


def is_transparent(char: str) -> bool:
    """True for the harakat that :func:`shape` strips (see the module docstring)."""
    return _in_ranges(ord(char), _TRANSPARENT_RANGES)


# --- the presentation-forms table, derived from the Unicode character database -----


def _build_forms() -> dict[int, dict[str, int]]:
    """``base code point -> {form name -> presentation-form code point}``.

    Derived by *naming* the correspondence instead of transcribing it: every
    presentation form is called ``"<the letter's own name> <FORM> FORM"``, so
    scanning the block once and looking the letters up by name gives the whole
    table with no possibility of a typo'd code point.
    """
    by_name: dict[tuple[str, str], int] = {}
    for codepoint in range(0xFE70, 0xFF00):
        try:
            name = unicodedata.name(chr(codepoint))
        except ValueError:
            continue  # an unassigned code point inside the block (U+FE75, …)
        for form in FORMS:
            suffix = f" {form} FORM"
            if name.endswith(suffix):
                by_name.setdefault((name[: -len(suffix)], form), codepoint)
    table: dict[int, dict[str, int]] = {}
    for codepoint in range(0x0600, 0x0700):
        try:
            name = unicodedata.name(chr(codepoint))
        except ValueError:
            continue
        forms = {form: by_name[(name, form)] for form in FORMS if (name, form) in by_name}
        if forms:
            table[codepoint] = forms
    return table


def _joining_type(forms: dict[str, int]) -> str:
    """The joining class implied by *which forms the letter has*.

    Initial **and** medial means it joins on both sides. Isolated **and** final
    means it accepts a connection from the previous letter and ends the chain.
    Isolated alone means it never joins. Anything without forms (Persian letters,
    marks, digits) cannot join at all, and a *joining* letter that somehow lacks
    the medial form is treated as dual-joining rather than losing a connection.
    """
    if "INITIAL" in forms and "MEDIAL" in forms:
        return DUAL
    if "ISOLATED" in forms and "FINAL" in forms:
        return RIGHT
    if "INITIAL" in forms:
        return DUAL
    return NON_JOINING


def _build_lam_alef(forms: dict[int, dict[str, int]]) -> dict[int, tuple[int, int]]:
    """``alef code point -> (isolated ligature, final ligature)``.

    Named the same way: the ligature of LAM with an alef variant is called
    ``"ARABIC LIGATURE LAM WITH <the alef's own name minus 'ARABIC LETTER '>
    <FORM> FORM"``, so the mapping is looked up, not listed. Only the four alef
    variants Unicode defines a ligature for are found; a lam before any other
    character (alef maksura, hamza) is *not* a ligature and is left alone.
    """
    by_name: dict[tuple[str, str], int] = {}
    for codepoint in range(0xFE70, 0xFF00):
        try:
            name = unicodedata.name(chr(codepoint))
        except ValueError:
            continue
        for form in ("ISOLATED", "FINAL"):
            suffix = f" {form} FORM"
            if name.endswith(suffix):
                by_name.setdefault((name[: -len(suffix)], form), codepoint)
    ligatures: dict[int, tuple[int, int]] = {}
    for codepoint, letter_forms in forms.items():
        name = unicodedata.name(chr(codepoint))
        if not name.startswith("ARABIC LETTER "):
            continue
        key = f"ARABIC LIGATURE LAM WITH {name[len('ARABIC LETTER '):]}"
        isolated = by_name.get((key, "ISOLATED"))
        final = by_name.get((key, "FINAL"))
        if isolated and final:
            ligatures[codepoint] = (isolated, final)
    return ligatures


_FORMS_BY_BASE = _build_forms()
_JOINING = {codepoint: _joining_type(forms) for codepoint, forms in _FORMS_BY_BASE.items()}
_JOINING[_TATWEEL] = DUAL
_LAM_ALEF = _build_lam_alef(_FORMS_BY_BASE)
#: The letter(s) that take part in a ligature, found by name like everything else:
#: "ARABIC LETTER LAM". Not a hardcoded 0x0644, for the same reason.
_LAM_CODES = frozenset(
    codepoint
    for codepoint in _FORMS_BY_BASE
    if unicodedata.name(chr(codepoint)).endswith(" LETTER LAM")
)


def joining_type(char: str) -> str:
    """The joining class of *char*: :data:`DUAL`, :data:`RIGHT` or :data:`NON_JOINING`.

    Derived from the presentation forms the letter has (see
    :func:`_joining_type`), with tatweel special-cased as the pure connector.
    """
    return _JOINING.get(ord(char), NON_JOINING)


def presentation_form(char: str, form: str) -> int | None:
    """The contextual *form* of *char* as a code point, or ``None`` if it has none."""
    return _FORMS_BY_BASE.get(ord(char), {}).get(form)


def unshaped_arabic(text: str) -> list[str]:
    """The Arabic-block characters in *text* that have no presentation form.

    Persian/Urdu letters, Arabic digits and punctuation land here. They are not
    dropped — they render, because the subset covers U+0600–U+06FF — but they
    never join, and a reader can see that. Returned in order of occurrence so
    the caller can both count them and name the distinct code points.
    """
    return [
        char
        for char in text
        if is_arabic(char) and ord(char) not in _FORMS_BY_BASE
    ]


# --- the shaper -------------------------------------------------------------------


def shape(text: str, has_glyph: Callable[[int], bool] | None = None) -> str:
    """Return *text* with every Arabic character replaced by its contextual form.

    The result is in **logical order** — the caller reorders it (see
    :func:`reorder`) because a PDF content stream is drawn left to right.

    What happens, in order:

    1. Harakat and the superscript alef are stripped (:func:`is_transparent`),
       because mark positioning needs GPOS and this module has no GPOS.
    2. Every remaining character is asked whether it *offers* a connection to the
       next one and whether it *accepts* one from the previous one; that pair
       picks the form: both → medial, previous only → final, next only →
       initial, neither → isolated.
    3. A LAM in an initial or medial position directly followed by one of the four
       alef variants becomes the **lam-alef ligature** — isolated if the lam was
       initial (it connects to nothing on its left), final if it was medial. Both
       characters are consumed. Lam-alef is the one ligature that must be right:
       written as lam + alef, an Arabic reader sees two letters where the script
       has one, in almost every word beginning "لا" (لا، بلا،蜡烛…). If the font
       has no glyph for the ligature, this falls back to the lam's own form
       followed by the alef's final form, which is *wider* than the ligature but
       never wrong, never missing, and never a blank.
    4. An Arabic character with no presentation form (Persian پ, an Arabic digit,
       punctuation) passes through unchanged, unshaped.
    5. Everything else — Latin, digits, punctuation — passes through unchanged.

    *has_glyph* is the font's "do you have a code point?" predicate, used only for
    the ligature fallback; it defaults to the embedded subset's ``cmap``. It is a
    parameter so the shaper stays testable without a font, and so the fallback can
    be exercised deliberately.
    """
    has_glyph = _in_embedded_font if has_glyph is None else has_glyph
    chars = [char for char in text if not is_transparent(char)]
    if not chars:
        return ""
    out: list[str] = []
    index = 0
    while index < len(chars):
        char = chars[index]
        if ord(char) not in _FORMS_BY_BASE:
            # Persian/Urdu letters, Arabic digits, punctuation: the subset has a
            # glyph for them, so they draw — just never joined.
            out.append(char)
            index += 1
            continue
        joins_previous = index > 0 and _offers(chars[index - 1]) and _accepts(char)
        following = ord(chars[index + 1]) if index + 1 < len(chars) else 0
        if ord(char) in _LAM_CODES and following in _LAM_ALEF:
            form_isolated, form_final = _LAM_ALEF[following]
            wanted = form_final if joins_previous else form_isolated
            if has_glyph(wanted):
                out.append(chr(wanted))
            else:
                # Documented fallback: the lam in its own form, then the alef in
                # its final form. Two glyphs where the script wants one joined
                # pair, but nothing is dropped and nothing is mis-shaped.
                out.append(chr(_FORMS_BY_BASE[ord(char)]["MEDIAL" if joins_previous else "INITIAL"]))
                out.append(chr(_FORMS_BY_BASE[ord(chars[index + 1])]["FINAL"]))
            index += 2
            continue
        joins_next = index + 1 < len(chars) and _accepts(chars[index + 1]) and _offers(char)
        out.append(chr(_select_form(_FORMS_BY_BASE[ord(char)], joins_previous, joins_next)))
        index += 1
    return "".join(out)


def _offers(char: str) -> bool:
    """True if *char* can hand a connection to the character after it."""
    return joining_type(char) == DUAL


def _accepts(char: str) -> bool:
    """True if *char* can take a connection from the character before it."""
    return joining_type(char) in (DUAL, RIGHT)


def _select_form(forms: dict[str, int], joins_previous: bool, joins_next: bool) -> int:
    """The contextual form code point for a letter's join situation.

    A letter can be missing one of the four forms (a *right* letter has no initial
    or medial form at all), so the four cases are resolved against what actually
    exists: a right letter asked for "medial" gets its final form, which is the
    only one that can be drawn.
    """
    for wanted in (
        ("MEDIAL", "FINAL", "INITIAL", "ISOLATED")
        if joins_previous and joins_next
        else ("FINAL", "ISOLATED", "INITIAL", "MEDIAL")
        if joins_previous
        else ("INITIAL", "ISOLATED", "FINAL", "MEDIAL")
        if joins_next
        else ("ISOLATED", "FINAL", "INITIAL", "MEDIAL")
    ):
        if wanted in forms:
            return forms[wanted]
    return next(iter(forms.values()))


def _in_embedded_font(codepoint: int) -> bool:
    return load_font().glyph_id(codepoint) is not None


# --- the bidi pass ----------------------------------------------------------------


#: Bracket pairs, as a translation table: ``str.translate`` keys on *ordinals*,
#: so a plain ``{"(": ")"}`` dict would silently do nothing.
_MIRROR = str.maketrans("()[]{}", ")(][}{")

#: Bidirectional character classes. Deliberately tiny: this is the subset of UAX #9
#: an invoice needs, and every class outside it behaves as a neutral.
_RTL, _LTR, _NUMBER, _WHITESPACE, _NEUTRAL = "R", "L", "EN", "WS", "ON"

#: Classes that keep their own (left-to-right) direction when they are next to
#: each other — a date, an amount, an invoice number must not be torn apart.
_LTR_CLASSES = (_LTR, _NUMBER)
_NEUTRAL_CLASSES = (_NEUTRAL, _WHITESPACE)


def _classify(char: str) -> str:
    """The bidi class of *char*.

    Arabic (with its presentation forms) is ``R``; ASCII letters are ``L``; ASCII
    digits are ``EN``; whitespace is ``WS``; everything else is ``ON``. Western
    digits are the *only* numbers, by decision (see ``docs/design.md`` #8): the
    customer reads ``1,250.00``, not Eastern Arabic numerals.
    """
    if is_arabic(char):
        return _RTL
    if "0" <= char <= "9":
        return _NUMBER
    if "A" <= char <= "Z" or "a" <= char <= "z":
        return _LTR
    if char.isspace():
        return _WHITESPACE
    return _NEUTRAL


def _ltr_spans(text: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` spans of *text* that read left to right as a unit.

    A span starts at a letter or a digit and swallows: more letters and digits,
    plus any run of neutrals and spaces that has a letter or a digit on **both**
    sides inside the span. That is what keeps ``INV-001``, ``1,234.50`` and
    ``14/03/2026`` whole instead of shredding them into single characters.
    """
    kinds = [_classify(char) for char in text]
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(kinds):
        if kinds[index] not in _LTR_CLASSES:
            index += 1
            continue
        start = index
        index += 1
        while index < len(kinds):
            if kinds[index] in _LTR_CLASSES:
                index += 1
                continue
            if kinds[index] in _NEUTRAL_CLASSES:
                ahead = index
                while ahead < len(kinds) and kinds[ahead] in _NEUTRAL_CLASSES:
                    ahead += 1
                if (
                    ahead < len(kinds)
                    and kinds[ahead] in _LTR_CLASSES
                    and kinds[index - 1] in _LTR_CLASSES
                ):
                    index = ahead
                    continue
            break
        spans.append((start, index))
    return spans


def reorder(text: str, base_rtl: bool | None = None) -> list[tuple[str, bool]]:
    """Reorder *text* into visual runs, left to right, for a drawing pass.

    Returns ``[(run_text, is_rtl), …]`` **in the order they must be drawn**: the
    first run is the leftmost one. Inside an RTL run the characters are already
    reversed, because a content stream emits glyphs left to right and an Arabic
    reader reads them right to left; inside an LTR run they are untouched.

    The base direction is RTL when *text* contains any Arabic character (and the
    invoice is Arabic), or whatever *base_rtl* forces. With an RTL base the run
    order is reversed — the first thing in the logical string ends up rightmost —
    and every RTL run is reversed character by character, with its brackets
    mirrored so they still open the right way round.

    This is a *simplified* Unicode Bidi pass: no explicit directional marks, no
    levels, no isolates, and neutrals take the base direction unless they sit
    between two left-to-right characters. That is the whole of what an invoice
    line needs; anything richer would be a real bidi implementation with a real
    bidi bug surface, and nothing here has the data to need one.
    """
    if not text:
        return []
    rtl = any(is_arabic(char) for char in text) if base_rtl is None else bool(base_rtl)
    runs: list[tuple[str, bool]] = []
    index = 0
    for start, end in _ltr_spans(text):
        if start > index:
            runs.append((text[index:start], rtl))
        runs.append((text[start:end], False))
        index = end
    if index < len(text):
        runs.append((text[index:], rtl))
    runs = _merge(runs)
    if not rtl:
        return runs
    visual: list[tuple[str, bool]] = []
    for run, is_rtl in reversed(runs):
        visual.append((_mirror(run[::-1]) if is_rtl else run, is_rtl))
    return visual


def _mirror(text: str) -> str:
    """Swap each bracket for its twin, so it still opens the right way round."""
    return text.translate(_MIRROR)


def _merge(runs: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Join neighbours that share a direction into one run.

    Bidi works in *runs*, and a run is one font and one direction for the
    drawing pass. Without this, ``A (B) C`` in an LTR paragraph would be drawn as
    three text-showing operators with a mirror-flip risk between them instead of
    the one string the author wrote.
    """
    merged: list[tuple[str, bool]] = []
    for run, is_rtl in runs:
        if merged and merged[-1][1] == is_rtl:
            merged[-1] = (merged[-1][0] + run, is_rtl)
        else:
            merged.append((run, is_rtl))
    return [run for run in merged if run[0]]
