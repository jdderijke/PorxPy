#!/usr/bin/env python3
"""Dump the raw Yahoo values behind a fund's profile figures.

Usage::

    python tools/inspect_fund.py TDIV.AS
    python tools/inspect_fund.py IWDA.AS EIMI.L VWRL.AS

Prints, with no interpretation applied, every Yahoo field PorxPy
considers for TER, turnover and total net assets — plus what the
extractor ultimately decided and why.

This exists because the fees-and-size figures come from two Yahoo
surfaces that disagree with each other in both value and apparent unit,
and yfinance requests quoteSummary with ``formatted=false``, so Yahoo's
own "fmt" string — the one field that would state the scale outright —
never reaches us. Two attempts to infer the unit from magnitude were
both wrong. The way to settle it is to look at the numbers.

Needs network access. Nothing is written: no cache, no overrides.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf                                       # noqa: E402

from porxpy.extractors import extract_fund_operations       # noqa: E402


# Every key that has ever plausibly carried one of these figures. Listed
# rather than filtered, so a key that is absent shows up as absent — that
# is itself the finding for most European UCITS listings.
INFO_KEYS = (
    "netExpenseRatio", "annualReportExpenseRatio",
    "annualHoldingsTurnover",
    "netAssets", "totalAssets",
    "currency", "quoteType", "longName",
)


def show(ticker: str) -> None:
    print(f"\n{'=' * 68}\n{ticker}\n{'=' * 68}")
    t = yf.Ticker(ticker)

    try:
        info = t.info or {}
    except Exception as exc:
        print(f"  .info failed: {exc}")
        info = {}

    print("\n  ticker.info")
    for k in INFO_KEYS:
        v = info.get(k)
        mark = "     " if v is None else "  -> "
        print(f"  {mark}{k:<26} {v!r}")

    print("\n  funds_data.fund_operations (raw frame)")
    try:
        ops_df = t.funds_data.fund_operations
        if ops_df is None or ops_df.empty:
            print("       <empty — Yahoo has no fundProfile for this listing>")
        else:
            for line in ops_df.to_string().splitlines():
                print(f"       {line}")
    except Exception as exc:
        print(f"       failed: {exc}")

    print("\n  extract_fund_operations() as PorxPy reads it")
    try:
        for k, v in extract_fund_operations(t).items():
            print(f"       {k:<20} {v!r}")
    except Exception as exc:
        print(f"       failed: {exc}")

    # The interpretation question, laid out rather than answered. If the
    # candidates below straddle the real fund size by a clean factor, the
    # unit is knowable; if none of them lands near it — as with
    # NL0011683594's 84139 against a real 8.59bn — the number is not a
    # scaled fund size and no arithmetic will make it one.
    # The category column is the tell. Where the fund column and the
    # category-average column carry the SAME number, the field is not
    # fund-specific — that is how fund_operations.totalNetAssets was
    # caught returning a category aggregate for every fund.
    print("\n  fund-vs-category check (identical => not fund-specific)")
    try:
        ops_df = t.funds_data.fund_operations
        if ops_df is not None and not ops_df.empty and ops_df.shape[1] >= 2:
            for attr in ops_df.index:
                a, b = ops_df.iloc[:, 0][attr], ops_df.iloc[:, 1][attr]
                same = (a == b) and a == a          # NaN != NaN
                flag = "  <-- SAME, category value" if same else ""
                print(f"       {str(attr):<30} {a!r:>14}  {b!r:>14}{flag}")
    except Exception as exc:
        print(f"       failed: {exc}")

    print("\n  total assets: candidates under each reading")
    cur = info.get("currency") or ""
    for label, raw in (("fund_operations", extract_fund_operations(t).get("totalNetAssets")),
                       ("info.netAssets", info.get("netAssets")),
                       ("info.totalAssets", info.get("totalAssets"))):
        if raw is None:
            print(f"       {label:<18} absent")
            continue
        try:
            f = float(raw)
        except (TypeError, ValueError):
            print(f"       {label:<18} non-numeric: {raw!r}")
            continue
        print(f"       {label:<18} raw={f:,.0f}   "
              f"as-units={f:,.0f} {cur}   "
              f"x1e3={f * 1e3:,.0f}   x1e6={f * 1e6:,.0f}")
    print("\n  Compare against the issuer's published fund size to see which "
          "\n  reading — if any — is correct.")


def main() -> int:
    tickers = sys.argv[1:]
    if not tickers:
        print(__doc__)
        return 1
    for tk in tickers:
        try:
            show(tk)
        except Exception as exc:
            print(f"\n{tk}: failed — {exc}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
