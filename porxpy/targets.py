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
                   settings: dict | None = None) -> str:
    """Render a target set, its tolerances and the Optimizer scalars.

    Args:
        targets: ``{facet: {level: {key: percent}}}`` as stored.
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
                if pct <= 0:
                    continue      # sparse: an absent target is not a zero one
                w.writerow(["target", facet, level, key,
                            _pretty_key(key), f"{pct:g}"])

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
    buf.write(f"{c} value: target    = % of the fund side\n")
    buf.write(f"{c}        tolerance = % of each target\n")
    buf.write(f"{c}        setting   = max_funds a count, min_weight a %,\n")
    buf.write(f"{c}                    min_trade an amount in base currency\n")
    buf.write(f"{c} label is decorative and ignored on import.\n")
    return buf.getvalue()


def targets_from_csv(text: str) -> tuple[dict, dict, dict, list[str]]:
    """Parse CSV text back into targets, tolerances and Optimizer scalars.

    Every problem is collected rather than raised, so the caller can
    reject the file as a whole and show everything wrong with it at
    once. That matches how a target set is validated on save: a set is
    coherent or it is not, and correcting one error at a time through
    five round-trips is the editor fighting the user.

    Args:
        text: The file's contents.

    Returns:
        ``(targets, tolerances, settings, problems)`` — targets as
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
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    lines = [ln for ln in (text or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith(TARGETS_CSV_COMMENT)]
    if not lines:
        return {}, {}, {}, ["the file has no rows"]

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
        return {}, {}, {}, [
            f"missing column(s): {', '.join(missing)}. Expected a header row "
            f"of: {', '.join(TARGETS_CSV_FIELDS)}"]

    for n, row in enumerate(reader, start=2):          # row 1 is the header
        kind  = (row.get("kind")  or "").strip().lower()
        facet = (row.get("facet") or "").strip()
        level = (row.get("level") or "").strip()
        key   = (row.get("key")   or "").strip()
        rawv  = (row.get(value_col) or "").strip()

        if kind not in ("target", "tolerance", "setting"):
            shown = kind or "(blank)"
            problems.append(f"row {n}: kind must be 'target', 'tolerance' or "
                            f"'setting', not {shown}")
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

        # kind == "target"
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
        if (facet, level, key) in seen:
            problems.append(
                f"row {n}: duplicate target for {facet}/{level}/{key}")
            continue
        seen.add((facet, level, key))
        if val > 0:
            targets.setdefault(facet, {}).setdefault(level, {})[key] = val

    return targets, tolerances, settings, problems
