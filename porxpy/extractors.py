"""
Live data extractors for Yahoo Finance, plus the cached gateway that wraps
them and the high-level :func:`load_fund_data` orchestrator that the Flask
routes call.

Function naming convention:

* ``extract_*`` runs against a fresh ``yfinance.Ticker`` — these are the
  expensive HTTP-touching functions.
* :func:`get_category` is the generic cache-or-fetch wrapper.
* :func:`get_price_history_cached` is the bespoke price-history loader
  with incremental top-up logic.
* :func:`load_fund_data` is the orchestrator — composes everything,
  including the manual-upload holdings provider and the look-through
  rollup, into a single API-ready response. Full holdings are populated
  exclusively via user uploads (see :mod:`porxpy.upload`); when no upload
  exists, the look-through is synthesized from the Yahoo top-10 if it
  covers a high enough fraction of the fund.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd
import yfinance as yf

from porxpy.config import (
    ENRICHABLE_FIELDS,
    HISTORY_PERIOD_FALLBACKS,
    PRICE_HISTORY_FULL_REFRESH_DAYS,
)
from porxpy.resolver import (
    build_ticker,
    candidate_variants,
    clean_holding_ticker_input,
    isin_country_variant,
    search_id_variant,
    search_name_variant,
)
from porxpy.breakdowns import build_fund_breakdowns, rollup_holdings
from porxpy.utils import (
    age_days,
    alias_delete,
    alias_get,
    alias_put,
    cache_put,
    cache_read,
    cache_write,
    df_cell,
    load_settings,
    now_iso,
    safe,
    symbol_info_get,
    symbol_info_put,
)


# ---------------------------------------------------------------------------
# Price history — period fallback + live-quote synthesis
# ---------------------------------------------------------------------------
def _fetch_history_with_fallback(ticker: yf.Ticker
                                 ) -> tuple[pd.DataFrame, str]:
    """Walk through period fallbacks until ``ticker.history`` returns data.

    Some Yahoo listings reject ``period="max"`` even when they have a
    valid live quote. The fallback chain is defined in
    :data:`porxpy.config.HISTORY_PERIOD_FALLBACKS`.

    Args:
        ticker: yfinance Ticker.

    Returns:
        ``(df, period_used)``. ``df`` may be empty if every period failed;
        ``period_used`` is ``""`` in that case.
    """
    last_err = None
    for period in HISTORY_PERIOD_FALLBACKS:
        try:
            df = ticker.history(period=period, auto_adjust=True)
            if df is not None and not df.empty:
                if period != "max":
                    print(f"[Price] {ticker.ticker} period='max' unavailable; "
                          f"using period='{period}' ({len(df)} rows)")
                return df, period
        except Exception as exc:
            last_err = exc
            print(f"[Price] {ticker.ticker} period='{period}' failed: {exc}")
            continue
    if last_err is not None:
        print(f"[Price] {ticker.ticker} all period fallbacks exhausted; "
              f"last error: {last_err}")
    return pd.DataFrame(), ""


def _live_quote_row(ticker: yf.Ticker) -> dict | None:
    """Build a single price-history row from ``ticker.info``'s live quote.

    Used as a fallback / top-up when historical bars are missing or stale.
    The row is flagged ``synthetic: True`` so callers can strip it before
    appending real bars during incremental updates.

    Args:
        ticker: yfinance Ticker.

    Returns:
        A row dict matching the price-history schema, or ``None`` if no
        usable price could be found.
    """
    try:
        info = ticker.info or {}
    except Exception as exc:
        print(f"[Price] {ticker.ticker} ticker.info failed: {exc}")
        return None

    close_raw = info.get("regularMarketPrice")
    if close_raw is None:
        close_raw = info.get("previousClose")
    if close_raw is None:
        return None
    try:
        close = float(close_raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(close) or math.isinf(close) or close <= 0:
        return None

    ts = info.get("regularMarketTime")
    try:
        if ts:
            d = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        d = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _f(k: str) -> float:
        """Read a numeric field from ``info`` with NaN/inf coerced to 0.0."""
        v = info.get(k)
        try:
            f = float(v) if v is not None else 0.0
            return f if not (math.isnan(f) or math.isinf(f)) else 0.0
        except (TypeError, ValueError):
            return 0.0

    open_v = _f("regularMarketOpen") or close
    high_v = _f("regularMarketDayHigh") or close
    low_v  = _f("regularMarketDayLow")  or close
    vol_v  = info.get("regularMarketVolume") or info.get("volume") or 0
    try:
        vol = int(vol_v) if vol_v is not None else 0
    except (TypeError, ValueError):
        vol = 0

    return {
        "date":      d,
        "open":      round(open_v, 4),
        "high":      round(high_v, 4),
        "low":       round(low_v,  4),
        "close":     round(close,  4),
        "volume":    vol,
        "synthetic": True,
    }


def extract_price_history(ticker: yf.Ticker) -> list[dict]:
    """Fetch the full available price history for a ticker.

    Wraps :func:`_fetch_history_with_fallback` and, regardless of whether
    history was returned, attempts to top up / replace today's bar with
    a synthetic row from ``ticker.info`` so funds with broken history
    endpoints (e.g. ROB7.MU) still get *some* price for valuation.

    Args:
        ticker: yfinance Ticker.

    Returns:
        List of bars sorted oldest-first. Empty if neither history nor a
        live quote was obtainable.
    """
    hist, period_used = _fetch_history_with_fallback(ticker)

    out: list[dict] = []
    if not hist.empty:
        hist.index = hist.index.tz_localize(None)
        for dt, row in hist.iterrows():
            c = safe(row.get("Close"))
            if c is None:
                continue
            out.append({
                "date":   dt.strftime("%Y-%m-%d"),
                "open":   round(float(safe(row.get("Open"))   or 0), 4),
                "high":   round(float(safe(row.get("High"))   or 0), 4),
                "low":    round(float(safe(row.get("Low"))    or 0), 4),
                "close":  round(float(c), 4),
                "volume": int(safe(row.get("Volume")) or 0),
            })

    live_row = _live_quote_row(ticker)
    if live_row is not None:
        if not out:
            out.append(live_row)
            print(f"[Price] {ticker.ticker} synthesised single row from ticker.info "
                  f"({live_row['date']} close={live_row['close']}) — no history available")
        else:
            last_date = out[-1]["date"]
            if live_row["date"] > last_date:
                out.append(live_row)
                print(f"[Price] {ticker.ticker} appended synthetic row {live_row['date']} "
                      f"close={live_row['close']} (last hist row was {last_date})")
            elif live_row["date"] == last_date and period_used in ("5d", "1d"):
                # If the only history we got was a tiny window, prefer the
                # live quote for the same date — it tends to be fresher.
                out[-1] = live_row
                print(f"[Price] {ticker.ticker} replaced last row with live quote "
                      f"({live_row['date']} close={live_row['close']}, period_used='{period_used}')")
    elif not out:
        print(f"[Price] {ticker.ticker} no history AND no live quote available")

    return out


def _fetch_history_since(ticker: yf.Ticker, start_date: str) -> list[dict]:
    """Fetch only bars on or after ``start_date``.

    Uses yfinance's ``start=`` parameter (Yahoo's v8 chart endpoint).
    Small payload, no ``period=`` involvement.

    Args:
        ticker: yfinance Ticker.
        start_date: ``YYYY-MM-DD``. Treated as inclusive by yfinance, so
            callers should pass ``last_cached_date + 1 day``.

    Returns:
        List of bars, oldest-first. Empty on any failure or no data.
    """
    try:
        df = ticker.history(start=start_date, auto_adjust=True)
    except Exception as exc:
        print(f"[Price/incr] {ticker.ticker} fetch since {start_date} failed: {exc}")
        return []
    if df is None or df.empty:
        return []
    df.index = df.index.tz_localize(None)

    out: list[dict] = []
    for dt, row in df.iterrows():
        c = safe(row.get("Close"))
        if c is None:
            continue
        out.append({
            "date":   dt.strftime("%Y-%m-%d"),
            "open":   round(float(safe(row.get("Open"))   or 0), 4),
            "high":   round(float(safe(row.get("High"))   or 0), 4),
            "low":    round(float(safe(row.get("Low"))    or 0), 4),
            "close":  round(float(c), 4),
            "volume": int(safe(row.get("Volume")) or 0),
        })
    return out


def _strip_synthetic_tail(rows: list[dict]) -> list[dict]:
    """Drop trailing synthetic rows from a price-history list.

    Synthetic rows are added by :func:`_maybe_topup_live` (and
    :func:`extract_price_history` when no real bars are available). When
    we're about to append fresh bars during an incremental update, we
    remove the synthetic tail first so a fresh real row can replace it.
    """
    while rows and rows[-1].get("synthetic"):
        rows = rows[:-1]
    return rows


def _maybe_topup_live(ticker: yf.Ticker, rows: list[dict]
                      ) -> tuple[list[dict] | None, dict]:
    """Append a synthetic live-quote row when today's bar is missing.

    Args:
        ticker: yfinance Ticker.
        rows: Current price-history list.

    Returns:
        ``(new_rows, meta)`` if a row was appended, ``(None, meta)`` if
        nothing was added (already current or no live quote available).
    """
    rows_no_synth = _strip_synthetic_tail(rows)
    last_date = rows_no_synth[-1]["date"] if rows_no_synth else None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if last_date == today:
        return None, {"added": 0}

    live_row = _live_quote_row(ticker)
    if live_row is None:
        return None, {"added": 0}

    if last_date is not None and live_row["date"] <= last_date:
        return None, {"added": 0}

    print(f"[Price/topup] {ticker.ticker} added synthetic {live_row['date']} "
          f"close={live_row['close']} (last real bar was {last_date or '—'})")
    return rows_no_synth + [live_row], {"added": 1}


# ---------------------------------------------------------------------------
# Other Yahoo extractors
# ---------------------------------------------------------------------------
def extract_sectors(ticker: yf.Ticker) -> list[dict]:
    """Extract Yahoo's published sector weightings.

    Args:
        ticker: yfinance Ticker.

    Returns:
        ``[{"sector": <key>, "weight": <fraction>}, ...]`` sorted by
        weight descending. Empty on missing/invalid data. The sector
        key is normalised to its canonical form via the sectors-CSV
        resolver (v0.15.0). Unresolvable values are preserved
        verbatim so the resolution dialog can surface them — they
        get a ``_unmatched: True`` flag for downstream tracking.
    """
    try:
        from porxpy.resources import resolve_sector
        sw = ticker.funds_data.sector_weightings
        if isinstance(sw, dict) and sw:
            out = []
            for sector, weight in sw.items():
                w = safe(weight)
                if w is None:
                    continue
                try:
                    wf = float(w)
                except (TypeError, ValueError):
                    continue
                if wf > 0:
                    # Yahoo keys are slug-style ("financialservices",
                    # "realestate"); the matches column should turn
                    # them into the canonical spaced form.
                    raw_key = str(sector)
                    canon = resolve_sector(raw_key)
                    item: dict = {
                        "sector": canon if canon else raw_key,
                        "weight": round(wf, 6),
                    }
                    if not canon:
                        item["_unmatched"] = True
                    out.append(item)
            out.sort(key=lambda x: x["weight"], reverse=True)
            return out
    except Exception as exc:
        print(f"[Sectors] ERROR: {exc}")
    return []


# NOTE: Yahoo's funds_data.asset_classes position keys ("bondPosition"
# etc.) are now folded to the canonical ASSET_CLASSES vocabulary via
# resources.resolve_fund_asset_class, which reads the `matches` column
# of Fund_class_definitions.csv. The hand-maintained
# _ASSET_ALLOCATION_KEYMAP that used to live here has been retired in
# favour of that single authority. To remap a Yahoo key, add it to the
# `matches` column of the relevant row in that CSV.


def extract_asset_allocation(ticker: yf.Ticker) -> list[dict]:
    """Extract the issuer-published asset-allocation breakdown.

    This is the fund company's own aggregation of its holdings into
    asset-class buckets — equity vs fixed income vs cash vs other — as
    surfaced by Yahoo's ``funds_data.asset_classes``. It is the
    Fund/ETF-level *asset-class breakdown card*, and is entirely
    distinct from :func:`detect_asset_class` (which produces a single
    overall label classifying the fund as a whole).

    Many issuers publish nothing here, in which case this returns an
    empty list — that emptiness is the cue for the per-card
    breakdown-source override (populate from the holdings roll-up).

    Yahoo's six position keys are folded into the four-way
    ``ASSET_CLASSES`` vocabulary via :data:`_ASSET_ALLOCATION_KEYMAP`
    (preferred/convertible → ``other``). Buckets that collapse to the
    same canonical key are summed.

    Args:
        ticker: yfinance Ticker.

    Returns:
        ``[{"key": <asset_class>, "weight": <fraction>}, ...]`` sorted by
        weight descending. Empty on missing/invalid data.
    """
    try:
        ac = ticker.funds_data.asset_classes
    except Exception as exc:
        print(f"[AssetAllocation] ERROR: {exc}")
        return []
    if not isinstance(ac, dict) or not ac:
        return []

    buckets: dict[str, float] = {}
    for raw_key, weight in ac.items():
        w = safe(weight)
        if w is None:
            continue
        try:
            wf = float(w)
        except (TypeError, ValueError):
            continue
        if wf <= 0:
            continue
        # Yahoo's position keys ("bondPosition", "stockPosition", …)
        # are carried in the `matches` column of
        # Fund_class_definitions.csv, so resolve_fund_asset_class folds
        # them to the canonical vocabulary. Unmapped keys → "other".
        from porxpy.resources import resolve_fund_asset_class
        canon = resolve_fund_asset_class(str(raw_key)) or "other"
        buckets[canon] = buckets.get(canon, 0.0) + wf

    out = [{"key": k, "weight": round(v, 6)} for k, v in buckets.items()]
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out


def extract_holdings(ticker: yf.Ticker) -> list[dict]:
    """Extract Yahoo's top-10 holdings list.

    Args:
        ticker: yfinance Ticker.

    Returns:
        ``[{"symbol": ..., "name": ..., "weight": <fraction>}, ...]``
        sorted by weight descending. Empty on missing/invalid data.
    """
    try:
        df = ticker.funds_data.top_holdings
        if df is not None and not df.empty:
            out = []
            for symbol, row in df.iterrows():
                name   = safe(row.get("Name"))
                weight = safe(row.get("Holding Percent"))
                try:
                    wf = float(weight) if weight is not None else None
                except (TypeError, ValueError):
                    wf = None
                out.append({
                    "symbol": str(symbol),
                    "name":   str(name) if name else str(symbol),
                    "weight": round(wf, 6) if wf is not None else None,
                })
            out.sort(key=lambda x: x["weight"] if x["weight"] is not None else -1,
                     reverse=True)
            return out
    except Exception as exc:
        print(f"[Holdings] ERROR: {exc}")
    return []


def extract_fund_operations(ticker: yf.Ticker) -> dict:
    """Extract expense ratio, turnover, and net assets from ``fund_operations``.

    Args:
        ticker: yfinance Ticker.

    Returns:
        ``{"expenseRatioRaw": <fraction>, "turnoverRaw": <fraction>,
        "totalNetAssets": <number>}``. Any field can be ``None``.
    """
    result = {"expenseRatioRaw": None, "turnoverRaw": None, "totalNetAssets": None}
    try:
        ops = ticker.funds_data.fund_operations
        if ops is not None and not ops.empty:
            result["expenseRatioRaw"] = df_cell(ops, "expense ratio", 0)
            result["turnoverRaw"]     = df_cell(ops, "turnover",      0)
            result["totalNetAssets"]  = df_cell(ops, "net assets",    0)
    except Exception as exc:
        print(f"[Operations] ERROR: {exc}")
    return result


def extract_isin_from_ticker(ticker: yf.Ticker, info: dict | None = None
                             ) -> str | None:
    """Best-effort ISIN extraction from a yfinance Ticker.

    Tries ``info['isin']`` first (cheap — already in the info dict for
    some funds) and falls back to the ``.isin`` property (separate HTTP
    call to Yahoo's quote-lookup endpoint).

    Args:
        ticker: yfinance Ticker.
        info: Optional pre-fetched ``ticker.info`` dict to avoid a redundant
            HTTP call.

    Returns:
        12-character ISIN string, or ``None`` if no valid ISIN was found.
    """
    candidates: list[str | None] = []
    if isinstance(info, dict):
        candidates.append(info.get("isin"))
    try:
        candidates.append(getattr(ticker, "isin", None))
    except Exception as exc:
        print(f"[ISIN] property lookup failed: {exc}")
    for c in candidates:
        if not c:
            continue
        s = str(c).strip().upper()
        # yfinance returns "-" when unknown; ISO 6166 ISINs are 12 chars
        if s and s != "-" and len(s) == 12:
            return s
    return None


def _isin_from_info(resolved_ticker: str) -> str | None:
    """Best-effort ISIN retrieval from the symbol-info cache for a resolved ticker.

    Checks the already-cached ``yf.Ticker.info`` dict for the ``isin``
    field — no extra HTTP call. Returns a valid 12-char ISIN or ``None``.
    Called after a successful ``get_symbol_info_cached`` to backfill the
    ``isin`` field on a holdings row.
    """
    from porxpy.utils import symbol_info_get
    cached = symbol_info_get(resolved_ticker)
    if not isinstance(cached, dict):
        return None
    # Try the info dict stored by extract_symbol_info (via symbol_info_put).
    # yfinance exposes 'isin' directly on ticker.info for many securities.
    raw = (cached.get("isin") or "").strip().upper()
    if raw and raw != "-" and len(raw) == 12:
        return raw
    # Also try fetching live via yfinance as a fallback — only one call
    # per resolved ticker, not per row, since the alias cache means each
    # unique ticker is resolved once across all rows.
    try:
        tk = yf.Ticker(resolved_ticker)
        isin = getattr(tk, "isin", None)
        if isin:
            s = str(isin).strip().upper()
            if s and s != "-" and len(s) == 12:
                return s
    except Exception:
        pass
    return None


def _cusip_from_isin(isin: str) -> str | None:
    """Derive a CUSIP from a US ISIN.

    US ISINs have the form ``US`` + 9-char CUSIP + 1 check digit.
    Strips the country prefix and check digit to recover the CUSIP.
    Returns ``None`` for non-US ISINs or invalid input.

    Args:
        isin: 12-character ISIN string (already validated).

    Returns:
        9-character CUSIP string, or ``None``.
    """
    if not isin or len(isin) != 12:
        return None
    if not isin.upper().startswith("US"):
        return None
    return isin[2:11]   # chars 2–10 inclusive = 9-char CUSIP


def extract_profile(ticker: yf.Ticker) -> dict:
    """Build the fund profile dict from Yahoo metadata.

    Combines ``ticker.info``, ``funds_data.fund_overview``, and
    ``funds_data.fund_operations``. Handles the unit mismatch on the
    expense ratio (``fund_operations`` returns 0.002 for 0.20%, while
    ``ticker.info['netExpenseRatio']`` returns 0.2 for 0.20%) and prefers
    the fund-operations source when available.

    Args:
        ticker: yfinance Ticker.

    Returns:
        Profile dict with keys like ``longName, shortName, currency,
        exchange, fundFamily, navPrice, expenseRatioPct, turnoverPct,
        totalNetAssets, isin``.
    """
    info: dict = {}
    try:
        info = ticker.info or {}
    except Exception as exc:
        print(f"[Profile/info] {exc}")

    overview: dict = {}
    try:
        fo = ticker.funds_data.fund_overview
        if isinstance(fo, dict):
            overview = fo
    except Exception as exc:
        print(f"[Profile/overview] {exc}")

    ops = extract_fund_operations(ticker)

    expense_pct = None
    if ops["expenseRatioRaw"] is not None:
        try: expense_pct = round(float(ops["expenseRatioRaw"]) * 100, 4)
        except Exception: pass
    if expense_pct is None:
        v = info.get("netExpenseRatio")
        if v is not None:
            try: expense_pct = round(float(v), 4)
            except Exception: pass

    turnover_pct = None
    if ops["turnoverRaw"] is not None:
        try: turnover_pct = round(float(ops["turnoverRaw"]) * 100, 2)
        except Exception: pass

    total_assets = None
    if ops["totalNetAssets"] is not None:
        try: total_assets = float(ops["totalNetAssets"])
        except Exception: pass

    keys_from_info = [
        "longName", "shortName", "symbol", "currency", "exchange",
        "quoteType", "legalType", "market", "fundFamily",
        "navPrice", "previousClose", "regularMarketPrice",
        "regularMarketVolume",
        "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
        "averageVolume", "averageDailyVolume10Day",
        "fundInceptionDate",
    ]
    profile = {k: safe(info.get(k)) for k in keys_from_info if safe(info.get(k)) is not None}

    if overview.get("family"):       profile["fundFamily"] = overview["family"]
    if overview.get("legalType"):    profile["legalType"]  = overview["legalType"]
    if overview.get("categoryName"): profile["category"]   = overview["categoryName"]

    if expense_pct is not None:  profile["expenseRatioPct"] = expense_pct
    if turnover_pct is not None: profile["turnoverPct"]     = turnover_pct
    if total_assets is not None: profile["totalNetAssets"]  = total_assets

    isin = extract_isin_from_ticker(ticker, info)
    if isin:
        profile["isin"] = isin

    # v0.15.0: route profile.currency through the resolver so a Yahoo
    # value like "USD" or "gbp" lands canonical (and unrecognised
    # values are preserved + flagged on the fund-level normalisation
    # path). Other profile fields (longName, exchange, etc.) aren't
    # in the resource taxonomy and pass through verbatim.
    raw_cur = (profile.get("currency") or "").strip()
    if raw_cur:
        from porxpy.resources import resolve_currency
        resolved = resolve_currency(raw_cur)
        profile["currency"] = resolved if resolved else raw_cur
        if not resolved:
            profile["_currency_unmatched"] = True

    return profile


def detect_asset_class(ticker: yf.Ticker, profile: dict) -> dict:
    """Heuristic detector for the fund's overall asset class.

    Combines structural signals (presence of ``equity_holdings`` or
    ``bond_holdings`` data) with keyword matching on the fund's
    name/category. Confidence is reported as ``high`` / ``medium`` /
    ``low`` so the UI can decide how prominently to display it.

    Args:
        ticker: yfinance Ticker.
        profile: Output of :func:`extract_profile`.

    Returns:
        ``{"class": <one_of_ASSET_CLASSES>, "confidence": <str>,
        "signals": [str, ...]}``.
    """
    signals: list[str] = []
    has_equity_data = False
    has_bond_data   = False

    try:
        eh = ticker.funds_data.equity_holdings
        if eh is not None and hasattr(eh, "empty") and not eh.empty:
            nonna = eh.iloc[:, 0].dropna() if eh.shape[1] > 0 else pd.Series([])
            if len(nonna) > 0:
                has_equity_data = True
                signals.append(f"equity_holdings has {len(nonna)} metrics")
    except Exception:
        pass

    try:
        bh = ticker.funds_data.bond_holdings
        if bh is not None and hasattr(bh, "empty") and not bh.empty:
            nonna = bh.iloc[:, 0].dropna() if bh.shape[1] > 0 else pd.Series([])
            if len(nonna) > 0:
                has_bond_data = True
                signals.append(f"bond_holdings has {len(nonna)} metrics")
    except Exception:
        pass

    try:
        br = ticker.funds_data.bond_ratings
        if isinstance(br, dict):
            total = sum(v for v in br.values() if isinstance(v, (int, float)) and v)
            if total > 0.05:
                has_bond_data = True
                signals.append(f"bond_ratings total={total:.2f}")
    except Exception:
        pass

    hay = " ".join([
        str(profile.get("category")   or ""),
        str(profile.get("longName")   or ""),
        str(profile.get("shortName")  or ""),
        str(profile.get("legalType")  or ""),
    ]).lower()

    bond_kw      = ["bond", "treasury", "gilt", "aggregate", "credit", "fixed income", "govies"]
    equity_kw    = ["equity", "stock", "msci", "s&p", "russell", "nasdaq", "dividend", "growth", "value"]
    cash_kw      = ["money market", "t-bill", "cash", "ultrashort", "enhanced cash"]
    commodity_kw = ["gold", "silver", "commodity", "oil", "copper", "metals"]

    cat_equity    = any(k in hay for k in equity_kw)
    cat_bond      = any(k in hay for k in bond_kw)
    cat_cash      = any(k in hay for k in cash_kw)
    cat_commodity = any(k in hay for k in commodity_kw)

    if cat_bond:      signals.append("name/category mentions bonds")
    if cat_equity:    signals.append("name/category mentions equity")
    if cat_cash:      signals.append("name/category mentions money market")
    if cat_commodity: signals.append("name/category mentions commodity")

    if cat_cash and not has_equity_data:
        return {"class": "cash", "confidence": "high", "signals": signals}
    if cat_commodity:
        return {"class": "commodity", "confidence": "medium", "signals": signals}
    if has_equity_data and has_bond_data:
        return {"class": "mixed", "confidence": "high", "signals": signals}
    if has_equity_data or cat_equity:
        return {"class": "equity",
                "confidence": "high" if has_equity_data else "medium",
                "signals": signals}
    if has_bond_data or cat_bond:
        return {"class": "fixed_income",
                "confidence": "high" if has_bond_data else "medium",
                "signals": signals}
    return {"class": "other", "confidence": "low",
            "signals": signals or ["no usable signals"]}


# ---------------------------------------------------------------------------
# Per-symbol info — for top-10 holdings enrichment
# ---------------------------------------------------------------------------
# When a fund only publishes the Yahoo top-10 (no full holdings list)
# AND those 10 rows already cover ≥ a user-configured threshold of the
# fund, the user can opt to enrich each row with HQ country and trading
# currency from yfinance. This avoids hunting down an iShares URL when
# the top-10 already gives us enough information for a useful look-through.
#
# The mapping from yfinance.Ticker.info to our row schema:
#   info["country"]   → row["country"]      (HQ country)
#   info["currency"]  → row["currency"]     (trading currency, per Yahoo)
#   info["quoteType"] → row["asset_class"]  (Equity / Fixed Income / blank)
#
# Yahoo top-10 symbols are sometimes bare (e.g. "NVDA") and sometimes
# Yahoo-suffixed (e.g. "7203.T"). We pass them through to yf.Ticker as-is.
# Symbols Yahoo doesn't recognise yield empty strings for everything, and
# the row's facets simply stay blank — handled gracefully by rollup_holdings.
def _quotetype_to_asset_class(quote_type: str | None) -> str:
    """Map a Yahoo ``quoteType`` to one of our asset-class labels.

    Maps ``EQUITY`` to ``"Equity"`` and ``BOND`` to ``"Fixed Income"``;
    everything else returns ``""`` so the rollup doesn't bucket weird
    quote types (ETF, MUTUALFUND, CRYPTOCURRENCY, etc.) into a meaningless
    category. Conservative on purpose — we'd rather show "—" than guess
    wrong.

    Args:
        quote_type: Raw string from ``info["quoteType"]``.

    Returns:
        Canonical asset-class key matching the ``ASSET_CLASSES``
        vocabulary in config.py: ``"equity"``, ``"fixed_income"``, or
        ``""``. Lowercased so the rollup canonicaliser doesn't need a
        second pass to fix display-case spellings.
    """
    qt = (quote_type or "").strip().upper()
    if qt == "EQUITY":
        return "equity"
    if qt == "BOND":
        return "fixed_income"
    return ""


def _seed_fund_structure(profile: dict) -> dict:
    """Derive Yahoo-seeded defaults for the fund "Structure" block.

    Yahoo's ``quoteType`` (and, as a fallback, ``legalType``) tells us
    whether a fund is an ETF or a plain open-ended fund — but Yahoo
    publishes nothing about replication method, and its active/passive
    signal is only a weak default. So this produces *defaults only*:
    the values the "Edit fund" dialog pre-fills. The user's stored
    override (if any) is layered on top in :func:`load_fund_data`.

    Seeding rules:
      * quoteType ETF                  → structure "etf",  style "passive"
      * quoteType MUTUALFUND / FUND    → structure "fund", style "active"
      * anything else / missing        → structure "unknown", style "unknown"
      * replication is always seeded "unknown" for an ETF and "n/a" for
        a plain fund — Yahoo can't tell us the real method.

    The active/passive seed is deliberately weak: active ETFs and index
    mutual funds both exist, so "passive for ETF / active for fund" is
    only a starting guess the user is expected to confirm or correct.

    Args:
        profile: The fund profile dict from :func:`extract_profile`.

    Returns:
        A normalised ``{structure, replication, style}`` dict suitable
        as the default before any user override is applied.
    """
    qt = (profile.get("quoteType") or "").strip().upper()
    lt = (profile.get("legalType") or "").strip().upper()

    if qt == "ETF" or "ETF" in lt or "EXCHANGE TRADED" in lt:
        structure, style, replication = "etf", "passive", "unknown"
    elif qt in ("MUTUALFUND", "FUND") or "FUND" in lt:
        structure, style, replication = "fund", "active", "n/a"
    else:
        structure, style, replication = "unknown", "unknown", "unknown"

    # normalise_fund_structure re-enforces the coupling rule.
    from porxpy.utils import normalise_fund_structure
    return normalise_fund_structure({
        "structure":   structure,
        "replication": replication,
        "style":       style,
    })


# justETF states replication as one of a few phrasings; map each to our
# REPLICATION_METHODS vocabulary. Order matters — "full replication" must
# be tested before a bare "replication", "optimised sampling" before
# "sampling", etc. Each entry is (substring_to_find, canonical_value).
_JUSTETF_REPLICATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("full replication",            "full"),
    ("full physical replication",   "full"),
    ("optimised sampling",          "sampled"),
    ("optimized sampling",          "sampled"),
    ("sampling technique",          "sampled"),
    ("physical replication with sampling", "sampled"),
    ("sampling",                    "sampled"),
    ("swap-based",                  "synthetic"),
    ("swap based",                  "synthetic"),
    ("synthetic replication",       "synthetic"),
    ("unfunded swap",               "synthetic"),
    ("funded swap",                 "synthetic"),
)


def lookup_fund_structure(isin: str) -> dict:
    """Best-effort lookup of replication method + style from justETF.

    Fetches the justETF profile page for ``isin`` and scrapes the
    replication method and management style (active/passive) from its
    text. This is a *suggestion* helper for the "Edit fund" dialog — it
    never persists anything; the user confirms the result.

    Honest about its limits:
      * Only ISINs justETF lists are covered (broadly European funds);
        a US-listed ETF with no European ISIN returns "not found".
      * It scrapes HTML, so a justETF layout change can break parsing —
        in which case the affected facet returns ``None`` rather than a
        wrong guess.
      * justETF may rate-limit or block automated requests; a failed
        fetch returns an ``ok: False`` result, not an exception.

    Args:
        isin: The fund's ISIN.

    Returns:
        ::

            {
              "ok":          bool,     # did the page fetch + parse?
              "source":      str,      # human-readable source label
              "url":         str,      # the page consulted
              "replication": {"value": <method|None>, "confidence": str},
              "style":       {"value": <"active"|"passive"|None>,
                              "confidence": str},
              "note":        str,      # populated when ok is False
            }

        ``value`` is ``None`` for a facet that could not be determined.
        ``confidence`` is ``"high"`` (an explicit phrasing matched),
        ``"low"`` (a weak/indirect signal), or ``"none"``.
    """
    import re
    import urllib.request
    import urllib.error
    from porxpy.config import (JUSTETF_PROFILE_URL, JUSTETF_LOOKUP_TIMEOUT_S,
                               JUSTETF_LOOKUP_UA)

    isin = (isin or "").strip().upper()
    result: dict = {
        "ok":          False,
        "source":      "justETF",
        "url":         "",
        "replication": {"value": None, "confidence": "none"},
        "style":       {"value": None, "confidence": "none"},
        "note":        "",
    }
    if not isin:
        result["note"] = "This fund has no ISIN, so justETF cannot be queried."
        return result

    url = JUSTETF_PROFILE_URL.format(isin=isin)
    result["url"] = url
    req = urllib.request.Request(url, headers={
        "User-Agent": JUSTETF_LOOKUP_UA,
        "Accept":     "text/html",
    })
    try:
        with urllib.request.urlopen(req, timeout=JUSTETF_LOOKUP_TIMEOUT_S) as resp:
            raw = resp.read(2_000_000)   # cap — profile pages are small
        html = raw.decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        result["note"] = (f"justETF returned HTTP {exc.code} for {isin} — "
                          f"the fund may not be listed there.")
        return result
    except Exception as exc:
        result["note"] = f"Could not reach justETF: {exc}"
        return result

    # justETF profile pages are content-light; strip tags to plain text
    # and lowercase for substring matching. We deliberately do NOT try
    # to parse specific DOM nodes — that is far more brittle than a text
    # scan, and the phrasings we match are distinctive enough.
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).lower()

    # justETF profile pages echo the fund name; if the ISIN is nowhere
    # in the page we very likely got a "not found" / redirect shell.
    if isin.lower() not in text:
        result["note"] = (f"justETF has no profile for {isin} "
                          f"(not listed, or the page format changed).")
        return result

    result["ok"] = True

    # ---- Replication method --------------------------------------------
    for needle, value in _JUSTETF_REPLICATION_PATTERNS:
        if needle in text:
            result["replication"] = {"value": value, "confidence": "high"}
            break

    # ---- Management style ----------------------------------------------
    # justETF profiles index funds (ETFs) — its profile text describes
    # the index being tracked. Explicit "actively managed" wording is
    # the strong active signal; otherwise the presence of index-tracking
    # language ("tracks the ... index", "seeks to track") is a passive
    # signal. We grade these honestly: explicit wording → high, the
    # index-tracking inference → low.
    if "actively managed" in text or "active management" in text:
        result["style"] = {"value": "active", "confidence": "high"}
    elif "passively managed" in text or "passive management" in text:
        result["style"] = {"value": "passive", "confidence": "high"}
    elif ("seeks to track" in text or "tracks the" in text
          or "replicates the performance" in text):
        # An index-tracking ETF — passive by construction, but inferred
        # rather than stated in those words.
        result["style"] = {"value": "passive", "confidence": "low"}

    if (result["replication"]["value"] is None
            and result["style"]["value"] is None):
        result["note"] = ("Reached the justETF page but could not parse "
                           "replication or style from it — set them manually.")
        result["ok"] = False

    return result


def extract_symbol_info(symbol: str) -> dict:
    """Pull HQ country / trading currency / asset-class / sector for one holding.

    Hits ``yfinance.Ticker(symbol).info`` once. All fields default to ``""``
    on lookup failure or missing keys — never raises.

    Returned values are CANONICAL forms ready for the rollup chokepoint
    (see :func:`porxpy.utils.canonicalise_facet_key`):

    * ``country``     — lowercased mstar form (e.g. ``"unitedstates"``);
                        unmappable Yahoo strings fall through lowercased.
    * ``currency``    — uppercased ISO code.
    * ``asset_class`` — canonical key from ``config.ASSET_CLASSES`` via
                        :func:`_quotetype_to_asset_class`.
    * ``sector``      — Yahoo's GICS-flavoured sector string verbatim,
                        used as-is for the per-position Sector column.
                        Equity holdings get a non-blank value; ETFs,
                        bonds, cash and crypto generally return ``""``.
    * ``sub_class``   — derived from ``asset_class`` (and absent if the
                        asset class itself couldn't be determined). The
                        per-symbol cache doesn't store this — it's
                        produced on the fly so the enrichment path can
                        treat it as just another field on the lookup
                        result.

    Emitting canonical forms here means enrichment-produced rows match
    CSV-upload-produced rows at the source, so the look-through merge
    in app.py doesn't end up with two buckets that display identically
    but key differently.

    Args:
        symbol: Yahoo ticker (e.g. ``"AAPL"``, ``"7203.T"``). Used as-is.

    Returns:
        ``{"country", "currency", "asset_class", "sub_class", "sector",
        "name", "quote_type"}``. Empty strings indicate "Yahoo had
        nothing useful for this symbol".
    """
    # Local imports — resources / utils depend only on config, no
    # circular risk. default_sub_class lets us emit sub_class as a
    # first-class enrichment field without the caller having to know
    # the asset-class → sub-class derivation rule.
    from porxpy.resources import country_to_mstar
    from porxpy.utils import default_sub_class

    out = {"country": "", "currency": "", "asset_class": "",
           "sub_class": "", "sector": "",
           "name": "", "quote_type": ""}
    if not symbol:
        return out
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        print(f"[SymbolInfo] {symbol} lookup failed: {exc}")
        return out

    raw_country = (info.get("country")   or "").strip()
    currency    = (info.get("currency")  or "").strip().upper()
    qt          = (info.get("quoteType") or "").strip().upper()
    name        = (info.get("longName") or info.get("shortName") or "").strip()
    sector      = (info.get("sector") or "").strip()

    # Canonicalise Yahoo's country (typically "United States") to the
    # mstar form ("unitedstates") so it merges with CSV-upload rows.
    if raw_country:
        mstar = country_to_mstar(raw_country)
        country = mstar if mstar else raw_country.lower()
    else:
        country = ""

    ac = _quotetype_to_asset_class(qt)
    # Sub class is derived from the (holding-flavoured) asset class via
    # the same mapping the upload pipeline uses. For ETF / unknown quote
    # types ac is blank, and sub_class follows — the enrichment loop
    # downstream only fills non-blank values, so blanks here are safe.
    from porxpy.utils import default_holding_asset_class
    sub = default_sub_class(default_holding_asset_class(ac))

    out.update({
        "country":     country,
        "currency":    currency,
        "asset_class": ac,
        "sub_class":   sub,
        "sector":      sector,
        "name":        name,
        "quote_type":  qt,
    })
    return out


def _info_looks_found(info: dict) -> bool:
    """Heuristic: does this symbol-info dict look like Yahoo had real data?

    Used to decide whether to count a symbol as "recognised by Yahoo" in
    upload-commit warnings. We check the fields most reliably populated
    when Yahoo knows a symbol — currency and quote_type — rather than
    sector or country which are sometimes blank even on real listings.
    """
    if not isinstance(info, dict):
        return False
    return bool(info.get("currency")) or bool(info.get("quote_type"))


# Maximum number of candidate forms to probe for a single raw input.
# Bounded so a 500-row holdings file with all-unknown tickers doesn't
# spend an unbounded number of Yahoo calls per row. The alias cache
# means repeat uploads cost zero probes after the first.
_VARIANT_PROBE_CAP = 3


def get_symbol_info_cached(symbol: str, *, force: bool = False,
                           isin: str | None = None,
                           cusip: str | None = None,
                           name: str | None = None,
                           retry_negative: bool = False) -> dict:
    """Resolve a possibly-non-Yahoo ticker via the variant-probe chokepoint.

    Tries the input as-is on Yahoo first; if Yahoo doesn't recognise it,
    iterates through candidate forms produced by
    :func:`porxpy.resolver.candidate_variants` (Bloomberg-spaced rewrite,
    concat-suffix strip, Refinitiv suffix strip, etc.) and returns the
    first one Yahoo accepts.

    When all variant candidates are exhausted two additional fallbacks are
    attempted in order:

    1. **ISIN country prefix** (:func:`porxpy.resolver.isin_country_variant`).
       If ``isin`` is provided, the ISO 3166-1 alpha-2 prefix (first two
       chars) is mapped to a Yahoo exchange suffix and the bare ticker is
       tried with that suffix.  E.g. ticker ``"AIR"``, ISIN ``"FR…"``
       → probe ``"AIR.PA"``.

    2. **Name search** (:func:`porxpy.resolver.search_name_variant`).
       If ``name`` is provided (and the ISIN fallback failed or was not
       available), ``yfinance.Search`` is called with the security name.
       The returned tickers are matched against the first four characters
       of the raw ticker; the first match is probed on Yahoo.

    Three caches collaborate to keep this cheap on repeat use:

    * **Symbol-info cache** (per resolved Yahoo ticker, 90-day TTL) —
      every fund holding ``AAPL`` benefits from one lookup.
    * **Alias cache** (per raw input, no TTL) — once we've decided
      ``"PLTRUS" → "PLTR"``, subsequent probes of ``"PLTRUS"`` skip
      straight to the ``PLTR`` info entry without re-trying variants.
    * **Negative cache** (per raw input, recorded as
      ``alias_put(raw, None)``) — inputs Yahoo can't recognise in any
      variant don't get re-probed on every upload.

    Args:
        symbol: Raw issuer-supplied ticker. Cleaned with
            :func:`clean_holding_ticker_input` before probing.
        force: Bypass both caches and re-probe live. Useful in tests
            and when the user wants to refresh.
        isin: Optional ISIN for the holding. Used as fallback when all
            variant candidates fail — the country prefix drives exchange
            suffix selection. Also passed to ``yf.Search`` as an exact
            identifier when no ticker is present on the row.
        cusip: Optional CUSIP (9-char US identifier). When no ticker is
            present, passed directly to ``yf.Search`` to resolve a Yahoo
            ticker. Tried before ISIN search (CUSIPs are US-specific and
            tend to resolve more precisely for US securities).
        name: Optional security name from the holdings file. Used as a
            last-resort fallback via Yahoo name search when all other
            probes have failed.

    Returns:
        The same dict shape as :func:`extract_symbol_info` plus
        ``"_found"`` (bool). ``{"_found": False}`` if no candidate
        resolved on Yahoo or the cleaned input was empty.
    """
    cleaned = clean_holding_ticker_input(symbol)

    # No ticker supplied — skip variant probing entirely and go straight to
    # identifier-based search. CUSIP is tried first (US-specific, very
    # precise), then ISIN (also an exact identifier). If neither resolves,
    # fall through to the name-search fallback at the bottom.
    if not cleaned:
        for _id_label, _id_val in (("CUSIP", cusip), ("ISIN", isin)):
            if not _id_val:
                continue
            id_ticker = search_id_variant(_id_val)
            if id_ticker:
                info = extract_symbol_info(id_ticker)
                info["_found"] = _info_looks_found(info)
                symbol_info_put(id_ticker, info)
                if info["_found"]:
                    info["_resolved_ticker"] = id_ticker
                    print(f"[SymbolInfo] no ticker — resolved via {_id_label}"
                          f" ({_id_val}) → {id_ticker}")
                    return info
        # Name search as last resort when no ticker and no id resolved
        if name:
            name_cand = search_name_variant(name, "")
            if name_cand:
                info = extract_symbol_info(name_cand)
                info["_found"] = _info_looks_found(info)
                symbol_info_put(name_cand, info)
                if info["_found"]:
                    info["_resolved_ticker"] = name_cand
                    print(f"[SymbolInfo] no ticker — resolved via name"
                          f" search ('{name}') → {name_cand}")
                    return info
        return {"_found": False}

    # Alias cache short-circuit. ``present`` means we've probed before;
    # ``resolved`` is the form that worked, or ``None`` for "tried,
    # nothing worked". Skipped under force=True.
    # ``retry_negative`` clears a stale None entry so a re-upload that
    # now supplies an ISIN/CUSIP gets a fresh probe instead of hitting
    # the cached failure immediately.
    if not force:
        present, resolved = alias_get(cleaned)
        if present:
            if resolved is None:
                if retry_negative:
                    alias_delete(cleaned)   # clear stale negative; fall through
                else:
                    return {"_found": False}
            cached = symbol_info_get(resolved)
            if cached is not None:
                # Backfill _found for entries written before the flag existed
                cached.setdefault("_found", _info_looks_found(cached))
                cached["_resolved_ticker"] = resolved
                return cached
            # Alias points to a resolved ticker but the info entry has
            # expired (90-day TTL). Fall through and re-probe — it's
            # cheap (1 Yahoo call, alias still holds).

    # Probe candidates in order. Skip cached negative results inline so
    # repeat false-positive candidates (e.g. AAPL → AA.LS) don't re-hit
    # Yahoo each time.
    candidates = candidate_variants(cleaned)[:_VARIANT_PROBE_CAP]
    for cand in candidates:
        # Per-candidate cache check (independent of the input alias).
        cached = None if force else symbol_info_get(cand)
        if cached is not None:
            cached.setdefault("_found", _info_looks_found(cached))
            if cached["_found"]:
                # Remember the alias so we can short-circuit next time
                if cand != cleaned or candidates.index(cand) != 0:
                    alias_put(cleaned, cand)
                cached["_resolved_ticker"] = cand
                return cached
            # Negatively-cached candidate — keep going, don't re-probe Yahoo
            continue

        # Not in cache — live probe via yfinance
        info = extract_symbol_info(cand)
        info["_found"] = _info_looks_found(info)
        symbol_info_put(cand, info)

        if info["_found"]:
            # Record the alias so subsequent probes of the same raw
            # input skip the variant generation and go straight to this
            # candidate's entry.
            if cand != cleaned:
                alias_put(cleaned, cand)
            info["_resolved_ticker"] = cand
            return info
        # else: candidate didn't resolve — try the next one. Each
        # candidate's negative result is cached above, so even if this
        # symbol comes up across multiple uploads we won't hit Yahoo
        # again for the same dud variant.

    # All candidates exhausted. Before recording a negative alias, try the
    # two extended fallbacks (ISIN country prefix, then name search).

    # Fallback 1 — ISIN country prefix.
    # Strip any existing suffix from cleaned, apply the ISIN-derived suffix,
    # and probe the result once. On success, record the alias and return.
    if isin:
        isin_cand = isin_country_variant(cleaned, isin)
        if isin_cand:
            cached = None if force else symbol_info_get(isin_cand)
            if cached is not None:
                cached.setdefault("_found", _info_looks_found(cached))
                if cached["_found"]:
                    alias_put(cleaned, isin_cand)
                    cached["_resolved_ticker"] = isin_cand
                    return cached
            else:
                info = extract_symbol_info(isin_cand)
                info["_found"] = _info_looks_found(info)
                symbol_info_put(isin_cand, info)
                if info["_found"]:
                    alias_put(cleaned, isin_cand)
                    info["_resolved_ticker"] = isin_cand
                    print(f"[SymbolInfo] {cleaned} resolved via ISIN prefix"
                          f" ({isin[:2]}) → {isin_cand}")
                    return info

    # Fallback 2 — identifier search (CUSIP / ISIN).
    # When a ticker existed but didn't resolve, try the exact identifiers
    # before falling back to the fuzzier name search. CUSIP first (US),
    # then ISIN. On success, alias the resolved ticker to cleaned so the
    # next probe of this raw ticker is fast.
    for _id_label, _id_val in (("CUSIP", cusip), ("ISIN", isin)):
        if not _id_val:
            continue
        id_ticker = search_id_variant(_id_val)
        if not id_ticker:
            continue
        cached = None if force else symbol_info_get(id_ticker)
        if cached is not None:
            cached.setdefault("_found", _info_looks_found(cached))
            if cached["_found"]:
                alias_put(cleaned, id_ticker)
                cached["_resolved_ticker"] = id_ticker
                return cached
        else:
            info = extract_symbol_info(id_ticker)
            info["_found"] = _info_looks_found(info)
            symbol_info_put(id_ticker, info)
            if info["_found"]:
                alias_put(cleaned, id_ticker)
                info["_resolved_ticker"] = id_ticker
                print(f"[SymbolInfo] {cleaned} resolved via {_id_label}"
                      f" ({_id_val}) → {id_ticker}")
                return info

    # Fallback 3 — name search.
    # Only attempted when a name was supplied. On success, probe the returned
    # ticker (it may already be cached from a prior lookup of that symbol).
    if name:
        name_cand = search_name_variant(name, cleaned)
        if name_cand:
            cached = None if force else symbol_info_get(name_cand)
            if cached is not None:
                cached.setdefault("_found", _info_looks_found(cached))
                if cached["_found"]:
                    alias_put(cleaned, name_cand)
                    cached["_resolved_ticker"] = name_cand
                    return cached
            else:
                info = extract_symbol_info(name_cand)
                info["_found"] = _info_looks_found(info)
                symbol_info_put(name_cand, info)
                if info["_found"]:
                    alias_put(cleaned, name_cand)
                    info["_resolved_ticker"] = name_cand
                    print(f"[SymbolInfo] {cleaned} resolved via name search"
                          f" ('{name}') → {name_cand}")
                    return info

    # All fallbacks failed. Record a negative alias so the next probe of
    # this raw input doesn't re-run the full loop.
    alias_put(cleaned, None)
    return {"_found": False}


def _apply_lookup_to_row(row: dict, info: dict, fields: list[str], *,
                          blank_only: bool) -> list[str]:
    """Splice Yahoo per-symbol lookup data into a holdings row.

    The single chokepoint for "given a row and a yfinance lookup result,
    write the selected enrichment fields onto the row". Used both by
    :func:`enrich_top10_holdings` (where the row starts empty so the
    blank-only check is trivially satisfied) and by the manual
    "Enrich through Yahoo" endpoint (where the row may already carry
    user data we must not overwrite).

    Field-specific normalisation matches the upload pipeline:
        * ``country`` is canonicalised via :func:`country_to_mstar`
        * ``currency`` is uppercased
        * everything else is a verbatim string assignment

    Args:
        row: The holdings row to mutate in place.
        info: The dict returned by :func:`get_symbol_info_cached`.
        fields: The subset of :data:`~porxpy.config.ENRICHABLE_FIELDS`
            the user has opted into. Fields outside that set are
            silently ignored — defence in depth.
        blank_only: When True, only fill fields whose current value on
            ``row`` is blank/missing. When False, every selected field
            overwrites the existing value (used by the top-10 path
            where the row was just built and has nothing to preserve).

    Returns:
        The list of field names actually written, in order. Useful for
        per-field "rows_filled" counters surfaced in the response.
    """
    from porxpy.resources import country_to_mstar

    if not isinstance(info, dict):
        return []

    written: list[str] = []
    for f in fields:
        if f not in ENRICHABLE_FIELDS:
            continue
        v = info.get(f)
        if v is None:
            continue
        v_s = str(v).strip()
        if not v_s:
            continue

        if blank_only:
            cur = row.get(f)
            is_empty = cur is None or (
                isinstance(cur, str) and not cur.strip())
            if not is_empty:
                continue

        if f == "country":
            mstar = country_to_mstar(v_s) or v_s
            row[f] = mstar
        elif f == "currency":
            row[f] = v_s.upper()
        else:
            row[f] = v_s
        written.append(f)
    return written


def enrich_top10_holdings(top_rows: list[dict],
                           fields: list[str] | None = None) -> list[dict]:
    """Convert a Yahoo top-10 list into unified-schema holdings rows.

    Output rows are full :data:`~porxpy.utils.HOLDINGS_ROW_FIELDS`
    superset rows (the same shape a manual upload produces), each with
    a stable ``_row_id``. The selected per-symbol facets (``country``,
    ``currency``, ``asset_class``, ``sub_class``, ``sector``,
    ``name``) come from the per-symbol Yahoo info lookup.

    Notes:
        * ``weight_pct`` is in percent (5.34 means 5.34%). Yahoo top-10
          weights are fractions (0.0534), so we multiply by 100 here.
        * The bond-specific columns (``duration`` / ``maturity`` /
          ``coupon`` / ``effective_date``) stay blank — Yahoo's top-10
          payload doesn't carry any bond metadata.
        * If ``fields`` is empty (the user unticked everything in
          settings), only the trivially-derivable fields (name, ticker,
          weight) are populated and the result is equivalent to a
          straight top-10 row build.

    Args:
        top_rows: Output of :func:`extract_holdings` — a list of
            ``{symbol, name, weight}`` dicts.
        fields: Subset of :data:`~porxpy.config.ENRICHABLE_FIELDS` to
            populate per row. ``None`` means "all of them" — the
            default that preserves the pre-checklist behaviour. An
            empty list means "none of them".

    Returns:
        A list of unified-schema rows ready for both
        :func:`~porxpy.utils.rollup_holdings` and the holdings cache.
    """
    from porxpy.utils import coerce_holdings_row

    eff_fields = list(ENRICHABLE_FIELDS) if fields is None else list(fields)

    out: list[dict] = []
    for r in top_rows or []:
        sym    = (r.get("symbol") or "").strip()
        weight = r.get("weight")
        try:
            weight_pct = float(weight) * 100.0 if weight is not None else None
        except (TypeError, ValueError):
            weight_pct = None
        if weight_pct is None or weight_pct <= 0:
            # No usable weight — skip; rollup ignores zero-weight rows anyway
            continue

        # Light cleanup only; the actual Yahoo-form selection happens
        # inside get_symbol_info_cached, which probes candidate variants
        # and lets Yahoo's response decide. We then use the alias cache
        # to recover the resolved form for the output row.
        cleaned = clean_holding_ticker_input(sym) if sym else ""
        info: dict = {}
        if cleaned and eff_fields:
            # Skip the lookup entirely when the user has unticked every
            # field — saves a Yahoo call per top-10 row when enrichment
            # is fully disabled, and the row falls through to its
            # minimally-populated state.
            info = get_symbol_info_cached(cleaned) or {}

        # Determine the ticker to put on the output row. If Yahoo
        # resolved a variant form, the alias cache holds it; fall back
        # to the cleaned input otherwise.
        resolved_ticker = cleaned
        if cleaned and info.get("_found"):
            present, alias = alias_get(cleaned)
            if present and alias:
                resolved_ticker = alias

        # Name handling depends on whether name was opted into the
        # enrichment list. If yes, prefer Yahoo's longName (typically
        # more informative); if no, fall back to whatever the top-10
        # payload itself gave us. Either way the row ships with SOME
        # name — the lookup data is only used as a tie-breaker.
        if "name" in eff_fields and info.get("_found"):
            display_name = (info.get("name") or r.get("name")
                            or resolved_ticker).strip()
        else:
            display_name = (r.get("name") or resolved_ticker
                            or sym or "").strip()

        # Build the base row (the always-derived fields).
        row = coerce_holdings_row({
            "name":         display_name,
            "ticker":       resolved_ticker,
            "weight_pct":   round(weight_pct, 6),
        })
        # Apply the selected enrichment. Blank-only is irrelevant here
        # (the row was just minted), but passing False makes the data
        # path explicit: top-10 builds fully accept the lookup result.
        if info.get("_found"):
            _apply_lookup_to_row(row, info, eff_fields, blank_only=False)
            # Re-run coerce so a freshly-set asset_class also defaults
            # the sub_class if the user didn't enrich sub_class.
            row = coerce_holdings_row(row, row_id=row["_row_id"])

        out.append(row)
    return out


def enrich_existing_holdings(rows: list[dict], fields: list[str]
                              ) -> tuple[list[dict], dict]:
    """Fill blank fields on already-built holdings rows from Yahoo lookups.

    Backs the manual "Enrich through Yahoo" button on the fund-page
    holdings tile. Iterates through the supplied rows and, for each one
    that carries a ticker, fetches the per-symbol Yahoo lookup and
    writes the selected ``fields`` ONLY where the current value is
    blank. A user upload's data is therefore never overwritten — the
    button is purely additive.

    Bond-specific columns (``duration`` / ``maturity`` / ``coupon`` /
    ``effective_date``) are not part of the enrichable set and stay
    untouched regardless.

    Args:
        rows: Holdings rows to enrich. Mutated in place (and also
            returned for caller convenience).
        fields: Subset of :data:`~porxpy.config.ENRICHABLE_FIELDS` to
            try filling. An empty list means "do nothing" — every row
            is left as-is and the counts come back zero.

    Returns:
        ``(rows, stats)``. ``stats`` is::

            {
              "fields":              [<requested fields>],
              "rows_filled":         {<field>: count, ...},
              "rows_processed":      <int>,
              "rows_skipped_no_ticker": <int>,
              "rows_yahoo_not_found":  <int>,
              "rows_with_changes":     <int>,
            }
    """
    from porxpy.utils import coerce_holdings_row

    eff_fields = [f for f in (fields or []) if f in ENRICHABLE_FIELDS]
    stats: dict[str, Any] = {
        "fields":                 eff_fields,
        "rows_filled":            {f: 0 for f in eff_fields},
        "rows_processed":         0,
        "rows_skipped_no_ticker": 0,
        "rows_yahoo_not_found":   0,
        "rows_with_changes":      0,
    }
    if not eff_fields or not rows:
        return rows, stats

    for row in rows:
        stats["rows_processed"] += 1
        sym      = (row.get("ticker") or "").strip()
        row_isin = row.get("isin")  or None
        row_cusip= row.get("cusip") or None
        if not sym and not row_isin and not row_cusip:
            stats["rows_skipped_no_ticker"] += 1
            continue
        try:
            info = get_symbol_info_cached(
                sym,
                isin=row_isin,
                cusip=row_cusip,
                name=row.get("name") or None,
            )
        except Exception as exc:
            print(f"[enrich_existing] {sym} error: {exc}")
            continue
        if not isinstance(info, dict) or not info.get("_found"):
            stats["rows_yahoo_not_found"] += 1
            continue

        # Backfill ticker / ISIN / CUSIP from the resolved info.
        resolved_ticker = (info.get("_resolved_ticker") or "").strip() or None
        if resolved_ticker and resolved_ticker != (row.get("ticker") or ""):
            row["ticker"] = resolved_ticker
        if resolved_ticker and not (row.get("isin") or "").strip():
            fetched_isin = _isin_from_info(resolved_ticker)
            if fetched_isin:
                row["isin"] = fetched_isin
        if not (row.get("cusip") or "").strip():
            isin_for_cusip = (row.get("isin") or "").strip().upper()
            if isin_for_cusip:
                derived = _cusip_from_isin(isin_for_cusip)
                if derived:
                    row["cusip"] = derived

        written = _apply_lookup_to_row(row, info, eff_fields, blank_only=True)
        if written:
            stats["rows_with_changes"] += 1
            for f in written:
                stats["rows_filled"][f] = stats["rows_filled"].get(f, 0) + 1

    # Re-coerce every row so a freshly-set asset_class default-fills
    # sub_class consistently, and the numeric / date types stay clean
    # for the rows we touched. Preserves _row_id (passed explicitly).
    coerced: list[dict] = []
    for r in rows:
        rid = r.get("_row_id")
        coerced.append(coerce_holdings_row(r, row_id=rid))
    # Mutate the input list in place so the caller's reference still
    # sees the updated rows.
    rows[:] = coerced
    return rows, stats


def raw_top10_to_rows(top_rows: list[dict]) -> list[dict]:
    """Convert a raw Yahoo top-10 list into unified-schema holdings rows.

    The no-enrichment counterpart to :func:`enrich_top10_holdings`: same
    output shape (full :data:`~porxpy.utils.HOLDINGS_ROW_FIELDS` rows
    with ``_row_id``\\ s), but no per-symbol Yahoo lookups — only the
    three fields Yahoo's holdings endpoint gives us directly (name,
    symbol, weight) are populated. Everything else stays blank.

    Kept as a separate entry point (callable as
    ``enrich_top10_holdings(rows, fields=[])`` would do the same job)
    for the rare cases where the orchestrator wants the cheap path
    explicitly without bothering with the symbol-info cache.

    Args:
        top_rows: Output of :func:`extract_holdings` — a list of
            ``{symbol, name, weight}`` dicts.

    Returns:
        A list of unified-schema rows (sparse — name / ticker /
        weight_pct only).
    """
    from porxpy.utils import coerce_holdings_row

    out: list[dict] = []
    for r in top_rows or []:
        sym    = (r.get("symbol") or "").strip()
        weight = r.get("weight")
        try:
            weight_pct = float(weight) * 100.0 if weight is not None else None
        except (TypeError, ValueError):
            weight_pct = None
        if weight_pct is None or weight_pct <= 0:
            continue
        cleaned = clean_holding_ticker_input(sym) if sym else ""
        out.append(coerce_holdings_row({
            "name":         (r.get("name") or cleaned or sym or "").strip(),
            "ticker":       cleaned,
            "weight_pct":   round(weight_pct, 6),
        }))
    return out


def top10_weight_sum_pct(top_rows: list[dict]) -> float:
    """Sum the weights of a top-10 list, returned in percent.

    Yahoo top-10 weights are fractions (0–1); this returns the sum
    multiplied by 100 so it's directly comparable to a user's threshold
    setting like 90.0.

    Args:
        top_rows: Output of :func:`extract_holdings`.

    Returns:
        Sum in percent (e.g. ``94.7`` for "the top 10 cover 94.7% of the
        fund"). ``0.0`` for empty / malformed input.
    """
    total = 0.0
    for r in top_rows or []:
        w = r.get("weight")
        try:
            wf = float(w) if w is not None else 0.0
        except (TypeError, ValueError):
            wf = 0.0
        if wf > 0:
            total += wf
    return round(total * 100.0, 4)


# ---------------------------------------------------------------------------
# Cached gateway
# ---------------------------------------------------------------------------
def get_category(yf_sym: str, isin: str, category: str, cache_cfg: dict,
                 extractor: Callable[[], Any], *, force: bool = False
                 ) -> tuple[Any, dict]:
    """Return cached data, or run the extractor and cache the result.

    The cache splits by category: listing-level categories
    (price_history, profile, upload_prefs) are keyed by ticker; fund-
    level categories (holdings, sectors, asset_class, asset_allocation)
    are keyed by ISIN. ``get_category`` routes to the right key
    automatically based on ``category``.

    Args:
        yf_sym: Resolved Yahoo ticker (used as the key for listing-level
            categories).
        isin: ISIN (used as the key for fund-level categories).
        category: One of :data:`porxpy.config.CACHE_CATEGORIES`.
        cache_cfg: Per-category config (output of
            :func:`porxpy.utils.normalise_cache_config`).
        extractor: Zero-argument callable invoked on cache miss.
        force: If True, bypass the cache and always invoke ``extractor``.

    Returns:
        ``(value, meta)``. ``meta.source`` is ``"cache"`` or ``"live"``.
    """
    from porxpy.utils import cache_get   # local import to avoid surface bloat
    from porxpy.config import FUND_CATEGORIES
    key = isin if category in FUND_CATEGORIES else yf_sym
    cat_cfg = cache_cfg.get(category, {})
    enabled = bool(cat_cfg.get("enabled"))

    if enabled and not force:
        cached, meta = cache_get(key, category, cache_cfg)
        if cached is not None:
            return cached, {"source": "cache", "cache_enabled": True, **meta}

    t0 = time.time()
    value = extractor()
    print(f"[Live] {yf_sym}/{category} in {time.time() - t0:.2f}s")

    if enabled:
        meta = cache_put(key, category, value)
        return value, {
            "source": "live", "cache_enabled": True,
            "fetched_at": meta["fetched_at"], "age_days": meta["age_days"],
            "ttl_days": cat_cfg.get("ttl_days", 0),
        }
    return value, {"source": "live", "cache_enabled": False}


def get_price_history_cached(yf_sym: str, ticker: yf.Ticker,
                             cache_cfg: dict, *, force: bool = False
                             ) -> tuple[list[dict], dict]:
    """Smart price-history loader with incremental top-up.

    Decision tree (with ``cache_cfg.price_history.enabled = True``):

    * ``force=True`` OR no cache OR ``cache_age >
      PRICE_HISTORY_FULL_REFRESH_DAYS`` → full refresh via
      :func:`extract_price_history`.
    * ``cache_age <= TTL`` → return cache as-is, but try to top up with
      the live quote if today's bar is missing.
    * ``TTL < cache_age <= PRICE_HISTORY_FULL_REFRESH_DAYS`` →
      incremental: fetch bars since last cached date, append, save.

    Args:
        yf_sym: Yahoo ticker (and cache key).
        ticker: yfinance Ticker (for the actual data calls).
        cache_cfg: Per-category cache config.
        force: Bypass cache entirely if True.

    Returns:
        ``(rows, meta)`` where ``meta.mode`` is one of ``cache_hit``,
        ``live_quote_topup``, ``incremental``, ``full_refresh``,
        ``full_refresh_no_real_rows``, or ``full_refresh_bad_date``.
    """
    cat_cfg = cache_cfg.get("price_history", {})
    enabled = bool(cat_cfg.get("enabled"))
    ttl     = cat_cfg.get("ttl_days", 0)

    if not enabled:
        return extract_price_history(ticker), {"source": "live", "cache_enabled": False}

    blob   = cache_read(yf_sym, "price_history")
    entry  = blob.get("price_history")
    cached = (entry or {}).get("value") if entry else None
    age    = age_days((entry or {}).get("fetched_at", "")) if entry else None

    # ── Full refresh paths ────────────────────────────────────────────────
    full_refresh_reason = None
    if force:
        full_refresh_reason = "force=True"
    elif not entry or not cached:
        full_refresh_reason = "no cache"
    elif age is None:
        full_refresh_reason = "unparseable timestamp"
    elif age > PRICE_HISTORY_FULL_REFRESH_DAYS:
        full_refresh_reason = f"cache age {age:.1f}d > {PRICE_HISTORY_FULL_REFRESH_DAYS}d threshold"

    if full_refresh_reason:
        print(f"[Price/cache] {yf_sym} full refresh ({full_refresh_reason})")
        t0 = time.time()
        value = extract_price_history(ticker)
        print(f"[Live] {yf_sym} price_history in {time.time() - t0:.2f}s ({len(value)} rows)")
        meta = cache_put(yf_sym, "price_history", value)
        return value, {
            "source":      "live",
            "cache_enabled": True,
            "fetched_at":  meta["fetched_at"],
            "age_days":    meta["age_days"],
            "ttl_days":    ttl,
            "row_count":   len(value),
            "mode":        "full_refresh",
        }

    # ── Cache fresh enough that we don't need a network call at all ──────
    base_rows = list(cached) if isinstance(cached, list) else []
    if age is not None and age <= ttl:
        topped, topup_meta = _maybe_topup_live(ticker, base_rows)
        if topped is not None:
            meta = cache_put(yf_sym, "price_history", topped)
            return topped, {
                "source":      "incremental",
                "cache_enabled": True,
                "fetched_at":  meta["fetched_at"],
                "age_days":    meta["age_days"],
                "ttl_days":    ttl,
                "row_count":   len(topped),
                "added_rows":  topup_meta["added"],
                "mode":        "live_quote_topup",
            }
        # Pure cache hit — no network.
        return base_rows, {
            "source":      "cache",
            "cache_enabled": True,
            "fetched_at":  entry.get("fetched_at"),
            "age_days":    round(age, 3),
            "ttl_days":    ttl,
            "row_count":   len(base_rows),
            "mode":        "cache_hit",
        }

    # ── Incremental: TTL < age <= 30d ─────────────────────────────────────
    real_rows = _strip_synthetic_tail(base_rows)
    last_date = real_rows[-1]["date"] if real_rows else None

    if last_date is None:
        # Cache exists but has no real rows. Treat as a full refresh.
        print(f"[Price/cache] {yf_sym} cache has no real bars; full refresh")
        t0 = time.time()
        value = extract_price_history(ticker)
        print(f"[Live] {yf_sym} price_history in {time.time() - t0:.2f}s ({len(value)} rows)")
        meta = cache_put(yf_sym, "price_history", value)
        return value, {
            "source":      "live",
            "cache_enabled": True,
            "fetched_at":  meta["fetched_at"],
            "age_days":    meta["age_days"],
            "ttl_days":    ttl,
            "row_count":   len(value),
            "mode":        "full_refresh_no_real_rows",
        }

    try:
        last_dt   = datetime.strptime(last_date, "%Y-%m-%d")
        start_str = (last_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception as exc:
        print(f"[Price/cache] {yf_sym} bad last_date {last_date}: {exc}; full refresh")
        value = extract_price_history(ticker)
        meta  = cache_put(yf_sym, "price_history", value)
        return value, {
            "source": "live", "cache_enabled": True,
            "fetched_at": meta["fetched_at"], "age_days": meta["age_days"],
            "ttl_days": ttl, "row_count": len(value), "mode": "full_refresh_bad_date",
        }

    print(f"[Price/cache] {yf_sym} incremental from {start_str} "
          f"(cache has {len(real_rows)} rows, last={last_date}, age={age:.1f}d)")
    t0 = time.time()
    new_rows = _fetch_history_since(ticker, start_str)
    print(f"[Live] {yf_sym} price_history incremental in {time.time() - t0:.2f}s "
          f"({len(new_rows)} new rows)")

    # De-dup defensively in case Yahoo's start= was inclusive of our last_date
    seen = {r["date"] for r in real_rows}
    appended = [r for r in new_rows if r["date"] not in seen]
    merged   = real_rows + appended

    topped, topup_meta = _maybe_topup_live(ticker, merged)
    if topped is not None:
        merged = topped

    # Persist — bump fetched_at even when nothing was appended (so we don't
    # re-poll Yahoo on every page reload over a weekend/holiday).
    meta = cache_put(yf_sym, "price_history", merged)
    return merged, {
        "source":      "incremental",
        "cache_enabled": True,
        "fetched_at":  meta["fetched_at"],
        "age_days":    meta["age_days"],
        "ttl_days":    ttl,
        "row_count":   len(merged),
        "added_rows":  len(appended) + (topup_meta["added"] if topped is not None else 0),
        "mode":        "incremental",
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------
def load_fund_data(isin: str, exchange: str | None, cache_cfg: dict,
                   force_refresh: bool = False,
                   known_ticker: str | None = None) -> dict:
    """Compose every per-fund extractor into a single API-ready response.

    This is the workhorse used by both ``/api/fund`` and the per-portfolio
    enrichment loop. It runs ticker resolution, profile/holdings/sectors/
    asset-class extraction, price history (smart-cached), and the iShares
    full-holdings provider, then derives the look-through breakdowns and
    the override-status surface for the UI.

    Args:
        isin: ISIN (or, in ticker-only mode, a ticker symbol used as the
            cache key).
        exchange: Optional MIC.
        cache_cfg: Per-category cache config from
            :func:`porxpy.utils.normalise_cache_config`.
        force_refresh: When True, bypass every cache and refetch live.
        known_ticker: Pre-resolved ticker — bypasses OpenFIGI entirely.

    Returns:
        Full per-fund response dict ready to be returned by the route.
        Notable fields:

        * ``ticker``, ``resolved_mic``, ``resolution`` — what the resolver
          decided.
        * ``profile``, ``sectors``, ``asset_class`` — the
          Yahoo-derived data.
        * ``holdings_rows`` — the unified per-position list (one superset
          row schema, every row carrying a ``_row_id``).
        * ``holdings_source`` — ``"manual_upload"`` / ``"yahoo_enriched"``
          / ``"yahoo_top10"`` / ``"none"``.
        * ``holdings_meta`` — provider, row count, weight sum, manual
          provenance, and the enrichment decision.
        * ``holdings_breakdowns`` — look-through rollup.
        * ``sectors_source`` — always ``"yahoo"``.
        * ``meta`` — per-category cache metadata (source, age, TTL).
    """
    yf_sym, resolved_mic, note = build_ticker(isin, exchange, known_ticker=known_ticker)
    ticker = yf.Ticker(yf_sym)

    profile, pmeta = get_category(yf_sym, isin, "profile", cache_cfg,
                                  lambda: extract_profile(ticker), force=force_refresh)
    sectors, smeta = get_category(yf_sym, isin, "sectors", cache_cfg,
                                  lambda: extract_sectors(ticker), force=force_refresh)
    asset_allocation, aameta = get_category(
        yf_sym, isin, "asset_allocation", cache_cfg,
        lambda: extract_asset_allocation(ticker), force=force_refresh)
    asset_class, ameta = get_category(yf_sym, isin, "asset_class", cache_cfg,
                                      lambda: detect_asset_class(ticker, profile or {}),
                                      force=force_refresh)

    # Per-fund asset-class override (the "Edit fund" dialog). The override
    # store is keyed by ticker and is NOT Yahoo-derived, so it survives a
    # force refresh — the line above may have just re-detected the class
    # live, and we deliberately override it again here. Applying it before
    # the holdings work below means enriched/top-10 holdings rows inherit
    # the overridden class via ``default_holding_asset_class`` too.
    from porxpy.utils import asset_class_override_get   # local: avoid cycle
    ac_override = asset_class_override_get(isin)
    if asset_class is None:
        asset_class = {"class": "other", "confidence": "low", "signals": []}
    asset_class["detected_class"] = asset_class.get("class")
    if ac_override:
        asset_class["class"]      = ac_override
        asset_class["overridden"] = True
        asset_class["confidence"] = "override"
    else:
        asset_class["overridden"] = False

    price_history, phmeta = get_price_history_cached(
        yf_sym, ticker, cache_cfg, force=force_refresh)

    # ──────────────────────────────────────────────────────────────────
    # Unified holdings slot (v0.5.0)
    #
    # There is ONE ``holdings`` cache slot. Its blob holds the
    # best-available per-position list in one superset row schema, with
    # a ``source`` field recording which degree of completeness it is:
    #
    #     "manual_upload"  — full list from a user-uploaded file
    #     "yahoo_enriched" — Yahoo top-10 + per-symbol Yahoo lookups
    #     "yahoo_top10"    — raw Yahoo top-10, sparse rows
    #
    # The slot is ``manual_refresh_only``: a present entry is ALWAYS a
    # cache hit, regardless of age. It is only refetched when the caller
    # passes ``force_refresh=True`` (the "Reload fund data" button on the
    # fund page, or "Refresh all" on the portfolio page).
    #
    # Precedence on a forced refresh: a ``manual_upload`` blob is NEVER
    # overwritten by the Yahoo path — the user's uploaded file is the
    # source of truth until they re-upload or explicitly clear it. Only
    # Yahoo-sourced (or empty) slots get refetched-and-rewritten.
    # ──────────────────────────────────────────────────────────────────
    from porxpy.utils import coerce_holdings_row   # local: avoid import cycle

    blob = cache_read(isin, "holdings")
    holdings_entry = blob.get("holdings") or {}
    holdings_blob  = holdings_entry.get("value") or {}
    if not isinstance(holdings_blob, dict):
        holdings_blob = {}

    cached_source = holdings_blob.get("source") or ""
    have_cached   = bool(holdings_blob.get("rows")) or cached_source == "manual_upload"
    is_manual     = cached_source == "manual_upload"

    # Decide whether to (re)fetch from Yahoo. We refetch when:
    #   * there's no usable cached blob at all, OR
    #   * force_refresh is set AND the cached blob is NOT a manual upload
    #     (manual uploads survive a forced refresh — they're user data).
    refetch = (not have_cached) or (force_refresh and not is_manual)

    # Enrichment metadata surfaced to the UI. ``applied`` reflects the
    # blob actually in effect after this block, whatever its origin.
    # The threshold concept is gone (0.12.7) — enrichment is now driven
    # by a per-field checklist in Settings, and always runs on a fresh
    # top-10 fetch when the user has at least one field ticked.
    enrichment_meta = {
        "applied": False,
        "reason":  "",
        "fields":  [],
        "sum_pct": None,
    }

    if not refetch:
        # Use the cached unified blob as-is. Backfill _row_id on any rows
        # that predate the schema (defence-in-depth; commit/fetch paths
        # already stamp them).
        rows = holdings_blob.get("rows") or []
        rows = [coerce_holdings_row(r) for r in rows]
        holdings_blob["rows"] = rows
        hold_age = age_days(holdings_entry.get("fetched_at", ""))
        hmeta = {
            "source":     "cache",
            "fetched_at": holdings_entry.get("fetched_at"),
            "age_days":   round(hold_age, 3) if hold_age is not None else None,
            "ttl_days":   None,   # manual_refresh_only — no expiry
        }
    else:
        # Refetch from Yahoo: pull the top-10 and shape it into the
        # unified schema. With no threshold gate, every top-10 we get
        # is run through enrich_top10_holdings (cheap for an empty
        # fields list — the function skips the per-symbol lookup when
        # no fields are selected).
        t0_h = time.time()
        top_rows = extract_holdings(ticker) or []
        print(f"[Live] {yf_sym} holdings (top-10) in {time.time() - t0_h:.2f}s")

        top10_count   = len(top_rows)
        top10_sum_pct = top10_weight_sum_pct(top_rows) if top10_count else None
        enrichment_meta["sum_pct"] = top10_sum_pct

        rows: list[dict] = []
        blob_source = "yahoo_top10"

        if top10_count > 0:
            settings = load_settings()
            en_fields = list(settings.get("enrichment", {}).get("fields") or [])
            enrichment_meta["fields"] = en_fields

            if not en_fields:
                # User has unticked every enrichment field — store the
                # raw top-10 with no per-symbol lookups.
                enrichment_meta["reason"] = "no enrichment fields selected"
                rows = raw_top10_to_rows(top_rows)
            else:
                print(f"[Enrich] {yf_sym} top-10 — enriching {top10_count} "
                      f"symbols with fields={en_fields}")
                t0_e = time.time()
                rows = enrich_top10_holdings(top_rows, fields=en_fields)
                blob_source = "yahoo_enriched"
                enrichment_meta["applied"] = True
                enrichment_meta["reason"]  = (
                    f"applied: {len(en_fields)} field(s) on "
                    f"{top10_count} symbols"
                )
                print(f"[Enrich] {yf_sym} done in {time.time() - t0_e:.2f}s "
                      f"({len(rows)} enriched rows)")
        else:
            enrichment_meta["reason"] = "no top-10 data"

        # Fund-asset-class fallback (parity with the manual-upload path):
        # any Yahoo-sourced row whose asset_class is still blank falls
        # back to the fund's asset class (equity → equity, fixed_income
        # → bond, cash → cash, else → other). raw_top10_to_rows rows are
        # always blank here; enriched rows are blank when Yahoo's
        # quoteType didn't classify the symbol. When the fund's asset
        # class isn't known, rows stay blank — and coerce_holdings_row
        # (already applied inside the row builders) leaves sub_class
        # blank to match.
        from porxpy.utils import default_holding_asset_class, default_sub_class
        fund_holding_ac = default_holding_asset_class(
            (asset_class or {}).get("class"))
        if fund_holding_ac:
            for r in rows:
                if not (r.get("asset_class") or "").strip():
                    r["asset_class"] = fund_holding_ac
                    # coerce_holdings_row already ran inside the row
                    # builder, so sub_class won't be re-defaulted for us
                    # — do it here from the freshly-filled asset class.
                    if not (r.get("sub_class") or "").strip():
                        r["sub_class"] = default_sub_class(fund_holding_ac)

        weight_sum = sum(
            float(r.get("weight_pct") or 0.0) for r in rows
        )
        holdings_blob = {
            "rows":          rows,
            "source":        blob_source,          # yahoo_top10 / yahoo_enriched
            "_provider":     "yahoo",
            "row_count":     len(rows),
            "weight_sum_pct": round(weight_sum, 6),
            "fetched_at":    now_iso(),
            "enrichment":    dict(enrichment_meta),
        }
        meta = cache_put(isin, "holdings", holdings_blob)
        hmeta = {
            "source":     "live",
            "fetched_at": meta["fetched_at"],
            "age_days":   meta["age_days"],
            "ttl_days":   None,
        }
        cached_source = blob_source
        is_manual     = False

    # ``holdings_rows`` is the single source of truth from here on.
    holdings_rows = holdings_blob.get("rows") or []
    holdings_source = holdings_blob.get("source") or "none"

    # Re-derive enrichment_meta / sum from whatever blob is in effect, so
    # a cache-hit path reports the same shape a fresh fetch would. A
    # manual upload has no enrichment concept — its sum is just the row
    # sum and ``applied`` stays False.
    weight_sum_pct = round(
        sum(float(r.get("weight_pct") or 0.0) for r in holdings_rows), 6
    ) if holdings_rows else None
    if not refetch:
        cached_enr = holdings_blob.get("enrichment") or {}
        if isinstance(cached_enr, dict) and cached_enr:
            enrichment_meta = {
                "applied": bool(cached_enr.get("applied")),
                "reason":  cached_enr.get("reason", ""),
                "fields":  list(cached_enr.get("fields") or []),
                "sum_pct": cached_enr.get("sum_pct", weight_sum_pct),
            }
        else:
            enrichment_meta["sum_pct"] = weight_sum_pct
        if holdings_source == "yahoo_enriched":
            enrichment_meta["applied"] = True

    # top10_sum_pct: kept as a response field for the UI's holdings
    # header badge. For Yahoo-sourced blobs it's the weight sum of the
    # (up to 10) rows; for a manual upload it's the full-list sum. The
    # frontend treats it as "coverage" either way.
    top10_sum_pct = weight_sum_pct

    # NOTE: the old "if no price history, try the ISIN as a Yahoo ticker"
    # fallback was removed with the three-mode fetch flow. ``yf_sym`` is
    # now always a properly resolved Yahoo ticker (never an ISIN used as
    # a placeholder), and yfinance rejects a real ISIN as a ticker
    # anyway. An empty price history is now simply reported as empty.

    # Look-through breakdowns: always rolled up from the one unified
    # holdings row set. ``breakdowns_source`` tells the frontend what
    # kind of rows fed the rollup so it can label the cards and decide
    # whether the per-position sector column is expected to be sparse.
    #   manual_upload  → "full"
    #   yahoo_enriched → "top10_enriched"
    #   yahoo_top10    → "top10_raw"  (sparse — only name/ticker/weight)
    #   (no rows)      → "none"
    rollup_rows = holdings_rows
    if holdings_source == "manual_upload":
        breakdowns_source = "full"
    elif holdings_source == "yahoo_enriched":
        breakdowns_source = "top10_enriched"
    elif holdings_source == "yahoo_top10":
        breakdowns_source = "top10_raw"
    else:
        breakdowns_source = "none"
    # Sectors always stay from Yahoo fund-level metadata (see the long
    # NOTE below) — the fund-page breakdown grid reads ``data.sectors``
    # for its Fund/ETF-level toggle and the rollup for Holdings-level.
    sectors_source = "yahoo"

    breakdowns = rollup_holdings(rollup_rows)

    # ──────────────────────────────────────────────────────────────────
    # Unified Fund/ETF-level breakdown cards (build_fund_breakdowns).
    #
    # The four cards — asset_class / sector / country / currency — each
    # show a distribution over the fund's holdings. Their Fund/ETF-level
    # data is the issuer's own published aggregate (Yahoo publishes only
    # asset_allocation and sectors; country/currency have no issuer
    # source and are empty unless overridden). The per-fund, per-card
    # breakdown-source override can flip any card to be populated from
    # the look-through holdings roll-up instead — and once overridden,
    # that card *is* the fund's Fund/ETF-level data (it rolls up into the
    # portfolio's Fund/ETF-level cards like issuer data).
    #
    # The override store is keyed by ticker and is NOT Yahoo-derived, so
    # — exactly like the asset-class override above — it survives a
    # forced refresh.
    # ──────────────────────────────────────────────────────────────────
    from porxpy.utils import (   # local: avoid cycle
        breakdown_overrides_get, uploaded_breakdowns_get,
    )
    bd_overrides    = breakdown_overrides_get(isin)
    uploaded_facets = uploaded_breakdowns_get(isin)
    fund_breakdowns = build_fund_breakdowns(
        breakdowns, sectors or [], asset_allocation or [],
        bd_overrides, uploaded_facets)

    # ──────────────────────────────────────────────────────────────────
    # Fund "Structure" block — {structure, replication, style}.
    #
    # Yahoo's quoteType/legalType seed sensible defaults (ETF vs Fund,
    # and a weak active/passive guess); Yahoo publishes no replication
    # method. Any user override stored in fund_structure.json is layered
    # on top — and, like the asset-class and breakdown overrides, it is
    # NOT Yahoo-sourced, so it survives a forced refresh.
    #
    # ``fund_structure``        — the effective block (seed + override).
    # ``fund_structure_seed``   — the Yahoo-seeded defaults alone, so the
    #                             "Edit fund" dialog can show what would
    #                             be used if the override were cleared.
    # ``fund_structure_is_override`` — whether a stored override is in
    #                             force (vs pure Yahoo seed).
    # ──────────────────────────────────────────────────────────────────
    from porxpy.utils import fund_structure_get, normalise_fund_structure
    fund_structure_seed = _seed_fund_structure(profile or {})
    _fs_override        = fund_structure_get(isin)
    if _fs_override:
        # A stored override may only specify some attributes; merge it
        # over the seed so unspecified attributes keep the Yahoo value.
        fund_structure = normalise_fund_structure({**fund_structure_seed,
                                                   **_fs_override})
        fund_structure_is_override = True
    else:
        fund_structure = fund_structure_seed
        fund_structure_is_override = False

    # NOTE: ``sectors`` is intentionally NOT overridden with the
    # look-through rollup here, even when full holdings are present.
    # The frontend's fund-page breakdown grid has a Fund/ETF-level vs
    # Holdings-level toggle (see renderFundBreakdowns in
    # fund_explorer.html) and reads two distinct sources:
    #
    #   * Fund/ETF level → data.sectors             (Yahoo fund metadata)
    #   * Holdings level → data.holdings_breakdowns.sector
    #                                               (look-through rollup)
    #
    # If we overwrote ``sectors`` with the rollup, both toggle positions
    # would render identical data for funds with full holdings — which
    # is exactly the bug this comment exists to prevent. ``sectors_source``
    # is therefore always "yahoo" in practice; it's left in the response
    # for backwards compatibility with any legacy reader.

    # Unified holdings metadata for the UI: provider, source, row count,
    # weight sum, manual-upload provenance (filename / date) when
    # applicable, top-coverage sum, and the enrichment decision. Replaces
    # the old split holdings_full_manual / top10_* fields.
    is_manual_upload = holdings_source == "manual_upload"
    holdings_meta = {
        "provider":       holdings_blob.get("_provider")
                          or ("manual" if is_manual_upload else "yahoo"),
        "source":         holdings_source,   # manual_upload / yahoo_enriched / yahoo_top10 / none
        "is_manual":      is_manual_upload,
        "row_count":      len(holdings_rows),
        "weight_sum_pct": weight_sum_pct,
        # Manual-upload provenance — null for Yahoo-sourced blobs.
        "uploaded_at":    holdings_blob.get("uploaded_at") if is_manual_upload else None,
        "filename":       holdings_blob.get("filename")    if is_manual_upload else None,
        # Top-coverage sum (frontend holdings-header badge) + enrichment
        # decision. For a manual upload, enrichment is not a concept —
        # ``applied`` is False and ``reason`` empty.
        "top10_sum_pct":  top10_sum_pct,
        "enrichment":     enrichment_meta,
    }

    return {
        "ticker":        yf_sym,
        "resolved_mic":  resolved_mic,
        "resolution":    note,
        "profile":       profile or {},
        # ── Unified holdings (v0.5.0) ──────────────────────────────────
        # One row set, one superset schema, one source tag. Every row
        # carries a stable ``_row_id`` for the holdings editor.
        "holdings_rows":   holdings_rows,
        "holdings_source": holdings_source,   # manual_upload / yahoo_enriched / yahoo_top10 / none
        "holdings_meta":   holdings_meta,
        # Look-through facet breakdowns (sector / currency / country /
        # asset_class) rolled up from ``holdings_rows``. Empty arrays
        # when there are no holdings rows at all — the frontend uses
        # that as the cue to show "no holdings, please upload a file".
        "holdings_breakdowns": breakdowns,
        "breakdowns_source":   breakdowns_source,   # full / top10_enriched / top10_raw / none
        "sectors":         sectors or [],
        "sectors_source":  sectors_source,   # always "yahoo" (see NOTE above)
        # Issuer-published asset-allocation breakdown (equity / bond /
        # cash / other split). [] when the issuer published nothing.
        "asset_allocation": asset_allocation or [],
        # Unified Fund/ETF-level breakdown cards (asset_class / sector /
        # country / currency), each {items, source, issuer_available,
        # holdings_available} — see build_fund_breakdowns. Per-card
        # holdings overrides are already applied here.
        "fund_breakdowns":  fund_breakdowns,
        # The fund's persisted per-card breakdown-source overrides
        # ({facet: "holdings"}). Empty when none are set. Surfaced so the
        # UI can render the per-card toggles in their current state.
        "breakdown_overrides": bd_overrides,
        # Fund "Structure" metadata — {structure, replication, style}.
        # ``fund_structure`` is the effective block (Yahoo seed with any
        # user override merged on top); ``fund_structure_seed`` is the
        # Yahoo-seeded default alone (what the "Edit fund" dialog falls
        # back to if the override is cleared); ``fund_structure_is_override``
        # flags whether a stored override is in force.
        "fund_structure":            fund_structure,
        "fund_structure_seed":       fund_structure_seed,
        "fund_structure_is_override": fund_structure_is_override,
        "asset_class":     asset_class,
        "price_history":   price_history or [],
        "meta": {
            "profile": pmeta, "holdings": hmeta,
            "sectors": smeta, "asset_class": ameta,
            "asset_allocation": aameta, "price_history": phmeta,
        },
        "holdings_note": (
            f"Yahoo Finance provides top {len(holdings_rows)} holdings only."
            if holdings_source in ("yahoo_top10", "yahoo_enriched") and holdings_rows
            else ("No holdings data available from Yahoo Finance."
                  if not holdings_rows else "")
        ),
    }
