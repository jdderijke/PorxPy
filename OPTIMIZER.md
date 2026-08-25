# The PorxPy Optimizer — how it works

*Applies to `porxpy/optimizer.py` as of v0.77.0.*

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
| `candidates` | Every pre-loaded fund: ticker, price, shares currently held, `include` flag, and its look-through exposure per `(facet, level, bucket)` — built by `breakdowns.candidate_exposures`, which computes only the levels the target set actually mentions |
| `targets` | Per facet, per LEVEL, a `{bucket: fraction}` map — e.g. `{"country": {"region": {"northAmerica": 0.40, ...}, "country": {"japan": 0.05}}}`. Sparse: a facet or level with no targets is ignored entirely |
| `cash_base` | Cash available, in the portfolio's base currency |
| `cash_exposure` | What cash itself counts as (asset class `cash`, a currency, a country) |
| `max_funds` | Ceiling on how many funds the design may use |
| `max_error` | Tolerance **per facet**, in fractions — `{"country": 0.02}` means "within 2 percentage points". Per facet, NOT per level; see §12 |
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

### Levels — each target is a constraint at its own grain

Since v0.65.0 a target names a **level** as well as a bucket, and the
matrix takes them one `(facet, level)` block at a time. Every fund's
exposure is measured independently at every level the target set
mentions, so a target set that mixes grains needs no ordering rule.

Targeting semiconductors 15%, software 10% and technology 35% gives
three rows. The semiconductor funds satisfy their own row and contribute
to the technology row as well, leaving 10% of technology to be filled by
any technology fund. That falls out of the algebra; there is no
"children first, then the remainder" pass anywhere in the module.

Levels are **not** re-expressed at a common grain. Rolling a sector
target down to sub-sectors would invent detail the user never gave, and
rolling one up would discard detail they did. The rule that a parent is
held to at least the sum of its targeted children is enforced at save
time by `targets.validate_target_levels`, so an arithmetically
impossible brief is refused before the solver ever sees it.

All three tree facets take part on equal terms: `sector`
(sub-sector / sector / super-sector), `country` (country / region /
super-region) and, since v0.70.0, `asset_class` (sub-class / asset class
/ super class). `currency` declares a single level of the same shape, so
nothing in this module branches on whether a facet has levels.

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

The scaling is applied **per `(facet, level)` block**, and each block
carries the full facet weight. A facet you target at three levels
therefore contributes three blocks of rows rather than one, and so
counts for roughly three times as much in the objective as an otherwise
identical facet you targeted at a single level. That is defensible — you
did state three separate intentions — but it is a consequence of the
construction rather than a decision anyone took, and it is worth knowing
before you conclude the solver is ignoring a facet you targeted once.

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
deviation_f = max over that facet's targeted buckets, AT EVERY LEVEL,
              of |A_raw·w − t_raw|
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
- **`achieved`** / **`deviation`** — `{facet: {level: {bucket: value}}}`,
  mirroring the shape of `targets`. What the design actually delivers and
  how far that is from the target, each bucket measured against the
  exposure **at its own level**. Signed: positive is overweight.
- **`facets`** — per facet: worst deviation, the tolerance, whether it was
  met. Flat, one entry per facet — the levels of a facet are collapsed
  into a single worst-case figure here, unlike `deviation` above.
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
- **Tolerance and reported error are per facet, not per level.** The
  matrix fits each level separately, but `max_error` is keyed by facet
  alone and `_facet_devs` groups residuals by facet alone, so the three
  levels of a sector target collapse into one worst-case number. You
  cannot ask for 2pp at super-sector and 8pp at sub-sector, and when a
  facet misses, the headline figure does not say which grain missed.
  The per-bucket `deviation` block does carry the level, so the answer
  is available — just not in the summary or the stopping test.
- **Exposure quality is the real limit.** The optimizer is exact about the
  data it is given. If a fund's look-through breakdown is stale, partial,
  or from an issuer card rather than actual holdings, the design is precise
  about the wrong numbers. Check coverage before trusting a tight fit.
- **An `unknown` slice is dead weight to the solver** (and, since v0.77.0,
  can be asserted away). A fund whose sector card covers 40% of it
  contributes 40% of its value to the sector fit and nothing usable for
  the rest: there is no "unknown" bucket to allocate against, so the fund
  looks like a poor fit for every target it might in fact satisfy. Where
  no source can supply more, the user can tick **coverage complete** on
  that card, which drops the unknown slice and scales the identified part
  up before `candidate_exposures` ever sees it. It is a fund-level
  override (`breakdown_complete.<facet>`), so the optimizer needs no
  knowledge of it — the block simply arrives complete. The cost is that a
  design can then be exact about an *assumption*, which is why nothing
  asserts it automatically and the card's badge reads `100% ASSUMED`.
- **No transaction costs, no tax, no minimum lot sizes.** Fractional shares
  are assumed throughout.

---

## 13. Known open issues

Defects specific to the optimiser, as opposed to the deliberate
boundaries in §12. Each is something that should be fixed rather than
something someone chose.

### The metadata facets are targetable, but the optimiser is blind to them

`config.TARGET_FACETS` is `BREAKDOWN_FACETS + META_FACETS`, so the
Targets tab offers `market_cap` and `style_box`, `targets.py` computes
deviations for them, and the X-ray card renders them. The optimise
endpoint does not. It builds each candidate's exposure with
`candidate_exposures(data["fund_breakdowns"], targets)`, and
`fund_breakdowns` covers only `_FUND_BD_FACETS` — `asset_class`,
`sector`, `country`, `currency`. The metadata one-hots come from
`breakdowns.meta_facet_items`, which is called by
`rollup_portfolio_fundlevel` and by nothing on the optimise path.

Every candidate therefore reports empty exposure for a metadata facet.
All of its weight falls into the synthetic `__other__` bucket, and the
target becomes unsatisfiable by any portfolio whatsoever. Confirmed by
running it rather than by reading it: with a `market_cap` target set,
`candidate_exposures` returns `{'market_cap': {}}` for every fund, and
the run reports

```
market_cap 100.0% (allowed 5%) … these targets need exposure none of
your candidate funds have.
```

That message is wrong in the way that matters. Every fund carries a
`market_cap` on its structure block; the optimiser was simply never
handed it. The user is told their universe is inadequate when the
universe is fine.

The proposed design itself is still correct — the dead rows are constant
across all assets, so they shift the least-squares fit not at all — but
`target_met` is false forever, and the stated reason sends the user
looking for funds they already own. Until this is closed, set targets on
the four breakdown facets only.

The fix is to merge `meta_facet_items` into the exposure dict the
optimise route builds, so a metadata facet answers as a one-hot
distribution at its own single level, exactly as `currency` already
does. `market_cap` and `style_box` have no entry in `FACET_LEVELS`, so
their level key is the facet name itself — the shape `_key_at_level` and
`build_facet_block` already assume for a flat facet, so nothing else has
to change.

### Targeting one facet at several levels silently multiplies its weight

`_add_target_rows` is called once per `(facet, level)` block, and each
call applies the full `facet_weight` for that facet. A facet targeted at
three levels therefore contributes three blocks of rows, each scaled as
though it were the facet's only one, and so pulls on the objective
roughly three times as hard as an otherwise identical facet targeted at
a single level.

Nobody decided this. It falls out of iterating levels inside the same
loop that applies the weight, and it means the relative importance of
your facets shifts as a side effect of how many grains you happened to
express them at — add a super-sector target to a sector target you
already had, and country quietly matters less than it did. The
`facet_weights` you set are no longer the weights in force.

The honest fix is to divide each block's scale by the number of levels
targeted for that facet, so a facet's total pull is the same however
many grains it is expressed at. That is a behaviour change to existing
designs, which is why it is recorded here rather than applied.
