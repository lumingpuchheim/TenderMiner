"""TenderMining test twins — doc/PAYMENT.md 3b.

The operator tests with real firms ("I will frequently add and remove
Jebsen"), and neither Stoppen nor Löschen is a true statement about a real
firm that merely lent its name to a test. A **twin** solves it: a customer
whose profile is built from the real firm's public award history (that is
what makes the test mails real), but whose identity is `test-<slug>` /
"TEST <name>" — so the real firm's row, states and funnel numbers are never
touched, and the admin counts line skips everything `test-`.

A twin is a full customer to the delivery machinery on purpose (operator,
2026-08-22: "i want to receive real monday mails for test companies"): the
Monday cron mails it like anyone else, the trial clock runs — four free
mails, the ask, then silence. `remove` + `add` restarts the trial;
`add --paid` makes the Mondays endless. `send` mails the current report NOW
instead of next Monday, through the same door (it counts as one of the
trial mails, because it is one).

    python testfirm.py add "Jebsen" --email you@example.org [--paid]
    python testfirm.py send "Jebsen"
    python testfirm.py remove "Jebsen"
    python testfirm.py list

`--email` falls back to the first TM_SALES_OWNERS address. `remove` erases
restlos — a twin never gets a Sperrvermerk — and refuses any sub_id that
does not start with `test-`, so this tool cannot delete a real firm.
Console only, prints, writes no files (house rule).
"""

import argparse
import sys
from datetime import datetime, timezone

import config
import invite
import ledger
import subscriptions
import tokens

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

PREFIX = 'test-'


class TestfirmError(ValueError):
    """A twin operation that must not happen."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _event(home, kind, sub_id, detail):
    ledger.append(home, 'app_events', [{
        'ts': _now(), 'kind': kind, 'sub_id': sub_id, 'detail': detail}])


def twin_id(company):
    return PREFIX + invite.slug(company)


def _default_email():
    import sales
    for addr in sales.owners().values():
        return addr
    return None


def add(home, company, *, email=None, paid=False, now=None):
    """-> (sub_id, twin name). The profile comes from the REAL firm's awards
    (outreach.firm via invite.target_row — same door as a real invitation);
    the identity is the twin's own, so `_known_names` never maps the real
    firm's name here and a later REAL invitation of the same firm works
    untouched."""
    email = email or _default_email()
    if not email:
        raise TestfirmError('no address: pass --email or set TM_SALES_OWNERS')
    row = invite.target_row(home, company)
    name = row['company']
    refs = list(row.get('profile_refs') or [])
    if len(refs) < invite.MIN_REFS:
        raise TestfirmError(
            f'{name!r} has {len(refs)} usable profile ref(s); '
            f'{invite.MIN_REFS} are the minimum — pick another firm to twin')
    sub_id = twin_id(name)
    if subscriptions.customer_get(home, sub_id) or [
            r for r in subscriptions.read_all(home)
            if r.get('sub_id') == sub_id]:
        raise TestfirmError(f'{sub_id!r} already exists — '
                            f'`testfirm.py remove "{company}"` first')
    tname = f'TEST {name}'
    subscriptions.customer_update(
        home, sub_id, name=tname, award_names=[tname],
        contact_email=email, consent_at=now or _now(),
        contact_note=f'Testzwilling von {name}', contact_state='active')
    version = {'sub_id': sub_id, 'version': 1, 'active': True,
               'name': tname, 'award_names': [tname],
               'nuts_prefixes': list(row.get('regions') or []) or None,
               'profile_refs': refs, **invite.DRAFT_KNOBS}
    if paid:
        version['plan'] = 'paid'
    subscriptions.append_version(home, version)
    _event(home, 'test_added', sub_id,
           f'twin of {name!r} -> {email}' + (' (paid)' if paid else ''))
    return sub_id, tname


def send(home, company, models_dir=None):
    """Mail the twin its current report NOW — the identical page, links and
    clock-tick the Monday cron would produce, through delivering.deliver
    with only the twin in scope. Subscriptions and delivery rows go to a
    sandbox (the real Monday delivery re-decides freshly); tokens and the
    send are real. -> message id or None (no picks = no mail, honestly)."""
    import argparse as ap
    import shutil

    import pandas as pd

    import delivering
    import training
    import util
    from util import Paths

    sub_id = twin_id(invite.target_row(home, company)['company'])
    today = util.now_utc().date()
    paths = Paths(home, models_dir or config.models_root())
    base = subscriptions.one(paths.subs_home, today.isoformat(), sub_id)
    if base is None:
        raise TestfirmError(f'no twin {sub_id!r} — `testfirm.py add` first')

    sandbox = paths.data / 'tryout' / sub_id
    if sandbox.exists():
        shutil.rmtree(sandbox)
    subscriptions.write_sandbox(sandbox, [base])
    paths.subs_home = sandbox
    paths.deliveries_home = ledger.start(sandbox)
    paths.reports = sandbox / 'reports'

    champ = training.current_champion(paths)
    if champ is None:
        raise TestfirmError('no champion model — run a cycle first')
    scored = [r for r in ledger.read(home, 'predictions')
              if r['model'] == champ['model_id']]
    awards = pd.read_parquet(paths.data / 'store' / 'awards.parquet')
    awarded = set(zip(awards['procedure_id'], awards['lot_id']))
    scored = [r for r in scored
              if (r['procedure_id'], r['lot_id']) not in awarded]
    deadline = pd.to_datetime([r.get('deadline_date') for r in scored],
                              errors='coerce')
    scored = [r for r, d in zip(scored, deadline)
              if pd.isna(d) or d.date() >= today]
    print(f'[testfirm] {len(scored)} open scored lots '
          f'(champion {champ["model_id"]})')
    args = ap.Namespace(track_window='12w', top_slice=0.2, tier_high=0.10,
                        tier_medium=0.20, min_slice_grades=25, mail=True)
    delivering.deliver(paths, scored, args)


def remove(home, company):
    """Erase the twin restlos — no Sperrvermerk, a twin never objected. The
    prefix guard is the whole safety story: this tool cannot touch a real
    firm's row. A Stripe subscription from a test checkout is cancelled
    first; if that fails the erase still proceeds (test-mode money is not
    money) and the id is printed for the dashboard."""
    sub_id = twin_id(invite.target_row(home, company)['company'])
    if not sub_id.startswith(PREFIX):
        raise TestfirmError(f'{sub_id!r} is not a test twin')
    cust = subscriptions.customer_get(home, sub_id)
    if cust is None:
        raise TestfirmError(f'no twin {sub_id!r} on file')
    stripe_sub = cust.get('stripe_subscription_id')
    if stripe_sub:
        import stripe_pay
        try:
            stripe_pay.cancel(stripe_sub)
            print(f'[testfirm] stripe subscription {stripe_sub} cancelled')
        except Exception as e:                                 # noqa: BLE001
            print(f'[testfirm] WARNING stripe cancel failed ({e}) — '
                  f'cancel {stripe_sub} in the dashboard yourself')
    gone = subscriptions.erase(home, sub_id)
    _event(home, 'erased', 'operator',
           f'{sub_id} (' + ', '.join(f'{t}: {n}' for t, n in gone.items())
           + ') restlos, Testzwilling')
    return sub_id, gone


def twins(home):
    """[(sub_id, name, state, plan, email)] of every twin on file."""
    out = []
    today = _now()[:10]
    import db
    con = db.connect(home, create=False)
    if con is None:
        return out
    rows = con.execute(
        "SELECT customer_id FROM customer WHERE customer_id LIKE 'test-%'"
    ).fetchall()
    con.close()
    for row in rows:
        cust = subscriptions.customer_get(home, row['customer_id']) or {}
        sub = subscriptions.one(home, today, row['customer_id'])
        out.append((row['customer_id'], cust.get('name'),
                    cust.get('contact_state') or 'active',
                    (sub or {}).get('plan') or 'trial',
                    cust.get('contact_email')))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--data-dir', default=None)
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add', help='create the twin of a target-list firm')
    a.add_argument('company')
    a.add_argument('--email', help='where its mails go '
                                   '(default: first TM_SALES_OWNERS address)')
    a.add_argument('--paid', action='store_true',
                   help='no trial clock — Monday mails forever')
    s = sub.add_parser('send', help="mail the twin's current report now")
    s.add_argument('company')
    r = sub.add_parser('remove', help='erase the twin restlos')
    r.add_argument('company')
    sub.add_parser('list', help='every twin on file')
    args = p.parse_args(argv)
    home = args.data_dir or config.data_root()
    try:
        if args.cmd == 'add':
            sub_id, tname = add(home, args.company, email=args.email,
                                paid=args.paid)
            print(f'[testfirm] {tname} angelegt ({sub_id}, '
                  f'{"paid" if args.paid else "trial"}) — der Montagsversand '
                  f'nimmt sie ab jetzt mit')
        elif args.cmd == 'send':
            send(home, args.company)
        elif args.cmd == 'remove':
            sub_id, gone = remove(home, args.company)
            print(f'[testfirm] {sub_id} restlos gelöscht: '
                  + ', '.join(f'{t}: {n}' for t, n in gone.items()))
        else:
            rows = twins(home)
            if not rows:
                print('[testfirm] keine Testzwillinge')
            for sub_id, name, state, plan, email in rows:
                print(f'{sub_id:40} {name or "":30} {state:12} {plan:6} '
                      f'{email or "—"}')
    except (TestfirmError, invite.InviteError,
            subscriptions.SubscriptionError) as e:
        sys.exit(f'[testfirm] {e}')


if __name__ == '__main__':
    main()
