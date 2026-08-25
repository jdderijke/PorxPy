"""
HTTP layer — Flask app and route handlers.

Each route is a thin wrapper that parses query/body, delegates to the
relevant business logic in :mod:`porxpy.extractors`, :mod:`porxpy.upload`,
or :mod:`porxpy.utils`, and returns JSON. Heavy lifting belongs in those
modules; route bodies stay readable.

Routes are grouped (in order):

* App meta — ``/``, ``/api/meta``
* Single-fund explorer — ``/api/fund``
* Portfolios — CRUD, view, price history
* Cache management — list, purge
* Holdings upload — preview, commit, clear
"""

from __future__ import annotations

import json
from datetime import datetime
import math
import re
import uuid
from pathlib import Path

import requests
from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)
from flask.json.provider import DefaultJSONProvider
from flask_cors import CORS

from porxpy import NAME, VERSION, BUILD_DATE
from porxpy.config import (
    FACET_LEVELS,
    FACET_DISPLAY_LEVELS,
    facet_level_field,
    facet_node_field,
    facet_pinned_field,
    facet_raw_field,
    BASE_DIR,
    BREAKDOWN_FACETS,
    UPLOAD_DIR,
    FACTSHEET_EXTENSIONS,
    FACTSHEET_MIME,
    DEFAULT_FUND_STRUCTURE,
    DEFAULT_INCLUDE_IN_OPTIMIZER,
    DEFAULT_SIZE_FLOOR_BASE,
    OVERRIDABLE_FIELDS,
    field_vocab,
    FIELD_SOURCES,
    BREAKDOWN_SOURCES,
    HOLDINGS_SOURCES,
    rollup_label_of,
    CACHE_CATEGORIES,
    CACHE_DIR,
    LISTINGS_DIR,
    FUNDS_DIR,
    LISTING_CATEGORIES,
    FUND_CATEGORIES,
    DEFAULT_CACHE_CONFIG,
    DEFAULT_SETTINGS,
    SYMBOL_INFO_CACHE_NAME,
)
from porxpy.extractors import (
    build_holdings_meta,
    enrich_existing_holdings,
    extract_price_history,
    load_fund_data,
)
from porxpy.resolver import (
    build_ticker,
    resolve_mode1_listings,
    split_yahoo_ticker,
    validate_mode2_ticker,
)
from porxpy.upload import (
    UploadCancelled,
    commit_breakdown_upload,
    get_upload_prefs,
    list_canonical_values,
    mark_cancelled,
    parse_breakdown_csv_preview,
    upload_clear,
    upload_commit,
    upload_preview_from_source,
)
from porxpy.breakdowns import (
    _key_at_level,
    candidate_exposures,
    facet_items,
    resolve_facet_node,
    aggregate_portfolio_holdings,
    build_fund_breakdowns,
    rollup_holdings,
    rollup_portfolio_fundlevel,
)
from porxpy.utils import (
    age_days,
    cache_purge,
    cache_read,
    cache_write,
    coerce_holdings_row,
    delete_portfolio,
    find_portfolio,
    fx_rate,
    fx_history,
    holdings_blobs_in,
    holdings_delete_source,
    holdings_get,
    holdings_put,
    holdings_sources_available,
    holdings_status_from_cache,
    holdings_store_get,
    factsheet_delete,
    factsheet_file,
    factsheet_get,
    factsheet_put,
    coerce_override_value,
    field_pins,
    field_source_set,
    factsheet_set_extraction,
    apply_overrides,
    override_delete,
    override_get,
    override_put,
    overrides_for,
    HOLDINGS_ROW_FIELDS,
    listing_identity_get,
    listing_identity_lookup_isin,
    listing_identity_put,
    load_overrides,
    load_isin_map,
    isin_map_put,
    load_portfolios,
    load_settings,
    normalise_cache_config,
    normalise_currency,
    normalise_facets,
    normalise_fund_structure,
    now_iso,
    portfolio_targets_get,
    portfolio_targets_put,
    price_in_base,
    save_settings,
    uploaded_breakdowns_delete,
    uploaded_breakdowns_get,
    uploaded_breakdowns_put,
    upsert_portfolio,
)
from porxpy.targets import compute_target_deviations

import yfinance as yf  # only for the per-fund retry fallback in price_history


def create_app() -> Flask:
    """Build and return the Flask application.

    Factory pattern so tests can build their own instance with a clean
    state. Wires every route below.

    Returns:
        Configured Flask app (CORS enabled, static served from project root).
    """
    app = Flask(__name__, static_folder=str(BASE_DIR))
    CORS(app)

    @app.before_request
    def _resources_fresh():
        """Re-read the resource CSVs when any has changed on disk.

        Once per request, not per classification: a file saved halfway
        through an upload must not have some rows classified under the
        old ruleset and some under the new. When nothing has changed
        this is a stat() per file and nothing else.

        A resource file used to take effect only on restart, and there
        was no way to tell which ruleset a given row had been classified
        under.
        """
        from porxpy.resources import ensure_resources_fresh
        try:
            changed = ensure_resources_fresh()
            if changed:
                print(f"[Resources] reloaded after on-disk change: "
                      f"{', '.join(changed)}")
        except Exception as exc:      # never fail a request over this
            print(f"[Resources] freshness check failed: {exc}")

    # ---- JSON that the browser will actually parse -----------------------
    # Python's encoder emits bare `NaN`, `Infinity` and `-Infinity` for
    # those float values. They are legal to json.dumps and to json.loads,
    # and they are rejected outright by the browser's JSON.parse — so one
    # NaN anywhere in a response makes the WHOLE response unreadable, with
    # an error naming a line number rather than a field.
    #
    # Yahoo hands us NaN routinely: 0P00015UO7.F carries them in `info`,
    # and extract_profile copies info values through verbatim. That makes
    # this a property of every endpoint, not of the one where it happened
    # to be noticed, so it is fixed once here rather than at each call
    # site — where a single missed spot breaks the response anyway.
    #
    # null is the honest rendering: NaN means "no value", which is exactly
    # what null means to the client.
    class _SafeJSONProvider(DefaultJSONProvider):
        @staticmethod
        def _clean(o, depth: int = 0):
            if depth > 24:
                return "<max depth>"
            if isinstance(o, float):
                return o if math.isfinite(o) else None
            if isinstance(o, dict):
                return {k: _SafeJSONProvider._clean(v, depth + 1)
                        for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [_SafeJSONProvider._clean(v, depth + 1) for v in o]
            return o

        def dumps(self, obj, **kwargs):
            return super().dumps(self._clean(obj), **kwargs)

    app.json = _SafeJSONProvider(app)

    # -----------------------------------------------------------------------
    # App meta
    # -----------------------------------------------------------------------
    @app.route("/")
    def index() -> Response:
        """Serve the single-page frontend."""
        return send_from_directory(str(BASE_DIR), "fund_explorer.html")

    @app.route("/api/meta")
    def api_meta() -> Response:
        """Return program identification and the build date.

        Used by the frontend to render the header banner ("PorxPy · v0.2.0
        · built 2026-04-28") and could be probed by any client wanting
        to confirm what backend it's talking to.
        """
        return jsonify({"name": NAME, "version": VERSION, "build_date": BUILD_DATE})


    # The per-facet card source is one registry field per facet now
    # ("breakdown_source.sector"). build_fund_breakdowns still wants the
    # {facet: source} map, so rebuild it here rather than teaching that
    # pure function about the override store.
    def _bd_sources(isin: str) -> dict:
        out = {}
        for f in BREAKDOWN_FACETS:
            v = override_get(isin, f"breakdown_source.{f}")
            if v:
                out[f] = v
        return out

    # Which facets the user has asserted are fully covered by their
    # chosen source. Read alongside the source pin at every site that
    # builds cards, because the two together are what the card means —
    # separating them would let a rebuild drop the assertion silently.
    def _bd_completed(isin: str) -> dict:
        return {
            f: True for f in BREAKDOWN_FACETS
            if bool(override_get(isin, f"breakdown_complete.{f}"))
        }

    # Which breakdown sources this fund HAS — not which have data for a
    # given facet. A factsheet that never mentions currencies is still a
    # factsheet, and "the factsheet does not say" is a real answer the
    # user is entitled to select. build_fund_breakdowns cannot work this
    # out from its arguments: an empty holdings roll-up looks identical
    # whether the fund has no holdings or holdings that classified to
    # nothing, and a factsheet leaves no trace in them at all.
    def _bd_presence(isin: str, rows: list | None = None) -> dict:
        if rows is None:
            # The active source's rows: "holdings" as a breakdown source
            # means "look through what this fund's holdings tile is
            # showing", so the two selectors cannot disagree about which
            # list that is.
            rows = (holdings_get(isin)[0].get("rows") or [])
        meta = factsheet_get(isin) or {}
        return {
            "holdings":  bool(rows),
            # Uploaded but never extracted means there are no numbers to
            # read off it yet, so the card cannot be pinned to it.
            "factsheet": bool(meta.get("extraction")),
        }

    # Both supplied sources are cleared the same way — remove the items,
    # then drop any card override left pinned to a source that no longer
    # has anything behind it — so they share one implementation rather
    # than the CSV path having a loop of its own and the factsheet path
    # having none, which is how the factsheet's items came to outlive the
    # document they were read from.
    def _clear_supplied_source(isin: str, source: str,
                               facet: str | None = None) -> bool:
        """Remove one supplied source's breakdown items and stale pins.

        Args:
            isin: Fund ISIN.
            source: One of
                :data:`~porxpy.config.SUPPLIED_BREAKDOWN_SOURCES`.
            facet: Clear only this facet; None clears every facet.

        Returns:
            True if anything was removed from the item store.
        """
        removed = uploaded_breakdowns_delete(isin, facet, source=source)
        # A factsheet supplies TWO things — the facet tables and the
        # position list — and both are readings of the same document. A
        # whole-source clear takes both, so replacing or deleting a
        # factsheet cannot leave last year's positions behind under this
        # year's document. A per-facet clear is a narrower request and
        # leaves the holdings alone.
        if source == "factsheet" and facet is None:
            removed = holdings_delete_source(isin, "factsheet") or removed
        supplied_now = uploaded_breakdowns_get(isin)
        for f, src in list(_bd_sources(isin).items()):
            # The store is source-keyed, so a facet's entry is a dict of
            # every supplied source and stays truthy while the OTHER
            # source survives this clear. Ask the source being cleared.
            if src == source and not (supplied_now.get(f) or {}).get(source):
                override_delete(isin, f"breakdown_source.{f}")
        return removed

    @app.route("/api/regions")
    def api_regions() -> Response:
        """Return the country→region reference map for the frontend.

        Drives the Country/Region toggle on the country breakdown cards:
        the frontend regroups a country breakdown (whose keys are
        canonical ``mstar_country`` values) into regional buckets purely
        client-side using this map. Loaded once by the frontend and
        cached.

        Returns:
            ``{"country_to_region": {mstar_country: mstar_region, ...},
            "regions": [<distinct mstar_region>, ...],
            "focus_regions": [{"key","label","kind"}, ...]}``.

        The two region lists are deliberately different things and are
        not interchangeable. ``regions`` is the *measured* taxonomy —
        the distinct values the country breakdown regroups into, derived
        from country_codes.csv. ``focus_regions`` is the *declared*
        vocabulary for a fund's ``focus_detail``, from regions.csv, and
        additionally contains super regions ("europe" spanning developed
        Europe, emerging Europe and the UK). A fund can be built for
        Europe; no holding is ever located in "europe".
        """
        from porxpy.resources import MSTAR_TO_REGION, REGION_ROWS
        return jsonify({
            "country_to_region": MSTAR_TO_REGION,
            "regions":           sorted(set(MSTAR_TO_REGION.values())),
            "focus_regions":     [
                {"key": r["key"], "label": r["label"], "kind": r["kind"]}
                for r in REGION_ROWS
            ],
        })

    # -----------------------------------------------------------------------
    # Holdings-classification / sector / currency reference data
    # -----------------------------------------------------------------------
    # The three resource CSVs added in 0.13.0 (Holdings_class_definitions,
    # sectors, currencies) drive the dropdowns in the edit-holding modal
    # and the value-coercion in the upload pipeline. We expose them via
    # one endpoint each so the frontend can fetch + cache them on first
    # load and rebuild the <datalist>s without round-tripping per modal
    # open.

    @app.route("/api/resources/holdings_classes")
    def api_resources_holdings_classes() -> Response:
        """Return the paired asset_class + sub_class taxonomy.

        Drives the two coupled dropdowns in the edit-holding modal:
        picking an asset class filters the sub-class options to the
        valid ones for that group. The frontend also uses the matches
        list as soft autocomplete hints.

        Response shape::

            {
              "rows": [
                {"asset_class": "...", "asset_class_desc": "...",
                 "sub_class":   "...", "sub_class_desc":   "...",
                 "matches":     [<aliases>]},
                ...
              ],
              "by_asset_class": {<asset_class>: [<sub_class>, ...]}
            }
        """
        from porxpy.resources import list_asset_nodes, ASSET_LEVELS
        return jsonify({
            "levels": list(ASSET_LEVELS),
            "nodes":  list_asset_nodes(),
        })

    @app.route("/api/resources/sectors")
    def api_resources_sectors() -> Response:
        """Return the Morningstar 11-sector taxonomy.

        Drives the Sector dropdown in the edit-holding modal and the
        upload normaliser (``"Information Technology"`` → ``"technology"``).

        ``hierarchy`` carries the level of every canonical name and each
        sub sector's parent, so the breakdown cards can ask "does this
        source's data reach sub-sector level" of any source's items
        rather than assuming which sources can. A factsheet naming
        "Semiconductors" reaches it; the same factsheet naming only
        "Technology" does not, and neither is knowable from the source
        alone.
        """
        from porxpy.resources import (SECTORS_ROWS, SECTOR_LEVEL_OF,
                                      SUB_SECTOR_PARENT)
        return jsonify({
            "rows": SECTORS_ROWS,
            "hierarchy": {
                "level_of":   dict(SECTOR_LEVEL_OF),
                "sub_parent": dict(SUB_SECTOR_PARENT),
            },
        })

    @app.route("/api/resources/currencies")
    def api_resources_currencies() -> Response:
        """Return the ISO 4217 currency master list.

        Drives the Currency dropdown in the edit-holding modal and the
        upload normaliser (``"yen"`` → ``"JPY"``).
        """
        from porxpy.resources import CURRENCY_ROWS
        return jsonify({"rows": CURRENCY_ROWS})

    @app.route("/api/resources/countries")
    def api_resources_countries() -> Response:
        """Return the country alias map + canonical list.

        The map is the same one :func:`porxpy.resources.country_to_mstar`
        uses on the server: any input form (long name, ISO alpha-2,
        ISO alpha-3, numeric code, manual short-form aliases like
        ``"UK"``, ``"South Korea"``) maps to a canonical
        ``mstar_country`` string. The edit-holding modal uses this to
        replace a free-text country entry with its canonical form on
        blur, parity with the sector / sub_class / currency fields.

        Response shape::

            {
              "aliases":   {<lowercased alias>: <mstar_country>, ...},
              "canonical": [<distinct mstar_country>, ...]  # sorted
            }
        """
        from porxpy.resources import COUNTRY_NAME_TO_MSTAR
        canonical = sorted(set(COUNTRY_NAME_TO_MSTAR.values()))
        return jsonify({
            "aliases":   COUNTRY_NAME_TO_MSTAR,
            "canonical": canonical,
        })


    # -----------------------------------------------------------------------
    # App-level settings (settings.json)
    # -----------------------------------------------------------------------
    @app.route("/api/settings", methods=["GET"])
    def api_settings_get() -> Response:
        """Return the current app settings plus the defaults.

        The frontend uses ``defaults`` to populate "Reset to default"
        controls without hardcoding the values. ``settings`` is always
        the normalised, validated form — never the raw on-disk dict.
        """
        return jsonify({
            "settings": load_settings(),
            "defaults": DEFAULT_SETTINGS,
        })

    @app.route("/api/settings", methods=["PUT"])
    def api_settings_put() -> Response:
        """Update app settings (full or partial replacement).

        The body is merged onto the current settings via
        :func:`porxpy.utils.normalise_settings`, then persisted.
        Returns the normalised dict that was actually saved.
        """
        body = request.get_json(force=True, silent=True) or {}
        # Merge: start from current, overlay incoming top-level keys.
        # We don't deep-merge — it's simpler for the frontend to PUT a
        # full snapshot, and we only have one section right now anyway.
        current = load_settings()
        merged  = {**current, **body}
        saved   = save_settings(merged)
        return jsonify({"settings": saved})

    # -----------------------------------------------------------------------
    # Single-fund explorer
    # -----------------------------------------------------------------------
    # An ISIN is 12 chars: 2-letter country code, 9 alphanumerics, 1
    # check digit. Anything not matching this is treated as a ticker.
    _ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

    @app.route("/api/fund")
    def api_fund() -> Response:
        """Return everything we know about one fund — three-mode fetch.

        The fetch flow has two input modes. Each ends with the full
        identity quad {ISIN, ticker, exchange, currency} resolved before
        the fund is loaded — no fund is ever identity-partial.

        Mode 1 — ISIN + exchange. PorxPy asks OpenFIGI for the base
            tickers of that ISIN on that exchange, probes each on Yahoo
            for its currency, and keeps only the Yahoo-confirmed ones.
            If several currencies result, the response is
            ``{"needs_currency": true, "choices": [...]}`` and the
            frontend re-requests with ``currency`` set.
        Mode 2 — a full Yahoo ticker, suffix included (e.g. ``BATG.L``),
            exactly as on the Yahoo Finance website. PorxPy confirms
            Yahoo returns live data for it, then the user supplies an
            ISIN (any unique string — in mode 2 the ISIN is only a
            holdings-cache key, so it is not validated). Until an ISIN
            is given the response is ``{"needs_isin": true}``.

        Query parameters:
            isin: ISIN code (mode 1), or the cache-key ISIN (mode 2).
            ticker: a full Yahoo ticker with exchange suffix (mode 2).
            exchange: MIC — required for mode 1.
            currency: chosen trading currency (mode 1, on the re-request).
            portfolio: optional portfolio id for its cache config.
            refresh: ``1`` to force a live refresh.

        Returns:
            The full per-fund payload plus the resolved identity quad;
            or a ``needs_currency`` / ``needs_isin`` prompt (status 422);
            or an ``error`` (status 422) when resolution truly fails.
        """
        isin     = request.args.get("isin", "").strip().upper()
        ticker_q = request.args.get("ticker", "").strip().upper()
        exchange = request.args.get("exchange", "").strip().upper() or None
        currency = request.args.get("currency", "").strip().upper() or None
        pid      = request.args.get("portfolio", "").strip() or None
        force    = request.args.get("refresh") == "1"
        # v0.21.0 explicit-save: viewing a fund (no commit) only writes
        # to the cache if the fund is already saved. Add &commit=1 to
        # the call when the user has explicitly chosen to save.
        commit   = request.args.get("commit") == "1"

        if not isin and not ticker_q:
            return jsonify({"error": "isin or ticker is required"}), 400

        # If a value was typed into the ISIN field that is really a
        # ticker (has an exchange suffix), treat it as the ticker. Only
        # done when no ticker was given — when a ticker IS present, the
        # isin param is mode 2's cache key and must be left untouched
        # (in mode 2 the key need not even look like a real ISIN).
        if isin and not ticker_q:
            looks_like_ticker = "." in isin and not _ISIN_RE.match(isin)
            if looks_like_ticker:
                ticker_q = isin
                isin = ""

        cache_cfg = DEFAULT_CACHE_CONFIG
        portfolio_name = None
        if pid:
            p = find_portfolio(pid)
            if p:
                cache_cfg = normalise_cache_config(p.get("cache_config"))
                portfolio_name = p.get("name")

        # ── Mode detection + identity resolution ───────────────────────
        # Every branch ends with all four of {isin, ticker, mic,
        # currency} known, or returns a prompt / error. resolved_note
        # explains the resolution for the UI banner.
        #
        # Mode is decided by whether a TICKER was supplied: a full Yahoo
        # ticker means mode 2. An ISIN with no ticker is mode 1.
        resolved_isin = ""
        resolved_tkr  = ""
        resolved_mic  = ""
        resolved_cur  = ""
        resolved_note = ""

        if not ticker_q:
            # ════ Mode 1: ISIN + exchange ══════════════════════════════
            if not isin:
                return jsonify({"error": "isin or ticker is required"}), 400
            if not exchange:
                return jsonify({"error":
                    "An exchange (MIC) is required when fetching by ISIN."}), 422

            listings, note = resolve_mode1_listings(isin, exchange)
            if not listings:
                # Nothing resolved — hard fail. Two cases produce this:
                #   1. OpenFIGI doesn't know this ISIN on this exchange
                #      (typical: Morningstar pseudo-listings — funds
                #      like FGR / UCITS that show on Yahoo Finance as
                #      0P*.F or 0P*.AS but are NOT exchange-listed
                #      instruments OpenFIGI tracks).
                #   2. OpenFIGI knows about base tickers, but none of
                #      them resolved on Yahoo (more unusual).
                # In either case the user can usually still load the
                # fund via mode-2: type the Yahoo ticker (e.g.
                # "0P00015UO7.F") in the search box directly. Surface
                # that hint so the path forward is obvious.
                hint = (" If this is a fund that only appears on "
                        "Yahoo Finance and not on a regular exchange "
                        "feed (e.g. UCITS / FGR funds, Morningstar "
                        "pseudo-tickers starting with 0P), search for "
                        "it on Yahoo Finance and paste the full ticker "
                        "(e.g. 0P00015UO7.F) into the search box "
                        "instead of the ISIN.")
                return jsonify({"error": note + hint, "mode": 1}), 422

            # Distinct Yahoo-confirmed currencies for this exchange.
            by_cur = {}
            for lst in listings:
                by_cur.setdefault(lst["currency"], lst["yf_symbol"])

            if currency:
                # Re-request with a chosen currency — pick that listing.
                chosen = by_cur.get(currency)
                if not chosen:
                    return jsonify({"error":
                        f"No {currency} listing for {isin} on {exchange}. "
                        f"Available: {', '.join(sorted(by_cur))}.",
                        "mode": 1}), 422
                resolved_cur = currency
                resolved_tkr = chosen
            elif len(by_cur) == 1:
                # Exactly one currency — no choice needed.
                resolved_cur, resolved_tkr = next(iter(by_cur.items()))
            else:
                # Several currencies — ask the user to choose, then the
                # frontend re-requests with &currency=.
                return jsonify({
                    "needs_currency": True,
                    "isin":     isin,
                    "exchange": exchange,
                    "choices":  [{"currency": c, "ticker": t}
                                 for c, t in sorted(by_cur.items())],
                    "mode":     1,
                }), 422

            resolved_isin = isin
            resolved_mic  = exchange
            resolved_note = note
            # Where each identity field actually came from. Recorded at
            # resolution time because this is the only place that knows:
            # by the time the fund page renders, an ISIN the user typed
            # and a ticker OpenFIGI found look alike.
            identity_sources = {
                "isin":     "user",
                "ticker":   "openfigi",
                "exchange": "user",
                "currency": "yahoo",
                "longName": "yahoo",
            }

        else:
            # ════ Mode 2: full Yahoo ticker (suffix included) ══════════
            # The ticker must carry an exchange suffix — bare-ticker
            # entry is not supported (a bare ticker is not a unique,
            # Yahoo-resolvable symbol).
            base, suffix_mic = split_yahoo_ticker(ticker_q)
            if not suffix_mic:
                return jsonify({"error":
                    f"'{ticker_q}' has no exchange suffix. Enter the full "
                    f"ticker exactly as on Yahoo Finance, e.g. BATG.L.",
                    "mode": 2}), 422

            # Confirm Yahoo recognises the exact ticker and reports a
            # currency. This is mode 2's only automatic step.
            cur, vnote = validate_mode2_ticker(ticker_q)
            if not cur:
                return jsonify({"error": vnote, "mode": 2}), 422

            # The user must supply an ISIN — its sole job is to be a
            # unique, stable holdings-cache key, so it is NOT validated
            # (it need not be a real ISIN, only unique).
            key_isin = request.args.get("isin", "").strip().upper()
            if not key_isin:
                # This ticker may have been fetched before — if so, the
                # ISIN the user gave then is already on record. Surface
                # it so the frontend can pre-fill the field. Source
                # order: the listings cache's identity block (direct,
                # authoritative lookup for THIS ticker), then a reverse
                # scan of isin_map.json as a fallback.
                known_isin = ""
                try:
                    known_isin = listing_identity_lookup_isin(ticker_q)
                    if not known_isin:
                        for key, ent in (load_isin_map() or {}).items():
                            if (ent or {}).get("ticker", "").upper() == ticker_q:
                                known_isin = key.partition("|")[0].upper()
                                break
                except Exception as exc:
                    print(f"[Identity] known-ISIN lookup failed for "
                          f"{ticker_q}: {exc}")
                # Prompt the frontend to collect the ISIN key. The ticker
                # is confirmed valid; a re-request with &isin= completes.
                return jsonify({
                    "needs_isin":  True,
                    "ticker":      ticker_q,
                    "currency":    cur,
                    "mode":        2,
                    "message":     vnote,
                    "known_isin":  known_isin,   # "" when never fetched before
                }), 422

            resolved_isin = key_isin
            resolved_tkr  = ticker_q
            resolved_mic  = suffix_mic
            resolved_cur  = cur
            resolved_note = (f"{vnote} ISIN key {key_isin} supplied by user.")
            # Mode 2 differs from mode 1 in provenance, not just in
            # input: the user typed the ticker, and the MIC is read off
            # its suffix rather than chosen. OpenFIGI is not consulted.
            identity_sources = {
                "isin":     "user",
                "ticker":   "user",
                "exchange": "user",
                "currency": "yahoo",
                "longName": "yahoo",
            }

        print(f"\n{'='*55}")
        print(f"  ISIN={resolved_isin}  Ticker={resolved_tkr}  "
              f"MIC={resolved_mic}  Cur={resolved_cur or '(from profile)'}  "
              f"Portfolio={portfolio_name or '(default)'}  Force={force}")
        print(f"  Resolution: {resolved_note}")
        print(f"{'='*55}")

        # Load with the fully-resolved ticker (known_ticker bypasses the
        # resolver's own OpenFIGI path — we have already resolved).
        data = load_fund_data(
            resolved_isin, resolved_mic, cache_cfg,
            force_refresh=force, known_ticker=resolved_tkr,
            commit=commit,
        )

        # ── Backfill the identity quad ─────────────────────────────────
        # Both modes resolve all four fields before loading, so the quad
        # is complete here — just attach it to the response and persist.
        if not resolved_cur:
            # Defensive fallback only — should not happen.
            resolved_cur = (data.get("profile") or {}).get("currency", "") or ""
        data["isin"]             = resolved_isin
        data["ticker"]           = resolved_tkr
        data["resolved_mic"]     = resolved_mic
        data["trading_currency"] = resolved_cur
        data["resolution"]       = resolved_note
        # Stamp the ISIN into the profile so downstream (justETF lookup,
        # cache list) can rely on it regardless of what Yahoo returned.
        if isinstance(data.get("profile"), dict) and resolved_isin:
            data["profile"]["isin"] = resolved_isin
        # Persist the resolution so the fund is identity-complete on the
        # next fetch without another OpenFIGI round-trip.
        if resolved_isin and resolved_tkr:
            isin_map_put(resolved_isin, resolved_mic, resolved_tkr,
                         resolved_mic, resolved_note)

        # Persist the identity quad INTO the per-listing cache file,
        # keyed by ticker. The cache list then reads ISIN, exchange and
        # trading currency straight off each listings cache file — they
        # no longer depend on the fund being in a portfolio. ``identity``
        # is a top-level sibling of the TTL'd Yahoo categories, never
        # ages out, never needs refreshing — it's an output of the
        # resolver, stamped here once.
        if resolved_tkr:
            try:
                listing_identity_put(resolved_tkr, {
                    "isin":      resolved_isin,
                    "ticker":    resolved_tkr,
                    "exchange":  resolved_mic,
                    "currency":  resolved_cur,
                    "sources":   identity_sources,
                })
            except Exception as exc:
                print(f"[Identity] write failed for {resolved_tkr}: {exc}")

        print(f"[Summary] price_pts={len(data['price_history'])}  "
              f"holdings={len(data['holdings_rows'])} ({data['holdings_source']})  "
              f"sectors={len(data['sectors'])}  "
              f"asset={data['asset_class'].get('class')}  "
              f"isin={resolved_isin}  cur={resolved_cur}")
        return jsonify(data)

    # -----------------------------------------------------------------------
    # Portfolios
    # -----------------------------------------------------------------------
    @app.route("/api/portfolios", methods=["GET"])
    def api_portfolios_list() -> Response:
        """List all portfolios plus the metadata the frontend needs.

        Returns the cache category list and asset-class enum so the
        frontend modals can populate their options without hardcoding.
        """
        from porxpy.resources import primary_asset_classes
        return jsonify({
            "portfolios":        load_portfolios(),
            "cache_categories":  CACHE_CATEGORIES,
            "default_cache_cfg": DEFAULT_CACHE_CONFIG,
            "asset_classes":     list(primary_asset_classes()),
        })

    @app.route("/api/portfolios", methods=["POST"])
    def api_portfolios_create() -> Response:
        """Create a new portfolio.

        Body:
            name (str, required)
            base_currency (str, default "USD")
            cache_config (dict, optional — see normalise_cache_config)
        """
        body = request.get_json(force=True, silent=True) or {}
        name          = (body.get("name") or "").strip()
        base_currency = (body.get("base_currency") or "USD").strip().upper()
        cache_cfg     = normalise_cache_config(body.get("cache_config"))
        if not name:
            return jsonify({"error": "name is required"}), 400

        portfolio = {
            "id":            str(uuid.uuid4()),
            "name":          name,
            "base_currency": base_currency,
            "created":       now_iso(),
            "cache_config":  cache_cfg,
            "funds":         [],
        }
        upsert_portfolio(portfolio)
        return jsonify(portfolio), 201

    @app.route("/api/portfolios/<pid>", methods=["GET"])
    def api_portfolio_get(pid: str) -> Response:
        """Fetch a single portfolio by id."""
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404
        return jsonify(p)

    @app.route("/api/portfolios/<pid>", methods=["PUT"])
    def api_portfolio_update(pid: str) -> Response:
        """Update name, base_currency, or cache_config on a portfolio."""
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404
        body = request.get_json(force=True, silent=True) or {}
        if "name" in body and str(body["name"]).strip():
            p["name"] = str(body["name"]).strip()
        if "base_currency" in body and str(body["base_currency"]).strip():
            p["base_currency"] = str(body["base_currency"]).strip().upper()
        if "cache_config" in body:
            p["cache_config"] = normalise_cache_config(body["cache_config"])
        upsert_portfolio(p)
        return jsonify(p)

    @app.route("/api/portfolios/<pid>", methods=["DELETE"])
    def api_portfolio_delete(pid: str) -> Response:
        """Delete a portfolio by id."""
        if not delete_portfolio(pid):
            return jsonify({"error": "portfolio not found"}), 404
        return jsonify({"deleted": pid})

    def _score_preset_label(preset_name) -> str:
        """The display name of a weight model, or "" when scoring is off.

        Echoed back with an optimisation so the result can say which
        model produced its scores. The picker on the page is not a safe
        substitute: it can be changed after a run, and the figures would
        then be captioned with a model that never produced them.
        """
        from porxpy.config import SCORING_PRESETS
        name = (preset_name or "").strip()
        if not name or name.lower() == "none":
            return ""
        presets = ((load_settings().get("scoring") or {}).get("presets")
                   or SCORING_PRESETS)
        return ((presets.get(name) or {}).get("label")) or name

    def _optimizer_scores(preset_name):
        """Peer scores for the optimiser, or None to skip score refinement.

        Returns the same score blocks the fund list shows, so the number
        the optimiser acts on is the number the user can see next to each
        fund — a scoring model that disagreed with the visible one would
        make the optimiser's choices unexplainable.
        """
        name = (preset_name or "").strip()
        if not name or name.lower() == "none":
            return None
        try:
            return (_score_universe_cached(name) or {}).get("scores") or None
        except Exception as exc:
            print(f"[Optimizer] scoring unavailable ({exc}); "
                  f"proceeding on fit alone")
            return None

    @app.route("/api/portfolios/<pid>/optimize", methods=["POST"])
    def api_portfolio_optimize(pid: str) -> Response:
        """Propose a portfolio design (or rebalance) matching the targets.

        Read-only — this *proposes*, it never mutates. The user reviews the
        trade list and applies it via ``POST /api/portfolios/<pid>/trades``.
        That propose/review/apply split is deliberate: an optimiser that
        silently rearranged your portfolio would be terrifying.

        The candidate universe is the pre-loaded funds (the listings cache)
        — that is exactly what "design a portfolio from my saved funds"
        means. Funds already in the portfolio are always included, whether
        or not they'd otherwise be candidates, since a rebalance has to be
        able to sell them.

        Body (all optional)::

            {
              "max_funds":   10,       # cap on funds in the design
              "min_weight":  0.01,     # drop dust positions below this
              "min_trade":   100.0,    # suppress trades below this (base ccy)
              "max_error": {           # tolerance PER FACET, as fractions
                  "asset_class": 0.02, # "within 2 percentage points"
                  "sector":      0.10,
                  "country":     0.05,
                  "currency":    0.15,
              },
              "candidates":  ["VWRL.AS", ...],   # restrict the universe
            }

        ``max_error`` also shapes the objective, not just the stopping test:
        a facet you demand 2% on is weighted five times harder than one you
        allow 10% on. Otherwise the solver would spread effort evenly and
        might never satisfy the strict facet at all.
        """
        from porxpy.optimizer import optimise_portfolio
        # Local imports, matching the pattern used by the other endpoints.
        from porxpy.utils import cash_positions_get
        # Country targets are region-keyed while fund breakdowns are
        # country-keyed; this maps one to the other.
        from porxpy.resources import MSTAR_TO_REGION

        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404

        body      = request.get_json(force=True, silent=True) or {}
        cache_cfg = normalise_cache_config(p.get("cache_config"))
        base_cur  = (p.get("base_currency") or "USD").upper()

        targets_pct = portfolio_targets_get(pid) or {}
        # Targets are stored as percents (what the user typed); the solver
        # works in fractions. Convert here so each layer speaks its natural
        # unit.
        # v0.65.0: two levels of nesting — {facet: {level: {key: pct}}}.
        targets = {}
        for facet, per_level in targets_pct.items():
            lvls = {}
            for level, blk in (per_level or {}).items():
                kept = {k: float(v) / 100.0 for k, v in (blk or {}).items()
                        if float(v or 0) > 0}
                if kept:
                    lvls[level] = kept
            if lvls:
                targets[facet] = lvls
        if not targets:
            return jsonify({"ok": False,
                            "reason": "no targets set — set some on the "
                                      "Targets tab first"}), 200

        held = {(f.get("ticker") or "").upper(): float(f.get("shares") or 0.0)
                for f in (p.get("funds") or [])}
        # Per-portfolio lock flags. Absent means unlocked, which is the
        # default and the overwhelmingly common case.
        locked_by_ticker = {(f.get("ticker") or "").upper(): bool(f.get("locked"))
                            for f in (p.get("funds") or [])}

        # Universe: explicit list, else every pre-loaded fund. Held funds
        # are always in, so a rebalance can sell them.
        wanted = body.get("candidates")
        if wanted:
            universe = {t.upper() for t in wanted} | set(held)
        else:
            # Every pre-loaded fund = every file in cache/listings/.
            # The listing file's existence IS the "saved" marker
            # (v0.21.0 explicit-save model), so this is exactly the
            # set of funds the user has committed to.
            universe = set(held)
            if LISTINGS_DIR.exists():
                universe |= {fp.stem.upper()
                             for fp in LISTINGS_DIR.glob("*.json")}

        candidates, skipped = [], []
        for tk in sorted(universe):
            ident = listing_identity_get(tk)
            isin  = (ident.get("isin") or "").strip().upper()
            if not isin:
                skipped.append({"ticker": tk, "reason": "no cached identity"})
                continue
            try:
                # commit=True: these are saved funds by definition.
                data = load_fund_data(isin, ident.get("exchange") or None,
                                      cache_cfg, known_ticker=tk, commit=True)
            except Exception as exc:
                skipped.append({"ticker": tk, "reason": f"load failed: {exc}"})
                continue

            ph = data.get("price_history") or []
            price = None
            for row in reversed(ph):
                try:
                    v = float(row.get("close"))
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    price = v
                    break
            if not price:
                skipped.append({"ticker": tk, "reason": "no price"})
                continue

            # Into base currency, so the solver is unit-consistent.
            fund_cur = ((data.get("profile") or {}).get("currency") or "").upper()
            fx = 1.0
            if fund_cur and fund_cur != base_cur:
                rate, _n = fx_rate(fund_cur, base_cur)
                if not rate:
                    skipped.append({"ticker": tk,
                                    "reason": f"no FX {fund_cur}->{base_cur}"})
                    continue
                fx = rate

            # Look-through exposure, per facet.
            #
            # Source preference: the holdings roll-up whenever it exists,
            # falling back to the fund's resolved breakdown card (issuer or
            # user upload) otherwise.
            #
            # This deliberately does NOT follow the per-card source override.
            # That override is a *display* choice — you may well want the
            # issuer's official view on the fund page. But for optimisation
            # the funds must be described on a comparable basis, and mixing
            # sources silently biases the result: a fund whose issuer data
            # sums to 0.85 is charged 15% into the "other" bucket, so it
            # loses to an identical fund described by a look-through summing
            # to 1.0 — not for being a worse fund, but for being worse
            # described.
            #
            # Look-through is the sounder basis in both directions: it can't
            # understate (a shortfall is genuinely un-held) and it can't
            # overstate (issuer sector weights may be normalised within the
            # equity sleeve, which would flatter a mixed fund into claiming
            # sector exposure it doesn't have).
            # Look-through exposure per (facet, level, key), computed by
            # breakdowns.candidate_exposures. Lifted out of here in
            # v0.66.3: as an inline block it could not be tested against
            # a real fund block without a live fetch.
            exposures, fund_src = candidate_exposures(
                data.get("fund_breakdowns") or {}, targets)

            candidates.append({
                "ticker":         tk,
                "name":           (data.get("profile") or {}).get("longName") or tk,
                "price_base":     price * fx,
                "current_shares": held.get(tk, 0.0),
                # Per-fund opt-out. Unchecked funds are frozen rather
                # than removed: a held position the user has ring-fenced
                # still shapes the portfolio's exposure, so the optimiser
                # has to design around it, not pretend it isn't there.
                "include":        bool(override_get(
                    isin, "include_in_optimizer", True)),
                # Per-portfolio, unlike `include`: this position is not to
                # be sold here, which says nothing about other portfolios.
                "locked":         bool(locked_by_ticker.get(tk, False)),
                "exposures":      exposures,
                "sources":        fund_src,   # {facet: holdings|fund|upload|none}
                # Peer identity, for the alternatives shortlist. Without
                # these three, scoring.peer_key read nothing off a
                # candidate and answered "unknown|none|" for all of them,
                # so every fund in the universe was every other fund's
                # peer and a bond fund could be offered as an alternative
                # to an equity tracker. load_fund_data has already
                # resolved both, so they are read rather than recomputed.
                "primary_asset_class": (data.get("asset_class") or {}).get("class") or "",
                "focus_type":     (data.get("fund_structure") or {}).get("focus_type") or "none",
                "focus_detail":   (data.get("fund_structure") or {}).get("focus_detail") or "",
            })

        # Cash: aggregate to base currency, and take its exposure from the
        # positions themselves (a EUR savings account is not the same
        # exposure as a USD one).
        cash_total = 0.0
        cash_exp: dict[str, dict[str, dict[str, float]]] = {}
        for c in cash_positions_get(pid):
            amt = float(c.get("amount") or 0.0)
            if amt <= 0:
                continue
            cur = (c.get("currency") or base_cur).upper()
            fx = 1.0
            if cur != base_cur:
                rate, _n = fx_rate(cur, base_cur)
                if not rate:
                    continue
                fx = rate
            v = amt * fx
            cash_total += v
            # v0.65.0: per (facet, level), the same shape a candidate's
            # exposures now have. The cash position states one value per
            # facet; each level it can reach is derived from it, exactly
            # as a fund's is. The country -> region remap that used to
            # live here is gone: region is a level, so a cash position in
            # Germany contributes to germany at country level AND to
            # europeDeveloped at region level, rather than being
            # rewritten as one or the other.
            for facet, field in (("asset_class", "asset_class"),
                                 ("sector", "sector"),
                                 ("country", "country"),
                                 ("currency", "currency")):
                raw = cur if facet == "currency" else (c.get(field) or "").strip()
                if not raw:
                    continue
                node, _miss = resolve_facet_node(facet, raw)
                if not node:
                    continue
                for level in FACET_LEVELS.get(facet, (facet,)):
                    key = _key_at_level(facet, node, level)
                    if not key or key in ("unknown", "n/a"):
                        continue
                    cash_exp.setdefault(facet, {}).setdefault(level, {})
                    cash_exp[facet][level][key] = \
                        cash_exp[facet][level].get(key, 0.0) + v

        for facet, per_level in cash_exp.items():
            for blk in per_level.values():
                tot = sum(blk.values())
                if tot > 0:
                    for k in blk:
                        blk[k] /= tot
        if not cash_exp:
            cash_exp = {"asset_class": {"asset_class": {"cash": 1.0}}}

        # A targeted facet for which NO candidate has any exposure data is a
        # different failure from "targets unreachable", and needs a different
        # fix. It usually means the facet's breakdown card is sourced from
        # the issuer, and Yahoo publishes nothing for it — country is the
        # usual victim. Left unexplained, the optimiser just reports a huge
        # unreachable error and sends the user hunting for a fund that will
        # never help, when the real fix is to switch that card to the
        # holdings look-through.
        facet_warnings = []
        # Which source each facet's data came from, across the universe.
        # Surfaced because a mixed run is worth knowing about: issuer and
        # look-through data are not always on the same basis, so a fund can
        # win or lose on how it's described rather than what it holds.
        # Per (facet, level): how many candidates actually answer it, and
        # with how much non-residual weight. Added in v0.66.4 because a
        # target reading 0% achieved has several possible causes — no
        # data, data at a level the target is not set at, or a genuine
        # miss — and the response said nothing that told them apart.
        # A permanent diagnostic, not a debug aid: "measured on 3 of 40
        # candidates" is the difference between a target the optimiser
        # failed to hit and one it could never see.
        from porxpy.breakdowns import NA_KEY as _NA, UNKNOWN_KEY as _UNK
        level_report: dict[str, dict[str, dict]] = {}
        for facet, per_level in (targets or {}).items():
            level_report[facet] = {}
            for level in per_level:
                answering, weight, keys_seen = 0, 0.0, set()
                for c in candidates:
                    blk = (((c.get("exposures") or {}).get(facet) or {})
                           .get(level) or {})
                    real = {k: w for k, w in blk.items() if k not in (_UNK, _NA)}
                    if real:
                        answering += 1
                        weight += sum(real.values())
                        keys_seen |= set(real)
                targeted = set(per_level.get(level) or {})
                level_report[facet][level] = {
                    "candidates_answering": answering,
                    "candidates_total":     len(candidates),
                    "exposure_weight":      round(weight, 4),
                    # The targeted buckets NO candidate carries. This one
                    # names the problem outright: a target on a bucket
                    # nothing in the universe holds cannot be met by any
                    # selection, however the solver is tuned.
                    "targeted_keys_absent": sorted(targeted - keys_seen),
                }

        source_mix: dict[str, dict[str, int]] = {}
        for facet in targets:
            mix: dict[str, int] = {}
            for c in candidates:
                s = (c.get("sources") or {}).get(facet, "none")
                mix[s] = mix.get(s, 0) + 1
            source_mix[facet] = mix

            # v0.66.3: RESIDUAL weight does not count as exposure.
            #
            # This summed every value including `unknown`, so a universe
            # in which no fund has any country data at all still totalled
            # 1.0 per fund and the guard stayed quiet. The user then saw
            # every country/region target sitting at 0% achieved with
            # nothing anywhere saying why — the optimiser had correctly
            # fitted the data it had, which was none.
            from porxpy.breakdowns import NA_KEY, UNKNOWN_KEY
            total = sum(
                sum(w for blk in ((c.get("exposures") or {}).get(facet)
                                  or {}).values()
                    for k, w in (blk or {}).items()
                    if k not in (UNKNOWN_KEY, NA_KEY))
                for c in candidates)
            if total <= 1e-9:
                facet_warnings.append(
                    f"No {facet} exposure data on any candidate fund — every "
                    f"{facet} target will read 0% achieved. The funds answer "
                    f"this facet only as 'unknown'. Set each fund's {facet} "
                    f"card to Holdings or Factsheet on its page, or upload "
                    f"holdings so a look-through is available.")
            elif mix.get("none"):
                # These funds aren't excluded — they're treated as 100%
                # "other" for this facet, so they can still serve the
                # untargeted remainder. But they can never help hit a
                # targeted bucket, which is worth saying out loud.
                facet_warnings.append(
                    f"{mix['none']} fund(s) have no {facet} data — they can "
                    f"only be used for the untargeted part of {facet}.")

        result = optimise_portfolio(
            candidates, targets, cash_total, cash_exp,
            max_funds=int(body.get("max_funds") or 10),
            min_weight=float(body.get("min_weight") or 0.01),
            # Default 100, not 0 — a zero minimum lets the optimiser emit
            # buy/sell suggestions of a couple of cents, which are noise.
            min_trade_base=float(body.get("min_trade", 100.0) or 0.0),
            # Per-facet tolerances: {"asset_class":0.02, "sector":0.10, ...}
            # A bare float is still accepted and applied to every facet.
            max_error=body.get("max_error") or 0.05,
            facet_weights=body.get("facet_weights") or None,
            # Fund quality, consulted only after the fit is inside every
            # tolerance. A preset of "" or "none" turns it off, which is
            # how the user asks for the best fit and nothing else.
            scores=_optimizer_scores(body.get("score_preset")),
            # Substitutions the user accepted from the alternatives table.
            # Sent back through the full optimiser rather than patched on
            # top of the previous answer: deviations do not add linearly,
            # so two swaps each costing 0.7pp may together cost 0.3pp or
            # 2.1pp, and only a real re-solve knows which.
            substitutions=body.get("substitutions") or None,
        )
        result["base_currency"]   = base_cur
        result["candidate_count"] = len(candidates)
        # Which weight model these scores came from. Every surface that
        # shows a score names its model, and for a run that model is the
        # one asked for here — not whatever the page's picker reads now.
        result["score_preset"]       = (body.get("score_preset") or "").strip()
        result["score_preset_label"] = _score_preset_label(
            body.get("score_preset"))
        result["skipped"]         = skipped
        result["cash_before"]     = round(cash_total, 2)
        result["facet_warnings"]  = facet_warnings
        result["source_mix"]      = source_mix
        result["level_report"]    = level_report
        return jsonify(result)

    @app.route("/api/portfolios/<pid>/trades", methods=["POST"])
    def api_portfolio_trades(pid: str) -> Response:
        """Apply a batch of trades. Atomic — all of them, or none.

        Consumes the same trade shape the optimiser emits, which is the
        whole point: the manual Buy/Sell dialog is just a batch of one, and
        "Apply this design" is a batch of many. One execution path, so the
        two cannot drift apart.

        Body::

            {"trades": [{"ticker", "shares_delta", "cash_id"}, ...]}
        """
        from porxpy.trades import apply_trades, price_lookup_from_cache

        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404

        body   = request.get_json(force=True, silent=True) or {}
        trades = body.get("trades") or []
        if not isinstance(trades, list) or not trades:
            return jsonify({"error": "no trades supplied"}), 400

        cache_cfg = normalise_cache_config(p.get("cache_config"))

        # A fund the optimiser picked may not be in the portfolio yet — it
        # was only a pre-loaded candidate. Add it at zero shares so the buy
        # has something to land on. Buying a fund is, after all, exactly
        # what "add it to the portfolio" means.
        existing = {(f.get("ticker") or "").upper() for f in (p.get("funds") or [])}
        added = []
        for t in trades:
            tk = (t.get("ticker") or "").strip().upper()
            if not tk or tk in existing:
                continue
            if not listing_identity_lookup_isin(tk):
                return jsonify({"error": f"{tk} is not a saved fund — "
                                         f"load and save it first"}), 400
            p.setdefault("funds", []).append({"ticker": tk, "shares": 0.0})
            existing.add(tk)
            added.append(tk)
        if added:
            upsert_portfolio(p)

        res = apply_trades(pid, trades, price_lookup_from_cache(cache_cfg))
        res["added_funds"] = added

        # Rolling back the auto-add on failure keeps the failed case a true
        # no-op — otherwise a rejected batch would still litter the
        # portfolio with empty positions.
        if not res.get("ok") and added:
            p = find_portfolio(pid)
            p["funds"] = [f for f in (p.get("funds") or [])
                          if (f.get("ticker") or "").upper() not in added]
            upsert_portfolio(p)
            res["added_funds"] = []

        return jsonify(res), (200 if res.get("ok") else 400)

    @app.route("/api/portfolios/<pid>/funds", methods=["POST"])
    def api_portfolio_add_fund(pid: str) -> Response:
        """Add (or replace) a fund in a portfolio.

        Body:
            ticker  (required)  — fully-resolved Yahoo ticker
            shares  (numeric, optional)
            note    (optional, free text)

        Under the 0.12.0 data model a portfolio entry stores only the
        ticker and the shares held — identity (ISIN, exchange,
        currency) lives in the listings cache and is shared across
        every portfolio that references the ticker. The frontend
        therefore POSTs only the ticker (which it already knows from
        having fetched the fund), never the ISIN or exchange.
        """
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404
        body = request.get_json(force=True, silent=True) or {}
        ticker_q = (body.get("ticker") or "").strip().upper()
        shares   = body.get("shares")
        if not ticker_q:
            return jsonify({"error": "ticker is required"}), 400

        # The fund must have been fetched at least once — that's how
        # its identity got into the listings cache, which the portfolio
        # later relies on for ISIN/exchange/currency. Without it the
        # portfolio row would be unloadable.
        if not listing_identity_lookup_isin(ticker_q):
            return jsonify({
                "error": f"ticker {ticker_q!r} has no identity recorded; "
                         "fetch the fund first"
            }), 404

        shares_val = None
        if shares is not None and shares != "":
            try:
                shares_val = float(shares)
            except (TypeError, ValueError):
                return jsonify({"error": "shares must be numeric"}), 400
            if shares_val < 0:
                return jsonify({"error": "shares must be ≥ 0"}), 400

        funds = p.get("funds", [])
        # Replace on duplicate — the ticker is the unique row key in a
        # portfolio (a portfolio cannot hold two listings of the same
        # exchange/currency variant separately, since both would resolve
        # to the same ticker).
        funds = [f for f in funds if (f.get("ticker") or "").upper() != ticker_q]
        funds.append({
            "ticker": ticker_q,
            "shares": shares_val,
            "added":  now_iso(),
            "note":   body.get("note") or "",
        })
        p["funds"] = funds
        upsert_portfolio(p)
        return jsonify(p)

    @app.route("/api/portfolios/<pid>/funds/<ticker>", methods=["PUT"])
    def api_portfolio_update_fund(pid: str, ticker: str) -> Response:
        """Update the shares held, or the lock flag, for a fund here.

        Two per-portfolio, per-fund properties: ``shares`` and ``locked``.
        Everything else — asset class, structure, cost — is fund-level and
        shared across portfolios.

        ``locked`` means "do not suggest selling what I hold of this fund
        in THIS portfolio". It is deliberately not the same thing as the
        fund-level ``include_in_optimizer`` flag, which means "never put
        this fund in a buy suggestion, in any portfolio". The two are
        independent and give four states:

            include  locked   the optimiser may
            -------  ------   ---------------------------------
            True     False    buy and sell freely
            False    False    sell, but never buy
            True     True     buy more, but never sell
            False    True     neither — fully frozen

        Path params:
            pid:    portfolio id.
            ticker: fund's resolved Yahoo ticker (the unique row key
                    within a portfolio).
        """
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404
        body = request.get_json(force=True, silent=True) or {}
        ticker = ticker.upper()

        target = None
        for f in p.get("funds", []):
            if (f.get("ticker") or "").upper() == ticker:
                target = f; break
        if not target:
            return jsonify({"error": "fund not in portfolio"}), 404

        if "shares" in body:
            v = body["shares"]
            if v in (None, ""):
                target["shares"] = None
            else:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return jsonify({"error": "shares must be numeric"}), 400
                if fv < 0:
                    return jsonify({"error": "shares must be ≥ 0"}), 400
                target["shares"] = fv
            target.pop("weight", None)   # drop any legacy weight field

        if "locked" in body:
            # Stored only when True: the default is unlocked, and writing
            # False everywhere would put a flag on every position in every
            # portfolio to record that nothing special is happening.
            if bool(body["locked"]):
                target["locked"] = True
            else:
                target.pop("locked", None)

        upsert_portfolio(p)
        return jsonify(p)

    @app.route("/api/portfolios/<pid>/funds/<ticker>", methods=["DELETE"])
    def api_portfolio_remove_fund(pid: str, ticker: str) -> Response:
        """Remove a fund from a portfolio by ticker."""
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404
        ticker = ticker.upper()
        before = len(p.get("funds", []))
        p["funds"] = [
            f for f in p.get("funds", [])
            if (f.get("ticker") or "").upper() != ticker
        ]
        if len(p["funds"]) == before:
            return jsonify({"error": "fund not in portfolio"}), 404
        upsert_portfolio(p)
        return jsonify(p)

    # -----------------------------------------------------------------------
    # Cash positions (v0.14.0)
    # -----------------------------------------------------------------------
    # Cash positions live on the portfolio dict alongside ``funds``.
    # The inline-edit table on the Cash sub-tab uses PUT to replace
    # the whole list atomically — simpler than per-cell PATCHes when
    # the user can add / remove / reorder rows freely. DELETE is also
    # exposed for the per-row delete button, which is more ergonomic
    # than "send the whole list minus one entry".

    @app.route("/api/portfolios/<pid>/cash", methods=["GET"])
    def api_portfolio_cash_get(pid: str) -> Response:
        """Return the cash positions for a portfolio.

        Always returns a 200 with a (possibly empty) list. A missing
        portfolio is a 404 — the only error case here.
        """
        from porxpy.utils import cash_positions_get
        if not find_portfolio(pid):
            return jsonify({"error": "portfolio not found"}), 404
        return jsonify({"cash_positions": cash_positions_get(pid)})

    @app.route("/api/portfolios/<pid>/cash", methods=["PUT"])
    def api_portfolio_cash_put(pid: str) -> Response:
        """Replace the cash positions for a portfolio.

        Body: ``{"cash_positions": [<position>, ...]}``. Each position
        is coerced through :func:`porxpy.utils.coerce_cash_position`,
        so the client can send partial dicts and trust the response
        for the canonical shape (including freshly minted ids on new
        rows). The portfolio rollup is NOT recomputed here — the
        frontend invalidates and re-fetches ``/view`` after a save
        the same way it already does for fund changes.
        """
        from porxpy.utils import cash_positions_set
        body = request.get_json(force=True, silent=True) or {}
        positions = body.get("cash_positions")
        if not isinstance(positions, list):
            return jsonify({"error": "cash_positions must be a list"}), 400
        try:
            saved = cash_positions_set(pid, positions)
        except KeyError:
            return jsonify({"error": "portfolio not found"}), 404
        return jsonify({"cash_positions": saved})

    @app.route("/api/portfolios/<pid>/cash/<position_id>", methods=["DELETE"])
    def api_portfolio_cash_delete(pid: str, position_id: str) -> Response:
        """Delete one cash position from a portfolio by id."""
        from porxpy.utils import cash_position_delete
        ok = cash_position_delete(pid, position_id)
        if not ok:
            return jsonify({"error": "portfolio or position not found"}), 404
        return jsonify({"deleted": position_id})

    # -----------------------------------------------------------------------
    # Portfolio targets — GET/PUT the per-portfolio target exposure dict
    # -----------------------------------------------------------------------
    # Targets are stored inside the portfolio entry itself (see
    # porxpy.utils.portfolio_targets_*). They are sparse — a facet
    # with an empty dict, or a key absent from a facet's dict, means
    # "no target set" rather than "target zero". The country facet
    # is keyed at the mstar_region level (10 buckets); the rollup
    # gets aggregated up to region in :func:`compute_target_deviations`
    # before comparison.
    @app.route("/api/portfolios/<pid>/targets", methods=["GET"])
    def api_portfolio_targets_get(pid: str) -> Response:
        """Return the stored targets for ``pid``.

        Always returns the four-facet shape (some facets possibly
        empty). 404 if the portfolio doesn't exist.
        """
        if not find_portfolio(pid):
            return jsonify({"error": f"no portfolio with id {pid!r}"}), 404
        return jsonify({"targets": portfolio_targets_get(pid)})

    @app.route("/api/portfolios/<pid>/targets", methods=["PUT"])
    def api_portfolio_targets_put(pid: str) -> Response:
        """Replace the stored targets for ``pid``.

        Body (JSON): ``{"targets": {<facet>: {<key>: percent, ...}, ...}}``.
        Replace semantics — the whole dict is overwritten. Pass an
        empty/all-facets-empty dict to clear.

        Returns:
            ``{targets}`` — the persisted (normalised) dict.
        """
        if not find_portfolio(pid):
            return jsonify({"error": f"no portfolio with id {pid!r}"}), 404
        body = request.get_json(force=True, silent=True) or {}
        raw  = body.get("targets")

        # v0.65.0: parent/child consistency is checked HERE, on save,
        # rather than per field. A target set is only coherent once it
        # is complete — typing semiconductors 15% before technology 35%
        # would fail a per-field check on a set that ends up perfectly
        # valid — so the whole set is judged at once and rejected as a
        # whole, leaving the user's edits intact to correct.
        from porxpy.targets import validate_target_levels
        from porxpy.utils import _coerce_targets
        problems = validate_target_levels(
            _coerce_targets(raw if isinstance(raw, dict) else {}))
        if problems:
            return jsonify({"error": "inconsistent targets",
                            "problems": problems}), 409

        try:
            persisted = portfolio_targets_put(pid, raw if isinstance(raw, dict) else {})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"targets": persisted})

    @app.route("/api/targets/meta/<facet>", methods=["GET"])
    def api_targets_meta(facet: str) -> Response:
        """Return the targetable values for a metadata facet.

        The meta facets (``market_cap``, ``style_box``) have a closed,
        short vocabulary defined in config rather than in a resource
        CSV, and not all of it can carry a target: ``unknown`` is a data
        gap and ``n/a`` restates the cash target already set on
        asset_class. Both still appear as buckets on the X-ray card and
        in the deviation report's untargeted summary — they are just not
        things a user can aim at, so the editor must not offer them.

        Returns:
            ``{values: [{"key": v, "label": display}, ...]}``, or a 404
            for a facet that is not a metadata facet.
        """
        from porxpy.config import META_FACET_TARGETABLE
        allowed = META_FACET_TARGETABLE.get(facet)
        if allowed is None:
            return jsonify({"error": f"not a metadata facet: {facet}"}), 404
        labels = {
            "large": "Large cap", "mid": "Mid cap", "small": "Small cap",
            "mixed": "Mixed",
            "growth": "Growth", "blend": "Blend", "value": "Value",
        }
        return jsonify({"values": [
            {"key": v, "label": labels.get(v, v)} for v in allowed
        ]})

    @app.route("/api/targets/regions", methods=["GET"])
    def api_targets_regions() -> Response:
        """Return the mstar_region list for the targets-editor dropdown.

        Sourced from the loaded ``country_codes.csv`` resource. Order
        is by region name; the labels are the raw ``mstar_region``
        values (e.g. ``"northAmerica"``, ``"emergingMarkets"``).

        Returns:
            ``{values: [{"key": region, "label": display}, ...]}``
            sorted by label.
        """
        # Local import — same pattern as upload.list_canonical_values.
        from porxpy.resources import COUNTRY_ROWS
        seen: set[str] = set()
        out: list[dict] = []
        for r in COUNTRY_ROWS:
            reg = (r.get("parent") or "").strip()
            if reg and reg not in seen:
                seen.add(reg)
                # mstar_region values are camelCase ("northAmerica");
                # convert to a human-friendly "North America" label.
                label = reg[:1].upper() + reg[1:]
                # Insert a space before each capital letter.
                import re as _re
                label = _re.sub(r"(?<!^)(?=[A-Z])", " ", label)
                out.append({"key": reg, "label": label})
        out.sort(key=lambda x: x["label"])
        return jsonify({"values": out})

    @app.route("/api/targets/asset_classes", methods=["GET"])
    def api_targets_asset_classes() -> Response:
        """Return the fund-level asset-class list for the Targets editor.

        Sourced from Fund_class_definitions.csv via
        :func:`porxpy.resources.facet_alias_targets`. These keys
        (equity / fixed_income / cash / mixed / commodity / other) are
        exactly what the portfolio rollup emits, so a target set here
        compares correctly against the actual exposure. (The holdings
        CSV-upload modal uses a different endpoint — the finer holdings
        vocabulary — on purpose.)

        Returns:
            ``{values: [{"key", "label"}, ...]}`` in CSV/display order.
        """
        from porxpy.resources import facet_alias_targets
        return jsonify({"values": facet_alias_targets("asset_class")})

    # -----------------------------------------------------------------------
    # Shared portfolio enrichment
    # -----------------------------------------------------------------------
    def _build_enriched_funds(p: dict, cache_cfg: dict, force: bool,
                              base_cur: str) -> tuple[list[dict], float]:
        """Run load_fund_data + valuation for every fund in a portfolio.

        Shared by ``/view`` and ``/holdings_rollup`` so the two endpoints
        value funds identically and cannot drift. Returns the per-fund
        ``enriched`` list (each entry carrying ``valuation``, ``data``,
        ``effective_asset_class``, a derived ``weight``, etc.) and the
        portfolio total in base currency.
        """
        enriched: list[dict] = []
        for f in p.get("funds", []):
            ticker_q = (f.get("ticker") or "").strip().upper()
            if not ticker_q:
                continue

            # Identity (ISIN, exchange) comes from the listings cache —
            # portfolios only store {ticker, shares} in 0.12.0+. A
            # missing identity means the listings cache for this ticker
            # was purged: the row needs a refetch to be usable.
            ident = listing_identity_get(ticker_q)
            isin     = (ident.get("isin")     or "").strip().upper()
            exchange = (ident.get("exchange") or "").strip().upper() or None

            if not isin:
                # Soft-fail this fund: surface "needs refetch" through
                # the enriched entry so the UI can render a placeholder
                # row with a prompt rather than crashing the whole view.
                data = {"error": "needs_refetch", "profile": {},
                        "holdings_rows": [], "holdings_source": "none",
                        "holdings_breakdowns": {}, "breakdowns_source": "none",
                        "fund_breakdowns": {}, "breakdown_overrides": {},
                        "asset_allocation": [], "sectors": [],
                        "asset_class": {"class": "other"}, "price_history": []}
            else:
                try:
                    # Portfolio members are already saved (they have a
                    # cache entry by definition); commit=True is the
                    # default but stated explicitly.
                    data = load_fund_data(
                        isin, exchange, cache_cfg,
                        force_refresh=force, known_ticker=ticker_q,
                        commit=True,
                    )
                except Exception as exc:
                    print(f"[Portfolio view] error for {ticker_q}/{isin}: {exc}")
                    data = {"error": str(exc), "profile": {},
                            "holdings_rows": [], "holdings_source": "none",
                            "holdings_breakdowns": {}, "breakdowns_source": "none",
                            "fund_breakdowns": {}, "breakdown_overrides": {},
                            "asset_allocation": [], "sectors": [],
                            "asset_class": {"class": "other"}, "price_history": []}

            # ``data["asset_class"]["class"]`` is already the effective
            # class — load_fund_data applies any per-fund override on top
            # of Yahoo detection — so no extra resolution is needed here.
            effective_class = (data.get("asset_class") or {}).get("class") or "other"

            # Valuation: shares × latest close in native cur → base cur
            shares_raw = f.get("shares")
            if shares_raw is None:
                shares = None
            else:
                try:
                    shares = float(shares_raw)
                    if shares < 0:
                        shares = None
                except (TypeError, ValueError):
                    shares = None

            ph = data.get("price_history") or []
            last_close = ph[-1]["close"]   if ph else None
            last_date  = ph[-1]["date"]    if ph else None

            native_cur = (data.get("profile") or {}).get("currency") or ""
            canon_cur, divisor = normalise_currency(native_cur)

            valuation: dict = {
                "shares":             shares,
                "last_close":         last_close,
                "last_close_date":    last_date,
                "native_currency":    native_cur,
                "adjusted_currency":  canon_cur or native_cur,
                "pence_divisor":      divisor,
                "value_native":       None,
                "value_base":         None,
                "fx_rate":            None,
                "fx_note":            "",
                "valuation_error":    None,
            }

            if shares is None:
                valuation["valuation_error"] = "no shares entered"
            elif last_close is None:
                valuation["valuation_error"] = "no price available"
            else:
                adj_close = last_close / divisor
                valuation["value_native"] = round(adj_close * shares, 6)
                base_px, meta = price_in_base(last_close, native_cur, base_cur)
                valuation["fx_rate"] = meta.get("fx_rate")
                valuation["fx_note"] = meta.get("fx_note")
                if base_px is None:
                    valuation["valuation_error"] = meta.get("error") or "fx conversion failed"
                else:
                    valuation["value_base"] = round(base_px * shares, 6)

            enriched.append({
                "isin":                  isin or None,
                "exchange":              exchange,
                "ticker":                ticker_q,
                "shares":                shares,
                "effective_asset_class": effective_class,
                "valuation":             valuation,
                "data":                  data,
                "needs_refetch":         not isin,
                "holdings_status":       holdings_status_from_cache(isin),
            })

        # ──────────────────────────────────────────────────────────────
        # Cash positions (v0.14.0). Inject one synthetic enriched-fund
        # entry per cash position so the existing rollups (asset class
        # / sector / country / currency tiles, the aggregate Holdings
        # sub-tab) see cash exactly like a single-holding fund — its
        # base-currency value contributes to the portfolio total, its
        # facet values feed the breakdowns, and it gets a row in the
        # Holdings sub-tab.
        #
        # FX is fetched once per distinct position currency via the
        # same fx_rate helper used for funds.
        # ──────────────────────────────────────────────────────────────
        from porxpy.utils import cash_positions_get
        from porxpy.breakdowns import synth_enriched_for_cash_position
        positions = cash_positions_get(p["id"]) if p.get("id") else []
        if positions:
            # Fetch FX rates per distinct position currency once. The
            # helper returns (rate, divisor) where rate is the
            # multiplier from native → base; divisor handles pence
            # variants. Cash positions don't have a pence convention
            # so divisor is normally 1.0 and we just use rate.
            from porxpy.utils import fx_history
            distinct_curs = sorted({(pos.get("currency") or "").upper()
                                     for pos in positions
                                     if pos.get("currency")})
            fx_per_currency: dict[str, float] = {}
            for cur in distinct_curs:
                if not cur or cur == base_cur:
                    fx_per_currency[cur] = 1.0
                    continue
                try:
                    # fx_history returns a date-indexed series; latest
                    # value is the current spot. fall back to 0.0 on
                    # any failure (position then contributes nothing,
                    # which is the safe behaviour — better than crashing
                    # the portfolio view).
                    rates = fx_history(cur, base_cur) or {}
                    if rates:
                        latest = max(rates.keys())
                        fx_per_currency[cur] = float(rates[latest])
                    else:
                        fx_per_currency[cur] = 0.0
                except Exception as exc:
                    print(f"[Cash] FX {cur}->{base_cur} error: {exc}")
                    fx_per_currency[cur] = 0.0

            for pos in positions:
                cur = (pos.get("currency") or "").upper()
                fx  = fx_per_currency.get(cur, 0.0)
                enriched.append(
                    synth_enriched_for_cash_position(pos, base_cur, fx))

        # Portfolio total (base currency) and derived weights
        total_base = 0.0
        for e in enriched:
            v = e["valuation"].get("value_base")
            if v is not None:
                total_base += float(v)

        for e in enriched:
            v = e["valuation"].get("value_base")
            e["weight"] = (v / total_base) if (v is not None and total_base > 0) else None

        return enriched, total_base

    @app.route("/api/portfolios/<pid>/view")
    def api_portfolio_view(pid: str) -> Response:
        """Return a fully-enriched view of a portfolio.

        For each fund: runs :func:`load_fund_data` (so cache TTLs apply
        per-portfolio), values it in the portfolio's base currency,
        rolls weights up to portfolio level, and emits asset-class /
        sector / currency breakdowns.

        Query parameters:
            refresh: ``1`` to force live refresh on every fund.
        """
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404

        cache_cfg = normalise_cache_config(p.get("cache_config"))
        force     = request.args.get("refresh") == "1"
        base_cur  = (p.get("base_currency") or "USD").upper()

        enriched, total_base = _build_enriched_funds(
            p, cache_cfg, force, base_cur)

        # ──────────────────────────────────────────────────────────────
        # Fund/ETF-level breakdown cards. Each fund's data.fund_breakdowns
        # block (issuer-published aggregates, with any per-card holdings
        # override already applied by load_fund_data) is aggregated to
        # portfolio level by the pure rollup_portfolio_fundlevel — one
        # uniform path for all four facets (asset_class / sector /
        # country / currency), so the override flows through
        # automatically and the portfolio cards cannot drift from the
        # fund cards.
        # ──────────────────────────────────────────────────────────────
        fl = rollup_portfolio_fundlevel(enriched, total_base)
        fundlevel_breakdowns = fl["fundlevel_breakdowns"]
        fundlevel_coverage   = fl["fundlevel_coverage"]

        # asset_class_breakdown / sector_breakdown / currency_breakdown
        # were removed in v0.61.0. They were the original response keys,
        # kept as aliases derived from fundlevel_breakdowns — the same
        # fact stored twice under two names, which is exactly the
        # duplicated state the levelled block exists to remove. With
        # fundlevel_breakdowns now carrying every level, an alias could
        # only ever expose one of them, and a consumer reading the alias
        # would silently be looking at the default level while believing
        # it had the facet.

        # Trading-currency exposure — each fund's base value grouped by
        # the currency its *shares* are quoted in (adjusted, so GBp rolls
        # up into GBP). This is the FX exposure of the fund wrappers
        # themselves, distinct from the look-through currency breakdown
        # of the underlying holdings. Surfaced separately so the UI can
        # still show wrapper-currency exposure.
        trading_currency_totals: dict[str, float] = {}
        for e in enriched:
            v = e["valuation"].get("value_base")
            if v is None or v <= 0:
                continue
            cur = (e["valuation"].get("adjusted_currency")
                   or e["valuation"].get("native_currency") or "").upper()
            if not cur:
                cur = "UNKNOWN"
            trading_currency_totals[cur] = (
                trading_currency_totals.get(cur, 0.0) + float(v))

        trading_currency_breakdown = []
        if total_base > 0:
            for cur, val in sorted(trading_currency_totals.items(),
                                   key=lambda x: -x[1]):
                trading_currency_breakdown.append({
                    "currency": cur,
                    "weight":   round(val / total_base, 6),
                    "value":    round(val, 2),
                })

        # Funds without valuations (drives the UI warning)
        unvalued_funds = [
            {"isin": e["isin"], "ticker": e["ticker"],
             "reason": e["valuation"].get("valuation_error") or "unknown"}
            for e in enriched if e["valuation"].get("value_base") is None
        ]

        # Per-portfolio exposure targets (Targets tab). Computed from
        # the just-built fund-level rollup. Cash positions are already
        # folded into ``enriched`` above as synthetic entries with
        # asset_class:cash items, so they participate in the rollup
        # and therefore in the deviation calculation automatically;
        # we don't add them again here.
        targets = portfolio_targets_get(p.get("id") or "")
        target_deviations = compute_target_deviations(
            fundlevel_breakdowns, targets)

        return jsonify({
            "portfolio":             p,
            "base_currency":         base_cur,
            "total_value_base":      round(total_base, 2),
            "funds":                 enriched,
            "unvalued_funds":        unvalued_funds,
            # Fund/ETF-level breakdown cards — the six facets aggregated
            # from each fund's data.fund_breakdowns (issuer data + any
            # per-card holdings override), as levelled BLOCKS of the same
            # shape the fund cards read. coverage is per level:
            # fundlevel_coverage[facet][level].
            "fundlevel_breakdowns":  fundlevel_breakdowns,
            "fundlevel_coverage":    fundlevel_coverage,
            # Trading-currency exposure of the fund wrappers themselves
            # (distinct from the look-through currency breakdown).
            "trading_currency_breakdown": trading_currency_breakdown,
            # Targets-tab data: the user's stored targets, plus the
            # per-facet deviation block. ``target_deviations`` is the
            # render-ready shape (see :func:`compute_target_deviations`);
            # ``targets`` is the editable {facet: {key: percent}} dict.
            "targets":               targets,
            "target_deviations":     target_deviations,
        })

    @app.route("/api/unmatched_facets")
    def api_unmatched_facets() -> Response:
        """Flat list of cached holding rows with unresolved facet values.

        Walks every funds-cache file in :data:`FUNDS_DIR` and emits one
        record per row whose ``_unmatched_facets`` stamp is non-empty.
        Each record carries the full holding row (so the dialog can
        render a portfolio-style table with the unmatched cells in red)
        plus a stable ``(fund_isin, row_id)`` key the bulk-apply
        endpoint uses to target the row for rewrite.

        v0.15.11: replaces the per-(facet, value) grouping from
        v0.15.7-10. The grouping prevented the user from seeing
        related mismatches on one row together, and bulk-changing
        across batches blew past the boundaries the user actually
        cared about. Flat rows with sort+filter+select-and-edit on
        the frontend is the right model — same mental model as the
        portfolio holdings table the user already knows.

        Cash positions are NOT walked: the cash editor uses strict
        ``<select>`` inputs for every resource-backed facet, so a
        present-day position can't carry an unresolvable value.

        Response shape::

            {
              "rows": [
                {
                  "fund_isin":  "GB00B...",        # which cache file
                  "row_id":     "abc123",          # which row within
                  "unmatched":  ["sector", "currency"],
                  "name":       "...", "ticker": "...", "isin": "...",
                  "weight_pct": 4.2,
                  "duration": 0, "coupon": 0, "maturity": "",
                  # Every level of every facet tree, finest first,
                  # blank where the tree does not reach.
                  "sub_sector": "beverages", "sector": "consumer defensive",
                  "super_sector": "defensive",
                  "country": "us", "region": "...", "super_region": "...",
                  "currency": "USD",
                  "sub_class": "shares", "asset_class": "...",
                  "super_class": "equity",
                  # The stated value and the grain it was stated at,
                  # for each facet whose tree has more than one level.
                  "sector_node": "beverages", "sector_level": "sub_sector",
                  "country_node": "...", "country_level": "...",
                  "asset_node": "...",  "asset_level": "...",
                  # What the source actually said, and whether the node
                  # above is the user's decision rather than a
                  # resolution of that text.
                  "sector_raw": "Beverages - Non-Alcoholic",
                  "sector_pinned": False,
                  "country_raw": "...", "country_pinned": False,
                  "asset_raw": "...",   "asset_pinned": False,
                  "currency_raw": "...", "currency_pinned": False,
                },
                ...
              ],
              "total":     <number of rows>,
            }
        """
        from porxpy.utils import cache_read

        out_rows: list[dict] = []
        if FUNDS_DIR.exists():
            for fp in FUNDS_DIR.glob("*.json"):
                isin = fp.stem.upper()
                blob = cache_read(isin, "holdings")
                holdings = (blob.get("holdings") or {}).get("value") or {}
                # Every source's rows, not just the one on screen: an
                # unresolved value in the factsheet's list is just as
                # unresolved when the tile happens to be showing Yahoo,
                # and a dialog that only listed the visible source would
                # hide work the user still has to do.
                rows = [r for hb in holdings_blobs_in(holdings)
                        for r in (hb.get("rows") or [])]
                if not rows:
                    continue
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    unmatched = r.get("_unmatched_facets") or []
                    if not unmatched:
                        continue
                    # Skip rows without a stable row_id — they can't be
                    # bulk-edited safely (no key to target). Shouldn't
                    # happen in normal flow since coerce_holdings_row
                    # mints one for every row, but legacy data might.
                    rid = r.get("_row_id") or ""
                    if not rid:
                        continue
                    out = {
                        "fund_isin":      isin,
                        "row_id":         rid,
                        "unmatched":      list(unmatched),
                        "name":           r.get("name") or "",
                        "ticker":         r.get("ticker") or "",
                        "isin":           r.get("isin") or "",
                        "weight_pct":     float(r.get("weight_pct") or 0),
                        "duration":       float(r.get("duration") or 0),
                        "coupon":         float(r.get("coupon") or 0),
                        "maturity":       r.get("maturity") or "",
                    }
                    # Every level of every facet tree, plus the stated
                    # node and the grain it was stated at.
                    #
                    # Until v0.75.2 this sent one column per facet —
                    # whichever the dialog's table happened to display.
                    # The resolve popover now shows the whole chain a
                    # value locked into (beverages -> consumer defensive
                    # -> defensive, with the matched node marked), and it
                    # builds that from the row's level columns. Sending a
                    # subset would have left this dialog inferring what
                    # the fund page reads directly, which is exactly how
                    # the two surfaces come to disagree about what a
                    # value means.
                    for facet, levels in FACET_LEVELS.items():
                        for lv in levels:
                            out[lv] = r.get(lv) or ""
                        # The row's own evidence and whether its node is
                        # a user decision, sent for every facet (v0.76.1).
                        #
                        # Both were missing until now, and the by-row tab
                        # reads both: it offers "add as alias" on the
                        # SOURCE's wording, and it offers to unpin a node
                        # the user set. Without the raw column the alias
                        # box was permanently disabled here while the
                        # fund page's identical popover offered it, and
                        # without the pinned column every selection
                        # reported itself unpinned, so an edit made in
                        # this dialog could not be undone from it.
                        out[facet_raw_field(facet)]    = r.get(facet_raw_field(facet)) or ""
                        out[facet_pinned_field(facet)] = bool(r.get(facet_pinned_field(facet)))
                        if len(levels) < 2:
                            # A single-level facet has no node/level
                            # pair to carry: its one column IS the
                            # stated value, and there is no coarser
                            # grain it could have been stated at.
                            continue
                        out[facet_node_field(facet)]  = r.get(facet_node_field(facet)) or ""
                        out[facet_level_field(facet)] = r.get(facet_level_field(facet)) or ""
                    out_rows.append(out)

        # Sort by weight descending — biggest holdings matter most;
        # frontend can re-sort but this is the right default.
        out_rows.sort(key=lambda x: x["weight_pct"], reverse=True)
        return jsonify({"rows": out_rows, "total": len(out_rows)})

    @app.route("/api/unmatched_values")
    def api_unmatched_values() -> Response:
        """Distinct unresolved facet VALUES across every fund and source.

        **v0.59.0.** The companion of :func:`api_unmatched_facets`,
        which lists holdings *rows*. This lists *values*, because the
        two need different fixes:

        * A row's blank or plainly-wrong value is a fact about that row.
          Edit the row. That is the other endpoint.
        * A spelling the vocabulary has never heard of is a fact about
          the vocabulary. Add the alias once and every occurrence
          everywhere resolves on next read.

        And crucially: a value supplied by Yahoo, by a factsheet
        extraction, or by a breakdown CSV could not be resolved at all
        before this endpoint existed. Those sources have no rows to
        edit — you cannot rewrite what a factsheet said — so the alias
        is the only repair available, and nothing surfaced them.

        Values are grouped by ``(facet, raw)`` with the raw compared
        case-insensitively, since two funds spelling the same junk with
        different capitalisation are one problem, not two.

        Weight is the sum of each occurrence's weight *within its own
        fund*, which makes it a comparable "how much is this costing
        me" number rather than a portfolio-scaled one — the same value
        may sit in funds this portfolio does not hold.

        Response shape::

            {
              "values": [
                {
                  "facet":       "sector",
                  "raw":         "Diversified Holdings",
                  "weight":      1.84,          # summed fund fractions
                  "occurrences": 3,
                  "sources":     ["factsheet", "holdings"],
                  "funds":       [{"isin": "IE00B...", "source": "..."}],
                },
                ...
              ],
              "total": <number of distinct values>,
            }
        """
        from porxpy.utils import cache_read, uploaded_breakdowns_get
        from porxpy.breakdowns import resolve_facet_node, rollup_holdings
        from porxpy.config import BREAKDOWN_FACETS, FACET_LEVELS

        # Levels are not facets. The rollup reports a miss under every
        # level of the tree it belongs to, but a value is one problem
        # and must be listed once, under the FACET, because that is the
        # only name the by-value dropdown knows: the alias-target
        # endpoint 400s on anything outside resources.FACET_ALIAS_LEVELS,
        # and the row then renders "no vocabulary loaded" with no way to
        # resolve it.
        #
        # This was a per-facet special case for country ("region",
        # "super_region") until v0.70.1. Sector and asset gained their
        # full level sets in v0.70.0 and the skip was never widened, so
        # sub_sector, super_sector, sub_class and super_class all leaked
        # through as pseudo-facets.
        _LEVEL_ONLY = {lv for f, lvs in FACET_LEVELS.items()
                       for lv in lvs if lv != f}

        # {(facet, raw_lower): {"raw","weight","occurrences","sources","funds"}}
        agg: dict[tuple[str, str], dict] = {}

        def _note(facet: str, raw: str, weight: float,
                  isin: str, source: str) -> None:
            raw = (raw or "").strip()
            if not raw:
                return
            k = (facet, raw.lower())
            entry = agg.get(k)
            if entry is None:
                # First spelling seen wins as the display form — the
                # user reads what a source actually wrote, not a
                # lowercased version of it.
                entry = agg[k] = {"facet": facet, "raw": raw, "weight": 0.0,
                                  "occurrences": 0, "sources": set(),
                                  "funds": []}
            entry["weight"] += max(0.0, float(weight or 0.0))
            entry["occurrences"] += 1
            entry["sources"].add(source)
            entry["funds"].append({"isin": isin, "source": source})

        if FUNDS_DIR.exists():
            for fp in FUNDS_DIR.glob("*.json"):
                isin = fp.stem.upper()

                # Holdings rows, via the rollup — it already resolves
                # every facet and reports what it could not place, with
                # weights normalised against the whole fund.
                try:
                    blob = cache_read(isin, "holdings")
                    holdings = (blob.get("holdings") or {}).get("value") or {}
                    # Pooled across sources, as in the listing above.
                    rows = [r for hbl in holdings_blobs_in(holdings)
                            for r in (hbl.get("rows") or [])]
                    if rows:
                        hb = rollup_holdings(rows)
                        for facet, misses in (hb.get("unresolved") or {}).items():
                            # "region" is a LEVEL of the country facet,
                            # not a facet of its own: it is derived from
                            # the same column, so an unresolved country
                            # value always produces an identical
                            # unresolved region. Listing both would ask
                            # the user to fix one value twice. The
                            # country entry's dropdown offers region
                            # targets, so nothing is lost.
                            if facet in _LEVEL_ONLY:
                                continue
                            for m in misses or []:
                                _note(facet, m.get("raw"), m.get("weight"),
                                      isin, "holdings")
                        # Row-stamped asset misses. Until v0.70.0 the
                        # holdings-row asset column was a DIFFERENT
                        # vocabulary from the breakdown facet of the same
                        # name, so these were reported under the pseudo-
                        # facets "holding_asset_class" and "sub_class".
                        # v0.70.0 merged both into the one asset tree and
                        # retired those names from FACET_ALIAS_LEVELS,
                        # which left every asset value in this dialog
                        # with no dropdown to resolve it against. They
                        # are one facet now, and are reported as one.
                        for r in rows:
                            if not isinstance(r, dict):
                                continue
                            for f in (r.get("_unmatched_facets") or []):
                                if f in ("asset_class", "sub_class",
                                         "super_class"):
                                    _note("asset_class", r.get(f),
                                          float(r.get("weight_pct") or 0) / 100.0,
                                          isin, "holdings")
                except Exception:
                    pass

                # Yahoo's own sector weightings.
                try:
                    sblob = cache_read(isin, "sectors")
                    sval = (sblob.get("sectors") or {}).get("value") or []
                    if isinstance(sval, list):
                        for it in sval:
                            if not isinstance(it, dict):
                                continue
                            _k, miss = resolve_facet_node(
                                "sector", it.get("sector"))
                            if miss:
                                _note("sector", miss, it.get("weight"),
                                      isin, "yahoo")
                except Exception:
                    pass

                # Yahoo's asset allocation.
                try:
                    ablob = cache_read(isin, "asset_allocation")
                    aval = (ablob.get("asset_allocation") or {}).get("value") or []
                    if isinstance(aval, list):
                        for it in aval:
                            if not isinstance(it, dict):
                                continue
                            _k, miss = resolve_facet_node(
                                "asset_class", it.get("key") or it.get("class"))
                            if miss:
                                _note("asset_class", miss, it.get("weight"),
                                      isin, "yahoo")
                except Exception:
                    pass

                # Factsheet extraction and user CSV, per facet per source.
                try:
                    ub = uploaded_breakdowns_get(isin)
                    for facet in BREAKDOWN_FACETS:
                        per_source = ub.get(facet) or {}
                        if not isinstance(per_source, dict):
                            continue
                        for source, items in per_source.items():
                            for it in items or []:
                                if not isinstance(it, dict):
                                    continue
                                # Same pair breakdowns._resolve_items
                                # reads: ``key`` is a pin the user has
                                # already decided and is not an unmatched
                                # value; ``raw`` is what the source said
                                # and is the only thing worth resolving.
                                # Resolving ``key`` here (the pre-0.76.0
                                # shape) meant no factsheet or upload
                                # value ever reached this dialog.
                                if (it.get("key") or "").strip():
                                    continue
                                _k, miss = resolve_facet_node(
                                    facet, it.get("raw"))
                                if miss:
                                    _note(facet, miss, it.get("weight"),
                                          isin, source)
                except Exception:
                    pass

        out = []
        for entry in agg.values():
            out.append({
                "facet":       entry["facet"],
                "raw":         entry["raw"],
                "weight":      round(entry["weight"], 6),
                "occurrences": entry["occurrences"],
                "sources":     sorted(entry["sources"]),
                "funds":       entry["funds"][:20],
            })
        # Heaviest first: the money decides what is worth resolving, and
        # junk that will never be aliased sinks to the bottom on its own
        # rather than needing a flag to hide it.
        out.sort(key=lambda x: (-x["weight"], x["facet"], x["raw"].lower()))
        return jsonify({"values": out, "total": len(out)})

    def _pin_facet(row: dict, facet: str, canonical: str,
                   clearing: bool = False, unpin: bool = False) -> None:
        """Set a facet on one row as a USER decision, then re-derive it.

        The counterpart of adding an alias, and deliberately a different
        claim: an alias says the vocabulary was incomplete, while this
        says the source was wrong about these particular rows.

        Two things make it a pin rather than a plain write. The row's
        raw text is left exactly as the source gave it — it is evidence,
        and the user overruling it does not make it untrue — and the
        pin column is set so the next normalisation takes the node as
        given instead of resolving the untouched raw text back over the
        top of the edit.

        Clearing is a decision too, and pinned for the same reason: "this
        row has no sector" must survive a re-read, which it would not if
        an empty node simply fell back to resolving the raw again.

        ``unpin`` is the reverse: it drops the pin and lets the raw text
        resolve again. It exists because a pin with no way out is a
        one-way door — a user who corrects a row and later fixes the
        vocabulary properly should be able to hand the row back to its
        source, and only the row itself knows it was ever overruled.

        The level columns are not written here. normalise_facets derives
        every one of them from the node, which is what stops them
        drifting out of agreement with it — the bug the four hand-written
        per-facet branches that used to sit here produced twice.
        """
        from porxpy.utils import normalise_facets
        if unpin:
            # Hand the row back to its source. The node is not cleared —
            # normalise_facets is about to rewrite it from the raw text —
            # and the raw was never touched by the pin, so this restores
            # exactly what the source said with no round-trip to it.
            row.pop(facet_pinned_field(facet), None)
        else:
            row[facet_node_field(facet)]   = "" if clearing else canonical
            row[facet_pinned_field(facet)] = True
        normalise_facets(row)

    @app.route("/api/unmatched_values/apply_rows", methods=["POST"])
    def api_unmatched_values_apply_rows() -> Response:
        """Rewrite every holdings ROW carrying one raw value (v0.70.2).

        The second of the two operations the by-value tab offers, and
        the counterpart of :func:`api_facet_alias`. The choice matters
        because the two make different claims:

        * **Add alias** says the vocabulary was incomplete. The raw text
          was a legitimate name for a canonical node all along, so it is
          written to the resource file and every occurrence everywhere
          resolves on next read, including occurrences in sources that
          have no rows at all.
        * **Change the rows** says the source was wrong. The text is not
          a name for anything; these particular rows should simply hold
          a different value. Nothing is written to the resource file, so
          the next import of the same spelling is unresolved again —
          which is correct, because nothing has been learned about the
          vocabulary.

        Body::

            {"facet": "asset_class", "canonical": "regular stock",
             "raw": "Mystery Tranche"}

        Only rows in the holdings cache are touched. A value that also
        came from Yahoo, a factsheet or an uploaded breakdown CSV is
        left alone there: those have no row to edit, which is exactly
        why the alias exists, and the response reports the shortfall
        rather than implying a complete fix.
        """
        from porxpy.utils import cache_read, cache_write
        from porxpy.resources import FACET_ALIAS_LEVELS
        from porxpy.config import (
            FACET_LEVELS, facet_node_field, facet_raw_field,
        )

        body      = request.get_json(force=True, silent=True) or {}
        facet     = (body.get("facet") or "").strip().lower()
        canonical = (body.get("canonical") or "").strip()
        raw       = (body.get("raw") or "").strip()

        # ``clear`` asks for the facet to be emptied on the matching
        # rows rather than set to something. It is a separate flag rather
        # than an empty ``canonical`` so that an accidentally-blank
        # canonical still fails loudly instead of silently wiping every
        # row carrying the raw value.
        clearing = bool(body.get("clear"))
        # "Give these rows back to their source": no canonical needed,
        # because the answer is whatever the raw text resolves to.
        unpin = bool(body.get("unpin"))

        if facet not in FACET_ALIAS_LEVELS:
            return jsonify({"error": f"unknown facet {facet!r}"}), 400
        if not raw:
            return jsonify({"error": "raw is required"}), 400
        if not canonical and not clearing and not unpin:
            return jsonify({
                "error": "canonical is required unless clearing or unpinning"
            }), 400
        if clearing:
            canonical = ""

        # Where a raw value for this facet can be sitting. Every level
        # column, plus the stated-node pair, because which one holds the
        # text depends on the grain the source wrote it at. Asset's node
        # pair is `asset_node` / `asset_level`, not `asset_class_node` —
        # the tree is named for the facet, the columns for the concept.
        # Where a raw value for this facet can be sitting. Every level
        # column, the stated node, and — since v0.76.0 — the raw column,
        # which is where an unresolved value now lives exclusively: the
        # level columns are blanked when nothing resolves, so a search
        # that skipped the raw would match none of the rows the by-value
        # tab is offering to fix.
        fields = list(FACET_LEVELS.get(facet) or (facet,))
        fields += [facet, facet_node_field(facet), facet_raw_field(facet)]

        rows_written = files_written = 0
        errors: list[dict] = []
        target = raw.lower()

        if FUNDS_DIR.exists():
            for fp in sorted(FUNDS_DIR.glob("*.json")):
                isin = fp.stem.upper()
                try:
                    blob = cache_read(isin, "holdings")
                except Exception:
                    continue
                holdings = (blob.get("holdings") or {}).get("value") or {}
                # Every source's rows. holdings_blobs_in hands back the
                # stored dicts themselves, so the in-place edits below
                # land in the blob this function writes back.
                rows = [r for hb in holdings_blobs_in(holdings)
                        for r in (hb.get("rows") or [])]
                if not rows:
                    continue

                touched = False
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if not any((row.get(f) or "").strip().lower() == target
                               for f in fields):
                        continue
                    _pin_facet(row, facet, canonical, clearing, unpin)
                    rows_written += 1
                    touched = True

                if touched:
                    blob.pop("_normalisation", None)
                    try:
                        cache_write(isin, "holdings", blob)
                        files_written += 1
                    except Exception as exc:
                        errors.append({"fund_isin": isin,
                                       "error": f"cache_write: {exc}"})

        return jsonify({
            "ok":            not errors,
            "facet":         facet,
            "raw":           raw,
            "canonical":     canonical,
            "rows_written":  rows_written,
            "files_written": files_written,
            "errors":        errors,
        })

    # -- Bundles: pre-loaded fund sets and portfolio backups ----------

    @app.route("/api/bundles/funds/export", methods=["POST"])
    def api_bundle_export_funds() -> Response:
        """Download a pre-loaded fund bundle.

        Body: ``{isins?: [...], include_price_history?: bool,
        include_factsheets?: bool}``. Omitting ``isins`` exports every
        cached fund.
        """
        from porxpy.bundles import export_funds
        body = request.get_json(force=True, silent=True) or {}
        data = export_funds(
            isins=body.get("isins"),
            include_price_history=bool(body.get("include_price_history")),
            include_factsheets=body.get("include_factsheets", True))
        stamp = datetime.now().strftime("%Y%m%d")
        return Response(
            data, mimetype="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="porxpy_funds_{stamp}.zip"'})

    @app.route("/api/bundles/portfolios/export", methods=["POST"])
    def api_bundle_export_portfolios() -> Response:
        """Download a portfolio backup (portfolios, targets, cash, settings)."""
        from porxpy.bundles import export_portfolios
        stamp = datetime.now().strftime("%Y%m%d")
        return Response(
            export_portfolios(), mimetype="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="porxpy_portfolios_{stamp}.zip"'})

    @app.route("/api/bundles/inspect", methods=["POST"])
    def api_bundle_inspect() -> Response:
        """Report what a bundle holds and what it would collide with.

        Writes nothing. The two-phase import exists because the conflict
        decisions can only be made against a list of what actually
        collides, and that list needs the bundle read against this
        install.
        """
        from porxpy.bundles import inspect_bundle
        f = request.files.get("file")
        data = f.read() if f else request.get_data()
        if not data:
            return jsonify({"error": "no bundle uploaded"}), 400
        return jsonify(inspect_bundle(data))

    @app.route("/api/bundles/apply", methods=["POST"])
    def api_bundle_apply() -> Response:
        """Write a bundle in, honouring the caller's conflict decisions.

        Multipart: ``file`` plus a ``decisions`` JSON field of
        ``{fund_actions: {ISIN: "skip"|"overwrite"}, default_fund_action,
        resource_action}``.
        """
        from porxpy.bundles import apply_bundle
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "no bundle uploaded"}), 400
        try:
            decisions = json.loads(request.form.get("decisions") or "{}")
        except Exception:
            decisions = {}
        report = apply_bundle(
            f.read(),
            fund_actions=decisions.get("fund_actions") or {},
            default_fund_action=decisions.get("default_fund_action") or "skip",
            resource_action=decisions.get("resource_action") or "merge_aliases")
        return jsonify(report)

    @app.route("/api/resources/facet_alias_targets/<facet>")
    def api_facet_alias_targets(facet: str) -> Response:
        """Level-tagged canonical vocabulary a raw value may be aliased to.

        The dropdown behind the by-value tab. Entries carry their level
        because "AI Hype" mapped to technology is a different claim from
        "AI Hype" mapped to semiconductors: the first says the source
        told us the sector, the second says it told us the sub sector,
        and only the second can ever answer a sub-sector question.
        """
        from porxpy.resources import facet_alias_targets, FACET_ALIAS_LEVELS
        f = (facet or "").strip().lower()
        if f not in FACET_ALIAS_LEVELS:
            return jsonify({"error": f"unknown facet {facet!r}"}), 400
        return jsonify({
            "facet":   f,
            "levels":  list(FACET_ALIAS_LEVELS[f]),
            "targets": facet_alias_targets(f),
        })

    @app.route("/api/resources/facet_alias", methods=["POST"])
    def api_facet_alias() -> Response:
        """Write one alias to a resource file. The by-value tab's apply.

        Body::

            {"facet": "sector", "level": "sub_sector",
             "canonical": "semiconductors", "raw": "AI Hype"}

        Nothing else happens — no cache is rewritten, no row is touched.
        The resource file's version bump is what repairs history: every
        cache stamped with the old version re-normalises on next read,
        and every breakdown re-resolves on every read regardless. That
        is the whole mechanism, and it is why this endpoint can be
        honest about failure where the row-rewrite path could not: an
        alias that did not get written here fixed nothing at all, so a
        silent failure would be a lie.
        """
        from porxpy.resources import (
            add_facet_alias, facet_alias_claims, facet_alias_conflict,
            FACET_ALIAS_LEVELS,
        )

        body      = request.get_json(force=True, silent=True) or {}
        facet     = (body.get("facet") or "").strip().lower()
        level     = (body.get("level") or "").strip().lower()
        canonical = (body.get("canonical") or "").strip()
        raw       = (body.get("raw") or "").strip()

        if facet not in FACET_ALIAS_LEVELS:
            return jsonify({"error": f"unknown facet {facet!r}"}), 400
        if level not in FACET_ALIAS_LEVELS[facet]:
            return jsonify({
                "error": f"level {level!r} is not one of "
                         f"{list(FACET_ALIAS_LEVELS[facet])} for {facet!r}"
            }), 400
        if not canonical or not raw:
            return jsonify({"error": "canonical and raw are both required"}), 400

        # Refuse before writing, with the reason, rather than after
        # with a shrug. The most common refusal — the text is itself a
        # canonical — is one a user can act on only if the message says
        # so, because the write would otherwise "succeed" and change
        # nothing: a canonical always outranks an alias at load.
        why = facet_alias_conflict(facet, level, canonical, raw)
        if why:
            return jsonify({"error": why}), 409

        # What this write will take away from somebody else, read before
        # it happens. A token means one node, so re-pointing it is a
        # move; the response says which node lost it so the change is
        # visible rather than inferred from a console warning at load.
        claims = [c for c in facet_alias_claims(facet, raw)
                  if not c["wildcard"]
                  and c["canonical"].strip().lower() != canonical.strip().lower()]
        wild = [c for c in facet_alias_claims(facet, raw) if c["wildcard"]]

        try:
            ok = add_facet_alias(facet, level, canonical, raw)
        except Exception as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

        if not ok:
            return jsonify({
                "error": f"could not add {raw!r} to {canonical!r} — either "
                         f"the canonical does not exist at {level} level, or "
                         f"the alias is already present."
            }), 409
        return jsonify({"ok": True, "facet": facet, "level": level,
                        "canonical": canonical, "raw": raw,
                        "moved_from": [{"canonical": c["canonical"],
                                        "level": c["level"]} for c in claims],
                        # Reported, never moved: re-pointing a pattern
                        # would re-classify every value it covers.
                        "wildcards_left": [{"canonical": c["canonical"],
                                            "token": c["token"]} for c in wild]})

    def _rewrite_asset_node(raw_asset_class: str, raw_value: str,
                            canonical: str) -> int:
        """Repoint stored rows whose asset value the user has just mapped.

        The alias makes the raw spelling resolvable, but rows already in
        the cache still carry it as their stated node with no level, so
        nothing would change until each was re-read. This rewrites the
        node to the canonical the user chose and clears the derived
        levels, leaving the next normalisation to fill them.

        Until v0.70.0 this rewrote a PAIR — asset_class and sub_class
        together — and had to look up which asset class a sub class
        belonged to in order to keep the two consistent. One tree, one
        stated value: there is no pair to keep consistent.

        Args:
            raw_asset_class: Optional scope — only rows whose asset
                column also matched this raw value are touched.
            raw_value: The raw spelling the user mapped.
            canonical: The node they mapped it to.

        Returns:
            Number of rows rewritten across all fund-cache files.
        """
        from porxpy.resources import ASSET_LEVEL_OF
        from porxpy.utils import cache_read, cache_write

        target = (canonical or "").strip().lower()
        if target not in ASSET_LEVEL_OF:
            return 0

        needle    = (raw_value or "").strip().lower()
        needle_ac = (raw_asset_class or "").strip().lower()
        n_rewrites = 0
        if FUNDS_DIR.exists():
            for fp in FUNDS_DIR.glob("*.json"):
                isin = fp.stem.upper()
                blob = cache_read(isin, "holdings")
                holdings = (blob.get("holdings") or {}).get("value") or {}
                rows = [r for hb in holdings_blobs_in(holdings)
                        for r in (hb.get("rows") or [])]
                if not rows:
                    continue
                touched = False
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    node = (row.get("asset_node") or "").strip().lower()
                    if node != needle:
                        continue
                    # Empty needle_ac = match any row carrying the raw
                    # value (legacy safety hatch); otherwise scope it.
                    if needle_ac and (row.get("asset_class")
                                      or "").strip().lower() != needle_ac:
                        continue
                    if node == target:
                        continue
                    row["asset_node"]  = target
                    row["asset_level"] = ""
                    row["asset_class"] = ""
                    row["sub_class"]   = ""
                    row["super_class"] = ""
                    row.pop("_unmatched_facets", None)
                    touched = True
                    n_rewrites += 1
                if touched:
                    blob.pop("_normalisation", None)
                    cache_write(isin, "holdings", blob)
        return n_rewrites

    @app.route("/api/resources/resolve", methods=["POST"])
    def api_resources_resolve() -> Response:
        """Apply user-decided alias mappings to the resource CSVs.

        Body::

            {
              "resolutions": [
                {"facet": "sector",
                 "raw":   "AI Hype",
                 "canonical": "technology"},
                ...
              ]
            }

        For each entry, the raw value is added as a new alias to the
        named canonical row in the appropriate CSV. The file's
        ``Version=N`` line is bumped on every successful write so
        cache stamps detect the change and re-normalise lazily.

        Returns the new versions and a per-row outcome list.
        """
        from porxpy.resources import (
            add_facet_alias, add_sector_alias, add_currency_alias,
            add_country_alias, country_to_mstar,
        )
        body = request.get_json(force=True, silent=True) or {}
        resolutions = body.get("resolutions") or []
        if not isinstance(resolutions, list):
            return jsonify({"error": "resolutions must be a list"}), 400

        results = []
        for entry in resolutions:
            if not isinstance(entry, dict):
                continue
            facet = (entry.get("facet") or "").strip()
            raw   = (entry.get("raw") or "").strip()
            canon = (entry.get("canonical") or "").strip()
            # For sub_class resolutions the frontend sends raw_ac too —
            # the asset_class side of the (raw_ac, raw_sub) pair the
            # user is resolving. Lets the rewrite step scope itself to
            # just the rows the user picked, not every row sharing
            # the raw sub_class.
            raw_ac = (entry.get("raw_ac") or "").strip()
            if not facet or not raw or not canon:
                results.append({**entry, "status": "skipped",
                                "reason": "missing field"})
                continue
            try:
                if facet == "sector":
                    ok = add_sector_alias(canon, raw)
                elif facet in ("sub_class", "asset_class",
                               "holding_asset_class"):
                    # One tree: the LEVEL comes from the node the user
                    # chose, not from which of three pseudo-facets the
                    # dialog happened to label the row with.
                    from porxpy.resources import (ASSET_LEVEL_OF,
                                                  add_facet_alias)
                    lv = ASSET_LEVEL_OF.get(canon.strip().lower(), "")
                    ok = bool(lv) and add_facet_alias("asset_class", lv,
                                                      canon, raw)
                    # Even when the alias is added successfully, rows
                    # whose asset_class doesn't match the chosen
                    # canonical's group will still fail the pair-check
                    # in normalise_facets and stay unmatched. Rewrite
                    # those rows' asset_class to the canonical's group
                    # so the next lazy migration finds a consistent
                    # pair. Done regardless of whether the alias was
                    # newly added or already existed — a duplicate
                    # alias add still means the user told us where
                    # this raw value belongs, and any cached rows that
                    # are out-of-group should be fixed too.
                    n_rewrites = _rewrite_asset_node(raw_ac, raw, canon)
                    results.append({
                        **entry,
                        "status":   "applied" if ok else "duplicate_or_missing",
                        "rewrites": n_rewrites,
                    })
                    continue
                elif facet == "currency":
                    ok = add_currency_alias(canon, raw)
                elif facet == "country":
                    # v0.15.9: country_codes.csv now has a matches
                    # column and version line on equal footing with
                    # the other resource files; aliases edit through
                    # add_country_alias just like the others.
                    ok = add_country_alias(canon, raw)
                else:
                    results.append({**entry, "status": "skipped",
                                    "reason": f"unknown facet '{facet}'"})
                    continue
                results.append({
                    **entry,
                    "status": "applied" if ok else "duplicate_or_missing",
                })
            except Exception as exc:
                results.append({**entry, "status": "error", "reason": str(exc)})

        return jsonify({"results": results})


    @app.route("/api/resources/reload", methods=["POST"])
    def api_resources_reload() -> Response:
        """Force a re-read of every resource CSV.

        Editing a resource file normally takes effect on the next
        request by itself. This exists for the case the automatic check
        cannot cover: a file restored from backup with an older
        timestamp, or a mount whose mtime is not to be trusted.

        Cached funds carry a fingerprint of the resource files they were
        normalised against, so the re-evaluation of already-imported
        data happens lazily on next read rather than being forced here —
        rewriting every cached fund up front would be a long blocking
        operation to no end.

        Returns:
            ``{reloaded, versions, fingerprints, changed}``.
        """
        from porxpy.resources import (
            reload_all_resources, resources_changed_on_disk,
            RESOURCE_FINGERPRINTS,
        )
        changed = resources_changed_on_disk()
        reload_all_resources()
        return jsonify({
            "reloaded":     True,
            "changed":      changed,
            "fingerprints": dict(RESOURCE_FINGERPRINTS),
        })


    @app.route("/api/unmatched_facets/apply", methods=["POST"])
    def api_unmatched_facets_apply() -> Response:
        """Bulk-rewrite cached holding rows to canonical facet values.

        v0.15.11 supersedes the per-(facet, value) resolve flow with a
        flat per-row model: the user picks a set of rows in the dialog
        and applies canonical values column-by-column. This endpoint
        is the bulk writer behind that flow.

        Body::

            {
              "updates": [
                {
                  "fund_isin": "GB00B...",     # which cache file
                  "row_id":    "abc123",       # which row within
                  "sets": {                    # one or more facet picks
                    "sector":    "technology",
                    "sub_class": "shares",     # asset_class derived
                    "country":   "unitedstates",
                    "currency":  "USD",
                  }
                },
                ...
              ],
              "add_aliases": true             # default true — for each
                                              # (facet, set_value) and the
                                              # row's PRE-EDIT raw value,
                                              # add the alias so future
                                              # imports of the same raw
                                              # resolve cleanly
            }

        Behaviour per update:
          - sub_class set → also derives asset_class from
            the asset tree and writes the stated node, so the row
            is in-group.
          - asset_class set without sub_class → write asset_class
            directly. (Rare — the dialog usually edits sub_class.)
          - Other facets → write the field directly.
          - The row's _unmatched_facets stamp is dropped; if other
            facets remain unmatched, the next cache_read's lazy
            migration will re-stamp accordingly.
          - File-level _normalisation stamp dropped so the migration
            re-runs on next read.

        Returns counts of rows written and aliases added per facet.
        """
        from porxpy.utils import cache_read, cache_write
        from porxpy.resources import (
            add_sector_alias, add_currency_alias,
            add_country_alias, add_facet_alias,
        )

        body = request.get_json(force=True, silent=True) or {}
        # "Give these rows back to their source" — drop the pin and let
        # the raw text resolve again. A flag rather than a magic value in
        # `sets`, so an accidentally-blank value still reads as a clear
        # (a decision) and never as an un-decision.
        unpin = bool(body.get("unpin"))
        updates = body.get("updates") or []
        add_aliases = bool(body.get("add_aliases", True))
        if not isinstance(updates, list) or not updates:
            return jsonify({"error": "updates list is required"}), 400

        # Group updates by fund_isin so each cache opens once.
        by_isin: dict[str, list[dict]] = {}
        for u in updates:
            isin = (u.get("fund_isin") or "").strip().upper()
            rid  = (u.get("row_id") or "").strip()
            sets = u.get("sets") or {}
            if not isin or not rid or not isinstance(sets, dict) or not sets:
                continue
            by_isin.setdefault(isin, []).append({"row_id": rid, "sets": sets})

        # Collect (facet, raw_value, canonical) triples seen across
        # updates for the alias-add pass. Deduped per (facet, raw).
        # Stored as {(facet, raw_lower) → canonical}.
        alias_candidates: dict[tuple[str, str], str] = {}

        rows_written = 0
        files_written = 0
        errors: list[dict] = []

        for isin, ulist in by_isin.items():
            try:
                blob = cache_read(isin, "holdings")
            except Exception as exc:
                errors.append({"fund_isin": isin, "error": f"cache_read: {exc}"})
                continue
            holdings = (blob.get("holdings") or {}).get("value") or {}
            if not isinstance(holdings, dict):
                errors.append({"fund_isin": isin, "error": "no holdings block"})
                continue
            # Pooled across sources: a row_id addresses one row wherever
            # it is stored, and the Resolve dialog offers rows from every
            # source, so the lookup below has to be able to find them.
            rows = [r for hb in holdings_blobs_in(holdings)
                    for r in (hb.get("rows") or [])]
            if not rows:
                errors.append({"fund_isin": isin, "error": "no holdings rows"})
                continue
            # Build a row_id → row index for fast targeting.
            idx_by_rid: dict[str, int] = {}
            for i, r in enumerate(rows):
                if isinstance(r, dict):
                    rid = r.get("_row_id") or ""
                    if rid:
                        idx_by_rid[rid] = i

            touched = False
            for u in ulist:
                idx = idx_by_rid.get(u["row_id"])
                if idx is None:
                    errors.append({"fund_isin": isin, "row_id": u["row_id"],
                                   "error": "row_id not found"})
                    continue
                row = rows[idx]
                sets = u["sets"]
                # Capture pre-edit raw values for the alias-add pass.
                # Only non-empty pre-edit values become alias
                # candidates (an empty raw can't be aliased).
                for facet, new_val in sets.items():
                    if facet not in ("sector", "sub_class", "country",
                                     "currency", "asset_class"):
                        continue
                    new_val = (new_val or "").strip()
                    # An empty value CLEARS the facet on these rows.
                    #
                    # It used to `continue`, so a caller asking to empty a
                    # facet was silently ignored. The UI worked around that
                    # by sending the literal "__undefined__", which no
                    # branch here recognised — so "clear the value" wrote
                    # the string `__undefined__` into the row and it came
                    # back as an unmatched value of its own. Naming the
                    # facet in `sets` IS the request to set it; setting it
                    # to nothing is clearing it, and that is now what
                    # happens.
                    clearing = not new_val
                    if not clearing:
                        raw = (row.get(facet_raw_field(
                            "asset_class" if facet in ("sub_class", "asset_class")
                            else facet)) or "").strip().lower()
                        if raw and add_aliases:
                            alias_candidates[(facet, raw)] = new_val
                    # One write for all four facets (v0.76.0). The
                    # per-facet branches that stood here — an asset pair,
                    # a currency upper(), a node/level pop for sector and
                    # country only — were four spellings of "the user
                    # decided this row means X", and each carried its own
                    # way of stopping the next normalisation reverting the
                    # edit. _pin_facet does it once: set the node, mark it
                    # a decision, re-derive the levels from it.
                    #
                    # The alias candidate above still reads the row's raw
                    # text, because teaching the vocabulary is a claim
                    # about the SOURCE's wording, not about the node the
                    # user picked.
                    _pin_facet(row, "asset_class" if facet in
                               ("sub_class", "asset_class") else facet,
                               new_val, clearing, unpin)
                # Drop the row's stale unmatched-stamp; the lazy
                # migration on next read will re-stamp if any facets
                # are still unmatched.
                row.pop("_unmatched_facets", None)
                rows_written += 1
                touched = True

            if touched:
                # Drop the file-level normalisation stamp so the next
                # cache_read re-runs the migration to clean up the
                # row's _unmatched_facets list.
                blob.pop("_normalisation", None)
                try:
                    cache_write(isin, "holdings", blob)
                    files_written += 1
                except Exception as exc:
                    errors.append({"fund_isin": isin,
                                   "error": f"cache_write: {exc}"})

        # Alias-add pass. Each (facet, raw) is tried at most once.
        # Failures (duplicate / canonical-not-found) are silent — the
        # row rewrites already happened, the alias is a bonus.
        aliases_added: dict[str, int] = {
            "sector": 0, "sub_class": 0, "country": 0, "currency": 0,
            "asset_class": 0,
        }
        if add_aliases:
            for (facet, raw), canon in alias_candidates.items():
                if raw == canon.lower():
                    continue   # raw already equals canonical
                try:
                    if facet == "sector":
                        if add_sector_alias(canon, raw):
                            aliases_added["sector"] += 1
                    elif facet in ("sub_class", "holding_asset_class"):
                        from porxpy.resources import ASSET_LEVEL_OF
                        lv = ASSET_LEVEL_OF.get(canon.strip().lower(), "")
                        if lv and add_facet_alias("asset_class", lv, canon, raw):
                            aliases_added["sub_class"] += 1
                    elif facet == "currency":
                        if add_currency_alias(canon, raw):
                            aliases_added["currency"] += 1
                    elif facet == "country":
                        if add_country_alias(canon, raw):
                            aliases_added["country"] += 1
                    elif facet == "asset_class":
                        from porxpy.resources import ASSET_LEVEL_OF
                        lv = ASSET_LEVEL_OF.get(canon.strip().lower(), "")
                        if lv and add_facet_alias("asset_class", lv, canon, raw):
                            aliases_added["asset_class"] += 1
                except Exception:
                    pass

        return jsonify({
            "rows_written":  rows_written,
            "files_written": files_written,
            "aliases_added": aliases_added,
            "errors":        errors,
        })


    @app.route("/api/portfolios/<pid>/holdings_rollup")
    def api_portfolio_holdings_rollup(pid: str) -> Response:
        """Aggregated portfolio-level holdings list.

        Walks every fund's holdings cache and merges positions for the
        same underlying holding (matched on the app-level
        ``holdings_match.key`` setting — name / ticker / isin) into one
        row, with that row's weight expressed against the whole
        portfolio. A synthetic "Unclassified" row absorbs the portfolio
        value not covered by real holdings so the Portfolio Value column
        reconciles to the total. The actual aggregation is a pure
        function — :func:`porxpy.breakdowns.aggregate_portfolio_holdings`.

        Lazy by design: the frontend fetches this only when the Holdings
        sub-tab is opened, so a plain portfolio view never pays the
        aggregation cost.

        Query parameters:
            refresh: ``1`` to force live refresh on every fund.

        Returns:
            The dict from ``aggregate_portfolio_holdings`` plus
            ``base_currency``.
        """
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404

        cache_cfg = normalise_cache_config(p.get("cache_config"))
        force     = request.args.get("refresh") == "1"
        base_cur  = (p.get("base_currency") or "USD").upper()

        enriched, total_base = _build_enriched_funds(
            p, cache_cfg, force, base_cur)

        match_key = (load_settings().get("holdings_match") or {}).get("key", "ticker")
        result = aggregate_portfolio_holdings(enriched, total_base, match_key)
        result["base_currency"] = base_cur
        return jsonify(result)

    @app.route("/api/portfolios/<pid>/price_history")
    def api_portfolio_price_history(pid: str) -> Response:
        """Per-fund and portfolio-total daily values in the base currency.

        Pre-fetches FX history for each non-base currency seen, builds
        per-fund date→value maps, then aligns everything on the union of
        dates with carry-forward for missing days. Pre-inception dates
        for each fund are back-filled with that fund's first recorded
        base value (so the portfolio total covers the entire date range
        any fund covers, rather than dropping early funds when others
        haven't started trading).

        Query parameters:
            refresh: ``1`` to force live refresh on every underlying load.
        """
        p = find_portfolio(pid)
        if not p:
            return jsonify({"error": "portfolio not found"}), 404

        cache_cfg = normalise_cache_config(p.get("cache_config"))
        force     = request.args.get("refresh") == "1"
        base_cur  = (p.get("base_currency") or "USD").upper()

        fx_series_cache: dict[str, dict[str, float]] = {}

        def get_fx_series(cur: str) -> dict[str, float]:
            """Memoise FX history per currency for the duration of this request."""
            key = f"{cur}->{base_cur}"
            if key not in fx_series_cache:
                fx_series_cache[key] = fx_history(cur, base_cur)
            return fx_series_cache[key]

        # Pass 1: compute each fund's per-date base-currency value
        per_fund: list[dict] = []
        warnings: list[dict] = []

        for f in p.get("funds", []):
            ticker_q = (f.get("ticker") or "").strip().upper()
            if not ticker_q:
                continue

            shares_raw = f.get("shares")
            try:
                shares = float(shares_raw) if shares_raw is not None else None
            except (TypeError, ValueError):
                shares = None
            if shares is None or shares <= 0:
                warnings.append({"ticker": ticker_q, "reason": "no shares set"})
                continue

            # Identity from the listings cache — portfolios are slim in
            # 0.12.0+. A missing identity means the listings cache was
            # purged; the fund can't be loaded without a refetch.
            ident = listing_identity_get(ticker_q)
            isin     = (ident.get("isin")     or "").strip().upper()
            exchange = (ident.get("exchange") or "").strip().upper() or None
            if not isin:
                warnings.append({"ticker": ticker_q,
                                 "reason": "needs refetch (no cached identity)"})
                continue

            try:
                # Portfolio members are already saved by definition;
                # commit=True is the default but stated explicitly.
                data = load_fund_data(
                    isin, exchange, cache_cfg,
                    force_refresh=force, known_ticker=ticker_q,
                    commit=True,
                )
            except Exception as exc:
                warnings.append({"ticker": ticker_q,
                                 "reason": f"fetch error: {exc}"})
                continue

            ph = data.get("price_history") or []
            if not ph:
                warnings.append({"ticker": ticker_q, "reason": "no price history"})
                continue

            native_cur         = (data.get("profile") or {}).get("currency") or ""
            canon_cur, divisor = normalise_currency(native_cur)
            effective_cur      = (canon_cur or base_cur).upper()

            rates        = {} if effective_cur == base_cur else get_fx_series(effective_cur)
            sorted_rates = sorted(rates.keys()) if rates else []
            last_rate    = None
            rate_idx     = 0

            date_to_val: dict[str, float] = {}
            for row in ph:
                d     = row["date"]
                close = row.get("close")
                if close is None:
                    continue

                if effective_cur == base_cur:
                    rate = 1.0
                else:
                    while rate_idx < len(sorted_rates) and sorted_rates[rate_idx] <= d:
                        last_rate = rates[sorted_rates[rate_idx]]
                        rate_idx += 1
                    rate = last_rate
                    if rate is None:
                        continue   # no FX data yet at this date; skip

                v = (close / divisor) * shares * rate
                if math.isnan(v) or math.isinf(v):
                    continue
                date_to_val[d] = v

            if not date_to_val:
                warnings.append({"ticker": ticker_q,
                                 "reason": "no overlap with FX series"})
                continue

            prof = data.get("profile") or {}
            per_fund.append({
                "ticker":          ticker_q,
                "isin":            isin,
                "name":            prof.get("longName") or prof.get("shortName")
                                   or ticker_q,
                "currency_native": effective_cur,
                "shares":          shares,
                "locked":          bool(f.get("locked")),
                "date_to_val":     date_to_val,
                "is_cash":         False,
            })

        # ──────────────────────────────────────────────────────────────
        # Cash positions (v0.14.0). Build a date→value map per cash
        # position using continuous compounding from its effective_date.
        # The "date axis" is the union of dates already produced by
        # funds; for a cash-only portfolio, or for a position whose
        # effective_date predates every fund's inception, we generate
        # a sparse monthly axis instead — the chart still draws a
        # continuous line because alignment in Pass 2 carries forward.
        # ──────────────────────────────────────────────────────────────
        from porxpy.utils import cash_positions_get
        from porxpy.breakdowns import cash_position_value_on_date
        cash_positions = cash_positions_get(pid) if pid else []
        if cash_positions:
            # Union of fund dates so far (might be empty in a cash-only
            # portfolio).
            fund_dates: set[str] = set()
            for fund in per_fund:
                fund_dates.update(fund["date_to_val"].keys())

            for pos in cash_positions:
                pos_cur = (pos.get("currency") or "").upper()
                principal = float(pos.get("amount") or 0.0)
                if principal == 0:
                    continue   # nothing to draw

                # FX series for this position's currency (empty when
                # already in base).
                rates = ({} if pos_cur == base_cur
                         else get_fx_series(pos_cur))
                sorted_rates = sorted(rates.keys()) if rates else []
                last_rate    = None
                rate_idx     = 0

                # Date axis: prefer the existing fund dates so the chart
                # aligns across all series. If we have no fund dates,
                # generate a monthly axis from the position's
                # effective_date (or 5 years back if none) up to today.
                eff_raw = (pos.get("effective_date") or "").strip()
                from datetime import datetime, timedelta
                today_iso = datetime.utcnow().date().isoformat()
                if fund_dates:
                    pos_dates = sorted(fund_dates) + [today_iso]
                    # dedupe + sort
                    pos_dates = sorted(set(pos_dates))
                else:
                    # Cash-only portfolio: monthly axis. Start from the
                    # earliest effective_date across all positions or
                    # 5 years back, whichever is earlier.
                    start = None
                    if eff_raw:
                        try:
                            start = datetime.strptime(
                                eff_raw, "%d/%b/%Y").date()
                        except ValueError:
                            start = None
                    if start is None:
                        start = (datetime.utcnow().date()
                                 - timedelta(days=5*365))
                    pos_dates = []
                    d = start
                    end = datetime.utcnow().date()
                    while d <= end:
                        pos_dates.append(d.isoformat())
                        # ~monthly step
                        d = d + timedelta(days=30)
                    if pos_dates[-1] != end.isoformat():
                        pos_dates.append(end.isoformat())

                date_to_val: dict[str, float] = {}
                for d in pos_dates:
                    # Accrued value in position currency on this date.
                    v_native = cash_position_value_on_date(pos, d)
                    if v_native <= 0:
                        continue

                    if pos_cur == base_cur:
                        rate = 1.0
                    else:
                        while (rate_idx < len(sorted_rates)
                               and sorted_rates[rate_idx] <= d):
                            last_rate = rates[sorted_rates[rate_idx]]
                            rate_idx += 1
                        rate = last_rate
                        if rate is None:
                            continue
                    v = v_native * rate
                    if math.isnan(v) or math.isinf(v):
                        continue
                    date_to_val[d] = v

                if not date_to_val:
                    warnings.append({
                        "ticker": f"cash:{pos.get('id', '')}",
                        "reason": "no FX data covers this date range",
                    })
                    continue

                per_fund.append({
                    "ticker":          f"cash:{pos.get('id', '')}",
                    "isin":            "",
                    "name":            (pos.get("name") or "(cash position)"),
                    "currency_native": pos_cur,
                    "shares":          None,
                    "date_to_val":     date_to_val,
                    "is_cash":         True,
                })

        if not per_fund:
            return jsonify({
                "base_currency": base_cur, "dates": [], "funds": [],
                "portfolio_values": [], "warnings": warnings,
            })

        # Pass 2: align everything on the union of dates
        all_dates: set[str] = set()
        for fund in per_fund:
            all_dates.update(fund["date_to_val"].keys())
        dates_sorted = sorted(all_dates)

        out_funds = []
        portfolio_values = [0.0] * len(dates_sorted)

        for fund in per_fund:
            d2v = fund["date_to_val"]
            fund_dates_sorted = sorted(d2v.keys())
            inception = fund_dates_sorted[0]
            first_val = d2v[inception]

            values: list[float] = []
            last_seen = first_val
            for d in dates_sorted:
                if d < inception:
                    v = first_val
                else:
                    v = d2v.get(d, last_seen)
                    last_seen = v
                values.append(round(v, 4))

            for i, v in enumerate(values):
                portfolio_values[i] += v

            out_funds.append({
                "ticker":          fund["ticker"],
                "isin":            fund["isin"],
                "name":            fund["name"],
                "currency_native": fund["currency_native"],
                "shares":          fund["shares"],
                "locked":          fund.get("locked", False),
                "values":          values,
                "inception_date":  inception,
                "is_cash":         fund.get("is_cash", False),
            })

        return jsonify({
            "base_currency":    base_cur,
            "dates":            dates_sorted,
            "funds":            out_funds,
            "portfolio_values": [round(v, 4) for v in portfolio_values],
            "warnings":         warnings,
        })

    # -----------------------------------------------------------------------
    # Cache management
    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # Best-in-class scoring (v0.35.0)
    # -----------------------------------------------------------------------
    def _score_universe_cached(preset_name: str | None = None) -> dict:
        """Score every saved fund from cache. No network, no writes.

        Assembles the universe from the listing and fund caches — cost,
        size and price history — and hands it to
        :func:`porxpy.scoring.score_universe`.

        Read-only on purpose: scoring is displayed on every fund list
        render, and a scoring pass that could trigger a Yahoo fetch would
        turn opening a tab into 35 network round-trips.
        """
        from porxpy.scoring import score_universe, trailing_returns
        from porxpy.config import (SCORING_PRESETS, DEFAULT_SCORING_PRESET,
                                   DEFAULT_FUND_STRUCTURE)
        # The same seed-then-override resolution the fund page uses. The
        # peer group is asset class x focus, and both are usually DERIVED
        # rather than stored — the focus from the fund's own name — so
        # reading the override store alone saw neither.
        from porxpy.extractors import _merge_fund_structure, _seed_fund_structure

        settings = load_settings()
        presets  = (settings.get("scoring") or {}).get("presets") or SCORING_PRESETS
        name     = preset_name if preset_name in presets else DEFAULT_SCORING_PRESET
        preset   = presets.get(name) or SCORING_PRESETS[DEFAULT_SCORING_PRESET]
        floor    = float((settings.get("scoring") or {})
                         .get("size_floor_base", DEFAULT_SIZE_FLOOR_BASE))

        funds = []
        for fp in sorted(LISTINGS_DIR.glob("*.json")):
            # Narrow: a corrupt cache file should be skipped, but a bug in
            # this function should not be. A bare `except Exception` here
            # swallowed a NameError on every file during development and
            # reported an empty universe rather than failing loudly.
            try:
                blob = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                print(f"[Scoring] skipping unreadable cache file {fp.name}")
                continue
            prof  = ((blob.get("profile") or {}).get("value")) or {}
            ident = blob.get("identity") if isinstance(blob.get("identity"), dict) else {}
            ticker = (prof.get("symbol") or ident.get("ticker") or fp.stem).upper()
            isin   = (ident.get("isin") or prof.get("isin") or "").upper()

            ph = ((blob.get("price_history") or {}).get("value")) or []
            ac = ""
            fs = dict(DEFAULT_FUND_STRUCTURE)
            if isin:
                # Overrides are a view over the cached data, applied at
                # response-assembly time — so reading the cache blob
                # directly sees Yahoo's value, not the user's. Scoring a
                # fund on a TER the user has explicitly corrected would
                # make the score disagree with every screen that shows it.
                #
                # The asset-class block goes into the view too: the
                # primary_asset_class override targets asset_class.class,
                # and a view without that block silently dropped it.
                _ac_blob = ((cache_read(isin, "asset_class").get("asset_class")
                             or {}).get("value") or {})
                _view = {"profile": dict(prof), "asset_class": dict(_ac_blob)}
                apply_overrides(isin, _view)
                prof = _view["profile"]
                ac   = _view["asset_class"].get("class") or ""

                # Resolve the structure the way the fund page does: seed
                # from the profile, then layer the stored overrides.
                # Reading the override store on its own reported
                # focus_type "none" for every fund the user had not
                # hand-edited — which is nearly all of them, since the
                # focus is normally read off the fund's name. Every such
                # fund then shared the peer key "<class>|none|", and
                # those with no cached asset class shared
                # "unknown|none|": one bucket holding a global technology
                # equity fund and a hedged credit bond fund, ranked
                # against each other on cost and size.
                _fs_seed, _fs_origins = _seed_fund_structure(prof, ac or None)
                _fs_override = {f: e for f, e in overrides_for(isin).items()
                                if f in DEFAULT_FUND_STRUCTURE}
                fs, _ = _merge_fund_structure(_fs_seed, _fs_override, _fs_origins)

            funds.append({
                "ticker":       ticker,
                "isin":         isin,
                "name":         prof.get("longName") or prof.get("shortName") or ticker,
                "primary_asset_class": ac or "unknown",
                "focus_type":          fs.get("focus_type") or "none",
                "focus_detail":        fs.get("focus_detail") or "",
                "ter":          prof.get("expenseRatioPct"),
                # No FX: sizes are in the fund's own currency. Mixing
                # currencies matters only at the floor boundary, and the
                # floor is a round number chosen with that slack in mind.
                "size_base":    prof.get("totalNetAssets"),
                "returns":      trailing_returns(ph),
            })

        scores = score_universe(funds, preset["components"], preset["wtrr"],
                                size_floor=floor)
        return {"preset": name, "label": preset.get("label") or name,
                "size_floor_base": floor, "scores": scores,
                "universe": len(funds)}

    # -----------------------------------------------------------------------
    # Source inspector (v0.36.0)
    # -----------------------------------------------------------------------
    @app.route("/api/ai/status", methods=["GET"])
    def api_ai_status() -> Response:
        """Whether the AI helper can run, and why not when it cannot.

        Two independent conditions — the user's consent and the presence
        of a key — reported separately, because the fix differs: one is a
        switch in Settings, the other an environment variable and a
        restart. Collapsing them to a single "unavailable" would leave
        the user guessing which.
        """
        from porxpy import ai as _ai
        settings = load_settings()
        enabled = bool((settings.get("ai") or {}).get("enabled"))
        has_key = _ai.api_key_present()
        return jsonify({
            "enabled":  enabled,
            "has_key":  has_key,
            "ready":    enabled and has_key,
            "key_env":  _ai.API_KEY_ENV,
            "model":    _ai.DEFAULT_MODEL,
            "edit_prompt": bool((settings.get("ai") or {}).get("edit_prompt")),
            # The default prompt, so the report can show exactly what was
            # sent. It is generated from the registry and the resource
            # CSVs, so it changes as those do — quoting a stale copy in
            # the UI would be worse than not showing it.
            "prompt":   _ai.build_extraction_prompt(),
        })

    def field_pin_values(isin: str) -> dict:
        """``{field: value}`` for pinned fields, for cross-field validation."""
        return {f: e.get("value") for f, e in field_pins(isin).items()}

    def fetch_field_from_source(ticker: str, isin: str, field: str,
                                source: str):
        """Ask one source for one field. ``(value, note)``.

        ``value`` is None when the source has nothing for that field —
        an answer, not a failure. Every source is asked through the same
        path the bulk fetch uses, so a field cannot mean one thing here
        and another there.
        """
        if source == "yahoo":
            from porxpy.extractors import (extract_profile, detect_asset_class,
                                           _seed_fund_structure)
            t = yf.Ticker(ticker)
            prof = extract_profile(t) or {}
            if field == "primary_asset_class":
                ac = detect_asset_class(t, prof) or {}
                return (ac.get("class") or None), "detected"
            if field == "holdingsCount":
                # Yahoo does not publish the fund's own holding count;
                # what it has is however many top-10 rows it returned,
                # which is a different number and would be a lie here.
                return None, "Yahoo does not publish a holdings count"
            seed, origins = _seed_fund_structure(prof, None)
            if field in seed:
                v = seed.get(field)
                return (None if v in ("unknown", "none", "") else v), origins.get(field)
            return prof.get(field), "profile"

        if source == "justetf":
            from porxpy.extractors import lookup_fund_structure
            res = lookup_fund_structure(isin) or {}
            blk = res.get(field)
            if isinstance(blk, dict):
                v = blk.get("value")
                return (None if v in ("unknown", "none", "") else v), "justETF"
            return None, res.get("note") or "justETF has no value for this field"

        if source == "factsheet":
            meta = factsheet_get(isin) or {}
            ex = meta.get("extraction") or {}
            if not meta:
                raise ValueError("no factsheet stored for this fund")
            if not ex:
                raise ValueError("this factsheet has not been extracted yet")
            blk = (ex.get("fields") or {}).get(field) or {}
            v = blk.get("value")
            note = (f"p{blk.get('page')}: {blk.get('quote')}"
                    if blk.get("quote") else "not stated in the factsheet")
            return (v if v is not None else None), note

        if source == "openfigi":
            return None, "OpenFIGI supplies identity only"
        raise ValueError(f"unknown source {source!r}")

    @app.route("/api/funds/<ticker>/fields", methods=["GET"])
    def api_fund_fields(ticker: str) -> Response:
        """The field taxonomy, with each field's pin and current value.

        One call gives the Edit dialog everything it needs: the groups in
        presentation order, which sources may supply each field, where
        each is currently pinned, and what that pin last produced.
        """
        from porxpy.config import (FIELD_GROUPS, SOURCE_LABELS,
                                   field_sources as _fsrc)
        isin = listing_identity_lookup_isin(ticker)
        pins = field_pins(isin) if isin else {}
        # Identification is read-only and so has no pin, but it does have
        # a provenance: the resolver records which of the ISIN, ticker,
        # exchange, currency and name it got from where. Caches written
        # before that was recorded carry nothing, and report nothing —
        # a blank is honest where a guessed "Yahoo" would not be. The
        # next fund load fills it in.
        ident_src = (listing_identity_get(ticker) or {}).get("sources") or {}
        out_groups = []
        for grp in FIELD_GROUPS:
            fields = []
            for f in grp["fields"]:
                env = pins.get(f["key"]) or {}
                # The field's own type and vocabulary, so the dialog can
                # offer a dropdown for a closed vocabulary instead of a
                # free-text box. Typing "lrge" into a box that accepts
                # only six values is a mistake the UI should not permit.
                spec = OVERRIDABLE_FIELDS.get(f["key"]) or {}
                fields.append({
                    "key":        f["key"],
                    "label":      f["label"],
                    "type":       spec.get("type") or "str",
                    "vocab":      list(field_vocab(spec)),
                    "unit":       spec.get("unit") or "",
                    "min":        spec.get("min"),
                    "max":        spec.get("max"),
                    "calculated": bool(f.get("calculated")),
                    "readonly":   bool(f.get("readonly")),
                    "sources":    list(_fsrc(f["key"])),
                    # The PIN only: which source this field is set to
                    # ask, or "" when nothing is pinned. It is NOT where
                    # the current value came from — that is
                    # data.field_sources, produced once by
                    # _field_provenance and read by both the tiles and
                    # this dialog.
                    #
                    # This used to fall back to the literal "yahoo" for
                    # anything unpinned, which is how the dialog came to
                    # caption a name-inferred focus and a defaulted style
                    # box as Yahoo data. Yahoo is a source when Yahoo
                    # supplied the value and at no other time.
                    "source":     env.get("source") or (
                        "" if f.get("calculated")
                        else ident_src.get(f["key"], "") if f.get("readonly")
                        else ""),
                    "pinned":     bool(env),
                    "value":      env.get("value"),
                    "ts":         env.get("ts"),
                })
            out_groups.append({
                "key": grp["key"], "label": grp["label"],
                "note": grp.get("note") or "",
                # How stale this group's data may get before a reload
                # refetches it. Carried with the group so the UI can show
                # and edit it where the data lives.
                "ttl_days": int((load_settings().get("group_ttl_days") or {})
                                .get(grp["key"], 0)),
                "fields": fields})
        return jsonify({"ticker": ticker, "isin": isin,
                        "groups": out_groups, "source_labels": SOURCE_LABELS})

    @app.route("/api/funds/<ticker>/fields/<field>/source", methods=["PUT"])
    def api_fund_field_source(ticker: str, field: str) -> Response:
        """Pin a field to a source and fetch that source's answer at once.

        Fetching immediately, per field, rather than batching per source
        on save: the point of choosing a source is to find out whether it
        has anything worth having, and a batch that resolves later hides
        exactly that. A source with nothing to say answers "unknown",
        which is a result — it tells the user not to bother asking again.

        Body: ``{"source": "...", "value": <only for source "user">}``.
        """
        from porxpy.config import field_sources as _fsrc, FIELD_SOURCES

        allowed = _fsrc(field)
        if not allowed:
            return jsonify({"error": f"{field} is calculated or read-only "
                                     f"and has no source to choose"}), 400

        body = request.get_json(force=True, silent=True) or {}
        src = (body.get("source") or "").strip().lower()
        if src not in allowed:
            return jsonify({"error": f"{field} can come from "
                                     f"{', '.join(allowed)} — not {src!r}"}), 400

        # Preview: fetch and answer, but store nothing.
        #
        # The dialog fetches the moment a source is chosen, because the
        # whole point of choosing one is to see whether it has anything —
        # but the choice itself is not committed until Save, or Cancel
        # would have to undo writes that had already happened. So the
        # fetch and the commit are separate operations on the same path.
        persist = body.get("persist", True) is not False

        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}"}), 404

        if src == "user":
            # The user's own value IS the assertion; there is nothing to
            # fetch. Validated through the registry where the field has
            # one, so a typed value is held to the same standard as a
            # fetched one.
            value = body.get("value")
            if field in OVERRIDABLE_FIELDS:
                try:
                    value = coerce_override_value(field, value,
                                                  context=field_pin_values(isin))
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 400
            env = field_source_set(isin, field, "user", value) if persist else None
            return jsonify({"ticker": ticker, "field": field, "source": "user",
                            "value": value, "pinned": bool(env),
                            "persisted": persist})

        try:
            value, note = fetch_field_from_source(ticker, isin, field, src)
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 502

        # Yahoo is the default, so choosing it WITHDRAWS the pin rather
        # than recording one — the same rule the breakdown-source
        # endpoint has always applied to its own default. A stored
        # "yahoo" pin asserts nothing, freezes a snapshot of the value,
        # and outranks the seed's recorded origin in the provenance map,
        # which is how a class inferred from a fund's name came to be
        # captioned as Yahoo data.
        if persist and src == "yahoo":
            override_delete(isin, field)
            env = None
        else:
            env = field_source_set(isin, field, src, value) if persist else None
        return jsonify({"ticker": ticker, "field": field, "source": src,
                        "value": value, "note": note, "pinned": bool(env),
                        "persisted": persist,
                        # An explicit flag, because None is a legitimate
                        # answer and the client must not read it as
                        # "the request failed".
                        "unknown": value is None})

    @app.route("/api/funds/<ticker>/fields/refresh", methods=["POST"])
    def api_fund_fields_refresh(ticker: str) -> Response:
        """Refetch pinned fields whose group TTL has elapsed.

        This is what the TTLs are for. A field pinned to a source carries
        the timestamp of the last answer; once that is older than its
        group's limit, the pin is re-asked automatically. The Reload Fund
        Data button is a different instruction — "refetch now, whatever
        the ages say" — and passing ``force`` expresses it.

        Fields pinned to ``user`` are never refetched. There is nothing
        to ask: the value IS the assertion, and re-asking would mean
        overwriting the user with a source they chose to replace.

        Unpinned fields are not touched here either — they follow the
        ordinary profile cache, which has its own freshness rules.
        """
        from porxpy.config import field_spec

        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}"}), 404

        body  = request.get_json(force=True, silent=True) or {}
        force = bool(body.get("force"))
        ttls  = load_settings().get("group_ttl_days") or {}

        refreshed, skipped, errors = {}, {}, []
        for field, env in field_pins(isin).items():
            src = env.get("source")
            if src == "user":
                skipped[field] = "your own value"
                continue
            spec = field_spec(field) or {}
            ttl = int(ttls.get(spec.get("group") or "", 0) or 0)
            age = age_days(env.get("ts") or "")
            if not force:
                if ttl <= 0 and age is not None:
                    pass                      # 0 means always refetch
                elif age is None or age <= ttl:
                    skipped[field] = (f"{age:.0f}d old, limit {ttl}d"
                                      if age is not None else "no timestamp")
                    continue
            try:
                value, note = fetch_field_from_source(ticker, isin, field, src)
            except (RuntimeError, ValueError) as exc:
                errors.append(f"{field}: {exc}")
                continue
            field_source_set(isin, field, src, value)
            refreshed[field] = {"source": src, "value": value,
                                "unknown": value is None, "note": note}

        return jsonify({"ticker": ticker, "isin": isin, "forced": force,
                        "refreshed": refreshed, "skipped": skipped,
                        "errors": errors})

    @app.route("/api/funds/<ticker>/fields/sources", methods=["PUT"])
    def api_fund_field_sources(ticker: str) -> Response:
        """Commit a set of pins in one go — what Save does.

        Body: ``{"pins": {field: {"source": ..., "value": ...}}}``.
        Values are supplied by the caller rather than re-fetched: the
        dialog already fetched them when each source was chosen, and
        asking again on Save would double every network call and could
        return something different from what the user just approved.

        Fields absent from ``pins`` are left alone. A pin of "yahoo" with
        no value clears the entry, since that is the default.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}"}), 404

        body = request.get_json(force=True, silent=True) or {}
        pins = body.get("pins") if isinstance(body.get("pins"), dict) else {}
        from porxpy.config import field_sources as _fsrc

        applied, errors = {}, []
        for field, blk in pins.items():
            if not isinstance(blk, dict):
                continue
            allowed = _fsrc(field)
            src = (blk.get("source") or "").strip().lower()
            if not allowed or src not in allowed:
                errors.append(f"{field}: cannot come from {src or '(none)'}")
                continue
            value = blk.get("value")
            if src == "user" and field in OVERRIDABLE_FIELDS:
                try:
                    value = coerce_override_value(field, value,
                                                  context=field_pin_values(isin))
                except ValueError as exc:
                    errors.append(f"{field}: {exc}")
                    continue
            try:
                field_source_set(isin, field, src, value)
                applied[field] = {"source": src, "value": value}
            except ValueError as exc:
                errors.append(f"{field}: {exc}")

        return jsonify({"ticker": ticker, "isin": isin,
                        "applied": applied, "errors": errors}), (400 if errors and not applied else 200)

    @app.route("/api/funds/<ticker>/source_fields", methods=["POST"])
    def api_fund_source_fields(ticker: str) -> Response:
        """Ask one source for its answer on a set of fields.

        Body: ``{"source": "yahoo"|"justetf", "fields": [name, ...]}``

        Every requested field comes back with a value. A source that has
        nothing to say about a field answers ``"unknown"`` (or None for
        the numeric fields) rather than being omitted — the caller asked
        this source what it thinks, and "I don't know" is an answer. That
        is what makes the Edit dialog's per-field fetch honest: a ticked
        field always changes to reflect the source that was pressed, and
        an unticked field is never touched.

        Note Yahoo does answer on focus, via the fund's own name — the
        issuer writes "MSCI Europe Small Cap" on the tin, and
        ``derive_structure_from_name`` reads it. A source having no
        dedicated field for something does not mean it has no
        information about it.

        Nothing is persisted here. The dialog stages the returned values
        into the form and the user's Save commits them, so a fetch can be
        reviewed — or abandoned — before it becomes durable.
        """
        body   = request.get_json(force=True, silent=True) or {}
        source = (body.get("source") or "").strip().lower()
        fields = [f for f in (body.get("fields") or []) if isinstance(f, str)]
        if source not in ("yahoo", "justetf", "factsheet"):
            return jsonify({"error": f"unknown source: {source!r}"}), 400
        if not fields:
            return jsonify({"error": "no fields requested"}), 400

        isin = listing_identity_lookup_isin(ticker)
        values: dict = {}
        notes:  dict = {}

        # Numeric fields answer None rather than the string "unknown" —
        # they have no such vocabulary, and None is what "no value" means
        # everywhere else in the profile.
        NUMERIC = {"expenseRatioPct", "totalNetAssets", "turnoverPct"}

        def _unknown(f):
            return None if f in NUMERIC else ("none" if f == "focus_type"
                                              else "" if f == "focus_detail"
                                              else "unknown")

        if source == "yahoo":
            try:
                from porxpy.extractors import (extract_profile,
                                               detect_asset_class,
                                               _seed_fund_structure)
                t = yf.Ticker(ticker)
                prof = extract_profile(t) or {}
                ac_block = detect_asset_class(t, prof) or {}
                seed, origins = _seed_fund_structure(prof, ac_block.get("class"))
                for f in fields:
                    if f == "primary_asset_class":
                        values[f] = ac_block.get("class") or "unknown"
                        # Every other field in this loop reports its
                        # origin; this one reported none, so the caller
                        # captioned it with the pin label ("Yahoo") no
                        # matter what had actually decided it.
                        _o = ac_block.get("origin") or ""
                        if _o and _o != "none":
                            notes[f] = _o
                    elif f in seed:
                        values[f] = seed.get(f)
                        notes[f] = origins.get(f) or "yahoo"
                    elif f in NUMERIC:
                        values[f] = prof.get(f)
                    else:
                        values[f] = _unknown(f)
            except Exception as exc:
                return jsonify({"error": f"Yahoo lookup failed: {exc}"}), 502

        elif source == "factsheet":
            # Reads the stored extraction rather than calling the API
            # again: one document, one reading. Re-extract is the button
            # for wanting a fresh one.
            meta = factsheet_get(isin) if isin else None
            ex = (meta or {}).get("extraction") or {}
            got = (ex.get("fields") or {})
            if not meta:
                return jsonify({"error": "no factsheet stored for this fund"}), 404
            if not ex:
                return jsonify({"error": "this factsheet has not been "
                                         "extracted yet"}), 409
            for f in fields:
                blk = got.get(f)
                if isinstance(blk, dict) and blk.get("value") is not None:
                    values[f] = blk["value"]
                    notes[f] = "factsheet"
                else:
                    values[f] = _unknown(f)
            # Citations travel with the values so the dialog can show
            # what each was read from — the whole guard against a
            # confident wrong number.
            return jsonify({"ticker": ticker, "isin": isin, "source": source,
                            "values": values, "notes": notes,
                            "citations": {f: {k: v for k, v in (got.get(f) or {}).items()
                                              if k in ("page", "quote", "confidence")}
                                          for f in fields if got.get(f)}})

        else:   # justetf
            if not isin:
                return jsonify({"error": "justETF is ISIN-keyed and no ISIN "
                                         "is on record for this ticker"}), 404
            try:
                from porxpy.extractors import lookup_fund_structure
                res = lookup_fund_structure(isin) or {}
                got = {k: (res.get(k) or {}).get("value")
                       for k in ("replication", "style", "distribution")}
                for f in fields:
                    v = got.get(f)
                    values[f] = v if v else _unknown(f)
                    if v:
                        notes[f] = "justetf"
                if not res.get("ok"):
                    notes["_source"] = res.get("note") or "justETF lookup failed"
            except Exception as exc:
                return jsonify({"error": f"justETF lookup failed: {exc}"}), 502

        return jsonify({"ticker": ticker, "isin": isin, "source": source,
                        "values": values, "notes": notes})

    def _json_safe(obj, _depth: int = 0):
        """Coerce arbitrary upstream data into something JSON.parse accepts.

        Python's encoder emits bare ``NaN`` and ``Infinity`` for those
        float values. Both are legal to it and to json.loads, and both are
        rejected outright by the browser's JSON.parse — so a single NaN
        anywhere in Yahoo's payload makes the entire inspector response
        unreadable, with an error pointing at a line number rather than at
        the field. 0P00015UO7.F is one such fund.

        Also flattens numpy scalars, pandas Timestamps, sets and anything
        else without a JSON representation, since the whole point of the
        inspector is to dump upstream data whose shape we do not control.
        The depth cap guards against a self-referencing structure.
        """
        import math
        if _depth > 12:
            return "<max depth>"
        if obj is None or isinstance(obj, (str, bool, int)):
            return obj
        if isinstance(obj, float):
            return obj if math.isfinite(obj) else None
        if isinstance(obj, dict):
            return {str(k): _json_safe(v, _depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [_json_safe(v, _depth + 1) for v in obj]
        # numpy scalars and anything else numeric-ish
        item = getattr(obj, "item", None)
        if callable(item):
            try:
                return _json_safe(item(), _depth + 1)
            except Exception:
                pass
        try:
            return str(obj)
        except Exception:
            return "<unrepresentable>"

    # -----------------------------------------------------------------------
    # Factsheets (v0.43.0)
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/factsheet", methods=["GET", "POST", "DELETE"])
    def api_fund_factsheet(ticker: str) -> Response:
        """Store, describe or remove a fund's factsheet.

        ``GET``    — metadata only (see ``/factsheet/file`` for the bytes).
        ``POST``   — multipart ``file``, or JSON ``{"source": "url or path"}``.
                     Optional ``as_of`` (YYYY-MM-DD) and ``note``.
        ``DELETE`` — removes the document and its metadata.

        Keyed by ISIN, like every other fund-level fact, so both listings
        of a dual-listed fund share one factsheet.

        A newer upload replaces the older wholesale, extraction included —
        an extraction must never outlive the document it came from.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if request.method == "GET":
            meta = factsheet_get(isin)
            if not meta:
                return jsonify({"ticker": ticker, "isin": isin,
                                "factsheet": None}), 200
            return jsonify({"ticker": ticker, "isin": isin, "factsheet": meta})

        if request.method == "DELETE":
            # The extraction's facet items live in the supplied-breakdown
            # store, not in the sidecar, so deleting the document alone
            # left them behind — the same "an extraction must never
            # outlive its document" rule this route already applies to
            # the sidecar, applied to the other place a reading is kept.
            _clear_supplied_source(isin, "factsheet", None)
            return jsonify({"ticker": ticker, "isin": isin,
                            "deleted": factsheet_delete(isin)})

        # ---- POST --------------------------------------------------------
        as_of = note = ""
        filename = ""
        data: bytes | None = None

        if request.files and "file" in request.files:
            f = request.files["file"]
            filename = f.filename or "factsheet.pdf"
            data = f.read() or b""
            as_of = (request.form.get("as_of") or "").strip()
            note  = (request.form.get("note") or "").strip()
        else:
            body = request.get_json(force=True, silent=True) or {}
            source = (body.get("source") or "").strip()
            as_of  = (body.get("as_of") or "").strip()
            note   = (body.get("note") or "").strip()
            if not source:
                return jsonify({"error": "provide a file or a source"}), 400
            # Same resolver the holdings and breakdown uploads use, so a
            # URL, a path and a dropped-then-stashed file all work here
            # too without a third way of naming a file.
            from porxpy.upload import resolve_source
            try:
                filename, data, _kind = resolve_source(source)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            # A dropped file resolves through the scratch copy, whose name
            # is "drop_f126105f_vaneck.pdf" — an implementation detail the
            # user has never seen. When the caller tells us what they
            # actually dropped, store that instead. The extension still
            # comes from the resolved file, so a mismatched display name
            # cannot change how the document is typed or served.
            display = (body.get("filename") or "").strip()
            if display:
                filename = Path(display).name + Path(filename).suffix \
                           if not Path(display).suffix else Path(display).name

        try:
            meta = factsheet_put(isin, filename, data or b"",
                                 as_of=as_of, note=note)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # "Replaces the older wholesale, extraction included" has to reach
        # the supplied-breakdown store as well as the sidecar: those items
        # are the previous document's reading, and leaving them would show
        # last year's split under this year's factsheet. Cleared after the
        # write succeeds, so a rejected upload takes nothing with it.
        _clear_supplied_source(isin, "factsheet", None)

        return jsonify({"ticker": ticker, "isin": isin, "factsheet": meta})

    @app.route("/api/funds/<ticker>/factsheet/extract", methods=["POST"])
    def api_fund_factsheet_extract(ticker: str) -> Response:
        """Read the stored factsheet with the AI helper.

        One call extracts metadata fields AND all four facet breakdowns,
        so the two provably come from a single reading of the document.
        The result is stored against the factsheet, not applied: fields
        stage into the Edit dialog's "Fetch from factsheet" button, and
        facets become the ``factsheet`` source on the breakdown cards.

        Off unless ``settings.ai.enabled`` is set AND an API key is in
        the environment. Both, deliberately: the switch is the user's
        consent, and the key's absence is a configuration fact the switch
        cannot conjure away.
        """
        from porxpy import ai as _ai

        settings = load_settings()
        if not (settings.get("ai") or {}).get("enabled"):
            return jsonify({"error": "the AI helper is switched off — "
                                     "enable it in Settings"}), 409
        if not _ai.api_key_present():
            return jsonify({"error": f"no API key: set {_ai.API_KEY_ENV} in "
                                     f"the environment and restart PorxPy"}), 409

        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}"}), 404
        meta = factsheet_get(isin)
        fp = factsheet_file(isin)
        if not meta or not fp:
            return jsonify({"error": "no factsheet stored for this fund"}), 404

        # An edited prompt, when the user has that option switched on.
        # Sent per call rather than stored: a prompt that persisted would
        # silently apply to every future extraction, including ones where
        # the user had forgotten they changed it.
        body = request.get_json(force=True, silent=True) or {}
        custom = (body.get("prompt") or "").strip()
        if custom and not (settings.get("ai") or {}).get("edit_prompt"):
            return jsonify({"error": "prompt editing is switched off"}), 409

        try:
            raw = _ai.extract_from_document(fp.read_bytes(), meta.get("ext") or "",
                                            prompt=custom or None)
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 502

        # The API call has already succeeded and been paid for by this
        # point, so everything after it is wrapped: a bug in our own
        # parsing should not surface as an HTML traceback the browser
        # reports as "NetworkError", losing both the diagnosis and the
        # result. The raw reply is returned alongside the error so the
        # extraction can be salvaged or the bug reproduced without
        # spending another call.
        try:
            result = _ai.validate_extraction(raw)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return jsonify({
                "error": f"the factsheet was read, but processing the reply "
                         f"failed: {type(exc).__name__}: {exc}",
                "raw": {k: v for k, v in (raw or {}).items()
                        if not str(k).startswith("_")},
            }), 500

        # Facet items are stored as the DOCUMENT said them, canonicalised
        # only where the vocabulary recognises the value. A key the model
        # invented survives as itself rather than being dropped —
        # resolution runs again on every read (see
        # breakdowns.resolve_facet_value), so the card reports it inside
        # its unknown slice and the Resolve dialog can fix it later by
        # adding an alias, with no need to re-extract the factsheet.
        # resolve_facet_NODE, not resolve_facet_value: the node keeps the
        # level the document named. Resolving to the facet's default
        # level here turned a factsheet's "Cyclical" into "unknown"
        # before it was ever stored — the store would hold a residual
        # where the document had a real answer, and no level of the
        # card could recover it.
        # Stored as the DOCUMENT's own wording, unresolved (v0.76.0).
        # This used to resolve here and store the canonical, so a
        # factsheet naming a label the vocabulary later learned could
        # never benefit from having learned it — the stored key was
        # already a conclusion, and re-resolving a conclusion returns
        # itself. The item now carries `raw`, and _resolve_items answers
        # the question on every read, exactly as a holdings row does.
        facet_items: dict[str, list] = {}
        for facet, blk in (result.get("facets") or {}).items():
            rows = []
            for it in blk.get("items") or []:
                # ``label_in_document`` is the exact printed text and is
                # therefore the raw; ``key`` is the model's canonical
                # GUESS and is only the fallback, for the ordinary rows
                # where the prompt does not ask for the printed text.
                # Storing the guess as the raw made the alias the Resolve
                # dialog offers to write an alias for something the model
                # invented rather than for anything the document says.
                raw = (str(it.get("label_in_document") or "").strip()
                       or str(it.get("key") or "").strip())
                # The extractor reports percentages, because that is what
                # a factsheet prints. The store holds FRACTIONS — the CSV
                # commit divides by 100 before writing, and every consumer
                # assumes 0–1. Storing percentages here put every card a
                # factor of 100 out.
                rows.append({"raw": raw,
                             "weight": round(float(it.get("weight") or 0.0) / 100.0, 8)})
            if rows:
                facet_items[facet] = rows
        # Written unconditionally, including when the document turned out
        # to state no breakdown at all: this reading REPLACES the previous
        # one, and skipping the write on an empty result left the earlier
        # extraction's numbers on the card as though this reading had
        # confirmed them. uploaded_breakdowns_put normalises the absent
        # facets to empty lists, which is the honest answer — "this
        # document does not say" — and touches no other source.
        try:
            uploaded_breakdowns_put(isin, facet_items, source="factsheet")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result.setdefault("rejected", []).append(
                {"item": "facets", "reason": f"could not be stored: {exc}"})

        # The position table becomes the fund's "factsheet" holdings
        # source, beside Yahoo's top-10 and any uploaded CSV. Written
        # unconditionally for the same reason the facets are: this
        # reading REPLACES the previous one, and skipping the write when
        # the document turned out to print no positions would leave the
        # earlier extraction's rows on the tile as though this reading
        # had confirmed them.
        #
        # Rows go through coerce_holdings_row, the same normaliser the
        # upload and Yahoo paths use, so a factsheet position is the same
        # kind of row as any other: facets resolved from the document's
        # own wording, bond columns typed, a stable _row_id minted so the
        # holdings editor can address it.
        hold_block = result.get("holdings") or {}
        try:
            hold_rows = [coerce_holdings_row(r)
                         for r in (hold_block.get("rows") or [])]
            weight_sum = round(
                sum(float(r.get("weight_pct") or 0.0) for r in hold_rows), 6)
            holdings_put(isin, {
                "rows":           hold_rows,
                "source":         "factsheet",
                "_provider":      "factsheet",
                "row_count":      len(hold_rows),
                "weight_sum_pct": weight_sum,
                # What the document said about its own completeness,
                # after the arithmetic check in ai._validate_holdings.
                "complete":       bool(hold_block.get("complete")),
                "page":           hold_block.get("page"),
                "quote":          hold_block.get("quote") or "",
                "as_of":          result.get("as_of") or "",
                "extracted_at":   now_iso(),
                "fetched_at":     now_iso(),
                "last_updated":   now_iso(),
            }, "factsheet")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result.setdefault("rejected", []).append(
                {"item": "holdings", "reason": f"could not be stored: {exc}"})

        # Adopt the document's own date when we did not have one. This is
        # what makes staleness honest: a factsheet is usually months old
        # on the day it is uploaded, and only the document knows by how
        # much.
        if result.get("as_of") and not (meta.get("as_of") or "").strip():
            factsheet_put(isin, meta.get("filename") or "factsheet",
                          fp.read_bytes(), as_of=result["as_of"],
                          note=meta.get("note") or "")

        # Keep the prompt with the result. Six months on, "why did it read
        # it that way" is answerable only if the instruction is on record
        # alongside the answer.
        result["prompt"] = custom or _ai.build_extraction_prompt()
        result["prompt_edited"] = bool(custom)

        try:
            stored = factsheet_set_extraction(isin, result)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return jsonify({"error": f"extraction succeeded but could not be "
                                     f"stored: {exc}",
                            "extraction": result}), 500
        return jsonify({"ticker": ticker, "isin": isin,
                        "extraction": result,
                        "factsheet": stored})

    @app.route("/api/funds/<ticker>/factsheet/file", methods=["GET"])
    def api_fund_factsheet_file(ticker: str) -> Response:
        """Serve the stored document itself, for the viewer popup.

        Inline rather than as an attachment: the point is to read it in a
        window beside the data it produced, not to download it again.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": "no identity recorded"}), 404
        fp = factsheet_file(isin)
        if not fp:
            return jsonify({"error": "no factsheet stored for this fund"}), 404
        meta = factsheet_get(isin) or {}
        return send_file(
            str(fp),
            mimetype=FACTSHEET_MIME.get(meta.get("ext") or "",
                                        "application/octet-stream"),
            as_attachment=False,
            download_name=meta.get("filename") or fp.name,
        )

    @app.route("/api/upload/stash", methods=["POST"])
    def api_upload_stash() -> Response:
        """Write dropped file bytes to scratch and return a real path.

        The holdings pipeline is path-based throughout — resolve_source,
        the remembered source_value, one-click re-upload — because the
        server runs on the user's own machine and a path can be re-read
        later. A browser drag-and-drop gives bytes and no path, so
        supporting it would otherwise mean a second, parallel pipeline.

        Instead the bytes are written to the upload scratch directory and
        the caller gets a path back, which then flows through the existing
        preview/commit flow untouched.

        The tradeoff is honest and worth stating in the UI: a stashed file
        lives in scratch, so "re-upload from the same source" will work
        only until that directory is cleared. A file chosen by path is
        re-readable indefinitely.
        """
        f = request.files.get("file")
        if f is None or not (f.filename or "").strip():
            return jsonify({"error": "no file in request"}), 400

        # Keep the extension — the parser dispatches on it — but not the
        # rest of the client-supplied name, which is attacker-controlled
        # in principle and path-bearing in practice on some platforms.
        name = Path(f.filename).name
        ext = Path(name).suffix.lower()
        # Data files plus factsheet documents: this endpoint backs every
        # drop zone, and a list that omitted PDFs would let the factsheet
        # zone accept a file the server then refused.
        STASHABLE = ((".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls")
                     + FACTSHEET_EXTENSIONS)
        if ext not in STASHABLE:
            return jsonify({"error": f"unsupported file type: {ext or '(none)'}"}), 400

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).stem)[:60] or "dropped"
        dest = UPLOAD_DIR / f"drop_{uuid.uuid4().hex[:8]}_{safe}{ext}"
        try:
            f.save(str(dest))
        except OSError as exc:
            return jsonify({"error": f"could not save file: {exc}"}), 500

        return jsonify({"path": str(dest), "filename": name,
                        "bytes": dest.stat().st_size, "stashed": True})

    @app.route("/api/tools/inspect", methods=["GET"])
    def api_tools_inspect() -> Response:
        """Raw, uninterpreted responses from every upstream source.

        Query:
            ``ticker`` and/or ``isin``. A ticker inspects Yahoo directly;
            an ISIN additionally inspects OpenFIGI and justETF.

        Returns a per-source block containing exactly what the source
        said, with no coercion, no unit conversion and no fallback
        chain applied — plus, for the fee-and-size fields, what PorxPy
        *would* derive from it, so the two can be compared side by side.

        This exists because that comparison could not be made. Total net
        assets was wrong in three consecutive releases, each time because
        a value was being interpreted without anyone being able to see
        what had actually arrived. A diagnostic that shows the raw
        response next to the derived value turns "the number looks wrong"
        into "this field says X and we turned it into Y".

        Live network calls, deliberately: the point is to see what the
        source returns NOW, not what the cache remembers. Nothing is
        written — no cache, no overrides, no identity records.
        """
        ticker = (request.args.get("ticker") or "").strip()
        isin   = (request.args.get("isin") or "").strip().upper()
        if not ticker and not isin:
            return jsonify({"error": "provide ticker and/or isin"}), 400

        # Fill in the ISIN when only a ticker was given, so the OpenFIGI
        # and justETF blocks are not silently skipped. Cache first, then
        # what Yahoo reports. Tracked separately so the report can say
        # where it came from rather than appearing to have been typed.
        isin_source = "supplied" if isin else ""
        if not isin and ticker:
            cached_isin = listing_identity_lookup_isin(ticker)
            if cached_isin:
                isin, isin_source = cached_isin.upper(), "listing identity cache"

        out: dict = {"ticker": ticker, "isin": isin,
                     "isin_source": isin_source, "sources": {}}

        # ---- Yahoo ------------------------------------------------------
        if ticker:
            y: dict = {"ok": False}
            try:
                t = yf.Ticker(ticker)
                info = dict(t.info or {})
                y["info"] = info
                y["info_key_count"] = len(info)

                # The fee-and-size keys, pulled out because they are the
                # ones that have caused trouble, with each candidate shown
                # under every plausible unit reading.
                def _n(v):
                    try:
                        f = float(v)
                        return None if f != f else f
                    except (TypeError, ValueError):
                        return None
                interesting = {}
                for k in ("netExpenseRatio", "annualReportExpenseRatio",
                          "annualHoldingsTurnover", "netAssets", "totalAssets",
                          "totalNetAssets", "fundFamily", "category",
                          "currency", "quoteType", "longName"):
                    if k in info:
                        v = info[k]
                        n = _n(v)
                        interesting[k] = {
                            "raw": v,
                            "as_units": n,
                            "x1e3": (n * 1e3) if n is not None else None,
                            "x1e6": (n * 1e6) if n is not None else None,
                        }
                y["fee_and_size_candidates"] = interesting


                # funds_data.fund_operations, with the category column
                # kept. Where the fund column and the category column
                # carry the same number, the field is not fund-specific —
                # that is how totalNetAssets was caught returning a
                # category aggregate for every fund.
                try:
                    ops_df = t.funds_data.fund_operations
                    if ops_df is not None and not ops_df.empty:
                        cols = [str(c) for c in ops_df.columns]
                        rows = []
                        for idx in ops_df.index:
                            vals = [(None if v != v else v) for v in ops_df.loc[idx].tolist()]
                            same = (len(vals) >= 2 and vals[0] is not None
                                    and vals[0] == vals[1])
                            rows.append({"attribute": str(idx), "values": vals,
                                         "same_as_category": same})
                        y["fund_operations"] = {"columns": cols, "rows": rows}
                    else:
                        y["fund_operations"] = {"columns": [], "rows": [],
                                                "note": "empty — Yahoo has no "
                                                        "fundProfile for this listing"}
                except Exception as exc:
                    y["fund_operations"] = {"error": str(exc)}

                # Everything else yfinance exposes for a fund. Each is
                # optional and each fails independently — a listing with
                # no bond holdings should not cost you the sector
                # weightings. Dumped whole rather than filtered: the
                # point of an inspector is to show what is there, and a
                # renderer that decided what mattered would be answering
                # the question instead of reporting it.
                def _frame(obj):
                    """DataFrame / Series / dict -> plain JSON."""
                    if obj is None:
                        return None
                    try:
                        if hasattr(obj, "empty") and obj.empty:
                            return {}
                        if hasattr(obj, "to_dict"):
                            d = obj.to_dict()
                            return json.loads(json.dumps(d, default=str))
                        return json.loads(json.dumps(obj, default=str))
                    except Exception as exc:
                        return {"_error": str(exc)}

                fd = {}
                try:
                    funds = t.funds_data
                    for attr in ("description", "fund_overview",
                                 "asset_classes", "top_holdings",
                                 "sector_weightings", "equity_holdings",
                                 "bond_holdings", "bond_ratings"):
                        try:
                            fd[attr] = _frame(getattr(funds, attr, None))
                        except Exception as exc:
                            fd[attr] = {"_error": str(exc)}
                except Exception as exc:
                    fd["_error"] = str(exc)
                y["funds_data"] = fd

                # Price history summary rather than the series itself —
                # thousands of bars would bury everything else, and the
                # span and endpoints are what you actually check.
                try:
                    h = t.history(period="max")
                    if h is not None and not h.empty:
                        y["history"] = {
                            "rows":  int(len(h)),
                            "first": str(h.index[0])[:10],
                            "last":  str(h.index[-1])[:10],
                            "columns": [str(c) for c in h.columns],
                            "last_close": float(h["Close"].iloc[-1]),
                        }
                    else:
                        y["history"] = {"rows": 0}
                except Exception as exc:
                    y["history"] = {"_error": str(exc)}

                for attr in ("fast_info", "isin"):
                    try:
                        v = getattr(t, attr, None)
                        y[attr] = _frame(dict(v)) if attr == "fast_info" and v else v
                    except Exception as exc:
                        y[attr] = {"_error": str(exc)}

                # What PorxPy derives from all of the above.
                from porxpy.extractors import extract_profile
                try:
                    derived = extract_profile(t) or {}
                    y["porxpy_derived"] = {
                        k: derived.get(k) for k in
                        ("expenseRatioPct", "turnoverPct", "totalNetAssets",
                         "totalNetAssetsSrc", "currency", "isin",
                         "market_cap", "style_box", "distribution")
                    }
                except Exception as exc:
                    y["porxpy_derived"] = {"error": str(exc)}
                y["ok"] = True
            except Exception as exc:
                y["error"] = str(exc)
            out["sources"]["yahoo"] = y

        # Yahoo occasionally reports the ISIN inline; last fallback before
        # the ISIN-keyed sources below decide they have nothing to work
        # with.
        if not isin:
            y_isin = ((out["sources"].get("yahoo") or {})
                      .get("porxpy_derived") or {}).get("isin")
            if y_isin:
                isin = str(y_isin).upper()
                out["isin"] = isin
                out["isin_source"] = "Yahoo"

        # ---- What is actually stored -------------------------------------
        # The live source and the derived value can both be right while
        # the screen is still wrong, because the screen reads the CACHE
        # with overrides applied on top. Showing all three side by side
        # is the only way to tell "the source is wrong" from "the cache
        # is stale" from "an override is pinning it" — three very
        # different problems that look identical from the fund tile.
        stored: dict = {}
        if ticker:
            blob = cache_read(ticker, "profile")
            entry = blob.get("profile") if isinstance(blob, dict) else None
            prof_cached = (entry or {}).get("value") or {}
            stored["cached_profile"] = {
                k: prof_cached.get(k) for k in
                ("expenseRatioPct", "turnoverPct", "totalNetAssets",
                 "totalNetAssetsSrc", "currency", "isin", "longName")
            }
            stored["cached_at"] = (entry or {}).get("ts")
        if isin:
            stored["overrides"] = overrides_for(isin)
        if stored:
            out["sources"]["stored"] = stored

        # ---- OpenFIGI ---------------------------------------------------
        # Both of these are ISIN-keyed. When no ISIN could be found the
        # block still appears, saying so — silently omitting it looked
        # like the source had been queried and returned nothing.
        if isin:
            try:
                from porxpy.resolver import _figi_post
                req = [{"idType": "ID_ISIN", "idValue": isin}]
                out["sources"]["openfigi"] = {
                    "ok": True, "request": req, "response": _figi_post(req),
                }
            except Exception as exc:
                out["sources"]["openfigi"] = {"ok": False, "error": str(exc)}
        else:
            out["sources"]["openfigi"] = {
                "ok": False, "skipped": True,
                "note": "no ISIN supplied and none on record for this ticker",
            }

        # ---- justETF ----------------------------------------------------
        if isin:
            try:
                from porxpy.extractors import lookup_fund_structure
                out["sources"]["justetf"] = lookup_fund_structure(isin)
            except Exception as exc:
                out["sources"]["justetf"] = {"ok": False, "error": str(exc)}
        else:
            out["sources"]["justetf"] = {
                "ok": False, "skipped": True,
                "note": "no ISIN supplied and none on record for this ticker",
            }

        # Sanitised once, at the boundary, rather than at each of the
        # dozen places that add data to `out` — a single missed spot
        # breaks the whole response.
        return jsonify(_json_safe(out))

    @app.route("/api/scores", methods=["GET"])
    def api_scores() -> Response:
        """Best-in-class scores for every saved fund.

        Query:
            ``preset`` — weight model name; defaults to the configured
            default.

        Returns:
            ``{preset, label, size_floor_base, universe, scores}`` where
            ``scores`` maps ticker to its score block. ``score_all`` ranks
            against the whole universe; ``score_peer`` against funds with
            the same asset class and focus, and is None when the peer
            group is too small to rank within.
        """
        return jsonify(_score_universe_cached(request.args.get("preset")))

    @app.route("/api/funds/<ticker>/peers", methods=["GET"])
    def api_fund_peers(ticker: str) -> Response:
        """Price series for the funds in this fund's peer group.

        The group is taken from the SCORED universe, which already
        computes it — so this list is by construction the same one the
        Score tile shows. v0.67.1 built it independently by calling
        load_fund_data per listing, which produced a different fund
        shape from the one scoring assembles and returned nothing.
        Two ways of deciding what a peer is meant two answers, and the
        one on the price chart was the wrong one.

        Series come from the cache blob directly: no fetch, no
        load_fund_data, so opening a fund page does not walk the network
        once per candidate.

        Returned unwindowed and unindexed — the caller already windows
        and normalises the subject fund, and doing it twice in two
        places is how the two stop agreeing.
        """
        tk = (ticker or "").strip().upper()
        scored = (_score_universe_cached(request.args.get("preset")) or {})
        blocks = scored.get("scores") or {}
        mine = blocks.get(tk) or {}
        group = [t for t in (mine.get("peers") or []) if t != tk]

        peers = []
        for other in group:
            fp = LISTINGS_DIR / f"{other}.json"
            if not fp.exists():
                continue
            try:
                blob = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            hist = ((blob.get("price_history") or {}).get("value")) or []
            if not hist:
                continue
            prof = ((blob.get("profile") or {}).get("value")) or {}
            peers.append({
                "ticker":   other,
                "name":     prof.get("longName") or prof.get("shortName") or other,
                "currency": (prof.get("currency") or "").upper(),
                "history":  [{"date": p.get("date"), "close": p.get("close")}
                             for p in hist if p.get("date")],
            })
        peers.sort(key=lambda p: p["ticker"])
        return jsonify({
            "ticker":   tk,
            "peer_key": mine.get("peer_key") or "",
            "peer_n":   mine.get("peer_n") or 0,
            "peers":    peers,
            "count":    len(peers),
            # Named so the UI can say WHY the row is empty: a group of
            # one, or peers that exist but have no cached price series.
            "group_size": len(mine.get("peers") or []),
        })

    @app.route("/api/scores/presets", methods=["GET"])
    def api_score_presets() -> Response:
        """The configured weight models, for the optimiser's picker."""
        from porxpy.config import SCORING_PRESETS, DEFAULT_SCORING_PRESET
        settings = load_settings()
        presets  = (settings.get("scoring") or {}).get("presets") or SCORING_PRESETS
        return jsonify({
            "presets": [{"key": k, "label": v.get("label") or k,
                         "components": v.get("components") or {},
                         "wtrr": v.get("wtrr") or {}}
                        for k, v in presets.items()],
            "default": DEFAULT_SCORING_PRESET,
            "size_floor_base": (settings.get("scoring") or {})
                               .get("size_floor_base", DEFAULT_SIZE_FLOOR_BASE),
        })

    @app.route("/api/cache/list", methods=["GET"])
    def api_cache_list() -> Response:
        """List every cached ticker with category ages and holdings status.

        Excludes FX rate caches (``FX_*``) and FX history caches (``FXH_*``).
        Each row carries enough data
        for the Pre-Loaded panel to display a "FULL ·n" badge without
        needing a follow-up request per fund, plus a list of portfolios
        that hold this fund (with the share counts) so the UI can show
        "in 2 portfolios" dropdowns.
        """
        # Build ticker → portfolio memberships in one pass over
        # portfolios.json. Portfolios store only {ticker, shares} since
        # 0.12.0 — identity (ISIN, exchange, currency) comes from the
        # listings cache's identity block, not from the portfolio entry.
        ticker_portfolios: dict[str, list[dict]] = {}
        for p in load_portfolios():
            for f in p.get("funds", []):
                tk = f.get("ticker")
                if not tk:
                    continue
                ticker_portfolios.setdefault(tk, []).append({
                    "id":       p.get("id"),
                    "name":     p.get("name"),
                    "shares":   f.get("shares"),
                })

        out = []
        # All overrides live in one ISIN-keyed file as of 0.12.0; loaded
        # once for the whole listing rather than per-fund.
        all_overrides = load_overrides()
        listing_cats = set(LISTING_CATEGORIES)
        fund_cats    = set(FUND_CATEGORIES)

        # Iterate listings — each file is one ticker. Walk into the
        # funds cache through the identity block to surface fund-level
        # category metadata and overrides.
        if not LISTINGS_DIR.exists():
            return jsonify({"cached": [], "count": 0})

        for fp in sorted(LISTINGS_DIR.glob("*.json")):
            ticker = fp.stem
            listing_blob = cache_read(ticker, "profile")
            if not listing_blob:
                continue

            ident      = listing_blob.get("identity") or {}
            isin_v     = (ident.get("isin")     or "").strip().upper() or None
            exchange_v = ident.get("exchange")  or None
            currency_v = ident.get("currency")  or None

            # Currency fallback — listings cached before the identity
            # block had Yahoo's currency only in the profile.
            prof = (listing_blob.get("profile") or {}).get("value") or {}
            if isinstance(prof, dict) and not currency_v:
                currency_v = prof.get("currency") or None
            name = None
            if isinstance(prof, dict):
                name = prof.get("longName") or prof.get("shortName")

            # Fund-level data lives in the funds cache (ISIN-keyed). A
            # listing whose identity has no ISIN yet (rare, only for
            # interrupted fetches) shows only listing-level categories.
            funds_blob = (cache_read(isin_v, "holdings") if isin_v else {}) or {}

            # Category ages — surface every populated category across
            # both halves of the split, so the UI can show the same
            # mosaic it did before 0.12.0.
            cats: dict[str, dict] = {}
            for cat in listing_cats:
                entry = listing_blob.get(cat)
                if isinstance(entry, dict):
                    ts  = entry.get("fetched_at", "")
                    age = age_days(ts) if ts else None
                    cats[cat] = {"fetched_at": ts or None,
                                 "age_days":   round(age, 3) if age is not None else None}
            for cat in fund_cats:
                entry = funds_blob.get(cat)
                if isinstance(entry, dict):
                    ts  = entry.get("fetched_at", "")
                    age = age_days(ts) if ts else None
                    cats[cat] = {"fetched_at": ts or None,
                                 "age_days":   round(age, 3) if age is not None else None}

            # Asset class — Yahoo's detected value vs the user override.
            ac_val = (funds_blob.get("asset_class") or {}).get("value") or {}
            ac_detected = ac_val.get("class") if isinstance(ac_val, dict) else None
            ov_entry    = all_overrides.get(isin_v or "", {}) if isin_v else {}
            ac_override = ov_entry.get("asset_class")
            bd_for_fund = ov_entry.get("breakdown_source") or {}
            fs_for_fund = ov_entry.get("fund_structure")

            out.append({
                "ticker":                ticker,
                "isin":                  isin_v,
                "exchange":              exchange_v,
                "currency":              currency_v,
                "name":                  name,
                "asset_class":           ac_override or ac_detected,
                "asset_class_detected":  ac_detected,
                "asset_class_override":  ac_override,
                "breakdown_overrides":   bd_for_fund,
                "fund_structure":        fs_for_fund,
                "categories":            cats,
                "has_isin":              bool(isin_v),
                # v0.32.0 — drives the "incl" column. ISIN-keyed like the
                # rest of the fund-level metadata, so both listings of a
                # dual-listed fund answer the same way; a listing with no
                # ISIN on record has nothing to key on and reports the
                # default.
                "include_in_optimizer":  (bool(override_get(
                                              isin_v, "include_in_optimizer", True))
                                          if isin_v else True),
                "holdings_status":       holdings_status_from_cache(isin_v) if isin_v else {},
                "portfolios":            ticker_portfolios.get(ticker, []),
            })

        # Sort: ones with ISIN first (clickable to re-fetch), then alphabetical
        out.sort(key=lambda x: (not x["has_isin"], x["ticker"]))
        return jsonify({"cached": out, "count": len(out)})

    @app.route("/api/cache/purge", methods=["POST"])
    def api_cache_purge() -> Response:
        """Purge a single ticker (or category) from the cache.

        Body:
            ticker (str, optional)
            category (str, optional — must be in CACHE_CATEGORIES)
        """
        body = request.get_json(force=True, silent=True) or {}
        ticker   = body.get("ticker")
        category = body.get("category")
        if category and category not in CACHE_CATEGORIES:
            return jsonify({"error": f"category must be one of {CACHE_CATEGORIES}"}), 400
        removed = cache_purge(ticker, category)
        return jsonify({"removed": removed, "ticker": ticker, "category": category})

    @app.route("/api/cache/purge_many", methods=["POST"])
    def api_cache_purge_many() -> Response:
        """Delete the cache files for one or more tickers.

        Body:
            tickers: list[str]

        If a deleted ticker is referenced by a portfolio, it will be
        re-fetched (and re-cached) the next time that portfolio is opened.
        """
        body = request.get_json(force=True, silent=True) or {}
        tickers = body.get("tickers") or []
        if not isinstance(tickers, list) or not all(isinstance(t, str) for t in tickers):
            return jsonify({"error": "tickers must be a list of strings"}), 400
        if not tickers:
            return jsonify({"removed": 0, "tickers": []})

        total_removed = 0
        purged: list[str] = []
        for tk in tickers:
            tk = tk.strip()
            if not tk:
                continue
            n = cache_purge(tk, None)
            if n > 0:
                total_removed += n
                purged.append(tk)
        return jsonify({"removed": total_removed, "tickers": purged})

    # -----------------------------------------------------------------------
    # Destructive resets — Settings buttons
    # -----------------------------------------------------------------------
    # Three separate, typed-confirmation endpoints:
    #
    #   /api/reset/portfolios — wipes portfolios.json only.
    #   /api/reset/cache      — wipes cache/listings/, cache/funds/,
    #                           isin_map.json. Overrides untouched.
    #   /api/reset/all        — wipes everything above PLUS
    #                           overrides.json. Settings, resources
    #                           and uploads stay.
    #
    # Each requires a body ``{"confirm": "RESET"}`` so a stray POST
    # cannot trigger a wipe. The frontend renders three buttons; each
    # opens a typed-confirmation modal that POSTs here only after the
    # user types the literal word "RESET".
    REQUIRED_TOKEN = "RESET"

    def _require_reset_token() -> Response | None:
        """Return a 400 Response if the confirmation token is missing.

        Returns ``None`` on success — the caller proceeds.
        """
        body = request.get_json(force=True, silent=True) or {}
        if (body.get("confirm") or "").strip() != REQUIRED_TOKEN:
            return jsonify({
                "error": f"missing or invalid confirm token "
                         f"(must be the literal word {REQUIRED_TOKEN!r})"
            }), 400
        return None

    @app.route("/api/reset/portfolios", methods=["POST"])
    def api_reset_portfolios() -> Response:
        """Wipe portfolios.json. Cache, overrides and settings are untouched."""
        err = _require_reset_token()
        if err is not None:
            return err
        from porxpy.config import PORTFOLIOS_FP
        had = PORTFOLIOS_FP.exists()
        try:
            if had:
                PORTFOLIOS_FP.unlink()
        except Exception as exc:
            return jsonify({"error": f"could not delete portfolios.json: {exc}"}), 500
        return jsonify({"reset": "portfolios", "file_existed": had})

    @app.route("/api/reset/cache", methods=["POST"])
    def api_reset_cache() -> Response:
        """Wipe both halves of the cache and the isin_map.

        Overrides survive — they are user intent, not cache. Settings
        and resources are untouched. After this, every fund needs a
        refetch before it can be loaded.
        """
        err = _require_reset_token()
        if err is not None:
            return err
        from porxpy.config import ISIN_MAP_FP
        listings_n = 0
        funds_n    = 0
        try:
            if LISTINGS_DIR.exists():
                for fp in LISTINGS_DIR.iterdir():
                    if fp.is_file():
                        fp.unlink(); listings_n += 1
            if FUNDS_DIR.exists():
                for fp in FUNDS_DIR.iterdir():
                    if fp.is_file():
                        fp.unlink(); funds_n += 1
            # Shared pseudo-caches at CACHE_DIR root (FX series, symbol
            # info, alias maps). Safe to wipe — they will rebuild on
            # demand from Yahoo.
            shared_n = 0
            if CACHE_DIR.exists():
                for fp in CACHE_DIR.iterdir():
                    if fp.is_file() and fp.suffix == ".json":
                        fp.unlink(); shared_n += 1
            isin_map_existed = ISIN_MAP_FP.exists()
            if isin_map_existed:
                ISIN_MAP_FP.unlink()
        except Exception as exc:
            return jsonify({"error": f"cache wipe failed: {exc}"}), 500
        return jsonify({
            "reset":             "cache",
            "listings_removed":  listings_n,
            "funds_removed":     funds_n,
            "shared_removed":    shared_n,
            "isin_map_removed":  isin_map_existed,
        })

    @app.route("/api/reset/all", methods=["POST"])
    def api_reset_all() -> Response:
        """Full factory reset: portfolios + cache + overrides + isin_map.

        Settings (settings.json), the bundled resources/ CSVs, and any
        in-flight upload tokens stay. The app will behave like a fresh
        install on next page load.
        """
        err = _require_reset_token()
        if err is not None:
            return err
        from porxpy.config import (
            PORTFOLIOS_FP, OVERRIDES_FP, ISIN_MAP_FP,
        )
        removed: dict = {"files": []}
        try:
            # Portfolios + overrides + isin_map at root
            for fp in (PORTFOLIOS_FP, OVERRIDES_FP, ISIN_MAP_FP):
                if fp.exists():
                    fp.unlink()
                    removed["files"].append(fp.name)
            # All cache files
            n = 0
            for d in (LISTINGS_DIR, FUNDS_DIR, CACHE_DIR):
                if not d.exists():
                    continue
                for fp in d.iterdir():
                    if fp.is_file() and fp.suffix == ".json":
                        fp.unlink(); n += 1
            removed["cache_files"] = n
        except Exception as exc:
            return jsonify({"error": f"full reset failed: {exc}"}), 500
        return jsonify({"reset": "all", **removed})

    # -----------------------------------------------------------------------
    # Holdings upload — preview, commit, clear, prefs
    # -----------------------------------------------------------------------
    # Two-step flow:
    #
    #   POST /preview ─ resolve a source string (URL or filesystem path)
    #                   to bytes, parse them, return a preview + token.
    #                   Auto-detects sheet (XLSX) / delimiter (CSV) /
    #                   header row.
    #   POST /commit  ─ apply the user's column mapping + parsing knobs
    #                   to the previewed data, write the unified
    #                   ``holdings`` cache slot (source="manual_upload"),
    #                   AND save the user's full set of choices (source,
    #                   mapping, defaults, etc.) into upload_prefs so the
    #                   next visit can prefill.
    #
    # Plus:
    #   GET  /prefs?ticker=…  ─ read upload_prefs for a fund.
    #   POST /clear           ─ remove a manual-upload holdings blob
    #                           (next fund load repopulates from Yahoo).
    #
    # Tokens live for UPLOAD_TOKEN_TTL_MIN minutes; expired tokens are
    # reaped on the next preview call.
    # ------------------------------------------------------------------
    @app.route("/api/upload/browse", methods=["GET"])
    def api_upload_browse() -> Response:
        """List a directory so the frontend can render a file picker.

        PorxPy is self-hosted and single-user — the server process runs on
        the same machine as the browser — so listing the local filesystem
        is listing the user's own disk. That's what makes the in-app
        picker possible at all: the browser can never tell us a file's
        path (security), but the server can read one straight off disk.
        The picker therefore returns a real path, which keeps the whole
        existing pipeline (resolve_source, remembered source_value,
        one-click re-upload) working unchanged.

        Query params:
            path: Directory to list. When empty, defaults to the user's
                home directory. On Windows, the literal string ``"/"``
                lists the available drive letters instead (there is no
                single filesystem root to show).

        Returns:
            ::

                {
                  "path":    "/home/jan/Downloads",   # normalised abs path
                  "parent":  "/home/jan",             # null at the root
                  "is_root": false,
                  "sep":     "/",                     # or "\\\\" on Windows
                  "dirs":  [{"name": "...", "path": "..."}, ...],
                  "files": [{"name": "...", "path": "...",
                             "size": 12345, "mtime": "2026-06-17T…"}, ...],
                }

            Only files with an extension the upload parser can actually
            handle are listed (csv / tsv / txt / xlsx / xlsm) — this
            dialog exists solely to pick a holdings file, so showing the
            user's photos and executables would be noise. Directories are
            always listed so navigation isn't blocked.

            Unreadable entries (permission denied, broken symlinks) are
            skipped rather than failing the whole listing.
        """
        import os
        from datetime import datetime, timezone

        # Extensions the upload parser understands. Kept in step with
        # upload_preview()'s format sniffing.
        # Factsheet documents are pickable too — the same browser backs
        # the factsheet dialog's Browse button.
        PICKABLE_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"} | set(FACTSHEET_EXTENSIONS)

        raw = (request.args.get("path") or "").strip()

        # Windows drive-list pseudo-root. There's no single "/" to show,
        # so "/" is a request for the list of drives.
        if os.name == "nt" and raw == "/":
            drives = []
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                d = f"{letter}:\\"
                if os.path.exists(d):
                    drives.append({"name": d, "path": d})
            return jsonify({
                "path": "/", "parent": None, "is_root": True,
                "sep": os.sep, "dirs": drives, "files": [],
            })

        target = Path(raw).expanduser() if raw else Path.home()
        try:
            target = target.resolve()
        except Exception:
            return jsonify({"error": f"cannot resolve path: {raw}"}), 400

        if not target.exists():
            return jsonify({"error": f"no such directory: {target}"}), 404
        if not target.is_dir():
            return jsonify({"error": f"not a directory: {target}"}), 400

        dirs, files = [], []
        try:
            entries = sorted(target.iterdir(),
                             key=lambda p: p.name.lower())
        except PermissionError:
            return jsonify({"error": f"permission denied: {target}"}), 403
        except Exception as exc:
            return jsonify({"error": f"cannot list {target}: {exc}"}), 500

        for p in entries:
            # Skip dotfiles / hidden entries — they're never holdings
            # files and they clutter the list badly on POSIX.
            if p.name.startswith("."):
                continue
            try:
                if p.is_dir():
                    dirs.append({"name": p.name, "path": str(p)})
                elif p.suffix.lower() in PICKABLE_EXT:
                    st = p.stat()
                    files.append({
                        "name":  p.name,
                        "path":  str(p),
                        "size":  st.st_size,
                        "mtime": datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc).isoformat(),
                    })
            except (OSError, PermissionError):
                # Unreadable entry (broken symlink, denied stat) — skip
                # it rather than blowing up the whole listing.
                continue

        # Parent. At a filesystem root, `parent` is the path itself; that
        # would render an "Up" entry that goes nowhere, so report None.
        # On Windows a drive root's parent is the drive-list pseudo-root.
        parent_p = target.parent
        if parent_p == target:
            parent = "/" if os.name == "nt" else None
        else:
            parent = str(parent_p)

        return jsonify({
            "path":    str(target),
            "parent":  parent,
            "is_root": parent is None,
            "sep":     os.sep,
            "dirs":    dirs,
            "files":   files,
        })

    @app.route("/api/upload/preview", methods=["POST"])
    def api_upload_preview() -> Response:
        """Resolve a source string, parse the result, return a preview + token.

        Body (JSON):
            source: A free-form URL or filesystem path. Accepts:
                * ``http://`` / ``https://`` — server fetches over HTTP(S)
                * ``file:///…`` — server reads the path the URI points to
                * Any absolute path (POSIX or Windows) — server reads it
            sheet: For XLSX, optional sheet name override (defaults to
                first non-trivial sheet).
            delimiter: For CSV, optional override (one of ``,``, ``;``,
                ``\\t``, ``|``, or the string ``"tab"``); defaults to
                auto-sniff.
        """
        body = request.get_json(force=True, silent=True) or {}
        source = (body.get("source") or "").strip()
        if not source:
            return jsonify({"error": "source is required"}), 400

        sheet     = body.get("sheet") or None
        delimiter = body.get("delimiter") or None
        if delimiter in ("\\t", "tab", "TAB"):
            delimiter = "\t"

        try:
            data = upload_preview_from_source(
                source, sheet_name=sheet, delimiter=delimiter,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"parse failed: {exc}"}), 500

        return jsonify(data)

    @app.route("/api/upload/normalise_sample", methods=["POST"])
    def api_upload_normalise_sample() -> Response:
        """Resolve a handful of mapped rows exactly as a commit would.

        The mapping dialog's preview showed the file's raw cell values,
        so a holdings file saying "Aandelen" previewed as "Aandelen"
        while the commit would store "equity" — the preview described
        the file rather than what was about to be written, which is the
        one thing it exists to show.

        Resolution stays on the server. The weight sum in that same
        dialog is a client-side mirror of the backend's arithmetic, and
        it has already drifted once; re-implementing the whole facet
        resolver in the browser would be that mistake with far more
        surface. This runs the real :func:`normalise_facets`.

        Body (JSON):
            rows: A list of ``{facet: raw_value}`` dicts — the mapped
                cells for the sample rows, unresolved.

        Returns:
            ``{rows: [{facet: {value, raw, unmatched}}]}`` — one entry
            per input row, per facet, carrying both the resolved value
            and what the file actually said so the dialog can show the
            difference.
        """
        body = request.get_json(force=True, silent=True) or {}
        rows = body.get("rows")
        if not isinstance(rows, list):
            return jsonify({"error": "rows must be a list"}), 400
        if len(rows) > 50:
            return jsonify({"error": "at most 50 rows"}), 400

        FACETS = ("asset_class", "sub_class", "sector", "country", "currency")
        out = []
        for raw_row in rows[:50]:
            if not isinstance(raw_row, dict):
                out.append({})
                continue
            probe = {f: (raw_row.get(f) or "") for f in FACETS}
            originals = dict(probe)
            _, unmatched = normalise_facets(probe)
            um = set(unmatched)
            out.append({
                f: {
                    "value":     probe.get(f) or "",
                    "raw":       str(originals.get(f) or ""),
                    "unmatched": f in um,
                }
                for f in FACETS
            })
        return jsonify({"rows": out})

    @app.route("/api/upload/prefs", methods=["GET"])
    def api_upload_prefs() -> Response:
        """Return the saved upload-dialog prefs for a fund.

        Query params:
            ticker: Yahoo ticker for the fund (required).

        Returns:
            ``{ticker, prefs: {...}}`` on hit, ``{ticker, prefs: null}``
            when the fund has never been uploaded for. The ``prefs`` dict
            mirrors what was POSTed to /commit last time, plus the source
            kind/value pulled off the preview token at that point.
        """
        ticker = (request.args.get("ticker") or "").strip()
        if not ticker:
            return jsonify({"error": "ticker is required"}), 400
        prefs = get_upload_prefs(ticker)
        return jsonify({"ticker": ticker, "prefs": prefs})

    @app.route("/api/upload/commit", methods=["POST"])
    def api_upload_commit() -> Response:
        """Apply a column mapping and write the cache for a fund.

        Body (JSON):
            token: Preview token from /api/upload/preview.
            ticker: Yahoo ticker for the fund (required — used as the
                cache key).
            isin: Optional ISIN for traceability.
            mapping: ``{name, weight, ticker, isin, sector, country,
                currency, asset_class}`` mapping each canonical field
                to a 0-based source-column index, or ``null`` to mean
                "this file has no such column". ``name`` and ``weight``
                are required.
            header_row: Zero-based row index of the header in the
                previewed grid.
            decimal: ``"dot"`` / ``"comma"`` / ``"auto"`` (default ``"auto"``).
            weight_unit: ``"percent"`` / ``"fraction"`` / ``"auto"``
                (default ``"auto"``).
            defaults: ``{field: value}`` for unmapped fields — applied to
                every row whose mapping is null. Only ``sector``,
                ``country``, ``currency``, ``asset_class`` are honoured.
            enrich_fields: list of unmapped fields (subset of ``["sector",
                "country", "currency", "asset_class"]``) for which the
                server should look up Yahoo per-symbol info on every row
                that has a ticker. Wins over the default value for rows
                where Yahoo has data; default fills the rest.
        """
        body = request.get_json(force=True, silent=True) or {}
        token  = body.get("token")
        ticker = body.get("ticker")
        if not token or not ticker:
            return jsonify({"error": "token and ticker are required"}), 400

        try:
            result = upload_commit(
                token,
                ticker=ticker,
                isin=body.get("isin"),
                mapping=body.get("mapping") or {},
                header_row=int(body.get("header_row", 0)),
                decimal=body.get("decimal", "auto"),
                weight_unit=body.get("weight_unit", "auto"),
                sheet_name=body.get("sheet"),
                delimiter=body.get("delimiter"),
                defaults=body.get("defaults") or {},
                enrich_fields=body.get("enrich_fields") or [],
            )
        except UploadCancelled:
            # User clicked Cancel mid-commit. The holdings cache was
            # never written (the cancel check fires before cache_put);
            # return 200 with a flag so the frontend can close the
            # dialog silently instead of showing an error toast.
            return jsonify({"cancelled": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"commit failed: {exc}"}), 500

        return jsonify(result)

    @app.route("/api/upload/cancel", methods=["POST"])
    def api_upload_cancel() -> Response:
        """Signal an in-flight upload commit to stop.

        Body (JSON): ``{"token": "<preview-token>"}``.

        Marks the token as cancelled in the upload module's registry.
        The active /api/upload/commit's enrichment loop polls this
        registry at each iteration and raises UploadCancelled before
        the cache write, so the on-disk holdings stay untouched.
        Always returns 200 — even if the token is already done or
        never existed, marking is harmless (the flag is cleared on
        commit exit anyway).
        """
        body = request.get_json(force=True, silent=True) or {}
        token = body.get("token")
        if not token:
            return jsonify({"error": "token is required"}), 400
        mark_cancelled(token)
        return jsonify({"ok": True})

    @app.route("/api/upload/clear", methods=["POST"])
    def api_upload_clear() -> Response:
        """Remove uploaded holdings for a fund (revert to top-10 fallback).

        Body (JSON):
            ticker: Yahoo ticker for the fund (required).
        """
        body = request.get_json(force=True, silent=True) or {}
        ticker = body.get("ticker")
        if not ticker:
            return jsonify({"error": "ticker is required"}), 400
        # Holdings are fund-level (ISIN-keyed) — resolve through the
        # listings cache's identity block.
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404
        removed = upload_clear(isin)
        return jsonify({"ticker": ticker, "removed": removed})

    # -----------------------------------------------------------------------
    # Holdings editor — patch a single holding row
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/holdings/<row_id>", methods=["PATCH"])
    def api_holdings_patch(ticker: str, row_id: str) -> Response:
        """Update one holding row in a fund's unified ``holdings`` cache slot.

        The fund's holdings table lets the user double-click a row to
        edit every field. That edit lands here.

        Path params:
            ticker: Yahoo ticker — the holdings cache key.
            row_id: The ``_row_id`` of the row to patch (minted by
                :func:`~porxpy.utils.coerce_holdings_row`).

        Body (JSON): any subset of the editable holding fields —
            ``name``, ``ticker``, ``isin``, ``sector``, ``asset_class``,
            ``sub_class``, ``country``, ``currency``, ``weight_pct``.
            Fields omitted from the body are left unchanged.
            ``weight_pct`` must be numeric — a non-numeric value is a
            400, never silently coerced.

        Provenance: if the holdings slot is Yahoo-sourced
        (``yahoo_top10`` / ``yahoo_enriched``), the very act of editing a
        row promotes the whole blob to ``source="manual_upload"`` — the
        user has curated it, so a later "Reload fund data" must not
        clobber the edit. A blob that is already ``manual_upload`` stays
        as-is.

        Returns:
            ``{ticker, row_id, row, source, weight_sum_pct, promoted,
            holdings_breakdowns, breakdowns_source}`` where ``row`` is the
            full patched row, ``source`` is the (possibly just-promoted)
            blob source, ``weight_sum_pct`` is the recomputed sum across
            all rows, ``promoted`` is True iff this call flipped a Yahoo
            blob to manual_upload, ``holdings_breakdowns`` is the
            look-through rollup recomputed from the post-edit rows, and
            ``breakdowns_source`` is its source label (full /
            top10_enriched / top10_raw / none) — the last two let the
            frontend refresh the doughnut cards in place rather than
            waiting for a full fund reload.
        """
        body = request.get_json(force=True, silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "body must be a JSON object"}), 400

        # The URL gives a ticker (the user's reference); holdings live in
        # the funds cache, keyed by ISIN. Resolve through the listings
        # cache's identity block.
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        # The source in effect is the one the user is looking at and
        # therefore the one they just edited a row of.
        hold, hold_source, hold_store = holdings_get(isin)
        if not hold.get("rows"):
            return jsonify({"error": f"no holdings cached for {ticker!r}"}), 404

        rows = hold.get("rows") or []
        idx = next((i for i, r in enumerate(rows)
                    if r.get("_row_id") == row_id), None)
        if idx is None:
            return jsonify({"error": f"row_id {row_id!r} not found"}), 404

        # Validate / collect the patch. Only schema fields are accepted;
        # anything else in the body is ignored. Numeric fields are
        # validated up front so a bad value fails cleanly with a 400
        # rather than being silently zeroed by coerce_holdings_row.
        # weight_pct is required-numeric; duration / coupon (bond fields)
        # may arrive blank from the editor, which we coerce to 0.0 — the
        # column then renders as "—" via the frontend's blank check.
        from porxpy.utils import HOLDINGS_NUMERIC_FIELDS
        # The three levelled facets are edited by NAMING A NODE, not by
        # setting one of the derived level columns. The editor sends one
        # value per facet — whatever level the user picked — under
        # ``<facet>_node``, and normalise_facets resolves it against the
        # definitions file and rewrites every level from it.
        #
        # These are admitted alongside the schema rather than added to
        # HOLDINGS_ROW_FIELDS: that tuple is the canonical COLUMN order,
        # walked by anything that renders or exports a row, and the
        # stated value is metadata that travels with the row rather than
        # a column of it.
        #
        # Before this, the gate accepted only the derived columns and a
        # special case downstream turned an incoming asset_class or
        # sub_class into a node. Anything else the user could pick was
        # accepted and then overwritten by re-derivation: a super-class
        # asset pick, and EVERY sector and country edit, were discarded
        # while the endpoint returned 200 and the freshly re-derived row.
        # The screen redrew showing the old value and said nothing.
        EDITABLE = set(HOLDINGS_ROW_FIELDS) | {
            "asset_node", "sector_node", "country_node",
        }
        patch: dict = {}
        for k, v in body.items():
            if k not in EDITABLE:
                continue
            if k in HOLDINGS_NUMERIC_FIELDS:
                if v is None or (isinstance(v, str) and not v.strip()):
                    # weight_pct is the only numeric where blank is an
                    # error — every holding must have a weight. For the
                    # bond-only numerics, blank is a valid "n/a".
                    if k == "weight_pct":
                        return jsonify(
                            {"error": "weight_pct must be a number"}), 400
                    patch[k] = 0.0
                    continue
                try:
                    patch[k] = float(v)
                except (TypeError, ValueError):
                    return jsonify(
                        {"error": f"{k} must be a number"}), 400
            else:
                patch[k] = "" if v is None else str(v)

        # Apply the patch onto the existing row, then run the whole row
        # back through coerce_holdings_row: it normalises asset_class to
        # the holding enum, lowercases / defaults sub_class, strips
        # strings, and preserves the row's _row_id (passed explicitly so
        # it can't drift) plus any provenance extras.
        old_row = rows[idx]
        merged = dict(old_row)
        merged.update(patch)
        # An edit is a deliberate human classification — drop the
        # "_defaulted" provenance list so the row is no longer treated
        # as carrying auto-filled values.
        merged.pop("_defaulted", None)

        # A levelled facet is edited by naming a node. The node is the
        # row's only assertion about that facet, so clearing the stored
        # grain is all this needs to do — coerce_holdings_row resolves
        # the node and rewrites every level from it.
        #
        # One rule, three facets. The asset-only special case this
        # replaces had to guess which of two competing columns the user
        # meant, and carried a corrective for the case where the guess
        # coarsened the row while a finer value was still sitting in the
        # form. With a single picker per facet there is no competition
        # and nothing to correct: the node outranks any stale derived
        # column that the editor happens to serialise alongside it.
        for facet in ("asset", "sector", "country"):
            if f"{facet}_node" in patch:
                merged[f"{facet}_level"] = ""   # re-resolved from the node

        new_row = coerce_holdings_row(merged, row_id=row_id)
        rows[idx] = new_row

        # Provenance: an edited list is user-curated, and a refetch must
        # not silently undo the user's work.
        #
        # Until v0.77.0 this was expressed by relabelling the blob
        # ``manual_upload`` — which worked because the slot held exactly
        # one blob, so relabelling it was the only way to say "do not
        # refetch over this". With one slot per source that same move
        # would file Yahoo's rows under Upload and overwrite a CSV the
        # user really did upload. The rows now stay in the source they
        # belong to and carry a flag instead; ``load_fund_data`` reads it
        # and leaves an edited Yahoo slot alone, which is exactly the
        # protection the relabelling used to buy.
        # Reported to the UI only for the Yahoo list, because that is the
        # only source a refresh would otherwise have overwritten — an
        # upload and a factsheet are never refetched, so nothing about
        # them changes on the first edit.
        promoted = hold_source == "yahoo" and not hold.get("user_edited")
        hold["user_edited"] = True

        # Recompute the weight sum across all rows (the editor shows it).
        weight_sum = round(
            sum(float(r.get("weight_pct") or 0.0) for r in rows), 6)
        hold["rows"]           = rows
        hold["row_count"]      = len(rows)
        hold["weight_sum_pct"] = weight_sum

        # v0.22.0 — editing a row is an update to the list.
        hold["last_updated"] = now_iso()
        holdings_put(isin, hold, hold_source, store=hold_store)

        # Recompute the look-through breakdowns from the just-mutated
        # rows and return them in the response. The breakdown cards are a
        # pure function of the holdings cache (see porxpy.breakdowns) —
        # this endpoint mutates that cache, so it must hand back the
        # freshly-derived cards, exactly as load_fund_data does on a
        # normal fund load. Without this the fund-page doughnut cards
        # would silently go stale until the next full reload.
        #
        # breakdowns_source mirrors the mapping in load_fund_data: it is
        # derived from the (possibly just-promoted) blob source, so a
        # Yahoo→manual promotion correctly flips the card label from
        # TOP·N(·ENRICHED) to FULL.
        final_source = hold.get("source")
        breakdowns_source = rollup_label_of(final_source)
        holdings_breakdowns = rollup_holdings(rows)

        # Rebuild the unified Fund/ETF-level cards too, so a card flipped
        # to the "holdings" source updates in place after a row edit.
        # Sectors and asset_allocation are fund-level (ISIN-keyed).
        patch_blob       = cache_read(isin, "sectors")
        issuer_sectors   = (patch_blob.get("sectors") or {}).get("value") or []
        issuer_alloc     = (patch_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides     = _bd_sources(isin)
        uploaded_facets  = uploaded_breakdowns_get(isin)
        fund_breakdowns  = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, uploaded_facets, _bd_presence(isin, rows),
            _bd_completed(isin))

        return jsonify({
            "ticker":              ticker,
            "row_id":              row_id,
            "row":                 new_row,
            "source":              final_source,
            "weight_sum_pct":      weight_sum,
            "promoted":            promoted,
            # Recomputed look-through breakdowns + their source label,
            # so the frontend can refresh the doughnut cards in place.
            "holdings_breakdowns": holdings_breakdowns,
            "breakdowns_source":   breakdowns_source,
            # Recomputed unified Fund/ETF-level cards + the fund's
            # current per-card overrides.
            "fund_breakdowns":     fund_breakdowns,
            "breakdown_overrides": bd_overrides,
        })

    # -----------------------------------------------------------------------
    # Holdings enrichment — "Enrich through Yahoo" button on the fund page
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/enrich_holdings", methods=["POST"])
    def api_holdings_enrich(ticker: str) -> Response:
        """Run blank-only Yahoo enrichment over a fund's cached holdings.

        Fills the per-symbol facets the user has opted into (see Settings
        → enrichment fields) for every cached row that carries a ticker.
        The fill is BLANK-ONLY: a non-empty value on the row — typically
        from a manual upload — is never overwritten. This makes the
        button safe to press repeatedly and against any holdings source
        (raw Yahoo top-10, enriched top-10, or full user upload).

        Behaviour:
            * The endpoint reads the cached blob, mutates rows in place,
              and writes the blob back. No new fetch of the top-10
              happens here — the existing rows (whatever their source)
              are what gets enriched.
            * Per-symbol Yahoo lookups go through the same cache the
              upload pipeline uses, so repeat presses are very cheap.
            * A Yahoo-sourced blob does NOT get promoted to
              ``manual_upload`` — enrichment is a Yahoo-side activity by
              definition. A blob that was already ``manual_upload``
              stays so.

        Returns:
            ``{ticker, source, weight_sum_pct, stats, holdings_rows,
            holdings_breakdowns, breakdowns_source, fund_breakdowns,
            breakdown_overrides}``. ``stats`` carries the per-field
            fill counts and skip reasons from
            :func:`~porxpy.extractors.enrich_existing_holdings`.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        # Enrichment fills blanks in the list the user is LOOKING AT,
        # and writes back into that same source. Enriching a factsheet's
        # positions must not deposit them in Yahoo's slot — the rows are
        # still the factsheet's assertion, with gaps filled in.
        hold, hold_source, hold_store = holdings_get(isin)
        if not hold.get("rows"):
            return jsonify({"error": f"no holdings cached for {ticker!r}"}), 404

        # Resolve the active enrichment field list. Settings is the
        # source of truth; an empty list means the user has unticked
        # everything and we return without touching anything (the
        # frontend still treats this as success — zero work done).
        settings = load_settings()
        fields   = list(settings.get("enrichment", {}).get("fields") or [])

        rows = hold.get("rows") or []
        rows, stats = enrich_existing_holdings(rows, fields)

        # Recompute the weight sum (rows may have been re-coerced, which
        # round-trips weight_pct through float — no actual change, but
        # cheap to recompute here for shape parity with /holdings PATCH).
        weight_sum = round(
            sum(float(r.get("weight_pct") or 0.0) for r in rows), 6)
        hold["rows"]           = rows
        hold["row_count"]      = len(rows)
        hold["weight_sum_pct"] = weight_sum

        # Persist; the blob's "source" is unchanged. Stamp an
        # "enriched_at" so the holdings tile can show "last enriched X
        # ago" if it wants to.
        hold["enriched_at"] = now_iso()
        # v0.22.0 — enrichment rewrites the rows, so it's an update
        # to the list too.
        hold["last_updated"] = now_iso()
        holdings_put(isin, hold, hold_source, store=hold_store)

        # Look-through breakdowns recomputed from the post-enrich rows.
        # The source label stays in sync with whatever the blob's source
        # is now (unchanged by enrichment).
        final_source = hold.get("source")
        breakdowns_source = rollup_label_of(final_source)
        holdings_breakdowns = rollup_holdings(rows)

        # Rebuild the unified Fund/ETF-level cards too — same rationale
        # as in api_holdings_patch: a card flipped to source "holdings"
        # depends on the holdings rollup we just recomputed.
        patch_blob       = cache_read(isin, "sectors")
        issuer_sectors   = (patch_blob.get("sectors") or {}).get("value") or []
        issuer_alloc     = (patch_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides     = _bd_sources(isin)
        uploaded_facets  = uploaded_breakdowns_get(isin)
        fund_breakdowns  = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, uploaded_facets, _bd_presence(isin, rows),
            _bd_completed(isin))

        return jsonify({
            "ticker":              ticker,
            "source":              final_source,
            "weight_sum_pct":      weight_sum,
            "stats":               stats,
            "holdings_rows":       rows,
            "holdings_breakdowns": holdings_breakdowns,
            "breakdowns_source":   breakdowns_source,
            "fund_breakdowns":     fund_breakdowns,
            "breakdown_overrides": bd_overrides,
        })

    # -----------------------------------------------------------------------
    # Per-fund asset class — set / clear the override
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/include_in_optimizer", methods=["PUT"])
    def api_fund_include_in_optimizer(ticker: str) -> Response:
        """Toggle whether a fund is offered to the optimiser.

        ISIN-keyed, so excluding a fund excludes every listing of it —
        two listings of the same fund are the same investment decision.

        Body::

            {"include": true|false}
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no cached identity for {ticker} — "
                                     f"load and save the fund first"}), 404

        body = request.get_json(force=True, silent=True) or {}
        if "include" not in body:
            return jsonify({"error": "body must contain 'include'"}), 400

        # Sparse store: asserting the default is the same as having no
        # opinion, so it withdraws the assertion rather than writing one.
        want = bool(body["include"])
        if want == DEFAULT_INCLUDE_IN_OPTIMIZER:
            override_delete(isin, "include_in_optimizer")
        else:
            override_put(isin, "include_in_optimizer", want)
        return jsonify({"ticker": ticker.upper(), "isin": isin,
                        "include_in_optimizer": want})

    # -----------------------------------------------------------------------
    # Holdings source — which of the three lists the fund shows (v0.77.0)
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/holdings_source",
               methods=["PUT", "DELETE"])
    def api_holdings_source(ticker: str) -> Response:
        """Pin (or unpin) which source supplies this fund's holdings.

        The holdings tile's selector, and the exact counterpart of
        ``/breakdown_source/<facet>`` for the four cards: the choice is a
        fund-level override keyed by ISIN, so it applies wherever the
        fund appears rather than being a view state of one browser tab.

        ``PUT`` body ``{"source": "yahoo"|"factsheet"|"upload"}``.
        ``DELETE`` withdraws the pin, returning the fund to
        :data:`~porxpy.config.HOLDINGS_SOURCE_PRECEDENCE`.

        A pin to a source the fund does not have is refused rather than
        stored — the selector strikes those through, so the only way to
        send one is out-of-band, and silently storing it would leave the
        tile showing something other than what was asked for with no way
        to tell why.

        Returns:
            ``{ticker, isin, source, pinned, available, holdings_rows,
            holdings_meta, holdings_source, holdings_breakdowns,
            breakdowns_source, fund_breakdowns, breakdown_overrides}`` —
            everything the tile and the four cards need to redraw,
            recomputed from the cache with no network call, exactly as
            the breakdown-source switch does.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if request.method == "DELETE":
            override_delete(isin, "holdings_source")
        else:
            body   = request.get_json(force=True, silent=True) or {}
            source = (body.get("source") or "").strip()
            if source not in HOLDINGS_SOURCES:
                return jsonify(
                    {"error": f"source must be one of "
                              f"{list(HOLDINGS_SOURCES)}"}), 400
            if source not in holdings_store_get(isin):
                return jsonify(
                    {"error": f"this fund has no {source} holdings"}), 409
            override_put(isin, "holdings_source", source)

        # Rebuild from the cache: the rows now in effect, their roll-up,
        # and the four cards (any of which may be reading "holdings").
        hold, active, store = holdings_get(isin)
        rows = hold.get("rows") or []
        variant = hold.get("source") or "none"
        sect_blob = cache_read(isin, "sectors")
        fund_breakdowns = build_fund_breakdowns(
            rollup_holdings(rows) if rows else {},
            (sect_blob.get("sectors") or {}).get("value") or [],
            (sect_blob.get("asset_allocation") or {}).get("value") or [],
            _bd_sources(isin), uploaded_breakdowns_get(isin),
            _bd_presence(isin, rows),
            _bd_completed(isin))

        return jsonify({
            "ticker":              ticker,
            "isin":                isin,
            "source":              active,
            "pinned":              override_get(isin, "holdings_source") or "",
            "available":           holdings_sources_available(isin, store),
            "holdings_rows":       rows,
            "holdings_source":     variant,
            "holdings_meta":       build_holdings_meta(isin, hold, active, store),
            "holdings_breakdowns": rollup_holdings(rows) if rows else {},
            "breakdowns_source":   rollup_label_of(variant),
            "fund_breakdowns":     fund_breakdowns,
            "breakdown_overrides": _bd_sources(isin),
        })

    @app.route("/api/funds/<ticker>/override/<field>",
               methods=["PUT", "DELETE"])
    def api_fund_override(ticker: str, field: str) -> Response:
        """Assert or withdraw one overridable field for a fund.

        The generic route into the per-field override store. Fields whose
        edit needs a richer response keep their own endpoint — flipping a
        breakdown card's source hands back the rebuilt cards, and saving
        the Structure block hands back the merged block — but a plain
        scalar like TER needs none of that, and adding one to the registry
        should not mean adding an endpoint.

        ``PUT``    — body ``{"value": ..., "note": "..."}``
        ``DELETE`` — withdraws the assertion, reverting to the derived
                     value. There is no stored value meaning "unset", so
                     deletion is the whole operation.

        Returns:
            ``{ticker, isin, field, value, source}``; ``value`` is None
            after a withdrawal.
        """
        if field not in OVERRIDABLE_FIELDS:
            return jsonify({"error": f"not an overridable field: {field}"}), 404

        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if request.method == "DELETE":
            cleared = override_delete(isin, field)
            return jsonify({"ticker": ticker, "isin": isin, "field": field,
                            "value": None, "cleared": cleared})

        body = request.get_json(force=True, silent=True) or {}
        if "value" not in body:
            return jsonify({"error": "body must contain 'value'"}), 400
        # Who is asserting this. Defaults to "user" — a hand-typed value —
        # but a value staged from a fetch must keep its own provenance,
        # or the fund page would report a factsheet reading as something
        # the user typed, and the "where did this come from" question
        # the override store exists to answer becomes unanswerable.
        src = (body.get("source") or "user").strip().lower()
        try:
            env = override_put(isin, field, body["value"], source=src,
                               note=body.get("note") or "")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ticker": ticker, "isin": isin, "field": field,
                        "value": env["value"], "source": env["source"]})

    @app.route("/api/funds/<ticker>/overrides", methods=["GET"])
    def api_fund_overrides(ticker: str) -> Response:
        """Every assertion stored for a fund, plus the registry.

        The registry travels with the data so the Edit dialog can build
        its inputs — type, bounds, unit, label — without a second
        vocabulary of its own that could drift from the server's.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404
        return jsonify({
            "ticker":    ticker,
            "isin":      isin,
            "overrides": overrides_for(isin),
            "fields":    {f: {k: v for k, v in spec.items()
                              if k in ("type", "vocab", "min", "max",
                                       "unit", "label")}
                          for f, spec in OVERRIDABLE_FIELDS.items()},
        })

    @app.route("/api/funds/<ticker>/asset_class", methods=["PUT", "DELETE"])
    def api_fund_asset_class(ticker: str) -> Response:
        """Set or clear the asset-class override for a fund.

        The override replaces the heuristically detected asset class. It
        is keyed by ISIN — overrides are fund-level: every listing of
        one fund shares them. The URL takes a ticker for the user's
        convenience; the handler resolves it through the listings
        cache's identity block.

        ``PUT``  — body ``{"asset_class": <one of the classification
                   keys in Primary_asset_class_definitions.csv>}`` sets
                   the override.
        ``DELETE`` — clears the override, reverting to auto-detection.

        Path params:
            ticker: Yahoo ticker.

        Returns:
            ``{ticker, asset_class}`` where ``asset_class`` is the
            override now in force, or ``None`` after a clear / when a
            ``DELETE`` found nothing to remove.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if request.method == "DELETE":
            cleared = override_delete(isin, "primary_asset_class")
            return jsonify({"ticker": ticker, "asset_class": None,
                            "cleared": cleared})

        body = request.get_json(force=True, silent=True) or {}
        ac   = body.get("asset_class")
        # Resolved, not just compared: the classification arrives from a
        # factsheet, justETF or a typed value as often as from the
        # dropdown, and "Fixed Income" is the same assertion as
        # "fixed_income". An unrecognised value is still rejected.
        from porxpy.resources import (primary_asset_classes,
                                      resolve_primary_asset_class)
        resolved = resolve_primary_asset_class(ac)
        if not resolved:
            allowed = primary_asset_classes()
            return jsonify({"error": f"asset_class must be one of {allowed}"}), 400
        ac = resolved
        override_put(isin, "primary_asset_class", ac)
        return jsonify({"ticker": ticker, "asset_class": ac})

    # -----------------------------------------------------------------------
    # Per-fund, per-card completeness assertion (v0.77.0)
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/breakdown_complete/<facet>",
               methods=["PUT", "DELETE"])
    def api_fund_breakdown_complete(ticker: str, facet: str) -> Response:
        """Assert (or withdraw) that one card's source covers the whole fund.

        For a fund whose breakdown rests on an incomplete set of holdings
        — an issuer top-10, a factsheet's largest positions — and for
        which no source can supply more, the ``unknown`` slice is not a
        gap anyone can close. Left in place it makes the fund useless to
        the optimiser, which cannot allocate against "unknown"; asserted
        away, the identified part is read as the whole fund and the
        solver has something to work with.

        Stored as a fund-level override keyed by ISIN, exactly like the
        card's source pin, because it is the same kind of statement: it
        changes what the card MEANS rather than how it is displayed. It
        therefore reaches everything that reads the fund's facet data —
        the portfolio X-ray, the target deviations, the optimiser — and
        not merely the tab it was ticked in.

        ``PUT``    — body ``{"complete": true|false}``; ``false`` is
                     equivalent to a clear (the default is "not
                     asserted", and there is no third state).
        ``DELETE`` — withdraws the assertion.

        Path params:
            ticker: Yahoo ticker.
            facet:  One of :data:`~porxpy.config.BREAKDOWN_FACETS`.

        Returns:
            ``{ticker, isin, facet, complete, completed, fund_breakdowns,
            breakdown_overrides}`` — ``completed`` is the fund's full
            ``{facet: True}`` map and ``fund_breakdowns`` the rebuilt
            cards, so the caller redraws from this reply rather than
            re-requesting the fund (which would re-resolve it upstream).
        """
        if facet not in BREAKDOWN_FACETS:
            return jsonify(
                {"error": f"facet must be one of {list(BREAKDOWN_FACETS)}"}), 400

        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        want = True
        if request.method == "DELETE":
            want = False
        else:
            body = request.get_json(force=True, silent=True) or {}
            want = bool(body.get("complete", True))

        # Sparse store: "not asserted" is the default, so withdrawing is
        # a delete rather than a stored False — the same rule
        # include_in_optimizer follows.
        if want:
            override_put(isin, f"breakdown_complete.{facet}", True)
        else:
            override_delete(isin, f"breakdown_complete.{facet}")

        rows = holdings_get(isin)[0].get("rows") or []
        sect_blob = cache_read(isin, "sectors")
        fund_breakdowns = build_fund_breakdowns(
            rollup_holdings(rows) if rows else {},
            (sect_blob.get("sectors") or {}).get("value") or [],
            (sect_blob.get("asset_allocation") or {}).get("value") or [],
            _bd_sources(isin), uploaded_breakdowns_get(isin),
            _bd_presence(isin, rows),
            _bd_completed(isin))

        return jsonify({
            "ticker":              ticker,
            "isin":                isin,
            "facet":               facet,
            "complete":            want,
            "completed":           _bd_completed(isin),
            "fund_breakdowns":     fund_breakdowns,
            "breakdown_overrides": _bd_sources(isin),
        })

    # -----------------------------------------------------------------------
    # Per-fund, per-card breakdown source — set / clear the override
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/breakdown_source/<facet>",
               methods=["PUT", "DELETE"])
    def api_fund_breakdown_source(ticker: str, facet: str) -> Response:
        """Set or clear the breakdown-source override for one fund/card.

        A fund's Fund/ETF-level breakdown card for ``facet`` normally
        shows the issuer-published aggregate (or nothing, for country /
        currency, which Yahoo never publishes). This override flips that
        card to be populated from the fund's physical holdings roll-up.
        It is keyed by ISIN — overrides are fund-level — and survives a
        "Reload fund data" — it is not Yahoo-sourced. The URL takes a
        ticker for the user's convenience and resolves to the ISIN.

        ``PUT``    — body ``{"source": "holdings"|"yahoo"|"upload"|"factsheet"}``.
                     ``"yahoo"`` is equivalent to a clear.
        ``DELETE`` — clears the override for this card.

        Path params:
            ticker: Yahoo ticker.
            facet:  One of :data:`~porxpy.config.BREAKDOWN_FACETS`.

        Returns:
            ``{ticker, facet, source, overrides, fund_breakdowns}`` —
            ``source`` is the value now in force (``"yahoo"`` after a
            clear), ``overrides`` the fund's full ``{facet: source}`` map,
            and ``fund_breakdowns`` the rebuilt cards.

        The cards are rebuilt and returned here, as the upload endpoints
        already do, so the caller does not have to re-request the fund to
        see its own change. It used to, and that was the bug: a fund
        reload arrives carrying both a ticker and an ISIN, which
        ``/api/fund`` reads as mode 2 and answers by re-validating the
        ticker against Yahoo. A rate-limited or merely unlucky Yahoo then
        failed the redraw of four cards that were already correct on
        disk, and the selector looked inert.
        """

        def _cards(isin_: str) -> dict:
            """Rebuild this fund's four cards from what is now stored."""
            rows      = holdings_get(isin_)[0].get("rows") or []
            sect_blob = cache_read(isin_, "sectors")
            return build_fund_breakdowns(
                rollup_holdings(rows) if rows else {},
                (sect_blob.get("sectors") or {}).get("value") or [],
                (sect_blob.get("asset_allocation") or {}).get("value") or [],
                _bd_sources(isin_), uploaded_breakdowns_get(isin_),
                _bd_presence(isin_, rows), _bd_completed(isin_))
        if facet not in BREAKDOWN_FACETS:
            return jsonify(
                {"error": f"facet must be one of {list(BREAKDOWN_FACETS)}"}), 400

        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if request.method == "DELETE":
            override_delete(isin, f"breakdown_source.{facet}")
            return jsonify({
                "ticker":          ticker,
                "facet":           facet,
                "source":          "yahoo",
                "overrides":       _bd_sources(isin),
                "fund_breakdowns": _cards(isin),
            })

        body   = request.get_json(force=True, silent=True) or {}
        source = body.get("source")
        if source not in BREAKDOWN_SOURCES:
            return jsonify(
                {"error": f"source must be one of {list(BREAKDOWN_SOURCES)}"}), 400
        # "fund" is the default, so selecting it withdraws the override.
        if source == "yahoo":
            override_delete(isin, f"breakdown_source.{facet}")
        else:
            override_put(isin, f"breakdown_source.{facet}", source)
        return jsonify({
            "ticker":          ticker,
            "facet":           facet,
            "source":          source,
            "overrides":       _bd_sources(isin),
            "fund_breakdowns": _cards(isin),
        })

    # -----------------------------------------------------------------------
    # Per-fund uploaded breakdowns — preview, commit, delete
    # -----------------------------------------------------------------------
    # The "Upload" source on a fund's breakdown card is populated from a
    # user-uploaded CSV. The CSV has columns ``facet, key, weight`` (any
    # order; header required) and can cover any subset of the four
    # canonical facets. Upload is two-step:
    #
    #   1. /api/funds/<ticker>/uploaded_breakdowns/preview (POST CSV).
    #      The server parses, canonicalises every (facet, key) it can,
    #      and returns either:
    #         a) a clean payload ready to commit inline (no token), or
    #         b) a token plus lists of unresolved facets / keys that
    #            the user must map in the resolution modal.
    #
    #   2. /api/funds/<ticker>/uploaded_breakdowns/commit. With the
    #      user's mapping decisions (or just the inline accepted payload
    #      when nothing was unresolved), this writes the canonical
    #      per-facet item lists into the uploaded_breakdowns cache.
    #
    # A separate DELETE wipes either one facet or the whole entry.
    #
    # Per the design (and unlike the holdings upload pipeline above):
    #
    # * No per-row drop: the user must map every unresolved item to a
    #   canonical value before commit. To exclude something, they
    #   remove it from the CSV and re-upload.
    # * No "keep as-is": the loose-vocabulary facets (country, sector)
    #   still require a canonical mapping; no free-text keys make it
    #   into the upload store.
    # * No persisted alias decisions: a future re-upload with the same
    #   typo will prompt the user again (kept narrow on purpose).

    @app.route("/api/funds/<ticker>/uploaded_breakdowns/preview",
               methods=["POST"])
    def api_uploaded_breakdowns_preview(ticker: str) -> Response:
        """Parse a breakdown CSV and report what (if anything) needs mapping.

        Accepts either a JSON body with raw CSV text, or a multipart
        upload with a ``file`` field. Returns the preview payload from
        :func:`porxpy.upload.parse_breakdown_csv_preview` — including
        the token (if any), the cleanly-resolved per-facet items, and
        the unresolved facets / keys for the resolution modal.

        Body (JSON):
            ``{"source": "https://… or C:\\path\\to\\breakdowns.csv"}``
            — resolved by the same :func:`porxpy.upload.resolve_source`
            the holdings upload uses, so a URL, a filesystem path and a
            ``file://`` URI all work and the two dialogs can be identical.
            ``{"filename": "...", "csv": "facet,key,weight\\n..."}``
        Or multipart form-data:
            ``file`` — the CSV file.

        Returns:
            ``{token?, filename, accepted, unresolved_facets,
              unresolved_keys, row_count, warnings}``.
            HTTP 400 on parse / structural errors.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        filename = "breakdowns.csv"
        data: bytes | None = None

        if request.files and "file" in request.files:
            f = request.files["file"]
            filename = f.filename or filename
            data = f.read() or b""
        else:
            body = request.get_json(force=True, silent=True) or {}
            source = (body.get("source") or "").strip()
            csv_text = body.get("csv")
            if source:
                from porxpy.upload import resolve_source
                try:
                    fname, data, _kind = resolve_source(source)
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 400
                filename = fname or filename
            elif isinstance(csv_text, str) and csv_text:
                filename = (body.get("filename") or filename).strip() or filename
                data = csv_text.encode("utf-8")

        if not data:
            return jsonify({"error": "no CSV content supplied (use multipart "
                                     "'file' or JSON 'csv' string)"}), 400

        try:
            preview = parse_breakdown_csv_preview(filename, data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"preview failed: {exc}"}), 500

        return jsonify({**preview, "ticker": ticker, "isin": isin})

    @app.route("/api/funds/<ticker>/uploaded_breakdowns/commit",
               methods=["POST"])
    def api_uploaded_breakdowns_commit(ticker: str) -> Response:
        """Commit a breakdown CSV upload after resolution.

        Body (JSON):
            token: Optional preview token (omit when nothing was
                unresolved and the inline ``accepted`` payload is being
                sent).
            accepted: Per-facet accepted items from the preview
                (only honoured when token is absent).
            facet_map: ``{raw_facet: canonical_facet}`` — every entry
                in the preview's ``unresolved_facets`` must appear here.
            key_map: ``{facet: {raw_key: canonical_key}}`` — every
                entry in the preview's ``unresolved_keys`` must appear
                here.

        Returns:
            ``{ticker, isin, facets, weights, summary, fund_breakdowns,
              breakdown_overrides}`` — the persisted per-facet item
            lists plus a freshly-rebuilt ``fund_breakdowns`` block so
            the frontend can refresh the card without a second
            round-trip.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        body = request.get_json(force=True, silent=True) or {}
        token    = body.get("token")
        accepted = body.get("accepted")
        facet_map = body.get("facet_map")
        key_map   = body.get("key_map")

        try:
            result = commit_breakdown_upload(
                isin, token=token, inline_accepted=accepted,
                facet_map=facet_map, key_map=key_map,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"commit failed: {exc}"}), 500

        # Rebuild fund_breakdowns so the frontend can update the cards
        # in place — same pattern as api_holdings_patch.
        rows = holdings_get(isin)[0].get("rows") or []
        holdings_breakdowns = rollup_holdings(rows) if rows else {}
        sectors_blob  = cache_read(isin, "sectors")
        issuer_sectors = (sectors_blob.get("sectors") or {}).get("value") or []
        issuer_alloc   = (sectors_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides   = _bd_sources(isin)
        # Read the supplied breakdowns back from the store rather than
        # reusing the commit's return value: that is the flat
        # {facet: items} for the source just written, and the cards need
        # the source-keyed {facet: {source: items}} across every source.
        fund_breakdowns = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, uploaded_breakdowns_get(isin),
            _bd_presence(isin, rows),
            _bd_completed(isin))

        return jsonify({
            "ticker":              ticker,
            "isin":                isin,
            "facets":              result["facets"],
            "weights":             result["weights"],
            "summary":             result["summary"],
            "fund_breakdowns":     fund_breakdowns,
            "breakdown_overrides": bd_overrides,
        })

    @app.route("/api/funds/<ticker>/uploaded_breakdowns/<facet>",
               methods=["DELETE"])
    def api_uploaded_breakdowns_delete_facet(ticker: str, facet: str) -> Response:
        """Clear the uploaded items for one facet (the other three survive).

        Path params:
            ticker: Yahoo ticker.
            facet:  One of :data:`~porxpy.config.BREAKDOWN_FACETS`, or
                    the literal ``"all"`` to clear every facet at once.

        Clears the ``upload`` source only. A factsheet extraction is the
        other supplied source for the same facet and is a separate
        assertion about the fund; it is cleared by deleting the factsheet.

        Side effect: any card still pinned to source ``"upload"`` for a
        now-empty facet has its override entry cleared, so the stored
        state matches what ``build_fund_breakdowns``' graceful-fallback
        rule would render anyway (source ``"yahoo"``).
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if facet == "all":
            removed = _clear_supplied_source(isin, "upload", None)
        elif facet in BREAKDOWN_FACETS:
            removed = _clear_supplied_source(isin, "upload", facet)
        else:
            return jsonify(
                {"error": f"facet must be one of "
                          f"{list(BREAKDOWN_FACETS) + ['all']}"}), 400

        supplied_now = uploaded_breakdowns_get(isin)

        # Rebuild fund_breakdowns so the frontend can update in place.
        rows = holdings_get(isin)[0].get("rows") or []
        holdings_breakdowns = rollup_holdings(rows) if rows else {}
        sectors_blob  = cache_read(isin, "sectors")
        issuer_sectors = (sectors_blob.get("sectors") or {}).get("value") or []
        issuer_alloc   = (sectors_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides   = _bd_sources(isin)
        fund_breakdowns = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, supplied_now, _bd_presence(isin, rows),
            _bd_completed(isin))

        return jsonify({
            "ticker":              ticker,
            "isin":                isin,
            "facet":               facet,
            "removed":             removed,
            "fund_breakdowns":     fund_breakdowns,
            "breakdown_overrides": bd_overrides,
        })

    @app.route("/api/uploaded_breakdowns/canonical/<facet>", methods=["GET"])
    def api_uploaded_breakdowns_canonical(facet: str) -> Response:
        """Return the canonical value list for ``facet`` (for dropdowns).

        Used by the resolution modal's "map to" dropdowns. Returns
        ``[{"key", "label"}, ...]`` sorted by label.
        """
        if facet not in BREAKDOWN_FACETS:
            return jsonify(
                {"error": f"facet must be one of {list(BREAKDOWN_FACETS)}"}), 400
        return jsonify({
            "facet":  facet,
            "values": list_canonical_values(facet),
        })

    # -----------------------------------------------------------------------
    # Per-fund "Structure" metadata — set / clear the override
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/structure", methods=["PUT", "DELETE"])
    def api_fund_structure(ticker: str) -> Response:
        """Set or clear the "Structure" override for a fund.

        The Structure block — ``{structure, replication, style}`` —
        describes how the fund is built. Yahoo's quoteType seeds
        defaults, but the user can override any of the three attributes
        via the "Edit fund" dialog. The override is keyed by Yahoo
        ticker, applies wherever the fund is loaded, and survives a
        "Reload fund data" — it is not Yahoo-sourced.

        ``PUT``    — body ``{"structure", "replication", "style"}``
                     (partial dicts are tolerated; missing attributes
                     fall back to ``"unknown"``). The structure/
                     replication coupling is enforced server-side: a
                     non-ETF always stores replication ``"n/a"``.
        ``DELETE`` — clears the override, reverting to the Yahoo seed.

        Path params:
            ticker: Yahoo ticker.

        Returns:
            ``{ticker, fund_structure, cleared?}`` — ``fund_structure``
            is the normalised block now stored (``None`` after a clear).
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if request.method == "DELETE":
            cleared = any(override_delete(isin, f)
                          for f in DEFAULT_FUND_STRUCTURE)
            return jsonify({"ticker": ticker, "fund_structure": None,
                            "cleared": cleared})

        body = request.get_json(force=True, silent=True) or {}
        # Accept either a flat {structure,replication,style} body or a
        # nested {"fund_structure": {...}} body.
        raw = body.get("fund_structure") if isinstance(
            body.get("fund_structure"), dict) else body
        block = normalise_fund_structure(raw)

        # Store only genuine divergence from the seed.
        #
        # A submitted value that already equals the seed is agreement, not
        # an assertion, so its override is withdrawn and the field tracks
        # the seed from here on. Without this, "Reload from Yahoo" was
        # self-defeating: it filled the form with Yahoo's answers, and
        # saving wrote those answers back as user overrides, pinning the
        # fields to a snapshot of Yahoo rather than following it.
        #
        # v0.33.0: withdrawal is now a delete rather than storing a
        # neutral value, so the file records only deliberate exceptions.
        # Per-field provenance from the client: which source supplied each
        # staged value. Absent means the user typed it.
        sources_in = body.get("sources") if isinstance(body.get("sources"), dict) else {}

        from porxpy.extractors import _seed_fund_structure, _merge_fund_structure
        prof = (cache_read(ticker, "profile").get("profile") or {}).get("value") or {}
        # Asset class is a fund-level (ISIN-keyed) category. Read-only —
        # this endpoint must never trigger a Yahoo round-trip — and
        # optional: without it the seed simply skips the cash/fixed-
        # income inferences.
        _ac = ((cache_read(isin, "asset_class").get("asset_class") or {})
               .get("value") or {}).get("class")
        seed, seed_origins = _seed_fund_structure(prof, _ac)

        for field, seed_val in seed.items():
            submitted = block.get(field)
            # A field the user explicitly fetched from a source is stored
            # even when it matches the seed.
            #
            # Withdrawing on agreement is right for a hand-typed value —
            # it keeps the field tracking the seed instead of pinning a
            # snapshot. It is wrong for a fetch: "justETF says unknown"
            # is an answer about justETF, and "unknown" is usually what
            # the seed says too, so the override was deleted and the
            # source silently reverted to Yahoo. The user asked a
            # question and the answer disappeared.
            explicit = field in sources_in
            if submitted == seed_val and not explicit:
                override_delete(isin, field)
            else:
                try:
                    override_put(isin, field, submitted,
                                 source=(sources_in.get(field) or "user"),
                                 context=block)
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 400

        stored = {f: e["value"] for f, e in overrides_for(isin).items()
                  if f in DEFAULT_FUND_STRUCTURE} or None

        # Return the EFFECTIVE block (Yahoo seed with the override merged
        # per field), not just what was stored. The caller needs it to
        # update the fund-meta tile directly; previously it had to trigger
        # a full re-fetch to see its own write, and that re-fetch was
        # error-swallowed — so a failed reload looked exactly like a failed
        # save.
        effective, sources = _merge_fund_structure(
            seed,
            {f: e for f, e in overrides_for(isin).items()
             if f in DEFAULT_FUND_STRUCTURE},
            seed_origins)
        return jsonify({"ticker": ticker,
                        "fund_structure": effective,
                        "fund_structure_stored": stored,
                        "fund_structure_sources": sources,
                        "fund_structure_seed": seed})

    # -----------------------------------------------------------------------
    # Assisted lookup of replication method + style (justETF, by ISIN)
    # -----------------------------------------------------------------------
    @app.route("/api/funds/<ticker>/structure_lookup", methods=["GET"])
    def api_fund_structure_lookup(ticker: str) -> Response:
        """Suggest replication method + style for a fund from justETF.

        A best-effort convenience for the "Edit fund" dialog: given the
        fund's ISIN, this consults justETF and returns *suggestions* for
        the replication method and management style, each with the
        source and a confidence grade. It never persists anything — the
        user reviews the suggestion and saves it through the normal
        Structure override.

        The ISIN is resolved from the fund's cached profile. A
        ``?isin=`` query parameter overrides it (useful when the fund
        is not cached yet).

        Path params:
            ticker: Yahoo ticker (used to locate the cached profile).

        Query params:
            isin: optional — ISIN to look up directly.

        Returns:
            The dict from :func:`extractors.lookup_fund_structure` —
            ``{ok, source, url, replication, style, note}``. ``ok`` is
            ``False`` (with an explanatory ``note``) when the fund is
            not on justETF or the page could not be parsed; this is a
            normal outcome, not an error, so the HTTP status is still
            200.
        """
        from porxpy.extractors import lookup_fund_structure

        isin = (request.args.get("isin") or "").strip().upper()
        if not isin:
            # Fallback 1: the identity block in the listings cache (the
            # authoritative ticker→ISIN link stamped by the fetch route).
            isin = listing_identity_lookup_isin(ticker)
        if not isin:
            # Fallback 2: PorxPy's own ISIN→ticker resolution store
            # (isin_map.json). Reverse-scan it for an entry whose
            # resolved ticker matches — its key carries the ISIN. This
            # is more reliable than Yahoo's profile.isin because it is
            # exactly the ISIN the user (or OpenFIGI) resolved the fund
            # from in the first place.
            tk_up = (ticker or "").strip().upper()
            for key, entry in (load_isin_map() or {}).items():
                if (entry or {}).get("ticker", "").upper() == tk_up:
                    # key is "ISIN|MIC"
                    isin = key.split("|", 1)[0].strip().upper()
                    if isin:
                        break

        result = lookup_fund_structure(isin)
        result["ticker"] = ticker
        return jsonify(result)

    return app
