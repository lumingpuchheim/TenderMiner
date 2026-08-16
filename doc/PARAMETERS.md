# PARAMETERS — the register, and the rules for touching a knob

Written 2026-08-16 from the operator's question in this session: "there are
so many parameters that I lose my overview — can I use Google's A/B method
to find the optimal combination?" The answer given was **no experiment
infrastructure at this scale; a register, a freeze, and one channel per
model** — this file is that answer as a spec. Companion to
[`EXPERIMENTS.md`](EXPERIMENTS.md) (forward shadow arms for the
competitiveness model, in progress in another worktree — nothing here edits
it; §6 says what to add there once it has merged) and
[`RELEVANCE.md`](RELEVANCE.md) (the receipts every gate value cites).

## 0. The rules

1. **Every tunable belongs to exactly one bucket** — *gate* ("is this my
   business"), *competitiveness* ("is this a low-contested tender"),
   *delivery* (what a customer sees of the two verdicts) or *monitoring*
   (when the system speaks up). The two models share no metric, no truth
   source and no cadence, so they never share a knob.
2. **Every tunable is either FROZEN or LIVE.** Frozen: set once, moved only
   with a written reason, never "tuned". Live: has a benchmark, a receipt,
   and may move. The register (§2) is the list; a knob not in it is a bug in
   this file, not a knob.
3. **One live knob per bucket at a time.** Google's layers, reduced to one
   operator: never move two knobs of the same bucket in the same week. Two
   knobs that must move together are one knob (a named configuration, as the
   receipts already do with their letters H, K, …).
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
O operational (not a tunable, listed so nothing is missing). **status**:
FROZEN / LIVE / DEAD (inert under the current mode) / ROLLBACK (kept only so
`GATE_MODE='embedding'` still runs). **stamped**: whether the value is
recoverable from a ledger row written under it.

### 2.1 Gate — `relevance.GateConfig`

| knob | value | status | stamped | receipt |
| --- | --- | --- | --- | --- |
| `mode` (`GATE_MODE`, env) | `evidence` | FROZEN | fp | RELEVANCE.md phase 8, 2026-08-06 |
| `evidence_nomination_min` | 2 | LIVE | fp | phase 8e, K≥2 vs K≥3 receipt |
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

### 2.2 Gate — `evidence.py` (verdict-affecting, **unstamped**)

| knob | value | status | stamped |
| --- | --- | --- | --- |
| `MIN_STEM_LEN` | 6 | FROZEN | no |
| `MAX_KEYWORDS` | 25 | FROZEN | no |
| `MAX_DOC_FREQ` | 0.02 | FROZEN | no |
| `MIN_WITNESSES` | 1 | FROZEN | no |
| `TYPO_MIN_LEN` | 8 | FROZEN | no |
| `SYN_THRESHOLD` | 0.80 | FROZEN | no |
| `CONVICT_BODY_MIN` | 2 | LIVE (the "title-or-two" rule) | no |
| `LABEL_DF_MAX` | 5 | FROZEN | no |
| `DICT_MIN_LOTS` | 20 | FROZEN | no |
| `DICT_VOTE_MARGIN` / `DICT_VOTE_MAX` | 0.5 / 6 | FROZEN | no |
| `DICT_MIN_RATIO` | 8.0 | FROZEN | no |
| `DICT_MIN_BUYERS` / `DICT_MIN_BUYER_SHARE` | 2 / 0.10 | FROZEN | no |
| `DICT_MAX_WORDS` | 30 | FROZEN | no |
| `DICT_CACHE_V` | 6 | O — cache version, bump when any of the above moves | no |
| `SEED`, `NEG_PER_FIRM`, `VOL_PER_FIRM`, `MIN_WINS` | 7 / 50 / 200 / 3 | O — sweep sampling, not verdicts | n/a |
| `SWEEP_BARS` | 0.40…0.70 | O — grid of the sweep | n/a |

Every FROZEN row here changes which words are witnesses, hence which lots
pass. Under the current code, moving any of them leaves `gate_config =
7d29fa0dce` on every delivery row. That is the rule-4 breach §4 fixes.

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

Of ~70 tunables, **LIVE: 5** — `evidence_nomination_min`, `min_code_hard`,
`CONVICT_BODY_MIN` (gate); `--threshold`, `MULTIHOT_MIN_SUPPORT`
(competitiveness) — plus per-customer overrides. ROLLBACK: 9. DEAD: 1.
Everything else is frozen or operational. That is the overview.

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

**4.1 Honest fingerprint.** The §2.2 verdict-affecting constants join
`GateConfig` as fields with the module constants as defaults, exactly as the
17 existing ones did (REFACTOR.md phase 3 pattern). `evidence.py` reads them
from the config it is handed, not from module state. `DICT_CACHE_V` and the
sweep-sampling constants stay module-level (rule: cache and sampling are not
verdicts). Test: two configs differing only in `SYN_THRESHOLD` have different
fingerprints; the default config's fingerprint changes once, is recorded in
this file, and every later delivery row carries the new value.

**4.2 One benchmark, one denominator.** `evidence.py --sweep` and `--judge`
print, on the first line, the benchmark file's git blob hash and its line
count, and the sample seed. Receipts quoted in RELEVANCE.md and here name
that line. Different denominators (the 19-lot hard set, the 103-lot grown
set, the 2473/25600 synthetic sample) stay — but a receipt that mixes them
in one row is not a receipt. No new file: the benchmark is already committed;
this only makes every number say which one it stands on.

**4.3 Live-config assertion.** `loop.py` prints `GateConfig().describe()`
and the fingerprint at the top of every cycle, and `/healthz` shows the
fingerprint. Purpose: the three env-driven knobs can be flipped by a stray
variable in cron's environment; today nothing would say so. One line each.

Not asked for: retiring the ROLLBACK ladder (cheap to keep, and it is the
tested rollback of a decision only ten days old); a generator for §2; a
settings file; anything customer-facing.

## 5. What is *not* built, on purpose

Layered traffic assignment, per-customer experiment config, bandits, an
experiments dashboard beyond EXPERIMENTS.md's page. With 100 deliveries and
18 graded lots, any live split is wider than the effect it hunts. Revisit
when there are hundreds of active customers and a feedback rate worth
counting; the schema already carries `model`, `gate_config`, `sub_version`
on every row, so nothing has to be re-instrumented then.

## 6. To add to EXPERIMENTS.md after the `ab-arms-spec` worktree merges

One line under §0: *backplay (`asof.World`, `rewind_all.py`) may reject an
arm before it earns a shadow slot; only forward grades promote.* And under
§6: `arm_grade` is also the per-(lot, model) grading this file's §3 wants —
reuse it over the 3,942 historical lots scored by more than one model rather
than writing a twin.

## 7. Decisions taken in this session

| decision | who | value |
| --- | --- | --- |
| Google-style experiment infrastructure now | assistant, operator agreed | no — labels are the bottleneck, not plumbing |
| unit of the register | assistant | one prose table, this file, kept by hand |
| what counts as evidence | assistant, extends EXPERIMENTS.md §0 | backplay rejects, forward promotes |
| gate knobs outside the fingerprint | assistant | fold into `GateConfig` (§4.1) |
| ROLLBACK ladder | assistant | keep, mark, do not tune |
| where the two-model split lives | assistant | the bucket column; no shared knob ever |
