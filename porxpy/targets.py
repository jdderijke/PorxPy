"""
Portfolio target deviations.

Pure-compute module. Reads a portfolio's per-facet rollup output (from
:mod:`porxpy.breakdowns`) and the user's per-facet targets (stored in
``portfolios.json`` — see :func:`porxpy.utils.portfolio_targets_get`)
and produces the per-facet deviation report shown on the Targets tab.

Design summary (per the design discussion captured in
``Designed_and_matching_portfolios_to_exposure_targets.odt``):

* Targets are sparse. A facet with no targets shows actuals only; a
  facet with some targets shows deviation bars for the targeted
  buckets and a separate "untargeted" summary listing the
  bucket-by-bucket actuals that fall outside the user's set targets.
* The country facet is region-keyed at the target level. The portfolio
  rollup is at the mstar_country level, so this module aggregates the
  rollup up to mstar_region before comparing.
* v0.28.0 adds the two metadata facets (``market_cap``, ``style_box``)
  to the same machinery. They arrive from the rollup already reshaped
  into one-hot distributions, so nothing here special-cases them. Two
  of their values — ``unknown`` and ``n/a`` — cannot carry a target
  (:data:`porxpy.config.META_FACET_TARGETABLE`), so they always land in
  the untargeted summary. That is the intended reading: an unclassified
  slice of the portfolio is a fact worth showing, but it is not a miss
  against a target the user never set, and renormalising it away would
  make a half-classified portfolio look fully classified.
* Cash positions (``portfolio.cash_positions``) are already folded
  into the Fund/ETF-level rollup as synthetic enriched entries by the
  caller (``api_portfolio_view``); they show up as ``asset_class:cash``
  items in the rollup. This module does NOT add them again — it
  consumes whatever the rollup provides.
* All math runs in fraction space (0–1). Targets are stored as
  percents (0–100, the numbers the user typed); the conversion is
  done locally so the storage and the rollup speak their natural
  units.
"""

from __future__ import annotations

from porxpy.config import TARGET_FACETS


def _to_fraction(pct: float) -> float:
    """Convert a stored percent (0–100) to a fraction (0–1)."""
    try:
        return float(pct) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _aggregate_country_items_to_region(items: list[dict]) -> list[dict]:
    """Roll an mstar_country item list up to mstar_region buckets.

    The portfolio's country rollup contains items keyed by
    ``mstar_country`` (e.g. ``"unitedstates"``); the Targets tab
    compares against region-level keys (e.g. ``"northAmerica"``).
    This helper consolidates by region using the in-process
    ``MSTAR_TO_REGION`` map.

    Args:
        items: ``[{"key": mstar_country, "weight": fraction,
                   "value": float}, ...]``.

    Returns:
        ``[{"key": mstar_region, "weight": fraction, "value": float}, ...]``.
        Countries with no region mapping (typically already
        region-shaped keys, or unknowns) are passed through with
        their key as-is — this is rare in practice but means the
        deviation report won't drop data silently.
    """
    # Local import — keeps targets.py importable without the resources
    # CSVs in some unit tests.
    from porxpy.resources import MSTAR_TO_REGION

    bucket_w: dict[str, float] = {}
    bucket_v: dict[str, float] = {}
    for it in items or []:
        key = (it.get("key") or "").strip()
        if not key:
            continue
        try:
            w = float(it.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        try:
            v = float(it.get("value") or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        region = MSTAR_TO_REGION.get(key, key)
        bucket_w[region] = bucket_w.get(region, 0.0) + w
        bucket_v[region] = bucket_v.get(region, 0.0) + v

    out = [
        {"key": k, "weight": w, "value": bucket_v.get(k, 0.0)}
        for k, w in bucket_w.items()
    ]
    out.sort(key=lambda it: -it["weight"])
    return out


def compute_target_deviations(fundlevel_breakdowns: dict,
                              targets: dict) -> dict:
    """Compute the per-facet target-vs-actual deviation block.

    Pure function. The caller is responsible for assembling
    ``fundlevel_breakdowns`` (typically the output of
    :func:`porxpy.breakdowns.rollup_portfolio_fundlevel`) and
    ``targets`` (typically :func:`porxpy.utils.portfolio_targets_get`).

    Args:
        fundlevel_breakdowns: Per-facet item lists as produced by the
            portfolio rollup — ``{facet: [{"key","weight","value"}, ...]}``.
            ``weight`` is a fraction of the portfolio (already normalised).
            Country items are at the mstar_country level; this function
            aggregates them to mstar_region internally for comparison.
        targets: Per-facet ``{key: percent}`` dicts. Country/region
            targets use mstar_region keys (e.g. ``"northAmerica"``).
            Sparse — keys absent mean "no target".

    Returns:
        ::

            {
              "facets": {
                "asset_class": {
                    "has_targets":   bool,
                    "items": [
                        {"key": str, "actual": fraction, "target": fraction,
                         "deviation": fraction},  # actual - target
                        ...
                    ],
                    "untargeted_pct":    fraction,
                    "untargeted_items":  [{"key": str, "actual": fraction}, ...],
                    "target_sum_pct":    fraction,  # sum of targets for this facet
                },
                "sector":     {...},
                "country":    {...},   # items keyed by mstar_region
                "currency":   {...},
                "market_cap": {...},
                "style_box":  {...},
              },
              "any_targets": bool,
            }

        ``deviation`` is signed: ``actual - target``. So a portfolio
        sitting at 70% equity vs a 60% target shows ``deviation =
        +0.10`` (overweight); a 25% bond holding vs a 30% target
        shows ``deviation = -0.05`` (underweight). The untargeted
        summary uses interpretation A from the design discussion —
        it lists the actual exposure of buckets the user did NOT
        target, regardless of how the targeted buckets add up.

        When a facet has no targets, ``has_targets`` is False and
        ``items`` is empty (the frontend hides that facet's chart
        entirely). ``any_targets`` is the OR across all four facets
        — the frontend uses it to decide whether to show the empty-
        state placeholder ("no targets set yet").
    """
    out_facets: dict[str, dict] = {}
    any_targets = False

    for facet in TARGET_FACETS:
        # Pull the relevant item list, aggregating country → region.
        raw_items = (fundlevel_breakdowns or {}).get(facet) or []
        if facet == "country":
            items = _aggregate_country_items_to_region(raw_items)
        else:
            items = [
                {"key":    (it.get("key") or "").strip(),
                 "weight": float(it.get("weight") or 0.0),
                 "value":  float(it.get("value")  or 0.0)}
                for it in raw_items
                if isinstance(it, dict) and (it.get("key") or "").strip()
            ]

        # Build a key → actual-fraction lookup for fast comparison.
        actual_by_key: dict[str, float] = {
            it["key"]: it["weight"] for it in items
        }

        target_block = (targets or {}).get(facet) or {}
        if not isinstance(target_block, dict):
            target_block = {}

        has_targets = bool(target_block)
        if has_targets:
            any_targets = True

        # Targeted buckets: one row per target key. The actual may be
        # zero (the portfolio doesn't hold any of that bucket — that
        # is a meaningful deviation, not a missing data point).
        targeted_items: list[dict] = []
        target_sum_pct = 0.0
        for key, pct in sorted(target_block.items()):
            tgt_frac = _to_fraction(pct)
            actual   = actual_by_key.get(key, 0.0)
            targeted_items.append({
                "key":       key,
                "actual":    round(actual, 6),
                "target":    round(tgt_frac, 6),
                "deviation": round(actual - tgt_frac, 6),
            })
            target_sum_pct += float(pct or 0.0)

        # Untargeted summary: every actual bucket whose key is NOT in
        # the target dict. We list each bucket separately so the
        # tooltip can show the breakdown — but only the total is
        # the "headline" number.
        untargeted_items: list[dict] = []
        untargeted_total = 0.0
        if has_targets:
            target_keys = set(target_block.keys())
            for it in items:
                if it["key"] in target_keys:
                    continue
                if it["weight"] <= 0:
                    continue
                untargeted_items.append({
                    "key":    it["key"],
                    "actual": round(it["weight"], 6),
                })
                untargeted_total += it["weight"]
            untargeted_items.sort(key=lambda x: -x["actual"])

        out_facets[facet] = {
            "has_targets":      has_targets,
            "items":            targeted_items,
            "untargeted_pct":   round(untargeted_total, 6),
            "untargeted_items": untargeted_items,
            "target_sum_pct":   round(target_sum_pct, 4),
        }

    return {
        "facets":      out_facets,
        "any_targets": any_targets,
    }
