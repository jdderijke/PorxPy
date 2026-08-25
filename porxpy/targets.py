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

from porxpy.breakdowns import facet_items
from porxpy.config import FACET_LEVELS, TARGET_FACETS


def _to_fraction(pct: float) -> float:
    """Convert a stored percent (0–100) to a fraction (0–1)."""
    try:
        return float(pct) / 100.0
    except (TypeError, ValueError):
        return 0.0


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
                    # What the targets commit, rolled up to the coarsest
                    # level, and the level that was — NOT the levels
                    # added together, which double-counts nested targets.
                    # See committed_pct().
                    "target_committed_pct": pct,
                    "committed_level":      str,
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
        # v0.65.0: grouped by LEVEL. Each target is compared against the
        # portfolio's distribution AT ITS OWN LEVEL — a sub-sector
        # target against the sub-sector distribution, a sector target
        # against the sector one. Nothing is re-expressed at a common
        # grain, because rolling a sector target down to sub-sectors
        # would invent detail the user never gave.
        #
        # Grouped rather than flat so a sub-sector 15% and a sector 20%
        # never sit adjacent in one list, where they read as competing
        # when they are in fact nested.
        block = (fundlevel_breakdowns or {}).get(facet) or {}
        target_block = (targets or {}).get(facet) or {}
        if not isinstance(target_block, dict):
            target_block = {}

        has_targets = any(bool(v) for v in target_block.values()
                          if isinstance(v, dict))
        if has_targets:
            any_targets = True

        levels_out: dict[str, dict] = {}

        for level, lvl_targets in target_block.items():
            if not isinstance(lvl_targets, dict) or not lvl_targets:
                continue
            raw_items = facet_items(block, level)
            items = [
                {"key":    (it.get("key") or "").strip(),
                 "weight": float(it.get("weight") or 0.0),
                 "value":  float(it.get("value")  or 0.0)}
                for it in raw_items
                if isinstance(it, dict) and (it.get("key") or "").strip()
            ]
            actual_by_key = {it["key"]: it["weight"] for it in items}

            # A target is UNMEASURABLE when nothing in the portfolio can
            # answer at that grain — every fund reports coarser, so the
            # level holds only residuals. Partial coverage stays
            # measurable: targeting semiconductors 15% when 60% of the
            # portfolio reports sub-sector is a real measurement, just
            # an uncertain one, and the coverage figure says so.
            available = bool((block.get("levels_available") or {}).get(level))
            coverage  = float((block.get("coverage") or {}).get(level) or 0.0)

            targeted_items = []
            for key, pct in sorted(lvl_targets.items()):
                tgt_frac = _to_fraction(pct)
                actual   = actual_by_key.get(key, 0.0)
                targeted_items.append({
                    "key":          key,
                    "actual":       round(actual, 6),
                    "target":       round(tgt_frac, 6),
                    "deviation":    round(actual - tgt_frac, 6),
                    "unmeasurable": not available,
                })

            untargeted_items, untargeted_total = [], 0.0
            target_keys = set(lvl_targets.keys())
            for it in items:
                if it["key"] in target_keys or it["weight"] <= 0:
                    continue
                untargeted_items.append({"key": it["key"],
                                         "actual": round(it["weight"], 6)})
                untargeted_total += it["weight"]
            untargeted_items.sort(key=lambda x: -x["actual"])

            levels_out[level] = {
                "items":            targeted_items,
                "untargeted_pct":   round(untargeted_total, 6),
                "untargeted_items": untargeted_items,
                "coverage":         round(coverage, 6),
                "measurable":       available,
            }

        out_facets[facet] = {
            "has_targets":    has_targets,
            "levels":         levels_out,
            # Renamed from target_sum_pct along with the arithmetic: the
            # old name described adding the levels together, which is
            # exactly what stopped being done. Leaving the name would
            # have let a reader keep assuming a sum.
            "target_committed_pct": committed_pct(facet, target_block),
            "committed_level":      (FACET_LEVELS.get(facet)
                                     or (facet,))[-1],
        }

    return {
        "facets":      out_facets,
        "any_targets": any_targets,
    }


# ---------------------------------------------------------------------------
# Parent/child consistency (v0.65.0)
# ---------------------------------------------------------------------------


def committed_pct(facet: str, per_level: dict) -> float:
    """How much of a facet a target set actually commits, in percent.

    Why this is not a sum
    ---------------------
    Targets nest. Semiconductors 15% and technology 35% are one
    commitment of 35%, not two commitments of 50% — the first is inside
    the second, which is the same rule
    :func:`validate_target_levels` enforces at save. Adding the levels
    together therefore produced totals over 100% for target sets that
    were perfectly coherent, and the number was read as an error when
    nothing was wrong.

    So the total is taken at the COARSEST level, where every target has
    been rolled up into the bucket that contains it. A total above 100%
    at that level is a real over-commitment — two super-sectors at 60%
    each genuinely cannot both happen — which is what makes the figure
    worth showing at all.

    Each bucket commits the LARGER of its own target and what its
    targeted children already commit. Larger, rather than its own
    target, so that an inconsistent intermediate state (children summing
    past their parent) still reports what is really committed instead of
    quietly under-reporting it. Save-time validation is what names that
    as a problem; this figure only has to stay honest.

    A bucket targeted only through its children still counts: sub-sector
    targets with no sector target commit their branch just as firmly.

    Args:
        facet: The facet these targets belong to.
        per_level: ``{level: {key: pct}}`` for that facet.

    Returns:
        Percent committed, 0.0 when there are no targets.
    """
    from porxpy.breakdowns import _key_at_level

    if not isinstance(per_level, dict) or not per_level:
        return 0.0
    levels = FACET_LEVELS.get(facet) or (facet,)

    # Finest first, carrying each level's commitments up into the next.
    carried: dict[str, float] = {}
    for level in levels:
        here: dict[str, float] = {}
        for child_key, val in carried.items():
            # A key that cannot be placed at this level stands alone
            # rather than vanishing — dropping it would under-report a
            # commitment the user really made.
            parent = _key_at_level(facet, child_key, level) or child_key
            here[parent] = here.get(parent, 0.0) + val
        for key, pct in (per_level.get(level) or {}).items():
            try:
                own = float(pct or 0.0)
            except (TypeError, ValueError):
                own = 0.0
            here[key] = max(here.get(key, 0.0), own)
        carried = here

    return round(sum(carried.values()), 4)


def validate_target_levels(targets: dict) -> list[str]:
    """Check every parent target against the sum of its children's.

    A parent bucket contains its children, so a target on it cannot be
    smaller than what its targeted children already commit. Targeting
    semiconductors 15% and software 10% commits 25% of technology; a
    technology target of 20% is then not merely unlikely, it is
    arithmetically impossible, and the optimiser would spend the run
    failing to satisfy it.

    Checked at SAVE, not per field. A target set is only coherent once
    it is complete — typing semiconductors 15% before technology 35%
    would fail a per-field check on a set that ends up perfectly valid,
    and an editor that rejects an intermediate state is an editor that
    fights the user.

    A parent target LARGER than the sum is fine and is the useful case:
    technology 35% against children summing to 25% asks the optimiser
    for those children plus 10% of any other technology.

    Args:
        targets: ``{facet: {level: {key: pct}}}``, already coerced.

    Returns:
        Human-readable problems, empty when consistent.
    """
    from porxpy.breakdowns import _key_at_level

    problems: list[str] = []
    for facet, per_level in (targets or {}).items():
        levels = FACET_LEVELS.get(facet) or ()
        if len(levels) < 2 or not isinstance(per_level, dict):
            continue

        # Finest first, so every level below a given one is a potential
        # child level.
        for depth, parent_level in enumerate(levels):
            parent_targets = per_level.get(parent_level) or {}
            if not parent_targets:
                continue
            for parent_key, parent_pct in parent_targets.items():
                committed = 0.0
                contributors: list[str] = []
                for child_level in levels[:depth]:
                    for child_key, child_pct in (per_level.get(child_level)
                                                 or {}).items():
                        if _key_at_level(facet, child_key,
                                         parent_level) == parent_key:
                            committed += float(child_pct)
                            contributors.append(f"{child_key} {child_pct:g}%")
                if contributors and committed > float(parent_pct) + 1e-9:
                    problems.append(
                        f"{facet}: {parent_key} is targeted at "
                        f"{float(parent_pct):g}%, but its targeted children "
                        f"already commit {committed:g}% "
                        f"({', '.join(sorted(contributors))}). A parent "
                        f"cannot be smaller than the sum of its children.")
    return problems
