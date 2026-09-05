# GETTING_STARTED.md — install PorxPy and design your first portfolio

*Current as of v0.103.1. Check the stamp against `porxpy/__init__.py`
before trusting a claim.*

Everything between a fresh clone and a designed portfolio: install the
app, load the pre-loaded fund set that ships with the repository, create
a portfolio, give it cash, say what exposure you want, and let the
optimiser propose the trades that get you there.

You need no market data of your own and no existing holdings. The
pre-loaded set is a curated fund database — around sixty funds with
holdings, factsheets, corrected structure fields and per-facet source
pins — so the optimiser has something real to design with from the first
minute. Building that set yourself is days of work; this guide starts
you past it.

Sections 1–3 get the app running. Sections 4–5 load the fund set and
create a portfolio. Sections 6–9 are the design loop itself: cash,
targets, optimiser, apply. Sections 10–13 are what to look at
afterwards, how to keep your work, and what to do when something looks
wrong.

---

## Contents

**Install and run**

1. [What you need](#1-what-you-need)
2. [Install](#2-install)
3. [Start the app](#3-start-the-app)

**Load the starting data**

4. [Import the pre-loaded fund set](#4-import-the-pre-loaded-fund-set)
5. [Create your first portfolio](#5-create-your-first-portfolio)

**Design a portfolio from scratch**

6. [Give the portfolio some cash](#6-give-the-portfolio-some-cash)
7. [Set your targets](#7-set-your-targets)
8. [Run the optimizer](#8-run-the-optimizer)
9. [Apply the trades](#9-apply-the-trades)

**Afterwards**

10. [Read what you now own](#10-read-what-you-now-own)
11. [Keep your work](#11-keep-your-work)
12. [If something looks wrong](#12-if-something-looks-wrong)
13. [Where to go next](#13-where-to-go-next)

---

## 1. What you need

- **Python 3.12 or newer.**
- A modern browser — Chrome, Firefox, Safari or Edge from the last few
  years.
- Internet access for the first fetch of anything new. The pre-loaded
  set is already fetched, so most of this guide works offline; the
  optimiser needs FX rates if your funds and your base currency differ.

Dependencies are installed from `requirements.txt` in the next step:
Flask and Flask-Cors for the server, `yfinance` and `requests` for the
market-data lookups, `pandas` and `openpyxl` for reading holdings files.

There is no database to install and nothing to configure. All state is
JSON files in the project directory.

---

## 2. Install

Clone the repository, create a virtual environment, install the
dependencies:

```bash
git clone https://github.com/jdderijke/PorxPy.git
cd PorxPy
```

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

That is the whole installation. There is nothing else to set up.

### The one optional extra

The AI factsheet helper — letting Claude read an issuer factsheet and
stage the fields, breakdowns and positions it finds — is off by default
and needs two things: an Anthropic API key, and the toggle switched on
in **Settings**.

Both live in the same place. Go to **Settings → AI helper**, tick
*Read factsheets with Claude*, paste your key into **Anthropic API
key**, and press Save. It takes effect immediately — no restart. The
key is stored in `settings.json` on this machine; that file is
gitignored and never committed, but it is plain text, so treat the
machine as you would treat the key. Once saved it is never shown again:
the field stays blank, and the line above it reads *Key stored
(sk-ant…WXYZ)* so you can tell which key is in place. **Forget key**
erases it.

An `ANTHROPIC_API_KEY` environment variable still works and is used
when no key is stored in Settings, which is the way to run the app
headless. A key entered in Settings takes precedence over it.

You do not need it for anything in this guide.

---

## 3. Start the app

From the project root, with the virtual environment active:

```bash
python main.py
```

You should see the startup banner, which is also how you confirm which
build you are about to use:

```
=======================================================
  PorxPy  v0.103.1 (built 2026-09-05)
  Portfolio X-ray Python
=======================================================
```

Then open <http://127.0.0.1:5000> in your browser. The same version
appears in the pill beside the PorxPy logo at the top left, so you can
check that the page and the server agree.

The server listens on `0.0.0.0:5000` with Flask's debug reloader on, so
saving a Python file restarts it automatically. Stop it with `Ctrl+C`.

The page is one screen with a fixed header and four tabs:

| Tab | What it is for |
|---|---|
| **Explore Funds/ETFs** | Importing funds, and everything you do to one fund. Two sub-tabs: *Load New Fund / ETF* and *Pre-Loaded Funds / ETFs*. |
| **Portfolio** | What you hold, how it has behaved, where it is exposed, what you want, and how to get there. |
| **Tools** | Resolving unmatched values, reloading the resource files, and other maintenance. |
| **Settings** | Backup & restore, enrichment behaviour, the AI toggle, and the danger zone. |

The **Portfolio** selector in the header chooses the active portfolio.
**+ New** creates one — that is section 5.

---

## 4. Import the pre-loaded fund set

The repository ships a fund bundle at
**`PreLoadedFunds/porxpy_funds.zip`**. It is a plain zip with a readable
`manifest.json`, and it contains what a fund *is* rather than what
anyone holds: cached fund and listing data, holdings lists, issuer
factsheets, corrected fields and the reference CSVs those funds were
classified against. It carries no portfolios, no positions and no cash —
those live in the other bundle type, and the two are deliberately kept
separate so you can take someone else's fund research without taking
their holdings.

To load it:

1. Go to **Settings → backup & restore**.
2. Under **Import a bundle**, click **Choose file…** and pick
   `PreLoadedFunds/porxpy_funds.zip`.
3. PorxPy reads the bundle and opens the import dialog. **Nothing has
   been written yet.** The summary line tells you how many funds the
   bundle holds and how many of them you already have; on a fresh
   install that second number is zero and every row reads *new — will be
   imported*.
4. Leave the **Resource files** action on **merge aliases, keep my key
   fields**. Merging unions the alias columns — the spellings that let a
   raw value from an issuer file resolve to a canonical node — and keeps
   your own descriptions and parents. Skip and overwrite each throw away
   one side's aliases, which is rarely what anyone wants.
5. Press **Import**. A progress bar runs while the funds, factsheets and
   resource rows are written.

When it finishes, the message under the panel reports what happened:
funds imported, factsheets written, and which resource files gained
rows.

### Check it landed

Go to **Explore Funds/ETFs → Pre-Loaded Funds / ETFs**. You should see
the imported funds listed, with a count in the sub-tab badge. Click any
row to open that fund's page — profile, price history, the four
breakdown cards (asset class, sector, country, currency) and its
holdings.

This set is now your **candidate universe**: the optimiser designs from
exactly these funds. You can add more at any time from *Load New Fund /
ETF* — see [IMPORT_NEW_FUNDS_GUIDE.md](IMPORT_NEW_FUNDS_GUIDE.md) for
that whole subject.

> **Re-importing later.** If you import the same bundle again after
> editing funds yourself, the dialog's conflict column states the cost
> of overwriting inline — *"replaces 3 of your edits"* — per fund, at
> the moment of choosing rather than afterwards. The default for a
> conflicting fund is to keep yours.

---

## 5. Create your first portfolio

In the header, press **+ New** beside the Portfolio selector.

| Field | What to put |
|---|---|
| **Name** | Anything — "Long-term core". |
| **Base currency** | The currency everything is reported and optimised in. Fund prices in other currencies are converted at the current FX rate. |
| **Cache configuration** | Leave the defaults. It controls which categories are stored on disk and how long before they are considered stale; the shipped values are sensible, and you can change them later from **⚙ Settings** on the portfolio. |

Press **Save**. The portfolio becomes active and the Portfolio tab opens
on its sub-tabs, in three groups:

| Group | Sub-tabs | Question it answers |
|---|---|---|
| What you hold | **Funds** · **Holdings** · **Cash** | What is in the portfolio, and one level deeper, what those funds hold. |
| How it behaves | **History** · **X-ray** | How it has performed, and where the look-through exposure actually sits. |
| What you want | **Targets** · **Optimizer** | The exposure you are aiming for, and the trades that would reach it. |

The portfolio is empty, which is exactly the starting point the
optimiser is built for: a from-scratch design is just a portfolio where
every position happens to be zero and all the money sits in cash.

---

## 6. Give the portfolio some cash

Go to **Portfolio → Cash** and press **+ Add cash position**.

A cash position is money you hold — a bank or broker account, not a
money-market fund. Fill in the row:

| Column | Notes |
|---|---|
| **Name** | "Broker account", "Savings". |
| **Country** | Where the account sits. It feeds the country tile. |
| **Amount** | How much. For a first run, something like 100,000 makes the proposed trades easy to read. |
| **Cur** | The position's currency. Converted to base currency wherever the two differ. |
| **Asset** | One value from the asset tree, at whatever level you know it — the rest of the tree is derived from it. Cash is fine. |
| **Effective date** | The date the balance is true as of. The History chart compounds forward from here. |
| **Interest %** | Optional. Continuous compounding from the effective date, so the cash line in History is not flat. |

Press **Save changes**. Nothing is written until you do; the footer
tells you how many edits are staged.

Cash counts toward the portfolio's asset-class, sector, country and
currency tiles and appears as rows in the Holdings sub-tab, so a
portfolio that is 100% cash already X-rays correctly — it is simply all
one bucket.

The Holdings sub-tab merges the same company across every fund that
holds it, so ASML appears once with the combined value rather than once
per fund. Its **Funds** column says how many of your funds contributed
to that row and opens to name them — the quickest way to find out why
an exposure you did not plan for is as big as it is.

The amount you enter here is the money the optimiser has to work with.
How much of it *stays* in cash is a separate decision, made in the next
section.

---

## 7. Set your targets

Go to **Portfolio → Targets** and press **Set targets**.

Targets are what you want the portfolio to look like. They are the
optimiser's entire brief — without them it has nothing to solve for.

### Cash held by me

At the top of the editor is **Cash held by me**, an *amount* in base
currency rather than a percentage. It is reserved before anything else:
the optimiser leaves exactly that much in your own accounts, selling
positions to raise it if you hold less. Every percentage below is then a
share of what remains — reserve 50,000 of 100,000 and "50% equity" means
25,000.

It is an amount and not a percentage on purpose. The optimiser cannot
buy a bank deposit, so a cash *target* would only ever be satisfied
approximately, and would drag every other target off course while it was
at it. A reservation is exact by construction: the money is simply not
in the budget.

Leave it at 0 for a first run, or set aside a buffer if you want one.

### The facet targets

Below that, one section per facet, in two groups:

| Group | Facets | What it measures |
|---|---|---|
| **Exposure** | Asset class · Sector · Region · Currency | Measured from holdings and aggregated to portfolio level — a genuine look-through. |
| **Style** | Market cap · Equity style | Fund-level classifications: one bucket per fund, not a look-through. Unclassified funds show as "unknown". |

In each section, pick a bucket and type a percentage. The four Exposure
facets use the same tree picker as the rest of the app: open it, expand
as far as you want to commit, and buckets you have already targeted are
not offered again. Market cap and Equity style are fund-level
classifications rather than trees, so they keep a plain list. Three
things are worth knowing:

- **Targets are sparse.** Set only the buckets you care about.
  Everything else is reported as "Untargeted", so you can see what you
  have not accounted for, and the total does not have to reach 100%.
- **Targets are levelled.** A facet is a tree, and you can target at any
  level of it: `technology` at sector level, `semiconductors` at
  sub-sector level, `developed` at super-region level. Every level is
  pickable — that is what the tree is showing you — and what you have
  set is grouped by level underneath. Targeting the same facet at
  several levels at once is worth reading `OPTIMIZER.md` §13 about
  first: the solver weights such a facet more heavily than you asked
  for.
- **Targets nest.** The figure beside each heading is what the section
  *commits*, not the levels added up: semiconductors 15% inside
  technology 35% commits 35%, not 50%. Past 100% the figure turns amber,
  because at that point the section really is over-committed.

A workable first set, if you want one to type in:

| Facet | Target |
|---|---|
| Asset class | equity 70%, fixed income 30% |
| Region | north america 55%, europe 25%, asia 20% |

Press **Save**. The Targets tab now shows one section per facet with
bars reading *actual − target*: positive is overweight, negative
underweight. On an all-cash portfolio everything is underweight, which
is the honest answer.

---

## 8. Run the optimizer

Go to **Portfolio → Optimizer**. It designs a portfolio from your
targets, using your pre-loaded funds and available cash, and expresses
the answer as a trade list. Funds you already hold can be sold, so the
same screen is a rebalance later on.

Nothing is changed until you press Apply.

The controls, top to bottom:

| Control | What it does |
|---|---|
| **Prefer better funds** | Off by default. When set, the optimiser swaps in higher-ranked funds *after* the targets are met, and only where the swap keeps every category inside its tolerance. Ranking is the peer score — funds compared against others of the same asset class and focus. |
| **Max fit error, per target category** | How many percentage points any single bucket may be off, per facet, defaulting to 5. This also *weights* the objective: a facet you demand 2% on is worked five times harder than one you allow 10% on. Spend your precision where it matters. |
| **Max funds** | Cap on how many funds the design may use. Ten is a reasonable start; raise it if the optimiser reports it could not reach the error target within the cap. |
| **Min weight %** | Positions smaller than this are dropped and the problem re-solved, so you do not end up with dust holdings. |
| **Min trade** | Trades worth less than this amount in base currency are suppressed. 0 means no minimum. |

Press **⚙ Propose design**. A progress bar runs while every pre-loaded
fund is read — on a cold cache that takes as long as a portfolio view,
and for the same reason.

Three panels come back:

- **Proposed trades** — one row per fund, with the score, what you hold
  now, what the design wants, the share delta, the amount in base
  currency, and a **Settle from** picker naming the cash position the
  trade draws on.
- **Better-scoring alternatives** — what swapping in a higher-ranked
  peer would cost in accuracy. Reported, never applied: whether the
  trade-off is worth it varies per portfolio, and the tool does not
  guess.
- **Resulting exposure** — what the design actually achieves against
  each target. Large residuals usually mean the targets are not
  reachable with the funds available, not that the solver failed.

If the result disappoints, the fix is usually one of: raise **Max
funds**, loosen the tolerance on the facet you care least about, or add
funds to the universe that can actually reach the bucket you targeted.
The status line names the reason where it can.

---

## 9. Apply the trades

Press **✓ Apply these trades** and confirm.

The batch is applied atomically: every leg is validated and priced
first, and if any one fails, none are applied. A half-applied proposal
would leave the portfolio in a state neither you nor the optimiser could
reason about.

What applying does is a transfer, not an accounting event: each fund's
share count goes up (or down) and the chosen cash position goes down (or
up) by shares × price × FX. Funds the design bought that were not in the
portfolio are added to it automatically — so **Portfolio → Funds** now
lists them, with the share counts the design chose.

There is deliberately no transaction log, cost basis or realised P&L.
PorxPy is a portfolio *design* tool, and the current positions fully
describe the portfolio.

---

## 10. Read what you now own

With positions in place, the rest of the app has something to say:

- **Portfolio → X-ray** — the four breakdown cards for the whole
  portfolio, each fund's facet breakdown weighted by its allocation.
  Move the level selector to read the same data at a finer or coarser
  grain; every level is computed on the server, so switching costs
  nothing.
- **Portfolio → Holdings** — every underlying position across every
  fund, merged, so you can see that three funds each hold the same
  company and what that adds up to.
- **Portfolio → Targets** — the tab you filled in, now showing real
  deviations instead of "everything underweight".
- **Portfolio → History** — the portfolio's price history, with the cash
  line compounding from its effective date.
- A fund's own page, from **Explore → Pre-Loaded Funds / ETFs**, for
  where any single number came from and how fresh it is.

---

## 11. Keep your work

Everything is files in the project directory, in two classes that are
treated very differently:

- **`cache/`** is losable. Delete it and PorxPy re-fetches. The one
  exception is `cache/factsheets/`, which is never expired — nothing
  replaces a document somebody went and found.
- **`portfolios.json`, `settings.json`, `overrides.json` and
  `isin_map.json`** are your state, never auto-purged, and all four are
  gitignored so they never end up in a commit.

**Settings → backup & restore** exports the two bundle types:

| Export | Contains | Use it for |
|---|---|---|
| **Export funds** | The curated asset — holdings, factsheets, corrected fields, per-card source pins and the resource files. | Moving your fund research to another install, or handing it to someone else. This is how `PreLoadedFunds/porxpy_funds.zip` was made. |
| **Export portfolios** | Portfolios, targets, cash positions and settings. | Your own backup. Restores on top of whatever fund set is present. |

Price history is off by default in the funds export, because it
dominates the file size and one refetch replaces it.

### Starting over

**Settings → danger zone** wipes data, selectively or completely — just
the price cache, everything cached, or everything including portfolios.
Deleting the `cache/` directory by hand does the same as the cache-only
option. Export a bundle first if there is curation you do not want to
redo.

---

## 12. If something looks wrong

**The page shows an old version, or a change did not take effect.** The
dev server binds port 5000, and on Windows a second process can bind the
same port without a visible error — so a stray second instance answers
some requests and not others. Check
<http://127.0.0.1:5000/api/meta>, which returns the running `version`
and `build_date`, and make sure only one `python main.py` is running.

**An amber ⚠ banner appears under the header.** Some raw value in your
data did not resolve against the reference CSVs — an issuer's spelling
of a sector, or a country no alias covers. Click it to open **Resolve
unmatched values**, which lists what did not resolve, sorted by weight.
Tell it what a value means once and the alias is written to the resource
file; every occurrence everywhere resolves on the next read, including
data extracted months ago. Nothing is rewritten and no migration is
needed.

**A breakdown card shows a large `unknown` slice.** That is a gap
somebody could close, and it is deliberately distinct from `n/a`, which
means the question does not apply. Usually the fix is a fuller holdings
list — see [IMPORT_NEW_FUNDS_GUIDE.md](IMPORT_NEW_FUNDS_GUIDE.md) — or
pinning that card to
a better source.

**The optimizer says a facet has no data.** A targeted facet that no
candidate can answer is a different failure from unreachable targets. It
usually means that facet's breakdown card is sourced from the issuer and
the issuer publishes nothing for it — country is the usual victim.
Switching that card to the holdings look-through generally fixes it.

**The optimizer refuses to run.** It needs targets (section 7) and cash
or existing positions (section 6). It also drops any candidate with no
usable price, or no FX rate into your base currency, and reports what it
skipped and why.

**The banner mentions legacy files.** A `NOTICE: pre-0.12 files
detected` block means old-layout files are lying around from an earlier
version. Nothing breaks — they are ignored. Clear them from **Settings →
danger zone** when convenient.

---

## 13. Where to go next

| Document | Subject |
|---|---|
| [README.md](README.md) | What PorxPy does, and the fullest architecture write-up. |
| [IMPORT_NEW_FUNDS_GUIDE.md](IMPORT_NEW_FUNDS_GUIDE.md) | Getting a fund from an ISIN to fully described — holdings uploads, factsheets, structure fields, scores. |
| [OPTIMIZER.md](OPTIMIZER.md) | How the solver actually works, and what its outputs mean. |
| [FACET_TREE.md](FACET_TREE.md) | The four facet trees — asset, sector, geography, currency — and how levels behave. |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release, and why. |
| [WISHLIST.md](WISHLIST.md) | Ideas that are deferred rather than promised. |

The natural next step after this guide is to widen the candidate
universe with funds you care about, which is
[IMPORT_NEW_FUNDS_GUIDE.md](IMPORT_NEW_FUNDS_GUIDE.md), and then to
re-run the optimiser:
the same screen that designed the portfolio rebalances it.
