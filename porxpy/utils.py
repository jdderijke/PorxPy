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
``resolve_facet_value``) now live in :mod:`porxpy.breakdowns` and are
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
    FACTSHEETS_DIR,
    FACTSHEET_EXTENSIONS,
    FACTSHEET_STALE_DAYS,
    LISTINGS_DIR,
    FUNDS_DIR,
    LISTING_CATEGORIES,
    FUND_CATEGORIES,
    BREAKDOWN_FACETS,
    SUPPLIED_BREAKDOWN_SOURCES,
    BREAKDOWN_SOURCES,
    DEFAULT_CACHE_CONFIG,
    DEFAULT_FUND_STRUCTURE,
    DEFAULT_SETTINGS,
    DISTRIBUTION_POLICIES,
    ENRICHABLE_FIELDS,
    FOCUS_TYPES,
    LEGACY_FOCUS_TYPES,
    MARKET_CAPS,
    STYLE_BOXES,
    FUND_STRUCTURES,
    FUND_STYLES,
    FX_HIST_TTL_HOURS,
    FX_TTL_HOURS,
    HOLDINGS_MATCH_KEYS,
    HOLDINGS_SOURCES,
    HOLDINGS_SOURCE_PRECEDENCE,
    HOLDINGS_SOURCE_VARIANT,
    holdings_source_of,
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
    UPLOAD_SOURCE_KINDS,
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
# 3 (v0.59.0): normalise_facets now resolves the country column at two
# levels and stamps country_node / country_level, mirroring sector.
# Caches stamped by generation 2 carry neither, so the region view would
# read every pre-existing row as unknown until something else happened
# to rewrite it.
# v0.70.0 — generation 4. The asset taxonomy became one tree, so every
# cached blob must re-fold its asset_class + sub_class pair into
# asset_node + asset_level and re-derive the three level columns. A row
# whose stored sub_class was an is_default fill rather than something
# the source said correctly becomes unknown at that level: the fill was
# never an assertion, and keeping it would carry a claim across the
# migration that nobody ever made.
# v0.70.0 — generation 5. Sector and country now store every level of
# their tree, as asset did from generation 4. A blob stamped 4 carries
# only the middle sector level and the country level, so a sub-sector,
# super-sector, region or super-region view of those rows would read as
# blank everywhere until something else happened to rewrite them. The
# stated value and its grain were already stored, so the migration
# re-derives rather than guesses: no row loses or gains an assertion.
# v0.76.0 — generation 6. Facet resolution now reads from <facet>_raw
# rather than from the stored node, so a blob written before this release
# has no input to resolve from. There is nothing to re-derive and nothing
# honest to guess, so the affected categories are DROPPED instead of
# migrated — see _drop_legacy_facet_stores.
_FACET_MIGRATOR_GENERATION = 6


def holdings_blobs_in(value: Any) -> list[dict]:
    """Every per-source holdings blob inside a ``holdings`` cache value.

    Accepts both shapes — the pre-0.77.0 single blob and the source map
    that replaced it — so the migration passes that walk stored rows do
    not each need to know which era a file was written in. Returned in
    :data:`~porxpy.config.HOLDINGS_SOURCES` order where the shape is the
    map, so a caller reporting on them is deterministic.
    """
    if not isinstance(value, dict):
        return []
    sources = value.get("sources")
    if isinstance(sources, dict):
        return [sources[s] for s in HOLDINGS_SOURCES
                if isinstance(sources.get(s), dict)]
    return [value] if isinstance(value.get("rows"), list) else []


def _drop_legacy_facet_stores(data: dict) -> list[str]:
    """Delete cached facet data that predates raw-as-input (v0.76.0).

    Two stores resolved their facet values at INGEST and kept only the
    canonical result: holdings rows (the raw was overwritten in the level
    column) and supplied breakdowns (the item's ``key`` was the resolved
    node). Neither can be re-resolved now that resolution reads from the
    raw text, and neither can have a raw invented for it — feeding a
    stored conclusion back in as if it were the source is precisely the
    loop that made an alias edit unable to reach a stored row.

    They are dropped rather than migrated. ``cache/`` is losable by
    design: a holdings upload and a factsheet extraction are both
    repeatable, so the cost is one re-import per fund, against a
    compatibility branch that would sit in this module permanently and
    be exercised by fewer rows every month while quietly behaving
    differently from the main path.

    Nothing outside ``cache/`` is touched. Portfolios, targets,
    overrides and the ISIN map are user state and hold no facet
    resolution of their own.

    Args:
        data: A fund-cache blob. Modified in place.

    Returns:
        The category names that were dropped, for the caller to record
        so the UI can tell the user what to re-import.
    """
    from porxpy.config import BREAKDOWN_FACETS, facet_raw_field

    dropped: list[str] = []

    holdings = (data.get("holdings") or {}).get("value") \
        if isinstance(data.get("holdings"), dict) else None
    if isinstance(holdings, dict):
        raw_fields = [facet_raw_field(f) for f in BREAKDOWN_FACETS]

        def _is_legacy(blob: dict) -> bool:
            rows = blob.get("rows")
            if not isinstance(rows, list) or not rows:
                return False
            # "Carries no raw column at all", not "its raw is empty". A
            # row whose source genuinely said nothing about any facet has
            # the keys present and blank; a legacy row has never heard of
            # them.
            return not any(any(rf in r for rf in raw_fields)
                           for r in rows if isinstance(r, dict))

        # v0.77.0 — one source's rows can be legacy while another's are
        # not (a 2025 upload beside a factsheet read last week), so the
        # drop is per source. Dropping the whole slot for one bad source
        # would take answers with it that are perfectly readable.
        sources = holdings.get("sources")
        if isinstance(sources, dict):
            gone = [src for src, blob in list(sources.items())
                    if isinstance(blob, dict) and _is_legacy(blob)]
            for src in gone:
                del sources[src]
            if gone:
                dropped.append("holdings")
                if not sources:
                    data.pop("holdings", None)
        elif _is_legacy(holdings):
            data.pop("holdings", None)
            dropped.append("holdings")

    ub = (data.get("uploaded_breakdowns") or {}).get("value") \
        if isinstance(data.get("uploaded_breakdowns"), dict) else None
    if isinstance(ub, dict):
        legacy = False
        for per_source in ub.values():
            if not isinstance(per_source, dict):
                continue
            for items in per_source.values():
                for it in (items or []):
                    if isinstance(it, dict) and "raw" not in it:
                        legacy = True
                        break
        if legacy:
            data.pop("uploaded_breakdowns", None)
            dropped.append("uploaded_breakdowns")

    return dropped


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
    from porxpy.resources import RESOURCE_FINGERPRINTS
    if not isinstance(data, dict) or not data:
        return False
    # Fingerprints alone since v0.64.0. The declared ``Version=N`` was
    # removed: it only moved when a writer remembered to bump it, so a
    # file edited by hand kept its old number and every cache stamped
    # with it believed itself current. The fingerprint changes whenever
    # the bytes do, which is the question actually being asked.
    #
    # A stamp written before v0.64.0 carries the version keys too, so it
    # cannot equal this dict and the cache re-normalises once. That is
    # the correct migration and needs no special case.
    current_versions = dict(RESOURCE_FINGERPRINTS)
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

    # Before anything is re-derived: a blob written before v0.76.0 has
    # facet stores with no raw text to derive FROM. Dropping them first
    # means the loops below never see a row they would resolve to blank.
    legacy_dropped = _drop_legacy_facet_stores(data)
    if legacy_dropped:
        touched = True

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
    for hold_blob in holdings_blobs_in(hold_val):
        rows = hold_blob.get("rows") or []
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

    # A fund_breakdowns re-normalisation loop lived here until v0.60.0,
    # written speculatively against a cache category that has never
    # existed: build_fund_breakdowns runs at request time and its output
    # is never persisted. It was dead on arrival, and v0.59.0 made it
    # dead twice over — facet values are now resolved on every read, so
    # there is nothing for a migration to repair. Removed rather than
    # updated to the levelled block shape, which would have been effort
    # spent keeping unreachable code plausible.

    # Always update the stamp — even when nothing changed, this
    # records that we checked, so subsequent reads short-circuit.
    from datetime import datetime, timezone
    data["_normalisation"] = {
        "is_normalized": True,
        "versions":      current_versions,
        "generation":    _FACET_MIGRATOR_GENERATION,
        "normalised_at": datetime.now(timezone.utc).isoformat(),
    }
    # Kept on the blob rather than only logged: the fund page has to be
    # able to say WHY its holdings went empty, and "re-upload them" is
    # only actionable if the screen knows that is what happened.
    if legacy_dropped:
        data["_legacy_purge"] = {
            "categories": legacy_dropped,
            "at":         datetime.now(timezone.utc).isoformat(),
            "reason":     "stored before v0.76.0 without a raw facet value",
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


def _legacy_purge_settle(data: dict) -> None:
    """Retire the ``_legacy_purge`` notice for categories that are back.

    The v0.76.0 migration drops any cached category stored before every
    facet kept the source's own wording, and leaves a marker naming what
    it dropped so the fund page can say why the screen went empty. The
    marker has to go away again once the user has acted on it, and the
    only honest signal for that is the category reappearing in the blob.

    Done here, in the one function every writer goes through, rather
    than at each of the eight ``cache_write``/``cache_put`` call sites.
    A per-caller clear is a per-caller thing to forget, and a stale
    "re-import your holdings" banner over holdings that are visibly
    present is worse than never having shown one.

    Args:
        data: The full cache blob about to be written. Modified in
            place; blobs without a marker are left untouched.
    """
    marker = data.get("_legacy_purge")
    if not isinstance(marker, dict):
        return
    remaining = [c for c in (marker.get("categories") or []) if c not in data]
    if not remaining:
        data.pop("_legacy_purge", None)
    else:
        marker["categories"] = remaining


def cache_write(key: str, category: str, data: dict) -> None:
    """Persist a cache blob to disk.

    Args:
        key: See :func:`cache_read`.
        category: See :func:`cache_read`.
        data: The full cache blob (all categories at this key) to write.
            The blob is overwritten in place — callers should
            read-modify-write.
    """
    _legacy_purge_settle(data)
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


# ---------------------------------------------------------------------------
# The holdings store (v0.77.0)
# ---------------------------------------------------------------------------
# One ISIN-keyed ``holdings`` cache slot, holding one blob PER SOURCE:
#
#   {"holdings": {"fetched_at": "...", "value": {
#       "sources": {
#           "yahoo":     {"rows": [...], "source": "yahoo_enriched", ...},
#           "factsheet": {"rows": [...], "source": "factsheet", ...},
#           "upload":    {"rows": [...], "source": "manual_upload", ...}}}}}
#
# Each per-source blob keeps exactly the shape the single blob had before
# 0.77.0 — rows, variant, counts, provenance, timestamps — so every
# consumer downstream of the accessors below is unchanged. What changed is
# that a write no longer destroys the other sources' answers: uploading a
# CSV used to overwrite the Yahoo rows outright, which is why "go back to
# what Yahoo says" was a refetch rather than a click.
#
# Which source is IN EFFECT is not stored here. It is a fund-level
# override (``holdings_source`` in overrides.json), applied on read, so it
# obeys the same rule as every other override: a view over the fetched
# data, never a mutation of it. With no pin, HOLDINGS_SOURCE_PRECEDENCE
# decides, richest source first.
#
# Every reader and writer in the app goes through the six functions below
# rather than reaching into the slot, so the on-disk shape is stated once.


def _migrate_holdings_value(val: dict | None) -> tuple[dict, bool]:
    """Rewrite a pre-0.77.0 single-blob holdings value to the source map.

    Detection is by SHAPE, as in :func:`_migrate_supplied_breakdowns`: the
    old value is a blob with ``rows``/``source`` at the top level, the new
    one has a ``sources`` dict and nothing else. A shape test converges —
    running it twice is a no-op — whereas a version marker would have to
    be written by a release that never wrote one.

    Migration rather than a purge because the blob being converted may be
    a manual upload, and a manual upload is the one thing in the cache
    that cannot be re-fetched: purging it would destroy user data whose
    only copy is the file they no longer have.

    Returns:
        ``({"sources": {source: blob}}, changed)``.
    """
    val = val if isinstance(val, dict) else {}
    if isinstance(val.get("sources"), dict):
        sources = {
            src: blob for src, blob in val["sources"].items()
            if src in HOLDINGS_SOURCES and isinstance(blob, dict)
        }
        return {"sources": sources}, False
    variant = str(val.get("source") or "").strip()
    src = holdings_source_of(variant)
    if not src:
        # No variant at all - an empty or unrecognisable slot. Nothing to
        # keep, and nothing lost by saying so.
        return {"sources": {}}, bool(val)
    return {"sources": {src: dict(val)}}, True


def holdings_store_get(isin: str) -> dict:
    """Every source's holdings blob for ``isin``, as ``{source: blob}``.

    Only sources that have actually been written are present, so a
    caller can ask "does this fund have factsheet holdings?" by looking
    for the key. Pre-0.77.0 entries are migrated on read and written back,
    exactly as ``uploaded_breakdowns_get`` does for its own store.
    """
    isin_u = (isin or "").strip().upper()
    if not isin_u:
        return {}
    blob  = cache_read(isin_u, "holdings")
    entry = blob.get("holdings")
    val   = entry.get("value") if isinstance(entry, dict) else None

    migrated, changed = _migrate_holdings_value(val)
    if changed:
        blob["holdings"] = {
            "fetched_at": (entry or {}).get("fetched_at") or now_iso(),
            "value":      migrated,
        }
        cache_write(isin_u, "holdings", blob)
    return dict(migrated.get("sources") or {})


def holdings_sources_available(isin: str, store: dict | None = None) -> dict:
    """``{source: bool}`` - which sources this fund HAS holdings from.

    "Has" means the slot has been written, not that it carries rows. A
    Yahoo fetch that came back empty is a real answer ("Yahoo publishes
    no holdings for this fund") and the tile says so, exactly as an
    extracted factsheet that lists no positions answers the tile with
    "the document does not say" rather than with an absence. The one
    exception is a slot with neither rows nor a variant, which
    :func:`_migrate_holdings_value` has already dropped.
    """
    store = holdings_store_get(isin) if store is None else store
    return {src: src in store for src in HOLDINGS_SOURCES}


def holdings_active_source(isin: str, store: dict | None = None) -> str:
    """Which source's holdings are in effect for ``isin``.

    The user's pin when they have one AND that source exists; otherwise
    the first of :data:`~porxpy.config.HOLDINGS_SOURCE_PRECEDENCE` that
    does. A pin to a source the fund has since lost falls back the same
    way a breakdown card does rather than showing an empty tile - the
    pin itself is left alone, so restoring the source restores the view.

    Returns:
        One of :data:`~porxpy.config.HOLDINGS_SOURCES`, or ``""`` when
        the fund has no holdings from any source.
    """
    store = holdings_store_get(isin) if store is None else store
    pinned = (override_get(isin, "holdings_source") or "").strip()
    if pinned in store:
        return pinned
    # Precedence considers only sources that carry rows first. A slot can
    # exist and be empty — Yahoo publishes no top-10 for most European
    # UCITS ETFs, and a factsheet may print no position table at all —
    # and letting an empty higher-precedence slot win would hide a list
    # the fund actually has behind one it does not. The empty slot is
    # still selectable by hand; it is just not what an unpinned fund
    # falls into.
    for src in HOLDINGS_SOURCE_PRECEDENCE:
        if (store.get(src) or {}).get("rows"):
            return src
    for src in HOLDINGS_SOURCE_PRECEDENCE:
        if src in store:
            return src
    return ""


def holdings_get(isin: str) -> tuple[dict, str, dict]:
    """The holdings blob in effect for ``isin``.

    The single read path for "what are this fund's holdings?" - used by
    the fund load, the row editor, the enrichment button, the status
    badges and every card rebuild, so none of them can disagree about
    which source is showing.

    Returns:
        ``(blob, source, store)`` where ``blob`` is the active source's
        blob (``{}`` when the fund has none), ``source`` is its key in
        :data:`~porxpy.config.HOLDINGS_SOURCES` (``""`` when there is no
        blob) and ``store`` is every source's blob, so a caller that also
        needs availability does not read the slot twice.
    """
    store  = holdings_store_get(isin)
    active = holdings_active_source(isin, store)
    return (dict(store.get(active) or {}), active, store)


def holdings_put(isin: str, blob: dict, source: str | None = None,
                 store: dict | None = None) -> dict:
    """Write one source's holdings blob, leaving the other sources alone.

    Args:
        isin: Fund ISIN (normalised to upper).
        blob: The per-source blob - rows, variant, counts, provenance.
        source: Which source this is. Defaults to the source the blob's
            own variant belongs to, so a caller that has already stamped
            ``source: "manual_upload"`` need not say "upload" twice.
        store: A store already read by the caller, to save a re-read.

    Returns:
        The cache metadata dict from :func:`cache_put`.

    Raises:
        ValueError: The source is not one of
            :data:`~porxpy.config.HOLDINGS_SOURCES`, or could not be
            worked out from the blob.
    """
    isin_u = (isin or "").strip().upper()
    if not isin_u:
        raise ValueError("holdings_put requires an ISIN")
    src = (source or holdings_source_of(blob.get("source") or "")).strip()
    if src not in HOLDINGS_SOURCES:
        raise ValueError(f"source must be one of {list(HOLDINGS_SOURCES)}")
    # A blob always names its own variant, so a slot read back in
    # isolation still says what kind of rows it holds.
    if not holdings_source_of(blob.get("source") or ""):
        blob = dict(blob, source=HOLDINGS_SOURCE_VARIANT[src])

    merged = dict(holdings_store_get(isin_u) if store is None else store)
    merged[src] = blob
    return cache_put(isin_u, "holdings", {"sources": merged})


def holdings_delete_source(isin: str, source: str) -> bool:
    """Drop one source's holdings for ``isin``; the others survive.

    Also clears a pin left dangling by the removal, so the stored state
    matches what :func:`holdings_active_source` would fall back to
    anyway - the same rule ``_clear_supplied_source`` applies to a
    breakdown card whose source has gone.

    Returns:
        True if anything was removed.
    """
    isin_u = (isin or "").strip().upper()
    src = (source or "").strip()
    if not isin_u or src not in HOLDINGS_SOURCES:
        return False
    store = holdings_store_get(isin_u)
    if src not in store:
        return False
    del store[src]
    cache_put(isin_u, "holdings", {"sources": store})
    if (override_get(isin_u, "holdings_source") or "") == src:
        override_delete(isin_u, "holdings_source")
    return True


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

    The blob reported is the one IN EFFECT — the source the user pinned,
    or the richest they have (see :func:`holdings_get`). A badge that
    described a source the fund page was not showing would be worse than
    no badge, so the two answer from the same accessor.

    Its ``source`` field records which degree of completeness the cached
    rows represent:

        * ``"manual_upload"``  — full per-position list from a user file
        * ``"yahoo_enriched"`` — Yahoo top-10 enriched via per-symbol
          lookups (country / currency / sector / asset class filled)
        * ``"yahoo_top10"``    — raw Yahoo top-10, sparse rows
        * ``"factsheet"``      — positions read off an uploaded factsheet

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
            "source": str,            # the blob's variant, or ""
            "source_key": str,        # yahoo / factsheet / upload, or ""
            "available": dict,        # {source: bool} across all three
            "top_count": int,         # cached holdings row count
            "top_sum_pct": float|None,# sum of cached row weights, or None
            "would_enrich": bool,     # always False since v0.5.0
        }``.
    """
    if not isin:
        return {"has_full": False, "full_count": 0, "source": "",
                "source_key": "",
                "available": {s: False for s in HOLDINGS_SOURCES},
                "top_count": 0, "top_sum_pct": None, "would_enrich": False}

    hold_blob, source_key, store = holdings_get(isin)

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
        "source_key":   source_key,
        "available":    holdings_sources_available(isin, store),
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
#
# Levelled facets (v0.70.0) — asset, sector and country each occupy
# THREE columns, one per level of their tree, and currency occupies one
# because its tree is one level deep. Every level is present on every
# row, filled or blank; none of them is the row's answer. The answer is
# the stated value in ``<facet>_node`` plus its grain in
# ``<facet>_level``, carried alongside as metadata, and normalise_facets
# re-derives all three columns from it on every read.
#
# The columns exist so that a consumer is never handed one level and
# asked to infer the others: the holdings tables and breakdown cards
# choose which level to display, and cannot choose what they were not
# given. Blank at a level means the tree does not reach it — either the
# source stated something coarser, or the node has siblings so deriving
# downward would be a guess.
#
# Asset gained its three in v0.70.0's first pass; sector and country
# kept storing only their middle level until the second, which is why
# no table could offer a region or sub-sector view.
#
# ``<facet>_raw`` (v0.76.0) sits beside each facet's level columns and is
# the odd one out: every other column here is a DERIVATION, rewritten by
# normalise_facets on every read, while the raw is what the source
# actually said and is written exactly once, by whichever writer ingested
# the row. It is a schema column rather than metadata because the
# holdings tables offer it as a display level — see FACET_DISPLAY_LEVELS
# in config.py for why it is a display level and not a tree level.
#
# The resolution metadata that is NOT displayable — ``<facet>_node``,
# ``<facet>_level``, ``<facet>_pinned`` — stays off this list and rides
# along as extras, which coerce_holdings_row preserves.
HOLDINGS_ROW_FIELDS: tuple[str, ...] = (
    "name", "ticker", "isin", "cusip",
    "sector", "sub_sector", "super_sector", "sector_raw",
    "asset_class", "sub_class", "super_class", "asset_raw",
    "country", "region", "super_region", "country_raw",
    "currency", "currency_raw", "weight_pct",
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
# A holding's asset value is a node in the Asset_definitions.csv tree —
# the same tree a fund's breakdown uses, since v0.70.0. The old
# four-value enum (equity / bond / cash / other) is gone: it was the
# same taxonomy at a different grain, spelled differently, which is
# what made "bond" and "fixed_income" two names for one thing.
#
# What remains here is the bridge between two GENUINELY different
# facets: a fund's primary_asset_class (what kind of fund this is) and
# the asset tree (what a position holds). A fund classified equity whose
# holdings carry no class of their own has equity holdings — that is an
# inference across facets, not a second vocabulary, so an explicit table
# is the honest way to state it.
_FUND_TO_HOLDING_ASSET_CLASS: dict[str, str] = {
    "equity":       "equity",         # super_class
    "fixed_income": "fixed income",   # super_class
    # cash maps to the asset_class node, not its super class "liquid" —
    # the same choice the stored-target migration makes, and for the
    # same reason: "cash" is what the user means, and liquid's only
    # child is cash so the two are numerically identical anyway.
    "cash":         "cash",
    # mixed / commodity / other → "other" (via the .get default below)
}

# Recognised spellings for a holding's asset class → canonical lowercase
# key. Covers Yahoo's quoteType-derived labels (which may arrive as
# "Equity", "Fixed Income", lowercase, etc.) and the fund-level keys, so
# whatever a holdings source hands us collapses to the 4-value enum.
def normalize_holding_asset_class(raw: str | None) -> str:
    """Collapse any asset-class spelling to the holding enum, or ``""``.

    Every spelling comes from the ``matches`` column of
    ``Asset_definitions.csv``, so adding one is a file edit rather than
    a code change.

    Answers at the asset facet's DEFAULT level, which is what the
    callers of this function want — a single coarse label for a
    position. Anything needing the grain the source actually stated
    reads the row's ``asset_level`` instead of calling this.

    Args:
        raw: Any asset-class string, any casing, or ``None``/blank.

    Returns:
        A canonical node at the facet's default level, or ``""`` when
        ``raw`` is blank, matches nothing, or resolves only at a finer
        level than the default.

        An unrecognised value returning ``""`` rather than ``"other"`` is
        deliberate. ``"other"`` is a real classification a file can
        legitimately assert, and answering it for anything unrecognised
        hid every unknown spelling from the Resolve dialog: the value
        looked classified, so nothing ever asked about it.
    """
    if raw is None:
        return ""
    key = str(raw).strip().lower()
    if not key:
        return ""
    from porxpy.resources import resolve_asset_tree     # local: avoid cycle
    from porxpy.config import FACET_DEFAULT_LEVEL
    tree = resolve_asset_tree(key)
    if not tree.get("level"):
        return ""
    val = tree.get(FACET_DEFAULT_LEVEL.get("asset_class", "super_class")) or ""
    return "" if val in ("unknown", "n/a") else val


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
# normalise_facets — single chokepoint for facet resolution (v0.15.0,
# rebuilt on raw-as-input in v0.76.0)
# ---------------------------------------------------------------------------
# Every facet value that enters PorxPy — from Yahoo, a user upload, a
# manual edit, or a cache file — passes through this function before it
# is written to disk.
#
# **What changed in v0.76.0, and why.** Until this release the function
# resolved the source's text once and stored the CANONICAL node, keeping
# the raw text only when it failed to resolve. That made the row's own
# answer un-re-derivable: a row that matched through the alias
# "Aandelen" stored "equity", so re-pointing that alias to another node
# changed every future import and no stored row, and two funds imported
# either side of the edit disagreed about the same source text with
# nothing on screen saying why. Re-normalising could not repair it,
# because by then the only input left was a canonical, and a canonical
# always resolves to itself.
#
# So the row now stores the source's text in ``<facet>_raw``, and that
# column is the INPUT to every subsequent resolution. ``<facet>_node``,
# ``<facet>_level`` and the level columns are all derivations, rewritten
# from the raw on every pass. An alias edit therefore reaches every
# stored row on its next read, which is the behaviour the resource files
# were always documented as having.
#
# ``<facet>_raw`` is the one column this function must never write. It
# is evidence, not a conclusion; the writers that ingest a row (the
# upload commit, Yahoo enrichment, the row editor) capture it once and
# it stays as the source left it.
#
# **The pin.** A user who edits a facet on selected rows is overruling
# the source, not correcting the vocabulary. Re-deriving from the
# untouched raw text on the next read would silently undo that, so such
# an edit also sets ``<facet>_pinned`` and the node is then taken as
# given. The level columns are still re-derived from the pinned node, so
# a re-parented tree still migrates a pinned row — the pin freezes which
# node the row means, not what that node means.
#
# The function stays idempotent: a second call resolves the same raw
# text to the same node and adds nothing to ``_unmatched_facets``. Cache
# reads call it eagerly to stamp the file with the current resource
# fingerprints.


def _resolve_facet_tree(facet: str, value: str) -> dict:
    """Resolve one value through ``facet``'s tree, in one uniform shape.

    Why this exists: the three levelled facets already answer with
    ``{matched, level, <one key per level>}`` while currency answers
    with a bare code. Adapting currency to the same shape is what lets
    :func:`normalise_facets` be a loop over the four facets rather than
    four hand-written blocks — which is not hypothetical tidiness, it is
    the exact drift those blocks produced: asset stored all three of its
    levels from v0.70.0's first pass while sector and country stored
    only their middle one until the second, purely because each block
    was maintained separately.

    Args:
        facet: One of :data:`~porxpy.config.BREAKDOWN_FACETS`.
        value: The text to resolve.

    Returns:
        ``{"matched": node, "level": grain, <level>: key, ...}``, with
        ``level`` empty when the value named nothing in the vocabulary.
    """
    from porxpy.resources import (
        resolve_asset_tree, resolve_country_tree, resolve_currency,
        resolve_sector_tree,
    )
    from porxpy.breakdowns import UNKNOWN_KEY

    if facet == "sector":
        return resolve_sector_tree(value)
    if facet == "country":
        return resolve_country_tree(value)
    if facet == "asset_class":
        return resolve_asset_tree(value)
    if facet == "currency":
        code = resolve_currency(value)
        # One level deep, so the node IS the level column and there is
        # no grain to report beyond whether it resolved at all.
        return {"currency": code or UNKNOWN_KEY,
                "level":    "currency" if code else "",
                "matched":  code or ""}
    raise KeyError(f"no resolver for facet {facet!r}")


def normalise_facets(row: dict | None) -> tuple[dict, list[str]]:
    """Re-derive every facet's node and level columns from its raw value.

    Args:
        row: Any dict-shaped row. Modified in place; also returned for
            chaining. Fields outside the four facets' column packs pass
            through untouched.

    Returns:
        ``(row, unmatched_facets)`` where ``unmatched_facets`` names the
        facets whose raw value did not resolve. The same list is stored
        on ``row["_unmatched_facets"]`` (sorted, de-duplicated) for the
        Resolve-unmatched-values dialog.
    """
    from porxpy.breakdowns import UNKNOWN_KEY
    from porxpy.config import (
        BREAKDOWN_FACETS, FACET_LEVELS, facet_level_field, facet_node_field,
        facet_pinned_field, facet_raw_field,
    )

    if row is None:
        return {}, []

    unmatched: list[str] = []

    for facet in BREAKDOWN_FACETS:
        raw_f   = facet_raw_field(facet)
        node_f  = facet_node_field(facet)
        level_f = facet_level_field(facet)
        pin_f   = facet_pinned_field(facet)
        levels  = FACET_LEVELS[facet]

        # A pinned row takes its node as given; everything else resolves
        # from the source text. The pin is read from its own column
        # rather than inferred from the raw and the node disagreeing: a
        # source whose text differs from the node it resolved to is the
        # ordinary case, not a user decision.
        pinned = bool(row.get(pin_f))
        stated = ((row.get(node_f) if pinned else row.get(raw_f)) or "").strip()

        def _blank() -> None:
            row[node_f] = ""
            if level_f:
                row[level_f] = ""
            for lv in levels:
                row[lv] = ""

        if not stated:
            _blank()
            continue

        tree = _resolve_facet_tree(facet, stated)
        if not tree["level"]:
            # The source said something and it named nothing in the
            # vocabulary. Every derived column goes blank — the row has
            # no node, and writing the raw text into a level column (as
            # this function did until v0.76.0) made an unresolved value
            # look like a bucket of its own to anything reading the
            # column directly. The evidence is safe in <facet>_raw, and
            # the facet is reported so the Resolve dialog can offer it.
            #
            # A pinned row can land here too, when the file has since
            # renamed or dropped the node the user chose. The pin is
            # deliberately kept: their decision has not become wrong,
            # its target has gone missing, and surfacing it as unmatched
            # is what lets them re-point it.
            _blank()
            unmatched.append(facet)
            continue

        row[node_f] = tree["matched"]
        if level_f:
            row[level_f] = tree["level"]
        # "" not "unknown" where the tree does not reach: an empty column
        # is what every consumer reads as "none", and the residual keys
        # are a breakdown-bucket concept rather than a row-field one.
        for lv in levels:
            row[lv] = "" if tree[lv] == UNKNOWN_KEY else tree[lv]

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
      the asset tree, which fills levels below a stated value only
      where the tree gives exactly one answer. If
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
    #   * folds the asset columns into one tree, storing the value the
    #     source stated plus its grain, and deriving the other levels
    #   * resolves sector / currency / country to their canonical CSV
    #     values via the matches alias lists
    #   * preserves the raw value (rather than blanking) for any
    #     facet that doesn't resolve, and records the field in
    #     row["_unmatched_facets"] for the user-facing review dialog
    normalise_facets(out)

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
    from porxpy.resources import country_to_mstar, resolve_currency

    raw = raw or {}
    out: dict[str, Any] = {}

    # Carry over non-schema extras first so the canonical fields below
    # always win on key collisions — the same rule coerce_holdings_row
    # applies, and now for the same reason. CASH_POSITION_FIELDS lists
    # display columns, not the levelled facets' stated values, so
    # rebuilding a position from that list alone dropped sector_node,
    # country_node and asset_node on every read. The fold then had only
    # a derivation to re-resolve from: a position stated at sub-sector
    # level came back one level coarser each time it was read, and one
    # stated at region level came back EMPTY, because the country column
    # of such a row is legitimately blank and there was nothing else
    # left to read.
    for k, v in raw.items():
        if k in CASH_POSITION_FIELDS or k == "id":
            continue
        out[k] = v

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

    # No sub-class defaulting here any more. normalise_facets derives
    # every level of the asset tree from the one value the source
    # stated, and derives DOWNWARD only where the answer is not a guess
    # (a node with exactly one child). Filling a sub class from
    # is_default wrote an assertion nobody made, indistinguishable
    # afterwards from one the source had actually given.

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
    if not isinstance(val, dict):
        return None
    # A cached "we found nothing" gets the short TTL, for the same
    # reason a negative alias does: the thing that produced it may have
    # been a rate limit or an outage rather than a fact about the
    # security. A positive entry keeps the full 90 days, because an
    # answer Yahoo actually gave is worth remembering.
    if not val.get("_found", True):
        from porxpy.config import NEGATIVE_ALIAS_TTL_DAYS
        if age > NEGATIVE_ALIAS_TTL_DAYS:
            return None
    return val


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
def _negative_alias_ttl(misses: int) -> float:
    """Days a negative entry is believed, given how often it has missed.

    Doubles per consecutive miss and is capped, so one unlucky probe
    costs a week while a genuinely unrecognisable spelling settles into
    silence instead of being re-probed forever.
    """
    from porxpy.config import (NEGATIVE_ALIAS_TTL_DAYS,
                               NEGATIVE_ALIAS_TTL_CAP_DAYS)
    n = max(1, int(misses or 1))
    return min(NEGATIVE_ALIAS_TTL_DAYS * (2 ** (n - 1)),
               NEGATIVE_ALIAS_TTL_CAP_DAYS)


def _negative_alias_expired(stamped_at: str | None, misses: int = 1) -> bool:
    """True when a negative alias is older than its escalating TTL.

    A malformed or missing timestamp counts as expired: an entry we
    cannot date is an entry we cannot vouch for, and re-probing costs
    one lookup while trusting it wrongly costs the holding forever.
    """
    if not stamped_at:
        return True
    try:
        t = datetime.fromisoformat(str(stamped_at))
    except (TypeError, ValueError):
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    return age > _negative_alias_ttl(misses)


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
        resolved = val.get("resolved")
        # A NEGATIVE entry expires; a positive one never does. See
        # config.NEGATIVE_ALIAS_TTL_DAYS for why the two are not
        # equally durable. An expired negative is reported as "never
        # probed", so the caller re-probes rather than trusting a miss
        # that may have been a rate limit or an outage.
        if resolved is None and _negative_alias_expired(
                val.get("stamped_at"), val.get("misses", 1)):
            return False, None
        return True, resolved
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
    entry = {"resolved": resolved, "stamped_at": now_iso()}
    if resolved is None:
        # Consecutive misses drive the escalating TTL. A hit clears the
        # count by simply not carrying it, so a spelling that starts
        # working is forgiven its history.
        prev = blob.get(raw)
        prior = prev.get("misses", 0) if isinstance(prev, dict) else 0
        entry["misses"] = int(prior or 0) + 1
    blob[raw] = entry
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

# ---------------------------------------------------------------------------
# Per-field override store (v0.33.0)
# ---------------------------------------------------------------------------
# overrides.json is ISIN-keyed; each entry is a flat table of field name →
# envelope. See :data:`porxpy.config.OVERRIDABLE_FIELDS` for what may be
# asserted and how each value is validated.
#
#   {"IE00B4L5Y983": {
#       "expenseRatioPct": {"value": 0.22, "source": "user",
#                           "ts": "2026-07-26T…", "note": "KIID"},
#       "market_cap":      {"value": "large", "source": "user", "ts": "…"},
#       "breakdown_source.sector": {"value": "holdings", ...}}}
#
# The table is SPARSE. A field is present exactly when the user (or a
# one-shot lookup like justETF) asserted it; absence means "no opinion"
# and the Yahoo/derived seed shows through. Nothing ever stores a value
# meaning "unset".
#
# What must NOT go in here: anything the load pipeline recomputes. A
# name-derived market cap written to this table would survive a fund
# rename for ever, and the UI's "inferred from fund name" caption would
# become a lie. Yahoo values and name-derived values stay runtime-only;
# this file holds what has to survive a refetch.

# Envelope fields carried alongside the value. ``source`` says who
# asserted it, not where the number originally came from.
OVERRIDE_SOURCES: tuple[str, ...] = ("user", "justetf", "csv",
                                    "factsheet", "yahoo")


def _ov_key(isin: str) -> str:
    """Normalise an ISIN to the canonical override-store key."""
    return (isin or "").strip().upper()


# Legacy top-level keys, pre-0.33.0.
_LEGACY_OV_KEYS = ("asset_class", "breakdown_source",
                   "include_in_optimizer", "fund_structure")


def _is_envelope(v) -> bool:
    """True for a v0.33.0 override envelope."""
    return isinstance(v, dict) and "value" in v


def _is_legacy_entry(entry: dict) -> bool:
    """True when an ISIN entry predates the flat per-field table.

    Detection is by SHAPE, not by key name. Two of the legacy top-level
    keys — ``asset_class`` and ``include_in_optimizer`` — are also field
    names in the new registry, so "does this entry contain the key
    ``asset_class``" stays true after a successful migration and the
    migration re-runs on every single load, rewriting the file each time.
    An envelope is a dict carrying ``value``; a legacy value is a bare
    string, bool or nested dict. That difference converges.
    """
    return any(not _is_envelope(v) for v in entry.values())


def _migrate_override_entry(entry: dict) -> dict:
    """Rewrite one pre-0.33.0 ISIN entry into the flat envelope table.

    Legacy values equal to their registry ``neutral`` are DROPPED rather
    than carried across. In the old dense blocks a neutral value meant
    "the user expressed no opinion" — migrating it as an assertion would
    pin the field to that value permanently, which is exactly the bug the
    sparse table exists to make impossible.
    """
    from porxpy.config import OVERRIDABLE_FIELDS

    out: dict[str, dict] = {}

    def _add(field: str, value) -> None:
        spec = OVERRIDABLE_FIELDS.get(field)
        if spec is None or value is None:
            return
        if value == spec.get("neutral"):
            return
        out[field] = {"value": value, "source": "user",
                      "ts": now_iso(), "note": "migrated from pre-0.33 overrides"}

    ac = entry.get("asset_class")
    if isinstance(ac, str):
        # Pre-0.33 blocks called it "asset_class"; the registry field is
        # "primary_asset_class" since 0.47.0.
        _add("primary_asset_class", ac)
    inc = entry.get("include_in_optimizer")
    if isinstance(inc, bool):
        _add("include_in_optimizer", inc)
    for facet, src in (entry.get("breakdown_source") or {}).items():
        _add(f"breakdown_source.{facet}", src)
    for field, value in (entry.get("fund_structure") or {}).items():
        _add(field, value)

    return _migrate_breakdown_source_values(_migrate_renamed_override_fields(out))


# Override fields renamed since the per-field store landed. Applied to
# every entry on read, because a rename postdates the store: an entry
# written before it is already in envelope shape and would never be
# visited by the legacy-shape migration.
_RENAMED_OVERRIDE_FIELDS: dict[str, str] = {
    # v0.47.0 — disambiguated from the asset_class BREAKDOWN FACET.
    "asset_class": "primary_asset_class",
}


def _migrate_renamed_override_fields(entry: dict) -> dict:
    """Move values from retired field names to their replacements.

    Exact-key matching only. A substring or prefix rule would also rewrite
    ``breakdown_source.asset_class`` — a different field, added in
    v0.44.0, that happens to end with the old name — and quietly break
    that facet's source selection.

    An existing value under the new name wins: the user has since set it
    deliberately, and a migration should not undo that.
    """
    out = dict(entry)
    for old_name, new_name in _RENAMED_OVERRIDE_FIELDS.items():
        if old_name not in out:
            continue
        env = out.pop(old_name)
        if new_name not in out and isinstance(env, dict) and "value" in env:
            out[new_name] = env
    return out


def _migrate_breakdown_source_values(entry: dict) -> dict:
    """Rename the ``fund`` breakdown source to ``yahoo`` (v0.44.0).

    Applied to every entry on read, not only to legacy-shaped ones: the
    rename landed after the per-field store existed, so a value written
    in 0.33–0.43 is already in envelope shape and would otherwise never
    be visited.

    ``yahoo`` is also the neutral value, so an override carrying it is
    not an assertion at all — it is dropped rather than translated,
    leaving the field to track the default as it would have anyway.
    """
    from porxpy.config import BREAKDOWN_FACETS

    out = dict(entry)
    for facet in BREAKDOWN_FACETS:
        field = f"breakdown_source.{facet}"
        env = out.get(field)
        if not (isinstance(env, dict) and env.get("value") == "fund"):
            continue
        del out[field]
    return out


def load_overrides() -> dict:
    """Load the override store, migrating any pre-0.33.0 entries in place.

    Migration is one-shot and self-healing: it runs on read, rewrites the
    file when anything changed, and is a no-op thereafter. There is no
    legacy reader — the old shape exists only inside
    :func:`_migrate_override_entry`.
    """
    if not OVERRIDES_FP.exists():
        return {}
    try:
        with open(OVERRIDES_FP, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    migrated, changed = {}, False
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if _is_legacy_entry(entry):
            entry = _migrate_override_entry(entry)
            changed = True
        else:
            renamed = _migrate_breakdown_source_values(
                _migrate_renamed_override_fields(entry))
            if renamed != entry:
                entry, changed = renamed, True
        if entry:
            migrated[key] = entry
    if changed:
        print(f"[Overrides] migrated {len(migrated)} entr(ies) to the "
              f"v0.33.0 per-field format.")
        save_overrides(migrated)
    return migrated


def save_overrides(m: dict) -> None:
    """Persist the override store."""
    try:
        with open(OVERRIDES_FP, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[Overrides] save error: {exc}")


def _vocab_message(field: str, allowed) -> str:
    """A rejection message that names the field, not the whole dictionary.

    ``focus_detail`` under a geographic focus admits every country, every
    region and every super region — some 280 values — and printing all of
    them produced a "rejected" line in the factsheet report that was a
    wall of country names with the actual problem lost inside it. A
    handful of examples and a count say the same thing and can be read.
    """
    vals = [str(a) for a in (allowed or [])]
    if len(vals) <= 12:
        return f"{field} must be one of {tuple(vals)}"
    shown = ", ".join(vals[:8])
    return (f"{field} must be one of {len(vals)} allowed values "
            f"(e.g. {shown}, …)")


def coerce_override_value(field: str, value, context: dict | None = None):
    """Validate and coerce a value against the field's registry entry.

    Args:
        field: Registry key.
        value: The candidate value.
        context: Other pending field values, for the one cross-field
            rule — ``focus_detail``'s vocabulary depends on the
            ``focus_type`` being set in the same edit.

    Returns:
        The coerced value.

    Raises:
        ValueError: Unknown field, wrong type, out of range, or outside
            the field's vocabulary.
    """
    from porxpy.config import OVERRIDABLE_FIELDS

    spec = OVERRIDABLE_FIELDS.get(field)
    if spec is None:
        raise ValueError(f"not an overridable field: {field}")
    kind = spec.get("type")

    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{field} must be true or false")

    if kind == "number":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a number")
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{field} must be a finite number")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:
            raise ValueError(f"{field} must be at least {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"{field} must be at most {hi}")
        return v

    v = ("" if value is None else str(value)).strip()
    if kind == "enum":
        # field_vocab, not spec["vocab"] — an enum whose vocabulary
        # lives in a resource file declares vocab_fn instead, and
        # reading only the tuple would reject every legal value.
        from porxpy.config import field_vocab
        vocab = field_vocab(spec, context)
        if v.lower() not in vocab:
            raise ValueError(_vocab_message(field, vocab))
        return v.lower()

    vocab_fn = spec.get("vocab_fn")
    if vocab_fn:
        import porxpy.resources as _res
        allowed = getattr(_res, vocab_fn)(context or {})
        # None means "free text is fine for this context" — a thematic
        # focus is exactly the case nothing can enumerate.
        if allowed is not None and v and v not in allowed:
            raise ValueError(_vocab_message(field, allowed))
    return v


def overrides_for(isin: str) -> dict:
    """Every stored envelope for one ISIN. Fresh dict, safe to mutate."""
    key = _ov_key(isin)
    if not key:
        return {}
    return {f: dict(e) for f, e in (load_overrides().get(key) or {}).items()
            if isinstance(e, dict)}


def field_source_get(isin: str, field: str) -> str:
    """Which source a field is pinned to. "yahoo" when nothing is pinned.

    A pin is the durable choice; the value stored beside it is the last
    answer that source gave. Absence means the default, which keeps an
    untouched fund free of an override per field just to say "normal".
    """
    env = overrides_for(isin).get(field)
    src = (env or {}).get("source")
    return src if src else "yahoo"


def field_source_set(isin: str, field: str, source: str, value=None,
                     note: str = "") -> dict | None:
    """Pin a field to a source, recording the answer that source gave.

    The envelope carries both because they answer different questions.
    ``source`` is the instruction — where to look, now and on every
    reload. ``value`` is the last answer, cached so opening a fund page
    does not re-scrape justETF for every pinned field.

    ``None`` as a value is meaningful: it records that the pinned source
    was asked and had nothing. Without it a fund page could not tell
    "not yet fetched" from "fetched, and the answer was unknown", and
    would re-ask a source that has already said no.

    Pinning back to the default clears the entry — presence is the
    assertion, as everywhere else in this store.

    Returns:
        The stored envelope, or None when the pin was cleared.
    """
    from porxpy.config import FIELD_SOURCES

    key = _ov_key(isin)
    if not key:
        raise ValueError("an ISIN is required")
    src = (source or "").strip().lower()
    if src not in FIELD_SOURCES:
        raise ValueError(f"source must be one of {FIELD_SOURCES}")

    m = load_overrides()
    entry = dict(m.get(key) or {})

    if src == "yahoo" and value is None:
        # Back to the default with nothing to remember: drop the entry
        # rather than storing "normal" as a fact.
        if field not in entry:
            return None
        del entry[field]
        if entry:
            m[key] = entry
        else:
            m.pop(key, None)
        save_overrides(m)
        return None

    env = {"source": src, "value": value, "ts": now_iso()}
    if note:
        env["note"] = str(note).strip()
    entry[field] = env
    m[key] = entry
    save_overrides(m)
    return env


def purge_default_pins(isin: str) -> list[str]:
    """Delete pins to "yahoo", which are not assertions.

    Every other source is a real instruction — ask justETF, read the
    factsheet, use my value. "Ask Yahoo" is what happens anyway, so
    storing it records nothing while doing two kinds of harm: it freezes
    a snapshot of the value (the v0.27 bug, in a new place), and it
    outranks the seed's own origin in the provenance map, captioning a
    name-inferred value as Yahoo data.

    Selecting Yahoo now withdraws the pin instead of writing one, the
    way the breakdown-source endpoint has always treated its default.
    This clears the ones already written, on fund load, so a fund
    corrects itself the first time it is read.

    Args:
        isin: Fund ISIN.

    Returns:
        The field names whose pins were removed.
    """
    if not isin:
        return []
    gone = [f for f, e in overrides_for(isin).items()
            if isinstance(e, dict) and e.get("source") == "yahoo"]
    for f in gone:
        override_delete(isin, f)
    return gone


def field_pins(isin: str) -> dict:
    """``{field: {source, value, ts}}`` for every pinned field."""
    return {f: e for f, e in overrides_for(isin).items()
            if isinstance(e, dict) and e.get("source")}


def override_get(isin: str, field: str, default=None):
    """The asserted value for one field, or ``default`` when unasserted."""
    env = overrides_for(isin).get(field)
    if not env or "value" not in env:
        return default
    return env["value"]


def override_source(isin: str, field: str) -> str | None:
    """Who asserted this field, or None when nobody has."""
    env = overrides_for(isin).get(field)
    return (env or {}).get("source")


def override_put(isin: str, field: str, value, source: str = "user",
                 note: str = "", context: dict | None = None) -> dict:
    """Assert a value for one field. Returns the stored envelope.

    Raises:
        ValueError: For a blank ISIN, an unknown field, an unknown
            source, or a value the registry rejects.
    """
    key = _ov_key(isin)
    if not key:
        raise ValueError("an ISIN is required to store an override")
    if source not in OVERRIDE_SOURCES:
        raise ValueError(f"source must be one of {OVERRIDE_SOURCES}")

    coerced = coerce_override_value(field, value, context)
    env = {"value": coerced, "source": source, "ts": now_iso()}
    if note:
        env["note"] = str(note).strip()

    m = load_overrides()
    entry = dict(m.get(key) or {})
    entry[field] = env
    m[key] = entry
    save_overrides(m)
    return env


def override_delete(isin: str, field: str | None = None) -> bool:
    """Withdraw one assertion, or every assertion for the fund.

    Deleting IS "revert to the Yahoo/derived value" — there is no stored
    value meaning "unset", so removing the entry is the whole operation.

    Returns:
        True if anything was actually removed.
    """
    key = _ov_key(isin)
    if not key:
        return False
    m = load_overrides()
    entry = m.get(key)
    if not entry:
        return False
    if field is None:
        del m[key]
    else:
        if field not in entry:
            return False
        del entry[field]
        if entry:
            m[key] = entry
        else:
            m.pop(key, None)
    save_overrides(m)
    return True


def _payload_get(payload: dict, path: str):
    """Read a dotted path out of a payload, or None."""
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _payload_set(payload: dict, path: str, value) -> bool:
    """Write ``value`` into ``payload`` at a dotted path. False if absent."""
    parts = path.split(".")
    node = payload
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return False
    if not isinstance(node, dict):
        return False
    node[parts[-1]] = value
    return True


def apply_overrides(isin: str, payload: dict) -> dict:
    """Write every targeted override into an assembled fund payload.

    Walks the registry rather than the stored entry, so a stale field
    left in the file by a downgrade is ignored rather than injected.
    Mutates ``payload`` in place.

    Args:
        isin: Fund ISIN.
        payload: The response dict under assembly — expected to already
            contain the blocks the registry targets (``profile``,
            ``fund_structure``).

    Returns:
        ``(applied, displaced)``. ``applied`` maps each overridden field
        to the source that asserted it, so the UI can caption the row.
        ``displaced`` maps it to the value that was there beforehand —
        the derived one the override is standing in front of.

        ``displaced`` exists because otherwise the derived value is
        simply gone: the override is written over the same payload slot,
        so by the time the client sees the response there is no record of
        what Yahoo said. That made "revert to Yahoo" a promise the UI
        could not keep — it could delete the override but had nothing to
        put back, and had to wait for a refetch to find out.
    """
    from porxpy.config import OVERRIDABLE_FIELDS

    stored = overrides_for(isin)
    applied:   dict[str, str] = {}
    displaced: dict[str, object] = {}
    for field, spec in OVERRIDABLE_FIELDS.items():
        target = spec.get("target")
        if not target:
            continue
        env = stored.get(field)
        if not env or "value" not in env:
            continue
        before = _payload_get(payload, target)
        if _payload_set(payload, target, env["value"]):
            applied[field]   = env.get("source") or "user"
            displaced[field] = before
    return applied, displaced


# ── uploaded_breakdowns ─────────────────────────────────────────────────────
# User-uploaded per-facet breakdown item lists, keyed by ISIN. This is
# the third source for the fund-level breakdown cards (alongside the
# issuer aggregate and the holdings roll-up). The data is materially
# bigger than a one-word source choice — full item lists per facet —
# so it lives in the cache layer as a fund-level category, not inside
# overrides.json. The cache entry is manual-refresh-only: never fetched,
# only written when the user commits a CSV upload.
#
# On-disk shape (under the ``uploaded_breakdowns`` category), one item
# list per facet PER SOURCE since v0.44.0 — a factsheet extraction and a
# user CSV are both "someone handed us these numbers" and neither may
# overwrite the other:
#   { "fetched_at": "...",
#     "value": { "asset_class": {"upload":    [{"raw","weight"}, ...],
#                                "factsheet": [...]},
#                "sector":      {...},
#                "country":     {...},
#                "currency":    {...} } }
# Each item carries ``raw`` — the source's own wording, resolved afresh
# on every read (v0.76.0) — plus an optional ``key`` where the user
# pinned it to a node. Only facets present with a non-empty list count as
# "uploaded"; absent or empty facets cannot be flipped to that source on
# the fund page.

def _clean_items(items) -> list[dict]:
    """Coerce a raw item list to ``[{"raw", ["key"], "weight"}, ...]``.

    The supplied-breakdown item shape since v0.76.0: ``raw`` is what the
    source actually said and is re-resolved on every read, and ``key`` is
    present only where the user pinned the item to a node, in which case
    it is taken as given. :func:`porxpy.breakdowns._resolve_items` is the
    reader that answers the pair; this is the only normaliser between it
    and the two writers (a CSV commit and a factsheet extraction).

    Until v0.76.3 it required ``key`` and emitted nothing else, which was
    the pre-0.76.0 shape and had three compounding effects. Every
    unpinned item was dropped — the whole of a factsheet extraction, and
    every un-pinned row of a CSV upload — so the breakdown card had
    nothing to show. The pins that did survive lost their ``raw``, so
    they read as pre-0.76.0 to :func:`_drop_legacy_facet_stores` and the
    next cache read deleted the entry outright. And because this function
    also runs on the READ path, via :func:`_migrate_supplied_breakdowns`,
    fixing only the writers would not have been enough.

    An item with no ``raw`` is a pre-0.76.0 conclusion with its input
    already thrown away, and is dropped for the reason
    :func:`_drop_legacy_facet_stores` gives: inventing a raw from a
    stored conclusion is exactly the loop that made an alias edit unable
    to reach a stored row. In practice the legacy drop has already
    removed those before this function sees them; the guard is here so
    nothing can write the old shape back in.
    """
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        raw = str(it.get("raw") or "").strip()
        if not raw:
            continue
        try:
            weight = float(it.get("weight") or 0.0)
        except (TypeError, ValueError):
            continue
        row = {"raw": raw, "weight": weight}
        key = str(it.get("key") or "").strip()
        if key:
            row["key"] = key
        out.append(row)
    return out


def _migrate_supplied_breakdowns(val: dict) -> tuple[dict, bool]:
    """Rewrite a pre-0.44.0 ``{facet: items}`` blob to ``{facet: {source: items}}``.

    Until 0.44.0 there was one non-derived source — a user CSV — so one
    item list per facet said everything. A factsheet extraction is a
    second, and storing both flat would mean each silently overwriting
    the other: upload a CSV, extract a factsheet, and selecting "Upload"
    would quietly show factsheet numbers.

    Detection is by SHAPE, not by a version marker: the old value maps a
    facet to a list, the new one maps it to a dict of source → list. That
    converges, whereas a key-name test would not (the facet names are
    unchanged).
    """
    out: dict[str, dict] = {}
    changed = False
    for facet in BREAKDOWN_FACETS:
        cur = (val or {}).get(facet)
        if isinstance(cur, list):
            # Legacy: everything stored flat came from a CSV upload.
            out[facet] = {"upload": _clean_items(cur)}
            changed = True
        elif isinstance(cur, dict):
            out[facet] = {
                src: _clean_items(items)
                for src, items in cur.items()
                if src in SUPPLIED_BREAKDOWN_SOURCES
            }
        else:
            out[facet] = {}
    return out, changed


def uploaded_breakdowns_get(isin: str, source: str | None = None) -> dict:
    """Per-facet item lists supplied by a non-derived source.

    Args:
        isin: Fund ISIN.
        source: One of :data:`SUPPLIED_BREAKDOWN_SOURCES` to get that
            source's lists as ``{facet: items}``. Omit for every source,
            as ``{facet: {source: items}}``.

    Always returns all four facet keys so callers can index without
    checking. Pre-0.44.0 entries are migrated on read.
    """
    empty_flat = {f: [] for f in BREAKDOWN_FACETS}
    if not isin:
        return dict(empty_flat) if source else {f: {} for f in BREAKDOWN_FACETS}

    blob  = cache_read(isin, "uploaded_breakdowns")
    entry = blob.get("uploaded_breakdowns")
    val   = entry.get("value") if isinstance(entry, dict) else None
    val   = val if isinstance(val, dict) else {}

    migrated, changed = _migrate_supplied_breakdowns(val)
    if changed:
        blob["uploaded_breakdowns"] = {
            "fetched_at": (entry or {}).get("fetched_at") or now_iso(),
            "value":      migrated,
        }
        cache_write((isin or "").strip().upper(), "uploaded_breakdowns", blob)

    if source:
        return {f: list(migrated.get(f, {}).get(source, []))
                for f in BREAKDOWN_FACETS}
    return migrated


def uploaded_breakdowns_put(isin: str, facets: dict,
                            source: str = "upload") -> dict:
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
    src = (source or "upload").strip().lower()
    if src not in SUPPLIED_BREAKDOWN_SOURCES:
        raise ValueError(f"source must be one of {SUPPLIED_BREAKDOWN_SOURCES}")

    # Read-then-merge, so writing one source leaves the other alone. The
    # read also migrates any legacy flat entry.
    existing = uploaded_breakdowns_get(isin_u)

    normalised: dict[str, list[dict]] = {f: [] for f in BREAKDOWN_FACETS}
    if isinstance(facets, dict):
        for facet in BREAKDOWN_FACETS:
            normalised[facet] = _clean_items(facets.get(facet))

    merged = {f: dict(existing.get(f) or {}) for f in BREAKDOWN_FACETS}
    for facet in BREAKDOWN_FACETS:
        merged[facet][src] = normalised[facet]

    blob = cache_read(isin_u, "uploaded_breakdowns")
    blob["uploaded_breakdowns"] = {
        "fetched_at": now_iso(),
        "value":      merged,
    }
    cache_write(isin_u, "uploaded_breakdowns", blob)
    return normalised


def uploaded_breakdowns_delete(isin: str, facet: str | None = None,
                               source: str = "upload") -> bool:
    """Remove one supplied source's breakdowns for ``isin``.

    The store is ``{facet: {source: items}}``, and the two supplied
    sources are independent assertions about the same fund: a factsheet
    extraction and a hand-uploaded CSV. Removing one must leave the
    other untouched — deleting the CSV used to take the factsheet's
    items with it, because the clear wrote a bare list where the
    source-keyed dict belongs, and the next read then re-keyed that
    list to ``upload`` as though it were a pre-0.44 flat entry.

    Args:
        isin: Fund ISIN.
        facet: Clear only this facet. None clears every facet.
        source: Which supplied source to clear. One of
            :data:`~porxpy.config.SUPPLIED_BREAKDOWN_SOURCES`.

    Returns:
        True if anything changed on disk.
    """
    isin_u = (isin or "").strip().upper()
    if not isin_u:
        return False
    src = (source or "upload").strip().lower()
    if src not in SUPPLIED_BREAKDOWN_SOURCES:
        raise ValueError(f"source must be one of {SUPPLIED_BREAKDOWN_SOURCES}")
    if facet is not None and facet not in BREAKDOWN_FACETS:
        return False

    # Read through the accessor so a legacy flat entry is migrated to the
    # source-keyed shape before we edit it.
    val    = uploaded_breakdowns_get(isin_u)
    facets = BREAKDOWN_FACETS if facet is None else (facet,)

    changed = False
    for f in facets:
        per_source = dict(val.get(f) or {})
        if per_source.pop(src, None):
            changed = True
        val[f] = per_source
    if not changed:
        return False

    blob = cache_read(isin_u, "uploaded_breakdowns")
    # Nothing left from any source: drop the entry rather than leaving an
    # empty husk behind.
    if not any(any(v for v in (val.get(f) or {}).values())
               for f in BREAKDOWN_FACETS):
        blob.pop("uploaded_breakdowns", None)
    else:
        blob["uploaded_breakdowns"] = {"fetched_at": now_iso(), "value": val}
    cache_write(isin_u, "uploaded_breakdowns", blob)
    return True


# ── upload_sources ──────────────────────────────────────────────────────────
# "Where did the last upload of each kind come from?", one record per
# member of UPLOAD_SOURCE_KINDS, keyed by ISIN. This is the memory behind
# the Source field of the three upload dialogs (holdings, factsheet,
# breakdown CSV): re-opening any of them offers back the URL or path the
# user gave last time, so re-importing an issuer document that has been
# updated is one click rather than a trip to the browser history.
#
# It is deliberately NOT provenance. What the data in effect came from is
# already recorded with the data itself — source_kind/source_value on the
# holdings blob, and the factsheet's own sidecar — and that record must
# not move when the user merely opens a dialog. This store answers the
# different question of what to type into an empty field.
#
# Kept as one store taking the kind as a parameter, rather than three
# per-dialog memories, because the three dialogs ask an identical
# question and must not drift apart in what they remember.

def upload_source_get(isin: str, kind: str | None = None) -> dict | None:
    """The remembered upload source for ``isin``.

    Args:
        isin: Fund ISIN.
        kind: One of :data:`~porxpy.config.UPLOAD_SOURCE_KINDS` to get
            that dialog's record, or None for every kind as
            ``{kind: record}``.

    Returns:
        ``{"source_kind", "source_value", "filename", "saved_at"}`` for a
        named kind (None when nothing is remembered), or the whole
        ``{kind: record}`` map when ``kind`` is None (possibly empty).
    """
    key = (isin or "").strip().upper()
    if not key:
        return None if kind else {}
    blob  = cache_read(key, "upload_sources")
    entry = blob.get("upload_sources")
    val   = entry.get("value") if isinstance(entry, dict) else None
    val   = val if isinstance(val, dict) else {}
    val   = {k: v for k, v in val.items()
             if k in UPLOAD_SOURCE_KINDS and isinstance(v, dict)}
    if kind is None:
        return val
    return val.get(kind) or None


def upload_source_put(isin: str, kind: str, source_value: str,
                      source_kind: str = "", filename: str = "") -> dict | None:
    """Remember where an upload came from, for the next time that dialog opens.

    Args:
        isin: Fund ISIN. A listing with no ISIN yet simply remembers
            nothing — there is no fund file to write to.
        kind: One of :data:`~porxpy.config.UPLOAD_SOURCE_KINDS`.
        source_value: The source string the user gave (URL, path, or the
            scratch path a dropped file was stashed at).
        source_kind: ``"url"`` or ``"disk"``. Derived from
            ``source_value`` when omitted, so callers that never had to
            classify it do not have to start.
        filename: What to show the user as the document's name — for a
            drop, the file they dropped rather than the scratch copy.

    Returns:
        The stored record, or None when nothing was stored (no ISIN, an
        unknown kind, or an empty source).

    Raises:
        ValueError: ``kind`` is not a known upload kind. A typo here
            would otherwise write a record nothing ever reads back.
    """
    key = (isin or "").strip().upper()
    if kind not in UPLOAD_SOURCE_KINDS:
        raise ValueError(f"kind must be one of {tuple(UPLOAD_SOURCE_KINDS)}")
    value = (source_value or "").strip()
    if not key or not value:
        return None

    # Local import: upload.py imports this module, so the classifier can
    # only be reached on call. It is the same one resolve_source uses, so
    # what we remember about a source and what the fetcher does with it
    # can never disagree.
    if not source_kind:
        from porxpy.upload import classify_source
        source_kind = classify_source(value)

    record = {
        "source_kind":  source_kind,
        "source_value": value,
        "filename":     (filename or "").strip(),
        "saved_at":     now_iso(),
    }
    current = upload_source_get(key) or {}
    current[kind] = record

    blob = cache_read(key, "upload_sources")
    blob["upload_sources"] = {"fetched_at": now_iso(), "value": current}
    cache_write(key, "upload_sources", blob)
    return record


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

    # v0.27.0 metadata. Same contract as the fields above: validate
    # against a closed vocabulary, fall back to the neutral value.
    market_cap = str(raw.get("market_cap", "")).strip().lower()
    if market_cap not in MARKET_CAPS:
        market_cap = "unknown"
    style_box = str(raw.get("style_box", "")).strip().lower()
    if style_box not in STYLE_BOXES:
        style_box = "unknown"
    focus_type = str(raw.get("focus_type", "")).strip().lower()
    # v0.68.0 renamed "region" to "geography". The forward map is applied
    # HERE, before the membership test, because this function is the
    # gate every stored structure passes through on its way to being
    # used. LEGACY_FOCUS_TYPES was applied only in scoring.peer_key,
    # which runs far downstream: the merge normalised first, found
    # "region" outside FOCUS_TYPES, reset it to "none" and blanked
    # focus_detail with it, so peer_key received a fund that no longer
    # claimed any focus at all. Every fund still carrying the old value
    # therefore collapsed into the "<class>|none|" catch-all and was
    # ranked against everything else of its asset class — a US growth
    # fund against a metals-miners ETF, which is what surfaced it.
    focus_type = LEGACY_FOCUS_TYPES.get(focus_type, focus_type)
    if focus_type not in FOCUS_TYPES:
        focus_type = "none"

    # focus_detail is free-form for "thematic" and a controlled key for
    # region/sector, but validating it against those vocabularies here
    # would couple this pure function to the resource CSVs. The frontend
    # constrains it with dropdowns; we only enforce the coupling rule
    # that detail without a type is meaningless.
    focus_detail = str(raw.get("focus_detail", "") or "").strip()
    if focus_type == "none":
        focus_detail = ""

    return {"structure":    structure,
            "replication":  replication,
            "style":        style,
            "distribution": distribution,
            "market_cap":   market_cap,
            "style_box":    style_box,
            "focus_type":   focus_type,
            "focus_detail": focus_detail}



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
    from porxpy.resources import RESOURCE_FINGERPRINTS
    # The same fingerprint dict the cache stamp uses, so a hand-edited
    # file re-normalises portfolios as well as fund caches.
    current_versions = dict(RESOURCE_FINGERPRINTS)
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

# The asset facet's pre-v0.70.0 keys, which were the fund-level
# vocabulary (equity / fixed_income / cash / mixed / commodity / other)
# rather than anything in the holdings taxonomy.
_ASSET_TARGET_RENAMES: dict[str, str] = {
    "fixed_income": "fixed income",   # same node, new spelling
}
_ASSET_TARGET_DROPPED: frozenset[str] = frozenset({"mixed", "commodity"})


def _migrate_asset_target_keys(block: dict) -> dict:
    """Rename and drop pre-v0.70.0 asset target keys.

    Works on either shape — a legacy ``{key: pct}`` block or a levelled
    ``{level: {key: pct}}`` one — because a fund saved between the two
    releases can be in either, and a migration that handles one shape
    only is a migration that fires once and then stops.

    Args:
        block: The raw asset_class target block from disk.

    Returns:
        A new block with renames applied and dropped keys removed.
    """
    dropped: list[str] = []

    def _one(d: dict) -> dict:
        out = {}
        for k, v in (d or {}).items():
            key = (k or "").strip() if isinstance(k, str) else ""
            if not key:
                continue
            if key in _ASSET_TARGET_DROPPED:
                dropped.append(key)
                continue
            out[_ASSET_TARGET_RENAMES.get(key, key)] = v
        return out

    if block and any(isinstance(v, dict) for v in block.values()):
        out = {lvl: _one(blk) for lvl, blk in block.items()
               if isinstance(blk, dict)}
        out = {lvl: blk for lvl, blk in out.items() if blk}
    else:
        out = _one(block)

    if dropped:
        print(f"[Targets] dropped asset-class target(s) with no node in "
              f"Asset_definitions.csv: {', '.join(sorted(set(dropped)))}")
    return out


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
    from porxpy.config import META_FACET_TARGETABLE, TARGET_FACETS

    from porxpy.config import FACET_DEFAULT_LEVEL, FACET_LEVELS

    out: dict[str, dict] = {f: {} for f in TARGET_FACETS}
    if not isinstance(raw, dict):
        return out
    for facet in TARGET_FACETS:
        block = raw.get(facet)
        if not isinstance(block, dict):
            continue

        # v0.65.0: targets are {facet: {level: {key: pct}}}.
        #
        # A bare key is not enough any more. `japan` is both a country
        # and a Morningstar region, and `unitedKingdom` the region
        # differs from `unitedkingdom` the country only in case — so
        # {"country": {"japan": 30}} does not say which 30% is meant.
        #
        # A pre-v0.65.0 dict is {key: pct} with no level. Detected by
        # shape rather than by a stored version: a level block's values
        # are dicts, a legacy block's are numbers.
        #
        # Each legacy key goes to the level that RECOGNISES it, not to
        # the facet's default level.
        #
        # v0.65.3: putting them all at the default was wrong for
        # country, and silently so. Before levels existed the Targets
        # tab offered REGIONS for the country facet — the old exposure
        # builder remapped country to region to match — so every legacy
        # country target is a region key. Migrating them to `country`
        # level left them where no exposure would ever carry that key,
        # and the optimiser reported them as simply unachieved with
        # nothing to indicate why.
        levels  = FACET_LEVELS.get(facet) or (facet,)
        default = FACET_DEFAULT_LEVEL.get(facet, levels[-1])

        # v0.70.0: the asset vocabulary became one tree, and two of its
        # old keys have no node under the new spelling. The generic
        # relocation below can only move a key it RECOGNISES, so these
        # are renamed first — otherwise they sit at a level whose
        # exposures can never carry them and the optimiser reports them
        # as merely unmet, which is indistinguishable from a real miss.
        #
        # "mixed" and "commodity" are dropped rather than renamed: a
        # HOLDING is never mixed, and commodity has no node in the tree.
        # They existed only in the fund-level vocabulary this facet used
        # to borrow. Dropping user data silently would be worse than the
        # bug, so it is logged.
        if facet == "asset_class" and isinstance(block, dict):
            block = _migrate_asset_target_keys(block)
        if block and not any(isinstance(v, dict) for v in block.values()):
            from porxpy.breakdowns import _key_at_level
            migrated: dict[str, dict] = {}
            for k, v in block.items():
                key = (k or "").strip() if isinstance(k, str) else ""
                if not key:
                    continue
                # The level a key belongs to is the one where it maps to
                # itself. A region key answers at region level and
                # nowhere else, so this is unambiguous.
                home = next((lv for lv in levels
                             if _key_at_level(facet, key, lv) == key), default)
                migrated.setdefault(home, {})[key] = v
            block = migrated

        for level, lvl_block in block.items():
            if level not in levels or not isinstance(lvl_block, dict):
                continue
            out[facet].setdefault(level, {})
            _coerce_level_targets(facet, level, lvl_block, out)

        # v0.66.5: relocate any key sitting at a level that does not
        # recognise it, whatever shape it arrived in.
        #
        # v0.66.2 repaired this only for LEGACY (unlevelled) blocks —
        # which missed the case that actually bit: v0.65.0's migration
        # put region keys at country level, and the moment the user
        # opened the Targets tab and saved, that wrong placement was
        # persisted in the levelled shape. The legacy branch then never
        # fired again, and the targets sat at a level whose exposures
        # can never carry them. The optimiser reported them as simply
        # unmet, because from its side that is indistinguishable.
        #
        # Idempotent: a key already at its home level maps to itself and
        # is left alone.
        if len(levels) > 1:
            from porxpy.breakdowns import _key_at_level
            relocated: dict[str, dict] = {}
            for level, blk in (out[facet] or {}).items():
                for key, pct in (blk or {}).items():
                    home = level
                    if _key_at_level(facet, key, level) != key:
                        home = next((lv for lv in levels
                                     if _key_at_level(facet, key, lv) == key),
                                    level)
                    relocated.setdefault(home, {})[key] = pct
            out[facet] = {lv: blk for lv, blk in relocated.items() if blk}
    return out


def _coerce_level_targets(facet: str, level: str, block: dict,
                          out: dict) -> None:
    """Coerce one ``(facet, level)`` block of ``{key: percent}``.

    Drops malformed entries, coerces percents to floats, clamps
    negatives to 0. Percents over 100 are KEPT — a 110% target is a
    user error worth surfacing rather than silently truncating.
    """
    from porxpy.config import META_FACET_TARGETABLE

    # The meta facets have a closed, short vocabulary and not all of it
    # is targetable. "unknown" is a data gap and "n/a" duplicates the
    # cash target on asset_class — neither is something a user can
    # meaningfully aim for, so a target on one is dropped rather than
    # stored and then never satisfiable.
    allowed = META_FACET_TARGETABLE.get(facet)
    for k, v in block.items():
        if not isinstance(k, str) or not k.strip():
            continue
        key = k.strip()
        if allowed is not None and key not in allowed:
            continue
        try:
            pct = float(v)
        except (TypeError, ValueError):
            continue
        if pct < 0:
            pct = 0.0
        out[facet][level][key] = pct


def portfolio_targets_get(pid: str) -> dict:
    """Return the targets dict for portfolio ``pid``.

    Always returns a full-width dict (one key per TARGET_FACET);
    missing facets are empty.

    Args:
        pid: Portfolio UUID.

    Returns:
        ``{asset_class: {...}, sector: {...}, country: {...},
          currency: {...}}``. Returns an empty-targets dict if the
        portfolio has no targets set or doesn't exist.
    """
    p = find_portfolio(pid)
    if not p:
        from porxpy.config import TARGET_FACETS
        return {f: {} for f in TARGET_FACETS}
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
            from porxpy.config import TARGET_FACETS
            if any(normalised.get(f) for f in TARGET_FACETS):
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
# ``rollup_holdings`` and ``resolve_facet_value`` now live in
# :mod:`porxpy.breakdowns`, the pure derivation layer shared by the
# fund, holding, and portfolio breakdown paths. They are re-exported
# here so existing ``from porxpy.utils import ...`` call sites keep
# working unchanged.
from porxpy.breakdowns import (  # noqa: F401  (compatibility re-export)
    resolve_facet_value,
    rollup_holdings,
)


# ---------------------------------------------------------------------------
# Application settings (settings.json)
# ---------------------------------------------------------------------------
# Free-form JSON dict, single source of truth for app-level toggles. Read
# by both the API (/api/settings GET) and by extractors that need to
# decide whether to enrich top-10 holdings. The on-disk format mirrors
# the in-memory one — DEFAULT_SETTINGS in config.py defines the shape.
def default_settings() -> dict:
    """The complete settings document as it stands with nothing set.

    ``DEFAULT_SETTINGS`` in config is only the seed for the two oldest
    sections; the defaults for scoring, group TTLs, the factsheet age
    limit and the AI consent flags live in their own config constants and
    are applied by :func:`normalise_settings`. Asking the normaliser what
    an empty document becomes is therefore the only way to get all six
    sections, and it cannot drift: it is the same function every save
    goes through, so a seventh section is in the defaults the moment it
    is normalised.

    This exists because ``GET /api/settings`` hands its ``defaults`` to
    the Settings tab's "Reset to defaults" button. While that payload was
    the partial config constant, the button silently reset two sections
    of six and left the scoring weights, the size floor, the group TTLs,
    the factsheet age and the AI toggles exactly as they were — while
    saying it had reset them.

    Returns:
        A full, validated settings dict. Safe to mutate: freshly built
        on every call.
    """
    return normalise_settings({})


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

    # ── scoring ───────────────────────────────────────────────────────
    # Three named weight models plus the size floor. Each preset is
    # validated independently and falls back to its config default, so a
    # hand-edited settings file with one broken preset does not cost the
    # user the other two.
    from porxpy.config import (DEFAULT_SIZE_FLOOR_BASE, RETURN_PERIODS,
                               SCORE_COMPONENTS, SCORING_PRESETS)

    sc_src = raw.get("scoring") if isinstance(raw.get("scoring"), dict) else {}

    def _weights(src, keys, defaults):
        """Non-negative floats for every key, defaulting per key.

        Not renormalised to sum to 1: the blend divides by the weights it
        actually used, so the absolute scale is irrelevant and forcing a
        sum would silently rewrite numbers the user typed.
        """
        out = {}
        for k in keys:
            v = (src or {}).get(k, defaults[k])
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = float(defaults[k])
            out[k] = f if f >= 0 else 0.0
        return out

    presets_out = {}
    for name, dflt in SCORING_PRESETS.items():
        src = (sc_src.get("presets") or {}).get(name) or {}
        presets_out[name] = {
            "label":      str(src.get("label") or dflt["label"]),
            "components": _weights(src.get("components"), SCORE_COMPONENTS,
                                   dflt["components"]),
            "wtrr":       _weights(src.get("wtrr"), tuple(RETURN_PERIODS),
                                   dflt["wtrr"]),
        }

    # ── group TTLs ────────────────────────────────────────────────────
    # One age limit per field group. Replaces the per-cache-category TTLs
    # as the thing the user actually sets: how often a fund's structure
    # or its costs go stale is a fact about that data, not about which
    # cache file it happens to live in.
    from porxpy.config import DEFAULT_GROUP_TTL_DAYS
    gt_src = raw.get("group_ttl_days") if isinstance(raw.get("group_ttl_days"), dict) else {}
    group_ttl = {}
    for g, dflt in DEFAULT_GROUP_TTL_DAYS.items():
        try:
            v = int(float(gt_src.get(g, dflt)))
        except (TypeError, ValueError):
            v = dflt
        group_ttl[g] = max(0, v)

    # ── ai ────────────────────────────────────────────────────────────
    # Consent to send an uploaded factsheet to the Anthropic API. Off by
    # default and stored separately from the key, which lives only in the
    # environment: settings.json sits in the project directory in
    # plaintext and gets copied around, which is no place for a
    # credential.
    ai_src = raw.get("ai") if isinstance(raw.get("ai"), dict) else {}
    ai_enabled = bool(ai_src.get("enabled", False))
    # Whether the extraction report offers an editable prompt. Off by
    # default: the generated prompt is built from the live registry, so
    # hand-editing it is a debugging affordance rather than something
    # most runs should involve.
    ai_edit_prompt = bool(ai_src.get("edit_prompt", False))

    # ── factsheet ─────────────────────────────────────────────────────
    # How old a factsheet may be before the UI flags it. Not a TTL: a
    # hand-uploaded document has no source to refetch from, so nothing
    # expires — this only decides when to say "this looks out of date".
    fs_src = raw.get("factsheet") if isinstance(raw.get("factsheet"), dict) else {}
    try:
        stale_days = int(float(fs_src.get("stale_days", FACTSHEET_STALE_DAYS)))
    except (TypeError, ValueError):
        stale_days = FACTSHEET_STALE_DAYS
    if stale_days < 0:
        stale_days = 0

    try:
        size_floor = float(sc_src.get("size_floor_base", DEFAULT_SIZE_FLOOR_BASE))
    except (TypeError, ValueError):
        size_floor = DEFAULT_SIZE_FLOOR_BASE
    if size_floor < 0:
        size_floor = 0.0

    return {
        "enrichment": {
            "fields": fields,
        },
        "holdings_match": {
            "key": match_key,
        },
        "scoring": {
            "presets":         presets_out,
            "size_floor_base": size_floor,
        },
        "factsheet": {
            "stale_days": stale_days,
        },
        "ai": {
            "enabled":     ai_enabled,
            "edit_prompt": ai_edit_prompt,
        },
        "group_ttl_days": group_ttl,
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


# ---------------------------------------------------------------------------
# Factsheets (v0.43.0)
# ---------------------------------------------------------------------------
# One factsheet per fund, keyed by ISIN, stored as the original bytes plus
# a JSON sidecar. Two files rather than one blob because the document is
# what the user wants to look at — serving it should be a file read, not a
# base64 decode — while everything about it needs to be queryable without
# opening it.
#
# A newer upload replaces the older wholesale, including any extraction
# derived from it. Keeping both would mean answering "which one did this
# TER come from" for every field, and the override store already records
# that per field in its note.


def _factsheet_paths(isin: str) -> tuple[Path, Path]:
    """``(directory, sidecar_path)`` for a fund. The doc's suffix varies."""
    key = (isin or "").strip().upper()
    return FACTSHEETS_DIR, FACTSHEETS_DIR / f"{key}.json"


def factsheet_get(isin: str) -> dict | None:
    """The stored factsheet's metadata, or None when there isn't one.

    Returns:
        ``{isin, filename, ext, bytes, uploaded_at, as_of, note,
        extraction, stale, age_days}``. ``as_of`` is the factsheet's own
        publication date when known; ``stale`` and ``age_days`` are
        computed against it, falling back to the upload date when it is
        not.
    """
    key = (isin or "").strip().upper()
    if not key:
        return None
    _, side = _factsheet_paths(key)
    if not side.exists():
        return None
    try:
        meta = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None

    # Age from the factsheet's own date where we have it. A document can
    # be years old on the day it is uploaded, so upload date answers
    # "when did I do this", not "how current is this data".
    ref = (meta.get("as_of") or "").strip() or (meta.get("uploaded_at") or "")
    meta["age_days"] = age_days(ref) if ref else None
    meta["age_from"] = "as_of" if (meta.get("as_of") or "").strip() else "upload"
    threshold = FACTSHEET_STALE_DAYS
    try:
        threshold = int((load_settings().get("factsheet") or {})
                        .get("stale_days", FACTSHEET_STALE_DAYS))
    except Exception:
        pass
    meta["stale_days"] = threshold
    meta["stale"] = (meta["age_days"] is not None
                     and threshold > 0
                     and meta["age_days"] > threshold)
    return meta


def factsheet_put(isin: str, filename: str, data: bytes,
                  as_of: str = "", note: str = "") -> dict:
    """Store a factsheet, replacing any previous one for this fund.

    Args:
        isin: Fund ISIN. Required — factsheets are fund-level, and a
            listing without an ISIN has nothing to hang one on.
        filename: The user's filename, used for its extension and shown
            back to them. Never used as a path.
        data: The document bytes.
        as_of: The factsheet's own publication date (``YYYY-MM-DD``) when
            known. Optional; staleness falls back to the upload date.
        note: Free text.

    Returns:
        The stored metadata, as :func:`factsheet_get` would return it.

    Raises:
        ValueError: Missing ISIN, empty file, or an unsupported type.
    """
    key = (isin or "").strip().upper()
    if not key:
        raise ValueError("an ISIN is required to store a factsheet")
    if not data:
        raise ValueError("the file is empty")

    ext = Path(filename or "").suffix.lower()
    if ext not in FACTSHEET_EXTENSIONS:
        raise ValueError(
            f"unsupported factsheet type {ext or '(none)'} — "
            f"expected one of {', '.join(FACTSHEET_EXTENSIONS)}")

    FACTSHEETS_DIR.mkdir(parents=True, exist_ok=True)
    # Drop any previous document: the extension may differ, so replacing
    # by name alone would leave the old file orphaned beside the new one.
    for old in FACTSHEETS_DIR.glob(f"{key}.*"):
        if old.suffix.lower() != ".json":
            try:
                old.unlink()
            except OSError:
                pass

    doc = FACTSHEETS_DIR / f"{key}{ext}"
    doc.write_bytes(data)

    meta = {
        "isin":        key,
        "filename":    Path(filename or "").name,
        "ext":         ext,
        "bytes":       len(data),
        "uploaded_at": now_iso(),
        "as_of":       (as_of or "").strip(),
        "note":        (note or "").strip(),
        # Filled by the extraction step; replaced wholesale with the
        # document, so an extraction can never outlive its source.
        "extraction":  None,
    }
    _, side = _factsheet_paths(key)
    side.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return factsheet_get(key) or meta


def factsheet_file(isin: str) -> Path | None:
    """Path to the stored document, or None when there isn't one."""
    key = (isin or "").strip().upper()
    if not key:
        return None
    meta = factsheet_get(key)
    if not meta:
        return None
    doc = FACTSHEETS_DIR / f"{key}{meta.get('ext') or ''}"
    return doc if doc.exists() else None


def factsheet_delete(isin: str) -> bool:
    """Remove a fund's factsheet and its metadata. True if anything went."""
    key = (isin or "").strip().upper()
    if not key or not FACTSHEETS_DIR.exists():
        return False
    gone = False
    for fp in FACTSHEETS_DIR.glob(f"{key}.*"):
        try:
            fp.unlink()
            gone = True
        except OSError:
            pass
    return gone


def factsheet_set_extraction(isin: str, extraction: dict | None) -> dict | None:
    """Attach (or clear) the AI extraction on a stored factsheet."""
    key = (isin or "").strip().upper()
    meta = factsheet_get(key)
    if not meta:
        return None
    meta = {k: v for k, v in meta.items()
            if k not in ("age_days", "age_from", "stale")}
    meta["extraction"] = extraction
    _, side = _factsheet_paths(key)
    side.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return factsheet_get(key)
