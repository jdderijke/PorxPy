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
COUNTRY_CODES_FP      = RESOURCES_DIR / "country_codes.csv"
COUNTRY_CURRENCY_FP   = RESOURCES_DIR / "country_currency.csv"
# Holdings classification — pairs of asset_class + sub_class with
# descriptions and a `matches` column of pipe-separated alternative
# spellings. Drives the edit-holding modal's two paired dropdowns and
# normalises uploaded values like "Common stock" → "shares".
HOLDINGS_CLASS_FP     = RESOURCES_DIR / "Holdings_class_definitions.csv"
# Fund-level asset-class vocabulary (the wider ASSET_CLASSES set:
# equity / fixed_income / cash / mixed / commodity / other) plus a
# `matches` column of alternative spellings. This is the authority the
# portfolio Targets editor reads from, and the source of the alias maps
# that normalise issuer/holdings spellings ("bond"/"bonds"/"bondPosition"
# → "fixed_income") up to the fund-level vocabulary. Distinct from
# Holdings_class_definitions.csv, which is the finer per-holding
# asset_class+sub_class taxonomy (and uses "bond", not "fixed_income").
FUND_CLASS_FP         = RESOURCES_DIR / "Fund_class_definitions.csv"
# Morningstar 11-sector taxonomy with super-sector grouping and a
# `matches` column. Drives the Sector dropdown and the upload-side
# coercion of issuer spellings ("Information Technology" → "technology").
SECTORS_FP            = RESOURCES_DIR / "sectors.csv"
# ISO 4217 currency master list with display names and a `matches`
# column for alternative spellings ("yen" → "JPY"). Drives the Currency
# dropdown — separate from country_currency.csv which is a per-country
# primary-currency mapping; this file is the full per-code list.
CURRENCIES_FP         = RESOURCES_DIR / "currencies.csv"

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
    # User-uploaded per-facet breakdown item lists. Sourced ONLY from
    # user CSV uploads (the third value of BREAKDOWN_SOURCES); never
    # fetched from anyone. Shape on disk:
    #   { "uploaded_breakdowns": {
    #       "fetched_at": "...",
    #       "value": { "asset_class": [{"key","weight"}, ...],
    #                  "sector":      [...], "country": [...],
    #                  "currency":    [...] } } }
    # Only facets present (non-empty) are uploaded; absent / empty
    # facets cannot be flipped to source "upload" on the fund page.
    "uploaded_breakdowns",
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
    # uploaded_breakdowns = user-uploaded per-facet breakdown item lists
    # (from the per-card "Upload" source on the fund page). Entirely
    # user-supplied, never fetched — so there is no meaningful TTL.
    # ``manual_refresh_only`` makes a present entry always fresh; only
    # an explicit re-upload (which writes a new entry anyway) replaces
    # it. ``ttl_days`` is retained purely for schema uniformity.
    "uploaded_breakdowns": {"enabled": True, "ttl_days": 0, "manual_refresh_only": True},
    "price_history": {"enabled": True, "ttl_days": 1},
    # upload_prefs = the user's last upload-dialog choices for a fund
    # (source URL/path, column mapping, header row, decimal, weight
    # unit, defaults, enrich fields). Prefs aren't fetched from anyone
    # — they only change when the user commits a new upload — so the
    # TTL is effectively "forever". 10 years is the practical upper
    # bound; cache_get treats anything above this as a miss.
    "upload_prefs":  {"enabled": True, "ttl_days": 3650},
}

# Canonical fund-level asset-class keys (the schema authority — what
# values are valid). The spellings/aliases and human descriptions for
# these keys live in Fund_class_definitions.csv (FUND_CLASS_FP), loaded
# by resources.py. Kept as a hardcoded list here because config must not
# import resources (resources imports config), and these keys are a
# stable schema contract used for validation across the app.
ASSET_CLASSES = ["equity", "fixed_income", "cash", "mixed", "commodity", "other"]


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
ENRICHABLE_FIELDS: tuple[str, ...] = (
    "name", "country", "currency", "asset_class", "sub_class", "sector",
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

# Valid values for a per-card breakdown-source override.
#   "fund"     — issuer-published Fund/ETF-level data (the default; no
#                override entry is stored for this value).
#   "holdings" — populate this card from the fund's physical holdings
#                roll-up instead.
#   "upload"   — populate this card from a user-uploaded CSV. Per-facet
#                item lists live in the ``uploaded_breakdowns`` fund-
#                level cache category (see below); only facets the CSV
#                actually covered can be flipped to this source.
BREAKDOWN_SOURCES: tuple[str, ...] = ("fund", "holdings", "upload")


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

# Default Structure block for a fund with no stored override. The
# values here are placeholders; load_fund_data overlays Yahoo-seeded
# defaults (see _seed_fund_structure) before applying any stored
# override on top.
DEFAULT_FUND_STRUCTURE: dict[str, str] = {
    "structure":   "unknown",
    "replication": "unknown",
    "style":       "unknown",
}
