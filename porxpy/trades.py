"""
Trade execution — moving value between cash and fund positions.

PorxPy is a portfolio *design* tool, not an accounting ledger. So a trade
here is not a financial event to be recorded; it is simply a transfer:

    cash.amount  -= shares_delta x price x fx
    fund.shares  += shares_delta

A sell is a buy with a negative ``shares_delta``, so one code path covers
both. Nothing historical is stored, because nothing historical is needed:
the current positions fully describe the portfolio. There is deliberately
no cost basis, no realised P&L and no transaction log — those answer
questions this tool is not asking, and carrying them would mean carrying
FX-at-trade-time, corporate actions and a cost-basis method too.

Why this is a batch primitive rather than a buy() and a sell()
-------------------------------------------------------------
Because the optimiser's output *is* a trade list. ``apply_trades`` is the
single execution path for both the manual Buy/Sell dialog (a list of one)
and the optimiser's "Apply this design" button (a list of many). Building
a bespoke manual path would mean writing the pricing, FX and validation
logic twice, and the two would drift.

Execution is **atomic**: every trade is validated and priced before any is
applied. A half-applied optimiser proposal — some legs filled, some
rejected — would leave the portfolio in a state nobody asked for and that
neither the user nor the optimiser can reason about. Better to reject the
batch and say why.
"""

from __future__ import annotations

from porxpy.utils import (
    cash_positions_get,
    cash_positions_set,
    find_portfolio,
    fx_rate,
    upsert_portfolio,
)


# Shares below this are treated as zero — a residue of 1e-12 shares is
# floating-point noise, not a position.
SHARES_EPS = 1e-9


def _last_close(price_history: list[dict]) -> float | None:
    """Last close from a price-history series, or None if unusable."""
    for row in reversed(price_history or []):
        try:
            v = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def apply_trades(pid: str, trades: list[dict], price_lookup) -> dict:
    """Validate and apply a batch of trades to a portfolio. All or nothing.

    Args:
        pid: Portfolio id.
        trades: ``[{"ticker", "shares_delta", "cash_id"}, ...]``.
            ``shares_delta`` is positive to buy, negative to sell.
            ``cash_id`` names the cash position that settles the trade —
            the user picks this per trade, since a portfolio may hold
            several cash pots in different currencies.
        price_lookup: ``ticker -> (price, currency)`` in the fund's own
            trading currency, or ``(None, None)`` if unpriceable. Injected
            rather than imported so this module stays free of Yahoo and
            trivially testable.

    Returns:
        ::

            {
              "ok":       bool,
              "errors":   [str, ...],       # non-empty => nothing applied
              "applied":  [{"ticker", "shares_delta", "price", "fx",
                            "cost", "cash_id", "shares_after",
                            "cash_after"}, ...],
              "warnings": [str, ...],
            }

        When ``ok`` is False the portfolio is untouched — not partially
        updated.
    """
    p = find_portfolio(pid)
    if not p:
        return {"ok": False, "errors": ["portfolio not found"],
                "applied": [], "warnings": []}

    funds = p.get("funds") or []
    cash  = cash_positions_get(pid)

    by_ticker = {(f.get("ticker") or "").upper(): f for f in funds}
    by_cash   = {c.get("id"): c for c in cash}

    errors:   list[str] = []
    warnings: list[str] = []
    planned:  list[dict] = []

    # Running tallies. Validation must consider the *cumulative* effect of
    # the batch, not each trade in isolation: three buys that each fit the
    # cash pot individually can still overdraw it together. Equally, a sell
    # earlier in the batch legitimately funds a buy later in it.
    shares_run: dict[str, float] = {}
    cash_run:   dict[str, float] = {}

    # Order the batch sells-first, regardless of how the caller listed it.
    #
    # The batch is atomic — it either all happens or none of it does — so
    # the order *within* it is ours to choose, not a user instruction. And
    # the natural order is: raise the cash, then spend it.
    #
    # This matters in practice. The optimiser emits trades sorted by size,
    # so a large buy routinely sits ahead of the sells that fund it. Walking
    # the list as given would reject that buy for insufficient cash and,
    # because we're atomic, throw away the whole rebalance — even though the
    # batch is perfectly affordable. A rebalance is precisely the case where
    # you sell one thing to buy another, so this is the common path, not an
    # edge case.
    #
    # The original position is kept so error messages still point at the row
    # the user is looking at, rather than at our internal ordering.
    def _is_buy(t: dict) -> bool:
        # Defensive: a non-numeric shares_delta must not blow up the sort.
        # Treat it as a buy so it sorts last and is reported by the
        # validation loop below, which produces a proper error message.
        try:
            return float(t.get("shares_delta") or 0.0) > 0
        except (TypeError, ValueError):
            return True

    ordered = sorted(enumerate(trades or []),
                     key=lambda pair: _is_buy(pair[1]))

    for orig_i, t in ordered:
        label = f"trade {orig_i + 1}"
        ticker = (t.get("ticker") or "").strip().upper()
        cash_id = (t.get("cash_id") or "").strip()

        try:
            delta = float(t.get("shares_delta"))
        except (TypeError, ValueError):
            errors.append(f"{label}: shares_delta must be numeric")
            continue

        if not ticker:
            errors.append(f"{label}: no ticker")
            continue
        if abs(delta) < SHARES_EPS:
            continue                       # no-op, silently skip

        fund = by_ticker.get(ticker)
        if not fund:
            errors.append(f"{label}: {ticker} is not in this portfolio")
            continue

        cpos = by_cash.get(cash_id)
        if not cpos:
            errors.append(f"{label}: cash position '{cash_id}' not found")
            continue

        price, fund_cur = price_lookup(ticker)
        if not price or price <= 0:
            errors.append(f"{label}: no price available for {ticker}")
            continue

        # FX from the fund's trading currency into the settling cash pot's
        # currency. Same-currency is the common case and costs nothing.
        cash_cur = (cpos.get("currency") or "").upper()
        fund_cur = (fund_cur or "").upper()
        if fund_cur and cash_cur and fund_cur != cash_cur:
            rate, _note = fx_rate(fund_cur, cash_cur)
            if not rate:
                errors.append(f"{label}: no FX rate {fund_cur}->{cash_cur}")
                continue
        else:
            rate = 1.0

        cur_shares = shares_run.get(ticker, float(fund.get("shares") or 0.0))
        cur_cash   = cash_run.get(cash_id, float(cpos.get("amount") or 0.0))

        cost = delta * price * rate      # sell => negative => cash rises
        new_shares = cur_shares + delta
        new_cash   = cur_cash - cost

        # Can't sell what you don't hold. (No shorting — and the optimiser
        # never proposes it, since weights are non-negative by construction.)
        if new_shares < -SHARES_EPS:
            errors.append(
                f"{label}: cannot sell {abs(delta):g} of {ticker} — "
                f"only {cur_shares:g} held")
            continue

        # Hard block on overdraft. The cash constraint is precisely what
        # makes the optimiser's job meaningful; letting it be violated
        # would hollow out the whole design premise.
        if new_cash < -0.005:            # half a cent of float tolerance
            errors.append(
                f"{label}: insufficient cash in '{cpos.get('name') or cash_id}' — "
                f"need {cost:,.2f} {cash_cur}, have {cur_cash:,.2f}")
            continue

        shares_run[ticker] = new_shares
        cash_run[cash_id]  = new_cash

        planned.append({
            "ticker":       ticker,
            "shares_delta": delta,
            "price":        price,
            "currency":     fund_cur,
            "fx":           rate,
            "cost":         round(cost, 2),
            "cash_id":      cash_id,
            "shares_after": round(max(0.0, new_shares), 6),
            "cash_after":   round(new_cash, 2),
        })

    # Atomic: one bad leg rejects the batch.
    if errors:
        return {"ok": False, "errors": errors, "applied": [],
                "warnings": warnings}
    if not planned:
        return {"ok": False, "errors": ["nothing to do"], "applied": [],
                "warnings": warnings}

    # ---- Commit ---------------------------------------------------------
    for ticker, sh in shares_run.items():
        # Clamp float dust to a clean zero so a fully-sold position reads
        # as 0 rather than 4.4e-16.
        by_ticker[ticker]["shares"] = 0.0 if abs(sh) < SHARES_EPS else round(sh, 8)
        if abs(sh) < SHARES_EPS:
            # Selling out leaves the fund in the portfolio at zero shares
            # rather than removing it: it stays a candidate the optimiser
            # can buy back into. Removing it is a separate, explicit act.
            warnings.append(
                f"{ticker} fully sold — kept in the portfolio at 0 shares")

    for cash_id, amt in cash_run.items():
        by_cash[cash_id]["amount"] = round(max(0.0, amt), 2)

    p["funds"] = funds
    upsert_portfolio(p)
    cash_positions_set(pid, cash)

    return {"ok": True, "errors": [], "applied": planned,
            "warnings": warnings}


def price_lookup_from_cache(cache_cfg: dict):
    """Build a ``ticker -> (price, currency)`` lookup backed by the cache.

    Reads the cached price history and profile written by
    ``load_fund_data``. Deliberately does **not** fetch: pricing a trade
    should never trigger a Yahoo round-trip, and a fund the user is
    trading has by definition been loaded already.
    """
    from porxpy.utils import cache_read

    def lookup(ticker: str):
        ph = (cache_read(ticker, "price_history")
              .get("price_history") or {}).get("value") or []
        price = _last_close(ph)
        if not price:
            return None, None
        prof = (cache_read(ticker, "profile")
                .get("profile") or {}).get("value") or {}
        return price, (prof.get("currency") or "").upper()

    return lookup
