# PARAMETERS — the register, and the rules for touching a knob

Written 2026-08-16 from the operator's question in this session: "there are
so many parameters that I lose my overview — can I use Google's A/B method
to find the optimal combination?" The answer given was **no experiment
infrastructure at this scale; a register, a freeze, and one channel per
model** — this file is that answer as a spec. Companion to
[`EXPERIMENTS.md`](EXPERIMENTS.md) (forward shadow arms for the
competitiveness model, in progress in another worktree — nothing here edits
it; §6 says what to add there once it has merged) and
[`RELEVANCE.md`](RELEVANCE.md) (the result lines every gate value cites).

## 0. The rules

1. **Every knob belongs to exactly one bucket** — *gate* ("is this my
   business"), *competitiveness* ("is this a low-contested tender"),
   *delivery* (what a customer sees of the two verdicts) or *monitoring*
   (when the system speaks up). The two models share no metric, no truth
   source and no cadence, so they never share a knob.
2. **Every knob is either FROZEN or LIVE.** Frozen: set once, moved only
   with a written reason, never "tuned". Live: has a benchmark, a receipt,
   and may move. The register (§2) is the list; a knob not in it is a bug in
   this file, not a knob.
3. **One live knob per bucket at a time.** Google's layers, reduced to one
   operator: never move two knobs of the same bucket in the same week. Two
   knobs that must move together are one knob (a named configuration, as the
   result lines already do with their letters H, K, …).
4. **The stamp must be honest.** Everything that changes a gate verdict is in
   `GateConfig.fingerprint`; everything that changes a competitiveness score
   is resolvable from `model_id` via `meta.json`. A knob that can move a
   verdict without moving the stamp is the first thing §4 fixes.
5. **Backplay may reject, only forward may promote.** The as-of engine
   ([`asof.py`](../asof.py)) is honest by construction on data leakage; what
   it cannot prevent is the operator having chosen a knob on the same lots it
   is then scored on. So a backplay result kills a candidate or ranks a
   coarse grid, and never by itself moves a live value into production.
6. **The register is prose, in this file, kept by hand.** No generator, no
   report file (memory: tools print to console). Amending it is part of the
   same commit that moves the value.

## 1. Where the ~70 tunables live today

| place | count | mechanism | stamped? |
| --- | --- | --- | --- |
| `loop.py run` CLI args | 27 | argparse defaults; cron passes none | `threshold` on every prediction row; the rest not at all |
| `relevance.py` constants → `GateConfig` | 17 | dataclass with fingerprint; three read env vars at import | yes — `gate_config` on delivery rows since 2026-08-08 |
| `evidence.py` constants | ~20 | module globals | **no** — not in the fingerprint |
| `calibrate.py` constants | 10 | module globals; produce the trust list | indirectly (trust list file *name* is in the fingerprint, its contents are not) |
| `single_bidder.py` constants | 5 | module globals | `model_id` → `meta.json` records features, threshold, n_rows; not the constants |
| `render.py`, `subscriptions.py` | 2 | module globals | no |
| per-subscription overrides | 5 fields | `subscription_version` row | yes — `sub_version` on delivery rows |

The overview was lost because these are seven mechanisms in seven files
with three different ideas of "recorded". §2 puts them in one table.

## 2. The register

Legend — **bucket**: G gate · C competitiveness · D delivery · M monitoring ·
O operational (not a knob, listed so nothing is missing). **status**:
FROZEN / LIVE / DEAD (inert under the current mode) / ROLLBACK (kept only so
`GATE_MODE='embedding'` still runs). **stamped**: whether the value is
recoverable from a ledger row written under it.

### 2.1 Gate — `relevance.GateConfig`

| knob | value | status | stamped | receipt |
| --- | --- | --- | --- | --- |
| `mode` (`GATE_MODE`, env) | `evidence` | FROZEN | fp | RELEVANCE.md phase 8, 2026-08-06 |
| `evidence_nomination_min` | 2 | DEAD while `conviction_nominates` is on (queue run 2026-08-16, §11.5) | fp | phase 8e, K≥2 vs K≥3 receipt |
| `conviction_nominates` (env) | on | FROZEN | fp | phase 8k, 79/126 |
| `similarity_nominates` (env) | off | FROZEN | fp | phase 8i, four reasons |
| `borderline_admit_p` | 0.0 | FROZEN (placeholder for an LLM reader) | fp | phase 8d |
| `nomination_bar` | 0.550 | DEAD while `similarity_nominates` is off | fp | phase 8 sweep |
| `min_relevance` | 0.700 | ROLLBACK (per-sub override still honoured) | fp | configuration H |
| `min_code_hard` | 0.825 | LIVE — used by route 1 in evidence mode too | fp | configuration F |
| `min_code_soft` | 0.725 | ROLLBACK | fp | configuration F |
| `soft_floor` / `soft_consensus` | 0.45 / 2 | ROLLBACK | fp | phase 5 |
| `use_expansion` | off | ROLLBACK | fp | configs C/D vs E |
| `borderline_margin` | 0.05 | ROLLBACK (rendering of "knapp aussortiert") | fp | configuration G |
| `trade_read_form` / `trade_read_param` | H2 / 0.0 | ROLLBACK | fp | phase 5 |
| `trade_talk_margin` / `trade_branches` | 0.225 / (453, 454) | ROLLBACK | fp | configuration K |
| `model_tag` | `jina-v2-base-de` | FROZEN | fp + `embed_model_tag` | calibration_<tag>.md |
| `trusted_codes` | `trusted_codes_<tag>.json` | FROZEN (regenerated by calibrate.py) | fp by *name* only | — |

### 2.2 Gate — `evidence.py` (verdict-affecting; stamped since 4.1 landed)

The full list is `evidence.RULES` (37 names) — the code is the register for
this table, and a test asserts every module-level constant is either in
`RULES` or in `NOT_RULES`. Grouped:

| group | knobs | status | stamped |
| --- | --- | --- | --- |
| lexicon sieve | `MIN_STEM_LEN 6`, `MAX_KEYWORDS 25`, `MAX_DOC_FREQ 0.02`, `LABEL_DF_MAX 5`, `WORD_MIN_REF_SHARE 0.34`\* | FROZEN | fp (`evidence_rules`) |
| matching tiers | `MIN_WITNESSES 1`, `TYPO_MIN_LEN 8`, `SYN_THRESHOLD 0.80` | FROZEN | fp |
| conviction | `CONVICT_BODY_MIN 2`, `CORE_TITLE_CONVICTS`\*, `CORE_SHARE 0.5`\*, `CORE_SINGLE_TITLE`\*, `CORE_NEED_BEARING`\*, `CORE_FAMILY`\*, `CORE_TITLE_FALLBACK`\*, `CORE_HYSTERESIS`\* (off), `CORE_TITLE_UNION`\* (empty), `LONE_TITLE_NEEDS_CODE`\*, `TITLE_CONTRADICTS_BODY`\*, `TRADE_READ_FORGIVES`\* (off) | `CONVICT_BODY_MIN` LIVE; rest FROZEN | fp |
| nomination | `WIDE_NOMINATION`\*, `NAME_KEYWORDS`\*, `ROOT_LEXICON`\*, `TRADE_ROOTS`\* | FROZEN | fp |
| trade dictionaries | `TRADE_DICTS`, `DICT_MIN_LOTS 20`, `DICT_TRUSTED_ONLY`\* (off), `DICT_CODE_SHARE 0.34`\*, `DICT_VOTE`\*, `DICT_VOTE_MARGIN 0.5`, `DICT_VOTE_MAX 6`, `DICT_MIN_IN 0.10`\*, `DICT_MIN_RATIO 8.0`, `DICT_MIN_BUYERS 2`, `DICT_MIN_BUYER_SHARE 0.10`, `BUYER_DIVERSITY`\*, `DICT_MAX_WORDS 30` | FROZEN | fp |
| not rules (`NOT_RULES`) | `DICT_CACHE_V 6` (bump when any dictionary knob moves), `SEED 7`, `NEG_PER_FIRM 50`, `VOL_PER_FIRM 200`, `MIN_WINS 3`, `SWEEP_BARS`, `ROOTS_FILE`, `KEY` | O | n/a |

\* = read from an environment variable at import; on/off ones default on
unless marked. Twenty of the 37 are env-driven — the reason §4.3 prints the
resolved configuration every cycle.

Before 4.1 every one of these could move while `gate_config = 7d29fa0dce`
stayed on every delivery row. Since 4.1 the default fingerprint is
**`7931c8e9cd`** (rules hash `a62e07fda4`); a delivery row carrying the old
value was judged before the evidence rules were stamped, under whatever
`evidence.py` said at that commit — git is the record for those.

### 2.3 Gate — `calibrate.py` (produce the trust list; contents unstamped)

`MIN_WINS 3`, `RECALL_TARGET 0.90`, `NEG_PER_FIRM 50`, `VOL_PER_FIRM 200`,
`TRUST_MIN_LOTS 10`, `TRUST_MARGIN 0.15`, `COHESION_SAMPLE 60`,
`BASELINE_SAMPLE 800`, `PSEUDO_REF_CAP 200`, `FP_K 8` — all FROZEN. Their
output is the committed `trusted_codes_<tag>.json`; git history is the record
of its contents (as `GateConfig.fingerprint`'s docstring already says). No
change proposed.

### 2.4 Competitiveness — `loop.py run` and `single_bidder.py`

| knob | value | bucket | status | stamped |
| --- | --- | --- | --- | --- |
| `--threshold` | 0.5 | C | LIVE | prediction row + `meta.json` |
| `--promote-epsilon` | 0.005 | C | FROZEN | `meta.json`.gate |
| `--val-window` | 8w | C | FROZEN | no |
| `--min-val-lots` | 30 | C | FROZEN | no |
| `--min-shuffle-positives` | 20 | C | FROZEN | no |
| `--iterations` | None | O (testing) | — | no |
| `ONE_HOT_MAX_SIZE` | 1024 | C | FROZEN | no |
| `MULTIHOT_MIN_SUPPORT` | 30 | C | LIVE (new 2026-08-16) | no |
| `SEED` | 42 | C | FROZEN | no |
| `LABEL_MAX_TENDERS` | 1 | C — *the label definition* | FROZEN | no (implicit in every grade) |
| `TOO_GOOD_ROC` | 0.85 | M | FROZEN | no |
| `--cpv`, `--country`, `--last` | 45 / DEU / 7d | O — scope | FROZEN | no |

Encoding choice (`feature_build`) becomes a stamped arm parameter under
EXPERIMENTS.md; the CatBoost hyper-parameters are whatever `sb.make_model`
hard-codes and are recoverable from `model.cbm` — FROZEN, not listed.

### 2.5 Delivery

| knob | value | status | stamped |
| --- | --- | --- | --- |
| `--tier-high` / `--tier-medium` | 0.10 / 0.20 | FROZEN | `tier` on prediction rows (the outcome, not the share) |
| `--top-slice` | 0.2 | FROZEN (metric definition) | no |
| `--report-top` | 30 | FROZEN | no |
| `--sim-max-picks` / `--sim-min-deadline-days` | 5 / 14 | FROZEN (mirror the product defaults) | no |
| `subscriptions.DEFAULT_MAX_PICKS` | 5 | FROZEN, decision 2026-08-04 | `sub_version` |
| `render.MAX_RECEIPTS` | 15 | FROZEN | no |
| per-sub `min_deadline_days`, `max_picks`, `min_relevance`, `min_code_hard`, `min_code_soft` | per row | LIVE per customer | `sub_version` |
| `embed.MAX_CHARS` | 2000 | FROZEN (also affects gate input) | no |

### 2.6 Monitoring

`--track-window 12w`, `--min-trade-grades 25`, `--min-flag-grades 30`,
`--min-slice-grades 25`, `--drift-window 4w`, `--drift-min-lots 30`,
`--missing-jump 0.15`, `--psi-warn 0.25`. All FROZEN. They decide when a
number is quoted or a warning printed; none changes a verdict or a delivery.
Nothing to optimise; nothing to stamp.

### 2.7 The count that matters

Of ~95 tunables (the first count of ~70 missed twenty env-driven switches in
`evidence.py`; `RULES` is now the authority there), **LIVE: 4** —
`min_code_hard`, `CONVICT_BODY_MIN` (gate); `--threshold`,
`MULTIHOT_MIN_SUPPORT` (competitiveness) — plus per-customer overrides.
ROLLBACK: 9. DEAD: 2 (`nomination_bar`; `evidence_nomination_min` since the
queue's first run, §11.5). Everything else is frozen or operational.
That is the overview. Note that "LIVE" here is a *description* of what has
been moved recently; under §8 a knob is LIVE only while a filed question is
open, so at the time of writing all five are formally FROZEN until one is
filed.

## 3. One channel per model

| | gate | competitiveness |
| --- | --- | --- |
| truth | a human reading the lot | `n_tenders` on the award notice, ~3 months after deadline, free |
| benchmark | [`benchmark_relevance.jsonl`](../benchmark_relevance.jsonl), 1,779 lines, committed, hand-read (memory: labels by the operator, never by code) | the graded ledger; 99k predictions from 23 models written before their awards, 18 graded so far, the rest arriving from October |
| **reject** with | `evidence.py --sweep` / `--judge` on the frozen benchmark; `rewind_*` for a customer's Monday | as-of retrain per cutoff (`asof.World`), paired on lots both arms scored |
| **promote** with | shadow-judge every live lot with the challenger; the disagreements are the next labels; per-customer feedback is a sanity check, never the verdict | EXPERIMENTS.md forward arms |
| hard bar | wrong-trade leakage ≤ 2.2 %, in both channels, binding in forward | precision at the delivered cutoff (EXPERIMENTS.md §13) |

Two disciplines that make the reject channel trustworthy:

- **Blind labelling.** When the operator reads a lot to grow the benchmark,
  the case must not say which configuration flagged it. Otherwise the
  benchmark drifts toward the champion's opinions and every later sweep
  rewards the champion for agreeing with itself.
- **Moving cutoff.** A gate value tuned on the benchmark, or a
  competitiveness knob tuned on cutoffs D₁…Dₙ, is confirmed on a held-out
  Dₙ₊₁ nobody looked at before it counts as more than a rejection.

## 4. Code changes this spec asks for

Small, gate-side, and in files no open worktree holds (`relevance.py`,
`evidence.py`, their tests). Each is one commit; the register row moves in
the same commit.

**4.1 Honest fingerprint — done 2026-08-16, as a snapshot.** `evidence.RULES`
names every verdict-affecting constant; `GateConfig.evidence_rules` snapshots
their live values at construction and they enter `as_dict()` and the
fingerprint. `DICT_CACHE_V` and the sweep-sampling constants are `NOT_RULES`
(cache and sampling are not verdicts). Default fingerprint moved once,
`7d29fa0dce` → `7931c8e9cd`, recorded in §2.2; `describe()` now also prints
`rules=<hash>`. **Not done, on purpose:** making `evidence.py` read the
values from the config it is handed. That is a refactor of a 2,000-line
module whose lexicon caches key on these constants, for a benefit (two
evidence configurations in one process) nothing needs yet; the honest stamp
is what rule 4 asked for and the snapshot gives it. When a sweep needs two
rule sets side by side, that is the trigger — and REFACTOR.md's
`relevance`↔`evidence` knot is where it belongs.

**4.2 One benchmark, one denominator — done 2026-08-16.**
`evidence.benchmark_cases()` is the only loader; the first call per process
prints `[benchmark] <file> blob <git blob hash> cases <n> seed <s> rules
<hash>`, and `--sweep`, `--judge` and the two other readers go through it. Receipts quoted in RELEVANCE.md and here name
that line. Different denominators (the 19-lot hard set, the 103-lot grown
set, the 2473/25600 synthetic sample) stay — but a receipt that mixes them
in one row is not a receipt. No new file: the benchmark is already committed;
this only makes every number say which one it stands on.

**4.3 Live-config assertion — done 2026-08-16.** `loop.py` prints
`[config] gate: <describe()>` at the top of every cycle; `/healthz` shows
`gate_config=<fingerprint>` read from the gate-config ledger (what customers
were last served under — the app image does not carry the gate's
dependencies, and recomputing would answer a different question). Purpose: the three env-driven knobs can be flipped by a stray
variable in cron's environment; today nothing would say so. One line each.

**4.4 `knobs.py` — the weekly proposal and the guard — done 2026-08-16.**
A module of its own, as specified. `LIVE` holds hand-filed questions and is
**empty**; since §11 the questions come from the queue, which the program
keeps itself (a hand-filed `Question` — id, knob, bucket, question, metric,
benchmark blob, grid, current, opened, stop, its own `run` — still takes its
bucket's slot). `weekly()` returns one
line per live question — `move up` / `move down` / `flat` /
`hold (underpowered)` / `stop date reached` — with the detail, the weeks
live and the stop date; `loop.py` prints them and `report.py` carries them
under a **Knobs** heading. `gate_guard()` is §8.3's blocking half.

Three decisions taken while building it, all narrowing:

- **No general sweep engine.** A question supplies its own `run`; the two
  harnesses that exist (`evidence.py --sweep`, `--judge`) already measure the
  gate on the frozen benchmark, and inventing a third mechanism before a real
  question exists would be a guess about what that question needs.
- **The guard skips delivery only.** Grading, training and prediction have
  already run and been written when it fires, so a mismatch costs a week's
  customer reports, never a week's data.
- **The guard's message is a diff, not two hashes.** `record_gate_config`
  stored the whole configuration, so when the register's fingerprint was
  recorded by an earlier cycle the message names the knobs that differ
  (`evidence.SYN_THRESHOLD: 0.8 -> 0.99`).

`EXPECTED_GATE_FINGERPRINT` lives in `knobs.py` and `tests/test_parameters.py`
reads it from there — one value to update in the ritual's commit, not two.
`tests/test_knobs.py` covers the verdict grid (clear winner up and down,
overlapping intervals are flat, thin denominators hold, a hard-bar breach is
barred whatever recall it buys, the stop date wins, flat twice closes), the
flat-streak surviving between cycles, a failing sweep never failing a cycle,
and the guard in all three states.

Not asked for: retiring the ROLLBACK grid (cheap to keep, and it is the
tested rollback of a decision only ten days old); a generator for §2; a
settings file; anything customer-facing.

## 5. What is *not* built, on purpose

Layered traffic assignment, per-customer experiment config, bandits, an
experiments dashboard beyond EXPERIMENTS.md's page. With 100 deliveries and
18 graded lots, any live split is wider than the effect it hunts. Revisit
when there are hundreds of active customers and a feedback rate worth
counting; the schema already carries `model`, `gate_config`, `sub_version`
on every row, so nothing has to be re-instrumented then.

## 6. Added to EXPERIMENTS.md once the `ab-arms-spec` worktree merged (done)

Both landed 2026-08-16. Under its §0: *backplay (`asof.World`,
`rewind_all.py`) may reject an arm before it earns a shadow slot; only
forward grades promote.* Under its §6: `arm_grade` **is** the per-(lot,
model) grading this file's §3 wants — drop `experiment`/`arm` and it is
(lot, model) → correct — so the 3,942 historical lots scored by more than one
model are a backfill of that writer, not a twin table.

## 7. Decisions taken in this session

| decision | who | value |
| --- | --- | --- |
| Google-style experiment infrastructure now | assistant, operator agreed | no — labels are the bottleneck, not plumbing |
| unit of the register | assistant | one prose table, this file, kept by hand |
| what counts as evidence | assistant, extends EXPERIMENTS.md §0 | backplay rejects, forward promotes |
| gate knobs outside the fingerprint | assistant | fold into `GateConfig` (§4.1) |
| ROLLBACK grid | assistant | keep, mark, do not tune |
| where the two-model split lives | assistant | the bucket column; no shared knob ever |

## 8. The protocol — how a knob moves, agreed 2026-08-16

The operator asked not to be the trusted instance ("I don't trust
myself"). So the value is never anyone's to pick: the software proposes from
a grid, the operator answers yes / hold / roll back, the assistant session
executes. Four rules.

### 8.1 Status grid — one step at a time, only these transitions

```
FROZEN   --(question filed)---------------> LIVE
LIVE     --(receipt: flat, or decided)----> FROZEN
LIVE     --(receipt: harmful)-------------> ROLLBACK   (old value kept, switch off)
ROLLBACK --(90 days unused)---------------> retired    (constant deleted; ledger keeps the stamp)
any      --(controlling switch off)-------> DEAD       (a fact, not a decision)
```

- **Going LIVE needs a filed question**: one sentence, the metric, the
  benchmark by blob hash, the grid, a stop date. No question, no tuning.
  One live knob per bucket at a time.
- **Going FROZEN needs a receipt**: "flat" (grid values inside each other's
  interval) or "decided" (value chosen, numbers cited). A frozen value is
  not "tweaked"; it goes through LIVE again.
- **DEAD** is whatever the controlling switch says; the register marks it.
- **Retired** after 90 days on ROLLBACK unused — the grid must not grow
  forever. (Operator default; may be overruled per knob in the receipt.)

### 8.2 Drifting a knob — the move rule

- Along the grid, **one step per weekly cycle**, never a jump to "the
  optimum" (a jump that looks great was fitted to the benchmark).
- **Backplay rejects, forward promotes** (§0.5): the sweep picks the
  candidate; it ships after one full cycle in shadow — gate: shadow-judge
  live lots, operator labels the disagreements blind; competitiveness:
  EXPERIMENTS.md arm — with wrong-trade leakage under 2.2 % *on live lots*.
- **Confirmed on a held-out** before promoting. Competitiveness: one cutoff
  date not used to pick the value. Gate (no time axis in the benchmark):
  the disagreements labelled *after* the value was chosen, i.e. the next
  cycle's blind labels — a sealed benchmark slice is not asked for.
- Every move is one commit changing exactly three things: the constant, its
  receipt comment, the register row. The fingerprint moves by itself. A move
  missing any of the three is reverted.

### 8.3 Roles — no single instance is trusted

- **Software proposes** (`knobs.py`, §4.4): per LIVE knob, the sweep at the
  neighbouring grid values on the frozen benchmark and a verdict line —
  `move up / move down / flat / hold (underpowered)` — with interval and
  stop date. Live knobs only; frozen ones are not re-swept weekly. It never
  edits.
- **Software blocks**: the cycle refuses to deliver when the resolved gate
  fingerprint is not in the gate-config ledger with a receipt (a constant
  edited without the ritual); leakage above 2.2 % on graded live lots turns
  the report line red and is written to the register as "bar breached,
  cycle N". Flipping the switch stays the operator's.
- **The operator decides only among the software's options**: accept the
  step, hold, roll back. Never "set it to 0.61".
- **The assistant session executes**: commit, register row, fingerprint
  check, tests. It does not choose the value either.

### 8.4 Stop conditions — a live question always ends

On the first of: the stop date; "flat" two cycles running; a decided value
confirmed on the held-out. Ending writes the receipt and returns the knob to
FROZEN. The weekly line says "live for N weeks, stop date D" so an extension
cannot happen silently.

## 9. `loop.py` does too much — the split (operator, 2026-08-16; done the same day)

1,541 lines, nine sections, one file that is downloader, grader, trainer,
predictor, deliverer, housekeeper, drift monitor, reporter and CLI. Phase 4
split `deliver()` downward into `selection.py` and `render.py`; nothing has
split `loop.py` sideways. §8's proposal machinery must not become a tenth
section, so the split comes first.

**Target**: `loop.py` is an orchestrator — paths, checkpoint, the ordered
calls, the CLI — of about 200 lines. Each step becomes a module named for
what it does, keeping its functions and docstrings verbatim:

| today (section) | module | public entry |
| --- | --- | --- |
| step 1 download | `download.py` exists; the wrapper `download()` moves there | `download.run(paths, args, checkpoint)` |
| step 2 grade + Wilson + flag stats + track record | `grading.py` | `grade`, `track_record`, `wilson`, `flag_stats` |
| step 3 learn + promote + champion | `training.py` | `learn`, `current_champion` |
| pick reasons + step 4 predict | `predicting.py` | `predict_open`, `explain_rows` |
| step 4b deliver + gate-config record + learned refs | `delivering.py` | `deliver`, `record_gate_config`, `learn_references` |
| housekeeping | `housekeeping.py` | `prune_caches` |
| drift monitors | `drift.py` | `drift_monitors` |
| step 5 report | `report.py` | `report`, `flag_view_lines` |
| small utils | `util.py` (or `config.py` where they already half-live) | `parse_window`, `now_utc`, `read_json`, … |

Rules for the move: behaviour-preserving, one module per commit, tests moved
with their functions, `loop.py` re-exports nothing (callers such as
`preview_report.py`, `rewind_report.py`, `render_dashboard.py` import the new
module — grep first). `_run_cycle` reads, afterwards, as the list above in
order. Sequenced **after the `ab-arms-spec` worktree merges** — it holds 200
changed lines of `loop.py` and a split under it would be a merge nobody
wants — and recorded in REFACTOR.md as phase 6.

### What actually happened, 2026-08-16

Eight commits, one per module, 340 tests green after each. `loop.py`: 1,541
→ 284 lines.

| module | lines | what moved |
| --- | --- | --- |
| `util.py` | 100 | `parse_window`, `now_utc`, `read_json`, `write_json`, `read_jsonl`, `stamp`, `append_jsonl`, `Paths` |
| `grading.py` | 245 | `grade`, `wilson`, `flag_stats`, `track_record`, `_top_slice_stats`, `SECTOR` |
| `training.py` | 249 | `learn`, `current_champion` |
| `predicting.py` | 231 | `predict_open`, `explain_rows`, the `WHY_*` phrase book |
| `delivering.py` | 173 | `deliver`, `record_gate_config`, `learn_references` |
| `housekeeping.py` | 82 | `prune_caches`, `_prune_scratch_world` |
| `drift.py` | 136 | `drift_monitors`, `_psi` |
| `report.py` | 186 | `report`, `flag_view_lines`, `_rate_ci` |

Three deviations from the table above, each deliberate:

- **Step 1 stayed in `loop.py`.** Moving the `download()` wrapper into
  `download.py` would put the *store rebuild* (`features.py`) inside the
  network job, which is a different program's business. The wrapper is a
  window subtraction and two `subprocess.run` calls — orchestration, so it
  stays with the orchestrator.
- **The clock's patch point moved on purpose.** `rewind_report.freeze_clock`
  replaced `loop.now_utc`, which only loop.py's own callers ever resolved;
  it now replaces `util.now_utc`, which every cycle module resolves at call
  time. Strictly this makes the freeze *wider* than before — the previous
  behaviour was the accident.
- **Five render aliases and `_lot_key` were deleted, not moved**
  (`clean_cell`, `date_de`, `html_page`, `table_html`, `receipt_html`).
  Nothing outside `loop.py` ever read them and nothing inside still called
  them; re-exports are what this phase removes.

One name collision surfaced and was resolved in favour of the module: the
cycle's local for the monitor results is now `drift_checks`, because `drift`
is a module. `SECTOR` travelled with its only caller (`track_record`) into
`grading.py`; the copies in `simulation.py` and `render_dashboard.py` are a
pre-existing triplication this phase neither widened nor fixed.

## 10. Automating the rejector — done 2026-08-16

The operator asked whether software could move a knob by itself, ideally
continuously. The answer taken: **the retreat and the rejection are safe to
automate; the advance is not.** A wrong rejection costs an improvement nobody
sees; a wrong promotion reaches customers. So a night job may kill a
candidate value on its own and may never promote one — §0.5's rule, now with
a machine on the rejecting half.

### 10.1 The override lever

`TM_GATE_OVERRIDE='{"NOMINATION_BAR": 0.60}'` — one candidate configuration
per **process**. Keys are the constants' own names in `evidence.py` or
`relevance.py`; each module applies the ones it owns (`util.apply_override`)
and a key nobody claims **raises** at the first `GateConfig` construction.
That refusal is the point: a run under a silently ignored override would
measure the champion and report the candidate's name.

Two properties come free. The override flows through `evidence.rules()` into
the gate fingerprint, so every measurement stamps itself as its own
configuration; and because it is per process, two candidates never share one
interpreter — which is exactly the limitation §4.1 accepted when it made
`evidence_rules` a snapshot rather than a read-from-config.

### 10.2 The job

`backplay.py`, via `docker/backplay.sh` and the third line of
`docker/crontab`, under the heavy lock so it never meets the Monday cycle
halfway. First scheduled Sunday 04:00 (the evidence changes weekly at best);
since §11.3 **nightly** 04:00 with a change detector, which spends the same
CPU and answers a day after the evidence moved instead of a week.

    python backplay.py            # every filed question's candidates
    python backplay.py --self-check   # the rule and the wiring, one second
    python backplay.py --show     # the record, and what has expired
    python backplay.py --knob evidence.NOMINATION_BAR --grid 0.50,0.55,0.60 --current 0.55

The last form measures a knob **ad hoc**, without filing a question — real
harness, real data, real rows, but nothing becomes LIVE. It exists because
"is this connected" deserved an answer that costs a command rather than a
deployment. `--self-check` answers the same question in a second on synthetic
numbers, and says that is what it is doing.

Per candidate: a subprocess with the override set, running the question's
harness — `evidence.py --judge` for a gate knob (minutes; `--out` writes the
table as JSON for exactly this), `rewind_all.py` for the end-to-end replay
over every weekly cutoff (~33 min, ~200 MB of scratch). A question supplies
its own `read` to pull its metric out of its own harness's document: there is
no universal parser, because only the question knows which row of which table
its number lives in.

A harness that exits non-zero or writes nothing **raises**. A rejection
resting on a crash is the worst kind of silent kill.

### 10.3 The rule, and why it is hard to satisfy

Several candidates over several measurements guarantee that some look bad by
chance, so the rejector is deliberately conservative. A candidate dies only
when:

- it breaches the hard bar (2.2 % wrong-trade leakage) on a **majority** of
  measurements — exactly half is not a majority; or
- it loses to the current value on **every** measurement, intervals disjoint,
  and there is more than one measurement. One bad cutoff is weather.

A consequence worth stating plainly: with the `judge` harness a candidate
usually has **one** measurement, so only the hard-bar rule can kill it. That
is the intended asymmetry — leakage is a refusal whatever recall it buys,
while "slightly worse on one benchmark run" is not evidence of anything.

### 10.4 Rejections expire

Rows in the `backplays` ledger, stamped with the gate fingerprint, the
benchmark, the harness and the day. They stand for `REJECTION_TTL_DAYS` (90,
matching §8.1's ROLLBACK retirement) and are then simply not read any more —
a horizon at read time, never a DELETE, because the ledger is frozen and the
record of what was believed when is worth keeping.

`knobs.weekly()` reads the live rejections, drops those values from the
proposal, and **names them in the line**: `backplay rejected 0.60 (leaks
above 2.2% on 3/4 measurements)`. The operator must see a rejection, never a
silently shorter grid.

### 10.5 What is still not automated, and why

- **Promotion.** Unchanged: only forward grades promote, and only the
  operator flips the switch (§8.3).
- **Continuous anything.** The gate harness is minutes and the replay is
  half an hour; the data behind them moves weekly at best (awards lag
  deadlines by ~3 months). Sampling faster than the truth changes converts
  noise into motion.
- **Door 3 — the bias no engine closes.** `asof.py` is honest by
  construction about data leakage, but the knobs were chosen by someone who
  had already seen those outcomes. Automating backplay *industrialises* that
  bias rather than removing it, which is precisely why this half may only
  reject, and why the most recent weeks should stay held out for the forward
  confirmation.
- **Auto-revert on a live guardrail breach.** Proposed in the same
  conversation and not built: it needs leakage measured on *delivered* lots,
  which is the forward channel, not this one. When that number exists weekly,
  reverting to the last recorded-good configuration is the second safe
  automatic move.

## 11. The queue — the program files the questions, 2026-08-16

§10 built the rejector and left `knobs.LIVE` empty: nothing was measured until
someone filed a question by hand. The operator then set the expectation
plainly — *automatic replay; the parameters are controlled by the program; I
am not to be asked for any hand-made value; I want to see which knobs are
tried and which are rejected.* So the last hand-made input, the question
itself, is now the software's.

### 11.1 Knobs and grids

Words used here, in plain terms: the **queue** is the list of knobs waiting
their turn, one live per bucket; a **grid** is the values a knob may take
and a **step** one of them; a **switch** is an on/off knob; **no effect** is
the verdict when every value tried gives identical numbers; the **evidence
version** (`stamp` in the ledger) names the benchmark, store and champion a
measurement stood on; a **result line** (receipt) is what a closed question
leaves behind; a **standing proposal** is a `move` nobody has acted on yet.

`knobs.KNOBS` lists every verdict-affecting rule the override lever reaches
— **39 knobs** as of 2026-08-16: `relevance.CONVICTION_NOMINATES`,
`SIMILARITY_NOMINATES`, `DEFAULT_MIN_CODE_HARD`, and 36 of the 37
`evidence.RULES` (all but `CORE_TITLE_UNION`, a string) — each with a
program-owned **grid**: for a number `lo`, `hi`, `step` (e.g.
`DEFAULT_MIN_CODE_HARD` 0.775 … 0.875 by 0.025, `SYN_THRESHOLD` 0.70 … 0.90
by 0.05, `CONVICT_BODY_MIN` 1 … 4); for a switch `(off, on)`. The list *is*
the register of what the search covers; the order is the rotation order —
the switch that decides the gate's shape (`CONVICTION_NOMINATES`) first,
then the conviction rules, then the lexicon, then the dictionaries. Not
listed: `EVIDENCE_NOMINATION_MIN` (§11.5), `NOMINATION_BAR` and
`BORDERLINE_ADMIT_P` (dead / placeholder while their switches are off).

`current` is read from the module at run time, never stored — the queue
cannot disagree with the code, and after the operator accepts a step the
question simply continues from the new step. A constant that sits off its
grid raises, and `tests/test_queue.py` builds every real question so that
is caught by the suite, not on a Monday.

### 11.2 One live knob per bucket, rotating — one knob per night

`knobs.queue()` keeps exactly one open question per bucket: the next knob in
the bucket's rotation after the last one closed (wrapping), opened today, stop
date `STOP_WEEKS = 8` out, id `auto:<knob>` (stable, so a rejection outlives
one opening and expires on its own clock, §10.4). A hand-filed question in
`LIVE` still takes its bucket's slot. State is `data/logs/knobs_queue.json`
— open questions and the closed ones with their result lines.

**A queue question closes the night it is answered** (`close_if_answered`,
2026-08-16 evening — the operator: "nothing is my own intuition, but an
automatic systematic parameter search", so nothing waits for a Monday or a
person): *no effect*; every neighbour rejected; a clear `move` (recorded as
a **standing proposal**); `flat` with every neighbour measured or rejected;
or the stop date. `hold` — a neighbour the harness could not measure — keeps
it open. The next knob opens on the *next* run, so one knob per bucket per
night; with 39 gate knobs and one bucket the rotation is ~5–6 weeks, then it
starts again and re-measures only what the evidence version says moved.

A `move` closes the question because the search must go on; the proposal
stays listed — Monday's report and `--show` print `PROPOSAL standing since
<date>` until the constant actually moves (the three-file commit, §8.2), at
which point the knob is simply re-measured from its new step next time round.

### 11.3 One pipeline: backplay measures and closes, Monday reads

A queue question has no `run` of its own. `backplay.py` measures it — the
current value first (a `role='current'` row) and then each neighbour under
`TM_GATE_OVERRIDE` — and every `backplays` row now carries `metric`, `n`,
`leakage` and the **evidence stamp** it stood on (benchmark blob, store files,
champion fingerprint). `knobs.judge_question()` reads the latest row per step
back as the sweep it used to be handed and applies the same verdict ladder;
`backplay.run` calls it the same night and closes what is answered,
`knobs.weekly()` calls it on Monday for whatever is still open, adds the
questions closed that week and the standing proposals, and prints
the **grid** in the line: `1 x | [2] 0.649 | 3 ok 0.612 | 4 .` — rejected,
current with its metric, survives with its metric, untried. The rejection a
value carries is its *latest* measurement's: measured again and surviving
lifts it before the 90 days are up.

**Cadence: nightly 04:00, measuring only what moved.** The evidence changes
weekly at best (§10.5), so the job compares the stamp with the one each
question's last measurement stood on and re-measures only when it differs or
the question is new; otherwise one line per question says what it stood on
and when. Nightly rather than Sunday so a benchmark label read on Tuesday is
measured Wednesday, not the following week. Cost when it does run: one judge
run per step, ~25 min each on the laptop, so ~75 min per question; the heavy
lock keeps it off the Monday cycle. `--force` re-measures regardless.

**Seeing it:** `python backplay.py --show` prints the queue — per bucket the
live knob, since when, its grid with every step marked, the proposal, the
rejections with reasons and dates — then the closed questions with result lines,
then the raw record. Monday's report carries the same grid line under
**Knobs**.

### 11.4 Two things found while wiring it, and one left out

- **`evidence.py --judge --out` had never produced a document.** `judge_run`
  returned a per-mode dict and `write_judge_json` expected the sweep's tuple
  rows; the first real run crashed after 25 minutes of measuring. It now
  returns two configuration rows — `evidence gate (committed)` and `embedding
  gate` — with the denominators, and `backplay.judge_read` finds the committed
  one. The candidate is measured through the **real** `relevance.judge()`
  path, not the sweep's replica verdict.
- The sweep's `(committed)` row hard-coded `K>=2`; it now reads
  `rel.EVIDENCE_NOMINATION_MIN`, so an override of K is measured there too.
- **`no effect`** joined the verdict grid: every measured step identical to
  the last digit means the knob cannot move a verdict under the current
  switches — DEAD, not flat — and the question closes at once instead of
  holding the bucket two cycles to learn it twice.
- **Competitiveness knobs are not on the queue.** `--threshold` and
  `MULTIHOT_MIN_SUPPORT` need the replay harness (33 min per cutoff set) *and*
  a lever that reaches them, which `TM_GATE_OVERRIDE` does not; listing them
  and measuring nothing would be the silent miss §10.1 refuses. When that
  lever exists they are three lines in `KNOBS`.

### 11.5 The first run — receipt, 2026-08-16

Laptop, sandbox copy of the store, `python backplay.py`; the queue opened
`relevance.EVIDENCE_NOMINATION_MIN` at 2, candidates 1 and 3; three
`evidence.py --judge` runs of ~22 min each, `[benchmark]
benchmark_relevance.jsonl blob 9e9e3f59076f cases 1752 seed 7`, 561 firms,
recall over 2,698 leave-one-out positives, leakage over 28,050 negatives:

| K | gate fingerprint | recall | leakage | verdict |
| --- | --- | --- | --- | --- |
| 2 (current) | `7931c8e9cd` | 0.649 | 2.20 % | — |
| 1 | `98cb288f72` | 0.649 | 2.20 % | survives |
| 3 | `ad1dc6cda8` | 0.649 | 2.20 % | survives |

Three fingerprints, so the override reached the gate; identical numbers to
sixteen digits, so **K cannot change a verdict**. The reason is in
`relevance._evidence_verdict`: `passed = nominated and convicting`, and with
`CONVICTION_NOMINATES` on (phase 8k) whatever convicts already nominates —
the witness rule can only add nominations that then fail to convict. The
register listed the knob as LIVE on the strength of the phase 8e receipt,
which was measured before 8k. It is DEAD while that switch is on; the row in
§2.1 says so, the knob left `KNOBS`, and the `no effect` verdict exists so the
next such knob costs one run, not two cycles. The queue moved on to
`DEFAULT_MIN_CODE_HARD` by itself.

Two smaller findings from the same run, both fixed and tested: on Windows
`subprocess.run(text=True)` decoded the judge's German titles as cp1252 and
the reader thread died at eleven kilobytes (`encoding='utf-8'` now); and the
25-minute crash of §11.4. The rows stand in the sandbox's `backplays` ledger;
the server's ledger is empty until the first night after deployment.

## 12. The forward channel for the gate — `shadow.py`, 2026-08-16

§3 promised the gate a promote channel — *shadow-judge every live lot with
the challenger; the disagreements are the next labels* — and §8.2 said a
value ships only after one cycle in shadow. Neither existed; the queue could
propose, and nothing could turn a proposal into forward evidence. Built the
same evening the operator said "go with 1".

### 12.1 What runs, and when

Every Monday, inside the cycle, before `knobs.weekly()`: for each **standing
proposal** (a `move` the queue found, whose constant nobody has moved), the
cycle's scored lots are judged twice per subscription — once under the
champion, once under the proposal, each in its own subprocess under
`TM_GATE_OVERRIDE` exactly as backplay measures. Where the two verdicts
differ (in / near / out) a row goes to the `gate_shadows` ledger (`role
'diff'`, with title, buyer, CPV, the description's first 500 characters and
the profile it was judged for); the cycle's counts go in one `summary` row
(judged, champion admits, challenger admits, disagreements). No standing
proposal → one quiet line, no subprocess. First real run, 400-lot sample,
7 subscriptions, 751 lot×profile judgments: `DEFAULT_MIN_CODE_HARD` 0.85
disagreed nowhere; `CONVICTION_NOMINATES` off disagreed on 5.

### 12.2 The reading is blind

`python shadow.py --label` shows each unread disagreement — profile, title,
buyer, CPV, text — and takes `i` / `o` / `s` / `q`. It never shows which
configuration said what, so the labels cannot drift toward the champion (§3).
Readings are `gate_labels` rows: (subscription, lot) → in / out. This is the
one hand action left in the loop, and it is the truth channel, not a
parameter: the operator reads lots (memory: relevance judgments by the
operator, never by code); the software does everything else.

### 12.3 The verdict, per challenger, computed on request

From the diffs joined with the readings — `challenger right` when its verdict
(`in` vs not) matches the reading, likewise `champion right` — and the
**added leakage**: the challenger's extra admissions read as `out`, over
everything it admits on live lots (`admits`, summed from the summaries).

| status | rule |
| --- | --- |
| `no cycle yet` | no summary row |
| `bar breached` | added leakage > 2.2 % — certain even with few read: every wrong admission is a fact, the unread ones can only add. **Proposal dropped.** |
| `collecting` | fewer than `MIN_LABELLED = 20` read |
| `ready to promote` | Wilson lower bound of challenger-right > 0.5 and not breached — the three-file commit is the operator's |
| `challenger loses` | Wilson upper bound < 0.5. **Proposal dropped.** |
| `undecided` | interval straddles 0.5 — keep reading |

Dropped means `shadow.challengers()` no longer runs it and the proposal line
says why. The forward channel therefore rejects on its own like backplay
does, and *promotes* only in the sense §8.3 allows: it writes the receipt
that makes the commit defensible.

### 12.4 Where it shows

Monday's report, under **Knobs**: one line per proposal — `PROPOSAL standing
since D: knob a -> b (…); forward: **collecting** — 1 cycle, 5 disagreements,
0 read (need 20) — python shadow.py --label` — and one line per challenger
with this cycle's counts. `python shadow.py --show` prints the same with the
numbers behind it. Ledgers: `gate_shadows`, `gate_labels` (db.py, append-only
like every other).

### 12.5 Not built

Reading through the web app; using customer feedback verdicts as labels
(sparse, and a customer's yes/no is a sanity check, never the verdict —
§3); a gate A/B with the unit customer × lot (EXPERIMENTS.md §10 — this is
its cheaper sibling: one champion, N challengers, no delivery ever switches).

## 13. The competitiveness knobs join the queue — 2026-08-16

§11.4 left `--threshold` and `MULTIHOT_MIN_SUPPORT` out because the lever
did not reach them and the replay harness had no reader. Both now exist, and
the queue has a second bucket.

- **One `THRESHOLD`.** `single_bidder.THRESHOLD = 0.5` is the constant; the
  cycle's `--threshold` defaults to it and `rewind_all.py` /
  `rewind_report.py` read it from there — one value where there were three
  copies of `0.5`. `single_bidder.py` applies `TM_GATE_OVERRIDE` like the two
  gate modules (the env var keeps its name; `util.OVERRIDE_OWNERS` names the
  three, and the unconsumed check imports them first so import order cannot
  make a valid key look unclaimed).
- **The replay payload carries what it stood on** (`schema` 2): `threshold`,
  `multihot_min_support`, `override`, and per lot the cutoff `week` it was
  first flagged in.
- **`backplay.replay_read`**: precision at the delivered cutoff, one
  measurement per cutoff week (flagged lots with a known award; share that
  ended with 0–1 bids), recall over the whole replay alongside for the
  record, leakage `None` — not a competitiveness number. The rejection rule
  pairs measurements **by week**; the queue's row pools them (one rate on
  one denominator; the per-week list stays as `n_measurements`).
- **Two knobs, bucket `competitiveness`, harness `replay`:**
  `single_bidder.THRESHOLD` 0.40 … 0.65 by 0.05 and
  `single_bidder.MULTIHOT_MIN_SUPPORT` 10 … 60 by 10. The grid for the
  threshold is capped on purpose: precision alone always votes for a higher
  cut-off, and the line shows `n` flagged so a proposal that starves the
  picks is visible; EXPERIMENTS.md §1's "recall secondary" is the reason the
  forward arm, not this number, decides.
- **Cadence.** A replay is ~33 min on the laptop per value, three values a
  night, so the replay bucket **sits out Monday night** (`REPLAY_SKIP_WEEKDAYS`)
  — a run still going at 08:15 would make the cycle wait on the lock. Both
  buckets run on the other nights, one knob each.
- **What promotes them:** not this. A `move` here is a standing proposal like
  any other, and the forward evidence for a competitiveness knob is an
  EXPERIMENTS.md arm — the queue's proposal is the reason to open one.

Also from the server's first night (§11.5): the whole `--judge` run overran
two hours there (store 2.6× the laptop's), so backplay now calls it with
`--modes evidence --no-volume` — the committed mode only, and without the
200-lots-per-firm volume sample that is 78 % of the calls and a number the
rule never reads. Same seed, same recall and leakage, an eighth of the cost.

