"""
Country and currency reference data.

Loads ``country_codes.csv`` and ``country_currency.csv`` (shipped with
the project at the repo root) once at import time into in-memory dicts:

* :data:`COUNTRY_NAME_TO_MSTAR` — case- and whitespace-insensitive alias
  map. Resolves any of *long English name*, *alpha-2*, *alpha-3*, *numeric
  ISO 3166-1 country code*, or the canonical *mstar_country* form to the
  canonical lowercased ``mstar_country`` value (e.g. ``"unitedstates"``,
  ``"unitedkingdom"``, ``"southkorea"``, ``"netherlands"``).

* :data:`MSTAR_TO_CURRENCY` — ``mstar_country`` → ISO-4217 alpha-3
  currency code (``"unitedstates"`` → ``"USD"``).

* :data:`MSTAR_TO_REGION` — ``mstar_country`` → ``mstar_region``, the
  Morningstar-style regional bucket (``"northAmerica"``,
  ``"europeDeveloped"``, ``"japan"``, …). Drives the Country/Region
  toggle on the country breakdown cards — see :func:`country_to_region`.

The CSV files have been pre-cleaned so the inputs here are sane:
``mstar_country`` is lowercased throughout; ``country_currency.csv`` has
exactly one row per country (multi-currency countries reduced to their
primary currency for portfolio-tracking purposes).

A small :data:`MANUAL_ALIASES` dict patches in everyday-English short
names that the source CSVs don't list verbatim — "UK", "United Kingdom",
"South Korea", "Russia", etc.

If either CSV is missing on disk this module falls back to empty maps
rather than raising — the upload flow simply won't be able to normalise
country names, which is reported in the per-row warnings on commit.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterable

from porxpy.config import (
    COUNTRY_CODES_FP,
    COUNTRY_CURRENCY_FP,
    CURRENCIES_FP,
    FUND_CLASS_FP,
    HOLDINGS_CLASS_FP,
    SECTORS_FP,
    REGIONS_FP,
)


# ---------------------------------------------------------------------------
# Versioned CSV helpers (v0.15.0)
# ---------------------------------------------------------------------------
# Every "matches"-bearing resource CSV (Holdings_class_definitions,
# sectors, currencies) carries a "Version=N" line at the very top.
# The version is bumped whenever a user edits the file — either by
# hand or via the resolution dialog — and is stamped onto every
# normalised cache file so we can detect stale entries on read.
#
# Format:
#   Version=3
#   header_col1,header_col2,...
#   data...
#
# Files without the version line (e.g. legacy installs of this app)
# are treated as version 0; loaders fall through cleanly.

_VERSION_RE = re.compile(r"^\s*Version\s*=\s*(\d+)\s*[,\s]*$")


def parse_versioned_csv(fp: Path) -> tuple[int, list[dict]]:
    """Read a resource CSV that may carry a ``Version=N`` line at the top.

    The function transparently handles both versioned and unversioned
    files. For an unversioned file it returns ``version=0`` and the
    full DictReader output. For a versioned file it returns the
    parsed version number and the rows starting from line 2 onwards.

    Args:
        fp: Path to the CSV file. Must exist (caller's responsibility
            to check).

    Returns:
        ``(version, rows)`` where ``rows`` is a list of dicts keyed
        by the header row.
    """
    text = fp.read_text(encoding="utf-8-sig")
    version = 0
    if text:
        first_line, _, rest = text.partition("\n")
        m = _VERSION_RE.match(first_line)
        if m:
            version = int(m.group(1))
            text = rest                              # drop the version line

    reader = csv.DictReader(text.splitlines())
    rows = list(reader)
    return version, rows


def write_versioned_csv(fp: Path, version: int, fieldnames: list[str],
                        rows: list[dict]) -> None:
    """Write a resource CSV with the ``Version=N`` line at the top.

    Used by the resolution endpoint (and any future "edit resource
    file via UI" flows). The version line is written first, then the
    standard header + body. Existing files are overwritten; the
    caller is responsible for any backup / atomicity.

    Args:
        fp: Target path.
        version: The version number to stamp on line 1.
        fieldnames: CSV header order.
        rows: Body rows; each must have keys subsetting fieldnames.
    """
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    fp.write_text(f"Version={version}\n" + buf.getvalue(), encoding="utf-8")


# Loaded after the resource files themselves; populated by the
# loader functions below. Exposed as a dict so the rest of the app
# can read the active versions without re-parsing the files.
RESOURCE_VERSIONS: dict[str, int] = {
    "holdings_classes": 0,
    "fund_classes":     0,
    "sectors":          0,
    "currencies":       0,
    # v0.15.9: country_codes.csv now uses the versioned format with a
    # matches column, on equal footing with the other resource files.
    "countries":        0,
    # v0.28.0: regions.csv — region + super-region vocabulary for
    # name-derived fund focus.
    "regions":          0,
}


# ---------------------------------------------------------------------------
# Manual aliases — DEPRECATED (kept for legacy compatibility).
# ---------------------------------------------------------------------------
# These were used pre-v0.15.9 when country_codes.csv had no matches
# column. They've all been migrated into the CSV's matches columns by
# migrate_country_codes.py. The dict is retained but empty; if anything
# external still references it, the indexing call site won't break.
MANUAL_ALIASES: dict[str, str] = {}


def _split_matches(s: str | None) -> list[str]:
    """Parse a pipe-separated ``matches`` cell into a list of aliases.

    Empty cells yield an empty list. Whitespace is stripped, blanks
    dropped. Lowercasing is the caller's responsibility — only the
    alias-lookup maps lowercase keys, since the original casing is
    occasionally useful for display.

    Args:
        s: Raw ``matches`` cell from a CSV row.

    Returns:
        Cleaned list of alias strings.
    """
    if not s:
        return []
    return [tok.strip() for tok in s.split("|") if tok.strip()]


def _norm_phrase(s: str | None) -> str:
    """Normalise a phrase for free-text matching against a fund name.

    Lowercases, reduces every non-alphanumeric run to a single space, and
    collapses whitespace — so the CSV entry ``"oil & gas"`` and the fund
    name fragment ``"Oil & Gas"`` reduce to the same token, and the CSV
    can stay in natural spelling. Both sides go through this.
    """
    if not s:
        return ""
    out = []
    for ch in str(s).lower():
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def _normalise_key(s: str | None) -> str:
    """Lower + strip a candidate alias key for case-insensitive lookup."""
    return (s or "").strip().lower()


def _load_country_codes() -> tuple[dict[str, str], list[dict]]:
    """Build alias→mstar_country map and structured rows list from country_codes.csv.

    v0.15.9: country_codes.csv now follows the same versioned-with-
    matches format as sectors.csv / currencies.csv / Holdings_class_
    definitions.csv. Each row carries:

        mstar_country, alpha-2, alpha-3, country-code, mstar_region, matches

    Every column (including ``matches`` items) becomes a lookup alias
    for the row's canonical ``mstar_country``. Pipe-separated matches
    follow the same convention as the other resource CSVs.

    First-write-wins for any colliding alias key — avoids unpredictable
    resolution if the user accidentally puts the same alias on two
    different countries' matches columns. (The migration script
    deduplicates by canonical, so this case shouldn't arise in
    practice; the guard is here for hand-edits.)

    Returns:
        ``(aliases, rows)``. ``aliases`` is the lookup dict used by
        country_to_mstar; ``rows`` is the per-canonical structured list
        used by add_country_alias to extend a row's matches column.
        Both empty if ``country_codes.csv`` is missing.
    """
    aliases: dict[str, str] = {}
    rows: list[dict] = []

    if not COUNTRY_CODES_FP.exists():
        print(f"[Resources] {COUNTRY_CODES_FP.name} not found — country "
              f"normalisation will be a no-op.")
        return aliases, rows

    version, raw_rows = parse_versioned_csv(COUNTRY_CODES_FP)
    RESOURCE_VERSIONS["countries"] = version
    for raw in raw_rows:
        mstar = _normalise_key(raw.get("mstar_country"))
        if not mstar:
            continue
        row = {
            "mstar_country": mstar,
            "alpha-2":       (raw.get("alpha-2")      or "").strip().upper(),
            "alpha-3":       (raw.get("alpha-3")      or "").strip().upper(),
            "country-code":  (raw.get("country-code") or "").strip(),
            "mstar_region":  (raw.get("mstar_region") or "").strip(),
            "matches":       _split_matches(raw.get("matches")),
        }
        rows.append(row)

        # Self-alias: every canonical resolves to itself.
        if mstar not in aliases:
            aliases[mstar] = mstar
        # Built-in ISO/numeric columns become aliases too.
        for col_val in (row["alpha-2"], row["alpha-3"], row["country-code"]):
            k = _normalise_key(col_val)
            if k and k not in aliases:
                aliases[k] = mstar
        # User-supplied matches.
        for m in row["matches"]:
            k = _normalise_key(m)
            if k and k not in aliases:
                aliases[k] = mstar

    # Layer the (now-empty) MANUAL_ALIASES dict — kept as a no-op
    # extensibility hook in case anything external still mutates it.
    for k, v in MANUAL_ALIASES.items():
        aliases[_normalise_key(k)] = v

    if raw_rows and not rows:
        first = raw_rows[0] if raw_rows else {}
        print(f"[Resources] WARNING: {COUNTRY_CODES_FP.name} parsed "
              f"{len(raw_rows)} row(s) but yielded 0 country aliases. "
              f"Header keys seen: {sorted(first.keys())!r}. "
              f"Expected an 'mstar_country' column.")

    return aliases, rows


def _load_country_currency() -> dict[str, str]:
    """Build mstar_country→ISO-currency map from country_currency.csv.

    The file is pre-cleaned: one row per country, primary currency only.

    Returns:
        Empty dict if ``country_currency.csv`` is missing.
    """
    if not COUNTRY_CURRENCY_FP.exists():
        print(f"[Resources] {COUNTRY_CURRENCY_FP.name} not found — "
              f"currency derivation will be a no-op.")
        return {}

    out: dict[str, str] = {}
    with open(COUNTRY_CURRENCY_FP, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            mstar = _normalise_key(row.get("mstar_country"))
            iso   = (row.get("iso_curr") or "").strip().upper()
            if mstar and iso:
                out[mstar] = iso
    return out


def _load_country_region() -> dict[str, str]:
    """Build mstar_country→mstar_region map from country_codes.csv.

    The ``mstar_region`` column is Morningstar's regional taxonomy — a
    small fixed set of buckets (``"northAmerica"``, ``"europeDeveloped"``,
    ``"europeEmerging"``, ``"asiaDeveloped"``, ``"asiaEmerging"``,
    ``"japan"``, ``"unitedKingdom"``, ``"australasia"``, ``"latinAmerica"``,
    ``"africaMiddleEast"``, ``"antartica"``). Each country maps to exactly
    one. This pairs with the canonical ``mstar_country`` keys the rollup
    already produces, so the Country/Region toggle is a pure regroup of
    existing breakdown data.

    First write wins for a given ``mstar_country`` key, mirroring
    :func:`_load_country_codes` — the camelCase duplicate rows later in
    the source file do not clobber the first clean row.

    Returns:
        Empty dict if ``country_codes.csv`` is missing.
    """
    if not COUNTRY_CODES_FP.exists():
        print(f"[Resources] {COUNTRY_CODES_FP.name} not found — region "
              f"derivation will be a no-op.")
        return {}

    out: dict[str, str] = {}
    version, raw_rows = parse_versioned_csv(COUNTRY_CODES_FP)
    # We don't bump RESOURCE_VERSIONS["countries"] here — _load_country_
    # codes already did. Both functions read the same file; using
    # parse_versioned_csv ensures the Version=N line is correctly
    # skipped rather than read as a header.
    for raw in raw_rows:
        mstar  = _normalise_key(raw.get("mstar_country"))
        region = (raw.get("mstar_region") or "").strip()
        if mstar and region and mstar not in out:
            out[mstar] = region
    return out


# Loaded once at import. Hot-reload during dev is opt-in via reload_resources().
COUNTRY_NAME_TO_MSTAR: dict[str, str]
COUNTRY_ROWS:          list[dict]
COUNTRY_NAME_TO_MSTAR, COUNTRY_ROWS = _load_country_codes()
MSTAR_TO_CURRENCY:     dict[str, str] = _load_country_currency()
MSTAR_TO_REGION:       dict[str, str] = _load_country_region()


def reload_resources() -> tuple[int, int, int]:
    """Re-read both CSVs from disk (useful when the user updates them).

    Updates the module-level :data:`COUNTRY_NAME_TO_MSTAR`,
    :data:`COUNTRY_ROWS`, :data:`MSTAR_TO_CURRENCY` and
    :data:`MSTAR_TO_REGION` maps in place so existing references stay
    valid.

    Returns:
        ``(alias_count, currency_count, region_count)`` after reloading.
    """
    global COUNTRY_NAME_TO_MSTAR, COUNTRY_ROWS, MSTAR_TO_CURRENCY, MSTAR_TO_REGION
    COUNTRY_NAME_TO_MSTAR, COUNTRY_ROWS = _load_country_codes()
    MSTAR_TO_CURRENCY     = _load_country_currency()
    MSTAR_TO_REGION       = _load_country_region()
    return (len(COUNTRY_NAME_TO_MSTAR), len(MSTAR_TO_CURRENCY),
            len(MSTAR_TO_REGION))


def country_to_mstar(raw: str | None) -> str | None:
    """Resolve any country name/code to the canonical mstar_country form.

    Args:
        raw: Free-form country string from a holdings file.

    Returns:
        Lowercased ``mstar_country`` value (e.g. ``"unitedstates"``), or
        ``None`` if the input couldn't be matched against any known alias.
    """
    return COUNTRY_NAME_TO_MSTAR.get(_normalise_key(raw))


def country_to_currency(raw: str | None) -> str | None:
    """Resolve any country name/code to its primary ISO-4217 currency.

    Convenience wrapper that composes :func:`country_to_mstar` with the
    currency-by-country lookup.

    Args:
        raw: Free-form country string from a holdings file.

    Returns:
        ISO-4217 alpha-3 currency code (e.g. ``"USD"``), or ``None`` if
        the country couldn't be resolved or no currency is on file.
    """
    mstar = country_to_mstar(raw)
    if not mstar:
        return None
    return MSTAR_TO_CURRENCY.get(mstar)


def country_to_region(raw: str | None) -> str | None:
    """Resolve any country name/code to its Morningstar regional bucket.

    Convenience wrapper that composes :func:`country_to_mstar` with the
    region-by-country lookup.

    Args:
        raw: Free-form country string, ISO code, or canonical
            ``mstar_country`` value.

    Returns:
        The ``mstar_region`` bucket (e.g. ``"northAmerica"``), or
        ``None`` if the country couldn't be resolved or has no region on
        file.
    """
    mstar = country_to_mstar(raw)
    if not mstar:
        return None
    return MSTAR_TO_REGION.get(mstar)


# ---------------------------------------------------------------------------
# Holdings classification (asset_class + sub_class, with matches column)
# ---------------------------------------------------------------------------
# The Holdings_class_definitions.csv file enumerates the canonical
# (asset_class, sub_class) pairs PorxPy uses on every holding row. It's
# the source of truth for:
#
#   * The two paired dropdowns in the edit-holding modal — picking an
#     asset class narrows the sub-class options to that group.
#   * Normalisation on upload: an issuer file's "Common stock" or
#     "EQ" hits the matches column for "shares" and stores as "shares".
#   * The default sub-class for a freshly-built holding (see
#     :func:`porxpy.utils.default_sub_class`, kept in sync with this
#     file by reading the first sub_class listed per asset_class).
#
# Column semantics:
#   asset_class      lowercased; one of equity / bond / cash / other
#   asset_class_desc human-readable description (display only)
#   sub_class        lowercased free text; per (asset_class, sub_class)
#                    unique
#   sub_class_desc   human-readable description (display only)
#   matches          pipe-separated list of alternative spellings the
#                    upload normaliser will accept (case-insensitive)
#
# Lookup tables:
#   HOLDINGS_CLASS_ROWS   ordered list of dicts (preserves CSV order so
#                         the dropdowns render in a stable order)
#   HOLDINGS_CLASS_INDEX  asset_class -> list of sub_class strings
#                         (membership + sub-class options per group)
#   HOLDINGS_SUB_ALIASES  alternative spelling -> canonical sub_class
#                         (alternative keys are lower-cased + trimmed)
#   HOLDINGS_AC_ALIASES   alternative spelling -> canonical asset_class
#                         (asset classes don't carry a per-row matches
#                          column today; this map is built from the
#                          column's distinct values for case-insensitive
#                          lookup, and can grow if we add aliases later)


def _load_holdings_classes() -> tuple[list[dict], dict[str, list[str]],
                                       dict[str, str], dict[str, str]]:
    """Load Holdings_class_definitions.csv into the four lookup structures.

    See the module-level table comment above for the structure of each
    returned value. Missing file → empty tables (the edit-holding modal
    then falls back to a free-text input with no autocomplete, but the
    rest of PorxPy keeps working).

    Returns:
        ``(rows, index, sub_aliases, ac_aliases)``.
    """
    rows: list[dict] = []
    index: dict[str, list[str]] = {}
    sub_aliases: dict[str, str] = {}
    ac_aliases:  dict[str, str] = {}

    if not HOLDINGS_CLASS_FP.exists():
        print(f"[Resources] {HOLDINGS_CLASS_FP.name} not found — holdings "
              f"classification will fall back to free text only.")
        return rows, index, sub_aliases, ac_aliases

    version, raw_rows = parse_versioned_csv(HOLDINGS_CLASS_FP)
    RESOURCE_VERSIONS["holdings_classes"] = version

    # Two-pass: collect every canonical sub_class first, then validate
    # matches against them. A matches token that equals another row's
    # canonical sub_class is always a CSV authoring mistake — the
    # alias is unreachable (first-write-wins gives that token back to
    # its own canonical row) and worse, suggests an authoring intent
    # that the file mechanically can't honour. Loud warning so the
    # mistake doesn't survive a casual review.
    all_canonicals: set[str] = set()
    for raw in raw_rows:
        sub = (raw.get("sub_class") or "").strip().lower()
        if sub:
            all_canonicals.add(sub)

    collisions: list[tuple[str, str, str]] = []   # (ac, sub, colliding_token)

    for raw in raw_rows:
        ac  = (raw.get("asset_class") or "").strip().lower()
        sub = (raw.get("sub_class")   or "").strip().lower()
        if not ac or not sub:
            continue
        matches_list = _split_matches(raw.get("matches"))
        row = {
            "asset_class":      ac,
            "asset_class_desc": (raw.get("asset_class_desc") or "").strip(),
            "sub_class":        sub,
            "sub_class_desc":   (raw.get("sub_class_desc")   or "").strip(),
            "matches":          matches_list,
        }
        rows.append(row)
        index.setdefault(ac, []).append(sub)

        # Canonical names are aliases for themselves (case-folded).
        sub_aliases[sub] = sub
        ac_aliases[ac]   = ac

        # User-supplied matches → canonical sub_class. First write
        # wins to keep behaviour predictable when two sub-classes
        # accidentally share an alias.
        for m in matches_list:
            k = m.strip().lower()
            if not k:
                continue
            if k in all_canonicals and k != sub:
                # Collision: this matches token equals another row's
                # canonical. Record and skip — DON'T add to aliases.
                # If we added it, first-write-wins behaviour would
                # mean the alias is dead code; better to leave the
                # canonical row's self-alias as the sole binding.
                collisions.append((ac, sub, k))
                continue
            if k not in sub_aliases:
                sub_aliases[k] = sub

    if collisions:
        print(f"[Resources] WARNING: {HOLDINGS_CLASS_FP.name} has "
              f"{len(collisions)} matches-token(s) that collide with a "
              f"canonical sub_class. These tokens were IGNORED (the "
              f"canonical's self-alias wins anyway). Remove them from "
              f"the matches column to clean up:")
        for ac, sub, tok in collisions:
            print(f"    - {ac}/{sub}: matches contains {tok!r} "
                  f"(itself a canonical)")

    return rows, index, sub_aliases, ac_aliases


HOLDINGS_CLASS_ROWS:  list[dict]
HOLDINGS_CLASS_INDEX: dict[str, list[str]]
HOLDINGS_SUB_ALIASES: dict[str, str]
HOLDINGS_AC_ALIASES:  dict[str, str]
(HOLDINGS_CLASS_ROWS, HOLDINGS_CLASS_INDEX,
 HOLDINGS_SUB_ALIASES, HOLDINGS_AC_ALIASES) = _load_holdings_classes()


def resolve_sub_class(raw: str | None) -> str | None:
    """Coerce any spelling of a sub-class to the canonical form.

    Args:
        raw: Free-form sub-class string (e.g. ``"Common stock"``,
            ``"EQ"``, ``"shares"``).

    Returns:
        Canonical lowercase sub_class from
        :data:`HOLDINGS_CLASS_ROWS`, or ``None`` if the input wasn't
        blank but couldn't be matched (caller chooses whether to
        store the raw value as a free-text fallback or reject).
    """
    if not raw:
        return None
    key = raw.strip().lower()
    return HOLDINGS_SUB_ALIASES.get(key)


def resolve_asset_class(raw: str | None) -> str | None:
    """Coerce any spelling of an asset class to the canonical form.

    The asset-class set is small (4 values today) and the file's
    ``asset_class`` column carries no per-row ``matches`` — currently
    only direct case-insensitive matches against the canonical names
    succeed. This helper exists so the upload / edit paths have a
    single chokepoint and can grow alias support later without
    touching call sites.

    Args:
        raw: Free-form asset-class string.

    Returns:
        Canonical lowercase asset_class, or ``None`` on no-match.
    """
    if not raw:
        return None
    key = raw.strip().lower()
    return HOLDINGS_AC_ALIASES.get(key)


# ---------------------------------------------------------------------------
# Fund-level asset classes (with matches column)
# ---------------------------------------------------------------------------
# The wider config.ASSET_CLASSES vocabulary — equity / fixed_income /
# cash / mixed / commodity / other — as a maintained CSV. This is the
# authority the portfolio Targets editor reads from, so its keys MUST
# match what the portfolio/holdings rollup emits (the rollup
# canonicalises every holding asset-class spelling up to this
# vocabulary; e.g. a "bond" holding rolls up to "fixed_income"). The
# `matches` column carries every alternative spelling — the holdings
# enum's "bond"/"bonds", free-text "fixed income", and Yahoo's
# funds_data position keys ("bondPosition" etc.) — so a single lookup
# table replaces the three hand-maintained alias maps that used to live
# in breakdowns.py (_ASSET_CLASS_ALIASES) and extractors.py
# (_ASSET_ALLOCATION_KEYMAP).
#
# Lookup tables:
#   FUND_CLASS_ROWS    ordered list of dicts {asset_class, description}
#                      (CSV order, which is the canonical display order)
#   FUND_CLASS_ALIASES alternative spelling (lowercase) → canonical key,
#                      including each canonical key as a self-alias

def _load_fund_classes() -> tuple[list[dict], dict[str, str]]:
    """Load Fund_class_definitions.csv into the lookup structures.

    Missing file → falls back to the hardcoded config.ASSET_CLASSES
    keys with no extra aliases (so the app keeps working, the Targets
    editor still offers the canonical keys, and only the convenience
    spellings are unavailable until the file is added).

    Returns:
        ``(rows, aliases)`` where ``rows`` is
        ``[{"asset_class","description"}, ...]`` in file order and
        ``aliases`` maps every spelling (lowercased) plus each
        canonical key (self-alias) to a canonical key.
    """
    from porxpy.config import ASSET_CLASSES   # local: avoid import cycle

    rows: list[dict] = []
    aliases: dict[str, str] = {}

    if not FUND_CLASS_FP.exists():
        print(f"[Resources] {FUND_CLASS_FP.name} not found — fund-level "
              f"asset-class aliases unavailable; using config.ASSET_CLASSES "
              f"keys only.")
        for ac in ASSET_CLASSES:
            rows.append({"asset_class": ac, "description": ""})
            aliases[ac] = ac
        return rows, aliases

    version, raw_rows = parse_versioned_csv(FUND_CLASS_FP)
    RESOURCE_VERSIONS["fund_classes"] = version

    seen: set[str] = set()
    for raw in raw_rows:
        ac = (raw.get("asset_class") or "").strip().lower()
        if not ac:
            continue
        if ac not in seen:
            seen.add(ac)
            rows.append({
                "asset_class": ac,
                "description":  (raw.get("description") or "").strip(),
            })
        # Canonical key is a self-alias.
        aliases[ac] = ac
        for m in _split_matches(raw.get("matches")):
            k = m.strip().lower()
            if k and k not in aliases:
                aliases[k] = ac

    # Defensive: ensure every config.ASSET_CLASSES key is present even
    # if the file omitted one, so validation against the enum and the
    # editor dropdown never silently lose a canonical value.
    for ac in ASSET_CLASSES:
        if ac not in seen:
            rows.append({"asset_class": ac, "description": ""})
            aliases.setdefault(ac, ac)

    return rows, aliases


FUND_CLASS_ROWS:    list[dict]
FUND_CLASS_ALIASES: dict[str, str]
(FUND_CLASS_ROWS, FUND_CLASS_ALIASES) = _load_fund_classes()


def resolve_fund_asset_class(raw: str | None) -> str | None:
    """Coerce any spelling of a FUND-level asset class to canonical form.

    Resolves against Fund_class_definitions.csv — the wider
    config.ASSET_CLASSES vocabulary (equity / fixed_income / cash /
    mixed / commodity / other). This is the chokepoint the rollup,
    the issuer asset-allocation extractor, and the upload path use to
    normalise spellings ("bond"/"bonds"/"bondPosition" → "fixed_income")
    up to the fund-level vocabulary.

    Args:
        raw: Free-form asset-class string, any casing, or None/blank.

    Returns:
        Canonical lowercase key from config.ASSET_CLASSES, or ``None``
        on no-match (caller decides whether to keep the raw value or
        fold into "other").
    """
    if not raw:
        return None
    key = str(raw).strip().lower()
    return FUND_CLASS_ALIASES.get(key)


def list_fund_asset_classes() -> list[dict]:
    """Return the fund-level asset classes for dropdown population.

    Used by the portfolio Targets editor. Order follows the CSV
    (the intended display order).

    Returns:
        ``[{"key": canonical, "label": Display Name}, ...]``.
    """
    out: list[dict] = []
    for r in FUND_CLASS_ROWS:
        ac = r.get("asset_class")
        if not ac:
            continue
        out.append({
            "key":   ac,
            "label": ac.replace("_", " ").title(),
        })
    return out


# ---------------------------------------------------------------------------
# Morningstar sectors (with matches column)
# ---------------------------------------------------------------------------
# Lookup tables:
#   SECTORS_ROWS     ordered list of dicts (preserves CSV order)
#   SECTOR_ALIASES   alternative spelling (lowercase) → canonical sector


def _load_sectors() -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """Load sectors.csv into the lookup structures.

    Two vocabularies, deliberately separate (v0.31.0):

    * ``matches`` → :data:`SECTOR_ALIASES`. Whatever spelling turns up in
      an uploaded holdings row, coerced to a canonical sector. Built for
      recall: it carries ``"other"``, ``"it"``, ``"cash"`` and a tail of
      one-off spellings seen in real issuer files.
    * ``style_match`` → :data:`SECTOR_STYLE_ALIASES`. Phrases that, found
      in a *fund name*, mean the fund is built around that sector. Built
      for precision.

    Reusing one column for both jobs does not work, and the failure is not
    hypothetical: ``"other"`` is a legitimate holdings alias for real
    estate, so "MSCI World Ex Other Sectors" resolved to a property fund;
    and ``"communication"`` is a legitimate holdings alias for technology,
    so "MSCI World Communication Services" matched two sectors at once and
    resolved to neither. The two vocabularies want opposite tolerances.

    A blank ``style_match`` cell is normal — the canonical sector name is
    always a match on its own, and for most rows that is enough. It is
    also how a row opts out entirely: "cash and/or derivatives" is a
    holdings residual bucket, not a thing a fund is *for*, and no fund
    name will ever contain the phrase.

    Returns:
        ``(rows, aliases, style_aliases)``. Empty if the file is missing.
    """
    rows: list[dict] = []
    aliases: dict[str, str] = {}
    style_aliases: dict[str, str] = {}

    if not SECTORS_FP.exists():
        print(f"[Resources] {SECTORS_FP.name} not found — sector "
              f"normalisation will be a no-op.")
        return rows, aliases, style_aliases

    version, raw_rows = parse_versioned_csv(SECTORS_FP)
    RESOURCE_VERSIONS["sectors"] = version
    for raw in raw_rows:
        sec = (raw.get("sector") or "").strip().lower()
        if not sec:
            continue
        row = {
            "sector":       sec,
            "super_sector": (raw.get("super_sector") or "").strip().lower(),
            "description":  (raw.get("description")  or "").strip(),
            "matches":      _split_matches(raw.get("matches")),
            "style_match":  _split_matches(raw.get("style_match")),
        }
        rows.append(row)

        aliases[sec] = sec
        for m in row["matches"]:
            k = m.strip().lower()
            if k and k not in aliases:
                aliases[k] = sec

        # The canonical name is always its own style match; the column
        # only adds to it.
        for m in [sec, *row["style_match"]]:
            k = _norm_phrase(m)
            if k and k not in style_aliases:
                style_aliases[k] = sec

    # Surface a present-but-unreadable file as a loud warning. A
    # silent empty list usually means the CSV's first column header
    # isn't 'sector' (BOM corruption, wrong case, renamed column),
    # and the downstream symptoms — empty Sector dropdown, dialog
    # rendering "Resource list is empty" — give the user no path to
    # the root cause.
    if raw_rows and not rows:
        first = raw_rows[0] if raw_rows else {}
        print(f"[Resources] WARNING: {SECTORS_FP.name} parsed "
              f"{len(raw_rows)} row(s) but yielded 0 sectors. "
              f"Header keys seen: {sorted(first.keys())!r}. "
              f"Expected a 'sector' column.")

    return rows, aliases, style_aliases


SECTORS_ROWS:         list[dict]
SECTOR_ALIASES:       dict[str, str]
SECTOR_STYLE_ALIASES: dict[str, str]
SECTORS_ROWS, SECTOR_ALIASES, SECTOR_STYLE_ALIASES = _load_sectors()


def resolve_sector(raw: str | None) -> str | None:
    """Coerce any spelling of a Morningstar sector to the canonical form.

    Args:
        raw: Free-form sector string (e.g.
            ``"Information Technology"`` → ``"technology"``).

    Returns:
        Canonical lowercase sector, or ``None`` if no match.
    """
    if not raw:
        return None
    return SECTOR_ALIASES.get(raw.strip().lower())


# ---------------------------------------------------------------------------
# Regions and super regions (v0.28.0)
# ---------------------------------------------------------------------------
# regions.csv is region-keyed, one row per mstar_region plus one row per
# super region. Two kinds in one file because a super region declares
# nothing but its own aliases — membership is expressed the other way
# round, by each region's ``super_regions`` cell.
#
# The alias column is named ``style_match``, matching sectors.csv. Unlike
# sectors.csv there is no second, looser ``matches`` column here: nothing
# normalises a free-text region the way uploaded holdings rows normalise
# a free-text sector, so this file has only the one job.
#
# Scope note, because the name invites the wrong assumption: this
# vocabulary exists ONLY to seed a fund's ``focus_type`` /
# ``focus_detail`` from its name. The country/region breakdown facet is
# built from MSTAR_TO_REGION (country_codes.csv) and does not consult
# any of this. "MSCI Europe" getting ``focus_detail: europe`` says what
# the fund is *for*; it says nothing about where its holdings sit, which
# is the facet's job and is measured, not inferred from a name.
#
# Lookup tables:
#   REGION_ROWS         ordered list of dicts (both kinds)
#   REGION_KEYS         the mstar_region keys
#   SUPER_REGION_KEYS   the super-region keys
#   REGION_ALIASES      normalised style_match phrase → key (either kind)
#   SUPER_REGION_MEMBERS  super key → set of member region keys
#
# There is deliberately no "global"/"world" entry. A global fund is not
# focused on a region; it is the absence of a regional focus, which is
# what ``focus_type: none`` already says.

def _load_regions() -> tuple[list[dict], dict[str, str], dict[str, set]]:
    """Load regions.csv into the region/super-region lookup structures.

    Returns:
        ``(rows, aliases, super_members)``. All empty if the file is
        missing — name-derived focus then becomes a no-op, which is the
        correct degradation: focus simply stays at its default.
    """
    rows: list[dict] = []
    aliases: dict[str, str] = {}
    super_members: dict[str, set] = {}

    if not REGIONS_FP.exists():
        print(f"[Resources] {REGIONS_FP.name} not found — name-derived "
              f"fund focus will be a no-op.")
        return rows, aliases, super_members

    version, raw_rows = parse_versioned_csv(REGIONS_FP)
    RESOURCE_VERSIONS["regions"] = version

    for raw in raw_rows:
        key = (raw.get("key") or "").strip()
        if not key:
            continue
        kind = (raw.get("kind") or "region").strip().lower()
        if kind not in ("region", "super"):
            kind = "region"
        row = {
            "key":           key,
            "kind":          kind,
            "label":         (raw.get("label") or key).strip(),
            "super_regions": _split_matches(raw.get("super_regions")),
            "style_match":   _split_matches(raw.get("style_match")),
        }
        rows.append(row)

        # The key itself is always an alias for itself.
        for token in [key, *row["style_match"]]:
            norm = _norm_phrase(token)
            if norm and norm not in aliases:
                aliases[norm] = key

        # Membership is declared bottom-up: each row names the super
        # regions it belongs to. Super regions may themselves belong to
        # others — "world" contains "developed", which contains
        # "northAmerica" — so supers register too, and the nesting is
        # resolved transitively below.
        for sup in row["super_regions"]:
            super_members.setdefault(sup, set()).add(key)

    # Flatten the nesting. Without this, "world" would contain only the
    # keys that named it directly, and the containment rule in
    # _derive_focus_from_name — which collapses several region hits into
    # the one super region enclosing them all — would fail for any name
    # matching both a super and something inside it. Iterating to a fixed
    # point handles arbitrary depth; the vocabulary is tiny, so the cost
    # is nil and a cycle simply stops adding members rather than hanging.
    for _ in range(len(rows) + 1):
        changed = False
        for sup, members in list(super_members.items()):
            expanded = set(members)
            for m in members:
                expanded |= super_members.get(m, set())
            expanded.discard(sup)          # a region cannot contain itself
            if expanded != members:
                super_members[sup] = expanded
                changed = True
        if not changed:
            break

    if raw_rows and not rows:
        first = raw_rows[0] if raw_rows else {}
        print(f"[Resources] WARNING: {REGIONS_FP.name} parsed "
              f"{len(raw_rows)} row(s) but yielded 0 regions. "
              f"Header keys seen: {sorted(first.keys())!r}. "
              f"Expected a 'key' column.")

    return rows, aliases, super_members


REGION_ROWS:          list[dict]
REGION_ALIASES:       dict[str, str]
SUPER_REGION_MEMBERS: dict[str, set]
REGION_ROWS, REGION_ALIASES, SUPER_REGION_MEMBERS = _load_regions()

REGION_KEYS:       tuple[str, ...] = tuple(
    r["key"] for r in REGION_ROWS if r["kind"] == "region")
SUPER_REGION_KEYS: tuple[str, ...] = tuple(
    r["key"] for r in REGION_ROWS if r["kind"] == "super")


def focus_region_vocabulary() -> tuple[str, ...]:
    """Every legal ``focus_detail`` value when ``focus_type`` is "region".

    Regions and super regions both qualify: a fund can be built for
    Japan (a region) or for Europe (a super region spanning developed
    Europe, emerging Europe and the UK), and both are real answers to
    "what is this fund for".
    """
    return REGION_KEYS + SUPER_REGION_KEYS


def focus_detail_vocabulary(context: dict | None = None):
    """Allowed ``focus_detail`` values for a given ``focus_type``.

    The one cross-field rule in the override registry: what counts as a
    valid detail depends entirely on the type being set alongside it.

    Args:
        context: The other field values in the same edit. Only
            ``focus_type`` is consulted.

    Returns:
        A tuple of allowed values, or ``None`` meaning "free text is
        valid here" — which is the honest answer for a thematic focus,
        since nothing can enumerate "Artificial Intelligence" ahead of
        time. ``focus_type: none`` allows only the empty string.
    """
    # Defensive str(): this is reachable from anything that validates an
    # override, including an AI reply whose shape we do not control. A
    # bad context should narrow the vocabulary, not raise.
    raw_ft = (context or {}).get("focus_type")
    ftype = (str(raw_ft) if isinstance(raw_ft, (str, int, float)) else "none")
    ftype = (ftype or "none").strip().lower()
    if ftype == "region":
        return focus_region_vocabulary()
    if ftype == "sector":
        return tuple(r["sector"] for r in SECTORS_ROWS)
    if ftype == "thematic":
        return None
    return ("",)


def resolve_region_alias(raw: str | None) -> str | None:
    """Coerce any spelling of a region or super region to its key.

    Args:
        raw: Free-form region string (e.g. ``"Emerging Markets"`` →
            ``"emerging"``, ``"Nikkei"`` → ``"japan"``).

    Returns:
        Canonical key, or ``None`` if no match.
    """
    return REGION_ALIASES.get(_norm_phrase(raw))


# ---------------------------------------------------------------------------
# Currencies (ISO 4217 with matches column)
# ---------------------------------------------------------------------------
# Lookup tables:
#   CURRENCY_ROWS    ordered list of dicts (code in original casing —
#                    typically uppercase, "GBp" preserves its mixed case)
#   CURRENCY_ALIASES alternative spelling / lower-cased code / lower-
#                    cased name → canonical code (in its original casing
#                    so callers can store it verbatim)


def _load_currencies() -> tuple[list[dict], dict[str, str]]:
    """Load currencies.csv into the lookup structures.

    Returns:
        ``(rows, aliases)``. Empty if the file is missing.
    """
    rows: list[dict] = []
    aliases: dict[str, str] = {}

    if not CURRENCIES_FP.exists():
        print(f"[Resources] {CURRENCIES_FP.name} not found — currency "
              f"dropdown will be empty.")
        return rows, aliases

    version, raw_rows = parse_versioned_csv(CURRENCIES_FP)
    RESOURCE_VERSIONS["currencies"] = version
    for raw in raw_rows:
        code = (raw.get("code") or "").strip()
        if not code:
            continue
        row = {
            "code":    code,
            "name":    (raw.get("name")    or "").strip(),
            "symbol":  (raw.get("symbol")  or "").strip(),
            "matches": _split_matches(raw.get("matches")),
        }
        rows.append(row)

        # Canonical code → itself (case-insensitive lookup key).
        aliases[code.lower()] = code
        # Name and matches go in too.
        if row["name"]:
            aliases.setdefault(row["name"].lower(), code)
        for m in row["matches"]:
            k = m.strip().lower()
            if k:
                aliases.setdefault(k, code)

    return rows, aliases


CURRENCY_ROWS:    list[dict]
CURRENCY_ALIASES: dict[str, str]
CURRENCY_ROWS, CURRENCY_ALIASES = _load_currencies()


def resolve_currency(raw: str | None) -> str | None:
    """Coerce any spelling of a currency to its canonical ISO code.

    Args:
        raw: ISO code, name, or alias (case-insensitive). Examples:
            ``"USD"``, ``"usd"``, ``"US Dollar"``, ``"yen"`` → ``"JPY"``,
            ``"pence"`` → ``"GBp"``.

    Returns:
        Canonical code in the casing it's stored under (typically
        uppercase, with ``"GBp"`` as a deliberate exception), or
        ``None`` if no match.
    """
    if not raw:
        return None
    return CURRENCY_ALIASES.get(raw.strip().lower())


def reload_holdings_class_resources() -> tuple[int, int, int]:
    """Re-read the three v0.13.0 reference CSVs from disk.

    Useful during development if the user edits one of the files. The
    module-level globals are updated in place so existing imports stay
    valid.

    Returns:
        ``(holdings_rows, sector_rows, currency_rows)`` after reload.
    """
    global HOLDINGS_CLASS_ROWS, HOLDINGS_CLASS_INDEX
    global HOLDINGS_SUB_ALIASES, HOLDINGS_AC_ALIASES
    global FUND_CLASS_ROWS, FUND_CLASS_ALIASES
    global SECTORS_ROWS, SECTOR_ALIASES, SECTOR_STYLE_ALIASES
    global CURRENCY_ROWS, CURRENCY_ALIASES
    global REGION_ROWS, REGION_ALIASES, SUPER_REGION_MEMBERS
    global REGION_KEYS, SUPER_REGION_KEYS

    (HOLDINGS_CLASS_ROWS, HOLDINGS_CLASS_INDEX,
     HOLDINGS_SUB_ALIASES, HOLDINGS_AC_ALIASES) = _load_holdings_classes()
    (FUND_CLASS_ROWS, FUND_CLASS_ALIASES) = _load_fund_classes()
    SECTORS_ROWS, SECTOR_ALIASES, SECTOR_STYLE_ALIASES = _load_sectors()
    CURRENCY_ROWS,  CURRENCY_ALIASES = _load_currencies()
    (REGION_ROWS, REGION_ALIASES, SUPER_REGION_MEMBERS) = _load_regions()
    REGION_KEYS = tuple(r["key"] for r in REGION_ROWS if r["kind"] == "region")
    SUPER_REGION_KEYS = tuple(r["key"] for r in REGION_ROWS
                              if r["kind"] == "super")
    return (len(HOLDINGS_CLASS_ROWS), len(SECTORS_ROWS), len(CURRENCY_ROWS))


# ---------------------------------------------------------------------------
# Resource-file editing (v0.15.0)
# ---------------------------------------------------------------------------
# The resolution dialog uses these helpers to write user-decided
# aliases back into the resource CSV files: for each unmatched value
# the user picks an existing canonical, and we append the raw value
# as a new entry in that canonical row's `matches` column. The CSV
# version is bumped so cache stamps detect the change and re-
# normalise lazily on next read.


def _bump_alias(rows: list[dict], canonical_key: str, canonical_value: str,
                matches_key: str, new_alias: str) -> bool:
    """Append ``new_alias`` to the row whose ``canonical_key == canonical_value``.

    Pure dict-list mutation. Used by the three add_*_alias helpers
    below to apply the same logic across all three resource files.

    Args:
        rows: List of normalised resource dicts (the same shape the
            loaders return).
        canonical_key: Field name holding the canonical value
            (``"sub_class"``, ``"sector"``, ``"code"``).
        canonical_value: The canonical we're adding the alias to
            (must already exist; caller's responsibility to validate).
        matches_key: Field name holding the matches list (always
            ``"matches"`` for our three files).
        new_alias: The raw value being added as an alias. Stored
            lower-cased. Whitespace-trimmed.

    Returns:
        ``True`` if the alias was newly added; ``False`` if it was
        already present (duplicate). Callers should still bump the
        file version on True, leave it alone on False.
    """
    alias = (new_alias or "").strip().lower()
    if not alias:
        return False
    for row in rows:
        if (row.get(canonical_key) or "").strip().lower() == canonical_value.strip().lower():
            existing = row.get(matches_key) or []
            if isinstance(existing, str):
                existing = [s.strip() for s in existing.split("|") if s.strip()]
            if alias in [e.lower() for e in existing]:
                return False
            existing.append(alias)
            row[matches_key] = existing
            return True
    return False


def add_holdings_class_alias(canonical_sub_class: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a sub_class's matches column. Bumps file version.

    Args:
        canonical_sub_class: The existing canonical sub_class to attach
            the alias to. Must already exist in the file.
        new_alias: The unmatched raw value the user wants to map onto
            this canonical.

    Returns:
        True if the file was modified, False if the alias was a
        duplicate or the canonical wasn't found.
    """
    rows = list(HOLDINGS_CLASS_ROWS)
    if not _bump_alias(rows, "sub_class", canonical_sub_class,
                       "matches", new_alias):
        return False
    # Serialise the matches list back into pipe-separated form.
    out = [{
        "asset_class":      r["asset_class"],
        "asset_class_desc": r["asset_class_desc"],
        "sub_class":        r["sub_class"],
        "sub_class_desc":   r["sub_class_desc"],
        "matches":          "|".join(r.get("matches") or []),
    } for r in rows]
    new_version = RESOURCE_VERSIONS["holdings_classes"] + 1
    write_versioned_csv(HOLDINGS_CLASS_FP, new_version,
                        ["asset_class","asset_class_desc",
                         "sub_class","sub_class_desc","matches"], out)
    reload_holdings_class_resources()
    return True


def add_sector_alias(canonical_sector: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a sector's matches column. Bumps file version."""
    rows = list(SECTORS_ROWS)
    if not _bump_alias(rows, "sector", canonical_sector,
                       "matches", new_alias):
        return False
    out = [{
        "sector":       r["sector"],
        "super_sector": r["super_sector"],
        "description":  r["description"],
        "matches":      "|".join(r.get("matches") or []),
    } for r in rows]
    new_version = RESOURCE_VERSIONS["sectors"] + 1
    write_versioned_csv(SECTORS_FP, new_version,
                        ["sector","super_sector","description","matches"], out)
    reload_holdings_class_resources()
    return True


def add_currency_alias(canonical_code: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a currency's matches column. Bumps file version."""
    rows = list(CURRENCY_ROWS)
    if not _bump_alias(rows, "code", canonical_code,
                       "matches", new_alias):
        return False
    out = [{
        "code":    r["code"],
        "name":    r["name"],
        "symbol":  r["symbol"],
        "matches": "|".join(r.get("matches") or []),
    } for r in rows]
    new_version = RESOURCE_VERSIONS["currencies"] + 1
    write_versioned_csv(CURRENCIES_FP, new_version,
                        ["code","name","symbol","matches"], out)
    reload_holdings_class_resources()
    return True


def add_country_alias(canonical_mstar_country: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a country's matches column. Bumps file version.

    Mirrors :func:`add_sector_alias` / :func:`add_currency_alias`.
    Writes the file, bumps :data:`RESOURCE_VERSIONS["countries"]`,
    reloads the in-memory tables so subsequent ``country_to_mstar``
    calls pick up the new alias immediately and the lazy facet
    migration in :mod:`porxpy.utils` re-normalises affected cache files
    on next read.

    Args:
        canonical_mstar_country: The existing canonical mstar_country
            value (e.g. ``"unitedstates"``) to attach the alias to.
            Case-insensitive.
        new_alias: The raw value the user wants to resolve to that
            canonical (e.g. ``"u.s. of a"``). Case-insensitive.

    Returns:
        ``True`` if the alias was newly written; ``False`` if the
        canonical wasn't found OR the alias was already present.
    """
    rows = [dict(r) for r in COUNTRY_ROWS]   # copy so we don't mutate live state
    if not _bump_alias(rows, "mstar_country", canonical_mstar_country,
                       "matches", new_alias):
        return False
    out = [{
        "mstar_country": r["mstar_country"],
        "alpha-2":       r["alpha-2"],
        "alpha-3":       r["alpha-3"],
        "country-code":  r["country-code"],
        "mstar_region":  r["mstar_region"],
        "matches":       "|".join(r.get("matches") or []),
    } for r in rows]
    new_version = RESOURCE_VERSIONS["countries"] + 1
    write_versioned_csv(
        COUNTRY_CODES_FP, new_version,
        ["mstar_country","alpha-2","alpha-3","country-code","mstar_region","matches"],
        out,
    )
    # reload_resources rebuilds COUNTRY_NAME_TO_MSTAR, COUNTRY_ROWS,
    # MSTAR_TO_CURRENCY and MSTAR_TO_REGION from the freshly-written
    # file — including the bumped version, which is what triggers
    # _maybe_migrate_facets on next cache_read.
    reload_resources()
    return True
