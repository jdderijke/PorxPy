"""
Infrastructure helpers shared across the rest of the package.

Bundles several small concerns that are tightly related but don't each
warrant their own module:

* generic JSON-safe value coercion (``safe``, ``df_cell``)
* timestamp helpers (``now_iso``, ``age_days``)
* on-disk ticker cache (``cache_read/write/get/put/purge``,
  ``holdings_status_from_cache``)
* FX conversion (``normalise_currency``, ``fx_rate``, ``fx_history``,
  ``price_in_base``)
* persistent ISIN→ticker map for OpenFIGI rate-limit relief
* portfolio JSON store (``load_portfolios``, ``upsert_portfolio``, ...)

The holdings/portfolio breakdown rollups (``rollup_holdings``,
``canonicalise_facet_key``) now live in :mod:`porxpy.breakdowns` and are
re-exported from here for backwards compatibility.

The functions in this module never reach out to Yahoo or OpenFIGI directly
beyond FX rate lookups (which use yfinance). They're all either pure or
hit the local disk / cache.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from porxpy.config import (
    CACHE_CATEGORIES,
    CACHE_DIR,
    LISTINGS_DIR,
    FUNDS_DIR,
    LISTING_CATEGORIES,
    FUND_CATEGORIES,
    ASSET_CLASSES,
    BREAKDOWN_FACETS,
    BREAKDOWN_SOURCES,
    DEFAULT_CACHE_CONFIG,
    DEFAULT_FUND_STRUCTURE,
    DEFAULT_SETTINGS,
    DISTRIBUTION_POLICIES,
    ENRICHABLE_FIELDS,
    FUND_STRUCTURES,
    FUND_STYLES,
    FX_HIST_TTL_HOURS,
    FX_TTL_HOURS,
    HOLDINGS_MATCH_KEYS,
    ISIN_MAP_FP,
    ISIN_MAP_TTL_DAYS,
    OVERRIDES_FP,
    REPLICATION_METHODS,
    PENCE_CURRENCIES,
    PORTFOLIOS_FP,
    SETTINGS_FP,
    SYMBOL_INFO_CACHE_NAME,
    SYMBOL_ALIAS_CACHE_NAME,
    SYMBOL_INFO_TTL_DAYS,
)


# ---------------------------------------------------------------------------
# Generic value helpers
# ---------------------------------------------------------------------------
def safe(val: Any) -> Any:
    """Coerce a value to something JSON-serialisable.

    ``NaN``, ``±inf``, and pandas NA all collapse to ``None``. ``Timestamp``
    and ``datetime`` are returned as ISO strings. NumPy scalars are
    unwrapped via ``.item()``. Everything else is returned unchanged.

    Args:
        val: Any Python / NumPy / pandas value.

    Returns:
        A JSON-friendly equivalent of ``val``, or ``None`` if the value is
        non-finite / NA.
    """
    if val is None:
        return None
    try:
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        if pd.isna(val):
            return None
    except Exception:
        # ``pd.isna`` complains on some custom types; treat them as fine.
        pass
    if isinstance(val, (pd.Timestamp, datetime)):
        return val.isoformat()
    if hasattr(val, "item"):        # numpy scalar → Python native
        return val.item()
    return val


def df_cell(df: pd.DataFrame, row_substr: str, col_index: int = 0) -> Any:
    """Look up a cell from a DataFrame by partial row-index match.

    Used to read named rows (e.g. "Annual Report Expense Ratio") from the
    DataFrame Yahoo returns from ``funds_data.fund_operations`` without
    being defensive about the exact index label.

    Args:
        df: Source DataFrame.
        row_substr: Case-insensitive substring to match against the row
            index labels. The first match wins.
        col_index: Positional column to read from. Defaults to 0 (the
            fund's own value column).

    Returns:
        The matched cell run through :func:`safe`, or ``None`` if no row
        matched or the lookup raised.
    """
    try:
        for idx in df.index:
            if row_substr.lower() in str(idx).lower():
                val = df.iloc[df.index.get_loc(idx), col_index]
                return safe(val)
    except Exception:
        pass
    return None


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def age_days(iso_ts: str) -> float | None:
    """Return the age of an ISO timestamp in days.

    Args:
        iso_ts: ISO 8601 timestamp produced by :func:`now_iso` (or compatible).

    Returns:
        Floating-point days since ``iso_ts``, or ``None`` if the string
        couldn't be parsed.
    """
    try:
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-ticker disk cache (one JSON file per ticker, all categories inside)
# ---------------------------------------------------------------------------
CACHE_DIR.mkdir(exist_ok=True)


def _cache_path_for(key: str, category: str) -> Path:
    """Resolve the on-disk path for a cache entry.

    The cache splits by category along the listing-vs-fund axis (see
    :data:`LISTING_CATEGORIES` / :data:`FUND_CATEGORIES`):

    * Listing-level categories (price_history, profile, upload_prefs)
      live at ``cache/listings/<ticker>.json``. ``key`` is the ticker.
    * Fund-level categories (holdings, sectors, asset_class,
      asset_allocation) live at ``cache/funds/<isin>.json``. ``key``
      is the ISIN.
    * Anything else (FX pair series, symbol-info pseudo-tickers and
      symbol-alias maps used internally) lives at the cache root —
      these are not per-fund and were never part of the listing/fund
      split. They keep the previous layout for compatibility.

    Args:
        key: Ticker or ISIN (or a pseudo-key like ``"FX_EURUSD"``).
        category: One of :data:`CACHE_CATEGORIES`, or any other string
            for the legacy "shared" cache files (FX, symbol_info, etc).

    Returns:
        Absolute path to the JSON file. The parent directory is
        guaranteed to exist (created lazily here).
    """
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
    if category in FUND_CATEGORIES:
        FUNDS_DIR.mkdir(parents=True, exist_ok=True)
        return FUNDS_DIR / f"{safe}.json"
    if category in LISTING_CATEGORIES:
        LISTINGS_DIR.mkdir(parents=True, exist_ok=True)
        return LISTINGS_DIR / f"{safe}.json"
    # Shared / pseudo-ticker caches (FX, symbol_info, symbol_aliases) —
    # not per-fund, kept at the cache root as before.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{safe}.json"


# Bump this whenever _maybe_migrate_facets itself changes in a way
# that requires re-running on already-stamped blobs. The stamp on
# disk records the generation it was written under; a mismatch
# forces a re-migration regardless of the resource-CSV versions.
# History:
#   1 — v0.15.0 initial migrator (had a cache-shape unwrap bug;
#       stamps were written but rows never actually walked).
#   2 — v0.15.3 fixed the unwrap; bumped to invalidate every gen-1
#       stamp so the corrected migrator runs once per blob.
_FACET_MIGRATOR_GENERATION = 2


def _maybe_migrate_facets(fp, data: dict) -> bool:
    """Lazily re-normalise facet fields in a cache blob if its stamp
    is stale relative to the current resource-CSV versions or the
    migrator generation.

    Returns:
        True if the blob was migrated (and the caller should persist
        it). False if the blob was already up-to-date.

    Side effect:
        Mutates ``data`` in place. Adds / updates the
        ``_normalisation`` stamp on every category dict it touches.
    """
    from porxpy.resources import RESOURCE_VERSIONS
    if not isinstance(data, dict) or not data:
        return False
    current_versions = dict(RESOURCE_VERSIONS)
    stamp = data.get("_normalisation") or {}
    # ``generation`` is bumped whenever the migrator itself changes
    # (e.g. v0.15.3 fixed the cache-shape unwrap — earlier stamps
    # claim "normalised" but walked the wrong layer and touched
    # nothing, so we ignore them by mismatching the generation).
    # current_versions matching is necessary but not sufficient.
    if (stamp.get("versions") == current_versions
            and stamp.get("generation") == _FACET_MIGRATOR_GENERATION
            and stamp.get("is_normalized")):
        return False     # already up-to-date

    # Walk every plausibly-facet-bearing structure in the blob and
    # run normalise_facets on it. The on-disk shape wraps every cache
    # category in ``{fetched_at, value: <payload>}`` (see cache_put),
    # so for ``holdings`` the rows live at
    # ``data["holdings"]["value"]["rows"]`` and for ``profile`` the
    # currency lives at ``data["profile"]["value"]["currency"]`` —
    # the .get("value") unwrap is mandatory. Without it the loops
    # silently iterate empty containers, ``touched`` stays False,
    # and the stamp at the end still marks the blob as up-to-date,
    # so the migration never retries. (This was the v0.15.0 bug.)
    touched = False

    def _unwrap(category: str) -> Any:
        """Return the inner ``value`` of a cache category, or {}."""
        entry = data.get(category)
        if not isinstance(entry, dict):
            return {}
        val = entry.get("value")
        return val if isinstance(val, dict) or isinstance(val, list) else {}

    # Holdings rows (every row carries the five resource-backed facet
    # fields; normalise_facets resolves each and stamps
    # _unmatched_facets for any miss).
    hold_val = _unwrap("holdings")
    if isinstance(hold_val, dict):
        rows = hold_val.get("rows") or []
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict):
                    normalise_facets(r)
                    touched = True

    # Fund profile (currency only — other profile fields aren't in
    # the resource taxonomy).
    prof = _unwrap("profile")
    if isinstance(prof, dict):
        cur = (prof.get("currency") or "").strip()
        if cur:
            from porxpy.resources import resolve_currency
            resolved = resolve_currency(cur)
            if resolved and resolved != cur:
                prof["currency"] = resolved
                # Migration just fixed it — drop the stale unmatched flag.
                prof.pop("_currency_unmatched", None)
                touched = True
            elif not resolved and not prof.get("_currency_unmatched"):
                prof["_currency_unmatched"] = True
                touched = True
            elif resolved and prof.get("_currency_unmatched"):
                # Already canonical but stale flag — clear it.
                prof.pop("_currency_unmatched", None)
                touched = True

    # Yahoo's sector weightings list (cached as the ``sectors``
    # category). Each item is ``{"sector": <key>, "weight": ...,
    # "_unmatched"?: True}``.
    from porxpy.resources import resolve_sector, country_to_mstar
    sectors_val = _unwrap("sectors")
    if isinstance(sectors_val, list):
        for it in sectors_val:
            if not isinstance(it, dict):
                continue
            k = (it.get("sector") or "").strip()
            if not k:
                continue
            resolved = resolve_sector(k)
            if resolved and resolved != k:
                it["sector"] = resolved
                it.pop("_unmatched", None)
                touched = True
            elif resolved and it.get("_unmatched"):
                it.pop("_unmatched", None)
                touched = True
            elif not resolved and not it.get("_unmatched"):
                it["_unmatched"] = True
                touched = True

    # Yahoo's asset_allocation list — items use ``class`` (already
    # normalised through normalize_holding_asset_class at extraction
    # time, but a no-op re-run keeps the stamp honest if the
    # vocabulary ever changes).
    aa_val = _unwrap("asset_allocation")
    if isinstance(aa_val, list):
        for it in aa_val:
            if not isinstance(it, dict):
                continue
            k = (it.get("class") or "").strip()
            if not k:
                continue
            resolved = normalize_holding_asset_class(k)
            if resolved and resolved != k:
                it["class"] = resolved
                touched = True

    # Fund-level breakdown lists (asset_class / sector / country /
    # currency cards built by build_fund_breakdowns). build_fund_
    # breakdowns runs at request time and is not itself a cached
    # category — but if a future version persists it, this loop is
    # already in the correct shape (unwrapped, per-facet items).
    fb_val = _unwrap("fund_breakdowns")
    if isinstance(fb_val, dict):
        from porxpy.resources import resolve_currency
        for facet, payload in fb_val.items():
            items = (payload or {}).get("items") or []
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                k = (it.get("key") or "").strip()
                if not k:
                    continue
                if facet == "sector":
                    resolved = resolve_sector(k)
                elif facet == "currency":
                    resolved = resolve_currency(k)
                elif facet == "country":
                    resolved = country_to_mstar(k)
                else:
                    continue
                if resolved and resolved != k:
                    it["key"] = resolved
                    it.pop("_unmatched", None)
                    touched = True
                elif resolved and it.get("_unmatched"):
                    it.pop("_unmatched", None)
                    touched = True
                elif not resolved and not it.get("_unmatched"):
                    it["_unmatched"] = True
                    touched = True

    # Always update the stamp — even when nothing changed, this
    # records that we checked, so subsequent reads short-circuit.
    from datetime import datetime, timezone
    data["_normalisation"] = {
        "is_normalized": True,
        "versions":      current_versions,
        "generation":    _FACET_MIGRATOR_GENERATION,
        "normalised_at": datetime.now(timezone.utc).isoformat(),
    }
    return True


def listing_exists(ticker: str) -> bool:
    """Return True if a listing-level cache file exists for ``ticker``.

    Used by the v0.21.0 explicit-save model: a listing existing in
    ``cache/listings/`` IS the marker that the fund has been saved to
    the pre-loaded list. There is no separate flag — the file's
    presence is the truth.

    Args:
        ticker: Yahoo ticker symbol (case-insensitive).

    Returns:
        ``True`` if ``cache/listings/<ticker>.json`` exists, else False.
    """
    if not ticker:
        return False
    fp = _cache_path_for(ticker, "profile")
    return fp.exists()


def cache_read(key: str, category: str) -> dict:
    """Read the on-disk cache blob for ``(key, category)``.

    Args:
        key: Ticker (listing-level categories), ISIN (fund-level
            categories), or a pseudo-key (shared categories).
        category: One of :data:`CACHE_CATEGORIES`, or any other string
            for shared caches. Determines which directory is read.

    Returns:
        The deserialised cache dict, or an empty dict if the file is
        missing or malformed. v0.15.0: if the blob's facet-
        normalisation stamp is stale relative to the current resource
        CSV versions, the blob is re-normalised lazily AND persisted
        back so subsequent reads are cheap. Migration is silent on
        success; on failure the read still returns the (possibly
        stale) blob — the user keeps working, the next save catches it.
    """
    fp = _cache_path_for(key, category)
    if not fp.exists():
        return {}
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    # Lazy facet migration (v0.15.0).
    try:
        if _maybe_migrate_facets(fp, data):
            # Persist the migrated blob so we don't redo the work on
            # next read. A persist failure here is non-fatal — the
            # caller's read sees the in-memory migrated copy
            # regardless.
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except Exception as exc:
                print(f"[Cache] facet migration persist failed "
                      f"({key}/{category}): {exc}")
    except Exception as exc:
        print(f"[Cache] facet migration error ({key}/{category}): {exc}")

    return data


def cache_write(key: str, category: str, data: dict) -> None:
    """Persist a cache blob to disk.

    Args:
        key: See :func:`cache_read`.
        category: See :func:`cache_read`.
        data: The full cache blob (all categories at this key) to write.
            The blob is overwritten in place — callers should
            read-modify-write.
    """
    fp = _cache_path_for(key, category)
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as exc:
        print(f"[Cache] write error for {key}/{category}: {exc}")


def cache_get(key: str, category: str, cache_cfg: dict
              ) -> tuple[Any, dict | None]:
    """Read a single cache category, honouring TTL.

    See :func:`cache_read` for which file is read. The signature is
    unchanged from pre-0.12 but the first argument is no longer just a
    "ticker" — it's whichever key is right for ``category`` (ticker for
    listing categories, ISIN for fund categories).
    """
    cfg = cache_cfg.get(category, {})
    if not cfg.get("enabled"):
        return None, None
    blob = cache_read(key, category)
    entry = blob.get(category)
    if not entry:
        return None, None
    age = age_days(entry.get("fetched_at", ""))

    # Manual-refresh-only categories (e.g. holdings): a present entry is
    # always fresh. age may still be None for a malformed timestamp —
    # that's fine, we return the value regardless and let age_days' None
    # flow through to meta.
    if cfg.get("manual_refresh_only"):
        return entry.get("value"), {
            "fetched_at": entry.get("fetched_at"),
            "age_days":   round(age, 3) if age is not None else None,
            "ttl_days":   None,
        }

    ttl = cfg.get("ttl_days", 0)
    if age is None or age > ttl:
        return None, None
    return entry.get("value"), {
        "fetched_at": entry.get("fetched_at"),
        "age_days":   round(age, 3),
        "ttl_days":   ttl,
    }


def cache_put(key: str, category: str, value: Any) -> dict:
    """Write a single cache category with the current timestamp.

    Args:
        key: See :func:`cache_read`.
        category: See :func:`cache_read`.
        value: JSON-serialisable payload to store.

    Returns:
        ``{"fetched_at": <iso_now>, "age_days": 0.0}``.
    """
    blob = cache_read(key, category)
    blob[category] = {"fetched_at": now_iso(), "value": value}
    cache_write(key, category, blob)
    return {"fetched_at": blob[category]["fetched_at"], "age_days": 0.0}


# ---------------------------------------------------------------------------
# Identity block — the {isin, ticker, exchange, currency} quad stored
# in each listings cache file alongside the TTL'd Yahoo categories. It
# is not itself a category (no TTL, no {fetched_at, value} envelope) —
# the fetch route writes it once, after resolution, so the listing
# always knows which ISIN's funds-cache file to consult and the cache
# list can show identity columns without portfolio lookups.
# ---------------------------------------------------------------------------
def listing_identity_get(ticker: str) -> dict:
    """Return the identity block for ``ticker``, or ``{}`` if absent.

    A returned dict — when present — carries ``{isin, ticker, exchange,
    currency, resolved_at}``. Empty dict means "no identity recorded
    yet" (a pre-resolution cache, or a fund cached before 0.11.2).
    """
    blob = cache_read(ticker, "profile")   # any listing category gets the file
    ident = blob.get("identity")
    return dict(ident) if isinstance(ident, dict) else {}


def listing_identity_put(ticker: str, identity: dict) -> None:
    """Write the identity block for ``ticker`` into its listings cache file.

    Read-modify-write of one top-level key, leaving every category
    entry in the same file untouched. The identity is fresh-as-of-now
    by virtue of being persisted here; ``resolved_at`` is stamped.
    """
    if not ticker or not isinstance(identity, dict):
        return
    blob = cache_read(ticker, "profile")
    blob["identity"] = {**identity, "resolved_at": now_iso()}
    cache_write(ticker, "profile", blob)


def listing_identity_lookup_isin(ticker: str) -> str:
    """Convenience: ISIN linked to ``ticker``, or ``""`` if not resolved.

    Used by any handler that has a ticker (URL path) but needs the
    fund-level cache file (ISIN-keyed). When this returns ``""`` the
    fund has never been fully fetched and the handler should treat the
    request as a 404 — there is no funds-cache file to operate on.
    """
    return listing_identity_get(ticker).get("isin", "") or ""


def cache_purge(key: str | None = None, category: str | None = None) -> int:
    """Remove cached data, optionally scoped to a key and/or category.

    Args:
        key: If given, restrict purging to this key's cache file(s).
            For listing-level categories that means a ticker; for
            fund-level, an ISIN. If ``None``, every file under the
            cache directories is considered.
        category: If given, only remove this category from the affected
            blob (file is rewritten without it). If ``None``, the
            matching cache file(s) are deleted entirely.

    Returns:
        Number of entries removed (categories cleared, or files deleted).
    """
    removed = 0
    if key and category:
        blob = cache_read(key, category)
        if category in blob:
            del blob[category]
            cache_write(key, category, blob)
            removed += 1
        return removed

    if key:
        # No category given — delete every file matching this key in
        # either sub-directory. A ticker can collide with an ISIN
        # textually only by coincidence; deleting both is fine since
        # callers passing a bare ``key`` mean "drop everything for it".
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
        for d in (LISTINGS_DIR, FUNDS_DIR, CACHE_DIR):
            fp = d / f"{safe}.json"
            if fp.exists():
                fp.unlink()
                removed += 1
        return removed

    # No key given — scope the wipe by category if provided, else
    # remove every per-fund cache file.
    if category:
        dirs = ([FUNDS_DIR] if category in FUND_CATEGORIES
                else [LISTINGS_DIR] if category in LISTING_CATEGORIES
                else [CACHE_DIR])
        for d in dirs:
            if not d.exists():
                continue
            for fp in d.iterdir():
                if not fp.is_file() or fp.suffix != ".json":
                    continue
                blob = cache_read(fp.stem, category)
                if category in blob:
                    del blob[category]
                    cache_write(fp.stem, category, blob)
                    removed += 1
        return removed

    for d in (LISTINGS_DIR, FUNDS_DIR):
        if not d.exists():
            continue
        for fp in d.iterdir():
            if fp.is_file() and fp.suffix == ".json":
                fp.unlink()
                removed += 1
    return removed


def holdings_status_from_cache(isin: str) -> dict:
    """Report holdings-status flags for an ISIN, from cache only.

    Pure cache lookup — no network. Used to decorate fund-list rows
    (Pre-Loaded, Portfolio) with a status badge so the user sees at a
    glance which funds have a full upload, which have been enriched,
    and which fall back to the plain Yahoo top-10.

    Holdings live in the funds cache (ISIN-keyed) under the new split,
    so the caller passes an ISIN — the ticker→ISIN resolution is the
    caller's job (typically via :func:`listing_identity_lookup_isin`).
    An empty ISIN returns an empty status.

    Since v0.5.0 there is a single unified ``holdings`` cache slot. Its
    ``source`` field records which degree of completeness the cached
    rows represent:

        * ``"manual_upload"``  — full per-position list from a user file
        * ``"yahoo_enriched"`` — Yahoo top-10 enriched via per-symbol
          lookups (country / currency / sector / asset class filled)
        * ``"yahoo_top10"``    — raw Yahoo top-10, sparse rows

    There is no separate "would_enrich" decision to recompute here any
    more: enrichment is applied at fetch time and baked into ``source``.
    The field is still returned (always ``False``) for backwards
    compatibility with any caller / badge that referenced it.

    Args:
        isin: Fund ISIN — the funds-cache key.

    Returns:
        ``{
            "has_full": bool,         # source == "manual_upload"
            "full_count": int,        # row count when has_full else 0
            "source": str,            # the blob's source, or ""
            "top_count": int,         # cached holdings row count
            "top_sum_pct": float|None,# sum of cached row weights, or None
            "would_enrich": bool,     # always False since v0.5.0
        }``.
    """
    if not isin:
        return {"has_full": False, "full_count": 0, "source": "",
                "top_count": 0, "top_sum_pct": None, "would_enrich": False}
    blob = cache_read(isin, "holdings")

    hold_blob = (blob.get("holdings") or {}).get("value") or {}
    if not isinstance(hold_blob, dict):
        hold_blob = {}

    rows   = hold_blob.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    source = hold_blob.get("source") or ""
    row_count = len(rows)

    has_full   = source == "manual_upload"
    full_count = row_count if has_full else 0

    # Sum of weights across the cached rows. weight_pct is always in
    # percent in the unified schema, so a plain sum is correct.
    sum_pct: float | None = None
    if row_count > 0:
        s = 0.0
        for r in rows:
            try:
                w = float(r.get("weight_pct") or 0.0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                s += w
        sum_pct = round(s, 4)

    return {
        "has_full":     has_full,
        "full_count":   full_count,
        "source":       source,
        "top_count":    row_count,
        "top_sum_pct":  sum_pct,
        "would_enrich": False,
    }


# ---------------------------------------------------------------------------
# Unified holdings row schema (v0.5.0)
# ---------------------------------------------------------------------------
# As of v0.5.0 there is ONE holdings cache slot and ONE row shape, used
# by every source (raw Yahoo top-10, enriched Yahoo top-10, full user
# upload). Sparse sources simply leave the fields they don't know blank.
# Every row carries a stable ``_row_id`` so the holdings editor can
# address a single position for in-place patching.
#
# HOLDINGS_ROW_FIELDS is the canonical column order for the row dict
# (``_row_id`` excluded — it's metadata, not a column). new_row_id()
# mints the identifier; coerce_holdings_row() takes any partial dict and
# returns a complete, type-clean superset row.
#
# Field type notes (handled inside coerce_holdings_row):
#   weight_pct, duration, coupon  → float (blank → 0.0)
#   maturity, effective_date      → DD/mmm/YYYY date strings; stored as
#                                   strings so a blank stays blank and an
#                                   unparseable issuer value isn't silently
#                                   dropped. Coerce attempts to normalise
#                                   common spellings to DD/mmm/YYYY but
#                                   passes unrecognised text through.
#   everything else               → trimmed string
#
# Bond-only fields (duration, maturity, coupon, effective_date) stay
# blank for equity / cash holdings — that's their normal state. The
# columns are always present in the schema for shape uniformity; the
# holdings table can show or hide them via the "Show bond columns" toggle.
HOLDINGS_ROW_FIELDS: tuple[str, ...] = (
    "name", "ticker", "isin", "cusip", "sector", "asset_class", "sub_class",
    "country", "currency", "weight_pct",
    "duration", "maturity", "coupon", "effective_date",
)

# Subset of HOLDINGS_ROW_FIELDS that store numeric values. Used by
# coerce_holdings_row to apply float-coercion (blank → 0.0) and by the
# patch endpoint to validate edits as numbers.
HOLDINGS_NUMERIC_FIELDS: tuple[str, ...] = ("weight_pct", "duration", "coupon")

# Subset of HOLDINGS_ROW_FIELDS that store dates as DD/mmm/YYYY strings.
HOLDINGS_DATE_FIELDS: tuple[str, ...] = ("maturity", "effective_date")


# ---------------------------------------------------------------------------
# Holding asset class / sub class (v0.6.0)
# ---------------------------------------------------------------------------
# A *holding's* asset class is a small, lowercase, fixed enum — distinct
# from a *fund's* asset class (the wider config.ASSET_CLASSES vocabulary).
# A holding is one of: equity / bond / cash / other.
#
# Sub class is free text — the upload column maps straight through, or it
# falls back to a per-asset-class default. All values are stored
# lowercase (storage, table display, and the editor all use the raw
# lowercase form — no Title-Case formatting layer).
HOLDING_ASSET_CLASSES: tuple[str, ...] = ("equity", "bond", "cash", "other")

# How a *fund's* asset class (config.ASSET_CLASSES) maps to a *holding's*
# asset class when a holding has no asset class of its own and we fall
# back to the fund's. Anything not listed here → "other".
_FUND_TO_HOLDING_ASSET_CLASS: dict[str, str] = {
    "equity":       "equity",
    "fixed_income": "bond",
    "cash":         "cash",
    # mixed / commodity / other → "other" (via the .get default below)
}

# Recognised spellings for a holding's asset class → canonical lowercase
# key. Covers Yahoo's quoteType-derived labels (which may arrive as
# "Equity", "Fixed Income", lowercase, etc.) and the fund-level keys, so
# whatever a holdings source hands us collapses to the 4-value enum.
_HOLDING_ASSET_CLASS_ALIASES: dict[str, str] = {
    "equity":        "equity",
    "equities":      "equity",
    "stock":         "equity",
    "stocks":        "equity",
    "share":         "equity",
    "shares":        "equity",
    "bond":          "bond",
    "bonds":         "bond",
    "fixed income":  "bond",
    "fixed_income":  "bond",
    "fixedincome":   "bond",
    "fixed-income":  "bond",
    "cash":          "cash",
    "cash & equivalents": "cash",
    "cash and equivalents": "cash",
    "money market":  "cash",
    "other":         "other",
}

# Per-asset-class default sub class (used when the upload file has no
# sub-class column and no per-row value). All lowercase.
_DEFAULT_SUB_CLASS: dict[str, str] = {
    "equity": "shares",
    "bond":   "corporate bond",
    "cash":   "free spendable cash",
    "other":  "undefined",
}


def normalize_holding_asset_class(raw: str | None) -> str:
    """Collapse any asset-class spelling to the holding enum, or ``""``.

    Maps Yahoo's ``quoteType``-derived labels ("Equity", "Fixed Income",
    …), the fund-level keys ("fixed_income" → "bond"), and already-correct
    lowercase values onto one of :data:`HOLDING_ASSET_CLASSES`.

    Args:
        raw: Any asset-class string, any casing, or ``None``/blank.

    Returns:
        One of ``"equity"`` / ``"bond"`` / ``"cash"`` / ``"other"``, or
        ``""`` when ``raw`` is blank/``None`` (so callers can tell
        "no value" apart from a real classification and decide whether
        to fall back to the fund's asset class).
    """
    if raw is None:
        return ""
    key = str(raw).strip().lower()
    if not key:
        return ""
    return _HOLDING_ASSET_CLASS_ALIASES.get(key, "other")


def default_holding_asset_class(fund_asset_class: str | None) -> str:
    """Derive a holding's asset class from its fund's asset class.

    Used as the fallback when a holding row has no asset class of its
    own: ``equity`` → ``equity``, ``fixed_income`` → ``bond``, ``cash``
    → ``cash``, and everything else (``mixed`` / ``commodity`` /
    ``other``) → ``other``.

    Args:
        fund_asset_class: The fund's asset class — a
            :data:`~porxpy.config.ASSET_CLASSES` key — or ``None``/blank
            when the fund's asset class isn't known (e.g. the fund has
            never been loaded, so its ``asset_class`` cache slot is
            empty).

    Returns:
        A holding asset-class key, or ``""`` when ``fund_asset_class`` is
        blank/``None`` — in which case the caller leaves the holding's
        asset class (and, downstream, its sub class) blank too.
    """
    if fund_asset_class is None:
        return ""
    key = str(fund_asset_class).strip().lower()
    if not key:
        return ""
    return _FUND_TO_HOLDING_ASSET_CLASS.get(key, "other")


def default_sub_class(holding_asset_class: str | None) -> str:
    """Return the default sub class for a holding asset class.

    ``equity`` → ``"shares"``, ``bond`` → ``"corporate bond"``, ``cash``
    → ``"free spendable cash"``, ``other`` → ``"undefined"``.

    Args:
        holding_asset_class: A holding asset-class key (one of
            :data:`HOLDING_ASSET_CLASSES`), or ``None``/blank.

    Returns:
        The lowercase default sub class, or ``""`` when
        ``holding_asset_class`` is blank/``None`` — a holding with no
        asset class gets no defaulted sub class either; the two are a
        pair.
    """
    if holding_asset_class is None:
        return ""
    key = str(holding_asset_class).strip().lower()
    if not key:
        return ""
    return _DEFAULT_SUB_CLASS.get(key, "undefined")


def new_row_id() -> str:
    """Return a fresh, short, opaque holdings-row identifier.

    12 hex chars from a uuid4 — collision-safe within a single fund's
    holdings list (and far beyond) while staying short enough to sit in
    a URL path for the ``PATCH .../holdings/<row_id>`` editor endpoint.
    """
    return uuid.uuid4().hex[:12]


def normalise_bond_date(raw: Any) -> str:
    """Best-effort coercion of a bond date to the DD/mmm/YYYY display form.

    Bond-issuer date columns arrive in many spellings:

    * ISO 8601 — ``"2030-09-15"`` / ``"2030/09/15"``
    * European — ``"15/09/2030"`` / ``"15.09.2030"`` / ``"15-09-2030"``
    * US       — ``"09/15/2030"`` (only unambiguous when the day > 12)
    * Mixed    — ``"15 Sep 2030"`` / ``"15-Sep-2030"`` / ``"Sep 15 2030"``
    * Datetime stamps from pandas / openpyxl with a time component

    We normalise the recognisable shapes to ``"DD/mmm/YYYY"`` (the form
    the upload column-mapping dialog and the holdings editor both
    expose). Unrecognised input is stripped and returned verbatim — a
    bad value never gets silently dropped, just shown as-is so the user
    can spot it in the table and fix it manually.

    Empty/None → ``""``.

    Args:
        raw: Any value from a holdings row's ``maturity`` /
            ``effective_date`` slot.

    Returns:
        ``"DD/mmm/YYYY"`` on success, the trimmed input on failure, or
        ``""`` for blank/None.
    """
    if raw is None:
        return ""
    # pandas Timestamp / datetime: format directly.
    if isinstance(raw, datetime):
        return raw.strftime("%d/%b/%Y")
    # NumPy / pandas date types expose .isoformat() reliably enough.
    s = str(raw).strip()
    if not s:
        return ""

    # Strip a time component if present ("2030-09-15 00:00:00" /
    # "2030-09-15T00:00:00"). We only strip a space-separated tail
    # when it looks like a time (HH:MM...) — otherwise we'd clobber
    # legitimate space-separated date formats like "15 Sep 2030".
    s_date = s.split("T", 1)[0]
    sp_idx = s_date.find(" ")
    if sp_idx != -1:
        tail = s_date[sp_idx + 1:]
        # HH:MM... → strip; otherwise the space is part of the date.
        if re.match(r"^\d{1,2}:\d{2}", tail):
            s_date = s_date[:sp_idx]

    # Order matters — ISO formats are unambiguous and tried first.
    formats = (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%d/%b/%Y", "%d-%b-%Y", "%d %b %Y",
        "%d/%B/%Y", "%d-%B-%Y", "%d %B %Y",
        "%b %d %Y", "%b %d, %Y", "%B %d %Y", "%B %d, %Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(s_date, fmt).strftime("%d/%b/%Y")
        except ValueError:
            continue

    # US m/d/y only when the day part > 12 (otherwise ambiguous with
    # European d/m/y and we'd risk silently swapping). 09/15/2030 → ok;
    # 09/05/2030 stays as whatever d/m/y returned, or passes through.
    try:
        parts = s_date.replace("-", "/").replace(".", "/").split("/")
        if len(parts) == 3 and len(parts[2]) == 4:
            a, b, c = int(parts[0]), int(parts[1]), int(parts[2])
            if a <= 12 and b > 12:
                return datetime(c, a, b).strftime("%d/%b/%Y")
    except (ValueError, TypeError):
        pass

    # Unrecognised — return the trimmed input verbatim.
    return s


# ---------------------------------------------------------------------------
# normalise_facets — single chokepoint for sector / sub_class /
# currency / country / asset_class normalisation (v0.15.0)
# ---------------------------------------------------------------------------
# Every value of these five facets that enters PorxPy — whether from
# Yahoo, a user upload, a manual edit, or a legacy cache file — must
# pass through this function before it gets written to disk. The
# function:
#
#   * Attempts to resolve each facet against its resource CSV
#     (sectors.csv, currencies.csv, Holdings_class_definitions.csv,
#     country_codes.csv).
#   * On a match, writes the canonical value to the row.
#   * On a miss, leaves the RAW value in place (so information isn't
#     lost while the user hasn't resolved unmatched batches yet) AND
#     appends the field name to a ``_unmatched_facets`` list on the
#     row. Downstream code uses that list to surface the unmatched
#     value to the user via the resolution dialog.
#   * For sub_class: a value that resolves but whose canonical
#     doesn't belong to the row's asset_class group is treated as
#     unmatched too (e.g. "shares" with asset_class="cash").
#
# The function is idempotent: calling it on an already-normalised
# row is a no-op (every facet resolves to itself, no _unmatched_facets
# gets added). This matters because cache reads call it eagerly to
# stamp the file with the current resource version.

NORMALISABLE_FACETS = ("sector", "sub_class", "currency", "country", "asset_class")


def normalise_facets(row: dict | None) -> tuple[dict, list[str]]:
    """Normalise the five resource-backed facet fields on a row in place.

    Args:
        row: Any dict-shaped row. Modified in place; also returned
            for chaining. Untouched fields (anything not in
            :data:`NORMALISABLE_FACETS`) pass through unchanged.

    Returns:
        ``(row, unmatched_fields)`` where ``unmatched_fields`` is the
        list of facet names whose raw value did not resolve. The same
        list is also stored on ``row["_unmatched_facets"]`` (sorted,
        de-duplicated) for downstream introspection.
    """
    from porxpy.resources import (
        HOLDINGS_CLASS_INDEX, country_to_mstar, resolve_currency,
        resolve_sector, resolve_sub_class,
    )

    if row is None:
        return {}, []

    unmatched: list[str] = []

    # asset_class — normalised by the dedicated holding-enum
    # function, which always maps to one of equity/bond/cash/other
    # or "" (never adds an alias). It can't "fail to match" the
    # canonical taxonomy in the same way; an unknown asset_class
    # silently becomes "" (and downstream sub_class defaulting
    # kicks in). Not added to unmatched.
    ac_raw = (row.get("asset_class") or "")
    ac_norm = normalize_holding_asset_class(ac_raw)
    row["asset_class"] = ac_norm

    # sector
    sec_raw = (row.get("sector") or "").strip()
    if sec_raw:
        resolved = resolve_sector(sec_raw)
        if resolved:
            row["sector"] = resolved
        else:
            row["sector"] = sec_raw                   # preserve raw
            unmatched.append("sector")
    else:
        row["sector"] = ""

    # currency
    cur_raw = (row.get("currency") or "").strip()
    if cur_raw:
        resolved = resolve_currency(cur_raw)
        if resolved:
            row["currency"] = resolved
        else:
            row["currency"] = cur_raw
            unmatched.append("currency")
    else:
        row["currency"] = ""

    # country
    cty_raw = (row.get("country") or "").strip()
    if cty_raw:
        resolved = country_to_mstar(cty_raw)
        if resolved:
            row["country"] = resolved
        else:
            row["country"] = cty_raw
            unmatched.append("country")
    else:
        row["country"] = ""

    # sub_class — must resolve AND belong to the row's asset_class
    # group. A value that resolves canonically but is paired with
    # the wrong asset_class is still "unmatched" (the user needs to
    # fix one or the other).
    sub_raw = (row.get("sub_class") or "").strip()
    if sub_raw:
        resolved = resolve_sub_class(sub_raw)
        valid_subs = HOLDINGS_CLASS_INDEX.get(ac_norm, [])
        if resolved and resolved in valid_subs:
            row["sub_class"] = resolved
        else:
            row["sub_class"] = sub_raw                # preserve raw
            unmatched.append("sub_class")
    else:
        row["sub_class"] = ""

    # Stamp the unmatched list onto the row. Sorted+unique so
    # different invocations on the same row are comparable.
    row["_unmatched_facets"] = sorted(set(unmatched))
    return row, sorted(set(unmatched))


def coerce_holdings_row(raw: dict | None, *, row_id: str | None = None) -> dict:
    """Normalise an arbitrary dict into a complete unified-schema holdings row.

    Fills every field in :data:`HOLDINGS_ROW_FIELDS` (missing ones become
    ``""`` or ``0.0`` for numerics), coerces the numeric fields,
    normalises the holding classification, attaches a ``_row_id``, and
    normalises the bond date strings to ``DD/mmm/YYYY``.

    Field-type coercion:

    * Numeric fields (``weight_pct``, ``duration``, ``coupon``) — parsed
      leniently: a non-numeric or blank value becomes ``0.0``. A row with
      no weight contributes nothing to the rollup; a bond row with no
      duration/coupon contributes zero to those columns. Real numbers
      keep downstream sums and filters simple.
    * Date fields (``maturity``, ``effective_date``) — run through
      :func:`normalise_bond_date`. Stored as strings (blank stays
      blank); recognisable input lands in ``DD/mmm/YYYY``;
      unrecognisable input passes through verbatim so the user can spot
      and fix it.
    * Everything else — trimmed string.

    Holding classification (v0.6.0):

    * ``asset_class`` is run through
      :func:`normalize_holding_asset_class`, collapsing any spelling
      (Yahoo's "Equity" / "Fixed Income", the fund-level keys, …) to the
      lowercase ``equity`` / ``bond`` / ``cash`` / ``other`` enum. A
      blank value stays blank — this function does NOT fall back to the
      fund's asset class (it has no fund context); that fallback lives
      in the upload / extractor paths which do.
    * ``sub_class`` is left as whatever the row carried, lowercased. If
      it's blank BUT ``asset_class`` is now set, it's filled with the
      per-asset-class default via :func:`default_sub_class`. If
      ``asset_class`` is also blank, ``sub_class`` stays blank — the two
      are a pair.

    Reading an old (pre-bond-field) cached row through this function
    self-heals it: the bond columns simply default to blank/zero.

    Any keys on ``raw`` that aren't part of the schema (e.g.
    ``_defaulted`` / ``currency_derived`` provenance flags from the
    upload pipeline, or the legacy ``exchange`` / ``shares`` fields from
    very old Yahoo rows) are preserved as-is.

    Args:
        raw: Any dict-shaped holdings row, possibly partial.
        row_id: Force this ``_row_id``. When ``None``, reuse
            ``raw["_row_id"]`` if present, else mint a new one.

    Returns:
        A new dict: every schema field present, ``_row_id`` set, plus
        any non-schema extras carried over from ``raw``.
    """
    raw = raw or {}
    out: dict[str, Any] = {}

    # Carry over non-schema extras first so the canonical fields below
    # always win on key collisions.
    for k, v in raw.items():
        if k in HOLDINGS_ROW_FIELDS or k == "_row_id":
            continue
        out[k] = v

    for f in HOLDINGS_ROW_FIELDS:
        v = raw.get(f)
        if f in HOLDINGS_NUMERIC_FIELDS:
            if v is None or (isinstance(v, str) and not v.strip()):
                out[f] = 0.0
            else:
                try:
                    out[f] = float(v)
                except (TypeError, ValueError):
                    out[f] = 0.0
        elif f in HOLDINGS_DATE_FIELDS:
            out[f] = normalise_bond_date(v)
        else:
            out[f] = (str(v).strip() if v is not None else "")

    # Holding classification — all five resource-backed facets go
    # through the single normalise_facets() chokepoint (v0.15.0).
    # That function:
    #   * normalises asset_class to the lowercase 4-value enum
    #   * resolves sector / currency / country / sub_class to their
    #     canonical CSV values via the matches alias lists
    #   * preserves the raw value (rather than blanking) for any
    #     facet that doesn't resolve, and records the field in
    #     row["_unmatched_facets"] for the user-facing review dialog
    #   * validates sub_class against the asset_class group
    normalise_facets(out)
    # If sub_class came through blank (vs unmatched), default from
    # asset_class — preserves the long-standing fall-through behaviour.
    if not out.get("sub_class") and out.get("asset_class"):
        out["sub_class"] = default_sub_class(out["asset_class"])

    out["_row_id"] = row_id or raw.get("_row_id") or new_row_id()
    return out


# ---------------------------------------------------------------------------
# Cash positions (v0.14.0)
# ---------------------------------------------------------------------------
# Cash positions live on the portfolio, not in the fund cache — they
# aren't funds, they have no Yahoo ticker, no top-10, no price history
# in the conventional sense. The portfolio dict gains a
# ``cash_positions`` array; each entry is a flat dict shaped like
# :data:`CASH_POSITION_FIELDS`.
#
# The fields overlap with HOLDINGS_ROW_FIELDS where it's natural (name,
# country, currency, asset_class, sub_class, sector, effective_date),
# diverge where holdings semantics don't fit (amount in position
# currency replaces weight_pct; interest replaces coupon), and drop
# the rest (no ticker, isin, duration, maturity).
#
# Rollup integration:
#   * For breakdowns (asset class / sector / country / currency tiles
#     and the portfolio Holdings sub-tab), each cash position is
#     treated as a one-row "synthetic fund" whose base-currency value
#     contributes to the weighted rollup.
#   * For price history (the portfolio value chart), each cash position
#     synthesises a daily series of
#         value(t) = amount * exp(interest_rate * t)
#     in the position currency, where t is years from effective_date;
#     the existing FX path then converts to base currency per date.
#     Continuous compounding gives a smooth climbing line — annual /
#     monthly compounding would draw small visible steps; the
#     mathematical difference at typical rates is negligible.
CASH_POSITION_FIELDS: tuple[str, ...] = (
    "name", "country", "amount", "currency",
    "effective_date", "interest",
    "asset_class", "sub_class", "sector",
)
CASH_POSITION_NUMERIC_FIELDS: tuple[str, ...] = ("amount", "interest")
CASH_POSITION_DATE_FIELDS:    tuple[str, ...] = ("effective_date",)


def coerce_cash_position(raw: dict | None, *, id: str | None = None) -> dict:
    """Normalise an arbitrary dict into a complete cash-position record.

    Symmetric to :func:`coerce_holdings_row` but for cash:

    * Fills every field in :data:`CASH_POSITION_FIELDS` (missing become
      ``""`` or ``0.0`` for the two numerics).
    * Coerces ``amount`` and ``interest`` to floats; blank/non-numeric
      becomes ``0.0``. ``interest`` is stored as a percent (5.0 means
      5%) — the rollup converts to a decimal at the point of use.
    * Coerces ``effective_date`` through :func:`normalise_bond_date` so
      it matches the DD/mmm/YYYY canonical form used by the bond
      fields. A blank effective_date is allowed — the rollup falls
      back to "started at the beginning of the price history" for
      that position.
    * Runs ``sector`` / ``currency`` / ``sub_class`` / ``country``
      through the same resolvers ``coerce_holdings_row`` uses, so a
      typed "us dollar" becomes "USD" etc.
    * Defaults ``asset_class`` to ``"cash"`` if blank — the most
      common case ("ING savings"). A deposit product with a
      ``sub_class`` of "regular currency bond" should be set
      explicitly by the user; we don't try to infer.
    * Attaches an ``id`` (uses the one supplied, the one already on
      ``raw``, or mints a fresh one) — cash positions are user-
      addressable for delete / patch, like holdings rows.

    Args:
        raw: Any dict-shaped cash position, possibly partial.
        id: Force this ``id``. When ``None``, reuse ``raw["id"]`` if
            present, else mint a new one.

    Returns:
        A new dict: every schema field present, ``id`` set.
    """
    from porxpy.resources import (
        country_to_mstar, resolve_currency, resolve_sector, resolve_sub_class,
    )
    from porxpy.resources import HOLDINGS_CLASS_INDEX

    raw = raw or {}
    out: dict[str, Any] = {}

    for f in CASH_POSITION_FIELDS:
        v = raw.get(f)
        if f in CASH_POSITION_NUMERIC_FIELDS:
            if v is None or (isinstance(v, str) and not v.strip()):
                out[f] = 0.0
            else:
                try:
                    out[f] = float(v)
                except (TypeError, ValueError):
                    out[f] = 0.0
        elif f in CASH_POSITION_DATE_FIELDS:
            out[f] = normalise_bond_date(v)
        else:
            out[f] = (str(v).strip() if v is not None else "")

    # Default cash-specific facets first so normalise_facets sees
    # them as canonical values rather than blanks. The chokepoint
    # then resolves anything the user typed (e.g. "us dollar" →
    # "USD") and annotates unmatched values with _unmatched_facets.
    CASH_SECTOR_DEFAULT = "cash and/or derivatives"
    # asset_class: default to "cash" when blank. Lets the user save
    # a position without explicitly picking the dropdown (the FE
    # already does, but a hand-edited portfolios.json benefits).
    if not (out.get("asset_class") or "").strip():
        out["asset_class"] = "cash"
    # sector: default the cash one if blank. If the user supplied
    # something else (e.g. moved a deposit to "financial services"),
    # let normalise_facets resolve it.
    if not (out.get("sector") or "").strip():
        out["sector"] = CASH_SECTOR_DEFAULT

    # Single chokepoint — resolves all five facets, preserves raws
    # on miss, stamps _unmatched_facets.
    normalise_facets(out)

    # If sub_class came through blank, default from asset_class. For
    # an unmatched sub_class, the raw user value is preserved and
    # flagged — we don't replace it with a default (that would lose
    # information the resolution dialog needs).
    if not out.get("sub_class") and out.get("asset_class") \
            and "sub_class" not in (out.get("_unmatched_facets") or []):
        out["sub_class"] = default_sub_class(out["asset_class"]) or ""

    out["id"] = id or raw.get("id") or new_row_id()
    return out


def cash_positions_get(pid: str) -> list[dict]:
    """Read the cash_positions list for a portfolio.

    Returns ``[]`` for a portfolio that has no list yet (the field is
    optional and only appears once the user adds the first position).
    Every returned row is :func:`coerce_cash_position`-cleaned so the
    caller can trust the shape.

    Args:
        pid: Portfolio UUID.

    Returns:
        List of normalised cash positions; empty list for a missing
        portfolio or empty list-field.
    """
    p = find_portfolio(pid)
    if not p:
        return []
    raw = p.get("cash_positions") or []
    if not isinstance(raw, list):
        return []
    return [coerce_cash_position(r) for r in raw if isinstance(r, dict)]


def cash_positions_set(pid: str, positions: list[dict]) -> list[dict]:
    """Replace the entire cash_positions list for a portfolio.

    Atomic write of the whole list — the simplest correct shape for
    the inline-edit table on the frontend, which always sends the
    current full list on save. Every incoming entry is normalised
    before persisting.

    Args:
        pid: Portfolio UUID.
        positions: Full list of cash-position dicts (each possibly
            partial — coerce_cash_position fills in missing fields).

    Returns:
        The normalised list that was just persisted (so the caller
        can echo it back to the client without re-reading).

    Raises:
        KeyError: No portfolio with ``pid``.
    """
    portfolios = load_portfolios()
    for i, p in enumerate(portfolios):
        if p.get("id") != pid:
            continue
        coerced = [coerce_cash_position(r) for r in (positions or [])
                   if isinstance(r, dict)]
        p["cash_positions"] = coerced
        portfolios[i] = p
        save_portfolios(portfolios)
        return coerced
    raise KeyError(f"portfolio {pid!r} not found")


def cash_position_delete(pid: str, position_id: str) -> bool:
    """Remove one cash position from a portfolio by its id.

    Args:
        pid: Portfolio UUID.
        position_id: The position's ``id`` (minted by coerce_cash_position).

    Returns:
        ``True`` if a position was removed, ``False`` if no portfolio
        or no matching id was found.
    """
    portfolios = load_portfolios()
    for i, p in enumerate(portfolios):
        if p.get("id") != pid:
            continue
        raw = p.get("cash_positions") or []
        if not isinstance(raw, list):
            return False
        new = [r for r in raw if isinstance(r, dict) and r.get("id") != position_id]
        if len(new) == len(raw):
            return False
        p["cash_positions"] = new
        portfolios[i] = p
        save_portfolios(portfolios)
        return True
    return False


# ---------------------------------------------------------------------------
# Per-symbol Yahoo info cache (HQ country, trading currency, asset class)
# ---------------------------------------------------------------------------
# Used by the top-10 enrichment path: when Yahoo top-10 is the only
# holdings data we have, we look up the country / currency / asset
# class for each holding's symbol so the look-through breakdowns can
# be populated. One shared file across all funds — every fund tracking
# AAPL benefits from a single lookup. Top-level keys are symbols; each
# value is a {fetched_at, value: {...}} entry shaped like the per-ticker
# category entries so age_days() works the same way.
def symbol_info_get(symbol: str) -> dict | None:
    """Read a non-stale symbol-info entry from the shared cache.

    Args:
        symbol: Yahoo-style ticker for a single holding (e.g. ``"AAPL"``,
            ``"7203.T"``). Case-sensitive and used as-is — the caller is
            responsible for any normalisation.

    Returns:
        ``{"country", "currency", "asset_class", "sub_class", "sector",
        "name", "quote_type"}`` or ``None`` if the entry is missing,
        malformed, or older than :data:`porxpy.config.SYMBOL_INFO_TTL_DAYS`.
    """
    if not symbol:
        return None
    blob  = cache_read(SYMBOL_INFO_CACHE_NAME, "_symbol_info")
    entry = blob.get(symbol)
    if not entry or not isinstance(entry, dict):
        return None
    age = age_days(entry.get("fetched_at", ""))
    if age is None or age > SYMBOL_INFO_TTL_DAYS:
        return None
    val = entry.get("value")
    return val if isinstance(val, dict) else None


def symbol_info_put(symbol: str, info: dict) -> None:
    """Persist a symbol-info entry with the current timestamp.

    Args:
        symbol: Yahoo ticker.
        info: Free-shaped dict — typically
            ``{"country", "currency", "asset_class", "name", "quote_type"}``.
            Stored verbatim; callers are responsible for sanitising.
    """
    if not symbol:
        return
    blob = cache_read(SYMBOL_INFO_CACHE_NAME, "_symbol_info")
    blob[symbol] = {"fetched_at": now_iso(), "value": info}
    cache_write(SYMBOL_INFO_CACHE_NAME, "_symbol_info", blob)


# ---------------------------------------------------------------------------
# Symbol alias cache (raw input → resolved Yahoo ticker, or None for "no match")
# ---------------------------------------------------------------------------
# When :func:`porxpy.extractors.get_symbol_info_cached` probes candidate
# variants for an input that doesn't match Yahoo as-is, the resolution
# is recorded here so the next probe of the same raw input skips
# straight to the right entry. A ``None`` value means "tried every
# candidate, nothing worked" — also short-circuited so we don't probe
# Yahoo over and over for the same dud input.
def alias_get(raw: str) -> tuple[bool, str | None]:
    """Read an alias cache entry for ``raw``.

    Args:
        raw: The raw input form (already cleaned by
            :func:`porxpy.resolver.clean_holding_ticker_input`).

    Returns:
        ``(present, resolved_or_none)`` where ``present`` is True if the
        raw form has been probed before. ``resolved_or_none`` is the
        Yahoo ticker that worked, or ``None`` if nothing did. Callers
        should treat ``(False, _)`` as "needs probing".
    """
    if not raw:
        return False, None
    blob = cache_read(SYMBOL_ALIAS_CACHE_NAME, "_symbol_aliases")
    if raw not in blob:
        return False, None
    val = blob[raw]
    # Tolerate both bare-string entries (older format) and dict entries
    # so a future refactor can add metadata without breaking the cache.
    if isinstance(val, dict):
        return True, val.get("resolved")
    return True, val


def alias_put(raw: str, resolved: str | None) -> None:
    """Persist an alias cache entry.

    Args:
        raw: The raw input form.
        resolved: The Yahoo ticker that worked, or ``None`` to mark the
            input as "tried, no match".
    """
    if not raw:
        return
    blob = cache_read(SYMBOL_ALIAS_CACHE_NAME, "_symbol_aliases")
    blob[raw] = {"resolved": resolved, "stamped_at": now_iso()}
    cache_write(SYMBOL_ALIAS_CACHE_NAME, "_symbol_aliases", blob)


def alias_delete(raw: str) -> bool:
    """Remove an alias cache entry for ``raw``, if present.

    Used to clear a stale negative alias (``None`` resolved value)
    before re-enriching on a fresh upload — without this, a ticker
    that failed to resolve on a previous upload would be short-
    circuited forever, even if the re-upload supplies an ISIN or
    CUSIP that would now let us find it.

    Args:
        raw: The raw input form (already cleaned).

    Returns:
        ``True`` if an entry was present and removed, ``False`` if
        nothing was cached for ``raw``.
    """
    if not raw:
        return False
    blob = cache_read(SYMBOL_ALIAS_CACHE_NAME, "_symbol_aliases")
    if raw not in blob:
        return False
    del blob[raw]
    cache_write(SYMBOL_ALIAS_CACHE_NAME, "_symbol_aliases", blob)
    return True


# ---------------------------------------------------------------------------
# FX conversion
# ---------------------------------------------------------------------------
def normalise_currency(cur: str | None) -> tuple[str, float]:
    """Resolve a Yahoo currency code to a canonical code and price divisor.

    Some Yahoo tickers quote in sub-units like ``GBp`` (British pence).
    The frontend wants prices in whole units of the canonical currency,
    so we return both the canonical code and the divisor to apply.

    Args:
        cur: A Yahoo currency code, e.g. ``"GBp"`` or ``"USD"``.

    Returns:
        ``(canonical_code, divisor)``. ``divisor`` is the number to divide
        the raw Yahoo price by — 100 for pence-to-pounds, 1.0 otherwise.
        For unknown codes the input is returned unchanged with divisor 1.0.
    """
    if not cur:
        return "", 1.0
    if cur in PENCE_CURRENCIES and PENCE_CURRENCIES[cur] is not None:
        main, divisor = PENCE_CURRENCIES[cur]    # type: ignore[misc]
        return main, divisor
    return cur.upper(), 1.0


def _fx_cache_key(pair: str) -> str:
    """Cache filename for a single FX rate (e.g. ``"FX_EURUSD"``)."""
    return f"FX_{pair.upper()}"


def fx_rate(from_cur: str, to_cur: str) -> tuple[float | None, str]:
    """Look up the latest FX rate ``from_cur → to_cur``.

    Hits the on-disk FX cache first (TTL :data:`FX_TTL_HOURS`); falls back
    to a Yahoo ``=X`` ticker lookup with an inverse-pair fallback.

    Args:
        from_cur: 3-letter source currency (e.g. ``"EUR"``).
        to_cur: 3-letter target currency (e.g. ``"USD"``).

    Returns:
        ``(rate, note)``. ``rate`` is the multiplier such that
        ``1 from_cur = rate to_cur``, or ``None`` if no rate could be
        obtained. ``note`` describes the source for logging.
    """
    if not from_cur or not to_cur:
        return None, "missing currency"
    from_cur = from_cur.upper()
    to_cur   = to_cur.upper()
    if from_cur == to_cur:
        return 1.0, "same currency"

    pair_key  = _fx_cache_key(f"{from_cur}{to_cur}")
    blob      = cache_read(pair_key, "_fx")
    entry     = blob.get("rate")
    if entry:
        age = age_days(entry.get("fetched_at", ""))
        if age is not None and age <= (FX_TTL_HOURS / 24.0):
            return entry.get("value"), f"cache (age {age*24:.1f}h)"

    # Live lookup via Yahoo's FX ticker (e.g. EURUSD=X)
    sym = f"{from_cur}{to_cur}=X"
    try:
        h = yf.Ticker(sym).history(period="5d", interval="1d")
        if not h.empty and "Close" in h.columns:
            rate = float(h["Close"].iloc[-1])
            if rate > 0 and not math.isnan(rate) and not math.isinf(rate):
                blob["rate"] = {"fetched_at": now_iso(), "value": rate}
                cache_write(pair_key, "_fx", blob)
                return rate, f"live ({sym})"
    except Exception as exc:
        print(f"[FX] {sym} error: {exc}")

    # Fall back to inverse
    inv_sym = f"{to_cur}{from_cur}=X"
    try:
        h = yf.Ticker(inv_sym).history(period="5d", interval="1d")
        if not h.empty and "Close" in h.columns:
            inv = float(h["Close"].iloc[-1])
            if inv > 0 and not math.isnan(inv) and not math.isinf(inv):
                rate = 1.0 / inv
                blob["rate"] = {"fetched_at": now_iso(), "value": rate}
                cache_write(pair_key, "_fx", blob)
                return rate, f"live via inverse ({inv_sym})"
    except Exception as exc:
        print(f"[FX] {inv_sym} error: {exc}")

    return None, f"no rate for {from_cur}→{to_cur}"


def price_in_base(price_native: float | None, native_cur: str,
                  base_cur: str) -> tuple[float | None, dict]:
    """Convert a price from its native currency to a base currency.

    Handles GBp→GBP (and similar sub-unit splits) before applying FX, so
    callers can pass raw Yahoo prices directly.

    Args:
        price_native: Raw price in ``native_cur`` units. ``None`` is
            propagated through.
        native_cur: Yahoo currency code of the price (may be a sub-unit
            like ``"GBp"``).
        base_cur: Target currency.

    Returns:
        ``(value_in_base, meta)``. ``meta`` keys:
            native_cur, base_cur, adjusted_cur, pence_divisor,
            fx_rate, fx_note, error (only set on failure).
    """
    meta: dict = {"native_cur": native_cur, "base_cur": base_cur,
                  "fx_rate": None, "fx_note": "", "adjusted_cur": native_cur,
                  "pence_divisor": 1.0}
    if price_native is None:
        return None, {**meta, "error": "no price"}

    canon, divisor = normalise_currency(native_cur)
    if canon and canon != native_cur:
        meta["adjusted_cur"]  = canon
        meta["pence_divisor"] = divisor
    adj_price = price_native / divisor

    if not canon:
        # Unknown native currency — assume it equals base and move on
        return adj_price, {**meta, "fx_note": "native currency unknown, assumed base"}

    if canon == base_cur.upper():
        return adj_price, {**meta, "fx_rate": 1.0, "fx_note": "same currency"}

    rate, note = fx_rate(canon, base_cur)
    meta["fx_rate"] = rate
    meta["fx_note"] = note
    if rate is None:
        return None, {**meta, "error": "fx lookup failed"}
    return adj_price * rate, meta


def fx_history(from_cur: str, to_cur: str) -> dict[str, float]:
    """Return a daily FX time series for ``from_cur → to_cur``.

    Used by the portfolio price-history chart to value each fund's
    historical prices in the portfolio's base currency. Cached on disk
    under ``FXH_<pair>.json`` and refreshed at most once per
    :data:`FX_HIST_TTL_HOURS`.

    Args:
        from_cur: Source currency.
        to_cur: Target currency.

    Returns:
        ``{"YYYY-MM-DD": rate, ...}``. Returns ``{}`` for same-currency
        pairs (callers must treat absence as ``1.0``) and on lookup failure.
    """
    if not from_cur or not to_cur:
        return {}
    from_cur = from_cur.upper()
    to_cur   = to_cur.upper()
    if from_cur == to_cur:
        return {}

    pair_key = f"FXH_{from_cur}{to_cur}"
    blob = cache_read(pair_key, "_fx")
    entry = blob.get("series")
    if entry:
        age = age_days(entry.get("fetched_at", ""))
        if age is not None and age <= (FX_HIST_TTL_HOURS / 24.0):
            val = entry.get("value") or {}
            if val:
                return val

    def _fetch(sym: str) -> dict[str, float]:
        """Pull a Yahoo FX series and convert it into a date→rate dict."""
        try:
            h = yf.Ticker(sym).history(period="max", interval="1d")
            if h.empty or "Close" not in h.columns:
                return {}
            h.index = h.index.tz_localize(None)
            out: dict[str, float] = {}
            for dt, row in h.iterrows():
                v = row.get("Close")
                try:
                    vf = float(v)
                except (TypeError, ValueError):
                    continue
                if vf > 0 and not math.isnan(vf) and not math.isinf(vf):
                    out[dt.strftime("%Y-%m-%d")] = vf
            return out
        except Exception as exc:
            print(f"[FX-hist] {sym} error: {exc}")
            return {}

    # Direct pair first, then inverse if that fails
    series = _fetch(f"{from_cur}{to_cur}=X")
    if not series:
        inv = _fetch(f"{to_cur}{from_cur}=X")
        if inv:
            series = {d: (1.0 / r) for d, r in inv.items() if r > 0}

    if series:
        blob["series"] = {"fetched_at": now_iso(), "value": series}
        cache_write(pair_key, "_fx", blob)
    return series


# ---------------------------------------------------------------------------
# ISIN map — persistent cache of ISIN+MIC → Yahoo ticker
# ---------------------------------------------------------------------------
def _isin_map_key(isin: str, mic: str | None) -> str:
    """Build the dict key used in ``isin_map.json``."""
    return f"{isin.upper()}|{(mic or '').upper()}"


def load_isin_map() -> dict:
    """Load the persisted ISIN→ticker map from disk.

    Returns:
        The full map dict, or ``{}`` on missing/invalid file.
    """
    if not ISIN_MAP_FP.exists():
        return {}
    try:
        with open(ISIN_MAP_FP, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[ISIN map] load error: {exc}")
        return {}


def save_isin_map(m: dict) -> None:
    """Persist the ISIN→ticker map to disk."""
    try:
        with open(ISIN_MAP_FP, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"[ISIN map] save error: {exc}")


def isin_map_get(isin: str, mic: str | None) -> dict | None:
    """Read a non-stale entry from the ISIN map.

    Args:
        isin: ISIN code.
        mic: Optional MIC the resolution was scoped to. ``None`` matches
            entries that were resolved without a MIC hint.

    Returns:
        ``{ticker, resolved_mic, note, resolved_at}`` or ``None`` when
        absent or older than :data:`ISIN_MAP_TTL_DAYS`.
    """
    m = load_isin_map()
    entry = m.get(_isin_map_key(isin, mic))
    if not entry:
        return None
    age = age_days(entry.get("resolved_at", ""))
    if age is None or age > ISIN_MAP_TTL_DAYS:
        return None
    return entry


def isin_map_put(isin: str, mic: str | None, ticker: str,
                 resolved_mic: str, note: str) -> None:
    """Add or refresh an ISIN-map entry.

    Args:
        isin: ISIN code.
        mic: Original MIC hint (may be ``None``).
        ticker: Resolved Yahoo ticker.
        resolved_mic: MIC the ticker is on (may differ from the hint when
            the resolver auto-selected a listing).
        note: Free-form explanation, surfaced to the UI for transparency.
    """
    if not ticker:
        return
    m = load_isin_map()
    m[_isin_map_key(isin, mic)] = {
        "ticker":       ticker,
        "resolved_mic": resolved_mic,
        "note":         note,
        "resolved_at":  now_iso(),
    }
    save_isin_map(m)


# ---------------------------------------------------------------------------
# Per-fund overrides — ONE store, ISIN-keyed (0.12.0+)
# ---------------------------------------------------------------------------
# A single overrides.json at the project root supersedes the three
# previous ticker-keyed override files (asset_class_overrides.json,
# breakdown_source_overrides.json, fund_structure.json). The keys are
# ISINs — overrides are fund-level: every listing of one fund shares
# them. Each ISIN maps to a sub-dict with optional sections:
#
#   {
#     "asset_class":       "fixed_income",          # str | absent
#     "breakdown_source":  {"country": "holdings"}, # dict | absent
#     "fund_structure":    {"structure": "etf",
#                           "replication": "full",
#                           "style": "passive"},    # dict | absent
#   }
#
# Overrides are NOT cache: they capture user intent and survive every
# cache refresh. They live at the project root, not under cache/, so
# the "Clear cache" reset never touches them. ``load_fund_data`` layers
# them on top of the Yahoo-derived data at read time.

def _ov_key(isin: str) -> str:
    """Normalise an ISIN to the canonical override-store key."""
    return (isin or "").strip().upper()


def load_overrides() -> dict:
    """Load the unified overrides file. Returns an empty dict if missing."""
    if not OVERRIDES_FP.exists():
        return {}
    try:
        with open(OVERRIDES_FP, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_overrides(m: dict) -> None:
    """Persist the unified overrides file."""
    try:
        with open(OVERRIDES_FP, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[Overrides] save error: {exc}")


def overrides_get(isin: str) -> dict:
    """Return all overrides for an ISIN, or an empty dict if none.

    The returned dict is a shallow copy and safe to mutate; persistence
    only happens through the ``*_put`` / ``*_delete`` helpers below.
    """
    key = _ov_key(isin)
    if not key:
        return {}
    return dict(load_overrides().get(key) or {})


# ── asset_class ─────────────────────────────────────────────────────────────
def asset_class_override_get(isin: str) -> str | None:
    """Return the per-fund asset-class override for ``isin``, or None."""
    v = overrides_get(isin).get("asset_class")
    return v if isinstance(v, str) and v.strip() else None


def asset_class_override_put(isin: str, asset_class: str) -> None:
    """Set the asset-class override for ``isin``. No-op for blank keys."""
    key = _ov_key(isin)
    if not key or not asset_class:
        return
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"asset_class must be one of {ASSET_CLASSES}")
    m = load_overrides()
    entry = dict(m.get(key) or {})
    entry["asset_class"] = asset_class
    m[key] = entry
    save_overrides(m)


def asset_class_override_delete(isin: str) -> bool:
    """Remove the asset-class override for ``isin``. Returns True if removed."""
    key = _ov_key(isin)
    if not key:
        return False
    m = load_overrides()
    entry = m.get(key)
    if not entry or "asset_class" not in entry:
        return False
    del entry["asset_class"]
    if entry:
        m[key] = entry
    else:
        # Pruning empty sub-dicts keeps the file readable.
        del m[key]
    save_overrides(m)
    return True


# ── breakdown_source ────────────────────────────────────────────────────────
def breakdown_overrides_get(isin: str) -> dict:
    """Return the per-facet breakdown-source override map for ``isin``.

    The shape is ``{facet: source}`` where ``source`` is the non-default
    choice (typically ``"holdings"``). An absent facet means "use the
    issuer-published value" — the default. Always returns a fresh dict.
    """
    v = overrides_get(isin).get("breakdown_source") or {}
    return dict(v) if isinstance(v, dict) else {}


def breakdown_override_get(isin: str, facet: str) -> str:
    """Source for a single facet — ``"fund"`` when no override is set."""
    return breakdown_overrides_get(isin).get(facet, "fund")


def breakdown_override_put(isin: str, facet: str, source: str) -> None:
    """Set the breakdown source for one facet of one fund.

    Setting it to the default (``"fund"``) deletes the override instead
    of storing it, keeping the on-disk shape minimal.
    """
    key = _ov_key(isin)
    if not key:
        return
    if facet not in BREAKDOWN_FACETS:
        raise ValueError(f"facet must be one of {BREAKDOWN_FACETS}")
    if source not in BREAKDOWN_SOURCES:
        raise ValueError(f"source must be one of {BREAKDOWN_SOURCES}")
    m = load_overrides()
    entry = dict(m.get(key) or {})
    bd = dict(entry.get("breakdown_source") or {})
    if source == "fund":
        bd.pop(facet, None)
    else:
        bd[facet] = source
    if bd:
        entry["breakdown_source"] = bd
    else:
        entry.pop("breakdown_source", None)
    if entry:
        m[key] = entry
    else:
        m.pop(key, None)
    save_overrides(m)


def breakdown_override_delete(isin: str, facet: str | None = None) -> bool:
    """Delete one facet's override, or the whole map when ``facet`` is None."""
    key = _ov_key(isin)
    if not key:
        return False
    m = load_overrides()
    entry = m.get(key)
    if not entry or "breakdown_source" not in entry:
        return False
    if facet is None:
        del entry["breakdown_source"]
        changed = True
    else:
        bd = entry["breakdown_source"]
        if facet not in bd:
            return False
        del bd[facet]
        if not bd:
            del entry["breakdown_source"]
        changed = True
    if entry:
        m[key] = entry
    else:
        m.pop(key, None)
    save_overrides(m)
    return changed


# ── uploaded_breakdowns ─────────────────────────────────────────────────────
# User-uploaded per-facet breakdown item lists, keyed by ISIN. This is
# the third source for the fund-level breakdown cards (alongside the
# issuer aggregate and the holdings roll-up). The data is materially
# bigger than a one-word source choice — full item lists per facet —
# so it lives in the cache layer as a fund-level category, not inside
# overrides.json. The cache entry is manual-refresh-only: never fetched,
# only written when the user commits a CSV upload.
#
# On-disk shape (under the ``uploaded_breakdowns`` category):
#   { "fetched_at": "...",
#     "value": { "asset_class": [{"key","weight"}, ...],
#                "sector":      [...],
#                "country":     [...],
#                "currency":    [...] } }
# Only facets present with a non-empty list count as "uploaded"; absent
# or empty facets cannot be flipped to source "upload" on the fund page.

def uploaded_breakdowns_get(isin: str) -> dict:
    """Return the per-facet uploaded item lists for ``isin``.

    Always returns a fresh dict with the four facet keys present (some
    possibly empty lists) so callers can index without checking for
    None. An ISIN with no upload returns ``{facet: []}`` across all four.
    """
    out: dict[str, list[dict]] = {
        "asset_class": [], "sector": [], "country": [], "currency": [],
    }
    if not isin:
        return out
    blob  = cache_read(isin, "uploaded_breakdowns")
    entry = blob.get("uploaded_breakdowns")
    if not isinstance(entry, dict):
        return out
    val = entry.get("value")
    if not isinstance(val, dict):
        return out
    for facet in BREAKDOWN_FACETS:
        items = val.get(facet)
        if isinstance(items, list):
            out[facet] = [
                {"key": str(it.get("key") or ""),
                 "weight": float(it.get("weight") or 0.0)}
                for it in items
                if isinstance(it, dict) and it.get("key")
            ]
    return out


def uploaded_breakdowns_put(isin: str, facets: dict) -> dict:
    """Replace the uploaded breakdowns for ``isin`` with ``facets``.

    Replace semantics: any existing entry is overwritten in full. The
    commit endpoint is the only caller, and it always passes the
    fully-resolved per-facet item lists from the preview token. Callers
    wanting "merge with existing" must read first, merge in memory,
    then call this.

    Args:
        isin: Fund ISIN (will be normalised to upper).
        facets: ``{facet: [{"key","weight"}, ...]}``. Facets absent from
            the dict are persisted as empty lists; facets present but
            empty are also persisted as empty (so a subsequent
            ``uploaded_breakdowns_get`` reports both equivalently).

    Returns:
        The normalised, persisted dict (same shape as
        :func:`uploaded_breakdowns_get`'s return).
    """
    isin_u = (isin or "").strip().upper()
    if not isin_u:
        return {}
    normalised: dict[str, list[dict]] = {
        "asset_class": [], "sector": [], "country": [], "currency": [],
    }
    if isinstance(facets, dict):
        for facet in BREAKDOWN_FACETS:
            items = facets.get(facet)
            if isinstance(items, list):
                normalised[facet] = [
                    {"key": str(it.get("key") or ""),
                     "weight": float(it.get("weight") or 0.0)}
                    for it in items
                    if isinstance(it, dict) and it.get("key")
                ]
    blob = cache_read(isin_u, "uploaded_breakdowns")
    blob["uploaded_breakdowns"] = {
        "fetched_at": now_iso(),
        "value":      normalised,
    }
    cache_write(isin_u, "uploaded_breakdowns", blob)
    return normalised


def uploaded_breakdowns_delete(isin: str, facet: str | None = None) -> bool:
    """Remove uploaded breakdowns for ``isin``.

    Args:
        isin: Fund ISIN.
        facet: If given, clear only that one facet (the other three
            survive). If None, clear the whole entry — the on-disk
            cache entry is deleted so the fund returns to the
            two-source state.

    Returns:
        True if anything changed on disk.
    """
    isin_u = (isin or "").strip().upper()
    if not isin_u:
        return False
    blob  = cache_read(isin_u, "uploaded_breakdowns")
    entry = blob.get("uploaded_breakdowns")
    if not isinstance(entry, dict):
        return False
    val = entry.get("value")
    if not isinstance(val, dict):
        return False

    if facet is None:
        del blob["uploaded_breakdowns"]
        cache_write(isin_u, "uploaded_breakdowns", blob)
        return True

    if facet not in BREAKDOWN_FACETS:
        return False
    if not val.get(facet):
        return False
    val[facet] = []
    # If every facet is now empty, drop the whole entry rather than
    # leaving an empty husk behind.
    if not any(val.get(f) for f in BREAKDOWN_FACETS):
        del blob["uploaded_breakdowns"]
    else:
        entry["fetched_at"] = now_iso()
        entry["value"]      = val
    cache_write(isin_u, "uploaded_breakdowns", blob)
    return True


# ── fund_structure ──────────────────────────────────────────────────────────
# normalise_fund_structure() lives just below — it is pure logic
# (coupling rules) and shared with the seed pathway in extractors.py.

def normalise_fund_structure(raw: dict | None) -> dict:
    """Coerce a partial/raw structure dict into a valid, coupled block.

    Unknown or missing values fall back to ``"unknown"``. The coupling
    rule between ``structure`` and ``replication`` is enforced so the
    stored block can never represent a nonsensical state (e.g. a plain
    fund with "synthetic" replication).

    Args:
        raw: A dict that may contain ``structure`` / ``replication`` /
            ``style`` keys, or ``None``.

    Returns:
        A complete ``{structure, replication, style}`` dict with every
        value drawn from the config vocabularies.
    """
    raw = raw if isinstance(raw, dict) else {}
    structure = str(raw.get("structure", "")).strip().lower()
    if structure not in FUND_STRUCTURES:
        structure = "unknown"
    style = str(raw.get("style", "")).strip().lower()
    if style not in FUND_STYLES:
        style = "unknown"
    replication = str(raw.get("replication", "")).strip().lower()
    if replication not in REPLICATION_METHODS:
        replication = "unknown"
    # v0.21.0 — distribution policy (accumulating / distributing / unknown).
    # Lives on the structure block so it travels with Replication and Style
    # through the override surface and the Edit Fund dialog.
    distribution = str(raw.get("distribution", "")).strip().lower()
    if distribution not in DISTRIBUTION_POLICIES:
        distribution = "unknown"

    # Couple replication to structure.
    if structure == "fund":
        # A non-ETF has no index-replication method.
        replication = "n/a"
    elif replication == "n/a":
        # "n/a" only makes sense for a plain fund; for an ETF (or an
        # unknown structure) treat it as "not yet specified".
        replication = "unknown"

    return {"structure":    structure,
            "replication":  replication,
            "style":        style,
            "distribution": distribution}



def fund_structure_get(isin: str) -> dict | None:
    """Return the user-supplied fund-structure block for ``isin``, or None."""
    v = overrides_get(isin).get("fund_structure")
    return dict(v) if isinstance(v, dict) and v else None


def fund_structure_put(isin: str, structure: dict) -> dict:
    """Persist a fund-structure override, normalising the coupling rules.

    Returns the normalised block that was actually written.
    """
    key = _ov_key(isin)
    if not key:
        return {}
    normalised = normalise_fund_structure(structure)
    m = load_overrides()
    entry = dict(m.get(key) or {})
    entry["fund_structure"] = normalised
    m[key] = entry
    save_overrides(m)
    return normalised


def fund_structure_delete(isin: str) -> bool:
    """Remove the structure override for ``isin``. Returns True if removed."""
    key = _ov_key(isin)
    if not key:
        return False
    m = load_overrides()
    entry = m.get(key)
    if not entry or "fund_structure" not in entry:
        return False
    del entry["fund_structure"]
    if entry:
        m[key] = entry
    else:
        m.pop(key, None)
    save_overrides(m)
    return True




# ---------------------------------------------------------------------------
# Portfolio store (JSON-backed)
# ---------------------------------------------------------------------------
def load_portfolios() -> list[dict]:
    """Load all portfolios from ``portfolios.json``.

    Returns:
        A list of portfolio dicts, or ``[]`` on missing/invalid file.
        v0.15.0: facet fields on every cash position are lazily
        re-normalised if the file's stamp is stale relative to the
        current resource-CSV versions; the migrated file is persisted
        back. Migration is silent on success.
    """
    if not PORTFOLIOS_FP.exists():
        return []
    try:
        with open(PORTFOLIOS_FP, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
    except Exception as exc:
        print(f"[Portfolios] load error: {exc}")
        return []

    # Lazy facet migration (v0.15.0). The stamp lives on the wrapper
    # object — since portfolios.json holds a list, we use a sidecar
    # ``_normalisation`` key on each individual portfolio dict.
    from porxpy.resources import RESOURCE_VERSIONS
    current_versions = dict(RESOURCE_VERSIONS)
    touched_any = False
    for portfolio in data:
        if not isinstance(portfolio, dict):
            continue
        stamp = portfolio.get("_normalisation") or {}
        if (stamp.get("versions") == current_versions
                and stamp.get("is_normalized")):
            continue
        # Re-run normalise_facets on every cash position.
        cash = portfolio.get("cash_positions") or []
        if isinstance(cash, list):
            for pos in cash:
                if isinstance(pos, dict):
                    normalise_facets(pos)
                    touched_any = True
        from datetime import datetime, timezone
        portfolio["_normalisation"] = {
            "is_normalized": True,
            "versions":      current_versions,
            "normalised_at": datetime.now(timezone.utc).isoformat(),
        }
        touched_any = True

    if touched_any:
        try:
            save_portfolios(data)
        except Exception as exc:
            print(f"[Portfolios] facet migration persist failed: {exc}")
    return data


def save_portfolios(portfolios: list[dict]) -> None:
    """Persist the portfolio list to disk.

    Args:
        portfolios: Full list to write (overwrites the existing file).
    """
    with open(PORTFOLIOS_FP, "w", encoding="utf-8") as f:
        json.dump(portfolios, f, indent=2, ensure_ascii=False)


def find_portfolio(pid: str) -> dict | None:
    """Look up a single portfolio by id.

    Args:
        pid: Portfolio UUID (the ``id`` field stored in ``portfolios.json``).

    Returns:
        The portfolio dict, or ``None`` if no match.
    """
    for p in load_portfolios():
        if p.get("id") == pid:
            return p
    return None


def upsert_portfolio(portfolio: dict) -> None:
    """Insert or update a portfolio (matched by ``id``).

    Args:
        portfolio: The portfolio dict to write. Must have an ``id`` key.
    """
    portfolios = load_portfolios()
    for i, p in enumerate(portfolios):
        if p.get("id") == portfolio.get("id"):
            portfolios[i] = portfolio
            save_portfolios(portfolios)
            return
    portfolios.append(portfolio)
    save_portfolios(portfolios)


def delete_portfolio(pid: str) -> bool:
    """Remove a portfolio by id.

    Args:
        pid: Portfolio UUID.

    Returns:
        ``True`` if a portfolio was removed, ``False`` if no match was found.
    """
    portfolios = load_portfolios()
    new = [p for p in portfolios if p.get("id") != pid]
    if len(new) == len(portfolios):
        return False
    save_portfolios(new)
    return True


# ── portfolio targets ───────────────────────────────────────────────────────
# Per-portfolio exposure targets, used by the Targets tab on the
# portfolio page. Stored INSIDE the portfolio entry in portfolios.json
# (not in a separate file) so that delete_portfolio takes them along
# automatically and so the export/import surface stays one file.
#
# On-disk shape: the ``targets`` field on a portfolio dict is
#
#     {
#       "asset_class": {key: percent, ...},
#       "sector":      {key: percent, ...},
#       "country":     {region_key: percent, ...},   # REGION level
#       "currency":    {key: percent, ...},
#     }
#
# Three semantic points the helpers preserve:
#
# * Sparse by design. A facet with an empty dict, or a key absent from
#   a facet's dict, means "no target set" — NOT "target zero". The
#   deviation report renders untargeted buckets in a separate
#   summary line. Zero is a legitimate user-set target ("I want
#   exactly 0% energy") and is preserved as a literal entry.
#
# * Country facet is region-keyed. Targets are at the Morningstar
#   region level (10 buckets) per the design discussion, not at
#   mstar_country granularity. The deviation report aggregates the
#   portfolio's country rollup up to region before comparing.
#
# * Percentages, not fractions. Targets are stored as the same
#   numbers the user typed (0–100). The deviation function converts
#   to fractions internally before comparing against the rollup
#   (which uses fractions). Storing percents avoids float drift on
#   the values the user actually sees.

def _coerce_targets(raw) -> dict:
    """Coerce a user/disk-supplied targets dict into the canonical shape.

    Drops unknown facets, drops malformed entries, coerces percents to
    floats in [0, 100]. Negative percents are clamped to 0. Percents
    over 100 are kept (a 110% target is a user error worth surfacing
    rather than silently truncating).

    Args:
        raw: The candidate dict (may be partial, missing, or have
            extra keys).

    Returns:
        A new dict with the four facet keys present (some possibly
        empty) and only valid {key: percent} entries.
    """
    out: dict[str, dict] = {f: {} for f in BREAKDOWN_FACETS}
    if not isinstance(raw, dict):
        return out
    for facet in BREAKDOWN_FACETS:
        block = raw.get(facet)
        if not isinstance(block, dict):
            continue
        for k, v in block.items():
            if not isinstance(k, str) or not k.strip():
                continue
            try:
                pct = float(v)
            except (TypeError, ValueError):
                continue
            if pct < 0:
                pct = 0.0
            out[facet][k.strip()] = pct
    return out


def portfolio_targets_get(pid: str) -> dict:
    """Return the targets dict for portfolio ``pid``.

    Always returns a four-key dict; missing facets are empty.

    Args:
        pid: Portfolio UUID.

    Returns:
        ``{asset_class: {...}, sector: {...}, country: {...},
          currency: {...}}``. Returns an empty-targets dict if the
        portfolio has no targets set or doesn't exist.
    """
    p = find_portfolio(pid)
    if not p:
        return {f: {} for f in BREAKDOWN_FACETS}
    return _coerce_targets(p.get("targets"))


def portfolio_targets_put(pid: str, targets: dict) -> dict:
    """Replace the targets dict for portfolio ``pid``.

    Replace semantics: the existing ``targets`` field on the portfolio
    is overwritten in full. To clear, pass an empty dict (or a dict
    with all four facets empty).

    Args:
        pid: Portfolio UUID.
        targets: The new targets dict. Coerced via :func:`_coerce_targets`
            before persisting.

    Returns:
        The normalised, persisted dict (same shape as
        :func:`portfolio_targets_get`'s return).

    Raises:
        ValueError: If ``pid`` does not match any portfolio.
    """
    portfolios = load_portfolios()
    for p in portfolios:
        if p.get("id") == pid:
            normalised = _coerce_targets(targets)
            # Drop the field entirely when no facet has any target,
            # so portfolios.json stays clean for users who haven't
            # opted into the feature.
            if any(normalised.get(f) for f in BREAKDOWN_FACETS):
                p["targets"] = normalised
            else:
                p.pop("targets", None)
            save_portfolios(portfolios)
            return normalised
    raise ValueError(f"no portfolio with id {pid!r}")


def normalise_cache_config(cfg: dict | None) -> dict:
    """Coerce a user-supplied cache config into a complete, typed dict.

    Missing categories fall back to :data:`DEFAULT_CACHE_CONFIG`. Negative
    or non-integer ``ttl_days`` are clamped to 0.

    Two properties are NOT user-editable — they're structural to the
    category and always taken from :data:`DEFAULT_CACHE_CONFIG`,
    overriding anything in the incoming ``cfg``:

    * ``manual_refresh_only`` — the ``holdings`` slot is refreshed only
      by explicit user action; the flag is part of the category's
      identity, not a knob.
    * ``enabled`` for a ``manual_refresh_only`` category — disabling
      such a category would be actively harmful: a disabled ``holdings``
      slot is a guaranteed cache miss, so every fund load would refetch
      from Yahoo and OVERWRITE any manually-uploaded holdings list.
      There is no coherent "disabled" state for a manual-refresh-only
      slot, so ``enabled`` is pinned ``True``. ``ttl_days`` is still
      accepted and stored for schema uniformity but has no effect.

    Args:
        cfg: Partial cache config (any subset of categories).

    Returns:
        ``{<category>: {"enabled": bool, "ttl_days": int,
        "manual_refresh_only": bool}, ...}`` covering every category in
        :data:`CACHE_CATEGORIES`.
    """
    out: dict[str, dict[str, Any]] = {}
    cfg = cfg or {}
    for cat in CACHE_CATEGORIES:
        raw          = cfg.get(cat) or {}
        defaults     = DEFAULT_CACHE_CONFIG[cat]
        manual_only  = bool(defaults.get("manual_refresh_only", False))
        out[cat] = {
            # enabled is pinned True for manual-refresh-only categories
            # (see docstring); user-tunable for all others.
            "enabled":  True if manual_only
                        else bool(raw.get("enabled", defaults["enabled"])),
            "ttl_days": max(0, int(raw.get("ttl_days", defaults["ttl_days"]))),
            # Structural, not user-tunable — always from the defaults.
            "manual_refresh_only": manual_only,
        }
    return out


def portfolio_ticker_hint(isin: str, exchange: str | None
                          ) -> tuple[str, str] | None:
    """Look up an ISIN in existing portfolio entries to skip OpenFIGI.

    Under the 0.12.0 slim-portfolio model, each portfolio entry stores
    only ``{ticker, shares}`` — identity lives in the listings cache.
    For every ticker that appears in any portfolio, this consults the
    listings cache's identity block; if the recorded ISIN matches the
    one being resolved (and the MIC matches, when given), the ticker
    is a valid short-circuit and OpenFIGI is bypassed.

    Args:
        isin: ISIN to resolve.
        exchange: Optional MIC. When ``""`` / ``None``, any exchange
            the listings cache happens to record is accepted.

    Returns:
        ``(ticker, resolved_mic)`` on a hit, ``None`` otherwise.
    """
    isin_norm = (isin or "").strip().upper()
    if not isin_norm:
        return None
    exch_norm = (exchange or "").strip().upper()
    seen_tickers: set[str] = set()
    for p in load_portfolios():
        for f in p.get("funds", []):
            tk = (f.get("ticker") or "").strip().upper()
            if not tk or tk in seen_tickers:
                continue
            seen_tickers.add(tk)
            ident = listing_identity_get(tk)
            if (ident.get("isin") or "").upper() != isin_norm:
                continue
            t_exch = (ident.get("exchange") or "").upper()
            if exch_norm == "" or t_exch == exch_norm:
                return tk, t_exch or exch_norm
    return None


# ---------------------------------------------------------------------------
# Holdings rollup — moved to porxpy.breakdowns
# ---------------------------------------------------------------------------
# ``rollup_holdings`` and ``canonicalise_facet_key`` now live in
# :mod:`porxpy.breakdowns`, the pure derivation layer shared by the
# fund, holding, and portfolio breakdown paths. They are re-exported
# here so existing ``from porxpy.utils import ...`` call sites keep
# working unchanged.
from porxpy.breakdowns import (  # noqa: F401  (compatibility re-export)
    canonicalise_facet_key,
    rollup_holdings,
)


# ---------------------------------------------------------------------------
# Application settings (settings.json)
# ---------------------------------------------------------------------------
# Free-form JSON dict, single source of truth for app-level toggles. Read
# by both the API (/api/settings GET) and by extractors that need to
# decide whether to enrich top-10 holdings. The on-disk format mirrors
# the in-memory one — DEFAULT_SETTINGS in config.py defines the shape.
def normalise_settings(raw: dict | None) -> dict:
    """Coerce a (possibly partial) settings dict into the full shape.

    Missing keys fall back to :data:`porxpy.config.DEFAULT_SETTINGS`.
    Type errors degrade silently to defaults rather than raising.

    Legacy migration (0.12.6 → 0.12.7):
        Earlier versions stored a ``top10_enrichment`` block of the form
        ``{"enabled": bool, "threshold_pct": float}``. That block is no
        longer the source of truth; the new ``enrichment.fields`` list
        is. When a legacy ``top10_enrichment`` block is the only thing
        on disk, we honour the intent of the old setting by translating:

        * ``enabled: False``  →  empty fields list ("don't enrich anything")
        * ``enabled: True``   →  full :data:`ENRICHABLE_FIELDS` list

        The old threshold value is dropped — there is no longer a
        threshold concept. After the next save the legacy block is no
        longer written.

    Args:
        raw: Partial or full settings dict (typically from the request
            body or from disk).

    Returns:
        A complete, validated settings dict ready to be persisted or
        consumed by the enrichment logic.
    """
    raw = raw or {}

    # ── enrichment.fields ─────────────────────────────────────────────
    en_defaults = DEFAULT_SETTINGS["enrichment"]
    src = raw.get("enrichment")

    if isinstance(src, dict) and "fields" in src:
        # Modern shape — validate the list contents against the legal
        # ENRICHABLE_FIELDS set. Unknown keys are dropped silently,
        # preserving order of the legal ones the user actually set.
        raw_fields = src.get("fields") or []
        if not isinstance(raw_fields, list):
            raw_fields = []
        fields = [f for f in raw_fields if f in ENRICHABLE_FIELDS]
    elif isinstance(raw.get("top10_enrichment"), dict):
        # Legacy shape — translate. enabled=False → empty list (don't
        # enrich anything), enabled=True → full default list. The old
        # threshold_pct is intentionally not consulted; with the new
        # always-enrich-top-10 policy there's no threshold to honour.
        legacy = raw["top10_enrichment"]
        try:
            legacy_on = bool(legacy.get("enabled", True))
        except Exception:
            legacy_on = True
        fields = list(ENRICHABLE_FIELDS) if legacy_on else []
    else:
        fields = list(en_defaults["fields"])

    # ── holdings_match.key ────────────────────────────────────────────
    # Which field merges "the same holding" across funds on the
    # portfolio Holdings sub-tab. Unknown values fall back to the
    # default ("ticker") rather than raising.
    hm_src = raw.get("holdings_match") or {}
    hm_def = DEFAULT_SETTINGS["holdings_match"]
    match_key = hm_src.get("key", hm_def["key"])
    if not isinstance(match_key, str) or match_key not in HOLDINGS_MATCH_KEYS:
        match_key = hm_def["key"]

    return {
        "enrichment": {
            "fields": fields,
        },
        "holdings_match": {
            "key": match_key,
        },
    }


def load_settings() -> dict:
    """Load app settings from ``settings.json``.

    Returns the defaults if the file is missing or malformed — the caller
    never has to deal with a broken settings file.
    """
    if not SETTINGS_FP.exists():
        return normalise_settings(None)
    try:
        with open(SETTINGS_FP, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return normalise_settings(raw if isinstance(raw, dict) else None)
    except Exception as exc:
        print(f"[Settings] load error: {exc}; falling back to defaults")
        return normalise_settings(None)


def save_settings(settings: dict) -> dict:
    """Persist settings to disk after normalising.

    Args:
        settings: Partial or full settings dict.

    Returns:
        The normalised dict that was actually written, so the caller can
        echo it back to the client without a second read.
    """
    norm = normalise_settings(settings)
    try:
        with open(SETTINGS_FP, "w", encoding="utf-8") as f:
            json.dump(norm, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"[Settings] save error: {exc}")
    return norm
