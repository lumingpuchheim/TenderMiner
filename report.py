"""Step 5 of the cycle: the operator's weekly report — PARAMETERS.md 9.

Markdown to `data/reports/`, and the flag-view lines that state the track
record in the only honest way available: every rate beside its Wilson
interval and its denominator, so a precision resting on four graded lots
cannot read like one resting on four hundred. Nothing here decides anything;
it is where the cycle says what it did.

This is the module PARAMETERS.md 4.4 will add the weekly knob proposal to —
one line per LIVE knob, `move up / move down / flat / hold`, from
`knobs.py`. That is why the split came first.
"""
from __future__ import annotations

import pandas as pd

import experiments
import ledger
import util


def _rate_ci(rate, ci):
    """'25 in 100 (95% CI 5-70)' — the interval is not decoration; it is the
    difference between a number you may quote and one you may not."""
    if rate is None:
        return '—'
    s = f'{rate*100:.0f} in 100'
    if ci:
        s += f' (95% CI {ci[0]*100:.0f}-{ci[1]*100:.0f})'
    return s


def flag_view_lines(record, args):
    """The precision/recall section: what the binary lonely/not-lonely call was
    worth, next to the one baseline that can beat it for free.

    Printed on every cycle that graded anything — including cycles where the
    model flagged nothing, which is itself a result and used to vanish from
    the report entirely."""
    if not record:
        return []
    f = record.get('flag')
    if not f:
        return []
    lines = ['## The flag: precision and recall '
             f'(binary view at the {args.threshold:.2f} cut-off)', '']

    thin = f['n'] < record.get('min_flag_grades', 0)
    if thin:
        lines += [f"**Too thin to read: {f['n']} graded lots against a floor of "
                  f"{record['min_flag_grades']}.** The numbers below are printed so the "
                  'series exists from day one, not because they mean anything yet — '
                  'awards publish a median 84 days after the tender, so this section '
                  'fills up roughly a quarter behind the predictions. Read the '
                  'confidence intervals, not the point estimates.', '']

    lines += [f"Over the trailing {record['window']}, {f['n']} graded lots: we called "
              f"{f['flagged']} of them lonely, and {f['positives']} really ended with "
              '0-1 bids.', '',
              '| | we said lonely | we said not | total |',
              '|---|---|---|---|',
              f"| **ended 0-1 bids** | {f['tp']} | {f['fn']} | {f['positives']} |",
              f"| **ended 2+ bids** | {f['fp']} | {f['tn']} | {f['n'] - f['positives']} |",
              f"| total | {f['flagged']} | {f['n'] - f['flagged']} | {f['n']} |", '']

    if f['flagged'] == 0:
        lines += ['We flagged nothing in this window, so precision is undefined and '
                  'recall is 0 — every single-bid lot was missed. A cut-off no lot '
                  'clears is a broken cut-off, not a cautious one.', '']
    else:
        lines += [f"- **precision** (the flags right): {_rate_ci(f['precision'], f['precision_ci'])}",
                  f"- **recall** (single-bid lots caught): {_rate_ci(f['recall'], f['recall_ci'])}",
                  f"- **F1**: {f['f1']:.2f}" if f['f1'] is not None else '- **F1**: —',
                  '']

    if f['positives'] == 0:
        # Degenerate window: with nothing to catch, precision is 0 for us AND
        # for the baseline, and comparing the two says nothing about either.
        lines += ['Not one graded lot in this window ended with 0-1 bids, so there was '
                  'nothing to catch: precision is 0 by construction and no comparison '
                  'against a baseline means anything here. Wait for a window that '
                  'contains positives.', '']
        return lines

    lines += ['Against the only free baseline — **call every lot lonely**: '
              f"precision {f['base']*100:.0f} in 100, recall 100 in 100"
              + (f", F1 {f['base_f1']:.2f}." if f['base_f1'] is not None else '.'), '']
    if f['precision'] is not None and not f['beats_base']:
        lines += [f"**The flag is not paying for itself:** its precision "
                  f"({f['precision']*100:.0f} in 100) is at or below the "
                  f"{f['base']*100:.0f} in 100 you get by flagging everything, so at this "
                  'cut-off the model is costing recall and buying nothing. '
                  + ('On this sample that is noise, not a verdict.' if thin else
                     'On this sample that is a real finding — move the cut-off or '
                     'retrain before quoting the flag to anyone.'), '']

    trades = [t for t in record.get('trades', []) if t.get('flag')]
    if trades:
        lines += [f"Per trade (trades with at least {args.min_trade_grades} graded lots):", '',
                  '| trade | graded | flags | precision | recall | flag everything |',
                  '|---|---|---|---|---|---|']
        for t in trades:
            tf = t['flag']
            prec = f"{tf['precision']*100:.0f} in 100" if tf['precision'] is not None else '—'
            rec = f"{tf['recall']*100:.0f} in 100" if tf['recall'] is not None else '—'
            lines.append(f"| {t['cpv3']} {t['name']} | {tf['n']} | {tf['flagged']} | "
                         f"{prec} | {rec} | {tf['base']*100:.0f} in 100 |")
        lines.append('')
    return lines


def report(paths, tenders, args, record, gate, drift, model_id, n_graded, n_predicted,
           trial_lines=(), knob_lines=()):
    latest_model = ledger.prediction_latest_per_lot(
        paths.ledger_home,
        exclude_models=experiments.shadow_models(paths.models, paths.ledger_home))
    open_rows = sorted(latest_model.values(), key=lambda r: -r['score'])

    info = {}
    for t in tenders.itertuples():
        info[(t.procedure_id, t.lot_id)] = t
    lines = [f'# TenderMining weekly report — {util.now_utc().date().isoformat()}', '']
    if record and record.get('top'):
        t = record['top']
        lines += ['## Verified track record (rank-based — the product view)', '',
                  f"Over the trailing {record['window']}: {record['graded']} predicted lots got their outcome. "
                  f"Of the **top {record['top_share']:.0%} of our ranking** ({t['k']} lots), "
                  f"**{t['hit']*100:.0f} in 100 ended with 0-1 bids**, vs {t['base']*100:.0f} in 100 "
                  f"across all graded lots — **lift {t['lift']:.1f}x**." if t['lift'] is not None else
                  'Top-slice lift not computable (no positives in the window).',
                  '']
        if record['trades']:
            lines += ['Per trade (trades with enough graded lots):', '']
            for tr in record['trades']:
                lines.append(f"- {tr['cpv3']} {tr['name']}: top {record['top_share']:.0%} of our ranking hit "
                             f"{tr['hit']*100:.0f} in 100, base {tr['base']*100:.0f} in 100 "
                             f"(lift {tr['lift']:.1f}x, {tr['n']} graded lots)")
            lines.append('')
        if record.get('tiers'):
            lines += ['What each tier really meant (graded outcomes per tier):', '']
            for t_ in record['tiers']:
                lines.append(f"- {t_['tier']}: {t_['hit']*100:.0f} in 100 ended with 0-1 bids "
                             f"({t_['n']} graded lots)")
            lines.append('')
    else:
        lines += ['## Verified track record', '',
                  'No graded outcomes in the window yet — grading starts as awards arrive.', '']

    lines += flag_view_lines(record, args)

    lines += [f'## This week\'s shortlist (top {args.report_top} of the ranking)', '',
              '| tier | score | deadline | est. value | title |', '|---|---|---|---|---|']
    for r in open_rows[:args.report_top]:
        t = info.get((r['procedure_id'], r['lot_id']))
        title = (str(getattr(t, 'title', ''))[:60] if t is not None else '')
        value = getattr(t, 'est_value_lot', None) if t is not None else None
        value = f'{value:,.0f}' if isinstance(value, (int, float)) and pd.notna(value) else ''
        lines.append(f"| {r.get('tier') or ''} | {r['score']:.2f} | "
                     f"{str(r.get('deadline_date'))[:10]} | {value} | {title} |")

    lines += ['', '## Health', '',
              f'- candidate model: {model_id} ({ "promoted" if gate and not gate.get("warnings") else "see warnings"})',
              f'- newly graded lots: {n_graded}',
              f'- new predictions: {n_predicted}']
    if gate:
        for name, status in gate.get('checks', {}).items():
            lines.append(f'- check {name}: {status}')
        for fmsg in gate.get('failures', []):
            lines.append(f'- TRUST CHECK FAILED: {fmsg}')
        for wmsg in gate.get('warnings', []):
            lines.append(f'- WARNING: {wmsg}')
    if drift:
        for name, status in drift['checks'].items():
            lines.append(f'- drift {name}: {status}')
        for wmsg in drift['warnings']:
            lines.append(f'- DRIFT WARNING: {wmsg}')
    if trial_lines:
        lines += ['', '## Experiments (doc/EXPERIMENTS.md)', '']
        lines += [f'- {tl}' for tl in trial_lines]
    if knob_lines:
        # PARAMETERS.md 8.3: the software proposes, the operator decides among
        # what it proposed. The line is a proposal even when it reads like an
        # instruction — nothing here has moved a value.
        lines += ['', '## Knobs (doc/PARAMETERS.md §8)', '']
        lines += [kl if kl.startswith('-') else f'- {kl}' for kl in knob_lines]

    paths.reports.mkdir(parents=True, exist_ok=True)
    out = paths.reports / f'report_{util.now_utc().date().isoformat()}.md'
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'[report] {out}')
    return out

