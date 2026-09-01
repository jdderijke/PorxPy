# WISHLIST.md — possible future enhancements

*Started 2026-08-18, at v0.72.3.*

Things worth doing that nobody has promised. This is deliberately **not**
a defect list: a bug lives in the Known open issues section of the
document that owns it — `OPTIMIZER.md` §13 for the solver,
`FACET_TREE.md` §17 for the facet trees, and `README.md` for the app,
its endpoints and its transport — because a defect is a thing that is
wrong, while an entry here is a thing that is merely absent.

Each entry says what it would change and why it is not already done, so
a later reader can judge whether the reasoning still holds rather than
re-deriving it.

---

## Upload and enrichment

### A progress bar for upload enrichment

**Wanted.** The commit prints progress to the terminal every 25 rows and
the dialog carries a static warning, which was enough to stop a slow
commit reading as a hung one (v0.72.3). It is not enough to watch. A
bar in the dialog, moving, with the row count and remaining time, is
what the operation actually calls for — you are staring at that dialog
for the whole of it.

**Why not yet.** The commit is a single synchronous POST that returns
only when every row is done, so there is nothing for a bar to read. It
needs either progress streamed from the server (SSE, or chunked
responses) or the commit split into a start call plus a pollable status
endpoint. The second fits the existing cancel-token machinery well: the
token already identifies an in-flight commit and already carries a
cancel flag, so a `GET /api/upload/progress/<token>` would have
somewhere natural to live.

### One answer to "which fields does enrichment fill"

Enrichment reads its field list from two different places. The automatic
top-10 pass and the fund page's Enrich button both take it from
**Settings -> enrichment fields**. The holdings-upload dialog takes it
from its own per-field Yahoo toggles, seeded from that fund's last
upload and defaulting to nothing ticked. So somebody with all five
fields ticked in Settings still gets a first upload with enrichment
entirely off unless they turn the toggles on in the dialog.

**Why not simply unify.** The per-file choice is defensible on its own
terms: an issuer file that already carries good sector data is a
reasonable place to want enrichment off for sector and on for currency,
and Settings is the wrong grain for that. The improvement wanted is
narrower — seed the dialog's toggles from Settings on a FIRST upload
(where there are no saved prefs to restore), so the app-wide answer is
the starting point and the per-file choice is a departure from it rather
than a fresh start from nothing.

### Concurrent per-holding lookups

A lookup costs about 0.01s cached and about a second when it misses and
runs the full fallback chain. The loop is strictly serial, so a few
hundred unrecognised rows is minutes of wall clock spent almost entirely
waiting on the network. Since v0.86.0 there is exactly one such loop
(`extractors.enrich_holdings_rows`), shared by the upload commit and the
fund page's Enrich button, so a bounded pool added there would speed up
both at once.

**Why not yet.** Yahoo rate-limits, and this project has already been
bitten by what a rate limit does: it answers 200 with an empty payload,
which is indistinguishable from "no such symbol" and used to be cached
as a permanent miss (fixed in v0.72.2 by expiring negatives rather than
by detecting the cause). Firing lookups in parallel makes rate limiting
more likely, not less, so this wants a bounded pool and a backoff on the
first sign of throttling — and ideally the progress bar first, so the
speed-up is visible and the failure mode is legible.

---

## Optimiser

### Volume as a fourth scoring component

**Wanted.** Liquidity belongs in what makes one fund better than another
at the same exposure. Two funds alike on cost, size and returns are not
alike if one trades a thousand times the other — the thin one costs you
on the spread every time the optimiser proposes a trade in it, and that
cost is invisible in the three components scored today.

The data is already fetched: `regularMarketVolume` and `averageVolume`
are Yahoo profile fields and appear in `field_sources`, so nothing new
needs collecting.

**Why not yet.** `SCORE_COMPONENTS` is `(ter, size, returns)`, and the
three weight models in `SCORING_PRESETS` are written against exactly
those three. A fourth changes every model's weights, and therefore every
score in the app — the fund list, the peer popovers, and the optimiser's
own preference between candidates. That is a re-scoring of the whole
universe, not an addition to it, so it wants doing deliberately rather
than alongside something else.

Two shape questions to settle first. **Which measure**: last traded day
is the freshest but the noisiest — a quiet Tuesday is not a verdict on a
fund — while an average over some window is steadier and is what the
spread actually follows. **Which arithmetic**: size is a FLOOR TEST
rather than a percentile, deliberately (see `DEFAULT_SIZE_FLOOR_BASE`),
because past "big enough" more is not better. Liquidity plausibly works
the same way — past the point where your own trade size disappears into
the daily volume, more volume buys you nothing — which would make it a
second floor test rather than a second percentile. Worth deciding on
that reasoning rather than by symmetry with the returns component.

### Tolerance and reported error per LEVEL, not just per facet

`max_error` is keyed by facet alone and `_facet_devs` groups residuals by
facet alone, so the three levels of a sector target collapse into one
worst-case number. You cannot ask for 2pp at super-sector and 8pp at
sub-sector, and when a facet misses, the headline figure does not say
which grain missed. The per-bucket `deviation` block already carries the
level, so the information exists — it is the summary and the stopping
test that do not use it.

---

## Facet trees

### Filter the cash picker on the branch, not the node name

*Half done in v0.82.0.* `facet_alias_targets` now serves each node's
`parent` alongside its level and path, so the vocabulary endpoint no
longer withholds the chain — that was the blocking half, and the tree
picker is built on it.

What remains is the consumer: the cash tab's picker still filters by node
NAME, excluding `bond future` — a derivative, not a deposit — by matching
the word. It can now ask whether a node descends from `cash` instead.
`facetTreeHtml` already takes a `filterFn` for exactly this, so the work
is switching that picker over rather than building a mechanism.

### One name for the third country level

`FACET_LEVELS` and the definitions file call it `super_region`; the
`config.py` commentary calls it `development` throughout; `resources.py`
wires the two together by assignment. Nothing computes the wrong answer,
but a reader looking for one name will not find it, and a reader who
finds the other will expect a geographic parent above region and get
`developed` / `emerging`. Pick one and retire the other.

### Upload preview at the stated grain

Item 5 of the v0.70.0 frontend plan, never started and always cosmetic:
the preview would show the grain each row was stated at, with the full
chain in the tooltip. Deliberately not level chips — the preview's job
is to show what will be stored, and `unknown` at a chip-selected level
would read as "this row will not be stored".

---

## Factsheets

### Ability to upload multiple documents

PorxPy now only allows the upload of 1 document (the factsheet) that then
is fed to the AI to extract data from. However some funds have more documentation
then just a factsheet. For instance a list of holdings, an analyst report etc.
Uploading more documents enabled the extraction of more meaningfull data, but at
the cost of more tokens.

## fund_explorer.html

### The file is getting too big
The file has been growing over all the versions produced since the start of the project.
One possibility to get a better manageable file is to extract de CSS part (and maybe the JS part)
different files, thereby reducing the size of the HTML file. Another possibility
is maybe to split the UI in html files per top-level tab.


## App deployment

### Make the app Windows installable
One windows installable app, including automatic launching of the default browser to start the UI.

### More user friendly way to enter the Claude API key
The key is currently added as an environment variable, better to enter it in the settings somewhere.
