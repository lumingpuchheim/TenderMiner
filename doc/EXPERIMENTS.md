# EXPERIMENTS — one hot vs target statistics, and the A/B method it needs

Written 2026-08-16 from the operator's questions in this session and the brief
"One Hot vs Target Statistics". This is the **spec** for the first experiment
and for `experiments.py`, which is exactly as general as that experiment
needs and no more. **Status: built 2026-08-16** — `experiments.py`, the two
tables in `db.py`, the arm hooks in `loop.py`, the `/experiments/<key>` route
in `app.py`, `tests/test_experiments.py` (19 tests) and `tests/test_multihot.py`
(10). The trial opens on its declared Monday, 2026-08-18, by itself.
Companions: [`ONLINE_LEARNING.md`](ONLINE_LEARNING.md) (the cycle the arms run
in), [`TRAINING.md`](TRAINING.md) (leakage rules and tripwires every arm keeps),
[`APP.md`](APP.md) (the web area the overview page joins).

## 0. The rule

**Both arms predict the same lots, before the award exists; the software says
when the picture is clear; the operator decides.**

- No historical replay, no backtest, as evidence (operator, 2026-08-16:
  "simulation may cheat"). Only a prediction in the ledger *before* the award
  published is graded.
- **Backplay may reject, only forward may promote**
  ([`PARAMETERS.md`](PARAMETERS.md) §0.5, agreed the same day). The line
  above governs *evidence for a winner*; it does not oblige us to shadow an
  arm we already know is hopeless. `asof.World` / `rewind_all.py` may kill a
  candidate arm, or rank a coarse grid, before it earns a shadow slot —
  never crown one. What `asof.py` cannot rule out is the arm having been
  designed after seeing those outcomes, which is exactly why a rejection is
  safe there and a promotion is not.
- The software never switches a model on its own. It computes, flags "ready",
  waits. The deadline is a backstop that turns the flag red, not a trigger.
- Exactly one arm feeds customers (the **delivering** arm); the other is a
  **shadow** — trained, scored, graded, never delivered. Switching rewrites
  no history: every ledger row already carries its model id.
- E-mails use one default method; there is no A/B in the e-mails. Nothing
  here assigns customers to groups.

---

# Part I — the case: `cpv-additional-encoding`

## 1. The question

`cpv_additional` is a list of extra CPV codes per lot. Until 2026-08-16 it became
three categorical columns — `cpv_additional__cpv2/3/4` — each holding the
*combination string* of the lot's codes truncated to 2/3/4 digits. The cpv4
combination reached 1,767 distinct values on the server, above
`ONE_HOT_MAX_SIZE = 1024`, so `assert_pure_one_hot` refused every candidate and
the server produced **no model since bootstrap** (brief §3).

**Built 2026-08-16, ahead of the experiment (operator: "use onehot as default so
that there is a prediction at all"):** every list column is now encoded
multi-hot by default — `single_bidder.fit_multihot` / `build_features`,
`TRAINING.md` "List columns are multi-hot". The server predicts again with the
`onehot` encoding. The experiment therefore asks whether the *other* honest
encoding would have been better, on real predictions:

| arm id | label (verbatim in every output) | what the model sees for the additional codes |
| --- | --- | --- |
| `onehot` | **one hot** | the default build: one 0/1 numeric column per distinct code, multi-hot, at cpv2/cpv3/cpv4; rare codes folded into a count (§3) |
| `ts` | **target statistics** | `feature_build='cpv_additional_combination'`: the three combination columns as before 2026-08-16, and CatBoost's ordered target statistics (CTR) for them, because the guard is told they may exceed the cap (§3). Every *other* list column stays multi-hot in both arms. |

Not an arm: raising the cap to 4096. Everything else — data, temporal split,
seed, class weights, all other features, tripwires, threshold, promotion
epsilon — is the same object for both arms.

**Verdict metric: precision at the delivered cutoff** — of the lots an arm
flagged (`score ≥ threshold`), the share that ended with 0–1 bids — on lots
graded for *both* arms. Recall secondary. Training-time PR-AUC is background.

## 2. What happens, Monday by Monday

**Monday 1 (opened, planned 2026-08-18).** The cycle finds the experiment
declared and open. `learn` runs twice — once per arm. Neither arm has a
champion, so each promotes on passing its checks:
`models/m2026-08-18-…-onehot/` and `models/m2026-08-18-…-ts/`, each with
`model.cbm` + `meta.json`; `models/arms/onehot/CURRENT` and
`models/arms/ts/CURRENT` point at them; `models/CURRENT` is rewritten to the
**onehot** model, because `onehot` is the delivering arm. `predict` scores
every open lot twice and writes both arms' rows to `prediction`, distinct
`model` values, same lots. `deliver` sees only the onehot rows — the server
delivers again for the first time. Report line:
`experiment cpv-additional-encoding: collecting — 0 lots graded (one hot 0 / target statistics 0), delivering one hot, deadline 2026-11-30 (104 d)`.
The page shows the same, plus both arms' training background (val PR-AUC,
tripwire text) collapsed.

**Mondays 2–6.** Same. Each arm gates against **its own** champion (val
PR-AUC ≥ own champion − epsilon, all tripwires). If `ts` fails a tripwire one
week, `ts` keeps its champion, keeps scoring, and the failure text appears on
its row — "a tripwire failure in an arm is itself a result". `collecting`
throughout; the page says why (awards publish 1–3 months after the notice).

**Mondays 7–12.** Awards for the first Mondays' lots arrive. `grade` writes
`grade` rows for the delivering arm as today (customer track record), and one
`arm_grade` row per arm per newly awarded lot the arm had scored (§6). Verdict
line moves: `collecting — 41 lots graded (both arms)`, then
`leaning target statistics — P 0.84, 137 paired lots, flagged precision 0.31 vs 0.26 (base 0.11)`,
then perhaps `ready: target statistics better, 96% — 212 paired lots`.

**When the operator decides** (early when clear, or at the red
`deadline reached` on 2026-11-30 at the latest):

```
python experiments.py show cpv-additional-encoding
python experiments.py close cpv-additional-encoding --winner ts --note "..."
python experiments.py deliver cpv-additional-encoding ts     # only if the winner should deliver
```

`close` records the decision; `deliver` rewrites `models/CURRENT` to the ts
champion. From that Monday the customer track record is ts's; nothing before
it changes. Both arms stop training the next cycle; the delivering arm's
champion stays, so the cycle is never left without a model.

## 3. The two feature builds, concretely

Both start from today's `build_features(df, roles, list_frame, multihot)`, whose
default already is the multi-hot encoding below (built 2026-08-16). An arm's
`feature_build` names a variation of it.

**`cpv_additional_combination` (arm `ts`).** `build_features` is told to leave
`cpv_additional` out of the multi-hot set and emit the three combination
columns `cpv_additional__cpv2/3/4` as categoricals, exactly as before
2026-08-16 (one small switch in `_multihot_levels`). `assert_pure_one_hot`
gains `exempt=()`: exempt columns are left out of the `max()` but still
reported, so the gate check reads
`pure_one_hot: passed (max cardinality 367; CTR columns: cpv_additional__cpv4 1767, cpv_additional__cpv3 …)`.
`one_hot_max_size` stays 1024, so CatBoost uses one-hot for every column
under the cap and ordered target statistics for the exempt ones above it —
the switch is CatBoost's own, per column, and the guard now *says* which
columns took it instead of refusing. Arm overrides:
`feature_build='cpv_additional_combination'`,
`guard_exempt=('cpv_additional__cpv2', 'cpv_additional__cpv3', 'cpv_additional__cpv4')`.

**`default` (arm `onehot`) — built.** No combination column; numeric columns
per level `L ∈ {cpv2, cpv3, cpv4}` (digits 2/3/4 of each code in the list):

- `cpv_additional__L__has_<code>` = 1 if `<code>` is among the lot's codes at
  that level, else 0 — for every code in the level's **vocabulary**;
- `cpv_additional__L__n_rare` = number of the lot's codes at that level that
  are *not* in the vocabulary;
- `cpv_additional__L__n` = number of distinct codes at that level (0 when the
  list is empty), so "no additional codes" is a value, not a row of zeros
  that looks like "only rare codes".

The same shape applies to every other list column (`selection_criteria_types`,
`exclusion_grounds`, …), flat: `<col>__has_<value>`, `<col>__n_rare`, `<col>__n`.
Both arms share those; only `cpv_additional` differs.

The **vocabulary** per column and level is the set of values present in at
least `MULTIHOT_MIN_SUPPORT = 30` distinct **lots** (grouped by `sb.KEY`, not
rows) of the full tenders frame, sorted (`sb.fit_multihot`). It is stored in
`meta.json` as `multihot`:

```json
"multihot": {"min_support": 30,
             "vocab": {"cpv_additional": {"cpv2": ["09", "31", …], "cpv3": [...], "cpv4": ["4521", …]},
                       "selection_criteria_types": {"*": ["slc-abil-facil-res", …]}, …}}
```

`learn` stores it in the arm's `meta.json`; `predict` rebuilds the open lots'
columns **from the stored vocabulary**, never from the open frame — that is
what makes the computability tripwire ("feature set differs for open lots")
hold and what makes week 6's columns equal week 1's. Measured width on the
laptop store: 312 features, 239 of them multi-hot; the guard is unaffected
because these are numeric. The `ts` arm's `meta.json` carries the same
`multihot` minus the `cpv_additional` entry.

## 4. Where things live for this experiment

| what | where |
| --- | --- |
| the declaration (id, question, arms, opened, deadline, `default_delivering`) | `experiments.py`, checked in (§7) |
| state: status, delivering arm, decision | table `experiment`, one row, through `ledger.py` |
| models | `models/m<ts>-onehot/`, `models/m<ts>-ts/`; `meta.json` gains `experiment`, `arm`, `label`, `feature_build`, `guard_exempt` (it already carries `multihot`); `models/registry.jsonl` rows gain `arm` |
| champion pointers | `models/arms/onehot/CURRENT`, `models/arms/ts/CURRENT`; `models/CURRENT` = the delivering arm's |
| predictions | table `prediction`, unchanged — UNIQUE `(procedure_id, lot_id, notice_id, model)` already separates the arms |
| customer track record | table `grade`, unchanged — delivering arm only |
| arm-vs-arm outcomes | table `arm_grade` (§6) |
| what customers saw | table `delivery`, unchanged (rows carry the model id) |
| the page | `/experiments/<key>` (§9) |

## 5. What the operator sees, exactly

The verdict line, in the cycle log, the operator report, `dashboard.html` and
on top of the page:

```
<id>: <status> — <n> paired lots (one hot <n₁> graded / target statistics <n₂>),
      flagged precision <p₁> vs <p₂> (base <b>), delivering <label>, deadline <date> (<d> d)
```

Below it on the page, per arm side by side, **cumulative** and, collapsed,
**per ISO week of award publication**:

| column | source |
| --- | --- |
| graded lots, positives, base rate | `arm_grade` |
| flagged: n, precision with Wilson 95 % interval, recall, beats base | `loop.flag_stats`, `loop.wilson` — reused, not re-implemented |
| top tier (`HIGH`) precision vs base | `arm_grade` |
| delivered precision — delivering arm only | join `arm_grade` × `delivery` |
| latest candidate: promoted?, val PR-AUC, tripwire text | `models/registry.jsonl` + `meta.json`, collapsed as "training background" |

## 6. `arm_grade` and the verdict

`grade()` today: per newly awarded lot, the *last prediction before the award*
→ one `grade` row (PK `(procedure_id, lot_id)`). That stays and keeps meaning
"the delivering arm" — it is what customers' track record is built from.

New, in the same step: for every open experiment and every arm, the arm's
last prediction before the award for that lot →

```
arm_grade (experiment, arm, procedure_id, lot_id, model, ts, score, threshold,
           flag, tier, label, n_tenders, award_pub, cpv3, place_nuts3, graded_at,
           seq, raw)   UNIQUE (experiment, arm, procedure_id, lot_id)
```

A frozen record like every other ledger — written once when the award
publishes, never recomputed from a rebuilt store; idempotent by the UNIQUE
key. Read and written through `ledger.py` (`'arm_grades'`).

**This is also the per-(lot, model) grading** [`PARAMETERS.md`](PARAMETERS.md)
§3 asks for, and the reason that file does not specify a second one. `grade`
scores one model per lot — the champion of the day — while 3,942 lots in the
store already carry predictions from two or more models, whose verdicts
nobody has ever computed. The shape above is exactly what answers that: drop
`experiment`/`arm` and it is (lot, model) → correct. When a use appears for
those historical pairs, extend this table's writer to backfill them; do not
write a twin.

**Paired.** The comparison uses only lots present in `arm_grade` for **both**
arms. An arm that missed a Monday is not compared on lots the other saw
alone.

**The verdict** — computed on request from `arm_grade`, never stored:

| status | rule |
| --- | --- |
| `collecting` | fewer than `MIN_PAIRED = 100` paired lots, or fewer than `MIN_FLAGGED = 30` flagged-and-graded lots in either arm |
| `no difference yet` | above the minimums, 0.20 < P < 0.80 |
| `leaning <label>` | above the minimums, P ≥ 0.80 |
| `ready: <label> better, <P>%` | above the minimums, P ≥ 0.95, **and** the winner's latest candidate passed every tripwire |
| `deadline reached` | today ≥ deadline and still open — red, on top of whichever line above applies |

P = posterior probability that one arm's flagged precision exceeds the
other's: independent Beta(½,½) posteriors on each arm's `(hits, flagged)`
over the paired lots, 20 000 draws, fixed seed (plain numpy). Ties break in
favour of the delivering arm — the burden of proof is on the shadow, which is
precision-over-recall applied to the test itself. All constants sit at the
top of `experiments.py` and are printed at the bottom of the page.

---

# Part II — the method, as general as Part I needs

## 7. Declaring an experiment

In code, not in a form (operator, 2026-08-16): an arm is a set of pipeline
overrides, and those live next to the pipeline. The whole first declaration:

```python
EXPERIMENTS = [
    Experiment(
        id='cpv-additional-encoding',
        question='Which encoding of the additional-CPV codes sorts 0/1-bidder '
                 'lots from the rest better, on real predictions?',
        opened='2026-08-18', deadline='2026-11-30',
        arms=[Arm('onehot', 'one hot', feature_build='default'),
              Arm('ts', 'target statistics', feature_build='cpv_additional_combination',
                  guard_exempt=('cpv_additional__cpv2', 'cpv_additional__cpv3',
                                'cpv_additional__cpv4'))],
        default_delivering='onehot',
    ),
]
```

An `Arm` has `id`, `label`, `feature_build` (a registered name — `default`,
`cpv_additional_combination`), `catboost` (dict forwarded to `sb.make_model`,
empty here), `guard_exempt` (columns `assert_pure_one_hot` leaves out of the
max). That is the entire override surface; nothing else about training can
differ between arms, by construction.

Checked at import time, so a bad declaration fails the test suite and never a
Monday: ids `[a-z0-9-]+` and unique; labels non-empty and unique within the
experiment; `feature_build` registered; `default_delivering` an arm;
`deadline` after `opened`.

**State** row created the first time the cycle sees a declared id without
one: `status='open'`, `delivering=default_delivering`, `decision=NULL`. A
declaration removed from code leaves its state row; the page marks it
"declaration missing" and its arms stop training.

**Zero open experiments** — most of the year — costs nothing: the cycle runs
one implicit arm `default` exactly as today, and `models/CURRENT` is the only
pointer in play. That is also the exact behaviour after `close`.

**One open experiment at a time.** Two open ones would need two delivering
arms feeding one set of customers, which is not a thing; a second open state
row makes the cycle refuse loudly (`experiments.open_experiment`) rather than
half-run. A JSONL-only home (a rewind sandbox, a test directory) never gets a
state row and never runs a trial — `ensure_state` does not create a database.

**Shadows stay out of everything but their own record.** The report shortlist
and the score-drift monitor read the predictions ledger; both are handed the
set of shadow model ids (`experiments.shadow_models`: every registry row whose
arm is not its experiment's delivering arm) and skip them. The customer track
record (`grade`) keeps a stamped row only if its arm is that experiment's
delivering arm — from the state table, so the rule outlives the trial.

## 8. What the cycle does per arm

Inside `_run_cycle`, when at least one experiment is open:

1. **learn** — `learn()` once per arm with the arm's `feature_build`,
   `catboost`, `guard_exempt`; gate against **its own** champion
   (`models/arms/<arm>/CURRENT`), unchanged rules; a failing arm keeps its
   champion and never aborts the cycle. Model id `m<ts>-<arm>`. The
   delivering arm's promotion also rewrites `models/CURRENT`.
2. **predict** — `predict_open()` once per arm on the same open lots; rows
   under each arm's model id; returns the **delivering arm's** rows to
   `deliver()`, `drift_monitors()` and `simulation` — a shadow never reaches
   a customer, a monitor or the simulation.
3. **grade** — `grade` as today for the delivering arm; `arm_grade` per arm
   (§6).
4. **report** — the verdict line (§5) in the log, the report and
   `dashboard.html`.

Compute: two trainings per weekly cycle; the monthly shuffled-label check
trains its three models per arm. Accepted (4-vCore VPS).

`experiments.py deliver <id> <arm>` rewrites `models/CURRENT` to that arm's
champion and updates the state row — nothing else. Past deliveries,
predictions and grades keep the model ids they were written with.

## 9. Commands and the page

```
python experiments.py                       # open + closed, one verdict line each
python experiments.py show <id>             # the §5 tables, cumulative and weekly
python experiments.py deliver <id> <arm>    # switch the delivering arm
python experiments.py close <id> --winner <arm|none> --note "..."
```

`close` writes `decision = {winner, note, closed_at, verdict_at_close}`,
`status='closed'`; it does **not** switch delivery — if the winner is not the
delivering arm it says so and prints the `deliver` line. Every command
prints; none writes a report file.

**The page**: one GET route in `app.py`, `/experiments/<key>`, `<key>` =
`TM_EXPERIMENTS_KEY` from `.env` (`secrets.token_urlsafe(24)`, generated
once; empty default in `.env.example`; a configuration line, not one of
`SECRETS.md`'s four credentials). Unset → 404 like any unknown path. An
unlisted URL, not authentication (operator: "not yet"); `robots.txt`
already disallows everything. Rendered with `render.py` helpers like the
customer pages. Sections: last cycle date (from the loop checkpoint, as
`/healthz` does) · **Open** — one card per experiment: question, opened →
deadline (red when past), delivering arm, the verdict line, the §5 tables ·
**Closed** — question, winner label, note, closed date, verdict at close ·
**Constants** — the §6 thresholds. Reads through the same modules the cycle
uses; never opens a storage file itself.

## 10. The second experiment — what would change

Nothing above knows the arms are CatBoost models except `learn`, `predict`
and the `feature_build` registry; the state row, `arm_grade`, the verdict,
the commands and the page only see "arm → prediction per lot" and "lot →
label from the award". So:

- **another single-bidder test** (e.g. target statistics for *all*
  categoricals against a clean baseline, the brief's "considered and
  rejected for now") is one more `Experiment(...)` entry, possibly one more
  registered `feature_build`. No new code path.
- **a test of the relevance gate** ("is this our business") would need three
  things this spec deliberately does not build: the unit becomes
  customer × lot, `learn`/`predict` become "build `relevance.Gate` with the
  arm's config and judge the lots the delivering gate judged", and the
  truth comes from the customer's feedback verdicts (`feedback.py`,
  `learned_refs`) instead of the award — sparse, so `collecting` lasts
  longer. `arm_grade` would gain `sub_id` in its key. That is a second spec,
  written when there is a second concrete question.

## 11. Not built, on purpose

Authentication on the page; a form to create or edit experiments; automatic
switching at the deadline or ever; customer assignment or bandits; re-grading
history (an experiment opened today has no graded lots today, whatever the
ledger holds); the relevance kind.

## 12. Tests

`tests/test_experiments.py`, sandbox data dir, no network, no real training
(a stub `learn`/`predict` that writes rows):

- declaration validation: duplicate ids, unknown `feature_build`,
  `deadline ≤ opened`, `default_delivering` not an arm → import error;
- state row on first sight; declaration removed → row survives, marked;
- `arm_grade`: one row per (experiment, arm, lot); a second cycle adds none;
- paired restriction: a lot graded for one arm only is in neither comparison;
- verdict thresholds at the boundaries (99/100 paired; 29/30 flagged; P at
  0.79 / 0.80 / 0.95); tie → delivering arm; `deadline reached` overlays;
- `deliver` rewrites `models/CURRENT` and nothing else; `close` on a
  non-delivering winner prints the hint and does not switch;
- zero open experiments → the ledger rows a cycle writes are identical to
  today's (golden compare);
- `app.py`: no key → 404; key → 200 and the labels "one hot" / "target
  statistics" appear verbatim.

For the real arms: the `default` (multi-hot) build is already covered by
`tests/test_multihot.py` (columns, `n_rare`, vocabulary round-trip through
JSON, identical columns on an open frame with an unseen code, guard passing,
CatBoost training and scoring). Still to write: `cpv_additional_combination`
emits the three combination categoricals and nothing multi-hot for that
column; `assert_pure_one_hot(exempt=…)` passes with the cpv4 column above the
cap and reports it as a CTR column; both arms train on a 300-lot fixture,
write predictions with distinct model ids on the same lots, and
`models/CURRENT` equals the onehot champion.

## 13. Decisions taken in this session

| decision | who | value |
| --- | --- | --- |
| headline metric | brief | precision at the delivered cutoff; recall secondary |
| evidence | operator | real predictions only; no replay, no backtest |
| what happens at "ready" | operator | show the result; the switch is the operator's |
| deadline | operator | a backstop; earlier decision when clear |
| who says "clear" | operator → assistant | the software flags it (§6); the operator decides |
| where the overview lives | operator | unlisted URL under the Murara area, no auth yet |
| e-mails | operator | one default method, no A/B |
| first-version scope | operator | model-vs-model only; this one case, method sized to it |
| how a test is created | assistant, confirmed | in code; state and decisions via CLI |
| framework | assistant, confirmed | in-house, no external product, no new dependency |
| delivering arm during trial | assistant, per the brief's recommendation | `onehot` |
| validation metrics on the page | assistant | collapsed background, never the verdict line |
