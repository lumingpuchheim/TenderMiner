# SECRETS — how credentials reach the server

Written 2026-08-15, from the operator's question "the server's `.env` needs
the keys; they must never be in git; I want safety and convenience". This is
the **spec** for `docker/secrets.sh`; nothing here is built yet except where a
line says so. Companions: [`OPERATIONS.md`](OPERATIONS.md) §4 step 4 (which
this replaces), [`.env.example`](../.env.example) (the list of what exists).

## 0. The rule

**The password manager is the only durable copy. The server's `.env` is a
cache of it. Nothing in between keeps a copy.**

- Not git: the repository is public.
- Not the laptop: the 2026-08-13 decision took the laptop out of every
  recovery path, and a plaintext `.env` on a laptop is a second thing to
  protect for no gain. (Verified 2026-08-15: the laptop holds no `.env`.)
- Not GitHub Actions secrets: they hold the deploy key and nothing else; a
  key that can rewrite `.env` on the server is a wider key than "deploy".
- Not a backup: `nightly.sh` deliberately excludes `.env` and `.cron-env`.

Consequence: **a rebuilt server is re-armed by pulling from the password
manager, never by copying a file from anywhere.**

## 1. Which secrets

The four lines of `.env` that are credentials, and nothing else — every other
line is configuration and lives in git as `.env.example` defaults or is
written by `bootstrap.sh`:

| key | what breaks without it |
| --- | --- |
| `RESEND_API_KEY` | no e-mail leaves the app or the cycle (signup confirmations, reports) |
| `RESTIC_PASSWORD` | the off-site backup is unwritable — and unreadable, forever, if lost |
| `AWS_ACCESS_KEY_ID` | restic cannot reach the bucket |
| `AWS_SECRET_ACCESS_KEY` | same |

`RESTIC_REPOSITORY` is the bucket URL — not secret, but it belongs to the same
group and is set the same way so the four-plus-one always travel together.

## 2. The tool: `docker/secrets.sh` (to build)

Runs **on the laptop**, talks to the server over the operator's own SSH key.
Three commands:

```
bash docker/secrets.sh status                 # what is set / empty on the server
bash docker/secrets.sh set  RESEND_API_KEY    # one key: read from the source, write, restart
bash docker/secrets.sh sync                   # all five: read each from the source, write, restart
```

**Where the value comes from — the "somewhere else".** One environment
variable, `TM_SECRET_SOURCE`, names a command that prints the value for a key
name given as `$1`. The script never sees a value on its own command line and
never writes one to the laptop's disk. Examples, one of which the operator
sets once in their shell profile:

```
# 1Password CLI — items in a vault "Murara", one item per key
TM_SECRET_SOURCE='op read "op://Murara/$1/credential"'
# Bitwarden CLI — items named murara/<KEY>
TM_SECRET_SOURCE='bw get password "murara/$1"'
# pass (gpg)
TM_SECRET_SOURCE='pass show murara/$1'
# KeePassXC
TM_SECRET_SOURCE='keepassxc-cli show -sa password ~/murara.kdbx "murara/$1"'
```

Unset → the script falls back to a hidden prompt (`read -rs`), which is the
"paste it from the manager by hand" mode and still satisfies §0.

**Naming convention in the manager**, so `sync` can find every key without a
map: the entry is named exactly like the `.env` key (`RESEND_API_KEY`), under
one folder/vault `murara`. The convention is the only coupling; the script
carries no manager-specific code.

**What `set`/`sync` do on the server**, in order:

1. Refuse if the target `.env` is inside a git checkout that a deploy
   hard-resets — it is not (`~/TenderMiner/.env` is gitignored), the check
   costs one `git check-ignore`.
2. Replace the line `KEY=…` or append it (`bootstrap.sh`'s `set_env`, but
   *overwriting* — this is the one tool that is allowed to). Atomic: write
   `.env.new`, `mv`. Mode 600 after.
3. Restart what reads it: `RESEND_API_KEY` → `docker compose up -d app`;
   the `RESTIC_*`/`AWS_*` group → the scheduler too, because `.cron-env` is
   written from the environment at scheduler start
   ([`docker-compose.yml`](../docker-compose.yml)).
4. Print `status`.

**What `status` prints** — key name, set or EMPTY, and the first character
of the value; never more. It is what an operator runs after a rebuild and
what proves a `sync` did what it said:

```
RESEND_API_KEY         set  r…     app restarted 12:04
RESTIC_REPOSITORY      set  s…
RESTIC_PASSWORD        EMPTY
AWS_ACCESS_KEY_ID      EMPTY
AWS_SECRET_ACCESS_KEY  EMPTY
```

**What it never does:** echo a value, pass one as an argument to `ssh` (it
goes over stdin, so it is in no process list on either machine), leave a
temporary file, or touch a key not in the §1 list — a typo'd key name is a
refusal, not a new line in `.env`.

## 3. Where it fits

- **`bootstrap.sh`** ends its summary with `bash docker/secrets.sh sync`
  instead of "edit `.env` by hand". A fresh server is fully armed by two
  commands from the laptop: bootstrap, sync.
- **`OPERATIONS.md` §4 step 4** becomes that one line.
- **Key rotation** (a leaked Resend key): rotate in Resend, update the
  manager entry, `secrets.sh set RESEND_API_KEY`. Nothing else moves.
- **The restic password** gets one extra sentence wherever it is mentioned:
  it must exist in the manager *before* the first `nightly.sh` push, because
  the first push encrypts against whatever `.env` holds, and a password that
  existed only on the machine dies with it.

## 4. Open decision — operator's

| decision | note |
| --- | --- |
| which password manager, and is its CLI installed on the laptop | decides the one line `TM_SECRET_SOURCE=`; everything else is manager-agnostic. Without a CLI the prompt mode works today and costs a paste per key per rebuild |

## 5. Order of work

1. Operator names the manager (§4).
2. `docker/secrets.sh` — `status`, `set`, `sync`; tested against the live
   server with `status` first, then `set` of the Resend key.
3. `bootstrap.sh` summary and `OPERATIONS.md` §4 step 4 updated to point here.
4. First real use: the restic group, the night the bucket exists.
