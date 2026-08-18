# SECRETS — how credentials reach the server

Rewritten 2026-08-18 from the operator's requirements: "not in git, still easy
to deploy; layered, not one global `.env`; and do not treat every password
separately, the list will grow". Supersedes the 2026-08-15 version, which
managed four named keys one at a time — that unit is wrong once Stripe,
a second mail provider and the next thing arrive. The unit here is a **file
per concern**, and nothing in the tooling knows a key name.

This is the **spec** for `docker/secrets.sh` and the `env.d/` layout; nothing
here is built yet except where a line says so. Companions:
[`OPERATIONS.md`](OPERATIONS.md) §4 step 4 (which this replaces),
[`.env.example`](../.env.example) (what exists today, to be split by §5),
[`HOSTING.md`](HOSTING.md).

## 0. The rules

1. **Git never holds a secret value.** The repository is public. Stage 2 (§6)
   allows encrypted values in git; stage 1 allows nothing.
2. **The password manager is the durable copy.** One entry per *file*
   (`murara/mail.env`, `murara/payments.env`, …), the whole file as a secure
   note. A rebuilt server is re-armed from there, never from a backup:
   `nightly.sh` keeps excluding `.env` and everything under `env.d/`.
3. **GitHub Actions holds the deploy key and nothing else.** The list of
   secrets grows without a GitHub secret ever being added.
4. **Every service sees only the files it needs.** Caddy never has the Stripe
   key in its environment; the app never has the admin hash.
5. **Adding a secret is a one-line change in one file** — no script, no
   compose edit, no map to update. Adding a *provider* is a new file plus one
   `env_file:` line for the service that uses it.

## 1. Layout — the same tree in two places

```
/etc/murara/env.d/           on the server (owner: deploy user, mode 0600, dir 0700)
~/.murara/env.d/             on the laptop, the working copy (see §7 before relying on it)

  site.env          TM_DOMAIN, TM_WWW_DOMAIN, TM_APP_URL, TM_TZ, TM_MAIL_FROM,
                    TM_PRICE_LINE, TM_IMPRESSUM        — configuration, no secret in it
  mail.env          RESEND_API_KEY
  payments.env      TM_STRIPE_URL, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET   (the last two once they exist)
  admin.env         TM_ADMIN_USER, TM_ADMIN_HASH
  backup.env        RESTIC_REPOSITORY, RESTIC_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  experiments.env   TM_EXPERIMENTS_KEY
```

Naming rule: the file is named after the *service the credential belongs to*
(the party that issued it), not after the consumer. `RESEND_API_KEY` is
`mail.env` because Resend issued it, whichever container uses it. A new
provider gets a new file; a second key from the same provider goes into the
existing one.

`site.env` is the one file that is not secret. It still lives here, not in
git, because it is *per machine* (the laptop has no domain) — the same reason
`TM_STATE` is not in git. It may be pasted into a chat without harm.

**What stays in `.env` next to `docker-compose.yml`:** only what compose
itself needs before any service starts — `TM_STATE`, `TM_TAG` (set
per-command by `deploy.sh`), `TM_APP_BIND`, `TM_APP_PORT`, `EMBED_MODEL`.
The split is "how compose runs" (`.env`) versus "what the containers know"
(`env.d/`). `deploy.sh` keeps reading `TM_STATE` from `.env` unchanged.

## 2. Compose reads the layers per service

The `environment:` block that interpolates `${X:-}` for every secret is
replaced by `env_file:` lists — one per service, listing only what it needs:

```yaml
tm:                                     # the base every service extends
  env_file:
    - path: /etc/murara/env.d/site.env
      required: false                   # a laptop without the file still runs
app:
  env_file:
    - path: /etc/murara/env.d/site.env
      required: false
    - path: /etc/murara/env.d/mail.env
      required: false
    - path: /etc/murara/env.d/payments.env
      required: false
    - path: /etc/murara/env.d/experiments.env
      required: false
scheduler:
  env_file:                             # nightly/weekly need mail + backup, nothing else
    - path: /etc/murara/env.d/site.env
      required: false
    - path: /etc/murara/env.d/mail.env
      required: false
    - path: /etc/murara/env.d/backup.env
      required: false
edge:
  env_file:
    - path: /etc/murara/env.d/site.env
      required: false
    - path: /etc/murara/env.d/admin.env
      required: false
```

- Later files override earlier ones; that is the layering. Order within one
  service is "least specific first".
- `required: false` needs Compose ≥ 2.24 — **precondition, checked once on
  the server with `docker compose version` before anything else moves.**
- The directory is a constant path, not `${TM_ENV_D}`: one fewer thing that
  can be unset. On the laptop the same absolute path is used (create it or
  leave it absent; the files are optional).
- The scheduler's `.cron-env` dump keeps working as is: it filters the
  container's environment, and that environment now comes from `env_file`.
  The grep pattern in [`docker-compose.yml`](../docker-compose.yml) is the
  one place that still lists prefixes; a new backup or mail variable with a
  new prefix has to be added there — noted in the file→keys table (§4).
- **The `$$` trap.** Today `TM_ADMIN_HASH` needs every `$` doubled because
  compose expands `$` in `.env`. Whether values in `env_file` are expanded
  too is *not* assumed either way: §5 step 4 settles it with
  `docker compose config` and the doc line in `admin.env`'s header says
  which it is.

## 3. The tool: `docker/secrets.sh` (to build)

Runs **on the laptop**, talks to the server over the operator's own SSH key
([`murara-vps-ssh`](../CLAUDE.md)). It moves *files*; it never parses a
value, never takes one as an argument, never prints one.

```
bash docker/secrets.sh push  [file…]   # laptop → server: rsync, chmod 600; then restart what changed
bash docker/secrets.sh pull  [file…]   # server → laptop, for a new laptop (refuses to overwrite a newer local file)
bash docker/secrets.sh diff            # per file: same / differs / only-here / only-there; per key: name + changed?  — never a value
bash docker/secrets.sh list            # server side: file, key, set|EMPTY, first character — the receipt after a rebuild
```

`push` details, in order:

1. `rsync -a --chmod=D700,F600 --no-delete` of the named files (default: all
   present locally) into `/etc/murara/env.d/`. Never `--delete`: a file
   missing on the laptop must not remove one on the server; `list` shows the
   difference and the operator decides.
2. Restart the services whose `env_file:` list contains a pushed file — read
   from `docker compose config` on the server, not from a table in the
   script. `docker compose up -d <services>`: recreate, so `.cron-env` is
   rewritten too.
3. Print `list`.

`list` output, what proves a push did what it said:

```
mail.env         RESEND_API_KEY          set  r…    app, scheduler restarted 12:04
backup.env       RESTIC_REPOSITORY       set  s…
backup.env       RESTIC_PASSWORD         EMPTY
admin.env        TM_ADMIN_HASH           set  $…    edge restarted 12:04
```

What it never does: echo a value; put one on a command line (rsync over ssh
carries file bodies, no process list on either machine sees a value); leave a
temporary file; edit `.env`. A key with a typo is not its problem — the file
is copied as is, and the app's own "no mail could be sent" line is where a
misnamed key is found. That is a deliberate loss against the 2026-08-15
design and the price of not carrying a key list.

## 4. Where it fits

- **`bootstrap.sh`** creates `/etc/murara/env.d` (0700, deploy user) and ends
  its summary with `bash docker/secrets.sh push` instead of "edit `.env`".
  A fresh server is armed by two commands from the laptop: bootstrap, push.
- **`deploy.sh`** is untouched: compose picks the files up on
  `up -d`. A deploy that changes an `env_file:` list is an ordinary code
  deploy.
- **`OPERATIONS.md` §4 step 4** becomes "`secrets.sh push`".
- **The file→keys→consumer table lives in `HOSTING.md`** — one lookup for
  "where does X live and who reads it". `.env.example` shrinks to the compose
  knobs and points there; each `env.d/` file gets an `env.d.example/`
  counterpart in git with keys and empty values, which is what "the machine
  is gone, what do I have to retype" reads.
- **Rotation** (a leaked Resend key): rotate at Resend, edit the manager's
  `murara/mail.env` note, edit `~/.murara/env.d/mail.env` (or `pull` after
  editing on the server), `push mail.env`. One file moves.
- **The restic password** keeps its extra sentence: it must exist in the
  manager *before* the first `nightly.sh` push.

## 5. Migration — one evening, one receipt

1. `docker compose version` ≥ 2.24 on the server, or stop here.
2. On the server: `docker compose config > /tmp/before.yml` (resolved
   environment, the reference).
3. Split today's `.env` into `env.d/` by the §1 table — a one-off
   `docker/split-env.sh` that copies lines by key name into the right file
   and leaves the compose knobs in `.env`; unknown keys are listed and
   refused, not guessed. `chmod 600`. Delete nothing yet.
4. Deploy the compose change; `docker compose config > /tmp/after.yml`;
   `diff` the two. Identical `environment:` per service — except that each
   service now lacks the keys §1 says it must not have — is the receipt. This
   step also answers the `$$` question: if `TM_ADMIN_HASH` differs, undouble
   in `admin.env` and re-diff.
5. `pull` the tree to the laptop; paste each file into the manager as
   `murara/<file>`. Only then remove the secret lines from `.env`.
6. Log in to `/admin`, send one test mail — the two consumers that break
   loudest.

## 6. Stage 2 — the files in git, encrypted (sops + age)

Not now; the operator's word (2026-08-18) is "I like the idea, I am not sure
I can handle the keys yet". Written down so stage 1 does not have to be
redone for it: **everything from §2 on stays identical**, only where the
plain files come from changes.

**What changes**

- The `env.d/` files are committed as `secrets/<file>.env`, values encrypted:
  `RESEND_API_KEY=ENC[AES256_GCM,data:…]`. Key *names* are readable in git,
  values are not; `git diff` shows which key changed, never to what.
- `deploy.sh` decrypts them into `/etc/murara/env.d/` before `up -d`
  (`SOPS_AGE_KEY_FILE=/etc/murara/age.key sops -d …`), atomically, mode 600.
- `secrets.sh push` disappears; a secret change is a commit. `list` and
  `diff` stay.

**The keys — two, each living in exactly one place**

| key | made where | lives where | backup |
| --- | --- | --- | --- |
| laptop | `age-keygen -o ~/.config/sops/age/keys.txt`, once | that file | one secure note in the password manager (~200 bytes) — the *only* copy outside the laptop |
| VPS | `age-keygen -o /etc/murara/age.key` inside `bootstrap.sh`, root 0600 | that file | none. A rebuilt VPS makes a new one and is added by the operator (below) |

Their **public** halves are in git, in `.sops.yaml`:

```yaml
creation_rules:
  - path_regex: secrets/.*\.env$
    age: age1laptop…,age1vps…
```

Adding a machine (rebuilt VPS, second laptop): generate its key there, copy
the *public* half into `.sops.yaml`, `sops updatekeys secrets/*.env` on the
laptop, commit, deploy. Removing one: delete its line, same `updatekeys` — a
decommissioned server can no longer open anything, no wiping ritual.

Loss cases: laptop gone → key from the manager. Laptop and manager gone →
the VPS still decrypts; `pull` the plain files, new laptop key, `updatekeys`.
Both machines gone → regenerate every credential at its provider, which is
also today's answer.

**Precondition for switching:** the operator has held the laptop key for a
while and once done a rebuild drill (`DRILL.md`) with it. Until then stage 1
is the design, and it is not a stopgap: it is complete on its own.

## 7. Open decisions — operator's

| decision | note |
| --- | --- |
| ~~may a plain working copy live on the laptop (`~/.murara/env.d/`)?~~ **Decided 2026-08-18: yes** — the operator relaxed the 2026-08-15 rule ("the laptop is out of every recovery path"). The laptop directory is the working copy, mode 0600, and the password manager stays the durable one (§0.2); the laptop is still not a *recovery* source — a rebuild pulls from the manager, then `push`. The alternative that would have kept the old rule (`push` reading each file from the manager via `TM_SECRET_SOURCE`, nothing at rest on the laptop) is not built. | closed |
| which password manager | decides only whether the alternative above is a one-liner; the file-per-entry convention is manager-agnostic |
| are the `env.d/` files backed up off-site by anything | this spec says no (rule 2); the manager is the copy. If yes, the manager stops being authoritative and the whole of §0.2 changes |

## 8. Order of work

1. ~~Operator answers §7 row 1.~~ Done 2026-08-18: laptop working copy allowed.
2. `docker compose version` on the server (§5.1).
3. `docker/secrets.sh` — `list` first (read-only, tested against the live
   server), then `pull`, `diff`, `push`.
4. Compose `env_file:` change + `docker/split-env.sh` + `env.d.example/`,
   run §5 end to end; the `before/after` diff is the receipt in the commit
   message.
5. `bootstrap.sh`, `OPERATIONS.md` §4 step 4, `HOSTING.md` table,
   `.env.example` shrunk.
6. Stage 2 when §6's precondition holds.
