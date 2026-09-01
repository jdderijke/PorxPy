# PorxPy Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.87.4] - 2026-09-01

### Fixed — "Reset to defaults" reset two settings of six and said it had reset them all

Found while auditing the rest of the Settings tab after the Danger zone
fix. The button restored the enrichment field list and the holdings
match key, then reported "Reset — click Save to persist" — while the
scoring weights, the size floor, the group TTLs, the factsheet age limit
and both AI toggles kept whatever they had. Clicking Save afterwards
persisted the values it had just claimed to clear, which is worse than
doing nothing: the user has been told the state is one thing and is
saving another.

The cause was upstream of the button. ``GET /api/settings`` handed the
frontend ``DEFAULT_SETTINGS`` as its ``defaults``, and that config
constant only seeds two of the six sections — the defaults for scoring,
group TTLs, the factsheet age and the AI flags live in their own config
constants and are applied by ``normalise_settings``. The button could
only reset what it had been given.

* ``utils.default_settings()`` returns ``normalise_settings({})``: the
  complete document as it stands with nothing set, built by the same
  function every save goes through, so a seventh section is in the
  defaults the moment it is normalised rather than when someone
  remembers to add it. The route now sends that.
* The frontend half was the mirror image — the reset poked the two
  controls it knew by name. The painting half of ``renderSettingsTab``
  is now ``renderSettingsControls()``, and both the tab render and the
  reset call it, so a control added to this tab is reset by
  construction. The reset copies the server's defaults into the working
  settings and repaints; it hardcodes no default of its own, since a
  default invented in the frontend is a second opinion about what a
  default is.

Also checked, and working: both bundle exports (including their
price-history and factsheet checkboxes), the file chooser, Save settings
(its payload was compared field by field against what is stored — all
six sections round-trip exactly), and the three Danger zone buttons
fixed in 0.87.3.

## [0.87.3] - 2026-09-01

### Fixed — none of the three Danger zone buttons did anything

Clear all portfolios, Clear all cache and Reset all data each opened
their confirmation dialog by adding the class `open` to it. The only
rule that makes a dialog visible is `.modal-bg.show{display:flex}`;
`.open` means something on the popover component and nothing at all on a
modal. So the dialog was built correctly, filled with the right title
and warning, and then left at `display:none` — no dialog, no error, no
clue, which reads exactly like three buttons that were never wired up.
The typed-RESET gate and the endpoints behind it were fine throughout;
nothing could reach them.

It was the classic shape of a bug in this codebase: fourteen dialogs do
the same thing and one of them says it differently. The fix removes the
opportunity rather than the instance — `showModal` / `hideModal` are now
the only two places that name the class, and all fifteen dialogs go
through them. A fifteenth dialog cannot invent its own vocabulary
without deleting a helper first.

## [0.87.2] - 2026-09-01

### Fixed — a bundle import left its button stuck, and never showed you what it imported

Reported after moving an install to another machine: import the
pre-loaded fund bundle, then open the portfolio backup, and the dialog
listed the portfolios but its Import button already read "Importing…"
and could not be clicked. Restarting made it work once, and then the
same thing happened again on the second import.

Two separate defects, both in the import dialog's ending.

**The button never came back.** Every handler in the app that starts a
request disables its button and relabels it, and every one restored it
in its `catch` block — none on success, because success closes the
dialog. But a dialog is not discarded when it closes; it is the same
element, hidden. So the busy state outlived the operation and the NEXT
open inherited it: a dead button with no error on screen and nothing to
say why. The fund-bundle import was the first one, which is exactly why
the portfolio import was the one that looked broken.

The fix belongs to closing rather than to any one handler, so `hideModal`
now clears the busy state of every button inside a dialog it hides, and
handlers declare that state through `setBtnBusy` / `clearBtnBusy`. It is
applied to all thirteen dialogs and all thirteen busy buttons, not just
the reported one — the same trap was live in the fund-bundle import, the
breakdown-CSV preview and commit, the holdings upload's parse and commit
steps, the holdings row editor, the Edit fund dialog and the factsheet
upload. The targets editor had a hand-written workaround for exactly
this, with a comment describing the bug; that copy is gone, since the
shared close now does the job for every dialog.

**The import did not reach the screen.** On success the dialog called a
`reloadAll` that does not exist in the file, behind a
`typeof reloadAll === 'function'` guard that therefore never passed. So
an import wrote portfolios and settings to disk and then left the page
showing what it had before — which is why the imported portfolios only
appeared after restarting everything. It now calls
`refreshConfigAndPortfolios`, the same function the app uses at startup,
which reloads the portfolio list, repopulates the selector and refreshes
the cached-funds panel; re-renders Settings when a portfolio backup
overwrote `settings.json` and that tab is open; and re-fetches the
resource vocabularies when a fund bundle merged into them.

## [0.87.1] - 2026-09-01

### Fund bundles no longer carry the exporter's filesystem paths

A fund bundle is meant to be handed to someone else, and it ships whole
cache files. Three of the things in those files record where an upload
came from — the holdings blob, the new `upload_sources` store, and
pre-0.87.0 `upload_prefs` blobs still on disk — and where an upload came
from is often `C:\Users\<you>\Downloads\…`. That path is inert on the
recipient's machine and carries the exporter's username and folder
layout out of their install for nothing in return.

`export_funds` now blanks every non-URL `source_value` on the way into
the zip. An http(s) URL is kept: an issuer's holdings schedule or
factsheet is public, and it is the most useful single thing in a curated
bundle, because it is how the recipient refreshes what they were given.
`source_kind` and `filename` are kept too, so the holdings tile still
reads `LOCAL:<filename>` and says the list was supplied rather than
fetched — only the location goes.

The scrub is written against the `source_value` key rather than against
those three known paths through the blob, so a fourth store adopting the
same convention is covered when it is written. Nothing on disk is
touched; only what leaves in the file. On the current cache this removes
47 paths and keeps 7 issuer URLs.

## [0.87.0] - 2026-09-01

### Every upload dialog now offers back the source you gave it last time

Uploading a fund's holdings already remembered where the file came from
and re-opened with it in the Source field. Uploading a **factsheet** or a
**facet CSV** for the same fund did not, and both are exactly the case
where remembering pays: an issuer publishes next month's factsheet at the
address it published this month's, so replacing one meant going back to
the browser history for a URL PorxPy had already been given.

All three dialogs now share one memory and one prefill:

* A new fund-level cache category, `upload_sources`, holds one record per
  upload kind — `holdings`, `factsheet`, `breakdown_csv` — each
  `{source_kind, source_value, filename, saved_at}`. The kinds are
  declared once in `config.UPLOAD_SOURCE_KINDS` and the store
  (`utils.upload_source_get` / `upload_source_put`) takes the kind as a
  parameter, so a fourth upload dialog would need no new mechanism.
* `GET /api/upload/source?ticker=…&kind=…` is the single reader, and
  `prefillUploadSource(kind, inputId, hintId)` the single frontend
  caller. Each dialog also shows the same `↩ last source: URL:… · saved …`
  line under the field that the holdings dialog had, so the three
  describe a source identically rather than one calling it a "file".
* The record is written **on commit**, never on preview: a preview the
  user abandons is not a source they chose. A multipart factsheet POST
  records nothing, because a browser file input names bytes, not a place
  to fetch them from again.

It is a fund-level memory (ISIN-keyed), unlike the column mapping, which
stays per-listing. Two listings of one fund can have differently laid-out
spreadsheets, but they cannot have differently located documents — a
factsheet is shared by every listing of the fund, so the URL should be
too. As a result the holdings source moved out of `upload_prefs`, which
now holds only what genuinely is per-listing (mapping, header row,
decimal, weight unit, defaults, enrich fields). A source recorded there
by an older build is promoted into the new store the first time a dialog
asks for it, so nothing that was remembered is lost.

The cache screen's label for `upload_prefs` was corrected with it: it
read "Holding upload URL's", which is now the other category's job.

## [0.86.3] - 2026-08-27

### Documentation — README.md and OPTIMIZER.md audited against the code

Both were checked claim by claim rather than re-stamped, and both are now
current at 0.86.3. Every doc in the repo now carries a version stamp.

**README.md** was two releases behind and had no stamp of its own — only
a version inside a sample terminal banner, which is the one place a
reader would not think to look for it. Corrections:

* the release line said 0.84.0;
* `yf_session.py` was missing from the code layout entirely, despite the
  README carrying a whole section on the transport faults it exists to
  fix;
* the listings-cache line named price history and profile but not the
  remembered upload column mapping, which also lives there;
* the TTL list gave one figure for FX, which has two — 6 hours for spot
  and 24 for history.

Everything else it asserts was verified and left alone: the module list
otherwise matches `porxpy/`, the seven portfolio sub-tabs match the
frontend, the per-category TTLs match `DEFAULT_CACHE_CONFIG`, and the
source-keyed `holdings` / `uploaded_breakdowns` description matches the
accessors.

**OPTIMIZER.md** was stamped v0.77.0 against code at 0.86.2. The solver
itself had not drifted — the numerics, the greedy-plus-swap structure,
the capped-simplex bisection and the frozen-position algebra all still
match the module — but four things had gone stale around it:

* §7b did not say that "higher-scoring" is relative to a **selectable**
  scoring model (v0.79.0), which matters because the same fund is a 95
  under one preset and a 12 under another, and `score_preset: none`
  skips the pass altogether;
* §10 listed eleven returned keys and the function returns seventeen;
* §11 quoted a ~4e-5 screening-noise figure that belonged to an older
  `SCREEN_ITER` of 60. Measured at the current 150 it is ~2e-6, for
  about a 3x speed-up over the exact solve — and the constant's own
  comment in `optimizer.py` was still describing the 60 setting too,
  which is now corrected from measurement rather than from memory;
* §12's coverage-complete note predated derived sources: since 0.86.0 a
  currency card sourced from country inherits the country card's
  assertion, so one tick can complete two of the facets the solver
  reads.

Both §13 known issues were re-confirmed rather than assumed — the
metadata-facet blindness by running `candidate_exposures` with a
`market_cap` target and getting an empty exposure back, the level-weight
multiplication by reading the full facet weight still applied once per
`(facet, level)` block. Both remain open.

`CLAUDE.md`'s two size figures were also stale (`fund_explorer.html` is
19k lines, not 16k; `app.py` 6.8k, not 5.9k).

### Added to the wishlist

"Which fields does enrichment fill" has two answers depending on the
screen — Settings for the automatic pass and the fund-page button, the
dialog's own toggles for an upload. Recorded with the narrow improvement
that is actually wanted (seed the dialog from Settings on a first
upload) rather than as a defect, since the per-file choice is deliberate.

## [0.86.2] - 2026-08-27

### Changed — Enrich through Yahoo acts on the ticked rows, and nothing else

The button treated an empty selection as "enrich the whole list", which
was how it behaved before selection existed. That makes one control mean
two different things depending on a state elsewhere on the screen, and
the expensive reading is the one you get by pressing it without noticing
the ticks had been cleared — on a fund of a few thousand holdings,
minutes of per-holding network calls nobody asked for.

It is now disabled until at least one row is ticked, with a tooltip
saying so, and its label carries the count once there is a selection.
The header tick box still selects everything visible, so "all of them"
remains one click — a deliberate action rather than a default.

The endpoint keeps its own default (no `row_ids` means every row), which
is the sane behaviour for an HTTP call with no screen attached. The fund
page simply never relies on it.

### Documentation

`IMPORT_GUIDE.md` was five releases behind and is now current at 0.86.1:
the Enrich section covers the tick-box requirement, the negative-cache
retry and the pinned-facet rule; the upload dialog's Yahoo toggles no
longer claim to need an identifier column; the breakdown-source table
gains "From country" with the reasons it is currency-only; and the
coverage-complete section gains the inherited assertion.

`CLAUDE.md` gains the derived-source registry in its sweep list and
`enrich_holdings_rows` in the module table, so the next change to either
is asked the consistency question at the point it is made.

## [0.86.1] - 2026-08-27

### Fixed — "From country" appeared, struck through, on all four cards

The derived currency source shipped in 0.86.0 was listed as a button on
every breakdown card and greyed out on the three that cannot use it.
That reads as a promise: a disabled source button everywhere else in this
app means "you could have this" — upload a factsheet, load some holdings
— and it is worth showing precisely because something would fill it.
Nothing will ever make a sector split derivable from geography, and the
same for asset class and for country itself.

The card block now carries `sources_applicable` beside `available`, and
the two answer the different questions they were being asked to share:

* `sources_applicable` — does this SOURCE exist for this FACET at all?
  The selector draws a button only for these.
* `available` — does THIS FUND have it? Applicable but absent is where a
  struck-through button belongs.

`config.sources_for_facet()` is the one place that answers the first, and
all three consumers now read it: the override vocabulary that validates a
stored pin, the endpoint that accepts a new one, and the block the
selector is drawn from. The frontend tests no facet name — a second
derived pair would still be a backend change only.

## [0.86.0] - 2026-08-27

### Fixed — holdings enrichment was two copies of one loop, and both were broken

The fund page's "Enrich through Yahoo" button did nothing on rows the
user had just corrected by hand, and the enrichment offered during a
holdings upload filled nothing at all. Both are the same defect: the
upload's pass 3 was a private copy of the fund page's enrichment loop,
and the two had drifted in every way two copies can.

**The copy wrote to the wrong columns.** `_apply_lookup_to_row` — the
shared writer — puts every facet value in that facet's `<facet>_raw`
column, because the raw is what a source SAID and `normalise_facets`
re-derives every level from it on the next read. The upload's copy wrote
`row["sector"]`, `row["country"]`, `row["currency"]` and
`row["asset_class"]`, the LEVEL columns, which `coerce_holdings_row`
blanks at the bottom of the same commit. So the dialog reported
"sector: 8 filled" and stored none of them. Its blank-only check read
those same level columns, which are empty at that point in the commit,
so the documented precedence (file > enrichment > default) was not
actually being enforced either — it only looked correct because the
write it guarded was discarded anyway.

**The same mistake sat in the two passes after it.** Pass 4 applied the
per-field defaults the user typed into the upload dialog, and pass 5
fell back to the fund's own asset class for rows still blank. Both wrote
level columns and both were discarded by the same coercion. A file with
no asset column produced holdings with no asset class at all, even for a
fund whose class was known — and the "default-filled 3 field(s)" line in
the result was true of the attempt and false of the outcome.

**The negative alias cache blocked the button.** `get_symbol_info_cached`
records "Yahoo has never heard of this" against the raw input so a
re-upload does not re-probe it. The upload passed `retry_negative=True`
to clear a stale one; the fund-page button did not. So the exact
sequence the button exists for — upload, watch Yahoo fail on some rows,
correct those rows by hand, press Enrich — hit the negative recorded
minutes earlier during the upload and returned "not found" without a
network call. Nothing happened, and nothing said why. The retry is now
the default for both callers, since both act on rows whose identifiers
may have changed since the negative was recorded.

**Only the copy honoured the finest-asset rule.** Asset is one facet at
several grains and the enrichment is supposed to take the finest answer
Yahoo gives — its `sub_class` where present, else its `asset_class` —
because the definitions file derives coarser levels from a finer value
and never the reverse. That rule is stated in `config.ENRICHABLE_FIELDS`
and was implemented only in the upload's copy, so the fund-page button
and the top-10 build coarsened every asset answer they were given.

Both callers now run `extractors.enrich_holdings_rows`, one loop with
the per-caller differences as parameters: which rows, a row cap, a
progress callback, and a cancel callback that raises the caller's own
exception (the upload keeps `UploadCancelled` and its token; the loop
knows nothing about either). `enrich_existing_holdings` is gone, renamed
into it.

### Fixed — upload enrichment refused the rows that need it most

The upload dialog's Yahoo buttons were disabled until a Ticker, CUSIP or
ISIN column was mapped, which locked out exactly the files that need
enrichment: an issuer position table of names and weights and nothing
else. The resolver has fallen back to a name search since v0.77.0 and
the upload's commit has passed the name since then too — only the
control still said otherwise. Name is a required mapping, so there is
nothing left to gate on; where no identifier column is mapped the
tooltip now says matching will be by name alone, which is slower and
less certain, rather than refusing.

### Fixed — the upload dialog's unmatched-value samples were always empty

The commit's "X values need review" summary read its example spellings
from the level column, which is the one column `normalise_facets`
guarantees to be blank for a facet that did not resolve. The count was
right and the examples were never there. Read from `<facet>_raw` now.

### Added — enrich only the rows you ticked

The holdings table's tick boxes now drive the Enrich button: with a
selection, only those rows are sent to Yahoo, and the button says so
("Enrich 12 selected through Yahoo"). With nothing ticked it still
enriches the whole list, as before. On a fund of a few thousand
holdings, fixing four rows was four thousand lookups.

The result line also says why nothing was filled when nothing was: rows
Yahoo did not recognise, lookups that could not complete, and rows with
nothing to look up now read differently instead of all reporting "no
blanks needed filling".

### Added — the currency card can be derived from the country card

Plenty of issuers publish a geographic split and no currency split at
all. The currency card gains a fifth source, **From country**, which
converts the fund's own country card country by country to each
country's primary currency (`Geography_definitions.csv`'s per-country
`currency` attribute).

It is a real source, not a view: the choice is the same ISIN-keyed
`breakdown_source.currency` override the other four use, so the
portfolio X-ray, the target deviations and the optimiser all read the
derived numbers exactly as they read any other card's.

It follows the card it reads, in one direction only:

* residual weight passes through unchanged — an `unknown` fifth of the
  country card is an `unknown` fifth of the currency card, and `n/a`
  stays `n/a`;
* a country with no currency on file becomes `unknown` and is named in
  the card's unresolved list, which is a row in the geography CSV away
  from being fixed;
* marking the **country** card coverage-complete completes the derived
  currency card too — the country split IS the currency split here, so
  requiring both ticks would let the user state two contradictory things
  about one set of numbers. The derived card reports
  `completed_from: "country"` and shows the assertion as inherited, with
  the control to withdraw it left on the country card where it lives;
* marking the **currency** card complete says nothing about country,
  which is not derived from anything.

The conversion reads the country card at its **country** level only: a
region names no single currency, so deriving from one would invent a
split rather than convert one.

The pair is a registry (`config.DERIVED_BREAKDOWN_SOURCES`) rather than
a test for "currency", and the frontend lists the button for every card
and lets the backend's `available` map strike it through where no
derivation exists — the same mechanism that disables "Upload" on a facet
no CSV covered. The reverse direction is deliberately absent and should
not be added for symmetry: a currency does not name a country, so
country-from-currency would invent detail rather than convert it.

### Changed — upload defaults no longer pre-resolve

The per-field default values typed into the upload dialog were each
resolved here first (`resolve_currency` / `country_to_mstar` /
`resolve_sector`) and the canonical stored, which made this a second
resolution authority beside `normalise_facets` — the divergence
FACET_TREE.md §17 recorded as "upload.py still pre-resolves sector", and
the reason a sector default was coarsened to the tree's middle level
before the tree saw the grain it was given. All four facets now pass the
user's text through to the raw column, as `asset_class` already did and
as a mapped column always has, so an alias added to a resource CSV next
month repairs rows filled by a default today.

## [0.85.6] - 2026-08-27

### Fixed — fund bundles carried factsheets out and threw them away coming back in

Exporting a pre-loaded fund set put every stored factsheet document and
its sidecar into the zip, and importing that zip wrote none of them. The
report said `factsheets: 0`, which is true and explains nothing, so the
loss looked like the export having omitted them.

Export and import each derived "which ISIN is this factsheet for" from
the file name, and derived it differently: export took `Path.stem`
(`IE00B4L5Y983`), import took `Path.name` (`IE00B4L5Y983.pdf`) and
upper-cased it into `IE00B4L5Y983.PDF`. That is never a member of the
accepted-ISIN set, so the skip branch fired for every factsheet in every
bundle. Factsheets are stored as `<ISIN>.<ext>` with no underscore
(`utils._factsheet_paths`), so the `_`-split that was meant to tolerate a
suffix never trimmed the extension the way the export side's `stem` did.

Both sides now call one `_factsheet_isin()` helper, so the two cannot
answer the same question differently again — the failure here was two
copies of a derivation, not a typo in one of them.

Nothing needs re-exporting: existing bundles already contain the
factsheets and will now import them.

Uploaded holdings, uploaded per-facet breakdowns, remembered upload
column mappings and the `breakdown_source` / `breakdown_complete` /
`holdings_source` pins were unaffected throughout — they ride inside the
fund and listing cache blobs, which are written and read wholesale.

## [0.85.5] - 2026-08-26

### Fixed — the holdings row editor discarded every facet edit and said nothing

Double-clicking a holdings row, changing Asset, Sector, Country or
Currency and pressing Save wrote nothing. The row did not move, and
reopening the editor showed the old value again.

`normalise_facets` reads `<facet>_node` **only** when `<facet>_pinned` is
set; otherwise it re-resolves the facet from the untouched
`<facet>_raw` — which is the whole point of the raw column, and the
reason a later alias in a resource CSV heals every past row with no
migration. The PATCH endpoint wrote the node and cleared the level
column, but never set the pin. So `coerce_holdings_row`, on that same
request, re-derived the facet straight back to what the source had said.
The endpoint then answered **200 carrying the pre-edit row**, the
frontend spliced that unchanged row into the table exactly as designed,
and the edit disappeared with nothing anywhere reporting a failure.

It survived this long because it appears to work on any row the Resolve
dialog has already pinned: those rows are pinned, so the node is
honoured, so an edit takes. Which row you happen to try decides whether
you see the bug.

The endpoint now calls `_pin_facet` — the same writer the Resolve
dialog's by-row tab uses — so the two surfaces that edit a row's facet
make the identical claim about it instead of one of them making none.
`_pin_facet` derives the level columns itself, so the hand-cleared
`<facet>_level` it replaces is gone.

**Currency was never handled at all.** The loop this replaces named three
column stems literally, `("asset", "sector", "country")`. Currency stores
its node under its own column name, so it fell straight through the gate
and no currency edit had ever been applied. The replacement walks
`BREAKDOWN_FACETS` and asks `facet_node_field()`, the one place that
knows which column each facet uses — so a facet cannot be missed by
being spelled differently.

**A facet is now sent only when the user moved that picker.** The server
pins whatever arrives, and a pin permanently stops the row re-resolving
from its source text. Sending all four on every save would mean
correcting a holding's *name* silently pinned its asset, sector, country
and currency too, and cost that row the ability to be fixed by a later
alias. The editor records what the pickers opened on and sends the
difference.

**The save could also die silently before sending anything.**
`saveHoldingEditor` read `document.getElementById('heAsset').value` and
its three siblings *before* its `try` block. Those four inputs are not in
the modal's markup — `ftMount` creates them, and
`populateEditHoldingDatalists` skips any facet whose vocabulary has not
loaded yet. Reading `.value` off the resulting `null` threw a TypeError
that escaped the function entirely: no request, no error, the dialog
simply did not react. The reads are guarded now, and an unmounted picker
is reported rather than saved around.

Verified end to end in a browser against a real uploaded holdings list:
each of the four facets changed through the tree picker, saved, and still
present on reopening — then restored through the same path.

## [0.85.4] - 2026-08-26

### Fixed — the Resolve dialog lost your place and your focus on Apply

v0.85.3 fixed the scroll jump on the fund page's holdings table and
stopped there. The same loss was still live in the Resolve / unmatched
values dialog, which is the other place the identical control is used —
so this entry is the sweep that should have come with it.

**The by-row tab threw you back to the top.** `ufRenderTable` carries the
scroll position across its *own* re-renders — a keystroke in a column
filter, a click on a sort header — by reading it off the scroller already
sitting in `#ufTableWrap`. But Apply goes through `ufRenderDialog`, which
replaces the whole shell first, so the wrap `ufRenderTable` then finds is
brand new and reports zero. That is why typing in a filter kept your
place and pressing Apply did not.

The by-value tab had solved this for itself, inline, in its own branch of
the same function. Its carry is now the shared `ufCarryScroll(root, sel,
render)` and the by-row branch uses it too — one implementation, both
axes, rather than one tab's private fix and the other's omission.

**Focus was destroyed and never handed back.** Applying rebuilds the list
under the popover, which removes both the popover and the button that
opened it; focus fell to `<body>`, leaving the keyboard with nowhere to
be. Most visible on the no-value entry, where the row you just cleared is
gone from under the cursor as well.

The popover now remembers the control it was opened from — by id, because
the element itself does not survive the rebuild but its replacement
carries the same id — and returns focus there whenever it closes:
applied, cancelled, or clicked away. All three triggers were given stable
ids (`fhedit-<facet>` on the fund page, `ufedit-<column>` in the by-row
tab, `ufValBulkEdit` in the by-value tab), because all three open the one
shared popover and all three lost focus the same way.

Every `focus()` on these paths now passes `preventScroll`. The fund page
is at that same moment restoring a scroll position across its own reload,
and a focus that scrolls its target into view would undo it — one fix
must not re-break the other.

## [0.85.3] - 2026-08-26

### Fixed — reloading a fund threw you back to the top of the page

Editing a facet on a holdings row and pressing Apply returned the reader
to the fund's name, however far down the table they had been working.

`reloadCurrentFund()` opens with `clearFundView()`, which hides every
fund panel so no stale tile shows during the fetch. That collapses the
document to the empty-state placeholder, and the browser clamps the
scroll to the top there and then — by the time the fund is drawn again
the position is gone. Nothing put it back.

The snapshot now happens before the collapse and is restored once the
page has its height back, through the same `_snapshotScroll` /
`_restoreScroll` helpers the portfolio tab and the two lists already use.
It sits inside `reloadCurrentFund` rather than at the call site, because
all eleven callers collapse the page for the same reason and would each
have needed the same three lines: the Resolve dialog's by-row tab (the
sibling control to the fund page's), the Edit-fund save, removing
uploaded holdings, reverting to Yahoo, and the resource reload.

One restore is not enough, which is the part worth recording.
`renderLoadedFund` returns before the page is its full height again — the
group tiles wait on the field taxonomy, the header's quality block on its
own fetch — so a restore fired then is clamped against a document that is
still short: a 1400px offset came back as 29, measured. The new
`_restoreScrollSettling` retries each frame until the offset actually
takes, then stops, with a deadline for the case where it never can (a
fund whose holdings were just removed is legitimately shorter than it
was, and this must not spin for the life of the page). Landing exits
immediately, so the normal case costs two or three frames and cannot
fight a user who scrolls afterwards.

Verified in a real browser: a 1400px window offset and a 300px scroll
inside the holdings table both survive the reload.

## [0.85.2] - 2026-08-26

### Fixed — the fund-data dot kept reporting a rating input you had just supplied

Pinning a field to a source in the Edit-fund dialog writes it to
`overrides.json`, and the scoring pass reads that store as a view over
the cached profile — so a TER pinned to the factsheet is a TER the fund
genuinely has, and the backend has been reporting it as present all
along. What the screen showed was the session-cached `/api/quality`
answer from before the save: the dot still said "missing ter", the score
still had no cost component, and re-reading the fund did not help,
because that path reads the same cached map.

`saveEditFundModal()` was simply not on the list of sites that call
`invalidateFundDerived()` — v0.85.0 patched the per-holding editor
(`saveHoldingEditor`), which is a different save path, and left the
fund-field dialog alone. Its comment there described the fund-field
dialog's effects, which is how the two came to be confused; it now
describes what a by-row edit actually changes.

Sweeping the rest of the same set found five more paths that mutate what
the two passes read and never told them:

- **Refetching stale pinned fields** (`/fields/refresh`) — same store, so
  a refreshed TER or size is a changed rating input.
- **Factsheet extraction** — fills rating inputs and facet items at once,
  so it moves both halves of the indicator.
- **Storing and removing a factsheet** — the server clears that source's
  supplied breakdowns either way (`_clear_supplied_source`), so every
  card pinned to the factsheet loses its numbers until the new document
  is read.
- **Bundle import** — imported funds join the universe, so peer groups
  resize for the funds already in it.
- **Saving Settings** — the weight model and the size floor are inputs to
  the quality pass as well as the score pass. This one refetched the
  scores by hand and left the dots on the previous model; it now goes
  through the same lever as everything else.

## [0.85.1] - 2026-08-26

### Fixed — the portfolio funds table had a Holdings header but no Holdings cell

The fund rows built their `holdingsCell` and then never emitted it: when
the Quality column went in at v0.84.0 the new cell took the Holdings
slot instead of being added beside it. The row was therefore eleven cells
under a twelve-column header, so the dots sat under **Holdings**, the
asset class under **Quality**, and everything after it one place left —
while the cash rows, which kept their Holdings cell, lined up correctly.

The v0.85.0 pass fixed the header, the `<colgroup>` and the placeholder
rows of this table but read the fund row as complete, which is how a
missing cell hid behind a corrected header. All three row templates in
the table — fund, cash and needs-re-fetch — now come to twelve, and the
pre-loaded list's row to eleven, checked against their own headers rather
than by eye.

## [0.85.0] - 2026-08-26

### Fixed — the quality dots, the score column and the peer list now follow what you change

Three passes hang off the whole fund universe rather than off one fund —
the five-dot quality indicator (`/api/quality`), the best-in-class scores
and the peer groups they carry (`/api/scores`), and the portfolio rollup
(`/view`). Each is fetched once and kept for the session, because each
walks every cached listing. Only the portfolio rollup was ever
invalidated when something changed.

So switching a breakdown card's source, ticking "coverage complete",
editing a fund's focus or focus detail, uploading or matching holdings,
resolving an unmatched value, and adding or deleting a cached fund all
left the dots showing their pre-change colours, the score column showing
its pre-change numbers and the peer list showing its pre-change members —
with nothing on screen saying they were stale. The stored change was
correct throughout; only the summaries of it were old, which is the worst
version of this bug to have, because the screen looks like the control
did not work.

The three caches go stale under exactly the same conditions, so they now
share one `invalidateFundDerived()` that drops all of them and repaints
whichever of the four surfaces is on screen — the fund page header, the
fund page's Score tile and peer toggles, the pre-loaded list and the
portfolio funds table. Every mutation site calls it in place of the lone
`lastLoadedPid = ''` it used to carry. One function rather than four lines
repeated at fifteen call sites: a site that remembered three of the four
is precisely the drift this replaces.

Two consequences worth naming. **Ticking "coverage complete" now moves
that facet's dot**, which it always should have: the backend already
drops the unknown slice and scales the rest up, so the card reads 100% —
but *hollow*, because the coverage is declared by you rather than counted
from holdings. And **peer groups are a property of the universe**, so
adding or deleting a fund now repaints the fund-data dot and the peer
score of everything that was ranked against it, not only of the row that
changed. Re-walking the peer group empties the chart's peer toggles, so
the lines that were drawn and that the new group still contains are put
back — a source flip should not silently clear a comparison you set up.

### Fixed — the pre-loaded list's filter row sat one column to the left

The list has eleven columns; its filter row had ten `<th>`. Everything
from Quality rightwards was therefore shifted: the score-model picker sat
under **Quality**, the portfolio filter under **Peers**, and the
optimiser-opt-in filter under **Portfolios**. Only the holdings filter,
which sits before the gap, was under its own heading.

The same omission was in both tables' `<colgroup>` — the Quality column
was added in v0.84.0 without a `<col>` — so every declared width from
Quality rightwards described the column to its left. Both are now
complete, and the portfolio funds table's placeholder rows (`No funds
yet`, `needs re-fetch`, the error row) span twelve columns rather than
the eleven they were written for.

### Fixed — the data tables squeezed their columns instead of scrolling

`.dtwrap` scrolled vertically only, and `.dtable` is `table-layout:fixed`
at `width:100%`. That combination honours the `<colgroup>` widths only
while they fit; once their sum exceeds the wrapper the browser shrinks
every column proportionally, and the Name column — the flex one, with the
longest content in it — is what the reader watches collapse.

The wrapper now scrolls on both axes, and each table states a `--dtmin`
floor: the sum of its fixed widths plus a usable minimum for Name. It
lives on the table so the three tables that share this styling share one
mechanism, each stating only its own number, rather than a `min-width`
rule per table drifting from the `<colgroup>` it is supposed to match.

### Added — the dot system explains itself

The indicator encodes three separate things — position, colour and fill —
and none is guessable. In particular, nothing on screen said that a
hollow green is a different claim from a solid one, which is the
distinction the whole indicator exists to make.

One `QUALITY_TIP` constant now carries the explanation, and every surface
that draws a dot hangs it off the dot: each individual dot's tooltip
gives its own reading first (`Sector — 82% at sector · counted from
holdings`) and the full scale after, both column headers carry it via
`applyQualityHeaderTips()`, and the fund page — the one surface with room,
and the one where you act on a dot — prints a two-line version under its
labelled block. Written once rather than five near-copies, for the usual
reason: five copies are five places the explanation can drift from what
the dots do.

The "coverage complete" checkbox says what it does to the dot, since that
is the control whose effect on the indicator was least visible: the facet
then reads 100% but hollow.

## [0.84.0] - 2026-08-25

### Added — a five-dot data-quality indicator on every fund

Fund data, asset, sector, country, currency — one dot each, in that fixed
order, on the pre-loaded list, the portfolio funds table and the fund page
header. Position carries which dimension a dot is about and colour carries
only how good it is, so nothing has to be read as a length. The column
sorts by the WORST dot first, with the average breaking ties: sorting a
quality column should surface what is broken, not what is average.

**The four facet dots** read each facet's coverage at its
`FACET_DEFAULT_LEVEL`, straight from `build_fund_breakdowns` — the same
number the breakdown card shows, so the dot cannot disagree with the card
it summarises. `unknown` counts against coverage and `n/a` counts as
answered, which `level_coverage` already got right: a cash sleeve has no
sector and never will, and scoring that as a gap left cash-heavy funds
permanently short with nothing anyone could do about it.

**Solid means measured, hollow means reported.** A solid dot was counted
from actual positions; a hollow one was published by the issuer, or rests
on the user's `breakdown_complete` assertion that a partial source covers
the whole fund. Both can read 100% and they are not the same claim, so the
difference is a second bit rather than being averaged into the colour.

**The fund-data dot is not a completeness measure**, and that is the part
worth explaining. The obvious design — count the populated fields — was
built and discarded after measuring the real universe: all 51 funds had a
primary asset class, 45 had a TER, 48 had a size, and 24 still could not be
rated. The blocker is peer-group size, a property of the universe rather
than of the fund, so a completeness bar would have shown those 24 green
while answering the question wrongly. It now reports readiness in three
states with three different remedies — `missing` (a rating input absent),
`alone` (inputs present, fewer than `MIN_PEER_GROUP` peers), `rated`.
Across the current cache that is 8 / 20 / 23.

`GET /api/quality` computes it for the whole cached universe, cache-only
and read-only for the same reason `_score_universe_cached` is: this runs
on a list render, and a pass that could trigger a Yahoo fetch would turn
opening a tab into one round-trip per fund. It reuses the scoring pass for
the readiness verdict, so the indicator and the score column cannot
disagree about the same fund, and feeds `build_fund_breakdowns` — a pure
function — from the cached blobs, so it produces exactly what the fund page
would. Measured at 1.6s over 51 funds, fetched once per session and painted
when it lands, the way the score chips already are.

All three surfaces render from one pair of functions (`qualityDotsHtml`
for the lists, `qualityRowsHtml` for the header, which adds labels and
nothing else), so the compact form is genuinely a summary of the detailed
one rather than a second opinion about the same fund.

## [0.83.2] - 2026-08-25

### Fixed — the upload preview's facet columns were blank, not missing

`POST /api/upload/normalise_sample` returned `value: ""` with
`unmatched: false` for every facet, so the mapping preview painted empty
cells over the file's own text and the facet columns looked as though the
feature had been removed.

`normalise_facets` speaks the row COLUMN PACK: it reads each facet's
source text from `<facet>_raw` and writes the resolved node to
`<facet>_node`. The endpoint was handing it `{facet: raw}` and reading the
same keys back. That could not announce itself, because every facet name
is also one of that facet's own level names — `sector` is a level of
sector, `country` of country, `currency` and `asset_class` likewise. So
`normalise_facets` saw no `<facet>_raw`, took the row as having no source
text, and ran its blanking pass over the derived columns — overwriting
precisely the keys the endpoint then read. Empty value, `unmatched` false,
no error anywhere.

The endpoint now builds its probe with `facet_raw_field` and reads results
back through `facet_node_field`. Verified end to end: "Technology" →
`technology`, "United States" → `unitedstates`, "Aandelen" →
`regular stock`, and "Wibble Nonsense" / "Atlantis" / "XYZ" come back
empty and flagged unmatched, which is what turns those cells red.

`sub_class` is dropped from the facet list at both ends. It is a level of
asset_class, not a facet, and no mapping column has produced it since
v0.72.0.

The client swallows failures from this endpoint deliberately — the
mapping still works without the preview — so this stayed silent. Worth
knowing when the preview looks wrong again: the endpoint is the place to
check first, and it answers to a plain curl.

## [0.83.1] - 2026-08-25

### Fixed — only the list scrolls in Resolve unmatched values, on both tabs

0.82.3 gave the dialog one scroll region, but put it on `.modal-b` — so
the help text, the facet chips and the summary line scrolled away with
the list, and each tab's table still carried its own `max-height:60vh`
scroller inside that. Two scrollbars again, on both tabs; the By row tab
is where it showed first.

The scroll region is now the table box itself, on both tabs, sized by an
unbroken flex chain rather than a viewport cap: the modal is a
fixed-height column, the header (title + tabs), the help text, the
summary line and the footer are all `flex:0 0 auto`, and the table box
takes exactly what is left. A `vh` cap could not do this — it makes a box
that is shorter or taller than the space available, and taller is the
second scrollbar.

Every ancestor in that chain carries `min-height:0`. Without it a flex
item refuses to shrink below its content, which pushes the footer — and
the Cancel button with it — off the bottom of the dialog.

Verified by rendering the dialog with 120 rows and a table wider than the
dialog: `.modal`, `.modal-b`, `#ufDialogBody` and `#ufTableWrap` all
report no overflow, `[data-ufscroll]` scrolls both axes, and the footer
sits inside the viewport.

Scroll-position preservation across a re-render is unchanged and still
reads both axes off that one element — which is now true again, having
briefly not been.

## [0.83.0] - 2026-08-25

### Changed — the Resolve popover offers two scopes and two buttons, not three scopes

"Leave untouched" was a third radio in the scope group whose entire
effect was to disable Apply. That is what Cancel already does, so one
outcome was offered twice in two different shapes — and a user reading
the group had to work out that one of its three options was not a scope
at all.

The radio's real job was making the OPENING state inert, so that a stray
Apply on a freshly-opened popover could not write anything. No radio
being selected does that just as well, so the group is now the two
choices that actually differ — pin these rows, or change all rows in all
funds — and the buttons are Cancel and Apply. Apply stays disabled until
a scope is chosen, with the note saying so rather than announcing
"Nothing will change."

The fallback when a chosen scope becomes unavailable follows the same
change: it clears the selection instead of selecting the no-op scope. The
rule it exists to protect is unchanged — a scope that becomes
unavailable never silently re-aims to the other one, because turning a
pin into a vocabulary write is exactly the surprise this dialog was
restructured to prevent.

`ufPopoverApply` now checks the two scopes directly and returns unless
one of them is both checked and enabled. It never inferred permission
from the button's disabled attribute, and it still does not.

## [0.82.9] - 2026-08-25

### Fixed — "no value" now says why it can do nothing, instead of doing nothing quietly

Choosing "no value (clear this facet)" for a value with no holdings rows
behind it left both scopes unselectable and the note reading "Nothing
will change." — a control that appeared broken with no reason given.

The mechanism is deliberate on both sides, which is why neither scope was
obviously at fault. "No value" is a per-ROW pin: it records "this row has
no country" on the rows themselves. It is not expressible as an alias,
because an alias is a claim about a WORD and "this word means nothing" is
not a meaning this vocabulary carries — so `clear` disables the alias
scope. And the row scope is disabled when none of the ticked values are
carried by holdings rows, which is the case for anything that came from
Yahoo, a factsheet or an uploaded CSV. Both off, the popover falls back
to "leave untouched", exactly as designed for a scope that becomes
unavailable — and the fallback message described the fallback rather than
the cause.

The note now names the dead end: what "no value" writes, why these values
have nothing to write it on, why an alias cannot express it either, and
what the two real options are — map it to a node, or leave it, since a
value that matches nothing already counts as unknown everywhere it is
used.

No behaviour changed: "no value" still works from the By row tab, and
from By value whenever a ticked value is carried by holdings rows.

## [0.82.8] - 2026-08-25

### Fixed — the oversized picker was a class-name collision, not a size

The tree picker rendered 180px tall in the Resolve popover while the
identical control was 20px in the holdings editor. Measured in a real
engine, the trigger's label carried `padding:80px 32px` and
`text-align:center` — 80 + 14 + 80 is exactly the 180.

The cause: `ftPaint` marks the trigger label `set` or `empty` depending on
whether a node is chosen, and the app already has a global
`.empty{text-align:center;padding:5rem 2rem}` for empty-state blocks. The
holdings editor opens on a value, so its label was `set` and unaffected;
the Resolve popover opens with nothing chosen, so its label was `empty`
and inherited an empty-state block's padding. Same component, same
stylesheet, one class name shared with something else.

`.derived` was a second, quieter instance of the same thing — a `.74rem`
type rule landing on the ancestor tick. Compound selectors hid both:
`.ftree-box.derived` outranks `.derived`, but only for the properties it
actually declares, so the font size still leaked through.

Every modifier class in the component is now prefixed `ft-` (`ft-empty`,
`ft-set`, `ft-sel`, `ft-anc`, `ft-on`, `ft-derived`, `ft-up`, `ft-open`,
`ft-none`, `ft-legacy`, `ft-extra`). Verified by rendering both mount
contexts headlessly: the trigger is 20px in each, rows 16px, filter 20px.

This is also why the three preceding rounds of compaction changed nothing
visible — the height was never coming from the rules being edited.

## [0.82.7] - 2026-08-25

### Fixed — the tree filter box was being styled as a form field

Inside the holdings editor the picker is mounted in a `.field`, and
`.field input` — specificity (0,1,1) — outranks a bare `.ftree-search`
(0,1,0). So the filter box took the editor's form-field styling: 40px
tall, .88rem type, .6rem of padding, an 8px radius. Next to rows set at
.64rem it dominated the panel, which is most of what "the tree picker is
oversized" was pointing at.

The rule is now `.ftree-panel .ftree-search`, which is (0,2,0) and wins
wherever the picker is mounted, with `height:auto` stated explicitly
because the inherited rule pins a fixed 40px that padding cannot undo.

This is why compacting the other dimensions changed nothing visible: the
element actually setting the panel's height was never being styled by the
rules being edited.

## [0.82.6] - 2026-08-25

### Fixed — a tree that cannot be drawn says so, instead of looking oversized

The `japan` self-loop fixed in 0.82.3 threw partway through painting the
picker, which skipped the sizing and placement step and left the panel
unsized and unplaced. On screen that read as an enormous empty control
rather than as the crash it was, and it sent two rounds of work at the
stylesheet.

The body render is now guarded: a vocabulary that cannot be walked draws
a red "could not draw this vocabulary" line, logs the facet and the error
to the console, and lets the rest of the control paint normally. A crash
should look like a crash.

## [0.82.5] - 2026-08-25

### Fixed — the tree picker trigger is the width of its field

A `<button>` shrink-wraps its label, and the trigger set no width, so it
was as wide as whatever placeholder it happened to carry and a different
width at every mount. The `<select>` it replaced took `width:100%` from
`.field select`. Set explicitly now, with `box-sizing:border-box`.

## [0.82.4] - 2026-08-25

### Changed — the tree picker is compact, at every mount

The picker was built at card density rather than control density, so it
read as oversized wherever it appeared — not only in the Resolve dialog,
where it was first noticed. It replaced a `<select>`, and it should sit in
a form at roughly the weight of one.

Every dimension came down together, so the proportions hold: the trigger
to 2px/5px padding at .65rem, rows to 1px/4px at .64rem with a 1.35 line
height, the checkbox to 11px, the panel to 280px wide with 4px of padding
and a 190px body, the indent step from 14px to 11px. `FT_PANEL_MAX_H`
tracks the stylesheet's body height, as it must — the two describe the
same box, one before measurement and one after.

## [0.82.3] - 2026-08-25

### Fixed — the country tree crashed on `japan`

A facet node's identity is (level, key), never the key alone — and the
tree picker's index was keyed by the bare key. The country vocabulary
contains `japan` twice, once as a country and once as a region. So the
two collapsed into one entry, and the country node's parent (`japan`, the
region) resolved to that same entry: a node that was its own child. The
walker recursed until it threw, which took out the whole country picker,
not just that branch — and because `ftPaint` throws before it reaches
`ftPosition`, the panel was left unsized and unplaced as well.

The index is now keyed by `level  key`, and a parent is resolved at
the level exactly one step coarser — which is where the backend puts it —
rather than by name. A `seen` guard in the walker and in the ancestor
walk means a future vocabulary quirk degrades instead of hanging the page.

What gets STORED is still the bare key, unchanged. Where a key lives at
two levels that value is genuinely ambiguous, and the deepest is chosen,
matching how the rest of the app reads a facet.

### Fixed — the tree filter now narrows the list

Typing kept a match's whole subtree, so a query matching a coarse node
("d" matching `developed`) pulled every country under it back into view
and the list never appeared to filter. A match now keeps itself and its
ancestors only — the ancestors purely so the branch can be drawn.
Filtering removes things.

### Fixed — Resolve unmatched values has one scroll region

The dialog scrolled at `max-height:90vh` while the table inside it
scrolled again at `.dtwrap`'s 560px, so two scrollbars moved
independently. Worse, the footer sat inside the scrolling modal, so the
Close button could be scrolled off the bottom.

The modal is now a flex column that does not itself scroll. The title and
the tab bar are pinned at the top (the tabs moved into the header for
this), Close is pinned at the bottom, and the only scrolling box is the
list between them. The inner wrappers give up their own max-height inside
this dialog, keeping their border — the frame is what the list reads as;
only the scrolling belonged to the parent.

### Fixed — the edit popover no longer opens past the bottom of the screen

Its placement clamped against a hard-coded 360px guess at its own height.
The popover has grown well past that, so a click low on the page put its
Close and Apply buttons below the viewport — and being `position:fixed`,
there was nothing to scroll to reach them.

`ufFitPopover` now measures the height it actually rendered at: below the
click when that fits, above it when it does not, and pinned to the top
margin with its own body scrolling when it fits neither way.

## [0.82.2] - 2026-08-25

### Fixed — the facet picker is one size, wherever it is mounted

The "should mean" picker in the Resolve unmatched values dialog opened
far larger than the identical control in the holdings editor, from the
same component.

Two things made the size follow the container rather than the control.
The panel was `left:0;right:0`, so it stretched to whatever it was
mounted in — a dialog field in one case, a popover in the other. And
`ftPosition` treated its height limit as a target rather than a cap: it
grew the body to fill the room under the trigger, and the Resolve popover
sits mid-screen with plenty of room, while the holdings editor's fields
sit low in a tall modal with very little. Same facet, same component, two
and a half times the height.

The panel is now a fixed size in both: `width:max(100%,260px)` capped at
320px, and one `FT_PANEL_MAX_H` for the body. `ftPosition` only ever
shrinks below that cap when the space really is tighter, which is what it
was added for — keeping a panel on screen, not filling the screen.

The label above it uses the modal's own `.mfl` class instead of an inline
colour, so the two read as the same control. No resolution hint was added
underneath to match the modal's: this dialog already prints the chain in
the "currently" box above, and repeating it below the picker would say
the same thing twice in a popover whose size was the complaint.

## [0.82.1] - 2026-08-25

### Fixed — facet pickers open on the current node, not the matched one

A facet value has two nodes behind it, and both the tree picker and the
holdings editor were opening on the wrong one. The **matched** node is
where the import resolved the source text to. The **current** node is
where the row actually sits afterwards, once the import path has placed
it deeper — `is_default` in the resource files, and the other rules that
run on import. Where the import made contact is provenance; it is not the
value, and it is not what the table shows.

The Resolve popover preset itself with `chain.find(e => e.matched)` and
the holdings editor seeded from the stated-node column, so both offered a
coarser value than the row actually holds — and offered to save it.

Both now resolve to the deepest level carrying a value, through one
shared `facetDeepestInChain` / `facetCurrentNode` pair rather than two
expressions of the same idea. The chain is finest-first and a row leaves
a level blank where its tree does not reach, so the first populated entry
is the node in force; `unknown` and `n/a` are guarded against, since
neither is a node a picker should open on. An unmatched row still falls
back to its stated-node column, which is where its raw source text lives
— the Resolve dialog cannot fix what it cannot show.

The editor's old comment argued the opposite case: that seeding from a
derived column would offer a grain the source never asserted. That had
the wrong subject. The deeper node is not a display choice the picker
invented, it is what the import already stored; showing the match is the
substitution that comment was trying to avoid.

Provenance is still reported where it belongs — the holdings cell tooltip
continues to say "Stated as X at Y level", which is a question about the
match and is answered by it.

### Fixed — a tree panel no longer opens past the bottom of the screen

The panel body always scrolled, so no node was ever unreachable, but the
panel itself is anchored under its trigger — and a trigger low in a
dialog has very little room beneath it, so the panel opened off the
viewport.

`ftPosition` now measures the trigger against the viewport when a panel
opens, when its contents change, and when a resize or scroll moves it.
The body's height comes from the room actually available rather than a
fixed 300px, and the panel flips above its trigger when there is more
space there. Opening downwards stays the default, since that is what a
dropdown is expected to do; the flip is for the case where down does not
fit. Viewport coordinates are the right frame here because the modal
scrolls inside a fixed backdrop, so "off the screen" is a viewport
question, not a dialog one.

## [0.82.0] - 2026-08-25

### Changed — peer lists name a fund, not a code

A peer row led with an identifier column: the ISIN, or a bare ticker when
the listing had no ISIN. So the same list named some members one way and
some another, which is what made a ticker turn up "sometimes". Both
surfaces that render peers — the popover on the fund page and on the
pre-loaded list, and the peer legend under the price chart — now show the
fund NAME and its TRADING CURRENCY, and no code at all.

The currency earns the column the identifier gave up. It is what actually
distinguishes two rows that otherwise read identically: one real peer
group here contains two listings of iShares Core MSCI World and two of
iShares Global Water, alike in name and differing in the currency they
trade in. It is also the more useful subtitle under the chart, where peer
series are drawn at their own price and the currency says why two lines
sit on different scales — and it matches the portfolio price legend,
which has shown the currency there all along while the peer legend showed
a ticker.

The ticker and ISIN moved to the hover title on both surfaces. They are
what a click acts on and what the caches are keyed by, so they stay
reachable — just not worth a column. Chart tooltips are named the same
way, instead of by bare ticker.

`score_universe` now carries `currency` alongside `name` and `isin`, for
the reason those two are already there: a consumer listing a peer group
should be able to name its members without a second lookup. It is
canonicalised through `normalise_currency`, so a pence-quoted listing
reads GBP like everywhere else; the `/funds/<t>/peers` endpoint was
uppercasing instead, which turned GBp into GBP by accident and would not
have handled any other sub-unit.

### Added — facet nodes are chosen from a tree, not a flat dropdown

Every dialog that picks a canonical facet node offered one flat
`<select>` of the whole vocabulary: 66 sector nodes, 250 countries, with
a `level-name` prefix as the only clue to where a node sat. The prefix
named the level without naming the branch, so `sub-japan` and
`region-japan` read as two spellings of one thing rather than as a
country inside a region.

The vocabulary is a tree — that is what `FACET_LEVELS` describes — so the
picker now draws it as one, in the holdings editor (all four facets), the
Resolve popover, and the upload defaults. Behaviour, as specified:

* **clicking a node expands it, and does nothing else.** Browsing and
  choosing are separate gestures, so exploring the tree can never write a
  value by accident.
* **the checkbox is the only thing that selects.** Ticking what is
  already ticked clears it.
* **ticking a node ticks its ancestors**, because a claim about a
  sub-node is necessarily a claim about the branch containing it. The
  ancestor ticks are drawn outlined rather than filled: only one node is
  the assertion, and invariant 1 keeps the whole tree *plus the stated
  level*, so a picker that drew derived and asserted ticks identically
  would hide the half that carries meaning.
* **nothing below the ticked node is ever ticked.** `is_default` in the
  resource files is an import-side rule for placing holdings whose source
  under-specifies them; it says nothing about what a user asserted, so
  this picker does not read it.

A filter box replaces the type-to-search a `<select>` gave for free, and
matching branches open automatically — a match three levels down is
useless if it still has to be found.

`facet_alias_targets` now returns `parent` per node, the key one level
coarser or `None` for a root. It is derivable from the existing `path`
plus the level order, and it is stated for the same reason `path` is: a
browser that worked out parentage itself would be a second authority on
the shape of a tree this module owns, and the two would agree only until
a facet gained a level. Currency has one level, so it comes back as 162
roots and the same component renders it as a flat checklist with no
special case.

`facetNodeOptions` and `ufFacetOptionsHtml` are removed. They have no
callers left, and a second builder for the same list is how two pickers
come to disagree about what a node is called. The behaviours they owned
travel with the tree: the level tag on every node, and the red
"not in the definitions file" row for a stored value the vocabulary no
longer contains.

The cash positions table still uses flat selects, stated in a comment at
that site: it edits facets in table cells, where a drop panel opens
inside a scrolling row. The component already takes the `filterFn` that
the cash asset list needs.

## [0.81.0] - 2026-08-25

### Fixed — a portfolio view no longer ships two blocks nothing on that screen reads

`GET /api/portfolios/<pid>/view` attached each fund's whole
`load_fund_data` blob under `funds[].data`, and two keys in there
dominated the response while answering nothing the screen asks.
`price_history` was 61.3% of a 27-entry view and had no reader at all;
`holdings_rows` was 35.3% and was read only for its `.length`.

Neither was idle by accident — each has its own endpoint, because each
screen needs an answer the raw block cannot give. The History chart
calls `/price_history`, which re-derives per-fund daily *values* in the
base currency with FX applied and carry-forward alignment; raw OHLCV
cannot answer that, and the chart never assembled it client-side. The
aggregated holdings table calls `/holdings_rollup`. Valuation for the
view itself is finished server-side in `_build_enriched_funds` before
anything is serialised, so the price series was already spent by the
time it was attached. Opening a fund's own page does not use the
attached copy either: `viewFundFromPortfolio` re-fetches `/api/fund` by
identity, as do all five sites that render a loaded fund.

The consequence was not only waste. With debug indentation the response
reached ~15.7MB and the development server did not deliver all of it —
`Content-Length: 15707874` against 15698590 bytes received — so the JSON
arrived truncated and unparseable and that portfolio's view failed to
load. It read as an intermittent fault because smaller portfolios were
unaffected.

Both keys are now dropped from the `/view` response only, by
`_slim_fund_data_for_view`, which copies rather than mutates so a
transport-level trim cannot change what any other consumer of those
blobs sees. `/holdings_rollup` shares the same `_build_enriched_funds`
and still aggregates `holdings_rows`; `/api/fund` still carries both in
full, because the fund page reads both. The 27-entry view went from
15,707,874 bytes (truncated) to 273,504 delivered whole.

`price_history` was dropped outright rather than kept as a trimmed
date-and-close series. Both screens that want prices already read the
cache server-side through their own endpoints, so an inline series would
have been ~1.9MB with no reader, and re-adding it later is a smaller
change than carrying it now.

The frontend's two `hs.top_count || (f.data?.holdings_rows || []).length`
fallbacks are gone with it. They were already dead: `top_count` and
`holdings_rows` both derive from the same cached rows so the two could
never disagree, and the synthetic cash entry, which has no
`holdings_status` at all, never reaches a branch that renders the count.
Left in place they would have silently become "always 0" and read as
intentional.

### Fixed — debug mode no longer doubles the size of every JSON response

`create_app()` left the JSON provider's `compact` unset, so Flask decided
by debug mode and indented every response when `debug=True` — which is
how `main.py` runs. Measured at 2.14x on one portfolio view, and it is
what pushed the largest responses into the range where the development
server truncated them.

`compact` is now set to `True` on `_SafeJSONProvider`, the NaN-scrubbing
provider this app installs, rather than on `app.json`. That placement is
the fix: `create_app` replaces the provider instance outright with
`app.json = _SafeJSONProvider(app)`, so setting `app.json.compact`
before that line is silently discarded and the responses stay indented.
On the class it cannot be undone by the order of the two statements.

## [0.80.0] - 2026-08-24

### Changed — a target total is what the targets commit, not the levels added up

The per-facet total in the targets editor, and on the Targets tab, added
every level together. Targets nest, so that double-counted:
semiconductors 15% set inside technology 35% showed as 50%, and a
coherent target set could read well past 100% with nothing wrong with
it. The number was doing the opposite of its job — it existed as a
sanity signal and had become a source of false alarm.

Both figures are now the **committed** total, taken at the coarsest
level of the facet's tree. Every target is rolled up into the bucket
that contains it, and each bucket commits the larger of its own target
and what its targeted children commit — larger, so that an inconsistent
intermediate state still reports what is really committed rather than
quietly under-reporting it. A bucket targeted only through its children
still counts, so sub-sector targets with no sector target commit their
branch just as firmly.

The scale now means something: 100% is the whole category spoken for,
and the amber warning past it marks a real over-commitment (two
super-sectors at 60% each cannot both happen) instead of an artefact of
counting a sub-sector twice. Each figure names the level it was taken
at, since a total without its grain is not a number anyone can check.

`target_sum_pct` in the portfolio view is renamed `target_committed_pct`
and joined by `committed_level`; leaving the old name would have let a
reader keep assuming a sum. The rule lives in `targets.committed_pct()`
for the stored view and in `tgCommittedPct()` for the editor, which has
to answer on every keystroke and cannot make a round trip to do it —
the editor's two copies of the old arithmetic (full render and
live-update) are now one call each to that helper.

### Added — every score says which model produced it

A score is a rank, and a rank means nothing without the weights it was
taken over: the same fund is 100 under Cost driven and 12 under Returns
driven, and a bare 12 reads as a bad fund rather than as one the chosen
model does not reward. Only the pre-loaded list said which model was in
force. Now every surface showing a score names it — the fund page's
Score tile and Score row, both peer lists, and the optimiser's trades
and alternatives tables.

The optimiser's tables are captioned from the answer, not from the
picker: `/api/portfolios/<pid>/optimize` echoes `score_preset` and
`score_preset_label`, because the picker can be changed after a run and
the figures on screen would then carry a model that never produced them.
With scoring off the label is empty, matching score cells that read "—".

The two peer popovers are now built by one function. Both show the same
ranking of the same funds under the same model, so the pre-loaded list's
popover gains the model picker the fund page got in 0.79.0 — only one
popover is ever open, so this is one control on screen, not thirty.
Changing the model there does not reopen the menu, because that list can
re-sort underneath it; on the fund page, where the subject does not
move, it reopens.

### Documented — two defects found while verifying this release

README gains a **Known open issues** section, the counterpart of
`OPTIMIZER.md` §13 and `FACET_TREE.md` §17 for the app itself. Two
entries, both found while checking the work above and neither caused by
it: `/api/portfolios/<pid>/view` ships every fund's price history and
holdings rows although the screen it serves reads neither — 96.5% of a
7.9MB response — which pushes large portfolios past the size the
development server delivers reliably, so the view arrives truncated; and
`create_app()` leaves `app.json.compact` unset, so debug mode indents
every response and roughly doubles it. Diagnosed and measured, not yet
fixed; the entries carry the numbers and the proposed fix.

### Added — how to stop an antivirus from inspecting HTTPS

README §External services now gives the two Avast paths — excluding
single domains (Settings ▸ General ▸ Exceptions) and turning HTTPS
scanning off altogether (Settings ▸ Scam Guardian ▸ Web Guard) — plus
the one-line `openssl s_client` check for telling whether a host is
still being intercepted, since "it should work now" is not something a
user should have to take on trust.

## [0.79.1] - 2026-08-24

### Fixed — every Yahoo fetch failed once an HTTPS-inspecting antivirus was active

Selecting any fund reported "Yahoo did not return live data for
<ticker>. Enter the full ticker including its exchange suffix" — a
message about the ticker, for a fault that had nothing to do with the
ticker. Underneath, every outbound call was failing at the TLS
handshake with `curl: (60) SSL certificate problem: unable to get local
issuer certificate`, for cached and uncached funds alike, because the
fetch route validates the symbol against Yahoo before serving anything.

The cause was on the machine, not in the fund data. An antivirus web
shield (Avast, here) inspects HTTPS by terminating the connection itself
and re-signing it with a root certificate of its own, installed in the
Windows certificate store. Browsers read that store and carry on
silently; Python does not — `certifi` ships a fixed list of public CAs —
so libcurl saw a chain ending in an issuer it had never heard of.

`porxpy/yf_session.py` already existed for a different fault that
produces the *same* curl error (a post-quantum ClientHello some
middleboxes cannot parse, fixed by pinning the TLS impersonation
profile), and its docstring stated that pointing the session at a CA
bundle changes nothing "because the trust store was never the problem".
That was true of that fault and false of this one. The module now
documents both, and says which symptom separates them.

It now builds, at startup, a bundle of certifi's roots plus the roots the
operating system already trusts, and hands it to the Yahoo transport.
Certificate verification stays on, and no root is trusted that the
machine did not already trust — this aligns Python with the machine's
trust policy rather than loosening it. `PORXPY_CA_BUNDLE` overrides the
bundle for a machine whose roots come from somewhere this cannot see.

**Not fixed, deliberately:** the `requests`-based lookups (OpenFIGI ISIN
resolution, justETF enrichment) still fail while such a scanner is
active. Python 3.13 turned on `VERIFY_X509_STRICT` by default, and
Avast's root is malformed in a way that check rejects outright —
basicConstraints not marked critical — whatever bundle it is offered in.
libcurl does not apply that check, which is why Yahoo works. Relaxing it
would weaken certificate validation for every connection the app makes,
so the remedy is to exclude those two hosts from HTTPS scanning. README
§External services records this.

## [0.79.0] - 2026-08-24

### Added — the score model is selectable from the fund page's peer list

The peer list is an ordering, and an ordering means nothing without the
weights it was taken over. The pre-loaded list has always said which
model produced its scores, in the select under the score column; the
fund detail page showed the same ranking, in the peers popover on the
Score row, with no way to see or change the model behind it. Opening
that popover now offers the same picker.

The choice is one session-wide setting rather than one per surface.
Changing it from either picker refetches the scores once and redraws
both the pre-loaded list and the open fund page, because the alternative
— the list ranked by cost and the fund page by returns, with nothing on
screen saying which was which — is two answers to one question. The peer
list re-sorts by construction, since it ranks on the `score_peer` the
newly chosen model just produced.

The optimiser's quality picker is deliberately left independent: its
model is part of a run's definition ("optimise for cost"), chosen per run
and able to be off entirely, not a way of looking at a list.

### Changed — a peer row is ISIN, name and score in fixed columns

Peer rows were ticker, then name and score run together in one
right-hand cell, so every score started at a different x and the column
could not be read down — which is the one thing a list of ranks is for.
Each row is now three fixed columns: ISIN, name, score. ISIN because it
names the fund itself, the identity the rest of the app is keyed by, and
because it is the same width on every row. The ticker moved to the hover
title, since that is what a click still acts on.

Both surfaces that render peers share the row builder, so the fund page
and the pre-loaded list line up identically.

### Fixed — popovers clamped to the wrong width at the right edge of the window

`togglePrePop` measured nothing and assumed every menu was 320px wide —
true when it was written, and silently wrong for the wider peer menus.
It now measures the menu after opening it, so a popover near the right
edge is positioned by its actual width.

## [0.78.1] - 2026-08-23

### Fixed — the version policy contradicted itself

`porxpy/__init__.py` is the single source of truth for the version, and
its own docstring said fixes bump `0.1.x` where CLAUDE.md says `0.0.x`.
The patch digit is what the project has actually done (0.76.1 → 0.76.4
were all fixes), so the docstring was a typo — but a typo in the file
that defines the rule, which is the worst place for one.

Both documents now also state the cadence, which neither did: bump once
per batch of work handed over rather than once per session, and take the
minor bump when a batch mixes a fix with a feature.

## [0.78.0] - 2026-08-23

### Changed — market cap is read from the exposure table, not from an average

The extraction prompt now tells the model how this field is meant to be
filled, which it previously had to guess from a bare list of allowed
values.

Factsheets state market cap two different ways, and the two deserve
different answers. Where the document prints a market-capitalisation
**exposure** breakdown — "Market capitalisation exposure",
"Marktkapitalisierung", a giant/large/mid/small/micro table — the field
takes the band holding the largest exposure. The bands are stated in the
prompt as company size in USD (large ≥ $10bn, mid $2bn–$10bn, small below
$2bn) so the model can map an issuer's own buckets onto them, and they
are generated from `MARKET_CAP_BUCKETS` — the same three numbers the code
uses to bucket a fetched median market cap, so the prompt and the
classifier cannot drift apart.

Where the document gives **only** a weighted-average or median market
cap, the answer is `mixed`. An average is one number standing in for a
distribution: a total-market tracker holding thousands of small caps
still shows an average in the hundreds of billions, because the average
follows the mega-caps that dominate the weighting. Reading a band off
that number would report the largest holdings as though they were the
fund, so `mixed` is the honest reading — the document has not said how
the fund is distributed.

Issuer wording is also mapped through the near-miss table now, so
"Giant", "Mega Cap", "Micro-cap" and "Multi-Cap" resolve to `large`,
`large`, `small` and `mixed` instead of failing validation and vanishing
from the result.

### Fixed — the extraction report said nothing about the positions it read

v0.77.0 taught the extraction to read a factsheet's holdings table, but
the report that shows what was read still listed only fields, breakdowns
and rejections. So an extraction that had just filled the holdings tile
reported nothing about it, and the only way to discover the positions had
arrived was to go and look at the tile.

The report now carries a holdings block, answering the three questions it
already answers for a breakdown — how many rows, what they sum to, and
whether the document presents them as the whole fund — plus the first
eight positions with the columns the document actually printed beside
them. A long schedule is summarised rather than reproduced: this is a
report on the reading, and the tile is where the rows live.

## [0.77.0] - 2026-08-23

### Added — the holdings tile has three sources, and you choose

The four breakdown cards have had a source selector since 0.44.0: Yahoo,
factsheet, holdings, upload, chosen per card and saved with the fund. The
holdings tile below them had none, because the `holdings` cache slot held
exactly one blob and whoever wrote last won. Uploading a CSV overwrote
Yahoo's top-10 outright, so "show me what Yahoo says instead" was a
refetch rather than a click, and there was no way to hold both.

The slot now holds one blob per source — `{"sources": {"yahoo": …,
"factsheet": …, "upload": …}}` — and the tile carries the same selector
the cards carry, with the same three names:

- **Issuer (Yahoo)** — the top-10 Yahoo publishes, plus whatever the
  per-symbol enrichment filled in.
- **Issuer (factsheet)** — the position table read off an uploaded
  factsheet. New in this release; see below.
- **Upload** — the CSV or XLSX you supplied.

Which source is showing is a fund-level override keyed by ISIN, exactly
like a card's source, so it applies in every portfolio that holds the
fund and in the optimiser — not just in the tab you set it in. With no
choice saved, the richest list wins (upload, then factsheet, then Yahoo),
which is the behaviour the single slot used to produce. A source that
exists but carries no rows never wins that fallback: Yahoo publishes no
holdings at all for most European UCITS ETFs, and letting its empty slot
outrank a factsheet's real list would have hidden the list behind the
absence of one.

The three actions on the tile are unchanged and now act on **whichever
source is showing**. Enrich through Yahoo fills blanks in the visible
list and writes them back to that source, so enriching a factsheet's
positions no longer has anywhere to go except the factsheet's own slot.
Upload writes the upload slot and leaves the other two alone. Show bond
columns is, as before, purely a view.

Existing caches migrate on read, by shape rather than by a version
marker: a blob with `rows` at the top level is filed under the source its
variant belongs to (`manual_upload` → upload, `yahoo_top10` /
`yahoo_enriched` → yahoo). Migrated rather than purged, because the blob
being converted may be a manual upload, and an upload is the one thing in
`cache/` that cannot be re-fetched.

Two smaller consequences worth knowing:

- **A forced refresh now refills Yahoo's slot even on a fund you have
  uploaded to.** It used to be suppressed, necessarily, because the fetch
  would have destroyed the upload. The upload is now safe because it
  lives somewhere else, and that is what makes Issuer (Yahoo) selectable
  again after an import.
- **Editing a row no longer relabels a Yahoo list as a manual upload.**
  That relabelling was how the old single slot said "do not refetch over
  this"; doing the same now would file Yahoo's rows under Upload and
  overwrite a real one. The rows stay where they belong and carry an
  edited flag instead, which the fund load reads and which stops a
  refresh from undoing your corrections just as the relabelling did.

### Added — factsheets are read for their position table too

The extraction that already reads a factsheet's fields and its four facet
breakdowns now also reads the holdings table it prints — "Top 10
holdings", "Grootste posities", a full schedule of investments — in the
same single call, so the positions, the breakdowns and the metadata
provably come from one reading of one document.

Rows are stored as the document worded them: the sector, country,
currency and asset-class columns land in each facet's raw column and are
resolved on every read, so an alias added to a resource CSV next month
repairs a factsheet read today, with no re-extraction. The guards are the
ones the fields already had — the table needs a page and a verbatim quote
or it is discarded, a row needs a name, a weight must be a number, and a
model is never asked to invent a ticker or ISIN for a position printed
with neither. A table headed "largest holdings", or one whose weights
fall short of 100, is marked partial however the reply describes it.

Deleting or replacing a factsheet removes its positions along with its
breakdowns. Both are readings of the same document, and last year's
positions must not survive under this year's sheet.

### Added — assert that a card's coverage is the whole fund

A fund-page breakdown card showing an *unknown* slice now offers a
checkbox: **coverage complete (+N%)**. It is for the case where the
breakdown rests on an incomplete set of holdings — an issuer top-10, a
factsheet's largest positions — and no source can supply more. Ticked, it
says "read this source's coverage as the whole fund": the unknown slice
is dropped and the identified part scaled up to fill it.

It is a **stored assertion, not a way of looking at the card**. The
distinction is the whole point. `unknown` is not something the optimiser
can allocate against, so a fund with a large unknown slice contributes
nothing usable to a design — and a control that only changed the picture
would leave that exactly where it was. The choice is a fund-level
override keyed by ISIN (`breakdown_complete.<facet>`), applied where the
card is built, so everything downstream reads the completed numbers: the
portfolio X-ray, the target deviations, and the optimiser.

Details worth knowing:

- **Per facet, per fund.** A fund may have trustworthy sector coverage
  and a hopeless country split; each card carries its own assertion.
- **`n/a` takes no part.** It means the question does not apply to that
  exposure — cash has no sector — so it is not a gap that could be
  filled. The identified part is scaled to `1 − n/a`, not to 1.
- **An implicit shortfall counts too.** Issuer fractions that simply sum
  to 0.94 with no residual item are scaled the same way: the assertion is
  about coverage, not about how a source chose to express the gap.
- **A card with nothing identified is left alone** and stays unavailable.
  Asserting completeness cannot conjure a split out of a card that has
  none, and the checkbox is not offered there.
- **The coverage badge says `100% ASSUMED`**, with the real figure in its
  tooltip. The numbers downstream treat the assertion as fact; the badge
  is where a reader can still tell it apart from a measurement.

The default is unchanged and deliberate: a top-10 covering a fifth of a
fund describes a fifth of a fund. Only the user can say otherwise, and
only for a fund where nothing better exists.

### Fixed — the holdings source selector was missing on a cached fund

There are two full fund render paths — the fetch path, and
`renderLoadedFund`, which draws a fund opened from the pre-loaded list —
and the new selector was added to the first only, so opening a fund from
the cache showed a holdings tile with no source buttons at all. Both
paths now go through one `renderHoldingsTileState`, which draws the
selector and the provenance notice together, so the next strip added to
that tile cannot reach one path and miss the other.

### Fixed — a factsheet's geographic focus was always rejected

The extraction report's "Not used" list showed `focus_detail` rejected
with a wall of ~280 country names as the reason. Two faults, one visible:

`focus_type` was renamed `region` → `geography` in the registry, and the
extraction's alias resolver was left testing the old name. So a
geographic focus never reached the alias table, and every value the table
exists to catch — "Europe", "Eurozone", "Emerging Markets" — was handed
to validation unresolved and refused. Both names are accepted now, and
those four resolve to `europe`, `europeDeveloped`, `emerging` and
`japan`.

The reason string then printed the entire allowed vocabulary. Rejections
now name a count and a few examples, which says the same thing and can be
read. The same vocabulary was also being pasted into every extraction
prompt; the country list is described rather than enumerated there, which
takes about 3,000 characters off every call.

### Changed

- `holdings_status_from_cache` and the fund-list badges report the source
  actually in effect, and gained `source_key` (yahoo / factsheet /
  upload) and `available` beside the existing variant name.
- The portfolio's field-overrule ranking places a factsheet's positions
  between an upload and an enriched Yahoo top-10: it is the issuer's own
  statement about the fund, where a per-symbol lookup is a third party's
  opinion about a security.
- The variant → roll-up label mapping (`full` / `top10_enriched` /
  `top10_raw` / `factsheet`) lived as an if/elif chain in three places —
  the fund load, the row editor and the enrichment button. It is now
  `config.rollup_label_of`, which is why the new variant reached all
  three.
- The breakdown cards' source buttons and the holdings tile's are built
  by one function. They are the same control over the same question, and
  two copies were two places for the wording, the styling and the meaning
  of a struck-through button to drift.

## [0.76.4] - 2026-08-21

### Fixed — a factsheet extraction reached the breakdown cards as nothing

Extraction ran, the report listed the sectors and countries it had read,
and every breakdown card pinned to the factsheet source showed a single
100% *unknown* slice. The reading was being thrown away between the
extractor and the store.

v0.76.0 changed the supplied-breakdown item shape: an item is now
`{"raw", "weight"}` — the source's own wording, re-resolved on every read
so an alias added today repairs a document read last month — plus an
optional `"key"` where the user has pinned it to a node. Both writers
were changed (the CSV commit and the factsheet extractor) and so was the
reader (`breakdowns._resolve_items`). Three sites in between were not,
and each still demanded the pre-0.76.0 `{"key", "weight"}`:

- `utils._clean_items`, the normaliser that every write AND every read
  passes through, kept only items that had a `key` and emitted nothing
  else. A factsheet extraction has no pinned items at all, so all four
  facets persisted as empty lists. This is the defect that produced the
  symptom.
- `breakdowns.build_fund_breakdowns` filtered the supplied lists by
  `key` a second time, so even a stored item would not have reached the
  card.
- The unmatched-values scan behind Tools → Resolve resolved each
  supplied item's `key`, so no factsheet or upload value ever appeared in
  the dialog that exists to fix exactly those.

The same normaliser also stripped `raw` from the pinned items it did
keep, which made them look pre-0.76.0 to the cache's legacy check — so a
CSV upload with hand-mapped rows was deleted wholesale on a later read.
That is why the bug looked intermittent between uploads and extractions.

Nothing needs re-importing beyond the affected fund: extractions saved
under 0.76.0–0.76.3 stored nothing, so re-extract (or re-upload the CSV)
and the cards fill in.

### Changed — a factsheet's stored raw is the printed text, not the model's guess

The extractor returns two things per row: `key`, its canonical guess, and
`label_in_document`, the text the document actually prints. The stored
`raw` was taken from `key`, which made the comment above it — *"stored as
the DOCUMENT's own wording, unresolved"* — false in the same way the
pre-0.76.0 reader/writer pair was: the value being re-resolved on every
read was a conclusion, so an alias added later could only ever repair the
model's invention rather than the wording on the page. `raw` is now the
printed text where the extractor supplied it, falling back to `key`
otherwise. Where the vocabulary does not know a printed spelling the
weight lands in *unknown* and the value appears in Tools → Resolve, which
is where a spelling the vocabulary has not learned belongs.

### Fixed — an extraction no longer outlives the document it was read from

Deleting a factsheet removed the file and its sidecar but left the facet
items the extraction had written, so the numbers survived the document.
Replacing a factsheet had the same gap: the sidecar was replaced
wholesale, the breakdown items were not, and last year's split showed
under this year's document. Both paths now clear the `factsheet` source's
items, and a re-extraction writes all four facets unconditionally, so a
document that turns out to state no breakdown clears the previous
reading rather than appearing to confirm it.

Clearing a CSV upload had a matching bug in the other direction: the
supplied store has been keyed by source since 0.44.0, and the check for
"is any upload data left" tested the whole facet — which stays truthy
while a factsheet extraction survives — so the card's `upload` pin was
left pointing at data that had gone. Both clears now share one helper
that removes the named source and drops only the pins that source was
backing.

## [0.76.3] - 2026-08-20

### Changed — the resolve popover asks for the scope, not for a verb

The popover offered two buttons, "add as alias" and "change value", beside
one free-text box pre-filled with the row's raw value. Both buttons
changed which node the rows ended up at, so the visible difference between
them was wording — while the difference that actually matters is how far
the change reaches. An alias is a claim about a WORD and moves every row
in every fund that spells it that way, now and on every future import. A
pin is a claim about the ROWS IN FRONT OF YOU and says nothing about the
word. In this cache 100% of holdings rows share their raw value with at
least one other row, and `Industrials` alone covers 432 rows across 9
funds, so the two are not close to equivalent.

Worse, leaving the box at its pre-filled raw made "change value" do both:
it wrote the alias and then pinned the rows. That is why the pin looked
redundant — in that path it was, since the alias alone already resolved
the raw to the new node.

The popover now asks the question directly. One node dropdown, then three
mutually exclusive scopes — change only the selected rows (pin), change all
rows in all funds (move the alias), or leave untouched — and one Apply.
"Leave untouched" is where the popover opens, so looking at what a value
resolves to is not one stray click away from writing something, and a
scope that becomes unavailable falls back to it rather than to the other
scope: silently re-aiming a change is how a pin turns into a vocabulary
edit. The free-text box
appears only under the second scope, where it is what it always should
have been: the alias being written, editable so a wildcard or a
re-spelling can be given. Wildcards were already documented as
alias-only; now they are unreachable from anywhere else.

Clearing moved into the node dropdown as a "no value" entry, because
"this row has no sector" is an answer at row scope rather than a third
reach.

### Fixed — a pin no longer writes vocabulary

Both row-scope paths sent the box text and taught it as an alias when it
did not already resolve. They now send the picked canonical, which always
resolves to itself, so a pin needs no vocabulary change at all. This is
what makes the two scopes genuinely independent rather than one being a
superset of the other.

### Added — the popover says what an alias will take away, before and after

Re-pointing a token is a move: some other node loses it. The endpoint has
reported `moved_from` since it was written and nothing displayed it, so a
change with two ends looked like it had one. The status line now names
both.

The note under the scopes also warns, before Apply, when an edited alias
no longer covers the raw the selected rows actually hold — narrowing it
with a wildcard, say. Those rows become unresolved for that facet, which
is a legitimate thing to ask for and should not be a surprise. The match
test mirrors `resources._alias_key` / `_norm_phrase` so the warning agrees
with what the backend will do.

## [0.76.2] - 2026-08-20

### Fixed — treasuries were being reported as cash

`Sector_definitions.csv` had accumulated sovereign and municipal
spellings in the `cash and/or derivatives` matches column — `u.s.
treasury bonds`, `u.s. treasury strips`, `sovereign issues`, and the bare
state names `new york`, `florida`, `california`, `illinois`, `georgia`.
Twenty-five rows in the local cache were affected, so a thirty-year
Treasury reported in the sector card as cash, which is not a gap in the
data but a wrong answer confidently given. That bucket's own description
says what it is for — "cash, deposit accounts, money-market positions,
and short-dated derivative offsets" — and a government bond is none of
them.

The derivative spellings stay where they are: forward FX contracts,
futures, swaptions and credit indices genuinely are derivative offsets,
and they account for the great majority of that bucket.

### Added — a facet tree can declare its own "does not apply" node

A sovereign issuer has no line of business, so the honest sector answer
is `n/a` — the residual that already means "there is no value to have",
as against `unknown`, "a source could fill this in". Only `unknown`
counts against coverage, so a treasury sleeve no longer drags a bond
fund's sector coverage down towards a gap nothing could ever close.

Until now nothing could reach that answer from the vocabulary.
`FACET_NOT_APPLICABLE` keys on the row's asset class and is consulted
only when the facet value is BLANK, which covers a holdings file that
says nothing about sector but not one whose sector column says
"U.S. Treasury Bonds" — and that is the case the funds here actually
present. The `not_applicable` argument on all four resolvers could
express it, but no caller had ever passed it True; it was dead code.

So `n/a` is now an ordinary node in the resource file, with its spellings
in the `matches` column like any other, and the resolvers know the name
is reserved. It is ONE node rather than a chain of three, because the
level indexes are keyed by name and three same-named rows collapse to a
single entry — only the winning level would have read `n/a`, leaving the
others `unknown`, which is precisely the per-level disagreement the
levels-travel-together rule exists to prevent. The resolver therefore
asserts the residual for the whole tree when it matches that node.

Recognised in the sector, geography and asset trees alike, so the
capability is uniform even though only the sector file declares the node
today. Adding a spelling is now a CSV edit that applies retroactively on
the next read, with no code change and no migration.

### Fixed — the sub-sector view disagreed with every other level

`sub_sector` is derived from `sector_level` rather than read from a
column of its own, on the reasoning that a row matched at sector level
has a sub-sector the source merely did not name. That reasoning does not
hold for a residual: a sovereign issuer has no line of business at any
grain. `sector_key_at_level` already states the rule for bucket keys —
"residuals pass through unchanged at every level" — and this derivation
was the one place not following it, so an `n/a` row read as a sub-sector
gap and counted against coverage there while reading as inapplicable
everywhere else.

## [0.76.1] - 2026-08-20

### Fixed — the Resolve dialog could not do half of what its popover offered

`/api/unmatched_facets` never sent the `<facet>_raw` or `<facet>_pinned`
columns, and the by-row tab's edit popover reads both. The raw column
became the input to "add as alias" in 0.76.0 — that was the point of the
release, since the alias has to be taught the source's own wording — but
this endpoint was not swept with the rest, so `raws` was always empty
there. The alias tab sat permanently disabled and the match chain never
rendered, while the identical popover on the fund page offered both. The
pinned column was missing for the same reason, so every selection in the
dialog reported itself unpinned and an edit made there could not be
undone from there.

Both columns are now sent for every facet, through `facet_raw_field` and
`facet_pinned_field` rather than the hand-spelled key names the loop used
for the node and level pair.

### Changed — one by-row edit context for both surfaces

The fund-page holdings table and the Resolve dialog's by-row tab write
the same rows through the same endpoint and are meant to be edited
identically, but each carried its own copy of the popover context. That
is how the pin controls came to exist on one and not the other: they were
added to the fund page in 0.76.0 and the twin was never swept. The two
genuinely differ in three things — how a selected row names its fund,
what the title says, and what has to be re-read once a write lands — so
those are now parameters of a shared `ufBuildRowEditContext` and
everything else is common. An unpin from either surface also invalidates
the cached portfolio view now, which the fund page's copy did and the
dialog's did not: an unpin changes what a row resolves to, so the rollups
that consume it cannot be left showing pre-edit numbers.

### Fixed — sterling was resolving to pence

`resolve_currency("GBP")` returned `"GBp"`, so every holding quoted in
pounds was labelled with the penny's code. `Currency_definitions.csv` is
right — GBP and GBp are two correct rows for two genuinely different
units — but the loader wrote each canonical code into the alias index
under a case-folded key with a plain assignment, so the later row won the
shared `"gbp"` slot and took the earlier one's meaning with it. Only
currency is exposed to this: ISO 4217 distinguishes a minor unit from its
major one by case alone, and every other tree's names differ by more than
case.

The folded key is now first-wins, so it means the major unit, and
`resolve_currency` tries an exact-case match before falling back to it.
`GBP` and `GBp` each resolve to themselves, `gbp` resolves to the pound,
and `GBX` and `pence` still resolve to the penny.

### Added — the fund page says what the 0.76.0 migration dropped

The migration writes a `_legacy_purge` marker naming the cache categories
it had to drop, because a row stored before 0.76.0 carries no raw text
and there is nothing to re-resolve it from. Nothing read the marker, so
the outcome was an empty holdings table and no account of where the file
went. The fund page now shows what was dropped, why, and which upload
brings it back, and the notice retires itself when the category
reappears — handled in `cache_write`, the one function every writer goes
through, rather than at each of the eight call sites that would each be a
place to forget it.

## [0.76.0] - 2026-08-20

### Changed — facet values are stored as the source said them

Every facet on a holdings row, and every item of a supplied breakdown,
now stores the source's own wording in a raw field, and that field is
the input to every later resolution. The node, the level and all the
level columns are derived from it on each read.

The reason is a defect that had no visible symptom. Resolution used to
happen once, at ingest, and only the canonical it produced was kept: a
row matched through the alias "Aandelen" stored "equity", and the word
the file used was gone. Re-pointing that alias afterwards therefore
changed every future import and not one stored row, because the only
input left was a canonical and a canonical always resolves to itself.
Two funds imported either side of such an edit disagreed about the same
source text with nothing on screen saying why, and the lazy
re-normalisation that was supposed to repair history could not: it was
re-deriving from its own previous conclusion. Supplied breakdowns had
the identical fault one layer up — the commit path stored the resolved
key while the reader's docstring described storing the raw, so the
reader re-resolved a canonical to itself on every read.

Rows a user edits by hand now carry a pin, because the raw text stays
as the source left it and the next normalisation would otherwise resolve
it straight back over the correction. The pin freezes which node the row
means, not what that node means, so a re-parented tree still migrates a
pinned row. The resolve popover shows the pin and can clear it, handing
the rows back to their source.

### Added

- The holdings tables offer the raw value as an extra level in their
  column-header level selector, beside the tree's own levels. It is a
  display level only, deliberately absent from `FACET_LEVELS`: raw text
  has no parent, no children and no partition property, so rolling up by
  it would put one bucket per spelling on a card and a target measured
  at that grain would mean nothing.
- Writing an alias now moves it. A token means exactly one node, and
  adding without removing left two claimants for the loader to arbitrate
  by depth behind a console warning nobody reads. Wildcard claims are
  reported but never moved — re-pointing `*bank*` from one row's edit
  would reclassify every value it covers — and the exact alias wins
  anyway, since lookup tries exact matches first.
- A canonical name can no longer be written as an alias of another node.
  The loaders already ruled that a canonical outranks any alias, so such
  a write changed the file and not the answer while reporting success.
  It is refused with the reason, and the dialog points at "change value"
  instead.

### Fixed

- `add_currency_alias` rebuilt Currency_definitions.csv under a
  `code,name,symbol,matches` header the file had not used since the
  resource schemas were unified; a successful write would have replaced
  the currency vocabulary with a flat list. It never fired only because
  the lookup it did first could not match — the same latent bug, and the
  same luck, that the geography writer had. All four facets now share
  one writer, so there is no per-file copy left to diverge.
- `resolve_facet_node` answered at the level the source named for sector
  and country but not for asset, which alone fell through to a helper
  that answers at the facet's own level. A factsheet saying "Aandelen"
  became the middle-level "shares and options" — a grain no source had
  stated — while a holdings row with the same word became "equity".

### Migration

Facet data stored before this release has no raw text to resolve from,
and inferring one from the stored node would fabricate evidence the
source never gave. Cached holdings and supplied breakdowns written under
the old shape are therefore dropped on first read rather than migrated,
and the fund records which categories went so the screen can say what to
re-import. Nothing outside `cache/` is touched.

## [0.75.3] — 2026-08-19

### Changed
- **The edit-holding modal shows each facet's tree, not just its level.**
  Under every facet picker sat one line reading `stated at Sub class
  level`, which said that picking "equity" was a coarser claim than
  picking "regular stock" without saying what the coarse claim covers.
  Each picker now draws the same **resolves to** block the resolve
  popover draws, from the same renderer, and redraws it on every change —
  the point of the hint is to describe the choice in front of you, not
  the one the row arrived with.

  It reads the tree from the vocabulary rather than from the row, since a
  node the user just picked has no row yet. The backend supplies that
  tree: `facet_alias_targets` entries now carry a `path`, the whole
  `{level: value}` the node resolves to. Deriving it in the browser would
  have been a second authority on what a node means — the rules are
  per-tree, asset filling a finer level through single-child nodes where
  sector and country never do — and it would drift from the real one the
  first time a single-child node gained a sibling.

  The upload dialog's default pickers and the cash-position editor
  deliberately keep their one-line form: they are a compact row per
  column, and four three-line blocks would be most of the dialog.

- **The resolve popover's "should mean" picker starts on the matched
  node.** With one spelling in front of you there is exactly one right
  answer to begin from, and the usual edit is to confirm or nudge it
  rather than hunt for it among 263 countries. A mixed selection still
  starts blank, as does an unmatched value — which by definition resolves
  to nothing.

- **`resolve_facet_tree(facet, raw)`** joins the three per-facet tree
  resolvers as the one dispatcher over them, currency included with its
  one-level tree of the same shape. Callers that know their facet still
  call the specific resolver; this is for the ones iterating
  `FACET_LEVELS`, which would otherwise each grow a four-branch `if` that
  is a fifth place the per-tree rules can drift from the four that own
  them.

### Fixed
- **The resolve popover's value box offered the wrong text to alias.** It
  was seeded from the column named after the facet rather than from the
  node the row actually stated, and those differ whenever a source spoke
  at a finer grain: a row matched at sub-sector level carries
  `semiconductors` in `sector_node` and the derived `technology` in
  `sector`. Harmless while the picker started blank; with it now starting
  on the matched node, one click of **add as alias** would have taught
  `technology` as a name for `semiconductors`, and every fund's
  technology sleeve would have resolved into a chip sub sector. Both edit
  surfaces now read the stated node — which for an unmatched row is the
  preserved raw text, exactly what is worth aliasing.

## [0.75.2] — 2026-08-19

### Changed
- **The resolve popover now shows the whole tree a value resolved into,
  not just the node's name.** The line said `matched to beverages`, which
  is true and not enough: a bare node name does not say what grain the
  source actually asserted, and that grain is the entire difference
  between the aliases a user is about to write. "Consumer defensive"
  alone leaves open whether the row was stated at sector level, or at
  sub-sector level as "beverages" and rolled up — and an alias attaches
  to the node the text locked into, nowhere else.

  Under `currently` there is now a **resolves to** block listing every
  level of the facet top to bottom, finest first — the same order the
  level chips and the node picker use, so the popover does not invert the
  tree the rest of the app draws. The matched level is marked and drawn
  in the accent colour; levels the tree does not reach from that node
  show an em dash rather than being omitted, because "this row has no sub
  sector" is a fact worth stating. A value that resolved to nothing still
  reads `unmatched` as before.

  All three surfaces that open the popover pass the chain through one
  helper reading the row's level columns, so the fund page's bulk edit
  and the Resolve dialog's by-row tab cannot describe one row's tree
  differently. The by-value tab passes none: every value in that list is
  unmatched by definition.

- **`/api/unmatched_facets` sends every level of every facet tree**, plus
  the `<facet>_node` / `<facet>_level` pair naming what was stated. It
  previously sent one column per facet — whichever the dialog's table
  happened to display — which left the by-row tab unable to build the
  chain the fund page reads straight off the row.

- **The holdings tables' facet tooltips read the same chain.** They
  built their own copy of the levelled read, which is how a near-copy
  drifts from the dialog that edits the value; both now go through
  `facetChainFromRow`, and the tooltip's level names match the popover's.
  `ufIsCanonical` gained a memoised set behind it, since the chain helper
  is now asked once per currency cell and a table renders up to 5000 rows.

- **One registry of spelled-out level names.** `LEVEL_LABELS_LONG`
  replaces the targets editor's private `TG_LEVEL_LABELS`, which knew
  only the sector and country levels; an asset target's level heading
  therefore read `sub_class`. It now reads `Sub class`, and the resolve
  popover's chain uses the same names — as does the single-row holdings
  editor's `stated at … level` hint, where the chip abbreviations "Sub"
  and "Super" only read unambiguously with their neighbours beside them.

### Fixed
- **A resolved currency's tooltip claimed the value was unresolved.**
  `holdingsFacetCell` read the grain from the row's `<facet>_level`
  column, and currency has none — its tree is one level deep, so there is
  nothing to stamp. An absent stamp was read as a failed match, so every
  currency cell in both holdings tables said `Stated as "USD"
  (unresolved).` The grain now comes off the chain, which answers for all
  four facets alike.

## [0.75.1] — 2026-08-19

### Fixed
- **Clicking a level chip in a holdings column header re-sorted the
  column.** The chips sit inside a `<th>` whose own click sorts, and each
  button stopped the click itself — but `.bkseg` draws a 1px border and
  rounds its corners under `overflow:hidden`, and those pixels belong to
  the containing span, not to any button. A click landing on the group's
  left edge or a clipped corner therefore missed every button, reached
  the `<th>`, and sorted instead of changing the level. The leftmost chip
  took the worst of it, its left edge being the most exposed.

  The container now absorbs the click, which covers the buttons, the
  border, the corners and the gaps between them in one place; the
  per-button guards are gone rather than kept alongside it. Both holdings
  tables get the fix, since both build their chips from
  `holdingsLevelChips`.

## [0.75.0] — 2026-08-19

### Changed
- **The resolve popover now states what is there and offers two explicit
  actions.** A line at the top names the value the rows actually hold and
  what it currently resolves to (or `unmatched`), reading
  `multiple values (N)` for a mixed selection. Below the **should mean**
  picker, a **new value** box — prefilled with the current value, blank
  when several are selected — sits beside two buttons:

  - **add as alias** writes the box text (wildcards allowed) into the
    resource file as a name for the chosen node. Rows are untouched. With
    several values selected each contributes its own spelling.
  - **change value** writes the box text to the selected rows. If the text
    does not already resolve to the chosen node it is taught as an alias
    in the same pass, so the rows resolve rather than returning as
    unmatched. If it already IS canonical, that is fine and no alias is
    written.

  This replaces one Apply behind two checkboxes, where the row box was
  disabled unless a checkbox was ticked and could not be emptied at all.
  The two operations make different claims — the vocabulary was
  incomplete, or the source was wrong — and now say so as two buttons.

### Fixed
- **"Clear the value" wrote the string `__undefined__` into the row.** The
  option existed in the UI and the sentinel reached no branch of any
  endpoint: `api_unmatched_facets_apply` wrote it verbatim, so clearing a
  facet produced a new unmatched value spelled `__undefined__`.

  Clearing is now an empty box, and an empty value means clear all the way
  down. `api_unmatched_facets_apply` treated an empty string as "skip this
  facet", which is why the honest spelling did not work either and the
  sentinel was invented; naming a facet in `sets` is now taken as the
  request to set it, and setting it to nothing clears it.
  `api_unmatched_values_apply_rows` gained an explicit `clear` flag rather
  than reading an empty `canonical`, so an accidentally-blank canonical
  still fails loudly instead of silently wiping every matching row.

- **A value that was already canonical could not be assigned.** The row
  box was seeded from the picker and locked unless "change the rows" was
  ticked, so setting rows to an existing node meant fighting the dialog.
  It is now just text in a box, and the note says whether it will resolve
  directly or be taught as an alias first.

## [0.74.0] — 2026-08-19

### Added
- **Bulk facet editing on the fund page's holdings table.** Tick rows,
  click **edit** on the Sector, Asset, Country or Currency column header,
  and set what they mean — with the option to write the alias at the same
  time. Shift-click extends a range from the last row ticked, and the
  select-all box carries a tri-state for a partial selection.

  This is the Resolve dialog's by-row gesture, brought to where the rows
  already are. It reuses that dialog's machinery outright —
  `ufOpenResolvePopover` for the picker, `ufWriteAlias` for the alias,
  `/api/unmatched_facets/apply` for the write — rather than a second
  implementation, because two surfaces writing facet values two ways is
  how they come to disagree about what a value means.

  The portfolio holdings table deliberately gets no edit affordance: its
  rows are aggregates, so one line can merge the same security held
  through three funds and "set this row's sector" has no single row to
  write to. Noted at the site of the asymmetry rather than left to read
  as an oversight.

### Changed
- **The factsheet dialog defaults its date to the previous month end.**
  Issuers publish as at a month end, and the sheet available on any given
  day is the previous month's. Blank was the old default and today was
  the tempting one; both read as fresher than the document is, which is
  the one thing the staleness threshold exists to measure honestly. A
  date already stored for that factsheet still wins.

### Fixed
- **The by-value tab's column filters lost focus after every character.**
  `ufValSetColFilter` re-rendered the whole dialog body on each
  keystroke, destroying the focused input, so typing a second character
  meant clicking back into the box. The by-row tab had solved this when
  it was written — it tags its inputs and restores focus after the
  rebuild — and the by-value tab, added later, never picked it up.

  The restore is now one helper both tabs call, so the next tab cannot
  be added without it.

## [0.73.2] — 2026-08-19

### Fixed
- **The resource-file table in Tools never rendered.**
  `renderResourceVersions` took `versions` and `fingerprints` and bailed
  out when `Object.keys(versions)` was empty. v0.64.0 removed the
  declared `Version=N` from the resource files and
  `/api/resources/reload` stopped sending the key with it, so `versions`
  had been `{}` on every call since — and the function returned before
  writing any markup. The fingerprints were being sent and never shown.

  Renamed to `renderResourceFingerprints` and keyed off the fingerprints,
  which is what the endpoint actually returns; the version column is gone
  with the version. The container id moves from `resVersions` to
  `resFingerprints` to match. A renderer keyed off the one field its
  producer sends cannot fall out of step the same way twice.

### Changed
- **`resources.py`’s header comment described a scheme removed three
  releases earlier.** The block above `_VERSION_RE` still explained the
  `Version=N` line in the present tense, worked example included, while
  `parse_resource_csv` seventy lines below recorded its removal in
  v0.64.0. Two statements in one file, the stale one first — which is how
  it came to be repeated as fact. Rewritten to describe fingerprinting,
  keeping the version scheme and the reason it failed (a number only
  moves when somebody remembers to move it, so hand-edited files kept
  their old one) as the rejected predecessor.

### Added
- **`IMPORT_GUIDE.md`** — a user guide for importing funds and ETFs:
  starting the app, the menu structure, every route a fund can arrive by,
  the resource files and unmatched-value resolution, factsheets, holdings
  and per-facet CSV uploads, peer grouping, the fund-edit and
  holding-edit dialogs, and every element of the four fund tiles.

## [0.73.1] — 2026-08-18

### Fixed
- **Peer groups were a single catch-all for most funds.** A fund whose
  stored `focus_type` was still the pre-v0.68.0 value `"region"` lost
  its focus entirely, and with it its peer group. `normalise_fund_structure`
  tests the value against `FOCUS_TYPES` — `("none", "geography",
  "sector", "thematic")` — and reset anything outside it to `"none"`,
  blanking `focus_detail` along with it. `LEGACY_FOCUS_TYPES` exists to
  map `region` forward and was applied in `scoring.peer_key`, but that
  runs far downstream: the merge had already normalised, discarded the
  value and handed on a fund claiming no focus at all.

  The migration now happens at the gate — in `normalise_fund_structure`,
  before the membership test — because that function is what every
  stored structure passes through on its way to being used. Applying it
  in one of the two places that needed it was the whole defect.

  Surfaced by BGF US Growth (`LU0938162269`), stored as
  `region` / `northAmerica`, sharing a 16-fund peer group with iShares
  Essential Metals Producers (`IE000ROSD5J6`), which has no focus at
  all. On this install the fix moves 31 of 47 funds out of the
  `<class>|none|` bucket and into real groups: `equity|geography|europe`
  (4), `equity|sector|energy` (3), `equity|geography|northamerica` (2)
  and so on, leaving 16 genuinely unfocused funds behind.

### Note
Correct grouping means smaller groups, and a group below
`MIN_PEER_GROUP` yields no peer score by design — a percentile within a
group of one is 100 by construction. Expect more funds to show no peer
score than before. That is the honest reading: they were previously
being ranked against funds that were never their competitors.

## [0.73.0] — 2026-08-18

### Changed
- **The by-value tab selects and edits in batches, like by-row.** The
  per-row `resolve…` button is gone. Every row carries a checkbox,
  shift-click extends a range, the header ticks everything listed, and
  one `edit (N)` button resolves the lot through the same popover the
  by-row tab opens. Several misspellings of one thing is the ordinary
  case on this tab, which is exactly why resolving them one at a time
  was tedious.

- **The list shows one facet at a time, and the chips became its
  selector.** Editing a batch means choosing a canonical node, and a
  node belongs to exactly one facet — so a selection spanning two facets
  could not be given a single answer. Rather than police that at Apply
  time with an error the user can walk into, the illegal state is made
  unrepresentable: the chips are a single choice rather than a set of
  toggles, and the list holds one facet because the edit acts on one
  facet.

  The tab opens on the HEAVIEST facet, matching the rule the value list
  already sorted by — the money decides what is worth resolving first.
  Switching facet clears the selection and the column filters: carrying
  either would leave ticks and filters in force over rows nobody can
  see.

- **The alias field follows the selection.** One ticked value gets the
  editable box, so the wildcard syntax is still reachable. Several
  contribute one alias each, written as they stand, because a single box
  could not say which of them it was changing.

### Removed
- `ufOpenValuePopover`, the per-row opener, and the multi-facet filter
  set it was built on. Leaving the single-row path beside the batch one
  would have been a second implementation of the same operation.

## [0.72.3] — 2026-08-18

### Fixed
- **An upload commit with Yahoo enrichment no longer looks hung.** It
  was not hanging; it was working in silence. A per-holding lookup costs
  about 0.01s when the symbol is already cached and about a second when
  it is not and the full fallback chain runs — variant probe, ISIN
  search, name search. A few hundred unrecognised rows is therefore
  legitimately minutes of work, and nothing was printed or shown for any
  of it, so the only available reading was "hung".

  The commit now prints a starting line, progress every 25 rows with
  elapsed time and an ETA, and a closing summary of what was filled,
  what was unrecognised and what had no ticker. The dialog carries the
  same warning where the user is actually waiting, including the row
  count and a note that Cancel stops it cleanly.

### Note
Two changes in this release series made that silence much more likely to
be met. v0.72.0 made enrichment run for MAPPED facets as well as
unmapped ones — intended, and the reason it can now touch every row of a
file whose column was already mapped. v0.72.2 expired stale negative
aliases, which on this install freed 388 previously-written-off symbols
to be probed once more, at about a second each. Both are one-off or
intended costs, but together they turned a fast commit into a slow one
with no explanation attached.

## [0.72.2] — 2026-08-18

### Fixed
- **A transient Yahoo failure no longer becomes a permanent one.**
  v0.71.4 stopped an EXCEPTION being cached as a miss, which covered the
  TLS outage. It did not cover the other shape: Yahoo answering 200 with
  an empty payload under rate limiting. That is indistinguishable from a
  genuine "no such symbol" at the point of decision, so no amount of
  classifying gets it right — `SAP.DE` was written off this way during
  the v0.72.1 diagnosis, on a symbol Yahoo knows perfectly well.

  Negative cache entries now EXPIRE; positive ones still do not. A hit
  is a durable fact about the world — `AIR FP` is `AIR.PA` — while a
  miss is a statement about one moment, and the things that produce one
  are usually temporary. This heals every cause at once, including
  causes not yet met, without having to detect any of them.

  The TTL escalates: 7 days after the first consecutive miss, then 14,
  28, 56, capped at 90. One unlucky probe is forgiven in a week, while a
  spelling that has failed five times running — a futures code, a swap
  leg, a cash line — settles into silence instead of being re-probed
  forever. A hit clears the count, so a spelling that starts working is
  forgiven its history. Applies to both negative caches: the symbol
  alias map and the `_found: False` entries in the symbol-info cache,
  which previously held for the full 90 days.

  An entry with a missing or unparseable timestamp counts as expired:
  re-probing costs one lookup, trusting it wrongly costs the holding.

## [0.72.1] — 2026-08-18

### Fixed
- **Yahoo is reachable again.** Every live lookup had been failing with
  `curl: (60) SSL certificate problem: unable to get local issuer
  certificate`, which reads as a certificate problem and is not one.
  yfinance builds its session as
  `curl_cffi.Session(impersonate="chrome")`, and `"chrome"` aliases the
  NEWEST profile curl_cffi ships. From Chrome 124 those profiles enable
  post-quantum key exchange, and the resulting ClientHello fails through
  TLS-inspecting middleboxes. Measured against Yahoo with curl_cffi
  0.15.0: `chrome99`…`chrome123`, `edge99`, `edge101` and `safari15_5`
  all succeed; `chrome124`, `chrome131` and `chrome133a` all fail.
  Pointing the session at a CA bundle changes nothing, because the trust
  store was never the problem.

  `porxpy.yf_session` pins the profile to `chrome123`, overridable with
  `PORXPY_YF_IMPERSONATE`. **Certificate verification stays enabled** —
  this is not a `verify=False` workaround and must not become one.

  The patch rebinds `new_session` in every yfinance module that imported
  it, not just on `_http`: `data.py`, `base.py`, `multi.py` and
  `scrapers/history.py` each do `from ._http import new_session`, which
  copies the function object at import time, so patching one attribute
  left all of them calling the original and the fix did nothing.

- **`Information Technology` resolves to `technology`.** The alias the
  `config.py` comment cites as the worked example of what the `matches`
  column is for was missing from it. `info tech` joins it. A bare `it`
  was tried and withdrawn: the file's own loader flagged it as already
  claimed by `semiconductors`, and a two-letter token is a poor alias.

### Changed
- **One asset entry in Settings → enrichment fields.** `sub_class` was
  listed beside `asset_class`, the same taxonomy at two grains — the
  shape removed from storage in v0.70.0, from the Resolve dialog in
  v0.70.2 and from the upload mapping in v0.72.0. `ENRICHABLE_FIELDS`
  drops it too. Saved settings naming it still load: the value is
  filtered out against the canonical list, so no migration is needed.

### Note
Seven negative symbol aliases written during the outage were cleared.
They were the mechanism v0.71.4 fixed — a miss caused by an unreachable
network cached as though Yahoo had answered — and would otherwise have
outlived it.

## [0.72.0] — 2026-08-18

### Changed
- **One column mapping per facet in the upload dialog.** The separate
  `sub_class` mapping is gone. It asked the user to answer the same
  question at two grains and let the two disagree — the same shape
  v0.70.0 removed from storage and v0.70.2 removed from the Resolve
  dialog. The single **Asset** column is read at whatever grain the file
  wrote it, and `Asset_definitions.csv` derives the rest. The retired
  column's aliases (`sub class`, `sub asset class`, `instrument type`)
  move onto the Asset column, so a file that used those headers still
  auto-maps.

- **Yahoo enrichment is offered for every facet, mapped or not.** It was
  available only for a facet with NO column mapped. An issuer file
  carrying a sector column with holes in it is the ordinary case, and
  that rule meant the only way to fill the holes was to leave the column
  unmapped and throw away the rows that did have a value. Enrichment
  writes are blank-only, so the file still wins wherever it actually
  said something: the documented precedence, file > Yahoo > default, is
  unchanged.

  For the asset facet, enrichment takes the FINEST answer Yahoo gives —
  its `sub_class` where present, else its `asset_class` — because the
  resolver derives coarser levels from a finer value but can never
  derive a finer one from a coarser.

- **A default value now fills gaps in a mapped column too.** The default
  pass skipped any field whose column was mapped, which made "fill in
  when no value is present" untrue in exactly the case it was wanted:
  a mapped column with blank cells. The apply loop was already
  blank-only, so a cell that has a value is still never touched.

- **Every facet's default is a dropdown from its definitions file**,
  at any level, replacing the free-text boxes on sector, country and
  currency. A typed default could name something the tree has never
  heard of, which lands straight back in the unmatched list.

- **One option builder for every facet dropdown in the app.**
  `facetNodeOptions` now emits inline `level-name` labels —
  `sub-money market`, `sector-technology` — and `ufFacetOptionsHtml`
  delegates to it. There were three presentations of one list
  (optgroups, `level — Label`, `key (Level)`); there is now one.

### Removed
- `assetDefaultOptions`, whose only job was to relabel the shared
  builder's blank option, and the retired `sub_class` entry in the
  upload dialog's seed mapping, so stale saved prefs cannot resurrect a
  mapping the dialog no longer offers.

## [0.71.4] — 2026-08-18

### Fixed
- **An unreachable Yahoo no longer reports as "Yahoo has never heard of
  your holdings".** `extract_symbol_info` swallowed the transport
  exception and returned an empty result, so `enrich_existing_holdings`
  counted it under `rows_yahoo_not_found`. A TLS, DNS or proxy failure
  was therefore indistinguishable on screen from a genuine miss: every
  row "not found", nothing filled, no error anywhere, and enrichment
  looking simply broken. The probe now carries `_error` out with the
  empty result, and the stats block gains `rows_lookup_failed` and
  `lookup_error` alongside the existing miss counter.

- **A miss caused by an outage is no longer cached as a negative
  alias.** The "all fallbacks failed" exit wrote `alias_put(cleaned,
  None)` unconditionally, so every symbol probed during an outage stayed
  negatively aliased after it ended and was skipped on the next attempt
  without any lookup at all — the outage outliving itself, one symbol at
  a time. The negative alias is now written only when Yahoo actually
  answered.

### Note
These two fix the REPORTING of a Yahoo failure. On this install the
underlying failure is environmental: yfinance 1.5.1 builds its session
as `curl_cffi.Session(impersonate="chrome")`, and curl_cffi 0.15.0's
Chrome-impersonation TLS path cannot verify Yahoo's certificate chain
here. A plain `curl_cffi.Session()` and `requests` both succeed against
the same URL, and pointing the impersonating session at certifi does not
help. Until that is resolved no live lookup can succeed, so enrichment
has nothing to write regardless of what the dialog offers.

## [0.71.3] — 2026-08-18

### Changed
- **One resolve popover, both tabs.** By-value and by-row ask the same
  two questions — what does this mean, and which repair do you want — so
  they are now one control parameterised by context rather than two that
  drift. The by-row `edit` button and the by-value `resolve…` button open
  the same dialog, with the same two independent checkboxes, the same
  editable alias field and the same editable row value.

- **Dropdown labels read `level-name`** — `sub-money market`,
  `sector-technology`, `country-japan` — in both tabs. The level leads
  because it is the part that changes what the choice MEANS: naming a
  sub sector says the source told us the sub sector, naming the sector
  says it told us only that.

- **The by-value list gains the by-row machinery**: facet chips as the
  pre-defined filter, a text filter per column, and sortable headers.
  On this tab the facets ARE the presets, since there is one row per
  (facet, spelling). Filter state is deliberately separate from the
  by-row tab's: a facet chip means "rows unmatched in this facet" there
  and "values OF this facet" here, and sharing the object would have
  made switching tabs silently reinterpret the filter.

- Column padding on the by-value list, so the funds count and the
  sources text no longer run together.

### Fixed
- **The by-row `sub_class` column no longer offers an edit button.**
  `LEVELLED_FACETS` is keyed by facet and `sub_class` is a LEVEL of
  `asset_class`, so `ufCanonicalsFor('sub_class')` could only ever
  answer "unsupported" and the button was guaranteed to alert rather
  than open — the same level-mistaken-for-facet error v0.70.2 fixed on
  the server. Asset is one facet with three levels and gets one control;
  editing it rewrites the stated node and re-derives every level.

### Removed
- `ufOpenEditPopover`'s and `ufOpenValuePopover`'s separate
  implementations, `ufApplyColumnEdit`, `ufApplyValuePopover` and
  `ufValuePopoverSync`. Two dialogs doing one job is how the two tabs
  came to disagree in the first place.

## [0.71.2] — 2026-08-18

### Changed
- **The by-value tab adopts the by-row layout.** Its inline select and
  pair of rival buttons are replaced by a `resolve…` button opening the
  same popover the by-row tab has always used. The inline form could not
  show the alias it was about to write, could not let it be edited — the
  only way to reach the v0.71.0 wildcard syntax — and could not let the
  value written to the rows differ from the node picked.

- **One canonical picker for both tabs.** They formatted the same list
  from the same endpoint two ways: `sub_sector — Semiconductors` in one,
  `semiconductors (Sub)` in the other. `ufFacetOptionsHtml` now builds
  the list for both, so the vocabulary learned in one tab reads
  identically in the other.

- **One rule for alias versus row change.** By-row offered them as
  independent options (a checkbox on top of the edit); by-value offered
  them as rival buttons, forcing a choice between two things a user may
  well mean at once. Both tabs now present two independent checkboxes
  with the same wording.

### Added
- **The alias is editable before it is written**, prefilled with the raw
  value, with the wildcard forms shown beneath the field. Writing
  `*treasury bills*` where the source said
  `US TREASURY BILLS 4.5% 2026` is now a normal thing to do from the
  dialog rather than a hand edit of the CSV.

- **The row value is editable before it is applied**, prefilled from the
  dropdown. A value that is not canonical is not refused: it is written
  as an alias of the picked node in the same apply, so the rows it lands
  on resolve rather than returning immediately as unmatched. The popover
  says which of the two cases you are in as you type.

### Removed
- `ufApplyValue`, `ufApplyValueRows`, `ufSetValuePick` and the
  `ufValuePicks` / `ufValueBusy` / `ufValueError` state they needed. The
  popover owns its own fields and errors, and leaving the inline path in
  place would have been a second implementation of the same operation —
  the kind that drifts until the two tabs disagree again.

## [0.71.1] — 2026-08-18

### Fixed
- **A level chip no longer reorders the holdings list.** Changing which
  grain a column displays is a question about what you are looking at,
  never a request to re-sort, and the rows reshuffling under the pointer
  made the table impossible to read across — you lost your place every
  time you looked at the same holdings a different way.

  v0.70.1 moved the sort key onto the new level whenever the column
  being re-levelled happened to be the sort column, reasoning that the
  rows would otherwise be ordered by a value no longer on screen. That
  is true, and it is the lesser problem. The order now stays put, and
  the header stops claiming the column is sorted: `data-key` has already
  followed the chip, so the indicator pass finds no match, drops the
  `sorted` class and clears the arrow. The list is ordered by a previous
  choice with nothing on screen asserting otherwise, and one click on
  the header sorts at the new level if that is what was wanted.

  Both holdings tables share `setHoldingsTableLevel`, so the fund page
  and the portfolio X-ray are corrected together.

## [0.71.0] — 2026-08-18

### Added
- **Wildcard aliases.** A leading and/or trailing `*` in a `matches`
  cell says where the alias may sit inside the value, instead of
  requiring it to BE the whole value:

  | Written in `matches` | Matches when the incoming value … |
  |---|---|
  | `*treasury bills*`   | contains it anywhere |
  | `*treasury bills`    | ends with it |
  | `treasury bills*`    | starts with it |
  | `treasury bills`     | is exactly it (unchanged) |

  Issuer holdings say `US TREASURY BILLS 4.5% 2026`, not `treasury
  bills`, and until now the only way to catch that was to enumerate
  every instrument line by hand or leave it unresolved forever.

  Exact always beats wildcard, and among wildcards the longest needle
  wins, so `government bond` outranks `bond` and the answer never
  depends on the order rows happen to sit in the file. A bare `*` is
  ignored rather than honoured: a rule saying "anything at all is
  technology" is an authoring mistake, and obeying it would be
  unexplainable from the screen.

  Available in every tree — asset, sector, geography, currency — because
  all four now share one matcher.

### Changed
- **One alias matcher for every facet tree.** Sector, asset, country and
  currency keyed their aliases with `_normalise_key` (lowercase and
  strip, exact on the whole string) while region and super-region used
  `_norm_phrase` (punctuation collapsed to spaces). `europe ex-uk` and
  `europe ex uk` both resolved, but `financial-services` and
  `united-kingdom` did not, though their spaced forms did — the country
  facet disagreeing with itself between its own levels. Every tree now
  normalises through one function, and matching runs through one lookup.

  This is a widening: everything that matched before still matches, and
  punctuation variants that previously had to be enumerated by hand come
  for free. On the development install it resolved 12 of 67 outstanding
  unmatched values on the spot, with no file edits.

- `Asset_definitions.csv` gains `*treasury bills*` on `government bond`
  as the first wildcard in the shipped data. Move it to
  `money market short term` if you class bills as cash rather than
  government debt — both readings are defensible and the file is where
  that choice belongs.

## [0.70.2] — 2026-08-18

### Fixed
- **Asset values in the Resolve dialog had no dropdown.** The by-value
  tab reported holdings-row asset misses under the pseudo-facets
  `holding_asset_class` and `sub_class`. v0.70.0 merged both into the
  one asset tree and retired those names from `FACET_ALIAS_LEVELS`, so
  the alias-target endpoint answered 400 for them and every asset row in
  the dialog rendered "no vocabulary loaded" with no way to resolve it.
  They are one facet now and are reported as `asset_class`.

- **Levels leaked into the dialog as facets.** The endpoint skipped
  `region` and `super_region` — country's non-facet levels — with a
  comment explaining that a value is one problem and must be listed
  once, under the facet. v0.70.0 gave sector and asset their full level
  sets and the skip was never widened, so `sub_sector`, `super_sector`,
  `sub_class` and `super_class` came through as pseudo-facets with no
  dropdown behind any of them. The rule is now derived from
  `FACET_LEVELS` rather than restated per facet, so a future level
  cannot reintroduce this.

### Added
- **The by-value tab offers both repairs, not just the alias.** *Add
  alias* says the vocabulary was incomplete — the spelling was a
  legitimate name all along, so it goes to the resource file and every
  occurrence everywhere resolves on next read. *Change rows* says the
  source was wrong — these particular holdings rows should hold a
  different value, and nothing is written to the resource file, so a
  later import of the same spelling is unresolved again. Neither is a
  safe default for the other, so both are offered and neither is
  pre-selected.

  *Change rows* is available only where holdings rows actually carry the
  value. A value from Yahoo, a factsheet or an uploaded CSV has no row
  to edit — which is why the alias path exists — and the button says so
  rather than being silently inert. `POST /api/unmatched_values/apply_rows`
  is the writer, using the same one-stated-node rules as the by-row tab.

## [0.70.1] — 2026-08-17

### Fixed
Three symptoms of one cause on both holdings tables: the header row is
built once per fund/portfolio load, but changing a level only re-rendered
the `<tbody>`. Everything living in the header therefore kept its startup
value.

- **The active-level chip did not move.** The rows changed, the indicator
  did not. The chip row is now repainted in place on every level change.
- **Column filters kept the old level's options.** They are rebuilt for
  the new level. A selection that still exists at the new level is kept —
  filtering Country to "japan" survives a switch to Region, because japan
  is a region too — and one that does not is cleared, along with its
  filter state, rather than left showing a value that can no longer match.
- **Sorting used a level that was not on screen.** The sort key was baked
  into the header when it was built, so a levelled column always sorted on
  the startup level. The level is now resolved at click time, `data-key`
  and the arrow marker follow the chips, and if the column was already the
  sort column its key moves with it.

- **Sort indicators leaked between the two tables.** Both carry class
  `hfullwrap`, and the fund table's indicator pass used a class selector,
  so sorting one table stamped and cleared arrows on the other's header.
  All header selectors are now scoped by id. Pre-existing, surfaced while
  fixing the above.

## [0.70.0] — 2026-08-17

### Changed
- **One asset taxonomy.** `Asset_definitions.csv` replaces
  `Holdings_class_definitions.csv` and `Fund_class_definitions.csv`.
  Those were two files describing one taxonomy at two grains, and they
  disagreed on spelling — `bond` in one, `fixed_income` in the other,
  for the same concept. **Both old files must be deleted from
  `resources/`.** Levels, finest first: `sub_class`, `asset_class`,
  `super_class`.

- **Every levelled facet now stores its whole tree.** A row's answer is
  the value the source stated plus the grain it stated it at; all three
  level columns are derived from that pair on every read and stored
  alongside. Asset gained this earlier in the release; sector and
  country only stored their middle level until now, which is why no
  holdings table could offer a sub-sector, super-sector, region or
  super-region view. `sub_sector`, `super_sector`, `region` and
  `super_region` join the row schema.

  The resolvers had been returning all three levels the whole time. The
  fold received them and wrote one down.

- **A holding's facet is edited by naming a node.** The editor sends one
  value per facet under `<facet>_node`, at whatever level the user
  picked, and the backend resolves it against the definitions file and
  rewrites every level from it. Blank at a level means the tree does not
  reach it — the source stated something coarser, or the node has
  siblings and deriving downward would be a guess.

### Frontend
- **One picker per facet in the holdings editor**, listing every node at
  every level. The paired asset dropdowns are gone: they forced the user
  to answer at two grains, which made a defaulted sub class
  indistinguishable from one a source had stated.

- **Level chips on both holdings tables and on the asset breakdown card.**
  Asset Class and Sub Class collapse into one Asset column. Cells show
  the chosen level, with the full chain and the row's stated value in
  the tooltip. Filters, sorting and search follow the displayed level;
  fund and portfolio tables keep independent chip state.

- **`—` and `unknown` now mean different things.** `—` is "this row says
  nothing about this facet"; `unknown` is "it says something, just not
  at the grain you are looking at". The old single-column view could not
  distinguish them.

- **Two hardcoded asset enums deleted** — the upload dialog's four-value
  default list and the cash tab's `[cash, bond]` pair. Both were second
  copies of a taxonomy that lives in a file, and both had drifted:
  `bond` is not a node in the tree, it is an alias of `fixed income`.

- **Portfolio holdings merge on the stated node.** The per-field overrule
  picks a winner independently for each field, so merging the derived
  columns could take a sub sector from one fund and a super sector from
  another and describe a tree that does not exist. One fact wins and the
  rest follow from it.

### Fixed
- **Sector and country edits are no longer silently discarded.** The
  patch endpoint accepted only the derived level columns, and a special
  case turned an incoming `asset_class` or `sub_class` into the row's
  stated value. Every other pick was accepted and then overwritten by
  re-derivation, because re-normalisation reads the stated value first.
  A sector or country edit changed nothing at all; nor would a
  super-class asset pick. The endpoint returned 200 with the freshly
  re-derived row, so the screen redrew showing the old value and said
  nothing. One rule now covers all three facets.

- **Cash positions no longer coarsen, or vanish, on re-read.**
  `coerce_cash_position` rebuilt each position from
  `CASH_POSITION_FIELDS` alone, dropping the stated values. The fold
  then had only a derivation to re-resolve from: a position stated at
  sub-sector level came back one level coarser every time it was read,
  and one stated at region level came back **empty**, because the
  country column of such a row is legitimately blank and nothing else
  remained. Non-schema extras are now carried through, the same rule
  `coerce_holdings_row` already applied.

### Migration
- Facet migrator generation 4 to 5. Cached blobs re-derive their sector
  and country levels on first read. The stated value and its grain were
  already stored, so this re-derives rather than guesses — no row loses
  or gains an assertion.
- **Back up `funds/` before installing.** v0.69.4 does not understand
  folded rows.
- Stored asset targets migrate: `fixed_income` to `fixed income`, `cash`
  to the asset-class node, `mixed` and `commodity` dropped and logged by
  name. Both the legacy and levelled target shapes are handled;
  idempotent.

## [0.69.4] — 2026-08-16

### Fixed
- **A pin to "yahoo" is no longer stored.** Every other source is a real
  instruction — ask justETF, read the factsheet, use my value. "Ask
  Yahoo" is what happens anyway, so storing it recorded nothing while
  doing two kinds of harm: it froze a snapshot of the value, and it
  outranked the seed's own origin in the provenance map. Merely opening
  the Edit dialog and selecting Yahoo on a field was enough to write
  one — which is why a money-market fund classified from its own name
  reported "Yahoo" after v0.69.3 had supposedly fixed exactly that.

  Choosing Yahoo now WITHDRAWS the pin, the same rule the
  breakdown-source endpoint has always applied to its own default.

- **Existing yahoo pins are cleared on fund load** (`purge_default_pins`),
  so a fund corrects itself the first time it is read rather than
  needing every field found and undone by hand. Purges are logged.

## [0.69.3] — 2026-08-16

### Fixed
- **One provenance map, read by both surfaces.** The fund tiles and the
  Edit dialog each worked out where a field's value came from, by
  different means, and disagreed: the tile read `fund_structure_sources`
  and could say "inferred from name" while the dialog read the fields
  endpoint and said "Yahoo" for the same field at the same moment.
  `_field_provenance` now produces one map covering every field in
  FIELD_GROUPS, emitted as `data.field_sources`, and both renderers call
  one function against it.

- **Yahoo is never asserted.** The fields endpoint fell back to the
  literal string `"yahoo"` for any unpinned editable field, so a
  `focus_type` worked out from the fund's name, a `style_box` inferred
  because the fund is fixed income, and a `market_cap` set to `n/a`
  because it is cash all reported as Yahoo data. A source is recorded
  where the value is produced or there is none: Yahoo is the source when
  Yahoo returned something, and a field with no recorded source shows no
  caption. There is no provenance for an absence.

- **The `source` field on the fields endpoint means the pin only** —
  which source this field is set to ask. It is not where the current
  value came from, and conflating the two is what produced the false
  Yahoo captions. The dialog now shows both: the dropdown for the pin,
  the caption for the actual origin.

- **`fundFieldSourceNote` no longer special-cases a single origin.** It
  translated `'name'` and let every other value fall through to the pin
  label, which happened to be harmless only because a `default` origin
  always accompanies a neutral value that renders as empty. That held by
  luck rather than by design.

## [0.69.2] — 2026-08-16

### Fixed
- **`primary_asset_class` reports its source like every other field.**
  It was the one field in the structure block with no entry in
  `fund_structure_sources`, so `fundFieldSourceNote` fell through to the
  pin label and captioned every classification "Yahoo" — including the
  ones decided by words in the fund's name. A reading of marketing copy
  was presented as measured data.

  The origin now goes into the same map the tile and the Edit dialog
  already read, so a name-derived class captions "inferred from name"
  through the existing renderer, and a user override captions "My own
  value" like any other asserted field. The field-resolution path sets
  its note the same way its sibling fields always have.

### Removed
- **`AC_ORIGIN_LABELS`**, added in 0.69.1 — a second source-label map in
  the frontend, parallel to `SOURCE_LABELS`, rendering into the tile's
  confidence sub-line instead of the source line every other field uses.
  Two ways to say where a value came from, differing in wording and in
  position, is the divergence this codebase removes on sight. It should
  never have been written: the mechanism it duplicated was already in
  the file. The frontend is byte-identical to v0.69.0 again.

## [0.69.1] — 2026-08-16

### Fixed
- **The asset-class tile says which kind of evidence decided the
  class.** The source pin reported "Yahoo" for everything detection
  produced, but detection reads two very different things: Yahoo's own
  holdings data, and words in the fund's name. A class inferred from
  marketing copy was being presented as measured data. The block now
  carries an ``origin`` — ``yahoo`` (structural signals), ``name``
  (a `style_match` phrase hit), ``user`` (an override, which replaces
  the evidence as well as the value) or ``none`` — and the tile renders
  it: "medium confidence · derived from name".

  Same vocabulary the fund_structure origins have used since v0.28.0,
  where `market_cap`, `style_box` and `focus_type` have always said when
  they were name-derived. The classification was the one field that
  never did.

  Blocks cached before this release carry no origin and show the
  confidence alone. A blank is honest where a guessed "from Yahoo data"
  would not be; the next force refresh fills it in.

## [0.69.0] — 2026-08-16

### Changed
- **`primary_asset_class` is data, not code.** The fund CLASSIFICATION —
  what kind of fund this IS — now lives in
  `Primary_asset_class_definitions.csv`, in the same schema as every
  other definitions file. Three separate pieces of hardcoding are gone:

  - **`config.ASSET_CLASSES`**, the vocabulary itself. The comment
    justifying it said config must not import resources. That is true at
    import time and only at import time; the lazy import in
    `config.asset_classes()` runs on call, long after both modules
    exist. What the constant actually bought was a vocabulary that could
    disagree with the file describing it.
  - **The `_load_fund_classes` top-up**, which filled its own rows from
    that constant whenever the file omitted a key — so the two could
    diverge silently, with no way to tell from the outside which one a
    given value had come from.
  - **Four keyword lists inside `detect_asset_class`** (`bond_kw`,
    `equity_kw`, `cash_kw`, `commodity_kw`), now the file's
    `style_match` column. Correcting how a Dutch-named fund or an
    issuer's house wording gets classified was a code change; it is a
    file edit.

  The PRECEDENCE between a name hint and Yahoo's structural signals
  stays in code. Weighing evidence is a judgement, not a vocabulary.

- **Two alias columns, the same split the sector file draws.**
  `matches` holds spellings of the value itself — "Fixed Income" IS the
  `fixed_income` class. `style_match` holds phrases that, found in a
  fund's NAME or category, are evidence for it: "gilt" is not a spelling
  of `fixed_income`, it is a reason to think a fund called that holds
  bonds.

- **The asset-class override endpoint resolves, rather than only
  comparing.** The classification arrives from a factsheet, justETF or a
  typed value as often as from the dropdown, and "Fixed Income" is the
  same assertion as `fixed_income`. Unrecognised values are still
  rejected.

### Added
- **`config.field_vocab()`** — one reader for both ways a field declares
  its allowed values, `vocab` for a fixed tuple and `vocab_fn` for one
  that lives in a resource file. All four call sites moved onto it. A
  caller reading only `spec["vocab"]` saw an empty vocabulary for any
  field using the other form, and would have rejected every legal value.
- **Two validator checks for the new file** — match-token collisions
  (one spelling cannot mean two things), and `style_match` collisions
  across classes. A name phrase pointing at two classes cannot decide
  either, and the caller would silently take whichever its own
  precedence reached first.

### Fixed
- **Name phrases match on word boundaries, not as bare substrings.**
  Moving the keywords into a normalised file broke them:
  `_norm_phrase` reduces punctuation to spaces, so `s&p` becomes `s p`
  — and "iShares Physical" contains "s p" across its word break. Every
  physically-backed fund classified as equity. The old hardcoded list
  was matched against a merely-lowercased name and never hit this;
  normalising the file is what exposed it.

## [0.68.0] — 2026-08-14

### Fixed
- **Peer funds on the price chart are named, not tickered.** Same markup
  and the same label rule as the portfolio price chart: the fund's name,
  with the ticker as the subtitle. A row of tickers is unreadable when
  the point is comparing funds you know by name, and the two charts
  disagreeing about how to name a fund was a difference with no reason
  behind it.

### Changed
- **`focus_type` "region" is renamed "geography"**, and both levelled
  focus vocabularies now offer EVERY level.

  - **geography** — countries, regions and super regions. A fund built
    for Japan alone is not an Asia fund; offering regions only forced a
    single-country tracker into the nearest region, which is a different
    fund.
  - **sector** — sub sectors, sectors and super sectors. A semiconductor
    fund is not a technology fund and a cyclicals fund is not any one
    sector; the middle level described neither.

  The picker is level-tagged, as everywhere else a levelled value is
  chosen: "Country — japan" and "Region — japan" are different answers.

  The old name is migrated **on read**, not by rewriting stored
  structures. `focus_type` feeds `peer_key`, and a fund left on "region"
  while its neighbours moved to "geography" would land in a different
  key and drop silently out of its own peer group — scored against
  nobody, in a group of one. `peer_key`, the override validator and the
  edit dialog all accept the legacy spelling; verified that a fund
  stored as `region|japan` and one saved as `geography|japan` share a
  group.

  This widens peer groups' precision in both directions: two Japan
  country funds are now peers with each other rather than with every
  Asia-Developed fund.

---

## [0.67.1] — 2026-08-14

### Fixed
- **The price chart showed no peers even when the Score tile listed
  three.** Two different ways of deciding what a peer is, and the price
  chart used the wrong one.

  The Score tile builds its universe by reading each cache blob directly
  and assembling the fund dict scoring expects. v0.67.0's peers endpoint
  built its own universe by calling `load_fund_data` per listing, which
  produces a different shape — so `peer_key` did not see the fields it
  reads, and the comparison never matched. It also meant a live fetch
  per candidate on every fund page.

  The endpoint now takes the group from the SCORED universe, which
  already computes it. The list is by construction the one the Score
  tile shows, and the price series come from the cache blob with no
  fetch at all.

- **An empty peer row now says why.** "This fund has no peers", "its
  peers have no cached price history" and "the lookup failed" looked
  identical — an empty row — which is exactly how v0.67.0 shipped
  broken and looked merely quiet. Each now states its own case, and the
  response carries `group_size` so the UI can tell them apart.

---

## [0.67.0] — 2026-08-14

**Peer group on the fund price chart**, with the price/indexed selector
the portfolio graph already had.

- **`/api/funds/<ticker>/peers`** returns the price series of every fund
  sharing this fund's peer group — asset class x focus, the funds that
  compete for the same slot. Two World equity trackers compete; a World
  tracker and a European bond fund do not. Series are returned
  unwindowed and unindexed: the caller already windows and normalises
  the subject fund, and doing it twice in two places is how the two stop
  agreeing.

- **Peers are hidden by default.** The tile is about this fund, and
  eight unasked-for lines would bury it. Each peer is a toggle, coloured
  to match its line.

- **Fund price / Indexed (=100).** Indexed is what makes the comparison
  mean anything: two funds priced at 12 and 480 tell you nothing side by
  side. Rebasing is to the first point IN THE WINDOW, not in the whole
  series, so the subject always starts at 100 at the left edge. The
  moving averages are rebased with it — computed on raw closes, they
  would otherwise sit on a different scale entirely.

- Toggling a peer on while in price mode says so, rather than silently
  drawing a flat line at the bottom of the axis.

- A date the peer has no price for is a null, not an interpolation. A
  gap is a gap, and drawing through it would invent prices.

- The view mode resets to Fund price on each fund you open: "indexed" is
  a comparison setting, and carrying it silently onto the next fund
  would leave you reading an index number as a price.

The peer walk reads every cached listing, so it is fetched lazily, once
per fund, and never awaited — the chart draws this fund without waiting
on it.

---

## [0.66.5] — 2026-08-14

### Fixed
- **Region targets read 0% achieved. The real cause, at last.**

  v0.65.0's migration put every legacy country target at the facet's
  DEFAULT level (`country`), when they were all region keys. v0.66.2
  fixed that migration — but only for LEGACY, unlevelled blocks. By then
  the damage was already persisted: opening the Targets tab once under
  v0.65.x and saving wrote the wrong placement back in the LEVELLED
  shape, after which the legacy branch could never fire again.

  So the targets sat at `country` level carrying region keys —
  `{"country": {"country": {"europeDeveloped": 25}}}` — and no country
  exposure will ever contain `europeDeveloped`. The optimiser reported
  them unmet, which from its side is indistinguishable from a genuine
  miss.

  Any key sitting at a level that does not recognise it is now relocated
  to the level that does, whatever shape it arrived in. Idempotent: a
  key already at its home level maps to itself and is left alone —
  `japan` deliberately targeted at region level stays at region level,
  because region does recognise it.

  Verified end to end: the exact broken shape now optimises to exactly
  40% and 35%.

### Note
The diagnostic added in v0.66.4 is what found this. "Nothing in the
universe holds Asia Developed, Europe Developed, North America —
measured on 37 of 45 funds" was the decisive line: exposure existed, the
level was answered, and the targeted keys were absent from it. That
combination is only possible if the keys are at the wrong level, which
three previous hypotheses had all missed.

---

## [0.66.4] — 2026-08-14

### Added
- **The optimiser now says WHY a target reads 0% achieved.** A zero has
  several possible causes — no fund answers that facet, no fund answers
  it at that level, nothing in the universe holds the targeted bucket,
  or the solver genuinely missed — and the result said nothing that told
  them apart. A row of zeroes looked identical in every case.

  The response carries `level_report`: per (facet, level), how many
  candidates answer with non-residual weight, out of how many, and which
  targeted buckets NO candidate holds. The result dialog turns that into
  a note above the table:

  - *no candidate fund reports at this level* — with the fix named.
  - *nothing in the universe holds X* — that target cannot be met by any
    selection, however the solver is tuned.
  - *measured on 3 of 40 funds* — partial coverage, still a real
    measurement.

  Nothing is shown when a level is fully measured, so it stays quiet
  when there is nothing to say.

  This is a permanent diagnostic rather than a debug aid. It exists
  because region targets reading 0% could not be explained from the
  outside, and three separate hypotheses about the cause were wrong.

---

## [0.66.3] — 2026-08-14

### Fixed
- **A facet with no data looked like a facet whose targets simply were
  not met.** The "no exposure data on any candidate fund" guard summed
  every value including `unknown`, so a universe in which no fund
  answers a facet at all still totalled 1.0 per fund and the guard
  stayed silent. Every target on that facet read 0% achieved with
  nothing anywhere explaining why — the optimiser had correctly fitted
  the data it had, which was none.

  Residual weight (`unknown`, `n/a`) no longer counts as exposure, so
  the warning fires exactly when there is genuinely nothing to fit. The
  message now names the fix: set the fund's card to Holdings or
  Factsheet, or upload holdings.

  This is the likely cause of region targets reading 0% while nothing
  appeared to be wrong: Yahoo publishes no country breakdown, so a fund
  whose country card is on its default source answers `unknown` at every
  country level — including region.

### Changed
- **`candidate_exposures` extracted** from the optimise endpoint into
  `breakdowns.py`. As an inline block it could not be exercised without
  a live fetch, which is exactly why a level-specific failure could sit
  in it while the pieces either side both tested clean. Now covered
  directly: a fund's region exposure is confirmed for the holdings,
  factsheet and no-data cases.

  It also reports `source: "none"` when a facet answers only in
  residuals, where the old inline version reported the card's nominal
  source — so the per-facet source mix in the optimiser report was
  claiming data that was not there.

---

## [0.66.2] — 2026-08-14

### Fixed
- **Region targets were never achieved by the optimiser.** Every
  pre-v0.65.0 country target is a REGION key — before levels existed the
  Targets tab offered regions for the country facet, and the old
  exposure builder remapped country to region to match. The v0.65.0
  migration put every legacy key at the facet's DEFAULT level, which for
  country is `country`, so those targets landed where no exposure would
  ever carry that key. The optimiser reported them as unachieved with
  nothing to say why.

  Legacy keys now migrate to the level that RECOGNISES them — the level
  at which the key maps to itself — rather than to the default. A
  legacy set mixing `germany`, `northAmerica` and `emerging` now splits
  correctly across country, region and super_region. `japan` goes to
  country level, being both a country and a region, since levels are
  ordered finest first.

  Verified end to end: a legacy `{europeDeveloped: 40, japan: 35}` set
  now optimises to exactly 40% and 35%.

- **The same bucket was named two different ways.** The Targets tab
  humanised the key ("Asia Developed") while the optimiser's result
  table printed it raw ("asiaDeveloped"), which reads as two different
  buckets rather than one bucket named twice. Both now use `tgKeyLabel`.

- Removed a stale comment in the optimiser's exposure builder still
  describing the country→region remap that region-as-a-level replaced.

---

## [0.66.1] — 2026-08-14

**Backup & restore UI.** A panel on the Settings tab and the two-phase
import dialog, completing 0.66.0.

- **Export** — funds (with checkboxes for price history, default off,
  and factsheets, default on) and portfolios, as separate downloads.
- **Import** — one file picker for either kind. The bundle is inspected
  first and nothing is written until you have seen what it would change.

The conflict table lists every fund in the bundle. New funds say so and
have no choice to make; existing funds get skip or overwrite, and
overwrite states its cost inline — *"replaces 3 of your edits"* — so the
count is visible at the moment of choosing rather than discovered
afterwards. One "apply to all" control sets every conflicting fund at
once and can still be adjusted per row afterwards.

Resource merging offers the four options with **merge aliases** the
default, and says why: skip and overwrite each discard one side's
aliases, which is rarely what anyone wants.

A portfolio bundle renders differently — the portfolios it holds, which
of them replace one you have, and any referenced funds this install
lacks, stated as a note rather than a blocker.

### Verified in 0.66.1
The two paths 0.66.0 shipped untested. They are unrelated to each other
and were tested with different bundles:

- **Portfolio round trip**, using a PORTFOLIO bundle — export, wipe,
  restore: the portfolio comes back with its targets intact and the
  missing referenced fund listed rather than blocking.
- **Factsheets**, using a FUND bundle — exported, deleted locally,
  restored by the import.

To be explicit, since the 0.66.1 wording invited the question: a
portfolio bundle contains `portfolios.json`, `settings.json` and
`manifest.json`, and nothing else. Factsheets are documents about a
fund, so they travel with the fund bundle. A portfolio backup carries
what you hold, not what the things you hold are.

---

## [0.66.0] — 2026-08-14

**Backup, restore and pre-loaded fund sets.** New `porxpy/bundles.py`
plus four endpoints. Backend only — the import dialog follows in
0.66.1. Nothing existing changes shape, so this is additive: the
endpoints are simply unused until the UI lands.

### Two bundles, deliberately separate

- **Funds** — the curated asset: holdings, factsheets, corrected
  structure fields, per-facet source pins, listings identity, and the
  resource files. Portable between installs and users.
- **Portfolios** — what you hold: portfolios, targets, cash, settings.
  Personal.

Mixing them would mean a user cannot take someone else's fund research
without also taking their holdings.

Both are plain zips with a readable `manifest.json`. When an import goes
wrong the user can open the file and see why.

### Import is two-phase

`inspect_bundle` reports what a bundle holds and what it collides with,
writing nothing; `apply_bundle` writes it with the caller's decisions.
One-shot import would either overwrite silently or refuse on the first
clash — the conflict list can only exist once the bundle has been read
against this install.

Per fund: **skip** or **overwrite**, with a per-fund default for "apply
to all". Overwrite is wholesale and the inspect report carries the count
of local overrides it would replace, so the prompt can state its cost
rather than discarding work silently.

### Overrides travel, except the personal one

`include_in_optimizer` stays local: it is a decision about your own
portfolio construction, not a fact about the fund, and shipping it would
have a new user's optimiser silently ignore funds. Everything else —
structure, replication, style, market cap, style box, focus, and the
four `breakdown_source` pins — is curation and travels. On overwrite the
local personal fields are preserved regardless, since the bundle cannot
have an opinion about something it never carried.

### Resource merge has four options

`skip` / `overwrite` / `merge_aliases` / cancel. The third exists
because the first two both lose work on a resource row: two installs can
define the same sector with different aliases, and whichever side loses
has its aliases discarded. `merge_aliases` unions the `matches` column
and keeps every local key field, and is the default.

### Details
- Price history is a checkbox, default off — it dominates the file size
  and one refetch replaces it. A bundle exported without it never wipes
  a local series on import: absent-from-bundle is not "this fund has
  none".
- A restored portfolio referencing funds this install lacks is imported
  anyway, with the missing funds listed. A portfolio backup should not
  be refused because of an unrelated gap in the fund set.
- Resources reload after an import, so a merged file takes effect with
  no restart.

---

## [0.65.2] — 2026-08-14

### Fixed
- **`/optimize` returned 500 on the per-facet coverage check.** The
  "no exposure data on any candidate" guard summed
  `c["exposures"][facet].values()` directly, which under the levelled
  shape adds dicts to an int. It now sums across every level the targets
  mention.
- The `optimise_portfolio` docstrings still described the flat
  `{facet: {bucket: fraction}}` shape for both `targets` and
  `exposures`. Corrected — a docstring that lies about a shape is how
  the next reader writes the next one of these.

---

## [0.65.1] — 2026-08-14

### Fixed
Four consumers of the flat target shape that v0.65.0 missed. The first
crashed; the others would have returned wrong numbers silently, which is
worse.

- **`/optimize` returned 500.** The targets-to-fractions conversion still
  assumed `{key: pct}` and got `{level: {key: pct}}`, so `float()` was
  handed a dict.
- **Achieved-vs-target was computed against the wrong index.** The
  result loop read `exposures[facet][key]`, which under the new shape
  finds nothing — every achieved exposure would have reported 0.0 and
  every deviation would have equalled the full target. A plausible-
  looking table of complete failure, with no error raised.
- **Cash exposure was still flat**, and still doing the country → region
  remap that region-as-a-level replaced. A cash position in Germany now
  contributes to germany at country level AND europeDeveloped at region
  level, instead of being rewritten as one or the other.
- **The optimiser's result table** read `deviation[facet][key]`. It now
  flattens the levels into one table per facet with the level named on
  each row — the optimiser result is a single proposal, so splitting it
  per level would fragment one answer, unlike the Targets tab where the
  levels are the thing being compared.

Verified by running a full `optimise_portfolio` against mixed-level
targets, which is the check that would have caught all four before
v0.65.0 shipped and which I did not do.

---

## [0.65.0] — 2026-08-14

**Tree facets, stage 3 — targets at any level.** The last of the tree
work. A target now names a level as well as a bucket, the optimiser fits
each one at its own grain, and the parent/child rule is enforced on save.

### Targets are keyed by level

`{facet: {level: {key: pct}}}`. A bare key was no longer enough: `japan`
is both a country and a Morningstar region, and `unitedKingdom` the
region differs from `unitedkingdom` the country only in case — so
`{"country": {"japan": 30}}` did not say which 30% was meant.

Pre-0.65.0 targets migrate to the facet's default level automatically,
detected by shape rather than a version flag: a level block's values are
dicts, a legacy block's are numbers. Nothing to do by hand.

### Mixed levels need no ordering rule

Targeting semiconductors 15%, software 10% and technology 35% gives
three constraint rows, each measured against the portfolio's
distribution at its own level. A semiconductor fund satisfies its own
row AND contributes to the technology row, so the remaining 10% of
technology is filled by any technology fund.

Verified against four candidate funds — pure semiconductor, pure
software, generic technology, healthcare — which fit to 15% / 10% / 10%
and the rest elsewhere. No "fill the children first, then the
remainder" pass exists, because the algebra already says it.

This also corrects the spec: it called for fitting "at the deepest
targeted level". That would have meant re-expressing a sector target at
sub-sector grain, which is the rolling-down-is-invention problem the
whole refactor is built against. Each target is a constraint at its own
level and nothing is translated.

### Parent/child consistency, checked on save

A parent cannot be targeted below the sum of its targeted children —
technology 20% against semiconductors 15% plus software 10% is not
unlikely, it is arithmetically impossible. A parent LARGER than the sum
is the useful case and is allowed.

Checked when you click Save, not per field: a target set is only
coherent once complete, and typing semiconductors 15% before technology
35% would fail a per-field check on a set that ends up perfectly valid.
The editor stays open with your edits intact and lists which parent is
too small and by how much. Grandchildren count too — a sub-sector target
is checked against a super-sector target two levels up.

### Deviation report grouped by level

One chart per level rather than one per facet: a sub-sector 15% and a
sector 20% are nested, not competing, and one shared axis invites
reading them as alternatives.

A level nothing in the portfolio can answer is stated as **not
measurable** rather than drawn as a row of full-length underweight bars,
which would read as "you hold none of this" when the truth is "we
cannot tell". Partial coverage still draws, with the figure beside it —
targeting semiconductors 15% when 60% of the portfolio reports
sub-sector is a real measurement, just an uncertain one.

### Also

- The targets editor groups its rows by level and its add-picker is
  level-tagged ("Sub sector — semiconductors").
- The fund-focus picker is restricted to sector-level entries; it names
  a fund's focus_detail, which sub and super sectors cannot express.
- The optimiser's candidate exposures are per `(facet, level, key)`, and
  the country→region remap that lived in that path is gone — region is a
  level now, so asking for it is an index rather than a translation.

---

## [0.64.5] — 2026-08-14

### Fixed
- **The by-value tab's list grew the whole dialog**, so a long list
  pushed the close button off the bottom of the screen and you had to
  scroll the entire modal to dismiss it. The by-row tab has never done
  this because it caps its table in an inner scroller; the by-value tab
  rendered straight into the body with no cap.

  The by-value table now sits in the same shape: capped at `60vh` with
  `overflow-y:auto`, its header row sticky, and the summary line and any
  error message left outside the scroller so they stay put with the tab
  strip and the close button.

- **Scroll position is preserved across an apply.** Adding one alias
  rebuilds the table — the list has to refresh, since resolving a value
  removes it — which destroyed the scroll container with it. Both axes
  now carry across, as the by-row tab already did. Without it, resolving
  the tenth value threw you back to the first, which is worst exactly
  when the list is long enough to matter.

---

## [0.64.4] — 2026-08-14

### Changed
- **The Review dialog refreshes its vocabularies when the window regains
  focus**, not only when the dialog is opened. That is the signal that
  matches the workflow: alt-tab to Excel, add a row to a definitions
  file, come back. Focus returns BEFORE the next click, so the dropdown
  opened afterwards is already current — no polling, and nothing fetched
  while simply working inside the dialog.

  Refreshing on the dropdown's own click was the obvious alternative and
  does not work: a native `<select>` renders its popup synchronously on
  mousedown while the fetch is async, so the first click would show the
  stale list and then mutate underneath the cursor. That is worse than
  stale — it is stale AND jumpy.

  Re-rendering happens only when the vocabulary actually changed, so an
  alt-tab that changed nothing does not scroll-jump the table. Pending
  picks survive the refresh, and focus while the dialog is closed
  fetches nothing.

---

## [0.64.3] — 2026-08-14

### Fixed
- **A row added to a definitions file mid-session never reached the
  dropdowns.** The backend was correct throughout — the
  `before_request` freshness check reloads on any on-disk change, and
  the endpoint served the new row on the very next call. The staleness
  was entirely in the browser, in two caches that were filled once per
  PAGE LOAD:

  - `loadHoldingClassResources()` populated `RES_HOLDINGS_CLASSES`,
    `RES_SECTORS`, `RES_CURRENCIES` and `RES_COUNTRIES` only when they
    were `null`, so every later call was a no-op. It now takes a
    `force` argument, and the Review dialog passes it on open.
  - The by-value tab kept `ufValueTargets` per facet across dialog
    opens, so its target dropdown was frozen at whatever the vocabulary
    had been the first time that facet appeared. Cleared on every open.

  Both caches existed to avoid refetching a 263-entry country list on
  every render, which is still worth avoiding — so the refresh is tied
  to opening the dialog, not to rendering it. The dialog whose entire
  purpose is resolving values against the resource files is the one
  place that must never serve a stale vocabulary.

---

## [0.64.2] — 2026-08-14

### Fixed
- **A resource file saved by Excel could not be read.** Excel does not
  write UTF-8 by default; on a Western Windows it writes Windows-1252,
  so a file opened, edited and saved came back with `0x97` where an em
  dash had been. The reader decoded UTF-8 only and raised on the first
  one, surfacing as

      [Resources] freshness check failed: 'utf-8' codec can't decode
      byte 0x97 in position 1120: invalid start byte

  with no indication of which file or why. All three read sites now go
  through `_read_resource_text`, which tries UTF-8 (BOM-tolerant), then
  Windows-1252, then Latin-1. Latin-1 is last rather than default
  precisely because it cannot fail: as a default it would silently
  accept genuinely corrupt bytes as text. The fallback is announced
  once per file.

- **The writer emitted UTF-8 with no BOM**, which is the other half of
  the same trap. Without a BOM, Excel opens a UTF-8 CSV as
  Windows-1252, so every accented character is mojibake on screen and
  corrupt on save — and v0.64.0 had just added 116 Dutch aliases full of
  them. Writes are now `utf-8-sig`, so `België` and `Oekraïne` survive
  an open-edit-save round trip through a spreadsheet.

  A file already saved as Windows-1252 heals itself: it is read
  tolerantly and rewritten with a BOM on the next alias write.

---

## [0.64.1] — 2026-08-14

### Resource files
- `currencies.csv` → **`Currency_definitions.csv`**, in the shared
  schema `type,name,description,parent_name,matches,is_default,attrs`.
  Currencies are flat, so `parent_name` and `is_default` go unused;
  carried anyway so every resource file has one shape and one reader.
  The symbol moves into `attrs` as `symbol=`. Loaded rows keep
  `code` / `name` / `symbol`, so the dropdown, the extraction prompt and
  the upload paths did not move.
- `sectors.csv` → **`Sector_definitions.csv`**.
- **Two dangling currency references fixed.** `SLE` (Sierra Leone's
  post-2022 leone) was named by the geography file and absent from the
  currency file; it is now a row, with the retired `SLL` kept. Venezuela
  pointed at `VEB`, the pre-2008 bolívar; it now points at `VES`, with
  `VEB` kept as an alias so older files still resolve. Both were
  redenominations — the two files were compiled at different times and
  neither knew the other existed.

### Added
- **Cross-file consistency validation**, driven by the fingerprint. One
  trigger in `_refresh_file_stamps`: every path that can change a
  resource file — startup, explicit reload, the just-in-time freshness
  check at the start of a request, and every alias write — ends there,
  so no caller has to remember to validate. A file edited in a
  spreadsheet mid-session is checked the moment it is picked up.

  Checks: every country's `currency=` names a currency row; every
  `parent_name` names a row of the level above, in the geography and
  sector files; and no match token is claimed by two rows in one
  vocabulary.

  Problems are printed, never raised. A dangling reference degrades one
  lookup; refusing to start over it would be the worse failure.

  Before this, Sierra Leone resolved as a country while its own declared
  currency resolved as nothing, and the Resolve dialog asked the user to
  fix a value one of their own files declared correct.

---

## [0.64.0] — 2026-08-14

**The `Version=N` header is gone from every resource file.** Two
mechanisms answered "has this file changed" and both were partial, in
different places:

- The declared version only moved when a writer remembered to bump it,
  so a file edited by hand kept its old number and every cache stamped
  with it believed itself current.
- The fingerprint changes whenever the bytes do — but it was written for
  **two of the five files**. An edit to `currencies.csv`,
  `Fund_class_definitions.csv` or the geography file changed no
  fingerprint at all.

Removing either alone would have lost coverage, which is why the version
half still appeared load-bearing. Both dicts are now one, filled from a
single `_RESOURCE_FILES` list so a file cannot be added to one and
forgotten in the other.

- `parse_versioned_csv` / `write_versioned_csv` become
  `parse_resource_csv` / `write_resource_csv` and no longer carry a
  version. The reader still SKIPS a leading `Version=` line if it finds
  one, so a local copy of an older file keeps working — without that
  check the header row would be eaten as the version line and every
  column would read `None`, a silent total failure.
- `RESOURCE_VERSIONS` is removed, along with every `= version` line and
  the `versions` key from two API responses. The cache and portfolio
  stamps compare fingerprints alone. A stamp written before this release
  carries the old keys, so it cannot match and re-normalises once —
  the correct migration, needing no special case.

**Dutch aliases** across every level: 116 forms, accented and
unaccented, so `België` and `Belgie` both resolve. Countries
(`Duitsland`, `Zwitserland`, `Verenigde Staten`/`VS`), regions
(`Noord-Amerika`, `Azië (opkomend)`), super regions (`Ontwikkelde
markten`, `Opkomende markten`) and focus groups (`Europa`, `Wereld`).

### Fixed
- **Every hyphenated country alias silently missed.** The geography
  loader stored the country map's keys with `_norm_phrase` while
  `country_to_mstar` looks them up with `_normalise_key`; the two treat
  punctuation differently, so a key stored one way and read the other
  never matched. Latent since 0.63.0 and invisible until the Dutch names
  arrived — `Zuid-Korea`, `Nieuw-Zeeland`, `Zuid-Afrika`, `Wit-Rusland`
  all failed while their unhyphenated neighbours worked. The region and
  focus maps keep `_norm_phrase`, because their readers use it.

### Note on currencies.csv
Still required, and not removable. It backs `resolve_currency`, which
became load-bearing in 0.59.0 when currency started being validated, and
it feeds the Resolve dialog's currency dropdown. It answers what a
currency IS; the geography file's `currency=` attribute answers which
one a country uses. No overlap.

---

## [0.63.1] — 2026-08-14

### Fixed
Four defects from the geography fold, all on the resolve-and-edit path.
The first is the serious one.

- **`add_country_alias` still wrote the OLD six-column schema.** It
  rebuilt the file from an in-memory structure and named the columns
  itself, so a successful write would have replaced the entire geography
  tree — every region, super region, focus group, parent and currency —
  with a flat country list. It never fired only because the in-memory
  rows no longer carry the column it looked for, so the lookup failed
  and the write was skipped. Failing safe was luck, not design.

  All three writers are now one `_add_geography_alias`, which edits the
  file's own rows in place and writes back the header it read, so a
  caller cannot retype the schema.

- **Edits to a holdings row did not stick.** `normalise_facets` prefers
  `<facet>_node` over `<facet>` as its input — deliberately, so
  re-running it cannot coarsen a row that matched at a finer grain — but
  that makes the node authoritative. The row editor wrote only the plain
  column, so the next cache read re-derived from the stale node and
  silently reverted the correction. The editor now clears the derived
  `_node` / `_level` pair for the facet it changes. Affects `country`
  and `sector`; the `sector` half predates the fold.

- **An added alias never repaired already-cached rows.** The geography
  loader did not record the file's declared version, as every other
  loader does, so `RESOURCE_VERSIONS` stayed at 0 while the file's
  version rose. The lazy cache migration saw no change and never
  re-normalised: the alias fixed the dropdown and left every cached row
  exactly as unresolved as before.

- **One unresolved country was listed three times** in the by-value tab
  — once each for `country`, `region` and `super_region`, which are all
  derived from the same column. Only `country` is listed; its dropdown
  offers targets at every level.

- `FACET_ALIAS_LEVELS` was removed by an over-wide edit in 0.63.0,
  breaking `/api/resources/facet_alias` with a 500.

---

## [0.63.0] — 2026-08-14

**One geography file, one tree.** `country_codes.csv`,
`country_currency.csv` and `regions.csv` are folded into
`Geography_definitions.csv`, in the same hierarchical schema the sector
and holdings-class files already use:

    type,name,description,parent_name,matches,is_default,attrs

Types are `country -> region -> super_region`, plus `focus_group`.
`parent_name` is SINGULAR, and that is the point: the old
`super_regions` column listed several per region, flattening two
orthogonal groupings and a universal into one cell with nothing marking
which was which. One parent makes the chain true by construction rather
than by convention.

### Two data errors the fold exposed

Merging `style_match` into `matches` made the two vocabularies disagree
on four countries, and in each case the region column was the wrong one:

- **China and India were in `asiaDeveloped`.** Since that region rolls
  up to `developed`, v0.62.0's super-region level reported China and
  India as **developed markets**. Both moved to `asiaEmerging`.
- Greece (`europeDeveloped`) and Turkey (`africaMiddleEast`) also
  disagree with `style_match`, but both are defensible classifications
  and are left as they were, by decision.

### style_match is gone

Of its 151 tokens: 56 named countries and were redundant once a tree
exists — resolve "germany" to the country and walk `parent_name` up,
which also gives focus_detail at country granularity, something the flat
column could not do. 67 named regions and were `matches` content
already. The remaining 28 are index names, harmless in `matches`
because nobody writes "NASDAQ" in a country column and the United States
is the right answer if they do.

The generator removed exactly 56 tokens as redundant, independently
confirming the count.

### Details

- `attrs` carries exactly `code=` (ISO alpha-3) and `currency=`. Alpha-2
  and the numeric code are SPELLINGS, not attributes, so they live in
  `matches` where the resolver already looks — `CH`, `CHE` and `756` all
  still resolve to Switzerland.
- `focus_group` rows (europe, asia, pacific, world) sit outside the
  chain with no parent and no children, skipped by every facet consumer.
  They are the pan-regional groupings a fund NAME can imply but a
  holding cannot belong to uniquely — which is exactly why they cannot
  be levels.
- Names are unique per TYPE, not globally: Morningstar has Japan and the
  United Kingdom as both a country and a region. Resolution prefers the
  deepest type, which is what `resolve_country_tree` already did.
- The `development` level is renamed `super_region`, matching the sector
  file's convention of level name == type name.
- `rollup_holdings` derives `super_region` natively alongside `region`,
  so a holdings row saying "Emerging Markets" counts at that level
  instead of vanishing into unknown.
- Derived structures keep their names (`MSTAR_TO_REGION`,
  `COUNTRY_NAME_TO_MSTAR`, …) so the ~86 downstream references did not
  move in the same release that changed the data. Renaming those is a
  follow-up.

---

## [0.62.0] — 2026-08-14

**Developed versus emerging, as a country level.** `country → region →
development`. Still a strict chain: every region maps to exactly one
status, verified against the file rather than assumed.

This is a portfolio X-ray question — how much of the portfolio sits in
developed markets — and it was invisible to everything that computes.
The distinction is already encoded in the region names
(`europeDeveloped`, `asiaEmerging`), which makes it readable but not
measurable: you could not set a target on it, the optimiser could not
fit it, and the deviation report could not show it.

The level aggregates across funds reporting at different grains, like
any other. Three funds — one reporting countries, one a region, one a
factsheet saying only "Emerging Markets 100%" — give a 50/50 developed
split at **100% coverage**, where Country reads 33% and Region 67%.

- `regions.csv` gains an explicit `development` column (Version=5), NOT
  read out of `super_regions`. That column flattens two orthogonal
  groupings plus a universal into one list with nothing marking which is
  which. One column serving two questions is the collision this project
  keeps getting bitten by; `super_regions` stays as-is for focus seeding.
- `resolve_development_facet` reads the two development super rows'
  `matches` AND `style_match`. Unlike the region resolver it consults
  `style_match` deliberately: "MSCI World" means the same thing in a
  fund's name and in a country column, because development status is a
  property of the holdings either way. Geography is where the two
  diverge — "China" in a fund name implies a region, in a country column
  it is a country — and geography is not a level this facet carries.
- The Resolve dialog offers development targets, so a source saying
  "Developed Markets" in a country column can be aliased.
  `add_development_alias` writes the two development rows only; the
  geographic super rows are rejected.
- Antarctica has no development status and reads `unknown` there. It is
  neither, and inventing one to fill the cell would be the invention
  these levels exist to prevent.

### Why there is still no geographic level

`super_regions`' geographic half is not a partition: northAmerica,
latinAmerica and africaMiddleEast have no geographic parent, while japan
and asiaDeveloped claim both asia and pacific. Rolling up would count
Japan twice and lose North America.

Nor can the two supers be chained in either order — americas, europe and
asia each span both development statuses, and every status spans several
geographies. They are siblings above region, not links in a chain. Adding
geography later forks `levels` and means stage 3's parent-held-to-sum-of-
children rule can no longer read parenthood off list adjacency. Deferred
deliberately, and noted in `config.FACET_LEVELS`.

---

## [0.61.0] — 2026-08-14

**Tree facets, stage 2.** The portfolio aggregates every level, the
X-ray cards get the same selector the fund cards have, and the three
back-compat aliases are gone.

A portfolio's sector data is now three complete distributions, exactly
as a fund's is. Each fund contributes to each level **independently,
from its own block at that level** — nothing is derived from another
level's portfolio total. Two funds of equal weight, one reporting
semiconductors 40% and one reporting technology 20%, give 20%
semiconductors at sub level and 30% technology at both sector and super.

Aggregating one level and re-cutting it would have meant the portfolio
could only ever be as fine as its coarsest fund, which is the opposite
of what levels are for.

### What this fixes on screen

A fund that reports at a grain the portfolio card did not use was
previously invisible there. A holdings file saying "Europe (Developed)"
in its country column contributed nothing to the country card; it now
counts in full at Region. A factsheet reporting only super sectors read
as `unknown` on the portfolio sector card — the known regression from
0.60.0 — and now reads correctly at Super.

`fundlevel_breakdowns[facet]` is a levelled block of the same shape the
fund cards read, so the portfolio card, the fund card and `facet_items()`
are one contract rather than three. `fundlevel_coverage[facet]` is now
per level.

### Removed

- **`asset_class_breakdown`, `sector_breakdown`, `currency_breakdown`.**
  The original response keys, kept as aliases derived from
  `fundlevel_breakdowns` — the same fact under two names. With every
  level now carried, an alias could only expose one of them, and a
  consumer reading it would be looking at the default level while
  believing it had the facet.
- **The portfolio's dedicated Country/Region toggle**, and with it
  `regroupToRegion`, `REGION_MAP`, `renderCountryGranularityOnly`,
  `setPortfolioCountryGranularity`, `portfolioSectorLevel` and
  `portfolioCountryGranularity`. Region is a level the backend
  aggregates; a country→region map fetched into the browser and never
  read is a standing invitation to reintroduce the second
  implementation this refactor removed.
- **`targets._aggregate_country_items_to_region`.** Asking for `region`
  is now the same request as asking for `sector` — which is the entire
  point of the block. Targets and the deviation table read through
  `facet_items` at the facet's default level; setting targets at any
  level is stage 3.

### Shared, not duplicated

`levelSegmentHtml` and `effectiveLevelIn` are used by both the fund and
the portfolio cards. The two keep independent state — panning the X-ray
to Super does not change what a fund page opens on — but share one
resolution rule and one set of level buttons, so a facet gaining a level
still costs nothing in the browser.

---

## [0.60.2] — 2026-08-14

### Fixed
- **The breakdown-CSV upload could not express a super sector either.**
  The third and last instance of the defect fixed for the factsheet
  prompt and the factsheet apply in 0.60.1. Three places in the CSV
  path, all now sharing the one authority (`facet_alias_targets`):

  - `_resolve_breakdown_key` called `resolve_sector`, which folds a sub
    sector up to its sector and answers `None` for a super sector, so a
    CSV row reading "Cyclical" was reported unresolvable. It now uses
    `resolve_facet_node` and keeps the level the CSV named.
  - `list_canonical_values` — the resolution modal's dropdown — offered
    the twelve sectors and Morningstar countries only. Every honest
    answer for a super-sector or region row was missing and the only
    available answers were wrong. It now enumerates every level, labelled
    with it ("super sector — cyclical").
  - `_allowed_canonical_set` — the commit-time validator — enumerated
    the same narrow lists, so mapping a row to a value the dropdown had
    just offered failed with "not a canonical value".

- **The CSV path resolved `asset_class` against the wrong vocabulary.**
  It used the HOLDINGS taxonomy (equity / bond / cash / other) while the
  breakdown facet it feeds uses the FUND one (equity / fixed_income /
  cash / mixed / commodity / other). A CSV saying "fixed income" or
  "commodity" could not resolve at all, and one saying "bond" resolved
  to a key the card has never heard of. Same collision named apart in
  0.59.0, reaching one more call site.

---

## [0.60.1] — 2026-08-14

### Fixed
- **A factsheet reporting super sectors was silently rewritten into
  sectors it never named.** A sector table reading "Cyclical 10% /
  Defensive 81% / Sensitive 9%" came back as consumer cyclical, consumer
  defensive and technology. Two independent defects, either of which
  alone would have caused it:

  1. **The extraction prompt offered only the twelve sectors.** There
     was no legal way to report a super sector, so the model did the
     only thing it could and guessed the nearest sector name. The prompt
     now lists all three sector levels and both country levels, with an
     instruction to answer at the grain the document uses and never
     convert between grains. The vocabulary comes from
     `facet_alias_targets` — the same authority behind the Resolve
     dialog's dropdown — rather than a second list free to drift.

  2. **The factsheet apply collapsed super sectors to `unknown` before
     storing them.** It called `resolve_facet_value`, which answers at
     the facet's DEFAULT level, so "cyclical" resolved to `unknown` and
     was written to the store as a residual. No level of the card could
     recover it. It now calls `resolve_facet_node`, which keeps the
     level the document named. Introduced in v0.59.0 and shipped in
     v0.60.0.

  The same correction applies to the unmatched-values endpoint, which
  was reporting super-sector values as unresolved. They are answers, not
  gaps, and offering to alias them would have invited exactly the
  invention this release exists to prevent.

  With both fixed, the test factsheet stores `cyclical / defensive /
  sensitive`, the card opens on **Super** at 100% coverage, and Sub and
  Sector are struck through — the document genuinely did not say which
  sectors, and the card now says so instead of guessing.

- **Not a name collision.** `cyclical` (super sector) versus `consumer
  cyclical` (sector) resolves correctly and always did: the loader's
  deepest-wins rule and the canonical-name-outranks-alias rule both hold
  for these three names. Verified directly rather than assumed.

---

## [0.60.0] — 2026-08-14

**Tree facets, stage 1.** A facet with levels is now ONE facet whose
value is a tree, emitted by the backend as a block, read by the fund
card. No number on screen changes: the browser already computed this for
the sector card, and stage 1 moves the computation to where the
portfolio can use it too.

A fund's sector data is three complete distributions, each summing to
the fund, describing the same money at three grains — semiconductors 40%
IS technology 40% IS cyclical 40%. The level selector picks which
finished distribution you look at and nothing else.

### The block

```
"sector": {
  "levels":           ["sub_sector", "sector", "super_sector"],
  "items":            {level: [...]},
  "coverage":         {level: 0.0-1.0},
  "levels_available": {level: bool},
  "levels_available_by_source": {source: {level: bool}},
  "default_level":    "sector",
  "source": ..., "available": ..., "unresolved": ...,
}
```

`available` says which SOURCES the fund has; `levels_available` says
which VIEWS the selected source supports. Same word for both is exactly
the collision that has bitten this project repeatedly, so they are named
apart.

Non-tree facets carry a single-level block of the same shape, so no
consumer branches on whether a facet has levels.

### One accessor

`facet_items(block, level=None)`. Every consumer goes through it; none
indexes `block["items"]["sector"]` by hand. `level=None` means the
facet's DEFAULT level — fixed per facet in `config.FACET_DEFAULT_LEVEL`,
never the deepest present. "Deepest available" would make the meaning of
a word depend on what happened to arrive: uploading a more detailed
factsheet for one fund would silently change the grain its targets are
measured at, with nothing on screen saying so.

### Country adopts the block now, not in stage 4

The spec staged country last because its resolution did not exist. It
does since 0.59.0, so `country → region` uses the identical block and
the identical selector. Super regions remain absent, and deliberately:
japan is asia *and* pacific *and* developed *and* world, so "the" super
region of a holding is undefined and rolling up to one would
double-count weight.

### Duplicate derivation removed

`sectorKeyAtLevel`, `sectorItemsAtLevel`, `subSectorItemsFor`,
`sectorLevelAvailable`, `subSectorAvailable`, `deepestSectorLevel`,
`effectiveSectorLevel`, `sectorCardItems`, `setFundSectorLevel` and
`setFundCountryGranularity` are gone, with the three hierarchy maps that
fed them. Ten functions replaced by six that index a block. Keeping the
browser copy would have been a second implementation of the same rule,
free to disagree with the first.

The country card's separate Country/Region toggle went with them: sector
and country now share one generic selector built from `levels`, so
adding a level to a facet is a backend change only.

### Fixed on the way

- **A source reporting only super sectors showed NO level as available,
  its own included.** Resolution folded every value to the facet's
  default level before the block derived the others, so a factsheet
  saying "Cyclical" arrived as `unknown` and cyclical was lost. The
  block now resolves to the canonical node AT THE LEVEL IT NAMED
  (`resolve_facet_node`) and derives from there.
- **The cross-source tooltip was decorative.** It asked "would another
  source answer this level?" by re-deriving the CURRENT source's items
  under a different source name, so it returned the same answer for
  every source but holdings. Answered from
  `levels_available_by_source`, computed per source in the backend.
- **Coverage had two definitions.** The fund card recomputed it in the
  browser as the sum of non-residual weights; the portfolio used
  `1 − unknown`. The card now reads `coverage[level]` from the block, so
  the two cannot disagree about the same fund.
- **Dead `fund_breakdowns` migration loop removed** from `utils.py`. It
  was written against a cache category that has never existed —
  `build_fund_breakdowns` runs at request time and is not persisted —
  and 0.59.0 made it dead twice over.

### Not in this release

Portfolio aggregation is still at the default level only; aggregating
every level and giving the X-ray card its own selector is stage 2, and
the three back-compat aliases go with it. Targets, deviations and the
optimiser continue to mean the default level.

---

## [0.59.2] — 2026-08-14

### Fixed
- **The by-row table still would not scroll sideways.** The 0.59.1 fix
  put `overflow-x:auto` on `#ufTableWrap`, which was the wrong element:
  `ufRenderTable` replaces that element's contents wholesale with its
  own `[data-ufscroll]` div, and *that* already carried
  `overflow-x:auto`. The scroll container was never missing.

  The table simply never became wide enough to need it. `width:100%`
  with no floor lets a table compress to fit its container indefinitely,
  so `scrollWidth` never exceeded `clientWidth` and the columns were
  squeezed rather than scrollable. The table now carries
  `min-width: max(900, columns × 96)px`, computed from the live column
  list so it tracks the conditional bond columns (10 columns → 960px,
  13 → 1248px).

  The redundant `overflow-x` added to `#ufTableWrap` in 0.59.1 is
  removed: two elements claiming the same job is precisely how that
  attempt looked correct and did nothing.

- **Horizontal scroll position is now preserved across a re-render.**
  `ufRenderTable` carried `scrollTop` but not `scrollLeft`. That was
  sufficient by accident while `scrollLeft` was always 0; with the table
  actually scrollable, ticking a checkbox while panned right would snap
  back to the first column — the same defect already fixed for the
  vertical axis.

---

## [0.59.1] — 2026-08-14

### Fixed
- **The Resolve dialog's by-row table could not be scrolled sideways.**
  The by-row table carries up to a dozen columns — five facets plus
  name/ticker/isin/weight, and the bond trio when any row has them —
  with `white-space:nowrap` on every cell, so it is routinely wider than
  the dialog. Nothing established a horizontal scroll container for it,
  so the right-hand columns were simply unreachable.

  Fixed on the table region rather than on the modal, so the filter bar,
  the facet chips and the tab strip stay put while you pan across the
  columns. The column-edit popover is `position:fixed` on
  `document.body` and so is not clipped by the new scroll container.

  The by-value table gets the same treatment plus a `min-width`: its six
  columns include a 280px target dropdown, and it clips on a narrow
  window for the same reason.

---

## [0.59.0] — 2026-08-14

**Unresolved facet values.** A value that does not resolve against its
resource file is no longer a bucket. It counts as `unknown`, and what
the source actually said is kept and surfaced.

Previously the raw text *was* the bucket key: an unrecognised sector
became a slice called "Diversified Holdings", an unrecognised country a
slice called "europe". That conflated two different statements — *this
fund holds 8% of that thing* and *the source said something we could not
place* — and only the first belongs in a distribution.

* `breakdowns.canonicalise_facet_key` is replaced by
  `resolve_facet_value(facet, raw) -> (key, unresolved_raw)`. Every
  entry point routes through it: holdings rows, Yahoo sector weightings,
  Yahoo asset allocation, factsheet extraction, uploaded CSVs.
* **Currency is validated for the first time.** It used to be uppercased
  and accepted, so an unrecognised currency was indistinguishable from a
  real one and nothing could surface it. Expect the first run to find a
  pile.
* Breakdown blocks carry `unresolved: [{raw, weight}]`, and the card
  shows it beneath the unknown slice — *of which unrecognised:
  "Diversified Holdings" 6%*. Without this the change would have hidden
  information rather than relocating it.
* **No storage change and no migration.** Every store already preserved
  the raw value on a miss, so resolution happens at derivation time and
  re-runs on every read. An alias added today repairs a factsheet
  extracted last month, with nothing rewritten.

**Country resolves at two levels.** `resolve_country_tree` mirrors
`resolve_sector_tree`: a value naming a country answers at country level
with its region derived, a value naming a region answers at region level
and leaves country `unknown`. `rollup_holdings` gains a `region` facet
beside `country`, exactly as `sub_sector` already sits beside `sector`,
derived from `country_node` so the two grains cannot disagree.

A holdings file saying "Europe ex-UK" in its country column used to be
flagged unmatched, telling the user to fix something that was never
wrong. It is now a region-level answer, silent in the dialog.

Super regions are deliberately NOT a level: a region belongs to several
at once (japan is asia *and* pacific *and* developed *and* world), so
rolling up to one would double-count weight. Deferred rather than
answered badly.

**Resolve unmatched values — a second tab, split by operation.**

* **By value** — one row per distinct `(facet, raw)` across every fund
  and every source, sorted by weight. Pick what a value means; the alias
  is written and every occurrence everywhere resolves on next read.
  Values from Yahoo, a factsheet or an uploaded CSV could not be
  resolved *at all* before this: those sources have no row to edit.
* **By row** — holdings rows, unchanged, for what an alias cannot
  express: a blank value, which has nothing to alias, and a row that is
  simply wrong rather than differently spelled.
* Sector and country targets are **level-tagged**. Mapping "AI Hype" to
  the semiconductors sub sector claims the source told us the sub
  sector; mapping it to technology claims it told us only the sector.
  Only the first can ever answer a sub-sector question.
* Junk values (a footnote marker, a stray header) have no honest target
  and stay in the list. They sink to the bottom on the weight sort
  rather than needing a flag to hide them.

**Resource-file writers filled in.**

* `add_super_sector_alias` — the third of the set. "Cyclicals" could
  previously only be mapped to a sector or sub sector, both of which
  claim the source said more than it did.
* `add_region_alias` — writes a new `matches` column in `regions.csv`,
  kept strictly apart from `style_match`. The two answer different
  questions: what a value in a country column means, versus what a
  fund's name implies about its focus. Merging them would make "china"
  in a holdings file resolve to a region.
* `add_fund_asset_class_alias` — `Fund_class_definitions.csv` had **no
  writer at all**, so a factsheet naming an asset class in an
  unrecognised spelling could never be resolved.

**Two asset-class vocabularies named apart.** `asset_class` the
breakdown facet (equity / fixed_income / cash / mixed / commodity /
other, from `Fund_class_definitions.csv`) and `holding_asset_class` the
holdings-row column (equity / bond / cash / other, from
`Holdings_class_definitions.csv`) are different key sets under one name.
One dropdown for both would let a factsheet's "Obligaties" be aliased to
`bond`, which the fund-level facet has never heard of — leaving the
value unresolved forever while the dialog insisted it was fixed. The
collision predates this release; naming them apart stops it reaching the
user.

Pending picks in the by-value tab survive an apply. Applying one alias
refreshes the whole list — resolving a value removes it and can change
what is left — which wiped every other row's selection. Found by
driving the dialog in a headless DOM, not by reading it.

Other: `FACET_NOT_APPLICABLE` gains `region` (a cash balance sits in no
country and therefore no region); facet migrator generation bumped to 3
so existing caches stamp `country_node` / `country_level`; the by-row
tab no longer adds aliases silently behind the row rewrite.

---

## [0.58.3] — 2026-08-04

### Fixed
- **Sector and Super were always offered, whatever the source reported.**
  Only the Sub button was availability-checked; the other two were
  hardcoded on. A source publishing only super sectors offered a Sector
  view that could be nothing but "unknown".

  All three levels are now checked the same way — a level is available
  when this source's data produces at least one real bucket there. The
  card falls back to the deepest level the source can actually answer
  rather than always to Sector, so a super-sector-only source opens on
  Super.

- **A super sector was being pushed back down into a sector.** Rolling up
  is derivation and rolling down is invention, and the key-mapping let
  `cyclical` pass through as though it were a sector — which reported a
  super-sector-only source as answering at sector level, the exact case
  above. Found by running the matrix rather than reading the code.

- **An unrecognised sector value was collapsed into "unknown" at sector
  level.** A value the taxonomy does not know is raw sector-field content
  — it is what the source literally said, it is stored in the sector
  field, and the sector view has always shown it. It passes through at
  sector level now, and cannot be placed at any other.

### Changed
- The unavailable-level tooltip distinguishes "this source does not
  report at that level, another one does" from "nothing this fund
  reports reaches that level", for every level rather than just Sub.

---

## [0.58.2] — 2026-08-04

### Fixed
- **The Sub view was offered or withheld on the basis of which source the
  card was set to, rather than what that source actually said.** Both
  halves were wrong: it read the holdings rollup no matter which source
  was selected, so a card set to the issuer offered a sub-sector view
  built from the fund's holdings — one source's numbers under another
  source's name. The narrower rule that replaced it, allowing sub sectors
  for the holdings source alone, was an assumption about issuers rather
  than a fact about a fund: a factsheet naming "Semiconductors" *has*
  answered at sub-sector level.

  Availability is now decided per source by asking whether that source's
  own items resolve at sub-sector level. The holdings source still reads
  its separate `sub_sector` distribution, because its sector buckets are
  folded to sector level and the finer grain lives elsewhere.

  When the current source cannot reach sub sectors but another available
  source can, the tooltip says so — "not from here" is actionable where
  "no data" is not.

### Added
- `/api/resources/sectors` returns the hierarchy — every canonical name's
  level, and each sub sector's parent — so the client can re-bucket a
  distribution at any level without hardcoding a taxonomy the resource
  file owns. Going coarser is derivation; going finer is invention, so a
  key that cannot reach the requested level becomes `unknown` rather than
  being dropped or guessed.

---

## [0.58.1] — 2026-08-04

### Fixed
- **A holdings upload discarded the sub-sector grain**, so re-importing a
  file whose sector column named a sub sector still left the card's *Sub*
  view struck through. The commit path resolved through
  `resolve_sector()`, which folds a sub sector up to its parent and
  returns "technology" for "Hardware" — correct as far as it goes, but
  the grain the file actually stated was gone by the time the row was
  written, and no later pass could recover it. `normalise_facets` reads
  `sector_node` in preference to `sector` precisely so re-normalisation
  cannot coarsen a row, and it was handed an already-coarsened one.

  The commit now resolves the whole tree and stores `sector_node` /
  `sector_level` alongside the sector-level name, matching what
  `normalise_facets` does.

  Verified against a real 409-row constituent file: the ASML row stores
  `sector_node=hardware`, `sector_level=sub_sector`, `sector=technology`,
  and the rollup produces a `hardware` bucket, so the Sub view enables.

### Note
The 0.55.3 entry says the sub-class-to-asset-class derivation "had been
added to the upload path in 0.55.1". It had not — that edit failed
silently and never landed. The behaviour is present regardless, because
`coerce_holdings_row` routes every uploaded row through
`normalise_facets`, which does the reconciliation. No functional gap, but
the changelog claim was wrong.

---

## [0.58.0] — 2026-08-04

Stage 2, first half: the sector level selector on the fund card, and the
rollup that feeds it.

### Added
- **`rollup_holdings` emits a `sub_sector` distribution** alongside
  `sector`, derived from each row's stored `sector_node` / `sector_level`
  rather than from a column of its own. A row matched only at sector
  level contributes to **unknown** — it has a sub sector, the source
  simply did not say which — while a cash position is `n/a` at both
  levels.

  `sub_sector` rides alongside `sector` rather than replacing it: targets,
  the deviation report and the X-ray cards all read "sector" meaning the
  middle level, and widening that name would have changed what it means
  to every one of them at once.

- **A Sub / Sector / Super selector on the fund's sector card**, in the
  same segment style as Country/Region. Purely a view control — it never
  leaves the browser, unlike the source buttons above it, which are a
  claim about where the fund's numbers come from and do propagate to the
  portfolio and the optimiser.

  *Sub* is struck through for a fund whose holdings carry no sub-sector
  classification, with a note that the issuer publishes at sector level
  and uploading holdings would fill it in. *Super* rolls the sectors up
  through the hierarchy the resource file owns, fetched rather than
  hardcoded; both residuals pass through untouched.

### Fixed
- **Per-facet coverage in `rollup_holdings` was row completeness**, the
  same number for every facet, so a full holdings list where nothing
  matched at sub-sector level reported 100% covered while every slice
  read "unknown". Coverage is now computed per facet as the share with an
  identified value, matching what `rollup_portfolio_fundlevel` has done
  since 0.51.0.

  This generalises the old meaning rather than replacing it: for a top-10
  list whose sectors are all known, the missing 79% *is* the unknown
  bucket, so it still reports 21%. Verified against that case.

### Still to come
- The same selector on the portfolio X-ray sector card, and the sub /
  super columns in the holdings list with their own filter and sort.
- Stage 3: targets at any level and the optimiser.

---

## [0.57.1] — 2026-08-04

### Fixed
- **A sub sector was unreachable by its own name** when a sector already
  listed that word among its matches. Canonical names were registered
  with `setdefault`, so the sector's claim — indexed first — kept the
  token and the sub-sector row existed with no way to select it. A
  canonical name now outranks any alias, and a deeper canonical outranks
  a shallower one, which is the deepest-match-wins rule applied to names
  as well as to match tokens.

  Found while preparing the sub-sector vocabulary: `software` is a match
  on the technology sector, so a `software` sub sector resolved to
  `technology` at sector level.

### Changed
- `sectors.csv` gains **50 sub sectors** across the twelve sectors, with
  their own match spellings. `is_default` is deliberately blank on all
  of them: a default sub sector would mean a fund reporting only
  "Technology" got silently assigned one, turning an honest *unknown*
  into an invented answer.
- **23 match tokens moved from sector rows down to the sub sector they
  actually name** — `software` and `semiconductors` off technology,
  `banks` and `insurance` off financial services, `pharma` and `biotech`
  off healthcare, and so on. A token naming a sub sector should not also
  be claimed by its parent: the deepest wins regardless, so the
  duplicate only produced a startup warning and a sector row that
  misdescribed what it matched.

  Verified no token resolves to a different sector than it did before,
  and `resolve_sector()` agrees with the old table on every one.

---

## [0.57.0] — 2026-08-04

Stage 1 of the three-level sector model: resolution and storage. Nothing
visible changes yet — the cards, columns and targets come next.

### Added
- **`resolve_sector_tree()`** resolves a raw value to all three levels at
  once and reports which level it matched at. The deepest match wins: a
  value naming a sub sector answers at sub-sector level and its
  ancestors are derived, while a value naming only a sector leaves the
  level below **unknown**.

  Unknown, not n/a. A Technology fund does have sub-sector exposure; the
  issuer simply did not publish it, and uploading the fund's holdings
  would reveal it — the "would more data fix it?" test the two-residual
  model has used since 0.51.0. Calling it not-applicable would report
  such a portfolio as fully covered at sub-sector level and hide the very
  gap the view exists to show. `n/a` still applies where the concept
  genuinely does not: a cash position has no sector at any level.

- **`SECTOR_NODE_ALIASES` and `SECTOR_LEVEL_OF`** — every spelling to its
  canonical name *at its own level*, beside the existing `SECTOR_ALIASES`
  which folds sub sectors up to their parent. Two tables deliberately:
  folding is a lossy view, and `resolve_sector()` keeps returning exactly
  what it always has, so no existing consumer changes behaviour.

- A **cross-level token collision** is reported at load. One spelling
  claimed by both a sector and a sub sector is an authoring mistake; the
  sub sector wins as the more specific, and the warning says so.

### Changed
- **Holdings rows store the deepest sector value plus its level**
  (`sector_node`, `sector_level`), and the sector-level name is derived
  from it. Storing two levels side by side lets a row say both "gold
  mining" and "technology", which has no answer — the same normalisation
  defect that had `bond` carrying two descriptions in
  `Holdings_class_definitions.csv`.

  Re-normalisation reads `sector_node` rather than `sector`, so repeated
  passes cannot quietly coarsen a row: re-resolving from a derivation
  loses the grain it was derived from. Verified idempotent.

### Still to come
- Stage 2: the three-way level selector on the fund and portfolio sector
  cards, and the two derived columns in the holdings list.
- Stage 3: targets at any level, with a parent's target held to at least
  the sum of its targeted children, and the optimiser running at the
  deepest targeted level. A target the data cannot measure — sub-sector
  targets against funds that only report at sector level — is reported as
  unmeasurable rather than blocking the run.

---

## [0.56.0] — 2026-08-04

### Changed
- **The upload dialog's preview shows the values that will actually be
  stored.** It printed the file's raw cells, so a holdings file saying
  "Aandelen" previewed as "Aandelen" while the commit wrote "equity" —
  the preview described the file rather than the outcome, which is the
  one thing it exists to show. Every mapping decision was being made
  against the wrong picture.

  Facet columns now show the resolved value in accent with the file's
  own text on hover, and an unrecognised value in red with a note that
  it will be stored as written and appear in *Resolve unmatched values*.
  So "renamed" and "not recognised" are distinguishable before
  committing rather than after.

### Added
- `POST /api/upload/normalise_sample` — resolves up to 50 mapped rows
  through the real `normalise_facets`.

  Resolution deliberately stays on the server. The weight sum in this
  same dialog is a client-side mirror of the backend's arithmetic and
  has already drifted from it once, which the comment above it records;
  re-implementing the whole facet resolver in the browser would be that
  mistake with far more surface. The table renders immediately with raw
  values and refines when the answer arrives, so mapping stays
  responsive, and a stale reply cannot overwrite a newer one.

---

## [0.55.5] — 2026-08-04

### Fixed
- **A resource file saved by a spreadsheet in a European locale was read
  as empty.** Excel writes CSV using the locale's list separator, which
  in most of Europe is `;` rather than `,`. Opening
  `Holdings_class_definitions.csv` and saving it re-delimited every line
  — and the parser, assuming commas, read the entire header as a single
  column name and discarded all 24 rows. The holdings taxonomy was empty
  with no error: no sub classes, no asset-class aliases, nothing.

  This is what made an alias added correctly to the `asset_class,equity`
  row do nothing. The alias was in exactly the right place; the file was
  never parsed.

  The delimiter is now sniffed from the header line, and files may
  differ from one another. It is also remembered per file and used when
  writing, so an alias added through the Resolve dialog gives the file
  back in the shape its editor expects rather than converting it every
  other save.
- The `Version=N` line now tolerates trailing delimiters. Excel pads it
  out to the column count — `Version=8;;;;;;` — which the strict pattern
  rejected, so the line was parsed as data and the version read as 0.

---

## [0.55.4] — 2026-08-04

### Added
- **The resource loader now reports a shifted column.** An alias appended
  with a comma instead of a pipe lands in `is_default` rather than
  `matches`, where it is silently ignored — the alias is visibly present
  in the file and does nothing, with no clue why. Two checks name it:
  an `is_default` cell that is neither true nor false, and any row with
  more cells than the header (an unquoted comma in a value shifts
  everything after it).

  Both print the row and say what to do, in the same style as the
  existing collision warning.

---

## [0.55.3] — 2026-08-04

### Fixed
- **An asset-class column holding a sub-class spelling stayed unmatched**,
  showing the raw value where the file could have named the asset class
  outright.

  In the pre-0.54 file every row had an asset_class *column*, so "the
  equity row" was the equity/**shares** row — and that row's `matches`
  column has always fed sub classes. An alias added there means
  *shares*, and the file states shares' parent is *equity*, so the asset
  class was fully determinable. Nothing was looking: the asset-class
  alias table had never heard of the value, and the sub-class table that
  knew exactly what it meant was never consulted.

  `normalise_facets` now reconciles the two columns, which describe the
  same thing at two grains:
  - a row naming a sub class but no asset class derives the asset class
    from `parent_name` (this had been added to the upload path in 0.55.1,
    but not to re-normalisation, so it never repaired existing rows);
  - an asset-class column holding a sub-class spelling resolves through
    the sub-class table to that sub class's parent.

  When both columns name an asset class and **disagree**, neither is
  trusted and the row stays unmatched — a real conflict for you to
  resolve, not one to pick a winner for. A genuinely unrecognised value
  still surfaces in the Resolve dialog as before.

---

## [0.55.2] — 2026-08-04

### Fixed
- **`normalise_facets` raised `UnboundLocalError: ac_norm` on any row with
  a blank asset class and a non-blank sub class**, surfacing as
  `[Cache] facet migration error (<ISIN>/asset_class)`. Introduced in
  0.55.1: restructuring the asset-class branch left `ac_norm` assigned
  only when the raw value was non-blank, but the sub-class check below
  pairs against it. It is now initialised before the branch.

  My own fault twice over — I verified the change against rows that had
  an asset class and never ran one that didn't. There is now a matrix
  test across blank, valid, unrecognised, mismatched and `None`
  combinations of the two fields.

### Changed
- **A sub class is no longer failed for the asset class's problem.** The
  pairing check — does this sub class belong to this asset class's group
  — needs a known asset class. When there isn't one, the sub class is
  judged on its own merits instead of being marked unmatched for a fault
  that belongs to the other column, which is already flagged in its own
  right.

---

## [0.55.1] — 2026-08-04

### Fixed
- **An asset-class alias added to the resource file could not repair
  rows that were already imported.** The alias itself worked — the file
  was read correctly, case folding was never the issue, and no version
  bump was needed. The problem was at the other end: when a raw value
  matched nothing, both the upload path and `normalise_facets`
  **overwrote the field**, first with `"other"` and, since 0.54.0, with
  `""`. That destroyed the only record of what the file had actually
  said, so re-normalisation later re-read a field with nothing left in
  it to re-resolve, and the rows stayed wrong however many aliases were
  added.

  The raw value is now preserved on a miss and the field reported as
  unmatched — which is exactly what `sector` has always done, three
  lines further down the same function. So adding the spelling repairs
  the existing rows on their next read, and until then the Resolve
  dialog can see and fix them.

  **Rows imported before this release cannot be repaired**: their raw
  values are already gone from the cache. Re-import those funds, or set
  the asset class by hand in the Resolve dialog.

- A holdings row that names a sub class but no asset class now derives
  the asset class from the sub class's `parent_name`, before falling
  back to the fund's own class. The file states which asset class each
  sub class rolls into, and the fund's class is a much weaker claim — a
  bond position inside an equity fund was being called equity.

### Note
The `Version=N` line no longer drives anything except display and the
migration check. Cache staleness has keyed off a content fingerprint
since 0.54.0, so a hand edit takes effect whether or not the version is
bumped. The alias writers still bump it, since a number that moves on
every write is a useful thing to see in the Tools tab.

---

## [0.55.0] — 2026-08-04

Stage 2 of the resource-file rework, plus asset class in the Resolve
dialog.

### Changed
- **`sectors.csv` moves to the hierarchical schema**, with three levels:
  `sub_sector` → `sector` → `super_sector`. The old file named super
  sectors inline on every sector row but never defined them, so they had
  no description, no matches of their own, and no way to add any. They
  are rows now; their descriptions are blank because there was nothing
  to carry over.
  `style_match` moves into `attrs`. It answers a different question from
  `matches` — `matches` normalises a holding's own sector value, while
  `style_match` recognises a sector inside a *fund's name* — and one
  column doing both jobs is what this rework is for.
  Only `sector` rows are classified against, exactly as before. Sub
  sectors are validated and indexed, and their spellings resolve up to
  the parent sector, so a holdings file using the finer vocabulary still
  classifies.
  Verified behaviour-preserving: the alias table, the style table, the
  sector names, super sectors and descriptions are all identical to what
  the pre-migration file produced.

### Fixed
- **`add_sector_alias` silently destroyed every `style_match` in the
  file.** It rebuilt `sectors.csv` from the loaded rows against a
  hardcoded four-column header, and `style_match` was not one of the
  four — so one alias added through the Resolve dialog wiped fund-name
  sector matching for all twelve sectors. Alias writing now rewrites the
  file preserving every column it carries, including ones the writer
  knows nothing about.

### Added
- **Asset class is an editable column in the Resolve dialog.** An
  unrecognised asset class was visible but unfixable without editing the
  CSV by hand — which is what 0.54.0 left open. Its dropdown is built
  from the same taxonomy as the sub-class list so the two cannot drift,
  it has a filter chip like the other facets, and confirming an edit
  writes the alias back through `add_holding_asset_class_alias`.
- `add_sub_sector_alias`, for the new level.

### Still to come
- Stage 3: countries and regions combined into one file, with
  `country_currency.csv` folding in as a `currency=` attribute.
- Stage 4: currencies, with `symbol` as an attribute.
- Super sector descriptions are blank and want filling in by hand.

---

## [0.54.0] — 2026-08-04

Stage 1 of the resource-file rework: the hierarchical schema, proved on
the smallest file.

### Changed
- **`Holdings_class_definitions.csv` moves to one row per concept**:
  `type,name,description,parent_name,matches,is_default,attrs`. The old
  shape stored asset-class facts on every sub-class row, so the file
  could contradict itself — `bond` carried two different descriptions
  across its eight rows and whichever loaded first won. The migration
  runs automatically on first load, keeps the most common spelling, and
  prints what it discarded rather than choosing quietly.
  `attrs` is `key=value;key=value`; `;` separates pairs because `|`
  already separates values *within* one attribute.
  Every consumer still receives the denormalised
  `{asset_class, asset_class_desc, sub_class, sub_class_desc, matches}`
  rows, now derived from the file rather than stored that way.
- **Validation on load**, reported the way the existing collision check
  is: unknown `type`, duplicate names, a `parent_name` that names no
  asset class, more than one `is_default` per parent, and a match token
  claimed by two classes.

### Fixed
- **An asset-class spelling added to the resource file had no effect.**
  `normalize_holding_asset_class` read a dict in `utils.py`, so the file
  was right and the code never looked — no restart could help, which is
  why adding "Aandelen" appeared to do nothing twice over. It now reads
  the file's `matches`.
- **An unrecognised asset class silently became `"other"`.** That is a
  real classification a file can legitimately assert, so a guess wearing
  its clothes was indistinguishable from an answer — and the value
  looked classified, so nothing ever asked about it. Unrecognised now
  leaves the field blank and the existing fund-level fallback applies.
- **The default sub class per asset class was hardcoded** in `utils.py`
  and unreachable from the file. It is the `is_default` column now.
- **`reload_resources()` reloads the country tables only**, despite its
  name; the other five files are reloaded by
  `reload_holdings_class_resources()`. Calling the first — which the
  name invites, and which I did — leaves five of seven files stale. Both
  are now wrapped by `reload_all_resources()`.
- **Cache staleness keyed off the declared `Version=N`**, which only
  moves when something remembers to bump it. A hand-edited file kept its
  old number, so every cache stamped with it believed itself current and
  never re-normalised. The stamp now carries a content fingerprint as
  well, so any edit invalidates it.

### Added
- **Resource files are re-read when they change on disk**, checked once
  per request rather than per classification — a file saved halfway
  through an upload must not leave some rows classified under the old
  ruleset and some under the new. When nothing has changed it is a
  `stat()` per file.
- **A "reload resource files" card in Tools**, for the cases the
  automatic check cannot see: a file restored from backup with an older
  timestamp, or a share whose timestamps are unreliable. Reports which
  files had actually changed, and lists each file's version and content
  fingerprint.
- `add_holding_asset_class_alias()`, the counterpart of the existing
  sub-class writer. The two are separate assertions: that a raw value
  names a particular sub class, and that it names an asset class without
  saying which.

### Still to come
- Stage 2 sectors (`sub_sector`/`sector`/`super_sector`), stage 3
  countries and regions combined, stage 4 currencies.
- The Resolve dialog does not yet offer asset class as an editable
  column, so an unrecognised asset class is visible but must be fixed by
  editing the CSV or re-importing.

---

## [0.53.1] — 2026-08-04

### Fixed
- **Focus detail accepted free text for a sector or region focus.** The
  dropdown was built from `_tgCanonical.sector`, which only the Targets
  editor ever populates — so opening the Edit fund dialog without having
  visited Targets found an empty list and fell through to a free-text
  input. Facet fields are strict dropdowns everywhere, and this was the
  one gap an unvalidatable value could get through.
  The dialog now fetches the sector and region vocabularies itself rather
  than borrowing another screen's state, and an empty list is reported as
  a load failure instead of silently becoming free text. Only a
  *thematic* focus is free text, which is deliberate — nothing can
  enumerate "Artificial Intelligence" ahead of time. A focus of `none`
  now says "not applicable" rather than offering an input for a value
  that cannot be set.

### Known, not yet fixed
- An asset-class spelling added to `Holdings_class_definitions.csv` still
  has no effect on holdings classification: `normalize_holding_asset_class`
  reads a table in `utils.py`, not the file, and an unrecognised value
  becomes `"other"` rather than surfacing in the Resolve dialog. Fixed
  properly by the resource-file rework rather than patched here, to avoid
  migrating the file twice in two releases.

---

## [0.53.0] — 2026-08-04

### Fixed — Resolve unmatched values
- **Ticking a checkbox scrolled back to the top.** The tick re-rendered
  the whole table so the summary count and select-all box could update,
  and replacing the scroll container's `innerHTML` reset its position —
  throwing the user back to the top of the list they were working down.
  A plain tick no longer redraws the rows at all; the checkbox already
  shows its own state, and only the count and the select-all box need
  syncing. Where a redraw is genuinely required — sort, shift-range,
  select-all — the scroll position is carried across.
- **Resolving everything in a filtered view left an empty table and an
  empty filter box.** The filter input lived in the dialog shell, which
  an apply rebuilds; the box came back blank while `ufFilter.text`
  survived, so the filter was still in force with nothing on screen
  saying so. The filter boxes are now rebuilt from state on every render
  and cannot disagree with what is applied. When a filter is what is
  hiding the remaining rows, the table says so and offers to clear it,
  and the dialog closes only when nothing is left anywhere rather than
  when the current filter happens to be empty.

### Added — Resolve unmatched values
- **A filter per column**, replacing the single box that matched across
  all of them. Filters narrow together, so "sector blank AND currency
  GBP" is now expressible — which is the question actually being asked
  when working through a resolution pass. Plus a "clear filters" button,
  since the facet chips and column boxes are otherwise cleared one at a
  time. Focus and caret survive the re-render each keystroke triggers.
- **Shift-click range selection.** Shift extends from the last row ticked
  without shift and applies that click's new state across the span, so it
  deselects as well as selects. The anchor stays put after a range, so a
  second shift-click re-spans from the same place rather than from
  wherever the last one ended.

---

## [0.52.1] — 2026-08-04

### Fixed
- **Peer groups were mostly one bucket.** A global technology equity fund
  and a hedged investment-grade credit fund shared a peer group, and were
  ranked against each other on cost and size.

  `_score_universe_cached` read the focus straight out of the override
  store. But the focus is normally *derived* — `_seed_fund_structure`
  reads it off the fund's own name — and the override store holds only
  what the user has hand-edited, which for most funds is nothing. So
  nearly every fund reported `focus_type: "none"`, and funds with no
  cached asset class reported `unknown` as well. The universe collapsed
  into a handful of `<class>|none|` groups and one large `unknown|none|`.
  The asset class had the same problem from the other side: the
  `primary_asset_class` override targets `asset_class.class`, and the
  view passed to `apply_overrides` contained only the profile block, so
  that override was silently dropped.

  Scoring now resolves both the way the fund page does — seed from the
  profile, then layer the stored overrides.

- **The optimiser's alternatives shortlist had no peer groups at all.**
  Its candidate dicts never carried an asset class or a focus, so
  `peer_key` answered `unknown|none|` for every one of them and the whole
  universe was a single peer group — a bond fund could be priced as an
  alternative to an equity tracker. The candidates now carry the peer
  fields, read off the already-resolved `load_fund_data` result.

### Changed
- **`peer_key` reads `primary_asset_class`, not `asset_class`.** The old
  name is now three different things: the breakdown facet (a distribution
  over classes), the cache category holding it, and the per-holding row
  field. The fund-level label was renamed in 0.47.0 and scoring was still
  asking for it by the old name — so `fund.get("asset_class")` quietly
  returned nothing rather than failing.

---

## [0.52.0] — 2026-08-04

### Added
- **Peer group, visible from both places a score is.** A rank means
  nothing without the field it was taken over, and until now the peer
  group was a number — `peer_n` — with no way to see who was in it.
  - Fund detail page: a `N peers ▼` control in the score row of the
    Operational group. Clicking a peer loads it: the fund is the subject
    of the page, so changing subject is the point.
  - Pre-loaded list: a Peers column using the same popover as Portfolios.
    Clicking a peer brings its row into view instead of loading it —
    there the list is what is being read, and navigating away would lose
    the reader's place. A peer excluded by the current filters is shown
    inert and says why rather than being a link that does nothing.

  Each row shows the peer score, sorted descending, with unranked funds
  last. The fund appears in its own list, marked: the group is "the funds
  this one is ranked against", and a fund belongs to its own. When the
  group is below `MIN_PEER_GROUP` the list still shows the members — two
  peers is worth seeing — with a line saying why there is no peer score.

- **Portfolio membership on the fund detail page**, combined with the add
  button rather than beside it: "where is this fund already" and "put it
  somewhere" are the same question asked a moment apart. Shows each
  portfolio with its share count, in the pre-loaded list's format.
  Informational only — jumping the reader out of the fund they are
  reading would answer a question they did not ask.

  Matched by **ticker**, as the pre-loaded list is. This page is
  per-listing — its own exchange, trading currency and ticker — and
  portfolios store `{ticker, shares}`, so a second listing of the same
  ISIN is a different holding and says so.

### Changed
- `/api/scores` blocks carry `name`, `isin` and `peers`. A consumer
  listing a peer group could previously derive the membership from
  `peer_key` but had no way to name anyone in it.

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
