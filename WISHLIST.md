# WISHLIST.md — possible future enhancements

*Started 2026-08-18, at v0.72.3. Last swept at v0.102.1.*

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

### One name for the third country level

`FACET_LEVELS` and the definitions file call it `super_region`; the
`config.py` commentary calls it `development` throughout; `resources.py`
wires the two together by assignment. Nothing computes the wrong answer,
but a reader looking for one name will not find it, and a reader who
finds the other will expect a geographic parent above region and get
`developed` / `emerging`. Pick one and retire the other.

### Upload preview at the stated grain

Item 5 of the v0.70.0 frontend plan, and still cosmetic — but less
absent than it was. The preview now resolves its five sample rows on the
server and shows each cell's canonical value, with the file's own
spelling in the tooltip and an unmatched cell in red. What it still does
not show is the **grain** each row was stated at, with the full chain in
the tooltip, which is the half that would reveal a file whose sector
column is about to be recorded one level deeper than it was written
(see `FACET_TREE.md` §17). Deliberately not level chips — the preview's
job is to show what will be stored, and `unknown` at a chip-selected
level would read as "this row will not be stored".

---

## Factsheets

### Ability to upload multiple documents

PorxPy now only allows the upload of 1 document (the factsheet) that then
is fed to the AI to extract data from. However some funds have more documentation
then just a factsheet. For instance a list of holdings, an analyst report etc.
Uploading more documents enabled the extraction of more meaningfull data, but at
the cost of more tokens.

## Tools

### Show the resource fingerprints without pressing Reload

**Wanted.** The Tools tab's resource-files card is the only place that
says which definition files are loaded and what content each was read
at. That table is populated from the response to **Reload resource
files**, so on opening the tab it is empty: the answer to "is the app
seeing my edit?" is available only by taking an action, and the action
is the one thing the card tells you that you usually do not need.

**Why not yet.** The fingerprints ride on `POST /api/resources/reload`,
and calling that on tab open would mean a side-effecting request fired
by navigation — the wrong shape, even though a reload is harmless. It
wants a plain `GET /api/resources/fingerprints` returning the same map,
called when the tab opens, with the reload response continuing to
refresh the table it already renders. Small, but it is a new endpoint
rather than a rewiring, which is why it is here rather than done.

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
