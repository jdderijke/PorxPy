# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Consistency comes first

**Consistency is of the utmost importance in this codebase.** When you change a
data definition, a storage format, a list behaviour in the UI, or anything of
that kind, ALWAYS check whether the same change should also be applied to the
other data elements and UI elements alongside it. Ask, every time: what else is
of this kind, and does it now disagree with what I just changed?

This matters more here than in most projects because so much of PorxPy is
parallel by construction — four breakdown facets, three levels per tree, four
breakdown sources, two cache scopes, the same holdings table rendered on both
the fund page and Portfolio → Holdings. A change made to one member of a set and not its
siblings does not fail loudly; it produces a screen where sector behaves one
way and country another, and nothing says which is intended.

Concretely, before finishing a change, sweep the parallel set it belongs to:

- Change one **facet** → check the other entries in `BREAKDOWN_FACETS`, and
  every level in `FACET_LEVELS` for that facet.
- Change one **level** of a tree → check the finer and coarser levels, and the
  `FACET_DEFAULT_LEVEL` entry.
- Change a **row/storage schema** → check every writer and every reader of that
  shape, plus the resource CSV and any bundle export that carries it.
- Change a **cache category** → check its counterpart scope (listings vs funds)
  and its entry in `DEFAULT_CACHE_CONFIG`.
- Change a **breakdown source** → check the others in `BREAKDOWN_SOURCES`,
  and ask whether the change applies to a *derived* source too
  (`DERIVED_BREAKDOWN_SOURCES` — currency-from-country). A derived source
  exists on one facet only, so `sources_for_facet()` decides which cards
  offer it; a card's `available` map is the different question of whether
  this fund has a source it could have.
- Change a **per-card override** → the two that describe a card travel
  together: `breakdown_source.<facet>` (whose numbers) and
  `breakdown_complete.<facet>` (read them as covering the whole fund).
  Every site that rebuilds cards must read both, or a rebuild silently
  drops the assertion.
- Change a **holdings source** → check the other two in `HOLDINGS_SOURCES`,
  and ask whether the breakdown-card selector needs the same change: the two
  selectors are the same control over the same question and share
  `srcSegHtml` in the frontend.
- Change a **table, filter, sort or level selector** in the frontend → check the
  other table that shares the behaviour; the fund-page and portfolio holdings
  tables are meant to behave identically. Route any in-place re-render through
  `preserveViewState` rather than restoring by hand: a panel rebuilt with
  `innerHTML` loses the reader's scroll position, the field they were typing in
  and their caret, and these panels repaint more than once (scores and quality
  land separately), so a per-caller restore covers only the first repaint.
  Filter state holds what the user TYPED; `searchNeedle` normalises at the
  comparison, because a box re-rendered from normalised state rewrites the
  user's text under the cursor.
- Change a **by-row facet edit** → check both surfaces that write rows: the
  fund-page holdings table and the Resolve dialog's by-row tab. They share
  `ufBuildRowEditContext`, so add to that rather than to one caller.

Naming, so a sweep looks in the right place: **X-ray** is the Portfolio
sub-tab holding the four breakdown cards (`pSubXray`) and has no table of its
own. The aggregated holdings table is a separate sub-tab, `pSubHoldings`,
titled `// portfolio holdings`. There is no such thing as an "X-ray holdings
table".

If a change deliberately applies to only one member of a set, say so in a
comment at the site of the change, so the asymmetry reads as a decision rather
than an oversight.

## Generalise what two or more elements share

The other half of the same rule. When a function is used by two or more elements
of the codebase — two facets, two sources, two cache scopes, two UI tables — see
whether it can be generalised so those elements genuinely share one
implementation, instead of one element getting a near-copy with its own name.

Ask it in both directions:

- **Before writing** a function that resembles an existing one, check whether the
  existing one can take a parameter (the facet, the level, the source, the table
  id) and serve both callers.
- **After changing** a function, check who else does nearly the same thing and
  should now be calling it rather than carrying a diverged copy.

The reason is the consistency rule above: two copies are two places a behaviour
can drift apart, and drift between parallel elements is exactly the failure this
codebase is most exposed to. A single parameterised function makes the shared
behaviour true by construction rather than by everyone remembering to update both
copies. It is also how the existing code is built — `resolve_facet_value`,
`items_at_level` and `_key_at_level` take the facet or level as an argument
rather than existing once per facet.

Generalise where the elements really are the same kind of thing. Where they are
not, keep them separate and say why in a comment — a forced abstraction over two
things that only look alike is its own kind of inconsistency.

## Commands

```bash
# Setup (Python 3.12+)
python -m venv .venv && .venv\Scripts\activate    # Windows; bash: source .venv/bin/activate
pip install -r requirements.txt

# Run — serves the app on 0.0.0.0:5000, Flask debug=True (auto-reload on save)
python main.py                    # then open http://127.0.0.1:5000

# Diagnostic scripts (run from the project root)
python tools/inspect_fund.py TDIV.AS [MORE.TICKERS]   # dump raw Yahoo fields behind TER/size/turnover (network, writes nothing)
python tools/data_coverage.py                          # profile-field coverage across the saved listing cache (no network)
```

The user usually keeps `main.py` running from PyCharm. **Check before
starting another one:** the dev server binds port 5000, and on Windows a
second process can bind the same port without a visible error — so a stray
`python main.py` leaves two instances, requests are answered by whichever
won, and a code change looks like it "didn't take effect" when you are
really talking to a different process. It fails silently and costs rounds
of debugging.

- Is one up, and does it have your change?
  `curl -s http://127.0.0.1:5000/api/meta` returns `version` and
  `build_date` straight from `porxpy/__init__.py`.
- Backend edits: `debug=True` means the stat reloader picks them up. If a
  reload needs nudging, `touch porxpy/app.py`.
- Only start one when nothing answers on that port.

There is **no test suite, linter config, or build step** in this repo. Verification is done by running the app and exercising the affected screen, or by the two `tools/` scripts. `test files/` holds sample CSVs for manually exercising the breakdown-upload flow, not automated tests.

The AI factsheet helper needs `ANTHROPIC_API_KEY` in the environment plus the Settings toggle; it is off by default and the key is never written to `settings.json`.

## Architecture

Flask backend (`porxpy/`) + one 19k-line vanilla-JS file (`fund_explorer.html`) + JSON files on disk. No database, no frontend framework, no ORM. `main.py` is a banner, a legacy-file check, and `create_app()`.

### Module roles

`app.py` (~6.8k lines) holds every route and is a thin HTTP layer by design — parse, delegate, jsonify. Business logic belongs in:

| Module | Owns |
|---|---|
| `config.py` | Paths, TTLs, and the **registries** other modules read: `FACET_LEVELS`, `FACET_DEFAULT_LEVEL`, `BREAKDOWN_FACETS`, `BREAKDOWN_SOURCES`, `DERIVED_BREAKDOWN_SOURCES` (+ `sources_for_facet`), `HOLDINGS_SOURCES` (plus the variant maps `HOLDINGS_VARIANT_SOURCE` / `HOLDINGS_VARIANT_ROLLUP` and their lookups), `ENRICHABLE_FIELDS`, `CACHE_CATEGORIES`, `OVERRIDABLE_FIELDS`, `FIELD_SOURCES`. Does no I/O. |
| `resources.py` | Loads the reference CSVs and resolves any raw string to a canonical facet node at every level (`resolve_sector_tree`, `resolve_country_tree`, `resolve_asset_tree`, `resolve_currency`). Also alias writing and `reload_resources()`. |
| `extractors.py` | Yahoo fetching and per-holding enrichment. `load_fund_data()` is the composition point for `/api/fund` and the portfolio enrichment loop. `enrich_holdings_rows()` is THE holdings-enrichment loop — the fund-page button and the upload commit both call it, and neither may grow a copy. They also ask it the same question: which fields may be filled comes from `utils.enrichment_fields()` alone (v0.100.0), so a caller never carries its own field list. The only thing a caller decides is WHICH ROWS. |
| `resolver.py` | Ticker variant generation and the search helpers behind the resolution chain. The chain itself is ordered in `extractors.get_symbol_info_cached`, by strength of identifier: ISIN → the file's ticker (variants, then the ISIN's country suffix) → CUSIP → the row's country as an exchange hint → name. Documented for users in IMPORT_NEW_FUNDS_GUIDE.md §11b. |
| `breakdowns.py` | Holdings → per-facet levelled breakdown (`rollup_holdings`, `build_fund_breakdowns`), and the portfolio aggregation pass (`aggregate_portfolio_holdings`, `rollup_portfolio_fundlevel`). |
| `utils.py` | Cache I/O (`cache_get`/`cache_put`/`cache_read`/`cache_write`/`cache_purge`), portfolios, settings, overrides, ISIN map. |
| `upload.py` / `bundles.py` / `scoring.py` / `targets.py` / `optimizer.py` / `trades.py` / `ai.py` | Holdings-file parsing; fund/portfolio bundle export-import; percentile scoring and peer groups; target-vs-actual deviation; greedy design solver (`optimise_portfolio`); atomic cash↔position trades; factsheet extraction via the Anthropic API. |

### The four invariants worth knowing before editing

**1. Facets are trees, and every level travels together.** A facet is one thing whose value is a tree — never sibling facets. `FACET_LEVELS` lists levels finest-first (`sector`: `sub_sector`→`sector`→`super_sector`; `country`: `country`→`region`→`super_region`; `asset_class`: `sub_class`→`asset_class`→`super_class`; `currency` has one level of the same shape so no consumer branches on arity). Storage keeps the whole tree **plus the stated level** the source actually asserted. Every endpoint emits every level; the level *selector* is a browser-side view. `FACET_DEFAULT_LEVEL` is fixed per facet and must never be computed from the data — "deepest available" would let one factsheet upload silently change the grain a target is measured at.

**2. The cache splits listing-level from fund-level data.** `cache/listings/<ticker>.json` holds what differs per listing (profile, price history, upload prefs); `cache/funds/<isin>.json` holds properties of the fund itself (holdings, sectors, asset class, asset allocation, uploaded breakdowns). Two share classes of one ETF share the fund file, so a holdings upload against either listing is visible from both. Per-category TTLs live in `DEFAULT_CACHE_CONFIG`; `holdings` and `uploaded_breakdowns` set `manual_refresh_only` — a present entry is always a hit, and only `force=True` bypasses it. Both of those slots are **source-keyed**: they hold one entry per supplying source (`holdings` → yahoo / factsheet / upload, via `utils.holdings_get` / `holdings_put`; `uploaded_breakdowns` → upload / factsheet per facet), so writing one source never overwrites another, and which one is in effect is an override applied on read. Never reach into either slot directly — go through its accessors, or a new source will be invisible to whatever you wrote.

**3. `cache/` is losable; the project-root JSON is not.** `portfolios.json`, `settings.json`, `overrides.json`, `isin_map.json` are user state and are never auto-purged (all four are gitignored). Factsheets under `cache/factsheets/` are the deliberate exception — fetched artefacts, but never expired, because nothing replaces a document the user went and found.

**4. Overrides are a view, not a mutation.** `overrides.json` is keyed by ISIN then field (`{value, source, ts, note}`), applied on read in `load_fund_data`. They are fund-level, so every portfolio holding the ISIN sees them, they survive refetch, and clearing one restores the fetched value with no round-trip. Never write an override back into the cached fetch.

Related: an unresolvable facet value is `unknown` (with the raw text preserved for the "Resolve unmatched values" dialog), never its own bucket — and `unknown` (a closable gap) is distinct from `n/a` (question doesn't apply). Resolution happens at *derivation* time, so adding an alias to a resource CSV fixes every past extraction on the next read with no migration.

### Reference CSVs (`resources/`)

All share one schema: `type,name,description,parent_name,matches,is_default,attrs`. `parent_name` is singular — that is what makes each tree a chain by construction. `matches` carries every spelling that should resolve to the row (alpha-2/alpha-3, numeric ISO, index names, non-English labels). Files are fingerprinted, so Tools → **Reload resource files** picks up edits without a restart.

Current files: `Asset_definitions.csv`, `Geography_definitions.csv`, `Sector_definitions.csv`, `Currency_definitions.csv`, `Primary_asset_class_definitions.csv`.

Two easily-confused things: the **asset tree** (`Asset_definitions.csv`) is the vocabulary a *breakdown* rolls up to — a distribution over holdings. `Primary_asset_class_definitions.csv` is a single classification of *the fund itself*, captured from a source and never derived from holdings. A 60/40 fund is `mixed` in the second while its breakdown is 60/40 in the first. Peer-group selection reads the classification.

### Request flow

`/` serves `fund_explorer.html` statically; the page then calls `/api/*` (the frontend hardcodes `const API = 'http://localhost:5000/api'`). `/api/fund` → cache lookup → on miss `extractors.load_fund_data` fetches, applies overrides, writes back → response carries profile, price history, the four breakdown cards (each from its configured source, each with all its levels) and merged holdings. `/api/portfolios/<pid>/view` is the same plus a `breakdowns.py` aggregation pass that weights each fund's facet breakdown by allocation, once per level.

External services: Yahoo Finance (always), OpenFIGI (ISIN→ticker), justETF (opt-in, ETFs only), Anthropic API (opt-in, factsheets only).

## Conventions

- **Version lives only in `porxpy/__init__.py`** (`NAME`/`VERSION`/`BUILD_DATE`), exposed via `/api/meta`. Bump `0.x.0` for features/refactors, `0.0.x` for fixes (a batch containing both takes the minor bump), and add a matching `CHANGELOG.md` entry (Keep a Changelog-ish, newest first, prose that explains the cause rather than just the symptom). Bump once per batch of work handed over, not once per session — a follow-up round reusing the previous round's number leaves "which build am I running?" unanswerable.
- **A version bump updates EVERY `.md` file.** This is a standing rule, not a
  best-effort. When the version in `porxpy/__init__.py` moves, walk the whole
  set of Markdown files and either update each one or confirm it is still
  correct: `README.md` (top stamp, the `Version` section at the bottom, the
  feature tour, the documentation map), `GETTING_STARTED.md` and
  `IMPORT_NEW_FUNDS_GUIDE.md` (both quote the startup banner, so both carry a
  version number in prose), `FACET_TREE.md` and `OPTIMIZER.md` (their stamps
  *and* their Known open issues sections, re-verified against the running code
  rather than carried forward on trust), `CHANGELOG.md`, `WISHLIST.md` where a
  shipped feature closes a deferred idea, and this file where the change alters
  how the codebase is worked on. The docs are parallel by construction and each
  claims to describe a named release: a stamp that lags does not fail loudly, it
  quietly makes every claim in the file untrustworthy without saying which ones
  went stale. Hand back the list of documents you checked, so the sweep is
  visible rather than assumed.
- **Comments record decisions, including rejected ones.** `config.py` in particular explains why sibling facets were backed out, why there is no geographic super-region, why a key was renamed. When changing something these comments cover, update the rationale rather than deleting it — and match the density: this codebase documents *why*, not *what*.
- **Docstrings are Google-style with an explicit "why this exists" paragraph**, on modules and non-trivial functions alike.
- Backend files are large and single-purpose; add to the existing module that owns the concern rather than creating a new one.

## Design docs

`README.md` (user-facing, also the fullest architecture write-up),
`OPTIMIZER.md` (solver internals) and `FACET_TREE.md` (the four facet
trees: asset, sector, geography, currency — how they behave today, plus
the v0.70.0 design record that produced them). All three carry a version
stamp naming the release they describe; keep it current when you edit
them, and check it against `porxpy/__init__.py` before trusting a claim.

`README.md` opens with a table saying which question each document owns.
When a document is added, renamed or given away part of its subject,
update that table in the same change — it is the only place the split is
stated, and a reader who cannot find `FACET_TREE.md` will not know to
look for it.

The two user-facing guides are `GETTING_STARTED.md` (install, import the
shipped `PreLoadedFunds/porxpy_funds.zip` set, and design a first
portfolio: cash, targets, optimiser) and `IMPORT_NEW_FUNDS_GUIDE.md`
(everything about getting one more fund in and fully described). They
carry the same version stamp as the design docs, and the installation
procedure lives in the first of them alone — README and the import guide
point at it rather than repeating it.

`WISHLIST.md` holds possible future enhancements — things worth doing that
nobody has promised. It is deliberately not a defect list: add an idea there
when it is deferred, and a bug to the owning document's Known open issues
instead.

`OPTIMIZER.md` §13 and `FACET_TREE.md` §17 are **Known open issues** —
verified defects, kept separate from the deliberate design boundaries in
the surrounding sections. Read the relevant one before concluding that
something is broken in a new way. Each document owns the issues of its
own subject; do not copy an issue between them.
