# ONLINE_LEARNING — the predict → grade → retrain loop

Status: concept, not yet implemented. Turns the one-off training notebook
([`train_single_bidder.ipynb`](train_single_bidder.ipynb), recipe in
[`TRAINING.md`](TRAINING.md)) into a running service that stays current on its own.
Data acquisition builds on [`DATA_PIPELINE.md`](DATA_PIPELINE.md); this document uses
its vocabulary (**component** = a box that always runs; **phase** = build order in
time, never a scope cut).

## What this is, in one paragraph

Today the model is a snapshot: trained once on January–June, evaluated once. This
loop makes it a habit. On a schedule, the program **downloads** the newest notices,
**grades** its own past predictions against outcomes that have arrived since,
**retrains** on everything known so far, **predicts** on all tenders still open for
bidding, and **reports** — a ranked list of "hardly anyone will bid on this" lots,
headed by our own verified track record. Every part of the honesty discipline from
TRAINING.md carries over unchanged; the loop just runs it repeatedly.

## The cycle

```
        ┌────────────┐     ┌───────────┐     ┌───────────┐     ┌───────────┐     ┌──────────┐
   ────►│  DOWNLOAD  │────►│   GRADE   │────►│   LEARN   │────►│  PREDICT  │────►│  REPORT  │
        │ new notices│     │ old preds │     │  retrain  │     │ open lots │     │ + track  │
        └────────────┘     │ vs new    │     │ + promote │     │ → ledger  │     │   record │
                           │ outcomes  │     └───────────┘     └───────────┘     └──────────┘
                           └───────────┘
```

**The interval is a parameter, never a constant.** The loop is invoked as

```bash
python loop.py run --last 7d      # download the last 7 days of tenders
python loop.py run --last 2w      # …or 2 weeks
python loop.py run --last 3m      # …or 3 months (e.g. first backfill)
```

`--last X` (days/weeks/months) sets the download window; the checkpoint guarantees
no gap regardless of what is passed — the effective window is
`max(--last, time since the last successful run)`, so overlapping windows are safe
(re-fetched notices dedup by `notice_id`) and missed runs self-heal. The scheduler
only decides how often to invoke the command; the command assumes nothing about
how often that is. Every other window in the system (validation window, track-record
window) is likewise a config parameter — the numbers in this document are defaults,
not constants.

A starting default of weekly fits the domain — bid deadlines run ~30+ days, so a
lot flagged this week is still actionable; the corpus grows ~600 lots/week; and
awards trail tenders by months, so grading much more often mostly grades nothing —
but changing it is a scheduler-config edit plus a different `--last`, no code.

**"Online learning" here means the loop is online, not the algorithm.** We do a full
retrain every cycle rather than incrementally updating the model. At this data size
CatBoost retrains in under a minute on a laptop CPU — a full retrain is simpler,
reproducible from scratch, and immune to the drift bugs of true incremental
learners. If the corpus ever makes retraining slow, that is a Phase-later
optimization, not a design change.

## The prediction ledger — the heart of the system

An **append-only** file. Every prediction the system ever makes is written down at
the moment it is made and never edited:

```jsonl
{"ts": "2026-08-07T06:12:00Z", "model": "m2026-08-07", "procedure_id": "…",
 "lot_id": "LOT-0001", "notice_id": "…", "publication_date": "2026-08-05",
 "score": 0.72, "tier": "HIGH", "deadline_date": "2026-09-10"}
```

Why this is the one component that must exist before anything else:

1. **Honest evaluation becomes possible.** When the award for that lot arrives in
   November, we grade *the row we wrote in August* — not a reconstruction, not a
   backtest. Nothing can be quietly recomputed with hindsight.
2. **It is the sales asset.** "Of the lots we flagged in the last quarter whose
   outcome is now public, 39 of 100 ended with 0–1 bids, versus 17 by chance" is a
   sentence a buyer can verify. It can only be written from a ledger.
3. **It feeds v2.** The buyer-history feature grows from exactly this kind of
   date-stamped record; the loop is what un-starves it (see the v2 verdict in the
   notebook: 14% coverage in training will rise mechanically every week).

A lot gets a new ledger row each time it is scored — including re-scores after a
corrigendum. Rows are never updated; grading joins ledger rows to outcomes at read
time.

## The five steps, precisely

### 1. Download (network; the only step that touches the internet)

Fetch the window given by `--last X` (days/weeks/months), widened to cover
everything since `checkpoint.json` if that is older (the existing pattern from
DATA_PIPELINE.md): new notice XMLs into the raw archive, then the offline extractor
appends to the growing store — the same two parquets the notebook reads, now
maintained instead of one-off:

```
data/store/tenders.parquet   # every notice revision, roles in column metadata
data/store/awards.parquet    # every award notice
data/logs/checkpoint.json    # last fetched date → next run resumes here
```

Dedup by `notice_id` (a re-downloaded notice replaces itself byte-identically);
revisions of a lot are all kept — TRAINING.md rule 3 requires them. A failed run
needs no repair: the next run's effective window stretches back to the checkpoint
and covers the gap.

### 2. Grade (the loop's conscience)

For every lot whose **award arrived since the last run**: look up all ledger rows
for that lot, mark each `correct` / `incorrect` (label: `n_tenders <= 1`, same
drop-rules as TRAINING.md), and append the verdicts to `data/ledger/grades.jsonl`.
The headline number grades the **last prediction made before the award** — that is
the prediction a customer would have acted on.

Output: a rolling track record over the configured track-record window
(a parameter; e.g. the trailing 12 weeks):

> 214 flagged-lot outcomes arrived. 82 ended with 0–1 bids (38/100 flags right;
> chance was 17/100). Of all 96 single-bid lots that resolved, we had flagged 31
> (catches 32/100).

Grading rules that keep it honest:

- **No award yet ≠ competitive.** A lot is graded only when its award is published.
  Ungraded predictions stay ungraded — they are "pending", never "wrong".
- **Some lots never get an award notice** (cancelled, never published). Track the
  pending-forever fraction; if it grows, say so in the report rather than silently
  shrinking the denominator.
- **The base rate is re-measured every cycle** from the same window as the track
  record, so "vs chance" always compares against the current market, not January's.

### 3. Learn (retrain + promote)

Retrain on **all labeled lots to date** using the notebook's exact recipe: features
mechanically by role, every revision a row at 1/k weight, buyer history computed
date-aware (rule 5), pure one-hot with the cardinality assertion.

The fresh model is a **candidate**, not automatically the new model:

- **Validation window:** the most recent stretch of awarded lots (a parameter;
  default ~8 weeks), held out temporally (train on everything first published
  before the window, same no-straddle assertion). This is a rolling version of
  the notebook's exam.
- **Promotion gate:** the candidate replaces the current champion only if it
  (a) passes the automated trust checks below, and (b) matches or beats the
  champion's PR-AUC on the validation window. Otherwise: keep the champion, log the
  failure, raise an alert. The system degrades to "last week's model" — never to
  "broken model".
- **Model registry:** `models/m<date>/` holds the model file, its metrics, the
  validation-window definition, and the training-data cutoff. Every ledger row
  names its model, so any past prediction can be traced to the exact artifact that
  made it.

#### Persisting models

One folder per model; a model is promoted or rolled back as one unit:

```
models/
  m2026-08-07/
    model.cbm          # CatBoost native format: trees, feature names, cat columns
    calibration.json   # the isotonic mapping fitted to THIS model's scores (Phase 4)
    meta.json          # provenance: trained_at, code_version (git sha), data_cutoff,
                       #   validation window + metrics, tripwire status, promoted y/n
  m2026-08-14/…
  registry.jsonl       # one line per model: id, promoted, headline metrics
  CURRENT              # one-line text file naming the champion → rollback = edit one line
```

Rules:

- **`.cbm`, never pickle.** `save_model()` / `load_model()` is CatBoost's stable
  binary format — loads instantly on any machine and Python version, needs no
  training data. Pickles break across library versions and execute code on load.
- **The calibration layer belongs to its model.** An isotonic mapping is only valid
  for the score distribution of the model it was fitted against; it travels in the
  same folder and is never mixed across models.
- **`meta.json` makes every model rebuildable.** `(raw archive, code_version, seed)`
  fully determines a model, so the raw archive is the thing that needs real backup —
  model files are cheap derivatives (a few MB each; a decade of weekly models is
  ~1 GB, so old folders are kept, never deleted).
- **Not in git.** The repo is public and models would grow it every week forever;
  they live on disk next to `data/`, optionally synced to Drive alongside the
  parquets for an off-machine copy.
- **The predict step reads `CURRENT`;** every ledger row records the `model_id`
  that scored it, closing the loop between registry and ledger.

### 4. Predict

Score **every lot whose bid deadline has not passed**, including re-scores of lots
that received a corrigendum since last cycle (production matches training: whatever
revision exists, score it). Append all scores to the ledger.

**Customers see tiers, not raw probabilities.** The notebook showed the raw scores
are over-confident at the top (calibration cell) and the top-50 ordering is noisy.
So: fit a calibration layer (isotonic regression) on the validation window, then
bucket into **HIGH / MEDIUM / LOW** by calibrated probability. A tier claim ("HIGH
means roughly 2 in 5 end single-bid") is checkable against the track record;
a raw "0.72" invites false precision.

### 5. Report

One artifact per cycle (markdown first; e-mail/HTML rendering is presentation, not
architecture):

1. **Track record header** — the grade-step sentence, first, always. The report
   leads with how right we have been, not with promises.
2. **The list** — open lots in tier order: title, buyer, region, estimated value,
   bid deadline, tier. Sorted so "act on this now" is the top of the page.
3. **Health footer** — lots downloaded this week, awards arrived, model promoted
   or held, trust-check status, drift warnings.

## Automated trust checks (every cycle, machine-enforced)

TRAINING.md rule 6 becomes code that gates promotion:

| Check | When | Alarm condition |
| --- | --- | --- |
| Too-good alarm | every retrain | validation ROC-AUC > 0.85 → block promotion, investigate leak |
| Computability dry-run | every retrain | any feature not computable for open lots → block |
| Shuffled-label run | monthly, and after any pipeline code change | shuffled PR-AUC > 1.5× base rate → block |
| Base-rate drift | every cycle | single-bid rate outside historical band → warn in report |
| Missingness drift | every cycle | a feature's null-rate jumps (source schema changed under us) → warn |
| Award-latency drift | every cycle | median tender→award gap shifts materially → warn (affects grading delay and v2 coverage) |
| Score-distribution drift | every cycle | this week's score histogram diverges from last month's → warn |

Warnings appear in the report footer; blocks keep the champion and notify. Nothing
fails silently.

## What can go wrong, and the designed answer

- **Source outage / format change** → download step fails loudly; extractor is
  offline and re-runnable; raw archive means nothing is ever lost; next run catches
  up from checkpoint.
- **A bug ships in feature code** → shuffled-label check on code change + the
  registry: every model is reproducible from (raw archive, code version), so a bad
  model can be rebuilt or rolled back; the ledger shows exactly which predictions
  it made.
- **The model quietly gets worse** → promotion gate stops bad candidates; the
  track record makes gradual decay visible in the one number we publish anyway.
- **Machine down for a month** → checkpoint + append-only stores: one run heals
  the gap. Predictions missed during downtime are simply absent from the ledger —
  a hole in coverage, never corrupted data.

## Where it runs

A single idempotent entry point — `python loop.py run` — that executes the five
steps and is safe to re-run. Scheduler-agnostic by design: Windows Task Scheduler
on the current machine is enough to start (the whole cycle is minutes of CPU;
Colab is not needed — training at this size is faster locally than the notebook
round-trip). A hosted runner is a deployment choice for later; note the repo is
public and `data/` is git-ignored, so any hosted option must keep the store and
ledger private (private artifact storage or a private data repo).

## Build order (phases in time — no component is cut)

1. **Store + skeleton loop** — incremental download/extract into the growing
   parquets; retrain; score open lots; append to ledger. *First useful output: the
   weekly ranked list.*
2. **Grading + track record** — grade arriving awards against the ledger; the
   report gains its header sentence. *First provable claim.*
3. **Promotion gate + trust checks** — candidate/champion, registry, automated
   tripwires and drift monitors. *The loop can now be left unattended.*
4. **Calibration + tiers + report polish** — isotonic layer, HIGH/MEDIUM/LOW,
   customer-ready formatting. *The output becomes a product, not a printout.*

Each phase leaves a running system; nothing in a later phase changes the data
written by an earlier one (the ledger format carries `model` and `tier` from day
one, `tier` simply null until Phase 4).

## Open decisions (defaults proposed, none blocking Phase 1)

- **Cadence** — how often the scheduler invokes `loop.py run --last X`; weekly
  with `--last 7d` proposed as the starting default.
- **Flag rule** — fixed threshold vs. "top N per week": tiers (Phase 4) largely
  dissolve this; until then, threshold 0.5 as in the notebook.
- **Report delivery** — markdown file in `data/reports/` first; e-mail later.
- **Scope widening** — the loop is CPV-45-agnostic by construction (features are
  role-driven); when to add further CPV branches is a business decision, not a
  code change.
