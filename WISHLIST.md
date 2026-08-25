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

### Concurrent per-holding lookups

A lookup costs about 0.01s cached and about a second when it misses and
runs the full fallback chain. The loop is strictly serial, so a few
hundred unrecognised rows is minutes of wall clock spent almost entirely
waiting on the network.

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

### Serve the parent chain from the vocabulary endpoint

`facet_alias_targets` serves each node's level but not its parent, which
is why the cash tab's picker filters by node NAME and has to exclude
`bond future` — a derivative, not a deposit — by matching the word.
Serving the chain would let every consumer filter on the branch instead,
and would retire a documented limitation rather than working around it.

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
