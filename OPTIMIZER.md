# The PorxPy Optimizer — how it works

*Applies to `porxpy/optimizer.py` as of v0.118.0. The full audit — every
claim in the document re-checked against the module — was done at
v0.91.0; since then the v0.96.0 peer-scoring change was folded into §7b
and §13's one remaining open issue was re-confirmed by reading
`_add_target_rows` at v0.97.0 and is unchanged at v0.98.0. v0.115.0 moved
tolerances from percentage points to a share of each target, which
touched §2, §4, §4b, §10 and §12. Check the stamp against
`porxpy/__init__.py` before trusting a claim.*

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
| `max_error_rel` | Tolerance **per facet**, as a share OF EACH TARGET — `{"country": 0.10}` means "within 10% of every country target". A 40% target then allows 4pp and a 5% target 0.5pp, floored at `TOL_FLOOR` = 0.5pp. Per facet, NOT per level; see §12. Relative since v0.115.0 |
| `min_weight` | Positions below this are pruned as dust |
| `min_trade_base` | Trades below this amount are suppressed as noise |

**Out:** a trade list, the resulting positions, the achieved exposure per
targeted bucket, the residual deviation per bucket and each bucket's own
allowance, whether every tolerance was met, and if not, which bucket
failed and by how much.

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

The three metadata facets — `market_cap`, `style_box` and, since
v0.104.0, `focus_theme` — are flat in the same way, and reach the matrix
as a one-hot per fund at weight 1.0 rather than as a distribution.
That weight is the whole content of a thematic target: an AI fund
supplies AI exposure with every euro in it, since the theme describes
the fund's mandate rather than any one holding. A fund that carries no
theme answers `n/a`, which is not a targetable bucket, so its money
lands in `__other__` and can serve only the untargeted remainder — the
correct reading, and the reason a thematic target above the share of
your universe that actually carries themes is unreachable by
construction.

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

### Where row weights come from (v0.115.0)

Every row carries its own allowance, and the allowance IS the weight:

```
allowance_b = max(max_error_rel[facet] x target_b, TOL_FLOOR)   # TOL_FLOOR = 0.005
row_scale_b = facet_weights[facet] / sqrt(n_buckets + 1) / allowance_b
```

then the whole system is divided by the smallest row_scale, so the numbers
stay near 1 rather than running to the raw 1/0.005 = 200. A common factor
on both A and t leaves the argmin untouched.

Minimising the scaled residual is therefore minimising the sum of squared
deviation-over-allowance: the solver equalises how far each bucket is
through its OWN budget, instead of treating a miss on a 5% target and a
miss on a 40% target as equally urgent. Before v0.115.0 the same idea was
applied one facet at a time, from a facet-wide tolerance; making the
allowance per bucket made the weight per bucket for free.

The synthetic `__other__` row gets an allowance on the same rule. When
your targets sum to 100% its target is 0, so it floors at 0.5pp and stray
exposure is penalised hard — exactly the "I want exactly this mix"
reading. When they sum to less, it is slack with a generous allowance and
costs the solver almost nothing.

**So tolerance does two jobs**: it sets the stopping test, and it sets how
hard the solver tries. That coupling is deliberate — split them and the
solver would work hardest precisely where the pass mark is loosest — but
it is worth knowing that tightening a tolerance changes the answer, not
just the pass mark. `facet_weights` remains available as a plain
multiplier, for a caller who wants importance to diverge from tolerance.

### Two matrices

`A`/`t` are the scaled versions the solver optimises. `A_raw`/`t_raw` are
unscaled, used only for *measuring* deviations, so the numbers reported to
you are in real percentage points rather than in solver units. A mask
excludes `__other__` rows from measurement — slack you never targeted is
not error.

---

## 4b. Cash is a reservation, not a column (v0.90.0)

The user states how much of the portfolio must stay as cash they hold —
**an amount in base currency, not a percentage** (`cash_reserve` on the
portfolio). That amount is taken off the top before anything is designed:

```
portfolio = held + frozen + cash_on_hand
fund_side = portfolio − reserve        ← what every target is a % of
free_base = fund_side − frozen         ← what the solver actually places
```

The solver never sees the reserve. Fund weights are a simplex over
`free_base` and sum to 1, so the design is fully invested by construction
and `cash_after` equals the reserve exactly — not approximately.

Both directions the user asked for fall out of the same arithmetic rather
than needing a branch:

| situation | what happens |
|---|---|
| cash on hand **above** the reserve | the difference is in `free_base`, so it gets invested |
| cash on hand **below** the reserve | `free_base` is smaller than the funds are worth, so the trade list sells the shortfall |

### Why it used to be a column, and why that was wrong

Until v0.89.0 cash was the last column of the exposure matrix, with its
weight a free variable, so a "leave 5% in cash" target needed no
special-casing and the no-overdraft rule came free. v0.89.0 went further
and added a `custody` facet (`direct` / `via_fund`) so that a target on
cash *the user holds* could be satisfied separately from a fund's own cash
sleeve — a fund's column carried `via_fund`, so the `custody: direct` row
was a 1 on the cash column and a 0 on every fund, and no fund could move
it.

That was a correct mechanism for the wrong requirement. It made the
reserve a **target**, and the optimiser has no instrument that buys a bank
deposit — so the amount was fitted approximately, in competition with
every other target, and pulled them off course while it was at it. The
user reported it: *"It tries to satisfy the cash held by me target, just
like the other targets."*

A reservation is exact, needs no facet, and makes the whole cash column
redundant: money that is not in the budget cannot be occupied by a fund,
by construction rather than by a row of zeros. `custody` was removed with
the column.

### What that changed elsewhere in this file

* `_build_facet_matrix` no longer takes `cash_exposure` and builds
  `len(candidates)` columns, not `len(candidates) + 1`.
* `_greedy_select`'s baseline was "everything in cash", which was a real
  portfolio because cash had a column. With no column and nothing
  selected there is no portfolio to score, so an all-zero weight vector
  stands in: `_facet_devs` then reports each target missed by its own
  size, which is the honest reading of "you hold none of this yet", and
  any fund improves on it.
* `frozen_share` is measured against `fund_side`, not the grand total,
  because that is the space the targets live in.
* A target set summing to less than 100% used to be able to park its
  slack in cash. It cannot now — the fund side is fully invested — so the
  slack goes to whatever untargeted funds exist, and the `__other__`
  bucket is what absorbs it.

### The deviation report measures the same thing

`compute_target_deviations` reads the fund-side rollup
(`fundlevel_breakdowns_ex_cash`), not the all-inclusive one. Until
v0.91.0 it read the latter, so the Targets tab measured achievement
against a denominator that included the reserve while the optimiser
measured against one that excluded it — the tab reporting a shortfall on
a design the solver had just called met. Two screens, one question, two
answers, which is the failure this codebase is most exposed to.

### Guard rails

The reserve is refused, with a written reason rather than a bad design,
when it is the whole portfolio or more, and when locked or excluded funds
are already worth more than the fund side that is left. Its **lower**
bound (never negative) is enforced at write; its **upper** bound is not,
deliberately — the portfolio's value moves with the market, so a reserve
that was legal when saved can exceed the total a week later with nobody
having touched it. The editor blocks a save above the value on screen, the
Targets tile says so when it has drifted, and the optimiser refuses. Three
places, because no single one of them can be true forever.

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

"Higher-scoring" is relative to the **scoring model in force**, which has
been selectable since v0.79.0. The run sends a `score_preset`, and the
endpoint feeds the optimiser the same score blocks the fund list is
showing — a model that disagreed with the visible one would make the
optimiser's choices unexplainable. Sending `none` skips this pass
entirely and the design is chosen on fit alone. The same fund can be a
95 under Cost driven and a 12 under Returns driven, so the alternatives
table is an answer to "better under this model", not "better".

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

A **pair** does, since v0.96.0. Peer groups of two were previously left
unscored — `MIN_PEER_GROUP` was 3 — and since this pass only considers
candidates that HAVE a peer score, both members of a pair were skipped
in both directions: no alternatives offered, and neither fund counted
toward the design's quality figure. Both now rank, 33 against 67, so a
two-fund group behaves like any other. Expect the portfolio score to
move on designs holding such funds; nothing about the pricing of a swap
changed, only which funds have a score to be priced against.

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

Allowances are divided by the same factor, row by row:

```
allowance_free = allowance / (1 − φ)
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
- **`achieved`** / **`deviation`** / **`tolerance`** — `{facet: {level:
  {bucket: value}}}`, all three mirroring the shape of `targets`. What the
  design delivers, how far that is from the target, and that bucket's own
  allowance, each measured against the exposure **at its own level**.
  `deviation` is signed: positive is overweight. `tolerance` is emitted
  rather than left for the caller to recompute (v0.115.0), so the relative
  rule and its floor live in one place and a table cannot disagree with
  the solver about whether a row passed.
- **`facets`** — per facet: `max_dev`, the biggest miss anywhere in the
  facet, plus the bucket that DECIDED it — `worst_bucket`, `worst_level`,
  `worst_dev`, that bucket's `tolerance`, and `ratio` = worst_dev over
  tolerance. `met` is `ratio <= 1`, not `max_dev <= tolerance`: allowances
  differ per bucket now, so the biggest miss and the worst miss are no
  longer the same row. `relative` echoes the setting the run used.
- **`target_met`** — every facet's worst bucket within its own allowance.
- **`reason`** — when a target is missed, which BUCKET, by how much, and
  against what allowance, plus whether more funds would help or the
  exposure simply is not available in your universe.
- **`swaps`**, **`frozen`** — exchanges the fit refinement applied, and
  what was left untouched. `frozen` is `{share, base, tickers}`.
- **`alternatives`** — per chosen fund, the better-scoring peers and what
  each substitution would cost.
- **`substitutions`** — the substitutions the caller requested and that
  were applied.
- **`ok`**, **`selected`**, **`total_base`**, **`cash_weight`**,
  **`cash_after`**, **`max_dev`** — the run's status flag, the chosen
  tickers, the portfolio total, cash as a fraction and as an amount after
  the trades, and the single worst deviation across all facets (the
  headline figure; per-facet detail is in `facets`).

### A target set is a file too (v0.118.0)

The Targets tab exports and imports the whole design as CSV —
`targets_to_csv` / `targets_from_csv` in `targets.py` — carrying all
seven targetable facets at every level, the per-facet tolerances and the
three scalars below. The cash reserve and `score_preset` deliberately do
not travel: the first describes a portfolio rather than a design, the
second names a scoring model that may not exist in the install reading
the file.

Targets **replace** on import, tolerances and scalars **merge**. That
asymmetry is deliberate: a target set is validated as a whole (§the
parent/child check), so a partial import could install a set the editor
would have refused, while the tolerances carry no cross-constraint.

### Settings are remembered per portfolio (v0.117.0)

The Optimizer panel's five controls — the per-facet tolerances, Max
funds, Min weight, Min trade and the quality picker — are stored on the
portfolio as `optimizer_settings`, beside its targets and its cash
reserve, and shipped on the `/view` payload so the panel renders from one
round-trip.

They are saved on **Run**: the settings that produced the design on
screen are by definition the ones worth keeping, and a separate Save
button would let the panel and the answer beside it disagree. The write
merges, so a request omitting a key keeps the stored one, and a facet
whose targets are cleared keeps its tolerance for when they come back.

Defaults come from `config.DEFAULT_OPTIMIZER_SETTINGS` — one source for
the solver's fallback, the endpoint's body defaults and the panel — and
values are clamped to `config.OPTIMIZER_SETTING_BOUNDS` on write, in
`utils`, where a hand-edited `portfolios.json` cannot get past them.

### What the endpoint adds

`optimise_portfolio` answers only the fitting question. The route around
it (`app.api_portfolio_optimize`) layers on three diagnostics that need
the candidate universe rather than the solve, and that exist because "0%
achieved" has several causes needing different fixes:

- **`source_mix`** — per facet, how many candidates described it from
  each source. A mixed run is worth knowing about: issuer cards and
  look-throughs are not always on the same basis.
- **`level_report`** — per (facet, level), how many candidates answer,
  with how much non-residual weight, which targeted buckets nothing in
  the universe holds, and (v0.116.0) **`silent_funds`**, the funds that
  say nothing at that level.
- **`facet_warnings`** and **`facet_gaps`** — the prose version, and the
  funds behind it. Since v0.116.0 a warning is `{text, facet, funds}`
  rather than a bare string: it names the funds it is about instead of
  counting them, because the remedy for every one of these is per fund —
  go to that fund and give the facet a source — and a count states the
  problem while withholding the only thing needed to act on it. The two
  lists answer the same question at different grains (`facet_gaps` = no
  source at all; `silent_funds` = nothing at this level), so the browser
  renders both through one function and both are clickable.

Nothing is applied. The trade list goes to the same `apply_trades`
primitive the manual Buy/Sell dialog uses, atomically, only when you press
Apply.

---

## 11. Performance

On a 35-fund, three-facet problem the whole run takes roughly 2 seconds.
Three things get it there:

- **Screening precision.** The hundreds of throwaway fits that rank
  candidates run at 150 solver iterations rather than 500
  (`SCREEN_ITER`). Measured on 12x9 problems, that lands within ~2e-6 of
  the converged residual for about a 3x speed-up. The chosen set is
  always re-solved exactly, so nothing reported inherits the screening
  tolerance.
- **Warm starts.** Each trial differs from the incumbent by one column, so
  the previous solution is the starting point.
- **Shortlisting**, as described in §7.

One caution learned the hard way: incumbent and challenger must be scored
at the *same* precision. Comparing a screening-precision challenger against
an exact-precision incumbent makes genuine improvements smaller than the
screening noise read as "no improvement", stopping the search early and
returning a worse portfolio. `SCREEN_ITER` was 60 when that was found,
where the noise floor is ~1e-4; at the current 150 it is ~2e-6, so the
trap is narrower but the rule is unchanged.

---

## 12. Known limits

- **Greedy + swap is not exhaustive.** Local search improves on greedy but
  offers no global guarantee.
- **Shortlisting can miss a good swap.** Deliberate; the alternative costs
  seconds of wall clock.
- **Tolerance does double duty** — stopping test and objective weight. Set
  `facet_weights` explicitly if you want those to differ.
- **Tolerance is SET per facet, though it now BINDS per bucket.**
  `max_error_rel` is keyed by facet alone, so the three levels of a sector
  target share one relative figure and you cannot ask for 5% at
  super-sector and 20% at sub-sector. What each bucket actually gets does
  vary — that figure times the bucket's own target — and since v0.115.0
  `_facet_devs` reports the bucket and level that decided each facet, so
  the summary no longer hides which grain missed. The remaining limit is
  the input, not the report.
- **No ceiling on an allowance.** At 20% relative, a 90% equity target
  allows 18 points. That is the instruction as given, and a cap would
  reintroduce the absolute tolerance v0.115.0 removed — but it does mean
  a loose relative setting is looser on big buckets than the old 5pp
  default was. Set that facet tighter if it is not what you meant.
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
  Since v0.86.0 a currency card sourced **from country** inherits the
  country card's assertion, so one tick there can complete two of the
  facets the solver reads — same caveat, twice over.
- **No transaction costs, no tax, no minimum lot sizes.** Fractional shares
  are assumed throughout.

---

## 13. Known open issues

Defects specific to the optimiser, as opposed to the deliberate
boundaries in §12. Each is something that should be fixed rather than
something someone chose.

One remains, re-confirmed at v0.115.0: `_add_target_rows` still computes
its `norm = facet_weight / sqrt(len(keys) + 1)` inside a body called once
per `(facet, level)` block, so the full facet weight is applied to every
level of a facet that is targeted at more than one. The v0.115.0 move to
per-bucket allowances did not touch this — the allowance divides `norm`,
it does not replace the per-block normalisation — so a facet targeted at
three levels still counts for roughly three times as much in the
objective as one targeted at a single level. The metadata-facet
blindness recorded here since v0.30.0 was **fixed in v0.89.0** — see the
resolved entry below.

### ~~The metadata facets are targetable, but the optimiser is blind to them~~ — fixed in v0.89.0

Kept as a record because the failure was invisible in exactly the way
worth remembering: the optimiser reported a confident, wrong diagnosis.

`config.TARGET_FACETS` is `BREAKDOWN_FACETS + META_FACETS`, so the
Targets tab offered `market_cap` and `style_box`, `targets.py` computed
deviations for them, and the X-ray card rendered them — while the
optimise endpoint built each candidate's exposure with
`candidate_exposures(data["fund_breakdowns"], targets)`, and
`fund_breakdowns` covers only the four breakdown facets. The metadata
one-hots come from `breakdowns.meta_facet_items`, which was called by
`rollup_portfolio_fundlevel` and by nothing on the optimise path.

Every candidate therefore reported empty exposure for a metadata facet,
all of its weight fell into the synthetic `__other__` bucket, and the
run said

```
market_cap 100.0% (allowed 5%) … these targets need exposure none of
your candidate funds have.
```

which was wrong in the way that matters: every fund carries a
`market_cap` on its structure block, and the optimiser had simply never
been handed it. The user was told their universe was inadequate when the
universe was fine.

**The fix.** `candidate_exposures` now takes `fund_structure` and
`is_cash`, and answers any targeted facet in `META_FACETS` from
`meta_facet_items` rather than from `fund_breakdowns`. A metadata facet
has no entry in `FACET_LEVELS`, so its level key is the facet name
itself — the shape `_key_at_level` and `build_facet_block` already
assume for a flat facet, so nothing else had to change.

It was found while building the `custody` facet that v0.90.0 then removed
(§4b); the fix outlived the feature that prompted it, because
`market_cap` and `style_box` were blind for the same reason and both are
still targetable.

It has since paid for itself again: `focus_theme`, added in v0.104.0,
joined `META_FACETS` and reached the optimiser with no change to this
path at all. That is the test of whether the fix was made in the right
place — a fix written per facet would have left the third one blind in
exactly the way the first two were.

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

Unchanged at v0.102.0, but easier to walk into: the targets editor's add
control is now the facet tree picker, which makes every level of a facet
visible and one click away, where the flat list it replaced buried the
coarse levels among the fine ones. Nothing about the solver moved — the
same design that was hard to express by accident is now easy to express
on purpose.
