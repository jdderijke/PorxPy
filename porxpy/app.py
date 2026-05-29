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

import math
import re
import uuid

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

from porxpy import NAME, VERSION, BUILD_DATE
from porxpy.config import (
    ASSET_CLASSES,
    BASE_DIR,
    BREAKDOWN_FACETS,
    BREAKDOWN_SOURCES,
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
    aggregate_portfolio_holdings,
    build_fund_breakdowns,
    canonicalise_facet_key,
    rollup_holdings,
    rollup_portfolio_fundlevel,
    rollup_portfolio_lookthrough,
)
from porxpy.utils import (
    age_days,
    asset_class_override_delete,
    asset_class_override_put,
    breakdown_override_delete,
    breakdown_override_put,
    breakdown_overrides_get,
    cache_purge,
    cache_read,
    cache_write,
    coerce_holdings_row,
    delete_portfolio,
    find_portfolio,
    fund_structure_delete,
    fund_structure_get,
    fund_structure_put,
    fx_history,
    holdings_status_from_cache,
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
            "regions": [<distinct mstar_region>, ...]}``.
        """
        from porxpy.resources import MSTAR_TO_REGION
        return jsonify({
            "country_to_region": MSTAR_TO_REGION,
            "regions":           sorted(set(MSTAR_TO_REGION.values())),
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
        from porxpy.resources import HOLDINGS_CLASS_ROWS, HOLDINGS_CLASS_INDEX
        return jsonify({
            "rows":           HOLDINGS_CLASS_ROWS,
            "by_asset_class": HOLDINGS_CLASS_INDEX,
        })

    @app.route("/api/resources/sectors")
    def api_resources_sectors() -> Response:
        """Return the Morningstar 11-sector taxonomy.

        Drives the Sector dropdown in the edit-holding modal and the
        upload normaliser (``"Information Technology"`` → ``"technology"``).
        """
        from porxpy.resources import SECTORS_ROWS
        return jsonify({"rows": SECTORS_ROWS})

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
        return jsonify({
            "portfolios":        load_portfolios(),
            "cache_categories":  CACHE_CATEGORIES,
            "default_cache_cfg": DEFAULT_CACHE_CONFIG,
            "asset_classes":     ASSET_CLASSES,
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
        """Update the shares held for a fund within a portfolio.

        Shares are the only per-portfolio, per-fund property under the
        slim portfolio model (0.12.0+). The asset class and other
        identity / metadata are fund-level — see
        ``PUT /api/funds/<ticker>/asset_class``.

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
        try:
            persisted = portfolio_targets_put(pid, raw if isinstance(raw, dict) else {})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"targets": persisted})

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
            reg = (r.get("mstar_region") or "").strip()
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
        :func:`porxpy.resources.list_fund_asset_classes`. These keys
        (equity / fixed_income / cash / mixed / commodity / other) are
        exactly what the portfolio rollup emits, so a target set here
        compares correctly against the actual exposure. (The holdings
        CSV-upload modal uses a different endpoint — the finer holdings
        vocabulary — on purpose.)

        Returns:
            ``{values: [{"key", "label"}, ...]}`` in CSV/display order.
        """
        from porxpy.resources import list_fund_asset_classes
        return jsonify({"values": list_fund_asset_classes()})

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
                    data = load_fund_data(
                        isin, exchange, cache_cfg,
                        force_refresh=force, known_ticker=ticker_q,
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

        # Back-compat aliases — the original response keys, now derived
        # from the unified rollup. asset_class_breakdown uses ``class``
        # and sector_breakdown uses ``sector`` as their key field, as
        # before; currency_breakdown uses ``currency``.
        asset_class_breakdown = [
            {"class": it["key"], "weight": it["weight"], "value": it["value"]}
            for it in fundlevel_breakdowns.get("asset_class", [])
        ]
        sector_breakdown = [
            {"sector": it["key"], "weight": it["weight"]}
            for it in fundlevel_breakdowns.get("sector", [])
        ]
        currency_breakdown = [
            {"currency": it["key"], "weight": it["weight"], "value": it["value"]}
            for it in fundlevel_breakdowns.get("currency", [])
        ]

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

        # ──────────────────────────────────────────────────────────────
        # Look-through breakdowns — aggregate each fund's per-position
        # rollup (data.holdings_breakdowns) into portfolio-level rollups,
        # weighted by base-currency value. The derivation itself lives in
        # :func:`porxpy.breakdowns.rollup_portfolio_lookthrough` — the
        # same pure module that produces the fund-page cards — so the
        # portfolio cards are guaranteed consistent with the fund cards
        # and can never go stale (the cache is the only source; this is
        # recomputed on every /view read).
        # ──────────────────────────────────────────────────────────────
        lt = rollup_portfolio_lookthrough(enriched, total_base)
        lookthrough_breakdowns = lt["lookthrough_breakdowns"]
        lookthrough_coverage   = lt["lookthrough_coverage"]

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
            # Back-compat aliases (derived from fundlevel_breakdowns).
            "asset_class_breakdown": asset_class_breakdown,
            "sector_breakdown":      sector_breakdown,
            "currency_breakdown":    currency_breakdown,
            "unvalued_funds":        unvalued_funds,
            # Fund/ETF-level breakdown cards — the four facets aggregated
            # from each fund's data.fund_breakdowns (issuer data + any
            # per-card holdings override). coverage[facet] = covered /
            # total so the frontend can show "X% covered" per card.
            "fundlevel_breakdowns":  fundlevel_breakdowns,
            "fundlevel_coverage":    fundlevel_coverage,
            # Trading-currency exposure of the fund wrappers themselves
            # (distinct from the look-through currency breakdown).
            "trading_currency_breakdown": trading_currency_breakdown,
            # Look-through rollups — same four facets, normalised against
            # COVERED portfolio value. coverage[facet] = covered / total
            # so the frontend can render "X% covered" alongside each
            # chart and toggle Yahoo-meta vs look-through views without
            # another round-trip.
            "lookthrough_breakdowns": lookthrough_breakdowns,
            "lookthrough_coverage":   lookthrough_coverage,
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
                  "asset_class": "equity", "sub_class": "shares",
                  "sector": "ai hype",  "country": "us", "currency": "...",
                  "duration": 0, "coupon": 0, "maturity": "",
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
                if not isinstance(holdings, dict):
                    continue
                rows = holdings.get("rows") or []
                if not isinstance(rows, list):
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
                    out_rows.append({
                        "fund_isin":      isin,
                        "row_id":         rid,
                        "unmatched":      list(unmatched),
                        "name":           r.get("name") or "",
                        "ticker":         r.get("ticker") or "",
                        "isin":           r.get("isin") or "",
                        "weight_pct":     float(r.get("weight_pct") or 0),
                        "asset_class":    r.get("asset_class") or "",
                        "sub_class":      r.get("sub_class") or "",
                        "sector":         r.get("sector") or "",
                        "country":        r.get("country") or "",
                        "currency":       r.get("currency") or "",
                        "duration":       float(r.get("duration") or 0),
                        "coupon":         float(r.get("coupon") or 0),
                        "maturity":       r.get("maturity") or "",
                    })

        # Sort by weight descending — biggest holdings matter most;
        # frontend can re-sort but this is the right default.
        out_rows.sort(key=lambda x: x["weight_pct"], reverse=True)
        return jsonify({"rows": out_rows, "total": len(out_rows)})

    def _rewrite_holdings_class_pair(raw_asset_class: str,
                                     raw_sub_class: str,
                                     canonical_sub_class: str) -> int:
        """Rewrite cached rows matching the (raw_ac, raw_sub) pair to the chosen pair.

        Scoped by the raw (asset_class, sub_class) pair the user is
        resolving, NOT just the raw sub_class. The per-pair scope
        matters because the same raw sub_class can legitimately
        appear under different asset_classes — e.g. ``shares`` showing
        up on both cash rows and other rows — and each batch may need
        a different target pair.

        Args:
            raw_asset_class: The raw asset_class side of the pair the
                user is resolving. Empty string means "match rows
                regardless of asset_class" (used by older callers
                without pair context — kept for safety).
            raw_sub_class: The raw sub_class side of the pair.
            canonical_sub_class: The chosen canonical sub_class. The
                target asset_class is derived from this via
                HOLDINGS_CLASS_ROWS.

        Returns:
            Number of rows rewritten across all fund-cache files.
        """
        from porxpy.resources import HOLDINGS_CLASS_ROWS
        from porxpy.utils import cache_read, cache_write

        target_ac  = None
        target_sub = canonical_sub_class.strip().lower()
        for r in HOLDINGS_CLASS_ROWS:
            if r.get("sub_class", "").lower() == target_sub:
                target_ac = r.get("asset_class", "").lower()
                break
        if not target_ac:
            return 0

        needle_sub = raw_sub_class.strip().lower()
        needle_ac  = (raw_asset_class or "").strip().lower()
        n_rewrites = 0
        if FUNDS_DIR.exists():
            for fp in FUNDS_DIR.glob("*.json"):
                isin = fp.stem.upper()
                blob = cache_read(isin, "holdings")
                holdings = (blob.get("holdings") or {}).get("value") or {}
                rows = holdings.get("rows") or []
                if not isinstance(rows, list):
                    continue
                touched = False
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row_sub = (row.get("sub_class") or "").strip().lower()
                    row_ac  = (row.get("asset_class") or "").strip().lower()
                    if row_sub != needle_sub:
                        continue
                    # Empty needle_ac = match any asset_class (legacy
                    # safety hatch); otherwise scope strictly.
                    if needle_ac and row_ac != needle_ac:
                        continue
                    if row_ac == target_ac and row_sub == target_sub:
                        continue   # both already correct
                    row["asset_class"] = target_ac
                    row["sub_class"]   = target_sub
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
            add_holdings_class_alias, add_sector_alias, add_currency_alias,
            add_country_alias, country_to_mstar, RESOURCE_VERSIONS,
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
                elif facet == "sub_class":
                    ok = add_holdings_class_alias(canon, raw)
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
                    n_rewrites = _rewrite_holdings_class_pair(raw_ac, raw, canon)
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

        return jsonify({
            "results":  results,
            "versions": dict(RESOURCE_VERSIONS),
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
            HOLDINGS_CLASS_ROWS and writes both, so the resulting pair
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
            HOLDINGS_CLASS_ROWS, add_sector_alias, add_currency_alias,
            add_country_alias, add_holdings_class_alias,
        )

        body = request.get_json(force=True, silent=True) or {}
        updates = body.get("updates") or []
        add_aliases = bool(body.get("add_aliases", True))
        if not isinstance(updates, list) or not updates:
            return jsonify({"error": "updates list is required"}), 400

        # Index canonical sub_class → asset_class for quick lookup.
        sub_to_ac: dict[str, str] = {}
        for r in HOLDINGS_CLASS_ROWS:
            sub_to_ac[(r.get("sub_class") or "").lower()] = (r.get("asset_class") or "").lower()

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
            rows = holdings.get("rows") or []
            if not isinstance(rows, list):
                errors.append({"fund_isin": isin, "error": "rows not a list"})
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
                    if not new_val:
                        continue
                    raw = (row.get(facet) or "").strip().lower()
                    if raw and add_aliases:
                        alias_candidates[(facet, raw)] = new_val
                    if facet == "sub_class":
                        # Paired write — also fix asset_class to match.
                        row["sub_class"] = new_val.lower()
                        target_ac = sub_to_ac.get(new_val.lower())
                        if target_ac:
                            row["asset_class"] = target_ac
                    else:
                        row[facet] = new_val.lower() if facet != "currency" else new_val.upper()
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
        }
        if add_aliases:
            for (facet, raw), canon in alias_candidates.items():
                if raw == canon.lower():
                    continue   # raw already equals canonical
                try:
                    if facet == "sector":
                        if add_sector_alias(canon, raw):
                            aliases_added["sector"] += 1
                    elif facet == "sub_class":
                        if add_holdings_class_alias(canon, raw):
                            aliases_added["sub_class"] += 1
                    elif facet == "currency":
                        if add_currency_alias(canon, raw):
                            aliases_added["currency"] += 1
                    elif facet == "country":
                        if add_country_alias(canon, raw):
                            aliases_added["country"] += 1
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
                data = load_fund_data(
                    isin, exchange, cache_cfg,
                    force_refresh=force, known_ticker=ticker_q,
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

        blob = cache_read(isin, "holdings")
        entry = blob.get("holdings") or {}
        hold  = entry.get("value") or {}
        if not isinstance(hold, dict) or not hold.get("rows"):
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
        EDITABLE = set(HOLDINGS_ROW_FIELDS)
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

        # Sub-class follows asset-class. The editor sends every field on
        # every save, so the body always carries both — we can't tell
        # "user touched sub_class" from the body alone. The rule: if the
        # asset class changed but the sub class did NOT, the existing
        # sub class is now stale (it was the default for the *old* asset
        # class) — blank it so coerce_holdings_row re-defaults it from
        # the new asset class. If the user did change sub_class, their
        # value is respected as-is.
        if "asset_class" in patch:
            from porxpy.utils import normalize_holding_asset_class
            old_ac = normalize_holding_asset_class(old_row.get("asset_class"))
            new_ac = normalize_holding_asset_class(patch.get("asset_class"))
            old_sc = (old_row.get("sub_class") or "").strip().lower()
            new_sc = (patch.get("sub_class") or "").strip().lower()
            if new_ac != old_ac and new_sc == old_sc:
                merged["sub_class"] = ""   # re-default from the new AC

        new_row = coerce_holdings_row(merged, row_id=row_id)
        rows[idx] = new_row

        # Provenance promotion: a Yahoo-sourced blob becomes a manual
        # upload the moment the user edits any row.
        promoted = False
        if hold.get("source") in ("yahoo_top10", "yahoo_enriched"):
            hold["source"]    = "manual_upload"
            hold["_provider"] = "manual"
            hold["uploaded_at"] = now_iso()
            # No file behind a promoted blob — make that explicit so the
            # holdings-card notice strip doesn't show a stale filename.
            hold.setdefault("filename", None)
            promoted = True

        # Recompute the weight sum across all rows (the editor shows it).
        weight_sum = round(
            sum(float(r.get("weight_pct") or 0.0) for r in rows), 6)
        hold["rows"]           = rows
        hold["row_count"]      = len(rows)
        hold["weight_sum_pct"] = weight_sum

        cache_write(isin, "holdings", blob)

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
        if final_source == "manual_upload":
            breakdowns_source = "full"
        elif final_source == "yahoo_enriched":
            breakdowns_source = "top10_enriched"
        elif final_source == "yahoo_top10":
            breakdowns_source = "top10_raw"
        else:
            breakdowns_source = "none"
        holdings_breakdowns = rollup_holdings(rows)

        # Rebuild the unified Fund/ETF-level cards too, so a card flipped
        # to the "holdings" source updates in place after a row edit.
        # Sectors and asset_allocation are fund-level (ISIN-keyed).
        patch_blob       = cache_read(isin, "sectors")
        issuer_sectors   = (patch_blob.get("sectors") or {}).get("value") or []
        issuer_alloc     = (patch_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides     = breakdown_overrides_get(isin)
        uploaded_facets  = uploaded_breakdowns_get(isin)
        fund_breakdowns  = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, uploaded_facets)

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

        blob  = cache_read(isin, "holdings")
        entry = blob.get("holdings") or {}
        hold  = entry.get("value") or {}
        if not isinstance(hold, dict) or not hold.get("rows"):
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
        cache_write(isin, "holdings", blob)

        # Look-through breakdowns recomputed from the post-enrich rows.
        # The source label stays in sync with whatever the blob's source
        # is now (unchanged by enrichment).
        final_source = hold.get("source")
        if final_source == "manual_upload":
            breakdowns_source = "full"
        elif final_source == "yahoo_enriched":
            breakdowns_source = "top10_enriched"
        elif final_source == "yahoo_top10":
            breakdowns_source = "top10_raw"
        else:
            breakdowns_source = "none"
        holdings_breakdowns = rollup_holdings(rows)

        # Rebuild the unified Fund/ETF-level cards too — same rationale
        # as in api_holdings_patch: a card flipped to source "holdings"
        # depends on the holdings rollup we just recomputed.
        patch_blob       = cache_read(isin, "sectors")
        issuer_sectors   = (patch_blob.get("sectors") or {}).get("value") or []
        issuer_alloc     = (patch_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides     = breakdown_overrides_get(isin)
        uploaded_facets  = uploaded_breakdowns_get(isin)
        fund_breakdowns  = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, uploaded_facets)

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
    @app.route("/api/funds/<ticker>/asset_class", methods=["PUT", "DELETE"])
    def api_fund_asset_class(ticker: str) -> Response:
        """Set or clear the asset-class override for a fund.

        The override replaces the heuristically detected asset class. It
        is keyed by ISIN — overrides are fund-level: every listing of
        one fund shares them. The URL takes a ticker for the user's
        convenience; the handler resolves it through the listings
        cache's identity block.

        ``PUT``  — body ``{"asset_class": <one of ASSET_CLASSES>}`` sets
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
            cleared = asset_class_override_delete(isin)
            return jsonify({"ticker": ticker, "asset_class": None,
                            "cleared": cleared})

        body = request.get_json(force=True, silent=True) or {}
        ac   = body.get("asset_class")
        if ac not in ASSET_CLASSES:
            return jsonify({"error": f"asset_class must be one of {ASSET_CLASSES}"}), 400
        asset_class_override_put(isin, ac)
        return jsonify({"ticker": ticker, "asset_class": ac})

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

        ``PUT``    — body ``{"source": "holdings"}`` (or ``"fund"``).
                     ``"fund"`` is equivalent to a clear.
        ``DELETE`` — clears the override for this card.

        Path params:
            ticker: Yahoo ticker.
            facet:  One of :data:`~porxpy.config.BREAKDOWN_FACETS`.

        Returns:
            ``{ticker, facet, source, overrides}`` — ``source`` is the
            value now in force (``"fund"`` after a clear) and
            ``overrides`` is the fund's full ``{facet: source}`` map
            after the change.
        """
        if facet not in BREAKDOWN_FACETS:
            return jsonify(
                {"error": f"facet must be one of {list(BREAKDOWN_FACETS)}"}), 400

        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if request.method == "DELETE":
            breakdown_override_delete(isin, facet)
            return jsonify({
                "ticker":    ticker,
                "facet":     facet,
                "source":    "fund",
                "overrides": breakdown_overrides_get(isin),
            })

        body   = request.get_json(force=True, silent=True) or {}
        source = body.get("source")
        if source not in BREAKDOWN_SOURCES:
            return jsonify(
                {"error": f"source must be one of {list(BREAKDOWN_SOURCES)}"}), 400
        breakdown_override_put(isin, facet, source)
        return jsonify({
            "ticker":    ticker,
            "facet":     facet,
            "source":    source,
            "overrides": breakdown_overrides_get(isin),
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
            csv_text = body.get("csv")
            if isinstance(csv_text, str) and csv_text:
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
        holdings_blob = cache_read(isin, "holdings")
        hold_value    = (holdings_blob.get("holdings") or {}).get("value") or {}
        rows          = hold_value.get("rows") or []
        holdings_breakdowns = rollup_holdings(rows) if rows else {}
        sectors_blob  = cache_read(isin, "sectors")
        issuer_sectors = (sectors_blob.get("sectors") or {}).get("value") or []
        issuer_alloc   = (sectors_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides   = breakdown_overrides_get(isin)
        fund_breakdowns = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, result["facets"])

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

        Side effect: if this clear leaves any cards still set to source
        ``"upload"`` for the now-empty facet, ``build_fund_breakdowns``'
        graceful-fallback rule kicks in on the next read — the card
        falls back to source ``"fund"``. We don't auto-clear the
        breakdown_source override entry here; the user can flip it
        explicitly if they want, but the fallback keeps the card
        rendering sensibly in the meantime.
        """
        isin = listing_identity_lookup_isin(ticker)
        if not isin:
            return jsonify({"error": f"no identity recorded for {ticker!r}; "
                                     "refetch the fund first"}), 404

        if facet == "all":
            removed = uploaded_breakdowns_delete(isin, None)
        elif facet in BREAKDOWN_FACETS:
            removed = uploaded_breakdowns_delete(isin, facet)
        else:
            return jsonify(
                {"error": f"facet must be one of "
                          f"{list(BREAKDOWN_FACETS) + ['all']}"}), 400

        # Also clear any source=upload override entries that no longer
        # have data to back them. The fallback in build_fund_breakdowns
        # would render those as "fund" anyway, but explicitly clearing
        # makes the stored state match what's actually in effect.
        current_overrides = breakdown_overrides_get(isin)
        upload_now        = uploaded_breakdowns_get(isin)
        for f, src in list(current_overrides.items()):
            if src == "upload" and not upload_now.get(f):
                breakdown_override_delete(isin, f)

        # Rebuild fund_breakdowns so the frontend can update in place.
        holdings_blob = cache_read(isin, "holdings")
        hold_value    = (holdings_blob.get("holdings") or {}).get("value") or {}
        rows          = hold_value.get("rows") or []
        holdings_breakdowns = rollup_holdings(rows) if rows else {}
        sectors_blob  = cache_read(isin, "sectors")
        issuer_sectors = (sectors_blob.get("sectors") or {}).get("value") or []
        issuer_alloc   = (sectors_blob.get("asset_allocation") or {}).get("value") or []
        bd_overrides   = breakdown_overrides_get(isin)
        fund_breakdowns = build_fund_breakdowns(
            holdings_breakdowns, issuer_sectors, issuer_alloc,
            bd_overrides, upload_now)

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
            cleared = fund_structure_delete(isin)
            return jsonify({"ticker": ticker, "fund_structure": None,
                            "cleared": cleared})

        body = request.get_json(force=True, silent=True) or {}
        # Accept either a flat {structure,replication,style} body or a
        # nested {"fund_structure": {...}} body.
        raw = body.get("fund_structure") if isinstance(
            body.get("fund_structure"), dict) else body
        block = normalise_fund_structure(raw)
        stored = fund_structure_put(isin, block)
        return jsonify({"ticker": ticker, "fund_structure": stored})

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
