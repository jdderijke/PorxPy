# The PorxPy Optimizer — how it works

*Applies to `porxpy/optimizer.py` as of v0.37.0.*

---

## 1. What it does

You set target exposures — "40% North America, 25% Europe, 30% technology,
5% cash". The optimizer looks at the funds you have pre-loaded, works out
which of them to hold and in what proportion so the blended portfolio sits
as close to those targets as it can, and gives you the buy/sell list that
gets you there.

**Designing and rebalancing are the same operation.** A portfolio built
from scratch is just one where every position happens to be zero and all
the money is in cash. So there is one solver, and it always answers with a
*trade list* rather than a set of weights. For an empty portfolio those
trades are all buys; for an existing one they are the buys and sells that
move you from where you are to where you want to be.

**What it is not.** It does not forecast returns, model risk, or optimise
anything in the Markowitz sense. There is no covariance matrix and no
expected-return vector. It is a fitting problem: get the exposure of what
you hold as close as possible to the exposure you asked for.

---

## 2. Inputs and outputs

**In:**

| Input | Meaning |
|---|---|
| `candidates` | Every pre-loaded fund: ticker, price, shares currently held, `include` flag, and its look-through exposure per facet |
| `targets` | Per facet, a `{bucket: fraction}` map — e.g. `{"country": {"northAmerica": 0.40, ...}}` |
| `cash_base` | Cash available, in the portfolio's base currency |
| `cash_exposure` | What cash itself counts as (asset class `cash`, a currency, a country) |
| `max_funds` | Ceiling on how many funds the design may use |
| `max_error` | Tolerance **per facet**, in fractions — `{"country": 0.02}` means "within 2 percentage points" |
| `min_weight` | Positions below this are pruned as dust |
| `min_trade_base` | Trades below this amount are suppressed as noise |

**Out:** a trade list, the resulting positions, the achieved exposure per
targeted bucket, the residual deviation per facet, whether every tolerance
was met, and if not, which facet failed and why.

Everything runs in **base currency** and in **fractions (0–1)**. Converting
prices and FX, and turning the percentages you typed into fractions, is the
caller's job. The module never touches Yahoo and never reads a file.

---

## 3. The shape of the problem

There are two problems nested inside each other:

- **Continuous:** given a fixed set of funds, what weights get closest to
  the targets? This has an exact answer, computed directly.
- **Discrete:** *which* funds should be in that set? This is combinatorial
  — choosing 8 funds from 35 is about 23 million possibilities — so it gets
  a heuristic.

The discrete search calls the continuous solver hundreds of times to score
its candidates. Almost all the runtime is in that inner loop.

---

## 4. Step one — the exposure matrix

Every targeted facet becomes a set of rows. Every candidate fund becomes a
column, with cash as the last column.

Cell `A[i][j]` is **fund j's exposure to bucket i**. If a portfolio holds
the funds in weights `w`, its exposure is:

```
e = A · w
```

That is an identity, not an approximation — a portfolio's sector exposure
*is* the weighted sum of its funds' sector exposures. This is why the
problem is linear, and why nothing more elaborate is called for.

### Only targeted facets take part

A facet you set no targets on contributes no rows. Scoring it would mean
inventing an intention you never expressed.

### The `__other__` row

Each facet gets one extra synthetic row collecting all exposure that falls
*outside* the buckets you targeted, with target:

```
target(__other__) = 1 − Σ(your targets for that facet)
```

One mechanism, two useful behaviours:

- Targets summing to 100% → `__other__` has target 0, so exposure leaking
  into untargeted buckets is penalised.
- Targets summing to 70% → `__other__` absorbs the remaining 30% with no
  penalty, so partial targets work without special-casing.

### Row scaling

Facets have wildly different bucket counts — 4 asset classes against 40
countries. Left alone, the country facet would dominate purely by having
ten times as many rows. So each facet's rows are scaled:

```
scale_f = facet_weight_f / √(n_buckets_f + 1)
```

The `√n` divisor equalises facets of different sizes. The
`facet_weight` then expresses how much you care.

### Where facet weights come from

By default they derive from your tolerances:

```
facet_weight_f = (1 / tolerance_f) / min over all facets of (1 / tolerance)
```

A facet you demand 2% on is weighted five times harder than one you allow
10% on. Without this, the solver would spread its effort evenly and might
never satisfy the strict facet at all. The normalisation by the minimum
just keeps the numbers near 1.

**So tolerance does two jobs**: it sets the stopping test, and it sets how
hard the solver tries. That coupling is deliberate, but worth knowing —
tightening a tolerance changes the answer, not just the pass mark.

### Two matrices

`A`/`t` are the scaled versions the solver optimises. `A_raw`/`t_raw` are
unscaled, used only for *measuring* deviations, so the numbers reported to
you are in real percentage points rather than in solver units. A mask
excludes `__other__` rows from measurement — slack you never targeted is
not error.

---

## 5. Step two — solving the weights

Given a set of columns, find the weights that best match the targets:

```
minimise    ‖A·w − t‖²
subject to  w ≥ 0
            Σ w = 1
```

Read plainly: *make the portfolio's exposure as close to the target as
possible, using only non-negative weights that add up to the whole
portfolio.*

Those two constraints define a **simplex**, and they carry real meaning:

- `w ≥ 0` — no short positions.
- `Σw = 1` — every euro is allocated. **Cash is one of the columns**, so
  its weight is also constrained non-negative. That is what makes
  overdraft structurally impossible: the solver cannot spend money that
  isn't there, because doing so would need a negative cash weight.

### How it is solved

FISTA-accelerated projected gradient descent: take a gradient step,
project back onto the simplex, repeat, with a momentum term that gives
quadratic rather than linear convergence.

The projection is exact — a sort-based algorithm that finds the closest
point on the simplex to any vector. It is what enforces both constraints
on every iteration.

No scipy. The problem is small and convex, so numpy solves it to tolerance
in microseconds, and the Windows PyInstaller build stays free of scipy's
packaging problems.

### Why not L1 for sparsity

The textbook move for "pick a few out of many" is an L1 penalty. It does
nothing here. On the simplex, `‖w‖₁ = Σw = 1` identically — the penalty is
a constant, its gradient is zero. Hence the discrete search in step three.

*(If continuous sparsity were wanted, negative entropy `λ·Σ wᵢ log wᵢ`
would work, since it is minimised at the vertices. Not currently used.)*

---

## 6. Step three — greedy forward selection

1. Start with everything in cash. That is the baseline to beat.
2. If every facet is inside its tolerance → **stop, success**.
3. Otherwise, for each fund not yet chosen, solve the weight problem for
   `{already chosen} + {this fund} + cash` and record the residual.
4. Keep whichever fund gave the lowest residual. Add it permanently.
5. Repeat until in tolerance, or `max_funds` is reached, or **no remaining
   fund improves the fit at all** — which means the targets are not
   reachable with your universe. That is a fact about your fund list, not
   a solver failure, and it is reported as such.

### Two different criteria, on purpose

- **Ranking** uses residual sum of squares — smooth, so the search path is
  stable.
- **Stopping** uses the worst per-facet deviation in real percentage
  points, because that is the number you set a threshold on:

```
deviation_f = max over that facet's targeted buckets of |A_raw·w − t_raw|
```

Reported per facet rather than as one overall number: forcing a single
tolerance means setting it to whatever the loosest facet needs, dragging
the strict ones down with it.

---

## 7. Step four — swap refinement

Greedy is myopic. A fund chosen in round one can never be un-chosen, even
once later picks make it redundant. A worked example, with targets of 50%
North America / 30% Europe / 20% Japan and `max_funds = 3`:

```
round 1   + WORLD   (65% NA, 20% EU, 8% JP)   best single fund   → picked
round 2   + JAPAN                                                → picked
round 3   + EURO                                                 → picked

greedy result:   WORLD + JAPAN + EURO   →  2.19pp off
optimal three:   SP500 + JAPAN + EURO   →  0.00pp off
```

Greedy spent its first slot on WORLD, which the optimal answer does not
use at all, and had no way to reconsider.

So after greedy converges, the optimizer tries **exchanging** each selected
fund for an unselected one, keeps any exchange that lowers the residual,
and repeats until a full round finds no improvement. The example above then
returns the optimal answer.

This is local search. It carries no guarantee of the global optimum, but it
removes exactly that failure, and it is deterministic — the same inputs
give the same portfolio.

**It runs unconditionally**, including when greedy already met every
tolerance. Greedy stops at the *first* qualifying design, so a successful
run lands just under the line — 1.8pp against a 2pp budget. This pass
exchanges funds within the existing set size, adding none, so it can often
take that to 0.4pp at no cost. Skipping it would mean showing a design
worse than it needs to be, and — since §7b prices every substitution
against the achieved deviation — would make the alternatives look more
expensive than they are.

### Candidate shortlisting

Trying every exchange is `|selected| × |remaining|` solves per round. To
cut that, each unselected fund is scored by how strongly its column aligns
with the current residual:

```
r = A·w − t                    (what the portfolio is currently short of)
alignment_j = |A[:,j] · r|     (how much fund j could reduce it)
```

Only the best 8 are tried. This is the screening step from matching
pursuit: the alignment score is the first-order estimate of how much a
column can reduce the residual, so what it discards has least to offer.

A fund outside the shortlist could in principle have made a good swap —
this is a heuristic, and the tradeoff is deliberate.

---

## 7b. Fund quality — priced, not applied

The passes so far answer *what fits the targets best*. Among designs that
fit acceptably there is usually a wide choice, and they are not equally
good: one may be built from cheap, large, well-performing funds and
another from expensive small ones.

Two ways to act on that were considered and rejected. A weighted
objective, `minimise error² − λ·score`, needs a `λ` trading "squared
exposure error" against "score points" — no interpretable unit, and
retuning whenever the universe or scoring changes. An automatic error
budget, spending whatever tolerance is left over on quality, fails for a
subtler reason: greedy stops at the *first* design inside tolerance, so
the leftover budget is near zero by construction and almost nothing would
ever be swapped.

Both also share a deeper flaw — they make the tool guess how much accuracy
you are willing to trade, which varies per portfolio and is precisely the
judgement you are best placed to make.

**So the optimizer prices the trade and lets you decide.** For every fund
in the design, it finds the higher-scoring funds in that fund's peer group
and works out what substituting each would actually cost: swap it in,
re-solve the weights, measure the new deviation.

```
  EXPENSIVE   score 12   ·  peer group equity|none|
     keep EXPENSIVE
     → CHEAP        95  (+83)    0.00 → 0.00pp    within tolerance
     → MIDDLING     55  (+43)    0.00 → 1.40pp    within tolerance
     → BOUTIQUE     71  (+59)    0.00 → 3.10pp    exceeds tolerance
```

Substitutions that break a tolerance are shown and flagged, not hidden —
you asked for the choice, and a swap costing 2.5pp against a 2pp tolerance
may still be the one you want.

**Peer group, not the whole universe.** The optimizer holds a European
bond fund because the targets demand one; offering to replace it with a
high-scoring US equity tracker would answer a question nobody asked. A
fund alone in its peer group has no alternatives to offer.

### Why the costs do not add up

Each row is priced against the **same baseline**, independently. That is
what makes the numbers comparable — but it also means accepting two swaps
that each cost 0.7pp may together cost 0.3pp or 2.1pp, because deviations
are not linear.

So ticking rows does not update the figures in place. Pressing
**Recalculate** re-runs the entire optimisation with the substitutions
forced into the selection, and returns real numbers. Substitutions are
applied *before* the weights are finalised, so a substituted design is
solved exactly like any other rather than patched afterwards.

---

## 8. Two flags: `incl` and `locked`

Two independent switches constrain what the optimizer may do with a fund.

**`incl`** lives on the fund, in the pre-loaded list, and applies
everywhere: *never put this fund in a buy suggestion, in any portfolio.*

**`locked`** lives on the position, in a portfolio's fund list, and
applies only there: *do not suggest selling what I hold of this fund in
this portfolio.*

Together they give four states:

| `incl` | `locked` | The optimizer may |
|---|---|---|
| on | off | buy and sell freely |
| off | off | sell, but never buy |
| on | on | buy more, but never sell |
| off | on | neither — fully frozen |

### The middle two are bounds, not exclusions

A fund that may be sold but not bought can hold any weight *at or below*
what you already own. A locked fund can hold any weight *at or above* it.
With `wᵢ⁰` the fund's current weight in the tradeable budget:

```
never buy   →   wᵢ ≤ wᵢ⁰
never sell  →   wᵢ ≥ wᵢ⁰
```

Those are per-column bounds on the simplex, which the plain projection
cannot express. So the solver projects onto a **capped simplex** instead —
`lb ≤ w ≤ ub`, `Σw = 1` — by bisection on a single scalar `λ`:

```
wᵢ(λ) = clip(yᵢ − λ, lbᵢ, ubᵢ)         find λ such that Σ w(λ) = 1
```

`Σw(λ)` is continuous and non-increasing in `λ`, so bisection converges
without a solver. Sixty iterations are exact to floating point.

Two consequences:

- **Locked funds are in the design by right.** A lower bound only binds if
  its column is present, so a locked holding is pre-selected rather than
  chosen by greedy — it is never swapped out, never pruned as dust, and
  never offered a "better in class" alternative, since substituting it
  would be exactly the sale you forbade.
- **Fully frozen funds** (`incl` off *and* `locked` on) are handled
  separately, as a fixed baseline outside the optimisation — see below.

### Fully frozen positions

A frozen holding is excluded from the optimizer's decisions but not from
the portfolio. Getting that wrong either way gives a wrong answer:
dropping it optimises a portfolio you do not have, while treating it as
tradeable sells the position you marked untouchable.

So it contributes a fixed baseline and the optimisation solves for the
rest. With `φ` the frozen share and `f` the exposure the frozen funds
contribute:

```
f + (1 − φ)·(A_free · w) = t        →        A_free · w = (t − f) / (1 − φ)
```

Tolerances are divided by the same factor:

```
tolerance_free = tolerance / (1 − φ)
```

because the solver now measures residuals inside the free sub-portfolio,
where the same money is a larger fraction. This keeps the stopping test
meaning what it meant before.

**Components of the reduced target can go negative** — that happens when
the frozen holdings already overshoot a bucket. Least squares on the
simplex handles it correctly: it reads as "put as little here as you can",
which is right, since the overshoot cannot be sold off.

Frozen holdings still appear in the proposed positions, flagged, so the
table reconciles to 100% rather than to `(1 − φ)`. And when a target is
missed with frozen holdings in play, the explanation says so — otherwise
"not reachable" reads as an optimizer failure rather than a consequence of
your own choices.

---

## 9. Step five — from weights to trades

Weights below `min_weight` are pruned and the remaining set re-solved, so
the pruned weight is redistributed properly rather than dropped.

Then, per fund:

```
amount        = weight × free_base          (money to put in this fund)
target_shares = amount / price
shares_delta  = target_shares − current_shares
```

`free_base` is the tradeable budget: held value of unfrozen funds plus
cash. Frozen value is deliberately excluded — it is not available to spend.

A trade is emitted only if `|shares_delta × price| ≥ min_trade_base`. This
also naturally skips the no-op case where a held fund's target equals what
you already own.

**Reported weights are of the whole portfolio** (`weight × (1 − φ)`), not
of the free sub-problem, because that is the number you compare against a
target.

---

## 10. What you get back

- **`trades`** — the buy/sell list, sorted by size.
- **`positions`** — the resulting portfolio, frozen ones flagged.
- **`achieved`** / **`deviation`** — per targeted bucket, what the design
  actually delivers and how far that is from the target. Signed: positive
  is overweight.
- **`facets`** — per facet: worst deviation, the tolerance, whether it was
  met.
- **`target_met`** — all facets within tolerance.
- **`reason`** — when a target is missed, which facet and by how much, plus
  whether more funds would help or the exposure simply is not available in
  your universe.
- **`swaps`**, **`frozen`** — exchanges the fit refinement applied, and
  what was left untouched.
- **`alternatives`** — per chosen fund, the better-scoring peers and what
  each substitution would cost.
- **`substitutions`** — the substitutions the caller requested and that
  were applied.

Nothing is applied. The trade list goes to the same `apply_trades`
primitive the manual Buy/Sell dialog uses, atomically, only when you press
Apply.

---

## 11. Performance

On a 35-fund, three-facet problem the whole run takes roughly 2 seconds.
Three things get it there:

- **Screening precision.** The hundreds of throwaway fits that rank
  candidates run at 150 solver iterations rather than 500. The chosen set
  is always re-solved exactly, so nothing reported inherits the screening
  tolerance.
- **Warm starts.** Each trial differs from the incumbent by one column, so
  the previous solution is the starting point.
- **Shortlisting**, as described in §7.

One caution learned the hard way: incumbent and challenger must be scored
at the *same* precision. Comparing a screening-precision challenger against
an exact-precision incumbent makes genuine improvements smaller than the
screening noise (~4e-5) read as "no improvement", stopping the search
early and returning a worse portfolio.

---

## 12. Known limits

- **Greedy + swap is not exhaustive.** Local search improves on greedy but
  offers no global guarantee.
- **Shortlisting can miss a good swap.** Deliberate; the alternative costs
  seconds of wall clock.
- **Tolerance does double duty** — stopping test and objective weight. Set
  `facet_weights` explicitly if you want those to differ.
- **Exposure quality is the real limit.** The optimizer is exact about the
  data it is given. If a fund's look-through breakdown is stale, partial,
  or from an issuer card rather than actual holdings, the design is precise
  about the wrong numbers. Check coverage before trusting a tight fit.
- **No transaction costs, no tax, no minimum lot sizes.** Fractional shares
  are assumed throughout.
