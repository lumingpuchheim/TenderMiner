# RUNBOOK — how to operate TenderMining

What to type, when, and what you get back. Component internals live in
[`README.md`](../README.md) (pipeline programs) and the design docs
([`ONLINE_LEARNING.md`](ONLINE_LEARNING.md), [`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md),
[`RELEVANCE.md`](RELEVANCE.md)); this file is only about running things.

## 1. The routine: two commands

```
python cycle.py run --last 7d       # update: any day, as often as you like — mails nobody
python deliver.py run               # send:   once a week, from what the last cycle wrote
```

**Until 2026-08-18 this was one command, `loop.py run`,** and the sending was
welded to the training: the customer reports were rendered from the scores
still in memory at the end of the same process. That meant a cycle could not
be run mid-week to *see* something — a new arm's first training, a changed
gate, the experiments page filling in — without also mailing every customer.
The two halves now stand apart, and the split follows one line: **everything
that produces state runs any time; the one step that reaches a person runs on
the schedule.**

**`cycle.py run`** does, in order: download the window's notices → rebuild the
store → **update the embedding sidecar** (new lots only, ~5 min) → open or
advance the A/B trial → grade outcomes → retrain/promote (once per arm during a
trial) → score open lots into the ledger → drift monitors, trial verdict,
knob lines → write the operator report → **simulate every winner company**
(~2 s, [`SIMULATION.md`](SIMULATION.md)) → dashboard, public site, admin
index, cache pruning. Nothing in it sends mail; nothing in it writes a
delivery row.

**`deliver.py run`** does the rest: reads **the delivering model's latest
prediction per lot still open** from the ledger (the same rows the report
just listed), learns each customer's own wins as references, slices per
subscription, gates, renders, mails, appends the delivery rows and turns the
trial-ask clock. It trains nothing and downloads nothing. It **refuses** when
the newest prediction is older than `--max-age` (default `1d`) — a Monday
whose 07:00 cycle died gets a loud line and no mail, never a mail job that
quietly retrains — and it waits behind a running cycle on the heavy lock
rather than reading half-written predictions.

- Run `cycle.py` on any cadence (weekly `--last 7d`; daily `--last 2d` also
  works). Predictions dedup by notice and model, so a second run in a week
  costs an hour of CatBoost and changes nothing a customer sees.
- Run `deliver.py` weekly. It is idempotent per day: a second run the same
  day finds every lot already on record for every customer and sends nothing.
- Everything customer-facing lands under
  `data/reports/subscriptions/<sub_id>/report_<date>.html` (+ annex).
- Operator artifacts: `data/reports/report_<date>.md`, `data/reports/dashboard.html`.
- **You never schedule the embedding or the gate separately** — they ride
  inside the cycle. If the sidecar is somehow broken, `deliver.py` prints
  `[deliver] relevance gate unavailable … delivering ungated` and continues;
  fix at leisure, nothing is lost.
- **`[knobs] GATE MISMATCH` stops delivery on purpose**
  ([`PARAMETERS.md`](PARAMETERS.md) §8.3). It means the gate configuration
  this run resolved to is not the one the register records — an edited
  constant, or a stray environment variable in the shell that launched the
  cycle (twenty of the evidence rules are env-driven). The message names the
  knobs that differ. Grading, training and this week's predictions have
  already been written and are not lost; only the customer reports are
  skipped. Fix by restoring the value, or by making the move properly —
  constant, receipt comment, register row, `knobs.EXPECTED_GATE_FINGERPRINT`,
  one commit — then re-run with `--skip-download`.
- The report's **Knobs** section is a *proposal*, never an action: one line
  per live question (`move up` / `move down` / `flat` / `hold`) with the
  knob's grid — `1 x | [2] 0.649 | 3 ok 0.612 | 4 .` — rejected, current,
  survives, untried. The questions are the program's own (PARAMETERS.md §11):
  one live knob per bucket, rotating through `knobs.KNOBS`; nobody files
  or picks a value by hand.

**The rejector is scheduled** — `docker/backplay.sh`, **04:00 every day but
Monday**, third line in `docker/crontab` beside the Monday cycle and the
nightly backup. Monday is skipped because a run can last twenty-one hours and
would otherwise still hold the heavy lock when the cycle starts (§1c). It
measures each step in its own subprocess under `TM_GATE_OVERRIDE` and may
**reject** — never promote — and it re-measures only when the evidence moved
(benchmark, store, champion fingerprint); most nights it prints what it stood
on and exits. Monday's report then carries the result in its **Knobs**
section, and `python backplay.py --show` prints the whole queue — grids,
rejections with reasons, closed questions with result lines — at any time.

**A proposal's forward evidence** — `shadow.py`, PARAMETERS.md §12 — comes
from the Monday cycle itself: every standing proposal is judged beside the
champion on that week's lots, and the disagreements wait for you to read them
blind:

```
docker compose run --rm tm python shadow.py --label
```

`i` = this lot is that customer's business, `o` = it is not, `s` = skip. When
enough are read the proposal line says `ready to promote`, `challenger loses`
or `bar breached`; `python shadow.py --show` prints the numbers.

To see the rejector work without waiting for the night:

```
docker compose run --rm tm python backplay.py --self-check
```

One second, synthetic numbers, no harness — it runs the real rejection rule
and prints what it decided, then the rejections standing today, the queue's
live questions and the evidence stamp. It proves the wiring, not the science.

To read the queue — what is live, what was tried, what was rejected and why:

```
docker compose run --rm tm python backplay.py --show
```

For a real measurement of some other knob, outside the queue:

```
docker compose run --rm tm python backplay.py --knob evidence.NOMINATION_BAR --grid 0.50,0.55,0.60 --current 0.55
```

That runs the gate harness once per neighbouring value (~25 minutes each)
under that value's own override, applies the rule, and writes real rows.
Rejections expire after 90 days and return as candidates — deliberate: a value
killed in one market is not dead forever.

To measure a candidate by hand, the same lever works anywhere:

```
TM_GATE_OVERRIDE='{"NOMINATION_BAR": 0.60}' python evidence.py --judge
```

A key no constant answers to stops the run. That is on purpose — an ignored
override would measure the champion and print the candidate's name.

Re-run a cycle without re-downloading (e.g. after a code change):

```
python cycle.py run --last 7d --skip-download
```

Render every customer's report to disk without mailing anyone (what
`preview_report.py` does for one customer):

```
python deliver.py run --no-mail
```

**Scheduled** ([`docker/crontab`](../docker/crontab), §1c): every **Monday
07:00** the cycle, followed by the simulation scorecard; every **Monday
11:00** the delivery. Four hours apart because the cycle takes 30–75
minutes and may wait up to an hour behind another heavy job on the lock; the
delivery waits behind the cycle on the same lock, so an early cycle overrun
delays the mail rather than skipping it, and a dead cycle stops it. The
simcheck log accumulates one dated block per week; watch the hit rate firm
up there as awards publish (~90-day median lag). Nothing else is scheduled by
design: calibration and backtests are event-driven (§4, §3), and the
embedding sidecar rides inside the cycle.

## 1b. The same two commands, in a container

Same commands, same outputs, none of the laptop's Python. Why it exists is
[`STORAGE.md`](STORAGE.md) 6.5; what to type is here.

```
docker compose build
docker compose run --rm tm python cycle.py run --last 7d
docker compose run --rm tm python deliver.py run
```

Every command in this runbook works with `docker compose run --rm tm` in front
of it — `python preview_report.py --sub beck --set max_picks=8`, `python evidence.py
--benchmark`, `python -m unittest discover -t . -s tests`. The image is the
stack, not the cycle.

**Two mounts, and they are the whole story.**

| mount | what | why it is a mount and not a layer |
| --- | --- | --- |
| `${TM_STATE:-./data}` → `/data` | this deployment's state: store, database, embeddings, reports, and `models/` | it has to outlive the container. Nothing else is writable |
| `tm-model-cache` → `/models_cache` | the 309 MB embedding model | a cache, re-downloadable, and no reason for a state backup to carry it |

`TM_STATE` defaults to `./data`, so a container started in the checkout finds
what the laptop finds. Point it at an absolute path outside the checkout and
nothing else changes — that is what 6.1 bought:

```
TM_STATE=C:\Users\user\workspace\tm-state docker compose run --rm tm python cycle.py run --last 7d
```

**Seed the model cache from this laptop rather than downloading it.** The model
is already in `%TEMP%\fastembed_cache`; copying it in means the first cycle in a
fresh container reaches HuggingFace zero times:

```
docker volume create tm-model-cache
docker run --rm -v tm-model-cache:/models_cache -v "%TEMP%\fastembed_cache:/host_cache:ro" --user root tendermining:latest sh -c "cp -r /host_cache/models--jinaai--jina-embeddings-v2-base-de /models_cache/ && chown -R 1000:1000 /models_cache"
```

fastembed logs `Local file sizes do not match the metadata` on a cache seeded
this way and then uses it anyway — a copied cache has no HuggingFace metadata
to compare against. Harmless; the vectors are byte-identical to the laptop's.

**Without compose**, which is what a host like Render or Railway will run:

```
docker run --rm -v C:\Users\user\workspace\tm-state:/data -v tm-model-cache:/models_cache tendermining:latest python cycle.py run --last 7d
```

## 1c. Monday 07:00 and 11:00, in the container

Two scheduled jobs ([`docker/crontab`](../docker/crontab)), since 2026-08-18
(the delivery moved from 08:30 to 11:00 on 2026-08-27, and backplay gave up
its Monday slot the same day — see below):

| when | script | what | log |
| --- | --- | --- | --- |
| Monday 07:00 | [`docker/cycle.sh`](../docker/cycle.sh) | `cycle.py run --last 7d`, then — *only if it succeeded* — a dated heading and the simulation scorecard | `data/logs/cycle.log`, `data/logs/simcheck.log` |
| Monday 11:00 | [`docker/deliver.sh`](../docker/deliver.sh) | `deliver.py run` — render, mail, record | `data/logs/deliver.log` |

Both append to `data/logs/cron.log` as well, which is what
`docker compose logs scheduler` tails. `deliver.sh` exits 2 when `deliver.py`
refused for staleness (no cycle within `--max-age`); cron.log then says
`DELIVERY REFUSED` and nobody was mailed — run `cycle.sh` by hand, then
`deliver.sh`, and the day is whole. Before 2026-08-18 there was one job,
`weekly.sh` at 08:15, doing both; `data/logs/loop_scheduled.log` is that
job's log, frozen where it stopped.

The schedule exists twice, and **only one of them may be switched on.**

**Option A — cron inside a container** ([`docker/crontab`](../docker/crontab)):

```
docker compose --profile scheduler up -d scheduler
docker compose logs -f scheduler
docker compose stop scheduler
```

**Option B — the Windows task keeps the trigger, the container does the work.**
Two `docker run`s replace the task's action line, one per script:

```
docker run --rm -v C:\Users\user\workspace\TenderMining\data:/data -v tm-model-cache:/models_cache tendermining:latest /app/docker/cycle.sh
docker run --rm -v C:\Users\user\workspace\TenderMining\data:/data -v tm-model-cache:/models_cache tendermining:latest /app/docker/deliver.sh
```

**On this laptop, B is the better one**, and it is not close. The existing task
does three things cron cannot:

| | Windows task | cron in a container |
| --- | --- | --- |
| laptop asleep at 08:15 | `StartWhenAvailable` — runs when it wakes | the Monday is simply skipped |
| on battery | will not start, stops if unplugged mid-run | runs regardless, flattens the battery |
| runaway cycle | `ExecutionTimeLimit` 6 h | runs forever |
| after a reboot | task survives | Docker Desktop `AutoStart` is **off**, so nothing is running |

A is the right shape the day this moves to a host that is always on — which is
where [`STORAGE.md`](STORAGE.md) 0 is heading, and it is why the service exists
now rather than being invented under time pressure later. It is behind a compose
profile so it cannot start by accident.

**Switching over, either way:** disable the old trigger first, in the same
sitting. Two schedulers appending to the same ledgers from two different Pythons
is the one outcome worse than no schedule at all.

```
Disable-ScheduledTask -TaskName 'TenderMining weekly loop'
```

Note the day numbering if you ever edit the schedule: cron's `dow` 1 is Monday;
the Windows trigger's `DaysOfWeek` 2 is the same day.

## 1d. The customer app

The web surface ([`APP.md`](APP.md)) runs from the same image and reads the
same `/data`, so a data change never needs a deploy — only a code change does.

```
docker compose up -d app          # http://localhost:8000/
docker compose logs -f app
```

Or without compose, which is what a host will run:

```
docker run -d --restart always -p 8000:8000 -v C:\Users\user\workspace\tm-state:/data tendermining:latest python app.py --port 8000
```

`TM_APP_PORT` moves the published port. `/healthz` answers `ok` plus the
cycle's last successful window and its age in days — that is the restart
check, and the fastest way to tell "the app is down" from "the cycle has not
run since Monday", which look identical from a customer's side.

**Nothing here listens on TLS.** The app is meant to sit behind a proxy that
terminates it (APP.md §9); it must never be the thing facing the internet
directly.

Tokens are minted, never typed:

```
docker compose run --rm tm python -c "import tokens; print(tokens.mint('/data','t','mueller-elektro'))"
```

`t` signup · `f` feedback (needs lot and verdict) · `s` stop · `c` recall. A
handler accepts only its own purpose, so a feedback link cannot act as an
unsubscribe link. Revoke with `tokens.revoke(...)`, or `tokens.revoke_all(...)`
for every live link one customer holds — which is what a hard stop needs, since
"stop sending" does nothing about the links already in their inbox.

## 2. Customers: add, change, render

A customer is lines in `data/subscriptions.jsonl` (private, gitignored,
append-only — never edit a line, append a higher `version`).

**Add a customer** — append one line. With win history, `profile_refs` are the
publication numbers of their won tenders' contract notices; without history,
one `profile_texts` sentence does it:

```jsonl
{"sub_id": "mueller-elektro", "version": 1, "effective_from": "2026-08-10",
 "name": "Müller Elektrotechnik GmbH",
 "cpv_prefixes": ["45"], "nuts_prefixes": ["DE2"],
 "profile_refs": ["00123456-2026", "00234567-2026"],
 "min_relevance": 0.482,
 "min_deadline_days": 14, "max_picks": 5, "avoid_n": 5, "active": true}
```

Leave `min_relevance` at the receipt default (see §4) unless feedback says
otherwise. Omitting it entirely disables the relevance gate for that customer
(pure CPV/region filter, old behaviour).

**Change a customer** (new trade reference, new threshold, wider region) —
append the same `sub_id` with `version: n+1` and a new `effective_from`.
**Deactivate** — append a version with `"active": false`.

**Render** — rendering is `deliver.py`'s job, and it renders every active
subscription (all of them, in milliseconds — one run, many views). To see a
customer's report *now* after editing their line, without mailing anyone:

```
python deliver.py run --no-mail
```

and open `data/reports/subscriptions/<sub_id>/report_<date>.html`. There is
deliberately no per-customer switch: every render also appends the delivery
ledger rows that make the track record auditable, and those must stay
complete for every active customer. (For one customer against a sandbox,
`preview_report.py`, §3.)

## 3. Testing a change against the pilot

Seven committed tools, ordered by cost. Reach for the cheapest one that
answers your question; none of them touches the real ledgers or reports.

| Question | Run | Cost |
| --- | --- | --- |
| What would customer X's report look like if a subscription field changed? | `python preview_report.py --sub <sub_id> --set FIELD=VALUE` | seconds |
| Why did lot Y pass / fail X's gate? What trade does its text read as? | `python explain_verdict.py --sub <sub_id> <TED-number> …` | seconds |
| Would we have recommended this firm's historical solo win, knowing only the past? | `python rewind_win.py --firm "Firma GmbH"` | ~10 min |
| Show me a real prediction report AND the later report that checks it (the "Rückblick" demo) | `python rewind_report.py --sub <sub_id> --cutoff YYYY-MM-DD` | ~15 min |
| Does the gate still judge every hand-labeled case correctly? | `python evidence.py --benchmark` | seconds |
| Is a customer's lexicon made of trade words — and what does a lexicon change cost in recall vs precision? | `python lexicon_receipt.py --lexicons` | ~5 min |
| Do the model's CPV columns still earn their keep — would deeper or shallower codes score better? | `python cpv_depth_receipt.py` (`--quick` for a first look) | ~3 min `--quick`, ~10 min full |
| Does a gate/model change make picks better overall? | `python rewind_all.py` | hours |

- **`preview_report.py`** re-renders one customer from the last cycle's prediction
  ledger inside a disposable sandbox (`data/tryout/<sub_id>/`, recreated per
  run) and prints the picks with their gate scores. `--set` is repeatable
  (`--set min_deadline_days=0 --set min_relevance=0.6`); `--keep-expired`
  also shows lots whose deadline has passed. The real subscription file is
  never modified — overrides live only in the sandbox.
- **`explain_verdict.py`** prints the profile fingerprint (hard/soft labels), each
  lot's pass path through the gate grid, and its text→label projections.
  With no TED numbers it explains the profile references themselves — the
  sanity check that a profile reads as the customer's trade.
- **`lexicon_receipt.py`** runs `benchmark_relevance.jsonl` through the real
  `judge()` under each lexicon switch (`base` / `buyers` / `roots`) and
  reports IN (should pass) and OUT (should be rejected) **separately** — the
  benchmark total hides direction, and a change that stops wrong-trade
  picks reads as a regression when 74 of 126 cases are recall cases.
  `--lexicons` also prints every firm's word list, because the operator's
  test for a lexicon is reading it: each word should name a Gewerk or a
  material. The vocabulary itself is [`cpv_trade_roots.txt`](../cpv_trade_roots.txt),
  written by hand — CPV lacks the materials and the regional trade names
  (Schreiner, Spengler, Flaschner), and the embedder cannot supply them
  (measured: linoleum↔bodenbelag 0.108, at noise).
- **`rewind_win.py`** rebuilds an as-of world before the target's deadline
  (store, trust list, thresholds, model — all time-isolated) and replays
  that cycle: was the win in the market, was it a pick.
- **`rewind_report.py`** renders TWO real customer reports across time: the weekly
  report as it would have looked at `--cutoff` (picks by a model that could
  not see past that date), and a check report at `--check-date` (default
  today) whose "Ihre Empfehlungen im Rückblick" grades those picks against
  the since-published outcomes. Output under `data/replay/<sub_id>/`; real
  ledgers untouched. Pick a cutoff 3–6 months back (awards lag ~3 months);
  the backtest report lists pick weeks with outcomes if you want a
  guaranteed-graded cutoff. This is the sales/demo artifact for "how do I
  know your predictions are any good".
- **`cpv_depth_receipt.py`** regenerates the numbers behind the CPV depth
  decision (TRAINING.md): cardinality against the one-hot cap per level, the
  spread of cpv6 rates inside a cpv4 bucket against a permutation null, an A/B
  retrain of the shipped feature build vs shallower and deeper variants across
  seeds and split dates, and the shuffled-label and too-good tripwires on the
  shipped arm. Nothing is registered and no champion is touched; `--out PATH`
  also writes the receipt as markdown. Run it when the feature encoding is
  questioned or after the scope widens beyond CPV 45 — the cardinalities in
  section 1 are what stands between `cpv_additional` and a silent CTR
  fallback.
- **`rewind_all.py`** replays every weekly cutoff and grades all picks
  against published outcomes on two axes — did the lot end with 0-1 bids,
  and did the firm the pick was handed to eventually win it themselves
  (any bidder count). **It writes no file and owns no path**: the run
  produces one JSON document on stdout, which you name
  (`TRADE_PAGES.md` §6d). Everything readable renders that document —
  `--render` for the prose, `trade_pages.py --replay` for the pages:

  ```
  python rewind_all.py > run-2026-08-11.json     # ~33 min, 46 cutoffs
  python rewind_all.py --render run-2026-08-11.json
  python trade_pages.py --replay run-2026-08-11.json
  ```

Rule of thumb: after editing a subscription, `preview_report.py`; when a verdict
surprises you, `explain_verdict.py`; before shipping a gate change, `rewind_all.py`
(and `calibrate.py` for the receipt); before shipping a FEATURE change,
`cpv_depth_receipt.py` — and remember it is a flag day: the champion cannot
score a build whose columns changed, so `learn()` promotes the candidate
unconditionally that cycle (TRAINING.md).

## 4. The study side: embeddings, calibration, trust

These are **event-driven, not scheduled**. The loop reads their last
committed output; nothing waits on them.

| When | Run | Writes |
| --- | --- | --- |
| automatically each cycle | (inside `cycle.py run`) | new lot vectors, `data/embeddings/<tag>/` |
| after changing the embedding model; after a big backfill; else ~monthly | `python calibrate.py` | `calibration_<tag>.md`, `trusted_codes_<tag>.json` (committed result lines) |
| curiosity / sales prep | `python calibrate.py --fingerprint "Firma GmbH"` | console only: the firm's named trades |

**Reading the receipt** (`calibration_<tag>.md`): the configuration table's
last rows are the shipping gate; "leakage" = share of wrong-trade lots that
would pass; "volume" = share of the whole market the average profile
admits. If a recalibration moves the defaults, update the `DEFAULT_*`,
`SOFT_*` and `TRADE_READ_*` constants in `relevance.py` in the same commit
as the receipt, and append new subscription versions for customers with an
explicit `min_relevance`.

**Rebuild everything from scratch** (new machine; sidecar deleted):

```
python embed.py --labels        # full backfill: ~40 min labels+lots MiniLM,
                                # several hours jina — checkpointed every
                                # 1000 lots, safe to interrupt and re-run
```

## 5. Switching the embedding model

The active model is the committed default `MODEL_TAG` in `embed.py`; the
`EMBED_MODEL` env var overrides it per-run so a new sidecar can build while
the old one keeps serving:

```
EMBED_MODEL=<new-tag> python embed.py --labels    # 1. full backfill, background-able
EMBED_MODEL=<new-tag> python calibrate.py         # 2. result lines for the new tag
# 3. compare result lines; if the new model wins:
#    - flip the default MODEL_TAG in embed.py
#    - update the two DEFAULT_* thresholds in relevance.py from the new receipt
#    - append new subscription versions for customers with explicit min_relevance
#    - commit code + result lines together, push
```

New models must first be added to the `MODELS` registry in `embed.py`
(name + dimensions). Old sidecars stay on disk untouched — a flip is one
reviewable commit, and rolling back is flipping the constant back.

**At every model flip, also re-measure the strip variant** (phase 6,
currently OFF — decision 2026-08-06): add a `<new-tag>-strip` entry and
run its backfill + calibration like any candidate. Under
`jina-v2-base-de` it lost to phase-5 corroboration (1.9% vs 1.5%
leakage; the two attack the same template noise), but that verdict is a
property of the model, not of the idea — a different model may leave
template noise the corroboration cannot see.

## 6. Where things live (quick reference)

| Path | What | In git? |
| --- | --- | --- |
| `data/store/*.parquet` | the two tables (tenders, awards) | no (rebuildable) |
| `data/embeddings/<tag>/` | vectors: lots + CPV labels, per model | no (rebuildable) |
| `data/subscriptions.jsonl` | customers, versioned | **no — private** |
| `data/outreach/targets.csv` | cold-contact target list (§7) | **no — private** |
| `data/ledger/*.jsonl` | predictions, grades, deliveries, simulations (append-only) | no |
| `data/reports/…` | operator report, dashboard, customer HTML | no |
| `calibration_<tag>.md`, `trusted_codes_<tag>.json` | study result lines | **yes** |
| `cpv_2008_de.csv` | official CPV dictionary (German) | yes |
| `embed.py` / `calibrate.py` / `relevance.py` / `cycle.py` / `deliver.py` / `simulation.py` | the programs | yes |
| `preview_report.py` / `explain_verdict.py` / `rewind_win.py` / `rewind_all.py` | the test tools (§3) | yes |
| `outreach.py` | target-list builder (§7) | yes |
| `data/tryout/`, `data/asof/` | disposable test sandboxes (as-of worlds: `asof.py`) | no |

## 7. Outreach: the cold-contact target list

The go-to-market side ([`GO_TO_MARKET.md`](GO_TO_MARKET.md)). One command
rebuilds the list of small repeat-winner firms with their contact details,
win history and current simulated-pick volume:

```
python outreach.py                    # small/micro, >=2 wins -> data/outreach/targets.csv
python outreach.py --sizes small micro medium --min-wins 1
```

Re-run after a backfill or when the awards store has grown (add `--rescan`
then — contact details are cached in `data/outreach/contacts.json`). The
CSV is private (personal data, gitignored). Campaign segmentation uses the
`trade_read*` columns (what the firm's won tenders *read as*, via the
embedding sidecar), not the won-lot CPV codes — a buyer's filing choice
can put a hygiene firm under 452; the text cannot. `trade_match == True`
is the mailable set; disagreeing rows go to hand review first. The
`profile_refs` column is paste-ready for a subscription line (§2): they are
contract-notice numbers, derived from each won lot key through the embedding
sidecar, not the award numbers the win itself carries. `profile_refs_n` says
how many actually came out — a firm can have fewer refs than wins (a won lot
whose contract notice is not in the sidecar, or several lots announced by one
notice), and below 2 the profile is thin. Which trades to campaign in comes from
the backtest's per-trade table (§3); the channel decision (letters, not
e-mail — §7 UWG) is documented in GO_TO_MARKET.md.
