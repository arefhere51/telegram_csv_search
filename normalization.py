"""Safe normalization for matching Persian text and phone-like identifiers.

Original CSV values are never replaced. These functions only generate search keys.
"""
from __future__ import annotations

import re
import unicodedata

_ARABIC_TO_PERSIAN = str.maketrans({
    "ي": "ی",
    "ى": "ی",
    "ك": "ک",
    "ة": "ه",
    "ۀ": "ه",
    "ؤ": "و",
    "إ": "ا",
    "أ": "ا",
    "ٱ": "ا",
    "ـ": "",
})
_DIGIT_TO_ASCII = str.maketrans({
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
})
_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_SPACE = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D+")


def normalize_text(value: str | None) -> str:
    """Create a Unicode-stable, Persian-aware search representation."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.translate(_ARABIC_TO_PERSIAN).translate(_DIGIT_TO_ASCII)
    normalized = _DIACRITICS.sub("", normalized)
    # Treat joiners as a word boundary, allowing both "روح‌الله" and "روح الله".
    normalized = normalized.replace("\u200c", " ").replace("\u200d", " ")
    return _SPACE.sub(" ", normalized).strip().casefold()


def normalize_phone(value: str | None, *, iran_mode: bool) -> str:
    """Normalize only the phone search key; raw phone text remains untouched.

    Iran mode maps +98 / 0098 and ten-digit 9xxxxxxxxx values to the conventional
    0xxxxxxxxxx representation. It intentionally does not alter other lengths.
    """
    digits = _NON_DIGIT.sub("", normalize_text(value))
    if not iran_mode:
        return digits
    if digits.startswith("0098") and len(digits) >= 12:
        return "0" + digits[4:]
    if digits.startswith("98") and len(digits) >= 11:
        return "0" + digits[2:]
    if digits.startswith("9") and len(digits) == 10:
        return "0" + digits
    return digits


def is_identifier_query(value: str) -> bool:
    """Return True when a query contains digits plus common identifier punctuation only."""
    return bool(value) and bool(re.fullmatch(r"[0-9۰-۹٠-٩+().\-\s/]+", value))


def fts_prefix_query(normalized_text: str) -> str | None:
    """Build a conservative FTS5 prefix expression without exposing FTS syntax."""
    terms = [term for term in normalized_text.split() if len(term) >= 2]
    if not terms:
        return None
    # Quoted terms make operator characters and punctuation literal; suffix * asks FTS5
    # for an indexed token-prefix match.
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
