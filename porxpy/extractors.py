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
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd
import yfinance as yf

# Pin the TLS impersonation profile before any Yahoo call is made. See
# porxpy.yf_session for why: yfinance asks curl_cffi for the NEWEST
# Chrome profile, and Chrome 124+ profiles fail the handshake through
# TLS-inspecting middleboxes, reported misleadingly as a certificate
# error.
from porxpy.yf_session import install as _install_yf_session
_install_yf_session()

from porxpy.config import (
    rollup_label_of,
    BREAKDOWN_FACETS,
    DEFAULT_FUND_STRUCTURE,
    DEFAULT_INCLUDE_IN_OPTIMIZER,
    ENRICHABLE_FIELDS,
    HISTORY_PERIOD_FALLBACKS,
    PRICE_HISTORY_FULL_REFRESH_DAYS,
)
from porxpy.resolver import (
    search_name_only,
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


# Yahoo keys already reported as unresolved. Once per key per run: the
# allocation extractor runs on every fund load, and a key Yahoo emits
# for a whole fund family would otherwise print on each one.
_ASSET_KEYS_REPORTED: set[str] = set()


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
        # are carried in the `matches` column of Asset_definitions.csv,
        # on the SUPER-class rows: Yahoo is saying "this slice is
        # equity", not naming an instrument. The tree answers at
        # whatever level the key names, and this distribution is built
        # at the facet's default grain. Unmapped keys → "other".
        # An unrecognised key still folds to "other", because a slice of
        # a fund has to land somewhere and dropping it would misstate
        # the whole distribution. But it says so: "other" is a real
        # classification an issuer can assert, so absorbing an unknown
        # key into it silently made a new Yahoo key indistinguishable
        # from a fund that genuinely holds other. The key set is small
        # and stable, so a log line is the proportionate answer — the
        # fix is a one-word alias edit, not a release.
        from porxpy.resources import resolve_asset_tree
        from porxpy.config import FACET_DEFAULT_LEVEL
        _lvl = FACET_DEFAULT_LEVEL.get("asset_class", "super_class")
        _tree = resolve_asset_tree(str(raw_key))
        canon = _tree.get(_lvl) if _tree.get("level") else ""
        if not canon or canon == "unknown":
            if str(raw_key) not in _ASSET_KEYS_REPORTED:
                _ASSET_KEYS_REPORTED.add(str(raw_key))
                print(f"[AssetAllocation] Yahoo key {raw_key!r} "
                      f"({wf:.1%}) does not resolve — counted as \"other\". "
                      f"Add it as an alias on the matching row of "
                      f"Asset_definitions.csv to classify it.")
            canon = "other"
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


# Compiled regexes for distribution detection. Word-boundary anchored
# so we don't false-match "Distribution" against parts of unrelated words
# in the fund name, and we cover the common parenthesised forms.
_DIST_ACCUMULATING = re.compile(
    r"\b(?:accumulating|accumulation|acc|c[ ]*acc)\b",
    re.IGNORECASE,
)
_DIST_DISTRIBUTING = re.compile(
    r"\b(?:distributing|distribution|dist|inc|income|d[ ]*inc)\b",
    re.IGNORECASE,
)


def detect_distribution(profile: dict, info: dict) -> str:
    """Detect distribution policy (accumulating / distributing / unknown).

    Yahoo Finance does not have a clean field for this, so we combine
    two signals:

    1. **Fund name** — most issuers tag the share class in the long or
       short name (e.g. ``"iShares Core MSCI World UCITS ETF USD (Acc)"``
       or ``"Vanguard FTSE All-World UCITS ETF (USD) Distributing"``).
       This is the strongest signal when present.
    2. **Dividend yield** — when ``trailingAnnualDividendYield`` is
       greater than zero, the fund has paid out in the last year and
       must be distributing. When it is zero or missing, the fund is
       likely accumulating, but could also be a brand-new fund or an
       equity index that simply hasn't paid yet — so this signal is
       weaker.

    Both signals are combined: if name says one thing and yield agrees
    we return that with confidence; if they disagree we trust the name;
    if both are silent we return ``unknown``.

    Args:
        profile: The profile dict being built. We read ``longName`` and
            ``shortName`` from it.
        info: The raw ``ticker.info`` dict (for the yield signal).

    Returns:
        One of ``"accumulating"``, ``"distributing"``, ``"unknown"``.
    """
    name_parts = [
        (profile.get("longName")  or ""),
        (profile.get("shortName") or ""),
    ]
    name = " ".join(name_parts).strip()

    name_acc  = bool(_DIST_ACCUMULATING.search(name)) if name else False
    name_dist = bool(_DIST_DISTRIBUTING.search(name)) if name else False

    # Yield signal — > 0 means the fund actually paid out, so it must
    # be distributing. < 0 is impossible; None/zero is "no signal".
    yield_positive = False
    try:
        v = info.get("trailingAnnualDividendYield")
        if v is not None and float(v) > 0:
            yield_positive = True
    except Exception:
        pass

    # Decision:
    # - Both signals agree on distributing → distributing.
    # - Both signals agree on accumulating → accumulating.
    # - Name explicit, yield silent → trust name.
    # - Name silent, yield positive → distributing.
    # - Name silent, yield zero/null → unknown (could be accumulating
    #   or a new fund that hasn't paid).
    # - Name says both (rare) → unknown.
    if name_acc and name_dist:
        return "unknown"
    if name_dist or yield_positive:
        return "distributing"
    if name_acc:
        return "accumulating"
    return "unknown"


def _compute_trailing_yield_from_dividends(ticker, info: dict) -> float | None:
    """Compute trailing 12-month yield from the actual dividend history.

    Used when Yahoo's ``trailingAnnualDividendYield`` /
    ``dividendYield`` / ``yield`` fields are all missing — which is
    common for European-listed UCITS ETFs (e.g. VWRL.AS) where Yahoo
    has the price and the dividend payments separately but never
    computes the ratio for us.

    Approach:
      1. Sum every cash dividend paid in the last 365 days.
      2. Divide by a sensible "current price" — preferring
         ``regularMarketPrice``, falling back to ``previousClose``
         then ``navPrice``.
      3. Return the result as a percent (rounded), or ``None`` if any
         input is missing or non-sensical.

    Both the dividend series and the price come back in the listing's
    trading currency, so the units cancel and no FX is needed.

    Args:
        ticker: yfinance Ticker (for ``.dividends``).
        info:   The already-fetched ``ticker.info`` dict.

    Returns:
        Yield in percent (e.g. ``1.57``) or ``None``.
    """
    # Pick a denominator. Fall back gently — any of these is a
    # reasonable current-price proxy.
    price = None
    for key in ("regularMarketPrice", "previousClose", "navPrice"):
        v = info.get(key)
        try:
            f = float(v) if v is not None else None
        except (TypeError, ValueError):
            f = None
        if f and f > 0:
            price = f
            break
    if price is None:
        return None

    # Sum trailing-365-day dividends.
    try:
        divs = ticker.dividends
    except Exception as exc:
        print(f"[Yield/divs] {exc}")
        return None
    if divs is None or len(divs) == 0:
        return None

    try:
        cutoff = pd.Timestamp.now(tz=divs.index.tz) - pd.Timedelta(days=365)
        recent = divs[divs.index >= cutoff]
        total = float(recent.sum())
    except Exception as exc:
        print(f"[Yield/divs sum] {exc}")
        return None
    if total <= 0:
        return None

    return round(total / price * 100, 4)


def _norm_metric_label(s: str) -> str:
    """Reduce a metric label to a comparison key (lowercase alphanumerics)."""
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


# Accepted spellings per metric, normalised.
#
# yfinance indexes equity_holdings by *display label*, and those are not
# mechanical transforms of Yahoo's API keys — "priceToBook" is shown as
# "Price/Book" (the "to" vanishes) and "threeYearEarningsGrowth" as
# "3 Year Earnings Growth" (word becomes digit). Normalising case and
# punctuation is therefore not enough to bridge them, so each metric
# lists every spelling we accept.
#
# "medianMarketCap" ↔ "Median Market Cap" happens to normalise to the
# same string, which is why an earlier version appeared to work for
# market cap while silently failing for both style signals.
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "medianMarketCap": ("medianmarketcap",),
    "priceToBook": ("pricetobook", "pricebook", "pb"),
    "priceToEarnings": ("pricetoearnings", "priceearnings", "pe"),
    "threeYearEarningsGrowth": ("threeyearearningsgrowth",
                                "3yearearningsgrowth"),
}


def _equity_metric(ticker: yf.Ticker, key: str):
    """Read one equity-holdings metric as ``(fund_value, category_average)``.

    yfinance returns ``funds_data.equity_holdings`` as a frame indexed by
    *display label* — ``"Median Market Cap"``, ``"Price/Book"``,
    ``"3 Year Earnings Growth"`` — not by Yahoo's camelCase API keys, with
    columns ``[<symbol>, "Category Average"]``.

    We match on a normalised form of the label so either convention
    resolves. Hardcoding the display strings would work today and break
    silently the moment yfinance relabels a row — which is exactly how the
    first cut of this failed: every lookup missed and every fund came back
    "unknown", with no error to show for it.

    Returns:
        ``(value, category_average)``, either of which may be ``None``.
    """
    try:
        eh = ticker.funds_data.equity_holdings
        if eh is None or eh.empty:
            return None, None
        wanted = set(_METRIC_ALIASES.get(key, ()))
        wanted.add(_norm_metric_label(key))
        for label in eh.index:
            if _norm_metric_label(label) not in wanted:
                continue
            row = eh.loc[label]
            vals = []
            for v in (row.tolist() if hasattr(row, "tolist") else [row]):
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    vals.append(None)
                    continue
                vals.append(f if math.isfinite(f) else None)
            fund_val = vals[0] if vals else None
            cat_val  = vals[1] if len(vals) > 1 else None
            return fund_val, cat_val
    except Exception as exc:
        print(f"[EquityMetric] {key}: {exc}")
    return None, None


def detect_market_cap(ticker: yf.Ticker) -> str:
    """Classify a fund into a market-cap bucket from Yahoo's median.

    Reads ``funds_data.equity_holdings["medianMarketCap"]`` — the median
    market cap of the fund's *equity* sleeve — and buckets it against
    :data:`porxpy.config.MARKET_CAP_BUCKETS`.

    Note what this is and isn't. It is a single scalar, so the resulting
    classification is one-hot: a fund is "large" or "mid", never 70/30.
    A large-cap fund does of course hold some mid caps; that nuance is
    lost until per-holding market caps arrive via the holdings-enrichment
    path, at which point this can become a real distribution the way
    asset_allocation already is.

    Returns:
        One of ``"large"``, ``"mid"``, ``"small"``, ``"unknown"``.
        Bond and cash funds have no equity sleeve and return ``"unknown"``
        — correctly, since market cap is an equity concept.
    """
    from porxpy.config import MARKET_CAP_BUCKETS
    val, _cat = _equity_metric(ticker, "medianMarketCap")
    if val is None:
        return "unknown"
    if not val or val <= 0 or not math.isfinite(val):
        return "unknown"
    for bucket, floor in MARKET_CAP_BUCKETS:
        if val >= floor:
            return bucket
    return "unknown"


def detect_style_box(ticker: yf.Ticker, profile: dict) -> str:
    """Classify a fund on the value–blend–growth axis.

    Yahoo publishes no style-box field, so this reads the fund's equity
    metrics *relative to its own category average* — which Yahoo helpfully
    supplies alongside each value as a ``…Cat`` row. Relative comparison
    matters: a P/B of 3.0 is growthy for a value fund and unremarkable for
    a tech fund, so an absolute threshold would misclassify whole
    categories.

    Two signals, both classic style-box axes:

    * **Price/Book** — growth stocks trade at a premium to book value.
    * **3-year earnings growth** — growth companies grow earnings faster.

    Each votes growth or value if it is more than 10% away from the
    category average; agreement gives that answer, disagreement or
    silence gives ``"blend"``. The dividend yield we already extract is
    deliberately *not* used as a third signal: high yield correlates with
    value, but a distributing share class of a growth index would then be
    mislabelled, and share-class policy has nothing to do with the
    underlying holdings.

    Returns:
        ``"growth"``, ``"blend"``, ``"value"``, or ``"unknown"`` when the
        fund has no equity metrics at all (bond and cash funds).
    """
    votes = []
    for key in ("priceToBook", "threeYearEarningsGrowth"):
        val, cat = _equity_metric(ticker, key)
        if val is None or not cat or cat <= 0:
            continue
        ratio = val / cat
        if ratio >= 1.10:
            votes.append("growth")
        elif ratio <= 0.90:
            votes.append("value")
        else:
            votes.append("blend")

    if not votes:
        return "unknown"
    if all(v == "growth" for v in votes):
        return "growth"
    if all(v == "value" for v in votes):
        return "value"
    return "blend"


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

    # ---- Fees and size ---------------------------------------------------
    # Yahoo carries these in two unrelated places, and which one is
    # populated depends on the listing rather than on anything sensible.
    # The quoteSummary "fundProfile" module (-> fund_operations) is the
    # richer source but is empty for a large share of European UCITS
    # ETFs; the flat `.info` blob often has the same numbers under
    # different keys for exactly those listings. So each figure walks a
    # chain rather than trusting one source.
    #
    # Units differ per key, which is where the bugs live:
    #   fund_operations.annualReportExpenseRatio -> fraction (0.002)
    #   info.netExpenseRatio                     -> already percent (0.20)
    #   info.annualReportExpenseRatio            -> fraction (0.002)
    # _pct_from() takes the expected unit explicitly rather than guessing
    # by magnitude — a 0.20% TER and a 0.20 fraction are indistinguishable
    # by size, so magnitude sniffing silently mangles cheap trackers.

    def _num(v):
        """float(v) or None, without raising on Yahoo's odd sentinels."""
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if (f != f or f in (float("inf"), float("-inf"))) else f

    def _pct_from(v, *, is_fraction: bool):
        """Positive percent from a source, or None.

        Zero is treated as ABSENT, not as a measurement. Yahoo returns
        0.0 for "we have no fee data" on a large share of European UCITS
        ETFs rather than omitting the key — IE00BDVPNG13 and many like
        it report a 0.00% TER and 0.0% turnover that no such fund has.

        Genuinely zero-fee funds do exist (Fidelity's ZERO index funds),
        so this rule does cost something. It is still the right default:
        a spurious 0.00% is worse than a blank, because a cost-aware
        optimiser reads it as "this fund is free" and prefers it over
        every real competitor. A blank merely says we don't know. And
        the loss is recoverable — a holder of a genuinely free fund can
        assert 0.0 through the override store, which is exactly the kind
        of "the source is wrong about this one fund" case it exists for.
        """
        f = _num(v)
        if f is None or f <= 0:
            return None
        return round(f * 100, 4) if is_fraction else round(f, 4)

    # Each chain walks on past a zero rather than stopping at it, so a
    # source that reports 0.0 cannot mask a later source that has the
    # real number.
    expense_pct = (_pct_from(ops["expenseRatioRaw"], is_fraction=True)
                   or _pct_from(info.get("netExpenseRatio"), is_fraction=False)
                   or _pct_from(info.get("annualReportExpenseRatio"),
                                is_fraction=True))

    # Turnover keeps the documented fraction convention (0.03 -> 3%). Two
    # caveats worth knowing before trusting it:
    #   * Unlike total net assets, the category column for turnover comes
    #     back <NA>, so the fund column does appear to be fund-specific.
    #   * The unit is NOT independently confirmed. VDIV.DE reports 1.155,
    #     which is 115.5% under the documented convention and 1.155% if
    #     the value is already a percent. Both are defensible for a
    #     dividend index tracker and nothing in the response settles it.
    # Left on the documented reading rather than guessed at again; check
    # one fund's KIID or annual report against the tile to confirm.
    turnover_pct = (_pct_from(ops["turnoverRaw"], is_fraction=True)
                    or _pct_from(info.get("annualHoldingsTurnover"),
                                 is_fraction=True))

    # ---- Total assets ----------------------------------------------------
    # fund_operations.totalNetAssets is NOT this fund's size. It is the
    # Morningstar CATEGORY aggregate, quoted in millions, and Yahoo
    # returns the same number in the fund column and the category-average
    # column. VDIV.DE makes it unmissable once you look:
    #
    #   Total Net Assets    84138.9100        84138.91
    #                       ^ VDIV.DE         ^ Category Average
    #
    # 84,138.91m is ~EUR 84bn across the category; the fund itself holds
    # ~EUR 8.13bn. That is the source of every wrong figure in 0.33.1
    # through 0.33.6, and of two failed attempts to rescue it by scaling.
    # There was no scale factor to find, because it was never this fund's
    # number.
    #
    # info.netAssets and info.totalAssets ARE fund-level and in plain
    # currency units — 8,130,660,900 EUR for VDIV.DE. So they are the
    # whole chain now, and fund_operations is not consulted for size at
    # all. It stays the primary source for the expense ratio, where the
    # category column comes back <NA> and the fund column is genuinely
    # fund-specific (0.0038 -> 0.38%, matching the issuer exactly).
    #
    # The floor stays as a guard rather than a heuristic: it catches a
    # figure that cannot describe a real fund instead of publishing a
    # confident "0.08M". It should no longer fire on normal listings.
    TOTAL_ASSETS_FLOOR = 1e6

    total_assets = None
    tna_source   = None
    for cand, label in ((info.get("netAssets"), "info.netAssets"),
                        (info.get("totalAssets"), "info.totalAssets")):
        f = _num(cand)
        if f is None or f <= 0:
            continue
        if f < TOTAL_ASSETS_FLOOR:
            print(f"[Profile] {label} total assets {f:g} below plausibility "
                  f"floor; treating as unknown")
            continue
        total_assets, tna_source = f, label
        break

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

    # Dividend yields. Yahoo's API has been inconsistent here over time
    # and across endpoints: ``trailingAnnualDividendYield`` is normally a
    # decimal fraction (0.0157 → 1.57%), but Yahoo started returning
    # ``dividendYield`` already in percent (1.57 → 1.57%) for some
    # securities. We auto-detect by magnitude: any value greater than 1
    # is treated as already-percent (no fund pays > 100% — yields above
    # 1 are virtually always already percent-scaled); anything ≤ 1 is
    # treated as a decimal and multiplied by 100. Either way the stored
    # value is in percent for display.
    def _yield_pct(raw):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        # > 1 → already percent; ≤ 1 → decimal fraction
        return round(v if v > 1 else v * 100, 4)

    # Trailing yield — try fields in order of accuracy:
    #   1. trailingAnnualDividendYield     (last 12 months, direct)
    #   2. yield                           (fund-info yield, sometimes present)
    #   3. computed from dividends series  (sum last 12m / current price)
    # Most funds populate (1); when missing we fall through. ``_src``
    # records which one landed so the UI can surface it in the tile.
    trail_pct = None
    trail_src = None
    for key, src in (("trailingAnnualDividendYield", "yahoo"),
                     ("yield",                      "yahoo")):
        v = _yield_pct(info.get(key))
        if v is not None:
            trail_pct, trail_src = v, src
            break
    if trail_pct is None:
        v = _compute_trailing_yield_from_dividends(ticker, info)
        if v is not None:
            trail_pct, trail_src = v, "computed"
    if trail_pct is not None:
        profile["trailingYieldPct"] = trail_pct
        profile["trailingYieldSrc"] = trail_src

    # Forward yield — try in order:
    #   1. dividendYield                   (Yahoo's published forward yield)
    #   2. fiveYearAvgDividendYield        (long-run average as backstop)
    fwd_pct = None
    fwd_src = None
    for key, src in (("dividendYield",            "yahoo"),
                     ("fiveYearAvgDividendYield", "5y_avg")):
        v = _yield_pct(info.get(key))
        if v is not None:
            fwd_pct, fwd_src = v, src
            break
    if fwd_pct is not None:
        profile["forwardYieldPct"] = fwd_pct
        profile["forwardYieldSrc"] = fwd_src

    # Distribution policy (accumulating vs distributing). Detected from
    # the fund's long/short name + yield signal — Yahoo has no clean
    # field for this. See :func:`detect_distribution`. The result is
    # stored as part of the profile so it propagates through the cache
    # category just like the rest of the profile.
    profile["distribution"] = detect_distribution(profile, info)

    # v0.27.0 — market-cap bucket and style box. Both are fund metadata in
    # the same sense asset_class is: a single classification, derived with
    # a user override, and also targetable.
    profile["market_cap"] = detect_market_cap(ticker)
    profile["style_box"]  = detect_style_box(ticker, profile)

    if overview.get("family"):       profile["fundFamily"] = overview["family"]
    if overview.get("legalType"):    profile["legalType"]  = overview["legalType"]
    if overview.get("categoryName"): profile["category"]   = overview["categoryName"]

    if expense_pct is not None:  profile["expenseRatioPct"] = expense_pct
    if turnover_pct is not None: profile["turnoverPct"]     = turnover_pct
    if total_assets is not None:
        profile["totalNetAssets"]    = total_assets
        # Which Yahoo key supplied it. The three disagree in both value
        # and apparent unit, so "where did this come from" is not a
        # curiosity — it is the first question to ask when a figure looks
        # wrong, and it was unanswerable before.
        profile["totalNetAssetsSrc"] = tna_source

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
        ``{"class": <one_of the classification keys>, "confidence": <str>,
        "signals": [str, ...], "origin": <str>}``.

        ``origin`` says which KIND of evidence decided it, which the
        source pin cannot: this whole function is the "Yahoo" pin, but
        within it a class can come from Yahoo's own holdings data
        (``"yahoo"``) or from words in the fund's name (``"name"``).
        Reporting the pin alone presented a reading of marketing copy
        as measured data. Same vocabulary as the fund_structure
        origins, which have drawn this distinction since v0.28.0.
        ``"none"`` when nothing decided it and the answer is a fallback.
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

    # The phrases live in Primary_asset_class_definitions.csv's
    # style_match column, not in four lists here. They are exactly the
    # kind of thing that needs correcting from live usage — a fund named
    # in Dutch, an issuer with a house word for money-market — and until
    # v0.69.0 that meant editing code. The precedence below stays in
    # code, because weighing a name hint against Yahoo's structural data
    # is a judgement, not a vocabulary.
    from porxpy.resources import primary_class_name_hits   # local: cycle
    name_hits = set(primary_class_name_hits(hay))

    cat_equity    = "equity"       in name_hits
    cat_bond      = "fixed_income" in name_hits
    cat_cash      = "cash"         in name_hits
    cat_commodity = "commodity"    in name_hits

    if cat_bond:      signals.append("name/category mentions bonds")
    if cat_equity:    signals.append("name/category mentions equity")
    if cat_cash:      signals.append("name/category mentions money market")
    if cat_commodity: signals.append("name/category mentions commodity")

    if cat_cash and not has_equity_data:
        return {"class": "cash", "confidence": "high",
                "signals": signals, "origin": "name"}
    if cat_commodity:
        return {"class": "commodity", "confidence": "medium",
                "signals": signals, "origin": "name"}
    if has_equity_data and has_bond_data:
        return {"class": "mixed", "confidence": "high",
                "signals": signals, "origin": "yahoo"}
    if has_equity_data or cat_equity:
        return {"class": "equity",
                "confidence": "high" if has_equity_data else "medium",
                "signals": signals,
                "origin": "yahoo" if has_equity_data else "name"}
    if has_bond_data or cat_bond:
        return {"class": "fixed_income",
                "confidence": "high" if has_bond_data else "medium",
                "signals": signals,
                "origin": "yahoo" if has_bond_data else "name"}
    return {"class": "other", "confidence": "low",
            "signals": signals or ["no usable signals"], "origin": "none"}


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


# ---------------------------------------------------------------------------
# Name-derived fund metadata (v0.28.0)
# ---------------------------------------------------------------------------
# Yahoo is silent on style box for most funds, silent on market cap for
# anything without an equity sleeve, and silent on focus always. But
# issuers encode all three in the fund's own name, because the name is
# marketing copy and these are exactly the things being marketed:
# "iShares MSCI Europe Small Cap UCITS ETF" is telling you it is a
# European small-cap fund in so many words.
#
# This is a weaker signal than Yahoo's numbers, so it fills gaps only —
# never overrides a Yahoo-derived value, let alone a user override — and
# it is tagged with its own provenance ("name") so the meta tile can say
# where the guess came from rather than passing it off as data.

# Market-cap words. Order matters only for readability; a name matching
# two different buckets resolves to "mixed", not to whichever came first.
_NAME_MARKET_CAP: tuple[tuple[str, str], ...] = (
    ("large cap",   "large"),
    ("largecap",    "large"),
    ("large",       "large"),
    ("mid cap",     "mid"),
    ("midcap",      "mid"),
    ("mid",         "mid"),
    ("small cap",   "small"),
    ("smallcap",    "small"),
    ("small",       "small"),
    ("smid",        "mixed"),      # small+mid in one word
    ("total market", "mixed"),
    ("all cap",     "mixed"),
    ("allcap",      "mixed"),
)

# Style-box words. Dividend and yield both signal value: a fund built to
# harvest income is buying cash-generative companies priced on today's
# earnings, which is the value end of the axis by definition.
_NAME_STYLE_BOX: tuple[tuple[str, str], ...] = (
    ("dividend",    "value"),
    ("yield",       "value"),
    ("income",      "value"),
    ("value",       "value"),
    ("growth",      "growth"),
    ("momentum",    "growth"),
)


def _name_tokens(name: str) -> str:
    """Normalise a fund name for phrase matching.

    Lowercases and reduces every non-alphanumeric run to a single space,
    then pads with a leading and trailing space so a caller can test for
    ``" small "`` and get word-boundary semantics for free — without
    which "smaller" reads as small-cap and "enlarged" as large-cap.
    """
    from porxpy.resources import _norm_phrase
    return f" {_norm_phrase(name)} "


def _match_phrases(padded: str,
                   table: tuple[tuple[str, str], ...]) -> set[str]:
    """Collect the distinct values whose phrase appears in ``padded``."""
    return {value for phrase, value in table
            if f" {phrase} " in padded}


def _derive_focus_from_name(padded: str) -> tuple[str, str]:
    """Derive ``(focus_type, focus_detail)`` from a normalised fund name.

    Sector beats region, per the design call: a sector fund inside a
    region ("MSCI Europe Information Technology") is a sector fund, and
    the region is the qualifier rather than the point.

    Multiple hits within one vocabulary mean the name is describing
    something we have no single value for, so we decline rather than
    guess — with one exception. Several *region* hits that all sit
    inside one super region collapse to that super region, because that
    is precisely what super regions are for: "Europe ex UK" hits both
    ``europe`` and ``unitedKingdom``, and ``europe`` is the honest
    answer.

    Returns:
        ``("sector"|"region"|"none", detail)``. ``("none", "")`` when
        nothing matches or the matches are irreconcilable.
    """
    from porxpy.resources import (
        SECTOR_STYLE_ALIASES, REGION_ALIASES, SUPER_REGION_MEMBERS,
        SUPER_REGION_KEYS,
    )

    # --- Sector first ---------------------------------------------------
    # SECTOR_STYLE_ALIASES, not SECTOR_ALIASES: the latter is the
    # holdings-normalisation vocabulary and is far too permissive for
    # free text. See _load_sectors for what went wrong when this used it.
    sectors = {canon for alias, canon in SECTOR_STYLE_ALIASES.items()
               if alias and f" {alias} " in padded}
    if len(sectors) == 1:
        return "sector", sectors.pop()
    if len(sectors) > 1:
        return "none", ""

    # --- Region ---------------------------------------------------------
    regions = {key for alias, key in REGION_ALIASES.items()
               if alias and f" {alias} " in padded}
    if not regions:
        return "none", ""
    if len(regions) == 1:
        return "region", regions.pop()

    supers = regions & set(SUPER_REGION_KEYS)
    plain  = regions - supers

    # Collapse several hits to the SMALLEST super region that covers
    # every plain region hit.
    #
    # Smallest, not merely "one that works", because the hierarchy nests:
    # "MSCI World" matches both `developed` and `world`, and answering
    # `world` would be true but useless — MSCI World is a developed-markets
    # index, and the narrower answer is the informative one. Ranking by
    # member count picks it without hard-coding a precedence order.
    #
    # This also handles names that intersect two supers with no
    # containment either way. "Developed Europe ex UK" hits `developed`,
    # `europe` and `unitedKingdom`; neither super contains the other, but
    # both cover the region hit, and `europe` is the tighter of the two.
    # Requiring one super to contain the other returned nothing at all
    # for such names.
    candidates = [
        sup for sup in supers
        if plain <= SUPER_REGION_MEMBERS.get(sup, set())
    ]
    if candidates:
        return "region", min(
            candidates,
            key=lambda k: (len(SUPER_REGION_MEMBERS.get(k, set())), k))
    return "none", ""


def derive_structure_from_name(name: str | None) -> dict:
    """Derive what a fund's *name* says about its metadata.

    Pure function of the name — no Yahoo call, no I/O beyond the
    already-loaded resource tables. Returns only the fields it can
    actually derive, so the caller can merge it as a gap-filler without
    having to know which keys count as "no opinion".

    Args:
        name: The fund's long name (``profile["longName"]``).

    Returns:
        A dict with any of ``market_cap``, ``style_box``, ``focus_type``,
        ``focus_detail``. Keys the name says nothing about are absent.
        ``focus_type`` and ``focus_detail`` always travel together.
    """
    padded = _name_tokens(name or "")
    if not padded.strip():
        return {}

    out: dict[str, str] = {}

    caps = _match_phrases(padded, _NAME_MARKET_CAP)
    if len(caps) == 1:
        out["market_cap"] = caps.pop()
    elif len(caps) > 1:
        # "Small & Mid Cap" names a fund that is genuinely both. That is
        # what "mixed" is for, and it beats throwing the signal away.
        out["market_cap"] = "mixed"

    boxes = _match_phrases(padded, _NAME_STYLE_BOX)
    if len(boxes) == 1:
        out["style_box"] = boxes.pop()
    # Two conflicting style words ("Dividend Growth") are a real
    # ambiguity with no blend-shaped answer available from a name, so
    # leave it for Yahoo's numbers or the user.

    focus_type, focus_detail = _derive_focus_from_name(padded)
    # v0.68.0 rename, applied where the derived value is produced so the
    # seeded value and a user-set one are always the same vocabulary.
    from porxpy.config import LEGACY_FOCUS_TYPES
    focus_type = LEGACY_FOCUS_TYPES.get(focus_type, focus_type)
    if focus_type != "none":
        out["focus_type"]   = focus_type
        out["focus_detail"] = focus_detail

    return out



# ---------------------------------------------------------------------------
# Per-field provenance (v0.69.3)
# ---------------------------------------------------------------------------
# Where each field's EFFECTIVE value actually came from, for every field
# the UI presents. One map, produced once, read by both the fund tiles
# and the Edit dialog.
#
# Before this the two surfaces worked it out separately and disagreed:
# the tile read fund_structure_sources and could say "inferred from
# name", while the dialog read the fields endpoint, whose fallback for
# anything unpinned was the literal string "yahoo". The same field, at
# the same moment, captioned two different ways.
#
# The rule is that a source is RECORDED, never assumed. Yahoo is a
# source when Yahoo supplied the value and at no other time. A field
# with no recorded source is absent from this map, and a field with no
# value has nothing to caption anyway — there is no provenance for an
# absence.
def _field_provenance(profile: dict, asset_class: dict, fund_structure: dict,
                      structure_sources: dict, overrides: dict,
                      isin: str | None) -> dict[str, str]:
    """Effective source per field, for every field in FIELD_GROUPS.

    Args:
        profile: The Yahoo profile block.
        asset_class: The classification block, carrying its own origin.
        fund_structure: The effective structure block.
        structure_sources: Per-field origins from _merge_fund_structure.
        overrides: Sparse per-field override envelopes for this fund.
        isin: Used to read the resolver's recorded identity sources.

    Returns:
        ``{field: source}`` covering only fields whose source is known.
        Sources are the SOURCE_LABELS keys plus ``"name"`` (inferred
        from the fund's own name) and ``"calculated"``.
    """
    from porxpy.config import FIELD_GROUPS
    from porxpy.utils import listing_identity_get

    ident_src = (listing_identity_get(isin) or {}).get("sources") or {} if isin else {}
    out: dict[str, str] = {}

    for grp in FIELD_GROUPS:
        for f in grp["fields"]:
            key = f["key"]

            # A pin is an assertion by the user about where to look, and
            # it outranks whatever the seed had.
            env = overrides.get(key) or {}
            if env.get("source"):
                out[key] = env["source"]
                continue

            if f.get("calculated"):
                out[key] = "calculated"
                continue

            if f.get("readonly"):
                # The resolver records which of the identity parts it got
                # from where. Caches written before it did carry nothing,
                # and report nothing.
                src = ident_src.get(key)
                if src:
                    out[key] = src
                continue

            if key == "primary_asset_class":
                origin = (asset_class or {}).get("origin") or ""
                if origin and origin != "none":
                    out[key] = origin
                continue

            if key in (structure_sources or {}):
                origin = structure_sources[key]
                # "default" means nobody supplied it and the neutral
                # stood in — which is an absence, not a source.
                if origin and origin != "default":
                    out[key] = origin
                continue

            # Operational and trading fields come from the Yahoo profile.
            # Yahoo is the source when Yahoo actually returned something;
            # a missing value has no source, which is why this is a
            # presence test and not a fallback.
            if profile.get(key) is not None:
                out[key] = "yahoo"

    return out


def _merge_fund_structure(seed: dict, stored: dict | None,
                          seed_origins: dict | None = None):
    """Merge stored assertions over the derived seed, per field.

    v0.33.0: ``stored`` is now a sparse map of field → override envelope,
    straight from the per-field store. A field is present exactly when
    somebody asserted it, so the merge is a plain presence test.

    That is the point of the sparse store. Previously the override was a
    dense eight-key block, so a stored "unknown" could not be told apart
    from a field the user had never touched, and a neutral-value table
    existed to guess. Guessing wrongly was the v0.27 bug: saving the Edit
    dialog once pinned every field to a snapshot of Yahoo, and no amount
    of re-fetching could dislodge it. With presence as the signal, that
    bug is no longer expressible.

    Args:
        seed: The derived structure block.
        stored: ``{field: {"value", "source", ...}}`` envelopes for the
            structure fields, from :func:`porxpy.utils.overrides_for`.
        seed_origins: Per-field provenance of the seed.

    Returns:
        ``(effective, sources)`` where ``sources`` maps each field to the
        asserting source (``"user"``, ``"justetf"``) or, where nothing was
        asserted, the seed's own origin (``"yahoo"``, ``"name"``,
        ``"default"``) — so the UI can caption each row honestly instead
        of labelling the whole block by whether *any* override exists.
    """
    from porxpy.utils import normalise_fund_structure
    seed = normalise_fund_structure(seed or {})
    stored = stored or {}

    merged, sources = {}, {}
    for field, seed_val in seed.items():
        env = stored.get(field)
        if isinstance(env, dict) and "value" in env:
            merged[field]  = env["value"]
            sources[field] = env.get("source") or "user"
        else:
            merged[field]  = seed_val
            # v0.28.0: the seed is no longer uniformly Yahoo-derived —
            # some fields are inferred from the fund's name. Caption the
            # row with where the value actually came from, so an
            # inferred value isn't presented as a measured one.
            sources[field] = (seed_origins or {}).get(field, "yahoo")

    # Re-normalise so the structure/replication coupling is re-applied to
    # the blended result, not just to each source separately.
    return normalise_fund_structure(merged), sources


def _seed_fund_structure(profile: dict,
                         asset_class: str | None = None) -> tuple[dict, dict]:
    """Derive seeded defaults for the fund "Structure" block.

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
        asset_class: The fund's detected asset class (``"equity"``,
            ``"fixed_income"``, ``"cash"``, ...), used by the v0.28.0
            gap-fillers below. Optional: omitting it costs only those
            two inferences.

    Returns:
        ``(seed, origins)``. ``seed`` is a normalised structure block;
        ``origins`` maps each field to ``"yahoo"`` (a real signal from
        Yahoo, or derived from one), ``"name"`` (inferred from the fund
        name) or ``"default"`` (nothing known — the neutral value).
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
    # v0.21.0: seed distribution from the detected value in the profile
    # (set by detect_distribution() inside extract_profile). The
    # detector returns "accumulating" / "distributing" / "unknown";
    # those are exactly the values normalise_fund_structure validates.
    distribution = (profile.get("distribution") or "unknown")

    seed = {
        "structure":    structure,
        "replication":  replication,
        "style":        style,
        "distribution": distribution,
        # v0.27.0 metadata. Derived in extract_profile (which has the
        # Ticker) and carried on the profile so the seed stays a pure
        # function of it.
        "market_cap":   profile.get("market_cap") or "unknown",
        "style_box":    profile.get("style_box")  or "unknown",
        "focus_type":   "none",
        "focus_detail": "",
    }
    origins = {k: ("default" if v in ("unknown", "none", "") else "yahoo")
               for k, v in seed.items()}

    # ---- v0.28.0 gap-fillers -------------------------------------------
    # Each of these only speaks where Yahoo was silent, and each records
    # its own provenance so the meta tile can distinguish a measured
    # value from an inferred one.

    ac = (asset_class or "").strip().lower()

    # Cash has no market cap — not "we don't know it", but "the concept
    # doesn't apply". Bonds are deliberately NOT included: a bond issuer
    # has a market cap, we just rarely learn it from Yahoo, and that is
    # what "unknown" already says.
    if seed["market_cap"] == "unknown" and ac == "cash":
        seed["market_cap"] = "n/a"
        origins["market_cap"] = "yahoo"

    # A fixed-income fund's return comes from coupons rather than
    # capital appreciation. That is the value end of the style axis on
    # any reading of it. Money-market funds land here for the same
    # reason, and it keeps a cash *fund* consistent with the cash
    # *position* the rollup synthesises.
    if seed["style_box"] == "unknown" and ac in ("fixed_income", "cash"):
        seed["style_box"] = "value"
        origins["style_box"] = "yahoo"

    # Last: what the issuer wrote on the tin. Weakest of the three, so
    # it fills only what is still empty.
    from_name = derive_structure_from_name(profile.get("longName")
                                           or profile.get("shortName"))
    for field, value in from_name.items():
        if seed.get(field) in ("unknown", "none", ""):
            seed[field] = value
            origins[field] = "name"
    # focus_detail travels with focus_type: if the type came from the
    # name, so did the detail, even though "" is its neutral value.
    if origins.get("focus_type") == "name":
        origins["focus_detail"] = "name"

    return normalise_fund_structure(seed), origins


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
              "ok":           bool,     # did the page fetch + parse?
              "source":       str,      # human-readable source label
              "url":          str,      # the page consulted
              "replication":  {"value": <method|None>, "confidence": str},
              "style":        {"value": <"active"|"passive"|None>,
                               "confidence": str},
              "distribution": {"value": <"accumulating"|"distributing"|None>,
                               "confidence": str},
              "note":         str,      # populated when ok is False
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
        "ok":           False,
        "source":       "justETF",
        "url":          "",
        "replication":  {"value": None, "confidence": "none"},
        "style":        {"value": None, "confidence": "none"},
        "distribution": {"value": None, "confidence": "none"},
        "note":         "",
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

    # ---- Distribution policy -------------------------------------------
    # justETF labels each fund's distribution policy explicitly in the
    # profile (often near "Distribution policy" or in the share-class
    # name). High-confidence matches are exact phrasings; we fall back
    # to looser substring matches only when no explicit phrasing exists.
    if ("distribution policy accumulating" in text
            or "policy: accumulating" in text
            or "fund is accumulating" in text):
        result["distribution"] = {"value": "accumulating", "confidence": "high"}
    elif ("distribution policy distributing" in text
            or "policy: distributing" in text
            or "fund is distributing" in text):
        result["distribution"] = {"value": "distributing", "confidence": "high"}
    elif "accumulating" in text and "distributing" not in text:
        result["distribution"] = {"value": "accumulating", "confidence": "low"}
    elif "distributing" in text and "accumulating" not in text:
        result["distribution"] = {"value": "distributing", "confidence": "low"}

    if (result["replication"]["value"] is None
            and result["style"]["value"] is None
            and result["distribution"]["value"] is None):
        result["note"] = ("Reached the justETF page but could not parse "
                           "replication, style, or distribution from it — "
                           "set them manually.")
        result["ok"] = False

    return result


def extract_symbol_info(symbol: str) -> dict:
    """Pull HQ country / trading currency / asset-class / sector for one holding.

    Hits ``yfinance.Ticker(symbol).info`` once. All fields default to ``""``
    on lookup failure or missing keys — never raises.

    Returned values are CANONICAL forms ready for the rollup chokepoint
    (see :func:`porxpy.breakdowns.resolve_facet_value`):

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
    # Local import — resources depends only on config, no circular risk.
    from porxpy.resources import country_to_mstar

    out = {"country": "", "currency": "", "asset_class": "",
           "sub_class": "", "sector": "",
           "name": "", "quote_type": ""}
    if not symbol:
        return out
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        # Carry the reason out with the empty result. Without it the
        # caller cannot tell "Yahoo has no such symbol" from "we never
        # reached Yahoo", and an outage reads on screen as though every
        # one of your holdings were unknown to the data source.
        print(f"[SymbolInfo] {symbol} lookup failed: {exc}")
        out["_error"] = f"{type(exc).__name__}: {exc}"
        return out

    raw_country = (info.get("country")   or "").strip()
    currency    = (info.get("currency")  or "").strip().upper()
    qt          = (info.get("quoteType") or "").strip().upper()
    name        = (info.get("longName") or info.get("shortName") or "").strip()
    sector      = (info.get("sector") or "").strip()

    # Yahoo's own wording, passed through (v0.76.0). This used to fold
    # "United States" to the mstar form here so it would merge with
    # upload rows in the rollup. Merging is no longer this function's
    # problem — every facet value is stored as the source said it and
    # resolved by normalise_facets on read, so two spellings of one
    # country merge because they resolve to one node, not because one
    # writer happened to canonicalise on the way past.
    country = raw_country

    ac = _quotetype_to_asset_class(qt)
    # Sub class is derived from the (holding-flavoured) asset class via
    # the same mapping the upload pipeline uses. For ETF / unknown quote
    # types ac is blank, and sub_class follows — the enrichment loop
    # downstream only fills non-blank values, so blanks here are safe.
    from porxpy.utils import default_holding_asset_class
    # No sub class derived here. normalise_facets fills the levels below
    # a stated value only where the tree gives one answer, so inventing
    # one from a Yahoo quoteType — which says "this is equity" and
    # nothing finer — would assert a grain no source stated.
    sub = ""

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


# Which row column each symbol-info field feeds. The facet fields land
# in their raw column; ``sub_class`` shares the asset tree's single raw
# column with ``asset_class``, exactly as an upload's two asset columns
# do. Fields absent from this map keep their own name.
_ENRICH_TARGET: dict[str, str] = {
    "country":     "country_raw",
    "currency":    "currency_raw",
    "sector":      "sector_raw",
    "asset_class": "asset_raw",
    "sub_class":   "asset_raw",
}


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
    # Transport errors seen while probing. Any non-empty value means the
    # network, not Yahoo, is why nothing was found — which must not be
    # cached as a negative alias and must not be reported as a miss.
    _probe_error = ""

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
                if info.get('_error') and not _probe_error: _probe_error = info['_error']
                info["_found"] = _info_looks_found(info)
                symbol_info_put(id_ticker, info)
                if info["_found"]:
                    info["_resolved_ticker"] = id_ticker
                    print(f"[SymbolInfo] no ticker — resolved via {_id_label}"
                          f" ({_id_val}) → {id_ticker}")
                    return info
        # Name search as last resort when no ticker and no id resolved
        if name:
            # Name-only: search_name_variant needs a ticker to verify a
            # hit against and returns nothing without one, which is why
            # this branch could never resolve anything before v0.77.0.
            name_cand = search_name_only(name)
            if name_cand:
                info = extract_symbol_info(name_cand)
                if info.get('_error') and not _probe_error: _probe_error = info['_error']
                info["_found"] = _info_looks_found(info)
                symbol_info_put(name_cand, info)
                if info["_found"]:
                    info["_resolved_ticker"] = name_cand
                    print(f"[SymbolInfo] no ticker — resolved via name"
                          f" search ('{name}') → {name_cand}")
                    return info
        if _probe_error:
            return {"_found": False, "_error": _probe_error}
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
        if info.get('_error') and not _probe_error: _probe_error = info['_error']
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
                if info.get('_error') and not _probe_error: _probe_error = info['_error']
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
            if info.get('_error') and not _probe_error: _probe_error = info['_error']
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
                if info.get('_error') and not _probe_error: _probe_error = info['_error']
                info["_found"] = _info_looks_found(info)
                symbol_info_put(name_cand, info)
                if info["_found"]:
                    alias_put(cleaned, name_cand)
                    info["_resolved_ticker"] = name_cand
                    print(f"[SymbolInfo] {cleaned} resolved via name search"
                          f" ('{name}') → {name_cand}")
                    return info

    # All fallbacks failed. Record a negative alias so the next probe of
    # this raw input doesn't re-run the full loop — but ONLY when Yahoo
    # actually answered. Caching a miss produced by an unreachable
    # network would make the outage outlive it: every symbol probed
    # during the outage would stay negatively aliased afterwards.
    if _probe_error:
        return {"_found": False, "_error": _probe_error}
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
            cur = row.get(_ENRICH_TARGET.get(f, f))
            is_empty = cur is None or (
                isinstance(cur, str) and not cur.strip())
            if not is_empty:
                continue

        # Facet values go to <facet>_raw; everything else (name,
        # quote_type) keeps its own column. Writing the level column
        # directly would be overwritten by the next normalise pass,
        # which re-derives every level from the raw.
        row[_ENRICH_TARGET.get(f, f)] = v_s
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
        # Distinct from the above: the lookup could not be COMPLETED
        # (TLS, DNS, proxy, rate limit), so nothing is known either way.
        "rows_lookup_failed":     0,
        "lookup_error":           "",
        "rows_with_changes":      0,
    }
    if not eff_fields or not rows:
        return rows, stats

    for row in rows:
        stats["rows_processed"] += 1
        sym      = (row.get("ticker") or "").strip()
        row_isin = row.get("isin")  or None
        row_cusip= row.get("cusip") or None
        row_name = row.get("name")  or None
        # A name is enough to try with (v0.77.0). It used to take a
        # ticker, an ISIN or a CUSIP, which meant a factsheet's position
        # table — names and weights, routinely nothing else — could not
        # be enriched at all, and the button reported every row skipped.
        # The name path is strict about what it accepts; see
        # resolver.search_name_only.
        if not sym and not row_isin and not row_cusip and not row_name:
            stats["rows_skipped_no_ticker"] += 1
            continue
        try:
            info = get_symbol_info_cached(
                sym,
                isin=row_isin,
                cusip=row_cusip,
                name=row_name,
            )
        except Exception as exc:
            # A transport failure is NOT a miss, and counting it as one
            # is how an unreachable Yahoo looks exactly like a Yahoo
            # that has never heard of your holdings: every row "not
            # found", nothing filled, no error anywhere. Recorded
            # separately, with the first message kept, so the dialog can
            # say "could not reach Yahoo" instead of implying the data
            # does not exist.
            stats["rows_lookup_failed"] += 1
            if not stats["lookup_error"]:
                stats["lookup_error"] = f"{type(exc).__name__}: {exc}"
            print(f"[enrich_existing] {sym} error: {exc}")
            continue
        if not isinstance(info, dict) or not info.get("_found"):
            if isinstance(info, dict) and info.get("_error"):
                stats["rows_lookup_failed"] += 1
                if not stats["lookup_error"]:
                    stats["lookup_error"] = str(info.get("_error"))
            else:
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
                 extractor, *, force: bool = False,
                 commit: bool = True
                 ):
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
        commit: v0.21.0 explicit-save model. When False AND the cache
            entry does not already exist, the extractor still runs but
            the result is NOT written to disk -- it is returned in
            memory only. When True (default), or when an entry already
            exists (refresh of an already-saved fund), writes happen as
            before. Rationale: the file's presence in ``cache/listings/``
            IS the marker that the fund is saved to the pre-loaded list.

    Returns:
        ``(value, meta)``. ``meta.source`` is ``"cache"`` or ``"live"``.
    """
    from porxpy.utils import cache_get, cache_read as _cache_read
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

    # Decide whether to persist. Write if either the caller explicitly
    # committed, or the entry already exists on disk (refresh of an
    # already-saved fund). Otherwise the data flows back in memory only.
    if enabled and (commit or bool(_cache_read(key, category))):
        meta = cache_put(key, category, value)
        return value, {
            "source": "live", "cache_enabled": True,
            "fetched_at": meta["fetched_at"], "age_days": meta["age_days"],
            "ttl_days": cat_cfg.get("ttl_days", 0),
        }
    return value, {"source": "live", "cache_enabled": enabled,
                   "committed": False}


def get_price_history_cached(yf_sym: str, ticker: yf.Ticker,
                             cache_cfg: dict, *, force: bool = False,
                             commit: bool = True
                             ) -> tuple[list[dict], dict]:
    """Smart price-history loader with incremental top-up.

    Decision tree (with ``cache_cfg.price_history.enabled = True``):

    * ``force=True`` OR no cache OR ``cache_age >
      PRICE_HISTORY_FULL_REFRESH_DAYS`` → full refresh via
      :func:`extract_price_history`.
    * ``cache_age <= TTL`` → return the cache as-is. **No network call.**
    * ``TTL < cache_age <= PRICE_HISTORY_FULL_REFRESH_DAYS`` →
      incremental: fetch bars since last cached date, append, save.

    TTL semantics are strict, which makes ``ttl_days`` directly useful as
    a "how often may this fund hit the network?" dial:

    * ``ttl_days = 0`` — age is always > 0, so the cache branch is never
      taken and every load refetches. Use this for always-current pricing.
    * ``ttl_days = 1`` — at most one fetch per fund per day. A portfolio
      opened repeatedly in one day costs zero network calls after the
      first load.

    Args:
        yf_sym: Yahoo ticker (and cache key).
        ticker: yfinance Ticker (for the actual data calls).
        cache_cfg: Per-category cache config.
        force: Bypass cache entirely if True.
        commit: v0.21.0 explicit-save model. When False AND no existing
            price-history cache for this ticker, the data is fetched
            but not written. When True (default) or when an entry
            already exists, writes happen as before.

    Returns:
        ``(rows, meta)`` where ``meta.mode`` is one of ``cache_hit``,
        ``incremental``, ``full_refresh``, ``full_refresh_no_real_rows``,
        or ``full_refresh_bad_date``.
    """

    # v0.21.0 explicit-save: only write to disk when commit=True OR an
    # entry already exists (refresh of saved fund).
    from porxpy.utils import cache_read as _cache_read
    should_write = commit or bool(_cache_read(yf_sym, "price_history"))

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
        if should_write:
            meta  = cache_put(yf_sym, "price_history", value)
        else:
            meta  = {"fetched_at": now_iso(), "age_days": 0.0, "committed": False}
        return value, {
            "source":      "live",
            "cache_enabled": True,
            "fetched_at":  meta["fetched_at"],
            "age_days":    meta["age_days"],
            "ttl_days":    ttl,
            "row_count":   len(value),
            "mode":        "full_refresh",
        }

    # ── Within TTL: pure cache hit, zero network calls ───────────────────
    #
    # This path used to call _maybe_topup_live() to append today's live
    # quote when the cached series ended yesterday. That defeated the
    # whole point of the TTL: the top-up is a network round-trip, and
    # "today's bar is missing" is true on essentially every app start
    # (overnight, weekends, holidays). So a portfolio of N funds fired N
    # live-quote calls on every single load despite the cache being
    # perfectly fresh — slow, and surprising given the user had asked for
    # a 1-day TTL.
    #
    # TTL now means what it says:
    #   ttl_days = 0  → age is always > 0, so this branch never taken;
    #                   every load refetches (fall through to incremental).
    #   ttl_days = 1  → one fetch per day, and nothing in between.
    #
    # The cost is that an intraday chart won't show today's moving price
    # until the TTL lapses. That's the trade the TTL is *for*; a user who
    # wants live intraday can set ttl_days = 0, or hit Reload.
    base_rows = list(cached) if isinstance(cached, list) else []
    if age is not None and age <= ttl:
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
        if should_write:
            meta  = cache_put(yf_sym, "price_history", value)
        else:
            meta  = {"fetched_at": now_iso(), "age_days": 0.0, "committed": False}
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
        if should_write:
            meta   = cache_put(yf_sym, "price_history", value)
        else:
            meta   = {"fetched_at": now_iso(), "age_days": 0.0, "committed": False}
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
    if should_write:
        meta  = cache_put(yf_sym, "price_history", merged)
    else:
        meta  = {"fetched_at": now_iso(), "age_days": 0.0, "committed": False}
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
def build_holdings_meta(isin: str, blob: dict, source: str, store: dict, *,
                        enrichment_meta: dict | None = None,
                        top10_sum_pct: float | None = None) -> dict:
    """Describe the holdings a fund is showing, for the holdings tile.

    Why this is a function rather than a literal inside the fund load:
    three endpoints hand the tile its state — the fund load, the source
    switch, and (indirectly) the enrichment button — and a tile told
    three slightly different stories about the same rows is exactly the
    kind of drift a source selector makes visible. One builder, called
    from each.

    Args:
        isin: Fund ISIN.
        blob: The per-source holdings blob in effect.
        source: Which of :data:`~porxpy.config.HOLDINGS_SOURCES` that is.
        store: Every source's blob, for the availability map.
        enrichment_meta: The enrichment decision, when the caller has
            just made one. Defaults to the blob's own stored record.
        top10_sum_pct: Coverage sum for the header badge. Defaults to
            the blob's weight sum.

    Returns:
        The ``holdings_meta`` block the frontend reads.
    """
    from porxpy.utils import holdings_sources_available, override_get

    weight_sum_pct = blob.get("weight_sum_pct")
    if weight_sum_pct is None and blob.get("rows"):
        weight_sum_pct = round(
            sum(float(r.get("weight_pct") or 0.0)
                for r in blob.get("rows") or []), 6)
    if top10_sum_pct is None:
        top10_sum_pct = weight_sum_pct
    if enrichment_meta is None:
        enrichment_meta = blob.get("enrichment") or {}

    holdings_source = blob.get("source") or "none"
    holdings_rows   = blob.get("rows") or []
    is_manual_upload = holdings_source == "manual_upload"
    return {
        "provider":       blob.get("_provider")
                          or ("manual" if is_manual_upload else "yahoo"),
        "source":         holdings_source,   # manual_upload / yahoo_enriched / yahoo_top10 / factsheet / none
        # v0.77.0 — the holdings tile's source selector. ``source_key``
        # is which of the three sources is in effect, ``available`` which
        # of them this fund has at all (so the tile can strike through the
        # rest), and ``pinned`` the user's stored choice — empty when they
        # have made none and precedence is deciding.
        "source_key":     source,
        "available":      holdings_sources_available(isin, store),
        "pinned":         (override_get(isin, "holdings_source") or ""),
        "is_manual":      is_manual_upload,
        # Has the user hand-corrected rows in this source? Set by the
        # row-edit endpoint, and the reason a Yahoo slot stops being
        # refetched — so the tile can say why a refresh left it alone.
        "edited":         bool(blob.get("user_edited")),
        # Factsheet provenance — null for the other two sources. A
        # position table that the document itself called partial says so
        # here rather than being presented as the whole fund.
        "extracted_at":   blob.get("extracted_at") if holdings_source == "factsheet" else None,
        "complete":       blob.get("complete") if holdings_source == "factsheet" else None,
        "page":           blob.get("page") if holdings_source == "factsheet" else None,
        "row_count":      len(holdings_rows),
        "weight_sum_pct": weight_sum_pct,
        # Manual-upload provenance — null for Yahoo-sourced blobs.
        "uploaded_at":    blob.get("uploaded_at") if is_manual_upload else None,
        "filename":       blob.get("filename")    if is_manual_upload else None,
        # v0.22.1 — upload provenance: "disk" (local file) or "url"
        # (fetched from the internet), plus the URL / path it came from.
        # Null for Yahoo-sourced blobs, and for manual blobs cached
        # before 0.22.1 (which recorded only the filename).
        "source_kind":    blob.get("source_kind")  if is_manual_upload else None,
        "source_value":   blob.get("source_value") if is_manual_upload else None,
        # v0.22.0 — unified "when was this holdings list last written?".
        # ``last_updated`` is stamped by every write path (upload, Yahoo
        # fetch, row edit, enrichment). The ``uploaded_at`` / ``fetched_at``
        # fallbacks cover blobs cached before 0.22.0, which have only the
        # path-specific key. Null only when there are no holdings at all.
        "last_updated":   (blob.get("last_updated")
                           or blob.get("uploaded_at")
                           or blob.get("fetched_at")
                           or None),
        # Top-coverage sum (frontend holdings-header badge) + enrichment
        # decision. For a manual upload, enrichment is not a concept —
        # ``applied`` is False and ``reason`` empty.
        "top10_sum_pct":  top10_sum_pct,
        "enrichment":     dict(enrichment_meta or {}),
    }


def load_fund_data(isin: str, exchange: str | None, cache_cfg: dict,
                   force_refresh: bool = False,
                   known_ticker: str | None = None,
                   *,
                   commit: bool = True) -> dict:
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
        commit: v0.21.0 explicit-save model. When False, freshly fetched
            data is returned to the caller in memory but NOT persisted
            to ``cache/listings/`` or ``cache/funds/`` — so loading a
            new fund into the viewer doesn't silently save it. When True
            (default) writes happen as before. A refresh of a fund that
            already has cache entries always writes, regardless of this
            flag, because the user has already committed to it.

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
                                  lambda: extract_profile(ticker),
                                  force=force_refresh, commit=commit)
    sectors, smeta = get_category(yf_sym, isin, "sectors", cache_cfg,
                                  lambda: extract_sectors(ticker),
                                  force=force_refresh, commit=commit)
    asset_allocation, aameta = get_category(
        yf_sym, isin, "asset_allocation", cache_cfg,
        lambda: extract_asset_allocation(ticker),
        force=force_refresh, commit=commit)
    asset_class, ameta = get_category(yf_sym, isin, "asset_class", cache_cfg,
                                      lambda: detect_asset_class(ticker, profile or {}),
                                      force=force_refresh, commit=commit)

    # Per-fund asset-class override (the "Edit fund" dialog). The override
    # store is keyed by ticker and is NOT Yahoo-derived, so it survives a
    # force refresh — the line above may have just re-detected the class
    # live, and we deliberately override it again here. Applying it before
    # the holdings work below means enriched/top-10 holdings rows inherit
    # the overridden class via ``default_holding_asset_class`` too.
    from porxpy.utils import override_get               # local: avoid cycle
    # Lazy migration: pins to "yahoo" are not assertions and were never
    # meant to be stored. Clearing them here means a fund corrects itself
    # the first time it is read, rather than needing the user to find and
    # undo each one. See utils.purge_default_pins.
    from porxpy.utils import purge_default_pins            # local: cycle
    _purged = purge_default_pins(isin)
    if _purged:
        print(f"[Overrides] {isin}: dropped {len(_purged)} default "
              f"(yahoo) pin(s): {', '.join(sorted(_purged))}")

    ac_override = override_get(isin, "primary_asset_class")
    if asset_class is None:
        asset_class = {"class": "other", "confidence": "low",
                       "signals": [], "origin": "none"}
    asset_class["detected_class"] = asset_class.get("class")
    if ac_override:
        asset_class["class"]      = ac_override
        asset_class["overridden"] = True
        asset_class["confidence"] = "override"
        # The override replaces the evidence as well as the value: what
        # detection had inferred no longer explains what is shown.
        asset_class["origin"]     = "user"
    else:
        asset_class["overridden"] = False

    price_history, phmeta = get_price_history_cached(
        yf_sym, ticker, cache_cfg, force=force_refresh, commit=commit)

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
    from porxpy.utils import (coerce_holdings_row,   # local: avoid import cycle
                              holdings_get, holdings_put,
                              holdings_sources_available)

    blob = cache_read(isin, "holdings")
    holdings_entry = blob.get("holdings") or {}

    # v0.77.0 — the slot holds one blob PER SOURCE and the user picks
    # which is in effect. ``holdings_blob`` below is that active blob, so
    # everything downstream of here reads exactly as it did when the slot
    # held only one.
    holdings_blob, active_source, holdings_store = holdings_get(isin)

    cached_source = holdings_blob.get("source") or ""
    is_manual     = cached_source == "manual_upload"

    # Do we already have a cached *result* for this fund?
    #
    # This is keyed off the presence of a cached blob (``source`` is
    # stamped by every write path), NOT off the blob having rows.
    #
    # That distinction matters enormously. Yahoo publishes no top-10
    # holdings for most European UCITS ETFs — EIMI.L, VWRL.AS and
    # friends all come back empty. Their cached blob is therefore
    # ``{"source": "yahoo_top10", "rows": []}``. Keying ``have_cached``
    # off ``rows`` being non-empty meant such a fund could NEVER satisfy
    # the cache: every portfolio load re-hit Yahoo's holdings endpoint
    # for it, forever. With several such funds in a portfolio that's
    # several needless network round-trips on every single app start —
    # which is exactly the "why is this so slow every time" symptom.
    #
    # "Yahoo has nothing for this fund" is a perfectly good answer and
    # is worth caching like any other. The escape hatch is unchanged:
    # force_refresh (the ↻ Reload Fund Data button) still refetches, so
    # a fund that later gains holdings on Yahoo can be picked up on
    # demand.
    have_cached = "yahoo" in holdings_store

    # Decide whether to (re)fetch from Yahoo. We refetch when:
    #   * this fund has no Yahoo holdings slot at all, OR
    #   * force_refresh is set.
    #
    # v0.77.0 changed what this question means. It used to be "is the one
    # blob stale?", and a manual upload therefore SUPPRESSED the fetch —
    # necessarily, since a fetch would have overwritten the upload. Now
    # each source owns its own slot, so a refresh refills Yahoo's slot and
    # cannot touch the upload or the factsheet; the user's file is safe
    # because it lives somewhere else, not because we declined to ask
    # Yahoo. That is what makes "Issuer (Yahoo)" selectable again on a
    # fund that has been uploaded to.
    #
    # The one thing that still suppresses a refetch is a Yahoo slot the
    # user has hand-edited. Their corrections live in that slot and a
    # refetch would overwrite them, which is the same protection a
    # promoted-to-manual_upload blob used to get — see the row-edit
    # endpoint, which sets the flag.
    yahoo_edited = bool((holdings_store.get("yahoo") or {}).get("user_edited"))
    refetch = (not have_cached) or (force_refresh and not yahoo_edited)

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
        # The active blob's own stamp, not the slot's: the slot is
        # rewritten whenever ANY source is, so a fund whose Yahoo rows
        # were refreshed this morning would otherwise report a
        # two-year-old factsheet as fetched today.
        hold_age = age_days(holdings_blob.get("last_updated")
                            or holdings_blob.get("fetched_at")
                            or holdings_entry.get("fetched_at", ""))
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
        from porxpy.utils import default_holding_asset_class
        fund_holding_ac = default_holding_asset_class(
            (asset_class or {}).get("class"))
        if fund_holding_ac:
            for r in rows:
                if not (r.get("asset_class") or "").strip():
                    # The fund's own class, which is a claim about the
                    # fund and not about this position. It fills the
                    # level it states and no finer: deriving a sub class
                    # from it would put an instrument-level assertion on
                    # a row whose source said nothing at all.
                    # The raw column, because this IS the source for
                    # these rows: the fund's own classification stands in
                    # where the holding stated nothing of its own.
                    r["asset_raw"] = fund_holding_ac

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
            # v0.22.0 — canonical last-written stamp (see upload.py).
            "last_updated":  now_iso(),
            "enrichment":    dict(enrichment_meta),
        }
        # v0.21.0 explicit-save: only persist holdings when commit=True
        # OR a holdings cache already exists for this ISIN. Otherwise the
        # blob is returned in memory only (consistent with the listing).
        from porxpy.utils import cache_read as _cache_read
        if commit or bool(_cache_read(isin, "holdings")):
            meta = holdings_put(isin, holdings_blob, "yahoo",
                                store=holdings_store)
            hmeta = {
                "source":     "live",
                "fetched_at": meta["fetched_at"],
                "age_days":   meta["age_days"],
                "ttl_days":   None,
            }
        else:
            hmeta = {
                "source":     "live",
                "fetched_at": now_iso(),
                "age_days":   0.0,
                "ttl_days":   None,
                "committed":  False,
            }
        cached_source = blob_source
        is_manual     = False

        # A refresh refills Yahoo's slot; it does not change which source
        # the user is looking at. Re-ask the store, so a fund pinned to
        # (or falling back to) its upload keeps showing the upload after
        # a "Reload fund data" that happened to also refresh Yahoo.
        active_blob, active_source, holdings_store = holdings_get(isin)
        # An uncommitted fund persists nothing, so the store read back is
        # empty while the page is about to show Yahoo's rows. Put the
        # in-memory blob in so the tile's availability map describes what
        # is actually on screen rather than reporting no source at all.
        holdings_store.setdefault("yahoo", holdings_blob)
        if active_source and active_source != "yahoo":
            holdings_blob = active_blob
            cached_source = holdings_blob.get("source") or ""
            is_manual     = cached_source == "manual_upload"
            hmeta = {
                "source":     "cache",
                "fetched_at": holdings_blob.get("last_updated")
                              or holdings_blob.get("fetched_at"),
                "age_days":   age_days(holdings_blob.get("last_updated")
                                       or holdings_blob.get("fetched_at") or ""),
                "ttl_days":   None,
            }
        elif not active_source:
            # Nothing was persisted (an uncommitted fund): the in-memory
            # Yahoo blob is what the page shows.
            active_source = "yahoo"

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
    #   factsheet      → "factsheet"
    #   (no rows)      → "none"
    # The mapping itself lives in config.rollup_label_of, shared with the
    # row editor and the enrichment button.
    rollup_rows = holdings_rows
    breakdowns_source = rollup_label_of(holdings_source)
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
        factsheet_get, override_get, uploaded_breakdowns_get,
    )
    # Per-facet card source. One registry field per facet now, so this
    # rebuilds the {facet: source} map build_fund_breakdowns expects.
    bd_overrides    = {
        f: override_get(isin, f"breakdown_source.{f}")
        for f in BREAKDOWN_FACETS
        if override_get(isin, f"breakdown_source.{f}")
    }
    uploaded_facets = uploaded_breakdowns_get(isin)
    # Which sources this fund HAS, which is what decides whether the
    # selector offers them — distinct from whether they happen to carry
    # data for a given facet. An extracted factsheet that omits the
    # currency split still answers the currency card, with "unknown".
    sources_present = {
        "holdings":  bool(rollup_rows),
        "factsheet": bool((factsheet_get(isin) or {}).get("extraction")),
    }
    # Facets the user has asserted their source covers completely. Read
    # here rather than downstream because everything that consumes this
    # payload — the fund page, the portfolio rollup, the optimiser — must
    # see the same completed card, not each apply the assertion itself.
    bd_completed = {
        f: True for f in BREAKDOWN_FACETS
        if bool(override_get(isin, f"breakdown_complete.{f}"))
    }
    fund_breakdowns = build_fund_breakdowns(
        breakdowns, sectors or [], asset_allocation or [],
        bd_overrides, uploaded_facets, sources_present, bd_completed)

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
    from porxpy.utils import (overrides_for, apply_overrides,
                              normalise_fund_structure)
    fund_structure_seed, _fs_seed_origins = _seed_fund_structure(
        profile or {}, (asset_class or {}).get("class"))
    # The structure override is now one registry field per attribute
    # rather than one dense block. Collecting them back into a dict keeps
    # _merge_fund_structure's shape, and — because the store is sparse —
    # every key present here is a genuine assertion, which is what that
    # function always wanted and previously had to infer.
    _fs_override        = {f: e for f, e in overrides_for(isin).items()
                           if f in DEFAULT_FUND_STRUCTURE}
    fund_structure, fund_structure_sources = _merge_fund_structure(
        fund_structure_seed, _fs_override, _fs_seed_origins)
    fund_structure_is_override = any(v == "user"
                                     for v in fund_structure_sources.values())

    # primary_asset_class is presented alongside the structure fields and
    # is provenanced the same way, so its origin belongs in the same map
    # the tile and the Edit dialog already read. It was the one field in
    # the block with no entry, so fundFieldSourceNote fell through to the
    # pin label and captioned every classification "Yahoo" — including
    # the ones decided by words in the fund's name, which is a reading of
    # marketing copy presented as measured data.
    #
    # A user override wins here as it does for every other field: it
    # replaces the evidence as well as the value.
    _ac_origin = (asset_class or {}).get("origin") or ""
    if ac_override:
        fund_structure_sources["primary_asset_class"] = "user"
    elif _ac_origin and _ac_origin != "none":
        fund_structure_sources["primary_asset_class"] = _ac_origin

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
    holdings_meta = build_holdings_meta(
        isin, holdings_blob, active_source, holdings_store,
        enrichment_meta=enrichment_meta, top10_sum_pct=top10_sum_pct)
    is_manual_upload = holdings_meta["is_manual"]

    # v0.21.0 explicit-save: a fund is "saved" when its listing-level
    # profile cache file exists on disk. This is the truth-of-state for
    # the pre-loaded list (no separate flag — the file's presence IS
    # the marker). The frontend uses this to drive the Save button.
    from porxpy.utils import (listing_exists as _listing_exists,
                              override_get)
    saved = _listing_exists(yf_sym)
    # v0.27.0 — optimiser opt-out, ISIN-keyed. Surfaced on every fund
    # response so the fund page and the pre-loaded list can both show it.
    include_opt = bool(override_get(isin, "include_in_optimizer",
                                    DEFAULT_INCLUDE_IN_OPTIMIZER))

    payload = {
        "ticker":        yf_sym,
        "saved":         saved,
        "include_in_optimizer": include_opt,
        "resolved_mic":  resolved_mic,
        "resolution":    note,
        "profile":       profile or {},
        # ── Unified holdings (v0.5.0) ──────────────────────────────────
        # One row set, one superset schema, one source tag. Every row
        # carries a stable ``_row_id`` for the holdings editor.
        "holdings_rows":   holdings_rows,
        "holdings_source": holdings_source,   # manual_upload / yahoo_enriched / yahoo_top10 / none
        "holdings_meta":   holdings_meta,
        # What the v0.76.0 facet migration had to drop from this fund's
        # cache, or {} when nothing was. Carried on the payload because
        # the alternative is a screen that shows empty holdings and no
        # reason: the rows were stored before every facet kept the
        # source's own wording, so re-resolving them was impossible and
        # dropping them was the only honest option. Only the user can
        # close that gap, by re-importing, so only the user can be told.
        # Cleared by the next holdings or breakdown write — see
        # utils.legacy_purge_clear.
        "legacy_purge":    blob.get("_legacy_purge") or {},
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
        # country / currency), each {items, source, available} — see
        # build_fund_breakdowns. Per-card
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
        # Per-field provenance: {field: "user"|"yahoo"}. Lets the tile
        # caption each row for where its value actually came from.
        "fund_structure_sources":    fund_structure_sources,
        # Per-field provenance for EVERY field in FIELD_GROUPS, not just
        # the structure block. One map, read by both the fund tiles and
        # the Edit dialog, so the two cannot caption the same field
        # differently — they used to derive it separately and did.
        #
        # A source is RECORDED, never assumed. The fields endpoint used
        # to fall back to the literal "yahoo" for anything unpinned,
        # which captioned a name-inferred focus and a defaulted style box
        # as Yahoo data. Either a source supplied the value or there is
        # no value: a field absent from this map has nothing to caption.
        "field_sources": _field_provenance(
            profile or {}, asset_class, fund_structure,
            fund_structure_sources, overrides_for(isin), isin),
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

    # v0.33.0 — write every *targeted* override into the assembled payload.
    #
    # Deliberately last, and deliberately generic: TER, total net assets
    # and the structure fields all land here by walking the registry, so
    # adding an overridable field is a registry entry rather than another
    # bespoke application site. Fields without a target path
    # (breakdown sources, include_in_optimizer) were already consumed
    # above by the code that actually needs them.
    #
    # Note this runs on the assembled response, not on the cached blob:
    # the override is a view over Yahoo's data, never a mutation of it,
    # so clearing the override restores Yahoo's value with no refetch.
    # override_seed carries the derived value each override displaced, so
    # the Edit dialog can show what Yahoo said alongside the user's number
    # and can restore it on revert without a refetch.
    payload["override_sources"], payload["override_seed"] = \
        apply_overrides(isin, payload)
    return payload
