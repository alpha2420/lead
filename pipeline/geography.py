"""Deterministic geography backstop for the ICP -> search-filter
translation. icp.py's prompt already instructs Gemini to expand a
region/timezone label into real place names at generation time (e.g.
"US Eastern Time Zone" -> every real EST state, listed individually) —
but that's a prompt instruction the model can still get wrong or skip,
and bi_accessors.py::_clean_geography_list() only *strips* a parenthetical
qualifier, it never expands or validates the underlying label. A location
string the search provider doesn't recognize silently returns zero results
for that entry, with nothing surfacing the failure. This module is the
code-level fallback: real timezone expansion, plus a conservative "does
this look like a real place name" flag.

Hand-rolled representative tables, same "good enough, not exhaustive"
scope as apify.py's _LEADS_FINDER_REGION_COUNTRIES, to stay consistent
with this codebase's existing convention of avoiding new taxonomy
dependencies for enum-shaped data."""

import re

_US_TIMEZONE_STATES: dict[str, list[str]] = {
    "eastern": [
        "Connecticut", "Delaware", "Florida", "Georgia", "Indiana", "Kentucky",
        "Maine", "Maryland", "Massachusetts", "Michigan", "New Hampshire",
        "New Jersey", "New York", "North Carolina", "Ohio", "Pennsylvania",
        "Rhode Island", "South Carolina", "Tennessee", "Vermont", "Virginia",
        "West Virginia", "District of Columbia",
    ],
    "central": [
        "Alabama", "Arkansas", "Illinois", "Iowa", "Kansas", "Louisiana",
        "Minnesota", "Mississippi", "Missouri", "Nebraska", "North Dakota",
        "Oklahoma", "South Dakota", "Tennessee", "Texas", "Wisconsin",
    ],
    "mountain": ["Arizona", "Colorado", "Idaho", "Montana", "New Mexico", "Utah", "Wyoming"],
    "pacific": ["California", "Nevada", "Oregon", "Washington"],
    "alaska": ["Alaska"],
    "hawaii": ["Hawaii"],
}

_TIMEZONE_ABBREV_TO_KEY = {
    "est": "eastern", "edt": "eastern",
    "cst": "central", "cdt": "central",
    "mst": "mountain", "mdt": "mountain",
    "pst": "pacific", "pdt": "pacific",
}


def _detect_timezone_key(text: str) -> "str | None":
    """Only matches when the phrase is clearly referring to a US
    timezone — an explicit abbreviation (EST/PST/...) or a timezone name
    paired with "time zone"/"timezone" — so it doesn't false-positive on
    unrelated text like "Pacific Northwest" (a real, valid region name,
    not a timezone label) or "Central America"."""
    t = text.lower()

    abbrev_match = re.search(r"\b(est|edt|cst|cdt|mst|mdt|pst|pdt)\b", t)
    if abbrev_match:
        return _TIMEZONE_ABBREV_TO_KEY[abbrev_match.group(1)]

    if re.search(r"time\s*zone|timezone", t):
        for name in _US_TIMEZONE_STATES:
            if re.search(rf"\b{name}\b", t):
                return name

    return None


def expand_timezone_labels(locations: list[str]) -> list[str]:
    """Deterministic expansion of a US-timezone label (e.g. "US Eastern
    Time Zone", "EST", "Pacific Time Zone cities") into its real member
    states. Non-timezone location strings pass through unchanged.
    De-dupes case-insensitively, preserving first-seen order/casing."""
    expanded: list[str] = []
    for loc in locations or []:
        key = _detect_timezone_key(str(loc))
        if key:
            expanded.extend(_US_TIMEZONE_STATES[key])
        else:
            expanded.append(str(loc).strip())

    seen: set = set()
    out: list[str] = []
    for loc in expanded:
        k = loc.lower()
        if loc and k not in seen:
            seen.add(k)
            out.append(loc)
    return out


_KNOWN_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
}

_KNOWN_CA_PROVINCES = {
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "ontario",
    "prince edward island", "quebec", "saskatchewan",
    "northwest territories", "nunavut", "yukon",
}

_KNOWN_COUNTRIES = {
    "united states", "usa", "us", "canada", "mexico", "united kingdom", "uk",
    "germany", "france", "netherlands", "spain", "italy", "ireland", "sweden",
    "belgium", "switzerland", "poland", "austria", "denmark", "finland",
    "norway", "portugal", "china", "india", "japan", "singapore", "australia",
    "hong kong", "south korea", "taiwan", "indonesia", "malaysia",
    "philippines", "thailand", "vietnam", "new zealand", "brazil", "argentina",
    "colombia", "chile", "peru", "united arab emirates", "uae",
    "saudi arabia", "israel", "qatar", "kuwait", "south africa", "nigeria",
    "kenya", "egypt", "ghana",
}

_KNOWN_LOCATIONS = _KNOWN_US_STATES | _KNOWN_CA_PROVINCES | _KNOWN_COUNTRIES

# Words that indicate a value is a descriptive/invented region label
# rather than a real place name a scraper's location filter would
# recognize — e.g. a phrase bi_accessors.py's own parenthetical-stripping
# wouldn't catch because it isn't wrapped in parentheses.
_SUSPICIOUS_LOCATION_WORDS = {
    "zone", "zones", "timezone", "region", "regions", "area", "areas",
    "belt", "corridor", "approximately", "roughly", "cities", "metro",
    "vicinity", "surrounding",
}


def _looks_like_invented_label(text: str) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words & _SUSPICIOUS_LOCATION_WORDS)


def validate_locations(locations: list[str]) -> dict:
    """
    Flags location strings that either (a) contain a descriptive/
    qualifier word indicating a fabricated region label rather than a
    real place name, or (b) exactly match neither the representative
    country/US-state/CA-province allowlist above nor look like an
    ordinary multi-word city name. Ordinary city names (the vast
    majority of real location values) are left as "recognized" by
    default — there's no bounded list of real cities to validate
    against, so the goal here is catching fabricated region labels, not
    full geographic authority data.

    This is a defense-in-depth *warning*, not a hard filter — see
    expand_timezone_labels() for the deterministic fix for the most
    common failure case. Unrecognized entries are still sent to the
    scraper; nothing here blocks a run.

    Returns {"recognized": [...], "unrecognized": [...]}.
    """
    recognized, unrecognized = [], []
    for loc in locations or []:
        cleaned = str(loc).strip()
        if not cleaned:
            continue
        if cleaned.lower() in _KNOWN_LOCATIONS:
            recognized.append(cleaned)
        elif _looks_like_invented_label(cleaned):
            unrecognized.append(cleaned)
        else:
            recognized.append(cleaned)
    return {"recognized": recognized, "unrecognized": unrecognized}
