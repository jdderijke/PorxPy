# PorxPy Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

---

## [0.33.8] — 2026-07-26

### Fixed
- **Root cause of every wrong total-assets figure since 0.33.1:**
  `fund_operations.totalNetAssets` is not the fund's size. It is the
  Morningstar CATEGORY aggregate in millions, and Yahoo returns the same
  number in the fund column and the category-average column. VDIV.DE
  shows 84,138.91 in both — ~EUR 84bn across the category, against the
  fund's actual ~EUR 8.13bn.
  There was never a scale factor to find, which is why two attempts to
  rescue the number by scaling failed. It was never this fund's number.
- The size chain now reads `info.netAssets` then `info.totalAssets`
  only, both fund-level and in plain currency units. `fund_operations` is
  no longer consulted for size at all. It remains the primary source for
  the expense ratio, where the category column returns <NA> and the fund
  column is genuinely fund-specific (0.0038 -> 0.38%, matching the
  issuer).
- This also explains why the 0.33.1 fallbacks appeared not to help:
  `fund_operations` sat first in the chain and always won with a category
  figure, so the correct `info.netAssets` added in that release was never
  reached. Total-assets coverage should improve materially on reload.
- The plausibility floor stays as a guard against an impossible figure
  rather than as a way to infer units. It should no longer fire on
  normal listings.

### Changed
- Turnover keeps the documented fraction convention, with the ambiguity
  documented rather than guessed at: VDIV.DE reports 1.155, which is
  115.5% on that reading and 1.155% if the value is already a percent,
  and nothing in the response settles it. The category column is <NA>
  for turnover, so the fund column does at least appear fund-specific.
  Worth checking one fund's KIID against the tile.
- `tools/inspect_fund.py` gained a fund-vs-category comparison that flags
  any attribute where both columns carry the same value — the check that
  caught this.

---

## [0.33.7] — 2026-07-26

### Fixed
- **Removed the total-assets unit heuristic entirely.** 0.33.1 treated
  Yahoo's two sources as sharing a unit; 0.33.6 tried to infer the unit
  by cross-checking them. Both were wrong, and 0.33.6's cross-check could
  not work in principle: `.info` is assembled from the same quoteSummary
  modules, so the two "sources" agreeing proves they read the same field,
  not that the value is in raw units.
  The evidence that settled it: NL0011683594 (VanEck Developed Markets
  Dividend Leaders, ~EUR 8.59bn per the issuer) reports 84139 — off by a
  factor of ~102,000, which is not a million, not a thousand, not any
  scale factor. That number is not a scaled fund size, and no arithmetic
  recovers a correct answer from it.
- Total assets is now passed through unaltered, with a plausibility floor
  of 1e6 below which the field is left unset. A fund holding less than a
  million of anything is closed, pre-launch, or not what the number
  describes. A blank tile says "unknown"; "0.08M" says something
  confident and false, and this figure feeds fund-size screening where a
  wrong number is worse than an absent one.
- `profile.totalNetAssetsSrc` records which Yahoo key supplied the value.
  The three disagree in value and apparent unit, so "where did this come
  from" was the first question to ask about a suspect figure and was
  previously unanswerable.

### Added
- `tools/inspect_fund.py TICKER [TICKER...]` — dumps the raw Yahoo values
  behind the fee and size figures with no interpretation applied, plus
  each candidate under an as-units / x1e3 / x1e6 reading. Needs network;
  writes nothing. Note yfinance requests quoteSummary with
  `formatted=false`, so Yahoo's own "fmt" string — the one field that
  states the scale outright — never arrives, which is why inference was
  the only option available and why it failed.

---

## [0.33.6] — 2026-07-26

### Fixed
- **Total assets off by a factor of a million.** Yahoo's fundProfile
  `totalNetAssets` (what `fund_operations` surfaces) is quoted in
  MILLIONS; `netAssets` / `totalAssets` in the flat `.info` blob are
  plain currency amounts. 0.33.1 chained them as if they were the same
  unit, so IE00BM8QRZ79 reported 4474 and the tile rendered "0.00M" for
  a fund holding ~4.5 billion. Rather than trusting the convention
  blindly, the two are now cross-checked when both exist: agreement
  after scaling by 1e6 confirms millions, agreement unscaled means Yahoo
  changed the convention, and outright disagreement prefers the flat
  value, which needs no unit assumption to be right. Millions is assumed
  only when fund_operations is the sole source.
- **Phantom overrides created by simply pressing Save.** The Figures
  inputs are filled with `toFixed(dp)`, but the save path compared that
  rounded string against the unrounded seed. A Yahoo TER of 0.207 shows
  as "0.21" and a size of 4474.38 shows as "4474", so both compared as
  changed, and opening the dialog and saving without typing anything
  stored an override of the rounded number — attributed to the user.
  Comparison now happens at display precision, making an untouched field
  a guaranteed no-op.
- **"Revert to Yahoo" left the row reading "overridden (user)".** The
  handler deleted the override but never redrew the caption, so the one
  moment the label was certainly false was the moment right after a
  successful revert. It now restores the derived value, redraws the row,
  and refreshes the tiles.
- Reverting had nothing to revert *to*: the server writes overrides into
  the same payload slot as the derived value, so the original was gone by
  the time the client saw it. `apply_overrides` now also returns what
  each override displaced, exposed as `override_seed` on the fund
  payload. The caption uses it to show "overridden (user) — Yahoo says
  4474", and revert uses it to restore without a refetch.

---

## [0.33.5] — 2026-07-26

### Added
- Total Return tile shows the date it is measured from ("since 2 Mar
  2015"). The figure is computed from the first bar of the cached price
  series, not from fund inception, and that window differs per fund
  depending on when it was first loaded and how much history Yahoo
  returned — so "+38%" was uninterpretable without knowing whether it
  covered ten years or eight months.
- `fmtDay()` helper, replacing the inline date formatting the Last Close
  tile used. Falls back to the raw string rather than rendering
  "Invalid Date" if Yahoo returns something unparseable.

---

## [0.33.4] — 2026-07-26

### Fixed
- Total net assets tile was wrong in three ways at once. It divided by
  1e9 and labelled "B" for anything above a million, so a EUR 5m fund
  read "0.01B"; the 1e3–1e6 branch divided by 1e6 but still labelled the
  result "B"; and the currency was hardcoded to `$` regardless of the
  fund's own currency, which was already in scope.
- New `fmtFundSize(v, cur)`: millions is the floor, since that is the
  unit factsheets and screeners quote fund size in, scaling to billions
  at 1,000M and trillions at 1,000B. Thresholds are tested on the
  unscaled value, so 999.90M and 1.00B meet with no gap or overlap.
  Uses the fund's currency code.

---

## [0.33.3] — 2026-07-26

### Fixed
- A reported TER or turnover of 0 is now treated as absent, not as a
  measurement. Yahoo returns 0.0 for "no fee data" on a large share of
  European UCITS ETFs rather than omitting the key — IE00BDVPNG13 among
  many others reports a 0.00% TER and 0.0% turnover that no such fund
  has. Genuinely zero-fee funds exist (Fidelity's ZERO range), so this
  costs something; it is still right, because a spurious 0.00% is worse
  than a blank once the optimiser is cost-aware — it reads as "free" and
  outranks every real competitor, whereas a blank only says unknown. A
  holder of a genuinely free fund can assert 0.0 through the override
  store.
- The fallback chains walk past a zero rather than stopping at it, so a
  source reporting 0.0 can no longer mask a later source that has the
  real number.
- `tools/data_coverage.py` counts zeros separately from real values on
  fee, size and yield fields, reported as "(n zero)". A naive
  present/absent count read those funds as fully populated and hid the
  gap being measured.

---

## [0.33.2] — 2026-07-26

### Fixed
- `tools/data_coverage.py` reported ISIN missing on every fund. It read
  `profile["isin"]`, but the ISIN lives in the listing file's top-level
  `identity` block written by `listing_identity_put` at resolution time —
  `profile["isin"]` is only populated on the subset of fetches where
  Yahoo exposes it inline. The same wrong lookup passed `""` to
  `overrides_for`, so the "overridden" counts were silently zero for
  every field as well. Identity is now merged over the profile before
  counting, and a fund with no identity block (pre-0.11.2 cache entry)
  is reported as genuinely missing rather than crashing.

---

## [0.33.1] — 2026-07-26

### Fixed
- TER, turnover and total net assets were read only from Yahoo's
  quoteSummary `fundProfile` module, which is empty for a large share of
  European UCITS ETFs — the exact funds where the fields were blank. Each
  figure now walks a fallback chain into the flat `.info` blob, which
  frequently carries the same numbers under different keys for those same
  listings:
  - TER: `fund_operations` → `info.netExpenseRatio` →
    `info.annualReportExpenseRatio`
  - Turnover: `fund_operations` → `info.annualHoldingsTurnover`
  - Total assets: `fund_operations` → `info.netAssets` → `info.totalAssets`
- Units are declared per source rather than sniffed by magnitude.
  `fund_operations` and `info.annualReportExpenseRatio` return a fraction
  (0.002); `info.netExpenseRatio` returns percent (0.20). A 0.20% TER and
  a 0.20 fraction are the same number, so magnitude sniffing would have
  silently mangled cheap trackers.
- NaN, infinity and non-numeric sentinels rejected rather than coerced;
  a reported total-assets of 0 is treated as absent, since Yahoo uses it
  for "unknown" instead of omitting the key.

### Added
- `tools/data_coverage.py` — offline report of which profile fields are
  populated across the saved funds, grouped by category, with the worst
  gaps and example tickers. Reads the cache only; no Yahoo calls.
  Existing cache entries predate this change, so a fund has to be
  reloaded before the new fallbacks can help it.

---

## [0.33.0] — 2026-07-26

### Changed
- **Overrides are now one per-field table instead of four bespoke ones.**
  `overrides.json` stays ISIN-keyed, but each entry is a flat map of field
  name → `{value, source, ts, note}`. Replaces the `asset_class`,
  `breakdown_source`, `include_in_optimizer` and `fund_structure`
  sub-structures, each of which had its own storage shape, its own
  validation and its own answer to "what does absent mean".
- **`config.OVERRIDABLE_FIELDS`** — one registry entry per overridable
  attribute carrying type, vocabulary or bounds, unit, label and an
  optional dotted `target` path into the fund payload. Adding an
  overridable field is now a registry entry, not another application
  site. `focus_detail` uses a `vocab_fn` hook because its vocabulary
  depends on the `focus_type` set alongside it.
- Twelve storage helpers collapse to five: `override_get` / `override_put`
  / `override_delete` / `overrides_for` / `apply_overrides`.
- `_FS_NEUTRAL` deleted. The store is sparse, so presence IS the
  assertion and absence IS "no opinion" — the v0.27 class of bug, where a
  stored `"unknown"` could not be told apart from an untouched field, is
  no longer expressible.
- Withdrawing an override is a delete, so `overrides.json` records only
  deliberate exceptions.

### Added
- **Editable TER and total net assets.** Both are registry entries with a
  `target` into `profile`, applied to the assembled response rather than
  to the cached Yahoo blob — so an override is a view over Yahoo's data,
  never a mutation of it, and reverting needs no refetch.
- Edit-fund dialog gains a "Figures" section: each field shows the
  effective value, says whether it came from Yahoo or an override, and
  carries a ↺ revert. A blank box, or a value equal to Yahoo's, withdraws
  the assertion rather than pinning a snapshot.
- `PUT`/`DELETE /api/funds/<ticker>/override/<field>` for registry fields
  that need no bespoke response, and `GET /api/funds/<ticker>/overrides`
  which returns the stored assertions plus the registry, so the dialog
  builds its inputs from the server's vocabulary rather than a copy.
- `override_sources` on the fund payload — who asserted each applied
  field.
- One-shot migration on read: pre-0.33 entries are rewritten on first
  load. Legacy values equal to their registry `neutral` are DROPPED, not
  carried across — in the old dense blocks a neutral meant "no opinion",
  and migrating it as an assertion would pin the field for ever.

### Fixed
- Migration detection was by key name, but `asset_class` and
  `include_in_optimizer` are both legacy top-level keys AND new registry
  field names — so the check stayed true after a successful migration and
  the file was rewritten on every load. Detection is now by shape: an
  envelope carries `value`, a legacy value is a bare string, bool or
  nested dict.

### Note
- Endpoints whose response is genuinely richer than "the value is stored"
  kept their own routes — flipping a breakdown card's source hands back
  the rebuilt cards, saving Structure hands back the merged block. Only
  the *storage* collapsed. Contrary to the earlier plan, collapsing the
  HTTP surface too would have cost the UI real functionality for no
  architectural gain.

---

## [0.32.1] — 2026-07-26

### Fixed
- Every list jumped back to the top after an in-place edit. Ticking the
  `incl` box on a row 80 entries down applied the edit and then left the
  user looking at row 1. Cause: the lists re-render by replacing
  innerHTML, which destroys the scrolled element and recreates it at
  scroll position zero.
  Added `preserveScroll()` — snapshots the window plus every
  `.dtwrap` / `.hfullwrap` / `.pleg` on the page, runs the render (sync or
  async), and restores. Applied to the pre-loaded list, the portfolio
  reload, and the portfolio-holdings rollup.
  Switching to a *different* portfolio still lands at the top, which is
  correct — that is new content, not the list you were reading.
  The sort/filter paths that only swap a `<tbody>` never had the problem
  and are untouched.

---

## [0.32.0] — 2026-07-26

### Added
- **`incl` column** in the pre-loaded fund list: per-fund opt-in to the
  optimiser, with sort and an All/Incl/Excl filter. Stored per fund (by
  ISIN), so both listings of a dual-listed fund answer the same way and
  toggling one updates the other in place. Disabled for rows with no ISIN
  on record — there is nothing to key the flag on.
- Frozen-holdings note on the optimiser panel, naming the funds left
  untouched and their share of the portfolio.
- `include_in_optimizer` on the `/api/cache/list` payload.

### Changed
- `regions.csv` column `matches` renamed to `style_match` (Version=2), to
  match `sectors.csv`. Both now name the same concept the same way.
- `include_in_optimizer_get` hoisted to app.py's module-level import.

---

## [0.31.0] — 2026-07-26

### Added
- **`style_match` column in `sectors.csv`** (Version=19) — a second,
  precision-oriented vocabulary for matching phrases in *fund names*,
  separate from `matches`, which exists to coerce whatever spelling turns
  up in an uploaded holdings row. `SECTOR_STYLE_ALIASES` alongside
  `SECTOR_ALIASES`.

### Fixed
- Name-derived sector focus used the holdings-normalisation aliases and
  got two things wrong on real fund names: `other` is a legitimate
  holdings alias for real estate, so "MSCI World Ex Other Sectors"
  resolved to a property fund; and `communication` is a legitimate alias
  for technology, so "MSCI World Communication Services" matched two
  sectors at once and resolved to neither. The two vocabularies want
  opposite tolerances and cannot be one column.
- `achieved` / `deviation` in the optimiser result leaked numpy scalars
  into `jsonify`, which cannot serialise them. Pre-existing.
- `_norm_region_token` renamed `_norm_phrase` and moved above
  `_load_sectors`, which now needs it at import time — as written it
  would have been a `NameError` on startup.

---

## [0.30.0] — 2026-07-26

### Added
- **Frozen positions in the optimiser.** A held fund with `include` False
  is excluded from the optimiser's decisions, not from the portfolio: its
  exposure still counts toward every target and toward the denominator,
  but it is never bought, sold or selected, and its value is not part of
  the budget. The problem reduces to the free sub-portfolio,
  `A_free @ w = (t - f) / (1 - phi)`, with tolerances divided by the same
  factor so the stopping test keeps its whole-portfolio meaning. A
  negative component of the reduced target — frozen holdings already
  overshooting a bucket — correctly reads as "put as little here as
  possible".
- `frozen` block on the optimiser result (`share`, `base`, `tickers`);
  frozen holdings listed in `positions` with `frozen: true` so the table
  still reconciles to 100%.
- `reason` names the exclusions when a target is missed and frozen
  holdings are in play, so it doesn't read as an optimiser failure.

---

## [0.29.0] — 2026-07-26

### Added
- **Targets tab covers all six facets**, grouped into Exposure
  (asset_class, sector, country, currency) and Style (market_cap,
  style_box). The groups differ in kind, not just in name: the first four
  are look-through distributions with a coverage caveat, the last two are
  fund-level classifications with an unclassified residual.
- `/api/targets/meta/<facet>` — targetable values per metadata facet.
- `_coerce_targets` validates meta-facet keys against
  `META_FACET_TARGETABLE`; a target on `unknown` or `n/a` is dropped
  rather than stored and then never satisfiable.

---

## [0.28.0] — 2026-07-26

### Added
- **Metadata facets in the portfolio rollup and X-ray.** `market_cap` and
  `style_box` are scalars, so each fund is reshaped into a one-hot
  distribution and fed through the existing rollup. Two new X-ray cards,
  rendered identically in both breakdown modes — there is no look-through
  variant of a fund-level classification.
- Cash positions carry `market_cap: n/a` and `style_box: value`. `n/a`
  rather than `unknown`: there is no market cap to discover for a bank
  balance, which is a different claim from not having discovered one.
- `mixed` and `n/a` added to `MARKET_CAPS`; `META_FACET_TARGETABLE`.
- **`resources/regions.csv`** — region + super-region vocabulary
  (europe, asia, pacific, emerging). Region-keyed, unlike the
  country-keyed `country_codes.csv`. Used only to seed a fund's focus
  from its name; the country/region breakdown facet does not consult it.
  There is deliberately no `global`/`world` entry — a global fund is the
  absence of a regional focus, which is what `focus_type: none` says.
- **Name-derived fund metadata** (`derive_structure_from_name`): market
  cap, style box, and focus type/detail inferred from the fund name.
  Gap-filling only — never overrides Yahoo, let alone a user override.
- Provenance is now four-valued (`user` / `yahoo` / `name` / `default`)
  so an inferred value isn't captioned as a measured one.

### Changed
- `_seed_fund_structure` returns `(seed, origins)` and takes the detected
  asset class. Cash ⇒ `market_cap: n/a`; fixed income or cash ⇒
  `style_box: value`.
- `focus_type` / `focus_detail` are no longer skipped by the
  "store only genuine divergence" logic — they are seeded now, so they
  can agree with the seed like any other field.

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
