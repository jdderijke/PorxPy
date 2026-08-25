"""
Best-in-class fund scoring.

Pure-compute module, like :mod:`porxpy.targets`. Ranks the pre-loaded
fund universe on cost, size and trailing returns, and hands back a score
per fund — one against the whole universe, one against the fund's peer
group.

Why ranks rather than raw values
--------------------------------
A TER is a fraction of a percent, a fund size is billions, a return is
tens of percent. Combining them directly means inventing exchange rates
between incomparable units, and whichever has the widest numeric spread
wins by accident. Percentiles remove the units entirely: every component
lands on 0-100 where higher is better, one outlier cannot dominate, and a
weight of 0.4 means the same thing for every component.

Percentiles rather than raw ranks because a rank is only meaningful
alongside the size of the field — rank 12 is excellent out of 200 and
poor out of 13. A preset tuned against today's 35 funds keeps its meaning
when the list reaches 50.

Two scores, and only one of them is for the optimiser
----------------------------------------------------
``score_all`` ranks a fund against every other fund. It answers "is this
a good fund", and it is what the fund list and detail page show.

``score_peer`` ranks it only against funds with the same asset class and
focus. It answers "is this the best fund *for this job*", which is the
only question the optimiser is ever asking: it needs a European bond fund
because the targets demand one, and how that fund compares to a US equity
tracker is irrelevant and misleading. Ranked globally, bond funds sit at
the bottom of any returns-weighted score permanently — not because they
are bad but because they are bonds — and a score-driven optimiser would
push toward equity, fighting the very targets it exists to satisfy.

So the optimiser reads ``score_peer`` and nothing else.

What happens when data is missing
---------------------------------
Component weights are renormalised across the components a fund actually
has. A fund with no TER on record is scored on size and returns alone, at
full weight, rather than being given a fabricated middling rank.

That does mean absent data can flatter a fund — a genuinely expensive
fund scores worse than one whose cost is unknown. Which is why every
score travels with its ``coverage``: a high score computed from one
component out of three is visibly thin, and the fund list can say so.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from porxpy.config import (
    DEFAULT_SIZE_FLOOR_BASE,
    LEGACY_FOCUS_TYPES,
    MIN_PEER_GROUP,
    RETURN_PERIODS,
    SCORE_COMPONENTS,
)


def _to_date(v) -> date | None:
    """Parse a price-history date cell. None when unusable."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not v:
        return None
    txt = str(v).strip()[:10]
    try:
        return datetime.strptime(txt, "%Y-%m-%d").date()
    except ValueError:
        return None


def _close(row) -> float | None:
    try:
        c = float(row.get("close"))
    except (TypeError, ValueError):
        return None
    return c if c > 0 else None


def trailing_returns(price_history: list[dict],
                     periods: dict[str, int] | None = None) -> dict[str, float | None]:
    """Percentage return over each trailing window.

    Args:
        price_history: ``[{"date": "YYYY-MM-DD", "close": float}, ...]``,
            oldest first (the order ``load_fund_data`` writes).
        periods: ``{label: days}``; defaults to
            :data:`porxpy.config.RETURN_PERIODS`.

    Returns:
        ``{label: pct or None}``. None when the series does not reach
        back far enough — a fund added last year genuinely has no 5-year
        number, and inventing one from its earliest available close would
        silently compare a partial period against full ones.

        These are PRICE returns: distributions are not added back. For
        accumulating funds that is the whole story; for distributing ones
        it understates by roughly the cumulative yield over the window,
        which is a real bias when the two are ranked against each other.
    """
    periods = periods or RETURN_PERIODS
    out: dict[str, float | None] = {k: None for k in periods}

    series = [(d, c) for d, c in
              ((_to_date(r.get("date")), _close(r)) for r in (price_history or []))
              if d and c]
    if len(series) < 2:
        return out
    series.sort(key=lambda x: x[0])

    last_d, last_c = series[-1]
    first_d = series[0][0]

    for label, days in periods.items():
        cutoff = last_d - timedelta(days=days)
        if first_d > cutoff:
            continue                      # series doesn't reach back that far
        # Nearest close at or before the cutoff. Walking backwards from
        # the end is fastest for short windows, which are the common case.
        base = None
        for d, c in reversed(series):
            if d <= cutoff:
                base = c
                break
        if base:
            out[label] = round((last_c - base) / base * 100.0, 4)
    return out


def _percentiles(values: dict[str, float], *, higher_is_better: bool) -> dict[str, float]:
    """Map ``{key: raw}`` to ``{key: percentile 0-100}``, higher = better.

    Ties share the average percentile of the positions they span, so two
    identical TERs cannot be separated by an accident of sort order.
    A single value scores 100 — there is nothing to be worse than.
    """
    if not values:
        return {}
    n = len(values)
    if n == 1:
        return {k: 100.0 for k in values}

    ordered = sorted(values.items(), key=lambda kv: kv[1],
                     reverse=not higher_is_better)
    # ordered[0] is the WORST, so index i maps to percentile i/(n-1)*100.
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        pct = (i + j) / 2.0 / (n - 1) * 100.0
        for k in range(i, j + 1):
            out[ordered[k][0]] = round(pct, 4)
        i = j + 1
    return out


def peer_key(fund: dict) -> str:
    """The fund's peer group: asset class x focus.

    Funds compete for a slot in the portfolio, and the slot is defined by
    what exposure it supplies. Two World equity trackers compete; a World
    tracker and a European bond fund do not.
    """
    # "primary_asset_class" since 0.47.0, and the rename matters here:
    # "asset_class" is also the name of a breakdown FACET (a distribution
    # over classes) and of the cache category holding it. This wants the
    # single fund-level label, which is the third thing.
    ac = (fund.get("primary_asset_class") or "unknown").strip().lower()
    ft = (fund.get("focus_type") or "none").strip().lower()
    fd = (fund.get("focus_detail") or "").strip().lower()
    # v0.68.0: "region" was renamed "geography". Normalised HERE rather
    # than by rewriting stored structures, because this is the one place
    # the value decides anything: a fund still carrying "region" while
    # its neighbours had moved to "geography" would land in a different
    # key and drop silently out of its own peer group — a group of one,
    # scored against nobody.
    ft = LEGACY_FOCUS_TYPES.get(ft, ft)
    return f"{ac}|{ft}|{fd}" if ft != "none" else f"{ac}|none|"


def _wtrr(returns_pct: dict[str, dict[str, float | None]],
          wtrr_weights: dict[str, float]) -> dict[str, float | None]:
    """Weighted trailing-return score per fund, as a percentile blend.

    Each period is percentiled across the funds that HAVE that period,
    then the periods are combined using the WTRR weights, renormalised
    per fund over the periods it actually has. A fund with three years of
    history is scored on the windows it can support rather than being
    marked down for its age — a young fund is not a bad fund.
    """
    per_period_pct: dict[str, dict[str, float]] = {}
    for label in RETURN_PERIODS:
        vals = {tk: r[label] for tk, r in returns_pct.items()
                if r.get(label) is not None}
        if vals:
            per_period_pct[label] = _percentiles(vals, higher_is_better=True)

    out: dict[str, float | None] = {}
    for tk in returns_pct:
        num = den = 0.0
        for label, pcts in per_period_pct.items():
            w = float(wtrr_weights.get(label) or 0.0)
            if w <= 0 or tk not in pcts:
                continue
            num += w * pcts[tk]
            den += w
        out[tk] = round(num / den, 4) if den > 0 else None
    return out


def score_universe(funds: list[dict],
                   component_weights: dict[str, float],
                   wtrr_weights: dict[str, float],
                   *,
                   size_floor: float = DEFAULT_SIZE_FLOOR_BASE,
                   min_peer_group: int = MIN_PEER_GROUP) -> dict[str, dict]:
    """Score every fund in the universe.

    Args:
        funds: ``[{"ticker", "primary_asset_class", "focus_type", "focus_detail",
            "ter", "size_base", "returns"}, ...]``. ``returns`` is the
            output of :func:`trailing_returns`; ``ter`` is a percent and
            ``size_base`` a base-currency amount, either of which may be
            None. The caller converts to base currency — this module does
            no FX.
        component_weights: ``{"ter","size","returns"}`` -> weight.
        wtrr_weights: ``{"1m","3m",...}`` -> weight.
        size_floor: Base-currency floor for the size test.
        min_peer_group: Peer groups smaller than this get no peer score.

    Returns:
        ``{ticker: {...}}`` with ``score_all``, ``score_peer``,
        ``peer_key``, ``peer_n``, ``components``, ``coverage``,
        ``covered``, ``returns_pct``.
    """
    if not funds:
        return {}

    tickers = [(f.get("ticker") or "").upper() for f in funds]
    by_ticker = {tk: f for tk, f in zip(tickers, funds) if tk}

    # ---- Component percentiles across the whole universe ----------------
    ter_vals = {tk: float(f["ter"]) for tk, f in by_ticker.items()
                if f.get("ter") is not None}
    # Cheap is good, so the ranking is inverted relative to the raw value.
    ter_pct = _percentiles(ter_vals, higher_is_better=False)

    # Size is a floor test, not a ranking. Above the floor, more is not
    # better: a fund twice the size of another is not twice as safe to
    # hold. What matters is clearing the bar.
    size_pct: dict[str, float] = {}
    for tk, f in by_ticker.items():
        sz = f.get("size_base")
        if sz is None:
            continue
        try:
            size_pct[tk] = 100.0 if float(sz) >= size_floor else 0.0
        except (TypeError, ValueError):
            continue

    returns_by_ticker = {tk: (f.get("returns") or {}) for tk, f in by_ticker.items()}
    ret_pct = _wtrr(returns_by_ticker, wtrr_weights)

    component_pct = {"ter": ter_pct, "size": size_pct,
                     "returns": {k: v for k, v in ret_pct.items() if v is not None}}

    # Which components this fund HAS DATA for. Deliberately independent
    # of the weights: under the Cost driven preset every WTRR weight is
    # zero, so the returns component blends to None even for a fund with
    # ten years of history. Counting that as missing data would report
    # 2/3 coverage for a fund whose data is complete, and the coverage
    # figure exists precisely to tell the user where the gaps are.
    has_data = {
        tk: {
            "ter":     tk in ter_vals,
            "size":    tk in size_pct,
            "returns": any(v is not None
                           for v in (returns_by_ticker.get(tk) or {}).values()),
        }
        for tk in by_ticker
    }

    def _blend(tk: str, pool: dict[str, dict[str, float]]) -> tuple[float | None, dict, int]:
        """Weighted blend over the components this fund actually has."""
        num = den = 0.0
        parts: dict[str, float | None] = {}
        for comp in SCORE_COMPONENTS:
            w = float(component_weights.get(comp) or 0.0)
            val = pool.get(comp, {}).get(tk)
            parts[comp] = val
            if val is None or w <= 0:
                continue
            num += w * val
            den += w
        have = sum(1 for v in has_data.get(tk, {}).values() if v)
        return (round(num / den, 2) if den > 0 else None), parts, have

    # ---- Peer-group percentiles -----------------------------------------
    groups: dict[str, list[str]] = {}
    for tk, f in by_ticker.items():
        groups.setdefault(peer_key(f), []).append(tk)

    peer_component_pct: dict[str, dict[str, dict[str, float]]] = {}
    for key, members in groups.items():
        if len(members) < min_peer_group:
            continue
        sub_ter = {tk: ter_vals[tk] for tk in members if tk in ter_vals}
        sub_ret = {tk: returns_by_ticker[tk] for tk in members}
        peer_component_pct[key] = {
            "ter":  _percentiles(sub_ter, higher_is_better=False),
            # The floor test is absolute — clearing EUR 500m is the same
            # achievement in any peer group — so it is not re-percentiled.
            "size": {tk: size_pct[tk] for tk in members if tk in size_pct},
            "returns": {k: v for k, v in _wtrr(sub_ret, wtrr_weights).items()
                        if v is not None},
        }

    out: dict[str, dict] = {}
    n_comps = len(SCORE_COMPONENTS)
    for tk, f in by_ticker.items():
        key = peer_key(f)
        members = groups.get(key, [])
        score_all, parts, have = _blend(tk, component_pct)

        if key in peer_component_pct:
            score_peer, _p, _h = _blend(tk, peer_component_pct[key])
        else:
            # Too few peers to rank against. Not a failure — a fund with
            # no peers genuinely cannot be best in class, and scoring it
            # 100 by default would make every uniquely positioned fund
            # look ideal.
            score_peer = None

        out[tk] = {
            # Carried so a consumer listing a peer group can name its
            # members without a second lookup: the group is derived from
            # peer_key here, and a ticker alone does not say what a fund
            # is.
            "name":        f.get("name") or tk,
            "isin":        f.get("isin") or "",
            "score_all":   score_all,
            "score_peer":  score_peer,
            "peer_key":    key,
            "peer_n":      len(members),
            # The group itself, so "which funds am I ranked against" is
            # answerable directly. Includes this fund: the list is the
            # peer group, and a fund is a member of its own.
            "peers":       sorted(members),
            "components":  parts,
            "covered":     have,
            "coverage":    round(have / n_comps, 4),
            "has_data":    has_data.get(tk, {}),
            "returns_pct": returns_by_ticker.get(tk) or {},
        }
    return out


def portfolio_score(weights_by_ticker: dict[str, float],
                    scores: dict[str, dict],
                    field: str = "score_peer") -> float | None:
    """Weight-averaged score of a portfolio.

    Weighted rather than a plain average because a 2% position in a poor
    fund matters about a fiftieth as much as a 40% one. Funds with no
    score are excluded from both sides of the average, so an unscoreable
    fund neither helps nor hurts.

    Returns None when nothing in the portfolio can be scored.
    """
    num = den = 0.0
    for tk, w in (weights_by_ticker or {}).items():
        s = (scores.get((tk or "").upper()) or {}).get(field)
        if s is None or w <= 0:
            continue
        num += w * s
        den += w
    return round(num / den, 2) if den > 0 else None
