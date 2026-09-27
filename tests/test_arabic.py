"""Tests for the Arabic shaper and the simplified bidi pass.

Two things are pinned here, and they are pinned *literally*:

- the **shaping** of a handful of words down to the exact presentation form of each
  character, because a wrong join is invisible in a test log and obvious to an
  Egyptian customer. Every expected code point is written out by hand with a
  comment naming its form, so a change in what the shaper picks shows up as a
  failing assertion rather than as a subtly different invoice;
- the **joining classes**, cross-checked against a hardcoded table. The production
  code *derives* them from the presentation forms the Unicode character database
  has, which is the right way to build them and the wrong way to *trust* them:
  this file is the independent statement of what the answer should be.
"""

from __future__ import annotations

import unicodedata

import pytest

from sender.infrastructure import arabic


def codepoints(text: str) -> list[str]:
    """``["U+FE91", …]`` — readable in a failure message, unlike bare hex."""
    return [f"U+{ord(char):04X}" for char in text]


def names(text: str) -> list[str]:
    return [unicodedata.name(char, "U+%04X" % ord(char)) for char in text]


# --- the shaping goldens ---------------------------------------------------------
#
# Written out longhand, one expected code point per character, each with the form
# it is. The shaper resolves every Arabic character to one of these.


def test_fatura_is_shaped_letter_by_letter() -> None:
    # فاتورة — the word on the title of every invoice.
    assert arabic.shape("فاتورة") == "ﻓﺎﺗﻮﺭﺓ"
    assert names(arabic.shape("فاتورة")) == [
        "ARABIC LETTER FEH INITIAL FORM",  # U+FED3 joins forward into the alef
        "ARABIC LETTER ALEF FINAL FORM",  # U+FE8E alef takes the join, ends the chain
        "ARABIC LETTER TEH INITIAL FORM",  # U+FE97 the alef does not join forward
        "ARABIC LETTER WAW FINAL FORM",  # U+FEEE waw ends the chain
        "ARABIC LETTER REH ISOLATED FORM",  # U+FEAD nothing before it joins
        "ARABIC LETTER TEH MARBUTA ISOLATED FORM",  # U+FE93 end of the word
    ]


def test_ala_is_shaped_with_a_medial_lam() -> None:
    # على — lam between two joining letters, so medial, not initial.
    assert arabic.shape("على") == "ﻋﻠﻰ"
    assert names(arabic.shape("على")) == [
        "ARABIC LETTER AIN INITIAL FORM",  # U+FECB
        "ARABIC LETTER LAM MEDIAL FORM",  # U+FEE0 joins the ain and the alef maksura
        "ARABIC LETTER ALEF MAKSURA FINAL FORM",  # U+FEF0 (not a lam-alef ligature)
    ]


def test_lam_alef_alone_becomes_the_isolated_ligature() -> None:
    # لا — lam offers a join forward, the alef accepts it, and neither connects
    # to anything before, so the pair takes the isolated ligature (U+FEFB).
    assert arabic.shape("لا") == "ﻻ"
    assert names(arabic.shape("لا")) == ["ARABIC LIGATURE LAM WITH ALEF ISOLATED FORM"]


def test_lam_alef_after_a_letter_becomes_the_final_ligature() -> None:
    # بلا — the lam now also connects to the beh on its left, so medial, and the
    # ligature is the final one (U+FEFC).
    assert arabic.shape("بلا") == "ﺑﻼ"
    assert names(arabic.shape("بلا")) == [
        "ARABIC LETTER BEH INITIAL FORM",  # U+FE91
        "ARABIC LIGATURE LAM WITH ALEF FINAL FORM",  # U+FEFC
    ]


def test_a_right_joining_letter_does_not_join_forward() -> None:
    # دل — dal is right-joining: it takes the connection on its left and then the
    # chain ends, so the lam behind it is isolated.
    assert arabic.shape("دل") == "ﺩﻝ"
    assert names(arabic.shape("دل")) == [
        "ARABIC LETTER DAL ISOLATED FORM",  # U+FEA9
        "ARABIC LETTER LAM ISOLATED FORM",  # U+FEDD
    ]


def test_a_dual_letter_joins_forward_into_a_right_joining_one() -> None:
    # لد — the mirror image of دل: the lam offers the connection the dal accepts.
    assert arabic.shape("لد") == "ﻟﺪ"
    assert names(arabic.shape("لد")) == [
        "ARABIC LETTER LAM INITIAL FORM",  # U+FEDF
        "ARABIC LETTER DAL FINAL FORM",  # U+FEAA
    ]


def test_every_alef_variant_has_its_own_lam_alef_ligature() -> None:
    """The four alef variants that form a ligature, and the one that does not.

    Derived by name, so this test is what proves the derivation found the right
    ones. A lam *after* an alef is not a ligature: the alef does not offer a join,
    so both letters stand alone.
    """
    for alef, isolated, final in (
        ("ا", "ﻻ", "ﻼ"),  # U+0627 → U+FEFB / U+FEFC
        ("آ", "ﻵ", "ﻶ"),  # U+0622 → U+FEF5 / U+FEF6
        ("أ", "ﻷ", "ﻸ"),  # U+0623 → U+FEF7 / U+FEF8
        ("إ", "ﻹ", "ﻺ"),  # U+0625 → U+FEF9 / U+FEFA
    ):
        assert arabic.shape("ل" + alef) == isolated
        assert arabic.shape("بل" + alef) == "ﺑ" + final
        # Alef first: the alef is right-joining, so it stands alone and the lam
        # behind it gets nothing to connect to either.
        assert arabic.shape(alef + "ل") == arabic.shape(alef) + "ﻝ"
    # Alef maksura (ى) is not in the ligature family: the lam stays a lam, and the
    # pair is two letters, exactly as it is written.
    assert names(arabic.shape("لى")) == [
        "ARABIC LETTER LAM INITIAL FORM",
        "ARABIC LETTER ALEF MAKSURA FINAL FORM",
    ]


def test_shaping_is_idempotent() -> None:
    """Shaping already-shaped text is a no-op.

    Presentation forms sit in the Arabic ranges but have no presentation forms of
    their own, so a second pass passes them through. That keeps a caller that
    shapes twice (a preview that re-renders an already-rendered value) from
    double-shaping into nonsense.
    """
    for text in ("فاتورة", "لا", "بلا", "رقم الفاتورة", "شركة النور للديكور"):
        once = arabic.shape(text)
        assert arabic.shape(once) == once


# --- the lam-alef fallback -------------------------------------------------------


def test_lam_alef_falls_back_to_two_letters_when_the_ligature_is_missing() -> None:
    """A font without the ligature gets the lam and the alef, not a blank.

    The documented fallback (:func:`arabic.shape`): the lam in its own form
    followed by the alef's final form. Two glyphs where the script wants one
    joined pair — visibly wider, never wrong and never missing.
    """
    no_ligature = lambda codepoint: codepoint not in (0xFEFB, 0xFEFC)  # noqa: E731
    assert codepoints(arabic.shape("لا", has_glyph=no_ligature)) == [
        "U+FEDF",  # lam initial
        "U+FE8E",  # alef final
    ]
    assert names(arabic.shape("لا", has_glyph=no_ligature)) == [
        "ARABIC LETTER LAM INITIAL FORM",  # U+FEDF
        "ARABIC LETTER ALEF FINAL FORM",  # U+FE8E
    ]
    # Medial this time, because the beh on its left does offer the join.
    assert codepoints(arabic.shape("بلا", has_glyph=no_ligature)) == [
        "U+FE91",  # beh initial
        "U+FEE0",  # lam medial
        "U+FE8E",  # alef final
    ]
    assert names(arabic.shape("بلا", has_glyph=no_ligature)) == [
        "ARABIC LETTER BEH INITIAL FORM",  # U+FE91
        "ARABIC LETTER LAM MEDIAL FORM",  # U+FEE0
        "ARABIC LETTER ALEF FINAL FORM",  # U+FE8E
    ]


def test_the_fallback_only_triggers_for_the_ligature() -> None:
    """A font that has every letter but not the ligature keeps every letter."""
    def has_all_but_the_ligature(codepoint: int) -> bool:
        return codepoint not in range(0xFEF5, 0xFEFD)

    assert arabic.shape("فاتورة", has_glyph=has_all_but_the_ligature) == arabic.shape("فاتورة")


# --- harakat, pass-throughs, and what is left alone -------------------------------


def test_harakat_are_stripped_because_marks_cannot_be_positioned() -> None:
    """Documented typographic simplification: the marks go, the letters stay.

    Placing a combining mark needs GPOS mark-attachment, which this shaper does
    not have; drawing the mark on its own would put it at the wrong offset. Note
    that the marks are removed *before* the joining is decided, so a word with
    marks in the middle shapes exactly like the same word without them.
    """
    assert names(arabic.shape("شَرَابٌ")) == [
        "ARABIC LETTER SHEEN INITIAL FORM",  # U+FEB7 connects forward to the reh
        "ARABIC LETTER REH FINAL FORM",  # U+FEAE takes that connection, ends the chain
        "ARABIC LETTER ALEF ISOLATED FORM",  # U+FE8D alef is right-joining: it ends it
        "ARABIC LETTER BEH ISOLATED FORM",  # U+FE8F and offers nothing to its left
    ]
    assert arabic.shape("شَرَابٌ") == arabic.shape("شراب")
    assert arabic.is_transparent("َ") and arabic.is_transparent("ٰ")
    assert not arabic.is_transparent("ب")


def test_letters_without_presentation_forms_pass_through_unshaped() -> None:
    """Persian/Urdu letters are drawn, just never joined — and are counted.

    They are not dropped: the subset covers U+0600–U+06FF so the glyph exists.
    :func:`arabic.unshaped_arabic` exists so the caller can warn about it instead
    of shipping a half-joined name silently.
    """
    # پ has no presentation form, so it passes through as itself; the teh marbuta
    # after it gets no connection (a non-joining letter offers none) → initial.
    assert arabic.shape("پت") == "پﺕ"
    assert codepoints(arabic.shape("پت")) == ["U+067E", "U+FE95"]
    assert arabic.unshaped_arabic("پت") == ["پ"]
    assert arabic.unshaped_arabic("فاتورة") == []


def test_latin_and_digits_pass_through_untouched() -> None:
    assert arabic.shape("Invoice 12 (A) - 3.5") == "Invoice 12 (A) - 3.5"
    assert arabic.unshaped_arabic("Invoice 12") == []


# --- the joining-class cross-check -----------------------------------------------
#
# The shaper derives each letter's class from *which forms exist* (see
# arabic._joining_type). That derivation is the right way to build the table, and
# this is the independent statement of what the table must say.


JOINING = {
    "ا": arabic.RIGHT,  # alef — takes a join, never offers one
    "ب": arabic.DUAL,  # beh
    "د": arabic.RIGHT,  # dal
    "ت": arabic.DUAL,  # teh
    "ر": arabic.RIGHT,  # reh
    "و": arabic.RIGHT,  # waw
    "ه": arabic.DUAL,  # heh
    "ل": arabic.DUAL,  # lam
    "م": arabic.DUAL,  # meem
    "ء": arabic.NON_JOINING,  # hamza
    # The rest of the alphabet, so the independent statement covers every
    # letter the shaper can shape, not a tenth of it.
    "آ": arabic.RIGHT,  # alef madda
    "أ": arabic.RIGHT,  # alef hamza above
    "ؤ": arabic.RIGHT,  # waw hamza
    "إ": arabic.RIGHT,  # alef hamza below
    "ئ": arabic.DUAL,  # yeh hamza
    "ة": arabic.RIGHT,  # teh marbuta
    "ث": arabic.DUAL,  # theh
    "ج": arabic.DUAL,  # jeem
    "ح": arabic.DUAL,  # hah
    "خ": arabic.DUAL,  # khah
    "ذ": arabic.RIGHT,  # thal
    "ز": arabic.RIGHT,  # zain
    "س": arabic.DUAL,  # seen
    "ش": arabic.DUAL,  # sheen
    "ص": arabic.DUAL,  # sad
    "ض": arabic.DUAL,  # dad
    "ط": arabic.DUAL,  # tah
    "ظ": arabic.DUAL,  # zah
    "ع": arabic.DUAL,  # ain
    "غ": arabic.DUAL,  # ghain
    "ف": arabic.DUAL,  # feh
    "ق": arabic.DUAL,  # qaf
    "ك": arabic.DUAL,  # kaf
    "ن": arabic.DUAL,  # noon
    "ى": arabic.RIGHT,  # alef maksura
    "ي": arabic.DUAL,  # yeh
}


@pytest.mark.parametrize("char, expected", sorted(JOINING.items()))
def test_derived_joining_classes_match_the_expected_table(char: str, expected: str) -> None:
    assert arabic.joining_type(char) == expected


def test_tatweel_is_dual_joining_because_it_has_no_forms() -> None:
    """The one letter whose class cannot be read off the presentation forms.

    Tatweel *is* the connection. It has no presentation form of its own, so the
    derivation would call it non-joining and every word that uses it to hold a
    join open would break. It is drawn as itself, between the two forms it joins.
    """
    assert arabic.joining_type("ـ") == arabic.DUAL
    assert arabic.presentation_form("ـ", "MEDIAL") is None
    assert names(arabic.shape("بـب")) == [
        "ARABIC LETTER BEH INITIAL FORM",  # U+FE91 offers the join to the tatweel
        "ARABIC TATWEEL",  # U+0640 drawn as itself: it *is* the connection
        "ARABIC LETTER BEH FINAL FORM",  # U+FE90 takes it
    ]


def test_the_presentation_form_table_is_derived_not_hardcoded() -> None:
    """The table is Unicode's, and it is asked for by name.

    Two checks that would both fail if a code point were ever typed in by hand:
    the forms of a letter are exactly the four the names promise, and a letter with
    no forms is absent rather than mapped to something.
    """
    assert [
        (form, arabic.presentation_form("ب", form)) for form in arabic.FORMS
    ] == [
        ("ISOLATED", 0xFE8F),
        ("INITIAL", 0xFE91),
        ("MEDIAL", 0xFE92),
        ("FINAL", 0xFE90),
    ]
    assert arabic.presentation_form("پ", "ISOLATED") is None
    assert arabic.joining_type("پ") == arabic.NON_JOINING


# --- the bidi pass ---------------------------------------------------------------


def test_a_mixed_line_puts_the_number_first_and_the_arabic_after_it() -> None:
    """Visual order: the Latin run is leftmost, the Arabic run is one piece.

    ``فاتورة رقم INV-001`` reads right to left, so the invoice number — last in
    logical order — is the leftmost thing on the line, intact.
    """
    runs = arabic.reorder("فاتورة رقم INV-001")
    assert runs == [
        ("INV-001", False),
        # The space between the Arabic and the number stays with the Arabic run
        # (a span absorbs the neutrals *after* it, never before), which puts the
        # gap in the right place once both runs are drawn left to right.
        ("فاتورة رقم "[::-1], True),
    ]
    # Un-reversing the RTL run gives the logical Arabic back, space included.
    rtl_run = next(run for run, is_rtl in runs if is_rtl)
    assert rtl_run[::-1] == "فاتورة رقم "


def test_an_rtl_run_is_reversed_for_drawing() -> None:
    """The run text a content stream draws is the *visual* order.

    A PDF emits glyphs left to right; an Arabic reader reads them right to left.
    Reversing is what makes one line of code produce a correct line of Arabic.
    """
    (run, is_rtl), = arabic.reorder("فاتورة")
    assert is_rtl is True
    assert run[::-1] == "فاتورة"  # reverse it and you get the word back
    assert run != "فاتورة"  # so this test really pins the reversal


@pytest.mark.parametrize(
    "text",
    [
        "INV-001",
        "1,234.50",
        "14/03/2026",
        "120.00 EGP",
        "A (B) C",
        "Acme Trading",
        "INV-42 (copy)",
    ],
)
def test_a_latin_line_stays_a_single_ltr_run(text: str) -> None:
    """Punctuation inside a Latin run must not split it.

    ``INV-001``, ``1,234.50`` and ``14/03/2026`` are the values an invoice is full
    of; shredding them into single characters would render them reversed, and one
    run also means one string literal to escape.
    """
    assert arabic.reorder(text) == [(text, False)]


def test_digits_inside_arabic_stay_in_reading_order() -> None:
    """``رول فيلكس 45 سم`` — the digits are their own run, unreversed."""
    assert arabic.reorder("رول فيلكس 45 سم") == [
        ("مس ", True),  # "سم " reversed: this is the leftmost thing on the line
        ("45", False),  # the digits keep their own order, wrapped in no brackets
        (" سكليف لور", True),  # " فيلكس رول" reversed, rightmost
    ]
    assert arabic.reorder("45 سم") == [("مس ", True), ("45", False)]


def test_brackets_are_mirrored_inside_an_rtl_run() -> None:
    """A bracket in Arabic has to face the other way to still enclose its text.

    The content follows the bracket in logical order, which places it to the
    bracket's *left* in a right-to-left line, so the opening bracket is drawn as a
    closing one. ``str.translate`` keys on ordinals — a ``{"(": ")"}`` dict would
    silently do nothing at all.
    """
    runs = arabic.reorder("نص (ملاحظة) هنا")
    assert runs == [("انه (ةظحالم) صن", True)]
    # An LTR run is left alone: its brackets already face their content.
    assert arabic.reorder("value [1] here") == [("value [1] here", False)]
    # And a mixed line mirrors only its Arabic runs.
    assert arabic.reorder("شركة (ABC) للديكور") == [
        ("روكيدلل (", True),  # an RTL run with an unbalanced "(" that must be ")"
        ("ABC", False),
        (") ةكرش", True),
    ]


def test_the_base_direction_can_be_forced() -> None:
    """``base_rtl`` is the caller's decision; ``None`` detects the direction.

    Forced ``False``, an Arabic line is laid out and drawn left to right — which is
    the point of the parameter: an Arabic string inside a Latin-only document
    (a transliterated name, a quoted code) is one left-to-right run, not a
    mirrored mess. Forced ``True`` does the same to a Latin line.
    """
    assert arabic.reorder("فاتورة", base_rtl=False) == [("فاتورة", False)]
    assert arabic.reorder("فاتورة") == [("ةروتاف", True)]  # detected: reversed
    assert arabic.reorder("INV-001", base_rtl=True) == [("INV-001", False)]


def test_empty_input_produces_no_runs_and_a_blank_one_produces_no_ink() -> None:
    assert arabic.reorder("") == []
    assert arabic.shape("") == ""
    assert arabic.shape("َُ") == ""  # marks only: nothing is left to draw
    # A blank line is one run of spaces: harmless, and the PDF writer never
    # reaches it because it substitutes a placeholder first.
    assert arabic.reorder("   ") == [("   ", False)]
