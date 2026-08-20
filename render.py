"""`SliceResult` -> the two customer documents and the delivery rows.

REFACTOR.md phase 4b. `loop.deliver()` did four jobs in one body; phase 4a
lifted out the first three (slice -> gate -> rank -> cap, now
[`selection.py`](selection.py)) and this module is the fourth. What is left in
`deliver()` is the dispatcher: load subscriptions, build the gate once, and
for each subscription call selection, then this, then write the files.

The split is the same one phase 4a made, for the same reason: three renderers
— the weekly report, the market annex, the retrospective receipts — were
sharing one 200-line function body, so none of them could be tested without
running a cycle, and a change to one was a change to all three.

**Nothing here does I/O.** Every function returns a string or a list of rows;
the caller writes them. That is what lets `preview_report.py` render a
customer's report into a sandbox and the cycle write the same bytes into
`data/reports/`, from one implementation.

Behaviour is preserved to the byte. The receipt for the extraction is every
live subscription's report and annex rendered before and after and compared
by SHA-256 — 13 files, all identical.
"""
from __future__ import annotations

import re

import util
from html import escape

import selection
import subscriptions

lot_key = selection.lot_key

MAX_RECEIPTS = 15  # itemized reviewed picks shown per report; the rest is counted


def clean_cell(v, width):
    """Markdown-table-safe cell: collapse all whitespace (newlines break table
    rows), replace pipes (they split cells), truncate."""
    return ' '.join(str(v).split()).replace('|', '/')[:width]


# Customer artifacts are HTML (SUBSCRIPTIONS.md decision 2026-08-05):
# self-contained, inline <style>, e-mail-body-ready.
HTML_STYLE = '''
 body { font-family:-apple-system,"Segoe UI",Roboto,sans-serif; color:#111827;
        max-width:880px; margin:24px auto; padding:0 16px; line-height:1.5; }
 h1 { font-size:20px; } h2 { font-size:15px; margin:26px 0 8px; }
 table { border-collapse:collapse; width:100%; font-size:13.5px; }
 th,td { text-align:left; padding:7px 9px; border-bottom:1px solid #e5e7eb; vertical-align:top; }
 th { color:#6b7280; font-weight:600; }
 a { color:#2563eb; text-decoration:none; }
 ul { padding-left:20px; } li { margin:4px 0; }
 .ok { color:#15803d; font-weight:700; } .miss { color:#b91c1c; font-weight:700; }
 .muted { color:#6b7280; font-size:12.5px; }
 td.v-green { background:#dcfce7; color:#166534; white-space:nowrap; }
 td.v-yellow { background:#fef9c3; color:#854d0e; white-space:nowrap; }
 td.v-red { background:#fee2e2; color:#991b1b; white-space:nowrap; }
'''


def date_de(iso):
    """'2026-08-31' -> '31.08.2026'; anything unparseable -> em dash."""
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', str(iso)[:10])
    return f'{m.group(3)}.{m.group(2)}.{m.group(1)}' if m else '—'

def frist_de(r):
    """The actionable date as the report prints it: the offer deadline, or a
    two-stage lot's participation deadline suffixed so the reader knows the
    action is an application, not yet a bid (doc/MODELING.md 10)."""
    d, part = util.frist(r)
    return date_de(d) + (' (Teilnahmeantrag)' if part else '')


def html_page(title, body_parts):
    return ('<!doctype html><html><head><meta charset="utf-8">\n'
            f'<title>{escape(title)}</title>\n<style>{HTML_STYLE}</style></head>\n<body>\n'
            + '\n'.join(body_parts) + '\n</body></html>\n')


def table_html(headers, body_rows):
    head = ''.join(f'<th>{h}</th>' for h in headers)
    return (f'<table><thead><tr>{head}</tr></thead><tbody>'
            + ''.join(body_rows) + '</tbody></table>')


def gate_stamp(profile, scores):
    """Delivery-row stamps for gated subscriptions (RELEVANCE.md phase 3);
    empty for ungated ones so their rows stay byte-identical to before.

    `gate_config` (REFACTOR.md phase 3) is the fingerprint of the rules that
    judged this lot. Without it a pick delivered under the embedding ladder
    and one delivered under the evidence gate are indistinguishable in the
    ledger, so a customer's retrospective silently pools two different
    decision procedures. The competition model was always stamped; the gate
    that decided the lot was the customer's business at all was not.
    """
    if not profile or scores is None:
        return {}
    from embed import MODEL_TAG
    text, code = scores[0], scores[1]
    cfg = profile.get('config')
    return {'relevance_score': text, 'code_relevance': code,
            'profile_version': profile['version'], 'embed_model_tag': MODEL_TAG,
            **({'gate_config': cfg.fingerprint, 'gate_mode': cfg.mode}
               if cfg is not None else {})}


def receipt_html(grades_recent, sub_deliveries, pred_info, kind='pick'):
    """Receipts before rates (SUBSCRIPTIONS.md): one line per delivered pick
    (or, kind='avoid', per warning) whose outcome has arrived — what we said,
    the bids that came, TED link as proof. Misses render exactly like hits;
    a warning is right when the lot ended contested. Returns an HTML block."""
    grade_by_lot = {(g['procedure_id'], g['lot_id']): g for g in grades_recent}
    by_lot = {}
    for d in sorted(sub_deliveries, key=lambda d: str(d['ts'])):
        by_lot.setdefault((d['procedure_id'], d['lot_id']), []).append(d)
    items = []
    for lot, ds in by_lot.items():
        g = grade_by_lot.get(lot)
        if g is None:
            continue
        before = [d for d in ds if str(d['ts'])[:10] <= str(g['award_pub'])[:10]]
        d = (before or ds)[-1]
        if d.get('kind', 'pick') == kind:
            items.append((d, g))
    if not items:
        # no graded outcome yet -> no section at all (decision 2026-08-06);
        # the retrospective appears once the first Zuschlag is published
        return ''
    items.sort(key=lambda ig: str(ig[1]['award_pub']), reverse=True)
    lis = []
    for d, g in items[:MAX_RECEIPTS]:
        info = pred_info.get((g['procedure_id'], g['lot_id']), {})
        title = escape(clean_cell(d.get('title') or info.get('title')
                                  or f"Los {g['lot_id']}", 60))
        buyer = d.get('buyer_name') or info.get('buyer_name')
        n = g.get('n_tenders')
        outcome = (f"{int(n)} Angebot{'e' if n != 1 else ''}" if n is not None
                   else ('0–1 Angebote' if g['label'] else 'mehr als 1 Angebot'))
        right = (not g['label']) if kind == 'avoid' else bool(g['label'])
        if kind == 'avoid':
            verdict = ('umkämpft wie gewarnt — Angebotskosten gespart' if right
                       else 'am Ende doch ruhig — diese Warnung hat Sie eine '
                            'Chance gekostet')
            said = 'Warnung'
        else:
            verdict = 'kaum Wettbewerb' if right else 'doch umkämpft'
            said = 'Empfehlung'
        nr = g.get('award_publication_number')
        link = (f' · <a href="https://ted.europa.eu/de/notice/-/detail/{escape(str(nr))}">'
                f'TED {escape(str(nr))}</a>' if nr else '')
        mark = ('<span class="ok">✓</span>' if right else '<span class="miss">✗</span>')
        buyer_s = f' ({escape(clean_cell(buyer, 40))})' if buyer else ''
        lis.append(f"<li>{mark} {said}, {date_de(g['award_pub'])} — {title}{buyer_s}: "
                   f'<b>{escape(outcome)}</b> — {escape(verdict)}{link}</li>')
    if len(items) > MAX_RECEIPTS:
        lis.append(f'<li class="muted">…und {len(items) - MAX_RECEIPTS} weitere '
                   f"bewertete {'Warnungen' if kind == 'avoid' else 'Empfehlungen'} "
                   'in Ihrem Lieferprotokoll.</li>')
    out = '<ul>' + ''.join(lis) + '</ul>'
    if kind == 'avoid':
        n_right = sum(1 for d, g in items if not g['label'])
        out += f'<p>Bisher trafen {n_right} von {len(items)} Warnungen zu.</p>'
    return out


# ------------------------------------------------------------ shared cells

def tender_cell(r):
    title = escape(clean_cell(r.get('title') or f"Los {r['lot_id']}", 70))
    nr = r.get('publication_number')
    return (f'<a href="https://ted.europa.eu/de/notice/-/detail/{escape(str(nr))}">'
            f'{title}</a>' if nr else title)


def buyer_cell(r):
    return escape(clean_cell(r.get('buyer_name') or '', 40))


def customer_name(sub):
    return sub.get('name', sub['sub_id'])


def market_line(sub, profile):
    """The customer's market in words, with the profile filter named when one
    is in force — both documents print it, and they must agree."""
    market = subscriptions.describe_market(sub)
    if profile:
        n_refs = len(sub.get('profile_refs') or [])
        n_texts = len(sub.get('profile_texts') or [])
        market += (', gefiltert auf Ihr Profil ('
                   + ' + '.join([f'{n_refs} gewonnene Ausschreibungen'] * (n_refs > 0)
                                + [f'{n_texts} Beschreibung(en)'] * (n_texts > 0))
                   + ')')
    return market


# ------------------------------------------------------- the weekly report

def criterion_line(r):
    """„100 % Preis" / „Preis 70 / Qualität 30" / '' — the award criterion of
    a pick, stated (doc/APP.md 8): a lot may be recommended DESPITE a pure
    price award because few bidders are expected, and the customer should
    read that here, not discover it in the documents."""
    kind = r.get('award_criterion_kind')
    pct = r.get('price_weight_pct')
    try:
        pct = None if pct is None or pct != pct else int(round(float(pct)))
    except (TypeError, ValueError):
        pct = None
    if pct is not None and pct >= 100:
        return '100 % Preis'
    if pct is not None:
        return f'Preis {pct} / Qualität {100 - pct}'
    if kind and 'price' in str(kind).lower():
        return '100 % Preis'
    if kind and 'quality' in str(kind).lower():
        return 'Preis und Qualität'
    return ''


def feedback_cell(r, feedback_link):
    """The two tokened links every shown lot carries (LAUNCH.md 3): one
    question, relevance, answered by one click each. Empty when the report
    is written for the file only."""
    if feedback_link is None:
        return ''
    yes = feedback_link(r['procedure_id'], r['lot_id'], 'ist unser Geschäft')
    no = feedback_link(r['procedure_id'], r['lot_id'], 'nicht unser Geschäft')
    # two separate buttons, not two lines of text: a reader must never take
    # them for one sentence (operator, 2026-08-17). Inline styles because
    # mail clients drop <style>.
    box = ('display:inline-block;padding:3px 10px;margin:3px 0;'
           'border-radius:4px;white-space:nowrap;text-decoration:none;'
           'font-size:90%;')
    return (f'<td class="fb">'
            f'<a href="{escape(yes)}" style="{box}border:1px solid #2a7;'
            f'color:#2a7">✔ Ja, unser Geschäft</a><br>'
            f'<a href="{escape(no)}" style="{box}border:1px solid #c44;'
            f'color:#c44">✘ Nein, nicht unser Geschäft</a></td>')


def ask_html(y_url, number=None, total=None, final=True):
    """The subscribe box on a trial report (LAUNCH.md 3; reworded and put on
    EVERY free mail 2026-08-20 — operator: ask directly, and not only once
    at the end). The question is the ask itself — „Möchten Sie weitere
    Empfehlungen erhalten?" — one button, the standing `y` link. On mails
    before the last the small line says the count and that a click costs
    nothing yet; on the LAST free mail it says what silence means."""
    if final:
        head = ('<b>Das ist Ihre letzte kostenlose Empfehlung.</b> '
                'Möchten Sie weitere erhalten?')
        foot = ('Sonst kommt ab jetzt kein Bericht mehr — nur gelegentlich '
                'eine Nachricht, wie unsere Empfehlungen für Sie ausgegangen '
                'sind. Jede trägt diesen Link wieder; Sie können auch später '
                'jederzeit einsteigen.')
    else:
        head = (f'<b>Empfehlung {number} von {total} kostenlosen.</b> '
                f'Möchten Sie auch danach weitere erhalten?')
        foot = ('Mit dem Klick entsteht noch keine Zahlungspflicht — den '
                'Preis nennen wir Ihnen vorher.')
    return ('<div style="margin:0 0 1.5em;padding:12px 14px;background:#fff8e6;'
            'border-left:3px solid #d9a400">'
            f'<p style="margin:0 0 .5em">{head}</p>'
            f'<p style="margin:0 0 .5em"><a href="{escape(y_url)}" '
            'style="display:inline-block;padding:6px 14px;border:1px solid '
            '#2a6;border-radius:4px;color:#2a6;text-decoration:none">'
            'Ja, weiter mit Murara</a></p>'
            f'<p style="margin:0;font-size:90%;color:#555">{foot}</p>'
            '</div>')


def customer_report(sub, sel, *, today, profile, receipts,
                    tier_high, tier_medium, ts, already,
                    feedback_link=None, footer_html='', ask=''):
    """The weekly report and this cycle's delivery rows.

    Returns `(html_or_None, deliveries)`. `None` means nothing to report —
    no picks and no graded outcome to look back on — in which case the cycle
    writes no report at all (decision 2026-08-06) and the annex still stands
    as the operator's lookup.

    `feedback_link(procedure_id, lot_id, verdict) -> URL` mints the per-lot
    `f` links (doc/APP.md 3); `footer_html` is `mailer.footer`'s block;
    `ask` is `ask_html(...)` on the one report that carries the ask. All
    supplied by the cycle, so the renderer stays free of tokens and
    storage — the file on disk and the mail are the same HTML.

    The delivery rows are built in the same pass as the pick table on purpose:
    they are the frozen record of what this customer actually saw, and a row
    that disagreed with the table above it would be worse than no row.
    """
    judged, rows, top = sel.judged, sel.ranked, sel.picks
    n_high = max(1, round(len(rows) * tier_high))
    n_med = max(1, round(len(rows) * tier_medium))
    name = customer_name(sub)

    # the report never cites how many lots we checked or matched — the size of
    # our haystack is our business, not the customer's (decision 2026-08-05);
    # the product is the short list itself
    # Report copy (decision 2026-08-06): the customer reads exactly two things
    # — is there a recommendation this week, and how did the previous
    # recommendations end. No product prose, no market statistics, no warnings
    # list, no annex mention.
    body = [f'<h1>{escape(name)} — Murara-Bericht — {date_de(today.isoformat())}</h1>']
    if ask:
        body.append(ask)
    body += [
            f'<p class="muted">Ihr Markt: {escape(market_line(sub, profile))}.</p>',
            '<h2>Empfehlungen dieser Woche</h2>']
    if not top:
        body += ['<p><b>Diese Woche keine Empfehlung.</b> Keine offene '
                 'Ausschreibung passte diese Woche eindeutig zu Ihrem Betrieb '
                 'und versprach zugleich so wenig Wettbewerb, dass sie Ihr '
                 'Angebotsbudget wert wäre.</p>']
    else:
        lead = ('Diese Ausschreibung passt zu Ihrem Betrieb und verspricht'
                if len(top) == 1 else
                f'Diese {len(top)} Ausschreibungen passen zu Ihrem Betrieb '
                'und versprechen')
        body += [f'<p>{lead} wenig Wettbewerb. Der Titel führt zur '
                 'offiziellen Bekanntmachung; beachten Sie die Frist.</p>']

    def why_mine_cell(r):
        """Plain-language reason a pick is the customer's business — words
        instead of the (internal) score, so a marginal case is judgeable
        at a glance."""
        why = (judged.get(lot_key(r)) or (None, None, None))[2]
        if not why:
            return ''
        kind, detail = why
        if kind == 'ref' and detail:
            return escape(f'ähnelt Ihrem Auftrag „{clean_cell(detail, 50)}“')
        if kind == 'ref':
            return 'ähnelt Ihrem Profil'
        if kind == 'evidence':
            # phase 8: quote the trade words actually found in the notice
            return escape(f'nennt {clean_cell(detail, 50)}')
        return escape(f'CPV-Code passt: {clean_cell(detail, 50)}')

    deliveries, pick_trs = [], []
    for i, r in enumerate(top):
        tier = 'HIGH' if i < n_high else ('MEDIUM' if i < n_high + n_med else 'LOW')
        why_cells = (f'<td>{why_mine_cell(r)}</td>' if profile else '')
        pick_trs.append(f"<tr><td>{tender_cell(r)}</td>"
                        f"<td>{frist_de(r)}</td>"
                        f'<td>{buyer_cell(r)}</td>'
                        f'{why_cells}'
                        f"<td>{escape(', '.join((r.get('why_lonely') or [])[:2]))}</td>"
                        f'<td>{escape(criterion_line(r))}</td>'
                        f'{feedback_cell(r, feedback_link)}</tr>')
        if (sub['sub_id'], r['procedure_id'], r['lot_id'], ts[:10]) not in already:
            deliveries.append({
                'ts': ts, 'sub_id': sub['sub_id'], 'sub_version': sub.get('version', 1),
                'procedure_id': r['procedure_id'], 'lot_id': r['lot_id'],
                'notice_id': r.get('notice_id'), 'model': r['model'],
                'score': r['score'], 'slice_rank': i + 1,
                'slice_size': len(rows), 'slice_tier': tier,
                'publication_number': r.get('publication_number'),
                'buyer_name': r.get('buyer_name'), 'title': r.get('title'),
                'kind': 'pick',
                **gate_stamp(profile, judged.get(lot_key(r))),
            })
    if pick_trs:
        headers = ['Ausschreibung', 'Frist', 'Auftraggeber']
        if profile:
            headers.append('warum Ihr Geschäft')
        headers.append('warum wir wenige Bieter erwarten')
        headers.append('Zuschlag')
        if feedback_link is not None:
            headers.append('Passt das zu Ihnen?')
        body += [table_html(headers, pick_trs)]
    if receipts:
        body += ['<h2>Ihre Empfehlungen im Rückblick</h2>', receipts]
    elif top:
        # picks but no graded outcome yet: state the accountability promise
        # instead of silence (decision 2026-08-06)
        body += ['<h2>Ihre Empfehlungen im Rückblick</h2>',
                 '<p>Ihre Empfehlungen stehen oben — jede wird dokumentiert, '
                 'und ihr Ergebnis wird hier bewertet, sobald der Zuschlag '
                 'veröffentlicht ist.</p>']
    # the warnings list is gone (decision 2026-08-06): the customer should
    # avoid MOST of the market, so naming five lots was noise; no kind:"avoid"
    # delivery rows are written any more (the ledger records what the customer
    # saw). Historical avoid rows stay and are excluded from the pick receipts
    # by receipt_html's kind filter.
    if footer_html:
        body.append(footer_html)
    page = (html_page(f'{name} — Murara-Bericht {date_de(today.isoformat())}',
                      body)
            if (top or receipts) else None)
    return page, deliveries


# -------------------------------------------------------- the market annex

def market_annex(sub, sel, *, today, profile, top_slice):
    """Every open tender in the slice with its verdict, sorted by deadline.

    Returns `(filename, html)`. The deadline filter is deliberately ignored
    here — a candidate with 10 days left still deserves its verdict, so the
    customer can check THEIR candidates and not only ours. `sel.market` is
    already relevance-gated when the subscription has a profile.

    Written per date but never mentioned in the report (decision 2026-08-06):
    it is the operator's lookup when a customer asks about a specific tender,
    not a customer surface.
    """
    name = customer_name(sub)
    annex_rows = sorted(sel.market, key=lambda r: -r['score'])
    n_crowd = max(1, round(len(annex_rows) * top_slice))
    verdicts = {}
    for rank, r in enumerate(annex_rows):
        if r.get('flag'):
            verdicts[lot_key(r)] = ('v-green', 'wenige Bieter erwartet',
                                    (r.get('why_lonely') or [])[:2])
        elif rank >= len(annex_rows) - n_crowd:
            verdicts[lot_key(r)] = ('v-red', 'viele Bieter erwartet',
                                    (r.get('why_crowded') or [])[:2])
        else:
            verdicts[lot_key(r)] = ('v-yellow', 'durchschnittliche Chancen', [])
    annex_trs = []
    for r in sorted(annex_rows, key=lambda r: str(util.frist(r)[0] or '9999')):
        cls, verdict, why = verdicts[lot_key(r)]
        annex_trs.append(f'<tr><td>{tender_cell(r)}</td>'
                         f"<td>{frist_de(r)}</td>"
                         f'<td>{buyer_cell(r)}</td>'
                         f'<td class="{cls}">{verdict}</td>'
                         f"<td>{escape(', '.join(why))}</td></tr>")
    body = [f'<h1>{escape(name)} — Marktübersicht — {date_de(today.isoformat())}</h1>',
            f'<p>Alle {len(annex_rows)} offenen Ausschreibungen in Ihrem '
            f'Markt ({escape(market_line(sub, profile))}), sortiert nach Frist. Schlagen Sie '
            'jede Ausschreibung nach, die Sie erwägen; das Urteil stammt '
            'aus demselben Modell wie Ihre Empfehlungen und '
            'wird in Ihrem Bericht überprüft.</p>',
            table_html(['Ausschreibung', 'Frist', 'Auftraggeber', 'Urteil',
                        'warum'], annex_trs)]
    if sel.borderline:
        # the borderline band (RELEVANCE.md): near-misses just under the
        # profile gate stay visible, so a miscalibrated gate is discovered
        # by reading, not by silence
        near_trs = [f'<tr><td>{tender_cell(r)}</td>'
                    f"<td>{frist_de(r)}</td>"
                    f'<td>{buyer_cell(r)}</td></tr>'
                    for r in sorted(sel.borderline,
                                    key=lambda r: str(util.frist(r)[0] or '9999'))]
        body += ['<h2>Knapp aussortiert</h2>',
                 f'<p>Diese {len(sel.borderline)} Ausschreibungen lagen knapp '
                 'unter der Ähnlichkeitsschwelle zu Ihrem Profil und wurden '
                 'deshalb nicht in Ihren Markt aufgenommen. Ist eine davon '
                 'doch Ihr Geschäft? Antworten Sie mit der TED-Nummer — '
                 'Ihr Profil lernt daraus.</p>',
                 table_html(['Ausschreibung', 'Frist', 'Auftraggeber'],
                            near_trs)]
    return (f'annex_{today.isoformat()}.html',
            html_page(f'{name} — Marktübersicht {date_de(today.isoformat())}', body))
