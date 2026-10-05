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
              "bad_trades": [int, ...],     # positions in `trades` the
                                            # errors are about (v0.132.0)
            }

        When ``ok`` is False the portfolio is untouched — not partially
        updated. Besides an overdraft, a purchase that would take a
        currency's accounts below the cash the user keeps in it
        (:func:`porxpy.utils.cash_reserves_get`) refuses the batch.
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
    # Which trades the errors are about, by their position in the list
    # the caller sent (v0.132.0) — so the trade table can highlight the
    # rows, rather than leaving the user to count down to "trade 27".
    bad: set[int] = set()

    def _fail(i: int, msg: str) -> None:
        errors.append(msg)
        bad.add(i)

    base_cur = (p.get("base_currency") or "").upper()

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
            _fail(orig_i, f"{label}: shares_delta must be numeric")
            continue

        if not ticker:
            _fail(orig_i, f"{label}: no ticker")
            continue
        if abs(delta) < SHARES_EPS:
            continue                       # no-op, silently skip

        fund = by_ticker.get(ticker)
        if not fund:
            _fail(orig_i, f"{label}: {ticker} is not in this portfolio")
            continue

        cpos = by_cash.get(cash_id)
        if not cpos:
            _fail(orig_i, f"{label}: cash position '{cash_id}' not found")
            continue

        price, fund_cur = price_lookup(ticker)
        if not price or price <= 0:
            _fail(orig_i, f"{label}: no price available for {ticker}")
            continue

        # FX from the fund's trading currency into the settling cash pot's
        # currency. Same-currency is the common case and costs nothing.
        cash_cur = (cpos.get("currency") or "").upper()
        fund_cur = (fund_cur or "").upper()
        if fund_cur and cash_cur and fund_cur != cash_cur:
            rate, _note = fx_rate(fund_cur, cash_cur)
            if not rate:
                _fail(orig_i, f"{label}: no FX rate {fund_cur}->{cash_cur}")
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
            _fail(orig_i,
                f"{label}: cannot sell {abs(delta):g} of {ticker} — "
                f"only {cur_shares:g} held")
            continue

        # Hard block on overdraft. The cash constraint is precisely what
        # makes the optimiser's job meaningful; letting it be violated
        # would hollow out the whole design premise.
        if new_cash < -0.005:            # half a cent of float tolerance
            _fail(orig_i,
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
            "cash_currency": cash_cur or base_cur,
            "_i":           orig_i,
        })

    # The cash the user keeps, per currency (v0.132.0). Checked after the
    # per-trade walk, in the same sells-first order, against the TOTAL of
    # each currency's accounts — a reserve is per currency, not per
    # account. Only a purchase can break it, and it is refused only when
    # it takes that currency below its reserve: a sale never is, and
    # neither is a batch that simply leaves a currency as short as it
    # already was. The trade that crosses the line is named, because the
    # usual cause is one account chosen by hand.
    #
    # A small tolerance — a tenth of a percent of the reserve, at least
    # RESERVE_TOLERANCE_UNITS — because the plan works from the
    # optimiser's rounded amounts while this walk re-prices from shares,
    # and a split leg lands a reserve exactly, so the two can disagree in
    # the last few units.
    if not errors:
        from porxpy.utils import cash_reserves_get
        reserves = cash_reserves_get(pid)
        if reserves:
            pool: dict[str, float] = {}
            for c in cash:
                cur = (c.get("currency") or base_cur).upper()
                pool[cur] = pool.get(cur, 0.0) + float(c.get("amount") or 0.0)
            for leg in planned:
                cur = (leg.get("cash_currency") or base_cur).upper()
                pool[cur] = pool.get(cur, 0.0) - leg["cost"]
                need = reserves.get(cur)
                if (not need or leg["shares_delta"] <= 0
                        or pool[cur] >= need - reserve_tolerance(need)):
                    continue
                cname = (by_cash.get(leg["cash_id"]) or {}).get("name") or leg["cash_id"]
                _fail(leg["_i"],
                      f"trade {leg['_i'] + 1}: buying {leg['ticker']} from "
                      f"'{cname}' leaves your {cur} accounts at "
                      f"{pool[cur]:,.2f} {cur}, below the {need:,.2f} {cur} you "
                      f"keep. Settle it from another account, or lower the "
                      f"{cur} you keep.")
                break

    # Atomic: one bad leg rejects the batch.
    if errors:
        return {"ok": False, "errors": errors, "applied": [],
                "warnings": warnings, "bad_trades": sorted(bad)}
    if not planned:
        return {"ok": False, "errors": ["nothing to do"], "applied": [],
                "warnings": warnings}

    # ---- Commit ---------------------------------------------------------
    sold_out: list[str] = []
    for ticker, sh in shares_run.items():
        if abs(sh) < SHARES_EPS:
            # Selling out removes the fund from the portfolio.
            #
            # It used to stay at zero shares on the reasoning that it
            # remained a candidate to buy back into. That reasoning was
            # wrong about where candidacy comes from: the optimiser
            # draws candidates from the pre-loaded fund list, not from
            # the portfolio, so a removed fund is just as buyable as it
            # was before. All the zero-shares row bought was a portfolio
            # holding nothing, cluttering every table that lists it.
            sold_out.append(ticker)
            warnings.append(f"{ticker} fully sold — removed from the portfolio")
        else:
            # Clamp float dust so a near-exact position reads cleanly.
            by_ticker[ticker]["shares"] = round(sh, 8)

    if sold_out:
        gone = set(sold_out)
        funds = [f for f in funds
                 if (f.get("ticker") or "").upper() not in gone]

    for cash_id, amt in cash_run.items():
        by_cash[cash_id]["amount"] = round(max(0.0, amt), 2)

    p["funds"] = funds
    upsert_portfolio(p)
    cash_positions_set(pid, cash)

    for leg in planned:
        leg.pop("_i", None)
    return {"ok": True, "errors": [], "applied": planned,
            "warnings": warnings, "bad_trades": []}


def price_lookup_from_cache(cache_cfg: dict):
    """Build a ``ticker -> (price, currency)`` lookup backed by the cache.

    Reads the cached price history and profile written by
    ``load_fund_data``. Deliberately does **not** fetch: pricing a trade
    should never trigger a Yahoo round-trip, and a fund the user is
    trading has by definition been loaded already.
    """
    from porxpy.utils import cache_read, normalise_currency

    def lookup(ticker: str):
        ph = (cache_read(ticker, "price_history")
              .get("price_history") or {}).get("value") or []
        price = _last_close(ph)
        if not price:
            return None, None
        prof = (cache_read(ticker, "profile")
                .get("profile") or {}).get("value") or {}
        # Whole units of the canonical currency (v0.131.0) — the same
        # rule the valuation and the optimiser's pricing apply. Upper-
        # casing GBp to GBP without dividing settled a pence-quoted fund
        # at 100x its price.
        cur, divisor = normalise_currency(prof.get("currency") or "")
        return price / divisor, cur

    return lookup


# Left in every account the plan drains to its floor, in that account's
# own units. The plan converts exactly as apply_trades does, but it works
# from the optimiser's rounded amounts while apply_trades re-prices from
# shares, so the two can differ by cents; an account planned to end at
# exactly 0 could then read as overdrawn by a cent and refuse the batch.
_SETTLE_BUFFER = 2.0

# How far below its reserve apply_trades lets a currency end before it
# refuses: the larger of this many units and a tenth of a percent of the
# reserve. Covers the buffer above and the same cents of re-pricing.
RESERVE_TOLERANCE_UNITS = 5.0


def reserve_tolerance(reserve: float) -> float:
    """How far below ``reserve`` a currency may end before it counts as short.

    One rule for the planner's warning and apply_trades' refusal, so the
    plan never warns about a batch that applies, nor stays quiet about
    one that is refused. A tenth of a percent covers Yahoo quoting the
    two directions of a currency pair separately — they differ by about
    a hundredth of a percent, which on a large cross-currency batch is
    tens of units — plus cents of re-pricing.
    """
    return max(RESERVE_TOLERANCE_UNITS, float(reserve or 0.0) * 0.001)


def plan_settlement(trades: list[dict], cash_positions: list[dict],
                    base_cur: str, fx,
                    reserves: dict | None = None) -> tuple[list[list], list[str]]:
    """Choose which cash account settles each trade, for the batch as a whole.

    Why this exists
    ---------------
    A trade moves money between one fund and one cash account, and the
    optimiser plans against the TOTAL of every account — so defaults
    chosen one trade at a time strand money. v0.131.0 sent each trade to
    the largest account in its own currency; selling dollar funds to buy
    euro funds then paid every sale into dollars and every purchase out
    of euros, and a rebalance the cash covered was refused. And the cash
    the user keeps (v0.132.0: one amount PER CURRENCY) has to end up in
    the accounts of that currency, which no per-trade rule can promise.

    The rule
    --------
    Each account must end at or above its FLOOR: its share of the
    reserve for its currency, split across that currency's accounts in
    proportion to what each holds (all of it on the largest when they
    hold less than the reserve). Within that:

    1. Every sale pays into the largest account in its own currency, and
       every purchase is paid from the account in its own currency with
       the most room — no conversion where none is needed.
    2. While some account ends below its floor, the shortfall is moved
       to an account with room to spare, preferring one in the same
       currency: first by moving a whole purchase there, then by paying a
       whole sale into the short account instead, and only when no whole
       trade fits, by SPLITTING one purchase across the two.

    Usually one trade splits: the optimiser spends down to the reserve
    exactly, so whole trades seldom land each currency's reserve to the
    euro on their own. A split trade is two legs of the same fund settled
    against two accounts, which apply_trades handles like any two trades.

    Every account is kept in its OWN currency here, and a leg is
    converted from the fund's currency to the account's at the same
    ``fx`` apply_trades uses — so what is planned is what is applied,
    give or take cents of rounding. Base currency is used only to compare
    accounts with each other. (A first version compared everything in
    base currency with a safety margin on each conversion; with no slack
    in the batch, the margin had nowhere to go but the reserve.)

    The plan is a default. The user can change any account before
    applying, and apply_trades refuses a choice that would leave a
    currency below its reserve — naming the trade that does it.

    Args:
        trades: The optimiser's trades — ``amount_base`` (signed, base
            currency, positive to buy) and ``currency``.
        cash_positions: The accounts — ``id``, ``currency``, ``amount``.
        base_cur: The portfolio's base currency.
        fx: ``(from_currency, to_currency) -> rate or None`` — the same
            lookup apply_trades converts with.
        reserves: ``{currency: amount}`` in each currency's own units.

    Returns:
        ``(legs, notes)``. ``legs[i]`` is ``[(cash_id, fraction), ...]``
        for trade ``i``, fractions summing to 1 — one entry unless it was
        split; empty only when no account could be used. ``notes`` says
        what could not be met, if anything.
    """
    base = (base_cur or "").upper()
    n = len(trades or [])
    notes: list[str] = []

    def rate(a: str, b: str) -> float | None:
        return 1.0 if a == b else fx(a, b)

    units: dict[str, dict] = {}
    for c in cash_positions or []:
        if not c.get("id"):
            continue
        cur = (c.get("currency") or base).upper()
        to_base = rate(cur, base)
        if not to_base:
            continue           # cannot be compared with the others
        try:
            amt = float(c.get("amount") or 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        units[c["id"]] = {"cur": cur, "to_base": to_base,
                          "bal": amt, "floor": 0.0}
    if not units:
        return [[] for _ in range(n)], notes

    # Floors, in each account's own units: the currency's reserve spread
    # over its accounts, plus the rounding buffer on any account that
    # holds no reserve (one that does may land on it exactly — that is
    # what the reserve tolerance in apply_trades is for).
    for cur, res in (reserves or {}).items():
        mine = [u for u in units.values() if u["cur"] == str(cur).upper()]
        if not mine or not res:
            continue
        held = sum(max(0.0, u["bal"]) for u in mine)
        if held >= float(res) and held > 0:
            for u in mine:
                u["floor"] = float(res) * max(0.0, u["bal"]) / held
        else:
            max(mine, key=lambda u: u["bal"])["floor"] = float(res)
    for u in units.values():
        if u["floor"] == 0.0:
            u["floor"] = _SETTLE_BUFFER

    kind = ["buy" if float(t.get("amount_base") or 0.0) > 0 else "sell"
            for t in trades]
    tcur = [(t.get("currency") or base).upper() for t in trades]
    # Each trade in its fund's own currency — the optimiser priced it into
    # base at this same pair, so dividing it back out is exact.
    native = []
    for i, t in enumerate(trades):
        r = rate(tcur[i], base) or 1.0
        native.append(abs(float(t.get("amount_base") or 0.0)) / r)

    def conv(i: int, u: str) -> float | None:
        return rate(tcur[i], units[u]["cur"])

    # legs[i]: {account id: amount in the FUND's currency}
    legs: list[dict[str, float]] = [dict() for _ in range(n)]

    def room(u: str) -> float:
        """What the account can still give, in its own currency."""
        e = units[u]["bal"]
        for i in range(n):
            x = legs[i].get(u)
            if x:
                e += x * conv(i, u) if kind[i] == "sell" else -x * conv(i, u)
        return e - units[u]["floor"]

    def room_base(u: str) -> float:
        return room(u) * units[u]["to_base"]

    def usable(i: int, u: str) -> bool:
        return bool(conv(i, u))

    def own(i: int, by_room: bool) -> str:
        cands = [u for u in units if usable(i, u)]
        same = [u for u in cands if units[u]["cur"] == tcur[i]]
        pool = same or [u for u in cands if units[u]["cur"] == base] or cands
        return max(pool, key=room_base if by_room else
                   (lambda u: units[u]["bal"] * units[u]["to_base"]))

    def move(i: int, src: str, dst: str, x: float) -> None:
        legs[i][src] -= x
        if legs[i][src] <= 1e-9:
            del legs[i][src]
        legs[i][dst] = legs[i].get(dst, 0.0) + x

    # 1. Own currency: sells first, then buys, each largest first — the
    #    order apply_trades applies them in.
    order = sorted(range(n), key=lambda i: (kind[i] == "buy",
                                            -float(abs(trades[i].get("amount_base") or 0))))
    for i in order:
        if any(usable(i, u) for u in units):
            legs[i][own(i, by_room=(kind[i] == "buy"))] = native[i]

    # 2. Move shortfalls to accounts with room. Every step removes part of
    #    a shortfall without creating one, so this ends; the bound is a
    #    guard, not a tuning knob.
    for _ in range(4 * n + 20):
        short = min(units, key=room_base)
        if room(short) >= -0.005:
            break
        donors = sorted((u for u in units if u != short and room(u) > 0.005),
                        key=lambda u: (units[u]["cur"] != units[short]["cur"],
                                       -room_base(u)))
        if not donors:
            break
        buys = sorted((i for i in range(n) if kind[i] == "buy" and short in legs[i]),
                      key=lambda i: -legs[i][short] * conv(i, short))
        done = False
        # (a) a whole purchase off the short account
        for d in donors:
            for i in buys:
                if usable(i, d) and legs[i][short] * conv(i, d) <= room(d):
                    move(i, short, d, legs[i][short])
                    done = True
                    break
            if done:
                break
        # (b) a whole sale paid into the short account instead
        if not done:
            for d in donors:
                for i in sorted((i for i in range(n)
                                 if kind[i] == "sell" and d in legs[i]
                                 and usable(i, short)),
                                key=lambda i: -legs[i][d]):
                    if legs[i][d] * conv(i, d) <= room(d):
                        move(i, d, short, legs[i][d])
                        done = True
                        break
                if done:
                    break
        # (c) split one purchase: the one closest to the gap, so the
        #     smaller part is as small as it can be.
        if not done and buys:
            gap = -room(short)
            for d in donors:
                fits = [i for i in buys if usable(i, d)]
                if not fits:
                    continue
                i = min(fits, key=lambda j: (legs[j][short] * conv(j, short) < gap,
                                             abs(legs[j][short] * conv(j, short) - gap)))
                x = min(legs[i][short], gap / conv(i, short), room(d) / conv(i, d))
                if x > 1e-6:
                    move(i, short, d, x)
                    done = True
                    break
        if not done:
            break

    # The same allowance apply_trades grants, so the plan only warns about
    # a batch that will actually be refused.
    worst = min(units, key=room_base)
    if room(worst) < -reserve_tolerance(units[worst]["floor"]):
        v = units[worst]
        notes.append(f"The trades cannot all be settled without taking your "
                     f"{v['cur']} accounts below what they should hold (short "
                     f"by about {-room(worst):,.0f} {v['cur']}). Choose the "
                     f"accounts by hand, or lower the cash you keep.")

    out: list[list] = []
    for i in range(n):
        tot = sum(legs[i].values())
        out.append([(u, x / tot) for u, x in legs[i].items()] if tot else [])
    return out, notes
