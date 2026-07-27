#!/usr/bin/env python3
"""Report which profile fields are missing across the pre-loaded funds.

Run from the project root::

    python tools/data_coverage.py

Reads the listing cache directly — no network, no Yahoo calls — and
counts how many saved funds have each profile field populated.

Why this exists: before adding a second data source (justETF, an issuer
feed, a paid API), it is worth knowing how big the hole actually is. The
answer decides whether the work is worth doing, and for which fields.

Note the cache is the *previous* fetch. Fields added or improved by a new
extractor version only appear after a forced reload of each fund, so a
field reported missing here may already be fixed for funds you reload.
The "stale" count at the bottom is how many entries predate the current
extractor and would need a reload to find out.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porxpy.config import LISTINGS_DIR                      # noqa: E402
from porxpy.utils import overrides_for                      # noqa: E402


# Grouped the way the fund page ought to be grouped, so the output doubles
# as a check on whether that grouping holds up against real data.
# Fields where a reported 0 almost certainly means "no data" rather than
# a measurement. A 0.00% TER or 0.0% turnover is not a real answer for
# any fund you are likely to hold.
ZERO_IS_SUSPECT: frozenset = frozenset({
    "expenseRatioPct", "totalNetAssets", "turnoverPct",
    "trailingYieldPct", "forwardYieldPct",
})

GROUPS: dict[str, tuple[str, ...]] = {
    # isin / exchange / currency come from the file's identity block,
    # merged over the profile above. They are listing-level facts, which
    # is why they live apart from the Yahoo-fetched profile.
    "Identity":     ("symbol", "isin", "exchange", "currency", "quoteType"),
    "Descriptive":  ("longName", "fundFamily", "category", "legalType",
                     "fundInceptionDate"),
    "Metadata":     ("distribution", "market_cap", "style_box"),
    "Costs & size": ("expenseRatioPct", "totalNetAssets", "turnoverPct"),
    "Income":       ("trailingYieldPct", "forwardYieldPct"),
    "Pricing":      ("previousClose", "navPrice", "fiftyTwoWeekHigh",
                     "fiftyTwoWeekLow", "averageVolume"),
}


def main() -> int:
    if not LISTINGS_DIR.exists():
        print(f"No listings cache at {LISTINGS_DIR} — nothing to report.")
        return 1

    files = sorted(LISTINGS_DIR.glob("*.json"))
    if not files:
        print("Listings cache is empty — save a fund first.")
        return 1

    present: Counter = Counter()
    overridden: Counter = Counter()
    # Zeros are counted apart from real values. Yahoo reports 0.0 for
    # "no data" on fees and turnover, so a naive present/absent count
    # reads those funds as fully populated and hides the actual gap.
    suspect_zero: Counter = Counter()
    total = 0
    missing_examples: dict[str, list[str]] = {}

    for fp in files:
        try:
            blob = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        prof = ((blob.get("profile") or {}).get("value")) or {}
        if not prof:
            continue
        total += 1

        # Identity is NOT part of the profile blob. It is a top-level key
        # in the same listing file, written by listing_identity_put when
        # the ticker was resolved — profile["isin"] only appears on the
        # subset of fetches where Yahoo happened to expose it inline.
        # Reading identity is the authoritative path; reading the profile
        # made every fund look like it was missing an ISIN, and silently
        # zeroed the override counts too, since overrides_for("") is {}.
        ident = blob.get("identity") if isinstance(blob.get("identity"), dict) else {}
        prof = {**prof, **{k: v for k, v in ident.items()
                           if k in ("isin", "ticker", "exchange", "currency") and v}}
        ticker = prof.get("symbol") or ident.get("ticker") or fp.stem
        ovr = overrides_for(prof.get("isin") or "")

        for fields in GROUPS.values():
            for f in fields:
                v = prof.get(f)
                if isinstance(v, (int, float)) and not isinstance(v, bool) \
                        and v == 0 and f in ZERO_IS_SUSPECT:
                    suspect_zero[f] += 1
                    missing_examples.setdefault(f, [])
                    if len(missing_examples[f]) < 5:
                        missing_examples[f].append(f"{ticker}(0)")
                elif v not in (None, ""):
                    present[f] += 1
                else:
                    missing_examples.setdefault(f, [])
                    if len(missing_examples[f]) < 5:
                        missing_examples[f].append(ticker)
                if f in ovr:
                    overridden[f] += 1

    if not total:
        print("No cached profiles found.")
        return 1

    print(f"\n{total} saved fund(s) in {LISTINGS_DIR}\n")
    for group, fields in GROUPS.items():
        print(f"  {group}")
        for f in fields:
            have = present[f]
            pct = 100.0 * have / total
            bar = "█" * round(pct / 5) + "·" * (20 - round(pct / 5))
            bits = []
            if suspect_zero[f]:
                bits.append(f"{suspect_zero[f]} zero")
            if overridden[f]:
                bits.append(f"{overridden[f]} overridden")
            note = f"  ({', '.join(bits)})" if bits else ""
            print(f"    {f:<20} {bar} {have:>3}/{total} {pct:5.1f}%{note}")
        print()

    gaps = [(f, total - present[f]) for fields in GROUPS.values()
            for f in fields if present[f] < total]
    gaps.sort(key=lambda x: -x[1])
    if gaps:
        print("  Biggest gaps")
        for f, n in gaps[:6]:
            eg = ", ".join(missing_examples.get(f, [])[:5])
            print(f"    {f:<20} missing on {n:>3} — e.g. {eg}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
