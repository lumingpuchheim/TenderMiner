# GLOSSARY — the words the specs use, in plain terms

Written 2026-08-16 after the operator asked for a crash course on the
jargon in [`PARAMETERS.md`](PARAMETERS.md) and [`EXPERIMENTS.md`](EXPERIMENTS.md).
Every entry: the word, what it means here, and the TenderMining example.
Alphabetical within four groups. When a spec uses a word that is not in
this file, add it here in the same commit.

## Knobs and configuration

**Knob / tunable / parameter** — a number or on/off switch in the code that
somebody chose and could have chosen differently. `--threshold 0.5` is a
knob: at 0.4 more lots are flagged. Not a knob: a lot's CPV code, a
customer's name — those are data.

**Bucket** — which of four jobs a knob serves: the *gate* ("is this my
business"), the *competitiveness model* ("is this a low-contested tender"),
*delivery* (how many picks a customer sees, in what order), *monitoring*
(when a warning prints). Sorting knobs into buckets is what turns one pile of
70 into four short lists.

**Frozen** — set once, left alone; moved only with a written reason, never
"tuned". **Live** — the knob currently being improved; has a benchmark and a
receipt. **Dead** — currently changes nothing (`NOMINATION_BAR` while
`SIMILARITY_NOMINATES` is off). **Rollback** — kept only so an earlier
configuration can be switched back on (the embedding ladder under
`GATE_MODE='embedding'`).

**Retired** — a ROLLBACK knob deleted from the code after 90 days unused
(PARAMETERS.md §8.1); the ledger keeps the stamps of the rows it once
judged, git keeps the value.

**Filed question** — the one-sentence entry ticket for a knob to go LIVE:
question, metric, benchmark by blob hash, grid, stop date. Without one a
knob stays FROZEN whatever anyone thinks of its value.

**Proposal (weekly line)** — what `knobs.py` prints in the report for a live
question: `move up`, `move down`, `flat`, `hold (underpowered)` or `stop date
reached`. A proposal is not an action — no code moves a value.

**Rejector** — the night job (`backplay.py`) that measures candidate knob
values and may kill them, never promote them. Safe to automate for the same
reason auto-revert is: its worst case is an improvement nobody sees.

**Override lever** — `TM_GATE_OVERRIDE`, a JSON object naming constants, read
once per process. It lets a candidate value be *measured* without anyone
editing the constant that holds it; a key no module claims raises rather than
being ignored.

**Guard (gate guard)** — the check that refuses to deliver when the gate
configuration a run resolved to is not the one the register records. It is
what an edited constant without the ritual looks like from outside.

**Rules (evidence rules)** — the `evidence.py` constants that decide which
words are witnesses and what convicts; `evidence.RULES` names them and the
gate fingerprint snapshots their values (`rules=<hash>` in `describe()`).

**Configuration** — one complete set of knob values, named so it can be
cited: "configuration H", "K≥2". Two knobs that must move together are one
configuration, not two knobs.

**Register** — the hand-kept table of every knob with bucket, value, status
and whether a ledger row records it. Lives in `PARAMETERS.md` §2.

**Stamp** — writing the identity of the configuration onto every record it
produced. `model = m2026-08-08-123827` on a prediction row is a stamp.
**Fingerprint** — a short hash of all the gate's knob values
(`gate_config = 7d29fa0dce` on a delivery row); one string that stands for
the whole configuration. **Honest stamp** — if any knob that can change a
verdict moves, the stamp changes too. Today `evidence.py`'s constants can
move without the fingerprint moving; that is the dishonesty
`PARAMETERS.md` §4.1 fixes.

**Env-driven** — a knob whose value is read from an environment variable at
start-up (`GATE_MODE`, `SIMILARITY_NOMINATES`, `CONVICTION_NOMINATES`). Handy
for a one-off run; risky because a stray variable in cron's environment
silently changes production.

## Measuring

**Benchmark** — a fixed set of lots whose right answer a human already
wrote down (`benchmark_relevance.jsonl`, 1,779 lines; memory rule: labels
come from the operator reading the text, never from code). Every
configuration is scored on the same set so results compare. **Frozen
benchmark** — not changed between two comparisons; if labels are added,
everything is re-scored on the new set and the receipt says which set.

**Label** — the right answer for one case: relevant / not relevant for the
gate; `n_tenders ≤ 1` for competitiveness (`LABEL_MAX_TENDERS`).

**Receipt** — the recorded evidence behind a decision: date, configuration,
before/after numbers, and which cases separated the options. RELEVANCE.md
is a chain of them ("K≥2: 51.5 % recall / 2.7 % leakage / 4.4 % volume").

**Precision** — of the lots flagged, the share that were right. **Recall** —
of the lots that were right, the share flagged. **Volume** — how many lots
were flagged at all (a report must not be empty, nor forty lines).
**Leakage** (gate sense) — wrong-trade leakage: a lot that is not the
customer's trade reaching their report. The 2.2 % bar is a hard refusal
whatever recall it buys.

**Flat** — a knob is flat when moving it across its sensible range barely
changes the result (K≥2 vs K≥3 on the wrong-trade cases: 28/32 each). Flat
means: freeze it, stop looking. The opposite — a small move changes the
outcome a lot — is where attention pays.

**Grid / sweep** — try a knob at several fixed values and record the result
at each (`evidence.py --sweep` over 0.40 … 0.70). **Coarse grid** — few,
widely spaced values: enough to see the shape (flat, rising, a peak), not
to find the exact optimum.

**Paired comparison** — judge the *same* lots with configuration A and B and
count where they disagree, instead of giving A one set of lots and B
another. Far fewer lots are needed for a trustworthy verdict, and the
3,942 lots already scored by two or more models are such a set.

**Confidence interval / Wilson interval** — the range a rate could plausibly
be in, given how few cases it rests on. `cycle.py` prints one beside every
rate so "3 of 4" never reads like "300 of 400".

**Underpowered** — too few cases for the comparison to tell the arms apart
even if one is really better. With 18 graded lots, any A/B split is
underpowered; that is why the specs say "no live split now".

## Testing a change

**Backplay / backtest / replay** — pretend it is a past date, run the system
on only what was known then, then compare with what actually happened
afterwards. Fast because the answers already exist. `asof.py` is the
engine; `rewind_all.py`, `rewind_win.py`, `rewind_report.py` are the three
programs over it.

**Forward / shadow** — run the system on today's open lots, write down its
verdict, wait months for the truth to arrive from the award notices. Slow;
nothing can leak. EXPERIMENTS.md is built on this.

**Leakage** (testing sense — a different word wearing the same spelling) —
*information leakage*: the test accidentally seeing the answer, e.g. a
backtest trained on lots whose award was already published. Context tells
which leakage is meant; when both could apply, the specs say "wrong-trade
leakage" or "information leakage" in full.

**Cutoff (D)** — the pretend "today" of a backplay; nothing published on or
after D is visible to the as-of world. **Moving cutoff / held-out** — tune
on cutoffs D₁…Dₙ, then confirm on a Dₙ₊₁ nobody looked at while tuning. A
result that holds only on the tuning cutoffs was fitted to them.

**Blind labelling** — when reading a lot to label it, not knowing which
configuration flagged it. Otherwise the benchmark drifts toward the
champion's opinions and every later sweep rewards the champion for agreeing
with itself.

**Backplay rejects, forward promotes** — the rule in `PARAMETERS.md` §0.5: a
backtest may kill a candidate or rank a coarse grid; only forward grades
may move a value into production.

## Experiments

**Arm** — one competing configuration in an experiment. **Champion** — the
configuration in production now. **Challenger** — one trying to replace it.
**Shadow arm** — a challenger that scores every lot and never reaches a
customer, a monitor or the simulation. **Delivering arm** — the one that
does reach customers (EXPERIMENTS.md).

**A/B test** — two arms, same period, one metric, decided in advance. In
this project the unit is a lot (or customer × lot), never a customer
receiving a different e-mail.

**Layers (Google)** — the trick behind thousands of simultaneous tests:
knobs that do not interact go in separate layers, each visitor is in one
test per layer, so the same traffic serves every layer. Irrelevant at this
scale; what survives is *never move two related knobs in the same week*.

**Bandit** — an experiment that shifts traffic toward the winning arm while
running. Not built, on purpose (EXPERIMENTS.md §11): the switch is the
operator's.

**Orchestrator** — a program that only *sequences* other modules (paths,
checkpoint, call this then that, the CLI) and holds no logic of its own.
PARAMETERS.md §9 wanted `loop.py` (now `cycle.py`) to become one.

**Verdict / ready** — EXPERIMENTS.md's software-computed line saying whether
the picture is clear enough to decide; the decision itself is the
operator's.
