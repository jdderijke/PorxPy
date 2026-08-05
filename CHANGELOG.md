# PorxPy Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

---

## [0.51.1] — 2026-08-04

### Fixed
- **`renderCountryCardChart` was deleted in 0.50.0**, so opening a
  portfolio's fund list threw `renderCountryCardChart is not defined`.
  Removing `renderCountryBreakdownLT` cut from that function's opening
  line to the next section marker, and this shared helper sat between the
  two. It is shared precisely because the Country/Region toggle re-renders
  the chart without re-reading the view, so it belonged to the surviving
  renderer as much as to the removed one.
  Restored, updated for the two residuals.

  Cutting by section marker rather than by function boundary is the
  anchor-overreach failure the working agreement warns about, in a form
  the occurrence count does not catch: the anchors were unique, the
  region between them was not. Verified this release by diffing the whole
  function inventory against 0.49.2 — `renderCountryCardChart` was the
  only unintended removal.

### Changed
- Two empty-state messages still told the user to "use Holdings-level mode
  here". That selector was removed in 0.50.0.

---

## [0.51.0] — 2026-08-04

### Added
- **The four breakdown facets now have two residuals instead of one.**
  0.50.4 renamed `undefined` to "Unknown", which flattened a distinction
  the meta facets have drawn since 0.28.0: `unknown` is a gap somebody
  could close, `n/a` means the question does not apply. The reader wants
  to know whether better data would fix it, and one bucket could not say.

  - `unknown` — there is a value; this source does not have it. A
    factsheet that omits the currency split, the 79% of a fund a top-10
    holdings list says nothing about.
  - `n/a` — there is no value to have. A cash balance has no sector.

  `config.FACET_NOT_APPLICABLE` decides which a blank value becomes, from
  the row's own asset class. It is deliberately narrow — PorxPy is
  asserting something no source said — and currently covers cash on
  sector and country only. Commodities are a defensible addition to both;
  left out until asked for, and a one-line change when it is. Bond
  positions are not there: an issuer has a sector and a country, and a
  blank one is missing data.

### Changed
- **Coverage counts `n/a` as answered.** Only `unknown` counts against it.
  Scoring an inapplicable question as a shortfall left a cash-heavy
  portfolio permanently short of 100% on sector and country with nothing
  anyone could do about it.
- A partial roll-up's shortfall is `unknown`, never `n/a` — the rows we
  are missing are holdings like any other.
- A source that exists but says nothing about a facet answers `unknown`.
- Two greys rather than two hues, `n/a` the lighter: both are the absence
  of a real bucket and neither should read as a category competing with
  the others, but `n/a` is the one needing no action.
- `regroupToRegion` passes both residuals through unchanged. A cash sleeve
  has no region for the same reason it has no country, and rewriting its
  `n/a` to `unknown` would have invented a gap. A real country missing
  from `REGION_MAP` is a gap, so it becomes `unknown`.
- One `isResidual` / `residualLabel` / `residualColour` triple at the top
  level, replacing the per-site key comparisons that had already drifted.

### Note on migration
None needed: the residual key is computed at request time by
`rollup_holdings` and `build_fund_breakdowns` and never persisted.
Holdings rows store their own raw facet values, and uploads store what was
uploaded.

---

## [0.50.4] — 2026-08-04

### Changed
- **The residual bucket reads "Unknown" on screen, not "undefined".** The
  stored key is unchanged — `undefined` is the data contract every rollup,
  target comparison and coverage sum is written against — but "undefined"
  is a programming word, and what it tells a reader is that the source was
  asked and did not know. The two metadata facets already said "Unknown";
  now all six agree.
  One `UNDEFINED_LABEL` constant, so the six facets cannot drift apart
  again. They already had: `fmtAssetClass` had no case for the residual
  and fell through to the raw key, so the asset-class card printed a
  lowercase "undefined" while every other card printed "Undefined", and
  `fmtSector` arrived at "Undefined" by title-casing rather than by
  decision.
- The same word in the prose above the cards, in the extraction report's
  partial-coverage note, and in the facet-mapping dropdown.

---

## [0.50.3] — 2026-08-04

### Fixed
- **`fmtDay` was declared inside the metrics-tile renderer**, so it was out
  of scope for `_bkSourceCaption`, which also calls it. Selecting the
  factsheet source threw `ReferenceError: fmtDay is not defined`, the
  exception unwound the whole four-card render, and the selector looked
  like it was refusing to switch. It is now declared at the top level.

  This is the original fault behind "switching between Yahoo and factsheet
  does not work", present since the factsheet caption gained a date. Every
  symptom follows from it, including the ones that looked contradictory:
  the cards render in the order asset_class, sector, country, currency, so
  switching *sector* to Yahoo redrew sector's chart and then threw on
  *country*, which was still on factsheet — the graph updated and the
  switch still "failed". Switching country to Yahoo threw on sector first
  and nothing visibly happened at all. The store was correct throughout;
  only the render died.

### Changed
- The result of a source switch is shown **in that card's own control
  row**, not in the app-wide status bar. The bar sits at the top of the
  Explore tab and the breakdown cards are several screens below it, so
  the message was invisible at the moment it mattered.

### Removed
- Nothing this release.

---

## [0.50.2] — 2026-08-04

### Changed
- **A source switch now says what it did.** The server answers with the
  source actually in force, which is not always the one asked for — a pin
  to a source the fund does not have falls back to Yahoo. That was
  reported by silence, which is indistinguishable from a button that does
  not work, and is exactly how it read. The status line now states either
  "sector now reads from the factsheet" or "currency stayed on Yahoo —
  this fund has no factsheet to read it from". A failed request reports
  its own error rather than a fixed sentence.

### Removed
- Unreachable code after the `return` in `_migrate_breakdown_source_values`
  — the remains of an interrupted edit, never executed.

---

## [0.50.1] — 2026-08-04

### Fixed
- **Switching a card's source appeared to do nothing on most facets**, even
  with the factsheet option enabled. The choice was saved correctly every
  time; the redraw was what failed. After the PUT the frontend re-requested
  `/api/fund` to rebuild the cards — and a loaded fund is identified by
  both a ticker and an ISIN, which `/api/fund` reads as mode 2 and answers
  by re-validating the ticker against Yahoo live. Whenever Yahoo declined,
  the redraw was skipped in silence, so the buttons and the chart stayed
  where they were. Which facets seemed broken depended on Yahoo's mood,
  not on the facet.
  The source endpoint now returns the rebuilt `fund_breakdowns` with its
  reply, as the upload endpoints already did, and the card redraws from
  that. No second request, no re-resolution, nothing to rate-limit.
  `refreshFundBreakdownsOnly` had no other caller and is gone.

### Changed
- The text above the fund's breakdown cards still said a source with no
  data for a card is struck through. Since 0.50.0 that is not what
  striking through means: a source the fund does not have is struck
  through, and a source it has but which says nothing about a card shows
  that card as 100% undefined.

---

## [0.50.0] — 2026-08-04

### Fixed
- **A breakdown card could not be pinned to a source that had no data for
  that facet.** `build_fund_breakdowns` silently substituted Yahoo, so
  the button sprang back and the selector looked broken. The pin is now
  always honoured; a source with nothing to say answers with a single
  100% "undefined" slice, which is a statement — "the factsheet does not
  break this down" — where a blank card was a shrug.
- **Removing an uploaded CSV destroyed the factsheet's breakdown for that
  facet.** `uploaded_breakdowns_delete` wrote a bare list where the
  source-keyed `{facet: {source: items}}` belongs, and the next read
  re-keyed that list to `upload` as though it were a pre-0.44 flat entry.
  The factsheet's items were gone from disk, so "Issuer (factsheet)" went
  permanently grey. The clear is now per source.
- The upload-commit endpoint rebuilt the cards from the commit's own
  return value — the flat `{facet: items}` for the source just written —
  so a just-committed CSV did not appear. It reads the store back.

### Changed
- **A source selector is enabled when the fund HAS that source**, not when
  that source happens to carry the facet. A factsheet that never mentions
  currencies still answers the currency card. Only a source the fund
  genuinely lacks — no factsheet, no extraction, no holdings, no CSV
  covering this facet — is struck through.
- Card captions describe a **selection, not an override**. "This
  fund-level information is overruled by the roll-up from its holdings"
  is gone, along with the second caption line it added on Holdings cards;
  every card now reads "Fund-level data selected from …" once.
- **Coverage counts identified exposure**, so the "undefined" residual is
  excluded everywhere. A card can read 0% while showing a full circle.
  Partial issuer data still counts in full: fractions totalling 0.85 with
  no residual bucket are an incomplete answer, not an absent one.
- **Every field with a value states its source** — Yahoo included. The old
  rule printed nothing for unpinned fields to avoid a column of the same
  word, which made Yahoo the one source you could not see and left a
  blank meaning both "Yahoo" and "nothing to say". A field with no value
  still says nothing, including one pinned to a source that came back
  empty: the pin is real but the answer is not.
- Structure and Operational stretch to the full height of Identification
  and Trading stacked, and the group block has a margin below it. The
  three columns previously ended at three different heights and the price
  history card sat flush against the bottom of Trading.
- The Upload option's tooltip pointed at "the upload button on the
  right". It sits immediately after the source selector, and the row
  wraps, so it names the button instead.

### Added
- The resolver records **where each identity field came from** — mode 1
  takes the ISIN and exchange from the user, the ticker from OpenFIGI;
  mode 2 takes ISIN, ticker and exchange from the user. By the time the
  fund page renders, a typed ISIN and a resolved ticker look alike, so
  this can only be captured at resolution. Caches written before this
  report nothing rather than a guessed "Yahoo", and fill in on reload.

### Removed
- **The X-ray tab's "use information on:" selector.** Each fund's own card
  already chooses that fund's source, including Holdings, so a
  portfolio-wide override contradicted the more specific choice. With it
  went the four `render*BreakdownLT` renderers,
  `rollup_portfolio_lookthrough`, the `lookthrough_breakdowns` and
  `lookthrough_coverage` payload keys, and the `.bkmode-label` style.
- `fundCardEmptyMsg` — there is no empty card state left to explain.
- The per-facet `issuer_available` / `holdings_available` /
  `upload_available` / `factsheet_available` flags, replaced by the
  single `available` map.
- A second, shadowed definition of `escapeHtml`. Both behaved
  identically and the later one won, but it is the duplicate-definition
  failure mode the working agreement warns about.

---

## [0.49.2] — 2026-07-26

### Removed
- The page-wide "use information on: Fund/ETF level | Holdings level"
  switch. "Holdings" is one of the four sources each card now chooses for
  itself, so the same choice existed twice, in two places, with different
  scope. `renderFundBreakdownsHoldings` and `fundBreakdownMode` went with
  it (83 lines).

### Changed
- The text above the breakdown cards described that two-way mode. It now
  describes the four sources a card can actually have, and says a source
  with no data for a card is struck through.
- **Every breakdown card states its source**, not just the unusual ones.
  Silence on the common case made Yahoo the only unlabelled source, when
  "where did this come from" is the same question whichever source
  answered it.
- The factsheet caption carries the document's date — "comes from the
  factsheet (30-06-2026)" — and says so when that factsheet is past its
  age limit. A breakdown read off a two-year-old sheet is a different
  claim from one read off last month's.
- Group tiles are laid out in three columns with Trading beneath
  Identification. Those two are five and seven short rows against nine
  and eight for the others, so four equal columns left the first mostly
  empty. Two breakpoints below that.

### Added
- **Fields past their group's age limit are refetched automatically**, on
  opening a fund. That is what the TTLs are for; without this they were
  decorative.
  Fields pinned to your own value are never refetched — there is nothing
  to ask, and re-asking would mean overwriting you with a source you
  chose to replace. Unpinned fields follow the ordinary profile cache.
- **Reload Fund Data now means force**, and says so: refetch everything
  now whatever the ages say, including every pinned field from its own
  source. Previously it refetched the profile but left pinned fields
  alone, so a fund pinned to justETF reloaded everything except the
  fields the user had actually chosen a source for.
- `POST /api/funds/<ticker>/fields/refresh` with an optional `force`,
  reporting what was refreshed, what was skipped and why.

---

## [0.49.1] — 2026-07-26

### Fixed
- **Setting a field to "My own value" gave a free-text box for every
  field**, including the ones that accept six values. Closed vocabularies
  now get a dropdown, numbers get a number input with the registry's own
  unit, bounds and step, and only genuinely free fields get text.
  `focus_detail` is computed from the `focus_type` chosen alongside it
  and redraws when that changes — otherwise it keeps offering regions
  after the fund has been called a sector fund.
- Dropdown options use the same formatters the tiles do, so a value reads
  the same in both places.

### Added
- **TTL per field group**, replacing the per-cache-category TTLs as the
  thing the user sets. How quickly data goes stale is a property of the
  data, not of which cache file it lives in: structure barely moves
  (365d), costs are restated a few times a year (90d), prices are stale
  the next morning (1d), and an ISIN does not change (3650d).
  Shown as a pill in each group's header on the fund page and editable in
  Settings under "data freshness". 0 means always refetch; fields pinned
  to your own value are never refetched regardless.
- **The extraction prompt now covers every data item** — all 25
  non-calculated fields across the four groups, plus the four breakdown
  facets. Identity fields are read back for cross-checking only: a
  factsheet naming a different ISIN usually means the wrong share class,
  which is worth showing and never worth writing over the key every
  fund-level record hangs on.

### Changed
- The fund page's group tiles are laid out four-across where the screen
  allows, each with a tinted header bar and a coloured edge so the groups
  are distinguishable at a glance rather than by reading their titles.
  Values are tabular-figure aligned, calculated ones italicised, and the
  source line appears only when a field is actually pinned — printing
  "Yahoo" on every unpinned row was a column of the same word.

---

## [0.49.0] — 2026-07-26

### Changed
- **The Edit fund dialog is rebuilt around sources.** It shows the same
  four groups as the fund page, one row per field: label, current value,
  a source dropdown, and — when the source is "My own value" — an input.
  Editing a fund is now choosing where each value comes from, rather than
  typing values into boxes.
- **Choosing a source fetches from it immediately**, per field. The point
  of picking a source is to find out whether it has anything worth
  having, and a batch that resolved on Save would hide exactly that. A
  source with nothing answers *unknown*, shown in amber with the reason
  underneath — that is an answer about the source, not a failure.
- **Nothing is stored until Save.** The fetch runs in preview mode
  (`persist: false`), so Cancel is simply dropping the working state:
  there are no writes to undo and nothing to re-fetch, because the stored
  pins and values are exactly as they were when the dialog opened. The
  footer says how many changes are pending.
- Save commits every changed pin in one call, sending the values already
  fetched rather than asking again — a second fetch would double the
  network calls and could return something other than what the user just
  approved.
- Calculated and identity fields still appear, so the whole fund is
  visible in one place, but with "calculated" or "identity" where the
  source dropdown would be.

### Added
- `PUT /api/funds/<ticker>/fields/sources` — bulk pin commit, reporting
  per-field errors while applying everything valid.
- `persist: false` on the single-field endpoint, for preview fetches.

### Removed
- ~400 lines the old dialog left behind: `EF_FIELDS`,
  `_OVERRIDE_FIGURES`, `efChanged`, `editFundState`, the per-field fetch
  panel, the structure/figures save path, and `renderFundMetaTile`.
  Three functions had ended up with two definitions each after the
  rebuild — the later one silently winning — which is exactly the kind of
  thing that survives a passing syntax check. Every dialog function is
  now verified to be defined exactly once.

---

## [0.48.2] — 2026-07-26

### Fixed
- The Score cell showed one number without saying which. It now reads
  "76 / 84" — universe rank then peer rank — matching the pre-loaded
  list, and the label says "Score (all / peer)". A missing peer score
  shows as "—" rather than collapsing to a single figure.

### Changed
- **Group tiles no longer box each field.** A bordered, shaded cell per
  field inside an already-bordered group was a tile within a tile: it
  added a border and a background without adding information, and it
  stopped the values lining up with each other — which is the one thing
  a column of figures should do.
  Each field is now a row of label / value / source, in two columns on a
  wide screen and one on a narrow one, with values right-aligned so they
  align down the column.

---

## [0.48.1] — 2026-07-26

### Changed
- **The fund page now shows four group tiles**, each holding the
  sub-tiles of its group: Identification, Structure, Operational,
  Trading. Replaces a "fund meta data" card and a flat metrics grid whose
  split was historical rather than meaningful — TER sat beside 52-week
  high, and the fund's structure sat somewhere else entirely.
- The groups come from the server (`/api/funds/<ticker>/fields`), so the
  page and the Edit dialog cannot disagree about which field belongs
  where, and adding a field is a config change rather than an edit in
  three places.
- Every sub-tile says where its value came from: the pinned source when
  one is set (highlighted), the derivation when the structure block
  supplies it, "calculated" for derived figures, and nothing for the
  read-only identity fields.
- Identification gets no Edit button — it has nothing to edit, and a
  button opening a dialog offering nothing is worse than its absence.
- `fundFieldValue` resolves every field from one place. Values come from
  three different shapes — the profile blob, the merged structure block,
  and figures PorxPy computes — and the tiles, the Edit dialog and the
  reload plan all need the same answer for the same key.
- YTD and total return are stashed on the payload by `showMetrics` rather
  than recomputed by the tiles: two derivations of one number is how they
  end up disagreeing.
- The flat metrics grid is retained purely as a fallback for a fund whose
  taxonomy request failed, so a single failed call cannot leave the page
  blank.

---

## [0.48.0] — 2026-07-26

### Added
- **Field taxonomy: four groups, per-field sources** (`FIELD_GROUPS` in
  config). Identification (5, read-only), Structure (9), Operational (8),
  Trading (7). Each field declares which sources may supply it; some are
  narrower than the default — `holdingsCount` comes from Yahoo, a
  factsheet or the user, and the 52-week extremes only from Yahoo or the
  user, since no other source states them.
- **A field is now PINNED to a source.** The override envelope
  `{source, value, ts}` previously meant "this value was asserted by this
  source"; it now means "this field is read from this source". For stored
  data the two coincide — a value justETF asserted is a field pinned to
  justETF — so **no migration was needed**, unusually.
  The distinction matters because a pin survives a reload and drives the
  refetch, which "who asserted this" could not express.
- `value: null` is meaningful: it records that the pinned source was
  asked and had nothing. Without it a fund page cannot tell "not yet
  fetched" from "fetched, answer unknown", and would re-ask a source that
  has already said no.
- `GET /api/funds/<ticker>/fields` — the taxonomy with each field's pin,
  value and available sources, in presentation order.
- `PUT /api/funds/<ticker>/fields/<field>/source` — pins a field AND
  fetches that source's answer in the same call. Per field, not batched
  per source on save: the point of choosing a source is to find out
  whether it has anything, and a batch that resolves later hides exactly
  that.
- Identification is read-only. The ISIN is the key every fund-level
  record hangs on — holdings, breakdowns, overrides, factsheet — so
  re-sourcing it would orphan all of them.

### Fixed
- Eight fields were displayed and cached but absent from the registry, so
  they could not be validated, extracted from a factsheet, or pinned:
  both yields, holdings count, last close, NAV, volume and the 52-week
  extremes. All 20 sourceable fields are now covered — verified against
  the taxonomy and against the extraction prompt.

### Note
- Backend only. The regrouped fund page and the rebuilt Edit dialog are
  the next two steps.

---

## [0.47.2] — 2026-07-26

### Fixed
- Expense Ratio, Turnover and Total Assets showed no source. They are the
  three fields most likely to have been corrected by hand or read off a
  factsheet, so "is this Yahoo's number or mine?" is the first question
  about them. Each now carries a source note — blank when nothing has
  overridden it, since "from Yahoo" on every untouched tile is noise and
  the absence already says it.
- **Extraction accepts `asset_class` as a name for
  `primary_asset_class`.** The prompt names the field
  `primary_asset_class` and the FACETS section immediately below names a
  DIFFERENT thing `asset_class`, so a reply answering under the shorter,
  more natural name is a reasonable mistake — and rejecting it lost a
  correct answer to a naming detail. That is the likely reason a
  factsheet saying "Passive Equity" and "The Fund invests in equities"
  produced no primary asset class.
  The alias also rescues extractions stored before the v0.47.0 rename.
- Aliases added for other display-name answers: `ter`, `ocf`,
  `ongoing_charges`, `expense_ratio`, `fund_size`, `total_net_assets`,
  `turnover`, `portfolio_turnover`.
- The prompt now states the distinction on the field line itself, where
  the model reads it, rather than only in a hints block below.

---

## [0.47.1] — 2026-07-26

### Fixed
- **Every provenance caption read "seeded from Yahoo".** The label table
  knew four sources, and anything else fell through to the Yahoo entry —
  so a value read off a factsheet or justETF reported itself as the one
  thing it certainly was not. All sources are now named, and an
  unrecognised one says so rather than guessing.
- **Captions ignored half the provenance.** Two maps carry it —
  `fund_structure_sources` for the structure block, `override_sources`
  for TER, size, turnover and primary asset class — and the caption read
  only the first, so the second group showed nothing at all.
- **An explicitly fetched value is now stored even when it matches the
  seed.** Withdrawing on agreement is right for a hand-typed value: it
  keeps the field tracking the seed instead of pinning a snapshot. It is
  wrong for a fetch. "justETF says unknown" is an answer about justETF,
  and "unknown" is usually what the seed says too — so the override was
  deleted and the source silently reverted to Yahoo. The user asked a
  question and the answer disappeared.
- The metadata tile is now labelled **Primary Asset Class**, and shows
  its source when one is set.

### Changed
- The primary-asset-class dropdown moved from its own row above the
  Structure block into the classification grid beside the other fields,
  and is labelled "Primary asset class". Its old position implied a
  different kind of setting.
- Removed the per-field ↺ buttons from TER and Total net assets, and the
  `revertOverrideField` handler behind them. Re-fetching a value is what
  the fetch panel does, for every field, from a named source — two
  controls doing one job is how this dialog ended up with three "from
  Yahoo" buttons in the first place.

---

## [0.47.0] — 2026-07-26

### Changed
- **The metadata field `asset_class` is renamed `primary_asset_class`**
  (migration), labelled "Primary asset class".
  Two different things carried that name: the fund's own kind — a single
  value from the issuer or Yahoo's classification — and the asset_class
  BREAKDOWN FACET, which is what the fund holds, as a distribution
  derived from look-through. A 60/40 fund is `primary_asset_class:
  mixed`, and its asset_class facet is roughly 60% equity / 40% fixed
  income.
  Relabelling alone would have separated them in the UI, but the AI
  extraction prompt is generated from the registry AND from
  `BREAKDOWN_FACETS`, so it listed "asset_class" twice — asking the model
  for one name with two meanings. That is a correctness problem, not a
  cosmetic one, and it is why this got the full rename rather than a new
  label.
  The facet keeps its name. So does the `asset_class` cache category (a
  fund-level slot nobody sees, and renaming it would invalidate every
  cached classification for nothing) and the `data.asset_class` payload
  block.
- The prompt now spells out the distinction explicitly, since the two
  remain adjacent in it.

### Migration
- `_migrate_renamed_override_fields` moves stored values to the new name
  on read, and runs on every entry — a rename postdates the per-field
  store, so an entry written before it is already in envelope shape and
  the legacy-shape migration would never visit it.
- **Exact-key matching only.** A substring or prefix rule would also
  rewrite `breakdown_source.asset_class` — a different field, added in
  0.44.0, that happens to end with the old name — and quietly break that
  facet's source selection. Tested explicitly.
- A value already stored under the new name wins: the user set it since,
  and a migration should not undo that.

---

## [0.46.0] — 2026-07-26

### Fixed
- **Applied factsheet values lost their provenance on save.** Both save
  paths wrote every field with the default source `"user"`, so a value
  staged from the factsheet was recorded as hand-typed and the fund page
  stopped showing where it came from. Both now carry the staged source
  per field — the figures endpoint takes `source`, the structure endpoint
  takes a `sources` map — and `factsheet` / `yahoo` joined
  `OVERRIDE_SOURCES`.
- **`asset_class` was silently unextractable.** The prompt's field list
  is built from registry entries that have a `target`, and asset_class
  had none, so it never appeared. Given one (`asset_class.class`).
- **`turnoverPct` was not in the registry at all** — displayed and
  cached, but neither correctable by hand nor readable from a factsheet.
  Added.
- **Near-miss vocabulary values were rejected outright**, so the field
  vanished with no sign an answer had been given. Values now resolve
  through case/spacing normalisation, a synonym table for the closed
  vocabularies, and — for `focus_detail` — the region and sector alias
  tables. "Physical" -> full, "Optimised sampling" -> sampled,
  "Common Contractual Fund" -> fund, "Accumuleren" -> accumulating,
  "Aandelen" -> equity, "Developed Markets" -> developed.
- **`focus_detail`'s vocabulary is now spelled out in the prompt**, per
  focus_type. It is the one cross-field rule in the registry and a
  per-field description cannot express it, so the model returned
  plausible names that failed validation.
- **European number formats.** `8.565.668.400` failed to parse at all
  (more than one dot can only be a thousands separator), and `1,155` as a
  percent was read as 1155 and rejected as out of range. Percent fields
  now hint that a comma is a decimal point — a 1155% turnover is not a
  real figure.

### Added
- **The prompt is shown in the extraction report**, and stored alongside
  each result: six months on, "why did it read it that way" is
  answerable only if the instruction is on record with the answer.
- **Optional prompt editing**, off by default, under Settings → AI
  helper. An edited prompt applies to that one run and is never saved as
  the default — a persisted prompt would silently apply to every future
  extraction, including ones where the user had forgotten changing it.
  The server refuses a custom prompt when the option is off.

---

## [0.45.3] — 2026-07-26

### Fixed
- Factsheet facet percentages were 100x too high. The extractor reports
  percentages, because that is what a factsheet prints, but the store
  holds fractions — the CSV commit divides by 100 before writing and
  every consumer assumes 0–1. The factsheet path wrote them raw.
  A sector table now stores 0.233 and the card renders 23.3%.

### Changed
- **One "✨ Extraction" button, always available once a factsheet
  exists**, replacing Extract / Re-extract. It opens the report, which is
  where the extraction action now lives.
  Previously the only way to see what had been read was to run a fresh
  extraction — so reviewing a result cost an API call, and the report
  vanished the moment it was closed. The extraction has always been kept
  on the factsheet's sidecar, so revisiting it costs nothing and survives
  restarts.
- The report carries a prominent **Extract now** / **Extract again**
  button, which says what it will cost, and shows the reason inline when
  it cannot run — the switch being off, or no key in the environment —
  rather than presenting a dead button.
- An unread factsheet opens the report with an explanation of what
  extraction does, instead of offering nothing.

---

## [0.45.2] — 2026-07-26

### Fixed
- Extraction crashed with
  `AttributeError: 'dict' object has no attribute 'strip'` on any
  factsheet where the model returned a focus. `validate_extraction`
  passed the model's raw field blocks as the coercion context, so
  `focus_detail_vocabulary` — which decides its vocabulary from the
  sibling `focus_type` — received `{"value": "region", "quote": …}` where
  it expects a plain value. A flat field → value map is now built for
  exactly that purpose.
  Note this was the API call working: the reply was real, and the fault
  was entirely in processing it.
- `focus_detail_vocabulary` no longer raises on a non-string context.
  It is reachable from anything that validates an override, including a
  reply whose shape is not ours to control, and a bad context should
  narrow the vocabulary rather than throw.
- Everything after the API call is wrapped. The call has been made and
  paid for by then, so a bug in our own parsing should not surface as an
  HTML traceback — which the browser reports as "NetworkError", losing
  both the diagnosis and the result. Failures now return a readable
  message plus the raw reply, so the extraction can be salvaged or the
  bug reproduced without spending another call. A facet-storage failure
  is reported in `rejected` rather than losing the fields alongside it.

---

## [0.45.1] — 2026-07-26

### Fixed
- The Extract button reported "Switch on the AI helper in Settings first"
  even with the helper on and the key detected. `refreshAiStatus()` was
  guarded by `if (!aiStatus.key_env || aiStatus.ready === undefined)`,
  and the seed object sets both of those — so the condition was never
  true, the status was never fetched on the fund page, and the button
  rendered from the pessimistic defaults for ever.
  The seed now carries `checked: false`, which distinguishes "not asked
  yet" from "asked, answer was no". A guard cannot tell a seed from a
  real reply without that, which is precisely how this happened.
- Saving Settings re-asks and repaints the fund page's buttons. Switching
  the helper on previously left the button reporting the state from
  before the save until a reload.

---

## [0.45.0] — 2026-07-26

### Added
- **AI factsheet extraction** (`porxpy/ai.py`). One call reads an
  uploaded factsheet and returns both the metadata fields and all four
  facet breakdowns, so the two provably come from a single reading —
  two calls could disagree about the same page.
- **Extract / Re-extract** button on the fund page, a
  **Fetch from factsheet** button in the Edit dialog, and
  **Issuer (factsheet)** on the breakdown cards now populated.
- `POST /api/funds/<ticker>/factsheet/extract`, `GET /api/ai/status`,
  and `factsheet` as a third source on `source_fields`.
- Settings gains the consent switch, off by default, stating plainly what
  leaves the machine: the public issuer document you uploaded, and
  nothing else — no holdings, no portfolios, no positions.
- The extraction report lists every value with the page and verbatim
  quote it came from, the breakdowns with their sums, what was rejected
  and why, and the approximate cost. Nothing is applied.

### What keeps it honest
- **The prompt is generated from the registry** — field names, types,
  vocabularies and bounds come from `OVERRIDABLE_FIELDS` and the resource
  CSVs. The model cannot be asked for something the store would reject,
  and adding an overridable field extends extraction with no change to
  the AI module.
- **Every value is re-validated** by the same `coerce_override_value` a
  hand-typed value goes through. `market_cap: "enormous"` is rejected by
  code that already existed.
- **A value without a citation is discarded** before validation.
  Hallucinated numbers are well-formed and pass every type check; the
  quote is the only thing distinguishing extraction from invention.
- **Units are declared, never inferred.** Every numeric answer states its
  unit and conversion is explicit. Inferring scale from magnitude is what
  made total net assets wrong in three consecutive releases, and
  "$11,7 miljard" is exactly that terrain. Locale-aware parsing handles
  both "84.138,91" and "84,138.91".
- **A facet's own arithmetic overrides its claim to be complete.** A
  table summing to 95.5 is partial whatever the model says, and a partial
  table treated as complete misstates every row in it.
- Temperature 0, so Re-extract is a retry rather than a lottery.
- The API key is read from `ANTHROPIC_API_KEY` and never written to disk;
  `settings.json` holds only the on/off switch.
- Extraction adopts the document's own date when none was given, which is
  what makes the staleness warning honest.

---

## [0.44.2] — 2026-07-26

### Fixed
- **A holdings rollup rescaled partial lists to 100%.** `rollup_holdings`
  divided every bucket by the sum of the rows' own weights, so any list
  summed to exactly 1.0 — a top-10 covering 21% of a fund was silently
  stretched to a whole portfolio, and the card reported full coverage
  while doing it. The `undefined` bucket only ever caught rows with a
  blank facet value, never the rows that were not there at all.
  Weights are now fractions of the WHOLE FUND: the denominator is 100
  unless the rows exceed it (rounding, or a leveraged fund genuinely
  above par), and the shortfall joins `undefined`. Both meanings of that
  bucket — "row present but unclassified" and "row absent" — are
  "weight we cannot attribute", which is what the reader needs.
  On the Northern Trust top-10, technology now reads 11.80% (its real
  weight) rather than 55.9%, with a 78.90% `undefined` slice.
- `coverage` is a real number again — the share of the fund the rows
  account for — instead of 1.0 by construction. A top-10 list is 21%
  covered however tidily its own weights add up.
- The portfolio look-through inherits the fix: a fund with 21% holdings
  coverage now contributes its real weight and an `undefined` remainder,
  rather than being blended in as though fully mapped.

---

## [0.44.1] — 2026-07-26

### Fixed
- Unpopulated sources on the facet cards looked fully selectable. The
  buttons carried a CSS *class* called `disabled`, while the stylesheet
  greys out `.bkseg button[disabled]` — the *attribute*. Nothing matched,
  so "Upload" read as an available source on funds that had never had a
  CSV.
  The real attribute is now emitted, which also makes the button
  genuinely inert rather than merely missing its click handler.
- Unavailable sources are struck through as well as dimmed. Opacity alone
  reads as "subtle" against an already-muted palette, and the point is
  that the option exists and is empty — visible, not selectable.

---

## [0.44.0] — 2026-07-26

### Changed
- **Breakdown sources are now four: `yahoo`, `factsheet`, `holdings`,
  `upload`.** The facet cards' selector reads "Issuer (Yahoo)" ·
  "Issuer (factsheet)" · "Holdings" · "Upload", and is built from what
  the card reports as available rather than a hardcoded list — so a fifth
  source is a backend change only.
- **`fund` renamed to `yahoo`** (migration). "Issuer card" was an
  unambiguous meaning for that key only while Yahoo was the sole way to
  get one; with a second issuer source the name says nothing that
  distinguishes them, and a stored key contradicting its label is how
  confusion starts.
  The rename is applied on every override read, not only to legacy-shaped
  entries: it postdates the per-field store, so a value written in
  0.33–0.43 is already in envelope shape and would never otherwise be
  visited. `yahoo` is also the neutral value, so such an override is
  dropped rather than translated — it was never an assertion.
- **`uploaded_breakdowns` is now `{facet: {source: items}}`** (migration).
  It was `{facet: items}` because a user CSV was the only non-derived
  source. A factsheet extraction is a second, and storing both flat would
  have meant each silently overwriting the other: upload a CSV, extract a
  factsheet, and selecting "Upload" would quietly show factsheet numbers.
  Detection is by shape — a facet mapping to a list is legacy, to a dict
  is current — which converges, where a key-name test would not.
  `uploaded_breakdowns_put` takes a `source` and merges, so writing one
  leaves the other intact.
- Cards fall back to the Yahoo issuer card when the requested source has
  nothing, and report which source was actually used. Yahoo is never
  disabled in the selector, since it is what everything else degrades to.

### Note
- Step 3 of the factsheet/AI work. `Issuer (factsheet)` appears now and
  is selectable as soon as a factsheet extraction exists — which is step
  4. Until then it shows as unavailable with a tooltip saying why.

---

## [0.43.1] — 2026-07-26

### Added
- **Factsheet staleness threshold is now settable**, in Settings under
  "factsheets", defaulting to 183 days. 0 disables the warning.
  It is deliberately NOT in the per-portfolio cache-config dialog, and it
  is not a TTL. Those settings answer "how long may we reuse this before
  refetching", which presumes a source to refetch from — a hand-uploaded
  document has none, so nothing expires and nothing is ever deleted. What
  the number controls is when the UI starts saying the document looks out
  of date, which is a display preference and belongs with the other
  global ones.
  It is also fund-level rather than portfolio-level: the same factsheet
  serves every portfolio holding the fund, so a per-portfolio threshold
  would let one portfolio call a document stale while another called it
  current.
- `factsheet_get` reports `stale_days` alongside `stale`, so the caller
  can say what threshold was applied.

---

## [0.43.0] — 2026-07-26

### Added
- **Factsheet storage.** One issuer factsheet per fund, keyed by ISIN, as
  the original bytes plus a JSON sidecar. PDF, image (a photo of a
  printed sheet is a perfectly good source) or HTML. Stored under
  `cache/factsheets/`.
- Three controls on the fund detail page beside Add to portfolio and
  Reload fund data: **Upload/Replace factsheet**, and **View factsheet**
  once there is one. View opens a popup rather than an in-page overlay,
  since the point is to read the document *beside* the data it produced.
- `GET/POST/DELETE /api/funds/<ticker>/factsheet` and
  `GET …/factsheet/file`. POST takes multipart or a `source` string,
  resolved by the same `resolve_source` every other upload uses — so URL,
  path and drop all work without a third way of naming a file.
- Drag and drop on the factsheet dialog, same shape as the other two.
- **Staleness is measured against the factsheet's own date**, not the
  upload date, with the date asked for in the dialog. The Northern Trust
  sheet used in testing reads "per Augustus 31, 2023" — nearly three
  years old when uploaded, and an upload-date rule would have called it
  fresh for six months. Threshold 183 days; View shows a ⚠ past it.
- Expiry flags, it does not delete. Every other cache entry can be
  re-fetched; a hand-uploaded document cannot, so expiring it would
  destroy the user's own file to save nothing.

### Changed
- The stash endpoint and the file browser accept factsheet types as well
  as data files — otherwise the factsheet drop zone would accept a PDF
  the server then refused.
- A dropped file's stored name is the one the user dropped, not the
  scratch copy's `drop_f126105f_vaneck.pdf`. The extension still comes
  from the resolved file, so a mismatched display name cannot change how
  the document is typed or served.

### Note
- Step 2 of the factsheet/AI work. Still no AI dependency: uploading,
  viewing and staleness are useful on their own. `extraction` exists on
  the sidecar and stays null until step 4, and is discarded whenever the
  document is replaced — an extraction must never outlive its source.

---

## [0.42.2] — 2026-07-26

### Fixed
- Dropping a file put the scratch path in the Source field —
  `…/uploads/drop_f299a9cc_bd.csv` — which was accurate and useless,
  since that is not a file the user has ever seen. The field now shows
  the dropped file's own name and carries the scratch path on a data
  attribute, so the display is recognisable and the server still gets a
  path it can re-read.
- Typing in the field clears the stashed path, so a hand-typed URL or
  path is used as given rather than silently resolving to whatever was
  dropped before it. Both dialogs also clear it on reset.

---

## [0.42.1] — 2026-07-26

### Changed
- **The two upload dialogs are now the same dialog.** The breakdown CSV
  upload had a bare `<input type=file>` with the browse control in a
  different place, and a drop left the field blank while the holdings
  dialog showed the resolved path. Two dialogs doing the same job should
  not look or behave differently.
  The breakdown dialog now has the holdings layout: one Source field
  taking a URL or a path, a Browse button beside it, the whole group a
  drop target, and a dropped file shown as its stashed path.
- `POST /api/funds/<ticker>/uploaded_breakdowns/preview` accepts a
  `source` string, resolved by the same `resolve_source` the holdings
  upload uses. So a URL, a filesystem path and a dropped-then-stashed
  file all take one code path instead of the multipart branch being the
  only way in. The multipart and inline-CSV bodies still work.
- The file picker takes a target, so Browse fills whichever Source field
  opened it.

---

## [0.42.0] — 2026-07-26

### Added
- **Drag and drop wherever a file can be given.** One `wireDropZone`
  helper covers the holdings upload and the breakdown CSV upload, so
  "which of these dialogs accepts a drop?" is never a question. The
  existing file picker and URL field are untouched — this is an
  additional way in, not a replacement.
- `POST /api/upload/stash` — writes dropped bytes to the scratch folder
  and returns a real path.
  The holdings pipeline is path-based end to end (resolve_source, the
  remembered source_value, one-click re-upload) because the server runs
  on the user's own machine and a path can be re-read later. A browser
  drop gives bytes and no path, so supporting it would otherwise have
  meant a second parallel pipeline. Stashing keeps one.
  The tradeoff is stated in the dialog: a dropped file lives in scratch,
  so re-uploading from the same source works until that folder is
  cleared, whereas a path or URL keeps working indefinitely.
- Stashed filenames are sanitised to their stem plus a random prefix, so
  a path-bearing or hostile client filename cannot escape the scratch
  directory. Extension is preserved, since the parser dispatches on it.

### Note
- First step of the factsheet/AI work. Deliberately no AI dependency and
  no migrations: drag and drop is useful on its own and can ship ahead of
  the rest.

---

## [0.41.2] — 2026-07-26

### Fixed
- **`JSON.parse: unexpected character` on any response containing a NaN.**
  Python's encoder emits bare `NaN`, `Infinity` and `-Infinity` for those
  float values. Both json.dumps and json.loads accept them; the browser's
  JSON.parse rejects them outright — so a single NaN anywhere makes the
  WHOLE response unreadable, with an error naming a line number rather
  than a field. Reported on the source inspector for 0P00015UO7.F, whose
  Yahoo `info` carries them.
- Fixed globally with a JSON provider that renders non-finite floats as
  `null`, rather than at the endpoint where it was noticed. The same
  values reach `/api/fund` — `extract_profile` copies `info` values
  through verbatim — so this was latent on the main endpoint too, and
  patching one call site would have left the others waiting to fail.
  `null` is the honest rendering: NaN means "no value", which is what
  `null` means to the client.
- The inspector additionally flattens numpy scalars, pandas objects and
  sets, since it dumps upstream data whose shape is not ours to control.

---

## [0.41.1] — 2026-07-26

### Fixed
- Startup crashed with `NameError: name 'super_members' is not defined`
  in `_load_country_codes`. The transitive super-region expansion added
  in 0.41.0 was inserted into the wrong function: the edit anchored on
  `if raw_rows and not rows:`, which appears in three loaders, and landed
  in the first. Moved into `_load_regions`, where its variables exist.
- The bug was invisible in testing because the test environment has no
  `country_codes.csv`, so `_load_country_codes` returned early and never
  reached the misplaced code. Re-verified with the file present.

---

## [0.41.0] — 2026-07-26

### Added
- **`regions.csv` v3: `developed` and `world` super regions.** `world`
  sits above everything — every region and every other super region
  belongs to it. `developed` covers North America, developed Europe, the
  UK, Japan, Australasia and developed Asia; `emerging` already existed
  and now also sits under `world`.
  This exists for peer grouping: previously every global fund fell into
  `focus_type: none`, which is also where funds with no derivable focus
  land. A World tracker and a fund nobody has classified were peers. Now
  they are not.
- Super regions may belong to other super regions. `_load_regions`
  resolves the nesting transitively to a fixed point, so `world`'s
  membership includes everything beneath `developed`, not just the rows
  that named `world` directly.

### Changed
- Several region hits now collapse to the **smallest** super region
  covering them, not merely to one that works. "MSCI World" matches both
  `developed` and `world`; `world` would be true but useless, since MSCI
  World is a developed-markets index and the narrower answer is the
  informative one. Ranking by member count picks it without hard-coding
  precedence.
  This also fixes names that intersect two supers with no containment
  either way: "Developed Europe ex UK" hits `developed`, `europe` and
  `unitedKingdom`, neither super contains the other, and the old rule
  returned nothing at all. It now returns `europe`, the tighter of the
  two supers covering the region hit.
- Dropped the bare `america` and `american` aliases from `northAmerica`.
  They collided with "Latin America" and "Latin American", so
  "MSCI EM Latin America" matched northAmerica as well as latinAmerica
  and resolved to nothing. `north america`, `usa`, `united states` and
  the index names cover the intended cases.

---

## [0.40.1] — 2026-07-26

### Fixed
- **Portfolio funds table columns were misaligned.** Adding the lock
  column in 0.40.0 replaced the Weight cell instead of inserting before
  it, so every cell after Value sat one column left of its header. The
  cash row never got a lock cell at all, shifting it independently.
  Both row templates now match the header at 11 columns, in the order
  Weight → Locked → Delete.
- "Actions" column renamed "Delete", which is all it does.

### Changed
- **Alternatives redrawn as a table per fund.** Rows are the chosen fund
  followed by its alternatives; columns are the targeted facets, showing
  the achieved deviation in each cell with the delta beneath and the
  tolerance in the header. The radio sits in column 0.
  The previous layout put each alternative's facet figures in a row of
  inline chips, so comparing two candidates on the same facet meant
  reading across two rows of differently-positioned text. In a table the
  comparison is a column, which is the comparison actually being made.
- Removed the CSS for the old chip layout rather than leaving it to rot.

---

## [0.40.0] — 2026-07-26

### Added
- **`locked` flag, per position, per portfolio.** "Do not suggest selling
  what I hold of this fund in THIS portfolio." Deliberately distinct from
  the fund-level `incl` flag, which means "never put this fund in a buy
  suggestion, in any portfolio". The two are independent and give four
  states: free / sell-but-never-buy / buy-but-never-sell / fully frozen.
- Lock column in the portfolio funds list, with sort and an
  All/Locked/Unlocked filter. Toggled in place — a full reload would
  re-sort the table under the cursor mid-click.
- `PUT /api/portfolios/<pid>/funds/<ticker>` accepts `locked`. Stored only
  when True: the default is unlocked, and writing False everywhere would
  put a flag on every position in every portfolio to record that nothing
  is happening.

### Changed
- **The solver projects onto a capped simplex.** The middle two states are
  not exclusions but per-column bounds — never buy means `w ≤ current`,
  never sell means `w ≥ current` — which the plain simplex projection
  cannot express. `_project_capped_simplex` solves
  `w = clip(y - lambda, lb, ub)` for the lambda giving `sum(w) = 1` by
  bisection; `sum(w(lambda))` is continuous and non-increasing, so 60
  iterations are exact to floating point. No new dependency.
- Locked positions are pre-selected rather than chosen by greedy (a lower
  bound only binds if its column is present), are never swapped out, are
  never pruned as dust, and are never offered a better-in-class
  alternative — substituting one would be exactly the sale that was
  forbidden.

### Fixed
- The swap-refinement guard protecting locked positions was written but
  never inserted: `forced_set` was computed and then not consulted, so the
  refinement happily exchanged a locked fund away. Caught by a test where
  a locked position was sold down to zero weight.
- The portfolio funds filter handler read the new lock filter *after*
  re-rendering, so it would have applied one click late.

---

## [0.39.2] — 2026-07-26

### Added
- Fund names in the alternatives panel, with the ticker as a sub-line.
  A table of tickers asks the reader to recognise identifiers rather than
  funds.
- **Per-facet consequences for every alternative.** Each substitution now
  shows what it does to each targeted facet — before, after, delta and
  that facet's tolerance — sorted by which facet pays most.
  The worst-deviation headline cannot say *where* the cost lands, and
  that is usually the deciding fact: a swap costing 1.4pp on a facet you
  set loosely is a different proposition from the same 1.4pp on your
  tightest one. Observed in testing: one alternative cost 8.65pp on
  country but only 1.57pp on sector — a single number reports neither.
- `facets` in each alternative is now `{facet: {before, after, delta,
  tolerance, within}}` rather than a bare deviation map; `name` added to
  both the held fund and each alternative.

---

## [0.39.1] — 2026-07-26

### Changed
- The swap refinement now runs unconditionally. It was skipped when
  greedy had already met every tolerance, on the reasoning that the user
  asked for "good enough" rather than "optimal". That stopped holding
  once substitutions were priced against the achieved deviation.
  Greedy stops at the FIRST qualifying design, so a successful run lands
  just under the line. The swap pass exchanges funds within the existing
  set size, adding none, so it can often take 1.8pp to 0.4pp for free.
  Skipping it showed a worse design at no saving, and priced every
  alternative in the quality table against an unnecessarily loose
  baseline — making more of them read "exceeds tolerance" than truly do.
  On a four-fund test the design now converges to 0.00pp at 2pp, 5pp and
  10pp tolerances alike, where previously the looser two stopped early.

---

## [0.39.0] — 2026-07-26

### Changed
- **Fund quality is now priced, not applied.** The automatic score-driven
  swap pass added in 0.38.0 is removed. It could barely ever fire: greedy
  stops at the *first* design inside tolerance, so the leftover error
  budget is near zero by construction and almost no swap would fit in it.
  More fundamentally, it made the tool guess how much accuracy the user is
  willing to trade — a judgement that varies per portfolio.
  Instead, for every fund in the design the optimiser finds the
  higher-scoring funds in its peer group and prices each substitution:
  swap it in, re-solve, measure the new deviation. The user picks.
- Substitutions that exceed a tolerance are shown and flagged, not hidden.

### Added
- `alternatives` on the optimiser result — per chosen fund, its
  better-scoring peers with score delta, deviation before and after, the
  per-facet breakdown, and whether the result stays within tolerance.
- `substitutions` parameter: `{held_ticker: replacement_ticker}`, forced
  into the selection before the weights are finalised, so a substituted
  design is solved exactly like any other rather than patched afterwards.
- "Better-scoring alternatives" panel in the Optimizer tab, one radio
  group per fund (keep, or one of the alternatives), with Recalculate.
  Costs are each measured against the same baseline, so they are
  comparable but do not add up — accepting several requires a real
  re-solve, which is what Recalculate does. The panel says so.
- Score column in the trade suggestions, showing all/peer for each fund
  bought or sold, matching the pre-loaded list's format.
- `score_all` / `score_peer` on both positions and trades.

---

## [0.38.0] — 2026-07-26

### Added
- **Fund quality in the optimiser.** A third refinement pass runs after
  the error-driven ones: accept an exchange when it raises the weighted
  portfolio score AND leaves every facet within tolerance.
  The tolerances become an error budget — once inside them, the stopping
  test stops meaning "stop here" and starts meaning "this is the feasible
  region", and the optimiser maximises quality within it. The alternative,
  a weighted objective `error² - lambda x score`, needs a lambda trading
  squared exposure error against score points, a quantity with no
  interpretable unit that would need retuning whenever the universe or
  scoring changed.
  Tight tolerances leave no room, nothing is accepted, and the result is
  exactly what the fit passes produced. If the design never reaches
  tolerance the pass does not run at all.
- Only `score_peer` is consulted. Ranked globally, bond funds sit at the
  bottom of any returns-weighted score permanently — not for being bad but
  for being bonds — and a score-driven pass would push toward equity,
  fighting the targets it exists to satisfy.
- Funds with no peer score are never swapped in: promoting one would mean
  guessing at a comparison that cannot be made.
- "Prefer better funds" picker in the Optimizer tab, defaulting to off,
  reading the same presets the fund list uses. Results report both sides
  of the trade — score before/after and worst deviation before/after.
- `score_swaps` and `score` on the optimiser result.

### Fixed
- `optimise_portfolio` documented `facet_weights` twice, the second entry
  claiming a default of 1.0 each and contradicting the `1/tolerance`
  default described directly above it.

### Note
- Scoring was built in 0.35.0 and shown in the UI in 0.35.1, but was never
  wired into the optimiser — flagged in a changelog note rather than
  surfaced. This closes that gap.

---

## [0.37.0] — 2026-07-26

### Changed
- **Edit fund: one per-field re-fetch panel, replacing three buttons.**
  The dialog had "Look up on justETF", "Reload from Yahoo" and the
  figures' own ↺ — three controls that each covered a different,
  undocumented subset of the same fields. "Reload from Yahoo" overwrote
  all six structure selects unconditionally, discarding user choices on
  market cap, equity style and replication; focus escaped only because
  someone had special-cased it.
  Now every overridable field is listed, grouped (Classification / Focus
  / Costs & size), each with a checkbox, its current value and where that
  value came from. Tick what you want, press "Fetch from Yahoo" or
  "Fetch from justETF". **Unticked fields are never touched.** A ticked
  field always takes the pressed source's answer, which may be "unknown"
  — the source was asked and that is its answer.
- The field list is declared once (`EF_FIELDS`) and drives the renderer,
  both fetch buttons and select-all. Adding an overridable field is one
  entry rather than four edits that can drift — which is how three
  buttons ended up covering three different subsets.
- Fetches are staged, not saved. Save commits them alongside anything
  typed manually, so a fetch can be reviewed or abandoned. Rows changed
  by a fetch are highlighted and captioned with the source until saved.

### Added
- `POST /api/funds/<ticker>/source_fields` — asks one source for its
  answer on a set of fields. Every requested field comes back with a
  value; a source with nothing to say answers "unknown" rather than being
  omitted. Persists nothing.
- Yahoo answers on focus too, via the fund name: the issuer writes "MSCI
  Europe Small Cap" on the tin and `derive_structure_from_name` reads it.
  A source having no dedicated field for something does not mean it has
  no information about it.

---

## [0.36.4] — 2026-07-26

### Fixed
- **Revert button was disabled exactly when it was needed.** It greyed
  itself out when no override existed, on the reasoning that there was
  nothing to revert. That confuses the two ways a displayed value can be
  wrong — an override pinning it, and a stale cache holding it — and only
  the first is an override problem. The user intent is the same either
  way: "this number is wrong, go and ask Yahoo". The button is now always
  enabled, always re-fetches, and treats a 404 from the DELETE (no
  override to remove) as the non-event it is.
  This also means 0.36.2 and 0.36.3 fixed a path that was never being
  reached — the button could not be pressed on a fund with no override.

### Added
- **Inspector shows the whole Yahoo surface**, not the fee-and-size
  subset: every `funds_data` block (description, fund_overview,
  asset_classes, top_holdings, sector_weightings, equity_holdings,
  bond_holdings, bond_ratings), a price-history summary, `fast_info`,
  `ticker.isin`, and the full `ticker.info`. Each fails independently, so
  a listing with no bond holdings still shows its sector weightings.
- **OpenFIGI and justETF blocks always appear.** They were omitted
  entirely when no ISIN was given, which looked identical to a source
  that had been queried and returned nothing. They now say they were
  skipped and why.
- The ISIN is resolved from the listing identity cache, then from Yahoo,
  when only a ticker is entered — so the ISIN-keyed sources run without
  the user having to look it up. The report says where the ISIN came
  from.

---

## [0.36.3] — 2026-07-26

### Fixed
- "Revert to Yahoo" still did not go to Yahoo. 0.36.2 replaced the
  client-side reconstruction with a re-fetch, but passed
  `forceRefresh=false` — so it re-read the same cached profile the stale
  seed came from, the value did not change, and the button still did not
  do what its label says. Now forces a live refetch.
- Revert shows progress while the fetch is in flight and repaints the row
  on failure, so the caption and the button's disabled state cannot drift
  apart.
- Revert buttons gained tooltips distinguishing "delete this override and
  re-fetch from Yahoo" from "nothing to revert".

---

## [0.36.2] — 2026-07-26

### Fixed
- "Revert to Yahoo" restored a stale value. The handler put back
  `override_seed` — the value the override displaced — but that seed is
  read from the cached profile, so reverting on a fund whose cache is
  stale faithfully restored the stale number. Observed as a fund reload
  showing the correct EUR 8.57bn while pressing revert put EUR 0.17m
  back: both behaving exactly as written, opposite results.
  Revert now re-fetches the fund instead of reconstructing the value
  client-side. One request, and it cannot disagree with the rest of the
  app because it is the same assembly every other screen reads. The
  client-side reconstruction remains as an offline fallback so the
  dialog is never left showing a value that was just deleted.

---

## [0.36.1] — 2026-07-26

### Fixed
- Inspector called `extract_profile(t, ticker)`; it takes one argument.
  The "what PorxPy derives" panel showed a TypeError instead of the
  derived values — the single most useful line in the report.

### Added
- **Stored (cache + overrides) block in the inspector**, open by default
  and placed directly after Yahoo. Shows the cached profile with its
  write timestamp — what the fund tile actually reads — and any stored
  overrides, which win over everything.
  This is the comparison that was missing. A correct source, a stale
  cache and a pinning override look identical from the fund tile, and
  only one of the three is a data-source problem.

---

## [0.36.0] — 2026-07-26

### Added
- **Tools tab — source inspector.** Enter a ticker and/or ISIN and see
  exactly what Yahoo, OpenFIGI and justETF return, with no coercion, no
  unit conversion and no fallback chain applied, next to what PorxPy
  derives from it. Live calls; nothing cached or written.
  Yahoo's block leads with the fee-and-size candidates, each shown raw
  and under x1e3 / x1e6 readings, and with `fund_operations` rendered
  including its category column — rows where the fund value equals the
  category value are flagged amber as not fund-specific.
  This exists because total net assets was wrong in three consecutive
  releases, every time because a value was being interpreted without
  anyone able to see what had arrived.
- `GET /api/tools/inspect?ticker=&isin=`.

### Fixed
- Scoring read the raw cached profile and ignored per-field overrides, so
  a fund whose TER or size the user had corrected was scored on Yahoo's
  value while every other screen showed the correction.

### Known — not yet fixed
- Size reported as "no data" for most funds, and total net assets wrong
  for at least IE00B5L8K969 (8.2bn reported as 0.17M). The size chain was
  narrowed to `info.netAssets` / `info.totalAssets` in 0.33.8 with a 1e6
  plausibility floor, and one of those two is the likely cause — but this
  field has been "fixed" on a guess three times already, so the inspector
  is shipping first and the fix will follow from what it shows.

---

## [0.35.2] — 2026-07-26

### Added
- **Scoring weights are editable in Settings.** One block per preset:
  the three component weights, the six return-period weights, and an
  editable preset label. Rendered from the settings payload rather than a
  hardcoded list, so a preset added in config appears without a frontend
  change and round-trips whether or not the frontend knows its name.
  Plus the fund-size floor, previously only settable by hand-editing
  settings.json.
- Saving re-fetches scores and redraws the fund list, so the numbers on
  screen match the model that was just saved.

### Fixed
- Every fund reported "no data" for returns. The data was there — a
  component blends to null when its weight is zero, and the Cost driven
  preset sets all six WTRR weights to zero by design, so the tooltip's
  `null -> "no data"` mapping mislabelled every fund. The tooltip now
  uses `has_data` to distinguish "no data" from "not weighted in this
  model".
  This is the second time the same conflation has surfaced: it was fixed
  for the coverage count in 0.35.0 and the display path was missed.

---

## [0.35.1] — 2026-07-26

### Added
- **Score column in the pre-loaded fund list.** Shows universe rank and
  peer rank as "72 / 84", peer emphasised because it is the actionable
  one. An amber asterisk marks a score computed from fewer than three
  components. The tooltip breaks out each component, names the peer group
  and its size, and says when the group is too small to rank within.
  Sortable on the peer score; unrankable funds sort last ascending rather
  than as zero, since unrankable is not the same as bad.
- Weight-model picker in the list's filter row. Changing it re-scores
  without re-reading the cache list, and preserves scroll position.
- **Score tile on the fund detail page** — peer score as the headline,
  universe rank and peer-group size on the sub-line. Reuses the same
  score map the list fills, so opening a fund after viewing the list
  costs no extra request; when the list has not been drawn it fetches
  once and redraws.
- Scores are fetched in parallel with the cache list rather than
  serialised, and a score failure degrades to "—" instead of breaking the
  list.

### Fixed
- Column order mismatch introduced in the same change: the colgroup had
  Portfolios before Score while the header and body rows had Score first,
  so the two columns got each other's widths.

---

## [0.35.0] — 2026-07-26

### Added
- **Best-in-class fund scoring** (`porxpy/scoring.py`, pure compute).
  Ranks the pre-loaded universe on cost, size and trailing returns and
  produces two scores per fund.
  - Everything is a PERCENTILE (0–100, higher better), not a raw value —
    a TER in percent and a size in billions combine without inventing an
    exchange rate between them, and no outlier can dominate. Percentiles
    rather than ranks so a preset tuned against 35 funds means the same
    thing at 50.
  - `score_all` ranks against the whole universe; `score_peer` against
    funds sharing asset class and focus. **The optimiser will read
    `score_peer` only.** Ranked globally, bond funds sit at the bottom of
    any returns-weighted score permanently — not for being bad but for
    being bonds — and a score-driven optimiser would push toward equity,
    fighting the targets it exists to satisfy.
  - Peer groups below `MIN_PEER_GROUP` (3) get no peer score. A
    percentile within a group of one is 100 by construction, which would
    make every uniquely positioned fund look ideal.
  - Fund size is a floor test, not a ranking: 100 above
    `DEFAULT_SIZE_FLOOR_BASE`, 0 below. Past "big enough not to close or
    trade badly", more is not better.
  - TER is inverted (cheap ranks high). Ties share the average
    percentile of the positions they span.
  - WTRR: each return window is percentiled across the funds that have
    it, then blended by the per-period weights, renormalised over the
    windows each fund can support — a young fund is scored on what it
    has rather than marked down for its age.
  - Missing components renormalise the remaining weights; every score
    carries `coverage` so a score computed from thin data is visibly
    thin.
- `settings.scoring` — three weight models (Cost driven / Cost and
  returns / Returns driven) plus the size floor, each preset validated
  independently so one broken preset in a hand-edited file does not cost
  the other two. Cost driven is the default: ranking on trailing returns
  is performance chasing, and cost is the one component that reliably
  persists.
- `GET /api/scores[?preset=]` and `GET /api/scores/presets`. Read-only
  from cache — scoring renders on every fund-list draw, and a pass that
  could trigger a fetch would turn opening a tab into 35 round-trips.

### Fixed
- Removed the stale `ishares.parse_csv` reference in `breakdowns.py`;
  no such module exists.
- `json` was not imported in `app.py`, and the scoring assembly's
  `except Exception: continue` swallowed the resulting NameError on every
  cache file — reporting an empty universe instead of failing. The
  exception is now narrowed to `(OSError, ValueError)` and logs what it
  skipped.
- Coverage counted a component as missing when its weight was zero. Under
  Cost driven every WTRR weight is zero, so a fund with ten years of
  history reported 2/3 coverage. Coverage now describes data availability
  only.

### Note
- Not yet wired: the optimiser's score-driven swap pass, the preset
  picker in the Optimizer tab, and the score columns in the pre-loaded
  list and fund detail page. Backend and API only.

---

## [0.34.2] — 2026-07-26

### Added
- Breakdown CSV upload asks about the other categories in the file. A CSV
  covering asset class, sector, country and currency uploaded from the
  Sector card left the other three cards on their previous source, so the
  data looked like it had not been uploaded.
  It had been: the commit has always persisted *every* facet the file
  contained — `uploaded_breakdowns_put(isin, facets_out)` — and what was
  facet-specific was only which card got switched over to the uploaded
  source. A new stage after commit lists the other covered categories,
  pre-ticked, and switches the ones the user keeps. The wording says the
  data is stored either way, because it is.
- `BD_FACETS` constant replacing four inlined copies of the breakdown
  facet list.

---

## [0.34.1] — 2026-07-26

### Changed
- A fund sold out entirely is now removed from the portfolio rather than
  kept at zero shares. The old behaviour was justified as keeping it "a
  candidate the optimiser can buy back into", which was wrong about where
  candidacy comes from: candidates are drawn from the pre-loaded fund
  list, not from the portfolio, so a removed fund is exactly as buyable
  as before. All the zero-shares row bought was a holding of nothing,
  cluttering every table that listed it. Partial sales are unaffected.
- Optimiser tolerance inputs are built from the facets that actually
  carry targets, instead of four hardcoded boxes. A tolerance on an
  untargeted facet has nothing to apply to — the backend ignores it, and
  asking for it implies the optimiser will do something it cannot. With
  no targets at all, the grid is replaced by a note saying so.
- Targets can be edited from the Optimizer tab: an "Edit targets" button
  opening the existing editor, plus a summary of which categories are
  targeted. Saving refreshes the tolerance inputs in place.

### Fixed
- `openTargetsEditor` seeded its working state from a hardcoded list of
  four facets, so any target set on `market_cap` or `style_box` — both
  targetable since 0.30.0 — was absent from the editor and silently wiped
  by the next save. Found while making the tolerance inputs dynamic.
- The same two facets had no tolerance input at all, so they always ran
  at the backend default rather than anything the user could set.

---

## [0.34.0] — 2026-07-26

### Added
- **Swap refinement after greedy selection.** Greedy forward selection is
  myopic — a fund chosen in round one can never be un-chosen, even once
  later picks make it redundant. With targets of 50% NA / 30% Europe /
  20% Japan and `max_funds=3`, greedy spent its first slot on a WORLD
  fund and returned WORLD + JAPAN + EURO at 2.19pp; the optimal three
  (SP500 + JAPAN + EURO) sit at 0.00pp and greedy could not reach them.
  After greedy converges, `_swap_refine` now tries exchanging each
  selected fund for an unselected one and keeps any exchange that lowers
  the residual, repeating until no improvement is found. Local search:
  no global guarantee, but it removes exactly that failure. The
  `max_funds=3` case above now returns the optimal answer.
- `swaps` on the optimiser result — how many exchanges were applied.

### Changed
- Screening precision for search solves. The hundreds of throwaway fits
  that rank candidates run at 150 FISTA iterations rather than 500; the
  chosen set is always re-solved exactly, so nothing reported inherits
  the screening tolerance.
- Warm starts: each trial differs from the incumbent by one column, so
  the previous solution is used as the starting point.
- The swap search shortlists incoming candidates by how well their column
  aligns with the current residual (the matching-pursuit screening step),
  trying the best 8 rather than all ~30. A fund outside the shortlist
  could in principle make a good swap, but the alignment score is the
  first-order estimate of how much a column can reduce the residual, so
  what it discards has least to offer.
- Net effect on a 35-fund, 3-facet problem: 12.2s -> 2.3s, with the
  solution quality unchanged (same funds, same 6.54pp worst deviation).
  Still fully deterministic.

### Fixed
- Screening precision initially made the result *worse* — `max_funds=8`
  returned 5 funds at 7.28pp where full precision found 6 at 6.54pp.
  Cause: screening-precision challengers were compared against an
  exact-precision incumbent, and a screening fit sits ~4e-5 above its
  converged value, so genuine improvements smaller than that read as "no
  fund helps" and stopped both searches early. Incumbent and challenger
  are now scored at the same precision.

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
