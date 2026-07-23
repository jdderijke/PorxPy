"""
Portfolio construction / rebalancing optimiser.

Pure compute. Given a set of candidate funds (each with a look-through
exposure vector), the user's per-facet exposure targets, and the money
available, this module chooses a small set of funds and their weights so
that the blended portfolio exposure sits as close to the targets as
possible — then expresses the answer as a buy/sell list.

Design notes
------------

**Construction and rebalancing are the same problem.** A from-scratch
design is just a portfolio in which every position happens to be zero and
all the money sits in cash. So there is one solver, and it always emits a
trade list (``shares_delta`` per fund) rather than a set of weights: for a
fresh portfolio those deltas are all buys; for an existing one they are
the buys and sells needed to reach the target.

**Cash is an asset, not just a budget.** Cash carries real exposure (asset
class "cash", a currency, a country), and users often target holding some
of it. So cash enters the optimisation as one more column of the exposure
matrix, and its weight is a free variable like any fund's. Two things fall
out for free: the "leave 5% in cash" target works with no special-casing,
and the no-overdraft constraint is automatic — cash weight is constrained
non-negative like everything else, so the solver simply cannot spend money
that isn't there.

**Why not L1 for sparsity.** The obvious move for "pick a few funds out of
200" is an L1 penalty. It does nothing here. Weights are non-negative and
sum to one (a simplex), so ``||w||_1 == sum(w) == 1`` identically — the
penalty is a constant and has no gradient. Instead we use greedy forward
selection: repeatedly add whichever single fund most reduces the error,
stop when it stops helping or we hit ``max_funds``, then re-solve on the
chosen set. This is fast (the sub-problems are tiny), needs no MIP solver,
and — the real benefit — is *explainable*: the caller gets the funds in the
order they were chosen and what each one bought in error reduction.

**No scipy.** The problem is small (a handful of chosen funds × a few dozen
target buckets) and convex, so FISTA-accelerated projected gradient in
numpy solves it to tolerance in microseconds. numpy already arrives with
pandas, so this module adds no dependency — which also keeps the planned
PyInstaller Windows build free of scipy, its known packaging obstacle.

Everything here works in **base currency** and in **fractions** (0–1).
Converting prices/FX and turning percent targets into fractions is the
caller's job (see ``app.api_portfolio_optimize``); this module never talks
to Yahoo and never reads a file, which keeps it trivially testable.
"""

from __future__ import annotations

import numpy as np


# A synthetic bucket collecting every exposure that falls outside the
# buckets the user actually targeted. See _build_facet_matrix.
OTHER_BUCKET = "__other__"

# Weights below this are treated as zero when reporting the solution — a
# 0.02% allocation is solver noise, not an intention.
WEIGHT_EPS = 1e-4


# ---------------------------------------------------------------------------
# Core numerics
# ---------------------------------------------------------------------------
def _project_simplex(v: np.ndarray) -> np.ndarray:
    """Euclidean projection of ``v`` onto the probability simplex.

    Returns the closest point to ``v`` satisfying ``w >= 0`` and
    ``sum(w) == 1``. Standard sort-based algorithm (Duchi et al., 2008);
    O(n log n) and exact, not iterative.

    This projection is what enforces both of our hard constraints at once:
    no short positions, and no spending money we don't have (since cash is
    one of the columns, cash weight >= 0 *is* the no-overdraft rule).
    """
    n = v.shape[0]
    if n == 0:
        return v
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - 1.0
    idx = np.arange(1, n + 1)
    cond = u - css / idx > 0
    if not cond.any():
        # Degenerate; fall back to uniform.
        return np.full(n, 1.0 / n)
    rho = idx[cond][-1]
    theta = css[cond][-1] / float(rho)
    return np.maximum(v - theta, 0.0)


def _solve_weights(A: np.ndarray, t: np.ndarray,
                   max_iter: int = 500,
                   tol: float = 1e-10) -> tuple[np.ndarray, float]:
    """Minimise ``||A w - t||^2`` subject to ``w >= 0``, ``sum(w) == 1``.

    FISTA-accelerated projected gradient. Convex with a simple projection,
    so this converges reliably; the problems are tiny (columns = chosen
    funds + cash, rows = target buckets) so 500 iterations is generous.

    Args:
        A: ``(n_buckets, n_assets)`` exposure matrix. Column ``j`` is
            asset ``j``'s exposure across every target bucket.
        t: ``(n_buckets,)`` target exposure vector.

    Returns:
        ``(w, sse)`` — the optimal weights and the residual sum of squares.
    """
    n = A.shape[1]
    if n == 0:
        return np.zeros(0), float(t @ t)
    if n == 1:
        w = np.ones(1)
        r = A @ w - t
        return w, float(r @ r)

    AtA = A.T @ A
    Atb = A.T @ t

    # Step size = 1/L where L is the Lipschitz constant of the gradient.
    L = float(np.linalg.eigvalsh(AtA)[-1]) if n > 1 else 1.0
    if L <= 0 or not np.isfinite(L):
        L = 1.0
    step = 1.0 / L

    w = np.full(n, 1.0 / n)
    y = w.copy()
    t_k = 1.0
    prev = w.copy()

    for _ in range(max_iter):
        grad = AtA @ y - Atb
        w_new = _project_simplex(y - step * grad)

        t_next = (1.0 + np.sqrt(1.0 + 4.0 * t_k * t_k)) / 2.0
        y = w_new + ((t_k - 1.0) / t_next) * (w_new - w)
        t_k = t_next
        w = w_new

        if np.max(np.abs(w - prev)) < tol:
            break
        prev = w.copy()

    r = A @ w - t
    return w, float(r @ r)


# ---------------------------------------------------------------------------
# Building the exposure matrix
# ---------------------------------------------------------------------------
def _build_facet_matrix(candidates: list[dict],
                        cash_exposure: dict,
                        targets: dict,
                        facet_weights: dict | None) -> tuple[np.ndarray, np.ndarray, list]:
    """Assemble the exposure matrix and target vector across all facets.

    Only facets the user actually set targets for take part — an
    untargeted facet is a facet they don't care about, and penalising it
    would invent an intention they never expressed.

    Within a targeted facet, everything *outside* the targeted buckets is
    collapsed into a single :data:`OTHER_BUCKET`, whose target is
    ``1 - sum(targets)``. This one trick handles both cases correctly:

    * Targets sum to 100% → "other" has target 0, so any stray exposure is
      penalised. ("I want exactly this mix.")
    * Targets sum to less than 100% → "other" absorbs the slack with no
      penalty. ("I care about these buckets; the rest is free.")

    Facets are normalised to comparable scale and then multiplied by
    ``facet_weights`` so that, say, asset-class accuracy can be made to
    matter more than sector accuracy.

    Returns:
        ``(A, t, rows)`` where ``A`` is ``(n_buckets, n_assets)``, ``t`` is
        ``(n_buckets,)``, and ``rows`` is ``[(facet, bucket), ...]``
        labelling each row for the caller's diagnostics. Assets are ordered
        as ``candidates`` then cash (cash is always the last column).
    """
    facet_weights = facet_weights or {}
    n_assets = len(candidates) + 1          # +1 for cash

    A_rows: list[np.ndarray] = []
    t_vals: list[float] = []
    rows: list[tuple[str, str]] = []
    # Unscaled twins. The scaled matrix is what the solver optimises; these
    # are what we *measure* with, because an error the user is asked to set a
    # threshold on has to be in units they recognise — percentage points of
    # exposure, not the solver's internal scaling.
    A_raw_rows: list[np.ndarray] = []
    t_raw_vals: list[float] = []
    is_explicit: list[bool] = []

    for facet, tgt in (targets or {}).items():
        if not tgt:
            continue                        # facet has no targets → ignore

        keys = sorted(tgt.keys())
        tgt_sum = sum(float(v) for v in tgt.values())
        # Slack for the "everything else" bucket. Clamped: an over-100%
        # target set is a user error, not something to model.
        other_target = max(0.0, 1.0 - tgt_sum)

        # A facet's rows are scaled so the facet as a whole contributes
        # comparably regardless of how many buckets it happens to have —
        # otherwise a 40-bucket country target would drown out a 4-bucket
        # asset-class target purely on row count.
        fw = float(facet_weights.get(facet, 1.0))
        scale = fw / np.sqrt(len(keys) + 1)

        for key in keys + [OTHER_BUCKET]:
            row = np.zeros(n_assets)
            for j, c in enumerate(candidates):
                exp_f = (c.get("exposures") or {}).get(facet) or {}
                if key == OTHER_BUCKET:
                    row[j] = max(0.0, 1.0 - sum(float(exp_f.get(k, 0.0))
                                                for k in keys))
                else:
                    row[j] = float(exp_f.get(key, 0.0))

            cash_f = (cash_exposure or {}).get(facet) or {}
            if key == OTHER_BUCKET:
                row[-1] = max(0.0, 1.0 - sum(float(cash_f.get(k, 0.0))
                                             for k in keys))
            else:
                row[-1] = float(cash_f.get(key, 0.0))

            raw_target = (other_target if key == OTHER_BUCKET
                          else float(tgt[key]))
            A_rows.append(row * scale)
            t_vals.append(raw_target * scale)
            rows.append((facet, key))

            A_raw_rows.append(row)
            t_raw_vals.append(raw_target)
            # Only the buckets the user actually typed count towards the
            # reported error. The synthetic "other" bucket is bookkeeping —
            # when targets sum to less than 100% it is pure slack, and
            # penalising the user for missing a target they never set would
            # make the error number meaningless.
            is_explicit.append(key != OTHER_BUCKET)

    if not A_rows:
        return (np.zeros((0, n_assets)), np.zeros(0), [],
                np.zeros((0, n_assets)), np.zeros(0),
                np.zeros(0, dtype=bool), np.zeros(0, dtype=object))

    return (np.vstack(A_rows), np.array(t_vals), rows,
            np.vstack(A_raw_rows), np.array(t_raw_vals),
            np.array(is_explicit, dtype=bool),
            np.array([r[0] for r in rows]))


# ---------------------------------------------------------------------------
# Greedy selection
# ---------------------------------------------------------------------------
def _facet_devs(A_raw: np.ndarray, t_raw: np.ndarray, mask: np.ndarray,
                row_facet: np.ndarray, w: np.ndarray) -> dict:
    """Worst deviation within EACH facet, in real percentage points.

    ``{"asset_class": 0.012, "country": 0.048, ...}`` — 0.048 means some
    region is 4.8 points off its target.

    Per-facet rather than one overall number because the facets are not
    equally important and you cannot say so with a single figure: forcing
    one tolerance means setting it to whatever the loosest facet needs,
    which drags the strict ones down with it.
    """
    out: dict[str, float] = {}
    if A_raw.shape[0] == 0:
        return out
    resid = np.abs(A_raw @ w - t_raw)
    for facet in set(row_facet.tolist()):
        sel = (row_facet == facet) & mask
        if sel.any():
            out[facet] = float(resid[sel].max())
    return out


def _all_within(devs: dict, tol: dict) -> bool:
    """True when every facet is inside its own tolerance."""
    return all(dev <= tol.get(facet, 1.0) + 1e-12
               for facet, dev in devs.items())


def _greedy_select(A: np.ndarray, t: np.ndarray,
                   A_raw: np.ndarray, t_raw: np.ndarray,
                   mask: np.ndarray, row_facet: np.ndarray,
                   n_candidates: int,
                   max_funds: int,
                   tol: dict) -> tuple[list[int], dict, bool]:
    """Greedy forward selection, stopping when EVERY facet is in tolerance.

    Each round, try adding every not-yet-chosen fund, solve the small weight
    problem for that trial set, and keep whichever gives the lowest residual.

    The stop is per-facet: keep going until no facet exceeds its own
    tolerance. A single overall tolerance can't express "asset class within
    2% but sector within 10%" — you'd have to set the one number to 10% and
    lose the asset-class precision entirely.

    We still stop early in two cases, both honest failures rather than
    premature quitting:

    * ``max_funds`` reached — the user capped it.
    * No remaining fund improves the fit at all — the targets aren't
      reachable with the candidates available. That's a fact about the fund
      universe, not a solver failure, and the caller says so.

    Ranking is by the (weighted) residual, which is smooth and stable;
    stopping is on per-facet max deviation, which is what the user cares
    about. Ranking on max-deviation directly would be non-smooth and make
    the greedy path erratic.

    Returns:
        ``(selected_indices, facet_devs, all_met)`` — indices in selection
        order, so the caller can show which funds did the real work.
    """
    cash_idx = A.shape[1] - 1
    selected: list[int] = []
    remaining = set(range(n_candidates))

    # Baseline: everything in cash. Any fund must beat that.
    w0, best_sse = _solve_weights(A[:, [cash_idx]], t)
    w_full = np.zeros(A.shape[1])
    w_full[cash_idx] = w0[0]
    best_devs = _facet_devs(A_raw, t_raw, mask, row_facet, w_full)

    while len(selected) < max_funds and remaining:
        if _all_within(best_devs, tol):
            return selected, best_devs, True      # the intended exit

        best_j, best_j_sse, best_j_w = None, best_sse, None
        for j in remaining:
            cols = selected + [j] + [cash_idx]
            w, sse = _solve_weights(A[:, cols], t)
            if sse < best_j_sse - 1e-12:
                best_j, best_j_sse, best_j_w = j, sse, (cols, w)

        if best_j is None:
            # Nothing left improves the fit: unreachable with these funds.
            return selected, best_devs, _all_within(best_devs, tol)

        selected.append(best_j)
        remaining.discard(best_j)
        best_sse = best_j_sse

        cols, w = best_j_w
        w_full = np.zeros(A.shape[1])
        for pos, col in enumerate(cols):
            w_full[col] = w[pos]
        best_devs = _facet_devs(A_raw, t_raw, mask, row_facet, w_full)

    return selected, best_devs, _all_within(best_devs, tol)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def optimise_portfolio(candidates: list[dict],
                       targets: dict,
                       cash_base: float,
                       cash_exposure: dict | None = None,
                       *,
                       max_funds: int = 10,
                       min_weight: float = 0.01,
                       min_trade_base: float = 100.0,
                       max_error: dict | float = 0.05,
                       facet_weights: dict | None = None) -> dict:
    """Design (or rebalance to) a portfolio matching the exposure targets.

    Args:
        candidates: Investable funds. Each is::

            {
              "ticker":         "VWRL.AS",
              "name":           "Vanguard FTSE All-World",
              "price_base":     102.34,   # per share, in BASE currency
              "current_shares": 0.0,      # what's held right now
              "exposures": {              # look-through, fractions 0–1
                  "asset_class": {"equity": 1.0},
                  "country":     {"northAmerica": 0.62, ...},
                  ...
              },
            }

            Funds with a missing or non-positive ``price_base`` are dropped
            (we cannot size a trade without a price).

        targets: ``{facet: {bucket: fraction}}``. Sparse — a facet with no
            targets is ignored entirely. Fractions, not percents.
        cash_base: Total investable cash, in base currency.
        cash_exposure: Cash's own exposure, same shape as a candidate's.
            Defaults to 100% ``asset_class: cash``.
        max_funds: Cap on how many funds the design may use.
        min_weight: Drop any fund the solver gives less than this weight,
            then re-solve without it. Prevents 0.4% dust positions.
        min_trade_base: Suppress trades smaller than this (base currency).
            A €30 rebalancing trade is not worth making, so this defaults
            to 100 rather than 0.
        max_error: Acceptable worst-case deviation **per facet**, as
            fractions: ``{"asset_class": 0.02, "sector": 0.10,
            "country": 0.05, "currency": 0.15}``. A bare float is accepted
            and applied to every facet. The solver keeps adding funds until
            every facet is inside its own tolerance, or it runs out of
            ``max_funds`` / of candidates that help.

            The tolerances also shape the objective (see ``facet_weights``)
            — a facet you demand 2% on is weighted five times harder than
            one you allow 10% on. Without that, the solver would spread its
            effort evenly and might never satisfy the strict facet at all.
        facet_weights: Explicit per-facet importance. Defaults to
            ``1 / max_error[facet]``, i.e. residuals measured in units of
            "how much I care". Override only if you want importance to
            diverge from tolerance.
        facet_weights: Per-facet importance, default 1.0 each.

    Returns:
        ::

            {
              "ok":           bool,
              "reason":       str,          # populated when ok is False
              "total_base":   float,        # investable value (funds + cash)
              "trades":       [{"ticker", "shares_delta", "amount_base",
                                "action", "price_base",
                                "current_shares", "target_shares"}, ...],
              "positions":    [{"ticker", "name", "weight",
                                "target_shares", "amount_base"}, ...],
              "cash_weight":  float,
              "cash_after":   float,
              "selected":     [ticker, ...],   # in selection order
              "achieved":     {facet: {bucket: fraction}},
              "deviation":    {facet: {bucket: achieved - target}},
              "target_met":   bool,     # every facet inside its tolerance?
              "facets":       {facet: {"max_dev", "tolerance", "met"}},
              "max_dev":      float,    # worst deviation across all facets
            }

        ``trades`` is directly consumable by ``porxpy.trades.apply_trades``
        — that is the whole point of the shape. Sells come out as negative
        ``shares_delta``.
    """
    if cash_exposure is None:
        cash_exposure = {"asset_class": {"cash": 1.0}}

    # Drop unpriceable candidates — a fund we can't price is a fund we
    # can't trade, and silently weighting it would produce a design the
    # user cannot actually execute.
    usable = [c for c in (candidates or [])
              if float(c.get("price_base") or 0.0) > 0.0]

    if not any((targets or {}).values()):
        return {"ok": False, "reason": "no targets set",
                "trades": [], "positions": [], "selected": []}

    # Total investable = what the funds are worth now + cash. Rebalancing
    # can sell as well as buy, so held value is part of the budget.
    held_base = sum(float(c.get("current_shares") or 0.0)
                    * float(c.get("price_base") or 0.0)
                    for c in usable)
    total_base = held_base + float(cash_base or 0.0)

    if total_base <= 0:
        return {"ok": False, "reason": "nothing to invest (no cash, no holdings)",
                "trades": [], "positions": [], "selected": []}
    if not usable:
        return {"ok": False, "reason": "no priceable candidate funds",
                "trades": [], "positions": [], "selected": []}

    # Normalise the tolerance to a per-facet dict.
    if isinstance(max_error, (int, float)):
        tol = {f: float(max_error) for f in targets}
    else:
        tol = {f: float((max_error or {}).get(f, 0.05)) for f in targets}
    tol = {f: (v if v > 0 else 0.001) for f, v in tol.items()}

    # Objective weights default to 1/tolerance: a 2%-tolerance facet gets
    # five times the weight of a 10% one, so the solver actually works
    # harder where you demanded more. Normalised so the numbers stay
    # well-conditioned rather than the raw 1/0.02 = 50.
    if not facet_weights:
        inv = {f: 1.0 / v for f, v in tol.items()}
        lo = min(inv.values()) if inv else 1.0
        facet_weights = {f: v / lo for f, v in inv.items()}

    A, t, rows, A_raw, t_raw, mask, row_facet = _build_facet_matrix(
        usable, cash_exposure, targets, facet_weights)
    if A.shape[0] == 0:
        return {"ok": False, "reason": "no targets set",
                "trades": [], "positions": [], "selected": []}

    cash_idx = A.shape[1] - 1

    # 1. Choose a small fund set — adding funds until the fit is good
    #    enough, not until it stops improving.
    sel, _devs, target_met = _greedy_select(
        A, t, A_raw, t_raw, mask, row_facet, len(usable), max_funds, tol)

    # 2. Solve weights on that set (+ cash).
    cols = sel + [cash_idx]
    w, _ = _solve_weights(A[:, cols], t)

    # 3. Prune dust and re-solve, so the pruned weight is redistributed
    #    properly rather than just dropped on the floor.
    keep = [i for i, j in enumerate(sel) if w[i] >= min_weight]
    if len(keep) < len(sel):
        sel = [sel[i] for i in keep]
        cols = sel + [cash_idx]
        w, _ = _solve_weights(A[:, cols], t)

    fund_w = {sel[i]: float(w[i]) for i in range(len(sel))}
    cash_w = float(w[-1])

    # 4. Weights → target shares → trades.
    trades, positions = [], []
    for j, c in enumerate(usable):
        weight   = fund_w.get(j, 0.0)
        if weight < WEIGHT_EPS:
            weight = 0.0
        price    = float(c["price_base"])
        cur      = float(c.get("current_shares") or 0.0)
        amount   = weight * total_base
        tgt_sh   = amount / price
        delta    = tgt_sh - cur

        if weight > 0:
            positions.append({
                "ticker":        c["ticker"],
                "name":          c.get("name") or c["ticker"],
                "weight":        round(weight, 6),
                "target_shares": round(tgt_sh, 6),
                "amount_base":   round(amount, 2),
            })

        # Emit a trade only if it moves the needle. Also skips the no-op
        # case where a held fund's target equals what we already own.
        if abs(delta * price) < max(min_trade_base, 0.0) or abs(delta) < 1e-9:
            continue
        trades.append({
            "ticker":         c["ticker"],
            "name":           c.get("name") or c["ticker"],
            "action":         "buy" if delta > 0 else "sell",
            "shares_delta":   round(delta, 6),
            "price_base":     round(price, 6),
            "amount_base":    round(delta * price, 2),
            "current_shares": round(cur, 6),
            "target_shares":  round(tgt_sh, 6),
        })

    positions.sort(key=lambda p: -p["weight"])
    trades.sort(key=lambda x: -abs(x["amount_base"]))

    # 5. Diagnostics: what exposure did we actually achieve, and where are
    #    we still off? This is what makes the result trustworthy rather
    #    than a black box — the user can see the residual error per bucket.
    w_full = np.zeros(A.shape[1])
    for j, weight in fund_w.items():
        w_full[j] = weight
    w_full[cash_idx] = cash_w

    achieved: dict[str, dict[str, float]] = {}
    deviation: dict[str, dict[str, float]] = {}
    for facet, tgt in (targets or {}).items():
        if not tgt:
            continue
        achieved[facet], deviation[facet] = {}, {}
        for key in sorted(tgt.keys()):
            got = sum(w_full[j] * float(((c.get("exposures") or {})
                                         .get(facet) or {}).get(key, 0.0))
                      for j, c in enumerate(usable))
            got += cash_w * float(((cash_exposure or {}).get(facet) or {})
                                  .get(key, 0.0))
            achieved[facet][key]  = round(got, 6)
            deviation[facet][key] = round(got - float(tgt[key]), 6)

    # Errors, per facet, in real percentage points on the buckets the user
    # actually targeted — the same numbers the deviation table shows, so the
    # summary and the table cannot disagree. Recomputed AFTER the dust-prune
    # re-solve, so it reflects the design actually being proposed.
    devs       = _facet_devs(A_raw, t_raw, mask, row_facet, w_full)
    target_met = _all_within(devs, tol)

    facet_report = {
        f: {"max_dev":   round(dev, 6),
            "tolerance": round(tol.get(f, 0.05), 6),
            "met":       bool(dev <= tol.get(f, 0.05) + 1e-12)}
        for f, dev in devs.items()
    }

    if target_met:
        reason = ""
    else:
        # Name the facets that actually missed, and by how much. "Fit error
        # 7%" tells the user nothing actionable; "sector is 7.2% off a 5%
        # tolerance" tells them exactly which target to relax or which fund
        # to go find.
        missed = [f for f, r in facet_report.items() if not r["met"]]
        detail = ", ".join(
            f"{f} {facet_report[f]['max_dev']*100:.1f}% "
            f"(allowed {facet_report[f]['tolerance']*100:g}%)"
            for f in missed)
        if len(sel) >= max_funds:
            reason = (f"Could not hit every tolerance using only {max_funds} "
                      f"funds — {detail}. Try raising Max funds.")
        else:
            reason = (f"Not reachable with the funds available — {detail}. "
                      f"Adding more funds does not help: these targets need "
                      f"exposure none of your candidate funds have.")

    return {
        "ok":          True,
        "reason":      reason,
        "target_met":  target_met,
        "total_base":  round(total_base, 2),
        "trades":      trades,
        "positions":   positions,
        "cash_weight": round(cash_w, 6),
        "cash_after":  round(cash_w * total_base, 2),
        "selected":    [usable[j]["ticker"] for j in sel],
        "achieved":    achieved,
        "deviation":   deviation,
        "facets":      facet_report,   # {facet: {max_dev, tolerance, met}}
        "max_dev":     round(max(devs.values()), 6) if devs else 0.0,
    }
