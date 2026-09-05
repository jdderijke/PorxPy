# FACET_TREE.md — the four facet trees

*Current as of v0.102.1. Check the stamp against `porxpy/__init__.py`
before trusting a claim.*

Sections 1–5 describe how the facet trees behave **today**, across all
four facets. Sections 6–16 are the v0.70.0 design record that produced
them, kept because the reasoning is the part that does not age. Section
17 lists what is still wrong.

Status: **complete and installable.** Items 1-4 of the frontend plan
are done; item 5 (upload preview grain) remains and is cosmetic.
v0.70.1 followed with fixes to the holdings tables' header row — the
level chip, the column filters and the sort key were all built once at
load and did not follow a level change. See §17 for issues this release
surfaced but did not cause.

---

## 1. The four trees at a glance

Every facet is ONE thing whose value is a tree. Levels are declared
finest-first in `config.FACET_LEVELS`; a facet with a single level
declares it in exactly the same shape, so no consumer anywhere branches
on whether a facet has levels.

| Facet | Levels (finest first) | Default | Definitions file | Nodes |
|---|---|---|---|---|
| `asset_class` | `sub_class` → `asset_class` → `super_class` | `super_class` | `Asset_definitions.csv` | 26 / 7 / 4 |
| `sector` | `sub_sector` → `sector` → `super_sector` | `sector` | `Sector_definitions.csv` | 51 / 12 / 4 |
| `country` | `country` → `region` → `super_region` | `country` | `Geography_definitions.csv` | 250 / 11 / 2 |
| `currency` | `currency` | `currency` | `Currency_definitions.csv` | 162 |

Counts are of rows in the file, re-counted at v0.97.0, and two of them
need a word. The fourth `super_sector` is `n/a` — the residual for
positions the equity sector question does not apply to, carried as a
row so it has a name and a description rather than being a special case
in code. `Geography_definitions.csv` also holds 4 `focus_group` rows,
which are not a level of the country tree at all: they are pan-regional
labels a *fund* can be focused on, read by peer grouping rather than by
a breakdown.

Four rules hold for all of them.

**Storage holds the whole tree, plus the stated level.** A row records
the value the source asserted and the grain it asserted it at; the other
levels are derived from that pair on every read. Without the stated
level, a re-derivation after a definitions-file edit cannot know which
level to derive *from*, and a derived value becomes indistinguishable
from an asserted one.

**Emission carries every level.** Every endpoint that emits a facet
emits all of its levels. Display picks which to show; no backend decides
for a screen, and no client derives a level it was not given.

**Nothing rolls down.** A value stated at region level answers at region
and super-region and reports `unknown` at country. Deriving a country
from a region would invent detail the source did not give. Rolling *up*
is always safe and always happens.

**Residuals are two words, not one.** `unknown` is a gap someone could
close; `n/a` means the question does not apply.
`config.FACET_NOT_APPLICABLE` decides which one a blank produces: for
`sector` and `country` at every level, a holding whose asset class is
`cash` gets `n/a`, and everything else gets `unknown`. A cash sleeve has
no sector and never will, and scoring that as a gap left cash-heavy
funds permanently short of 100% with nothing anyone could do about it.

### The shared file schema

```
type,name,description,parent_name,matches,is_default,attrs
```

`parent_name` is singular, which is what makes each tree a chain by
construction rather than by convention. `matches` is a `|`-separated
list of every spelling that should resolve to the row.

Since v0.71.0 a `matches` entry may carry a leading and/or trailing `*`,
saying where the alias is allowed to sit inside the incoming value:

| Written in `matches` | Matches when the value … |
|---|---|
| `*treasury bills*`   | contains it anywhere |
| `*treasury bills`    | ends with it |
| `treasury bills*`    | starts with it |
| `treasury bills`     | is exactly it |

Exact beats wildcard, and among wildcards the longest needle wins, so
`government bond` outranks `bond` and the result never depends on row
order in the file. A bare `*` is ignored rather than honoured.

Matching is identical in all four trees: one normaliser, one lookup.
Both the stored alias and the incoming value are reduced by collapsing
every non-alphanumeric run to a single space, so punctuation is a
spelling difference rather than a meaning difference and
`financial-services` reaches `financial services` without anyone
enumerating it. `attrs` is a
`key=value` bag, used today for currency symbols. The delimiter is
sniffed per file: `Asset_definitions.csv`, `Sector_definitions.csv` and
`Primary_asset_class_definitions.csv` are semicolon-delimited, while
`Geography_definitions.csv` and `Currency_definitions.csv` are
comma-delimited. Both are read as-is rather than converted.

Files are fingerprinted, so Tools → **Reload resource files** picks up
an edit without a restart and without a version bump.

---

## 2. Asset

Levels: `sub_class` → `asset_class` → `super_class`. Default:
**`super_class`**.

Super classes are `equity`, `fixed income`, `liquid` and `other`. Asset
classes below them include `shares and options`, `government`,
`corporate`, `debt and claim securities`, `cash`, `crypto` and
`financial products`. Sub classes are the instrument-level leaves —
`regular stock`, `government bond`, `corporate bond`, `money market`,
`interest rate swap`, `mortgage backed`, and so on.

The default is the SUPER level, alone among the trees. Until v0.70.0 the
asset vocabulary *was* equity / fixed_income / cash / other, which in
the tree is the super level; defaulting to the middle one would have
silently moved every stored target, deviation and optimiser fit to a
much finer grain than the one it was set at.

Resolution: `resources.resolve_asset_tree`. Projection between levels:
`resources.asset_key_at_level`.

---

## 3. Sector

Levels: `sub_sector` → `sector` → `super_sector`. Default: **`sector`**.

The middle level is the Morningstar taxonomy — `technology`,
`healthcare`, `financial services`, `energy` and so on — grouped into
three super sectors: `cyclical`, `sensitive`, `defensive`. Sub sectors
are the 50 finer buckets beneath them, such as `semiconductors` under
`technology`.

This is the tree where mixed-grain targets pay off most obviously. A
semiconductor fund is not a technology fund, and asking whether you are
overweight "tech" means something different at each of the three levels.

Resolution: `resources.resolve_sector_tree`. Projection:
`breakdowns.sector_key_at_level`.

---

## 4. Geography

Levels: `country` → `region` → `super_region`. Default: **`country`**.

250 countries roll up to 11 regions, which roll up to two super regions:
`developed` and `emerging`. The facet is named `country` after its
finest level while the file is named after the whole tree — the file
covers all three levels, so naming it after the finest would have been
wrong, but the facet keeps the name every consumer already stores.

**The super level is a development axis, not a geographic one.** There
is deliberately no geographic parent above region: that grouping is not
a partition. `northAmerica`, `latinAmerica` and `africaMiddleEast` have
no geographic parent, while `japan` and `asiaDeveloped` would claim both
`asia` and `pacific`. Rolling up to it would count Japan twice and lose
North America. Development status *is* a partition — every region maps
to exactly one status — so it is the level that exists. See §17 for the
naming confusion this left behind.

The same file also carries four `focus_group` rows — `europe`, `asia`,
`pacific`, `world` — which sit **outside** the chain. They are the
pan-regional groupings a fund's *name* can imply but a holding cannot
belong to uniquely, which is exactly why they cannot be levels. They
seed `focus_type` / `focus_detail`; the country breakdown never consults
them.

Resolution: `resources.resolve_country_tree`. Projection:
`breakdowns.country_key_at_level`.

---

## 5. Currency

One level: `currency`. Default: **`currency`**.

162 rows of ISO-4217 codes with display names, alternate spellings in
`matches` (`yen` → `JPY`, `sterling` → `GBP`, `rmb` → `CNY`) and a
display symbol in `attrs`. `GBp` — the penny, one hundredth of `GBP` —
is a row in its own right, because London quotes ETFs in it and treating
it as `GBP` misprices a holding by a factor of 100.

The file also carries the metals and `XDR`: `XAU` gold, `XAG` silver,
`XPT` platinum, `XPD` palladium. They are not currencies, but they are
what a commodity ETF's holdings are denominated in, and they resolve
through the same path rather than falling out as `unknown`.

Currency is flat because it genuinely is. It declares its single level
in the same shape as the trees, so `_key_at_level` handles it by falling
through to "this facet answers at its own level and nowhere else" rather
than by special-casing it.

Resolution: `resources.resolve_currency`. There is no projection
function; the flat branch of `breakdowns._key_at_level` covers it.

---

## 6. The architectural rule this release exists to serve

Agreed before any code was written, and it governs every decision below.

- **Storage holds the whole tree.** Every level, filled with a value,
  `unknown`, or `n/a`. Not one fact plus derivations.
- **Storage also records the STATED LEVEL.** The grain the source
  actually asserted. Without it, lazy migration cannot know which level
  to re-derive the others from after a definitions-file edit, and a
  derived value becomes indistinguishable from an asserted one.
- **Emission always carries the whole tree.** Every endpoint that emits
  a facet emits every level of it. A single-level facet emits a
  one-level tree of the same shape, so no consumer branches on arity.
- **Display chooses the level.** No backend endpoint decides which
  level a screen shows, and no client derives a level it wasn't given.

Corollary that came up repeatedly: **a source is recorded, never
assumed.** This is why the v0.69.x provenance work happened mid-stream.

---

## 7. Asset is one tree, not two columns

`Asset_definitions.csv` replaces `Holdings_class_definitions.csv` (the
per-holding `asset_class` + `sub_class` pair) and
`Fund_class_definitions.csv` (the breakdown vocabulary the rollup
aggregated to). Those were two files describing one taxonomy at two
grains, disagreeing on spelling — `bond` in one, `fixed_income` in the
other — for the same concept.

Levels, finest first: `sub_class` → `asset_class` → `super_class`.

Row storage: `asset_node` (the stated value) + `asset_level` (its grain)
+ the three derived level columns. `super_class` was added to
`HOLDINGS_ROW_FIELDS`.

### Downward derivation rule

Derive downward **only when a parent has exactly one child**; otherwise
the level below stays `unknown`.

- `equity` has one asset-class child, so naming equity names it too.
- `shares and options` has two sub classes, so the level below is
  honestly unknown.

This is safe *because* the stated level travels with the value: if a
single-child node later gains a sibling, lazy migration re-derives and
those rows correctly become unknown. The fill is a derivation, never a
stored assertion.

**`is_default` means picker preselection only.** It is never written to
storage. Writing it made an invented sub class indistinguishable from
one the source stated — every equity holding carried `shares` that
nobody had asserted.

### Aliases sit on the node whose meaning they carry

`stockPosition` means "this slice is equity", so it lives on the
super-class row. `common stocks` names the leaf, so it lives there.
Recording an alias at the wrong grain makes a row claim a grain its
source never stated.

**Deliberate exception, decided by the user:** `shares`, `share`,
`stock` and `stocks` were moved DOWN onto sub-class `regular stock`
("in 99.9% of cases that's what it is anyway"), to preserve the
sub-class detail on thousands of legacy rows whose stored `shares` was
an `is_default` fill. Known trade-off: a future upload saying "stocks"
records a grain finer than the source stated, and the stated-grain field
will not reveal it as a guess. The collective nouns — `equities`,
`equity securities`, `equity`, `aandelen`, `stockPosition` — stay at
super level.

---

## 8. `primary_asset_class` is a DIFFERENT facet

Not a level of the asset tree, not a rollup of holdings. A single
classification of the fund — captured from Yahoo, a factsheet, justETF
or the user — feeding **peer-group selection** in `scoring.py`.

Vocabulary: equity / fixed_income / cash / mixed / commodity / other,
in `Primary_asset_class_definitions.csv`. Canonical keys unchanged, so
no stored fund data migrates.

Two alias columns, the same split the sector file draws:

- `matches` — spellings of the value itself ("Fixed Income" IS
  `fixed_income`).
- `attrs`' `style_match` — phrases that, in a fund's NAME or category,
  are evidence for it ("gilt" is not a spelling of fixed income; it is a
  reason to think a fund called that holds bonds).

Name phrases match on **word boundaries**, not substrings. `_norm_phrase`
reduces punctuation to spaces, so `s&p` becomes `s p` — and "iShares
Physical" contains "s p" across its word break.

`mixed` and `commodity` exist only here. A *holding* is never mixed.

---

## 8b. Cash the user holds is not a facet either (v0.90.0)

"Is this cash in my own account, or inside a fund I own?" looks at first
like a missing `sub_class` next to `free spendable cash`. It is not, and
the reasoning survives even though the answer changed twice.

**Why not a node.** The distinction returns for *every* cash sub-class —
a deposit you hold versus a deposit a fund holds, collateral versus
collateral — and again for bonds and equities if either is ever held
directly. A node would need a `direct` twin of every node in the tree,
each with its own `matches` spellings, and every holdings import would
have to guess which twin to resolve to when the importer cannot possibly
know: "direct" is not a property of the holding, it is a property of who
holds it.

**Why it must not split the cash bucket.** The X-ray answers "how much of
my money is in cash" with one number covering both your deposits and the
funds' own cash sleeves. A `direct cash` node would pull your deposits
*out* of `asset_class: cash`, which is the opposite of what the breakdown
is for.

**And why it is not a facet either.** v0.89.0 made it one — a `custody`
meta facet, `direct` / `via_fund`, so the optimiser could satisfy a
percentage target on your own cash separately from a fund's sleeve.
v0.90.0 removed it. The requirement was never a percentage: the user
wants to say *"50,000 stays liquid"*, and the optimiser cannot buy a bank
deposit, so as a target it could only ever be approximated — in
competition with every other target. Stated as an amount and reserved off
the top (`cash_reserve` on the portfolio), it is exact, and the facet has
nothing left to do: the reserved money is simply not in the solver's
matrix. See `OPTIMIZER.md` §4b.

The lesson worth keeping is that all three attempts failed the same test
in different places — a node splits the exposure, a facet turns an amount
into a percentage, and only removing the money from the budget expresses
"this is not available to invest".

### Where the cash surfaces sit

| surface | axis | direct cash |
|---|---|---|
| Portfolio → Funds | position | excluded — not a fund |
| Portfolio → Cash | position | the only place it is edited |
| Portfolio → Holdings | fund look-through | excluded — not held by a fund |
| Portfolio → X-ray | exposure | **merged** with the funds' cash sleeves, unless the reader unticks "include cash held by me" |
| Portfolio → History | exposure | same toggle, same default |
| Targets / optimiser | budget | reserved off the top, as an amount; every target is a % of the fund side |

Filtering happens at render and in the budget, **never in the rollup** —
the rollup must keep seeing cash for the X-ray to be right.

The include-cash toggle (v0.91.0) is a **view** choice: it changes which
positions the two exposure views describe, never what any position means,
so it lives in the browser and is remembered per portfolio, exactly like
the facet level selectors. The server ships both rollups with the
portfolio payload — `fundlevel_breakdowns` over everything and
`fundlevel_breakdowns_ex_cash` over the funds alone — so flipping it
costs no round-trip. The Targets tab always reads the second one, because
since v0.90.0 a target describes the fund side by definition.

### One denominator rule

Every percentage in the app is a share of the **fund side** — the funds
list, the portfolio holdings table, every target. The single exception is
the portfolio overview's own split (`Funds 98.5% + Cash 1.5% = Total`),
which is a share of the grand total because that is the quantity it is
describing.

---

## 8c. A credit rating is not a facet either (v0.93.0)

Bond holdings carry a rating — `quality` in the row schema, shown as the
**Rating** column — and it looks like a facet from a distance: a small
vocabulary, ordered, arriving as a raw string from an issuer's file. It
is deliberately not one, and the difference is worth stating because it
is the same boundary §8 and §8b draw from the other side.

A facet is a *tree the app owns*: raw text resolves to a canonical node,
the node carries its ancestors, and every level is derivable. A rating is
**stored exactly as the source wrote it** — "BBB-", "Baa3" and "AA (sf)"
all survive verbatim — because rewriting Moody's spellings into S&P's on
ingest would silently restate what the issuer's document said, which is
the one thing a holdings row must never do. There is no canonical node to
resolve to and no parent to derive, so there is nothing for
`normalise_facets` to do with it and nothing for a breakdown to roll it
up into.

What the app does own is the **ordering**, which is a separate question
and did need answering: alphabetical sort puts AA+ before AAA and B
before BBB, which is worse than not sorting because it looks sorted.
`config.credit_quality_rank` reads both agencies' scales onto one ordinal
— Moody's Baa2 *is* S&P's BBB — and strips the decorations that ride
along with a rating without changing it. A spelling the table does not
know ranks as `None` rather than as junk: an unknown rating is a gap in
our table, not a claim about the bond, so it sorts to the end in both
directions alongside the blanks. The scale is served to the frontend with
the rest of the config rather than mirrored in the page, for the same
reason the facet vocabularies are.

So: rating is aggregated across funds (two funds holding the same bond
should agree on it, and the source-rank rule that settles a facet
disagreement settles this one too), it is sortable and filterable in both
holdings tables, and it is editable per row. It is never a breakdown, a
target, or something the optimiser reads.

---

## 9. Defaults and grain

`FACET_LEVELS["asset_class"] = ("sub_class", "asset_class", "super_class")`

`FACET_DEFAULT_LEVEL["asset_class"] = "super_class"` — **not** the
facet's own name. The pre-0.70.0 asset vocabulary WAS equity /
fixed_income / cash / other, which in the tree is the super level.
Defaulting to the middle level would silently move every target,
deviation and optimiser fit to a much finer grain than the one they
were set at.

---

## 10. Stored-target migration (user data — approved mapping)

| Stored key | Becomes | Level |
|---|---|---|
| `equity` | `equity` | super_class |
| `fixed_income` | `fixed income` | super_class |
| `cash` | `cash` | asset_class |
| `other` | `other` | super_class |
| `mixed` | **dropped**, logged | — |
| `commodity` | **dropped**, logged | — |

`cash` maps to the asset-class node rather than its super class
`liquid` to preserve the user's intent; since `liquid` has exactly one
child the two are numerically identical.

Handles both the legacy `{key: pct}` and levelled `{level: {key: pct}}`
shapes, because a portfolio saved between releases can be either.
Idempotent. Drops are logged by name, never silent.

---

## 11. Migration is one-way

`_FACET_MIGRATOR_GENERATION` bumped 3 → 4. Every cached blob re-folds
`asset_class` + `sub_class` into the tree on first read.

**Back up the `funds/` cache before installing 0.70.0.** v0.69.4 does
not understand folded rows.

---

## 12. Unresolved values

Two rules, deliberately different, both settled:

- **A holdings row** keeps its raw text, records no grain, and is
  flagged for the Resolve dialog. `other` is a real classification a
  file can assert, so folding unknowns into it hid every unknown
  spelling.
- **A Yahoo `funds_data.asset_classes` key** still folds to `other` — a
  slice of a fund must land somewhere — but logs once per key per run,
  naming the fix: add it as an alias in `Asset_definitions.csv`.

### Asserting completeness (v0.77.0)

A fund-page breakdown card showing an `unknown` slice offers a
**coverage complete (+N%)** checkbox. It exists for one situation: the
card rests on an incomplete set of holdings — an issuer top-10, a
factsheet's largest positions — and there is no factsheet, no file and no
issuer publication that would close the gap. Ticked, the card's
`unknown` slice is dropped and the identified part scaled up, so the
source's coverage is read as the whole fund.

This is **not** a view. It is stored as a fund-level override
(`breakdown_complete.<facet>`, keyed by ISIN, alongside the card's
source pin) and applied in `build_fund_breakdowns`, so every consumer of
the fund's facet data sees the completed card: the fund page, the
portfolio X-ray, the target deviations, and the optimiser. That is the
reason it must be stored rather than held in the browser — `unknown` is
not something a solver can allocate against, so a fund with a large
unknown slice is not merely incompletely described, it is unusable, and
a control that changed only the picture would not have addressed that.

The rules it obeys:

- **Every level travels together** (§ invariant 1). `assume_complete_block`
  applies the rescale at all of a facet's levels; a level with nothing
  identified is left alone and stays unavailable, because asserting
  completeness cannot conjure a sub-sector split from a card that only
  reaches sector.
- **`n/a` takes no part.** It means the question does not apply to that
  exposure — cash has no sector — so it is not a closable gap, and moving
  exposure into or out of it would state something false in both
  directions. The identified part is scaled to `1 − n/a`.
- **An implicit shortfall is closed too**: issuer fractions summing to
  0.94 with no residual item are scaled up like an explicit `unknown`.
- **The pre-assertion coverage survives** in the block's `identified`
  map, and the card's badge reads `100% ASSUMED` with the real figure in
  its tooltip. The numbers downstream treat the assertion as fact; the
  badge is where a reader can still tell assertion from measurement.

The default remains what § 12 and the `_RESIDUAL_KEYS` comment describe:
a top-10 covering a fifth of a fund describes a fifth of a fund, and
nothing renormalises that away on its own.

---

## 13. Resolve dialog

Three sibling pseudo-facets (`asset_class`, `holding_asset_class`,
`sub_class`) collapsed into one levelled facet. `facet_alias_targets`
returns 36 nodes tagged with their level, finest first — same shape as
sector and country. `add_facet_alias` routes the write to the right row
of one file via the existing `_add_hier_alias`; an invalid level is
refused rather than writing nowhere.

---

## 13b. Picking a node (v0.82.0, corrected in v0.82.1)

Every dialog that picks a canonical node — the holdings editor's four
facets, the Resolve popover, the upload defaults — draws the vocabulary
as the tree it is, replacing one flat `<select>` of every node at every
level. The flat list prefixed each entry with its level (`sub-japan`,
`region-japan`), which named the level without naming the branch, so a
country and the region containing it read as two spellings of one thing.

The component (`facetTreeHtml` / `ftBodyHtml` in `fund_explorer.html`)
takes its structure from the backend and derives none of it:
`facet_alias_targets` returns `parent` per node — the key one level
coarser, `None` for a root — alongside the `path` it already returned.
`parent` is computable from `path` plus the level order, and is stated
anyway for the reason §6 gives: one authority over the shape of a tree.
Currency's single level comes back as 162 roots, and the same renderer
draws it as a flat checklist without branching on arity.

The interaction rules, and why each is what it is:

| rule | reason |
|---|---|
| clicking a node expands it, never selects | browsing and choosing must be different gestures, or a value gets written by looking |
| only the checkbox selects; re-ticking clears | one explicit act per assertion |
| ticking a node ticks its **ancestors** | a claim about a sub-node is a claim about the branch containing it |
| ancestor ticks are outlined, the asserted tick is filled | storage keeps the whole tree **plus the stated level** (§9); drawing derived and asserted alike would hide the half that carries meaning |
| nothing **below** the ticked node is ever ticked | see below |

That last rule is worth stating plainly, because the resource files carry
an `is_default` column that looks like it should apply. It does not.
`is_default` is an **import-side** rule — it decides where a holding
lands when the source file under-specifies its facet. It says nothing
about what a *user* asserted through the interface, so the picker does
not read it. A user who wants the finer node ticks the finer node.

Note also that `is_default` is a third mechanism, distinct from the
single-child descent `resolve_facet_tree` uses to fill `path`. What a
node *resolves to* is a backend question, answered in the resolution
hint under the picker; what the user *ticked* is the assertion. Keeping
those two ideas apart is what keeps the checkmarks honest.

### What the picker opens on (v0.82.1)

The picker presets on the **current** node — the deepest level carrying a
value — and never on the node the import matched. These are routinely
different: the import resolves source text to one node, then places the
row deeper by its own rules (`is_default` among them). Where the
resolution made contact is provenance; the deeper node is what the row
is, and it is what the holdings table already displays, so it is what the
picker must open on and offer to save.

`facetDeepestInChain` walks the chain finest-first and takes the first
populated entry. A row leaves a level blank where its tree does not
reach, so "first populated" is exactly "deepest in force". `unknown` and
`n/a` are skipped — neither is a node to open on. An unmatched row falls
back to its stated-node column, which holds the raw source text the
Resolve dialog exists to fix.

Provenance is still reported where the question is genuinely about the
match: the holdings cell tooltip says "Stated as X at Y level".

Not converted: the cash positions table, which edits facets in table
cells where a drop panel would open inside a scrolling row. Stated in a
comment at that site so the asymmetry reads as a decision.

---

## 14. What is done (staged, verified by running)

Backend only. Verified: app boots, validator clean, 36 nodes served
across three levels, rollup sums to the same total at each level,
targets migrate correctly in both shapes.

- `resources.py` — asset tree loader, `resolve_asset_tree`,
  `asset_key_at_level`, `list_asset_nodes`; both retired loaders and
  their ~15 accessors deleted; one `reload_all_resources`.
- `utils.py` — the fold in `normalise_facets`, generation 4, target
  migration, downward defaulting removed from all three sites.
- `breakdowns.py` — asset joins the levelled facets; `_key_at_level`
  learns the third tree.
- `config.py` — `FACET_LEVELS`, `FACET_DEFAULT_LEVEL`, `ASSET_FP`,
  `PRIMARY_CLASS_FP`; retired path constants removed.
- `extractors.py` — Yahoo allocation on the tree, unresolved keys logged.
- `upload.py` — both asset columns pass through to one resolver.
- `app.py` — endpoints, alias-apply, `_rewrite_asset_node`, and the
  holdings patch writing the NODE for asset.
- `Holdings_class_definitions.csv` and `Fund_class_definitions.csv`
  deleted.

### Second backend pass

The first pass converted asset and left sector and country as they
were. Measured against §1 that is not a finished backend: a holdings
row carried all three asset levels but only one sector level and one
country level, so a table could not have offered a sub-sector or region
view no matter what the frontend did. The claim that the holdings patch
"fixes the long-standing bug where editor edits to sector/country were
silently discarded" was also wrong — it fixed it for asset only, and
`sector_node` and `country_node` appeared nowhere in `app.py`. Verified
by running an actual edit: sector technology → healthcare and country
Germany → France both came back unchanged, with a 200.

- `utils.py` — sector and country folds store every level;
  `sub_sector`, `super_sector`, `region`, `super_region` added to
  `HOLDINGS_ROW_FIELDS`; generation 5; `coerce_cash_position` carries
  non-schema extras so stated values survive a re-read.
- `app.py` — the editable set admits `asset_node`, `sector_node` and
  `country_node`; the asset-only special case is gone, replaced by one
  rule over the three facets.

Verified by running: all fourteen modules import, app boots with 83
routes, a live PATCH lands a super-class asset pick, a sub-sector pick
and a region pick simultaneously with stale derived columns present in
the body and correctly overruled, a generation-4 blob migrates and
re-migration is a no-op, and repeated reads of a cash position are
stable.

---

## 15. Frontend — what was built

1. **`RES_HOLDINGS_CLASSES` retired.** `RES_FACET_NODES` loads all four
   facets from `/api/resources/facet_alias_targets/<facet>`, which
   already served exactly the right shape. Every facet arrives
   level-tagged and identically shaped whatever its depth, so nothing
   branches on arity — currency is a one-level tree, not a special case.

2. **One picker per facet in the editor.** Every node at every level,
   grouped under level headings. Seeds from the STATED node, never a
   derived column: seeding from a derivation would pre-select a grain
   the source never asserted, and saving it unchanged would turn a
   display choice into an assertion. Saves send `<facet>_node` only.

3. **Level chips on both holdings tables.** Asset Class and Sub Class
   collapsed into one Asset column; Sector, Asset and Country each carry
   a `.bkseg` chip row, reusing the breakdown cards' control. Filters
   repopulate per level, sorting follows the displayed level, and search
   spans every level of every tree. Fund and portfolio keep independent
   state.

4. **Chips on the asset breakdown card** — free. `renderFundCardControls`
   builds its segment from the block with no per-facet branch, so
   extending LEVEL_LABELS/LEVEL_TIPS was the whole change.

Two display tokens, deliberately different: `—` means the row says
nothing about this facet; `unknown` (muted italic) means it says
something, just not at the grain on screen. The single-column view
could not tell those apart.

Also done: the cash sub-tab's coupled dropdowns collapsed to one, and
both hardcoded asset enums deleted — the four-value upload default list
and the two-value `[cash, bond]` cash list. Both were second copies of
a taxonomy that lives in a file, and both had drifted: `bond` is not a
node in the tree at all, it is an alias of super class `fixed income`.

### Still open

Folded into §17, so there is one list of open issues rather than two
that can drift apart.

---

## 15b. Original plan (for reference)

Frontend only. Nothing renders until item 1.

1. **Nine `RES_HOLDINGS_CLASSES` call sites** move from
   `{rows, by_asset_class}` to `{levels, nodes}`. Lines ~3003, 3030,
   3088, 4901, 5059, 9115, 9192, 9246, 13252, 13266, 13684.
2. **Editor's paired dropdowns** → one level-tagged picker, sourced from
   `facet_alias_targets`.
3. **Level chips on both holdings tables** (fund page + portfolio) —
   *the original request that started all of this*. Reuse the existing
   `levelSegmentHtml` / `bkseg` control. Cell shows the chosen level,
   `unknown` where the row doesn't reach it, full chain in the tooltip.
   Chip state persists per table, independently.
4. **Chips on the asset breakdown card.**
5. **Upload preview** showing the stated grain, chain in the tooltip —
   deliberately NOT chips: the preview's job is to show what will be
   stored, and `unknown` at a chip-selected level would read as "this
   row won't be stored".

Items 1–3 are the minimum for a releasable 0.70.0.

---

## 16. Working notes for next session

- **Verify by running, not by compiling.** A missing module-level
  function is a `NameError`, not a syntax error — `py_compile` passed
  the whole time I had deleted four shared helpers.
- **Never cut code by comment marker.** The AST-span deletion was safe;
  the marker-based cut that followed swallowed `parse_attrs`,
  `format_attrs`, `_truthy` and `_flag_problem`, all used by the sector
  and geography loaders.
- **Ship the visible half early.** Driving the backend to completion
  first left nothing installable for the whole release.

---

## 17. Known open issues

Defects in the facet-tree machinery, grouped by the tree they belong to.
Every item was re-checked against the running code at **v0.97.0** rather
than carried forward on trust, and against the shipped fund cache where
a claim could be measured instead of reasoned about. That moved three of
them, and v0.98.0 then fixed two: the technology sector aliases and
`Asia ex-Japan` were both resolving to the wrong thing, which is a
quieter defect than the "does not resolve" recorded before, because a
wrong answer never reaches the amber banner. Both are kept below as
records rather than deleted. The asset picker's name filter was closed
in v0.89.0.
Deliberate boundaries are described in the sections above; everything
here is something that should be fixed.

### Fixed in v0.93.1 — the asset card ignored its own default level

`build_fund_breakdowns` replaces a card's derived levels with the
holdings rollup's own where one exists, and it skipped two levels while
doing so: the facet-named level (right — that is where the items came
from) and `block["default_level"]` (wrong, and invisible for three
facets out of four).

`sector`'s default level is `sector` and `country`'s is `country`, so for
them the two conditions name the same level and the second never fires.
`asset_class` is the exception: `FACET_DEFAULT_LEVEL` puts it at
`super_class`, deliberately, because the asset vocabulary's original four
values live at the super level and defaulting finer would re-grain every
target set. So asset_class was the only facet whose default level never
got its native rollup — it was derived from the middle level instead.

That is harmless while the middle level has data and fatal when it does
not. Import a bond file with no asset column and a default of "fixed
income" — a super-class node — and every row states fixed income at
super level with the finer levels honestly blank, because you cannot
derive downward through a node with several children. The card derived
all three levels from a blank middle one, reported 100% unknown, and
struck through every level button, while the holdings table beside it
showed fixed income on all 8,500 rows.

The graft now skips only the level the items came from. Worth keeping as
a record because of its shape: a condition that is a no-op for three
members of a set and a bug for the fourth is invisible in exactly the way
this codebase's parallel structure makes likely — nothing failed, one
facet simply answered a different question from its siblings.

### Fixed in v0.89.0 — cash positions carried no facets at all

Recorded rather than deleted, because both halves failed silently and
the second is a trap any future non-fund position will fall into.

**The defaults were written into derived columns.**
`coerce_cash_position` defaulted a blank `asset_class` to `cash` and a
blank `sector` to `cash and/or derivatives`, then called
`normalise_facets`. But `normalise_facets` resolves from each facet's
**raw** column and blanks every derived column when the raw is empty —
and a cash position has no raw: the user picks a node in the table,
which lands in the node column. So the defaults were written into
`asset_class` and `sector`, which are derived, and were wiped a few
lines later. Every cash position came back with all four facets empty,
contributing nothing to the asset-class breakdown — the one bucket a
deposit most obviously belongs in. The fix seeds each facet's raw column
from what the position states (raw, else node, else level column, else
the cash default), through the `facet_raw_field` / `facet_node_field`
helpers rather than four hardcoded names.

**The synthetic breakdown was not levelled.**
`synth_enriched_for_cash_position` built `{"items": [ … ]}` — a bare
list with no levels. `facet_items` returns a bare list for *every* level
it is asked for, so a deposit stated as `cash` (an `asset_class`-level
node) also answered `cash` at `super_class` level, where the right
answer is `liquid`. The X-ray then drew the user's own deposits and the
funds' cash sleeves as two separate buckets at the same level — the one
thing the cash design says must never happen. Cash positions now go
through `build_facet_block`, the same function a real fund's block comes
from, so the stated node rolls up and down its tree and the two merge.

**A blank currency valued the position at zero.** The FX rate table was
built only from positions that named a currency, so a position with a
blank currency field found no rate — and the missing rate was coerced to
`0.0` with a comment calling that the safe behaviour. It is the opposite:
a zero does not blank a cell, it silently removes the position from the
portfolio total, the X-ray denominator and every target deviation. A
blank currency now means the base currency (the default the optimise
endpoint already applied to the same field), and a genuinely missing rate
is a `valuation_error`, as it is for a fund.

### Geography

**One level, two names: `super_region` and `development`.**
`FACET_LEVELS["country"]` is `("country", "region", "super_region")`, and
`Geography_definitions.csv` carries `super_region` rows whose values are
`developed` and `emerging`. The comment block in `config.py` that
explains the third country level calls it `development` throughout —
*"development is the third country level, added in v0.62.0, and it keeps
country a strict chain: country -> region -> development"*. In
`resources.py` the two names are wired together by assignment:
`REGION_DEVELOPMENT = REGION_SUPER` and
`DEVELOPMENT_KEYS = SUPER_REGION_KEYS`. So the level is a development
axis wearing a geographic name, with a second vocabulary aliased on top
of it. Nothing computes the wrong answer today, but a reader looking for
`development` in `FACET_LEVELS` will not find it, and a reader who finds
`super_region` will reasonably expect a geographic parent above region
and get `developed` / `emerging` instead. Pick one name and retire the
other.

**`country_key_at_level`'s docstring contradicts the code beneath it.**
It reads *"with two levels rather than three. See `FACET_LEVELS` for why
there is no super-region level."* Both halves are false: `FACET_LEVELS`
declares three country levels, and the function body implements
`super_region` explicitly, mapping a region through `REGION_DEVELOPMENT`
to reach it. The docstring describes the design as it stood before the
third level landed, and cites as authority the very constant that
refutes it.

**Multi-region "ex" phrasings still do not resolve.** `Europe ex-UK` was
the example of this and is now **fixed**: the `europeDeveloped` row
carries aliases for the common spellings, so `Europe ex-UK`,
`Europe ex UK`, `EUROPE EX-U.K.`, `Europe excluding UK`,
`MSCI Europe ex UK`, `FTSE Developed Europe ex UK` and the Dutch
`Europa ex VK` all answer at region level. The mapping is exact rather
than approximate, because `unitedKingdom` is its own region in this
taxonomy and `europeDeveloped` therefore already excludes it.

The class of "ex" phrasings that names more than one region is the part
that has no honest answer. `Asia ex-Japan` spans `asiaDeveloped` and
`asiaEmerging`; `World ex-US` spans nearly all of them. Neither is a
single node, so neither can be an alias of one, and inventing a node for
each would put overlapping buckets into a tree whose levels are supposed
to partition. `World ex-US` behaves accordingly: it does not resolve, and
lands in the Resolve-unmatched-values dialog where the user can say what
they meant for that fund.

**Fixed in v0.98.0 — `Asia ex-Japan` resolved to `asiaEmerging`.**
`asia ex japan` was a plain alias on the `asiaEmerging` row of
`Geography_definitions.csv`, so the phrase answered *emerging Asia* at
region level. Asia ex-Japan is not emerging Asia — Korea, Taiwan,
Singapore and Hong Kong are developed in this taxonomy and are exactly
what the phrase keeps. The alias converted "we cannot place this" into a
confident wrong region, and did it silently: an unresolved value reaches
the amber banner and gets a decision, a resolved one never appears again.

Where the phrase actually occurs is the useful part of the answer.
Searching the shipped cache at v0.97.0, every occurrence is in a **fund
name** — *iShares Core MSCI Pacific ex-Japan*, *Vanguard FTSE Developed
Asia Pacific ex Japan*, *Xtrackers MSCI Pacific ex Japan* — and not one
is a value in a country breakdown or a holdings cell. A fund name is not
a breakdown bucket, and it already has a home: `focus_type: geography`
with a `focus_detail`, where `Geography_definitions.csv`'s `focus_group`
rows (`asia`, `pacific`, `europe`, `world`) exist precisely to name a
pan-regional span that is not a level of the country tree. That is where
"Pacific ex-Japan" belongs, and it costs the breakdown nothing.

As a breakdown value the phrase has no honest node, which is the general
rule stated above: it spans `asiaDeveloped` and `asiaEmerging` (and
`australasia` when written as "Pacific"), so no single region holds it
and a new node would overlap the ones it is made of. It resolves to nothing
now and is decided per fund in the Resolve dialog. Moving the alias to
`asiaDeveloped` was considered and rejected: the funds in this cache
carrying the phrase are developed-market funds, so it would have been
the less wrong guess — but still a guess, baked into the vocabulary and
applied to every future fund silently, which is the thing worth avoiding.
The alias was deleted rather than moved: the phrase now resolves to
nothing and reaches the Resolve dialog, which is the honest behaviour for
a span no node covers. `Europe ex-UK` still resolves, because there the
exclusion really does name one node.

### Sector

**`cash` is both a sector and not-applicable, depending on the path.**
`config.FACET_NOT_APPLICABLE` maps `sector` to `{"cash"}`, so a holding
whose asset class is cash and whose sector is blank gets `n/a` — the
question does not apply. But `Sector_definitions.csv` also carries a
twelfth middle-level row, `cash and/or derivatives`, parented under
`defensive`, and `cash` is one of its `matches`. So a holding whose
sector text literally says "cash" resolves to
`{'sector': 'cash and/or derivatives', 'super_sector': 'defensive'}`
instead. The same cash is `n/a` down one path and a defensive sector
down the other, which means a cash sleeve can inflate the defensive
super-sector rather than sitting outside the distribution. The eleven
Morningstar sectors are a taxonomy of operating businesses; cash is not
one, and the twelfth row makes the level mean two things at once.

**Fixed in v0.98.0 — the technology sector's own names resolved to
sub-sectors.** Kept as a record, because it is the clearest instance of
why the stated level is stored at all, and because the second half of
the fix is a rule the next facet will want.

Two spellings an issuer writes to mean *the technology sector* each
resolved to a different sub-sector, and the app recorded that the issuer
had asserted that finer grain. Measured against the shipped fund cache
at v0.97.0:

| Raw value in a sector column | Rows | Was recorded as | What those rows actually are |
|---|---|---|---|
| `IT` | 1,057 | `semiconductors` | ASML, SAP, Infineon, Nokia, Ericsson, Capgemini, Dassault Systèmes |
| `Information Technology` | 319 | `it services` | TSMC, Samsung Electronics, SK Hynix, MediaTek, Hon Hai, Xiaomi, ASE |

The two were very nearly inverted — Capgemini stored as a semiconductor
company, TSMC as a consultancy — and neither was visible: a wrong
sub-sector never reaches the Resolve dialog, because nothing failed.

The language is what made it easy to get wrong. "Information Technology"
does contain "IT", so `it services` looks like its home. But `it
services` is not "IT" in this vocabulary: that row's description is
*"Consulting, outsourcing, and technology services"* — the Accenture /
Capgemini industry — while `technology`'s is *"Software, hardware,
semiconductors, IT services"*, which is the whole sector the issuer
named. `Information Technology` is GICS sector 45's label and `IT` is
that label abbreviated; neither says which industry inside the sector a
holding belongs to. `style_match` was never involved: it is matched
against a **fund's** name and category to find what the fund is focused
on, and is never consulted when a holding's sector cell is resolved.

**The half that is a vocabulary fix.** Those spellings now sit on the
`technology` sector row. The rows re-derive on the next read — sector
`technology`, sub-sector honestly blank — with no re-import, because
resolution happens at derivation time.

**The half that is a rule.** A blank sub-sector is the truthful answer,
but it is not the useful one, and no re-derivation can invent it: the
grain is not in the file. It is in Yahoo, whose `industry` field the app
had never read. Enrichment now offers it as the sector facet's finer
answer, exactly as `sub_class` is offered ahead of `asset_class`, under
two conditions worth stating because they generalise:

- *Finest that resolves.* A finer answer is preferred only where the
  definitions file can place it — about half of Yahoo's ~145 industries
  cannot be placed here yet — so a missing alias costs nothing instead
  of turning a good sector into `unknown`.
- *Refinement, not overwrite.* An occupied cell is not an answered one.
  A value that resolves strictly finer **and** agrees at every level the
  row already states adds grain without changing a claim, and is
  written; anything that contradicts the row is left to the user.

Both rules, with worked examples of what does and does not count as a
refinement, are set out for users in
[IMPORT_NEW_FUNDS_GUIDE.md §11b](IMPORT_NEW_FUNDS_GUIDE.md#11b-how-enrichment-works),
which is where enrichment is documented as a whole.

**Unresolved asset values are invisible to the rollup.** A holdings row
whose asset text does not resolve produces nothing in
`rollup_holdings`'s `unresolved` map — confirmed by feeding it rows with
junk at `asset_class`, `sub_class` and `super_class` in turn, all of
which report nothing, while the same junk in `sector`, `country` or
`currency` is reported. Asset values reach the Resolve dialog only via
the separate per-row `_unmatched_facets` stamp, which is a different
code path with different coverage. So the dialog's asset list is
whatever that stamp happened to catch, not what the fold actually failed
to place, and an asset miss arriving through a path that does not stamp
rows is silently unresolvable. Sector and country do not have this
split. (Distinct from the v0.70.2 fix, which corrected the *name* those
misses were reported under; this is about which misses are reported at
all.)

**`rollup_holdings` emits no `super_sector`.** Its facet tuple is
`("sector", "sub_sector", "currency", "country", "region",
"super_region", "asset_class", "sub_class", "super_class")`. Asset and
country each emit all three of their levels; sector emits two. The
breakdown cards are unaffected, because `build_facet_block` derives the
missing level from the items it is given, so this was left alone rather
than storing a value that is also computed. It remains the one place
where the rule *storage holds the whole tree* is not literally true, and
anything reading the rollup directly rather than through the block
builder will find sector shallower than its siblings.

### Asset

**Fixed in v0.89.0 — `cashAssetOptions` filtered by node NAME.** Kept as
a record because the shape recurs. The cash position picker restricted
its list by testing node *names*, so `bond future` — a derivative, not a
deposit — had to be excluded by name too, and any future node whose name
happened to contain "cash" or "bond" would have been offered whether it
belonged in the branch or not. It is now `cashAssetFilter`, which tests
the node's **branch** against the tree the picker already indexes, so the
restriction follows the taxonomy instead of imitating it. The limitation
was closed rather than moved.

**`futures` means two nodes, and the loader says so.** Verified at
v0.98.0 by starting the app: `Asset_definitions.csv` reports *"match
'futures' is claimed by 'shares and options' and 'stock future'; the
deeper node wins, but one spelling meaning two things is an authoring
mistake"* on every load. Both rows list it — the `shares and options`
asset class alongside `futures contracts`, and the `stock future`
sub-class alongside `share futures` and `stock futures`. The deeper node
wins, so a holding whose asset column says "futures" is recorded as a
stock future specifically, at sub-class grain, which is the same
too-deep claim the sector aliases were making: a file writing "futures"
has not said *which* future. Dropping it from the sub-class row leaves
the coarser, truthful answer. It is one edit, left here rather than
applied because it re-grains stored rows and nobody has asked for it.

Beyond that, the entries recorded under Sector above are about the
rollup rather than the vocabulary.

### Currency

Nothing outstanding. The flat facet is the one that works exactly as
specified, which is mostly a statement about how much less there is to
get wrong when a facet has one level.

It is also the only facet with a **derived source** (v0.86.0):
`from_country` converts the fund's finished country card, country by
country, through `Geography_definitions.csv`'s per-country `currency`
attribute. The registry is `config.DERIVED_BREAKDOWN_SOURCES`; the
conversion is `breakdowns.derive_facet_items`. It reads the country card
at its **country** level only — a region names no single currency — and
passes residuals through unchanged, so an unknown fifth of the country
card is an unknown fifth of the derived currency card. A completeness
assertion on the country card propagates to the derived card and is
reported there as `completed_from: "country"`; the reverse does not
happen and must not be added by symmetry, since nothing about the
country card is computed from currency.

The selector shows the button on the currency card alone. A card's block
carries `sources_applicable` (which sources exist for this facet, from
`config.sources_for_facet`) beside `available` (which of them this fund
has); the first decides whether a button is drawn, the second whether it
is live. Keeping them apart is what stops a derived source appearing,
struck through, on the three cards it can never mean anything on.

### Cross-cutting

**Item 5, upload preview grain.** Still open, and still cosmetic. Half of
it arrived in the meantime: the mapping preview now resolves its five
sample rows on the server (`/api/upload/normalise_sample` — the resolver
is never mirrored in JavaScript) and shows each cell's canonical value,
in accent colour with the file's own spelling in the tooltip, or in red
where nothing matched. What it still does not show is the **grain** — the
level the value was stated at — which is the half that would tell you a
file saying "Information Technology" is about to record a sub-sector
rather than a sector. Deliberately not chips: the preview's job is to
show what will be stored, and `unknown` at a chip-selected level would
read as "this row will not be stored".
