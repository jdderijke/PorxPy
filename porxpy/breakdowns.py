"""
Breakdown derivation — the single pure-function layer that turns cached
*source* data into *derived* breakdown cards.

PorxPy's breakdown cards have exactly two source kinds:

* **fund metadata** — Yahoo's fund-level sector/asset-class info (with
  any per-fund asset-class override applied), plus the portfolio's
  per-fund shares;
* **holdings** — the per-position rows in the unified ``holdings`` cache
  slot, whether sourced from a Yahoo top-10, an enriched top-10, or a
  full user upload.

The cards are never authoritative and are never stored: they are a pure
function of whatever is in the cache *right now*. This module is that
function. It does no I/O, touches no cache, and holds no state — given
the same source data it always produces the same cards. Every endpoint
that returns breakdown cards (``load_fund_data``, ``api_holdings_patch``,
``api_portfolio_view``) routes through here, so a card physically cannot
go stale: there is nowhere to store a stale one.

Three derivation levels, three functions:

* :func:`canonicalise_facet_key` — normalise one raw facet value to a
  canonical rollup-bucket key (shared by the two rollups below).
* :func:`rollup_holdings` — **fund / holding level.** One fund's
  per-position rows → that fund's look-through breakdowns.
* :func:`rollup_portfolio_lookthrough` — **portfolio level.** A list of
  already-valued funds (each carrying its own ``rollup_holdings``
  output) → the portfolio-wide look-through breakdowns.

For backwards compatibility, :mod:`porxpy.utils` re-exports
``canonicalise_facet_key`` and ``rollup_holdings`` so existing
``from porxpy.utils import ...`` call sites keep working.
"""

from __future__ import annotations

from typing import Any

from porxpy.config import META_FACETS, TARGET_FACETS


# ---------------------------------------------------------------------------
# Facet-key canonicalisation
# ---------------------------------------------------------------------------
# Different sources of per-position holdings rows disagree on how facet
# values are spelled:
#
#   * CSV upload (upload.py) routes ``country`` through
#     :func:`country_to_mstar`, producing lowercased mstar forms like
#     ``"unitedstates"``. ``asset_class`` is stored verbatim from the
#     file (so iShares CSVs produce ``"Equity"``).
#   * Top-10 enrichment (extractors.extract_symbol_info) returned Yahoo's
#     literal forms — ``"United States"`` and ``"Equity"`` — until the
#     companion fix in extractors.py canonicalised at the source.
#
# When two funds in a portfolio came from different sources, the two
# forms survived as DISTINCT buckets in the rollup but rendered with the
# SAME visible label after the frontend's ``fmtCountry`` /
# ``fmtAssetClass`` formatters — appearing as a duplicate row in the
# portfolio's Holdings-level breakdown cards.
#
# Canonicalising at every rollup chokepoint makes the rollup itself
# the authoritative normalisation layer, so it works regardless of what
# upstream paths feed in. The display formatters then have a single
# canonical form to match against.
#
# Per-facet canonicalisation rules:
#   country     — :func:`country_to_mstar` (lowercased mstar form);
#                 if unmappable, lowercased+stripped raw value.
#   asset_class — known Title-Case Yahoo forms mapped to the
#                 ``ASSET_CLASSES`` vocabulary in config.py; otherwise
#                 lowercased+stripped raw value (per user choice — keeps
#                 unknowns visible rather than swallowing them as
#                 "other").
#   currency    — uppercased+stripped (handles ``"usd"`` vs ``"USD"``).
#   sector      — left as-is. Different sources use different sector
#                 vocabularies (GICS levels, Yahoo's shorthand) and a
#                 collision-free canonical form would need a real
#                 vocabulary map. Out of scope here; revisit if a
#                 sector-side duplicate surfaces.

# NOTE: the asset-class spelling aliases that used to live here as
# _ASSET_CLASS_ALIASES have moved to Fund_class_definitions.csv (loaded
# by resources.py). canonicalise_facet_key now calls
# resources.resolve_fund_asset_class so there is a single maintained
# authority for the fund-level vocabulary rather than three hand-kept
# copies. To add a spelling, edit the `matches` column in that CSV.


def canonicalise_facet_key(facet: str, raw: Any) -> str:
    """Return the canonical key for a rollup-bucket lookup.

    Empty / ``None`` / ``"-"`` inputs return ``""`` so the caller can
    decide whether to fold them into the ``"undefined"`` bucket.
    Non-empty inputs return a canonical string suitable for use as a
    rollup-bucket key.

    Args:
        facet: One of ``"country"``, ``"asset_class"``, ``"currency"``,
            ``"sector"``. Unknown facets pass through with just
            ``.strip()``.
        raw: The raw facet value off a holdings row.

    Returns:
        Canonical key string, or ``""`` for blank/sentinel inputs.
    """
    # Local import to avoid a circular reference at module import time —
    # resources.py only depends on config, not on utils, so this is safe.
    from porxpy.resources import country_to_mstar

    s = ("" if raw is None else str(raw)).strip()
    if not s or s == "-":
        return ""

    if facet == "country":
        # country_to_mstar normalises case + handles aliases. Falls back
        # to a lowercased+stripped form on unmappable inputs so two
        # "Atlantis" rows from two different funds still bucket together.
        mstar = country_to_mstar(s)
        return mstar if mstar else s.lower()

    if facet == "asset_class":
        # Resolve against Fund_class_definitions.csv (the fund-level
        # vocabulary the rollup aggregates to). resolve_fund_asset_class
        # handles "bond"/"bonds"/"fixed income"/"bondPosition" → the
        # canonical "fixed_income", etc. Unmappable inputs fall back to
        # the lowercased raw so two identical unknowns still bucket
        # together (matching the old behaviour).
        from porxpy.resources import resolve_fund_asset_class
        canon = resolve_fund_asset_class(s)
        return canon if canon else s.lower()

    if facet == "currency":
        return s.upper()

    # sector and any unknown facet — keep raw form (already stripped).
    return s


# ---------------------------------------------------------------------------
# Fund / holding level — one fund's per-position rows → its breakdowns
# ---------------------------------------------------------------------------
def rollup_holdings(rows: list[dict]) -> dict:
    """Compute look-through breakdowns from a full per-position holdings list.

    Each input row carries ``weight_pct`` (a number, percent — 5.34 means
    5.34%) plus textual fields ``sector``, ``currency``, ``country``,
    ``asset_class``. Blank values for one or more of those fields (cash
    residuals, derivatives, securities-lending collateral, plain gaps in
    the upload) are kept and bucketed under the literal key ``"undefined"``
    on the affected facet — this way the visible buckets always sum to
    100% and the user can see how much of the rollup is unclassified.

    Facet values are canonicalised via :func:`canonicalise_facet_key`
    before bucketing — see that function for the per-facet rules.

    Returned weights are FRACTIONS (0–1), normalised against the total
    weight of all rows (including blanks-as-undefined), so each facet
    sums to 1.0 by construction. Per-facet ``coverage`` is therefore
    always 1.0 when ``total_weight_pct > 0``; the field is retained for
    backwards compatibility with callers that read it.

    Args:
        rows: Full-holdings rows as parsed by ``ishares.parse_csv`` or
            an upload commit.

    Returns:
        ::

            {
              "sector":      [{"key": "Technology",   "weight": 0.34},
                              {"key": "undefined",    "weight": 0.02}, ...],
              "currency":    [{"key": "USD",          "weight": 0.71}, ...],
              "country":     [{"key": "unitedstates", "weight": 0.61}, ...],
              "asset_class": [{"key": "equity",       "weight": 0.97}, ...],
              "coverage":    {"sector": 1.0, "currency": 1.0,
                              "country": 1.0, "asset_class": 1.0},
              "total_weight_pct": 99.87,
            }
    """
    facets = ("sector", "currency", "country", "asset_class")
    empty  = {
        **{f: [] for f in facets},
        "coverage":         {f: 0.0 for f in facets},
        "total_weight_pct": 0.0,
    }
    if not rows:
        return empty

    # Per-facet bucket dict keyed by the textual facet value (or
    # ``"undefined"`` for blanks). One row contributes its weight to
    # every facet's bucket — either to a real key or to "undefined".
    UNDEFINED = "undefined"
    buckets: dict[str, dict[str, float]] = {f: {} for f in facets}
    total_w = 0.0

    for r in rows:
        try:
            w = float(r.get("weight_pct") or 0.0)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        total_w += w
        for facet in facets:
            key = canonicalise_facet_key(facet, r.get(facet))
            if not key:
                key = UNDEFINED
            buckets[facet][key] = buckets[facet].get(key, 0.0) + w

    out: dict = {"total_weight_pct": round(total_w, 4)}
    for facet in facets:
        if total_w > 0:
            items = [
                {"key": k, "weight": round(w / total_w, 6)}
                for k, w in buckets[facet].items()
            ]
            # Sort by weight desc, but pin "undefined" at the end so
            # consumers can show it as a tail slice without resorting.
            items.sort(key=lambda x: (x["key"] == UNDEFINED, -x["weight"]))
        else:
            items = []
        out[facet] = items

    # Coverage is now 1.0 by construction whenever any rows exist (the
    # "undefined" bucket absorbs everything that used to count as
    # uncovered). Retained as a per-facet field for backwards compat.
    out["coverage"] = {
        f: 1.0 if total_w > 0 else 0.0 for f in facets
    }
    return out


# ---------------------------------------------------------------------------
# Fund-level breakdown cards — unified four-facet block
# ---------------------------------------------------------------------------
# The fund page and the portfolio X-ray both render four breakdown cards:
# asset_class / sector / country / currency. Each card is a *distribution
# over the fund's holdings* and has three possible data sources:
#
#   "fund"     — the issuer's own published aggregate of its holdings on
#                that facet (Yahoo funds_data). Available for asset_class
#                (asset_classes) and sector (sector_weightings) only;
#                Yahoo publishes NO country or currency distribution, so
#                those two are always empty at this tier.
#   "holdings" — the look-through roll-up of the fund's physical holdings
#                rows (rollup_holdings, above).
#   "upload"   — a user-uploaded CSV, per-facet, canonicalised on ingest
#                (see porxpy.utils.uploaded_breakdowns_*). This is the
#                fallback for funds where the issuer publishes nothing
#                useful AND no holdings list is available — for example,
#                actively managed funds with no top-10 disclosure and no
#                CSV from the issuer. The user gets the breakdowns from
#                a fund factsheet (typically by screenshotting the
#                issuer's fund page and asking an LLM to extract a CSV).
#
# Per fund, per facet, the user can override the source from "fund" to
# "holdings" or "upload" (persisted as "breakdown_source.<facet>"
# in the per-field override store — see porxpy.utils.override_put).
# Once overridden, that card *is* the fund's Fund/ETF-level data: it rolls up
# into the portfolio's Fund/ETF-level cards exactly like issuer data.
#
# build_fund_breakdowns is the single chokepoint that resolves all of
# this into one uniform structure, so every downstream consumer (the
# fund page, the portfolio rollup) is override-agnostic.

# Display order of the four cards — asset_class first to match the grid.
_FUND_BD_FACETS: tuple[str, ...] = ("asset_class", "sector", "country", "currency")


def _items_from_issuer_sectors(sectors: list[dict] | None) -> list[dict]:
    """Reshape issuer sector weightings to the canonical item schema.

    ``extract_sectors`` emits ``[{"sector", "weight"}]``; the unified
    card schema is ``[{"key", "weight"}]``. Weights are issuer fractions
    (0–1) and are passed through unchanged.
    """
    out: list[dict] = []
    for s in sectors or []:
        key = (s.get("sector") or "").strip()
        try:
            w = float(s.get("weight") or 0.0)
        except (TypeError, ValueError):
            continue
        if key and w > 0:
            out.append({"key": key, "weight": round(w, 6)})
    out.sort(key=lambda x: -x["weight"])
    return out


def _items_from_issuer_asset_allocation(alloc: list[dict] | None) -> list[dict]:
    """Validate/normalise the issuer asset-allocation list.

    ``extract_asset_allocation`` already emits ``[{"key", "weight"}]``
    with canonical keys; this is a defensive copy + re-sort so a bad
    cached blob can't leak a malformed row downstream.
    """
    out: list[dict] = []
    for it in alloc or []:
        key = (it.get("key") or "").strip()
        try:
            w = float(it.get("weight") or 0.0)
        except (TypeError, ValueError):
            continue
        if key and w > 0:
            out.append({"key": key, "weight": round(w, 6)})
    out.sort(key=lambda x: -x["weight"])
    return out


def build_fund_breakdowns(holdings_breakdowns: dict,
                          sectors: list[dict] | None,
                          asset_allocation: list[dict] | None,
                          overrides: dict | None,
                          uploaded_facets: dict | None = None) -> dict:
    """Resolve the four fund-level breakdown cards into one uniform block.

    This is a pure function: it reads only its arguments and does no I/O.

    For each of the four facets it decides, from ``overrides``, whether
    the card is sourced from issuer data (``"fund"``), the holdings
    roll-up (``"holdings"``), or a user CSV upload (``"upload"``), then
    emits the resolved item list together with enough metadata for the
    frontend to render the source label, the per-source availability
    flags, and the "populate from holdings" nudge.

    Args:
        holdings_breakdowns: The fund's look-through roll-up — the dict
            returned by :func:`rollup_holdings` (facet → item list, each
            item ``{"key", "weight"}``). May be empty.
        sectors: Issuer sector weightings as emitted by
            ``extract_sectors`` (``[{"sector", "weight"}]``).
        asset_allocation: Issuer asset-allocation breakdown as emitted by
            ``extract_asset_allocation`` (``[{"key", "weight"}]``).
        overrides: The fund's ``{facet: source}`` override map (from
            the ``breakdown_source.*`` override fields). A facet absent
            from the map defaults to ``"fund"``.
        uploaded_facets: Per-facet user-uploaded item lists (from
            :func:`porxpy.utils.uploaded_breakdowns_get`). Shape
            ``{facet: [{"key","weight"}, ...]}``. A facet with an empty
            list is treated as "no upload" — the card can't be flipped
            to source ``"upload"`` until an upload covers it. ``None``
            is equivalent to all-empty lists (the default before any
            CSV is uploaded for the fund).

    Returns:
        ::

            {
              "asset_class": {
                  "items":              [{"key","weight"}, ...],
                  "source":             "fund" | "holdings" | "upload",
                  "issuer_available":   bool,   # issuer published anything
                  "holdings_available": bool,   # roll-up has anything
                  "upload_available":   bool,   # an upload covers this facet
              },
              "sector":   {...},
              "country":  {...},
              "currency": {...},
            }

        Item weights are fractions (0–1). For the ``"fund"`` source they
        are issuer fractions as published (may not sum to 1.0); for
        ``"holdings"`` they are the roll-up's normalised fractions; for
        ``"upload"`` they are the fractions written at commit time
        (the upload pipeline normalises percent → fraction on ingest).

        Fallback rule: if ``overrides`` requests a source that has no
        data for that facet, the card silently falls back to ``"fund"``
        and ``source`` reflects what was actually picked. This handles
        two cases gracefully: the user removes an upload that was
        previously selected, or holdings data is wiped via Reset. The
        fallback never panics — it just shows whatever issuer-side data
        exists (which may itself be empty, in which case the card
        renders empty with its three availability flags so the user
        can pick another source).
    """
    overrides = overrides or {}
    hb = holdings_breakdowns if isinstance(holdings_breakdowns, dict) else {}
    ub = uploaded_facets if isinstance(uploaded_facets, dict) else {}

    # Issuer-published item lists per facet. Yahoo publishes only
    # asset_class and sector; country and currency have no issuer source.
    issuer: dict[str, list[dict]] = {
        "asset_class": _items_from_issuer_asset_allocation(asset_allocation),
        "sector":      _items_from_issuer_sectors(sectors),
        "country":     [],
        "currency":    [],
    }

    out: dict[str, dict] = {}
    for facet in _FUND_BD_FACETS:
        issuer_items   = issuer.get(facet) or []
        holdings_items = [
            it for it in (hb.get(facet) or [])
            if isinstance(it, dict) and it.get("key")
        ]
        upload_items   = [
            it for it in (ub.get(facet) or [])
            if isinstance(it, dict) and it.get("key")
        ]
        src = overrides.get(facet, "fund")
        if src not in ("fund", "holdings", "upload"):
            src = "fund"

        # Resolve the items for the requested source, with graceful
        # fallback to "fund" when that source has no data for this
        # facet. ``src`` is reassigned to reflect what was actually
        # picked so the frontend's "data: <source>" label is honest.
        if src == "holdings":
            if holdings_items:
                items = holdings_items
            else:
                items = issuer_items
                src = "fund"
        elif src == "upload":
            if upload_items:
                items = upload_items
            else:
                items = issuer_items
                src = "fund"
        else:
            items = issuer_items

        out[facet] = {
            "items":              [dict(it) for it in items],
            "source":             src,
            "issuer_available":   bool(issuer_items),
            "holdings_available": bool(holdings_items),
            "upload_available":   bool(upload_items),
        }
    return out


# ---------------------------------------------------------------------------
# Portfolio level — many valued funds → portfolio-wide look-through
# ---------------------------------------------------------------------------
# Facets carried up to portfolio level. Same four as rollup_holdings, but
# ordered asset_class-first to match the portfolio view's card grid.
_LT_FACETS: tuple[str, ...] = ("asset_class", "sector", "country", "currency")


def rollup_portfolio_lookthrough(enriched: list[dict],
                                 total_base: float) -> dict:
    """Aggregate per-fund look-through breakdowns into portfolio-level ones.

    Each fund contributes ``value_base × fund_facet_weight`` to every
    facet's portfolio-level bucket. Funds with no breakdowns
    (``breakdowns_source == "none"``) contribute nothing — their
    ``value_base`` counts toward "uncovered" portfolio value, surfaced
    via per-facet coverage.

    Per-fund rollups already include an ``"undefined"`` bucket for blank
    facet values (see :func:`rollup_holdings`). At the portfolio level,
    ``"undefined"`` gets a final pass that drops it if the accumulated
    weight is essentially zero — the frontend asked for "hide if 0%".

    Each output item carries ``weight`` (fraction of the COVERED
    portfolio value) and ``value`` (base-currency money). Items are
    sorted descending, with ``"undefined"`` pinned at the end when
    present.

    This is a pure function: it reads only the ``enriched`` list passed
    in (each entry's ``valuation.value_base`` and
    ``data.holdings_breakdowns``) and ``total_base``. It does no I/O.

    Args:
        enriched: Per-fund dicts as built by ``api_portfolio_view`` —
            each carries a ``valuation`` dict (with ``value_base``) and
            a ``data`` dict (with ``holdings_breakdowns``).
        total_base: Portfolio total value in base currency. Used as the
            denominator for per-facet coverage.

    Returns:
        ::

            {
              "lookthrough_breakdowns": {
                  "asset_class": [{"key": ..., "weight": ..., "value": ...}, ...],
                  "sector":      [...],
                  "country":     [...],
                  "currency":    [...],
              },
              "lookthrough_coverage": {
                  "asset_class": 0.97, "sector": 0.81, ...
              },
            }
    """
    UNDEFINED = "undefined"
    # Per-facet accumulators: key → base-currency money.
    lt_buckets:       dict[str, dict[str, float]] = {f: {} for f in _LT_FACETS}
    # Per-facet covered value: sum of value_base across funds that
    # contributed any rollup data on that facet (i.e. the fund had
    # holdings_breakdowns and the facet list was non-empty).
    lt_covered_value: dict[str, float] = {f: 0.0 for f in _LT_FACETS}

    for e in enriched:
        v = (e.get("valuation") or {}).get("value_base")
        if v is None or v <= 0:
            continue
        bd = (e.get("data") or {}).get("holdings_breakdowns") or {}
        if not isinstance(bd, dict):
            continue
        for facet in _LT_FACETS:
            items = bd.get(facet) or []
            if not items:
                continue
            lt_covered_value[facet] += float(v)
            for it in items:
                raw_key = (it.get("key") or "").strip()
                # Per-fund rollups already emit canonical keys (see
                # canonicalise_facet_key), but we run the canonicaliser
                # again here as a defence-in-depth pass. This ensures
                # the portfolio merge correctly collapses duplicates
                # whatever shape upstream sent. The literal "undefined"
                # bucket is preserved as-is so it stays as the
                # blank-value residual.
                if not raw_key:
                    key = UNDEFINED
                elif raw_key == UNDEFINED:
                    key = UNDEFINED
                else:
                    key = canonicalise_facet_key(facet, raw_key) or UNDEFINED
                try:
                    w = float(it.get("weight") or 0.0)
                except (TypeError, ValueError):
                    continue
                lt_buckets[facet][key] = (
                    lt_buckets[facet].get(key, 0.0) + float(v) * w)

    lookthrough_breakdowns: dict[str, list[dict]] = {}
    lookthrough_coverage:   dict[str, float] = {}
    for facet in _LT_FACETS:
        covered = lt_covered_value[facet]
        lookthrough_coverage[facet] = round(
            covered / total_base, 6) if total_base > 0 else 0.0
        if covered <= 0:
            lookthrough_breakdowns[facet] = []
            continue
        items: list[dict] = []
        for k, val in lt_buckets[facet].items():
            # Drop a 0% undefined slice (the frontend doesn't want
            # noise when everything's mapped). Real keys with a
            # nonzero contribution always pass through.
            if k == UNDEFINED and val <= 1e-9:
                continue
            items.append({
                "key":    k,
                "weight": round(val / covered, 6),
                "value":  round(val, 2),
            })
        # Sort by weight desc, with "undefined" pinned at the end so
        # the chart legend reads "real categories first, residual
        # last" without the frontend having to resort.
        items.sort(key=lambda x: (x["key"] == UNDEFINED, -x["weight"]))
        lookthrough_breakdowns[facet] = items

    return {
        "lookthrough_breakdowns": lookthrough_breakdowns,
        "lookthrough_coverage":   lookthrough_coverage,
    }


# ---------------------------------------------------------------------------
# Portfolio level — many funds' Fund/ETF-level cards → portfolio cards
# ---------------------------------------------------------------------------
# The portfolio X-ray's "Fund/ETF level" mode aggregates each fund's
# Fund/ETF-level breakdown cards (the build_fund_breakdowns block —
# issuer data, with any per-card holdings override already applied)
# weighted by base-currency value.
#
# This is deliberately uniform across all four facets: every facet is a
# per-fund {key: weight} distribution, so one accumulator handles them
# all. A per-facet "covered value" tracks the base value of funds that
# contributed any data on that facet, so the frontend can show how much
# of the portfolio the card actually represents (a fund with an empty
# card — e.g. country with no override — contributes nothing and is not
# counted as covered).

def meta_facet_items(fund_structure: dict | None) -> dict[str, list[dict]]:
    """One-hot item lists for the metadata facets (v0.28.0).

    ``market_cap`` and ``style_box`` are scalars on a fund's structure
    block, not distributions — a fund is "large cap", never 70/30. To
    feed them through the same portfolio rollup as the four real
    breakdown facets, each is reshaped into a one-item distribution at
    weight 1.0.

    Every value is emitted, including ``"unknown"`` and ``"n/a"``. They
    are real buckets: a portfolio where a fifth of the money sits in
    funds nobody has classified should say so on the card, rather than
    quietly renormalising the other four fifths to 100% and reading as
    though the classification were complete. Neither is targetable (see
    ``config.META_FACET_TARGETABLE``), so both land in the deviation
    report's untargeted summary — visible, but not counted as a miss
    against a target the user never set.

    Args:
        fund_structure: A fund's effective structure block, or ``None``.
            Cash positions carry a synthetic one (see
            :func:`synth_enriched_for_cash_position`).

    Returns:
        ``{facet: [{"key", "weight"}], ...}`` for each facet in
        ``META_FACETS``. A facet whose value is missing entirely yields
        an empty list, and the fund then counts as uncovered there —
        which in practice only happens for a structure block that never
        went through ``normalise_fund_structure``.
    """
    fs = fund_structure if isinstance(fund_structure, dict) else {}
    out: dict[str, list[dict]] = {}
    for facet in META_FACETS:
        val = str(fs.get(facet) or "").strip().lower()
        out[facet] = [{"key": val, "weight": 1.0}] if val else []
    return out


def rollup_portfolio_fundlevel(enriched: list[dict],
                               total_base: float) -> dict:
    """Aggregate per-fund Fund/ETF-level cards into portfolio-level ones.

    Each fund contributes ``value_base × facet_item_weight`` to every
    facet bucket, reading the fund's ``data.fund_breakdowns`` block (as
    produced by :func:`build_fund_breakdowns`). Funds with an empty card
    on a facet contribute nothing there.

    This is a pure function: it reads only the ``enriched`` list and
    ``total_base``. It does no I/O.

    Args:
        enriched: Per-fund dicts as built by ``api_portfolio_view`` —
            each carries ``valuation`` (with ``value_base``) and
            ``data`` (with ``fund_breakdowns``).
        total_base: Portfolio total value in base currency. Denominator
            for per-facet coverage.

    Returns:
        ::

            {
              "fundlevel_breakdowns": {
                  "asset_class": [{"key","weight","value"}, ...],
                  "sector":      [...],
                  "country":     [...],
                  "currency":    [...],
                  "market_cap":  [...],
                  "style_box":   [...],
              },
              "fundlevel_coverage": {
                  "asset_class": 0.97, "sector": 0.81, ...
              },
            }

        Each item ``weight`` is a fraction of the COVERED portfolio
        value for that facet; ``value`` is base-currency money. Items
        are sorted by weight descending.

        v0.28.0: six facets, not four. The last two are the metadata
        facets — one-hot per fund rather than distributions, and read
        off each entry's ``data.fund_structure`` rather than its
        breakdown cards. They aggregate identically once reshaped,
        which is the whole point of reshaping them.
    """
    buckets:       dict[str, dict[str, float]] = {f: {} for f in TARGET_FACETS}
    covered_value: dict[str, float] = {f: 0.0 for f in TARGET_FACETS}

    for e in enriched:
        v = (e.get("valuation") or {}).get("value_base")
        if v is None or v <= 0:
            continue
        data = e.get("data") or {}
        fb = data.get("fund_breakdowns") or {}
        if not isinstance(fb, dict):
            fb = {}
        # The meta facets are not cards and have no source selector —
        # they are one-hot reshapes of the fund's own structure block.
        # Derived here rather than baked into fund_breakdowns because
        # that block is rebuilt in five places (source toggles, upload
        # commits) that have no reason to know about fund metadata, and
        # any one of them forgetting would silently drop the facet.
        meta = meta_facet_items(data.get("fund_structure"))
        fv = float(v)
        for facet in TARGET_FACETS:
            if facet in META_FACETS:
                items = meta.get(facet) or []
            else:
                items = (fb.get(facet) or {}).get("items") or []
            if not items:
                continue
            # The fund has data on this facet → its whole value counts
            # as covered, regardless of whether the item weights sum to
            # exactly 1.0 (issuer fractions sometimes don't).
            covered_value[facet] += fv
            for it in items:
                key = (it.get("key") or "").strip()
                if not key:
                    key = "undefined"
                try:
                    w = float(it.get("weight") or 0.0)
                except (TypeError, ValueError):
                    continue
                buckets[facet][key] = buckets[facet].get(key, 0.0) + fv * w

    fundlevel_breakdowns: dict[str, list[dict]] = {}
    fundlevel_coverage:   dict[str, float] = {}
    for facet in TARGET_FACETS:
        covered = covered_value[facet]
        fundlevel_coverage[facet] = (
            round(covered / total_base, 6) if total_base > 0 else 0.0)
        if covered <= 0:
            fundlevel_breakdowns[facet] = []
            continue
        # Normalise against the summed bucket money rather than `covered`
        # directly — issuer fractions may not total 1.0 per fund, and the
        # card should still read as a 100% distribution.
        bucket_total = sum(buckets[facet].values())
        denom = bucket_total if bucket_total > 0 else covered
        items: list[dict] = []
        for k, val in buckets[facet].items():
            items.append({
                "key":    k,
                "weight": round(val / denom, 6),
                "value":  round(val, 2),
            })
        items.sort(key=lambda x: (x["key"] == "undefined", -x["weight"]))
        fundlevel_breakdowns[facet] = items

    return {
        "fundlevel_breakdowns": fundlevel_breakdowns,
        "fundlevel_coverage":   fundlevel_coverage,
    }


# ---------------------------------------------------------------------------
# Portfolio holdings aggregation — many funds' holdings → one merged list
# ---------------------------------------------------------------------------
# The portfolio Holdings sub-tab shows every underlying position across
# every fund in the portfolio, with positions for the *same* holding held
# by *different* funds merged into a single row.
#
# Match key (which field decides "same holding") is an app-level setting,
# one of "name" / "ticker" / "isin". A blank value for the chosen key
# never matches anything — each such holding stays its own row.
#
# Weight maths. A holding sits inside a fund at an intra-fund weight
# (row["weight_pct"], a percent). The fund sits in the portfolio at a
# portfolio weight (fund_value_base / portfolio_total). So a holding's
# contribution to the portfolio, in base-currency money, is:
#
#     fund_value_base * (row["weight_pct"] / 100)
#
# summed over every fund that holds it. The merged row's portfolio
# weight is that summed money over the portfolio total; its
# "value_base" is the summed money itself.
#
# Coverage residual. The Portfolio Value column must sum to the true
# portfolio total. Funds with no usable holdings, and the un-looked-
# through remainder of funds whose rows sum to < 100%, are gathered into
# one synthetic "Unclassified" row carrying exactly:
#
#     portfolio_total - sum(real merged holding values)
#
# so the column reconciles to the total by construction.
#
# Field overrule (the same holding, conflicting non-blank values across
# funds). For each output field, among all contributing funds that have
# a non-blank value, the winner is chosen by:
#   1. source rank — manual_upload (0) beats yahoo_enriched (1) beats
#      yahoo_top10 (2) / anything else (3);
#   2. then larger contributing fund — bigger fund_value_base wins.
# A blank field in one fund is simply filled from another fund that has
# it; only genuine non-blank disagreements invoke the ranking.

# Source trust ranking for the field-overrule tie-break. Lower = wins.
_SOURCE_RANK: dict[str, int] = {
    "manual_upload":  0,
    "yahoo_enriched": 1,
    "yahoo_top10":    2,
}
_SOURCE_RANK_DEFAULT = 3

# Output fields carried on a merged portfolio-holding row, excluding the
# identity/weight/value fields which are handled specially.
_MERGE_FIELDS: tuple[str, ...] = (
    "name", "ticker", "isin", "sector", "asset_class", "sub_class",
    "country", "currency",
)

# The synthetic residual row's match identity. Chosen so it cannot
# collide with any real holding's computed match key.
_RESIDUAL_KEY = "\x00__unclassified__"


def _norm_match_value(match_key: str, row: dict) -> str:
    """Return the normalised match-key value for a holdings row.

    ``name`` is case-folded, stripped of punctuation, and
    whitespace-collapsed — so ``"Apple Inc"`` and ``"APPLE INC."`` match.
    This is deterministic normalisation, not fuzzy matching: it removes
    only trivial spelling differences (case, punctuation, spacing), never
    does stemming or similarity scoring. ``ticker`` / ``isin`` are
    case-folded and stripped. A blank value returns ``""`` — the caller
    treats that as "no match" and keeps the row separate.
    """
    raw = ("" if row.get(match_key) is None else str(row.get(match_key))).strip()
    if not raw:
        return ""
    if match_key == "name":
        # Drop punctuation (keep alphanumerics and spaces), lowercase,
        # collapse runs of whitespace. "Apple Inc." -> "apple inc".
        cleaned = "".join(
            c if (c.isalnum() or c.isspace()) else " " for c in raw)
        return " ".join(cleaned.lower().split())
    return raw.lower()


def aggregate_portfolio_holdings(funds: list[dict],
                                 total_base: float,
                                 match_key: str = "ticker") -> dict:
    """Merge every fund's holdings into one portfolio-level holdings list.

    This is a pure function — it reads only the ``funds`` list and
    ``total_base`` passed in, and does no I/O.

    Args:
        funds: The ``enriched`` per-fund dicts as built by
            ``api_portfolio_view`` — each carries ``valuation``
            (``value_base``), ``data`` (``holdings_rows`` and
            ``holdings_source``), plus ``ticker`` / ``isin``. Funds are
            processed in list order; that order is the stable tiebreak
            when source rank and contribution are equal.
        total_base: Portfolio total value in base currency. Denominator
            for portfolio weights and the residual row.
        match_key: One of ``"name"`` / ``"ticker"`` / ``"isin"`` — the
            field deciding holding identity. Unknown values fall back to
            ``"ticker"``.

    Returns:
        ::

            {
              "rows": [
                {"name","ticker","isin","sector","asset_class",
                 "sub_class","country","currency",
                 "weight_pct",        # % of WHOLE portfolio
                 "portfolio_value",   # base-currency money
                 "fund_count",        # how many funds hold it
                 "is_residual"},      # True only for the synthetic row
                ...
              ],
              "match_key":          "ticker",
              "total_base":         123456.78,
              "covered_value":      107000.00,   # Σ real holding money
              "covered_pct":        0.8671,      # covered / total
              "funds_total":        7,
              "funds_with_holdings":5,
            }

        ``rows`` is sorted by ``portfolio_value`` descending, with the
        synthetic residual row (when non-zero) pinned last.
    """
    if match_key not in ("name", "ticker", "isin"):
        match_key = "ticker"

    # accumulator per merged holding:
    #   value      — Σ base-currency money contributed
    #   fund_ids   — set of contributing fund indices (for fund_count)
    #   fields     — per output field: current winning value + the
    #                (source_rank, -contribution, order) of its source
    acc: dict[str, dict] = {}
    # Insertion order of first-seen keys, so equal-value rows sort stably.
    order: list[str] = []

    funds_total         = len(funds)
    funds_with_holdings = 0
    covered_value       = 0.0
    # Blank-key holdings can't merge — give each a unique synthetic key.
    blank_seq           = 0

    for fund_idx, e in enumerate(funds):
        v = (e.get("valuation") or {}).get("value_base")
        if v is None or v <= 0:
            # Fund has no portfolio value — its holdings can't be
            # weighted in. Skipped; its value (if any) is captured by
            # the residual via the total.
            continue
        fund_value = float(v)
        data = e.get("data") or {}
        rows = data.get("holdings_rows") or []
        if not rows:
            continue
        source = data.get("holdings_source") or "none"
        src_rank = _SOURCE_RANK.get(source, _SOURCE_RANK_DEFAULT)

        contributed_any = False
        for row in rows:
            try:
                w = float(row.get("weight_pct") or 0.0)
            except (TypeError, ValueError):
                continue
            if w <= 0:
                continue
            # Money this position contributes to the portfolio.
            money = fund_value * (w / 100.0)
            if money <= 0:
                continue
            contributed_any = True

            mk = _norm_match_value(match_key, row)
            if not mk:
                # No match key — unique synthetic key, never merges.
                key = f"\x00blank\x00{blank_seq}"
                blank_seq += 1
            else:
                key = mk

            slot = acc.get(key)
            if slot is None:
                slot = {
                    "value":    0.0,
                    "fund_ids": set(),
                    "fields":   {},
                    # Tracks whether the merged row originated from a
                    # cash position. Sticky-True: if any contributing
                    # entry was cash, the merged row is cash. In
                    # practice this never collides (cash rows have
                    # blank ticker/isin and therefore get a unique
                    # synthetic key), so it's just a per-row flag.
                    "is_cash":  False,
                    # Annual interest rate (percent) when the row is
                    # cash, else 0.0. Carried through to the output
                    # so the Holdings sub-tab can show it in its
                    # interest column.
                    "interest": 0.0,
                }
                acc[key] = slot
                order.append(key)
            slot["value"] += money
            slot["fund_ids"].add(fund_idx)
            if e.get("is_cash"):
                slot["is_cash"] = True
                try:
                    slot["interest"] = float(row.get("interest") or 0.0)
                except (TypeError, ValueError):
                    pass

            # Field overrule. For each field, a non-blank value competes
            # on (source_rank, -contribution, fund order). Lower wins.
            rank_tuple = (src_rank, -money, fund_idx)
            for fld in _MERGE_FIELDS:
                val = row.get(fld)
                sval = ("" if val is None else str(val)).strip()
                if not sval:
                    continue
                cur = slot["fields"].get(fld)
                if cur is None or rank_tuple < cur[1]:
                    slot["fields"][fld] = (sval, rank_tuple)

        if contributed_any:
            funds_with_holdings += 1

    # Build output rows.
    rows_out: list[dict] = []
    for key in order:
        slot = acc[key]
        money = slot["value"]
        covered_value += money
        row: dict = {fld: (slot["fields"].get(fld, ("", None))[0])
                     for fld in _MERGE_FIELDS}
        row["weight_pct"] = (
            round(money / total_base * 100.0, 6) if total_base > 0 else 0.0)
        row["portfolio_value"] = round(money, 2)
        row["fund_count"]      = len(slot["fund_ids"])
        row["is_residual"]     = False
        row["is_cash"]         = slot.get("is_cash", False)
        row["interest"]        = slot.get("interest", 0.0)
        rows_out.append(row)

    rows_out.sort(key=lambda r: -r["portfolio_value"])

    # Synthetic residual row — whatever portfolio value the real merged
    # holdings did not account for (funds with no holdings, plus the
    # un-looked-through tail of partial-coverage funds). Emitted only
    # when materially non-zero so a fully-covered portfolio stays clean.
    residual = (total_base - covered_value) if total_base > 0 else 0.0
    if residual > 0.005:
        rows_out.append({
            **{fld: "" for fld in _MERGE_FIELDS},
            "name":            "Unclassified / not looked through",
            "weight_pct":      round(residual / total_base * 100.0, 6),
            "portfolio_value": round(residual, 2),
            "fund_count":      0,
            "is_residual":     True,
            "is_cash":         False,
            "interest":        0.0,
        })

    return {
        "rows":                 rows_out,
        "match_key":            match_key,
        "total_base":           round(total_base, 2),
        "covered_value":        round(covered_value, 2),
        "covered_pct":          (round(covered_value / total_base, 6)
                                 if total_base > 0 else 0.0),
        "funds_total":          funds_total,
        "funds_with_holdings":  funds_with_holdings,
    }


# ---------------------------------------------------------------------------
# Cash positions → synthetic enriched-fund entries
# ---------------------------------------------------------------------------
# Cash positions (v0.14.0) live on the portfolio, not in the fund
# cache. To integrate them into the portfolio-level breakdowns,
# holdings list, and price history without forking every rollup
# function, we synthesise an enriched-fund-shaped dict per cash
# position and inject it into the `enriched` list before all three
# rollups run.
#
# Each synthetic entry:
#   * carries a current base-currency value (principal × exp(r × t) ×
#     FX_to_base), where t is years since the position's effective
#     date and r is the annual interest rate as a decimal;
#   * exposes a single-key breakdown for each facet (asset_class,
#     sector, country, currency) so the portfolio rollups see a
#     fully-classified one-row "fund";
#   * carries a one-row ``holdings_rows`` list mirroring the position,
#     so the aggregate Holdings sub-tab gets the position as its own
#     row, weighted by its share of the portfolio total.
#
# The position is given ``holdings_source = "manual_upload"`` so it
# wins source-rank ties against any Yahoo-sourced holding it might
# collide with — though in practice a cash position should never
# share a match key with a fund holding.
#
# The dict's outer fields (``ticker`` / ``isin``) are set to synthetic
# but distinct strings so duplicate-detection logic that keys on
# ticker/isin treats each cash position as its own thing.

import math


def synth_enriched_for_cash_position(pos: dict,
                                     base_currency: str,
                                     fx_to_base: float,
                                     now_utc=None) -> dict:
    """Build an enriched-fund-shaped dict from a cash position.

    Args:
        pos: Normalised cash position dict (see
            :func:`porxpy.utils.coerce_cash_position`). Required keys:
            id, name, amount, currency, interest, effective_date,
            asset_class, sub_class, country, sector.
        base_currency: The portfolio's base currency. Used only for
            the synthetic entry's currency-side metadata; the
            position's actual currency is preserved on the breakdown.
        fx_to_base: The cash position's currency → base-currency rate
            (current spot). 1.0 when the position is already in base.
            For historic dates, the caller (price-history aggregator)
            applies a per-date rate separately.
        now_utc: Optional override for "now" (used by tests). When
            ``None``, uses datetime.now(timezone.utc).

    Returns:
        A dict shaped like one entry in ``api_portfolio_view``'s
        ``enriched`` list, with:
            - valuation.value_base (accrued, in base currency)
            - data.holdings_breakdowns (one bucket per facet)
            - data.fund_breakdowns      (same)
            - data.holdings_rows        (one row mirroring the position)
            - data.holdings_source      ("manual_upload")
            - ticker / isin / name      (synthetic, prefixed "cash:")
            - is_cash                   (sentinel flag for callers that
                                         want to skip cash positions in
                                         fund-only loops)
    """
    from datetime import datetime, timezone

    now = now_utc or datetime.now(timezone.utc)

    # Years elapsed since effective_date. The position dict stores
    # the date in DD/mmm/YYYY form; parse it and fall back to zero
    # elapsed (no accrual yet) on blank/unparseable values.
    eff_raw = (pos.get("effective_date") or "").strip()
    t_years = 0.0
    if eff_raw:
        try:
            eff_dt = datetime.strptime(eff_raw, "%d/%b/%Y").replace(
                tzinfo=timezone.utc)
            delta_days = (now - eff_dt).total_seconds() / 86400.0
            if delta_days > 0:
                t_years = delta_days / 365.25
        except ValueError:
            t_years = 0.0

    principal = float(pos.get("amount") or 0.0)
    rate_pct  = float(pos.get("interest") or 0.0)  # interest is in %
    rate      = rate_pct / 100.0

    # Continuous compounding — smooth curve, negligible difference vs
    # annual at typical rates, and matches the price-history math so
    # the chart and the breakdown total agree to the cent.
    accrued_in_currency = principal * math.exp(rate * t_years) if t_years > 0 \
                          else principal
    value_base = accrued_in_currency * float(fx_to_base or 0.0)

    pos_currency    = (pos.get("currency")    or "").upper().strip()
    pos_country     = (pos.get("country")     or "").strip()
    pos_sector      = (pos.get("sector")      or "").strip()
    pos_asset_class = (pos.get("asset_class") or "").strip()
    pos_sub_class   = (pos.get("sub_class")   or "").strip()

    # Per-facet single-key breakdown shape — weight 1.0 means "all of
    # this position's money belongs to that bucket". Blank facets are
    # left out of the list so the rollup's "covered value" logic
    # naturally excludes them, mirroring how a fund with no sector
    # data isn't counted as covered for sector.
    def _single_facet(key: str) -> list[dict]:
        return [{"key": key, "weight": 1.0}] if key else []

    holdings_breakdowns = {
        "asset_class": _single_facet(pos_asset_class),
        "sector":      _single_facet(pos_sector),
        "country":     _single_facet(pos_country),
        "currency":    _single_facet(pos_currency),
    }
    fund_breakdowns = {
        "asset_class": {"items": _single_facet(pos_asset_class)},
        "sector":      {"items": _single_facet(pos_sector)},
        "country":     {"items": _single_facet(pos_country)},
        "currency":    {"items": _single_facet(pos_currency)},
    }

    # One holdings row mirroring the position. weight_pct here is "%
    # of the synthetic one-position fund" — i.e. always 100.0 — so
    # aggregate_portfolio_holdings's `fund_value × (weight_pct/100)`
    # arithmetic yields exactly the position's base-currency value.
    holdings_row = {
        "name":            pos.get("name") or "(unnamed cash position)",
        "ticker":          "",
        "isin":            "",
        "sector":          pos_sector,
        "asset_class":     pos_asset_class,
        "sub_class":       pos_sub_class,
        "country":         pos_country,
        "currency":        pos_currency,
        "weight_pct":      100.0,
        # Bond columns — duration / coupon / maturity are blank; the
        # cash position's interest rate IS the "coupon equivalent",
        # surfaced as the row's interest field via the bond-columns
        # toggle if the user has it on.
        "interest":        rate_pct,
        "effective_date":  eff_raw,
        # Cash positions don't have a stable per-row id of the
        # holdings flavour; the position's own id is unique among
        # the synthetic rows, so use it directly.
        "_row_id":         pos.get("id") or "",
    }

    return {
        # Identity — synthetic prefixed strings so any duplicate
        # detection on ticker/isin treats each cash position as
        # distinct from every fund and every other cash position.
        "ticker":      f"cash:{pos.get('id', '')}",
        "isin":        f"CASH-{pos.get('id', '')}",
        "name":        pos.get("name") or "(unnamed cash position)",
        "is_cash":     True,
        # Cash positions are weighted in via valuation.value_base, not
        # via shares — set shares to None so any fund-side code that
        # branches on `shares is None` (e.g. the "unvalued" warning)
        # treats cash uniformly.
        "shares":      None,
        # Mirror the fund-side ``effective_asset_class`` field — some
        # downstream consumers (notably the X-ray portfolio view) read
        # it directly off the enriched dict rather than walking
        # data.asset_class.
        "effective_asset_class": pos_asset_class or "other",
        # Marks the entry so any fund-specific code path (e.g. the
        # /api/fund/... lookup) can skip it cleanly.
        "valuation": {
            "value_base":        value_base,
            "value_native":      accrued_in_currency,
            "native_currency":   pos_currency,
            "adjusted_currency": pos_currency,
            "fx_to_base":        float(fx_to_base or 0.0),
        },
        "data": {
            "ticker":              f"cash:{pos.get('id', '')}",
            "holdings_source":     "manual_upload",
            "holdings_rows":       [holdings_row],
            "holdings_breakdowns": holdings_breakdowns,
            "fund_breakdowns":     fund_breakdowns,
            # Asset class lives at the top level too in real funds — we
            # surface it here so any consumer reading data.asset_class
            # gets the same value as the breakdown.
            "asset_class":         {"class": pos_asset_class},
            # v0.28.0 — the meta facets, in the shape
            # rollup_portfolio_fundlevel reads them from. Market cap is
            # "n/a" rather than "unknown": there is no market cap to
            # discover for a bank balance, which is a different claim
            # from not having discovered one. Style box is "value" for
            # the same reason a bond fund is — the return is interest,
            # not capital appreciation.
            "fund_structure":      {"market_cap": "n/a",
                                    "style_box":  "value"},
        },
        # Pass through the original position so the caller has a
        # handle for the price-history path (which needs the
        # principal, rate, and effective_date again).
        "cash_position": pos,
    }


def cash_position_value_on_date(pos: dict, date_str: str) -> float:
    """Accrued value of a cash position on a given date, in its currency.

    Args:
        pos: Normalised cash position dict.
        date_str: Date in ``"YYYY-MM-DD"`` form (the format pandas
            history rows use).

    Returns:
        ``principal × exp(rate × t)`` where t is years between the
        position's effective_date and the given date (clamped to
        ``[0, ∞)`` — no negative-time discounting). Returns ``0.0``
        for a position whose effective_date is after the given date
        (the position didn't exist yet).
    """
    from datetime import datetime

    principal = float(pos.get("amount") or 0.0)
    rate_pct  = float(pos.get("interest") or 0.0)
    rate      = rate_pct / 100.0
    eff_raw   = (pos.get("effective_date") or "").strip()

    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return 0.0

    if not eff_raw:
        # No effective date — position is treated as having always
        # existed; full principal × accrual since target=today is
        # what the breakdown sees too, so for any historic date we
        # report just the principal (no accrual basis).
        return principal

    try:
        eff = datetime.strptime(eff_raw, "%d/%b/%Y")
    except ValueError:
        return principal

    if target < eff:
        return 0.0
    delta_days = (target - eff).total_seconds() / 86400.0
    t = max(0.0, delta_days / 365.25)
    return principal * math.exp(rate * t)
