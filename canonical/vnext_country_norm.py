"""Pinned country normalization for vNext identity resolution.

The alias tables and legacy normalizer body are copied from the frozen C17/C21
lineage; the known-country set is copied from C23.  Production code must not
import those legacy scripts.  Classification adds only the WP2.1
known/unknown/ambiguous boundary around the copied normalizer.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NORMALIZER_VERSION = "vnext-country-known-neutral-v1"

SOURCE_PROVENANCE: dict[str, tuple[Path, int, str]] = {
    "build_strict_country_aliases": (
        Path("tools/build_strict_canonical.py"),
        25_498,
        "97aedf10755fa80fdef6abc39651da200cc6e19e573ba78b980dfd55bde67d06",
    ),
    "c17_country_aliases_ext": (
        Path("tools/canonical_v2_c17_make_web_polish.py"),
        16_202,
        "13334b47ae4e2c9b4909984b2f1e591249ab384b7c4027668441c4a66edce569",
    ),
    "c21_country_normalizer": (
        Path("tools/canonical_v2_c21_make_web_polish.py"),
        19_534,
        "60c188bbae3a5fadf03d72de919fe5ad41bf0d3a75b9d72d7ce0058222cbbb92",
    ),
    "c23_known_countries": (
        Path("tools/canonical_v2_c23_final.py"),
        15_197,
        "f8adf319e388119c659a8c04435e1a1b4e5f056baf560aea37d9ede50cebb0c5",
    ),
}

# Verbatim copy: tools/build_strict_canonical.py COUNTRY_ALIASES.
COUNTRY_ALIASES = {
    "korea, republic of": "South Korea",
    "republic of korea": "South Korea",
    "south korea": "South Korea",
    "u.s.a.": "United States",
    "usa": "United States",
    "us": "United States",
    "united states of america": "United States",
    "united states": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "united kingdom": "United Kingdom",
    "viet nam": "Vietnam",
    "russian federation": "Russia",
    "czech republic": "Czechia",
}

# Verbatim copy: tools/canonical_v2_c17_make_web_polish.py.
_COUNTRY_ALIASES_EXT = {
    **{k: v for k, v in COUNTRY_ALIASES.items()},
    "korea, democratic people's republic of": "North Korea",
    "korea, dpr": "North Korea",
    "democratic people's republic of korea": "North Korea",
    "north korea": "North Korea",
    "dprk": "North Korea",
    "people's republic of china": "China",
    "china, people's republic of": "China",
    "iran, islamic republic of": "Iran",
    "iran (islamic republic of)": "Iran",
    "macedonia, the former yugoslav republic of": "North Macedonia",
    "republic of north macedonia": "North Macedonia",
    "moldova, republic of": "Moldova",
    "syrian arab republic": "Syria",
    "tanzania, united republic of": "Tanzania",
    "lao people's democratic republic": "Laos",
    "hong kong, china": "Hong Kong",
    "hong kong sar": "Hong Kong",
    "taiwan, province of china": "Taiwan",
    "republic of china": "Taiwan",
    "palestine, state of": "Palestine",
    "vietnam, socialist republic of": "Vietnam",
    "bolivia, plurinational state of": "Bolivia",
    "venezuela, bolivarian republic of": "Venezuela",
    "korea, republic of": "South Korea",
    "republic of korea": "South Korea",
}


def _normalize_country_ext(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = " ".join(text.replace("\u00a0", " ").split()).casefold()
    return _COUNTRY_ALIASES_EXT.get(key, text)


# Verbatim copy: tools/canonical_v2_c21_make_web_polish.py.
_NATIVE_COUNTRY_ALIASES = {
    "españa": "Spain",
    "italia": "Italy",
    "the netherlands": "Netherlands",
    "nederland": "Netherlands",
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    "deutschland": "Germany",
    "österreich": "Austria",
    "osterreich": "Austria",
    "schweiz": "Switzerland",
    "suisse": "Switzerland",
    "svizzera": "Switzerland",
    "svizra": "Switzerland",
    "sverige": "Sweden",
    "norge": "Norway",
    "danmark": "Denmark",
    "suomi": "Finland",
    "magyarország": "Hungary",
    "magyarorszag": "Hungary",
    "polska": "Poland",
    "česko": "Czechia",
    "cesko": "Czechia",
    "česká republika": "Czechia",
    "hrvatska": "Croatia",
    "slovensko": "Slovakia",
    "slovenija": "Slovenia",
    "ελλάδα": "Greece",
    "ellada": "Greece",
    "belgique": "Belgium",
    "belgïe": "Belgium",
    "belgië": "Belgium",
    "belgie": "Belgium",
    "luxemburg": "Luxembourg",
    "lëtzebuerg": "Luxembourg",
    "ísland": "Iceland",
    "island": "Iceland",
    "ireland": "Ireland",
    "éire": "Ireland",
    "eire": "Ireland",
    "portugal": "Portugal",
    "中国": "China",
    "中華人民共和國": "China",
    "中華民國": "Taiwan",
    "台灣": "Taiwan",
    "日本": "Japan",
    "한국": "South Korea",
    "대한민국": "South Korea",
    "조선": "North Korea",
    "조선민주주의인민공화국": "North Korea",
    "ประเทศไทย": "Thailand",
    "việt nam": "Vietnam",
    "viet nam": "Vietnam",
    "الإمارات": "United Arab Emirates",
    "الإمارات العربية المتحدة": "United Arab Emirates",
    "السعودية": "Saudi Arabia",
    "المملكة العربية السعودية": "Saudi Arabia",
    "مصر": "Egypt",
    "brasil": "Brazil",
    "méxico": "Mexico",
    "mexico": "Mexico",
    "россия": "Russia",
    "российская федерация": "Russia",
    "україна": "Ukraine",
    "ukraina": "Ukraine",
    "rumänien": "Romania",
    "românia": "Romania",
}


def normalize_country_full(value: Any) -> str | None:
    """Verbatim C21 normalizer body, with the copied local C17 helper."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text_nfc = unicodedata.normalize("NFC", text)
    key = " ".join(text_nfc.split()).casefold()
    if key in _NATIVE_COUNTRY_ALIASES:
        return _NATIVE_COUNTRY_ALIASES[key]
    return _normalize_country_ext(text)


# Verbatim copy: tools/canonical_v2_c23_final.py _KNOWN_COUNTRIES.
KNOWN_COUNTRIES = set(_NATIVE_COUNTRY_ALIASES.values())
KNOWN_COUNTRIES.update({
    "Spain", "Italy", "France", "Germany", "Netherlands", "Belgium", "United Kingdom",
    "United States", "Canada", "Mexico", "Brazil", "Argentina", "Chile", "Peru",
    "Japan", "China", "South Korea", "North Korea", "Taiwan", "Hong Kong",
    "Vietnam", "Thailand", "Indonesia", "Malaysia", "Philippines", "Singapore",
    "India", "Pakistan", "Bangladesh", "Sri Lanka",
    "Australia", "New Zealand",
    "Russia", "Ukraine", "Poland", "Czechia", "Slovakia", "Hungary", "Romania",
    "Bulgaria", "Croatia", "Serbia", "Slovenia",
    "Sweden", "Norway", "Denmark", "Finland", "Iceland", "Ireland",
    "Austria", "Switzerland", "Portugal", "Greece", "Turkey", "Cyprus",
    "Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain",
    "Iran", "Iraq", "Israel", "Jordan", "Lebanon", "Egypt", "Morocco", "Tunisia",
    "South Africa", "Nigeria", "Kenya", "Ethiopia", "Ghana",
    "Estonia", "Latvia", "Lithuania", "Belarus", "Moldova",
    "Albania", "North Macedonia", "Montenegro", "Bosnia and Herzegovina",
    "Luxembourg", "Liechtenstein", "Monaco", "Malta", "Andorra", "San Marino",
})

# WP2.1 policy addition.  This is not an alias expansion.
AMBIGUOUS_COUNTRY_KEYS = frozenset({"georgia"})


@dataclass(frozen=True)
class CountryNormalization:
    raw: str
    status: str
    canonical: str | None
    reason: str


def classify_country(value: Any) -> CountryNormalization:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return CountryNormalization(raw, "unknown", None, "empty")
    key = " ".join(unicodedata.normalize("NFC", raw).split()).casefold()
    if key in AMBIGUOUS_COUNTRY_KEYS:
        return CountryNormalization(raw, "ambiguous", None, "fixed_ambiguous_key")
    canonical = normalize_country_full(raw)
    if canonical in KNOWN_COUNTRIES:
        return CountryNormalization(raw, "known", canonical, "legacy_known_country")
    return CountryNormalization(raw, "unknown", None, "not_in_pinned_known_country_set")


COUNTRY_NORMALIZER_SPEC = {
    "version": NORMALIZER_VERSION,
    "base_aliases": dict(sorted(COUNTRY_ALIASES.items())),
    "extended_aliases": dict(sorted(_COUNTRY_ALIASES_EXT.items())),
    "native_aliases": dict(sorted(_NATIVE_COUNTRY_ALIASES.items())),
    "known_countries": sorted(KNOWN_COUNTRIES),
    "ambiguous_country_keys": sorted(AMBIGUOUS_COUNTRY_KEYS),
    "unknown_and_ambiguous_policy": "neutral_no_positive_no_conflict",
    "provenance": {
        role: {"path": path.as_posix(), "size_bytes": size, "sha256": digest}
        for role, (path, size, digest) in sorted(SOURCE_PROVENANCE.items())
    },
}


__all__ = [
    "AMBIGUOUS_COUNTRY_KEYS", "COUNTRY_NORMALIZER_SPEC", "CountryNormalization",
    "KNOWN_COUNTRIES", "NORMALIZER_VERSION", "SOURCE_PROVENANCE", "classify_country",
    "normalize_country_full",
]
