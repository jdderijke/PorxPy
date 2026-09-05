# IMPORT_NEW_FUNDS_GUIDE.md — importing new funds and ETFs

*Current as of v0.102.1. Check the stamp against `porxpy/__init__.py`
before trusting a claim.*

Everything between typing an ISIN into an empty box and having a fully
described fund — holdings, breakdowns, structure fields, a peer group
and a score — sitting in your pre-loaded set, ready for a portfolio.

Installing PorxPy, loading the pre-loaded fund set that ships with the
repository, and building a first portfolio are a different subject, and
they live in [GETTING_STARTED.md](GETTING_STARTED.md). This guide
assumes the app is installed and running.

Sections 1–3 cover starting the app and finding your way around.
Sections 4–7 are the import routes themselves. Sections 8–14 are the
work of filling in what the first fetch could not supply. Sections
15–17 explain what you are looking at afterwards, and section 18 is the
whole thing as an ordered checklist.

---

## Contents

**Before you start**

1. [Starting the app](#1-starting-the-app)
2. [The menu structure](#2-the-menu-structure)
3. [Inside Explore Funds/ETFs](#3-inside-explore-fundsetfs)

**Getting a fund in**

4. [The four import routes](#4-the-four-import-routes)
5. [Route A — importing by ISIN](#5-route-a--importing-by-isin)
6. [Route B — importing by Yahoo ticker](#6-route-b--importing-by-yahoo-ticker)
7. [Saving the fund](#7-saving-the-fund)

**Filling the gaps**

8. [Why the resource files decide everything](#8-why-the-resource-files-decide-everything)
9. [The Resolve unmatched values dialog](#9-the-resolve-unmatched-values-dialog)
10. [The factsheet](#10-the-factsheet)
11. [Uploading holdings](#11-uploading-holdings)
    - [11b. How enrichment works](#11b-how-enrichment-works)
12. [Uploading a facet CSV](#12-uploading-a-facet-csv)
13. [The Edit fund dialog](#13-the-edit-fund-dialog)
14. [The Edit holding dialog](#14-the-edit-holding-dialog)

**Reading the screen**

15. [The tiles and every element in them](#15-the-tiles-and-every-element-in-them)
16. [How funds are grouped into peers](#16-how-funds-are-grouped-into-peers)
17. [The Pre-Loaded Funds / ETFs tab](#17-the-pre-loaded-funds--etfs-tab)

**Reference**

18. [A complete import, end to end](#18-a-complete-import-end-to-end)

---

## 1. Starting the app

PorxPy is a Flask application you run on your own machine. There is no
installer and no service — you start it from a terminal, and it serves a
single web page that talks to itself over HTTP.

Installing it — cloning the repository, creating the virtual environment,
installing the dependencies — is covered once in
[GETTING_STARTED.md](GETTING_STARTED.md) §2, together with loading the
pre-loaded fund set that gives you a universe to work from. Everything
below assumes that has been done.

From the project root, with the virtual environment active:

```bash
python main.py
```

You should see the startup banner, which is also how you confirm which
version you are looking at:

```
=======================================================
  PorxPy  v0.102.1 (built 2026-09-05)
  Portfolio X-ray Python
=======================================================
```

Then open <http://127.0.0.1:5000> in a browser. The version also appears
in the pill beside the PorxPy logo at the top left of the page, so you
can check that the page and the server agree.

The server listens on `0.0.0.0:5000` with Flask's debug reloader on, so
saving a Python file restarts it automatically. Stop it with `Ctrl+C`.

> **If the banner mentions legacy files.** A `NOTICE: pre-0.12 files
> detected` block means old-layout files are lying around from an earlier
> version. Nothing breaks — they are simply ignored. Clear them from
> **Settings → Danger zone** when convenient.

### The optional AI helper

Reading factsheets with Claude ([section 10](#10-the-factsheet)) is off by
default and needs two things: the environment variable
`ANTHROPIC_API_KEY` set *before* you start the app, and the toggle
switched on in **Settings**. The key is read from the environment and
never written to `settings.json`, which sits in the project directory in
plain text and tends to get copied around.

Everything else works offline once fetched. The only outbound traffic is
Yahoo Finance, OpenFIGI for ISIN lookups, and — if you ask for it —
justETF and the Anthropic API.

---

## 2. The menu structure

The page is one screen with a fixed header and four tabs. Nothing
navigates away; switching tabs swaps the panel below.

### The header

| Element | What it does |
|---|---|
| **PorxPy** logo | Brand, tagline, and the version pill — the running build, read from the server. |
| **Portfolio** selector | The active portfolio. Whatever is chosen here is the portfolio that fund pages check membership against, and the one the Portfolio tab shows. **+ New** creates one. |
| **⚠ banner** | Appears under the header, on any tab, when values anywhere in your data failed to resolve against the resource files. Clicking it opens the [Resolve unmatched values](#9-the-resolve-unmatched-values-dialog) dialog. It is application-wide, not per portfolio. |

### The four tabs

| Tab | Sub-tabs | What it is for |
|---|---|---|
| **Explore Funds/ETFs** | Load New Fund / ETF · Pre-Loaded Funds / ETFs | Importing funds, and everything you do to one fund. This guide lives here. |
| **Portfolio** | Funds · Holdings · Cash ⋮ History · X-ray ⋮ Targets · Optimizer | What you actually hold, the look-through analysis of it, your target allocations and the trade solver. The three groups answer *what is in it*, *how it has behaved and where it is exposed*, and *what you want and how to get there*. |
| **Tools** | — | Two cards: the **source inspector** (shows raw upstream responses for one fund, uncoerced, nothing cached or written) and **resource files** (version list plus a manual reload button). |
| **Settings** | — | Backup & restore bundles, cache age limits per field group, the enrichment field checklist, the scoring preset, the holdings match key, the AI toggle, and the danger zone. |

The Explore tab is selected on load, with **Load New Fund / ETF** active.
The badge on the **Pre-Loaded Funds / ETFs** sub-tab is a live count of
how many funds you have saved.

---

## 3. Inside Explore Funds/ETFs

The Explore tab has exactly two sub-tabs, and they answer two different
questions.

### Load New Fund / ETF

The import screen and, once something is loaded, the full fund page. From
top to bottom:

1. **The search panel** — one input that takes either an ISIN or a Yahoo
   ticker, an exchange dropdown that appears only when needed, and
   **Fetch →**. Below it a status bar, and two panels that appear only
   when the fetch needs something more from you.
2. **The fund header** — name, cache status, and the action buttons:
   save, optimiser opt-out, add to portfolio, reload, and the factsheet
   controls.
3. **Four group tiles** — Identification, Structure, Operational,
   Trading. Every data element about the fund lives in one of them. See
   [section 15](#15-the-tiles-and-every-element-in-them).
4. **The price chart** — adjusted close, period buttons from 1W to MAX,
   moving averages, an indexed mode, and peer overlays.
5. **Four breakdown cards** — asset class, sector, country, currency.
   Each with its own source selector, level selector and coverage badge.
6. **The holdings card** — the per-position table, with the enrich and
   upload buttons above it.

### Pre-Loaded Funds / ETFs

The list of every fund saved on disk — your universe. It is what the
optimiser draws from and what peer groups are computed across. Covered in
[section 17](#17-the-pre-loaded-funds--etfs-tab).

---

## 4. The four import routes

Two of these fetch a fund live from Yahoo; one restores funds in bulk
from a file; the fourth is not really an import but is how most funds
arrive after the first day.

| Route | Kind | Summary |
|---|---|---|
| **A** | live fetch | **ISIN + exchange.** Resolved through OpenFIGI, then confirmed on Yahoo. The exchange is required — you pick the MIC. May ask you to choose a currency. |
| **B** | live fetch | **Yahoo ticker.** Exchange is derived from the suffix. A bare ticker is refused. Will ask you for an ISIN to key the fund cache. |
| **C** | bulk | **Import a fund bundle** from Settings → backup & restore. Many funds at once, with holdings, factsheets, field pins and resource files. Two-phase: you see a conflict table before anything is written. The set shipped at `PreLoadedFunds/porxpy_funds.zip` arrives this way — see [GETTING_STARTED.md](GETTING_STARTED.md) §4. |
| **D** | re-open | **From an existing row** — a Pre-Loaded list row, a peer in a Peers popover, or a fund row on Portfolio → Funds. Not a new import. |

Routes A and B are the same button. PorxPy decides which one you are on
by looking at what you typed: the hint beside the input label reads
`detected: ISIN` or `detected: Yahoo ticker` as you type, and the form
reshapes itself accordingly.

Fund bundles carry the URL a fund's holdings or factsheet was fetched
from, so the recipient can refresh what they were handed, but not local
file paths — those are inert on anyone else's machine. Fund bundles also
deliberately carry no portfolios — that is a separate bundle
type, so you can take someone else's fund research without also taking
their holdings.

---

## 5. Route A — importing by ISIN

1. Go to **Explore Funds/ETFs → Load New Fund / ETF**.
2. Type the ISIN into the input, e.g. `IE00B4L5Y983`. The hint flips to
   `detected: ISIN` and an **Exchange (MIC)** dropdown appears beside it.
3. Pick the exchange. The list is grouped by region and each entry shows
   the MIC and its plain name — `XAMS – Euronext Amsterdam`. This is not
   optional: one ISIN is listed on many exchanges in many currencies, and
   each listing has its own price, ticker and trading currency.
4. Click **Fetch →**. The status bar shows `Resolving …` while PorxPy
   asks OpenFIGI for the ticker on that exchange and then confirms it on
   Yahoo.
5. If the fund resolves cleanly, the page fills in: header, tiles, chart,
   breakdown cards, holdings.

### When several currencies match

One ISIN on one exchange can still resolve to more than one
Yahoo-confirmed listing — typically a GBP and a GBp line, or a EUR and a
USD share class. PorxPy will not guess. A **currency picker** panel
appears listing the confirmed currencies; click one and the fetch is
re-issued for that listing. **Cancel** abandons it.

> **Why this matters later.** The listing you choose becomes a row in the
> pre-loaded list, keyed by ticker. The *fund* data behind it — holdings,
> breakdowns, factsheet, overrides — is keyed by ISIN and shared by every
> listing of the same fund. Import the GBP and the USD line of one ETF
> and you get two rows, one set of holdings.

---

## 6. Route B — importing by Yahoo ticker

1. Type the full Yahoo symbol **including its exchange suffix** —
   `IWDA.AS`, `BATG.L`, `VWCE.DE`. The hint reads
   `detected: Yahoo ticker` and a note underneath states the exchange it
   derived: `Exchange: Euronext Amsterdam (from .AS)`.
2. Click **Fetch →**.
3. PorxPy confirms the ticker on Yahoo, then — because a ticker
   identifies a *listing* and the fund cache is keyed by ISIN — asks you
   for an ISIN.
4. Type the ISIN (from the fund's factsheet or KID) and click **Load
   fund**. If you have fetched this ticker before, the box is pre-filled
   with the ISIN you used last time; press the button to confirm.

> **A bare ticker is refused.** Typing `IWDA` alone gives
> `add an exchange suffix (e.g. .L)` and the fetch will not run. A symbol
> without a suffix is ambiguous across exchanges, and guessing one would
> silently give you the wrong listing's prices.

> **The ISIN is a key, not a claim.** PorxPy does not checksum-validate
> the ISIN you supply on this route. It only has to be unique — it is
> used as the filename of the fund-level cache entry. A real ISIN
> naturally satisfies that, and is what you should use, but for an
> instrument that genuinely has none, any unique string works. It is
> saved so you are not asked again.

---

## 7. Saving the fund

A fetched fund is on screen but not necessarily on disk. Look at the fund
header:

| Control | What it does |
|---|---|
| **★ Save to pre-loaded** | Shown when the fund is **not** saved. Clicking it re-requests the fund with a commit flag, which persists profile, holdings, sectors, asset class and price history to the cache. |
| **✓ Saved** | Shown once it is on disk. Adding the fund to a portfolio saves it implicitly, so this often appears without you pressing anything. |
| **⚙ In optimizer / ⃠ Excluded** | Only shown for a saved fund. Flips whether the optimiser may buy or sell it. Stored per fund by ISIN, so it covers every listing. |
| **+ Add to portfolio** | Adds this listing to the active portfolio, and shows which portfolios already hold it. Membership is by ticker — two listings of one ETF are two holdings. |
| **↻ Reload Fund Data** | Refetches everything now regardless of age limits: profile, prices, sectors, and every pinned field from its own source. Fields set to your own value are left alone, and **uploaded holdings are never touched**. |

Once saved, the fund appears in the Pre-Loaded list and counts toward
peer groups and scoring.

---

## 8. Why the resource files decide everything

Every facet value that arrives from anywhere — a Yahoo sector name, a
country in a holdings file, an asset type on a factsheet — is a raw
string. It only becomes a bucket in a breakdown if it can be *resolved*
against a reference CSV in `resources/`. A value that resolves to nothing
does not become its own slice; it counts as `unknown`, with the raw text
kept so you can fix it later.

That single rule is why a fund can look badly described on import and
become well described half an hour later without anything being
refetched.

### The five files

| File | Supplies | Levels, finest first |
|---|---|---|
| `Asset_definitions.csv` | The asset **breakdown** vocabulary — what a fund's holdings roll up to | `sub_class` → `asset_class` → `super_class` |
| `Sector_definitions.csv` | Morningstar sector taxonomy | `sub_sector` → `sector` → `super_sector` |
| `Geography_definitions.csv` | The whole country tree, plus pan-regional focus groups | `country` → `region` → `super_region` |
| `Currency_definitions.csv` | ISO-4217 currencies | `currency` |
| `Primary_asset_class_definitions.csv` | What kind of fund this **is** — one label, never derived from holdings | — |

> **Two files that sound alike.** `Asset_definitions.csv` is the
> vocabulary a *breakdown* rolls up to — a distribution over holdings.
> `Primary_asset_class_definitions.csv` is a single classification of the
> fund itself. A 60/40 fund is `mixed` in the second while its breakdown
> is 60% equity / 40% fixed income in the first. **Peer-group selection
> reads the classification**, not the breakdown.

### The shared schema

All five files use one layout:

```
type,name,description,parent_name,matches,is_default,attrs
```

Two columns do the work:

- **`parent_name`** is singular. That is what makes each tree a chain by
  construction rather than by convention — every node has exactly one
  parent, so a value that resolves at any level automatically carries its
  ancestors.
- **`matches`** holds every spelling that should resolve to that row:
  alpha-2 and alpha-3 codes, numeric ISO codes, index names, and
  non-English labels. A Dutch factsheet says *Aandelen*; a holdings file
  may say `756` for Switzerland. Both belong in `matches`.

There is no version line and no header of any kind above the column row
— the first line of every file is `type,name,…`. The delimiter, however,
is sniffed per file, so a CSV saved by a spreadsheet in a locale that
uses `;` is read as-is rather than silently parsed as one enormous
column. That is not theoretical: `Asset_definitions.csv`,
`Sector_definitions.csv` and `Primary_asset_class_definitions.csv` are
semicolon-delimited today, `Currency_definitions.csv` and
`Geography_definitions.csv` comma-delimited, and neither group needs
converting.

### Editing them

You can edit a resource CSV by hand at any time. Change detection is by
**content fingerprint** — a short hash of the file's bytes, re-checked
before each request — so a saved edit applies to the next thing you do,
with no restart, no number to bump and no migration. Cached funds record
the fingerprint they were classified against and re-evaluate themselves
when it no longer matches, so an edit reaches holdings you imported last
month.

> **Why there is no version number.** A declared `Version=N` on each
> file's first line did exist, and was removed in v0.64.0. A number only
> moves when whoever edited the file remembers to bump it, so a file
> corrected by hand kept its old one — and every cache stamped with that
> number believed itself current and never re-normalised. The fingerprint
> answers the real question, and answers it for hand edits too. A leading
> `Version=` line in an old local copy is still skipped on read so such a
> file keeps working, but nothing writes one.

Use **Tools → Reload resource files** when the automatic check cannot see
the change — a file restored from a backup with an older timestamp, or a
network share with unreliable timestamps. The status line beside the
button reports which files had in fact changed, or says that every file
already matched what was in memory.

> **The consequence worth remembering.** Resolution happens at
> *derivation* time, not at import time. Adding an alias to a resource CSV
> fixes every past extraction — every fund, every source, including a
> factsheet read weeks ago — on the next read. Nothing is rewritten and
> nothing needs re-importing.

---

## 9. The Resolve unmatched values dialog

When anything fails to resolve, the amber banner appears under the page
header on every tab. Click **Review →** to open the dialog. It has two
tabs, and they exist because there are two different kinds of problem.

### By value

One row per **distinct** unrecognised value, across every fund and every
source — holdings, Yahoo, factsheets and uploaded CSVs alike — sorted by
weight so the ones that matter float to the top. Pick what a value
*means* from the dropdown and the alias is written into the resource
file's `matches` column. Every occurrence everywhere resolves on the next
read.

Sector and country targets name a **level**, and so does this mapping.
Mapping *"AI Hype"* to the semiconductors sub-sector claims the source
told you the sub-sector; mapping it to technology claims it told you only
the sector. Choose the level the source actually asserted.

Values that are junk — a footnote marker, a stray header row — have no
honest target. Leave them; they sink to the bottom on the weight sort.

### By row

For what an alias cannot express: a **blank** value, which has nothing to
alias, and a row that is simply **wrong** rather than differently spelled
— a fact about that one holding, not about the vocabulary. Unmatched
cells are shown in red. Sort by clicking a header, filter to narrow the
list, tick the rows you want, then click **edit** on a column header to
set a canonical value on every selected row at once.

> **There is deliberately no "define a new canonical" button.** The dialog
> maps a spelling onto a concept the vocabulary already has. Inventing a
> new node is a change to the taxonomy, which belongs in a hand-edit of
> the CSV — where you can also give it a parent.

### `unknown` is not `n/a`

Every facet distinguishes two residuals, and both appear in breakdowns
and holdings tables:

| Shown as | Means | Can better data fix it? |
|---|---|---|
| `unknown` | There is a value; this source does not have it — or it did not resolve. | Yes. It counts against coverage. |
| `n/a` | There is no value to have. A cash balance has no sector and no country. | No, and it does not count against coverage. |
| `—` (in a holdings row) | This row says nothing at all about this facet. | Yes — enrich or edit the row. |

---

## 10. The factsheet

Yahoo covers some funds badly. The issuer's own factsheet usually has
everything Yahoo is missing — TER, fund size, replication, the number of
holdings, and the breakdowns themselves. PorxPy lets you attach that
document to the fund and, optionally, have it read.

### Uploading one

1. With the fund loaded, click **📄 Upload factsheet** in the fund
   header. (It reads **📄 Replace factsheet** once one exists.)
2. Give it a source: paste a URL, paste a local path, click
   **📁 Browse…** to pick from disk, or drop the file onto the panel.
   PDF, image (a photo of a printed sheet is fine) and HTML are accepted.
   If you have uploaded a factsheet for this fund before, the field opens
   with that source already in it — an issuer publishes next month's
   sheet at the same address as this month's, so replacing it is usually
   a matter of clicking **Replace**.
3. Set the **Factsheet date** — the date printed on the sheet itself, not
   today. This is the field most worth filling in: age is measured from
   it, and a sheet can be three years old on the day you upload it.
4. Optionally add a note, e.g. `share class A, EUR`.
5. **Upload**.

The document is stored with the fund and shared by every listing of it. A
newer upload replaces the previous one. Factsheets are never expired
automatically — nothing can replace a document you went and found — but
one older than 183 days (adjustable in Settings) is flagged stale, and
the **👁 View factsheet** button grows a ⚠.

### Reading it with Claude

Once a factsheet exists, an **✨ Extraction** button appears. It opens the
report, which holds the extraction action itself — so reviewing a
previous reading costs nothing and survives a restart.

Clicking **✨ Extract now** sends the document to the Anthropic API. The
report then shows:

- **Fields** — one row per value read, with the *page number* and the
  *verbatim quote* it came from.
- **Breakdowns** — per facet, how many rows and what they sum to, and
  whether the reading is complete or partial. A partial breakdown shows
  the remainder as `unknown` rather than being scaled up.
- **Holdings** — the position table the document prints, if it prints
  one. These become the **Issuer (factsheet)** source on the holdings
  tile (section 11), beside Yahoo's top-10 and any file you upload. The
  sector / country / currency columns are stored exactly as the document
  worded them and resolved against the definitions files on every read,
  so teaching an alias later repairs a sheet read earlier.
- **Not used** — what was rejected and why.
- The approximate API cost, the factsheet's own date, and an expandable
  panel showing the exact prompt that was sent.

One field is *derived* rather than transcribed. **Market cap** is read
from a market-capitalisation exposure table where the document prints one
— the band with the largest exposure wins, with large ≥ $10bn, mid
$2bn–$10bn and small below $2bn, so an issuer's own giant/large/mid/small
buckets map onto ours. If the sheet gives only a weighted-average or
median market cap, the answer is **mixed**: an average is one number
standing in for a distribution, and a total-market fund holding thousands
of small caps still averages in the hundreds of billions.

> **Nothing is applied automatically.** The extraction is staged, not
> committed. To take a **field**, open the
> [Edit fund dialog](#13-the-edit-fund-dialog) and set that field's source
> to **Factsheet**. To take a **breakdown**, click **Issuer (factsheet)**
> on the relevant card. To take the **positions**, click
> **Issuer (factsheet)** on the holdings tile. You approve each value
> with its quote in front of you.

Replacing or deleting the factsheet removes what was read from it — the
breakdowns and the positions both. They are readings of one document, and
last year's list must not survive under this year's sheet.

If the button explains that the helper is off or no key was found, see
[section 1](#1-starting-the-app) — the toggle is in Settings and the key
must be in the environment before the app starts.

---

## 11. Uploading holdings

Yahoo publishes at most the top ten holdings of a fund, which for a
1,400-position world tracker covers around 15% of it. A real X-ray needs
the issuer's full list. The holdings card at the bottom of the fund page
is where that happens.

### The three sources

Since v0.77.0 a fund can hold **three** position lists at once, and you
choose which one it shows — the same selector, in the same words, as the
four breakdown cards above it:

| Button | Where the rows come from | Pill in the list |
|---|---|---|
| **Issuer (Yahoo)** | Yahoo's top-10, plus whatever *Enrich through Yahoo* filled in | `TOP · 10`, or `TOP · 10 · ENRICHED` |
| **Issuer (factsheet)** | The position table read off the factsheet (section 10) | `FACTSHEET · 10` |
| **Upload** | The CSV or XLSX you supplied | `FULL · 1442` |

A source this fund does not have is struck through. The choice is saved
**with the fund**, not with the browser: every portfolio holding it, and
the optimiser, use the list you picked here. With no choice saved the
richest list wins — upload, then factsheet, then Yahoo — which is what a
fresh upload does without you having to also select it.

Uploading no longer destroys what was there. Yahoo's rows survive an
upload, an upload survives an extraction, and **Remove uploaded** returns
the tile to whichever of the other two the fund still has, rather than to
an empty table.

> **A reload refreshes Yahoo's slot only.** **↻ Reload Fund Data** and
> **↻ Live refresh all** refetch Yahoo's top-10 — which they can now do
> safely even on a fund you have uploaded to, because the upload is in a
> different slot. Your file and your factsheet's positions are never
> refetched: nothing exists to refetch them from.

### The cheap step first: Enrich through Yahoo

**↻ Enrich through Yahoo** looks up holdings one at a time and fills in
what the row does not already say — blanks only, never a value your file
supplied, and never a facet you have set by hand on a row (those are
pinned, and the pin wins). It works on whichever source the tile is
showing and writes back into that same source, so enriching a
factsheet's positions does not deposit them in Yahoo's slot. Which
fields it may fill is your choice, in **Settings → enrichment**; the
five available are name, country, currency, asset class and sector. The
run reports its progress on the shared progress bar, so a few thousand
holdings is a bar rather than a frozen button.

**It acts on the rows you tick, and only those.** The button is disabled
until something is ticked, and its label says how many
("Enrich 12 selected through Yahoo"). Tick boxes are the leftmost column
of the holdings table; the header box selects everything currently
visible, so "all of them" is one click, and shift-click extends a range.
An empty selection is never read as "all" — on a fund of a few thousand
holdings that would be minutes of per-holding network calls nobody
asked for.

It is also worth pressing **again** after you have corrected a row by
hand. A ticker Yahoo failed to recognise is remembered so a re-upload
does not re-probe it, but this button clears that memory before it
looks — so adding an ISIN to a row and pressing Enrich really does try
again.

> **The full account is [section 11b](#11b-how-enrichment-works)**: what
> gets filled and what is never touched, how a holding is identified
> (ISIN → ticker → CUSIP → name), what "refinement" means, what is
> remembered between runs, and why a run sometimes stops early.

> **The first run after upgrading to v0.99.1 re-asks Yahoo once per
> symbol.** Per-symbol answers are cached for 90 days, and entries
> written before the industry was read carry no industry at all — so
> they are treated as stale and replaced on first use. Nothing is lost;
> the run is simply slower once.

### The full upload

Click **⬆ Upload holdings**. The dialog runs in two views.

**View 1 — where the file is.** One field accepts either form:

- **A URL**, e.g. an iShares product-page CSV link. The server fetches it
  directly.
- **A local path**, e.g. `C:\Users\me\Downloads\AGGH.xlsx`, or the same
  as a `file://` URI. The server reads it from disk.

You can also click **📁 Browse…** for an in-app file browser, or drop the
file onto the panel. A dropped file is copied into PorxPy's scratch
folder — a path or a URL keeps working indefinitely, a dropped copy only
until that folder is cleared, which is a good reason to prefer the first
two. Whatever you enter is remembered *per fund*, so next time the dialog
opens pre-filled, with a line underneath naming where that came from and
when. All three upload dialogs — this one, the factsheet (section 10) and
the facet CSV (section 12) — remember their source the same way, because
an issuer re-publishes a document at the address it published the last
one at. Click **Parse file →**.

**View 2 — mapping the columns.** Four file-level controls sit at the
top: **Sheet** (XLSX only), **Header row**, **Decimal notation** (auto /
dot / comma) and **Weight unit** (auto / percent / fraction).
Auto-detection is usually right; the controls exist for when it is not.

Below them, one row per PorxPy field. Each row has a dropdown listing
your file's columns, and PorxPy pre-selects a best guess by matching
header names against known aliases — so most of the time you are
confirming rather than choosing.

| Field | Required | Default value |
|---|---|---|
| Name | yes | — |
| Weight | yes | — |
| Ticker | no | — |
| ISIN | no | — |
| CUSIP | no | — |
| Sector | no | yes |
| Country | no | yes |
| Currency | no | yes |
| Asset | no | yes |
| Duration | no | yes |
| Maturity | no | yes |
| Coupon % | no | yes |
| Effective date | no | yes |
| Quality (rating) | no | yes |

For the four facet fields, an unmapped column can be given a **default
value** applied to every row.

Under the list is one checkbox: **Fill blanks from Yahoo after import**
(v0.100.0). It is the same operation as the fund page's *Enrich through
Yahoo*, running over every imported row instead of the ticked ones —
one routine, one behaviour, so the two cannot answer differently. Which
fields it may fill is set once in **Settings → enrichment** and is not
asked again here.

It replaced four per-field **📥 Yahoo** buttons, which were a false
economy: the lookup is one network call per *row* that answers every
field at once, so restricting the fields never saved a call. All they
did was let this dialog disagree with Settings about what enrichment
does — and default to nothing ticked, so an import quietly enriched
nothing while Settings said otherwise.

Precedence is unchanged: file > Yahoo > default. A mapped column that
actually says something is never overwritten, Yahoo fills its holes,
and the default fills whatever is left. A row is looked up by its ISIN,
ticker or CUSIP where the file has one and by its **name** where it does
not, so a position table of names and weights can be enriched too —
slower and less certain, which the note beside the checkbox says.

A live preview of the first five mapped rows sits below, with a running
weight sum. Click **Save holdings** to commit.

### How messy tickers get resolved

Issuers use different ticker conventions — Bloomberg-spaced `AIR FP`,
dot-separated `AIR.FP`, Refinitiv suffixes, concatenated country codes
like `PLTRUS`. You do not have to clean the file first: the upload
identifies each row through the same chain, in the same order of trust,
that the enrichment button uses, and writes the canonical ticker, ISIN
and CUSIP back to the row so the data is clean going forward. That chain
is set out in [section 11b](#11b-how-enrichment-works).

> **Uploaded holdings are yours, permanently.** A manual upload is
> **never** touched by **↻ Reload Fund Data** or by **↻ Live refresh all**
> on the portfolio. To update it, upload again — the source and column
> mapping are remembered, so it is two clicks. **Remove uploaded** drops
> that source and returns the tile to the factsheet's positions or
> Yahoo's top-10, whichever the fund still has.

### The holdings table

Columns: Name, Ticker, ISIN, CUSIP, Sector, Asset, Country, Cur, Weight —
plus five bond columns (Duration, Maturity, Coupon %, Eff. Date, Rating)
behind a **▶ Show bond columns** toggle. **Rating** is credit quality,
stored exactly as the source wrote it — "BBB-", "Baa3" and "AA (sf)" all
survive verbatim — but sorted by credit standing rather than
alphabetically, with both agencies' scales read onto one scale, so Baa2
and BBB are the same rung and AAA sorts above AA+. There is a search box, a per-column
filter row, and sortable headers. Sector, Asset and Country headers carry
**level chips**; clicking one switches that column between the levels of
its tree, and the column sorts on whichever level is displayed. The chip
row owns the whole bottom line of the header cell, so a click that misses
a chip changes nothing rather than re-sorting the table; sorting is the
column name on the line above.

---

## 11b. How enrichment works

Enrichment is the app asking Yahoo about each *holding* — not about the
fund — and writing back what the holding's own row does not already say.
It is what turns a file of 1,442 names and weights into rows a sector,
country, currency and asset breakdown can be folded out of.

Two things run it, and they are the **same loop** over the same rows, so
they cannot disagree:

- **↻ Enrich through Yahoo** on the holdings tile, over the rows already
  cached;
- the **holdings upload**, as its third pass, over the rows just parsed
  out of your file.

The only thing a caller decides is *which rows*. Everything below is
true of both.

### What it fills

Five fields, each landing in one column:

| Field | What Yahoo is asked for | Where it lands on the row |
|---|---|---|
| **name** | the security's own name | `name` |
| **country** | the company's domicile | `country_raw` |
| **currency** | the trading currency of the listing | `currency_raw` |
| **asset class** | `sub_class` where it can be placed, else `asset_class` | `asset_raw` |
| **sector** | `industry` where it can be placed, else `sector` | `sector_raw` |

Which of the five may be filled is **your choice**, in
**Settings → enrichment**. An unticked field is skipped on every path,
including the upload's pass; untick all five and enrichment does nothing
at all, which is a supported way to run.

Two details in that table carry more weight than they look.

**Facet values land in the `*_raw` column, never in a level column.** The
raw is what a *source said*; every level of the tree — `sub_sector` /
`sector` / `super_sector`, and the same for the other three facets — is
re-derived from it on the next read. Writing a level directly is silently
undone by that pass. It also means an enriched value no resource file can
place is not lost: it sits in the raw column and turns up in the
**Resolve unmatched values** dialog (section 9), where one alias fixes it
and every past row re-derives.

**The finest answer that resolves wins.** Asset and sector are each one
tree at several grains, and a finer value yields the coarser ones while
the reverse is impossible — so Yahoo's `industry` is preferred to its
`sector`, and its `sub_class` to its `asset_class`. But *only where the
definitions file can place the finer value*: roughly half of Yahoo's ~145
industries have no home in `Sector_definitions.csv` yet, and writing an
unplaceable industry over a placeable sector would turn a correct answer
into `unknown` — more detail, less information. So the coarse answer is
taken whenever the fine one cannot be placed, and a missing alias costs
nothing until somebody adds it.

### What it never touches

- **Weights.** `weight_pct` is the issuer's statement about the fund, and
  Yahoo has no opinion on it.
- **The bond columns** — duration, maturity, coupon, effective date and
  rating. Yahoo's per-symbol endpoint does not carry them, and a blank
  here is the normal state of an equity row rather than a gap.
- **The level columns** themselves, for the reason above: they are
  derived, not stored decisions.
- **Any facet you have pinned** by editing the row (section 14). A pin is
  a decision you made about this holding, so it counts as occupied even
  when its raw column is empty — which is its ordinary state after a
  by-row edit, because the edit states a *node* rather than a source's
  wording. Writing the raw would not move the row, but it would change
  what the Resolve dialog offers to alias, attributing Yahoo's wording to
  a value you chose by hand.
- **A well-formed identifier.** See *the two corrections* below.

### How a holding is identified

A row is looked up by whatever identifies it, in a fixed order of trust:
**ISIN → ticker → CUSIP → name**. An ISIN names exactly one security; a
ticker is ambiguous across exchanges, a CUSIP covers North America only,
and a name is a guess. A row carrying Apple's ISIN beside a ticker of
`MSFT` therefore resolves to Apple, and the same row with the ISIN
corrupted falls through to the ticker and resolves to Microsoft.

In full, the chain stops at the first step Yahoo answers:

1. **The ISIN**, when it is valid — check digit, not merely shape —
   passed to Yahoo's search endpoint. It runs *first and by right*: an
   identifier naming one security outranks a symbol the file also
   supplied.
2. **The ticker the file wrote**, through plausible Yahoo rewrites.
   Issuers use Bloomberg-spaced `AIR FP`, dot-separated `AIR.FP`,
   Refinitiv suffixes, concatenated country codes like `PLTRUS`; you do
   not have to clean the file first.
3. **The ticker with the ISIN's country suffix** — `AIR` on a row whose
   ISIN begins `FR` is probed as `AIR.PA`. This rewrites the *symbol*
   rather than asking who the ISIN is, which is why it sits with the
   ticker rather than with step 1.
4. **The CUSIP**, by search. Exact like an ISIN but North America only,
   so it answers after the ticker the file actually wrote.
5. **The row's own country as an exchange hint** — a bare `SIE` on a row
   that says *Duitsland* is probed as `SIE.DE`. A ticker plus a country
   is a stronger claim than a name, so this comes before the last step.
6. **The name**, last, and treated as the guess it is.

> **A guessed ticker has to agree with the file.** A short symbol is a
> guess about which listing is meant, and Yahoo will answer with
> whichever company owns that symbol somewhere in the world: `CAP` in a
> European fund file is Capgemini in Paris, and Yahoo's bare `CAP`
> neighbours `CAP.SN`, a Chilean steel producer. So a **ticker-variant**
> candidate is rejected when its currency *and* its domicile both
> contradict the row's own, and probing continues. Both have to
> disagree, because either alone is legitimately noisy — some issuers
> report every row in the fund's reporting currency, and Yahoo's country
> is the company's domicile rather than the listing's. The exception is a
> file whose currency column *varies* across its rows: that column is
> then stating each listing's trading currency, and a mismatch is
> decisive on its own. Steps 1 and 4 are never second-guessed — they name
> one security by construction — and step 6 is already strict.

The **name search** is strict on purpose, because it is what a
factsheet's position table usually leaves you with, and a wrong hit
attaches another company's sector, country and currency to your
position — worse than leaving the row blank. Every word of the printed
name must appear in the candidate's own name, **in the same order**,
after both sides have been stripped of corporate-form noise ("NV", "SA",
"plc", "Ltd", "Holding", "Class A", "ADR" and their kin, plus every
single letter). "Novo Nordisk" therefore matches "Novo Nordisk A/S" and
does not match "Novo Integrated Sciences". It also searches the *head*
of the name, stopping at the first token carrying a digit: "US TREASURY
N/B 4.25% 15/11/2034" searches as "US TREASURY N/B", because the coupon
and maturity are precise and unsearchable. Only if that finds nothing is
the whole name tried.

Once a row resolves, the canonical ticker, ISIN and CUSIP are written
back to it, so the data is clean going forward.

### Blank-only, and what "refinement" means

The rule is **blank-only**: a value already on the row — from your file,
from a previous run, or pinned by hand — is not overwritten. That is what
makes the button safe to press repeatedly, and what keeps the upload's
documented precedence (file > enrichment > default) true by construction
rather than by everyone remembering to check.

But *occupied* and *answered* are different things, and only the second
is a reason not to write. A row whose file said "IT" has a sector and no
sub-sector: the cell is full, the finer level is unanswered, and
blank-only alone would leave it empty forever even though Yahoo knows it.
So exactly one case writes into a non-blank cell — a **refinement**:

> A value is a refinement when it resolves at a **strictly finer level**
> than what the row already carries, **and agrees with it at every level
> the row already states**.

Worked through on a row whose `sector_raw` says "IT", which the resource
files place at sector `technology` with the sub-sector left `unknown` —
a gap that can still be closed, not an answer (section 9):

| Yahoo says | Resolves to | Written? | Why |
|---|---|---|---|
| Semiconductor Equipment & Materials | `technology` / `semiconductors` | **yes** | finer, and still `technology` — it adds a level without changing a claim |
| Banks | `financial services` / `banks` | no | finer, but it *contradicts* the sector the row states |
| Technology | `technology` | no | same grain — nothing to add |
| an unplaceable industry | nothing | no | a value that places nothing cannot refine anything; it is a question for the Resolve dialog, not a claim to deepen |

A disagreement between two sources is not the tool's to settle silently,
which is why the second row is left to you rather than won by whichever
source ran last. Refinement applies to any facet, since every facet is a
tree — it is simply sector and asset class where a source routinely
states the coarse grain and stops.

### The two corrections

Two things *are* overwritten, and both are corrections rather than
opinions:

- **The name, when an identifier resolved the row.** An ISIN, ticker or
  CUSIP match describes the same security, so Yahoo's spelling is a
  better spelling of it — and the one the rest of the app can search on
  again: "APPLE INC COMMON STOCK USD0.00001" becomes "Apple Inc." A
  **name** match never rewrites the name it searched on; that would
  launder a guess into a fact and make the next run's guess a different
  one. It also happens only when `name` is among your ticked fields — the
  setting is the consent.
- **A malformed identifier.** A blank ISIN or CUSIP is filled; a
  *malformed* one is replaced once the row has resolved by another route;
  a **valid** one is always left alone. Validity means the check digit,
  not the shape, because a transposed digit has the right shape, never
  resolves, and would otherwise be re-probed forever.

The status line reports the two separately — "filled 40 gaps" and
"overwrote 40 values your file supplied" are different claims, and the
second is the one worth reading.

### What is remembered between runs

Three caches make a second run cheap, and one of them can surprise you:

| Cache | Keyed by | Lives |
|---|---|---|
| Symbol info | the resolved Yahoo ticker | 90 days |
| Alias | the raw input, plus the row's country when it has one | no expiry |
| Negative alias | the same key | no expiry |

Every fund holding `AAPL` benefits from one lookup, and once
`PLTRUS → PLTR` is known, later rows skip the chain entirely. The alias
key carries the row's country because a raw ticker is not unique: an
issuer file writes `SAN` for Banco Santander in Madrid and `SAN` for
Sanofi in Paris, in the same file, and one key for two companies means
whichever resolved first answers for both.

The **negative** alias is the surprising one: an input Yahoo recognised
in no variant is remembered as such, so a re-upload does not re-probe it.
That is why the tile's button deliberately **clears the negative first** —
its whole purpose is to be pressed after you have corrected a row, and
the negative was recorded against the identifiers the row used to carry.
Adding an ISIN and pressing Enrich really does try again.

A lookup that failed because the *network* failed is never cached in
either direction: an outage that recorded negatives would outlive itself.

### When a run stops early

Enrichment is one network call per holding, so a large fund is thousands
of requests and a rate limit is a question of when rather than whether.
When twelve lookups **in a row fail to complete** — a rate limit, a
dropped connection, a proxy refusing — the run stops itself, keeps what
it had already enriched, and reports how many rows it never attempted
(v0.101.0).

It does **not** stop for holdings Yahoo simply does not know: that is a
different counter, and a file full of unlisted bonds must run to the end
rather than abort on its twelfth row. It also does not sleep between
calls or retry inside the run — a fixed delay makes an already-long run
longer without addressing the case, and retrying is how a rate limit
becomes a longer rate limit.

### Reading the result

The status line afterwards separates outcomes that look identical if you
only count what was filled. On a run that did something:

```
Enriched 226 rows (name: 219, sector: 198) · corrected 3 isin
```

The two halves are different claims, and the second is the one worth
reading: "filled 219 gaps" and "overwrote 3 values your file supplied"
are not the same sentence.

When nothing was filled, the line says **why**, because the causes want
different responses from you:

| The line says | What happened | What to do |
|---|---|---|
| `No blanks filled — 4 lookups failed (…)` | the requests did not complete: network, proxy, or a rate limit | try again later; nothing is known either way yet |
| `No blanks filled — 23 not found on Yahoo` | asked and answered: no such security | correct the row's identifiers, or accept that it is unlisted |
| `No blanks filled — 2 with nothing to look up` | no ISIN, ticker, CUSIP or name at all on the row | fill something in by hand first (section 14) |
| `No blanks needed filling (250 rows checked)` | every selected row was already answered | nothing — this is the healthy end state |
| `Enrichment stopped early. …  88 row(s) were not attempted.` | the circuit breaker above | press the button again later; the rows already done are kept |

A run that filled nothing because Yahoo was unreachable, one that filled
nothing because the holdings are unlisted bonds, and one that filled
nothing because there was nothing to ask about are three different
problems — and without this they are one blank status line.

---

## 12. Uploading a facet CSV

Sometimes you have the fund's published breakdown but not its holdings —
a factsheet gives you a country split and a sector split and nothing
else. Rather than inventing 1,400 rows to produce four percentages, you
can upload the percentages directly.

### Where the sources live

Each of the four breakdown cards on the fund page has its own source
selector. Every source that *applies to that facet* is shown; ones the
fund does not happen to have are struck through and inert, because
uploading a factsheet or a CSV is exactly what would fill them.

| Source | The numbers are… | Available when |
|---|---|---|
| **Issuer (Yahoo)** | The issuer's own card, as Yahoo reports it | always |
| **Issuer (factsheet)** | The issuer's numbers, read off the uploaded factsheet | an extraction has been run |
| **Holdings** | Computed by rolling up this fund's own holdings | holdings rows exist |
| **Upload** | A CSV you supplied | a CSV covering that facet exists |
| **From country** | The fund's own country card, converted country by country to each country's primary currency | **currency card only**, when the country card identifies something |

**From country** is for the many funds whose issuer publishes a
geographic split and no currency split at all. It appears on the currency
card and nowhere else — a sector or asset split derived from geography
would mean nothing, so it is not a source those cards lack, it is not one
of their sources. The conversion reads the country card at **country**
level: a region names no single currency.

It follows the card it converts. An `unknown` slice on the country card
is an `unknown` slice here; `n/a` stays `n/a`; and a country with no
currency on file lands in `unknown` and is named in the card's unresolved
list — which is one row in `Geography_definitions.csv` away from being
fixed. The reverse is not offered: a currency does not name a country, so
country-from-currency would invent detail rather than convert it.

> **A source is not a view.** Choosing a source persists as a fund-level
> override keyed by ISIN. Every portfolio that holds this fund, and the
> optimiser, will use it. The **level** chips beside the source buttons
> are the opposite — a pure browser-side view of the same finished data,
> changing nothing anywhere else.

### When the coverage is all you will ever get

A card whose source only covers part of the fund shows the rest as
`unknown` — an issuer top-10 covering 15% of a world tracker leaves 85%
unaccounted for, and that is the honest reading. But `unknown` is nothing
the optimiser can allocate against, so a fund described that way
contributes almost nothing to a design.

Where no source can close the gap — no factsheet, no issuer file, nothing
to upload — tick **coverage complete (+N%)** beside that card's source
buttons. The unknown slice is dropped and what *was* identified is scaled
up to account for the whole fund, on the assumption that the part you
cannot see looks like the part you can.

- It is **saved with the fund**, per facet, and used everywhere its facet
  data is: the portfolio X-ray, the target deviations, and the optimiser.
  This is the point of it — a display-only version would leave the
  optimiser exactly as stuck as before.
- The coverage badge then reads **`100% ASSUMED`**, with the real figure
  in its tooltip. The numbers downstream treat the assertion as fact; the
  badge is how you tell it from a measurement.
- `n/a` is left alone. It means the question does not apply — a cash
  sleeve has no sector — so it is not a gap to be closed, and the
  identified part is scaled to fill only what `unknown` held.
- A card with nothing identified at all does not offer the checkbox.
  There is no shape to spread the gap over, and inventing one is the one
  thing this must not do.
- **A derived card inherits the assertion.** Ticking it on the *country*
  card completes the currency card that is derived from it too — the
  country split IS the currency split there, so two separate ticks could
  only ever contradict each other. The currency card then shows
  `coverage complete (from country)`, ticked and greyed, with the control
  that withdraws it left on the country card. Ticking it on the currency
  card says nothing about country, which is not derived from anything.

Untick to go back to reporting only what is actually there.

### The upload itself

Click **▲ upload CSV** beside any card's source buttons. The file needs
three columns, header row required, any order:

```csv
facet,key,weight
country,Japan,6.4
country,United States,68.1
sector,Technology,24.0
```

It may cover any subset of asset class, sector, country and currency.
Weights may be percent (0–100) or fraction (0–1) — the parser detects
which, per facet. Duplicate `(facet, key)` pairs are summed.

The dialog runs in up to three stages:

1. **Source** — URL, path, Browse or drop, exactly like the holdings
   upload. Then **Preview**.
2. **Resolution** — appears only if something did not canonicalise.
   Unrecognised values in the *facet* column get a dropdown of the four
   canonical facets; unrecognised *keys* get a dropdown of that facet's
   canonical values. You must resolve every item before **Commit**
   enables. There is no skip: a row that cannot be placed should be fixed
   or removed in the file.
3. **Adoption** — if the file covered facets beyond the card you started
   from, PorxPy asks whether those cards should display the uploaded data
   too. The data is stored either way; this only chooses which cards show
   it, and you can switch any card over later.

Afterwards the card's **Upload** option becomes selectable, and the
button beside it changes to **↻ replace upload** with an **✕** to remove.

Each card also carries a **coverage badge** in its title — the share of
the fund the displayed breakdown actually accounts for, with `unknown`
counting against it and `n/a` not.

---

## 13. The Edit fund dialog

This is where you fill in what nobody supplied, and where you choose
*which source to believe* for each field individually. It is the single
most useful screen for a badly covered fund.

### Three ways to open it

- Click **Edit** in the header of any group tile on the fund page
  (Structure, Operational or Trading — Identification has no Edit button
  because it has nothing editable).
- **Double-click** a row in the Pre-Loaded list. This works even for rows
  with no ISIN on record.
- **Double-click** a fund row on the Portfolio → Funds tab.

### What a row looks like

The dialog lists the same four groups as the tiles, and every field gets
four things: its **label**, its **current value**, a **source dropdown**,
and — when the source is your own value — an input for it. Beneath the
row, a caption states where the value actually came from.

Read the two carefully: the dropdown shows the *pin* (which source to
ask), the caption shows the *provenance* (where the displayed value came
from). They differ whenever nothing is pinned — a focus type worked out
from the fund's name is not a Yahoo value, and the caption says
`inferred from name`.

### The sources

| Source | Answers |
|---|---|
| **Yahoo** | The default for nearly everything. Prices, TER, size, yields, classification. |
| **justETF** | ETF structure — replication method and management style, by ISIN. Best-effort, European funds mostly, scraped from a profile page. |
| **Factsheet** | Whatever the extraction read off the uploaded document. This is how you take a staged factsheet value. |
| **My own value** | You type it. A field with a closed vocabulary gets a dropdown rather than a text box, so a typo cannot get through. |

### How it behaves

1. Changing a source **fetches from it immediately**, in preview. The row
   shows `asking justETF…` and then the answer. This is deliberate: the
   reason to pick a source is to find out whether it has anything, and a
   batch that only resolved on Save would hide exactly that.
2. If the source has nothing, the row reads `unknown` with the note
   `that source has no value for this field`. That is a real answer about
   that source, not a failure — and it is why the pin is recorded rather
   than inferred from whether a value arrived.
3. Nothing is stored until you press **Save**. A counter at the bottom
   reads `3 changes not saved — Cancel discards them`.
4. **Save** commits the pins. Values are sent as already fetched rather
   than re-fetched, so you get exactly what you approved.

> **A pin survives everything.** The pin means "this field is bound to
> this source", not merely "this value came from there". It survives a
> reload and *drives* the refetch: **↻ Reload Fund Data** re-asks each
> field's own pinned source. Fields set to your own value are never
> overwritten. And because overrides are a view applied on read, clearing
> one restores the fetched value with no round-trip.

### Two fields worth special attention

**Focus** and **Focus detail** are coupled, and together with the primary
asset class they determine the fund's peer group. Set **Focus** first —
`none`, `geography`, `sector` or `thematic` — because it decides what the
detail dropdown offers:

- **geography** → every node of the country tree, level-tagged, so
  `Country — japan` and `Region — japan` are distinguishable answers.
- **sector** → every node of the sector tree, likewise level-tagged. A
  semiconductor fund is not a technology fund.
- **thematic** → free text, because nothing can enumerate "Artificial
  Intelligence" in advance.
- **none** → not applicable.

### Why Identification is read-only

ISIN, ticker, name, exchange and trading currency cannot be re-sourced
here. The ISIN is the key every fund-level record hangs on — holdings,
breakdowns, overrides, factsheet — so re-sourcing it would orphan all of
them. Correcting an ISIN is a separate, deliberate act.

---

## 14. The Edit holding dialog

For fixing a single row of a fund's holdings — a cash line with no asset
class, a bond with the wrong country, a position whose sector the issuer
left blank.

### Opening it

**Double-click any row** in the holdings table at the bottom of the fund
page. Every row carries the tooltip `Double-click to edit`. There is no
button; the row is the control.

### The fields

| Field | Notes |
|---|---|
| **Name** | The security's name, as it should read in the table. |
| **Ticker · ISIN · CUSIP** | The three identifiers, on one row. Filling any of them improves the odds the row resolves on a future enrich. |
| **Asset** | One picker listing **every node at every level** of the asset tree, grouped by level. You name a node; the backend resolves it and fills the rest of the tree in. |
| **Sector** | The Morningstar tree, same shape. |
| **Country · Currency** | Country tree and ISO-4217 list. |
| **Weight %** | The position's share of the fund. Required, and must be a number. |
| **Bond metadata** | Duration (years), Coupon % (per year), Quality (the credit rating, free text — "BBB-", "Baa3"), and Maturity and Effective date, both as `DD/mmm/YYYY`. Usually blank for equity holdings. |

Under each facet picker a hint reads `stated at sub class level` or
similar. That is the point of the single picker: choosing *equity* is a
coarser claim than choosing *regular stock*, and the hint makes the
difference visible rather than something you have to infer from a group
heading.

All four facet fields are strict dropdowns built from the definitions
files — there is no free-text path, because an unvalidatable value in a
facet column is exactly what the resource files exist to prevent.

> **Editing a Yahoo list stops it being refreshed.** Editing any row of
> Yahoo's list marks it as one you have curated, and a fund reload then
> leaves it alone — which is what makes the edit survive. Before v0.77.0
> the same protection was expressed by relabelling the list a manual
> upload; now that a fund can hold a real upload at the same time, the
> rows stay under Issuer (Yahoo) and carry the flag instead. The tile
> says so under the table.

---

## 15. The tiles and every element in them

Once a fund is loaded, four group tiles sit between the header and the
price chart. The grouping is by *how often the data changes*, which is
also how its age limit is set.

### Tile anatomy

Each tile header carries three things: the group name, an **age pill**,
and an **Edit** button (absent on Identification, which has nothing to
edit). Each row inside is three columns: **label · value · source**.

- A value of `—` means there is no value.
- A field with a value *always* says where it came from, including the
  ordinary case of Yahoo. That repeats a word down the column
  deliberately: the alternative was silence on the common case, which
  made Yahoo the one source you could not see.
- A field with **no** value says nothing in the source column. There is
  no provenance for an absence — including for a field pinned to a source
  that came back empty.

Source captions you will see: `Yahoo`, `OpenFIGI`, `justETF`,
`Factsheet`, `My own value`, `inferred from name`, `calculated`.

### Identification — never expires

How the fund is identified. Entirely read-only.

| Element | Meaning |
|---|---|
| **ISIN** | The fund's identifier, and the key its holdings, breakdowns, overrides and factsheet are stored under. |
| **Ticker** | The Yahoo symbol of this listing, suffix included. |
| **Name** | The fund's long name. |
| **Exchange** | The exchange this listing trades on. |
| **Trading currency** | The currency this listing is priced in — not the fund's base currency. |

### Structure — 365 days

What kind of fund this is. Rarely changes, so these are the fields most
worth pinning to a source you trust — and the ones the optimiser and peer
grouping read.

| Element | Meaning | Values |
|---|---|---|
| **Primary asset class** | What the fund *is*, as a single label. Not derived from its holdings. Feeds the peer group. | from `Primary_asset_class_definitions.csv` |
| **Fund structure** | The wrapper type. | `etf` · `fund` · `unknown` |
| **Replication** | How an index-tracking ETF holds its index. Yahoo publishes nothing here, so it is user-set or from justETF. | `full` · `sampled` · `synthetic` · `n/a` · `unknown` |
| **Management style** | Active stock-picking vs passive index-tracking. Seeded from the quote type, but active ETFs and index funds both exist. | `active` · `passive` · `unknown` |
| **Distribution** | Whether income is reinvested or paid out. Detected from the fund name plus dividend yield. | `accumulating` · `distributing` · `unknown` |
| **Market cap** | Size bucket for the equity sleeve. `mixed` is an intention (a total-market tracker); `unknown` is a gap; `n/a` is cash. | `large` · `mid` · `small` · `mixed` · `unknown` · `n/a` |
| **Equity style** | The growth/value axis. Named `style_box` internally to avoid colliding with management style. | `growth` · `blend` · `value` · `unknown` |
| **Focus** | What the fund is built to concentrate on. Half of the peer key. | `none` · `geography` · `sector` · `thematic` |
| **Focus detail** | The specific target, validated against the vocabulary the focus type implies. | depends on Focus |

### Operational — 90 days

Cost, size and activity. Restated a few times a year.

| Element | Meaning |
|---|---|
| **Trailing yield** | Income over the past year as a percentage. Either Yahoo's figure or one computed from actual dividend history. |
| **Forward yield** | The published forward yield, with a five-year average as a fallback. |
| **Expense ratio (TER)** | Annual cost as a percentage. The heaviest component of the default score. |
| **Turnover** | Portfolio turnover, percent per year. |
| **Total assets** | Fund size in the trading currency. Tested against a floor, not ranked. |
| **Number of holdings** | What the *fund* says it holds — "1,442 positions". Not how many rows you have loaded, which may be a top-10. |
| **Data points** | *Calculated.* How many daily price bars are cached for this listing. |
| **Score (all / peer)** | *Calculated.* Two ranks, 0–100: against the whole saved universe, then within this fund's peer group. The source column on this row is replaced by the **Peers popover** — click it to list the group and jump to any member. |

### Trading — 1 day

Prices and volume. Should normally stay on Yahoo — a price pinned to a
document never updates again, and stale prices feed valuations and the
optimiser.

| Element | Meaning |
|---|---|
| **Last close** | Previous close in the trading currency. |
| **NAV** | Net asset value per share, where the issuer publishes it. |
| **Volume (last day)** | Shares traded on the most recent bar. |
| **52w high · 52w low** | The year's range. Yahoo or your own value only. |
| **YTD return** | *Calculated.* From the first bar of the current calendar year. |
| **Total return** | *Calculated.* Measured from the **first bar of the cached price series**, not from fund inception — so the window differs per fund depending on when you first loaded it. |

> **The age pills are live.** When a fund page loads, any field past its
> group's age limit is re-asked from its pinned source automatically, and
> the status line says so. The **↻ Reload Fund Data** button is a
> different instruction — "refetch now, whatever the ages say". The limits
> themselves are adjustable in Settings.

---

## 16. How funds are grouped into peers

Funds compete for a slot in a portfolio, and the slot is defined by what
exposure it supplies. Two world equity trackers compete; a world tracker
and a European bond fund do not. So the peer key is built from exactly
two things:

```
peer_key = primary_asset_class | focus_type | focus_detail
```

Both come from the Structure tile. That is the practical consequence
worth internalising: **if a fund is in the wrong peer group, the fix is in
the Edit fund dialog** — set its primary asset class, then its focus type
and focus detail. Nothing else influences grouping.

A fund with `focus: none` is grouped on asset class alone.

### Why a group can be too small

A peer group of **one** produces no peer score at all: there is no
ordering inside it, and any number would be invented. Its peer score is
`—`, and the optimiser leaves the fund alone on score grounds — it stays
fully eligible on targets, which is why it is held anyway.

A group of **two** is ranked. It was not until v0.96.0, and the reason it
was not is worth knowing, because it also changed every other score:
percentiles used to map the worst fund to exactly 0 and the best to
exactly 100, which in a pair is absurd on its face — one fund perfect and
the other worthless on the strength of whatever separates them, however
little that is. Percentiles are now the plotting position
`rank / (n + 1)`, so a pair lands on 33 and 67, three funds on 25/50/75,
and a fifty-fund universe on 2 through 98. At universe scale the two
formulas differ by under two points, so nothing was lost where the old
one was defensible.

### The two scores

| Score | Answers | Read by |
|---|---|---|
| **Overall** | "Is this a good fund?" — ranked against every saved fund. | The fund list and the fund page. |
| **Peer** | "Is this the best fund *for this job*?" — ranked within the peer group. | The optimiser's alternatives table, exclusively. |

### What goes into a score

Three components, all expressed as percentiles so a cost in fractions of
a percent and a size in billions can be combined without inventing an
exchange rate between them:

- **TER** — percentile across the universe, inverted (cheap is good).
- **Fund size** — a *floor test*, not a percentile. 100 if the fund clears
  roughly half a billion in base currency, 0 if it does not. Past "big
  enough not to be at risk of closure", more is not better.
- **Trailing returns** — a weighted blend over 1m/3m/6m/1y/3y/5y windows,
  each percentiled among the funds that have it. A fund with three years
  of history is scored on the windows it can support rather than marked
  down for its age.

Three weight presets ship in Settings: **cost driven** (the default — 70%
TER, 30% size, no returns), **cost and returns**, and **returns driven**.
Cost is the default deliberately: ranking on trailing returns is
performance chasing, and cost is the one component that reliably
persists. Each score travels with its coverage, so a high score computed
from one component out of three is visibly thin.

### Where you see the group

- The **Peers** column in the Pre-Loaded list — a popover listing every
  member with its peer score. Click one to bring it into view.
- The **Score** row of the Operational tile — the same popover, in the
  source slot.
- The **price chart** — peer toggles below it overlay each peer's price
  series, indexed to 100 at the left edge of the window, since two funds
  priced at 12 and 480 tell you nothing side by side.

---

## 17. The Pre-Loaded Funds / ETFs tab

Every fund saved on disk, one row per **listing**. Two currency variants
of one ETF are two tickers, so two rows, sharing one set of fund-level
data. The badge on the sub-tab is the count.

### The columns

| Column | Shows | Sort | Filter |
|---|---|---|---|
| **Ticker** | The Yahoo symbol of this listing. | yes | search |
| **ISIN** | The fund identifier, or `—` if none is on record. | yes | search |
| **Cur** | Trading currency of this listing. | yes | — |
| **Name** | The fund's long name. | yes | search |
| **Holdings** | A pill: `FULL · n` for an upload, `TOP · n · ENRICHED`, `TOP · n`, or `—`. Hover for the coverage percentage. | yes | FULL / ENRICHED / TOP |
| **Quality** | Five dots: how completely *this fund* is described — not a bond's credit rating, which is the Rating column in a holdings table. Hover for what is missing. | yes | — |
| **score** | Two figures — universe rank, then peer rank. `—` on the right means the fund is alone in its peer group, so there is nothing to rank it against. | yes | weight preset |
| **Peers** | Popover listing the funds this one is ranked against, itself included. | — | — |
| **Portfolios** | Popover listing which portfolios hold this listing. | — | by portfolio |
| **incl** | Checkbox: may the optimiser buy or sell this fund. Disabled for rows with no ISIN, since the flag is stored per fund. | yes | Incl / Excl |
| **✕** | Deletes the cache file for this listing. Confirms if it is in a portfolio. | — | — |

Above the table: a search box covering ticker, ISIN and name; a live
"showing n of N" counter; and a **× Clear filters** button that appears
once any filter is set. The filter row sits under the headers. The score
column's filter is really a selector — it chooses which weight preset the
scores are computed with, and the whole column recomputes.

### Interacting with a row

- **Single click** — loads the fund into the Load New Fund / ETF view.
- **Double click** — opens the [Edit fund dialog](#13-the-edit-fund-dialog).

> **Rows with no ISIN.** A fund that was fetched but never given an ISIN
> cannot be re-fetched from this list — the row is dimmed and single-click
> does nothing. Double-click still opens the edit dialog, since that only
> needs a ticker. The **incl** checkbox is disabled for the same reason:
> the flag is stored per fund, by ISIN.

Note that **✕** deletes the cached data, not a portfolio holding. If the
fund is in a portfolio it stays there and will be refetched next time
that portfolio loads.

---

## 18. A complete import, end to end

The order below is the one that wastes the least effort — cheap automatic
steps first, hand-curation last, and the resource files fixed once rather
than per fund. It is the per-fund work; importing the shipped bundle in
bulk is [GETTING_STARTED.md](GETTING_STARTED.md) §4.

1. **Fetch it.** ISIN plus exchange, or ticker with suffix. Resolve the
   currency picker or supply the ISIN key if asked.
2. **Save it.** **★ Save to pre-loaded**, or add it to a portfolio, which
   saves it implicitly.
3. **Look at the tiles.** Count the dashes. That is your work list.
4. **Upload the factsheet** if Yahoo left much blank, with its own
   printed date. Run the extraction if you have the AI helper on, and
   read the report with its quotes.
5. **Fill the Structure group** in the Edit fund dialog. Primary asset
   class, focus type and focus detail first — those three decide the peer
   group and therefore every score you will look at afterwards. Set each
   field's source to Factsheet, justETF, or your own value as
   appropriate.
6. **Get holdings in.** Try **↻ Enrich through Yahoo** first — tick the
   header box to select every row, and it is two clicks and free. If the
   fund deserves a real X-ray, upload the issuer's full list.
7. **Or upload facet CSVs** if you have published percentages but no
   holdings, and adopt them on every card the file covers.
8. **Point each breakdown card at its best source.** Holdings for a fund
   with a full list; Issuer (factsheet) where the extraction was good;
   Issuer (Yahoo) otherwise. On the currency card, **From country** where
   the country card is good and no currency split exists anywhere.
9. **Clear the amber banner.** Open **Resolve unmatched values**, work the
   *By value* tab from the top — the heaviest values first — and use
   *By row* for blanks and one-off corrections.
10. **Fix stragglers by hand.** Double-click individual holdings rows for
    anything the vocabulary cannot express.
11. **Check the score and the peer list.** If the peer group looks wrong,
    the answer is in step 5, not here.
12. **Export a fund bundle** from **Settings → Backup & restore** once you
    have a set worth keeping. Building a hundred-odd curated funds is days
    of work; the bundle is a zip with a readable manifest.

> **One habit pays for itself.** When a value fails to resolve, fix it in
> the **resource file** rather than on the row. A row fix corrects one
> holding in one fund. An alias in `matches` corrects that spelling
> everywhere it has ever appeared and everywhere it will appear —
> including in funds you have not imported yet.

---

## Known open issues

None recorded for this document. Defects in the facet trees belong in
`FACET_TREE.md` §17 and defects in the solver in `OPTIMIZER.md` §13;
deferred enhancement ideas belong in `WISHLIST.md`.
