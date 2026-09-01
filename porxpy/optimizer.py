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

# Precision for SCREENING solves — the hundreds of throwaway fits greedy
# and the swap search run to rank candidates against each other.
#
# Those fits are never shown to anyone; they exist to answer "is A better
# than B", and the ranking is settled long before the weights are. The
# inner loop is dominated by numpy call overhead at these tiny sizes, so
# iteration count is very nearly the whole cost.
#
# Measured against the 500-iteration exact solve on 12x9 problems: at 150
# iterations the residual sits within ~2e-6 of converged for a ~3x
# speed-up. The value was 60 for a while — ~1e-4 off, and faster still —
# and the comment here kept quoting that trade-off after the constant was
# raised (corrected in v0.86.3 by measuring rather than by reading).
#
# The chosen set is always re-solved at full precision afterwards, so
# nothing the user sees inherits the screening tolerance.
SCREEN_ITER = 150
SCREEN_TOL  = 1e-9

# How many incoming funds the swap search tries per round, ranked by how
# well they align with the current residual. Trying every unselected fund
# is |selected| x |remaining| solves per round; at ~7ms a solve that is
# seconds of wall clock for a gain the shortlist almost always contains.
SWAP_CANDIDATES = 8


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


def _project_capped_simplex(y: np.ndarray, lb: np.ndarray, ub: np.ndarray,
                            iters: int = 60) -> np.ndarray:
    """Closest point to ``y`` in ``{w : lb <= w <= ub, sum(w) == 1}``.

    The plain simplex projection assumes every weight may range over
    ``[0, 1]``. Per-fund bounds break that: a position the user has
    locked cannot go below what they already hold, and a fund excluded
    from buying cannot go above it.

    The solution has the form ``w_i = clip(y_i - lambda, lb_i, ub_i)`` for
    a single scalar ``lambda`` — the Lagrange multiplier on the sum
    constraint. ``sum(w(lambda))`` is non-increasing in lambda and
    continuous, so bisection finds the lambda giving ``sum == 1`` without
    needing a solver. 60 iterations halve the bracket 60 times, which is
    exact to floating point.

    Bracket: at ``lambda = min(y - ub)`` every weight is at its upper
    bound and the sum is at its maximum; at ``lambda = max(y - lb)`` every
    weight is at its lower bound and the sum is at its minimum. If 1 lies
    outside ``[sum(lb), sum(ub)]`` the constraints are contradictory and
    no projection exists — the caller checks feasibility first, so this
    clamps rather than raising.
    """
    lo = float(np.min(y - ub))
    hi = float(np.max(y - lb))
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if np.clip(y - mid, lb, ub).sum() > 1.0:
            lo = mid
        else:
            hi = mid
    return np.clip(y - 0.5 * (lo + hi), lb, ub)


def _solve_weights(A: np.ndarray, t: np.ndarray,
                   max_iter: int = 500,
                   tol: float = 1e-10,
                   w0: np.ndarray | None = None,
                   lb: np.ndarray | None = None,
                   ub: np.ndarray | None = None) -> tuple[np.ndarray, float]:
    """Minimise ``||A w - t||^2`` subject to ``w >= 0``, ``sum(w) == 1``.

    FISTA-accelerated projected gradient. Convex with a simple projection,
    so this converges reliably; the problems are tiny (columns = chosen
    funds + cash, rows = target buckets) so 500 iterations is generous.

    Args:
        A: ``(n_buckets, n_assets)`` exposure matrix. Column ``j`` is
            asset ``j``'s exposure across every target bucket.
        t: ``(n_buckets,)`` target exposure vector.
        w0: Optional starting point. The searches evaluate thousands of
            column sets that differ from the previous one by a single
            column, so the previous answer is nearly the next answer and
            starting from it converges in a fraction of the iterations.
            Must be a valid simplex point of the right length; it is
            projected defensively regardless.

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

    # Per-column bounds. Absent means the plain simplex, which is what
    # every unconstrained fund gets.
    bounded = lb is not None or ub is not None
    if bounded:
        _lb = np.zeros(n) if lb is None else np.asarray(lb, dtype=float)
        _ub = np.ones(n)  if ub is None else np.asarray(ub, dtype=float)
        _project = lambda v: _project_capped_simplex(v, _lb, _ub)
    else:
        _project = _project_simplex

    if w0 is not None and w0.shape[0] == n:
        w = _project(np.asarray(w0, dtype=float))
    else:
        w = _project(np.full(n, 1.0 / n)) if bounded else np.full(n, 1.0 / n)
    y = w.copy()
    t_k = 1.0
    prev = w.copy()

    for _ in range(max_iter):
        grad = AtA @ y - Atb
        w_new = _project(y - step * grad)

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
        ``(n_buckets,)``, and ``rows`` is ``[(facet, bucket, level), ...]``
        labelling each row for the caller's diagnostics. Assets are ordered
        as ``candidates`` then cash (cash is always the last column).
    """
    facet_weights = facet_weights or {}
    n_assets = len(candidates) + 1          # +1 for cash

    A_rows: list[np.ndarray] = []
    t_vals: list[float] = []
    rows: list[tuple[str, str, str]] = []
    # Unscaled twins. The scaled matrix is what the solver optimises; these
    # are what we *measure* with, because an error the user is asked to set a
    # threshold on has to be in units they recognise — percentage points of
    # exposure, not the solver's internal scaling.
    A_raw_rows: list[np.ndarray] = []
    t_raw_vals: list[float] = []
    is_explicit: list[bool] = []

    # v0.65.0: targets are {facet: {level: {key: fraction}}}.
    #
    # Each target is a constraint AT ITS OWN LEVEL, and every fund's
    # exposure is measured at every level independently — so a target
    # set mixing grains needs no ordering rule. Targeting semiconductors
    # 15%, software 10% and technology 35% gives three rows; the
    # semiconductor funds satisfy their own row and contribute to the
    # technology row too, leaving 10% of technology to be filled by any
    # technology fund. That is the behaviour without a "children first,
    # then the remainder" pass, because the algebra already says it.
    #
    # Levels are NOT re-expressed at a common grain. Rolling a sector
    # target down to sub-sectors would invent detail the user did not
    # give, which is the rule this whole refactor is built on.
    def _add_target_rows(facet: str, level: str, tgt: dict) -> None:
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
                exp_f = (((c.get("exposures") or {}).get(facet) or {})
                         .get(level) or {})
                if key == OTHER_BUCKET:
                    row[j] = max(0.0, 1.0 - sum(float(exp_f.get(k, 0.0))
                                                for k in keys))
                else:
                    row[j] = float(exp_f.get(key, 0.0))

            cash_f = (((cash_exposure or {}).get(facet) or {})
                      .get(level) or {})
            if key == OTHER_BUCKET:
                row[-1] = max(0.0, 1.0 - sum(float(cash_f.get(k, 0.0))
                                             for k in keys))
            else:
                row[-1] = float(cash_f.get(key, 0.0))

            raw_target = (other_target if key == OTHER_BUCKET
                          else float(tgt[key]))
            A_rows.append(row * scale)
            t_vals.append(raw_target * scale)
            rows.append((facet, key, level))

            A_raw_rows.append(row)
            t_raw_vals.append(raw_target)
            # Only the buckets the user actually typed count towards the
            # reported error. The synthetic "other" bucket is bookkeeping —
            # when targets sum to less than 100% it is pure slack, and
            # penalising the user for missing a target they never set would
            # make the error number meaningless.
            is_explicit.append(key != OTHER_BUCKET)

    for facet, per_level in (targets or {}).items():
        for level, tgt in (per_level or {}).items():
            if tgt:
                _add_target_rows(facet, level, tgt)

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
                   tol: dict,
                   lb: np.ndarray | None = None,
                   ub: np.ndarray | None = None,
                   forced: list[int] | None = None) -> tuple[list[int], dict, bool]:
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
    # Locked positions are in the design by right: a lower bound only
    # binds if its column is present.
    selected: list[int] = list(forced or [])
    remaining = set(range(n_candidates)) - set(selected)

    def _bnd(cols):
        """Bounds for a column subset, or (None, None) when unbounded."""
        if lb is None and ub is None:
            return None, None
        return (None if lb is None else lb[cols],
                None if ub is None else ub[cols])

    # Baseline: everything in cash. Any fund must beat that.
    #
    # Two residuals are tracked deliberately. ``best_sse`` is measured at
    # SCREENING precision because every challenger is, and a comparison
    # between precisions is not a comparison at all: a screening fit sits
    # ~4e-5 above its converged value, so a genuine improvement smaller
    # than that would read as "no fund helps" and stop the search early.
    # The exact solve is kept only for the deviations, which drive the
    # stopping test and must be honest.
    _base_cols = selected + [cash_idx]
    _l, _u = _bnd(_base_cols)
    w0, _ = _solve_weights(A[:, _base_cols], t, lb=_l, ub=_u)
    w_full = np.zeros(A.shape[1])
    for _pos, _col in enumerate(_base_cols):
        w_full[_col] = w0[_pos]
    cur_w = w_full
    best_devs = _facet_devs(A_raw, t_raw, mask, row_facet, w_full)
    _, best_sse = _solve_weights(A[:, _base_cols], t, lb=_l, ub=_u,
                                 max_iter=SCREEN_ITER, tol=SCREEN_TOL)

    while len(selected) < max_funds and remaining:
        if _all_within(best_devs, tol):
            return selected, best_devs, True      # the intended exit

        best_j, best_j_sse, best_j_w = None, best_sse, None
        # Warm start: incumbent weights, with the trial column at zero.
        # cols is [*selected, j, cash], so the new column sits second-last.
        warm = np.zeros(len(selected) + 2)
        for pos, col in enumerate(selected):
            warm[pos] = cur_w[col]
        warm[-1] = cur_w[cash_idx]
        for j in remaining:
            cols = selected + [j] + [cash_idx]
            _tl, _tu = _bnd(cols)
            w, sse = _solve_weights(A[:, cols], t, lb=_tl, ub=_tu,
                                    max_iter=SCREEN_ITER, tol=SCREEN_TOL,
                                    w0=warm)
            if sse < best_j_sse - 1e-12:
                best_j, best_j_sse, best_j_w = j, sse, (cols, w)

        if best_j is None:
            # Nothing left improves the fit: unreachable with these funds.
            return selected, best_devs, _all_within(best_devs, tol)

        selected.append(best_j)
        remaining.discard(best_j)

        # Re-solve the winner exactly. The screening fit ranked it; the
        # deviations reported from it drive the stopping test, and that
        # test must not fire on a number that is 4e-5 out.
        cols, _w_screen = best_j_w
        best_sse = best_j_sse            # screening scale, as above
        _kl, _ku = _bnd(cols)
        w, _ = _solve_weights(A[:, cols], t, lb=_kl, ub=_ku)
        w_full = np.zeros(A.shape[1])
        for pos, col in enumerate(cols):
            w_full[col] = w[pos]
        cur_w = w_full
        best_devs = _facet_devs(A_raw, t_raw, mask, row_facet, w_full)

    return selected, best_devs, _all_within(best_devs, tol)


def _swap_refine(A: np.ndarray, t: np.ndarray,
                 A_raw: np.ndarray, t_raw: np.ndarray,
                 mask: np.ndarray, row_facet: np.ndarray,
                 selected: list[int], n_candidates: int,
                 tol: dict,
                 lb: np.ndarray | None = None,
                 ub: np.ndarray | None = None,
                 forced: list[int] | None = None,
                 max_rounds: int = 4) -> tuple[list[int], dict, bool, int]:
    """Improve a greedy selection by exchanging chosen funds for unchosen.

    Greedy forward selection is myopic: a fund is chosen because it was
    the best single addition at the time, and it can never be un-chosen,
    even once later picks make it redundant. The classic case is a broad
    fund taken in round one and then made worthless by the narrow funds
    that follow:

        targets 50% NA / 30% Europe / 20% Japan, max_funds=3
        greedy  -> WORLD + JAPAN + EURO      = 2.19pp off
        optimal -> SP500 + JAPAN + EURO      = 0.00pp off

    Greedy spent its first slot on WORLD, which the optimal three-fund
    answer does not use at all.

    So after greedy converges, try every (selected, unselected) exchange
    and keep any that lowers the residual. Repeat until a full round finds
    no improvement. That is local search: it cannot guarantee the global
    optimum, but it removes exactly the failure above, and it is cheap —
    |selected| x |remaining| tiny solves per round, milliseconds at these
    sizes. Deterministic, so the same inputs still give the same portfolio.

    Ranked on residual, like greedy, for the same reason: it is smooth,
    whereas max-deviation is not and makes the search path erratic. The
    best swap in a round is applied, not the first found, so the result
    does not depend on iteration order.

    Args:
        selected: Column indices chosen by :func:`_greedy_select`.
        n_candidates: How many columns are selectable (frozen funds and
            cash sit beyond this and must never be swapped in).
        max_rounds: Safety bound. Each accepted round strictly lowers the
            residual so cycling is impossible, but a bound keeps a
            pathological case from running long.

    Returns:
        ``(selected, facet_devs, all_met, n_swaps)``.
    """
    cash_idx = A.shape[1] - 1
    selected = list(selected)
    # A locked position cannot be swapped out — that is what locking it
    # means — so its slot is not a candidate for exchange.
    forced_set = set(forced or [])

    def _fit(cols: list[int], *, exact: bool = True,
             warm: np.ndarray | None = None) -> tuple[float, np.ndarray]:
        kw = {} if exact else {"max_iter": SCREEN_ITER, "tol": SCREEN_TOL}
        full = cols + [cash_idx]
        _l = None if lb is None else lb[full]
        _u = None if ub is None else ub[full]
        w, sse = _solve_weights(A[:, full], t, w0=warm, lb=_l, ub=_u, **kw)
        w_full = np.zeros(A.shape[1])
        for pos, col in enumerate(cols + [cash_idx]):
            w_full[col] = w[pos]
        return sse, w_full

    # Incumbent scored at screening precision so challengers compare
    # like with like; see the note in _greedy_select.
    best_sse, _ = _fit(selected, exact=False)
    _, best_w = _fit(selected)
    n_swaps = 0

    for _ in range(max_rounds):
        remaining = [j for j in range(n_candidates) if j not in selected]
        if not remaining or not selected:
            break

        # Shortlist the incoming candidates instead of trying all of them.
        #
        # A fund can only improve the fit by supplying exposure the
        # portfolio is currently short of, so score each unselected fund
        # by how strongly its column aligns with the current residual
        # (r = A w - t) and try only the best few. This is the screening
        # step from matching pursuit, and it is the difference between
        # |selected| x |remaining| solves per round and |selected| x K.
        #
        # It is a heuristic: a fund outside the shortlist could in
        # principle have made a good swap. But the alignment score is
        # exactly the first-order estimate of how much a column can
        # reduce the residual, so the ones it discards are the ones with
        # least to offer — and the alternative, at ~7ms per solve, is an
        # optimiser that takes ten seconds to answer.
        resid = A @ best_w - t
        align = np.abs(A[:, remaining].T @ resid)
        shortlist = [remaining[i] for i in np.argsort(align)[::-1][:SWAP_CANDIDATES]]

        best_move = None                      # (sse, position, incoming)
        for pos in range(len(selected)):
            if selected[pos] in forced_set:
                # Locked: the user said not to sell it. Exchanging it is
                # exactly that, however much it would improve the fit.
                continue
            for j in shortlist:
                trial = list(selected)
                trial[pos] = j
                warm = np.array([best_w[c] for c in trial] + [best_w[cash_idx]])
                sse, _w = _fit(trial, exact=False, warm=warm)
                if sse < best_sse - 1e-12 and (best_move is None
                                               or sse < best_move[0]):
                    best_move = (sse, pos, j)

        if best_move is None:
            break                             # local optimum reached

        best_sse, pos, j = best_move
        selected[pos] = j
        _, best_w = _fit(selected)
        n_swaps += 1

    devs = _facet_devs(A_raw, t_raw, mask, row_facet, best_w)
    return selected, devs, _all_within(devs, tol), n_swaps


def _portfolio_score(w_full: np.ndarray, cols: list[int],
                     tickers: list[str], scores: dict) -> float | None:
    """Weight-averaged peer score of a candidate selection.

    Weighted, not a plain average: a 2% position in a mediocre fund
    matters about a fiftieth as much as a 40% one, and an unweighted mean
    would let a dust position veto a good design.

    Funds with no peer score are excluded from BOTH sides of the average,
    so an unscoreable fund neither helps nor hurts. Returns None when
    nothing in the selection can be scored.
    """
    num = den = 0.0
    for c in cols:
        if c >= len(tickers):
            continue
        blk = scores.get(tickers[c].upper()) or {}
        sc = blk.get("score_peer")
        w = float(w_full[c])
        if sc is None or w <= 0:
            continue
        num += w * sc
        den += w
    return (num / den) if den > 0 else None


def _score_alternatives(A: np.ndarray, t: np.ndarray,
                        A_raw: np.ndarray, t_raw: np.ndarray,
                        mask: np.ndarray, row_facet: np.ndarray,
                        selected: list[int], n_candidates: int,
                        tol: dict, tickers: list[str], names: list[str],
                        scores: dict, peer_of: list[str],
                        lb: np.ndarray | None = None,
                        ub: np.ndarray | None = None,
                        forced: list[int] | None = None,
                        top_n: int = 3) -> list[dict]:
    """Price the better-scoring alternatives to each chosen fund.

    For every fund in the design, find the higher-scoring funds in its
    peer group and work out what substituting each would actually cost:
    swap it in, re-solve the weights, measure the new deviation.

    Nothing is applied. The point is to put the trade in front of the
    user — "this fund scores 12, its peer scores 95, taking it costs you
    1.2pp of country accuracy" — and let them decide. An optimiser that
    spent an error budget automatically would be guessing at how much
    accuracy the user is willing to trade, which varies per portfolio and
    is exactly the judgement they are best placed to make.

    Alternatives that break a tolerance are included and flagged, not
    hidden: the user asked for the choice, and a substitution costing
    2.5pp against a 2pp tolerance may still be the one they want.

    Peer group, not the whole universe: the optimiser holds a European
    bond fund because the targets demand one, and offering to replace it
    with a high-scoring US equity tracker would be answering a question
    nobody asked.

    Each row is priced against the SAME baseline, independently. That is
    what makes the numbers comparable — but it also means accepting two
    rows does not cost the sum of their two prices, since deviations do
    not add linearly. The caller must recompute the combined result
    before applying, which is what ``substitutions`` is for.

    Returns:
        ``[{"ticker", "score", "peer_key", "alternatives": [...]}, ...]``,
        one entry per selected fund, ordered as selected. A fund already
        best in its group yields an empty ``alternatives`` list rather
        than being omitted, so the table can say so.
    """
    cash_idx = A.shape[1] - 1
    forced_set = set(forced or [])

    def _fit(cols):
        full = cols + [cash_idx]
        _l = None if lb is None else lb[full]
        _u = None if ub is None else ub[full]
        w, sse = _solve_weights(A[:, full], t, lb=_l, ub=_u)
        w_full = np.zeros(A.shape[1])
        for pos, col in enumerate(cols + [cash_idx]):
            w_full[col] = w[pos]
        return w_full

    base_devs = _facet_devs(A_raw, t_raw, mask, row_facet, _fit(selected))
    base_worst = max(base_devs.values()) if base_devs else 0.0

    def _nm(j):
        return (names[j] if j < len(names) and names[j] else tickers[j])

    def _score_of(j):
        return (scores.get(tickers[j].upper()) or {}).get("score_peer")

    out: list[dict] = []
    for pos, j in enumerate(selected):
        if j in forced_set:
            # Locked: the user has said not to sell it, so offering a
            # replacement would be proposing exactly that.
            out.append({"ticker": tickers[j], "name": _nm(j),
                        "score": (round(float(_score_of(j)), 2)
                                  if _score_of(j) is not None else None),
                        "peer_key": peer_of[j] if j < len(peer_of) else "",
                        "locked": True, "alternatives": []})
            continue
        my_score = _score_of(j)
        my_peer  = peer_of[j] if j < len(peer_of) else ""
        alts: list[dict] = []

        # Same peer group, better score, not already in the design.
        cands = [k for k in range(n_candidates)
                 if k != j and k not in selected
                 and (peer_of[k] if k < len(peer_of) else "") == my_peer
                 and _score_of(k) is not None
                 and (my_score is None or _score_of(k) > my_score)]
        cands.sort(key=lambda k: -_score_of(k))

        for k in cands[:max(0, top_n)]:
            trial = list(selected)
            trial[pos] = k
            devs = _facet_devs(A_raw, t_raw, mask, row_facet, _fit(trial))
            worst = max(devs.values()) if devs else 0.0
            # Per-facet before/after/delta, not just the worst figure.
            # A substitution that costs 1.4pp overall may be spending all
            # of it on a facet you barely care about, or all of it on the
            # one you set the tightest tolerance on — and the headline
            # number cannot tell those apart.
            per_facet = {}
            for f in set(list(devs.keys()) + list(base_devs.keys())):
                b = base_devs.get(f, 0.0)
                a = devs.get(f, 0.0)
                per_facet[f] = {
                    "before": round(b, 6),
                    "after":  round(a, 6),
                    "delta":  round(a - b, 6),
                    "tolerance": round(float(tol.get(f, 0.0)), 6),
                    "within":    a <= float(tol.get(f, float("inf"))) + 1e-9,
                }
            alts.append({
                "ticker":       tickers[k],
                "name":         _nm(k),
                "score":        round(float(_score_of(k)), 2),
                "score_delta":  (round(float(_score_of(k)) - float(my_score), 2)
                                 if my_score is not None else None),
                "dev_before":   round(base_worst, 6),
                "dev_after":    round(worst, 6),
                "dev_delta":    round(worst - base_worst, 6),
                "facets":       per_facet,
                "within_tol":   _all_within(devs, tol),
            })

        out.append({
            "ticker":       tickers[j],
            "name":         _nm(j),
            "score":        round(float(my_score), 2) if my_score is not None else None,
            "peer_key":     my_peer,
            "alternatives": alts,
        })
    return out


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
                       facet_weights: dict | None = None,
                       scores: dict | None = None,
                       substitutions: dict | None = None,
                       alternatives_top_n: int = 3) -> dict:
    """Design (or rebalance to) a portfolio matching the exposure targets.

    Args:
        candidates: Investable funds. Each is::

            {
              "ticker":         "VWRL.AS",
              "name":           "Vanguard FTSE All-World",
              "price_base":     102.34,   # per share, in BASE currency
              "current_shares": 0.0,      # what's held right now
              "include":        True,     # may the optimiser trade it?
              # look-through, fractions 0–1, per (facet, LEVEL, key)
              "exposures": {
                  "asset_class": {"asset_class": {"equity": 1.0}},
                  "country":     {"country": {"unitedstates": 0.62, ...},
                                  "region":  {"northAmerica": 0.62, ...},
                                  "super_region": {"developed": 0.9, ...}},
                  ...
              },
            }

            Funds with a missing or non-positive ``price_base`` are dropped
            (we cannot size a trade without a price).

            ``include`` defaults to True. A held fund with ``include``
            False is *frozen*: its exposure still counts toward every
            target and toward the portfolio denominator, but it is never
            bought, sold or selected, and its value is not part of the
            budget. An unheld fund with ``include`` False is simply not
            a candidate. Freezing constrains what is reachable — that is
            the point of it — so ``reason`` says so when a target is
            missed and frozen holdings are in play.

        targets: ``{facet: {level: {bucket: fraction}}}``. Sparse — a
            facet or level with no targets is ignored entirely.
            Fractions, not percents.
        cash_base: Total investable cash, in base currency.
        cash_exposure: Cash's own exposure, same shape as a candidate's
            — including the level dimension.
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
        scores: ``{ticker: {"score_peer": 0-100 or None}}`` from
            :func:`porxpy.scoring.score_universe`. When supplied, the
            result carries a ``alternatives`` block listing, per chosen
            fund, the better-scoring funds in its peer group and what
            substituting each would cost in deviation. Nothing is applied
            automatically — see ``substitutions``.
        substitutions: ``{held_ticker: replacement_ticker}`` to force into
            the design after selection. This is how the caller applies
            the alternatives the user accepted.
        alternatives_top_n: How many alternatives to offer per fund.

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
              "cash_weight":  float,        # of the WHOLE portfolio
              "cash_after":   float,
              "frozen":       {"share", "base", "tickers"},
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
    priceable = [c for c in (candidates or [])
                 if float(c.get("price_base") or 0.0) > 0.0]

    if not any((targets or {}).values()):
        return {"ok": False, "reason": "no targets set",
                "trades": [], "positions": [], "selected": []}

    # ---- Frozen positions (v0.30.0) -------------------------------------
    # A fund with ``include`` False is excluded from the optimiser's
    # *decisions*, not from the portfolio. That distinction is the whole
    # point of the flag, and getting it wrong in either direction gives a
    # wrong answer:
    #
    #   * Dropping it entirely would optimise a portfolio the user does
    #     not have — the exposure is real and still dilutes every target.
    #   * Treating it as tradeable would let the optimiser sell the very
    #     position the user marked as not-to-be-touched.
    #
    # So it contributes a fixed baseline to the achieved exposure, its
    # value is removed from the tradeable budget, and it is never bought,
    # sold or selected. A held position with no shares is not frozen in
    # any meaningful sense — there is nothing to freeze — so it is simply
    # dropped rather than carried as a zero-weight constraint.
    # Two independent flags, four states:
    #
    #   include  locked   the optimiser may
    #   -------  ------   -------------------------------------------
    #   True     False    buy and sell freely
    #   False    False    sell, but never buy       (upper bound)
    #   True     True     buy more, but never sell  (lower bound)
    #   False    True     neither — fully frozen
    #
    # `include` lives on the fund and applies everywhere: "never put this
    # in a buy suggestion, in any portfolio". `locked` lives on the
    # position and applies here only: "do not suggest selling what I hold
    # in THIS portfolio".
    #
    # The middle two are not exclusions but BOUNDS, which is why the
    # solver needed a capped-simplex projection. An unheld fund that
    # cannot be bought is dropped outright — there is nothing to sell and
    # nothing may be added.
    usable, frozen = [], []
    for c in priceable:
        buyable  = bool(c.get("include", True))
        sellable = not bool(c.get("locked", False))
        held     = float(c.get("current_shares") or 0.0) > 0.0
        if buyable and sellable:
            usable.append(c)
        elif not held:
            if buyable:
                usable.append(c)          # locked but unheld: nothing to lock
            # unheld and unbuyable: not a candidate at all
        elif not buyable and not sellable:
            frozen.append(c)              # fully frozen, fixed baseline
        else:
            usable.append(c)              # bounded on one side

    # Total investable = every fund's current worth + cash. Rebalancing
    # can sell as well as buy, so held value is part of the budget —
    # except for the frozen part, which is held value we may not touch.
    held_base = sum(float(c.get("current_shares") or 0.0)
                    * float(c.get("price_base") or 0.0)
                    for c in usable)
    frozen_base = sum(float(c.get("current_shares") or 0.0)
                      * float(c.get("price_base") or 0.0)
                      for c in frozen)
    total_base = held_base + frozen_base + float(cash_base or 0.0)
    free_base  = held_base + float(cash_base or 0.0)

    if total_base <= 0:
        return {"ok": False, "reason": "nothing to invest (no cash, no holdings)",
                "trades": [], "positions": [], "selected": []}
    if not usable:
        return {"ok": False,
                "reason": ("no priceable candidate funds" if not frozen else
                           "every priceable fund is excluded from the "
                           "optimiser (incl. unchecked)"),
                "trades": [], "positions": [], "selected": []}
    if free_base <= 0:
        return {"ok": False,
                "reason": "nothing tradeable — all value sits in excluded funds",
                "trades": [], "positions": [], "selected": []}

    frozen_share = frozen_base / total_base if total_base > 0 else 0.0

    # ---- Per-column bounds ----------------------------------------------
    # Expressed as fractions of the FREE budget, because that is the space
    # the solver works in. Cash is the last column and is never bounded.
    n_cols = len(usable) + len(frozen) + 1
    lb_full = np.zeros(n_cols)
    ub_full = np.ones(n_cols)
    forced: list[int] = []
    for j, c in enumerate(usable):
        held_w = ((float(c.get("current_shares") or 0.0)
                   * float(c.get("price_base") or 0.0)) / free_base
                  if free_base > 0 else 0.0)
        if held_w <= 0:
            continue
        if not bool(c.get("include", True)):
            ub_full[j] = min(1.0, held_w)     # may shrink, never grow
        if bool(c.get("locked", False)):
            lb_full[j] = min(1.0, held_w)     # may grow, never shrink
            # A lower bound only binds if the column is in the solve, so a
            # locked holding is part of the design by right rather than by
            # selection.
            forced.append(j)

    has_bounds = bool(forced) or bool((ub_full[:len(usable)] < 1.0).any())

    # Locks can contradict each other, or contradict a cash target. Caught
    # here rather than returning a silently wrong answer: the projection
    # would clamp and produce weights that satisfy nothing.
    if lb_full.sum() > 1.0 + 1e-9:
        locked_tks = [usable[j]["ticker"] for j in forced]
        return {"ok": False,
                "reason": (f"locked positions already account for "
                           f"{lb_full.sum()*100:.0f}% of the tradeable budget, "
                           f"so no design is possible. Unlock one of: "
                           f"{', '.join(locked_tks)}."),
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

    # Frozen funds get columns too, so their exposure is expressed in
    # exactly the same row space and scaling as everything else. They are
    # simply never selectable: `_greedy_select` is told there are only
    # `len(usable)` candidates, and `usable` comes first in the column
    # order.
    A, t, rows, A_raw, t_raw, mask, row_facet = _build_facet_matrix(
        usable + frozen, cash_exposure, targets, facet_weights)
    if A.shape[0] == 0:
        return {"ok": False, "reason": "no targets set",
                "trades": [], "positions": [], "selected": []}

    cash_idx = A.shape[1] - 1

    # ---- Reduce to the free sub-problem ---------------------------------
    # The frozen funds contribute a fixed vector f of whole-portfolio
    # exposure. The free part carries the rest, and its own weights sum to
    # 1 within itself, so:
    #
    #     f + (1 - phi) * (A_free @ w) = t        with phi = frozen share
    #     =>          A_free @ w = (t - f) / (1 - phi)
    #
    # Components of the reduced target can go negative — that happens when
    # the frozen holdings already overshoot a bucket. Least squares on the
    # simplex handles it correctly: it reads as "put as little here as
    # you can", which is exactly right, since the overshoot cannot be
    # sold off.
    phi  = frozen_share
    free = max(1.0 - phi, 1e-9)
    if frozen:
        fz_w = np.zeros(A.shape[1])
        for k, c in enumerate(frozen):
            col = len(usable) + k
            fz_w[col] = (float(c.get("current_shares") or 0.0)
                         * float(c.get("price_base") or 0.0)) / total_base
        f_scaled = A     @ fz_w
        f_raw    = A_raw @ fz_w
        t_free     = (t     - f_scaled) / free
        t_raw_free = (t_raw - f_raw)    / free
        # The user's tolerance is in whole-portfolio points, but the
        # solver now measures residuals inside the free sub-portfolio,
        # where the same money is a larger fraction. Divide the tolerance
        # by the same factor so the stopping test means what it meant
        # before.
        tol_free = {fct: v / free for fct, v in tol.items()}
    else:
        f_scaled = np.zeros(A.shape[0])
        f_raw    = np.zeros(A_raw.shape[0]) if A_raw.shape[0] else np.zeros(0)
        t_free, t_raw_free, tol_free = t, t_raw, tol

    # 1. Choose a small fund set — adding funds until the fit is good
    #    enough, not until it stops improving.
    _lb = lb_full if has_bounds else None
    _ub = ub_full if has_bounds else None
    sel, _devs, target_met = _greedy_select(
        A, t_free, A_raw, t_raw_free, mask, row_facet,
        len(usable), max_funds, tol_free, lb=_lb, ub=_ub, forced=forced)

    # 1b. Local search over exchanges, undoing greedy's myopia.
    #
    #     Runs unconditionally, including when greedy already met every
    #     tolerance. It used to be skipped in that case, on the reasoning
    #     that the user asked for "good enough" rather than "optimal".
    #     That reasoning stopped holding once the alternatives table
    #     existed.
    #
    #     Greedy stops at the FIRST design inside tolerance, so a
    #     successful run lands just under the line — 1.8pp against a 2pp
    #     budget. The swap pass exchanges funds within the existing set
    #     size, adding none, so it can often take that to 0.4pp for free.
    #     Two things follow from skipping it: the design shown is worse
    #     than it needs to be at no saving, and every substitution in the
    #     quality table is priced against an unnecessarily loose baseline,
    #     so more of them read "exceeds tolerance" than truly do.
    #
    #     It costs a few hundred milliseconds and can only lower the
    #     deviation, never raise it — an exchange is accepted only when
    #     the residual falls.
    sel, _devs, target_met, n_swaps = _swap_refine(
        A, t_free, A_raw, t_raw_free, mask, row_facet,
        sel, len(usable), tol_free, lb=_lb, ub=_ub, forced=forced)

    # 1c. Caller-requested substitutions.
    #
    #     Applied here, after the fit passes have chosen a set and before
    #     the weights are finalised, so a substituted design is solved
    #     exactly like any other rather than being patched afterwards.
    #     This is how the "best in class" table applies the user's picks:
    #     it sends the substitutions it wants and gets back a real,
    #     fully-recomputed result — which matters because deviations do
    #     not add up linearly, so a set of individually-priced swaps can
    #     cost more or less together than the sum of their parts.
    applied_subs: list[dict] = []
    if substitutions:
        by_tk = {(c.get("ticker") or "").upper(): j for j, c in enumerate(usable)}
        for out_tk, in_tk in substitutions.items():
            oj = by_tk.get((out_tk or "").upper())
            ij = by_tk.get((in_tk or "").upper())
            if oj is None or ij is None or ij in sel:
                continue
            if oj in sel:
                sel[sel.index(oj)] = ij
                applied_subs.append({"out": out_tk.upper(), "in": in_tk.upper()})

    # 2. Solve weights on that set (+ cash).
    cols = sel + [cash_idx]
    w, _ = _solve_weights(A[:, cols], t_free,
                          lb=None if _lb is None else _lb[cols],
                          ub=None if _ub is None else _ub[cols])

    # 3. Prune dust and re-solve, so the pruned weight is redistributed
    #    properly rather than just dropped on the floor.
    # Never prune a locked position: its weight is the user's instruction,
    # not the solver's choice, and dropping it would silently sell it.
    keep = [i for i, j in enumerate(sel)
            if w[i] >= min_weight or j in set(forced)]
    if len(keep) < len(sel):
        sel = [sel[i] for i in keep]
        cols = sel + [cash_idx]
        w, _ = _solve_weights(A[:, cols], t_free,
                              lb=None if _lb is None else _lb[cols],
                              ub=None if _ub is None else _ub[cols])

    # These are weights *within the free sub-portfolio*, so they sum to 1
    # across the selected funds plus cash — not across the whole
    # portfolio, which also contains the frozen part.
    fund_w = {sel[i]: float(w[i]) for i in range(len(sel))}
    cash_w = float(w[-1])

    # 4. Weights → target shares → trades. Money is sized against the free
    #    budget; the reported weight is of the whole portfolio, because
    #    that is the number the user compares against a target.
    trades, positions = [], []
    for j, c in enumerate(usable):
        weight   = fund_w.get(j, 0.0)
        if weight < WEIGHT_EPS:
            weight = 0.0
        price    = float(c["price_base"])
        cur      = float(c.get("current_shares") or 0.0)
        amount   = weight * free_base
        tgt_sh   = amount / price
        delta    = tgt_sh - cur

        _sc = (scores or {}).get((c["ticker"] or "").upper()) or {}
        if weight > 0:
            positions.append({
                "ticker":        c["ticker"],
                "name":          c.get("name") or c["ticker"],
                "score_all":     _sc.get("score_all"),
                "score_peer":    _sc.get("score_peer"),
                "weight":        round(weight * free, 6),
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
            # Carried on the trade so the suggestion list can show what
            # is being bought or sold in quality terms, not just size.
            "score_all":      _sc.get("score_all"),
            "score_peer":     _sc.get("score_peer"),
            "action":         "buy" if delta > 0 else "sell",
            "shares_delta":   round(delta, 6),
            "price_base":     round(price, 6),
            "amount_base":    round(delta * price, 2),
            "current_shares": round(cur, 6),
            "target_shares":  round(tgt_sh, 6),
        })

    # Frozen holdings are part of the proposed portfolio — they are just
    # not part of the proposal. Listing them keeps the position table
    # reconciling to 100% instead of quietly summing to (1 - phi), and
    # the flag lets the UI grey them out.
    for c in frozen:
        amt = (float(c.get("current_shares") or 0.0)
               * float(c.get("price_base") or 0.0))
        positions.append({
            "ticker":        c["ticker"],
            "name":          c.get("name") or c["ticker"],
            "weight":        round(amt / total_base, 6) if total_base else 0.0,
            "target_shares": round(float(c.get("current_shares") or 0.0), 6),
            "amount_base":   round(amt, 2),
            "frozen":        True,
        })

    positions.sort(key=lambda p: -p["weight"])
    trades.sort(key=lambda x: -abs(x["amount_base"]))

    # Price the better-scoring peers of every chosen fund. Reported, never
    # applied: the user decides whether the accuracy is worth the quality.
    alternatives: list[dict] = []
    if scores:
        try:
            from porxpy.scoring import peer_key as _peer_key
            peer_of = [_peer_key(c) for c in usable]
        except Exception:
            peer_of = ["" for _ in usable]
        alternatives = _score_alternatives(
            A, t_free, A_raw, t_raw_free, mask, row_facet,
            sel, len(usable), tol_free,
            [c.get("ticker") or "" for c in usable],
            [c.get("name") or "" for c in usable], scores, peer_of,
            lb=_lb, ub=_ub, forced=forced, top_n=alternatives_top_n)

    # 5. Diagnostics: what exposure did we actually achieve, and where are
    #    we still off? This is what makes the result trustworthy rather
    #    than a black box — the user can see the residual error per bucket.
    # Whole-portfolio weights: the free design scaled back down by
    # (1 - phi), plus the frozen part at its actual size. Everything from
    # here on is reported against the portfolio the user will actually
    # hold, not against the slice the solver was allowed to move.
    w_full = np.zeros(A.shape[1])
    for j, weight in fund_w.items():
        w_full[j] = weight * free
    w_full[cash_idx] = cash_w * free
    for k, c in enumerate(frozen):
        w_full[len(usable) + k] = (
            (float(c.get("current_shares") or 0.0)
             * float(c.get("price_base") or 0.0)) / total_base
            if total_base else 0.0)

    # v0.65.0: {facet: {level: {key: ...}}}, mirroring the targets, and
    # each bucket measured against the exposure AT ITS OWN LEVEL. Reading
    # exposures[facet][key] here would have silently found nothing and
    # reported every achieved exposure as 0.0 — a wrong number rather
    # than an error, which is the worse failure.
    achieved: dict[str, dict[str, dict[str, float]]] = {}
    deviation: dict[str, dict[str, dict[str, float]]] = {}
    for facet, per_level in (targets or {}).items():
        for level, tgt in (per_level or {}).items():
            if not tgt:
                continue
            achieved.setdefault(facet, {})[level] = {}
            deviation.setdefault(facet, {})[level] = {}
            for key in sorted(tgt.keys()):
                got = sum(w_full[j] * float((((c.get("exposures") or {})
                                              .get(facet) or {})
                                             .get(level) or {}).get(key, 0.0))
                          for j, c in enumerate(usable + frozen))
                got += w_full[cash_idx] * float(
                    (((cash_exposure or {}).get(facet) or {})
                     .get(level) or {}).get(key, 0.0))
                # float() because `got` is a numpy scalar here, and the
                # Flask JSON encoder does not know what to do with one.
                achieved[facet][level][key]  = round(float(got), 6)
                deviation[facet][level][key] = round(
                    float(got) - float(tgt[key]), 6)

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
        # Say so when the user's own exclusions are part of the reason.
        # Otherwise "not reachable" reads as an optimiser failure, when in
        # fact a chunk of the portfolio was placed off-limits by hand and
        # the remaining free money cannot compensate for it.
        if frozen:
            reason += (f" Note that {phi*100:.0f}% of the portfolio is held "
                       f"in {len(frozen)} fund(s) excluded from the optimiser "
                       f"(incl. unchecked), whose exposure counts toward "
                       f"these targets but cannot be traded.")

    return {
        "ok":          True,
        "reason":      reason,
        "target_met":  target_met,
        "total_base":  round(total_base, 2),
        "trades":      trades,
        "positions":   positions,
        "cash_weight": round(cash_w * free, 6),
        "cash_after":  round(cash_w * free_base, 2),
        "selected":    [usable[j]["ticker"] for j in sel],
        "achieved":    achieved,
        "deviation":   deviation,
        "facets":      facet_report,   # {facet: {max_dev, tolerance, met}}
        "swaps":         n_swaps,      # exchanges the fit refinement applied
        "alternatives":  alternatives, # per chosen fund, better-scoring peers
        "substitutions": applied_subs, # substitutions the caller requested
        "frozen": {
            "share":   round(phi, 6),
            "base":    round(frozen_base, 2),
            "tickers": [c["ticker"] for c in frozen],
        },
        "max_dev":     round(max(devs.values()), 6) if devs else 0.0,
    }
