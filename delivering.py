"""Step 4b of the cycle: what each customer actually sees — PARAMETERS.md 9.

`deliver()` is the dispatcher — one run, many views: filter this cycle's
scored open lots per subscription, re-rank and re-tier *within* the slice
(`selection.py`), render the customer's report (`render.py`), and append the
delivery rows that are the frozen record of what that customer saw, stamped
with the model id, the subscription version and the gate fingerprint. Never
a model call, never a store join.

`record_gate_config()` files the gate configuration the first time its
fingerprint is seen, so a stamp on a delivery row resolves from the data
directory alone rather than from git archaeology (PARAMETERS.md 4.3 reads it
back for `/healthz`). `learn_references()` is step 4c: a customer's own wins
become profile references (RELEVANCE.md 9), and it never fails a cycle — a
feedback problem is not a delivery problem.
"""
from __future__ import annotations

import config
import ledger
import render
import selection
import subscriptions
import util


def record_gate_config(paths, config):
    """Append this configuration to the gate-config registry the first time
    its fingerprint is seen, so the stamp on a delivery row is resolvable
    from the data directory alone — not from git archaeology over whichever
    commit was deployed that week. Append-only, one line per configuration,
    like every other ledger here.

    Scoped to the DELIVERY ledger, not to the data dir: preview_report.py and
    rewind_report.py redirect deliveries into a sandbox while still reading the real
    store, and a sandbox experiment must not append a configuration to the
    record of what customers were actually served under."""
    home = paths.deliveries_home
    if any(r.get('fingerprint') == config.fingerprint
           for r in ledger.read(home, 'gate_configs')):
        return False
    ledger.append(home, 'gate_configs',
                  [{'fingerprint': config.fingerprint,
                    'first_seen': util.now_utc().isoformat(timespec='seconds'),
                    **config.as_dict()}])
    print(f'[deliver] new gate configuration recorded: {config.describe()}')
    return True


def learn_references(paths, tenders, awards, args):
    """Step 4c (RELEVANCE.md phase 9): a customer's own wins become profile
    references — including the ones this gate rejected, which are the
    false negatives worth seeing. Derived data: appended to
    data/ledger/learned_refs.jsonl, never to the subscription file, so
    subscription versions keep meaning "the operator decided something".
    Runs before deliver() so this cycle's report already benefits. Never
    fails a cycle — a feedback problem is not a delivery problem."""
    try:
        import feedback
        today = util.now_utc().date().isoformat()
        subs = subscriptions.load(paths.subs_home, today)
        if not subs:
            return []

        def gate_factory():
            import relevance as rel
            return rel.Gate(paths.data, as_of=today)

        return feedback.learn(paths.data, subs, awards, tenders, today,
                              gate_factory=gate_factory)
    except Exception as e:
        print(f'[learn] skipped ({e})')
        return []


def criteria_of(paths, rows):
    """[(award_criterion_kind, price_weight_pct)] per row, from the tender
    store; (None, None) wherever the store cannot say. Any problem reading
    the store reads as unknown — the criterion is a line in the report, not
    a reason to fail the delivery."""
    rows = list(rows)
    try:
        import pandas as pd
        df = pd.read_parquet(paths.store_tenders,
                             columns=['procedure_id', 'lot_id',
                                      'award_criterion_kind',
                                      'price_weight_pct'])
        by = {}
        for r in df.itertuples(index=False):
            by.setdefault((r.procedure_id, r.lot_id),
                          (r.award_criterion_kind if r.award_criterion_kind == r.award_criterion_kind else None,
                           r.price_weight_pct if r.price_weight_pct == r.price_weight_pct else None))
    except Exception as e:                                     # noqa: BLE001
        print(f'[deliver] award criteria unavailable ({e}) — column stays empty')
        return [(None, None)] * len(rows)
    return [by.get((r['procedure_id'], r['lot_id']), (None, None)) for r in rows]


def mail_links(home, sub_id):
    """(feedback_link, footer_html, headers) for one customer, or
    (None, '', None) when the report is written for the file only — no
    address on record means no mail, and no tokens minted for a mail that
    will not exist. Everything customer-facing points at `mailer.app_url()`."""
    import mailer
    import tokens
    cust = subscriptions.customer_get(home, sub_id) or {}
    if not cust.get('contact_email'):
        return None, '', None
    base = mailer.app_url()

    def link(procedure_id, lot_id, verdict):
        tok = tokens.mint(home, 'f', sub_id, procedure_id=procedure_id,
                          lot_id=lot_id, verdict=verdict)
        return f'{base}/f/{tok}'

    footer_html, headers = mailer.footer(home, sub_id)
    return link, footer_html, headers


def send_report(home, sub, today, page, headers, transport=None):
    """The report goes out (doc/ONBOARDING.md 9.3). Through the guarded
    mailer, kind `report` (active customers only — the mailer refuses the
    rest and ledgers why). Never raises: a mail that could not go is one
    printed line and a `send_refused` row, not a failed cycle — the file on
    disk is written either way. -> message id or None."""
    import mailer
    name = render.customer_name(sub)
    subject = f'Murara-Bericht {render.date_de(today.isoformat())} — {name}'
    try:
        mid = mailer.send(home, 'report', sub['sub_id'], subject, page,
                          headers=headers, transport=transport)
        print(f"[deliver] {sub['sub_id']}: report mailed ({mid})")
        return mid
    except mailer.MailerError as e:
        print(f"[deliver] {sub['sub_id']}: report NOT mailed — {e}")
    except Exception as e:                                     # noqa: BLE001
        print(f"[deliver] {sub['sub_id']}: report NOT mailed — "
              f'transport error {e!r}')
    return None


def deliver(paths, scored, args):
    """The dispatcher: one run, many views. Filter this cycle's scored open
    lots per subscription, re-rank and re-tier WITHIN the slice, write the
    customer's report, append delivery-ledger rows (the frozen record of what
    this customer actually saw). Never a model call, never a store join."""
    today = util.now_utc().date()
    subs = subscriptions.load(paths.subs_home, today.isoformat())
    if not subs:
        print('[deliver] no active subscriptions — skipped')
        return 0
    # latest revision per lot: a customer sees each lot once, as last published
    latest = {}
    for row in scored:
        key = (row['procedure_id'], row['lot_id'])
        if key not in latest or str(row['publication_date']) >= str(latest[key]['publication_date']):
            latest[key] = row
    past = ledger.read(paths.deliveries_home, 'deliveries')
    already = {(d['sub_id'], d['procedure_id'], d['lot_id'], str(d['ts'])[:10])
               for d in past}
    by_sub = {}
    for d in past:
        by_sub.setdefault(d['sub_id'], []).append(d)
    cutoff = (util.now_utc() - util.parse_window(args.track_window)).date().isoformat()
    grades_recent = [g for g in ledger.read(paths.ledger_home, 'grades')
                     if str(g['award_pub'])[:10] >= cutoff]
    # receipt fallback for delivery rows written before title/buyer were stamped
    pred_info = ledger.prediction_titles(paths.ledger_home)
    # the award criterion per lot, for the report's „Zuschlag" line (APP.md 8):
    # a prediction row does not carry it, the tender store does
    for row, crit in zip(latest.values(),
                         criteria_of(paths, latest.values())):
        row.setdefault('award_criterion_kind', crit[0])
        row.setdefault('price_weight_pct', crit[1])
    ts = util.now_utc().isoformat(timespec='seconds')
    # relevance gate (RELEVANCE.md phase 3): loaded once per cycle, only when a
    # subscription asks for it; unavailable sidecars degrade to ungated delivery
    # with a loud line, never a failed cycle
    gate = None
    try:
        import relevance as rel
        if any(rel.wants_gate(s) for s in subs):
            # as_of=today unions each customer's learned references
            # (feedback.py); without it a profile is the subscription line
            # alone — see relevance.Gate
            gate = rel.Gate(paths.data, as_of=today.isoformat())
            # the rules this cycle judges under, on the record before any
            # verdict is written (REFACTOR.md phase 3)
            record_gate_config(paths, gate.config)
    except Exception as e:
        print(f'[deliver] relevance gate unavailable ({e}) — delivering ungated')
        gate = None
    n_rows = 0
    for sub in subs:
        profile = None
        if gate is not None and rel.wants_gate(sub):
            try:
                profile = rel.build_profile(gate, sub)
                # a profile with no lexicon and no core root cannot pass ANY
                # lot — the customer gets an empty report every cycle and
                # nothing says why. Say why.
                mute = rel.mute_reason(profile, gate.config)
                if mute:
                    print(f"[deliver] {sub['sub_id']}: ** MUTE PROFILE ** "
                          f'{mute}')
            except Exception as e:
                print(f"[deliver] {sub['sub_id']}: profile error ({e}) — "
                      f'delivering ungated')
        # slice -> gate -> rank -> cap, in selection.py because the all-lots
        # rewind runs the same four steps and must run THESE (REFACTOR.md
        # phase 4). The gate sees the widest candidate set — deadline ignored,
        # since the annex needs a verdict for short-deadline lots too — and
        # near-misses render separately.
        sel = selection.for_sub(sub, latest.values(), today, gate=gate,
                                profile=profile)
        # render.py turns the SliceResult into the two documents and the
        # delivery rows (REFACTOR.md phase 4b). This loop keeps only the
        # dispatch and the writing: everything above is "what does this
        # customer get", everything below is "where does it go".
        receipts = render.receipt_html(grades_recent,
                                       by_sub.get(sub['sub_id'], []), pred_info)
        # `args.mail` False = the report is written for the file only: what
        # preview_report.py wants (a tryout must not mint tokens or mail
        # anyone), and what a dry run wants. The cycle leaves it True.
        feedback_link, footer_html, headers = (
            mail_links(paths.data, sub['sub_id'])
            if getattr(args, 'mail', True) else (None, '', None))
        page, deliveries = render.customer_report(
            sub, sel, today=today, profile=profile, receipts=receipts,
            tier_high=args.tier_high, tier_medium=args.tier_medium,
            ts=ts, already=already, feedback_link=feedback_link,
            footer_html=footer_html)
        annex_name, annex = render.market_annex(
            sub, sel, today=today, profile=profile, top_slice=args.top_slice)
        out = paths.reports / 'subscriptions' / sub['sub_id'] / f'report_{today.isoformat()}.html'
        out.parent.mkdir(parents=True, exist_ok=True)
        (out.parent / annex_name).write_text(annex, encoding='utf-8')
        if page is not None:
            out.write_text(page, encoding='utf-8')
            if feedback_link is not None:
                # an address on record: the same HTML goes out by mail
                # (ONBOARDING.md 9.3); no address -> file only, no attempt
                send_report(paths.data, sub, today, page, headers)
        else:
            # nothing to recommend and nothing graded to look back on -> no
            # report this cycle (decision 2026-08-06); the annex above is
            # still written as the operator's lookup
            print(f"[deliver] {sub['sub_id']}: nothing to report — "
                  f'no report written')
        ledger.append(paths.deliveries_home, 'deliveries', deliveries)
        n_rows += len(deliveries)
        print(f"[deliver] {sub['sub_id']}: {len(sel.picks)} lots delivered "
              f'({len(sel.ranked)} matched, {len(deliveries)} new delivery rows)')
    return n_rows


