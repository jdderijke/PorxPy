"""
Static configuration: paths, exchange/ticker lookup tables, cache layout,
TTL settings, and country/currency reference paths.

Nothing in this module does I/O. Anything that needs to be tunable at
runtime (e.g. per-portfolio cache TTLs) is *defaulted* here and overridden
elsewhere — see ``porxpy.utils.normalise_cache_config``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths — anchored at the project root (the directory containing ``main.py``)
# ---------------------------------------------------------------------------
# ``BASE_DIR`` resolves to ``<project>/`` regardless of where the package is
# imported from, since this file lives at ``<project>/porxpy/config.py``.
BASE_DIR              = Path(__file__).resolve().parent.parent
FRONTEND_FP           = BASE_DIR / "fund_explorer.html"

# Top-level user-data files at the project root (not under ``cache/``,
# because they are NOT cache — they are user state, permanent, valuable,
# never auto-purged). The cache directory boundary "stuff we can lose
# without losing user state" is the design line here.
PORTFOLIOS_FP         = BASE_DIR / "portfolios.json"
SETTINGS_FP           = BASE_DIR / "settings.json"

# One ISIN-keyed file consolidating the three former override stores
# (asset_class_overrides.json, breakdown_source_overrides.json,
# fund_structure.json — all retired in 0.12.0). Sub-blocks per ISIN:
# ``{asset_class, breakdown_source, fund_structure}``. Overrides are
# fund-level, not portfolio-level: every portfolio holding the ISIN
# sees the same override. See :mod:`porxpy.utils` (``overrides_get`` /
# ``overrides_put`` / ``load_overrides``) and the application in
# :func:`porxpy.extractors.load_fund_data`.
OVERRIDES_FP          = BASE_DIR / "overrides.json"

# Ticker resolutions discovered by the fetch route. Keyed
# ``"<ISIN>|<MIC>"`` → ``{ticker, resolved_mic, note, resolved_at}``.
# This is cached resolution metadata, not cache of Yahoo data — kept
# at the project root because it persists across cache wipes (same
# rationale as portfolios and overrides).
ISIN_MAP_FP           = BASE_DIR / "isin_map.json"

# Cache root and its two ISIN-vs-ticker sub-directories. The split is
# the data-model insight that landed in 0.12.0:
#   listings/   ticker-keyed   — listing-level data: identity quad
#                                 (isin/ticker/exchange/currency),
#                                 price history, profile, volume.
#                                 Two listings of the same fund (e.g.
#                                 BATG.L GBp vs BATT.L USD) genuinely
#                                 have different prices and currencies.
#   funds/      ISIN-keyed     — fund-level data: holdings, sectors,
#                                 asset class, asset allocation. These
#                                 are properties of the FUND, not the
#                                 listing; both listings of one fund
#                                 share one funds-cache file.
# Cache is "stuff we can lose without losing user state" — TTL'd, may
# be purged. User intent (overrides, portfolios) lives outside.
CACHE_DIR             = BASE_DIR / "cache"
LISTINGS_DIR          = CACHE_DIR / "listings"
FUNDS_DIR             = CACHE_DIR / "funds"

# Reference data shipped with the project (CSVs of country codes and
# country→currency mappings, plus the v0.13.0 holdings-classification,
# Morningstar-sector, and ISO-4217-currency tables that back the edit-
# holding dropdowns and the upload column normalisation). Read-only;
# not user data, not cache.
RESOURCES_DIR         = BASE_DIR / "resources"
GEOGRAPHY_FP          = RESOURCES_DIR / "Geography_definitions.csv"
# Fund-level CLASSIFICATION vocabulary (equity / fixed_income / cash /
# mixed / commodity / other) — what kind of fund this IS, as captured
# from Yahoo, a factsheet, justETF or the user. Not resolved from, and
# not a rollup of, the fund's holdings: a 60/40 fund is "mixed" while
# its asset_class FACET is roughly 60% equity / 40% fixed income.
#
# Two alias columns, the same split the sector file draws:
#   matches      spellings of the value itself ("fixed income" → fixed_income)
#   style_match  phrases that, found in a fund's NAME or category, imply
#                the classification ("gilt", "ultrashort", "nasdaq")
PRIMARY_CLASS_FP      = RESOURCES_DIR / "Primary_asset_class_definitions.csv"
# The asset taxonomy as ONE tree: super_class -> asset_class -> sub_class.
# Replaces Holdings_class_definitions.csv and Fund_class_definitions.csv,
# which described the same taxonomy at two grains and disagreed on
# spelling ("bond" vs "fixed_income") for the same concept.
ASSET_FP              = RESOURCES_DIR / "Asset_definitions.csv"
# Morningstar 11-sector taxonomy with super-sector grouping and a
# `matches` column. Drives the Sector dropdown and the upload-side
# coercion of issuer spellings ("Information Technology" → "technology").
SECTORS_FP            = RESOURCES_DIR / "Sector_definitions.csv"

# v0.28.0: region + super-region vocabulary. Region-keyed, unlike the
# country-keyed country_codes.csv that MSTAR_TO_REGION is built from —
# a super_regions column there would repeat the same value across every
# country row of a region, with no single place to read the vocabulary.
# Used ONLY to seed focus_type / focus_detail from a fund name; the
# country/region breakdown facet does not consult it.
# ISO 4217 currency master list with display names and a `matches`
# column for alternative spellings ("yen" → "JPY"). Drives the Currency
# dropdown — separate from country_currency.csv which is a per-country
# primary-currency mapping; this file is the full per-code list.
CURRENCIES_FP         = RESOURCES_DIR / "Currency_definitions.csv"

# Server-side scratch space for the holdings upload flow. The user picks
# a file → we parse it once and stash the parsed-but-unmapped form under
# /uploads/<token>.json → the column-mapping dialog refers back to that
# token → /commit reads the token, applies the mapping, writes to the
# fund's funds-cache holdings slot. Tokens expire after UPLOAD_TOKEN_TTL_MIN.
UPLOAD_DIR            = BASE_DIR / "uploads"
UPLOAD_TOKEN_TTL_MIN  = 30


# ---------------------------------------------------------------------------
# Exchange MIC → Yahoo Finance suffix
# ---------------------------------------------------------------------------
# Yahoo's ticker symbols use a suffix per exchange (e.g. ``SWDA.L`` for
# London). MICs are the ISO 10383 market identifier codes returned by
# OpenFIGI. Empty string means "no suffix" (US listings).
MIC_TO_YF: dict[str, str] = {
    "XLON": ".L",   "XAMS": ".AS",  "XPAR": ".PA",  "XBRU": ".BR",
    "XLIS": ".LS",  "XMIL": ".MI",  "XFRA": ".F",   "XETR": ".DE",
    "XSTU": ".SG",  "XMUN": ".MU",  "XHAM": ".HM",  "XBER": ".BE",
    "XSWX": ".SW",  "XVTX": ".SW",  "XHEL": ".HE",  "XSTO": ".ST",
    "XCSE": ".CO",  "XICE": ".IC",  "XOSL": ".OL",  "XWBO": ".VI",
    "XBUD": ".BD",  "XWAR": ".WA",  "XPRA": ".PR",  "BMEX": ".MC",
    "XATH": ".AT",  "XNYS": "",     "XNAS": "",     "XASE": "",
    "ARCX": "",     "BATS": "",     "XTSE": ".TO",  "XTSX": ".V",
    "XMEX": ".MX",  "XHKG": ".HK",  "XSHG": ".SS",  "XSHE": ".SZ",
    "XTKS": ".T",   "XKRX": ".KS",  "XKOS": ".KQ",  "XASX": ".AX",
    "XNZE": ".NZ",  "XBOM": ".BO",  "XNSE": ".NS",  "XSES": ".SI",
    "XBKK": ".BK",  "XIDX": ".JK",  "XKLS": ".KL",  "XTAI": ".TW",
    "XTPE": ".TWO", "XJSE": ".JO",  "BVMF": ".SA",
}

# OpenFIGI exchange codes are NOT ISO MICs — OpenFIGI uses its own
# (largely Bloomberg-style) exchange-code vocabulary. The resolver must
# translate an ISO MIC to the OpenFIGI code before sending it as
# ``exchCode``; passing the raw MIC (e.g. "XMUN") makes OpenFIGI return
# nothing. A MIC absent from this map has no known OpenFIGI code — the
# resolver then queries without an ``exchCode`` (unscoped) and filters
# the results itself.
#
# Codes verified against OpenFIGI's published exchange-code list. The
# European regional German exchanges (Munich, Stuttgart, etc.) share
# Bloomberg-style two-letter codes; "GR" is the XETRA/Germany composite
# that OpenFIGI accepts for most German-listed instruments.
MIC_TO_FIGI: dict[str, str] = {
    "XLON": "LN",   # London
    "XAMS": "NA",   # Amsterdam (Euronext)
    "XPAR": "FP",   # Paris (Euronext)
    "XBRU": "BB",   # Brussels (Euronext)
    "XLIS": "PL",   # Lisbon (Euronext)
    "XMIL": "IM",   # Milan
    "XETR": "GY",   # XETRA
    "XFRA": "GF",   # Frankfurt floor
    "XMUN": "GM",   # Munich
    "XSTU": "GS",   # Stuttgart
    "XHAM": "GH",   # Hamburg
    "XBER": "GB",   # Berlin
    "XSWX": "SW",   # SIX Swiss
    "XVTX": "SW",   # SIX (virt-x)
    "XSTO": "SS",   # Stockholm
    "XHEL": "FH",   # Helsinki
    "XCSE": "DC",   # Copenhagen
    "XICE": "IR",   # Iceland
    "XOSL": "NO",   # Oslo
    "XWBO": "AV",   # Vienna
    "BMEX": "SM",   # Madrid (BME)
    "XNYS": "UN",   # NYSE
    "XNAS": "UW",   # NASDAQ
    "XASE": "UA",   # NYSE American
    "ARCX": "UP",   # NYSE Arca
    "BATS": "UF",   # Cboe BZX
    "XTSE": "CN",   # Toronto
    "XTSX": "CV",   # TSX Venture
    "XMEX": "MM",   # Mexico
    "XHKG": "HK",   # Hong Kong
    "XSHG": "C1",   # Shanghai
    "XSHE": "C2",   # Shenzhen
    "XTKS": "JT",   # Tokyo
    "XKRX": "KS",   # Korea (KOSPI)
    "XKOS": "KQ",   # KOSDAQ
    "XASX": "AT",   # ASX
    "XNZE": "NZ",   # New Zealand
    "XBOM": "IB",   # Bombay
    "XNSE": "IS",   # India NSE
    "XSES": "SP",   # Singapore
    "XBKK": "TB",   # Bangkok
    "XIDX": "IJ",   # Jakarta
    "XKLS": "MK",   # Kuala Lumpur
    "XTAI": "TT",   # Taiwan
    "XJSE": "SJ",   # Johannesburg
    "BVMF": "BZ",   # Brazil B3
}


# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# justETF profile-page URL template, used by the Structure assisted-
# lookup (replication method + management style). justETF keys its
# profile pages by ISIN and states both facets as plain text. This is
# a best-effort convenience lookup: it only covers ISINs justETF lists
# (broadly, European-domiciled funds), it scrapes HTML so it is
# inherently brittle, and the result is always presented to the user as
# a *suggestion* to confirm — never auto-committed. See
# :func:`porxpy.extractors.lookup_fund_structure`.
JUSTETF_PROFILE_URL = "https://www.justetf.com/en/etf-profile.html?isin={isin}"
JUSTETF_LOOKUP_TIMEOUT_S = 12
JUSTETF_LOOKUP_UA = (
    "Mozilla/5.0 (compatible; PorxPy/1.0; portfolio x-ray tool)"
)


# ---------------------------------------------------------------------------
# Cache layout
# ---------------------------------------------------------------------------
# Cache data splits cleanly into two layers because there are two
# different things being identified:
#
#   ticker = a LISTING — a specific line on an exchange in a currency.
#            BATG.L and BATT.L are different listings of the same fund.
#   ISIN   = the FUND itself — the underlying pool of assets. Both
#            BATG.L and BATT.L share one ISIN.
#
# So price/profile/identity (which differ per listing) are keyed by
# ticker → ``cache/listings/<ticker>.json``. Holdings, sectors, asset
# class and asset allocation (which are properties of the fund itself)
# are keyed by ISIN → ``cache/funds/<isin>.json``. The two listings of
# one fund share one funds-cache file — a single upload of full
# holdings against either listing is visible from both.
LISTING_CATEGORIES: list[str] = [
    "profile", "price_history", "upload_prefs",
]
FUND_CATEGORIES: list[str] = [
    "holdings", "sectors", "asset_class", "asset_allocation",
    # Per-facet breakdown item lists that a source HANDED US rather than
    # us deriving them — a user CSV upload and an AI factsheet reading,
    # i.e. SUPPLIED_BREAKDOWN_SOURCES. Never fetched from anyone. The
    # category kept its original name when the factsheet source joined it
    # in 0.44.0; renaming it would have orphaned every stored upload for
    # a cosmetic gain. Shape on disk:
    #   { "uploaded_breakdowns": {
    #       "fetched_at": "...",
    #       "value": { "asset_class": {"upload":    [{"raw","weight"}, ...],
    #                                  "factsheet": [...]},
    #                  "sector":      {...}, "country": {...},
    #                  "currency":    {...} } } }
    # Each item carries "raw" — what that source actually said, resolved
    # afresh on every read since 0.76.0 — plus an optional "key" where the
    # user pinned it to a node. Sources are independent: writing one
    # leaves the other alone, and only facets that source covered
    # (non-empty) can be flipped to it on the fund page.
    "uploaded_breakdowns",
    # upload_sources = "where did the last upload of each kind come
    # from?", one entry per UPLOAD_SOURCE_KINDS member:
    #   { "upload_sources": {
    #       "fetched_at": "...",
    #       "value": { "holdings":      {"source_kind": "url",
    #                                    "source_value": "https://…",
    #                                    "filename": "…", "saved_at": "…"},
    #                  "factsheet":     {...},
    #                  "breakdown_csv": {...} } } }
    # Fund-level rather than per-listing, unlike upload_prefs: the
    # *layout* of a spreadsheet can differ between a fund's listings
    # (which is why the column mapping stays ticker-keyed), but the
    # *location* of the issuer's document cannot — a factsheet and a
    # holdings schedule belong to the fund, and both listings of it
    # should offer the same URL back.
    "upload_sources",
]
# Union, preserved for code that iterates "every category" (cache list,
# settings UI, etc.). Order is listings-first for cosmetic stability.
CACHE_CATEGORIES: list[str] = LISTING_CATEGORIES + FUND_CATEGORIES

DEFAULT_CACHE_CONFIG: dict[str, dict[str, Any]] = {
    "profile":       {"enabled": True, "ttl_days": 30},
    # holdings = the unified per-position holdings slot. A single blob
    # holds whatever the best-available source produced: raw Yahoo
    # top-10, Yahoo top-10 enriched with per-symbol lookups, or a full
    # user upload — all sharing one superset row schema. There is no
    # TTL: holdings only ever change when the user explicitly asks for
    # it (the "Reload fund data" button on the fund page, or "Refresh
    # all" on the portfolio page), both of which set force=True. The
    # ``manual_refresh_only`` flag tells the cache layer to treat a
    # present entry as a hit regardless of age; only force=True bypasses
    # it. ``ttl_days`` is retained (ignored when manual_refresh_only is
    # set) purely so the per-category config schema stays uniform.
    "holdings":      {"enabled": True, "ttl_days": 0, "manual_refresh_only": True},
    "sectors":       {"enabled": True, "ttl_days": 7},
    "asset_class":   {"enabled": True, "ttl_days": 90},
    # asset_allocation = the issuer-published asset-class breakdown of
    # the fund's holdings (equity / fixed_income / cash / other split),
    # from Yahoo's funds_data.asset_classes. This is the Fund/ETF-level
    # asset-class *breakdown card* — distinct from the single-label
    # "asset_class" category above. Issuer data, changes slowly; same
    # 7-day cadence as the sector breakdown.
    "asset_allocation": {"enabled": True, "ttl_days": 7},
    # uploaded_breakdowns = per-facet breakdown item lists supplied by a
    # user CSV upload or a factsheet extraction (the two per-card sources
    # on the fund page that hand us numbers). Never fetched — so there is
    # no meaningful TTL. ``manual_refresh_only`` makes a present entry
    # always fresh; only an explicit re-upload or re-extraction (each of
    # which writes a new entry anyway) replaces it. ``ttl_days`` is
    # retained purely for schema uniformity.
    "uploaded_breakdowns": {"enabled": True, "ttl_days": 0, "manual_refresh_only": True},
    "price_history": {"enabled": True, "ttl_days": 1},
    # upload_prefs = the user's last upload-dialog choices for a fund
    # (source URL/path, column mapping, header row, decimal, weight
    # unit, defaults, enrich fields). Prefs aren't fetched from anyone
    # — they only change when the user commits a new upload — so the
    # TTL is effectively "forever". 10 years is the practical upper
    # bound; cache_get treats anything above this as a miss.
    "upload_prefs":  {"enabled": True, "ttl_days": 3650},
    # upload_sources = the remembered Source field of each upload dialog
    # (see FUND_CATEGORIES). Like upload_prefs it is never fetched from
    # anyone — it changes only when the user completes an upload — so
    # the TTL is the same effective "forever".
    "upload_sources": {"enabled": True, "ttl_days": 3650},
}

# The upload dialogs whose Source field is remembered per fund.
#
# Three dialogs ask the same question — "where is the document?" — and
# accept the same answers (an http(s) URL, a local path, a file:// URI,
# or a dropped file stashed in the scratch folder). They are listed here
# so the store, the API and the frontend prefill are one implementation
# taking the kind as a parameter, rather than three near-copies that can
# drift apart in what they remember and how they show it.
#
# The keys are storage keys and must not be renamed casually: a rename
# orphans every remembered source. The labels are what the dialogs and
# the cache screen call each kind.
UPLOAD_SOURCE_KINDS: dict[str, str] = {
    "holdings":      "holdings file",
    "factsheet":     "factsheet",
    "breakdown_csv": "breakdown CSV",
}

# Canonical fund-level classification keys. The authority is
# Primary_asset_class_definitions.csv, read through
# resources.primary_asset_classes() — there is no second copy here.
#
# A hardcoded list lived here until v0.69.0, on the reasoning that
# config must not import resources. That reasoning holds at import time
# and only at import time: the lazy import below runs on call, long
# after both modules exist. What the constant actually bought was a
# vocabulary that could disagree with the file defining it, which is the
# divergence this codebase removes on sight.
def asset_classes() -> tuple[str, ...]:
    """Canonical primary-asset-class keys, in file order.

    Returns:
        Tuple of lowercase keys from Primary_asset_class_definitions.csv.
    """
    from porxpy.resources import primary_asset_classes   # local: import cycle
    return primary_asset_classes()


def field_vocab(spec: dict, context: dict | None = None) -> tuple[str, ...]:
    """Allowed values for an OVERRIDABLE_FIELDS entry.

    One resolver for both ways a vocabulary can be declared, so no
    caller has to know which kind a given field uses: ``vocab`` for a
    fixed tuple, ``vocab_fn`` for one that lives in a resource file.

    Args:
        spec: An OVERRIDABLE_FIELDS entry.
        context: Passed to ``vocab_fn`` resolvers that depend on another
            field's value (focus_detail depends on focus_type).

    Returns:
        Tuple of allowed values, empty when the field has no vocabulary.
    """
    fn = spec.get("vocab_fn")
    if fn:
        import porxpy.resources as _res                  # local: import cycle
        return tuple(getattr(_res, fn)(context or {}) or ())
    return tuple(spec.get("vocab") or ())


# ---------------------------------------------------------------------------
# TTL knobs (not part of the per-category cache config)
# ---------------------------------------------------------------------------
# FX pair cache — same on-disk layout as per-ticker cache, stored under FX_<pair>
FX_TTL_HOURS = 6

# Historical FX series cache — yesterday's FX doesn't change; today's
# appends once a day, so refresh once per day is enough.
FX_HIST_TTL_HOURS = 24

# ISIN→ticker resolutions are extremely stable (an ISIN's ticker on a given
# exchange essentially never changes). Cache on disk for a long time to
# avoid hammering OpenFIGI, which rate-limits aggressively.
ISIN_MAP_TTL_DAYS = 30

# How long a NEGATIVE symbol-alias entry ("tried this spelling, nothing
# worked") stays believed. Positive entries never expire: "AIR FP is
# AIR.PA" is a durable fact about the world. A negative is not — it is a
# statement about one moment, and the things that produce one are often
# temporary: a rate limit, a TLS failure, an outage, or Yahoo simply not
# covering the security yet.
#
# Without an expiry a transient failure becomes permanent. Every symbol
# probed during an outage is written off, and because the alias short-
# circuits before any lookup, the next attempt never even asks — the
# outage outlives itself, silently, one symbol at a time. That is
# exactly what happened on 2026-08-18, when a curl_cffi impersonation
# bug took every lookup down and the misses it caused survived the fix.
#
# Seven days: long enough that re-uploading the same holdings file does
# not re-probe hundreds of junk tickers, short enough that a bad day
# does not cost a month.
NEGATIVE_ALIAS_TTL_DAYS = 7

# ...but doubling on each consecutive miss, capped here. A spelling that
# has failed five times running is junk — a futures code, a swap leg, a
# cash line — and re-probing it weekly forever is pure waste. A spelling
# that failed once may just have been unlucky. Escalating separates the
# two without needing to tell them apart: the first miss is forgiven in
# a week, the fifth is not asked about again for three months.
NEGATIVE_ALIAS_TTL_CAP_DAYS = 90

# When the price_history cache is older than this many days, do a full
# ``period="max"`` refresh instead of an incremental top-up. This is the
# coarse safety net for retroactive split/dividend adjustments that
# ``auto_adjust=True`` applies to historical bars — incremental fetching
# of just the tail won't pick those up. 30 days = at most a one-month
# window of stale adjustments before everything gets resynced.
PRICE_HISTORY_FULL_REFRESH_DAYS = 30

# Per-symbol Yahoo info cache (HQ country, trading currency, quoteType).
# Used to enrich Yahoo top-10 holdings rows when full holdings aren't
# available. HQ country and trading currency don't change in any
# practical sense, so this can be cached for a long time.
SYMBOL_INFO_TTL_DAYS = 90

# Cache filename for the shared symbol-info store. One file shared
# across all funds — every fund tracking AAPL benefits from a single
# lookup. Anchored at the cache dir like the per-ticker caches, but
# not a fund cache (so cache_purge with no ticker scope leaves it
# alone — see _is_fund_cache_file in utils).
SYMBOL_INFO_CACHE_NAME = "_symbol_info"

# Per-input alias cache for the variant-probing flow. Records the raw
# user-supplied form → the candidate Yahoo accepted, so subsequent
# probes for the same raw form skip straight to the right entry.
# A null/missing alias value means "tried, nothing worked" (negative
# cache) — the next probe still skips, returning {"_found": False}.
SYMBOL_ALIAS_CACHE_NAME = "_symbol_aliases"


# ---------------------------------------------------------------------------
# Currency normalisation
# ---------------------------------------------------------------------------
# Some funds quote in sub-units (GBp = British pence, ZAc = SA cents, etc.).
# Mapping is sub_unit → (canonical_currency, divisor_to_apply_to_prices).
# A divisor of 100 means GBp 10158 → GBP 101.58.
# ``"GBP": None`` is a sentinel: the canonical code itself maps to no-op.
PENCE_CURRENCIES: dict[str, tuple[str, float] | None] = {
    "GBP": None,
    "GBp": ("GBP", 100.0),
    "GBX": ("GBP", 100.0),
    "ZAc": ("ZAR", 100.0),
    "ILA": ("ILS", 100.0),
}


# ---------------------------------------------------------------------------
# Yahoo period-fallback chain
# ---------------------------------------------------------------------------
# Some Yahoo listings (e.g. ROB7.MU and other small/regional Xetra/Munich
# tickers) reject ``period="max"`` with:
#     'Period max is invalid, must be one of: 1d, 5d'
# even though regularMarketPrice / previousClose ARE returned by ticker.info.
# We try these in order until something comes back.
HISTORY_PERIOD_FALLBACKS: tuple[str, ...] = ("max", "1y", "5d", "1d")


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------
# Holdings-row enrichment: when the rows of a fund's holdings list have
# blank facets, we can fill them in by looking up each holding's ticker
# on Yahoo (via yfinance.Ticker.info). Two paths trigger this:
#
#   * Yahoo top-10 fetch path (extractors.load_fund_data) — runs
#     automatically the first time we fetch a top-10 list (no threshold
#     gate; we always try).
#   * Manual "Enrich through Yahoo" button on the fund's holdings tile —
#     runs against the currently-cached rows (raw top-10 or a user
#     upload) and fills blanks only, never overwriting a non-blank cell.
#
# ``enrichment.fields`` is the user-controlled checklist of WHICH fields
# may be enriched. Unticked fields are skipped on every path. The set
# of legal field keys is :data:`ENRICHABLE_FIELDS` — restricted to the
# ones a per-symbol yfinance lookup can actually contribute.
#
# Default = all six ticked, which reproduces the previous behaviour
# ("auto-enrich top-10 with everything we can").
# ONE entry per facet. "sub_class" was listed separately until v0.72.1 —
# the same taxonomy at two grains, which is the shape removed from
# storage in v0.70.0, from the Resolve dialog in v0.70.2 and from the
# upload column mapping in v0.72.0. The asset enrichment takes the
# FINEST answer Yahoo gives (its sub_class where present, else its
# asset_class) and the definitions file derives the coarser levels: a
# finer value yields the coarser ones, never the reverse.
ENRICHABLE_FIELDS: tuple[str, ...] = (
    "name", "country", "currency", "asset_class", "sector",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "enrichment": {
        "fields": list(ENRICHABLE_FIELDS),
    },
    # Portfolio-level holdings aggregation: which field identifies "the
    # same holding" across two different funds, so their positions get
    # merged into one row on the portfolio Holdings sub-tab. One of
    # "name", "ticker", "isin". App-level (not per-portfolio). Ticker is
    # the default — exact, and present on most rows. "name" matches on a
    # normalised form (case-folded, whitespace-collapsed) but is not
    # fuzzy; "isin" is exact. A blank value for the chosen key never
    # matches anything (each such holding stays its own row).
    "holdings_match": {
        "key": "ticker",
    },
}

# Valid values for settings["holdings_match"]["key"].
HOLDINGS_MATCH_KEYS: tuple[str, ...] = ("name", "ticker", "isin")


# ---------------------------------------------------------------------------
# Breakdown cards
# ---------------------------------------------------------------------------
# The four facets shown as breakdown cards on the fund page and the
# portfolio X-ray. Each can independently carry a per-fund
# breakdown-source override (see BREAKDOWN_OVERRIDES_FP above).
BREAKDOWN_FACETS: tuple[str, ...] = (
    "asset_class", "sector", "country", "currency",
)

# ---------------------------------------------------------------------------
# Facet levels (v0.60.0)
# ---------------------------------------------------------------------------
# A facet with levels is ONE facet whose value is a tree, not several
# sibling facets that happen to be related.
#
# Sibling facets were half-built once and backed out. They are wrong
# because this table drives the targets UI: three sector entries would
# mean three unrelated sector target groups, and the rule that a parent
# is held to at least the sum of its targeted children cannot be
# expressed between facets that do not know they are related.
#
# Finest level FIRST. Non-tree facets declare a single level of the same
# shape, so no consumer ever branches on whether a facet has levels.
FACET_LEVELS: dict[str, tuple[str, ...]] = {
    "sector":      ("sub_sector", "sector", "super_sector"),
    "country":     ("country", "region", "super_region"),
    "currency":    ("currency",),
    "asset_class": ("sub_class", "asset_class", "super_class"),
}

# What a facet means when nobody names a level.
#
# Fixed per facet, NEVER computed from the data. "Deepest available"
# sounds more generous but makes the meaning of a word depend on what
# happened to arrive: uploading a more detailed factsheet for one fund
# would silently change the grain its targets are measured at and the
# grain the optimiser fits, with nothing on screen saying so.
#
# The level SELECTOR is a view over the finished levels and lives
# entirely in the browser. This constant is for everything that computes
# a number.
FACET_DEFAULT_LEVEL: dict[str, str] = {
    "sector":      "sector",
    "country":     "country",
    "currency":    "currency",
    # super_class, not the facet's own name. Until v0.70.0 the asset
    # vocabulary WAS equity / fixed_income / cash / other — which in the
    # tree is the super level, not the middle one. Defaulting to
    # "asset_class" would silently move every target, deviation and
    # optimiser fit to a much finer grain than the one they were set at.
    "asset_class": "super_class",
}

# ---------------------------------------------------------------------------
# Per-facet row columns (v0.76.0)
# ---------------------------------------------------------------------------
# A holdings row stores a facet as a small pack of columns, and three of
# them are named for the CONCEPT rather than for the facet: the asset
# tree's columns are ``asset_node`` / ``asset_level`` / ``asset_raw``,
# not ``asset_class_node``, because the tree is *asset* and
# ``asset_class`` is only its middle level. Every other facet's concept
# name happens to equal its facet name, which is why the difference was
# survivable as an inline ``"asset" if facet == "asset_class" else facet``
# in two places in app.py. It is a registry here so that the raw and
# pinned columns cannot be derived one way in one module and another way
# in the next.
FACET_COLUMN_STEM: dict[str, str] = {
    "sector":      "sector",
    "country":     "country",
    "currency":    "currency",
    "asset_class": "asset",
}


def facet_raw_field(facet: str) -> str:
    """Row column holding what the SOURCE said, before resolution.

    Written once at ingest and never re-derived — it is the only field
    on the row that is evidence rather than a conclusion. See
    :func:`porxpy.utils.normalise_facets` for why the resolution reads
    from it on every pass.
    """
    return f"{FACET_COLUMN_STEM.get(facet, facet)}_raw"


def facet_node_field(facet: str) -> str:
    """Row column holding the resolved node.

    Currency answers with its own single column rather than a
    ``currency_node``: its tree is one level deep, so the node and the
    level column are the same thing and inventing a second name for one
    value would be the arity branch FACET_LEVELS exists to avoid.
    """
    stem = FACET_COLUMN_STEM.get(facet, facet)
    return stem if facet == "currency" else f"{stem}_node"


def facet_level_field(facet: str) -> str:
    """Row column holding the grain the node sits at, or ``""``.

    Empty for currency, which has one level and therefore nothing to
    stamp.
    """
    stem = FACET_COLUMN_STEM.get(facet, facet)
    return "" if facet == "currency" else f"{stem}_level"


def facet_pinned_field(facet: str) -> str:
    """Row column marking the node as a USER decision, not a resolution.

    Set when someone edits the facet on selected rows. It stops
    :func:`porxpy.utils.normalise_facets` re-deriving the node from the
    raw text on the next read — which would otherwise silently undo the
    edit, since re-resolving the untouched source text is exactly what
    the row is being read for.
    """
    return f"{FACET_COLUMN_STEM.get(facet, facet)}_pinned"


# The extra level the holdings TABLES offer in their column-header level
# selector: the text the source actually said, before any resolution.
#
# Deliberately NOT a member of FACET_LEVELS. That registry drives the
# rollups, level coverage, FACET_DEFAULT_LEVEL, the targets editor and
# the optimiser's rows, and "raw" is not a level of the facet tree — it
# has no parent, no children, and no partition property. Rolling holdings
# up by raw text would produce one bucket per spelling, so a card would
# show a slice per typo and a target measured at that grain would mean
# nothing. It is a VIEW of a row's own evidence, which is why it lives
# in a display registry read only by the two holdings tables.
#
# The extra entry is the raw COLUMN's name rather than a bare "raw",
# because every consumer of a level reads it as a row field —
# ``row[level]`` in the cell renderer, in the column filter and in the
# sort key alike. Naming the level after the column it displays keeps
# that true for the raw view, so it needs no special case in any of
# them; a level called "raw" would have had to be translated in three
# separate readers, which is three places to forget.
FACET_DISPLAY_LEVELS: dict[str, tuple[str, ...]] = {
    facet: (*levels, facet_raw_field(facet))
    for facet, levels in FACET_LEVELS.items()
}

# ``development`` is the third country level, added in v0.62.0, and it
# keeps country a strict chain: country -> region -> development, with
# every region mapping to exactly one status.
#
# What is NOT here is a geographic super level. regions.csv's
# ``super_regions`` column flattens two orthogonal groupings plus a
# universal into one list, and the geographic half is not a partition:
# northAmerica, latinAmerica and africaMiddleEast have no geographic
# parent, while japan and asiaDeveloped claim both asia and pacific.
# Rolling up to it would count Japan twice and lose North America.
#
# The two are also not orderable. Neither derives from the other —
# americas, europe and asia each span both development statuses, and
# every status spans several geographies — so they are siblings above
# region, not links in a chain. Adding geography later means `levels`
# forks, and the stage-3 rule that a parent is held to at least the sum
# of its targeted children stops being able to read parenthood off list
# adjacency. Deferred deliberately, not overlooked.

# Valid values for a per-card breakdown-source override.
#   "yahoo"    — issuer-published Fund/ETF-level data (the default; no
#                override entry is stored for this value).
#   "holdings" — populate this card from the fund's physical holdings
#                roll-up instead.
#   "upload"   — populate this card from a user-uploaded CSV. Per-facet
#                item lists live in the ``uploaded_breakdowns`` fund-
#                level cache category (see below); only facets the CSV
#                actually covered can be flipped to this source.
# Where a breakdown card's numbers come from.
#
#   yahoo     the issuer's own card as Yahoo reports it
#   factsheet the issuer's own numbers, read off an uploaded factsheet
#   holdings  computed by looking through the fund's holdings
#   upload    a CSV the user supplied
#
# "yahoo" was called "fund" until v0.44.0. That name meant "the issuer
# card" back when Yahoo was the only way to get one; with a second issuer
# source it says nothing that distinguishes the two, and a stored key
# whose name contradicts its label is how confusion starts. Renamed, with
# a migration.
#   from_country  currency only — the country card, converted country by
#                 country to each country's primary currency
BREAKDOWN_SOURCES: tuple[str, ...] = ("yahoo", "factsheet", "holdings",
                                      "upload", "from_country")

# Which sources supply their own item lists rather than deriving them.
# Both are stored per facet per source in the uploaded_breakdowns cache.
SUPPLIED_BREAKDOWN_SOURCES: tuple[str, ...] = ("upload", "factsheet")

# ---------------------------------------------------------------------------
# Cross-facet derived sources (v0.86.0)
# ---------------------------------------------------------------------------
# A source whose numbers are ANOTHER FACET'S finished card, converted.
# The map is {facet: facet it derives from} and is deliberately a
# registry rather than an if-test on "currency", so a second such pair
# — if one is ever justified — is a row here and not a new branch in
# build_fund_breakdowns.
#
# Only currency has one, and only from country. Currency is the facet
# most often missing: plenty of issuers publish a geographic split and
# no currency split at all, and for a long-only fund of listed equities
# "where is it" is a decent proxy for "what is it priced in". The
# reverse derivation is NOT offered and must not be added by symmetry —
# a currency does not name a country (EUR spans twenty of them), so
# country-from-currency would invent detail rather than convert it.
#
# The derivation is over the SOURCE facet's card as the user has it: its
# chosen source, its completeness assertion, its own unknown slice. That
# is what makes the two behave as one decision in the direction the user
# asked for — completing country completes the currency derived from it
# — while leaving the reverse direction untouched, since nothing about
# the country card is computed from currency.
DERIVED_BREAKDOWN_SOURCES: dict[str, str] = {
    "currency": "country",
}


def derived_source_key(from_facet: str | None) -> str:
    """The source name a derivation from ``from_facet`` is stored under.

    One function so the stored string, the selector button and the
    validation vocabulary cannot disagree about what the source is
    called. Returns ``""`` for ``None``, which is what a facet with no
    derivation registered passes in.
    """
    return f"from_{from_facet}" if from_facet else ""


# Every derived source name, for the validators that need to ask "is
# this source facet-restricted?" without knowing which facet.
_DERIVED_SOURCE_KEYS: frozenset[str] = frozenset(
    derived_source_key(f) for f in DERIVED_BREAKDOWN_SOURCES.values())


def sources_for_facet(facet: str) -> tuple[str, ...]:
    """Which sources EXIST for this facet, whether or not a fund has them.

    The common four, plus this facet's derivation where it has one. A
    derived source is meaningless on the facets that register none —
    "sector from country" says nothing — so it is not a source those
    cards lack, it is not one of their sources at all.

    That distinction is the reason this is separate from a card's
    ``available`` map, which answers the different question "does THIS
    fund have it": a fund with no factsheet still has a factsheet source,
    shown struck through, because uploading one would fill it. Nothing
    would ever fill "sector from country".

    One function so the three consumers cannot disagree: the override
    vocabulary that validates a stored pin, the endpoint that accepts a
    new one, and the block the selector is drawn from.

    Args:
        facet: One of :data:`BREAKDOWN_FACETS`.

    Returns:
        The source names in :data:`BREAKDOWN_SOURCES` order.
    """
    own = derived_source_key(DERIVED_BREAKDOWN_SOURCES.get(facet))
    return tuple(s for s in BREAKDOWN_SOURCES
                 if s not in _DERIVED_SOURCE_KEYS or s == own)


# ---------------------------------------------------------------------------
# Holdings sources (v0.77.0)
# ---------------------------------------------------------------------------
# Where a fund's per-position holdings list comes from. Deliberately the
# same three names the breakdown cards use for the sources that HAND US
# numbers, minus "holdings" — a holdings list cannot be looked through to
# itself — so the fund page's two source selectors read alike:
#
#   yahoo     the top-10 Yahoo publishes, optionally enriched per symbol
#   factsheet the position table read off an uploaded factsheet
#   upload    a CSV/XLSX the user supplied
#
# All three are stored SIDE BY SIDE, one blob per source, in the single
# ISIN-keyed ``holdings`` cache slot (see utils.holdings_store_get). Until
# 0.77.0 the slot held one blob and the newest write won, so uploading a
# CSV destroyed the Yahoo rows and there was no way back without a
# refetch. Independent slots are what makes the selector a selector
# rather than a re-import.
HOLDINGS_SOURCES: tuple[str, ...] = ("yahoo", "factsheet", "upload")

# Which source gets used when the user has pinned none, richest first.
# An upload is a full position list, a factsheet table is usually the top
# holdings with real facet columns, and Yahoo's top-10 is the thinnest of
# the three. This is the rule that keeps a fresh upload visible without
# the user having to also select it — the behaviour that existed when the
# slot held only one blob, preserved rather than reinvented.
HOLDINGS_SOURCE_PRECEDENCE: tuple[str, ...] = ("upload", "factsheet", "yahoo")

# A blob's ``source`` field records its VARIANT — how complete and how
# enriched the rows are — and predates the source concept by a dozen
# releases. Variant and source answer different questions ("how good are
# these rows?" vs "who said them?"), so both are kept, and this map is
# the one place that relates them. Every reader of a variant name goes
# through it rather than testing the string itself.
#
#   manual_upload   full per-position list from a user file
#   yahoo_enriched  Yahoo top-10 plus per-symbol Yahoo lookups
#   yahoo_top10     raw Yahoo top-10, sparse rows
#   factsheet       positions read off a factsheet by the AI helper
HOLDINGS_VARIANT_SOURCE: dict[str, str] = {
    "manual_upload":  "upload",
    "yahoo_enriched": "yahoo",
    "yahoo_top10":    "yahoo",
    "factsheet":      "factsheet",
}

# The variant a source writes when it has nothing more specific to say.
# Used when a source slot is created (an empty Yahoo fetch, a factsheet
# with no position table) so a slot always names its own variant.
HOLDINGS_SOURCE_VARIANT: dict[str, str] = {
    "yahoo":     "yahoo_top10",
    "factsheet": "factsheet",
    "upload":    "manual_upload",
}


def holdings_source_of(variant: str) -> str:
    """The holdings SOURCE a blob variant belongs to.

    Args:
        variant: A blob's ``source`` field — ``manual_upload``,
            ``yahoo_top10``, ``yahoo_enriched`` or ``factsheet``.

    Returns:
        One of :data:`HOLDINGS_SOURCES`, or ``""`` for an unknown or
        empty variant (a slot that has never been written).
    """
    return HOLDINGS_VARIANT_SOURCE.get((variant or "").strip(), "")


# What KIND of rows fed a look-through roll-up, per blob variant. The
# breakdown cards label themselves from this ("LOOK-THROUGH" vs
# "ENRICHED") and the fund page uses it to decide whether a sparse
# per-position sector column is expected. Three copies of this mapping
# existed as if/elif chains — the fund load, the row editor and the
# enrichment button — which is three places for a new variant to be
# forgotten; a factsheet's positions were exactly the variant that would
# have been.
HOLDINGS_VARIANT_ROLLUP: dict[str, str] = {
    "manual_upload":  "full",
    "yahoo_enriched": "top10_enriched",
    "yahoo_top10":    "top10_raw",
    # A factsheet position table is the issuer's own list and usually
    # names the top holdings with real facet columns beside them. It is
    # neither a full list nor a Yahoo top-10, so it says which it is
    # rather than borrowing a label that would misdescribe its coverage.
    "factsheet":      "factsheet",
}


def rollup_label_of(variant: str) -> str:
    """The ``breakdowns_source`` label for a holdings blob variant.

    Args:
        variant: A blob's ``source`` field.

    Returns:
        ``full`` / ``top10_enriched`` / ``top10_raw`` / ``factsheet``, or
        ``none`` when there is no recognisable variant.
    """
    return HOLDINGS_VARIANT_ROLLUP.get((variant or "").strip(), "none")


# ---------------------------------------------------------------------------
# Fund "Structure" metadata
# ---------------------------------------------------------------------------
# Three coupled attributes describing how a fund is built. Surfaced in
# the "Fund Meta Data" tile and editable via the "Edit fund" dialog.

# Fund structure — the wrapper type. Seeded from Yahoo ``quoteType``
# (ETF → "etf", everything else fund-like → "fund").
FUND_STRUCTURES: tuple[str, ...] = ("etf", "fund", "unknown")

# Replication method — how an index-tracking ETF holds its index.
# Only meaningful for ETFs; a plain (non-ETF) fund is "n/a". Yahoo
# publishes no replication data, so this is user-set. "unknown" is the
# default for an ETF until the user fills it in.
#   full      — full physical replication (holds every constituent)
#   sampled   — physical replication via (optimised) sampling
#   synthetic — swap-based / derivative replication
#   n/a       — not applicable (the fund is not an ETF)
#   unknown   — an ETF whose replication method has not been set
REPLICATION_METHODS: tuple[str, ...] = (
    "full", "sampled", "synthetic", "n/a", "unknown",
)

# Management style — active stock-picking vs passive index-tracking.
# Seeded from ``quoteType`` (ETF → passive, fund → active) as a default
# only; active ETFs and index funds both exist, so it stays editable.
FUND_STYLES: tuple[str, ...] = ("active", "passive", "unknown")

# v0.21.0: distribution policy. Detected from fund name + dividend yield
# (see :func:`porxpy.extractors.detect_distribution`) and editable by
# the user the same way as Replication and Style.
DISTRIBUTION_POLICIES: tuple[str, ...] = (
    "accumulating", "distributing", "unknown",
)

# v0.27.0 — fund metadata used by the optimiser.
#
# These sit alongside structure/replication/style/distribution in the same
# override block: they are fund metadata, derived from Yahoo with a user
# override, exactly like distribution policy.
#
# They are also *targetable*. That mirrors asset_class, which is likewise
# both a metadata classification (detect_asset_class → "equity") and a
# target facet. See TARGET_FACETS below.

# Market-cap bucket. Derived from Yahoo's ``medianMarketCap`` for the
# fund's equity sleeve, using the thresholds in MARKET_CAP_BUCKETS.
# v0.28.0: "mixed" and "n/a" join the three buckets.
#   mixed   — the fund is built to span buckets (a total-market tracker,
#             a "Small & Mid Cap" fund). An intention, so targetable.
#   unknown — missing data. Not targetable.
#   n/a     — market cap is not a meaningful concept for this position.
#             In practice: cash. Bond funds are NOT n/a — a bond issuer
#             has a market cap; we usually just don't know it, which is
#             what "unknown" is for.
MARKET_CAPS: tuple[str, ...] = (
    "large", "mid", "small", "mixed", "unknown", "n/a",
)

# Threshold table, in USD, evaluated top-down: the first bucket whose
# ``min_usd`` the fund's median market cap meets wins. Kept here rather
# than in a resource CSV because it is three numbers that essentially
# never change; promote it to a CSV if that stops being true.
MARKET_CAP_BUCKETS: tuple[tuple[str, float], ...] = (
    ("large", 10_000_000_000.0),   # >= $10bn
    ("mid",    2_000_000_000.0),   # >= $2bn
    ("small",          0.0),       # anything above zero
)

# Morningstar-style equity style box axis. NOTE the deliberate naming:
# ``style`` already means active-vs-passive (FUND_STYLES above), so the
# growth/value axis is ``style_box`` to avoid a collision in storage,
# in the API and on the fund meta tile.
# No "n/a" here and no "mixed": "blend" already IS the mixed case, and
# every asset class has a position on this axis — a bond fund's return
# comes from coupons rather than capital appreciation, which is "value"
# by any reading. Cash likewise.
STYLE_BOXES: tuple[str, ...] = ("growth", "blend", "value", "unknown")

# ── The two residuals of a breakdown ────────────────────────────────────
# Weight that lands in no real bucket is not all the same thing, and the
# meta facets above already say so: "unknown" is a data gap, "n/a" means
# the concept does not apply to this position. The four breakdown facets
# used to call both "undefined", which answered the wrong question — the
# reader wants to know whether better data would fix it.
#
#   unknown — there is a value; this source does not have it. The
#             factsheet omits the currency split; a top-10 holdings list
#             says nothing about the other 90%. More data would fix it.
#   n/a     — there is no value to have. A cash balance has no sector.
#             No source will ever fill this in, and it is not a gap.
#
# Only "unknown" counts against coverage. Treating n/a as a shortfall
# left a cash-heavy portfolio permanently under-covered on sector with
# nothing anyone could do about it.
UNKNOWN_KEY: str = "unknown"
NA_KEY:      str = "n/a"

# Which asset classes make a facet inapplicable, so a blank value becomes
# n/a rather than unknown. Deliberately narrow: PorxPy is asserting
# something no source said, so it does it only where the answer is not
# arguable. Cash has no sector and no country of issue.
#
# Commodities are a defensible addition to both — bullion has no equity
# sector and no issuer — but that is a judgement about instruments rather
# than a fact about the concept, so it is left out until asked for.
# Bond positions are NOT here: an issuer has a sector and a country, and
# a blank one is missing data, which is what "unknown" is for.
#
# A facet absent from this table is never n/a: every position is
# denominated in some currency and belongs to some asset class.
FACET_NOT_APPLICABLE: dict[str, frozenset[str]] = {
    "sector":     frozenset({"cash"}),
    "sub_sector": frozenset({"cash"}),
    "country":    frozenset({"cash"}),
    # A cash balance sits in no country, and therefore in no region.
    # Without this the region view would report a cash sleeve as a gap
    # while the country view reported it inapplicable — the same
    # position answered two ways.
    "region":       frozenset({"cash"}),
    "super_region": frozenset({"cash"}),
}

# A `custody` facet (direct / via_fund) lived here in v0.89.0, to make
# "cash I hold myself" a percentage target the optimiser could satisfy
# separately from a fund's own cash sleeve. It was removed in v0.90.0
# when that target became an AMOUNT reserved off the top instead (see
# `cash_reserve` on the portfolio). A reservation needs no facet: the
# reserved money is simply not in the optimiser's matrix, so no fund can
# occupy it by construction, and the funds/cash/total figures on the
# portfolio overview already state the split in money. The facet was the
# right mechanism for a percentage target and the wrong one for a
# reservation.

# Which values of each meta facet may carry a target. "unknown" is a
# data gap rather than an intention, and "n/a" duplicates the cash
# target you would already set on asset_class — neither belongs in the
# Targets dropdown. Both still appear as buckets in the X-ray card and
# in the deviation report's untargeted summary.
META_FACET_TARGETABLE: dict[str, tuple[str, ...]] = {
    "market_cap": ("large", "mid", "small", "mixed"),
    "style_box":  ("growth", "blend", "value"),
}

# What a fund is built to concentrate on. ``focus_detail`` is validated
# against a different vocabulary depending on this: regions for
# "region", the sectors CSV for "sector", and free text for "thematic"
# (nothing can infer "Artificial Intelligence" from Yahoo).
# v0.68.0: "region" renamed to "geography", because the vocabulary
# behind it is no longer regions alone — a fund's focus can now be a
# country, a region or a super region, and calling that set "region"
# named one of its three levels after the whole.
#
# LEGACY_FOCUS_TYPES maps the old value forward. Stored focus_type
# values are migrated on read rather than by a rewrite: focus_type feeds
# peer_key, so a fund left on "region" while its neighbours moved to
# "geography" would silently drop out of its own peer group.
FOCUS_TYPES: tuple[str, ...] = ("none", "geography", "sector", "thematic")
LEGACY_FOCUS_TYPES: dict[str, str] = {"region": "geography"}

# Default Structure block for a fund with no stored override. The
# values here are placeholders; load_fund_data overlays Yahoo-seeded
# defaults (see _seed_fund_structure) before applying any stored
# override on top.
DEFAULT_FUND_STRUCTURE: dict[str, str] = {
    "structure":    "unknown",
    "replication":  "unknown",
    "style":        "unknown",
    "distribution": "unknown",
    "market_cap":   "unknown",
    "style_box":    "unknown",
    "focus_type":   "none",
    "focus_detail": "",
}

# ---------------------------------------------------------------------------
# Credit quality (v0.93.0)
# ---------------------------------------------------------------------------
# A bond holding's credit rating, as the issuer's file wrote it. Two
# agencies' scales are in circulation and a holdings file may use either,
# or a mangled version of one: S&P and Fitch write AAA / AA+ / BBB- ,
# Moody's writes Aaa / Aa1 / Baa3 / Caa1, and files in the wild arrive
# uppercased ("BAA-", "CAA+"), hyphenated, suffixed ("AA (sf)") or with
# an outlook attached ("BBB- *-").
#
# The RANK is what the app needs and the spelling is what the user needs.
# So the row stores the source's own wording — like every `<facet>_raw`
# column — and this table answers the one question the wording cannot:
# which of two ratings is better. Without it a rating column sorts
# alphabetically, where "AA+" lands before "AAA" and "B" before "BBB",
# which is worse than not sorting at all because it looks sorted.
#
# Kept in config rather than a resource CSV for the same reason
# MARKET_CAP_BUCKETS is: it is a closed scale of two dozen entries that
# the agencies essentially never change. Promote it to a CSV if that
# stops being true.
#
# One ordinal covers both scales because they are deliberately parallel —
# Moody's Baa2 IS S&P's BBB, and every published mapping says so. The
# number is an order, not a score: the gap between two adjacent ranks
# carries no meaning and nothing should average them.
CREDIT_QUALITY_RANK: dict[str, int] = {
    # investment grade
    "AAA": 1,  "AA+": 2,  "AA": 3,  "AA-": 4,
    "A+":  5,  "A":   6,  "A-":  7,
    "BBB+": 8, "BBB": 9,  "BBB-": 10,
    # speculative grade
    "BB+": 11, "BB": 12,  "BB-": 13,
    "B+":  14, "B":  15,  "B-":  16,
    "CCC+": 17, "CCC": 18, "CCC-": 19,
    "CC":  20, "C":  21,
    # in default, and the two "no rating" spellings that are an answer
    # rather than a gap: the issuer said the holding is unrated.
    "D":   22,
    "NR":  23, "UNRATED": 23,
}

# Moody's spellings, mapped onto the same ordinal. Written out rather
# than derived by a transliteration rule, because the rule has exceptions
# (Moody's has no "Aa+", it has "Aa1") and a rule with exceptions is
# harder to check than a table.
CREDIT_QUALITY_ALIASES: dict[str, str] = {
    "AAA": "AAA",
    "AA1": "AA+",  "AA2": "AA",   "AA3": "AA-",
    "A1":  "A+",   "A2":  "A",    "A3":  "A-",
    "BAA1": "BBB+", "BAA2": "BBB", "BAA3": "BBB-",
    "BA1": "BB+",  "BA2": "BB",   "BA3": "BB-",
    "B1":  "B+",   "B2":  "B",    "B3":  "B-",
    "CAA1": "CCC+", "CAA2": "CCC", "CAA3": "CCC-",
    "CAA": "CCC",  "CAA+": "CCC+", "CAA-": "CCC-",
    "BAA": "BBB",  "BAA+": "BBB+", "BAA-": "BBB-",
    "BA":  "BB",   "CA":  "CC",
    "N/A": "NR",   "NOT RATED": "NR", "UNRATED": "NR",
}


def credit_quality_rank(raw: str | None) -> int | None:
    """Where a written rating sits on the scale, or ``None``.

    Args:
        raw: Whatever the source wrote — "BBB-", "Baa3", "AA (sf)",
            "  bb+ ", or anything else.

    Returns:
        The ordinal (1 is best), or ``None`` when the text names no
        rating this app recognises. ``None`` is deliberate rather than a
        worst-case number: an unrecognised spelling is a gap in this
        table, and sorting it to the bottom as though it were junk debt
        would be a claim nobody made.
    """
    if not raw:
        return None
    key = str(raw).strip().upper()
    if not key:
        return None
    # Strip the decorations that ride along with a rating without
    # changing it: a structured-finance "(sf)" suffix, an outlook, and
    # surrounding punctuation. Done before the lookup so "AA (sf)" and
    # "AA" are one entry rather than two.
    for cut in (" (", " *", "/", ","):
        if cut in key:
            key = key.split(cut, 1)[0].strip()
    key = key.replace(" ", "")
    key = CREDIT_QUALITY_ALIASES.get(key, key)
    return CREDIT_QUALITY_RANK.get(key)


# Whether a fund is offered to the optimiser at all. Stored per fund
# (ISIN-keyed, like the rest of this block) rather than per listing,
# since two listings of the same fund are the same investment decision.
DEFAULT_INCLUDE_IN_OPTIMIZER: bool = True

# ---------------------------------------------------------------------------
# Facet taxonomy
# ---------------------------------------------------------------------------
# Three names, because two distinct things were previously conflated:
#
#   BREAKDOWN_FACETS — have breakdown *cards* on the fund page, with a
#       selectable source (issuer / holdings look-through / CSV upload).
#   META_FACETS      — derived from fund metadata; one-hot per fund, read
#       as a scalar off the fund's structure block. There is no "issuer
#       market-cap breakdown" to choose between, so these get no source
#       selector. Both answer at a single level, so neither has an entry
#       in FACET_LEVELS.
#   TARGET_FACETS    — everything you can set a target on, and therefore
#       everything the optimiser and the deviation report work across.
#
# When look-through market-cap data arrives later (per-holding marketCap
# via the existing holdings-enrichment path), market_cap can graduate to
# a real distribution — exactly as asset_allocation supersedes the
# asset_class scalar today — without changing this taxonomy.
META_FACETS: tuple[str, ...] = ("market_cap", "style_box")

TARGET_FACETS: tuple[str, ...] = BREAKDOWN_FACETS + META_FACETS



# ---------------------------------------------------------------------------
# Overridable field registry (v0.33.0)
# ---------------------------------------------------------------------------
# One table describing every fund attribute the user may assert a value
# for. Replaces four purpose-built override sub-structures in
# overrides.json (asset_class, breakdown_source, include_in_optimizer,
# fund_structure), each of which had its own storage shape, its own
# validation and its own answer to "what does absent mean".
#
# The single most useful property of the new store is that it is SPARSE:
# a field is present exactly when the user has asserted something about
# it, and absent otherwise. That makes "no opinion" unrepresentable as a
# stored value, which is what the v0.27 override bug was — a block that
# always wrote all eight structure fields, so a stored "unknown" could
# not be told apart from an untouched one, and saving the Edit dialog
# once pinned every field to a snapshot of Yahoo.
#
# ``neutral`` exists only to migrate the old dense blocks: a legacy value
# equal to its neutral was never an assertion and is dropped rather than
# carried across. Nothing writes a neutral value from here on.
#
# Per-entry keys:
#   type      "enum" | "number" | "bool" | "str"
#   vocab     allowed values, for type "enum"
#   vocab_fn  name of a resolver in porxpy.resources, for vocabularies
#             that live in a resource file (primary_asset_class) or
#             depend on another field's value (focus_detail). Looked up
#             lazily so config stays free of resource-CSV imports.
#             Read ``vocab`` and ``vocab_fn`` through field_vocab(), not
#             directly — a caller that reads only one silently sees an
#             empty vocabulary for fields declaring the other.
#   min/max   inclusive bounds, for type "number"
#   unit      "%" | "currency" — display only
#   label     what the UI calls it
#   neutral   the legacy "no opinion" value (migration only)
#   target    dotted path into the /api/fund payload where the effective
#             value is written. Optional: fields whose consumer reads
#             them directly (breakdown sources, include_in_optimizer)
#             have no payload slot to write into.
OVERRIDABLE_FIELDS: dict[str, dict] = {
    # Renamed from "asset_class" in v0.47.0.
    #
    # Two different things carried that name: this one — what kind of fund
    # this is, a single value from the issuer or Yahoo's classification —
    # and the asset_class BREAKDOWN FACET, which is what the fund holds,
    # as a distribution derived from look-through. Labels alone could have
    # separated them in the UI, but the AI extraction prompt is generated
    # from this registry and from BREAKDOWN_FACETS, so it listed
    # "asset_class" twice, asking the model for one name with two
    # meanings. That is a correctness problem, not a cosmetic one.
    #
    # The facet keeps its name; only the metadata field moves.
    "primary_asset_class": {
        "type": "enum", "vocab_fn": "primary_asset_classes", "neutral": None,
        "label": "Primary asset class",
        # Targeted so it reaches the payload like every other field — and,
        # more to the point, so it appears in the extraction prompt, which
        # is built from the targeted fields. Without a target it was
        # silently unextractable.
        "target": "asset_class.class",
    },
    "structure": {
        "type": "enum", "vocab": FUND_STRUCTURES, "neutral": "unknown",
        "label": "Fund structure", "target": "fund_structure.structure",
    },
    "replication": {
        # "n/a" IS meaningful here (a non-ETF fund), so only "unknown"
        # counts as the absence of an opinion.
        "type": "enum", "vocab": REPLICATION_METHODS, "neutral": "unknown",
        "label": "Replication", "target": "fund_structure.replication",
    },
    "style": {
        "type": "enum", "vocab": FUND_STYLES, "neutral": "unknown",
        "label": "Management style", "target": "fund_structure.style",
    },
    "distribution": {
        "type": "enum", "vocab": DISTRIBUTION_POLICIES, "neutral": "unknown",
        "label": "Distribution", "target": "fund_structure.distribution",
    },
    "market_cap": {
        "type": "enum", "vocab": MARKET_CAPS, "neutral": "unknown",
        "label": "Market cap", "target": "fund_structure.market_cap",
    },
    "style_box": {
        "type": "enum", "vocab": STYLE_BOXES, "neutral": "unknown",
        "label": "Equity style", "target": "fund_structure.style_box",
    },
    "focus_type": {
        "type": "enum", "vocab": FOCUS_TYPES, "neutral": "none",
        "label": "Focus", "target": "fund_structure.focus_type",
    },
    "focus_detail": {
        # Validated against regions ∪ super-regions, the sector list, or
        # nothing at all, depending on the pending focus_type — the one
        # cross-field rule in the table.
        "type": "str", "vocab_fn": "focus_detail_vocabulary", "neutral": "",
        "label": "Focus detail", "target": "fund_structure.focus_detail",
    },
    "include_in_optimizer": {
        "type": "bool", "neutral": DEFAULT_INCLUDE_IN_OPTIMIZER,
        "label": "Include in optimiser",
    },
    "expenseRatioPct": {
        "type": "number", "min": 0.0, "max": 100.0, "unit": "%",
        "label": "TER", "target": "profile.expenseRatioPct",
    },
    "totalNetAssets": {
        "type": "number", "min": 0.0, "unit": "currency",
        "label": "Total net assets", "target": "profile.totalNetAssets",
    },
    # The rest of the sourceable fields. They were displayed and cached
    # but never in the registry, so they could not be validated, could
    # not be extracted from a factsheet, and could not carry a pin.
    "trailingYieldPct": {
        "type": "number", "min": 0.0, "max": 100.0, "unit": "%",
        "label": "Trailing yield", "target": "profile.trailingYieldPct",
    },
    "forwardYieldPct": {
        "type": "number", "min": 0.0, "max": 100.0, "unit": "%",
        "label": "Forward yield", "target": "profile.forwardYieldPct",
    },
    "holdingsCount": {
        # What the FUND says it holds — "Aantal aandelen 1442" — not how
        # many rows we managed to load. Ours may be a top-10.
        "type": "number", "min": 0.0, "max": 100000.0,
        "label": "Number of holdings", "target": "profile.holdingsCount",
    },
    "previousClose": {
        "type": "number", "min": 0.0, "unit": "currency",
        "label": "Last close", "target": "profile.previousClose",
    },
    "navPrice": {
        "type": "number", "min": 0.0, "unit": "currency",
        "label": "NAV", "target": "profile.navPrice",
    },
    "regularMarketVolume": {
        "type": "number", "min": 0.0,
        "label": "Volume", "target": "profile.regularMarketVolume",
    },
    "fiftyTwoWeekHigh": {
        "type": "number", "min": 0.0, "unit": "currency",
        "label": "52w high", "target": "profile.fiftyTwoWeekHigh",
    },
    "fiftyTwoWeekLow": {
        "type": "number", "min": 0.0, "unit": "currency",
        "label": "52w low", "target": "profile.fiftyTwoWeekLow",
    },
    "turnoverPct": {
        # Was displayed and cached but never overridable, so it could be
        # neither corrected by hand nor read from a factsheet.
        "type": "number", "min": 0.0, "max": 1000.0, "unit": "%",
        "label": "Portfolio turnover", "target": "profile.turnoverPct",
    },
}

# The four breakdown-source selectors are one registry entry each, built
# here rather than typed out, so a new breakdown facet cannot be added
# without its override coming along.
for _facet in BREAKDOWN_FACETS:
    # A derived source is legal only on the facet that registers one, so
    # each selector's vocabulary is the common sources plus its own
    # derivation — not the full BREAKDOWN_SOURCES tuple. Otherwise
    # "sector from country" would validate and then resolve to nothing,
    # which is a stored pin no card can honour.
    OVERRIDABLE_FIELDS[f"breakdown_source.{_facet}"] = {
        "type": "enum", "neutral": "yahoo",
        "vocab": sources_for_facet(_facet),
        "label": f"{_facet} card source",
    }
    # "Read this card's source as covering the whole fund." Stored beside
    # the source pin because it is the same kind of thing: a fund-level
    # decision about what the card MEANS, not a way of looking at it. It
    # has to be stored rather than held in the browser precisely because
    # the consumers that matter are elsewhere — the portfolio X-ray, the
    # target deviations and the optimiser all read the fund's card, and
    # an `unknown` slice is not something a solver can allocate against.
    OVERRIDABLE_FIELDS[f"breakdown_complete.{_facet}"] = {
        "type": "bool", "neutral": False,
        "label": f"{_facet} coverage asserted complete",
    }
del _facet

# The holdings tile has the same kind of selector as those four cards —
# which source's numbers this fund shows — so its pin is stored the same
# way: a fund-level override keyed by ISIN, applied on read, cleared to
# fall back to HOLDINGS_SOURCE_PRECEDENCE. There is no "neutral" value:
# unlike a breakdown card, whose unpinned state is always Yahoo, an
# unpinned holdings tile follows the precedence list, so the absence of
# an entry is itself the meaningful state.
OVERRIDABLE_FIELDS["holdings_source"] = {
    "type": "enum", "vocab": HOLDINGS_SOURCES,
    "label": "Holdings source",
}


# ---------------------------------------------------------------------------
# Best-in-class scoring (v0.35.0)
# ---------------------------------------------------------------------------
# Funds that fit the exposure targets equally well are not equally good.
# Scoring ranks the pre-loaded universe on cost, size and trailing
# returns so the optimiser can prefer the better ones — and so the fund
# list can show what "better" means before the optimiser is involved.
#
# Everything is a PERCENTILE (0-100, higher is better), not a raw value.
# Percentiles are scale-free, so a TER in percent and a fund size in
# billions combine without normalisation and no single outlier can
# dominate. They also survive the universe changing size: a preset tuned
# against 35 funds means the same thing at 50.

# Trailing-return windows, in calendar days. Approximate by design — the
# nearest available close is used, and markets are shut on plenty of
# exact anniversaries.
RETURN_PERIODS: dict[str, int] = {
    "1m":  30,
    "3m":  91,
    "6m":  182,
    "1y":  365,
    "3y":  1095,
    "5y":  1825,
}

# A peer group smaller than this cannot be ranked within.
#
# Peer groups are asset class x focus, which produces groups of one — the
# only technology-sector fund, the only European bond fund. There is no
# ranking to be had inside a group of one, so below this size the peer
# score is None and the optimiser leaves the fund alone on score grounds.
# It stays fully eligible on targets — which is why it is held anyway.
#
# Was 3 until v0.96.0, on the reasoning that a two-fund percentile is
# more misleading than no percentile: under the old i/(n-1) formula the
# pair scored 0 and 100, which reads as "worthless" and "perfect" on the
# strength of whatever separates them, however little that is. That was
# a defect in the FORMULA rather than a fact about pairs — one of two
# funds really does outrank the other, and refusing to say so left every
# such fund unrated and invisible to the optimiser. With the plotting
# position rank/(n+1) a pair scores 33.3 and 66.7 (see
# scoring._percentiles), which states the ordering without overstating
# the confidence, so the gate now sits at the smallest group that HAS an
# ordering.
# How many lookups in a row may fail at the TRANSPORT level before an
# enrichment run gives up. Not a retry budget: each of these is a
# request that never reached an answer (TLS, DNS, proxy, or Yahoo
# refusing to serve us), and the run has thousands more rows to go.
#
# Without this, a rate limit part-way through a large fund is invisible:
# every remaining row fails the same way, the loop carries on to the end
# because a failure is not a reason to stop, and the run "completes"
# having filled nothing and spent an hour proving it. Twelve consecutive
# failures is far past coincidence — a genuine unresolvable holding
# comes back "Yahoo does not know this symbol", which is a different
# counter and never trips this one.
#
# Deliberately not a delay between calls. A fixed sleep would make an
# already-long run longer while doing nothing about the case it exists
# for; stopping early and saying why is what the user can act on.
ENRICH_TRANSPORT_FAILURE_LIMIT: int = 12

MIN_PEER_GROUP: int = 2

# Fund size is a FLOOR TEST, not a percentile.
#
# Size matters as "big enough not to be at risk of closure or to trade
# badly", and above that threshold more of it is not better: a EUR 100bn
# fund is not twice the fund a EUR 50bn one is. So the component scores
# 100 when the fund clears the floor and 0 when it does not, rather than
# ranking the whole universe on a quantity whose differences stop meaning
# anything past a certain point.
DEFAULT_SIZE_FLOOR_BASE: float = 500_000_000.0

# The three weight models, selectable per optimiser run.
#
# Cost driven is the default deliberately. Ranking on trailing returns is
# performance chasing, and the evidence that past returns predict future
# ones is weak at best; cost is the one component that reliably persists.
SCORING_PRESETS: dict[str, dict] = {
    "cost_driven": {
        "label":      "Cost driven",
        "components": {"ter": 0.70, "size": 0.30, "returns": 0.00},
        "wtrr":       {"1m": 0.0, "3m": 0.0, "6m": 0.0,
                       "1y": 0.0, "3y": 0.0, "5y": 0.0},
    },
    "cost_and_returns": {
        "label":      "Cost and returns",
        "components": {"ter": 0.40, "size": 0.20, "returns": 0.40},
        "wtrr":       {"1m": 0.05, "3m": 0.10, "6m": 0.15,
                       "1y": 0.20, "3y": 0.25, "5y": 0.25},
    },
    "returns_driven": {
        "label":      "Returns driven",
        "components": {"ter": 0.15, "size": 0.15, "returns": 0.70},
        "wtrr":       {"1m": 0.10, "3m": 0.15, "6m": 0.15,
                       "1y": 0.20, "3y": 0.20, "5y": 0.20},
    },
}

DEFAULT_SCORING_PRESET: str = "cost_driven"
SCORE_COMPONENTS: tuple[str, ...] = ("ter", "size", "returns")


# ---------------------------------------------------------------------------
# Factsheets (v0.43.0)
# ---------------------------------------------------------------------------
# Issuer factsheets, uploaded by the user for funds Yahoo covers badly.
# Stored under cache/ because they are fetched artefacts rather than user
# intent — but see FACTSHEET_STALE_DAYS below for why they are never
# expired automatically.
FACTSHEETS_DIR = CACHE_DIR / "factsheets"

# What a factsheet may be. PDF is the norm; images are accepted because a
# photograph of a printed sheet is a perfectly good source document, and
# HTML because some issuers publish a page rather than a file.
FACTSHEET_EXTENSIONS: tuple[str, ...] = (
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".html", ".htm",
)

FACTSHEET_MIME: dict[str, str] = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".html": "text/html",
    ".htm":  "text/html",
}

# A factsheet older than this is flagged as stale — NOT deleted.
#
# Deliberately NOT in the per-portfolio cache config, and not a TTL.
# Those settings answer "how long may we reuse this before refetching",
# which presumes a source we can refetch from. There is no such source
# here: the document arrived by hand, so there is nothing to expire
# toward. What the number actually controls is when the UI says "this
# document is old, you may want a newer one" — a display threshold, and
# one that belongs to the fund rather than to any portfolio holding it.
#
# It lives in app settings alongside the other global preferences. See
# ``settings.factsheet.stale_days``; this constant is the default.
#
# Every other cache entry can be re-fetched, so expiry means "throw it
# away and ask again". A hand-uploaded document cannot be re-fetched:
# expiring it would destroy the user's own file to save nothing. So this
# is a staleness threshold, and the file stays until explicitly replaced.
#
# Measured against the factsheet's OWN as-of date where known, not the
# upload date. The Northern Trust sheet used in testing says "per
# Augustus 31, 2023" — nearly three years old when uploaded, and an
# upload-date rule would have called it fresh for six months.
FACTSHEET_STALE_DAYS: int = 183


# ---------------------------------------------------------------------------
# Field groups and per-field sources (v0.48.0)
# ---------------------------------------------------------------------------
# Every fund data item, grouped as it is presented, with the sources that
# can supply it.
#
# The override envelope now means "this field is PINNED to this source",
# where it previously meant "this value was asserted by this source". For
# stored data the two coincide — a value justETF asserted is a field
# pinned to justETF — so nothing needs migrating; what changes is that a
# pin now survives a reload and drives the refetch.
#
# A source that has nothing for a field answers "unknown". That is a real
# answer about that source, not a failure, and it is why the pin has to
# be recorded rather than inferred from whether a value arrived.

# Sources that can supply data, in the order a fresh fund tries them.
FIELD_SOURCES: tuple[str, ...] = ("yahoo", "openfigi", "justetf",
                                  "factsheet", "user")

SOURCE_LABELS: dict[str, str] = {
    "yahoo":     "Yahoo",
    "openfigi":  "OpenFIGI",
    "justetf":   "justETF",
    "factsheet": "Factsheet",
    "user":      "My own value",
}

# The four presentation groups. ``sources`` lists what may supply each
# field; ``calculated`` fields are derived by PorxPy and have no source
# to choose.
# How long each group's data may be reused before a reload refetches it.
#
# Per GROUP rather than per cache category, because the groups are how
# the data is now organised and how often it changes is a property of the
# group: a fund's structure barely moves, its costs shift a couple of
# times a year, its price moves daily. The old per-category TTLs answered
# a question about storage layout rather than about the data.
#
# 0 means "always refetch"; a large number means "effectively never".
DEFAULT_GROUP_TTL_DAYS: dict[str, int] = {
    "identity":    3650,   # an ISIN does not change; re-resolving risks harm
    "structure":   365,    # replication and domicile move rarely, but do move
    "operational": 90,     # TER and fund size are restated a few times a year
    "trading":     1,      # prices are stale the next morning
}

FIELD_GROUPS: tuple[dict, ...] = (
    {
        "key":   "identity",
        "label": "Identification",
        "note":  "How the fund is identified. Read-only: the ISIN is the "
                 "key every fund-level record hangs on — holdings, "
                 "breakdowns, overrides, factsheet — so re-sourcing it "
                 "would orphan all of them. Correcting an ISIN is a "
                 "separate, deliberate act.",
        "fields": (
            {"key": "isin",             "label": "ISIN",             "readonly": True},
            {"key": "ticker",           "label": "Ticker",           "readonly": True},
            {"key": "longName",         "label": "Name",             "readonly": True},
            {"key": "exchange",         "label": "Exchange",         "readonly": True},
            {"key": "currency",         "label": "Trading currency", "readonly": True},
        ),
    },
    {
        "key":   "structure",
        "label": "Structure",
        "note":  "What kind of fund this is. Rarely changes, so these are "
                 "the fields most worth pinning to a source you trust.",
        "fields": (
            {"key": "primary_asset_class", "label": "Primary asset class"},
            {"key": "structure",           "label": "Fund structure"},
            {"key": "replication",         "label": "Replication"},
            {"key": "style",               "label": "Management style"},
            {"key": "distribution",        "label": "Distribution"},
            {"key": "market_cap",          "label": "Market cap"},
            {"key": "style_box",           "label": "Equity style"},
            {"key": "focus_type",          "label": "Focus"},
            {"key": "focus_detail",        "label": "Focus detail"},
        ),
    },
    {
        "key":   "operational",
        "label": "Operational",
        "note":  "Cost, size and activity. Changes a few times a year.",
        "fields": (
            {"key": "trailingYieldPct", "label": "Trailing yield"},
            {"key": "forwardYieldPct",  "label": "Forward yield"},
            {"key": "expenseRatioPct",  "label": "Expense ratio (TER)"},
            {"key": "turnoverPct",      "label": "Turnover"},
            {"key": "totalNetAssets",   "label": "Total assets"},
            # What the FUND says it holds, not what we managed to load.
            # A factsheet stating 1,442 holdings is the fund's own count;
            # our holdings list may be a top-10.
            {"key": "holdingsCount",    "label": "Number of holdings",
             "sources": ("yahoo", "factsheet", "user")},
            {"key": "dataPoints",       "label": "Data points",  "calculated": True},
            {"key": "score", "label": "Score (all / peer)", "calculated": True},
        ),
    },
    {
        "key":   "trading",
        "label": "Trading",
        "note":  "Prices and volume. Defaults to Yahoo and should usually "
                 "stay there — a price pinned to a document never updates "
                 "again, and stale prices feed valuations and the "
                 "optimiser.",
        "fields": (
            {"key": "previousClose",    "label": "Last close"},
            {"key": "navPrice",         "label": "NAV"},
            {"key": "regularMarketVolume", "label": "Volume (last day)"},
            {"key": "fiftyTwoWeekHigh", "label": "52w high",
             "sources": ("yahoo", "user")},
            {"key": "fiftyTwoWeekLow",  "label": "52w low",
             "sources": ("yahoo", "user")},
            {"key": "ytdReturnPct",     "label": "YTD return",   "calculated": True},
            {"key": "totalReturnPct",   "label": "Total return", "calculated": True},
        ),
    },
)

# Which sources may supply each field. Defaults to everything except
# OpenFIGI, which only ever answers identity.
DEFAULT_FIELD_SOURCES: tuple[str, ...] = ("yahoo", "justetf", "factsheet", "user")


def field_spec(field: str) -> dict | None:
    """The group entry for a field, or None if it is not one we present."""
    for grp in FIELD_GROUPS:
        for f in grp["fields"]:
            if f["key"] == field:
                return {**f, "group": grp["key"], "group_label": grp["label"]}
    return None


def field_sources(field: str) -> tuple[str, ...]:
    """Sources that may supply a field. Empty when it is calculated."""
    spec = field_spec(field) or {}
    if spec.get("calculated") or spec.get("readonly"):
        return ()
    return tuple(spec.get("sources") or DEFAULT_FIELD_SOURCES)
