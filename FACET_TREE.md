# FACET_TREE.md — the four facet trees

*Current as of v0.77.0.*

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
| `asset_class` | `sub_class` → `asset_class` → `super_class` | `super_class` | `Asset_definitions.csv` | 25 / 7 / 4 |
| `sector` | `sub_sector` → `sector` → `super_sector` | `sector` | `Sector_definitions.csv` | 50 / 12 / 3 |
| `country` | `country` → `region` → `super_region` | `country` | `Geography_definitions.csv` | 250 / 11 / 2 |
| `currency` | `currency` | `currency` | `Currency_definitions.csv` | 162 |

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
Verified against the code at v0.70.1 rather than carried forward on
trust. Deliberate boundaries are described in the sections above;
everything here is something that should be fixed.

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

What remains unresolvable is the class of "ex" phrasings that name more
than one region. `Asia ex-Japan` spans `asiaDeveloped` and
`asiaEmerging`; `World ex-US` spans nearly all of them. Neither is a
single node, so neither can be an alias of one, and inventing a node for
each would put overlapping buckets into a tree whose levels are supposed
to partition. They correctly land in the Resolve-unmatched-values dialog
instead, where the user can say which region they meant for that fund.

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

**`Information Technology` does not resolve.** It returns `unknown` at
every level, confirmed by calling `resolve_sector_tree`. This is the
canonical issuer spelling, and `config.py` documents it as the worked
example of what the file is for: *"Drives the Sector dropdown and the
upload-side coercion of issuer spellings ('Information Technology' →
'technology')"*. The alias is simply absent from the `matches` column
that comment describes. An alias gap rather than a code fault, but a
conspicuous one, since it is the exact case the design cites.

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

**`upload.py` still pre-resolves sector.** It reduces sector to its
middle level before handing the row on. The coerce at the end of the
fold corrects it, so nothing is stored wrong, but it is a second
implementation of a rule that already lives in the fold, and the two can
drift.

### Asset

**`cashAssetOptions` filters by node NAME.** The vocabulary endpoint
serves each node's level but not its parent, so the cash tab excludes
`bond future` — a derivative, not a deposit — by matching its name.
Serving the parent chain and filtering on the branch is the honest fix.

### Currency

Nothing outstanding. The flat facet is the one that works exactly as
specified, which is mostly a statement about how much less there is to
get wrong when a facet has one level.

### Cross-cutting

**Item 5, upload preview grain.** Not started, and cosmetic: the preview
would show the stated grain with the chain in the tooltip. Deliberately
not chips — the preview's job is to show what will be stored, and
`unknown` at a chip-selected level would read as "this row will not be
stored".
