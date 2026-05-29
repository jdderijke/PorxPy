# PorxPy Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

---

## [0.20.5] — 2026-05-29

### Fixed
- Ticker, ISIN, and CUSIP not written back to holding row after resolution.
  `get_symbol_info_cached` now injects `_resolved_ticker` into the info dict
  at all 11 successful return sites so callers always know which Yahoo ticker
  was used, regardless of the resolution path taken.
- `enrich_existing_holdings` was silently skipping rows that had no ticker
  but did have an ISIN or CUSIP. Now passes them through to the id-search
  fallback and applies the same identifier backfill logic.

---

## [0.20.4] — 2026-05-29

### Fixed
- Holding edit dialog (double-click on row) broken after the Ticker/ISIN grid
  was widened to 3 columns. The `str_replace` left a dangling `</div>` and
  dropped the Ticker field from the modal HTML entirely, causing
  `getElementById('heTicker')` to fail and crash `openHoldingEditor`.

---

## [0.20.3] — 2026-05-29

### Added
- CUSIP column in the fund holdings table (between ISIN and Sector).
- CUSIP field in the holding edit dialog (Ticker / ISIN / CUSIP, 3-column grid).
- CUSIP included in the search haystack on both the fund and portfolio tabs.
- CUSIP automatically derived from resolved US ISINs (ISIN chars 2–10) and
  written back to the holding row after enrichment.

### Fixed
- Enrichment was skipped for rows with no ticker but a valid CUSIP or ISIN.
  Pass 3 now only skips rows that have none of the three identifiers.
- `enrich_existing_holdings` had the same skip-gate problem; fixed in parallel.

---

## [0.20.2] — 2026-05-29

### Fixed
- CUSIP column mapping not remembered on re-upload: `cusip` was missing from
  the `seedMapping` block that populates `uploadState.mapping` from saved prefs.
- Stale enrichment results on re-upload: previously-failed tickers left a
  negative alias (`None`) in the alias cache that short-circuited every
  subsequent probe. Added `alias_delete()` to `utils.py` and a
  `retry_negative=True` parameter to `get_symbol_info_cached`; upload Pass 3
  always passes `retry_negative=True` so re-uploads get a clean probe.

---

## [0.20.1] — 2026-05-29

### Fixed
- Upload dialog still showed "Yahoo enrichment requires the Ticker column to
  be mapped" even after mapping a CUSIP or ISIN. There were three separate
  locations with the old message; only one was updated in 0.20.0:
  - Enrich button `disabled` / tooltip logic (`buildMappingRows`).
  - Commit-time guard before the enrichment loop.
  - Descriptive hint text in the mapping panel.

---

## [0.20.0] — 2026-05-29

### Added
- **CUSIP support** throughout the holdings pipeline:
  - New `cusip` field in `HOLDINGS_ROW_FIELDS` (utils.py) — preserved through
    `coerce_holdings_row` like ISIN.
  - CUSIP column mapping in the upload dialog (`MAP_FIELDS`, auto-detects
    `cusip` header); CUSIP validated as 8–9 alphanumeric chars and stored on
    the holding row.
  - `search_id_variant(identifier)` in `resolver.py` — passes a CUSIP or ISIN
    directly to `yf.Search` and returns the first result's ticker. No prefix
    matching needed; identifiers are unambiguous.
  - `get_symbol_info_cached` gains `cusip` kwarg. When no ticker is present,
    tries CUSIP search → ISIN search → name search in order. When a ticker
    exists but fails, tries CUSIP/ISIN id-search as Fallback 2 (before name
    search, which becomes Fallback 3).
  - Upload Pass 3 passes `cusip=row.get("cusip")` to `get_symbol_info_cached`.
  - `enrich_existing_holdings` likewise passes `cusip`.
- Enrich buttons in the upload dialog now enabled when a CUSIP or ISIN column
  is mapped, in addition to Ticker.

---

## [0.19.0] — 2026-05-29

### Added
- **Dot-separated Bloomberg country codes** normalised in
  `clean_holding_ticker_input`: `AIR.FP` → `AIR FP`, feeding into the
  existing Bloomberg-spaced variant path. Only 2-letter suffixes that match
  a known Bloomberg country code are converted; genuine Yahoo suffixes
  (`.L`, `.PA`, `.AS`) are left untouched.
- **`_ISIN_PREFIX_TO_YF`** map in `resolver.py`: ISO 3166-1 alpha-2 country
  prefix → Yahoo exchange suffix (30 countries/regions).
- **`isin_country_variant(cleaned, isin)`**: strips any existing suffix from
  the ticker, applies the ISIN-derived Yahoo suffix, and returns the candidate.
  Example: ticker `"AIR"` + ISIN `"FR0000120271"` → `"AIR.PA"`.
- **`search_name_variant(name, ticker_prefix)`**: calls `yf.Search` with the
  security name and matches returned tickers against the first 4 characters of
  the raw ticker. Used as last-resort fallback.
- **Extended fallback chain** in `get_symbol_info_cached` (after all variant
  candidates exhausted):
  1. ISIN country prefix (`isin_country_variant`)
  2. Identifier search — CUSIP then ISIN (`search_id_variant`)  *(added 0.20.0)*
  3. Name search (`search_name_variant`)
- `get_symbol_info_cached` gains `isin` and `name` kwargs.
- Upload Pass 3 passes `isin` and `name` from the row to enrichment.
- `enrich_existing_holdings` likewise passes `isin` and `name`.

---

## [0.18.0] — 2026-05-28

### Added
- **Targets tab** (`targets.py`): per-facet target-vs-actual deviation report.
  Targets are sparse; facets with no targets show actuals only. Country facet
  is aggregated from `mstar_country` to `mstar_region` before comparison.
- New resource CSVs: `Fund_class_definitions.csv`, `Holdings_class_definitions.csv`,
  `sectors.csv`, `currencies.csv`.
- `compute_target_deviations()` pure function; consumes the portfolio rollup
  and per-facet target dicts from `portfolios.json`.

---

## [0.15.0] — 2026-05-25

### Added
- Cash row fixes: Shares field and broken delete button repaired.
- Upload cancel support: token-keyed cancel registry; Cancel button fires
  `/api/upload/cancel` and stops the Yahoo enrichment loop between rows.
- Unmatched-facet summary on upload commit response (count + per-facet
  samples); surfaces bogus sector/country/currency spellings even before
  the fund is added to a portfolio.

---

*Older versions not documented here.*
