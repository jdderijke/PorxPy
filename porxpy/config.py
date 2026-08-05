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

# v0.28.0: region + super-region vocabulary. Region-keyed, unlike the
# country-keyed country_codes.csv that MSTAR_TO_REGION is built from —
# a super_regions column there would repeat the same value across every
# country row of a region, with no single place to read the vocabulary.
# Used ONLY to seed focus_type / focus_detail from a fund name; the
# country/region breakdown facet does not consult it.
REGIONS_FP            = RESOURCES_DIR / "regions.csv"
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
BREAKDOWN_SOURCES: tuple[str, ...] = ("yahoo", "factsheet", "holdings", "upload")

# Which sources supply their own item lists rather than deriving them.
# Both are stored per facet per source in the uploaded_breakdowns cache.
SUPPLIED_BREAKDOWN_SOURCES: tuple[str, ...] = ("upload", "factsheet")


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
FOCUS_TYPES: tuple[str, ...] = ("none", "region", "sector", "thematic")

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
#   META_FACETS      — derived from fund metadata; one-hot per fund. There
#       is no "issuer market-cap breakdown" to choose between, so these
#       get no card and no source selector.
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
#             that depend on another field's value (focus_detail). Looked
#             up lazily so config stays free of resource-CSV imports.
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
        "type": "enum", "vocab": tuple(ASSET_CLASSES), "neutral": None,
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
    OVERRIDABLE_FIELDS[f"breakdown_source.{_facet}"] = {
        "type": "enum", "vocab": BREAKDOWN_SOURCES, "neutral": "yahoo",
        "label": f"{_facet} card source",
    }
del _facet


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
# only technology-sector fund, the only European bond fund. A percentile
# within a group of one is 100 by construction, so every uniquely
# positioned fund would score perfectly and be preferentially swapped in:
# precisely backwards. Below this size the peer score is None and the
# optimiser leaves the fund alone on score grounds. It stays fully
# eligible on targets — which is why it is held anyway.
MIN_PEER_GROUP: int = 3

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
