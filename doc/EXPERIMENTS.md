# EXPERIMENTS — A/B tests between models, decided by the operator

Written 2026-08-16 from the operator's questions in this session and the brief
"One Hot vs Target Statistics" (arms `onehot` / `ts`). This is the **spec** for
`experiments.py`; nothing here is built yet except where a line says so.
Companions: [`ONLINE_LEARNING.md`](ONLINE_LEARNING.md) (the cycle the arms run
in), [`TRAINING.md`](TRAINING.md) (leakage rules and tripwires every arm keeps),
[`APP.md`](APP.md) (the web area the overview page joins).

## 0. The rule

**Every arm predicts the same units, before the truth exists; the software
tells the operator when the picture is clear; the operator decides.**

- No historical replay and no backtest count as evidence (operator decision
  2026-08-16 — "simulation may cheat"). Only a prediction written to the
  ledger *before* the outcome was published is graded.
- The software never switches a model on its own. It computes, it flags
  "ready", it waits. The deadline is a backstop that turns the flag red, not
  a trigger that acts.
- Exactly one arm feeds customers at any time (the **delivering** arm); the
  others are **shadows** — trained, scored, graded, never delivered. Switching
  the delivering arm rewrites no history: every ledger row already carries
  its model id.
- Business-action tests (mail wording, cadence, pricing) are **out of scope**.
  E-mails use one default method; there is no A/B in the e-mails (operator
  decision 2026-08-16). Nothing below assigns customers to groups.

## 1. Vocabulary

| word | meaning |
| --- | --- |
| **experiment** | one question with N arms, a kind, a deadline, a status |
| **kind** | what is being predicted and how truth arrives — `single_bidder` now, `relevance` later (§8) |
| **arm** | a named set of overrides on the kind's pipeline, with a **human label** used verbatim in every output (`onehot` → "one hot", `ts` → "target statistics") |
| **unit** | the thing one prediction and one truth attach to — a lot for `single_bidder`, a customer × lot for `relevance` |
| **delivering arm** | the one arm whose predictions reach customers; the champion pointer `models/CURRENT` always names *its* champion |
| **shadow** | every other arm |
| **verdict** | the software's current reading: `collecting`, `leaning <label>`, `ready: <label>`, `deadline reached`, plus the numbers behind it |
| **decision** | the operator's closing act: winner (or "no difference"), note, date |

## 2. What is built, where

| piece | lives in | new? |
| --- | --- | --- |
| experiment declarations (id, question, kind, arms, deadline) | `experiments.py`, a checked-in `EXPERIMENTS` list | new module |
| experiment **state** (status, delivering arm, decision) | table `experiment` in `data/tendermining.db`, via `ledger.py` | new table |
| per-arm graded predictions | ledger `arm_grades` (table `arm_grade`), via `ledger.py` | new table |
| per-arm champion pointer and models | `models/arms/<arm>/CURRENT`, models as today with `arm` in `meta.json` | extension |
| training / scoring / grading per arm | `loop.learn`, `loop.predict_open`, `loop.grade` loop over arms | ~40 lines |
| verdict statistics | `experiments.verdict(...)`, plain numpy — no new dependency | new |
| operator commands | `python experiments.py [list|show|deliver|close]` | new |
| overview page | one route in `app.py`, rendered by `render.py` helpers | new route |

The two tables are the only schema change. `prediction` needs none: its
UNIQUE key `(procedure_id, lot_id, notice_id, model)` already lets two arms
score one lot. `grade` (PRIMARY KEY `(procedure_id, lot_id)`) stays what it is
— **the delivering arm's** record, the one customers' track record is built
from. The arm-vs-arm comparison lives in `arm_grade`, one row per
`(experiment, arm, procedure_id, lot_id)`, and is a frozen record like every
other ledger here: written once when the award publishes, never recomputed
from a rebuilt store.

## 3. Declaring an experiment

In code, not in a form (operator decision 2026-08-16): an arm is a set of
pipeline overrides, and those only live sensibly next to the pipeline. The
first entry:

```python
EXPERIMENTS = [
    Experiment(
        id='cpv-additional-encoding',
        question='Which encoding of the additional-CPV codes sorts 0/1-bidder '
                 'lots from the rest better on real predictions?',
        kind='single_bidder',
        opened='2026-08-18',            # first Monday both arms train
        deadline='2026-11-30',          # backstop, see §5
        arms=[
            Arm('onehot', 'one hot',
                feature_build='cpv_additional_multihot',   # §9
                catboost={}, guard_exempt=()),
            Arm('ts', 'target statistics',
                feature_build='default',
                catboost={},                                # one_hot_max_size stays 1024
                guard_exempt=('cpv_additional__cpv2',
                              'cpv_additional__cpv3',
                              'cpv_additional__cpv4')),      # may exceed the cap → CTR
        ],
        default_delivering='onehot',
    ),
]
```

Rules the declaration enforces at import time (a bad declaration must fail
the test suite, not the Monday cycle):

- ids and arm ids are `[a-z0-9-]+`, unique; labels are non-empty and unique
  within the experiment;
- `kind` is one of the registered kinds (§8);
- `default_delivering` names one of the arms;
- `deadline` is a date after `opened`;
- an arm's overrides may touch only what its kind exposes: for
  `single_bidder` that is `feature_build` (a registered name), `catboost`
  (a dict forwarded to `make_model`), `guard_exempt` (columns
  `assert_pure_one_hot` may let exceed the cap). Everything else — data,
  temporal split, seed, class weights, tripwires, threshold, promotion
  epsilon — is the same object for every arm and cannot be overridden.

The **state** row is created the first time the cycle sees a declared id it
has no row for: `status='open'`, `delivering=default_delivering`,
`decision=NULL`. Removing a declaration from code does not delete state; a
row whose declaration is gone shows on the page as "declaration missing" and
its arms stop training.

**Zero open experiments** is the normal case for most of the year and must
cost nothing: the cycle then runs one implicit arm named `default` exactly as
it does today, and `models/CURRENT` is the only pointer in play.

## 4. What the cycle does per arm

Inside `_run_cycle`, for the `single_bidder` kind:

1. **learn** — `learn()` runs once per arm. Its overrides come from the arm.
   Each arm is gated **against its own champion** (`models/arms/<arm>/CURRENT`)
   with the unchanged rules: all trust checks pass and val PR-AUC ≥ own
   champion − epsilon. A failing arm keeps its own champion and never aborts
   the cycle; the failure is recorded on that arm and shown on the page ("a
   tripwire failure in an arm is itself a result").
   Model ids become `m<timestamp>-<arm>`; `meta.json` gains `experiment`,
   `arm`, `label`, `feature_build`, `feature_state` (§9). The delivering
   arm's promotion also rewrites `models/CURRENT`, so nothing downstream of
   it changes.
2. **predict** — `predict_open()` scores the same open lots once per arm and
   writes each arm's rows under its own model id (dedup by the UNIQUE key,
   as today). It returns the **delivering arm's** rows to `deliver()`,
   `drift_monitors()` and `simulation` — shadows never reach a customer, the
   drift monitors or the simulation.
3. **grade** — `grade()` keeps writing `grade` for the delivering arm's
   "last prediction before the award" (customer track record, unchanged).
   Additionally, for every open experiment and every arm, it writes one
   `arm_grade` row per newly awarded lot the arm had predicted: the arm's
   last prediction before the award, with `label`, `n_tenders`, `award_pub`,
   `score`, `flag`, `tier`, `model`. Idempotent by the UNIQUE key.
4. **report** — the operator report and `dashboard.html` gain one line per
   open experiment: verdict, n graded per arm, days to deadline.

Compute: two trainings per weekly cycle, and the monthly shuffled-label
check trains its three models **per arm**. Accepted (4-vCore VPS; brief §6).

Switching the delivering arm (`experiments.py deliver …`) rewrites
`models/CURRENT` to the new arm's champion **and nothing else**. Past
deliveries, predictions and grades keep the model ids they were written
with; the customer track record from that day on is the new arm's.

## 5. Metrics and the verdict

**Headline: precision at the delivered cutoff** — of the lots an arm flagged
(`score ≥ threshold`), the share that ended with 0–1 bids — against the base
rate of the same graded lots. Recall secondary. Training-time validation
metrics (PR-AUC on the recent window) are shown collapsed as background,
never in the verdict line (operator decision: background only).

Per arm, side by side, cumulative and per ISO week of award publication:

- graded lots `n`, positives, base rate;
- flagged precision with its Wilson interval (`loop.flag_stats`, `loop.wilson`
  — reused, not re-implemented), recall, `beats_base`;
- precision of the top tier (`tier == 'HIGH'`), against base;
- for the delivering arm only: precision of what was actually delivered
  (join with the `delivery` ledger);
- tripwire status of the arm's latest candidate.

**Paired evidence.** Both arms score the same lots, so the comparison uses
only lots graded for *both* arms. That is what makes the numbers comparable
week to week: an arm that missed a Monday does not get compared on lots the
other saw alone.

**The verdict** — computed on request from `arm_grade`, never stored:

| status | rule |
| --- | --- |
| `collecting` | fewer than `MIN_FLAGGED = 30` flagged-and-graded lots in either arm, or fewer than `MIN_PAIRED = 100` paired lots |
| `leaning <label>` | above the minimums and P(precision<sub>A</sub> > precision<sub>B</sub>) ≥ 0.80 |
| `ready: <label> better, <P>%` | above the minimums, P ≥ 0.95, **and** the winner's latest candidate passed every tripwire |
| `no difference yet` | above the minimums, 0.20 < P < 0.80 |
| `deadline reached` | today ≥ deadline and still open — shown red on top of whichever line above applies |

P is the posterior probability that one arm's flagged precision exceeds the
other's: independent Beta posteriors (Jeffreys prior, ½/½) on each arm's
`(hits, flagged)`, evaluated by 20 000 draws with a fixed seed so the page is
reproducible. Ties in labels break in favour of the delivering arm (the
burden of proof is on the shadow, which is the precision-over-recall rule
applied to the test itself). All thresholds are named constants at the top
of `experiments.py`, printed on the page.

Expected timing (brief §5): awards publish 1–3 months after the notice, so
`collecting` for the first 6–10 weeks is normal and the page says so instead
of showing an empty table.

## 6. Operator commands

```
python experiments.py                       # open + closed, one line each, verdict inline
python experiments.py show <id>             # the per-arm tables, weekly + cumulative
python experiments.py deliver <id> <arm>    # switch the delivering arm (rewrites models/CURRENT)
python experiments.py close <id> --winner <arm|none> --note "..."
```

`close` records `decision = {winner, note, closed_at, verdict_at_close}` and
sets `status='closed'`. Closing does **not** switch delivery — if the winner
is not the delivering arm the command says so and names the `deliver` call;
one act, one command. A closed experiment's arms stop training the next
cycle; the delivering arm's champion stays whatever it was, so a close never
leaves the cycle without a model. Every command prints; none writes a report
file.

## 7. The overview page

One route in `app.py`, GET only, HTML from the same helpers as the customer
pages: **`/experiments/<key>`** where `<key>` is the value of
`TM_EXPERIMENTS_KEY` in `.env` (32+ URL-safe characters, generated once with
`secrets.token_urlsafe(24)`). Unset → the route does not exist (404 like any
other unknown path). Not authentication — an unlisted URL, as the operator
asked; `robots.txt` already disallows everything. Authentication is a later
change and this spec does not pretend otherwise. `TM_EXPERIMENTS_KEY` is a
configuration line, not one of the four credentials in `SECRETS.md` §1, and
travels with `.env.example` as an empty default.

Sections, top to bottom:

1. **Open** — one card per open experiment: question, kind, opened →
   deadline (days left, red when past), delivering arm, **verdict line**,
   then the per-arm cumulative table (§5), then a collapsed weekly table
   and a collapsed "training background" table (val PR-AUC, tripwire text
   of the latest candidate per arm).
2. **Closed** — one line each: question, winner (label), note, closed date,
   the verdict at close.
3. **Constants** — the thresholds from §5, so a reader knows what "ready"
   meant when the page was read.

The page reads `experiment`, `arm_grade`, `delivery` and the model registry
through the same modules the cycle uses; it never opens a storage file
itself. Its live numbers change only when a cycle runs — a paragraph at the
top names the last cycle date (from the loop checkpoint, as `/healthz` does).

## 8. Kinds — the seam that keeps this general

A kind answers three questions; the registry, the arm bookkeeping, the
verdict, the commands and the page never ask a fourth:

```python
class Kind:
    unit_key: tuple[str, ...]                          # what one prediction attaches to
    def learn(self, arm, ctx) -> Candidate             # train / build this arm; gate vs its own champion
    def predict(self, arm, ctx) -> list[dict]          # one ledger-shaped row per unit, model id set
    def outcomes(self, ctx, since) -> dict[key, truth] # units whose truth arrived
```

| kind | unit | `learn` | `predict` | `outcomes` |
| --- | --- | --- | --- | --- |
| `single_bidder` — **built by this spec** | lot `(procedure_id, lot_id)` | `single_bidder.train` with the arm's overrides, today's tripwires | score open lots | award notice → `label` from bid count (`sb.assemble`) |
| `relevance` — **specified, not built** | customer × lot `(sub_id, procedure_id, lot_id)` | build `relevance.Gate` with the arm's config / references / lexicon | judge the same lots the delivering gate judged | the customer's feedback verdicts (`feedback.py`, `learned_refs`) |

Honest caveat for `relevance`: its truth is sparse — only lots the customer
bothered to judge get one — so `collecting` lasts longer and the numbers rest
on fewer units. The status shows the counts; no design removes the sparsity.

## 9. The first experiment: `cpv-additional-encoding`

From the brief. Two arms, differing **only** in how the additional-CPV codes
reach the model:

| arm | label | encoding |
| --- | --- | --- |
| `onehot` | **one hot** | `feature_build='cpv_additional_multihot'`: one 0/1 numeric column per distinct code ("has 4521", …), multi-hot, at the levels the column has today (cpv2 / cpv3 / cpv4). No categorical column → no `one_hot_max_size` involved, rule 4 holds structurally. Codes seen in fewer than `MIN_SUPPORT = 30` training lots fold into one `n_rare_additional_cpv` count per level. |
| `ts` | **target statistics** | `feature_build='default'`: the combination column as today, `one_hot_max_size` 1024, and `guard_exempt` names the three `cpv_additional__cpv*` columns so `assert_pure_one_hot` lets *those* exceed the cap → CatBoost's ordered target statistics for exactly those. |

Not an arm: raising the cap (e.g. 4096). Rejected in the brief — a later
wall, not the absence of one.

**`feature_state`.** The `onehot` build has state: the code vocabulary per
level (which codes got a column, which folded into the rare count) is fixed
on the training frame and must be applied identically to open lots — the
computability tripwire ("feature set differs for open lots") checks exactly
this. So the build returns its state, `learn` stores it in `meta.json`, and
`predict` rebuilds features from the stored state, never from the open
frame. `feature_build='default'` has empty state. This is the one place §3's
"an arm may change the feature build" touches `single_bidder.build_features`:
a `feature_build` hook after the role-driven build, before the guard.

**Delivering arm during the trial: `onehot`** (operator: "keep the rule until
decided"). Note what this means on the server today: there is no champion
at all (brief §3), so the first `onehot` promotion is what makes the server
predict again — the experiment is also the fix.

## 10. What is not built, on purpose

- No authentication on the page (operator: "not yet").
- No form to create experiments; no editing of arms after `opened`.
- No automatic switch of the delivering arm, at the deadline or ever.
- No customer assignment, no bandit, no business-action tests.
- No re-grading of history: an experiment opened today has no graded lots
  today, whatever the ledger holds from before — rule §0.
- No `relevance` kind implementation — the seam only.

## 11. Tests

`tests/test_experiments.py`, on a sandbox data dir (`subscriptions.write_sandbox`
style, `--network none`-safe, no model training — a fake `Kind` with a
two-line `learn`/`predict`/`outcomes`):

- declaration validation: duplicate ids, unknown kind, override outside the
  kind's surface, deadline before opened → import-time error;
- state creation on first sight; declaration removed → row survives, marked;
- `arm_grade` written once per (experiment, arm, lot); re-running the cycle
  adds nothing;
- paired restriction: a lot graded for one arm only is in neither arm's
  comparison;
- verdict thresholds at the boundaries (29 vs 30 flagged; P at 0.79 / 0.80 /
  0.95); tie goes to the delivering arm; `deadline reached` overlays;
- `deliver` rewrites `models/CURRENT` and nothing else; `close` on a
  non-delivering winner prints the `deliver` hint and does not switch;
- zero open experiments → cycle output identical to today (golden compare
  of the ledger rows written);
- `app.py`: route absent when the key is unset; 200 with the key; the page
  names labels verbatim.

For the real `single_bidder` kind, one small end-to-end on a 300-lot fixture:
both arms train, both write predictions with distinct model ids on the same
lots, `models/CURRENT` equals the `onehot` arm's champion, and
`cpv_additional_multihot` reproduces its columns on open lots from
`feature_state`.

## 12. Decisions taken in this session

| decision | who | value |
| --- | --- | --- |
| headline metric | brief | precision at the delivered cutoff; recall secondary |
| evidence | operator | real predictions only; no replay, no backtest |
| what happens at "ready" | operator | show the result; the switch is the operator's |
| deadline | operator | a backstop; earlier decision allowed when clear |
| who says "clear" | operator → assistant | the software flags it (§5); the operator decides |
| where the overview lives | operator | unlisted URL under the Murara area, no auth yet |
| e-mails | operator | one default method, no A/B |
| first-version scope | operator | model-vs-model only |
| how a test is created | assistant, confirmed | in code; state and decisions via CLI |
| framework | assistant, confirmed | in-house, no external product, no new dependency |
| delivering arm during trial | assistant, per brief's recommendation | `onehot` |
| validation metrics on the page | assistant | collapsed background, never the verdict line |
