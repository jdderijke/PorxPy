# PorxPy

**Portfolio X-ray Python** — a self-hosted tool for analysing the
true exposure of an investment portfolio across funds, ETFs, sectors,
countries, currencies, and asset classes.

PorxPy runs locally on your own machine. Your portfolio data never
leaves it. The only external traffic is read-only lookups against
public market-data sources (Yahoo Finance, OpenFIGI, optionally
justETF).

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

### Compare against your own targets

You set target allocations per facet (e.g. "40% North America, 25%
Europe, 20% Asia, 15% emerging markets"). PorxPy compares your
portfolio against them and shows signed deviation bars — green for
overweight, red for underweight — so you can see at a glance where
you're off your plan.

### Manage funds and portfolios

- Add funds by ticker or ISIN. PorxPy fetches profile, price history,
  and holdings from Yahoo Finance.
- Group funds into one or more portfolios, with shares (or units) held
  per fund.
- Upload full holdings CSVs or Excel files when the fund issuer
  publishes them (top-10 from Yahoo is often not enough for a real
  X-ray).
- Override anything that's wrong at the source (asset class,
  replication method, breakdown source) and the override sticks across
  every portfolio that holds the fund.

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
  config.py           Paths, constants, TTLs, exchange code maps
  extractors.py       Yahoo Finance fetching and per-holding enrichment
  resolver.py         Ticker variant generation and resolution chain
  breakdowns.py       Holdings roll-up → per-facet weighted breakdown
  targets.py          Target-vs-actual deviation computation
  upload.py           Holdings file parsing, column mapping, enrichment
  utils.py            Cache I/O, portfolio data, coercion helpers
  resources.py        Reference data loading (countries, currencies, ...)
fund_explorer.html    Single-file frontend (HTML + JS, no framework)
resources/            Reference CSVs (shipped with the project)
```

### Data layout

```
portfolios.json       Your portfolios (name, funds, shares, targets)
settings.json         App-level settings
overrides.json        Per-fund overrides, keyed by ISIN
isin_map.json         Cached ISIN → ticker resolutions (from OpenFIGI)
cache/
  listings/<ticker>.json    Per-listing data (price history, profile)
  funds/<isin>.json         Per-fund data (holdings, breakdowns, sectors)
  _symbol_info.json         Shared per-symbol info cache (HQ country, etc.)
  _symbol_aliases.json      Resolved ticker alias cache
  FX_*.json                 FX rate caches
uploads/              Server-side scratch for in-progress holdings uploads
```

The cache distinguishes between **listing-level** data (ticker-keyed —
price, profile) and **fund-level** data (ISIN-keyed — holdings,
sectors, breakdowns). Two different listings of the same fund (e.g.
the GBp and USD share classes of one ETF) share one fund-level cache
entry, so a holdings upload made against either listing is immediately
visible from both.

The user-data files at the project root (portfolios, settings,
overrides) live outside `cache/` on purpose: cache is "stuff we can
lose without losing user state", and is purgeable. User intent is not.

### Request flow

A typical fund page render looks like:

1. The browser opens `/`, which serves `fund_explorer.html` as static
   content.
2. JavaScript on the page calls `/api/fund?ticker=...`.
3. Flask asks the cache layer for the fund's data; on miss or expiry,
   `extractors.load_fund_data` fetches from Yahoo, applies overrides,
   and writes back to the cache.
4. The JSON response includes profile, price history, the four
   breakdown cards (each from its configured source), and the merged
   holdings list.

A portfolio X-ray goes through `/api/portfolios/<pid>/view` and is
much the same, but adds a final aggregation pass in `breakdowns.py`
that weighs every fund's facet breakdown by its allocation in the
portfolio.

### External services

| Service        | Purpose                                              | When called                          |
|----------------|------------------------------------------------------|--------------------------------------|
| Yahoo Finance  | Fund profile, price history, holdings, FX, search    | Always (the main data source)        |
| OpenFIGI       | ISIN → ticker resolution                             | When adding a fund by ISIN           |
| justETF        | ETF structure (replication, style) — best effort     | Optional, ETFs only, user-confirmed  |

All responses are cached locally with TTLs that reflect how often the
underlying data actually changes (price: 1 day, sectors: 7 days,
profile: 30 days, asset class: 90 days, ISIN→ticker: 30 days).

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
- `requests` — HTTP for OpenFIGI / justETF
- `pandas` — CSV / Excel parsing in the holdings upload flow
- `openpyxl` — Excel file reading

---

## Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/jdderijke/PorxPy.git
cd PorxPy
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

That's it. There's nothing else to configure.

---

## Running

From the project root:

```bash
python main.py
```

You should see a startup banner:

```
=======================================================
  PorxPy  v0.20.5  (built 2026-05-29)
  Portfolio X-ray Python
=======================================================
```

Then open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your
browser.

The first time you add a fund, PorxPy will fetch its data from Yahoo
(takes a few seconds). Subsequent loads of the same fund are instant
from cache.

### Stopping

Press `Ctrl+C` in the terminal.

### Resetting

If you want a clean slate, delete `cache/` to wipe all fetched data
(your portfolios and settings stay). Or use Settings → Danger zone in
the web UI, which does the same and offers selective wipes (e.g. just
the price cache, or everything including portfolios).

---

## Privacy

PorxPy is single-user and self-hosted. Your portfolio data never
leaves your machine. The only outbound traffic is:

- Yahoo Finance — symbol lookups, price data, holdings, FX rates,
  search.
- OpenFIGI — ISIN-to-ticker resolution (public, unauthenticated API).
- justETF — only when you explicitly trigger the structure lookup, and
  only for ETFs by ISIN.

There are no telemetry, analytics, or update checks.

---

## Version

Current release: **0.20.5** (2026-05-29)

See [CHANGELOG.md](CHANGELOG.md) for the full version history.
