# PorxPy

*Current as of v0.103.1. This is the fullest architecture write-up;
check the stamp against `porxpy/__init__.py` before trusting a claim.*

**Portfolio X-ray Python** — a self-hosted tool for analysing the
true exposure of an investment portfolio across funds, ETFs, sectors,
countries, currencies, and asset classes.

PorxPy runs locally on your own machine. Your portfolio data never
leaves it. The only external traffic is read-only lookups against
public market-data sources (Yahoo Finance, OpenFIGI, optionally
justETF).

---

## The documentation, and which document answers what

Six documents, each owning one question, so nothing is explained twice
and there is one place to correct when something changes. In reading
order for a new user:

| Document | Answers | Read it when |
|---|---|---|
| **[GETTING_STARTED.md](GETTING_STARTED.md)** | How do I install PorxPy, load the fund set that ships with it, and design a first portfolio? | First. It is the only place the installation procedure lives, and it runs end to end: install, import `PreLoadedFunds/porxpy_funds.zip`, create a portfolio, give it cash, set targets, run the optimiser, apply the trades. |
| **[IMPORT_NEW_FUNDS_GUIDE.md](IMPORT_NEW_FUNDS_GUIDE.md)** | How do I add a fund the shipped set does not have, and get it fully described? | When you want your own funds in the cache. ISIN and ticker imports, holdings uploads, factsheets and their extraction, §11b's full account of how enrichment identifies and fills a holding, the Edit fund dialog, resolving unmatched values, and what every tile means. |
| **README.md** (this file) | What is PorxPy, what does it do, and how is it built? | For the feature tour and the architecture — module roles, the cache layout, the request flow, external services, privacy. |
| **[FACET_TREE.md](FACET_TREE.md)** | How do facets actually work? | When a breakdown reads oddly, or before changing anything that touches one. The four trees, why every level travels together, what `unknown` means against `n/a`, what is deliberately *not* a facet, and §17's verified defects. |
| **[OPTIMIZER.md](OPTIMIZER.md)** | How does the solver decide? | When a design surprises you. The exposure matrix, why cash is a reservation rather than a column, greedy selection and swap refinement, how tolerances weight the objective, and §13's verified defects. |
| **[CHANGELOG.md](CHANGELOG.md)** · **[WISHLIST.md](WISHLIST.md)** | What changed, and what might change? | The changelog explains the cause behind each release, not just the symptom. The wishlist is deferred ideas — deliberately not a defect list; defects live in the owning document's *Known open issues*. |

The two guides are the *user* path — how to work the app. The three
reference documents are the *why* path — the reasoning behind the
behaviour, kept because that is the part that does not age. Each carries
a version stamp naming the release it describes; check it against
`porxpy/__init__.py` before trusting a claim.

---

## What it does for you

### See what you actually own

If you hold three ETFs and a couple of single stocks, your real
exposure is hidden inside the funds. A "global equity" ETF and an
"S&P 500" ETF look distinct on paper but overlap by 60% in the same
US large-caps. PorxPy unpacks every fund into its underlying holdings,
weights them by your allocation, and shows you the merged reality:

- Which companies do I really own, and how much?
- Where in the world is my money invested?
- Which sectors am I overexposed to?
- What currencies am I really running?
- How much is equity vs fixed income vs cash, after looking through
  every fund?

Cash gets a rule of its own, because "cash" means two things. Money in
your own accounts is a **position** — it lives on the Cash sub-tab, and
it is not listed among your funds or among the funds' holdings, because
no fund holds it. But it is still your money, so in the X-ray it is added
to the funds' own cash sleeves and answers as one number: separate by
position, merged by exposure. The portfolio overview states all three
figures with their shares of the total — funds, cash, and the whole.

Every other percentage in the app is a share of the **fund side**. You
say how much cash to keep as an amount ("50,000 stays liquid"), it is
reserved before anything else, and the funds list, the holdings table and
every target then describe what is left.

The portfolio's sub-tabs are grouped by what they answer — what you hold
(Funds, Holdings, Cash), how it has behaved and where it is exposed
(History, X-ray), and what you want and how to get there (Targets,
Optimizer). The middle pair carries an **Include cash held by me**
checkbox, because it is the only pair the answer genuinely changes for.

### Read every facet at the grain you're asking about

"What sector is this?" and "where is this?" are not single questions, so
they are not single numbers. Each is a tree, and the same breakdown is
available at every level of it:

- **Sector** — sub-sector → sector → super-sector. Semiconductors,
  technology, cyclicals. A semiconductor fund is not a technology fund,
  and asking whether you are overweight "tech" means something
  different at each of the three.
- **Country** — country → region → super-region. Japan, Asia Developed,
  Developed Markets. "How much of me is in emerging markets?" is a
  super-region question; "how much in India?" is a country one.
- **Asset** — sub-class → asset class → super class. Government bond,
  government, fixed income. "How much fixed income do I hold?" and "how
  much of that is government paper?" are two different questions, and
  a holdings file that says only "bond" answers the coarse one without
  pretending to answer the fine one.

The level selector sits on the breakdown cards — on the fund page and
on Portfolio → X-ray alike — and on both holdings tables, the fund
page's and Portfolio → Holdings. Every level is computed independently
rather than by rolling the finest one up. A fund that only publishes
region-level data still contributes to the region distribution instead
of being lost in it — and a holdings file that says "Europe ex-UK"
answers at region level rather than being flagged as an unrecognised
country.

### Design a portfolio, don't just measure one

Set your targets, and PorxPy will propose the trades that get you there:
which funds to buy, which to sell, and how much. It works from the funds
you have already loaded, cannot overdraw your cash, and shows the residual
error per facet so you can see exactly where the design still misses and
decide whether to relax a target or go find another fund.

Any fund can be opted out with the **incl** checkbox in the pre-loaded
list. A holding you have opted out of is left alone — but its exposure
still counts toward your targets, so the optimiser designs around it
rather than pretending it isn't there.

Alongside the design, the optimiser prices every **better-scoring
alternative** to each fund it chose: swap the peer in, re-solve the
weights, and report what the substitution actually costs — *"this fund
scores 12, its peer scores 95, taking it costs you 1.2pp of country
accuracy"*. Nothing is applied. An optimiser that spent an error budget
automatically would be guessing at how much accuracy you are willing to
trade, which varies per portfolio and is exactly the judgement you are
best placed to make. Alternatives that break a tolerance are shown and
flagged rather than hidden, and each is priced against the same baseline
independently — so accepting two does not cost the sum of their two
prices, and the combined result is recomputed before anything is
applied.

### Compare against your own targets

You set target allocations per facet (e.g. "40% North America, 25%
Europe, 20% Asia, 15% emerging markets"). PorxPy compares your
portfolio against them and shows signed deviation bars — green for
overweight, red for underweight — so you can see at a glance where
you're off your plan.

A target names a **level** as well as a bucket, so "25% Europe" at
region level and "10% Germany" at country level are two different
statements and are fitted at their own grain. Where both are set, a
parent is held to at least the sum of its targeted children — that rule
is enforced when you save, not discovered later as an unmeetable brief.

One setting is not a target at all. **Cash held by me** is an amount, not
a percentage, and it is reserved before anything is designed: the
optimiser leaves exactly that much in your own accounts, selling
positions to raise it if you currently hold less and investing the
difference if you hold more. Every percentage target is then a share of
what remains — reserve 50,000 of 100,000 and "50% equity" means 25,000.

Because targets nest that way, the per-facet total shown while you edit
them, and on the Targets tab, is **what they commit**, not the levels
added together: every target is rolled up into the bucket that contains
it and counted once, at the coarsest level. Targeting semiconductors 15%
inside technology 35% commits 35%, not 50%. So the figure stays on a
scale where 100% means the whole category is spoken for, and a total
above it is a genuine over-commitment rather than an artefact of
counting a sub-sector twice.

Targets come in two groups. **Exposure** — asset class, sector, country,
currency — is measured by looking through your funds to what they
actually hold. **Style** — market cap and equity style — is a
classification of each fund as a whole, so a fund nobody has classified
shows up as "unknown" rather than being quietly dropped from the
denominator. Both groups are targetable; "unknown" is not, because it is
a gap in the data rather than something you can aim for.

### Rank the funds, not just the portfolio

Two funds can supply the same exposure at very different cost. PorxPy
scores every fund in your pre-loaded set on three components — TER, fund
size, and trailing returns — as **percentiles**, so a cost in fractions
of a percent and a size in billions can be combined without inventing an
exchange rate between them. Fund size is a floor test rather than a
percentile: past "big enough not to be at risk of closure", more of it
is not better.

Each fund carries two scores:

- **Overall** — against the whole universe. Answers "is this a good
  fund", and is what the fund list and detail page show.
- **Peer** — against funds with the same asset class and focus. Answers
  "is this the best fund *for this job*", which is the only question
  worth asking when the optimiser holds a European bond fund because the
  targets demand one. Ranked globally, bond funds would sit at the
  bottom of any returns-weighted score permanently — not because they
  are bad but because they are bonds — and offering to replace one with
  a high-scoring US equity tracker would be answering a question nobody
  asked. The optimiser's alternatives table reads the peer score and
  nothing else.

Three weight presets ship in Settings — **cost driven** (the default),
**cost and returns**, and **returns driven** — and each score travels
with its coverage, so a high score computed from one component out of
three is visibly thin. A peer group of one fund yields no peer score at
all — there is no ordering inside a group of one, so any number would be
invented — but a group of **two** is ranked: one of the pair does outrank
the other, and refusing to say so left those funds unrated and invisible
to the optimiser.

Percentiles are the plotting position `rank / (n + 1)`, counting from the
worst. A pair scores 33 and 67, a trio 25/50/75, the whole 51-fund
universe 2 through 98. That is why nothing ever scores exactly 0 or 100
on a ranked component: nothing is best-in-class against a field that
might yet grow, and the alternative — stretching whatever field is to
hand across the full range — declared one of two funds perfect and the
other worthless on the strength of a basis point between them. The size
component is a floor test rather than a ranking, so it still scores
exactly 0 or 100.

Every place a score appears also names the model it was computed under —
the fund list's column header, the fund page's Score row, the peer lists,
and the optimiser's trade and alternatives tables. A rank means nothing
without the weights it was taken over: the same fund is 96 under Cost
driven and 12 under Returns driven, and a bare 12 reads as a bad fund
rather than as one the chosen model does not reward.

The peer group is visible from both places a score is — the fund list's
Peers column and the fund page's Score row — as ISIN, name and score in
three columns, ranked best first. Either list carries the picker for the
weight model, and the model is one setting: changing it in one place
re-ranks the other, since two lists of the same funds under different
weightings would be two answers to one question. The optimiser's own
quality picker is separate on purpose — that model is part of a run, not
a way of looking at a list — which is why its tables are captioned with
the model that run actually used, not with whatever the picker reads now.

The fund price chart can also overlay every peer's price series —
indexed to 100 at the left edge of the window, since two funds priced at
12 and 480 tell you nothing side by side.

### See at a glance whether the data is good enough

Every fund carries five dots — **fund data · asset · sector · country ·
currency** — in the pre-loaded list, the portfolio funds table and the
fund page header. Colour runs red → orange → yellow → green, so one look
down the column finds the weak facet and one look across a row finds the
weak fund. The column sorts by the **worst** dot first: a fund with four
greens and a red is a fund with a red.

The four facet dots are that facet's coverage — how much of the fund's
exposure its chosen source can actually place, with `unknown` counting
against it and `n/a` counting as answered, because a cash sleeve has no
sector and never will.

**A solid dot was measured; a hollow one was reported.** Solid means the
number was counted from actual positions (a holdings look-through or an
upload). Hollow means the issuer published it, or you asserted that a
partial source covers the whole fund. Both can read 100%, and they are
not the same claim — so the difference stays visible rather than being
averaged away.

**The fund-data dot is not a completeness score.** It answers whether the
optimiser can rate this fund, which turns out to be a different question:
a fund can have every field filled in and still be unrankable. Three
states, three different things to do about them:

| | meaning | what fixes it |
|---|---|---|
| red | a rating input is missing — TER, size, or focus | supply it: a factsheet, or your own value |
| orange | inputs are all present, but too few peers to rank against | load more similar funds, or broaden the focus |
| green | the optimiser can rank it | nothing |

Orange is the one worth knowing about, because its fix is the
counter-intuitive one. Peer groups are `primary asset class × focus ×
focus detail`, and a group needs three members before anyone in it can be
ranked. An over-specific focus detail splits the universe into groups of
one — so *narrowing* a fund's description can be what makes it
unrankable, and broadening it is the repair.

**How to read a dot is on the dot.** Hovering any dot gives that dot's
own reading — "Sector — 82% at sector · counted from holdings" — followed
by the whole scale: what each colour means, and what solid and hollow
mean. The column headers carry the same text, and the fund page prints
the two-line version under its labelled block, since that is the screen
where you act on a dot.

**The dots follow what you change.** Switching a card's breakdown source,
declaring a facet complete, editing a fund's focus, uploading or matching
holdings, adding an alias in the Resolve dialog, or adding and deleting
funds all move a dot — and all of them repaint it immediately, along with
the score column and the peer list, wherever those are on screen. Peer
groups in particular are a property of the *universe* rather than of one
fund, so loading a fund can take its peers from orange to green without
anything about them having changed.

### Manage funds and portfolios

- Add funds by ticker or ISIN. PorxPy fetches profile, price history,
  and holdings from Yahoo Finance.
- Group funds into one or more portfolios, with shares (or units) held
  per fund, plus cash positions.
- See the same company once across every fund that holds it, on
  **Portfolio → Holdings**. The **Funds** column says how many of your
  funds hold each merged position and opens to name them, so a row you
  did not expect to be that large can be traced back to where it came
  from without filtering the funds table one fund at a time.
- Read the **bond metadata** an issuer's holdings file carries — duration,
  maturity, coupon, effective date and credit rating — on import, in both
  holdings tables, and merged across funds at portfolio level. Ratings
  sort by credit standing rather than alphabetically, and both agencies'
  scales are understood (BBB- and Baa3 are the same rung).
- Upload full holdings CSVs or Excel files when the fund issuer
  publishes them (top-10 from Yahoo is often not enough for a real
  X-ray). Yahoo can fill the columns the file leaves blank, during the
  upload or afterwards from the holdings tile — the same pass either
  way, identifying each holding by its **ISIN first, then ticker, then
  CUSIP, and only then its name**, because an ISIN names one security
  where a name is a guess. Nothing the file supplied is overwritten, with
  two deliberate exceptions: an identifier column that is malformed is
  replaced with the real one (a valid one never is), and where an
  identifier resolved the holding,
  Yahoo's name replaces the issuer's, so "APPLE INC COMMON STOCK
  USD0.00001" becomes "Apple Inc." A name-search match never rewrites the
  name it searched on.
  Tick rows in the holdings table to enrich only those.
  [IMPORT_NEW_FUNDS_GUIDE.md §11b](IMPORT_NEW_FUNDS_GUIDE.md#11b-how-enrichment-works)
  is the full account: what is filled and what is never touched, the
  resolution chain in order, what counts as a *refinement* of a value
  the file already gave, and what is cached between runs.
- Hold **three position lists per fund at once** — Yahoo's top-10, the
  table read off the factsheet, and your uploaded file — and choose
  which one the fund shows. The choice is saved with the fund, so every
  portfolio holding it and the optimiser use the list you picked; an
  upload no longer destroys what Yahoo said, and removing it takes you
  back rather than leaving you with an empty table.
- Upload the issuer's **factsheet** for funds Yahoo covers badly, and
  pin any breakdown card to it. Optionally, let the AI helper read the
  document and stage the fields, breakdowns and positions it finds —
  every value carrying the page and verbatim quote it came from, and
  nothing applied until you say so.
- Derive the **currency card from the country card**, for the many funds
  whose issuer publishes a geographic split and no currency split at all.
  "From country" converts the country card country by country to each
  country's primary currency, and then follows it: an unknown slice there
  stays unknown here, and marking the country card coverage-complete
  marks the derived currency card complete too. Not the reverse — a
  currency does not name a country, so country-from-currency would invent
  detail rather than convert it, and the currency card's own
  completeness assertion says nothing about country.
- Tell a breakdown card that its source **covers the whole fund**, when
  the breakdown rests on a partial holdings list and nothing better
  exists. The unknown slice is dropped and the rest scaled up, and the
  assertion is saved with the fund — so the portfolio X-ray, the target
  deviations and the optimiser all read the fund as fully described,
  which an "unknown" slice never lets them do. The badge still says
  `ASSUMED`, so nobody mistakes the assertion for a measurement.
- See where every field's value actually came from. A source is
  recorded where a value is produced, never assumed: a fund class
  worked out from words in the fund's name says "inferred from name"
  rather than claiming to be Yahoo data, and a field nobody has a
  source for says nothing at all rather than guessing.
- Override anything that's wrong at the source — asset class,
  replication method, breakdown source, TER, total net assets — and the
  override sticks across every portfolio that holds the fund and
  survives every refetch. Overrides are a view over the fetched data,
  never a mutation of it, so clearing one restores the original value
  without a round-trip.

### Handle messy real-world data

Holdings files from different issuers use different ticker conventions
— Bloomberg-spaced (`AIR FP`), Bloomberg dot-separated (`AIR.FP`),
Refinitiv suffixes, concatenated country codes (`PLTRUS`). PorxPy
resolves them through a fallback chain so you don't have to clean the
file before uploading:

1. **Variant probing** — generate plausible Yahoo equivalents and try
   them in order.
2. **ISIN country prefix** — map the first two characters of the ISIN
   (e.g. `FR`) to the Yahoo exchange suffix (`.PA`) and try the bare
   ticker with that suffix.
3. **Identifier search** — pass the CUSIP or ISIN directly to Yahoo's
   search endpoint.
4. **Name search** — search Yahoo by security name and match results
   against the first characters of the raw ticker.

Once resolved, the canonical ticker, ISIN, and CUSIP are all written
back to the holding row so you have clean data going forward.

### Say "we couldn't place this" instead of guessing

A facet value that doesn't resolve against its resource file is **not** a
bucket. It counts as `unknown`, and the raw text is kept and shown
beneath the slice — *of which unrecognised: "Diversified Holdings" 6%*.
The alternative, which PorxPy used to do, was to let the raw text be the
bucket key, so an unrecognised sector became a slice called "Diversified
Holdings" — conflating *this fund holds 8% of that thing* with *the
source said something we could not place*, when only the first belongs in
a distribution.

Every facet also distinguishes two residuals: `unknown` is a gap somebody
could close, `n/a` means the question does not apply. One bucket could
not say which, and the reader wants to know whether better data would fix
it.

In the holdings tables the same distinction is drawn a row at a time.
`—` means this row says nothing about this facet; `unknown` means it says
something, just not at the grain you are currently looking at. A single
column could not tell you which, so switching level looked like data
appearing and disappearing.

The **Resolve unmatched values** dialog lists what didn't resolve,
sorted by weight, with a row per distinct value across every fund and
every source. Tell it what a value means once and the alias is written
to the resource file; every occurrence everywhere resolves on the next
read, including a factsheet extracted last month — resolution happens at
derivation time, so nothing is rewritten and no migration is needed.

### Back up your work, or hand it to someone else

Building a usable pre-loaded set — a hundred-odd funds with holdings,
factsheets, corrected structure fields and per-facet source pins — is
days of work. Two bundle types, deliberately kept separate:

- **Funds** — the curated asset. What a fund *is* and what was
  established about it, including factsheets and resource files.
  Portable between installs and between users.
- **Portfolios** — what *you* hold: portfolios, targets, cash and
  settings. Personal, and restored on top of whatever fund set is
  present.

Mixing them would mean you cannot take someone else's fund research
without also taking their holdings.

Both are plain zips with a readable `manifest.json`, so a failed import
can be diagnosed by opening the file. Import is two-phase: the bundle is
inspected against your install first and nothing is written until you
have seen the conflict table, where overwriting states its cost inline
(*"replaces 3 of your edits"*) at the moment of choosing rather than
afterwards.

---

## High-level architecture

PorxPy is a small Flask application with a single-file vanilla-JS
frontend. There is no database; everything lives in JSON files at the
project root or under `cache/`. There is no framework on the client
side either — just one HTML file with embedded JavaScript.

### Code layout

```
main.py               Entry point — starts Flask on 0.0.0.0:5000
porxpy/
  app.py              Flask app factory and all HTTP/API routes
  config.py           Paths, constants, TTLs, facet/level tables,
                      the overridable-field registry
  extractors.py       Yahoo Finance fetching and per-holding enrichment
  resolver.py         Ticker variant generation and resolution chain
  breakdowns.py       Holdings roll-up → per-facet levelled breakdown
  targets.py          Target-vs-actual deviation computation
  scoring.py          Fund scoring and peer groups (cost / size / returns)
  ai.py               Factsheet extraction via the Anthropic API (opt-in)
  optimizer.py        Greedy portfolio design against exposure targets
  trades.py           Atomic trade execution (cash ↔ fund positions)
  upload.py           Holdings file parsing, column mapping, enrichment
  bundles.py          Export/import of fund sets and portfolio backups
  utils.py            Cache I/O, portfolio data, coercion helpers
  resources.py        Reference-data loading and facet-value resolution
  yf_session.py       Yahoo transport: TLS impersonation pinning and the
                      trust store (see "TLS interception" below)
fund_explorer.html    Single-file frontend (HTML + JS, no framework)
resources/            Reference CSVs (shipped with the project)
  Geography_definitions.csv    country → region → super-region
  Sector_definitions.csv       sub-sector → sector → super-sector
  Asset_definitions.csv        sub-class → asset class → super class
  Currency_definitions.csv     ISO-4217 currencies
  Primary_asset_class_definitions.csv  what kind of fund this IS
```

`Asset_definitions.csv` and `Primary_asset_class_definitions.csv` are
close in name and answer different questions. The first is the
vocabulary the asset BREAKDOWN rolls up to — a distribution over a
fund's holdings, at three grains. The second is a single CLASSIFICATION
of the fund itself, captured from Yahoo, a factsheet, justETF or the
user, and never derived from its holdings. A 60/40 fund is `mixed` in
the second while its breakdown is roughly 60% equity / 40% fixed income
in the first. Peer-group selection reads the classification.

`Asset_definitions.csv` arrived in 0.70.0 and replaces
`Holdings_class_definitions.csv` (the per-holding class of a row) and
`Fund_class_definitions.csv` (the vocabulary the rollup aggregated to).
Those were two files describing one taxonomy at two grains, and they
disagreed on spelling — `bond` in one, `fixed_income` in the other, for
the same concept. **Both must be deleted from `resources/` when
upgrading.**

The frontend has four tabs — **Explore Funds/ETFs**, **Portfolio** (with
Funds, Cash, History, X-ray, Targets, Holdings and Optimizer views),
**Tools** (source inspector, resource reload) and **Settings**.

### Reference files and facet levels

All five resource CSVs share one hierarchical schema:

```
type,name,description,parent_name,matches,is_default,attrs
```

The delimiter is sniffed per file rather than fixed, so a file saved by
a spreadsheet in a locale that uses `;` is read as-is instead of having
to be converted first.

`parent_name` is singular, which is what makes each tree a chain by
construction rather than by convention. `matches` holds every spelling
that should resolve to the row — alpha-2 and alpha-3 codes, numeric ISO
codes, index names, and non-English labels, since a Dutch factsheet says
*Aandelen* and a holdings file may say `756` for Switzerland.

Which levels exist per facet:

| Facet         | Levels (finest first)                      | Default level |
|---------------|--------------------------------------------|---------------|
| `sector`      | `sub_sector` → `sector` → `super_sector`   | `sector`      |
| `country`     | `country` → `region` → `super_region`      | `country`     |
| `currency`    | `currency`                                 | `currency`    |
| `asset_class` | `sub_class` → `asset_class` → `super_class` | `super_class` |

The `country` facet is fed by `Geography_definitions.csv` — the file
covers the whole tree, so naming it after the finest level would have
been wrong, but the facet keeps the name every consumer already stores.
The same file also carries `focus_group` rows (europe, asia, pacific,
world), which sit outside the chain: they are the pan-regional groupings
a fund *name* can imply but a holding cannot belong to uniquely, which
is exactly why they cannot be levels.

`asset_class` defaults to `super_class` rather than to its own middle
level. Until 0.70.0 the asset vocabulary WAS equity / fixed_income /
cash / other, which in the tree is the super level (today's four are
equity, fixed income, liquid and other); defaulting to the middle one
would have silently moved every existing target, deviation and optimiser
fit to a much finer grain than the one it was set at.

The default level is fixed per facet and never computed from the data:
"deepest available" would make the meaning of a word depend on what
happened to arrive, so uploading a more detailed factsheet for one fund
would silently change the grain its targets are measured at. Non-tree
facets declare a single level of the same shape, so nothing downstream
has to branch on whether a facet has levels.

Editing a resource file needs no version bump — the files are hashed, and
Tools → **Reload resource files** picks up changes without a restart.

### Data layout

```
portfolios.json       Your portfolios (name, funds, shares, cash, targets)
settings.json         App-level settings (enrichment, scoring presets,
                      factsheet staleness, AI on/off)
overrides.json        Per-fund overrides, keyed by ISIN, then by field
                      ({value, source, ts, note} per assertion)
isin_map.json         Cached ISIN → ticker resolutions (from OpenFIGI)
cache/
  factsheets/<isin>.*       Uploaded issuer factsheets + metadata sidecar
  listings/<ticker>.json    Per-listing data (price history, profile,
                            remembered upload column mapping)
  funds/<isin>.json         Per-fund data (holdings, breakdowns, sectors,
                            remembered upload sources)
  _symbol_info.json         Shared per-symbol info cache (HQ country, etc.)
  _symbol_aliases.json      Resolved ticker alias cache
  FX_*.json / FXH_*.json    FX spot and historical rate caches
uploads/              Server-side scratch for in-progress holdings uploads
```

The cache distinguishes between **listing-level** data (ticker-keyed —
price, profile) and **fund-level** data (ISIN-keyed — holdings,
sectors, breakdowns). Two different listings of the same fund (e.g.
the GBp and USD share classes of one ETF) share one fund-level cache
entry, so a holdings upload made against either listing is immediately
visible from both.

Two fund-level slots hold one entry **per source** rather than a single
value: `uploaded_breakdowns` keeps a CSV upload and a factsheet reading
side by side per facet, and (since 0.77.0) `holdings` keeps Yahoo's
top-10, the factsheet's position table and your uploaded file side by
side. In both cases the sources are independent assertions about the
same fund, writing one leaves the others untouched, and which one is in
effect is a fund-level override applied on read — never a property of
the stored data.

The user-data files at the project root (portfolios, settings,
overrides) live outside `cache/` on purpose: cache is "stuff we can
lose without losing user state", and is purgeable. User intent is not.

Factsheets are the exception that proves the rule — they sit under
`cache/` because they are fetched artefacts, but they are never expired
automatically, because nothing else can replace a document the user went
and found.

### Request flow

A typical fund page render looks like:

1. The browser opens `/`, which serves `fund_explorer.html` as static
   content.
2. JavaScript on the page calls `/api/fund?ticker=...`.
3. Flask asks the cache layer for the fund's data; on miss or expiry,
   `extractors.load_fund_data` fetches from Yahoo, applies overrides,
   and writes back to the cache.
4. The JSON response includes profile, price history, the four
   breakdown cards (each from its configured source, each carrying every
   level it has), and the merged holdings list.

A portfolio X-ray goes through `/api/portfolios/<pid>/view` and is
much the same, but adds a final aggregation pass in `breakdowns.py`
that weighs every fund's facet breakdown by its allocation in the
portfolio — once per level, since a fund contributes to each level
independently.

### External services

| Service        | Purpose                                              | When called                          |
|----------------|------------------------------------------------------|--------------------------------------|
| Yahoo Finance  | Fund profile, price history, holdings, FX, search    | Always (the main data source)        |
| OpenFIGI       | ISIN → ticker resolution                             | When adding a fund by ISIN           |
| justETF        | ETF structure (replication, style) — best effort     | Optional, ETFs only, user-confirmed  |
| Anthropic API  | Reading an uploaded issuer factsheet                 | Off by default; only when you ask    |

All responses are cached locally with TTLs that reflect how often the
underlying data actually changes (price: 1 day, sectors and issuer asset
allocation: 7 days, profile: 30 days, fund asset class: 90 days,
ISIN→ticker: 30 days, FX spot: 6 hours, FX history: 24 hours). Holdings
are the exception: they are
never expired on a clock, because a fund's holdings only change when you
ask for them — "Reload fund data" on the fund page, or "Refresh all" on
the portfolio.

### TLS interception (antivirus and corporate proxies)

Software that inspects HTTPS — most antivirus "web shields", most
corporate proxies — does not observe your traffic, it replaces it. It
terminates the connection itself and re-signs the response with a root
certificate of its own, which it installs in the **operating system's**
certificate store. Every browser on the machine keeps working and shows
no warning, because browsers read that store.

Python does not. It ships a fixed list of public certificate
authorities (`certifi`) and knows nothing about the machine's own. So
the moment such a scanner is switched on, every Yahoo call starts
failing with:

```
curl: (60) SSL certificate problem: unable to get local issuer certificate
```

and PorxPy reports it as "Yahoo did not return live data for …", because
from the fetch's point of view that is all it knows.

PorxPy handles this itself: at startup it builds a certificate bundle of
`certifi`'s roots **plus** the roots the operating system already trusts,
and points its Yahoo transport at it (`porxpy/yf_session.py`; the startup
banner logs how many were added). Verification stays on, and nothing is
trusted that the machine did not already trust. Set `PORXPY_CA_BUNDLE` to
a PEM file to supply your own instead.

**Turning the scanning off, if you would rather.** In Avast Free
Antivirus the two controls are in different places:

- *Exclude individual sites* — **☰ Menu ▸ Settings ▸ General ▸
  Exceptions ▸ Add exception**, confirm with **Next ▸ I understand the
  risks**, choose the **Website / Domain** tab, enter the domain (e.g.
  `api.openfigi.com`) and click **Add**.
- *Stop inspecting HTTPS altogether* — **☰ Menu ▸ Settings ▸ Scam
  Guardian ▸ Web Guard**, and untick **Enable HTTPS scanning**. Avast
  then stops re-signing encrypted traffic; the rest of Web Guard's
  protection stays on. (Web Guard was called Web Shield in older
  versions.)

To check whether a host is still being intercepted, look at who issued
its certificate:

```bash
echo | openssl s_client -connect api.openfigi.com:443 -servername api.openfigi.com 2>/dev/null | grep issuer
```

A real certificate authority (DigiCert, Amazon, Let's Encrypt) means the
traffic reaches the site untouched. The scanner's own name there means it
is still in the middle.

**Known limit.** This restores Yahoo, which is where the funds come from,
but not the `requests`-based lookups — OpenFIGI ISIN resolution and
justETF enrichment. Python 3.13 enabled the strict X.509 check
`VERIFY_X509_STRICT` by default, and some scanner roots are malformed in
a way it rejects outright (Avast's, for one, does not mark its basic
constraints critical) no matter which bundle they are offered in. Turning
that check off would relax certificate validation for every connection
the app makes, so PorxPy does not. If you need those two services while a
scanner is active, exclude `api.openfigi.com` and `www.justetf.com` from
its HTTPS scanning, or turn that scanning off.

Individual fields age by **group** rather than by cache category, since
how often something changes is a property of the data and not of where
it happens to be stored: identification effectively never (an ISIN does
not change), structure yearly, operational figures like TER and fund
size quarterly, trading data daily. That is what the field-level
freshness indicators and the per-field re-source controls read.

---

## Requirements

- **Python 3.12 or newer**
- A modern browser (Chrome, Firefox, Safari, Edge — anything from the
  last few years)
- Internet access (for Yahoo Finance / OpenFIGI lookups; once cached,
  PorxPy works offline)

Dependencies (see `requirements.txt`):

- `Flask` — HTTP server
- `Flask-Cors` — CORS support
- `yfinance` — Yahoo Finance client
- `requests` — HTTP for OpenFIGI / justETF / Anthropic
- `pandas` — CSV / Excel parsing in the holdings upload flow
- `openpyxl` — Excel file reading

---

## Getting started

Installing PorxPy, loading the pre-loaded fund set that ships with the
repository, and designing a first portfolio end to end are all covered
in **[GETTING_STARTED.md](GETTING_STARTED.md)** — start there.

The short version: clone the repo, `pip install -r requirements.txt`,
`python main.py`, open <http://127.0.0.1:5000>, and import
`PreLoadedFunds/porxpy_funds.zip` from Settings → backup & restore so
there is a fund universe to work with from the first minute.

For everything about getting *more* funds in — ISIN and ticker imports,
holdings uploads, factsheets, structure fields and scores — see
[IMPORT_NEW_FUNDS_GUIDE.md](IMPORT_NEW_FUNDS_GUIDE.md), and for what the
other documents cover, the map at the top of this file.

---

## Privacy

PorxPy is single-user and self-hosted. Your portfolio data never
leaves your machine. The only outbound traffic is:

- Yahoo Finance — symbol lookups, price data, holdings, FX rates,
  search.
- OpenFIGI — ISIN-to-ticker resolution (public, unauthenticated API).
- justETF — only when you explicitly trigger the structure lookup, and
  only for ETFs by ISIN.
- Anthropic API — only if you switch on the AI helper in Settings, and
  only the factsheet you uploaded. No holdings, no portfolios, no
  positions. Off by default. The API key is entered in Settings and
  stored in `settings.json`, which is gitignored and never committed;
  an `ANTHROPIC_API_KEY` environment variable is used instead when
  Settings holds no key. The key is never sent back to the browser —
  the page only ever sees a masked hint like `sk-ant…WXYZ`
  than stored.

Bundles are files on your disk. Nothing is uploaded anywhere, and a
fund bundle deliberately leaves out the overrides that describe how
*you* use a fund rather than what the fund is. It also strips the
filesystem paths behind remembered uploads: an issuer URL is kept,
because the recipient can fetch from it too, but `C:\Users\you\…` is
inert on their machine and would carry your username and folder layout
along with the research.

There are no telemetry, analytics, or update checks.

---

## Known open issues

Verified defects in the parts of PorxPy this document owns — the app as a
whole, its endpoints and its transport. Deliberate design boundaries are
described where the feature is, not here, and each of the other design
documents keeps the issues of its own subject: `OPTIMIZER.md` §13 for the
solver, `FACET_TREE.md` §17 for the facet trees. Ideas that are merely
absent rather than wrong belong in `WISHLIST.md`.

Nothing is currently open here. The two defects listed at v0.80.0 — a
portfolio view shipping every fund's price history and holdings, and
Flask's debug mode doubling every JSON response — were both fixed in
v0.81.0; see the CHANGELOG for what each was and why the fix sits where
it does.

---

## Version

Current release: **0.103.1** (2026-09-05)

See [CHANGELOG.md](CHANGELOG.md) for the full version history.
