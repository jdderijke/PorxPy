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
import hashlib
import re
from pathlib import Path
from typing import Iterable

from porxpy.config import (
    LEGACY_FOCUS_TYPES,
    GEOGRAPHY_FP,
    
    CURRENCIES_FP,
    ASSET_FP,
    PRIMARY_CLASS_FP,
    SECTORS_FP,
    
)


# ---------------------------------------------------------------------------
# Resource CSV helpers
# ---------------------------------------------------------------------------
# A resource CSV is a header row and its data. Nothing above the header,
# no declared version.
#
# It was not always so. Until v0.64.0 every "matches"-bearing file
# carried a "Version=N" line as its first line, bumped on each edit and
# stamped onto every normalised cache file so stale entries could be
# spotted on read. The scheme failed at exactly the case it existed for:
# the number only moved when whoever wrote the file remembered to move
# it, so a file corrected by hand in a text editor — the ordinary way a
# taxonomy gets fixed — kept its old number, and every cache stamped
# with that number believed itself current and never re-normalised.
#
# The replacement is a content fingerprint over the bytes; see
# RESOURCE_FINGERPRINTS below. It answers the same question without
# depending on anybody remembering anything, and it answers it for hand
# edits too. Change detection lives there and only there — two
# mechanisms for one question is how the weaker one ends up load-bearing.

# Recognises a legacy "Version=N" first line so it can be SKIPPED on
# read (see parse_resource_csv). Nothing writes one any more, but a
# local copy of a pre-v0.64.0 file must keep working: without this the
# header row would be consumed as the version line and every column
# would come back None — a silent, total failure.
#
# Trailing delimiters are Excel padding the line out to the column
# count — "Version=8;;;;;;" is the same statement as "Version=8".
_VERSION_RE = re.compile(r"^\s*Version\s*=\s*(\d+)\s*[,;\s]*$")

# The delimiter each resource file was last READ with, so a rewrite
# gives it back in the same shape.
#
# Excel writes CSV using the locale's list separator, which in most of
# Europe is ";" rather than ",". Opening a resource file and saving it
# silently re-delimits every line — and a parser that assumes commas
# then reads the whole header as one column name and discards every
# row, leaving the taxonomy empty with no error. Sniffing costs nothing
# and makes the file survive a round trip through a spreadsheet.
_FILE_DELIMS: dict[str, str] = {}


def _detect_delimiter(header_line: str) -> str:
    """Guess a CSV delimiter from its header line.

    Counts candidates rather than using :class:`csv.Sniffer`, which
    needs more of the file than a header and can be thrown by a quoted
    value containing the other candidate.
    """
    counts = {d: header_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


# Which non-UTF-8 encoding each file was last decoded as, so the warning
# is printed once rather than once per loader pass.
_ENCODING_WARNED: dict[str, str] = {}


def _read_resource_text(fp: "Path") -> str:
    """Read a resource file whatever encoding it was last saved in.

    **v0.64.2.** Excel does not save UTF-8 by default. On a Western
    Windows it writes Windows-1252, so a file opened, edited and saved
    comes back with 0x97 where an em dash was — and utf-8 decoding
    raises on the first one, which surfaced as a freshness check dying
    with "invalid start byte" and no indication of which file or why.

    Tries UTF-8 (BOM-tolerant) first, then Windows-1252, then Latin-1,
    which cannot fail. Latin-1 is the last resort rather than the
    default precisely because it never errors: it would silently accept
    genuinely corrupt bytes as text.

    The fallback is announced, because a file that is no longer UTF-8
    will keep needing it until something rewrites it — and this
    project's own writer now does that on the next alias write.
    """
    raw = fp.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc != "utf-8-sig" and _ENCODING_WARNED.get(fp.name) != enc:
            # Once per file per encoding: the loaders read some files
            # more than once per reload, and three identical lines say
            # no more than one.
            _ENCODING_WARNED[fp.name] = enc
            print(f"[Resources] {fp.name} is not UTF-8 — decoded as {enc}. "
                  f"It will be rewritten as UTF-8 with a BOM on the next "
                  f"alias write, which Excel round-trips correctly.")
        return text
    return raw.decode("latin-1", errors="replace")


def parse_resource_csv(fp: Path) -> list[dict]:
    """Read a resource CSV.

    **v0.64.0.** The ``Version=N`` header is gone. It was a number
    somebody had to remember to bump, and a file edited by hand kept its
    old one — so every cache stamped with that number believed itself
    current and never re-normalised. The content fingerprint answers the
    actual question, and answers it for hand edits too.

    A leading ``Version=`` line is still SKIPPED if present, so a local
    copy of an older file keeps working. Without that check the header
    row would be consumed as the version line and every column would
    come back ``None`` — a silent, total failure.

    Args:
        fp: Path to the CSV. Must exist (caller's responsibility).

    Returns:
        Rows as dicts keyed by the header row.
    """
    text = _read_resource_text(fp)
    if text:
        first_line, _, rest = text.partition("\n")
        if _VERSION_RE.match(first_line.strip()):
            text = rest                              # legacy file

    lines = text.splitlines()
    delim = _detect_delimiter(lines[0]) if lines else ","
    _FILE_DELIMS[fp.name] = delim
    return list(csv.DictReader(lines, delimiter=delim))


def write_resource_csv(fp: Path, fieldnames: list[str],
                       rows: list[dict]) -> None:
    """Write a resource CSV: header row, then body. No version line.

    Args:
        fp: Target path.
        fieldnames: CSV header order.
        rows: Body rows; each must have keys subsetting fieldnames.
    """
    import io
    # Write the file back with the delimiter it was last read with, so a
    # file a spreadsheet re-delimited on save is not silently converted
    # back on the next alias write — which would leave the user's
    # editor and PorxPy disagreeing about the format every other save.
    delim = _FILE_DELIMS.get(fp.name, ",")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n",
                            delimiter=delim)
    writer.writeheader()
    writer.writerows(rows)
    # utf-8-SIG: the BOM is what makes Excel open the file as UTF-8
    # rather than guessing Windows-1252, so the Dutch aliases (België,
    # Oekraïne) survive a round trip through a spreadsheet instead of
    # coming back as mojibake. Without it, editing this file in Excel
    # corrupts every accented character in it.
    fp.write_text(buf.getvalue(), encoding="utf-8-sig")


# One change-detection mechanism, covering every resource file.
#
# v0.64.0 removed RESOURCE_VERSIONS, the declared ``Version=N`` on each
# file's first line. Two mechanisms answered the same question and the
# weaker one was load-bearing: the version only moved when a writer
# remembered to bump it, so a file edited by hand kept its old number
# and every cache stamped with it believed itself current.
#
# The fingerprint had its own gap — it was written for two of the five
# files, so an edit to currencies.csv, Fund_class_definitions.csv or the
# geography file changed no fingerprint at all. Both halves were
# partial, in different places, which is why removing either alone would
# have lost coverage. This dict is now filled from ONE list of files
# (see _refresh_file_stamps) so a new resource file cannot be added to
# half of it.
RESOURCE_FINGERPRINTS: dict[str, str] = {}


def _fingerprint(fp: Path) -> str:
    """Short content hash of a resource file, or "" when it is absent."""
    try:
        return hashlib.sha256(fp.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""



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


# ---------------------------------------------------------------------------
# Alias matching — one implementation, every tree (v0.71.0)
# ---------------------------------------------------------------------------
# Two problems solved together, because solving either alone would have
# left the other harder.
#
# 1. MATCHING WAS INCONSISTENT. Sector, asset, country and currency
#    aliases were keyed with _normalise_key (lower + strip, exact on the
#    whole string) while region and super_region used _norm_phrase
#    (punctuation collapsed). So `europe ex-uk` and `europe ex uk` both
#    resolved, while `financial-services` and `united-kingdom` did not
#    though their spaced forms did — the country facet disagreeing with
#    itself between its own levels. Every tree now normalises through
#    _alias_key, which is _norm_phrase. That is a WIDENING: everything
#    that matched before still matches, and punctuation variants that
#    previously had to be enumerated by hand now come for free.
#
# 2. AN ALIAS COULD ONLY BE THE WHOLE VALUE. Issuer data says
#    "US TREASURY BILLS 4.5% 2026" where the file says "treasury bills",
#    and there was no way to express "this text anywhere in the value".
#    A leading and/or trailing `*` in the matches column now says where
#    the alias may sit:
#
#        *treasury bills*   the value CONTAINS it
#        *treasury bills    the value ENDS WITH it
#        treasury bills*    the value STARTS WITH it
#        treasury bills     the value IS it (unchanged)
#
# Exact always beats wildcard, and among wildcards the LONGEST needle
# wins. Both rules exist so the answer cannot depend on the order rows
# happen to sit in the file: a wildcard is a broad claim, so anything
# more specific outranks it, and "government bond" outranks "bond".
_ALIAS_KINDS = ("contains", "endswith", "startswith")


def _alias_key(s: str | None) -> str:
    """The one normaliser every tree's aliases are keyed by.

    Deliberately _norm_phrase rather than _normalise_key: punctuation is
    a spelling difference, not a meaning difference, and issuer data is
    full of it. Both the stored alias and the incoming value go through
    this, so the comparison stays symmetric.
    """
    return _norm_phrase(s)


def _alias_index(raw_map: dict) -> tuple[dict, list]:
    """Split a freshly-built alias map into exact and wildcard forms.

    Called once per tree at load, on the map that tree just built, so
    the wildcard syntax and the normalisation rule are defined in one
    place and cannot drift between facets.

    Args:
        raw_map: ``{token: target}`` as the loader assembled it, tokens
            still carrying any ``*`` the file wrote.

    Returns:
        ``(exact, wild)``. ``exact`` is ``{normalised: target}``, a
        drop-in for the dict the loader used to return. ``wild`` is a
        list of ``(kind, needle, target)`` sorted longest-needle-first.
    """
    exact: dict[str, str] = {}
    wild: list[tuple[str, str, str]] = []
    for token, target in (raw_map or {}).items():
        t = (token or "").strip()
        if not t:
            continue
        lead, trail = t.startswith("*"), t.endswith("*")
        core = _alias_key(t.strip("*"))
        if not core:
            # A bare "*" would match everything. Refused rather than
            # honoured: a file that says "anything at all is technology"
            # is a mistake, and obeying it would be unexplainable from
            # the screen.
            continue
        if lead and trail:
            wild.append(("contains", core, target))
        elif lead:
            wild.append(("endswith", core, target))
        elif trail:
            wild.append(("startswith", core, target))
        else:
            exact.setdefault(core, target)
    # Longest needle first, then kind, then text: a total order, so two
    # installs with the same file always resolve a value the same way.
    wild.sort(key=lambda w: (-len(w[1]), w[0], w[1]))
    return exact, wild


def _alias_lookup(exact: dict, wild: list, raw: str | None) -> str | None:
    """Resolve one raw value against a tree's alias index.

    Exact first, then wildcards longest-needle-first. Returns the target
    or None.
    """
    key = _alias_key(raw)
    if not key:
        return None
    hit = exact.get(key)
    if hit is not None:
        return hit
    for kind, needle, target in wild or ():
        if kind == "contains":
            if needle in key:
                return target
        elif kind == "startswith":
            if key.startswith(needle):
                return target
        elif kind == "endswith":
            if key.endswith(needle):
                return target
    return None


# Wildcard tables, one per tree, filled by the loaders. Keyed by tree
# name rather than returned alongside each exact map, because the
# loaders' return signatures are unpacked at several call sites and the
# exact maps are consumed directly as dicts all over the codebase.
ALIAS_WILD: dict[str, list] = {
    "sector": [], "asset": [], "currency": [],
    "country": [], "region": [], "super_region": [], "focus": [],
}


# ---------------------------------------------------------------------------
# Geography — one file, one tree (v0.63.0)
# ---------------------------------------------------------------------------
# ``Geography_definitions.csv`` replaces country_codes.csv,
# country_currency.csv and regions.csv, in the same hierarchical schema
# the sector and holdings-class files already use:
#
#   type,name,description,parent_name,matches,is_default,attrs
#
# Types: country -> region -> super_region, plus focus_group.
#
# ``parent_name`` is SINGULAR here, which is the point. The old
# regions.csv listed several supers per region — developed, world,
# europe, pacific — flattening two orthogonal groupings and a universal
# into one column with nothing marking which was which. Rolling up to it
# would have counted Japan twice (asia AND pacific) and lost North
# America entirely. One parent makes the chain true by construction
# rather than by convention.
#
# ``focus_group`` rows sit OUTSIDE the chain: europe, asia, pacific and
# world are the pan-regional groupings a fund NAME can imply but a
# holding cannot belong to uniquely. They have no parent, no children,
# and are skipped by every facet consumer — they exist only to seed
# focus_type / focus_detail.
#
# ``attrs`` carries exactly two keys, and only what is actually used:
#   code=<ISO alpha-3>   currency=<ISO 4217>
# alpha-2 and the numeric code are SPELLINGS, not attributes, so they
# live in ``matches`` where the resolver already looks.
#
# Names are unique per TYPE, not globally: Morningstar has Japan and the
# United Kingdom as both a country and a region. Resolution therefore
# prefers the deepest type, which is what resolve_country_tree already
# does — a value naming a country is a country answer.

_GEOGRAPHY_TYPES = ("country", "region", "super_region", "focus_group")


def _load_geography() -> dict:
    """Load Geography_definitions.csv into every derived structure.

    Returns a dict of the structures the rest of the app reads. Kept as
    one loader because the file is one tree: building the country map
    without the region parents, or the region parents without the super
    rows, produces halves that can disagree.
    """
    out = {
        "country_rows": [], "region_rows": [],
        "name_to_country": {}, "country_to_region": {},
        "country_to_currency": {}, "region_to_super": {},
        "region_facet_aliases": {}, "focus_aliases": {},
        "super_aliases": {},
        "super_members": {},
    }
    if not GEOGRAPHY_FP.exists():
        print(f"[Resources] {GEOGRAPHY_FP.name} not found — country, region "
              f"and currency resolution will be no-ops.")
        return out

    raw_rows = parse_resource_csv(GEOGRAPHY_FP)
    rows = []
    for raw in raw_rows:
        t = (raw.get("type") or "").strip().lower()
        n = (raw.get("name") or "").strip()
        if not t or not n:
            continue
        if t not in _GEOGRAPHY_TYPES:
            print(f"[Resources] {GEOGRAPHY_FP.name}: unknown type {t!r} "
                  f"on {n!r} — row skipped.")
            continue
        rows.append({
            "type":        t,
            "name":        n,
            "label":       (raw.get("description") or "").strip() or n,
            "parent":      (raw.get("parent_name") or "").strip(),
            "matches":     _split_matches(raw.get("matches")),
            "is_default":  _truthy(raw.get("is_default")),
            "attrs":       parse_attrs(raw.get("attrs")),
        })

    by_type = {t: [r for r in rows if r["type"] == t] for t in _GEOGRAPHY_TYPES}

    # ── super_region ──────────────────────────────────────────────────
    for r in by_type["super_region"]:
        out["super_members"].setdefault(r["name"], set())

    # ── region ────────────────────────────────────────────────────────
    for r in by_type["region"]:
        out["region_rows"].append({
            "key": r["name"], "kind": "region", "label": r["label"],
            "parent": r["parent"], "matches": r["matches"],
        })
        if r["parent"]:
            out["region_to_super"][r["name"]] = r["parent"]
            out["super_members"].setdefault(r["parent"], set()).add(r["name"])
        # A region's own name and label resolve to it in a country column.
        for tok in [r["name"], r["label"], *r["matches"]]:
            # Raw, not normalised: _alias_index does the normalising,
            # and _norm_phrase would strip the '*' that carries the
            # wildcard before the index ever saw it.
            k = (tok or "").strip()
            if k:
                out["region_facet_aliases"].setdefault(k, r["name"])

    # super_region rows are facet-resolvable too: "Emerging Markets" in a
    # country column is a real answer, just a coarse one. They also seed
    # focus from a fund's name, so they feed both vocabularies.
    for r in by_type["super_region"]:
        out["region_rows"].append({
            "key": r["name"], "kind": "super", "label": r["label"],
            "parent": "", "matches": r["matches"],
        })
        for tok in [r["name"], r["label"], *r["matches"]]:
            # Raw, not normalised: _alias_index does the normalising,
            # and _norm_phrase would strip the '*' that carries the
            # wildcard before the index ever saw it.
            k = (tok or "").strip()
            if k:
                out["super_aliases"].setdefault(k, r["name"])

    # ── country ───────────────────────────────────────────────────────
    for r in by_type["country"]:
        name = r["name"].lower()
        code = (r["attrs"].get("code") or "").strip().upper()
        cur  = (r["attrs"].get("currency") or "").strip().upper()
        out["country_rows"].append({
            "name": name, "label": r["label"] or name,
            "parent": r["parent"], "code": code, "currency": cur,
            "matches": r["matches"],
        })
        if r["parent"]:
            out["country_to_region"][name] = r["parent"]
        if cur:
            out["country_to_currency"][name] = cur
        # _normalise_key, NOT _norm_phrase: this map is read by
        # country_to_mstar, which normalises with _normalise_key. The two
        # treat punctuation differently, so a key stored one way and
        # looked up the other silently misses — which is why every
        # hyphenated alias ("Zuid-Korea", "Nieuw-Zeeland") failed while
        # its unhyphenated neighbours worked. The region and focus maps
        # below keep _norm_phrase because THEIR readers use it.
        for tok in [name, r["label"], code, *r["matches"]]:
            k = _normalise_key(tok)
            if k:
                out["name_to_country"].setdefault(k, name)

    # ── focus_group ───────────────────────────────────────────────────
    # Name-derived focus only. Never a facet bucket: a holding does not
    # belong to "pacific" uniquely, which is exactly why these are not
    # in the chain.
    for r in by_type["focus_group"]:
        for tok in [r["name"], r["label"], *r["matches"]]:
            # Raw, not normalised: _alias_index does the normalising,
            # and _norm_phrase would strip the '*' that carries the
            # wildcard before the index ever saw it.
            k = (tok or "").strip()
            if k:
                out["focus_aliases"].setdefault(k, r["name"])

    # Same shared index as every other tree. name_to_country was the one
    # geography map still keyed with _normalise_key; it now matches its
    # own siblings, which is the inconsistency this release removes.
    for _map, _tree in (("region_facet_aliases", "region"),
                        ("super_aliases",        "super_region"),
                        ("name_to_country",      "country"),
                        ("focus_aliases",        "focus")):
        out[_map], ALIAS_WILD[_tree] = _alias_index(out[_map])
    return out


def reload_resources() -> tuple[int, int, int]:
    # NOTE: countries only, despite the name. Use reload_all_resources()
    # to re-read every file.
    """Re-read both CSVs from disk (useful when the user updates them).

    Updates the module-level :data:`COUNTRY_NAME_TO_MSTAR`,
    :data:`COUNTRY_ROWS`, :data:`MSTAR_TO_CURRENCY` and
    :data:`MSTAR_TO_REGION` maps in place so existing references stay
    valid.

    Returns:
        ``(alias_count, currency_count, region_count)`` after reloading.
    """
    _reload_geography()
    return (len(COUNTRY_NAME_TO_MSTAR), len(MSTAR_TO_CURRENCY),
            len(MSTAR_TO_REGION))


def _reload_geography() -> None:
    """Re-read Geography_definitions.csv into every derived structure.

    One file, one reload. The three predecessors reloaded independently,
    which was fine while they were three files and is a correctness bug
    now: reloading the country map without the region parents would
    leave a fund's country resolving while its region did not.
    """
    global COUNTRY_ROWS, COUNTRY_NAME_TO_MSTAR, MSTAR_TO_REGION
    global MSTAR_TO_CURRENCY, REGION_ROWS, REGION_FACET_ALIASES
    global REGION_ALIASES, SUPER_REGION_MEMBERS, REGION_SUPER
    global REGION_DEVELOPMENT, REGION_KEYS, SUPER_REGION_KEYS
    global DEVELOPMENT_KEYS

    geo = _load_geography()
    COUNTRY_ROWS          = geo["country_rows"]
    COUNTRY_NAME_TO_MSTAR = geo["name_to_country"]
    MSTAR_TO_REGION       = geo["country_to_region"]
    MSTAR_TO_CURRENCY     = geo["country_to_currency"]
    REGION_ROWS           = geo["region_rows"]
    REGION_FACET_ALIASES  = geo["region_facet_aliases"]
    REGION_ALIASES        = {**geo["region_facet_aliases"],
                             **geo["super_aliases"], **geo["focus_aliases"]}
    SUPER_REGION_MEMBERS  = geo["super_members"]
    REGION_SUPER          = geo["region_to_super"]
    REGION_DEVELOPMENT    = REGION_SUPER
    REGION_KEYS = tuple(r["key"] for r in REGION_ROWS if r["kind"] == "region")
    SUPER_REGION_KEYS = tuple(r["key"] for r in REGION_ROWS
                              if r["kind"] == "super")
    DEVELOPMENT_KEYS = SUPER_REGION_KEYS


def country_to_mstar(raw: str | None) -> str | None:
    """Resolve any country name/code to the canonical mstar_country form.

    Args:
        raw: Free-form country string from a holdings file.

    Returns:
        Lowercased ``mstar_country`` value (e.g. ``"unitedstates"``), or
        ``None`` if the input couldn't be matched against any known alias.
    """
    return _alias_lookup(COUNTRY_NAME_TO_MSTAR, ALIAS_WILD["country"], raw)


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
# ---------------------------------------------------------------------------
# Just-in-time freshness (v0.54.0)
# ---------------------------------------------------------------------------
# A resource file edited while the app is running used to have no effect
# until a restart, and the user had no way to tell which ruleset a given
# classification had used.
#
# Re-reading and re-parsing on every call would be far too slow — 250
# country rows per holding — so what happens per call is a stat(): if
# mtime and size are unchanged, nothing is read. The check is also
# deliberately NOT done mid-request; see ``ensure_resources_fresh``.
_FILE_STAMPS: dict[str, tuple[float, int]] = {}


def _file_stamp(fp: Path) -> tuple[float, int]:
    try:
        st = fp.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, 0)


# The one list of resource files. Both the change-detection dicts are
# filled from it, so a file cannot be added to one and forgotten in the
# other — which is exactly how the fingerprint came to cover two of five.
_RESOURCE_FILES: tuple[tuple[str, "Path"], ...] = (
    ("Primary_asset_class_definitions.csv", PRIMARY_CLASS_FP),
    ("Asset_definitions.csv",           ASSET_FP),
    ("Sector_definitions.csv",         SECTORS_FP),
    ("Currency_definitions.csv",       CURRENCIES_FP),
    ("Geography_definitions.csv",      GEOGRAPHY_FP),
)


def resources_changed_on_disk() -> list[str]:
    """Names of the resource files whose bytes differ from what is loaded.

    Returns:
        A list of file names, empty when everything in memory matches
        disk.
    """
    changed = []
    for name, fp in _RESOURCE_FILES:
        stamp = _file_stamp(fp)
        if _FILE_STAMPS.get(name) != stamp:
            changed.append(name)
    return changed


def ensure_resources_fresh() -> list[str]:
    """Reload the resource files if any has changed on disk.

    Called at the START of a request rather than per classification, so
    every row in one upload is classified under one ruleset. A file
    saved halfway through a commit takes effect on the next request,
    which is the only answer that keeps a single import internally
    consistent.

    Returns:
        The names of the files that had changed, empty when nothing did.
    """
    changed = resources_changed_on_disk()
    if changed:
        reload_all_resources()
    return changed


def reload_all_resources() -> None:
    """Re-read every resource file.

    Two reload functions used to exist and neither covered the whole
    set, so calling the one whose name invited it left most files
    stale. There is one now.
    """
    global PRIMARY_CLASS_ROWS, PRIMARY_CLASS_ALIASES, PRIMARY_CLASS_STYLE
    global ASSET_ROWS, ASSET_NODE_ALIASES, ASSET_LEVEL_OF
    global ASSET_CHILDREN, ASSET_PARENT
    global SECTORS_ROWS, SECTOR_ALIASES, SECTOR_STYLE_ALIASES
    global CURRENCY_ROWS, CURRENCY_ALIASES, CURRENCY_CODES

    (PRIMARY_CLASS_ROWS, PRIMARY_CLASS_ALIASES,
     PRIMARY_CLASS_STYLE) = _load_primary_asset_classes()
    (ASSET_ROWS, ASSET_NODE_ALIASES, ASSET_LEVEL_OF,
     ASSET_CHILDREN, ASSET_PARENT) = _load_assets()
    SECTORS_ROWS, SECTOR_ALIASES, SECTOR_STYLE_ALIASES = _load_sectors()
    CURRENCY_ROWS, CURRENCY_ALIASES, CURRENCY_CODES = _load_currencies()
    _reload_geography()
    _refresh_file_stamps()
    validate_resources()


# ---------------------------------------------------------------------------
# Cross-file consistency (v0.64.1)
# ---------------------------------------------------------------------------
# The resource files reference each other. A country's ``currency=``
# names a row in the currency file; every ``parent_name`` names a row of
# the level above. Nothing checked either, so a dangling reference
# surfaced as a mystery: Sierra Leone resolved as a country, its
# currency SLE resolved as nothing, and the Resolve dialog asked the
# user to fix a value one of their own files declared correct. The app
# was disagreeing with itself and putting the argument on screen.
#
# Redenominations guarantee this recurs — Venezuela, Turkey and Zimbabwe
# have all changed codes — so this is a check, not a one-time fix.

_PARENT_OF_TYPE = {
    # file key      child type       -> required parent type ("" = none)
    "geography":  {"country": "region", "region": "super_region",
                   "super_region": "", "focus_group": ""},
    "sectors":    {"sub_sector": "sector", "sector": "super_sector",
                   "super_sector": ""},
    "currencies": {"currency": ""},
}


def validate_resources() -> list[str]:
    """Check every cross-reference and collision. Returns problem lines.

    Run at import and after every reload, so a file edited mid-session
    is validated the moment it is picked up rather than at next start.

    Never raises and never mutates: a bad reference degrades one lookup,
    and refusing to start over it would be a worse failure than the one
    being reported.
    """
    problems: list[str] = []

    # 1. Currency references from the geography file.
    known_currencies = {r["code"].upper() for r in CURRENCY_ROWS if r.get("code")}
    if known_currencies:
        for country, cur in sorted(MSTAR_TO_CURRENCY.items()):
            if cur and cur.upper() not in known_currencies:
                problems.append(
                    f"Geography_definitions.csv: country {country!r} has "
                    f"currency={cur!r}, which is not a row in "
                    f"Currency_definitions.csv")

    # 2. parent_name references inside the geography file.
    region_names = set(REGION_KEYS)
    super_names  = set(SUPER_REGION_KEYS)
    for r in COUNTRY_ROWS:
        p = (r.get("parent") or "").strip()
        if not p:
            problems.append(f"Geography_definitions.csv: country "
                            f"{r['name']!r} has no parent_name")
        elif p not in region_names:
            problems.append(f"Geography_definitions.csv: country "
                            f"{r['name']!r} names parent {p!r}, which is "
                            f"not a region row")
    for r in REGION_ROWS:
        if r.get("kind") != "region":
            continue
        p = (r.get("parent") or "").strip()
        if p and p not in super_names:
            problems.append(f"Geography_definitions.csv: region "
                            f"{r['key']!r} names parent {p!r}, which is not "
                            f"a super_region row")

    # 3. parent_name references inside the sector file.
    for name, level in SECTOR_LEVEL_OF.items():
        if level == "sub_sector":
            parent = SUB_SECTOR_PARENT.get(name)
            if not parent:
                problems.append(f"Sector_definitions.csv: sub sector "
                                f"{name!r} has no parent sector")
            elif SECTOR_LEVEL_OF.get(parent) != "sector":
                problems.append(f"Sector_definitions.csv: sub sector "
                                f"{name!r} names parent {parent!r}, which is "
                                f"not a sector row")

    # 4. Match-token collisions WITHIN a vocabulary. An alias is a claim
    #    that one spelling means one thing; two rows claiming it makes
    #    the file self-contradicting, and which wins then depends on
    #    load order — a bug that shows up only after a restart, often on
    #    a different machine.
    #
    #    Checked here rather than before each alias write because a file
    #    hand-edited in a spreadsheet can introduce a collision just as
    #    easily as the dialog can, and only one of those two routes
    #    would ever call a pre-commit hook.
    for label, rows_, key_field in (
            ("Geography_definitions.csv", COUNTRY_ROWS, "name"),
            ("Currency_definitions.csv",  CURRENCY_ROWS, "code"),
            ("Primary_asset_class_definitions.csv", PRIMARY_CLASS_ROWS, "key")):
        claimed: dict[str, str] = {}
        for r in rows_:
            owner = (r.get(key_field) or "").strip()
            for tok in r.get("matches") or []:
                k = _normalise_key(tok)
                if not k:
                    continue
                prev = claimed.get(k)
                if prev and prev != owner:
                    problems.append(
                        f"{label}: {tok!r} is claimed by both {prev!r} and "
                        f"{owner!r}; one spelling cannot mean two things")
                claimed.setdefault(k, owner)

    # 5. style_match phrases across classification keys. A phrase that
    #    means two classes makes the name evidence ambiguous, and
    #    primary_class_name_hits would return both with no way to
    #    choose — the caller would silently take whichever its own
    #    precedence rules happened to reach first.
    claimed_style: dict[str, str] = {}
    for key, phrases in PRIMARY_CLASS_STYLE.items():
        for phrase in phrases:
            prev = claimed_style.get(phrase)
            if prev and prev != key:
                problems.append(
                    f"Primary_asset_class_definitions.csv: style_match "
                    f"{phrase!r} is claimed by both {prev!r} and {key!r}; a "
                    f"name phrase pointing at two classes cannot decide "
                    f"either")
            claimed_style.setdefault(phrase, key)

    for line in problems:
        print(f"[Resources] INCONSISTENCY — {line}")
    return problems


def _refresh_file_stamps() -> None:
    """Re-read every resource file's stamp, and validate if any changed.

    **v0.64.1.** One trigger for the whole consistency check. Every path
    that can change a resource file — startup, an explicit reload, the
    just-in-time freshness check at the start of a request, and every
    alias write — ends here, so hooking the check to the fingerprint
    covers all of them without any of them having to remember.

    Fingerprint-driven rather than event-driven on purpose: a file
    changed on disk by a text editor is indistinguishable from one
    changed by an alias write, and both need the same check. Validating
    after an alias write is strictly more than that write can break —
    it only ever touches ``matches``, never a key — but the redundancy
    costs one pass over rows already in memory and removes a class of
    "which caller forgot to check".
    """
    changed = False
    for name, fp in _RESOURCE_FILES:
        _FILE_STAMPS[name] = _file_stamp(fp)
        fresh = _fingerprint(fp)
        if RESOURCE_FINGERPRINTS.get(name) != fresh:
            changed = True
        RESOURCE_FINGERPRINTS[name] = fresh
    if changed:
        validate_resources()












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
# ---------------------------------------------------------------------------
# Shared resource-file helpers
# ---------------------------------------------------------------------------
# Attribute cells are ``key=value;key=value``. Pairs are separated by
# ``;`` because ``|`` already separates the values INSIDE one attribute
# (a sector's ``style_match`` is itself a list).
_ATTR_PAIR_SEP = ";"
_ATTR_KV_SEP = "="

_TRUTHY = ("true", "yes", "1", "y", "x")
_FALSY = ("", "false", "no", "0", "n")


def parse_attrs(raw: str | None) -> dict[str, str]:
    """Parse an ``attrs`` cell into a dict. Malformed pairs are skipped."""
    out: dict[str, str] = {}
    for pair in (raw or "").split(_ATTR_PAIR_SEP):
        pair = pair.strip()
        if not pair or _ATTR_KV_SEP not in pair:
            continue
        k, _, v = pair.partition(_ATTR_KV_SEP)
        k = k.strip().lower()
        if k:
            out[k] = v.strip()
    return out

def format_attrs(attrs: dict[str, str] | None) -> str:
    """Serialise an attrs dict back into its cell form."""
    return _ATTR_PAIR_SEP.join(
        f"{k}{_ATTR_KV_SEP}{v}"
        for k, v in sorted((attrs or {}).items()) if v != "")

def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TRUTHY

def _flag_problem(raw: str | None) -> str:
    """Describe why an is_default cell is neither true nor false.

    A value here that is neither is almost always a column shift — an
    alias appended with a comma instead of a pipe ends up in this cell
    and is silently ignored, while the matches column never sees it. The
    symptom is an alias that has visibly been added and does nothing, so
    it is worth naming loudly.
    """
    v = (raw or "").strip().lower()
    if v in _TRUTHY or v in _FALSY:
        return ""
    return (f"is_default is {raw.strip()!r}, which is neither true nor "
            f"false — if you meant to add a match, it belongs in the "
            f"matches column separated by '|', not after a comma")


# ---------------------------------------------------------------------------
# Assets — super_class -> asset_class -> sub_class (v0.70.0)
# ---------------------------------------------------------------------------
# Asset_definitions.csv replaces Holdings_class_definitions.csv (the
# per-holding asset_class + sub_class pair) and Fund_class_definitions.csv
# (the breakdown vocabulary the rollup aggregated to). Those were two
# files describing one taxonomy at different grains, disagreeing on
# spelling — "bond" in one, "fixed_income" in the other — for the same
# concept.
#
# The file was already a tree: asset_class rows with sub_class children
# naming their parent. Only the storage and the code treated the two as
# independent columns.
_ASSET_TYPES = ("sub_class", "asset_class", "super_class")
# Finest first, matching FACET_LEVELS.
ASSET_LEVELS: tuple[str, ...] = ("sub_class", "asset_class", "super_class")


def _load_assets() -> tuple[list[dict], dict[str, str], dict[str, str],
                            dict[str, list[str]], dict[str, str]]:
    """Load Asset_definitions.csv into the tree lookup structures.

    Returns:
        ``(rows, node_aliases, level_of, children_of, parent_of)``.
        ``rows`` is every node in file order, each with its level,
        description and matches — the display order for pickers.
    """
    rows: list[dict] = []
    node_aliases: dict[str, str] = {}
    level_of: dict[str, str] = {}
    children_of: dict[str, list[str]] = {}
    parent_of: dict[str, str] = {}

    if not ASSET_FP.exists():
        print(f"[Resources] {ASSET_FP.name} not found — asset "
              f"classification will be a no-op.")
        ALIAS_WILD["asset"] = []
        return rows, node_aliases, level_of, children_of, parent_of

    problems: list[str] = []
    parsed: list[dict] = []
    for raw in parse_resource_csv(ASSET_FP):
        rtype = (raw.get("type") or "").strip().lower()
        name  = (raw.get("name") or "").strip().lower()
        # Blank separator rows keep the file readable in a spreadsheet.
        if not rtype and not name:
            continue
        if rtype not in _ASSET_TYPES:
            problems.append(f"unknown type {rtype!r} on row {name!r} — "
                            f"expected one of {list(_ASSET_TYPES)}")
            continue
        if not name:
            problems.append(f"a {rtype} row has no name")
            continue
        if name in level_of:
            problems.append(f"duplicate node {name!r}")
            continue
        level_of[name] = rtype
        parsed.append({
            "name":    name,
            "level":   rtype,
            "parent":  (raw.get("parent_name") or "").strip().lower(),
            "matches": _split_matches(raw.get("matches")),
            "default": _truthy(raw.get("is_default")),
            "description": (raw.get("description") or "").strip(),
        })

    # Parents, validated against the level above. A node naming a parent
    # at the wrong level is a file error, not something to work around.
    order = {lvl: i for i, lvl in enumerate(ASSET_LEVELS)}
    for e in parsed:
        want = order[e["level"]] + 1
        if e["level"] == "super_class":
            if e["parent"]:
                problems.append(f"super_class {e['name']!r} names a parent")
            continue
        if not e["parent"]:
            problems.append(f"{e['level']} {e['name']!r} has no parent_name")
            continue
        plvl = level_of.get(e["parent"])
        if plvl is None:
            problems.append(f"{e['level']} {e['name']!r} names parent "
                            f"{e['parent']!r}, which is not defined in this file")
            continue
        if order.get(plvl) != want:
            problems.append(f"{e['level']} {e['name']!r} names parent "
                            f"{e['parent']!r}, which is a {plvl} — a parent "
                            f"must sit exactly one level up")
            continue
        parent_of[e["name"]] = e["parent"]
        children_of.setdefault(e["parent"], []).append(e["name"])

    # Aliases. A canonical name always outranks a match, and a DEEPER
    # canonical outranks a shallower one — the same rule the sector tree
    # draws, and for the same reason: without it a sub class sharing a
    # word with its parent's matches is unreachable by its own name.
    for e in parsed:
        node_aliases[e["name"]] = e["name"]
    for e in parsed:
        for m in e["matches"]:
            k = _normalise_key(m)
            if not k or k in level_of:
                continue
            prior = node_aliases.get(k)
            if prior is None:
                node_aliases[k] = e["name"]
                continue
            if prior == e["name"]:
                continue
            if order[level_of[e["name"]]] < order[level_of[prior]]:
                node_aliases[k] = e["name"]
                keep, lose = e["name"], prior
            else:
                keep, lose = prior, e["name"]
            problems.append(
                f"match {m!r} is claimed by {lose!r} and {keep!r}; the "
                f"deeper node wins, but one spelling meaning two things "
                f"is an authoring mistake")

        rows.append({"key": e["name"], "level": e["level"],
                     "description": e["description"],
                     "matches": e["matches"], "is_default": e["default"]})

    if not rows:
        problems.append("no asset rows found — asset classification will "
                        "be a no-op")
    if problems:
        print(f"[Resources] {ASSET_FP.name}: {len(problems)} problem(s) "
              f"found on load:")
        for prob in problems:
            print(f"    - {prob}")

    node_aliases, ALIAS_WILD["asset"] = _alias_index(node_aliases)
    return rows, node_aliases, level_of, children_of, parent_of


ASSET_ROWS:        list[dict]
ASSET_NODE_ALIASES: dict[str, str]
ASSET_LEVEL_OF:    dict[str, str]
ASSET_CHILDREN:    dict[str, list[str]]
ASSET_PARENT:      dict[str, str]
(ASSET_ROWS, ASSET_NODE_ALIASES, ASSET_LEVEL_OF,
 ASSET_CHILDREN, ASSET_PARENT) = _load_assets()


def resolve_asset_tree(raw: str | None,
                       not_applicable: bool = False) -> dict[str, str]:
    """Resolve a raw asset value to all three levels at once.

    The deepest match wins, ancestors are derived, and a level FINER
    than the match is derived only when the node has exactly one child —
    "equity" has a single asset class beneath it, so naming equity names
    that too, while a node with several children leaves the level below
    unknown rather than inventing a grain the source never stated.

    That rule is safe precisely because the stated level travels with
    the value: if a single-child node later gains a sibling, lazy
    migration re-derives and those rows correctly become unknown.

    Args:
        raw: Any spelling, at any of the three levels, or blank.
        not_applicable: True when the concept genuinely does not apply.

    Returns:
        ``{"sub_class", "asset_class", "super_class", "level",
        "matched"}``. Unresolved levels are ``"unknown"``; every level
        is ``"n/a"`` when ``not_applicable``.
    """
    from porxpy.breakdowns import NA_KEY, UNKNOWN_KEY

    if not_applicable:
        return {lvl: NA_KEY for lvl in ASSET_LEVELS} | {"level": "", "matched": ""}

    out = {lvl: UNKNOWN_KEY for lvl in ASSET_LEVELS} | {"level": "", "matched": ""}
    node = _alias_lookup(ASSET_NODE_ALIASES, ALIAS_WILD["asset"], raw)
    if not node:
        return out

    level = ASSET_LEVEL_OF.get(node, "")
    out["level"] = level
    out["matched"] = node

    # The reserved ``n/a`` node, exactly as resolve_sector_tree treats it
    # (v0.76.2) — see the long note there for why it is one node rather
    # than one per level. Carried into every tree so the vocabulary is
    # uniformly capable: a file can declare "these spellings mean the
    # question does not apply" for any facet, not just the one that
    # happened to need it first. No asset file uses it today; a tree that
    # never defines the node simply never matches it.
    if node == NA_KEY:
        for lv in ASSET_LEVELS:
            out[lv] = NA_KEY
        return out
    if not level:
        return out

    # The matched node and everything above it.
    cur = node
    while cur:
        out[ASSET_LEVEL_OF[cur]] = cur
        cur = ASSET_PARENT.get(cur, "")

    # Below it, only while the answer is not a guess.
    cur = node
    while True:
        kids = ASSET_CHILDREN.get(cur) or []
        if len(kids) != 1:
            break
        cur = kids[0]
        out[ASSET_LEVEL_OF[cur]] = cur
    return out


def asset_key_at_level(key: str, level: str) -> str | None:
    """Move one asset bucket key to ``level``, or ``None`` if it cannot.

    Walks up through the parent map, and down only through single-child
    nodes — the same rule resolve_asset_tree applies, so a distribution
    re-bucketed at a level agrees with the rows it came from.
    """
    from porxpy.breakdowns import _RESIDUAL_KEYS

    if key in _RESIDUAL_KEYS:
        return key
    lvl = ASSET_LEVEL_OF.get(key, "")
    if not lvl:
        return None
    if lvl == level:
        return key

    order = {l: i for i, l in enumerate(ASSET_LEVELS)}
    if order[level] > order[lvl]:
        cur = key
        while cur and ASSET_LEVEL_OF.get(cur) != level:
            cur = ASSET_PARENT.get(cur, "")
        return cur or None
    cur = key
    while ASSET_LEVEL_OF.get(cur) != level:
        kids = ASSET_CHILDREN.get(cur) or []
        if len(kids) != 1:
            return None
        cur = kids[0]
    return cur


def list_asset_nodes(level: str | None = None) -> list[dict]:
    """Asset-tree nodes for picker population, in file order.

    Returns:
        ``[{"key", "level", "label", "description"}, ...]``.
    """
    return [{"key": r["key"], "level": r["level"],
             "label": r["key"].replace("_", " ").title(),
             "description": r.get("description") or ""}
            for r in ASSET_ROWS if not level or r["level"] == level]


# ---------------------------------------------------------------------------
# Primary asset class — the fund-level CLASSIFICATION (v0.69.0)
# ---------------------------------------------------------------------------
# What kind of fund this IS, as a single value: equity / fixed_income /
# cash / mixed / commodity / other. Captured from Yahoo, a factsheet,
# justETF or the user — never derived from the fund's holdings, and
# deliberately NOT the same thing as the asset_class breakdown facet.
# A 60/40 fund is "mixed" here while its facet is 60% equity / 40%
# fixed income; scoring.py reads this one to pick a peer group.
#
# Until v0.69.0 the vocabulary was a hardcoded list in config.py and the
# name/category keywords that detect it were four more lists inside
# extractors.detect_asset_class. Neither could be corrected without a
# code change, and the vocabulary could disagree with the file that
# described it.
#
# Two alias columns, the same distinction the sector file draws:
#   matches      spellings of the value itself — "fixed income" IS the
#                fixed_income class.
#   style_match  phrases that, found in a fund's NAME or category, imply
#                the class. "gilt" is not a spelling of fixed_income; it
#                is evidence that a fund called that holds bonds.
_PRIMARY_CLASS_TYPE = "primary_asset_class"


def _load_primary_asset_classes() -> tuple[list[dict], dict[str, str],
                                           dict[str, list[str]]]:
    """Load Primary_asset_class_definitions.csv.

    Returns:
        ``(rows, aliases, style_matches)`` — ``rows`` in file order
        (the display order), ``aliases`` mapping every spelling plus
        each canonical key to a canonical key, and ``style_matches``
        mapping each canonical key to its name/category phrases.
    """
    rows: list[dict] = []
    aliases: dict[str, str] = {}
    style: dict[str, list[str]] = {}

    if not PRIMARY_CLASS_FP.exists():
        print(f"[Resources] {PRIMARY_CLASS_FP.name} not found — the fund "
              f"classification vocabulary is empty; every primary asset "
              f"class will fail validation until the file is restored.")
        return rows, aliases, style

    problems: list[str] = []
    for raw in parse_resource_csv(PRIMARY_CLASS_FP):
        rtype = (raw.get("type") or "").strip().lower()
        name  = (raw.get("name") or "").strip().lower()
        if not rtype and not name:
            continue
        if rtype != _PRIMARY_CLASS_TYPE:
            problems.append(f"unknown type {rtype!r} on row {name!r} — "
                            f"expected {_PRIMARY_CLASS_TYPE!r}")
            continue
        if not name:
            problems.append(f"a {rtype} row has no name")
            continue
        if name in aliases and aliases[name] == name:
            problems.append(f"duplicate primary asset class {name!r}")
            continue

        attrs = parse_attrs(raw.get("attrs"))
        rows.append({
            "key":         name,
            "description": (raw.get("description") or "").strip(),
            "matches":     _split_matches(raw.get("matches")),
        })
        aliases[name] = name
        for m in _split_matches(raw.get("matches")):
            k = _normalise_key(m)
            if not k:
                continue
            prev = aliases.get(k)
            if prev and prev != name:
                problems.append(
                    f"{m!r} is claimed by both {prev!r} and {name!r}; "
                    f"one spelling cannot mean two things")
                continue
            aliases[k] = name
        style[name] = [_norm_phrase(p)
                       for p in _split_matches(attrs.get("style_match"))
                       if _norm_phrase(p)]
        # Phrases are matched on WORD boundaries, not as bare substrings.
        # _norm_phrase reduces punctuation to spaces, which turns "s&p"
        # into "s p" — and "iShares Physical" contains "s p" across its
        # word break. Substring matching would then classify every
        # physically-backed fund as equity. The old hardcoded list was
        # matched against a merely-lowercased name and never hit this;
        # moving the phrases into a normalised file is what exposes it.

    if not rows:
        problems.append("no primary_asset_class rows found — fund "
                        "classification will fail validation")
    if problems:
        print(f"[Resources] {PRIMARY_CLASS_FP.name}: {len(problems)} "
              f"problem(s) found on load:")
        for prob in problems:
            print(f"    - {prob}")

    return rows, aliases, style


PRIMARY_CLASS_ROWS:   list[dict]
PRIMARY_CLASS_ALIASES: dict[str, str]
PRIMARY_CLASS_STYLE:   dict[str, list[str]]
(PRIMARY_CLASS_ROWS, PRIMARY_CLASS_ALIASES,
 PRIMARY_CLASS_STYLE) = _load_primary_asset_classes()


def primary_asset_classes(context: dict | None = None) -> tuple[str, ...]:
    """Canonical primary-asset-class keys, in file order.

    Args:
        context: Accepted and ignored — the signature matches the other
            ``vocab_fn`` resolvers so config.field_vocab can call any of
            them without knowing which kind it has.

    Returns:
        Tuple of lowercase canonical keys.
    """
    return tuple(r["key"] for r in PRIMARY_CLASS_ROWS if r.get("key"))


def resolve_primary_asset_class(raw: str | None) -> str | None:
    """Coerce any spelling of a fund CLASSIFICATION to canonical form.

    Args:
        raw: Free-form value, any casing ("Fixed Income", "balanced").

    Returns:
        Canonical key, or ``None`` on no match — the caller decides
        whether an unrecognised value is an error or folds to "other".
    """
    if not raw:
        return None
    return PRIMARY_CLASS_ALIASES.get(_normalise_key(raw))


def primary_class_name_hits(haystack: str | None) -> list[str]:
    """Classification keys whose name/category phrases occur in a string.

    The evidence half of classification: an issuer encodes the kind of
    fund in its name, so "iShares Core Global Aggregate Bond" carries
    the word that identifies it. Returns every hit rather than a winner,
    because the caller weighs these against structural signals from
    Yahoo and only it knows their precedence.

    Args:
        haystack: Fund name, category, legal type — any free text.

    Returns:
        Canonical keys with at least one phrase hit, in file order.
    """
    hay = _norm_phrase(haystack or "")
    if not hay:
        return []
    words = hay.split()
    def _hit(phrase: str) -> bool:
        parts = phrase.split()
        if not parts:
            return False
        n = len(parts)
        return any(words[i:i + n] == parts
                   for i in range(len(words) - n + 1))
    return [r["key"] for r in PRIMARY_CLASS_ROWS
            if any(_hit(p) for p in PRIMARY_CLASS_STYLE.get(r["key"], []))]


def list_primary_asset_classes() -> list[dict]:
    """The classification vocabulary for dropdown population.

    Returns:
        ``[{"key", "label", "description"}, ...]`` in file order.
    """
    return [{"key": r["key"],
             "label": r["key"].replace("_", " ").title(),
             "description": r.get("description") or ""}
            for r in PRIMARY_CLASS_ROWS if r.get("key")]


# ---------------------------------------------------------------------------
# Morningstar sectors (with matches column)
# ---------------------------------------------------------------------------
# Lookup tables:
#   SECTORS_ROWS     ordered list of dicts (preserves CSV order)
#   SECTOR_ALIASES   alternative spelling (lowercase) → canonical sector


# The closed type vocabulary for sectors.csv. Three levels, coarsest
# last: a sub sector rolls into a sector, a sector into a super sector.
# Only the middle level is used for classification today; the other two
# exist so the file can carry the hierarchy before anything consumes it.
_SECTOR_TYPES = ("sub_sector", "sector", "super_sector")


def _migrate_sectors_file() -> bool:
    """Convert the pre-0.55 sectors.csv to the hierarchical schema.

    The old shape was ``sector,super_sector,description,matches,
    style_match`` — one row per sector, with the super sector named
    inline and never defined anywhere. It had no description, no
    matches of its own, and no way to add any.

    ``style_match`` moves into ``attrs``. It is a genuinely different
    question from ``matches``: ``matches`` normalises a holding's own
    sector value, while ``style_match`` recognises a sector inside a
    *fund's name*. One column doing both jobs is what the rework is for.

    Returns:
        True if the file was rewritten.
    """
    if not SECTORS_FP.exists():
        return False
    text = _read_resource_text(SECTORS_FP)
    version, body = 0, text
    first, _, rest = text.partition("\n")
    m = _VERSION_RE.match(first)
    if m:
        version, body = int(m.group(1)), rest

    reader = csv.DictReader(body.splitlines())
    names = [f.strip() for f in (reader.fieldnames or [])]
    if "type" in names:
        return False                              # already migrated
    if "sector" not in names:
        return False                              # not a shape we know
    old_rows = list(reader)
    if not old_rows:
        return False

    supers: list[str] = []
    sectors: list[dict] = []
    for r in old_rows:
        sec = (r.get("sector") or "").strip().lower()
        if not sec:
            continue
        sup = (r.get("super_sector") or "").strip().lower()
        if sup and sup not in supers:
            supers.append(sup)
        style = (r.get("style_match") or "").strip()
        sectors.append({
            "type": "sector", "name": sec,
            "description": (r.get("description") or "").strip(),
            "parent_name": sup,
            "matches": (r.get("matches") or "").strip(),
            "is_default": "",
            # style_match keeps its pipes: attrs pairs are separated by
            # ";", so a value may contain "|" freely.
            "attrs": format_attrs({"style_match": style}) if style else "",
        })

    out = [{
        "type": "super_sector", "name": sup,
        # The old file named super sectors but never defined them, so
        # there is nothing to carry over. Fill these in by hand.
        "description": "", "parent_name": "", "matches": "",
        "is_default": "", "attrs": "",
    } for sup in supers]
    out.extend(sectors)

    write_resource_csv(SECTORS_FP,
        ["type", "name", "description", "parent_name", "matches",
         "is_default", "attrs"], out)
    print(f"[Resources] {SECTORS_FP.name}: migrated to the hierarchical "
          f"schema — {len(supers)} super_sector and {len(sectors)} sector "
          f"rows (version {version} -> {version + 1}). Super sectors have "
          f"no descriptions; the old file never defined them.")
    return True


def _load_sectors() -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """Load sectors.csv into the sector lookup structures.

    Three levels live in one file, distinguished by ``type``. Only
    ``sector`` rows are returned in ``rows`` — that is the level every
    consumer classifies against, and the level the Targets editor and
    the AI prompt enumerate. Sub sectors and super sectors are validated
    and indexed so the hierarchy is available, without changing what
    "a sector" means to anything already using it.

    * ``matches`` → :data:`SECTOR_ALIASES`. Whatever spelling turns up in
      a holdings file for that sector.
    * ``attrs``' ``style_match`` → :data:`SECTOR_STYLE_ALIASES`. Phrases
      that, found in a *fund's name*, imply the fund targets that sector.
      A different question from ``matches``, which is why it is a
      different field.

    Returns:
        ``(rows, aliases, style_aliases)``.
    """
    global SUB_SECTOR_PARENT, SECTOR_SUPER, SECTOR_LEVEL_OF, SECTOR_NODE_ALIASES

    rows: list[dict] = []
    aliases: dict[str, str] = {}
    style_aliases: dict[str, str] = {}
    node_aliases: dict[str, str] = {}
    SUB_SECTOR_PARENT = {}
    SECTOR_SUPER = {}
    SECTOR_LEVEL_OF = {}
    SECTOR_NODE_ALIASES = {}

    if not SECTORS_FP.exists():
        print(f"[Resources] {SECTORS_FP.name} not found — sector "
              f"normalisation will be a no-op.")
        return rows, aliases, style_aliases

    _migrate_sectors_file()
    raw_rows = parse_resource_csv(SECTORS_FP)

    problems: list[str] = []
    by_type: dict[str, list[dict]] = {t: [] for t in _SECTOR_TYPES}
    for raw in raw_rows:
        rtype = (raw.get("type") or "").strip().lower()
        name = (raw.get("name") or "").strip().lower()
        if not rtype and not name:
            continue
        if rtype not in _SECTOR_TYPES:
            problems.append(f"unknown type {rtype!r} on row {name!r} — "
                            f"expected one of {list(_SECTOR_TYPES)}")
            continue
        if not name:
            problems.append(f"a {rtype} row has no name")
            continue
        by_type[rtype].append({
            "name":        name,
            "description": (raw.get("description") or "").strip(),
            "parents":     [p.strip().lower()
                            for p in _split_matches(raw.get("parent_name"))],
            "matches":     _split_matches(raw.get("matches")),
            "attrs":       parse_attrs(raw.get("attrs")),
        })

    super_names = {e["name"] for e in by_type["super_sector"]}
    sector_names = {e["name"] for e in by_type["sector"]}

    # Sectors: the classification level.
    seen: set[str] = set()
    for e in by_type["sector"]:
        sec = e["name"]
        if sec in seen:
            problems.append(f"duplicate sector {sec!r}")
            continue
        seen.add(sec)
        for sup in e["parents"]:
            if sup not in super_names:
                problems.append(f"sector {sec!r} names super_sector {sup!r}, "
                                f"which is not defined in this file")
        SECTOR_SUPER[sec] = [s for s in e["parents"] if s in super_names]

        rows.append({
            "sector":       sec,
            # Kept for consumers that read it; the first parent, since a
            # sector belonging to two super sectors has no single one.
            "super_sector": (SECTOR_SUPER[sec] or [""])[0],
            "description":  e["description"],
            "matches":      e["matches"],
            "style_match":  _split_matches(e["attrs"].get("style_match")),
        })
        aliases[sec] = sec
        SECTOR_LEVEL_OF[sec] = "sector"
        node_aliases[sec] = sec
        for m in e["matches"]:
            k = _normalise_key(m)
            if k and k not in aliases:
                aliases[k] = sec
            if k:
                node_aliases.setdefault(k, sec)
        for m in [sec, *_split_matches(e["attrs"].get("style_match"))]:
            k = _norm_phrase(m)
            if k and k not in style_aliases:
                style_aliases[k] = sec

    # Sub sectors: validated and indexed, not yet classified against.
    for e in by_type["sub_sector"]:
        if not e["parents"]:
            problems.append(f"sub_sector {e['name']!r} has no parent_name")
            continue
        if len(e["parents"]) > 1:
            problems.append(f"sub_sector {e['name']!r} names "
                            f"{len(e['parents'])} parents — a sub sector "
                            f"rolls into exactly one sector")
            continue
        parent = e["parents"][0]
        if parent not in sector_names:
            problems.append(f"sub_sector {e['name']!r} names parent "
                            f"{parent!r}, which is not a sector in this file")
            continue
        SUB_SECTOR_PARENT[e["name"]] = parent
        SECTOR_LEVEL_OF[e["name"]] = "sub_sector"
        # A canonical name always outranks any alias, and a deeper
        # canonical outranks a shallower one. Without this a sub sector
        # called "software" was unreachable by its own name, because the
        # technology SECTOR listed "software" among its matches and was
        # indexed first — the row existed and nothing could ever select
        # it.
        node_aliases[e["name"]] = e["name"]
        # Two alias tables, deliberately.
        #
        # ``aliases`` folds a sub sector's spellings up to its parent
        # sector, which is what resolve_sector() has always returned and
        # what every current consumer expects.
        #
        # ``node_aliases`` keeps the level, so a caller that wants the
        # whole tree can tell "this row said gold mining" from "this row
        # said basic materials". Folding is a lossy view of the second,
        # not a different fact — when the finer grain is what the file
        # actually said, throwing it away is the thing to avoid.
        if e["name"] not in aliases:
            aliases[e["name"]] = parent
        for m in e["matches"]:
            k = _normalise_key(m)
            if k and k not in aliases:
                aliases[k] = parent
            if k:
                prior = node_aliases.get(k)
                if prior is None or prior == e["name"]:
                    node_aliases[k] = e["name"]
                else:
                    # Deepest wins, so a sub sector takes the token from
                    # a sector or super sector. Sectors are indexed
                    # first, so this has to overwrite rather than
                    # setdefault — otherwise the coarser level would
                    # keep it and the warning below would describe
                    # behaviour that never happened.
                    node_aliases[k] = e["name"]
                    problems.append(
                        f"match {k!r} is claimed by {prior!r} "
                        f"({SECTOR_LEVEL_OF.get(prior, '?')}) and by "
                        f"{e['name']!r} (sub_sector). The sub_sector wins "
                        f"as the more specific, but one token meaning two "
                        f"things is an authoring mistake")

    if not rows:
        problems.append("no sector rows found — sector normalisation will "
                        "be a no-op")
    if problems:
        print(f"[Resources] {SECTORS_FP.name}: {len(problems)} problem(s) "
              f"found on load:")
        for prob in problems:
            print(f"    - {prob}")

    # Super sectors are nodes too: a file may say only "cyclical".
    for e in by_type["super_sector"]:
        SECTOR_LEVEL_OF[e["name"]] = "super_sector"
        node_aliases.setdefault(e["name"], e["name"])
        for m in e["matches"]:
            k = _normalise_key(m)
            if k:
                node_aliases.setdefault(k, e["name"])

    node_aliases, ALIAS_WILD["sector"] = _alias_index(node_aliases)
    SECTOR_NODE_ALIASES.clear()
    SECTOR_NODE_ALIASES.update(node_aliases)
    return rows, aliases, style_aliases


SECTORS_ROWS:         list[dict]
SECTOR_ALIASES:       dict[str, str]
SECTOR_STYLE_ALIASES: dict[str, str]
# sub_sector -> its sector, and sector -> its super sectors (a list: the
# schema allows several, though nothing does that today).
SUB_SECTOR_PARENT: dict[str, str] = {}
SECTOR_SUPER:      dict[str, list[str]] = {}
# Every canonical sector-tree name -> which of the three levels it is,
# and every spelling -> the canonical name AT ITS OWN LEVEL.
SECTOR_LEVEL_OF:     dict[str, str] = {}
SECTOR_NODE_ALIASES: dict[str, str] = {}
SECTORS_ROWS, SECTOR_ALIASES, SECTOR_STYLE_ALIASES = _load_sectors()


# The residual keys, as breakdowns defines them. Imported lazily inside
# the function to keep resources free of a breakdowns dependency.
SECTOR_LEVELS: tuple[str, ...] = ("sub_sector", "sector", "super_sector")


def resolve_sector_tree(raw: str | None,
                        not_applicable: bool = False) -> dict[str, str]:
    """Resolve a raw sector value to all three levels at once.

    The deepest match wins: a value that names a sub sector answers at
    sub-sector level and its ancestors are derived, while a value naming
    only a sector leaves the level below **unknown** rather than n/a.
    That distinction is the one drawn everywhere else in the app — a
    Technology fund does have sub-sector exposure, the issuer simply did
    not publish it, and uploading the fund's holdings would reveal it.
    Calling it "not applicable" would report such a portfolio as fully
    covered at sub-sector level and hide the very gap the view exists to
    show.

    Args:
        raw: Any spelling, at any of the three levels, or blank.
        not_applicable: True when the concept genuinely does not apply to
            this position — a cash balance has no sector at any level.
            Then all three come back ``"n/a"``.

    Returns:
        ``{"sub_sector", "sector", "super_sector", "level", "matched"}``.
        ``level`` is the level the value matched at, or ``""`` when it
        matched nothing; ``matched`` is the canonical name it matched.
        Unresolved levels are ``"unknown"``; every level is ``"n/a"``
        when ``not_applicable``.
    """
    from porxpy.breakdowns import NA_KEY, UNKNOWN_KEY

    if not_applicable:
        return {"sub_sector": NA_KEY, "sector": NA_KEY,
                "super_sector": NA_KEY, "level": "", "matched": ""}

    out = {"sub_sector": UNKNOWN_KEY, "sector": UNKNOWN_KEY,
           "super_sector": UNKNOWN_KEY, "level": "", "matched": ""}
    node = _alias_lookup(SECTOR_NODE_ALIASES, ALIAS_WILD["sector"], raw)
    if not node:
        return out
    level = SECTOR_LEVEL_OF.get(node, "")
    out["level"] = level
    out["matched"] = node

    # The reserved ``n/a`` node answers at EVERY level, whichever level
    # the file happens to define it at (v0.76.2).
    #
    # It is a node in the CSV like any other, so the spellings that mean
    # "this position has no line of business" — sovereign and municipal
    # issuers — are maintained in the ``matches`` column alongside every
    # other alias, and adding one needs no code change. What it cannot be
    # is a chain of three nodes all named ``n/a``, because SECTOR_LEVEL_OF
    # is keyed by NAME: three same-named rows collapse to one entry and
    # only the level that won would read ``n/a``, leaving the other two
    # ``unknown``. The same position would then be inapplicable on the
    # Super view and a data gap on the Sector view, which is exactly the
    # per-level disagreement the levels-travel-together rule exists to
    # prevent — and the finer views would count it against coverage.
    #
    # So: one node, and the residual is asserted for the whole tree here.
    # This is the same answer the ``not_applicable`` argument gives; the
    # vocabulary can now reach it, which nothing else could.
    if node == NA_KEY:
        out["sub_sector"] = out["sector"] = out["super_sector"] = NA_KEY
        return out

    if level == "sub_sector":
        out["sub_sector"] = node
        sec = SUB_SECTOR_PARENT.get(node, "")
        if sec:
            out["sector"] = sec
            sup = (SECTOR_SUPER.get(sec) or [""])[0]
            if sup:
                out["super_sector"] = sup
    elif level == "sector":
        out["sector"] = node
        sup = (SECTOR_SUPER.get(node) or [""])[0]
        if sup:
            out["super_sector"] = sup
    elif level == "super_sector":
        out["super_sector"] = node
    return out


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

# One load, every structure. Names preserved from the three files this
# replaces so the ~86 downstream references did not move in the same
# release that changed the data.
_GEO = _load_geography()

COUNTRY_ROWS:          list[dict] = _GEO["country_rows"]
COUNTRY_NAME_TO_MSTAR: dict[str, str] = _GEO["name_to_country"]
MSTAR_TO_REGION:       dict[str, str] = _GEO["country_to_region"]
MSTAR_TO_CURRENCY:     dict[str, str] = _GEO["country_to_currency"]
REGION_ROWS:           list[dict] = _GEO["region_rows"]
REGION_FACET_ALIASES:  dict[str, str] = _GEO["region_facet_aliases"]
REGION_ALIASES:        dict[str, str] = {**_GEO["region_facet_aliases"],
                                         **_GEO["super_aliases"],
                                         **_GEO["focus_aliases"]}
SUPER_REGION_MEMBERS:  dict[str, set] = _GEO["super_members"]
REGION_SUPER:          dict[str, str] = _GEO["region_to_super"]
# The 0.62.0 name, kept as the same object: a region's super_region IS
# its development status under this file.
REGION_DEVELOPMENT:    dict[str, str] = REGION_SUPER
REGION_KEYS:       tuple[str, ...] = tuple(
    r["key"] for r in REGION_ROWS if r["kind"] == "region")
SUPER_REGION_KEYS: tuple[str, ...] = tuple(
    r["key"] for r in REGION_ROWS if r["kind"] == "super")
DEVELOPMENT_KEYS:  tuple[str, ...] = SUPER_REGION_KEYS

REGION_KEYS:       tuple[str, ...] = tuple(
    r["key"] for r in REGION_ROWS if r["kind"] == "region")
SUPER_REGION_KEYS: tuple[str, ...] = tuple(
    r["key"] for r in REGION_ROWS if r["kind"] == "super")


def focus_region_vocabulary() -> tuple[str, ...]:
    """Every legal ``focus_detail`` when ``focus_type`` is "geography".

    **v0.68.0: all three levels.** Countries, regions and super regions
    all qualify, because all three are real answers to "what is this
    fund for" — a Japan fund (country and region alike), a European
    developed fund (region), an emerging markets fund (super region).
    Offering regions alone forced a single-country tracker into the
    nearest region, which is a different fund.

    Ordered coarsest-last so the broad answers do not bury the specific
    ones in a 260-entry list.
    """
    countries = tuple(sorted(r["name"] for r in COUNTRY_ROWS if r.get("name")))
    return countries + REGION_KEYS + SUPER_REGION_KEYS


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
    # v0.68.0: accept the legacy "region" spelling. Stored fund
    # structures still carry it, and a validator that rejected them
    # would make every pre-0.68 focus unsaveable the next time its
    # fund was edited.
    ftype = LEGACY_FOCUS_TYPES.get(ftype, ftype)
    if ftype == "geography":
        return focus_region_vocabulary()
    if ftype == "sector":
        # All three sector levels, matching geography. A fund built for
        # semiconductors is not a technology fund, and a cyclicals fund
        # is not any one sector — forcing both into the middle level
        # described neither.
        return tuple(name for name, lvl in SECTOR_LEVEL_OF.items()
                     if lvl in ("sub_sector", "sector", "super_sector"))
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
    return (_alias_lookup(REGION_ALIASES, ALIAS_WILD["region"], raw)
            or _alias_lookup(REGION_ALIASES, ALIAS_WILD["super_region"], raw))


def resolve_region_facet(raw: str | None) -> str | None:
    """Coerce a country-column value to a region key, or ``None``.

    The facet counterpart of :func:`resolve_region_alias`. Reads
    ``regions.csv``'s ``matches`` column, never ``style_match``, and
    answers only for region-kind rows.

    Args:
        raw: A value found in a country column (e.g. ``"Europe ex-UK"``).

    Returns:
        Canonical region key, or ``None`` if nothing matched.
    """
    return _alias_lookup(REGION_FACET_ALIASES, ALIAS_WILD["region"], raw)


def resolve_country_tree(raw: str | None,
                         not_applicable: bool = False) -> dict[str, str]:
    """Resolve a raw country value to both levels at once.

    The country counterpart of :func:`resolve_sector_tree`, and
    deliberately the same shape so the two can be read by one caller.
    The deepest match wins: a value naming a country answers at country
    level and its region is derived, while a value naming only a region
    leaves the level below **unknown** rather than n/a — a European
    equity sleeve does sit in particular countries, the source simply
    did not say which.

    Super regions are not a level here. A region belongs to several at
    once — japan is asia *and* pacific *and* developed *and* world — so
    "the" super region of a holding is not defined, and inventing one
    would double-count weight. That question is deferred rather than
    answered badly.

    Args:
        raw: Any spelling, at either level, or blank.
        not_applicable: True when the concept genuinely does not apply
            to this position. Then both levels come back ``"n/a"``.

    Returns:
        ``{"country", "region", "super_region", "level", "matched"}``.
        ``level`` is the level the value matched at (``"country"``,
        ``"region"``, ``"super_region"``, or ``""`` for no match);
        ``matched`` is the canonical name it matched. Unresolved levels
        are ``"unknown"``.
    """
    from porxpy.breakdowns import NA_KEY, UNKNOWN_KEY

    if not_applicable:
        return {"country": NA_KEY, "region": NA_KEY, "super_region": NA_KEY,
                "level": "", "matched": ""}

    out = {"country": UNKNOWN_KEY, "region": UNKNOWN_KEY,
           "super_region": UNKNOWN_KEY, "level": "", "matched": ""}
    s = (raw or "").strip()
    if not s:
        return out

    # The reserved ``n/a`` node, as in the sector and asset trees
    # (v0.76.2) — see resolve_sector_tree for why it is one node rather
    # than one per level. Matched by name here rather than through a
    # node index, because geography resolves through three separate
    # lookups (country, then region, then development) and the reserved
    # name must not be able to answer differently depending on which of
    # them happened to claim it first. No geography file declares it
    # today; a supranational issuer with no country of risk is the case
    # it exists for.
    if _alias_key(s) == _alias_key(NA_KEY):
        return {"country": NA_KEY, "region": NA_KEY, "super_region": NA_KEY,
                "level": "country", "matched": NA_KEY}

    def _fill_from_region(reg: str) -> None:
        out["region"] = reg
        dev = REGION_DEVELOPMENT.get(reg) or ""
        if dev:
            out["super_region"] = dev

    # Country first. A value that names a country is a country answer
    # even when a region shares the spelling — the finer grain is what
    # the source actually said.
    mstar = country_to_mstar(s)
    if mstar:
        out["level"]   = "country"
        out["matched"] = mstar
        out["country"] = mstar
        reg = MSTAR_TO_REGION.get(mstar) or ""
        if reg:
            _fill_from_region(reg)
        return out

    reg = resolve_region_facet(s)
    if reg:
        out["level"]   = "region"
        out["matched"] = reg
        _fill_from_region(reg)
        return out

    # Development last, and it is a real answer: a factsheet saying
    # "Emerging Markets 30%" has told us something true about 30% of the
    # fund, just not which region and not which country.
    dev = resolve_development_facet(s)
    if dev:
        out["level"]       = "super_region"
        out["matched"]     = dev
        out["super_region"] = dev
    return out


def resolve_development_facet(raw: str | None) -> str | None:
    """Coerce a country-column value to ``developed`` / ``emerging``.

    Reads the ``matches`` column of the two super_region rows, plus
    their names and labels.

    v0.63.0: there is no longer a separate ``style_match`` column to
    consult. It was merged into ``matches`` when the geography files
    were folded, because the evidence said it had stopped being a
    distinct vocabulary: of its 151 tokens, 56 named countries and were
    simply redundant once a tree existed (resolve "germany" to the
    country and walk up), 67 named regions and were ``matches`` content
    already, and the remaining 28 were index names — harmless in
    ``matches``, since nobody writes "NASDAQ" in a country column and
    the United States would be the right answer if they did.
    """
    norm = _norm_phrase(raw)
    if not norm:
        return None
    for row in REGION_ROWS:
        if row["key"] not in DEVELOPMENT_KEYS:
            continue
        tokens = [row["key"], row["label"], *row["matches"]]
        if any(_norm_phrase(t) == norm for t in tokens):
            return row["key"]
    return None


COUNTRY_LEVELS: tuple[str, ...] = ("country", "region", "super_region")


# ---------------------------------------------------------------------------
# Currencies (ISO 4217 with matches column)
# ---------------------------------------------------------------------------
# Lookup tables:
#   CURRENCY_ROWS    ordered list of dicts (code in original casing —
#                    typically uppercase, "GBp" preserves its mixed case)
#   CURRENCY_ALIASES alternative spelling / lower-cased code / lower-
#                    cased name → canonical code (in its original casing
#                    so callers can store it verbatim)


def _load_currencies() -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """Load Currency_definitions.csv into the lookup structures.

    Returns:
        ``(rows, aliases, codes)`` — the loaded rows, the case-folded
        alias index, and the canonical codes in the file's own casing.
        All empty if the file is missing.
    """
    rows: list[dict] = []
    aliases: dict[str, str] = {}

    if not CURRENCIES_FP.exists():
        print(f"[Resources] {CURRENCIES_FP.name} not found — currency "
              f"dropdown will be empty.")
        return rows, aliases, {}

    # v0.64.1: the file uses the shared hierarchical schema
    #   type,name,description,parent_name,matches,is_default,attrs
    # with type=currency, no parent, and the symbol in attrs. Currencies
    # are flat, so parent_name and is_default are unused — carried
    # anyway so every resource file has one shape and one reader.
    #
    # The LOADED rows keep code/name/symbol, because that is what the
    # dropdown, the extraction prompt and the upload paths read. The
    # file's vocabulary changed; the in-memory contract did not.
    raw_rows = parse_resource_csv(CURRENCIES_FP)
    for raw in raw_rows:
        rtype = (raw.get("type") or "currency").strip().lower()
        if rtype != "currency":
            continue
        code = (raw.get("name") or "").strip()
        if not code:
            continue
        attrs = parse_attrs(raw.get("attrs"))
        row = {
            "code":    code,
            "name":    (raw.get("description") or "").strip(),
            "symbol":  (attrs.get("symbol") or "").strip(),
            "matches": _split_matches(raw.get("matches")),
        }
        rows.append(row)

        # Canonical code -> itself, under a case-FOLDED lookup key.
        #
        # setdefault, not assignment (v0.76.1). Two canonical codes can
        # differ only in case: GBP is the pound and GBp is the penny,
        # and they are different currencies a hundredfold apart. An
        # unconditional write let the later row win the shared "gbp"
        # key, so resolve_currency("GBP") answered "GBp" and every
        # sterling holding was labelled pence. First-wins makes the
        # folded key mean the pound, and the case-sensitive pass in
        # resolve_currency is what still tells the two apart.
        # No warning on a collision: GBP/GBp is the expected pair, not a
        # mistake in the file, and both still resolve exactly through
        # the case-sensitive pass. A message on every start that nobody
        # can act on is noise.
        aliases.setdefault(code.lower(), code)
        # Name and matches go in too.
        if row["name"]:
            aliases.setdefault(row["name"].lower(), code)
        for m in row["matches"]:
            k = m.strip().lower()
            if k:
                aliases.setdefault(k, code)

    aliases, ALIAS_WILD["currency"] = _alias_index(aliases)
    # Codes exactly as the file spells them, for the case-sensitive pass
    # in resolve_currency. Currency is the only tree that needs one: it
    # is the only vocabulary where case is part of the identity rather
    # than presentation, because ISO 4217 uses a lowercase final letter
    # for minor units (GBp, ZAr, ILa). Every other tree's names differ
    # by more than case, so folding them loses nothing.
    codes = {r["code"]: r["code"] for r in rows}
    return rows, aliases, codes


CURRENCY_ROWS:    list[dict]
CURRENCY_ALIASES: dict[str, str]
CURRENCY_CODES:   dict[str, str]
CURRENCY_ROWS, CURRENCY_ALIASES, CURRENCY_CODES = _load_currencies()


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

    Note:
        Case-sensitive first, then the ordinary case-folded index
        (v0.76.1). ISO 4217 distinguishes a minor unit from its major
        one by case alone — ``GBp`` is the penny, ``GBP`` the pound —
        so folding the two together turns a sterling holding into a
        pence one and misstates its currency exposure by a factor of a
        hundred. An exact-case hit is therefore always honoured as
        stated; anything else falls through to the tolerant path, where
        the folded key deliberately means the major unit.
    """
    if not raw:
        return None
    exact = CURRENCY_CODES.get((raw or "").strip())
    if exact is not None:
        return exact
    return _alias_lookup(CURRENCY_ALIASES, ALIAS_WILD["currency"], raw)




def resolve_facet_tree(facet: str, raw: str | None,
                       not_applicable: bool = False) -> dict[str, str]:
    """Resolve a raw value to every level of ``facet``'s tree.

    **v0.75.3.** The one dispatcher over the four per-facet resolvers.
    Callers that already know which facet they are holding keep calling
    :func:`resolve_sector_tree` and friends directly; this exists for the
    ones that do not — anything iterating :data:`~porxpy.config.FACET_LEVELS`
    and needing the same answer for whichever facet it is on. Without it
    such a caller writes a four-branch ``if`` that is a fifth place the
    per-tree rules can drift from the four that own them.

    Currency answers here too, with a one-level tree of the same shape,
    so no consumer has to branch on arity. That is the same choice
    :data:`~porxpy.config.FACET_LEVELS` makes.

    Args:
        facet: One of ``sector`` / ``country`` / ``currency`` /
            ``asset_class``.
        raw: Any spelling, at any level of that facet's tree, or blank.
        not_applicable: True when the concept genuinely does not apply
            to this position, so every level comes back ``n/a``.

    Returns:
        ``{<level>: value, ..., "level": stated, "matched": node}`` —
        the shape the three tree resolvers already share. ``level`` is
        the grain the value matched at, ``""`` when it matched nothing;
        unresolved levels are ``"unknown"``. An unknown ``facet`` yields
        the same empty-shaped answer rather than raising, because a
        caller looping over a registry should not have to guard.
    """
    from porxpy.breakdowns import NA_KEY, UNKNOWN_KEY
    from porxpy.config import FACET_LEVELS

    if facet == "sector":
        return resolve_sector_tree(raw, not_applicable)
    if facet == "country":
        return resolve_country_tree(raw, not_applicable)
    if facet == "asset_class":
        return resolve_asset_tree(raw, not_applicable)

    levels = FACET_LEVELS.get(facet) or (facet,)
    if not_applicable:
        return {lv: NA_KEY for lv in levels} | {"level": "", "matched": ""}
    out = {lv: UNKNOWN_KEY for lv in levels} | {"level": "", "matched": ""}
    if facet != "currency":
        return out
    # Currency's "tree" is its alias map: one level, one lookup, and the
    # matched code is the answer at that level.
    code = resolve_currency(raw)
    if code:
        out["currency"] = code
        out["level"]    = "currency"
        out["matched"]  = code
    return out

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








def add_sector_alias(canonical_sector: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a sector's matches column. Bumps file version.

    Rewrites from the file rather than from the loaded rows. The previous
    version rebuilt the file from ``SECTORS_ROWS`` with a hardcoded
    four-column header, which silently discarded ``style_match`` from
    every row — one alias added through the Resolve dialog wiped the
    fund-name matching for all twelve sectors.
    """
    # Delegates to add_facet_alias since v0.76.0, so the canonical
    # guard and the move-off-the-previous-owner rule apply however
    # the write was reached. A second path into the same file is
    # what let the currency writer keep a header the file had
    # stopped using.
    return add_facet_alias("sector", "sector", canonical_sector, new_alias)

def add_sub_sector_alias(canonical_sub_sector: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a sub sector's matches column."""
    # Delegates to add_facet_alias since v0.76.0, so the canonical
    # guard and the move-off-the-previous-owner rule apply however
    # the write was reached. A second path into the same file is
    # what let the currency writer keep a header the file had
    # stopped using.
    return add_facet_alias("sector", "sub_sector", canonical_sub_sector, new_alias)

def add_super_sector_alias(canonical_super_sector: str,
                           new_alias: str) -> bool:
    """Add ``new_alias`` to a super sector's matches column.

    The third of the set. Without it a source saying "Cyclicals" could
    be mapped to a sector or a sub sector — both of which would be
    claiming the source said more than it did — but not to the level it
    actually named.
    """
    # Delegates to add_facet_alias since v0.76.0, so the canonical
    # guard and the move-off-the-previous-owner rule apply however
    # the write was reached. A second path into the same file is
    # what let the currency writer keep a header the file had
    # stopped using.
    return add_facet_alias("sector", "super_sector", canonical_super_sector, new_alias)

def _add_hier_alias(fp: Path, version_key: str, rtype: str,
                    canonical: str, new_alias: str) -> bool:
    """Append an alias to one row of a hierarchical resource file.

    Reads and rewrites the file itself, preserving every column it
    carries — including ones this function knows nothing about, which is
    what makes it safe as more attributes are added.

    Args:
        fp: The resource file.
        rtype: The row's ``type``.
        canonical: The ``name`` to add the alias to.
        new_alias: The raw spelling to add.

    Returns:
        True if the file changed; False when the canonical is unknown or
        the alias is already present.
    """
    canon = (canonical or "").strip().lower()
    alias = (new_alias or "").strip()
    if not canon or not alias:
        return False

    raw_rows = parse_resource_csv(fp)
    if not raw_rows:
        return False
    hit = None
    for raw in raw_rows:
        if ((raw.get("type") or "").strip().lower() == rtype
                and (raw.get("name") or "").strip().lower() == canon):
            hit = raw
            break
    if hit is None:
        return False

    existing = [m.strip() for m in _split_matches(hit.get("matches"))]
    if alias.lower() in {m.lower() for m in existing} or alias.lower() == canon:
        return False
    hit["matches"] = "|".join(existing + [alias])

    # Every column the file has, in its own order — not a list written
    # here, which is how style_match came to be dropped.
    fields = list(raw_rows[0].keys())
    out = [{f: (r.get(f) or "") for f in fields} for r in raw_rows]
    write_resource_csv(fp, fields, out)
    reload_all_resources()
    _refresh_file_stamps()
    return True


def add_currency_alias(canonical_code: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a currency's matches column.

    Delegates to :func:`add_facet_alias` as of v0.76.0. It used to
    rebuild the whole file from ``CURRENCY_ROWS`` under a
    ``code,name,symbol,matches`` header, which Currency_definitions.csv
    has not used since the resource schemas were unified — a successful
    write would have flattened the currency vocabulary. It never fired
    because the in-memory rows no longer carry the ``code`` key it
    looked up, which is luck rather than design, exactly as the same
    bug in the geography writer was.
    """
    return add_facet_alias("currency", "currency", canonical_code, new_alias)


def _add_geography_alias(row_type: str, canonical: str,
                         new_alias: str) -> bool:
    """Add ``new_alias`` to one Geography_definitions.csv row's matches.

    **v0.63.1.** One writer for all three country levels. The three
    predecessors each rebuilt the file from an in-memory structure and
    named the columns themselves — which was survivable while each owned
    its own file, and became a live hazard the moment they shared one:
    ``add_country_alias`` still emitted the old six-column country
    schema, so a successful write would have replaced the whole
    geography tree — every region, super region, focus group, parent and
    currency — with a flat country list.

    It never fired, because the in-memory rows no longer carry the
    column it looked for, so the lookup failed and the write was
    skipped. Failing safe is luck, not design. This function edits the
    file's own rows in place and writes back the header it read, so the
    schema cannot be retyped by a caller.

    Args:
        row_type: ``country`` / ``region`` / ``super_region``.
        canonical: Existing ``name`` to attach the alias to.
        new_alias: The raw value that should resolve to it.

    Returns:
        True if the file changed.
    """
    canon = (canonical or "").strip()
    alias = (new_alias or "").strip()
    if not canon or not alias or not GEOGRAPHY_FP.exists():
        return False

    raw_rows = parse_resource_csv(GEOGRAPHY_FP)
    if not raw_rows:
        return False

    hit = None
    for raw in raw_rows:
        if ((raw.get("type") or "").strip().lower() == row_type
                and (raw.get("name") or "").strip().lower() == canon.lower()):
            hit = raw
            break
    if hit is None:
        return False

    existing = [m.strip() for m in _split_matches(hit.get("matches"))]
    if alias.lower() in {m.lower() for m in existing} \
            or alias.lower() == canon.lower():
        return False
    hit["matches"] = "|".join(existing + [alias])

    # Write back exactly the columns the file has, in its own order.
    fields = list(raw_rows[0].keys())
    write_resource_csv(GEOGRAPHY_FP, fields,
                        [{f: (r.get(f) or "") for f in fields}
                         for r in raw_rows])
    reload_all_resources()
    _refresh_file_stamps()
    return True


def add_country_alias(canonical_country: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a country row's matches column."""
    # Delegates to add_facet_alias since v0.76.0, so the canonical
    # guard and the move-off-the-previous-owner rule apply however
    # the write was reached. A second path into the same file is
    # what let the currency writer keep a header the file had
    # stopped using.
    return add_facet_alias("country", "country", canonical_country, new_alias)

def add_development_alias(canonical_super_region: str,
                          new_alias: str) -> bool:
    """Add ``new_alias`` to a super_region row (developed / emerging)."""
    # Delegates to add_facet_alias since v0.76.0, so the canonical
    # guard and the move-off-the-previous-owner rule apply however
    # the write was reached. A second path into the same file is
    # what let the currency writer keep a header the file had
    # stopped using.
    return add_facet_alias("country", "super_region", canonical_super_region, new_alias)

def add_region_alias(canonical_region: str, new_alias: str) -> bool:
    """Add ``new_alias`` to a region row's matches column."""
    # Delegates to add_facet_alias since v0.76.0, so the canonical
    # guard and the move-off-the-previous-owner rule apply however
    # the write was reached. A second path into the same file is
    # what let the currency writer keep a header the file had
    # stopped using.
    return add_facet_alias("country", "region", canonical_region, new_alias)

# ``asset_class`` appears twice under one name, and the two are NOT the
# same vocabulary:
#
#   asset_class          the fund-level breakdown facet — equity /
#                        fixed_income / cash / mixed / commodity / other,
#                        from Fund_class_definitions.csv.
#   holding_asset_class  the column on a holdings row — equity / bond /
#                        cash / other, from Holdings_class_definitions.csv.
#
# Offering one list for both would let a user alias a factsheet's
# "Obligaties" to the holdings vocabulary's "bond", which the fund-level
# facet has never heard of, and the value would read unresolved forever
# with the dialog insisting it had been fixed.
FACET_ALIAS_LEVELS: dict[str, tuple[str, ...]] = {
    "sector":              ("sub_sector", "sector", "super_sector"),
    "country":             ("country", "region", "super_region"),
    "currency":            ("currency",),
    # One tree since v0.70.0. These were three sibling pseudo-facets —
    # asset_class (the fund vocabulary), holding_asset_class and
    # sub_class — which is the shape config.FACET_LEVELS warns against:
    # the dialog offered three unrelated lists for one taxonomy, and a
    # spelling could be aliased into whichever the user happened to
    # pick.
    "asset_class":         ("sub_class", "asset_class", "super_class"),
}


def facet_alias_targets(facet: str) -> list[dict]:
    """Every canonical a raw value of ``facet`` may be aliased to.

    Args:
        facet: One of :data:`FACET_ALIAS_LEVELS`.

    Returns:
        A list of ``{"level", "key", "label", "path"}``, ordered
        coarsest level last for sector (so the finest, most specific
        claim is nearest the top) and alphabetically within a level.

        ``path`` (v0.75.3) is the whole tree the node resolves to,
        ``{level: value}`` with the levels the tree does not reach left
        out. It is here rather than left to the browser because the
        rules that fill it are per-tree — asset derives a finer level
        through single-child nodes, sector and country never do — and
        re-implementing that client-side would be a second authority on
        what a node means, drifting from this one the first time a tree
        gains a sibling. The picker that shows a node therefore shows
        exactly what storing it will produce.
    """
    out = _facet_alias_nodes(facet)
    from porxpy.breakdowns import NA_KEY, UNKNOWN_KEY
    levels = list(FACET_ALIAS_LEVELS.get(facet, ()))
    for row in out:
        tree = resolve_facet_tree(facet, row["key"])
        row["path"] = {lv: v for lv, v in tree.items()
                       if lv not in ("level", "matched")
                       and v not in (UNKNOWN_KEY, NA_KEY)}
        # ``parent`` (v0.82.0) — the node one level COARSER, or None for a
        # root. It is derivable from ``path`` plus the level order, but it
        # is stated here for the same reason ``path`` is: the tree picker
        # draws the vocabulary as a tree, and a browser that worked out
        # parentage for itself would be a second authority on the shape of
        # a tree this module already owns. The two would agree until a
        # facet gained a level or a node was reparented.
        #
        # ``levels`` is finest-first, so the parent sits at the NEXT index.
        # Nodes whose tree does not reach that level (``antartica`` has no
        # super-region, ``n/a`` has no path at all) get None and are drawn
        # as roots at their own level, which is what they are.
        try:
            nxt = levels[levels.index(row["level"]) + 1]
        except (ValueError, IndexError):
            nxt = None
        row["parent"] = row["path"].get(nxt) if nxt else None
    return out


def _facet_alias_nodes(facet: str) -> list[dict]:
    """The bare ``{level, key, label}`` vocabulary, one block per facet.

    Split out of :func:`facet_alias_targets` so the path pass runs once
    over whichever block answered, rather than being repeated at the end
    of all four.
    """
    f = (facet or "").strip().lower()
    out: list[dict] = []

    if f == "sector":
        by_level: dict[str, list[str]] = {lv: [] for lv in
                                          ("sub_sector", "sector", "super_sector")}
        for name, level in SECTOR_LEVEL_OF.items():
            if level in by_level:
                by_level[level].append(name)
        for level in ("sub_sector", "sector", "super_sector"):
            for name in sorted(by_level[level]):
                out.append({"level": level, "key": name, "label": name})
        return out

    if f == "country":
        for row in COUNTRY_ROWS:
            key = (row.get("name") or "").strip()
            if key:
                out.append({"level": "country", "key": key, "label": key})
        out.sort(key=lambda r: r["label"])
        regions = [r for r in REGION_ROWS if r["kind"] == "region"]
        for row in sorted(regions, key=lambda r: r["label"]):
            out.append({"level": "region", "key": row["key"],
                        "label": row["label"]})
        for row in REGION_ROWS:
            if row["key"] in DEVELOPMENT_KEYS:
                out.append({"level": "super_region", "key": row["key"],
                            "label": row["label"]})
        return out

    if f == "currency":
        for row in CURRENCY_ROWS:
            code = (row.get("code") or "").strip()
            if code:
                name = (row.get("name") or "").strip()
                out.append({"level": "currency", "key": code,
                            "label": f"{code} — {name}" if name else code})
        return out

    if f == "asset_class":
        # Finest level first, so the most specific claim sits nearest
        # the top — the same ordering the sector list uses, and for the
        # same reason: a user resolving a value usually means the
        # specific thing, and the coarse answer is the fallback.
        by_level: dict[str, list[str]] = {lv: [] for lv in ASSET_LEVELS}
        for name, level in ASSET_LEVEL_OF.items():
            if level in by_level:
                by_level[level].append(name)
        for level in ASSET_LEVELS:
            for name in sorted(by_level[level]):
                out.append({"level": level, "key": name, "label": name})
        return out


    return out


# ---------------------------------------------------------------------------
# Alias writing (v0.76.0 — one writer for all four facets)
# ---------------------------------------------------------------------------
# All four resource files share the schema documented in CLAUDE.md
# (``type,name,description,parent_name,matches,is_default,attrs``) and
# every one of them names its row TYPE after the facet level the row
# sits at. That makes a per-facet writer unnecessary, and the per-facet
# writers were actively dangerous: ``add_currency_alias`` rebuilt
# Currency_definitions.csv from an in-memory structure with a
# ``code,name,symbol,matches`` header the file has not had since the
# schemas were unified, so a successful write would have replaced the
# whole currency vocabulary with a flat four-column list. It never
# fired only because the lookup it did first could not match. That is
# the same failure ``_add_geography_alias`` was written to end, one file
# over, and the lesson did not travel — which is the argument for one
# writer rather than four.
FACET_RESOURCE: dict[str, tuple[Path, tuple[str, ...]]] = {
    "sector":      (SECTORS_FP,    ("sub_sector", "sector", "super_sector")),
    "country":     (GEOGRAPHY_FP,  ("country", "region", "super_region")),
    "asset_class": (ASSET_FP,      ("sub_class", "asset_class", "super_class")),
    "currency":    (CURRENCIES_FP, ("currency",)),
}


def facet_canonical_level(facet: str, value: str) -> str:
    """The level ``value`` is a canonical NAME at, or ``""``.

    The check behind the rule that a canonical may not be aliased to
    another node. A canonical is not a spelling that happens to resolve
    somewhere — it is the node's own identity, and the loaders enforce
    that directly (*"a canonical name always outranks any alias"*), so
    an alias written against one is dead on arrival: the file changes,
    the resolution does not, and the dialog reports a success that
    changed nothing.
    """
    fp, levels = FACET_RESOURCE.get(facet, (None, ()))
    if fp is None or not (value or "").strip():
        return ""
    want = _alias_key(value)
    for raw in parse_resource_csv(fp) or []:
        rtype = (raw.get("type") or "").strip().lower()
        if rtype in levels and _alias_key(raw.get("name")) == want:
            return rtype
    return ""


def facet_alias_claims(facet: str, token: str) -> list[dict]:
    """Every row of ``facet``'s file whose ``matches`` claim ``token``.

    Two kinds of claim, kept apart because only one of them can be moved:

    * **exact** — the token is listed literally. Re-pointing it means
      taking it off this row.
    * **wildcard** — the row carries a pattern such as ``*bank*`` that
      happens to cover the token. Moving THAT would silently re-point
      every value containing "bank" across every fund, from a click on
      one row, so a wildcard claim is reported and never touched. An
      exact alias added to the new node wins anyway, because
      :func:`_alias_lookup` tries exact matches before wildcards.

    Returns:
        ``[{"level", "canonical", "token", "wildcard"}]``.
    """
    fp, levels = FACET_RESOURCE.get(facet, (None, ()))
    if fp is None or not (token or "").strip():
        return []
    want = _alias_key(token)
    out: list[dict] = []
    for raw in parse_resource_csv(fp) or []:
        rtype = (raw.get("type") or "").strip().lower()
        if rtype not in levels:
            continue
        for m in _split_matches(raw.get("matches")):
            core = _alias_key(m.strip("*"))
            is_wild = m.strip().startswith("*") or m.strip().endswith("*")
            if is_wild:
                key = _alias_key(token)
                lead, trail = m.strip().startswith("*"), m.strip().endswith("*")
                hit = ((lead and trail and core in key)
                       or (lead and not trail and key.endswith(core))
                       or (trail and not lead and key.startswith(core)))
            else:
                hit = core == want
            if hit:
                out.append({"level":     rtype,
                            "canonical": (raw.get("name") or "").strip(),
                            "token":     m.strip(),
                            "wildcard":  is_wild})
    return out


def facet_alias_conflict(facet: str, level: str, canonical: str,
                         raw_value: str) -> str:
    """Why this alias cannot be written, or ``""`` if it can.

    Split from :func:`add_facet_alias` so the HTTP layer can answer with
    a specific message and a 409 rather than a bare False, while every
    other caller still gets the same refusal enforced inside the writer.
    """
    if facet not in FACET_RESOURCE:
        return f"unknown facet {facet!r}"
    fp, levels = FACET_RESOURCE[facet]
    if level not in levels:
        return f"level {level!r} is not one of {list(levels)} for {facet!r}"
    if not (canonical or "").strip() or not (raw_value or "").strip():
        return "canonical and raw are both required"

    canon_level = facet_canonical_level(facet, raw_value)
    if canon_level:
        if _alias_key(raw_value) == _alias_key(canonical):
            return (f"{raw_value!r} already IS the canonical "
                    f"{canon_level.replace('_', ' ')} — it resolves there "
                    f"directly and needs no alias.")
        return (f"{raw_value!r} is the canonical name of a "
                f"{canon_level.replace('_', ' ')}, so it cannot be made a "
                f"name for {canonical!r} as well. A canonical always "
                f"outranks an alias, so the write would change the file "
                f"and not the answer. Use 'change value' to make these "
                f"rows mean something else instead.")
    return ""


def add_facet_alias(facet: str, level: str, canonical: str,
                    raw_value: str) -> bool:
    """Write one alias, moving it off any node that already claims it.

    Three rules, all of them consequences of one fact: a token means
    exactly one node, and the loaders arbitrate a token claimed twice by
    depth with only a console warning to show for it.

    1. A canonical name is never written as an alias of another node —
       see :func:`facet_alias_conflict`.
    2. An exact claim on any other node is REMOVED before the new one is
       written. Adding without removing leaves two claimants, which the
       loader resolves deepest-first, so the value silently keeps meaning
       what it meant while the file says otherwise.
    3. Wildcard claims are left alone. Moving a pattern re-points every
       value it covers, which is never what a user editing one row asked
       for; the new exact alias takes precedence anyway.

    Args:
        facet: One of :data:`FACET_RESOURCE`.
        level: The level of ``canonical`` — the grain the claim is made
            at, so "cyclical" as a sector and as a super sector stay
            different statements.
        canonical: The node name the alias should resolve to.
        raw_value: The spelling to attach. May itself be a wildcard
            pattern (``*bank*``), which is written as given.

    Returns:
        True if the file changed.
    """
    if facet_alias_conflict(facet, level, canonical, raw_value):
        return False

    fp, _levels = FACET_RESOURCE[facet]
    canon = (canonical or "").strip().lower()
    alias = (raw_value or "").strip()

    raw_rows = parse_resource_csv(fp)
    if not raw_rows:
        return False

    target = None
    for raw in raw_rows:
        if ((raw.get("type") or "").strip().lower() == level
                and (raw.get("name") or "").strip().lower() == canon):
            target = raw
            break
    if target is None:
        return False

    want = _alias_key(alias.strip("*"))
    is_wild_alias = alias.startswith("*") or alias.endswith("*")
    changed = False

    # Rule 2: strip the exact token from every other row first, so the
    # add below cannot produce a second claimant even transiently.
    for raw in raw_rows:
        if raw is target:
            continue
        existing = [m.strip() for m in _split_matches(raw.get("matches"))]
        to_drop = [m for m in existing
                   if not (m.startswith("*") or m.endswith("*"))
                   and _alias_key(m) == want]
        if to_drop:
            raw["matches"] = "|".join(m for m in existing if m not in to_drop)
            changed = True

    existing = [m.strip() for m in _split_matches(target.get("matches"))]
    if _alias_key(alias) in {_alias_key(m) for m in existing}:
        # Already on the target. Still a real change if the move above
        # took it off somebody else.
        if changed:
            _write_facet_rows(fp, raw_rows)
        return changed
    target["matches"] = "|".join(existing + [alias])
    _write_facet_rows(fp, raw_rows)
    return True


def remove_facet_alias(facet: str, canonical: str, token: str) -> bool:
    """Take one exact alias off one node. Wildcards are never removed."""
    if facet not in FACET_RESOURCE:
        return False
    fp, levels = FACET_RESOURCE[facet]
    raw_rows = parse_resource_csv(fp)
    if not raw_rows:
        return False
    want = _alias_key(token)
    canon = (canonical or "").strip().lower()
    changed = False
    for raw in raw_rows:
        if (raw.get("type") or "").strip().lower() not in levels:
            continue
        if canon and (raw.get("name") or "").strip().lower() != canon:
            continue
        existing = [m.strip() for m in _split_matches(raw.get("matches"))]
        kept = [m for m in existing
                if (m.startswith("*") or m.endswith("*"))
                or _alias_key(m) != want]
        if len(kept) != len(existing):
            raw["matches"] = "|".join(kept)
            changed = True
    if changed:
        _write_facet_rows(fp, raw_rows)
    return changed


def _write_facet_rows(fp: Path, raw_rows: list[dict]) -> None:
    """Write a resource file back with the header it was read with.

    Every column the file has, in its own order — never a list named
    here. Naming the columns in the writer is how ``style_match`` came
    to be dropped once and how the currency writer came to hold a header
    the file no longer used.
    """
    fields = list(raw_rows[0].keys())
    write_resource_csv(fp, fields,
                       [{f: (r.get(f) or "") for f in fields} for r in raw_rows])
    reload_all_resources()
    _refresh_file_stamps()


# Fill the change-detection registry at import. Previously two loaders
# each wrote their own entry and the other three files wrote none, so
# the dict silently covered two of five.
_refresh_file_stamps()
