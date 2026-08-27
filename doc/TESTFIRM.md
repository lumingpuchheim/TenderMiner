# TESTFIRM — testing with real firms, without touching them

The operator's manual for `testfirm.py`. Spec background: doc/PAYMENT.md
§3b; decided with the operator 2026-08-22.

## 1. What it is, in three sentences

You test the product with real firms like Jebsen, because only a real
firm's award history produces real picks — but every admin state you could
put on a real firm's row during a test is a lie (*Stoppen* says "they
objected", the Sperrvermerk says "never contact them", and both outlive the
test). `testfirm.py` builds a **twin** instead: a customer whose profile is
copied from the real firm's public awards, but whose identity is
`test-<slug>` / "TEST <name>", with the mails going to your own inbox. The
real firm's row stays untouched and invitable the whole time, and the tool
is physically unable to delete anything that is not a twin.

## 2. What a twin is and is not

| | twin (`test-…`) | the real firm |
| --- | --- | --- |
| profile / picks | the real firm's — same lots, same relevance | — |
| mails go to | your address (`--email`, default: first `TM_SALES_OWNERS`) | the firm |
| Monday cron | **mails it like any customer** (your decision: "i want to receive real monday mails for test companies") | only if really subscribed |
| trial clock | runs honestly: 4 free mails → ask → silence | runs |
| admin counts line | **excluded** — a test never inflates your funnel numbers | counted |
| admin firm list | not shown (it is not a market firm); see `list` | shown |
| delete | restlos, no Sperrvermerk — it never objected | always with Sperrvermerk |

## 3. Preconditions

- The cycle has run (a champion model and scored lots exist) — true on the
  server every week.
- The firm is on the target list with ≥ 2 usable profile references
  (`invite.py` would demand the same).
- `TM_MAIL_FROM` + `RESEND_API_KEY` are set (they are, since 2026-08-20),
  or every send fails loudly.

All commands run **inside the app container on the server**, where the
live data and mail keys are:

```
ssh -i C:\Users\user\.ssh\murara_ovh debian@57.129.112.187
cd TenderMiner
docker compose exec -T app python testfirm.py <command>
```

(On the laptop the same commands work against a local data dir with
`--data-dir`, but there is rarely a reason: the point of a twin is the real
Monday rhythm, and that lives on the server.)

## 4. The commands

### add — create the twin

```
python testfirm.py add "Jebsen"                    # trial, mails to TM_SALES_OWNERS
python testfirm.py add "Jebsen" --paid             # no clock, Mondays forever
python testfirm.py add "Jebsen" --email x@y.de     # different inbox
```

Expected output:

```
[testfirm] TEST Jebsen GmbH & Co. KG angelegt (test-jebsen-gmbh-co-kg, trial)
— der Montagsversand nimmt sie ab jetzt mit
```

From now on the Monday 11:00 delivery treats the twin as a customer. On
`trial` you get the real prospect experience: four reports, each with the
subscribe box ("Empfehlung n von 4"), then the ask, then nothing — exactly
what a real firm would see. `remove` + `add` restarts the trial from 1.
With `--paid` there is no clock and no ask; the report comes every Monday
that has picks.

Refuses when: the twin already exists (`remove` first); the firm has fewer
than 2 profile references (pick another firm); no address is known (pass
`--email` or set `TM_SALES_OWNERS`).

### send — a Monday mail, today

```
python testfirm.py send "Jebsen"
```

Renders and mails the twin's current report through the identical door the
cron uses (`delivering.deliver`, `mail=True`): same subject, same buttons,
same footer, real tokens. **It counts as one of the four trial mails,
because it is one** — the clock in the subscribe box moves. Expected
output ends with:

```
[deliver] test-jebsen-gmbh-co-kg: report mailed (<resend id>)
```

"no picks — report written, not mailed" is honest too: a week without a
recommendation sends nothing, for twins as for customers.

### remove — erase the twin, restlos

```
python testfirm.py remove "Jebsen"
```

Cancels a test-mode Stripe subscription if the twin has one (a failure is
printed, never blocking), then erases everything — customer row, versions,
tokens, events — with **no Sperrvermerk**: a twin never objected. Expected:

```
[testfirm] test-jebsen-gmbh-co-kg restlos gelöscht: customer: 1,
subscription_version: 2, token: 14, app_event: 9
```

The guard: `remove` only ever resolves to `test-<slug>` ids. Called for a
firm without a twin it refuses — it cannot fall through to the real row.

### list — what twins exist

```
python testfirm.py list
```

One line per twin: sub_id, name, contact state, plan, inbox. Twins do not
appear on the admin firm list (they are not market firms), so this is
where you check what is currently running.

## 5. The full Jebsen loop

```
python testfirm.py add "Jebsen"        # twin exists, trial 0/4
python testfirm.py send "Jebsen"       # today's report in your inbox (1/4)
                                       # …Mondays: 2/4, 3/4, 4/4 + ask, silence
python testfirm.py remove "Jebsen"     # everything gone
python testfirm.py add "Jebsen"        # fresh trial, 0/4
```

Clicking through a mail is safe by design: every link opens a page, and
only the button ON the page stores anything. You can also complete a
test-mode Stripe checkout from the twin's subscribe box (card
`4242 4242 4242 4242`) — the twin becomes `Kunde · bezahlt` via the real
webhook, and *Stoppen* / `remove` cancels the test subscription again.

## 6. What stays out of bounds

- **A real firm's states.** Nothing in a twin's lifecycle writes to the
  real firm's row; inviting the real Jebsen later works as if no test ever
  happened.
- **Your funnel numbers.** The counts line skips `test-` ids.
- **The frozen pilot firms.** Erasure refuses pre-migration firms
  categorically; twins are always post-migration, so this never bites the
  test loop.
- One honest leftover: a twin's Monday deliveries write real delivery-
  ledger rows under its `test-` sub_id (the frozen record of what was
  actually sent). They are filterable by the prefix and die with nothing —
  `remove` deletes the twin, not the ledger of what it was sent.

## 7. When something fails

| symptom | cause, fix |
| --- | --- |
| `refuses: … has 1 usable profile ref(s)` | firm too thin to profile — same rule as real invitations; twin a firm with more awards |
| `already exists — remove first` | leftover twin from the last test: `remove`, then `add` |
| `no address: pass --email or set TM_SALES_OWNERS` | `site.env` lost its `TM_SALES_OWNERS` line, or you're on a bare laptop |
| mail not arriving | `[deliver] … report NOT mailed — …` names the reason (no picks, mailer refusal, Resend down); the send is also in `app_events` |
| `no champion model` | fresh/laptop data dir without a cycle — run on the server |
