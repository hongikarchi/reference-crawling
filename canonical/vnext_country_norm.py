"""Pinned country normalization for vNext identity resolution.

The alias tables and legacy normalizer body are copied from the frozen C17/C21
lineage; the known-country set is copied from C23.  Production code must not
import those legacy scripts.  WP2.2 adds a pinned, static ISO 3166-1
English-name overlay.  The table is embedded deliberately: candidate
production must not depend on an installed locale package or mutable lookup.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NORMALIZER_VERSION = "vnext-country-iso3166-known-neutral-v2"

ISO_SOURCE_PROVENANCE = {
    "standard": "ISO 3166-1:2020 edition 4",
    "standard_url": "https://www.iso.org/standard/72482.html",
    "snapshot_project": "pycountry 26.2.16 / iso-codes 4.20.1",
    "snapshot_source_path": "src/pycountry/databases/iso3166-1.json",
    "snapshot_wheel_path": "pycountry/databases/iso3166-1.json",
    "snapshot_size_bytes": 43_284,
    "snapshot_sha256": "f01b812b57fba9f31ff621bf33e7c7570a01964dbeb5be2167e94decf538c89f",
    "embedded_payload_format": "alpha_2|name|official_name-or-empty|common_name-or-empty\\n",
    "embedded_payload_size_bytes": 8_188,
    "embedded_payload_sha256": "62bc1e3a3c3e24b1362619aad2df4c7974226432e6051ff540fc56da4d4fa85c",
}

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


# Exact English name fields from the pinned upstream snapshot described in
# ISO_SOURCE_PROVENANCE below. Empty fields mean the source record did not
# publish that optional name. Keeping these separate from product-facing
# canonical labels makes both standard lineage and normalization auditable.
_ISO_3166_1_UPSTREAM_NAMES = """
AW|Aruba||\nAF|Afghanistan|Islamic Republic of Afghanistan|\nAO|Angola|Republic of Angola|\nAI|Anguilla||\nAX|Åland Islands||\nAL|Albania|Republic of Albania|\nAD|Andorra|Principality of Andorra|\nAE|United Arab Emirates||\nAR|Argentina|Argentine Republic|\nAM|Armenia|Republic of Armenia|\nAS|American Samoa||\nAQ|Antarctica||\nTF|French Southern Territories||\nAG|Antigua and Barbuda||\nAU|Australia||\nAT|Austria|Republic of Austria|\nAZ|Azerbaijan|Republic of Azerbaijan|\nBI|Burundi|Republic of Burundi|\nBE|Belgium|Kingdom of Belgium|\nBJ|Benin|Republic of Benin|\nBQ|Bonaire, Sint Eustatius and Saba|Bonaire, Sint Eustatius and Saba|\nBF|Burkina Faso||\nBD|Bangladesh|People's Republic of Bangladesh|\nBG|Bulgaria|Republic of Bulgaria|\nBH|Bahrain|Kingdom of Bahrain|\nBS|Bahamas|Commonwealth of the Bahamas|\nBA|Bosnia and Herzegovina|Republic of Bosnia and Herzegovina|\nBL|Saint Barthélemy||\nBY|Belarus|Republic of Belarus|\nBZ|Belize||\nBM|Bermuda||\nBO|Bolivia, Plurinational State of|Plurinational State of Bolivia|Bolivia\nBR|Brazil|Federative Republic of Brazil|\nBB|Barbados||\nBN|Brunei Darussalam||\nBT|Bhutan|Kingdom of Bhutan|\nBV|Bouvet Island||\nBW|Botswana|Republic of Botswana|\nCF|Central African Republic||\nCA|Canada||\nCC|Cocos (Keeling) Islands||\nCH|Switzerland|Swiss Confederation|\nCL|Chile|Republic of Chile|\nCN|China|People's Republic of China|\nCI|Côte d'Ivoire|Republic of Côte d'Ivoire|\nCM|Cameroon|Republic of Cameroon|\nCD|Congo, The Democratic Republic of the||\nCG|Congo|Republic of the Congo|\nCK|Cook Islands||\nCO|Colombia|Republic of Colombia|\nKM|Comoros|Union of the Comoros|\nCV|Cabo Verde|Republic of Cabo Verde|\nCR|Costa Rica|Republic of Costa Rica|\nCU|Cuba|Republic of Cuba|\nCW|Curaçao|Curaçao|\nCX|Christmas Island||\nKY|Cayman Islands||\nCY|Cyprus|Republic of Cyprus|\nCZ|Czechia|Czech Republic|\nDE|Germany|Federal Republic of Germany|\nDJ|Djibouti|Republic of Djibouti|\nDM|Dominica|Commonwealth of Dominica|\nDK|Denmark|Kingdom of Denmark|\nDO|Dominican Republic||\nDZ|Algeria|People's Democratic Republic of Algeria|\nEC|Ecuador|Republic of Ecuador|\nEG|Egypt|Arab Republic of Egypt|\nER|Eritrea|the State of Eritrea|\nEH|Western Sahara||\nES|Spain|Kingdom of Spain|\nEE|Estonia|Republic of Estonia|\nET|Ethiopia|Federal Democratic Republic of Ethiopia|\nFI|Finland|Republic of Finland|\nFJ|Fiji|Republic of Fiji|\nFK|Falkland Islands (Malvinas)||\nFR|France|French Republic|\nFO|Faroe Islands||\nFM|Micronesia, Federated States of|Federated States of Micronesia|\nGA|Gabon|Gabonese Republic|\nGB|United Kingdom|United Kingdom of Great Britain and Northern Ireland|\nGE|Georgia||\nGG|Guernsey||\nGH|Ghana|Republic of Ghana|\nGI|Gibraltar||\nGN|Guinea|Republic of Guinea|\nGP|Guadeloupe||\nGM|Gambia|Republic of the Gambia|\nGW|Guinea-Bissau|Republic of Guinea-Bissau|\nGQ|Equatorial Guinea|Republic of Equatorial Guinea|\nGR|Greece|Hellenic Republic|\nGD|Grenada||\nGL|Greenland||\nGT|Guatemala|Republic of Guatemala|\nGF|French Guiana||\nGU|Guam||\nGY|Guyana|Republic of Guyana|\nHK|Hong Kong|Hong Kong Special Administrative Region of China|\nHM|Heard Island and McDonald Islands||\nHN|Honduras|Republic of Honduras|\nHR|Croatia|Republic of Croatia|\nHT|Haiti|Republic of Haiti|\nHU|Hungary|Hungary|\nID|Indonesia|Republic of Indonesia|\nIM|Isle of Man||\nIN|India|Republic of India|\nIO|British Indian Ocean Territory||\nIE|Ireland||\nIR|Iran, Islamic Republic of|Islamic Republic of Iran|Iran\nIQ|Iraq|Republic of Iraq|\nIS|Iceland|Republic of Iceland|\nIL|Israel|State of Israel|\nIT|Italy|Italian Republic|\nJM|Jamaica||\nJE|Jersey||\nJO|Jordan|Hashemite Kingdom of Jordan|\nJP|Japan||\nKZ|Kazakhstan|Republic of Kazakhstan|\nKE|Kenya|Republic of Kenya|\nKG|Kyrgyzstan|Kyrgyz Republic|\nKH|Cambodia|Kingdom of Cambodia|\nKI|Kiribati|Republic of Kiribati|\nKN|Saint Kitts and Nevis||\nKR|Korea, Republic of||South Korea\nKW|Kuwait|State of Kuwait|\nLA|Lao People's Democratic Republic||Laos\nLB|Lebanon|Lebanese Republic|\nLR|Liberia|Republic of Liberia|\nLY|Libya|Libya|\nLC|Saint Lucia||\nLI|Liechtenstein|Principality of Liechtenstein|\nLK|Sri Lanka|Democratic Socialist Republic of Sri Lanka|\nLS|Lesotho|Kingdom of Lesotho|\nLT|Lithuania|Republic of Lithuania|\nLU|Luxembourg|Grand Duchy of Luxembourg|\nLV|Latvia|Republic of Latvia|\nMO|Macao|Macao Special Administrative Region of China|\nMF|Saint Martin (French part)||\nMA|Morocco|Kingdom of Morocco|\nMC|Monaco|Principality of Monaco|\nMD|Moldova, Republic of|Republic of Moldova|Moldova\nMG|Madagascar|Republic of Madagascar|\nMV|Maldives|Republic of Maldives|\nMX|Mexico|United Mexican States|\nMH|Marshall Islands|Republic of the Marshall Islands|\nMK|North Macedonia|Republic of North Macedonia|\nML|Mali|Republic of Mali|\nMT|Malta|Republic of Malta|\nMM|Myanmar|Republic of Myanmar|\nME|Montenegro|Montenegro|\nMN|Mongolia||\nMP|Northern Mariana Islands|Commonwealth of the Northern Mariana Islands|\nMZ|Mozambique|Republic of Mozambique|\nMR|Mauritania|Islamic Republic of Mauritania|\nMS|Montserrat||\nMQ|Martinique||\nMU|Mauritius|Republic of Mauritius|\nMW|Malawi|Republic of Malawi|\nMY|Malaysia||\nYT|Mayotte||\nNA|Namibia|Republic of Namibia|\nNC|New Caledonia||\nNE|Niger|Republic of the Niger|\nNF|Norfolk Island||\nNG|Nigeria|Federal Republic of Nigeria|\nNI|Nicaragua|Republic of Nicaragua|\nNU|Niue|Niue|\nNL|Netherlands|Kingdom of the Netherlands|\nNO|Norway|Kingdom of Norway|\nNP|Nepal|Federal Democratic Republic of Nepal|\nNR|Nauru|Republic of Nauru|\nNZ|New Zealand||\nOM|Oman|Sultanate of Oman|\nPK|Pakistan|Islamic Republic of Pakistan|\nPA|Panama|Republic of Panama|\nPN|Pitcairn||\nPE|Peru|Republic of Peru|\nPH|Philippines|Republic of the Philippines|\nPW|Palau|Republic of Palau|\nPG|Papua New Guinea|Independent State of Papua New Guinea|\nPL|Poland|Republic of Poland|\nPR|Puerto Rico||\nKP|Korea, Democratic People's Republic of|Democratic People's Republic of Korea|North Korea\nPT|Portugal|Portuguese Republic|\nPY|Paraguay|Republic of Paraguay|\nPS|Palestine, State of|the State of Palestine|\nPF|French Polynesia||\nQA|Qatar|State of Qatar|\nRE|Réunion||\nRO|Romania||\nRU|Russian Federation||\nRW|Rwanda|Rwandese Republic|\nSA|Saudi Arabia|Kingdom of Saudi Arabia|\nSD|Sudan|Republic of the Sudan|\nSN|Senegal|Republic of Senegal|\nSG|Singapore|Republic of Singapore|\nGS|South Georgia and the South Sandwich Islands||\nSH|Saint Helena, Ascension and Tristan da Cunha||\nSJ|Svalbard and Jan Mayen||\nSB|Solomon Islands||\nSL|Sierra Leone|Republic of Sierra Leone|\nSV|El Salvador|Republic of El Salvador|\nSM|San Marino|Republic of San Marino|\nSO|Somalia|Federal Republic of Somalia|\nPM|Saint Pierre and Miquelon||\nRS|Serbia|Republic of Serbia|\nSS|South Sudan|Republic of South Sudan|\nST|Sao Tome and Principe|Democratic Republic of Sao Tome and Principe|\nSR|Suriname|Republic of Suriname|\nSK|Slovakia|Slovak Republic|\nSI|Slovenia|Republic of Slovenia|\nSE|Sweden|Kingdom of Sweden|\nSZ|Eswatini|Kingdom of Eswatini|\nSX|Sint Maarten (Dutch part)|Sint Maarten (Dutch part)|\nSC|Seychelles|Republic of Seychelles|\nSY|Syrian Arab Republic||Syria\nTC|Turks and Caicos Islands||\nTD|Chad|Republic of Chad|\nTG|Togo|Togolese Republic|\nTH|Thailand|Kingdom of Thailand|\nTJ|Tajikistan|Republic of Tajikistan|\nTK|Tokelau||\nTM|Turkmenistan||\nTL|Timor-Leste|Democratic Republic of Timor-Leste|\nTO|Tonga|Kingdom of Tonga|\nTT|Trinidad and Tobago|Republic of Trinidad and Tobago|\nTN|Tunisia|Republic of Tunisia|\nTR|Türkiye|Republic of Türkiye|\nTV|Tuvalu||\nTW|Taiwan, Province of China|Taiwan, Province of China|Taiwan\nTZ|Tanzania, United Republic of|United Republic of Tanzania|Tanzania\nUG|Uganda|Republic of Uganda|\nUA|Ukraine||\nUM|United States Minor Outlying Islands||\nUY|Uruguay|Eastern Republic of Uruguay|\nUS|United States|United States of America|\nUZ|Uzbekistan|Republic of Uzbekistan|\nVA|Holy See (Vatican City State)||\nVC|Saint Vincent and the Grenadines||\nVE|Venezuela, Bolivarian Republic of|Bolivarian Republic of Venezuela|Venezuela\nVG|Virgin Islands, British|British Virgin Islands|\nVI|Virgin Islands, U.S.|Virgin Islands of the United States|\nVN|Viet Nam|Socialist Republic of Viet Nam|Vietnam\nVU|Vanuatu|Republic of Vanuatu|\nWF|Wallis and Futuna||\nWS|Samoa|Independent State of Samoa|\nYE|Yemen|Republic of Yemen|\nZA|South Africa|Republic of South Africa|\nZM|Zambia|Republic of Zambia|\nZW|Zimbabwe|Republic of Zimbabwe|
""".strip()


def _parse_upstream_names() -> dict[str, tuple[str, str | None, str | None]]:
    parsed: dict[str, tuple[str, str | None, str | None]] = {}
    for line in _ISO_3166_1_UPSTREAM_NAMES.splitlines():
        code, name, official_name, common_name = line.split("|", 3)
        parsed[code] = (name, official_name or None, common_name or None)
    official_count = sum(row[1] is not None for row in parsed.values())
    common_count = sum(row[2] is not None for row in parsed.values())
    if len(parsed) != 249 or official_count != 173 or common_count != 11:
        raise RuntimeError("ISO upstream-name snapshot must be 249/173/11")
    payload = "".join(
        f"{code}|{parsed[code][0]}|{parsed[code][1] or ''}|{parsed[code][2] or ''}\n"
        for code in sorted(parsed)
    ).encode("utf-8")
    if len(payload) != ISO_SOURCE_PROVENANCE["embedded_payload_size_bytes"]:
        raise RuntimeError("ISO embedded payload size mismatch")
    if hashlib.sha256(payload).hexdigest() != ISO_SOURCE_PROVENANCE["embedded_payload_sha256"]:
        raise RuntimeError("ISO embedded payload SHA mismatch")
    return parsed


ISO_3166_1_UPSTREAM_NAMES = _parse_upstream_names()


def _country_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).replace("\u00a0", " ").split()).casefold()


ISO_COUNTRY_ALIASES: dict[str, str] = {}


def _normalize_country_ext(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = " ".join(text.replace("\u00a0", " ").split()).casefold()
    return _COUNTRY_ALIASES_EXT.get(key, ISO_COUNTRY_ALIASES.get(key, text))


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
    """C21 normalization followed by the pinned ISO English-name overlay."""
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
LEGACY_KNOWN_COUNTRIES = set(_NATIVE_COUNTRY_ALIASES.values())
LEGACY_KNOWN_COUNTRIES.update({
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


def _legacy_canonical_target(value: str) -> str | None:
    key = _country_key(value)
    if key in _NATIVE_COUNTRY_ALIASES:
        return _NATIVE_COUNTRY_ALIASES[key]
    if key in _COUNTRY_ALIASES_EXT:
        return _COUNTRY_ALIASES_EXT[key]
    if value in LEGACY_KNOWN_COUNTRIES:
        return value
    return None


ISO_COUNTRY_CANONICAL_BY_CODE: dict[str, str] = {}
ISO_COUNTRY_ALIASES = {}
for _code in sorted(ISO_3166_1_UPSTREAM_NAMES):
    _name, _official_name, _common_name = ISO_3166_1_UPSTREAM_NAMES[_code]
    _names = tuple(x for x in (_name, _official_name, _common_name) if x is not None)
    _legacy_targets = {target for value in _names if (target := _legacy_canonical_target(value))}
    if len(_legacy_targets) > 1:
        raise RuntimeError(f"conflicting legacy country targets for {_code}: {sorted(_legacy_targets)}")
    _canonical = next(iter(_legacy_targets), _common_name or _name)
    ISO_COUNTRY_CANONICAL_BY_CODE[_code] = _canonical
    for _alias in _names:
        _key = _country_key(_alias)
        if _key in AMBIGUOUS_COUNTRY_KEYS:
            continue
        _previous = ISO_COUNTRY_ALIASES.setdefault(_key, _canonical)
        if _previous != _canonical:
            raise RuntimeError(f"conflicting ISO country alias: {_alias!r}")

_iso_canonical_countries = set(ISO_COUNTRY_CANONICAL_BY_CODE.values())
_ambiguous_canonicals = {
    ISO_COUNTRY_CANONICAL_BY_CODE[code]
    for code, names in ISO_3166_1_UPSTREAM_NAMES.items()
    if _country_key(names[0]) in AMBIGUOUS_COUNTRY_KEYS
}
KNOWN_COUNTRIES = (set(LEGACY_KNOWN_COUNTRIES) | _iso_canonical_countries) - _ambiguous_canonicals
ISO_COUNTRY_ADDITIONS = {
    "known_countries": sorted(KNOWN_COUNTRIES - LEGACY_KNOWN_COUNTRIES),
    "aliases": [
        {"alias_key": key, "canonical": canonical}
        for key, canonical in sorted(ISO_COUNTRY_ALIASES.items())
        if key not in _COUNTRY_ALIASES_EXT and key not in _NATIVE_COUNTRY_ALIASES
    ],
    "excluded_ambiguous_keys": sorted(AMBIGUOUS_COUNTRY_KEYS),
}


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
        reason = "legacy_known_country" if canonical in LEGACY_KNOWN_COUNTRIES else "iso_3166_1_known_country"
        return CountryNormalization(raw, "known", canonical, reason)
    return CountryNormalization(raw, "unknown", None, "not_in_pinned_iso_country_set")


COUNTRY_NORMALIZER_SPEC = {
    "version": NORMALIZER_VERSION,
    "base_aliases": dict(sorted(COUNTRY_ALIASES.items())),
    "extended_aliases": dict(sorted(_COUNTRY_ALIASES_EXT.items())),
    "native_aliases": dict(sorted(_NATIVE_COUNTRY_ALIASES.items())),
    "legacy_known_countries": sorted(LEGACY_KNOWN_COUNTRIES),
    "known_countries": sorted(KNOWN_COUNTRIES),
    "iso_3166_1": {
        "source": ISO_SOURCE_PROVENANCE,
        "entry_count": len(ISO_3166_1_UPSTREAM_NAMES),
        "official_name_count": sum(row[1] is not None for row in ISO_3166_1_UPSTREAM_NAMES.values()),
        "common_name_count": sum(row[2] is not None for row in ISO_3166_1_UPSTREAM_NAMES.values()),
        "canonical_by_alpha2": dict(sorted(ISO_COUNTRY_CANONICAL_BY_CODE.items())),
        "executable_aliases": dict(sorted(ISO_COUNTRY_ALIASES.items())),
    },
    "additions": ISO_COUNTRY_ADDITIONS,
    "ambiguous_country_keys": sorted(AMBIGUOUS_COUNTRY_KEYS),
    "unknown_and_ambiguous_policy": "neutral_no_positive_no_conflict",
    "provenance": {
        role: {"path": path.as_posix(), "size_bytes": size, "sha256": digest}
        for role, (path, size, digest) in sorted(SOURCE_PROVENANCE.items())
    },
}


__all__ = [
    "AMBIGUOUS_COUNTRY_KEYS", "COUNTRY_NORMALIZER_SPEC", "CountryNormalization",
    "ISO_3166_1_UPSTREAM_NAMES", "ISO_COUNTRY_ADDITIONS", "ISO_COUNTRY_ALIASES",
    "ISO_COUNTRY_CANONICAL_BY_CODE", "ISO_SOURCE_PROVENANCE", "KNOWN_COUNTRIES",
    "LEGACY_KNOWN_COUNTRIES", "NORMALIZER_VERSION", "SOURCE_PROVENANCE", "classify_country",
    "normalize_country_full",
]
