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
from porxpy.config import (BASELINE_MIN_TARGET_PCT, BREAKDOWN_FACETS,
                           FACET_LEVELS, TARGET_FACETS)


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

        # Rolled up ONE LEVEL AT A TIME, finest first, exactly as
        # committed_pct does it — and for the same reason.
        #
        # This used to compare a parent against the sum of every finer
        # level at once, which counts a grandchild on top of the child
        # that already contains it. Sparse target sets almost never
        # tripped it; a set covering every level trips it everywhere, and
        # v0.119.0's baseline import produces exactly such a set. It
        # reported asset-class equity at 99.5% as over-committed by its
        # "children" regular stock 99.5% AND shares and options 99.5%,
        # when the first of those is inside the second.
        #
        # A bucket's effective commitment is the LARGER of its own target
        # and what its own children commit, so each branch counts once
        # however many levels of it are targeted.
        carried: dict[str, float] = {}
        for level in levels:
            here: dict[str, float] = {}
            here_from: dict[str, list[str]] = {}
            for child_key, val in carried.items():
                parent = _key_at_level(facet, child_key, level) or child_key
                here[parent] = here.get(parent, 0.0) + val
                here_from.setdefault(parent, []).append(
                    f"{child_key} {val:g}%")
            for key, pct in (per_level.get(level) or {}).items():
                try:
                    own = float(pct or 0.0)
                except (TypeError, ValueError):
                    own = 0.0
                child_sum = here.get(key, 0.0)
                if child_sum > own + 1e-9:
                    # Named with the IMMEDIATE children only. Listing every
                    # descendant made the message unreadable and implied
                    # they were all being added together, which is the very
                    # arithmetic this check no longer does.
                    problems.append(
                        f"{facet}: {key} is targeted at {own:g}%, but the "
                        f"buckets inside it already commit "
                        f"{child_sum:g}% ({', '.join(sorted(here_from.get(key, [])))}). "
                        f"A parent cannot be smaller than what it contains.")
                here[key] = max(child_sum, own)
            carried = here

    return problems


# ---------------------------------------------------------------------------
# Baseline target sets (v0.119.0)
# ---------------------------------------------------------------------------

# Buckets that exist in a breakdown but can never carry a target.
#
# `unknown` is a closable data gap and `n/a` says the question does not
# apply — the distinction this codebase keeps everywhere. A target on
# either is unsatisfiable by construction, so a baseline drops them
# rather than writing one and letting the optimiser fail it for ever.
UNTARGETABLE_BUCKETS: frozenset[str] = frozenset({"unknown", "n/a"})


def build_baseline_targets(fund_breakdowns: dict, *,
                          min_pct: float = BASELINE_MIN_TARGET_PCT,
                          facets: tuple[str, ...] = BREAKDOWN_FACETS
                          ) -> tuple[dict, list[str]]:
    """Turn one fund's breakdown cards into a whole target set.

    Why this exists
    ---------------
    A sparse target set cannot say "and the rest at market weight". Set
    technology to 30% and nothing else, and the optimiser is asked for
    "technology 30%, not-technology 70%" — it holds no opinion at all
    about how that 70% splits, so 30% technology and 70% financial
    services satisfies it exactly. That missing sentence is the problem,
    and the only way to say it is to give every bucket a number.

    A fund already IS such a set of numbers. Reading a broad index fund's
    breakdown into the targets makes market weight the starting point,
    after which a tilt is one slider: raise technology and the others
    give ground proportionally, instead of the remainder being a blank
    cheque.

    What it deliberately does not do
    --------------------------------
    * **The metadata facets are excluded.** ``market_cap``, ``style_box``
      and ``focus_theme`` are one-hot per fund, so a baseline would write
      "large 100%" — a constraint nobody asked for, and one that cannot
      be tilted because there is nothing to tilt it against. They stay
      hand-set. Stated here because it is exactly the kind of asymmetry
      that otherwise reads as a facet somebody forgot.
    * **``unknown`` and ``n/a`` are dropped, not renormalised away.**
      Dropping leaves the facet committing less than 100%, and that
      unclaimed slice is precisely what the optimiser's OTHER bucket is
      free to fill. Renormalising would dress a half-classified fund up
      as a fully classified one.
    * **Buckets under ``min_pct`` are dropped.** See
      :data:`~porxpy.config.BASELINE_MIN_TARGET_PCT`.
    * **A geared fund is expressed as shares of its gross exposure.** A
      fund that has borrowed against its holdings reports more than 100%
      of its net assets, which is a fact about the fund and not a broken
      card. It is divided down here only because
      :func:`porxpy.breakdowns.rollup_portfolio_fundlevel` already
      normalises every card to a 100% distribution, so a target above
      100% could never be met by any portfolio. The note names the
      gearing so the number is not silently lost.

    Dropping a child never breaks the tree. A parent keeps its own
    figure, so whatever its children no longer account for simply stays
    inside the parent as unclaimed room — the same reading
    :func:`validate_target_levels` already gives a parent whose targeted
    children sum to less than it does.

    Args:
        fund_breakdowns: The fund's resolved breakdown cards, i.e.
            ``load_fund_data(...)["fund_breakdowns"]`` — the output of
            :func:`porxpy.breakdowns.build_fund_breakdowns`. Read through
            :func:`porxpy.breakdowns.facet_items` at every level the
            facet has, so each card's own configured source is honoured
            and the numbers are the ones that fund's X-ray shows.
        min_pct: Smallest bucket to write, in percent.
        facets: Which facets to read. Defaults to the four distribution
            facets; the argument exists so a caller can narrow the set,
            not so a metadata facet can be smuggled into it.

    Returns:
        ``(targets, notes)`` — targets in the stored shape
        ``{facet: {level: {key: percent}}}``, and human-readable notes
        naming what was skipped and why. The notes are why this returns
        a pair: a fund with no currency breakdown has to SAY so, because
        a silently absent facet looks identical to one the user chose not
        to target.
    """
    out: dict[str, dict[str, dict[str, float]]] = {}
    notes: list[str] = []

    for facet in facets:
        block = (fund_breakdowns or {}).get(facet) or {}
        per_level: dict[str, dict[str, float]] = {}
        tiny = 0
        rescaled: list[str] = []
        geared: dict[str, float] = {}
        for level in (FACET_LEVELS.get(facet) or (facet,)):
            items = [it for it in facet_items(block, level)
                     if isinstance(it, dict)]

            # A level summing past 1 is GEARING, not a broken card. A fund
            # that has borrowed against its holdings genuinely has gross
            # exposure above its net assets, and the 2.07 that found this
            # is a real fund in this cache rather than a unit bug. The
            # number is data and must never be read as an error.
            #
            # It is still divided out HERE, for one narrow reason: the
            # portfolio side has already normalised it away.
            # rollup_portfolio_fundlevel computes each item's weight as
            # `val / bucket_total` on purpose — "the card should still
            # read as a 100% distribution" — so the ACTUAL that a target
            # is measured against is always a 100% distribution. A 207%
            # target could therefore never be met by anything, and the
            # optimiser would spend every run failing it. A target and its
            # actual have to live in the same space, and this is what puts
            # them there. The note says so in the fund's own terms, naming
            # the gearing, rather than calling the card wrong.
            #
            # One-directional, and the two directions are different
            # phenomena rather than mirror images. A level summing to LESS
            # than 1 is the ordinary case — `unknown` was dropped and the
            # shortfall is real unclaimed room — so scaling it up would
            # dress a half-classified fund as a fully classified one.
            #
            # Threshold at 1% over rather than at any overshoot: a card
            # summing to 1.003 is rounding, the inflation it causes is
            # under the optimiser's own tolerance floor, and saying
            # "geared" about a fund that is not would be worse than the
            # 0.3pp it corrected.
            total = 0.0
            for it in items:
                try:
                    total += float(it.get("weight") or 0.0)
                except (TypeError, ValueError):
                    pass
            scale = (1.0 / total) if total > 1.01 else 1.0
            if scale != 1.0:
                rescaled.append(level)
                geared[level] = total

            lvl: dict[str, float] = {}
            for it in items:
                key = (it.get("key") or "").strip()
                if not key or key in UNTARGETABLE_BUCKETS:
                    continue
                try:
                    pct = float(it.get("weight") or 0.0) * scale * 100.0
                except (TypeError, ValueError):
                    continue
                if pct <= 0:
                    continue
                if pct < min_pct:
                    tiny += 1
                    continue
                # 4dp: the editor stores what the user drags to, and a
                # baseline arriving at full float precision shows as
                # 14.000000000000002 the first time anything sums it.
                lvl[key] = round(pct, 4)
            if lvl:
                per_level[level] = lvl
        if rescaled:
            gross = ", ".join(f"{lv} {geared[lv] * 100:.0f}%" for lv in rescaled)
            notes.append(
                f"{facet}: this fund's exposure adds up to more than its net "
                f"assets ({gross}) — it is geared. Targets here are written "
                f"as shares of that exposure, because the portfolio X-ray "
                f"normalises every fund's card to 100% too, so a target "
                f"above 100% could never be met.")
        if per_level:
            out[facet] = per_level
            if tiny:
                notes.append(
                    f"{facet}: {tiny} bucket{'' if tiny == 1 else 's'} under "
                    f"{min_pct:g}% left untargeted — below that the "
                    f"optimiser's own tolerance already covers them.")
        else:
            notes.append(
                f"{facet}: this fund has no usable breakdown, so the facet "
                f"was left unset rather than targeted at zero.")

    return out, notes


def prune_pins(targets: dict, pins: dict) -> dict:
    """Drop pins that no longer name a target.

    A pin is a constraint ON a target — "this number does not move when a
    sibling does". Once the target is gone the pin has nothing to hold,
    and a stored pin with no target would silently re-pin a bucket the
    user added back later, which reads as the editor refusing to move a
    slider for no visible reason.

    Pure function, called from the storage coercion and from the CSV
    reader, so both writers agree without either knowing about the other.

    Args:
        targets: ``{facet: {level: {key: percent}}}``.
        pins: ``{facet: {level: {key: True}}}``, possibly stale.

    Returns:
        A new pins dict holding only pins whose target exists. Empty
        levels and facets are dropped, so an all-stale map comes back as
        ``{}`` rather than a shell of empty dicts.
    """
    out: dict[str, dict[str, dict[str, bool]]] = {}
    for facet, per_level in (pins or {}).items():
        if not isinstance(per_level, dict):
            continue
        t_facet = (targets or {}).get(facet) or {}
        for level, block in per_level.items():
            if not isinstance(block, dict):
                continue
            t_level = t_facet.get(level) or {}
            kept = {k: True for k in block if block[k] and k in t_level}
            if kept:
                out.setdefault(facet, {})[level] = kept
    return out


# ---------------------------------------------------------------------------
# CSV interchange for a target set (v0.118.0)
# ---------------------------------------------------------------------------
# Designing a coherent target set is slow, careful work, and until now it
# lived in exactly one place: the portfolio it was typed into. There was
# no way to keep two of them, diff them, or try a variant without
# destroying the original. A file fixes all three, and a CSV in
# particular is the format that can be opened in a spreadsheet and edited
# by hand — which is the actual request, not merely a transport.
#
# The tolerances travel with the targets deliberately. A tolerance is
# meaningless without the target it is a share of ("within 10% of it"),
# so a file carrying one without the other would describe half a design.
#
# The cash reserve deliberately does NOT travel. It is an amount in base
# currency saying how much of THIS portfolio stays liquid — the one
# number on the Targets tab that is per-portfolio rather than
# per-design, and importing someone else's would be importing their bank
# balance.
#
# One file, one row per entry, discriminated by `kind`. The wide
# alternative — a tolerance column filled only on a facet's first row —
# was rejected because the blanks are easy to get wrong by hand and
# re-sorting the sheet, the first thing anyone does in a spreadsheet,
# silently moves which row carries the tolerance.

TARGETS_CSV_FIELDS: tuple[str, ...] = (
    "kind", "facet", "level", "key", "label", "value")

# The scalar Optimizer settings the file carries, and the unit each is
# written in. They are not per facet, so they ride as `kind=setting` rows
# with the setting's name in `key` and `facet` left blank.
#
# `score_preset` is deliberately NOT here. It names a scoring model that
# exists in the install that exported the file and may not exist in the
# one importing it, and a target set is meant to be portable between
# portfolios and between installs. The other three are plain numbers that
# mean the same thing everywhere.
#
# This is why the value column is `value` and not `value_pct`: it now
# carries three units — a percentage, a count, and an amount of base
# currency — and a column named for one of them would be lying about the
# other two. The trailing comment block in the file says which is which.
TARGETS_CSV_SETTINGS: dict[str, str] = {
    "max_funds":  "count",
    "min_weight": "percent",      # 1 = 1% of the fund side
    "min_trade":  "base currency",
}

# Rows beginning with this are written as guidance and skipped on read.
TARGETS_CSV_COMMENT = "#"


def _pretty_key(key: str) -> str:
    """A human label for a bucket key. Decorative only.

    Written into the ``label`` column on export and IGNORED on import,
    so a row can be retitled, translated, or the column deleted
    entirely without changing what the file means. ``key`` is always the
    authority.

    Deliberately a small local prettifier rather than a second copy of
    the frontend's ``tgKeyLabel``: this text never drives behaviour, and
    sharing a labeller would couple a pure-compute module to the display
    layer for a column nothing reads back. Most keys are already
    canonical words ("corporate bond"); the camelCase ones are the
    Morningstar region codes ("northAmerica").
    """
    if not key:
        return ""
    if key.isupper():                     # currency codes: EUR, USD
        return key
    out, prev_lower = [], False
    for ch in key:
        if ch.isupper() and prev_lower:
            out.append(" ")
        out.append(ch)
        prev_lower = ch.islower()
    return "".join(out).replace("_", " ").strip().title()


def targets_to_csv(targets: dict, tolerances: dict | None = None,
                   settings: dict | None = None,
                   pins: dict | None = None) -> str:
    """Render a target set, its tolerances and the Optimizer scalars.

    Args:
        targets: ``{facet: {level: {key: percent}}}`` as stored.
        pins: ``{facet: {level: {key: True}}}`` — which targets are
            pinned. Written as ``kind=pin`` rows with a value of 1.
            Pins travel with the targets for the same reason tolerances
            do: a pin is a statement about a specific target, so a file
            carrying one without the other would describe half a design.
        tolerances: ``{facet: fraction}`` — the optimiser's per-facet
            relative tolerance. Written as a PERCENT, matching what the
            Optimizer panel shows, so the file and the screen agree.
        settings: The Optimizer panel's scalars. Only the keys in
            :data:`TARGETS_CSV_SETTINGS` are written, and ``min_weight``
            is converted to a percent for the same reason.

    Returns:
        CSV text: a header row, target rows sorted by facet then level
        then key for a stable diff, then tolerance rows, then setting
        rows, then comment lines giving the unit of ``value`` for each
        kind — the one thing about this file a reader cannot infer.
    """
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TARGETS_CSV_FIELDS)

    for facet in sorted(targets or {}):
        per_level = (targets or {}).get(facet) or {}
        if not isinstance(per_level, dict):
            continue
        # Levels in the facet's own finest-first order rather than
        # alphabetically, so the file reads the way FACET_LEVELS does.
        order = list(FACET_LEVELS.get(facet) or (facet,))
        for level in sorted(per_level,
                            key=lambda l: (order.index(l) if l in order
                                           else 99, l)):
            block = per_level.get(level) or {}
            for key in sorted(block):
                try:
                    pct = float(block[key])
                except (TypeError, ValueError):
                    continue
                # A stored 0 IS a target — "hold none of this" — and the
                # optimiser enforces it to within TOL_FLOOR. Only the
                # ABSENCE of a key means untargeted. This used to drop
                # zeros, which silently turned "none of this" into "no
                # opinion" on every export/import round trip (v0.119.0).
                if pct < 0:
                    continue
                w.writerow(["target", facet, level, key,
                            _pretty_key(key), f"{pct:g}"])

    # Pins after all the targets, so a hand-edited file reads as "here is
    # the design, and here is what I have nailed down in it".
    for facet in sorted(pins or {}):
        per_level = (pins or {}).get(facet) or {}
        if not isinstance(per_level, dict):
            continue
        order = list(FACET_LEVELS.get(facet) or (facet,))
        for level in sorted(per_level,
                            key=lambda l: (order.index(l) if l in order
                                           else 99, l)):
            block = per_level.get(level) or {}
            for key in sorted(block):
                if not block[key]:
                    continue
                w.writerow(["pin", facet, level, key, _pretty_key(key), "1"])

    for facet in sorted(tolerances or {}):
        try:
            frac = float((tolerances or {})[facet])
        except (TypeError, ValueError):
            continue
        if frac <= 0:
            continue
        w.writerow(["tolerance", facet, "", "", "", f"{frac * 100:g}"])

    for name in TARGETS_CSV_SETTINGS:
        if name not in (settings or {}):
            continue
        try:
            v = float((settings or {})[name])
        except (TypeError, ValueError):
            continue
        # min_weight is stored as a fraction and shown as a percent. The
        # file follows the SCREEN, so what a user reads here is what they
        # would type into the panel.
        if name == "min_weight":
            v *= 100.0
        w.writerow(["setting", "", "", name, _pretty_key(name), f"{v:g}"])

    c = TARGETS_CSV_COMMENT
    buf.write(f"{c} value: target    = % of the fund side (0 = hold none;\n")
    buf.write(f"{c}                    no row at all = no target)\n")
    buf.write(f"{c}        pin       = 1 (this target does not move when a\n")
    buf.write(f"{c}                    sibling or its parent is changed)\n")
    buf.write(f"{c}        tolerance = % of each target\n")
    buf.write(f"{c}        setting   = max_funds a count, min_weight a %,\n")
    buf.write(f"{c}                    min_trade an amount in base currency\n")
    buf.write(f"{c} label is decorative and ignored on import.\n")
    return buf.getvalue()


def targets_from_csv(text: str) -> tuple[dict, dict, dict, dict, list[str]]:
    """Parse CSV text back into targets, tolerances and Optimizer scalars.

    Every problem is collected rather than raised, so the caller can
    reject the file as a whole and show everything wrong with it at
    once. That matches how a target set is validated on save: a set is
    coherent or it is not, and correcting one error at a time through
    five round-trips is the editor fighting the user.

    Args:
        text: The file's contents.

    Returns:
        ``(targets, pins, tolerances, settings, problems)`` — targets as
        ``{facet: {level: {key: percent}}}``, tolerances as
        ``{facet: fraction}``, settings as ``{name: number}`` in the
        units the optimiser stores (``min_weight`` back to a fraction),
        and human-readable problems. When ``problems`` is non-empty the
        first three must not be applied.
    """
    import csv
    import io

    from porxpy.config import META_FACETS, meta_target_allowed

    targets: dict[str, dict[str, dict[str, float]]] = {}
    tolerances: dict[str, float] = {}
    settings: dict[str, float] = {}
    pins: dict[str, dict[str, dict[str, bool]]] = {}
    problems: list[str] = []
    # Keyed by KIND as well, so a bucket may carry both a target row
    # and a pin row without the second reading as a duplicate.
    seen: set[tuple[str, str, str, str]] = set()

    lines = [ln for ln in (text or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith(TARGETS_CSV_COMMENT)]
    if not lines:
        return {}, {}, {}, {}, ["the file has no rows"]

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    # `value_pct` is accepted as an alias for `value`: files exported by
    # the first build of this feature carried the older name, and
    # refusing them would make a target set someone had already saved
    # unreadable by the tool that wrote it.
    names = reader.fieldnames or []
    value_col = "value" if "value" in names else (
        "value_pct" if "value_pct" in names else "")
    missing = [c for c in ("kind", "facet") if c not in names]
    if not value_col:
        missing.append("value")
    if missing:
        return {}, {}, {}, {}, [
            f"missing column(s): {', '.join(missing)}. Expected a header row "
            f"of: {', '.join(TARGETS_CSV_FIELDS)}"]

    for n, row in enumerate(reader, start=2):          # row 1 is the header
        kind  = (row.get("kind")  or "").strip().lower()
        facet = (row.get("facet") or "").strip()
        level = (row.get("level") or "").strip()
        key   = (row.get("key")   or "").strip()
        rawv  = (row.get(value_col) or "").strip()

        if kind not in ("target", "pin", "tolerance", "setting"):
            shown = kind or "(blank)"
            problems.append(f"row {n}: kind must be 'target', 'pin', "
                            f"'tolerance' or 'setting', not {shown}")
            continue
        # A setting is not per facet, so it is checked against its own
        # vocabulary and skips the facet gate entirely.
        if kind != "setting" and facet not in TARGET_FACETS:
            problems.append(f"row {n}: unknown facet {facet!r}. Known: "
                            f"{', '.join(TARGET_FACETS)}")
            continue
        try:
            val = float(rawv)
        except (TypeError, ValueError):
            problems.append(f"row {n}: value {rawv!r} is not a number")
            continue
        if val < 0:
            problems.append(f"row {n}: value cannot be negative")
            continue

        if kind == "setting":
            if key not in TARGETS_CSV_SETTINGS:
                problems.append(
                    f"row {n}: unknown setting {key!r}. Known: "
                    f"{', '.join(TARGETS_CSV_SETTINGS)}")
                continue
            if key in settings:
                problems.append(f"row {n}: {key} is set twice")
                continue
            # Back into the units the optimiser stores. The file speaks
            # the panel's language; `optimizer_settings_set` clamps to
            # the real bounds, so nothing here has to know them.
            settings[key] = val / 100.0 if key == "min_weight" else val
            continue

        if kind == "tolerance":
            if not 0 < val <= 100:
                problems.append(
                    f"row {n}: a tolerance is a percentage OF THE TARGET, so "
                    f"it must be above 0 and at most 100")
                continue
            if facet in tolerances:
                problems.append(f"row {n}: {facet} already has a tolerance")
                continue
            tolerances[facet] = val / 100.0
            continue

        # kind == "target" or "pin". Both name one bucket, so they share
        # every check about whether that bucket can exist at all.
        levels = FACET_LEVELS.get(facet) or (facet,)
        if not level:
            # A flat facet's only level is its own name, so an omitted
            # level is unambiguous there and an error anywhere else —
            # guessing one on a tree would pick the grain for the user,
            # which is the decision FACET_DEFAULT_LEVEL exists to keep
            # out of the data.
            if len(levels) == 1:
                level = levels[0]
            else:
                problems.append(f"row {n}: {facet} needs a level (one of "
                                f"{', '.join(levels)})")
                continue
        if level not in levels:
            problems.append(f"row {n}: {level!r} is not a level of {facet}. "
                            f"Levels: {', '.join(levels)}")
            continue
        if not key:
            problems.append(f"row {n}: a target needs a key")
            continue
        if facet in META_FACETS and not meta_target_allowed(facet, key):
            problems.append(
                f"row {n}: {key!r} cannot carry a target on {facet}")
            continue
        if (kind, facet, level, key) in seen:
            problems.append(
                f"row {n}: duplicate {kind} for {facet}/{level}/{key}")
            continue
        seen.add((kind, facet, level, key))

        if kind == "pin":
            # Any truthy value pins. 0 is accepted and means "not pinned",
            # so a spreadsheet user can toggle a column rather than delete
            # rows — and an unpinned row saying so is more legible in a
            # diff than a row that vanished.
            if val:
                pins.setdefault(facet, {}).setdefault(level, {})[key] = True
            continue

        # A 0 here is a real target meaning "hold none of this", so it is
        # written through. Only an absent ROW means untargeted. This used
        # to be `if val > 0`, which discarded every deliberate zero on
        # import (v0.119.0).
        targets.setdefault(facet, {}).setdefault(level, {})[key] = val

    # A pin naming a bucket with no target row holds nothing. Dropped
    # rather than reported: a hand-edited file that deletes a target and
    # forgets its pin obviously means to drop both.
    return targets, prune_pins(targets, pins), tolerances, settings, problems
