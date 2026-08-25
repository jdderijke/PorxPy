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

The derivation levels, and the function for each:

* :func:`resolve_facet_value` — resolve one raw facet value to a
  canonical rollup-bucket key (shared by every rollup below).
* :func:`rollup_holdings` — **fund / holding level.** One fund's
  per-position rows → that fund's look-through breakdowns.
* :func:`build_fund_breakdowns` — **fund level.** Resolves each of the
  four cards to its pinned source.
* :func:`rollup_portfolio_fundlevel` — **portfolio level.** A list of
  already-valued funds (each carrying its own ``build_fund_breakdowns``
  block) → the portfolio-wide cards. Every fund contributes on the basis
  its own card is set to, so there is no portfolio-wide source switch.

For backwards compatibility, :mod:`porxpy.utils` re-exports
``resolve_facet_value`` and ``rollup_holdings`` so existing
``from porxpy.utils import ...`` call sites keep working.
"""

from __future__ import annotations

from typing import Any

from porxpy.config import (BREAKDOWN_SOURCES, FACET_DEFAULT_LEVEL,
                           FACET_LEVELS, FACET_NOT_APPLICABLE,
                           META_FACETS, NA_KEY, SUPPLIED_BREAKDOWN_SOURCES,
                           TARGET_FACETS, UNKNOWN_KEY)


# The two residuals. Weight that lands in no real bucket is either a gap
# somebody could close (UNKNOWN_KEY) or a question that does not apply to
# the position (NA_KEY) — see the note beside them in config.
#
# Neither is renormalised away on its own: a top-10 list covering a fifth
# of a fund describes a fifth of a fund, and scaling it to 100% by
# default would invent the rest.
#
# v0.77.0 adds the one exception, and it is an exception the USER makes,
# per fund and per facet: `breakdown_complete.<facet>`. Where no source
# can supply more — the issuer publishes a top-10 and nothing else, and
# there is no factsheet and no file to be had — an `unknown` slice is not
# a gap anyone can close, and leaving it in place makes the fund useless
# to the optimiser rather than merely incompletely described. The
# assertion says "read this source's coverage as the whole fund", and
# assume_complete_items below is what carries it out. Nothing asserts it
# automatically: the default is still, and deliberately, that a fifth is
# a fifth.
_RESIDUAL_KEYS = (UNKNOWN_KEY, NA_KEY)


def _blank_facet_key(facet: str, asset_class: str) -> str:
    """Which residual a blank ``facet`` value becomes for this position.

    A cash row has no sector — no source will ever supply one, and
    counting it as a gap left cash-heavy portfolios permanently
    under-covered. Anything else is a gap: the value exists and whoever
    filled in this row did not have it.
    """
    return (NA_KEY
            if asset_class in FACET_NOT_APPLICABLE.get(facet, frozenset())
            else UNKNOWN_KEY)


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
# by resources.py). resolve_facet_value now calls
# resources.resolve_asset_tree so there is a single maintained
# authority for the fund-level vocabulary rather than three hand-kept
# copies. To add a spelling, edit the `matches` column in that CSV.


def resolve_facet_node(facet: str, raw: Any) -> tuple[str, str]:
    """Resolve a value to its canonical node AT THE LEVEL IT NAMED.

    The difference from :func:`resolve_facet_value` is the whole reason
    levelled blocks work. That function answers "what is this at the
    facet's default level", which is what a flat consumer wants; a
    factsheet saying "Cyclical" therefore comes back ``unknown``,
    because cyclical is not a sector.

    Folding a value to one level before deriving the others destroys
    what the source actually said: a super-sector-only factsheet would
    report NO level as available, its own included. The block stores the
    deepest thing the source said and derives every level from it, so
    this is the resolution the block builder uses.

    Returns:
        ``(node, unresolved_raw)`` — same contract as
        :func:`resolve_facet_value`, but ``node`` may sit at any of the
        facet's levels.
    """
    from porxpy.resources import resolve_country_tree, resolve_sector_tree

    s = ("" if raw is None else str(raw)).strip()
    if not s or s == "-":
        return "", ""
    if s in _RESIDUAL_KEYS:
        return s, ""

    if facet == "sector":
        tree = resolve_sector_tree(s)
        return (tree["matched"], "") if tree["level"] else ("", s)

    if facet == "country":
        tree = resolve_country_tree(s)
        return (tree["matched"], "") if tree["level"] else ("", s)

    if facet == "asset_class":
        # Asset was missing from this list until v0.76.0, so it alone
        # fell through to resolve_facet_value below — which answers at
        # the facet's own level, not at the level the source named. The
        # asset tree fills a finer level through single-child nodes, so
        # "Aandelen" (an alias of the super class *equity*) came back as
        # the middle-level "shares and options": a grain no source had
        # stated. A holdings row carrying the same word resolved to
        # equity, because normalise_facets always used the tree. One
        # spelling, two answers, depending only on whether it arrived in
        # a row or in a factsheet.
        from porxpy.resources import resolve_asset_tree
        tree = resolve_asset_tree(s)
        return (tree["matched"], "") if tree["level"] else ("", s)

    # Single-level facets: the node IS the value.
    return resolve_facet_value(facet, raw)


def resolve_facet_value(facet: str, raw: Any) -> tuple[str, str]:
    """Resolve one raw facet value, reporting whether it resolved.

    **v0.59.0.** Replaces ``canonicalise_facet_key``, which returned a
    bucket key and nothing else — so a value the taxonomy had never
    heard of came back as its own lowercased self and became a slice on
    the card, sitting beside real keys as though it were one of them.
    "Diversified Holdings" is not a sector; the fund does not hold 8% of
    it. That conflated two different statements — *this fund holds 8% of
    that thing* and *the source said something we could not place* — and
    only the first belongs in a distribution.

    Two consequences of separating them:

    * Coverage becomes monotonic across the sector levels. The
      non-monotonic case — covered at sector, unknown at super — existed
      only because an unrecognised value passed through at sector level
      and could go no further.
    * The tree derivation loses its special case: recognised values roll
      up, everything else is ``unknown``, at every level.

    Nothing about *storage* changes. Every store already preserves the
    raw value on a miss — a holdings row keeps it in the column, a
    factsheet item keeps it as its key — so resolution happens here, on
    the way to a bucket, and re-runs on every read. That is what makes
    an added alias repair history with no migration: the next read
    resolves what the previous read could not.

    Args:
        facet: ``"country"``, ``"region"``, ``"asset_class"``,
            ``"currency"`` or ``"sector"``. Unknown facets pass through
            as resolved, stripped — they are not resource-backed and
            have no vocabulary to fail against.
        raw: The raw facet value.

    Returns:
        ``(key, unresolved_raw)``.

        * ``("", "")`` — blank input. The caller picks a residual.
        * ``(key, "")`` — resolved; ``key`` is canonical.
        * ``("", raw)`` — the source said something, and it named
          nothing in the vocabulary. The caller folds the weight into
          ``unknown`` and records ``raw`` so the user can resolve it.
    """
    # Local import to avoid a circular reference at module import time —
    # resources.py only depends on config, not on utils, so this is safe.
    from porxpy.resources import (
        country_to_mstar, resolve_currency,
        resolve_region_facet, resolve_sector_tree,
    )

    s = ("" if raw is None else str(raw)).strip()
    if not s or s == "-":
        return "", ""

    # Residual keys are answers, not values to re-resolve. A rollup
    # feeding its own output back through here — which build_fund_
    # breakdowns does for the holdings source — must not report
    # "unknown" as an unresolved raw value.
    if s in _RESIDUAL_KEYS:
        return s, ""

    if facet == "country":
        mstar = country_to_mstar(s)
        if mstar:
            return mstar, ""
        # A value naming a region is a real answer that this level
        # cannot carry — "Europe ex-UK" tells us something true, just
        # not which country. Reporting it unresolved would put it in the
        # dialog asking to be mapped to a country, and any such mapping
        # would claim the source said more than it did. Unknown here,
        # answered in the region view, and silent in the dialog.
        if resolve_region_facet(s):
            return "", ""
        return "", s

    if facet == "super_region":
        from porxpy.resources import resolve_country_tree
        tree = resolve_country_tree(s)
        if tree["level"]:
            k = tree["super_region"]
            return (k, "") if k not in ("unknown",) else ("", "")
        return "", s

    if facet == "region":
        # A country column may name a region — "Europe ex-UK" is a real
        # answer, just not to the country question. Resolved here so the
        # region view can use it; the country view reports it unknown.
        reg = resolve_region_facet(s)
        if reg:
            return reg, ""
        mstar = country_to_mstar(s)
        if mstar:
            from porxpy.resources import MSTAR_TO_REGION
            derived = MSTAR_TO_REGION.get(mstar) or ""
            return (derived, "") if derived else ("", "")
        return "", s

    if facet in ("asset_class", "sub_class", "super_class"):
        # One tree (Asset_definitions.csv), answered at the level asked
        # for. Until v0.70.0 this read Fund_class_definitions.csv while
        # holdings rows read Holdings_class_definitions.csv — two files
        # describing one taxonomy at two grains, disagreeing on spelling
        # ("bond" against "fixed_income") for the same concept.
        #
        # A value that resolves but cannot reach the level asked for is
        # NOT a miss: "equity" is a perfectly good answer that simply
        # says nothing at sub-class grain. Reporting it as unresolved
        # would send the user to the Resolve dialog to fix a value that
        # was never wrong.
        from porxpy.resources import resolve_asset_tree
        tree = resolve_asset_tree(s)
        if not tree["level"]:
            return "", s
        key = tree.get(facet) or UNKNOWN_KEY
        return ("", "") if key == UNKNOWN_KEY else (key, "")

    if facet == "currency":
        # Validated against currencies.csv for the first time in
        # v0.59.0. It used to be uppercased and accepted, so an
        # unrecognised currency was indistinguishable from a real one
        # and no dialog could ever surface it.
        code = resolve_currency(s)
        return (code, "") if code else ("", s)

    if facet == "sector":
        tree = resolve_sector_tree(s)
        if tree["level"]:
            # The rollup's "sector" bucket is the middle level. A value
            # that matched only at super-sector level has no sector to
            # report — that is a gap in what the source said, not an
            # unresolved value, so it is not offered for aliasing.
            return (tree["sector"] if tree["sector"] not in _RESIDUAL_KEYS
                    else UNKNOWN_KEY), ""
        return "", s

    # Not a resource-backed facet — nothing to fail against.
    return s, ""


# ---------------------------------------------------------------------------
# Fund / holding level — one fund's per-position rows → its breakdowns
# ---------------------------------------------------------------------------
def rollup_holdings(rows: list[dict]) -> dict:
    """Compute look-through breakdowns from a full per-position holdings list.

    Each input row carries ``weight_pct`` (a number, percent — 5.34 means
    5.34%) plus textual fields ``sector``, ``currency``, ``country``,
    ``asset_class``. Blank values are kept rather than dropped, so the
    visible buckets always sum to 100% and the reader can see how much of
    the rollup is unclassified. Which residual a blank lands in depends on
    the row: a cash position has no sector to find, so it is ``"n/a"``,
    while a derivative or a gap in the upload is ``"unknown"`` — see
    :data:`~porxpy.config.FACET_NOT_APPLICABLE`.

    Facet values are resolved via :func:`resolve_facet_value`
    before bucketing — see that function for the per-facet rules.

    Returned weights are FRACTIONS (0–1) of the WHOLE FUND, not of the
    rows supplied. A holdings list that covers 60% of a fund yields items
    summing to 0.60 plus an ``"unknown"`` bucket of 0.40 — it is not
    rescaled to 100%, because doing so would report a top-10 list as if
    it were the entire portfolio. Per-facet ``coverage`` is the share the
    rows genuinely account for.

    Args:
        rows: Full-holdings rows, as produced by a holdings upload
            commit or by Yahoo enrichment.

    Returns:
        ::

            {
              "sector":      [{"key": "Technology",   "weight": 0.34},
                              {"key": "unknown",      "weight": 0.02}, ...],
              "currency":    [{"key": "USD",          "weight": 0.71}, ...],
              "country":     [{"key": "unitedstates", "weight": 0.61}, ...],
              "asset_class": [{"key": "equity",       "weight": 0.97}, ...],
              "coverage":    {"sector": 1.0, "currency": 1.0,
                              "country": 1.0, "asset_class": 1.0},
              "total_weight_pct": 99.87,
            }
    """
    # sub_sector rides alongside sector rather than replacing it. The
    # two answer different questions of the same rows, and every existing
    # consumer — targets, the deviation report, the X-ray cards — reads
    # "sector" meaning the middle level. Widening that name would have
    # changed what it means to all of them at once.
    #
    # v0.59.0 adds "region" beside "country" on exactly the same
    # footing. A country column may name a region — "Europe ex-UK" is a
    # real answer, just not to the country question — and until then
    # such a value became a bucket called "europe ex-uk" sitting among
    # the countries. Deriving the region for every row also means the
    # region view is aggregated from what each row actually said rather
    # than re-bucketed afterwards.
    facets = ("sector", "sub_sector", "currency", "country", "region",
              "super_region", "asset_class", "sub_class", "super_class")
    empty  = {
        **{f: [] for f in facets},
        "coverage":         {f: 0.0 for f in facets},
        "unresolved":       {f: [] for f in facets},
        "total_weight_pct": 0.0,
    }
    if not rows:
        return empty

    # Per-facet bucket dict keyed by the textual facet value. One row
    # contributes its weight to every facet's bucket — to a real key, or
    # to one of the two residuals when the row's value is blank.
    buckets: dict[str, dict[str, float]] = {f: {} for f in facets}
    # Weight behind each raw value the vocabulary did not recognise,
    # per facet. Not a bucket of its own — these weights are already
    # inside "unknown" — but the annotation that lets the card say what
    # its unknown slice is made of and the dialog say what to fix.
    unresolved: dict[str, dict[str, float]] = {f: {} for f in facets}
    total_w = 0.0

    for r in rows:
        try:
            w = float(r.get("weight_pct") or 0.0)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        total_w += w
        # A blank sector means one thing on an equity row and another on
        # a cash row, so the row's own asset class decides which residual
        # it lands in.
        # normalise_facets has already resolved the row's asset tree, so
        # the column is canonical. Re-resolving it here would ask the
        # same question twice and could answer differently.
        row_ac = (r.get("asset_class") or "").strip().lower()
        for facet in facets:
            raw_miss = ""
            if facet == "sub_sector":
                # Derived, never read from a column of its own: the row
                # stores the deepest value it matched plus which level
                # that is, and a sub sector exists only when the match
                # went that deep. A row matched at sector level has a sub
                # sector — the source just did not say which — so this is
                # a gap, not an inapplicable question.
                node = (r.get("sector_node") or "").strip().lower()
                # ...unless the row resolved to the n/a residual, which
                # is not a level-specific answer: a sovereign issuer has
                # no line of business at ANY grain, so "no sector
                # applies" holds for the sub-sector view too. That is the
                # rule sector_key_at_level already states for bucket keys
                # ("residuals pass through unchanged at every level");
                # this derivation simply did not follow it, so a treasury
                # sleeve counted against sub-sector coverage as a gap
                # nothing could ever close. Only the sub-sector view
                # needs saying: every other level reads its own column,
                # where normalise_facets has already put the residual.
                key = node if (node == NA_KEY
                               or (r.get("sector_level") or "") == "sub_sector") else ""
            elif facet in ("asset_class", "sub_class", "super_class"):
                # One tree, three levels, read from the level columns
                # normalise_facets already derived from the ONE value the
                # source stated. Reading them rather than re-resolving
                # keeps the buckets agreeing with the rows: a row that
                # said "equity" has a super class and an asset class (its
                # single child) and no sub class, and re-resolving from a
                # derived column would lose which of those the file
                # actually asserted.
                key = (r.get(facet) or "").strip().lower()
            elif facet in ("region", "super_region"):
                # Read from country_node — the deepest value the row
                # actually said — not from the derived country column.
                # A row whose file said "Europe (Developed)" has an
                # EMPTY country (there is no country to name) but a real
                # region, and reading the derived column would throw
                # that away and report the row as a gap.
                key, raw_miss = resolve_facet_value(
                    facet, r.get("country_node") or r.get("country"))
            else:
                key, raw_miss = resolve_facet_value(facet, r.get(facet))
            if raw_miss:
                unresolved[facet][raw_miss] = \
                    unresolved[facet].get(raw_miss, 0.0) + w
            if not key:
                key = _blank_facet_key(facet, row_ac)
            buckets[facet][key] = buckets[facet].get(key, 0.0) + w

    # Normalise against the WHOLE FUND, not against the rows we happen to
    # have.
    #
    # Holdings weights are percentages of the fund, so a complete list
    # sums to ~100. Dividing by the rows' own total made any list sum to
    # exactly 1.0 — which silently rescaled a partial list up to a whole
    # fund. A top-10 list covering 21% of a fund reported the largest
    # holding's sector at 4.7x its real weight, and the card claimed
    # 100% coverage while doing it.
    #
    # So the denominator is 100 unless the rows exceed it (rounding, or a
    # leveraged fund whose exposures genuinely sum above par), and the
    # shortfall becomes "unknown". The rows we are missing are holdings
    # like any other — they have sectors and countries, we simply do not
    # have the rows. That is a gap, never an inapplicable question.
    denom = max(total_w, 100.0)
    shortfall = max(0.0, denom - total_w)

    out: dict = {"total_weight_pct": round(total_w, 4)}
    for facet in facets:
        if total_w > 0:
            counts = dict(buckets[facet])
            if shortfall > 0:
                counts[UNKNOWN_KEY] = counts.get(UNKNOWN_KEY, 0.0) + shortfall
            items = [
                {"key": k, "weight": round(w / denom, 6)}
                for k, w in counts.items()
            ]
            # Sort by weight desc, but pin both residuals at the end so
            # consumers can show them as tail slices without resorting.
            items.sort(key=lambda x: (x["key"] in _RESIDUAL_KEYS, -x["weight"]))
        else:
            items = []
        out[facet] = items

    # Coverage is the share of the fund this facet has been ANSWERED for,
    # computed per facet rather than once for all of them.
    #
    # It used to be row completeness — total_w / denom — which is the
    # same number for every facet and says nothing about whether the
    # rows actually carried a value. A full holdings list where nothing
    # matched at sub-sector level read 100% covered while every slice
    # said "unknown". Excluding the unknown bucket generalises the old
    # meaning rather than replacing it: for a top-10 list whose sectors
    # are all known, the missing 79% IS the unknown bucket, so this still
    # reports 21%.
    #
    # "n/a" counts as covered. A cash sleeve has no sector and never
    # will, and scoring that as a gap left a cash-heavy fund permanently
    # short of 100% with nothing anyone could do about it.
    out["coverage"] = {}
    for facet in facets:
        if total_w <= 0:
            out["coverage"][facet] = 0.0
            continue
        unknown_w = sum(it["weight"] for it in out[facet]
                        if it["key"] == UNKNOWN_KEY)
        out["coverage"][facet] = round(max(0.0, 1.0 - unknown_w), 6)

    # Unresolved values, normalised against the same denominator as the
    # buckets so their weights are directly comparable to the unknown
    # slice they sit inside.
    out["unresolved"] = {}
    for facet in facets:
        rows_u = [{"raw": raw, "weight": round(w / denom, 6)}
                  for raw, w in unresolved[facet].items()] if total_w > 0 else []
        rows_u.sort(key=lambda x: -x["weight"])
        out["unresolved"][facet] = rows_u
    return out


# ---------------------------------------------------------------------------
# Fund-level breakdown cards — unified four-facet block
# ---------------------------------------------------------------------------
# The fund page and the portfolio X-ray both render four breakdown cards:
# asset_class / sector / country / currency. Each card is a *distribution
# over the fund's holdings* and has three possible data sources:
#
#   "yahoo"    — the issuer's own published aggregate of its holdings on
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
# Per fund, per facet, the user can override the source from "yahoo" to
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


def _resolve_items(facet: str,
                   items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Re-bucket one source's items, separating what would not resolve.

    Supplied sources store what the source said, in ``raw``, and
    resolution happens here on every read — which is what lets an alias
    added today repair a factsheet extracted last month with nothing
    rewritten.

    Until v0.76.0 this docstring described that behaviour while the
    commit path stored the RESOLVED node under ``key``, so a factsheet
    naming "Diversified Holdings" was filed as whatever it resolved to
    at ingest and this function re-resolved a canonical to itself. The
    claim was true of the reader and false of the writer, which is the
    hardest kind of wrong to notice.

    Returns:
        ``(items, unresolved)``. Items are canonical-keyed with
        unresolved weight folded into ``unknown``; ``unresolved`` is
        ``[{"raw", "weight"}]``, sorted heaviest first.
    """
    buckets: dict[str, float] = {}
    misses:  dict[str, float] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            w = float(it.get("weight") or 0.0)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        # ``raw`` is what the source said and is resolved here, on every
        # read; ``key`` is a user's pin and is taken as given. Before
        # v0.76.0 the stored ``key`` WAS the resolved node, so this
        # resolution re-resolved a canonical to itself and the docstring
        # above described a propagation that could not happen. Items
        # written under the old shape are dropped by the cache migration
        # rather than read here, so there is no third case.
        pinned = (it.get("key") or "").strip()
        key, raw_miss = ((pinned, "") if pinned
                         else resolve_facet_node(facet, it.get("raw")))
        if raw_miss:
            misses[raw_miss] = misses.get(raw_miss, 0.0) + w
        if not key:
            key = UNKNOWN_KEY
        buckets[key] = buckets.get(key, 0.0) + w

    out_items = [{"key": k, "weight": round(w, 6)} for k, w in buckets.items()]
    out_items.sort(key=lambda x: (x["key"] in _RESIDUAL_KEYS, -x["weight"]))
    out_miss = [{"raw": r, "weight": round(w, 6)} for r, w in misses.items()]
    out_miss.sort(key=lambda x: -x["weight"])
    return out_items, out_miss


def build_fund_breakdowns(holdings_breakdowns: dict,
                          sectors: list[dict] | None,
                          asset_allocation: list[dict] | None,
                          overrides: dict | None,
                          uploaded_facets: dict | None = None,
                          sources_present: dict | None = None,
                          completed: dict | None = None) -> dict:
    """Resolve the four fund-level breakdown cards into one uniform block.

    This is a pure function: it reads only its arguments and does no I/O.

    For each facet it reads the pinned source from ``overrides`` and
    emits that source's item list. **The pin is always honoured.** A
    source that exists but has nothing to say about this facet yields a
    single 100% ``"unknown"`` bucket rather than falling back to
    Yahoo — silently substituting another source made the selector look
    broken, because picking one with no data for that facet snapped the
    card straight back to Yahoo.

    "Has this source anything for this facet" and "does this source
    exist for this fund" are different questions, and only the second
    decides whether the selector offers it. A factsheet that omits the
    currency split is still a factsheet, and answering "unknown" from it
    is a real answer. That existence cannot be read off the arguments
    here — an empty holdings roll-up may mean no holdings at all or
    holdings that classified to nothing, and a factsheet leaves no trace
    in any of them — so callers pass it in ``sources_present``.

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
            from the map defaults to ``"yahoo"``.
        uploaded_facets: Per-facet, per-source supplied item lists (from
            :func:`porxpy.utils.uploaded_breakdowns_get`). Shape
            ``{facet: {source: [{"key","weight"}, ...]}}``. ``None`` is
            equivalent to all-empty.
        completed: ``{facet: bool}`` — facets the user has asserted are
            fully covered by their chosen source (the
            ``breakdown_complete.*`` override fields). For those, the
            card's ``unknown`` slice is dropped and the identified part
            scaled up to account for the fund; the block carries
            ``completed: True`` and keeps the pre-assertion coverage in
            ``identified``. Everything downstream — the portfolio X-ray,
            the target deviations, the optimiser — then reads the fund as
            fully described, which is the point: an ``unknown`` slice
            tells the solver nothing it can act on.
        sources_present: ``{"holdings": bool, "factsheet": bool}`` —
            whether the fund has any holdings at all, and whether it has
            a factsheet that has been extracted. Yahoo is always
            present; ``upload`` is per-facet, because a CSV covers the
            facets it covers and claims nothing about the others.
            ``None`` means neither is present.

    Returns:
        ::

            {
              "asset_class": {
                  "items":     [{"key","weight"}, ...],
                  "source":    "yahoo" | "factsheet" | "holdings" | "upload",
                  "available": {source: bool, ...},
              },
              "sector":   {...},
              "country":  {...},
              "currency": {...},
            }

        ``available`` says which sources the selector may offer. ``items``
        is never empty: a source with nothing for this facet gives
        ``[{"key": "unknown", "weight": 1.0}]``.

        Item weights are fractions (0-1). For ``"yahoo"`` they are issuer
        fractions as published (which may not sum to 1.0); for
        ``"holdings"`` the roll-up's fractions; for the supplied sources
        the fractions written at ingest.
    """
    overrides = overrides or {}
    hb = holdings_breakdowns if isinstance(holdings_breakdowns, dict) else {}
    ub = uploaded_facets if isinstance(uploaded_facets, dict) else {}
    sp = sources_present if isinstance(sources_present, dict) else {}

    # Issuer-published item lists per facet. Yahoo publishes only
    # asset_class and sector; country and currency have no issuer source
    # and answer "unknown" like any other source with nothing to say.
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
        # ``ub`` is {facet: {source: items}} since v0.44.0 - one entry per
        # supplied source, because a factsheet extraction and a user CSV
        # are both "someone handed us these numbers" and neither should
        # overwrite the other.
        per_source = (ub.get(facet) or {}) if isinstance(ub.get(facet), dict) else {}
        # A supplied item is real if it has EITHER — ``raw`` (the source's
        # wording, resolved by _resolve_items below) or ``key`` (a user's
        # pin). Testing only ``key`` was the pre-0.76.0 shape and dropped
        # every unpinned item here, so a factsheet extraction reached the
        # card as nothing at all. The holdings list above is different and
        # correctly tests ``key``: rollup_holdings resolves as it rolls
        # up, so its items are canonical by the time they arrive.
        supplied = {
            name: [it for it in (per_source.get(name) or [])
                   if isinstance(it, dict) and (it.get("raw") or it.get("key"))]
            for name in SUPPLIED_BREAKDOWN_SOURCES
        }

        items_by_source = {
            "yahoo":    issuer_items,
            "holdings": holdings_items,
            **supplied,
        }

        # Yahoo is always there. Holdings and the factsheet exist per
        # fund. An upload exists per facet: one CSV may carry sectors and
        # say nothing about countries, and offering "Upload" on the
        # country card would promise a source that was never given.
        available = {
            "yahoo":     True,
            "holdings":  bool(sp.get("holdings")),
            "factsheet": bool(sp.get("factsheet")),
            "upload":    bool(supplied.get("upload")),
        }

        src = overrides.get(facet, "yahoo")
        if src not in BREAKDOWN_SOURCES or not available.get(src):
            # The pin names a source this fund does not have — the
            # factsheet was deleted, the holdings were reset. Yahoo is
            # the only source that is always there.
            src = "yahoo"

        raw_items = [dict(it) for it in (items_by_source.get(src) or [])]

        # Resolve every source's keys through the one chokepoint, so a
        # value the vocabulary does not recognise counts as unknown
        # rather than becoming a slice of its own. The holdings source
        # arrives already resolved (rollup_holdings ran the same
        # function), so this is a no-op for it and its unresolved list
        # is carried through instead of recomputed — the rollup knows
        # the per-row weights, which the bucketed items no longer do.
        items, unresolved = _resolve_items(facet, raw_items)
        if src == "holdings":
            unresolved = [dict(u) for u in
                          ((hb.get("unresolved") or {}).get(facet) or [])]

        if not items:
            # The source exists but says nothing about this facet. That
            # is a gap in what it publishes, not an inapplicable
            # question — a different factsheet would answer it.
            items = [{"key": UNKNOWN_KEY, "weight": 1.0}]

        block = build_facet_block(facet, items)

        # The holdings rollup folds its sector buckets to sector level,
        # so its finer grain lives in a distribution of its own rather
        # than in the items — and the same for country/region. Those are
        # two independent rollups of the SAME rows, so they agree by
        # construction, and each is a better answer at its level than
        # anything derivable from the other. Where such a rollup exists,
        # it replaces the derived level.
        #
        # This is precisely why items are materialised per level rather
        # than derived on read: for this source the finer level is not a
        # function of the coarser one.
        if src == "holdings":
            for lv in block["levels"]:
                if lv in (facet, block["default_level"]):
                    continue
                native = [it for it in (hb.get(lv) or [])
                          if isinstance(it, dict) and it.get("key")]
                if native:
                    block["items"][lv]            = [dict(it) for it in native]
                    block["coverage"][lv]         = level_coverage(native)
                    block["levels_available"][lv] = any(
                        it["key"] not in _RESIDUAL_KEYS for it in native)

        # Which VIEWS each source could offer, not just the chosen one.
        # The card uses this to say "this source does not report at that
        # level, but another one does" — which is actionable where a
        # bare disabled button is not.
        #
        # It has to be answered from each source's own items. The
        # previous frontend asked the question by re-deriving the
        # CURRENT source's items under a different source name, so it
        # returned the same answer for every source and the tooltip was
        # decorative.
        lav_by_source: dict[str, dict[str, bool]] = {}
        for sname, sitems in items_by_source.items():
            if not available.get(sname):
                continue
            resolved, _ = _resolve_items(facet, sitems)
            sblock = build_facet_block(facet, resolved)
            if sname == "holdings":
                for lv in sblock["levels"]:
                    native = [it for it in (hb.get(lv) or [])
                              if isinstance(it, dict) and it.get("key")]
                    if native:
                        sblock["levels_available"][lv] = any(
                            it["key"] not in _RESIDUAL_KEYS for it in native)
            lav_by_source[sname] = sblock["levels_available"]

        # The user's completeness assertion, applied last so it acts on
        # the finished block — including the native finer levels the
        # holdings source substitutes above, which would otherwise keep
        # an unknown slice the chosen level no longer has.
        if (completed or {}).get(facet):
            assume_complete_block(block)

        block["source"]     = src
        block["available"]  = available
        block["levels_available_by_source"] = lav_by_source
        # What the unknown slice is made of. Empty when the gap is a
        # silence rather than something we failed to place.
        block["unresolved"] = unresolved
        out[facet] = block
    return out


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
    # {facet: {level: {key: money}}} — every level aggregated, always.
    #
    # A portfolio's sector data is three complete distributions exactly
    # as a fund's is. Each fund contributes to each level independently,
    # from its OWN block at that level: fund A reporting sub sectors and
    # fund B reporting only sectors both count in full at sector level,
    # while at sub-sector level B's whole weight is unknown. Nothing is
    # derived from another level's portfolio total.
    #
    # Doing it any other way — aggregating one level and re-cutting it —
    # would mean the portfolio could only ever be as fine as its
    # coarsest fund, which is the opposite of what the levels are for.
    def _levels_of(facet: str) -> tuple[str, ...]:
        return FACET_LEVELS.get(facet) or (facet,)

    buckets: dict[str, dict[str, dict[str, float]]] = {
        f: {lv: {} for lv in _levels_of(f)} for f in TARGET_FACETS}
    covered_value: dict[str, dict[str, float]] = {
        f: {lv: 0.0 for lv in _levels_of(f)} for f in TARGET_FACETS}

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
            for level in _levels_of(facet):
                if facet in META_FACETS:
                    items = meta.get(facet) or []
                else:
                    # This fund's OWN distribution at this level. A fund
                    # that cannot reach the level contributes unknown
                    # there and its real answer at the levels it can.
                    items = facet_items(fb.get(facet) or {}, level)
                if not items:
                    continue
                unknown_w = 0.0
                for it in items:
                    key = (it.get("key") or "").strip() or UNKNOWN_KEY
                    try:
                        w = float(it.get("weight") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if key == UNKNOWN_KEY:
                        unknown_w += w
                    buckets[facet][level][key] = \
                        buckets[facet][level].get(key, 0.0) + fv * w
                # Coverage measures the share of the portfolio this facet
                # has been ANSWERED for, so only "unknown" counts against
                # it. "n/a" is an answer — a cash sleeve has no sector and
                # never will — and scoring it as a gap left a cash-heavy
                # portfolio permanently short of 100% with nothing anyone
                # could do. Everything else counts in full, including
                # issuer fractions that don't quite total 1.0: an
                # incomplete answer is still an answer.
                #
                # Per level, because the same portfolio is genuinely
                # better covered at some grains than others.
                covered_value[facet][level] += fv * max(0.0, 1.0 - unknown_w)

    fundlevel_breakdowns: dict[str, dict] = {}
    fundlevel_coverage:   dict[str, dict[str, float]] = {}
    for facet in TARGET_FACETS:
        levels    = _levels_of(facet)
        per_level: dict[str, list[dict]] = {}
        cov_level: dict[str, float] = {}
        for level in levels:
            covered = covered_value[facet][level]
            cov_level[level] = (round(covered / total_base, 6)
                                if total_base > 0 else 0.0)
            if covered <= 0:
                per_level[level] = []
                continue
            # Normalise against the summed bucket money rather than
            # `covered` directly — issuer fractions may not total 1.0
            # per fund, and the card should still read as a 100%
            # distribution.
            bucket_total = sum(buckets[facet][level].values())
            denom = bucket_total if bucket_total > 0 else covered
            items = [{"key": k,
                      "weight": round(val / denom, 6),
                      "value":  round(val, 2)}
                     for k, val in buckets[facet][level].items()]
            items.sort(key=lambda x: (x["key"] in _RESIDUAL_KEYS, -x["weight"]))
            per_level[level] = items

        # The SAME block shape the fund cards read, so the portfolio
        # card, the fund card and facet_items() are one contract rather
        # than three. levels_available carries no source dimension here:
        # a portfolio has no source selector, its funds each have their
        # own.
        fundlevel_breakdowns[facet] = {
            "levels":           list(levels),
            "items":            per_level,
            "coverage":         cov_level,
            "levels_available": {
                lv: any(it["key"] not in _RESIDUAL_KEYS for it in per_level[lv])
                for lv in levels},
            "default_level":    FACET_DEFAULT_LEVEL.get(facet, levels[-1]),
        }
        fundlevel_coverage[facet] = cov_level

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
#   1. source rank — manual_upload (0) beats factsheet (1) beats
#      yahoo_enriched (2) beats yahoo_top10 (3) / anything else (4);
#   2. then larger contributing fund — bigger fund_value_base wins.
# A blank field in one fund is simply filled from another fund that has
# it; only genuine non-blank disagreements invoke the ranking.

# Source trust ranking for the field-overrule tie-break. Lower = wins.
_SOURCE_RANK: dict[str, int] = {
    "manual_upload":  0,
    # v0.77.0. A factsheet's position table is the issuer's own list,
    # with whatever sector / country / currency columns the issuer chose
    # to print beside it — the same kind of evidence an uploaded file
    # carries, and better than a per-symbol Yahoo lookup, which is a
    # third party's opinion about the security rather than the fund's
    # own statement. It ranks below an upload only because an upload is
    # usually the complete schedule where a factsheet prints the top ten.
    "factsheet":      1,
    "yahoo_enriched": 2,
    "yahoo_top10":    3,
}
_SOURCE_RANK_DEFAULT = 4

# Output fields carried on a merged portfolio-holding row, excluding the
# identity/weight/value fields which are handled specially.
_MERGE_FIELDS: tuple[str, ...] = (
    "name", "ticker", "isin", "sector", "asset_class", "sub_class",
    "country", "currency",
    # The stated value of each levelled facet. Merged rather than the
    # derived level columns, and re-derived from afterwards, because the
    # per-field overrule picks a winner INDEPENDENTLY for each field: a
    # merged row could otherwise take its sub sector from one fund and
    # its super sector from another and end up describing a tree that
    # does not exist. One fact wins, the rest follow from it.
    "asset_node", "sector_node", "country_node",
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
        # Re-derive the level columns from the nodes that won the merge,
        # so every level of the merged row describes the same tree. The
        # import is local: utils imports breakdowns for UNKNOWN_KEY, and
        # a module-level import here would close the cycle.
        from porxpy.utils import normalise_facets
        normalise_facets(row)
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
        # The stated value of each levelled facet, carried so the
        # portfolio holdings table can show a cash position at any level
        # its tree reaches. Without these the row arrives with one level
        # filled and the rest blank, and a region or sub-sector view
        # would show every cash position as a gap.
        "asset_node":      pos.get("asset_node") or "",
        "sector_node":     pos.get("sector_node") or "",
        "country_node":    pos.get("country_node") or "",
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


# ---------------------------------------------------------------------------
# Facet levels — derivation and the one accessor (v0.60.0)
# ---------------------------------------------------------------------------
# A facet with levels is one facet whose value is a tree. Aggregation
# happens at EVERY level: a fund's sector data is three complete
# distributions, each summing to the fund, describing the same money at
# three grains. Semiconductors 40% IS Technology 40% IS Cyclical 40%.
#
# The level selector chooses which finished distribution is displayed
# and nothing else. It never leaves the browser.
#
# Two rules, both one-directional:
#
#   * Rolling UP is derivation. Rolling DOWN would be invention — a key
#     naming only a sector cannot answer a sub-sector question, and a
#     super sector cannot be pushed back into a sector because
#     "cyclical" is not one.
#   * Weight that cannot reach a level becomes ``unknown``, never
#     dropped, so every level still sums to the fund.
#
# Since v0.59.0 there is no third rule. A key the taxonomy does not
# recognise used to pass through at sector level; it is now resolved to
# ``unknown`` before it ever reaches here, so these functions see only
# canonical names and residuals.


def sector_key_at_level(key: str, level: str) -> str | None:
    """Move one sector bucket key to ``level``, or ``None`` if it cannot.

    Args:
        key: A canonical sector-tree name, or a residual.
        level: One of ``sub_sector`` / ``sector`` / ``super_sector``.

    Returns:
        The key at that level, or ``None`` when the key cannot reach it.
        Residuals pass through unchanged at every level — ``n/a`` stays
        inapplicable however you look at it.
    """
    from porxpy.resources import (SECTOR_LEVEL_OF, SECTOR_SUPER,
                                  SUB_SECTOR_PARENT)

    if key in _RESIDUAL_KEYS:
        return key
    lvl = SECTOR_LEVEL_OF.get(key, "")
    if not lvl:
        return None
    if lvl == level:
        return key
    if level == "sub_sector":
        return None                     # cannot go finer
    sector = (SUB_SECTOR_PARENT.get(key) if lvl == "sub_sector"
              else key if lvl == "sector" else None)
    if level == "sector":
        return sector or None
    if lvl == "super_sector":
        return key
    return (SECTOR_SUPER.get(sector) or [None])[0] if sector else None


def country_key_at_level(key: str, level: str) -> str | None:
    """Move one country bucket key to ``level``, or ``None`` if it cannot.

    The country counterpart of :func:`sector_key_at_level`, with two
    levels rather than three. See :data:`~porxpy.config.FACET_LEVELS` for
    why there is no super-region level.
    """
    from porxpy.resources import (DEVELOPMENT_KEYS, MSTAR_TO_REGION,
                                  REGION_DEVELOPMENT, REGION_KEYS)

    if key in _RESIDUAL_KEYS:
        return key
    is_country = key in MSTAR_TO_REGION
    is_region  = key in REGION_KEYS
    is_dev     = key in DEVELOPMENT_KEYS

    if level == "country":
        return key if is_country else None

    region = (key if is_region
              else MSTAR_TO_REGION.get(key) if is_country
              else None)
    if level == "region":
        return region or None
    if level == "super_region":
        if is_dev:
            return key
        return (REGION_DEVELOPMENT.get(region) or None) if region else None
    return None


def _key_at_level(facet: str, key: str, level: str) -> str | None:
    """Dispatch to the right tree, or pass through for a flat facet."""
    if facet == "sector":
        return sector_key_at_level(key, level)
    if facet == "country":
        return country_key_at_level(key, level)
    if facet == "asset_class":
        from porxpy.resources import asset_key_at_level
        return asset_key_at_level(key, level)
    # Single-level facet: it answers at its own level and nowhere else.
    return key if level == FACET_DEFAULT_LEVEL.get(facet, facet) else None


def items_at_level(facet: str, items: list[dict], level: str) -> list[dict]:
    """Re-bucket one distribution at ``level``.

    Anything that cannot reach the level becomes ``unknown``: the weight
    is real and still has to appear, we simply cannot place it at this
    grain. That is what keeps every level summing to the same total.
    """
    buckets: dict[str, dict] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        k = _key_at_level(facet, it.get("key") or "", level) or UNKNOWN_KEY
        b = buckets.setdefault(k, {"key": k, "weight": 0.0})
        try:
            b["weight"] += float(it.get("weight") or 0.0)
        except (TypeError, ValueError):
            pass
        if it.get("value") is not None:
            try:
                b["value"] = b.get("value", 0.0) + float(it["value"])
            except (TypeError, ValueError):
                pass
    out = list(buckets.values())
    for b in out:
        b["weight"] = round(b["weight"], 6)
    out.sort(key=lambda x: (x["key"] in _RESIDUAL_KEYS, -x["weight"]))
    return out


def level_coverage(items: list[dict]) -> float:
    """Share of the distribution that has been ANSWERED at this level.

    ``unknown`` counts against coverage; ``n/a`` counts as answered — a
    cash sleeve has no sector and never will, and scoring that as a gap
    left cash-heavy funds permanently short of 100% with nothing anyone
    could do about it.

    Computed per level rather than once per facet, because the same fund
    is genuinely better covered at some grains than others.
    """
    unknown = sum(float(it.get("weight") or 0.0) for it in items or []
                  if isinstance(it, dict) and it.get("key") == UNKNOWN_KEY)
    return round(max(0.0, 1.0 - unknown), 6)


def assume_complete_items(items: list[dict]) -> list[dict]:
    """Read a partial distribution as if it accounted for the whole fund.

    Drops the ``unknown`` slice and scales what was identified up to fill
    the space it leaves — the answer to "if the part we could not place
    looks like the part we could, what is the split?".

    ``n/a`` keeps its weight and is not scaled. It means the question does
    not apply to that exposure — a cash sleeve has no sector — so it is
    not a gap that could be filled, and moving exposure into or out of it
    would state something false in both directions. The identified part
    is therefore scaled to ``1 - n/a``, not to 1.

    This also closes an IMPLICIT shortfall: issuer fractions that simply
    sum to 0.94 with no residual item are scaled up the same way, because
    the assertion is about coverage, not about how the source chose to
    express the gap.

    Returns ``items`` unchanged when nothing was identified — a card that
    is entirely unknown has no shape to lend the gap, and inventing one
    is precisely what this must not do. ``value`` (the portfolio cards'
    money column) is scaled with the weight so the two stay consistent.

    Args:
        items: One level's distribution, weights as fractions.

    Returns:
        A new list.
    """
    src = items or []
    na = sum(float(it.get("weight") or 0.0) for it in src
             if isinstance(it, dict) and it.get("key") == NA_KEY)
    known = sum(float(it.get("weight") or 0.0) for it in src
                if isinstance(it, dict) and it.get("key") not in _RESIDUAL_KEYS)
    if known <= 0:
        return [dict(it) for it in src if isinstance(it, dict)]

    scale = max(0.0, 1.0 - na) / known
    out: list[dict] = []
    for it in src:
        if not isinstance(it, dict):
            continue
        key = it.get("key")
        if key == UNKNOWN_KEY:
            continue
        row = dict(it)
        if key != NA_KEY:
            row["weight"] = round(float(it.get("weight") or 0.0) * scale, 6)
            if it.get("value") is not None:
                try:
                    row["value"] = float(it["value"]) * scale
                except (TypeError, ValueError):
                    pass
        out.append(row)
    out.sort(key=lambda x: (x["key"] in _RESIDUAL_KEYS, -x["weight"]))
    return out


def assume_complete_block(block: dict) -> dict:
    """Apply :func:`assume_complete_items` to every level of one facet block.

    Every level travels together (invariant 1), so the assertion is made
    at all of them or the card would mean different things depending on
    which chip is selected. A level with nothing identified is left alone
    and stays unavailable — asserting completeness cannot conjure a
    sub-sector split out of a card that only reaches sector.

    The block records what it did: ``completed`` is the flag, and
    ``identified`` keeps the coverage BEFORE the assertion, per level, so
    the fund page can show that 100% is asserted rather than measured.
    Mutates and returns ``block``.
    """
    block["identified"] = dict(block.get("coverage") or {})
    for lv in block.get("levels") or []:
        items = assume_complete_items((block.get("items") or {}).get(lv) or [])
        block["items"][lv]            = items
        block["coverage"][lv]         = level_coverage(items)
        block["levels_available"][lv] = any(
            it["key"] not in _RESIDUAL_KEYS for it in items)
    block["completed"] = True
    return block


def facet_items(block: dict, level: str | None = None) -> list[dict]:
    """The one accessor. Every consumer goes through it.

    No consumer indexes ``block["items"]["sector"]`` by hand — that is
    what makes adding a level to a facet a one-argument change at each
    call site rather than a hunt through nine files.

    Args:
        block: One facet's block from :func:`build_fund_breakdowns` or
            :func:`rollup_portfolio_fundlevel`.
        level: Which grain. ``None`` means the facet's default level —
            fixed per facet, never the deepest one present; see
            :data:`~porxpy.config.FACET_DEFAULT_LEVEL`.

    Returns:
        The item list, or ``[]`` for a level this block does not carry.
    """
    if not isinstance(block, dict):
        return []
    items = block.get("items")
    if not isinstance(items, dict):
        # Not a levelled block. Nothing should emit one after v0.60.0,
        # but a caller handed a bare list back gets it rather than [].
        return items if isinstance(items, list) else []
    lvl = level or block.get("default_level") or ""
    return items.get(lvl) or []


def build_facet_block(facet: str, items: list[dict]) -> dict:
    """Materialise one facet's levels from a single source's items.

    Args:
        facet: The facet name.
        items: That source's distribution, at whatever grain it came in.

    Returns:
        ``{levels, items, coverage, levels_available, default_level}``.

        ``levels_available`` says which VIEWS this source's data
        supports — a level is available when its items produce at least
        one non-residual bucket there. Kept deliberately apart from
        ``available``, which says which SOURCES the fund has. The same
        word for both is exactly the collision that has bitten this
        project repeatedly.
    """
    levels = FACET_LEVELS.get(facet) or (facet,)
    per_level = {lv: items_at_level(facet, items, lv) for lv in levels}
    return {
        "levels":           list(levels),
        "items":            per_level,
        "coverage":         {lv: level_coverage(per_level[lv]) for lv in levels},
        "levels_available": {
            lv: any(it["key"] not in _RESIDUAL_KEYS for it in per_level[lv])
            for lv in levels
        },
        "default_level":    FACET_DEFAULT_LEVEL.get(facet, levels[-1]),
    }


def candidate_exposures(fund_breakdowns: dict,
                        targets: dict) -> tuple[dict, dict]:
    """One fund's look-through exposure, per ``(facet, level, key)``.

    **v0.66.3.** Lifted out of the optimise endpoint, where it was an
    inline block that could not be tested against a real fund block
    without a live Yahoo fetch — which is why a region-level failure
    could sit in it unseen while both the pieces either side of it
    tested clean.

    Only the levels the target set actually mentions are computed. A
    fund contributes to each independently: nothing is derived from
    another level's total.

    Args:
        fund_breakdowns: The fund's ``{facet: block}`` map.
        targets: ``{facet: {level: {key: fraction}}}``.

    Returns:
        ``(exposures, sources)`` where exposures is
        ``{facet: {level: {key: weight}}}`` and sources is
        ``{facet: source_name}`` (``"none"`` when the fund answers
        nothing for that facet).
    """
    exposures: dict[str, dict[str, dict[str, float]]] = {}
    sources:   dict[str, str] = {}

    for facet, per_level in (targets or {}).items():
        block = (fund_breakdowns or {}).get(facet) or {}
        per_level_out: dict[str, dict[str, float]] = {}
        answered = False

        for level in (per_level or {}):
            blk: dict[str, float] = {}
            for it in facet_items(block, level):
                if not isinstance(it, dict):
                    continue
                key = (it.get("key") or "").strip()
                if not key:
                    continue
                try:
                    w = float(it.get("weight") or 0.0)
                except (TypeError, ValueError):
                    continue
                blk[key] = blk.get(key, 0.0) + w
            per_level_out[level] = blk
            # "Answered" means a real bucket, not a residual. A block
            # that is entirely `unknown` carries weight but no
            # information, and counting it as an answer is what let a
            # fund with no country data look like a fund with country
            # data whose targets simply were not met.
            if any(k not in _RESIDUAL_KEYS for k in blk):
                answered = True

        exposures[facet] = per_level_out
        sources[facet] = (block.get("source") or "yahoo") if answered else "none"

    return exposures, sources
