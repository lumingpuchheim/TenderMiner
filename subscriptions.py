"""TenderMining subscriptions — what a subscription IS, in one place.

REFACTOR.md phase 1. Before this module the answers to three questions were
folklore, re-derived wherever they were needed:

  * **Which version speaks on date D?** Implemented six times (delivering.deliver,
    delivering.learn_references, tryout, explain, replay, feedback) — same rule,
    six spellings.
  * **Is this lot in the customer's market?** Implemented twice, in
    `loop._matches` and inline in `backtest.replay` — and they DRIFTED: one
    compared the subscription's CPV prefix against the 3-digit slicing key,
    the other against the full code. That drift was a real bug (a prefix
    deeper than 3 digits matched nothing; fixed in `bbb5292`) and it meant
    the backtest was not measuring the selection the product ships.
  * **Is this field even real?** Nothing asked. `top_n` and `avoid_n` sat in
    live subscription lines, read by no code at all, silently ignored.

This module owns all three, **and it owns storage** — which is the point.
Phase 2 moved subscriptions from `data/subscriptions.jsonl` into the SQLite
database (`db.py`) by changing this file's internals and nothing else: not one
call site moved, because callers name a home DIRECTORY and let `storage()`
resolve it. A home with a database is served from it; a home with only the
file is served from the file, so there was no flag day. See doc/STORAGE.md.

## Validation is deliberately asymmetric

An **unknown** field raises. It is a typo, and a typo that is silently
ignored is discovered from a wrong report weeks later.

A **retired** field warns and is ignored. The subscription file is
append-only by design — a v2 line carrying `avoid_n` is a true historical
record of what the operator decided in August 2026, and refusing to read it
would mean the system can no longer read its own history. Retired fields are
listed in RETIRED with what replaced them, so the warning says something
useful.
"""

import json
import sys
from pathlib import Path

import config

# Every field a subscription line may carry. The comment is the field's
# meaning; SUBSCRIPTIONS.md is the specification.
KNOWN = {
    'sub_id',             # stable customer key, required
    'version',            # int >= 1; versions are appended, never edited
    'effective_from',     # ISO date this version starts speaking; absent = always
    'active',             # false deactivates (as a new version, never an edit)
    'name',               # display name, and the default award_names entry
    'award_names',        # winner_names spellings that are this customer
    'cpv_prefixes',       # market: lot CPV starts with any of these (OR)
    'nuts_prefixes',      # market: lot NUTS starts with any of these (OR)
    'min_deadline_days',  # deadline promise; excludes unknown deadlines when > 0
    'max_picks',          # cap on the delivered list
    'profile_refs',       # publication numbers of the customer's own wins
    'profile_texts',      # free-text descriptions of what they do
    'min_relevance',      # gate: text-channel bar (enables the gate)
    'min_code_hard',      # gate: trusted-code bar
    'min_code_soft',      # gate: inferred-label bar
    'plan',               # 'trial' (absent = trial) | 'paid' — LAUNCH.md 3, ONBOARDING.md 9.5
}

PLANS = ('trial', 'paid')
# The trial is counted in MAILS WITH RECOMMENDATIONS, not weeks (operator,
# 2026-08-20): sending is event-driven now — no good tender, no mail — so
# "four free weeks" could mean four mails or none. Every trial customer gets
# FREE_REPORTS mails that each contain at least one recommendation; the ask
# rides on the last of them. `trial_status` reports the count it is handed
# (delivering.trial_state counts `send` events of kind report).
FREE_REPORTS = 4
TRIAL_DAYS = 28           # LAUNCH.md: the old four-weeks clock — retired 2026-08-20, kept for the doc trail

# Fields that were once real. Kept readable, never authoritative.
RETIRED = {
    'top_n': 'max_picks (decision 2026-08-04)',
    'avoid_n': 'nothing — the warnings list was removed on 2026-08-06, so no '
               'kind:"avoid" rows are written any more',
}

DEFAULT_MAX_PICKS = 5  # SUBSCRIPTIONS.md decision 2026-08-04


class SubscriptionError(ValueError):
    """A subscription line that cannot be trusted to mean what it says."""


def _where(source, lineno):
    return f'{source}:{lineno}' if lineno else str(source)


def _is_missing(v):
    """None and the JSON null a sandbox writer emits both mean 'not set'.
    rewind_report.py writes `min_relevance: null` for a firm with no resolvable
    win — an explicit absence, not a bad value."""
    return v is None


def validate(row, source='<row>', lineno=None, retired_out=None):
    """A validated copy of one subscription line, retired fields dropped.

    Raises SubscriptionError with the file and line for anything that cannot
    be read with confidence. Returns a plain dict: the wire format stays a
    mapping, because relevance.build_profile is also handed synthetic specs
    by the receipt harnesses (evidence.judge_run, rewind_all.as_of_profile).

    `retired_out`: a dict collecting {field: count} instead of warning per
    occurrence. An append-only file accumulates retired fields on every
    historical line, so read_all aggregates one warning per field — a cycle
    log should not carry eleven identical lines.
    """
    if not isinstance(row, dict):
        raise SubscriptionError(f'{_where(source, lineno)}: not an object')
    out = {}
    for key, value in row.items():
        if key in RETIRED:
            if retired_out is None:
                print(f'[subscriptions] {_where(source, lineno)}: ignoring '
                      f'retired field {key!r} — superseded by {RETIRED[key]}')
            else:
                retired_out[key] = retired_out.get(key, 0) + 1
            continue
        if key not in KNOWN:
            raise SubscriptionError(
                f'{_where(source, lineno)}: unknown field {key!r}. '
                f'Known fields: {", ".join(sorted(KNOWN))}')
        out[key] = value

    sub_id = out.get('sub_id')
    if not isinstance(sub_id, str) or not sub_id.strip():
        raise SubscriptionError(
            f'{_where(source, lineno)}: sub_id is required and must be a '
            f'non-empty string')

    def _int(key, minimum):
        v = out.get(key)
        if _is_missing(v):
            return
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            raise SubscriptionError(
                f'{_where(source, lineno)}: {key} must be an integer '
                f'>= {minimum}, got {v!r}')

    _int('version', 1)
    _int('min_deadline_days', 0)
    _int('max_picks', 0)

    for key in ('min_relevance', 'min_code_hard', 'min_code_soft'):
        v = out.get(key)
        if _is_missing(v):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise SubscriptionError(
                f'{_where(source, lineno)}: {key} must be a number in [0, 1], '
                f'got {v!r}')

    for key in ('cpv_prefixes', 'nuts_prefixes', 'profile_refs',
                'profile_texts', 'award_names'):
        v = out.get(key)
        if _is_missing(v):
            continue
        if isinstance(v, str) or not isinstance(v, (list, tuple)):
            raise SubscriptionError(
                f'{_where(source, lineno)}: {key} must be a list, got {v!r}')
        if not all(isinstance(x, str) and x.strip() for x in v):
            raise SubscriptionError(
                f'{_where(source, lineno)}: {key} must contain non-empty '
                f'strings, got {v!r}')

    # A CPV prefix is matched against the lot's full 8-digit code. A prefix
    # that no code could start with is a mistake worth catching at load time:
    # this is the class of error that gave jebsen-blitzschutz v1 an empty
    # market for days (`cpv_prefixes: ["453123"]` tested against a 3-digit key).
    for p in out.get('cpv_prefixes') or []:
        if not p.isdigit() or not 2 <= len(p) <= 8:
            raise SubscriptionError(
                f'{_where(source, lineno)}: cpv_prefixes entry {p!r} is not '
                f'2-8 digits — a CPV code is 8 digits and is matched by prefix')

    eff = out.get('effective_from')
    if not _is_missing(eff) and not _iso_date(str(eff)):
        raise SubscriptionError(
            f'{_where(source, lineno)}: effective_from must be YYYY-MM-DD, '
            f'got {eff!r}')
    active = out.get('active')
    if not _is_missing(active) and not isinstance(active, bool):
        raise SubscriptionError(
            f'{_where(source, lineno)}: active must be true or false, '
            f'got {active!r}')
    plan = out.get('plan')
    if not _is_missing(plan) and plan not in PLANS:
        raise SubscriptionError(
            f'{_where(source, lineno)}: plan must be one of {PLANS}, '
            f'got {plan!r}')
    return out


def trial_status(rows, as_of, sent_reports=0):
    """Where one customer stands in the trial -> dict, from ITS versions
    (every version of one sub_id, any order) on `as_of` (ISO date), plus
    `sent_reports` — how many report mails have already gone out (the
    caller counts them; delivering.trial_state reads the send ledger).

      plan     'paid' | 'trial'
      started  ISO date of the first active version, or None (never active)
      sent     report mails so far (each carried a recommendation — the
               no-picks-no-mail rule guarantees it)
      ends     None while mails remain free; 'now' once the next mail would
               be past the FREE_REPORTS quota
      ask_due  True when the next mail is the LAST free one or later —
               the ask rides on it (operator, 2026-08-20: the trial is
               four mails with recommendations, not four weeks)

    No new field on the subscription (ONBOARDING.md 9.5): a customer who
    says yes gets a version with `plan: paid` and the count stops
    mattering."""
    as_of = str(as_of)[:10]
    speaking = resolve(rows, as_of)
    plan = (speaking[0].get('plan') or 'trial') if speaking else 'trial'
    starts = sorted(str(r.get('effective_from') or '')[:10]
                    for r in rows if r.get('active', True))
    started = next((s for s in starts if s), None)
    sent = int(sent_reports)
    if not started:
        return {'plan': plan, 'started': None, 'sent': sent, 'ends': None,
                'ask_due': False}
    return {'plan': plan, 'started': started, 'sent': sent,
            'ends': ('now' if sent >= FREE_REPORTS else None),
            'ask_due': plan == 'trial' and sent >= FREE_REPORTS - 1}


def _iso_date(s):
    return (len(s) == 10 and s[4] == '-' and s[7] == '-'
            and s[:4].isdigit() and s[5:7].isdigit() and s[8:].isdigit())


FILE_NAME = 'subscriptions.jsonl'
# Left behind by the phase-2 migration. A legacy file that still PARSES is the
# dangerous failure mode: a caller would get plausible, silently out-of-date
# customers and no error at all. So the migration renames the file to
# `<FILE_NAME>.migrated-<date>` and storage() refuses to read a home that has
# both the marker and the old file.
MIGRATED_GLOB = FILE_NAME + '.migrated-*'


def storage(home):
    """Resolve a subscription HOME DIRECTORY to its storage.

    **Callers pass the directory, never the file.** That is the whole
    contract: the storage format is this module's business, so it changed
    (phase 2 moved it into SQLite) without touching a single call site. Any
    code that constructs `<dir>/subscriptions.jsonl` itself has reached past
    the interface — so passing a path that looks like storage raises here
    instead of half-working.

    -> ('db', path) | ('jsonl', path) | None

    **The database wins when both exist**, which is the state during and after
    migration, because `db.py --migrate` deliberately does not delete the
    originals. That preference is only safe because `read_all` cross-checks
    the two (see below); without the check, an operator editing the file after
    migrating would have their edit silently ignored — the exact failure this
    module is built to prevent.
    """
    p = Path(home)
    if p.suffix in ('.jsonl', '.db', '.sqlite'):
        raise SubscriptionError(
            f'pass the subscription HOME DIRECTORY, not a storage file '
            f'({p.name}). The storage format belongs to subscriptions.py; '
            f'call subscriptions.load(data_dir, as_of) and let it resolve.')
    if not p.exists():
        return None
    leftovers = sorted(p.glob(MIGRATED_GLOB))
    live = p / FILE_NAME
    if leftovers and live.exists():
        raise SubscriptionError(
            f'{p}: found both {live.name} and {leftovers[-1].name}. The '
            f'migrated marker says storage moved, the live file says it did '
            f'not — refusing to guess which one describes your customers.')
    import db
    if db.path_for(p).exists():
        return ('db', db.path_for(p))
    return ('jsonl', live) if live.exists() else None


# Retired-field notices are announced ONCE per process per field. A cycle
# calls load() twice and each call reads both the database and (for the
# staleness cross-check) the file, so an announce-every-read notice printed
# eight identical lines into the cycle log. The operator needs to know once.
_WARNED = set()


def _announce_retired(where, retired, unit):
    for field, n in sorted(retired.items()):
        if field in _WARNED:
            continue
        _WARNED.add(field)
        print(f'[subscriptions] {where}: ignoring retired field {field!r} on '
              f'{n} {unit}(s) — superseded by {RETIRED[field]}')


def _read_jsonl(path, quiet=False):
    rows, retired = [], {}
    for i, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise SubscriptionError(f'{path}:{i}: not valid JSON ({e})') from e
        rows.append(validate(row, source=path, lineno=i, retired_out=retired))
    if not quiet:
        _announce_retired(path.name, retired, 'line')
    return rows


def read_all(home):
    """Every version, validated, in storage order. A home with no storage is
    not an error — a deployment simply has no customers yet.

    When a database and a source file both exist, the database answers, but
    only after proving it is not behind: any `(sub_id, version)` present in the
    file and missing from the database raises. Two sources with one silently
    preferred is how a customer ends up served from a stale market filter,
    so the drift is a loud failure with the fix in the message.
    """
    where = storage(home)
    if where is None:
        return []
    kind, path = where
    if kind == 'jsonl':
        return _read_jsonl(path)

    import db
    con = db.connect(home, create=False)
    # `raw` is the verbatim original line, so migrated rows still carry any
    # retired field they were written with — aggregate the notice as the file
    # path does, or a load prints one line per version
    retired = {}
    rows = [validate(r, source=path, retired_out=retired)
            for r in db.subscription_rows(con)]
    _announce_retired(path.name, retired, 'version')
    legacy = Path(home) / FILE_NAME
    if legacy.exists():
        stored = db.subscription_versions_present(con)
        missing = [(r.get('sub_id'), int(r.get('version') or 1))
                   for r in _read_jsonl(legacy, quiet=True)
                   if (r.get('sub_id'), int(r.get('version') or 1)) not in stored]
        if missing:
            raise SubscriptionError(
                f'{legacy} has {len(missing)} subscription version(s) the '
                f'database does not: {missing[:5]}. The database is what gets '
                f'served, so this edit would be ignored. Run '
                f'`python db.py --migrate` to take it in.')
    return rows


def resolve(rows, as_of):
    """The versions in force on `as_of`, active ones only.

    SUBSCRIPTIONS.md: versioned, never edited — the newest version with
    `effective_from <= as_of` speaks for the subscription, and `active:
    false` deactivates. A line with no `effective_from` has always been in
    force (rewind_report.py writes such lines into its sandbox).
    """
    as_of = str(as_of)
    in_force = {}
    for row in rows:
        eff = str(row.get('effective_from') or '')
        if eff > as_of:
            continue
        key = (eff, int(row.get('version') or 1))
        cur = in_force.get(row['sub_id'])
        if cur is None or key >= cur[0]:
            in_force[row['sub_id']] = (key, row)
    return [row for _, row in in_force.values() if row.get('active', True)]


def load(home, as_of):
    """The active subscriptions in force on `as_of`. The one entry point.

    `home` is the directory subscriptions live in — `data/` in production, a
    sandbox directory under preview_report.py/rewind_report.py. Never a file path; see
    storage()."""
    return resolve(read_all(home), as_of)


def one(home, as_of, sub_id):
    """The single subscription `sub_id` in force on `as_of`, or None."""
    return next((s for s in load(home, as_of) if s['sub_id'] == sub_id), None)


def write_sandbox(home, subs):
    """Create throwaway subscription storage in `home` — the only supported
    way to build a disposable customer set.

    preview_report.py and rewind_report.py used to write `subscriptions.jsonl` themselves,
    which made two more places that knew the storage format. They ask for a
    sandbox now, and phase 2 changes what that means in exactly one file.
    Every row is validated first: a sandbox that cannot be read back is worse
    than useless, because its report looks real.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    rows = [validate(s, source=f'write_sandbox({home.name})') for s in subs]
    # a sandbox uses the SAME storage the real customers use, so preview_report.py and
    # rewind_report.py exercise the shipped read path rather than a second format
    # only sandboxes know about
    import db
    db.put_subscriptions(home, rows)
    return home


def append_version(home, row):
    """Append one validated subscription version to LIVE storage — the write
    the app's signup handler uses (doc/APP.md 4). Validation first, then
    ledger.append, so the storage format stays ledger.py's business and an
    invalid version can never reach disk. Returns rows written (0 if the
    (sub_id, version) already exists — idempotent, like every append)."""
    import ledger
    validated = validate(row, source=f"append_version({row.get('sub_id')})")
    return ledger.append(home, 'subscriptions', [dict(row)]) if validated else 0


# ------------------------------------------------- the customer row (identity)

CONTACT_STATES = ('active', 'soft_stopped', 'hard_stopped')

# The columns the app may write on `customer`. Guarded like KNOWN: a typo'd
# field must fail loudly, not vanish into an UPDATE that touched nothing.
CUSTOMER_FIELDS = {'name', 'award_names', 'contact_email', 'contact_note',
                   'billing_note', 'consent_at', 'contact_state',
                   # the salesman watching this prospect (doc/SALES.md 3);
                   # NULL = nobody, which is every self-signup
                   'owner'}


def customer_get(home, sub_id):
    """The customer row as a dict, or None. contact_state of NULL reads as
    'active' — every pre-existing customer consented to be one (LAUNCH.md 3)."""
    import db
    con = db.connect(home, create=False)
    if con is None:
        return None
    row = con.execute('SELECT * FROM customer WHERE customer_id = ?',
                      (sub_id,)).fetchone()
    con.close()
    if row is None:
        return None
    d = dict(row)
    d['contact_state'] = d.get('contact_state') or 'active'
    if isinstance(d.get('award_names'), str):
        import json
        d['award_names'] = json.loads(d['award_names'])
    return d


def customer_update(home, sub_id, **fields):
    """Write identity/contact fields on the customer row, creating it if new.

    `customer` is the one mutable table (STORAGE.md 1): identity must stay
    correctable and erasable, so this is an UPDATE on purpose, unlike
    everything else in this module. contact_state is validated against the
    three states of LAUNCH.md 3 — an unknown state on this column would
    silently change who the mailer may write to."""
    import db
    bad = set(fields) - CUSTOMER_FIELDS
    if bad:
        raise SubscriptionError(
            f'unknown customer field(s) {sorted(bad)}; '
            f'known: {", ".join(sorted(CUSTOMER_FIELDS))}')
    state = fields.get('contact_state')
    if state is not None and state not in CONTACT_STATES:
        raise SubscriptionError(
            f'contact_state {state!r} is not one of {CONTACT_STATES}')
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if isinstance(fields.get('award_names'), (list, tuple)):
        import json                    # the column is a JSON array (db.py)
        fields['award_names'] = json.dumps(list(fields['award_names']),
                                           ensure_ascii=False)
    con = db.connect(home)
    with con:
        con.execute(
            f'{db.IGNORE_PREFIX} customer (customer_id, created_at)'
            f' VALUES (?, ?)', (sub_id, now))
        sets = ', '.join(f'{k} = ?' for k in fields)
        con.execute(
            f'UPDATE customer SET {sets}, updated_at = ? WHERE customer_id = ?',
            [*fields.values(), now, sub_id])
    con.close()


def override(sub, **fields):
    """A validated copy with `fields` replaced — the sandbox operation.

    Used by preview_report.py's `--set`, rewind_all.as_of_profile and rewind_report.py's
    as-of spec. Validated, so a sandbox cannot explore a field that does not
    exist: `--set min_relevence=0.6` now fails loudly instead of rendering
    the unchanged report and looking like the setting had no effect.
    """
    merged = dict(sub)
    merged.update(fields)
    return validate(merged, source=f"override({sub.get('sub_id')})")


# ------------------------------------------------------------ market filter

def _cell(row, key):
    """A row value, with pandas NaN normalised to None. Rows come from the
    prediction ledger (dicts, NaN already stamped to null) and from the store
    (pandas rows, where NaN is truthy and would poison a bare `or ''`)."""
    v = row.get(key)
    if v is None:
        return None
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


def cpv_in_market(row, prefixes):
    """Full-CPV prefix match (SUBSCRIPTIONS.md: a lot matches if "its CPV
    starts with any listed prefix").

    Tested against `cpv_main`, the full code. `cpv3` is the truncated slicing
    key and is only a fallback for rows stamped before `cpv_main` was written
    — live code, not a formality: no prediction-ledger row written so far
    carries `cpv_main`, and preview_report.py replays exactly those rows. Under the
    fallback a prefix LONGER than the key it would be tested against cannot
    be proven and therefore does not match, per the keyless-row rule.
    """
    code = _cell(row, 'cpv_main')
    if code is not None:
        return any(str(code).startswith(str(p)) for p in prefixes)
    key = str(_cell(row, 'cpv3') or '')
    return bool(key) and any(key.startswith(str(p)) for p in prefixes
                             if len(str(p)) <= len(key))


def in_market(sub, row):
    """Is this lot inside the subscription's market? CPV and NUTS only — the
    deadline promise is separate, because the annex needs a verdict for
    short-deadline lots too. Omitted filter = no constraint.

    A row that cannot prove it is in the slice does not match
    (SUBSCRIPTIONS.md keyless-row rule).
    """
    if sub.get('cpv_prefixes'):
        if not cpv_in_market(row, sub['cpv_prefixes']):
            return False
    if sub.get('nuts_prefixes'):
        nuts = _cell(row, 'place_nuts3')
        if nuts is None or not any(str(nuts).startswith(str(p))
                                   for p in sub['nuts_prefixes']):
            return False
    return True


def min_days(sub):
    return int(sub.get('min_deadline_days') or 0)


def max_picks(sub):
    v = sub.get('max_picks')
    return int(DEFAULT_MAX_PICKS if _is_missing(v) else v)


def deadline_ok(sub, row, today, days=None):
    """Does the lot honour the subscription's deadline promise on `today`?

    The promise is measured against the ACTIONABLE date: the offer deadline
    — or, for a two-stage procedure whose notice has no offer deadline, the
    participation-request deadline (doc/MODELING.md 10): "at least 14 days
    left to act" is exactly as honourable for a Teilnahmeantrag as for a
    bid. A lot with neither date still FAILS whenever a promise was made.
    `days=0` disables the check.
    """
    import pandas as pd  # only the deadline path needs pandas
    want = min_days(sub) if days is None else int(days)
    if want <= 0:
        return True
    deadline = pd.to_datetime(_cell(row, 'deadline_date'), errors='coerce')
    if pd.isna(deadline):
        deadline = pd.to_datetime(_cell(row, 'participation_deadline_date'),
                                  errors='coerce')
    if pd.isna(deadline):
        return False
    return (deadline.date() - today).days >= want


def matches(sub, row, today, days=None):
    """in_market + deadline_ok. `days=0` gates on the market alone."""
    return in_market(sub, row) and deadline_ok(sub, row, today, days)


def describe_market(sub):
    """The customer-facing one-liner for their market (German, as delivered)."""
    parts = ['CPV ' + '/'.join(sub.get('cpv_prefixes') or ['alle']),
             'Region ' + '/'.join(sub.get('nuts_prefixes') or ['alle'])]
    if min_days(sub):
        parts.append(f'≥{min_days(sub)} Tage bis zur Angebotsfrist')
    return ', '.join(parts)


def main():
    """`python subscriptions.py [--data-dir data] [--as-of YYYY-MM-DD]` —
    validate the storage and print what is in force. The cheapest possible
    check after editing a subscription."""
    import argparse
    from datetime import date
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument('--data-dir', default=config.data_root())
    ap.add_argument('--as-of', default=date.today().isoformat())
    args = ap.parse_args()
    rows = read_all(args.data_dir)
    live = resolve(rows, args.as_of)
    where = storage(args.data_dir)
    src = f'{where[1]} [{where[0]}]' if where else f'{args.data_dir} (no storage yet)'
    print(f'[subscriptions] {src}: {len(rows)} version(s) valid, '
          f'{len(live)} active on {args.as_of}')
    for s in sorted(live, key=lambda s: s['sub_id']):
        gate = ('gated' if s.get('min_relevance') is not None
                and (s.get('profile_refs') or s.get('profile_texts'))
                else 'ungated')
        print(f"  {s['sub_id']:24s} v{s.get('version', 1):<3} "
              f"{describe_market(s)} | {gate}, max_picks={max_picks(s)}")


if __name__ == '__main__':
    main()
