"""Render the loop's JSON result files as one human-readable HTML dashboard.

    python render_dashboard.py            # writes data/reports/dashboard.html

Reads (all optional — missing files render as 'pending'):
  models/CURRENT, models/<id>/meta.json, models/registry.jsonl
  data/ledger/predictions.jsonl, data/ledger/grades.jsonl
  data/logs/loop_checkpoint.json, data/logs/drift_latest.json

Self-contained output: inline SVG, no external libraries, works offline.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

import config
import ledger

REPO = Path(__file__).resolve().parent

INK = '#111827'; INK2 = '#6b7280'; GRID = '#e5e7eb'; ACCENT = '#2563eb'; FILL = '#60a5fa'

SECTOR = {'450': 'general construction', '451': 'site preparation', '452': 'civil engineering',
          '453': 'building installation', '454': 'finishing trades'}

MIN_SLICE = 25  # graded lots before a slice's stats are quoted (mirrors --min-slice-grades)


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]


def esc(s):
    return html.escape(str(s))


# ------------------------------------------------------------------ svg pieces

def line_chart(title, points, fmt='{:.2f}', width=430, height=190):
    """Single-series dot+line chart with direct value labels. points: [(label, value)]"""
    if not points:
        return f'<div class="panel"><h3>{esc(title)}</h3><p class="muted">no data yet</p></div>'
    pad_l, pad_r, pad_t, pad_b = 44, 16, 30, 34
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or max(abs(hi), 0.1)
    lo -= span * 0.25; hi += span * 0.25
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def x(i):
        return pad_l + (iw * (i / max(1, len(points) - 1)) if len(points) > 1 else iw / 2)

    def y(v):
        return pad_t + ih * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">']
    parts.append(f'<text x="{pad_l}" y="16" fill="{INK}" font-size="13" font-weight="600">{esc(title)}</text>')
    for gv in (lo + (hi - lo) * f for f in (0.0, 0.5, 1.0)):
        gy = y(gv)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" fill="{INK2}" font-size="10" text-anchor="end">{fmt.format(gv)}</text>')
    if len(points) > 1:
        path = ' '.join(f'{"M" if i == 0 else "L"}{x(i):.1f},{y(v):.1f}' for i, (_, v) in enumerate(points))
        parts.append(f'<path d="{path}" fill="none" stroke="{ACCENT}" stroke-width="2"/>')
    for i, (label, v) in enumerate(points):
        parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" fill="{ACCENT}">'
                     f'<title>{esc(label)}: {fmt.format(v)}</title></circle>')
        parts.append(f'<text x="{x(i):.1f}" y="{y(v) - 9:.1f}" fill="{INK}" font-size="11" '
                     f'text-anchor="middle">{fmt.format(v)}</text>')
        parts.append(f'<text x="{x(i):.1f}" y="{height - 12}" fill="{INK2}" font-size="10" '
                     f'text-anchor="middle">{esc(label)}</text>')
    parts.append('</svg>')
    return f'<div class="panel">{"".join(parts)}</div>'


def histogram(title, values, threshold=None, bins=20, width=880, height=220):
    if len(values) == 0:
        return f'<div class="panel"><h3>{esc(title)}</h3><p class="muted">no data yet</p></div>'
    pad_l, pad_r, pad_t, pad_b = 40, 16, 30, 30
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int(v * bins))] += 1
    peak = max(counts)
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    bw = iw / bins
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">']
    parts.append(f'<text x="{pad_l}" y="16" fill="{INK}" font-size="13" font-weight="600">{esc(title)}</text>')
    for f in (0.5, 1.0):
        gy = pad_t + ih * (1 - f)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" stroke="{GRID}"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" fill="{INK2}" font-size="10" text-anchor="end">{int(peak * f)}</text>')
    for i, c in enumerate(counts):
        if c == 0:
            continue
        bh = ih * c / peak
        bx, by = pad_l + i * bw + 1, pad_t + ih - bh
        label = f'{i / bins:.2f}–{(i + 1) / bins:.2f}: {c} lots'
        parts.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw - 2:.1f}" height="{bh:.1f}" rx="3" '
                     f'fill="{FILL}"><title>{label}</title></rect>')
        if c == peak:
            parts.append(f'<text x="{bx + bw / 2:.1f}" y="{by - 5:.1f}" fill="{INK}" font-size="11" '
                         f'text-anchor="middle">{c}</text>')
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        tx = pad_l + iw * frac
        parts.append(f'<text x="{tx:.1f}" y="{height - 8}" fill="{INK2}" font-size="10" text-anchor="middle">{frac:.2f}</text>')
    if threshold is not None:
        tx = pad_l + iw * threshold
        parts.append(f'<line x1="{tx:.1f}" y1="{pad_t}" x2="{tx:.1f}" y2="{pad_t + ih}" '
                     f'stroke="{INK2}" stroke-width="1.5" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{tx + 4:.1f}" y="{pad_t + 12}" fill="{INK2}" font-size="10">flag cut-off {threshold}</text>')
    parts.append('</svg>')
    return f'<div class="panel">{"".join(parts)}</div>'


def tile(value, label, sub=''):
    sub_html = f'<div class="tile-sub">{esc(sub)}</div>' if sub else ''
    return (f'<div class="tile"><div class="tile-value">{esc(value)}</div>'
            f'<div class="tile-label">{esc(label)}</div>{sub_html}</div>')


def table(headers, rows):
    head = ''.join(f'<th>{esc(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(f'<td>{esc(c)}</td>' for c in row) + '</tr>' for row in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def slice_matrix(grades):
    """One row per industry x region (NUTS-1) with graded outcomes: graded /
    base / top-20% hit / lift. Slices under MIN_SLICE stay greyed with their
    count — the new-branch credibility clock, visibly still running."""
    by = {}
    for g in grades:
        if not g.get('cpv3'):
            continue
        region = str(g['place_nuts3'])[:3] if g.get('place_nuts3') else '?'
        by.setdefault((g['cpv3'], region), []).append(g)
    if not by:
        return '<p class="muted">no graded outcomes with slicing keys yet</p>'
    body = []
    for (cpv3, region), rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        name = f'{cpv3} {SECTOR.get(cpv3, "")}'.strip()
        if len(rows) < MIN_SLICE:
            body.append(f'<tr class="thin"><td>{esc(name)}</td><td>{esc(region)}</td>'
                        f'<td>{len(rows)} (needs {MIN_SLICE})</td><td>—</td><td>—</td><td>—</td></tr>')
            continue
        rs = sorted(rows, key=lambda x: -x['score'])
        k = max(1, len(rs) // 5)
        base = sum(x['label'] for x in rs) / len(rs)
        hit = sum(x['label'] for x in rs[:k]) / k
        lift = f'{hit/base:.1f}x' if base else '—'
        body.append(f'<tr><td>{esc(name)}</td><td>{esc(region)}</td><td>{len(rs)}</td>'
                    f'<td>{base*100:.0f} in 100</td><td>{hit*100:.0f} in 100</td><td>{lift}</td></tr>')
    head = ''.join(f'<th>{esc(h)}</th>' for h in
                   ['industry', 'region', 'graded', 'base', 'top-20% of ranking', 'lift'])
    return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def gate_panel(data):
    """The verdict x outcome join, rendered every cycle so nobody has to
    call `simulation.py check` to see whether the relevance gate's
    admissions beat its rejections on published outcomes. The aggregation
    lives in simulation.verdict_outcomes — one computation, two readers."""
    try:
        import simulation
        rows = simulation.verdict_outcomes(data)
    except Exception as e:                                # noqa: BLE001
        return f'<p class="muted">gate panel unavailable: {esc(repr(e))}</p>'
    if not rows:
        return ('<p class="muted">no graded gate verdicts yet — verdicts are '
                'written each cycle, outcomes arrive with the awards '
                '(~90-day median lag)</p>')
    return (
        '<p>Every winner company in the store is a simulated customer; each '
        'simulated pick carries a relevance-gate verdict written at pick '
        'time. If <b>admit</b> rows beat <b>reject</b> rows on both columns, '
        'the gate is buying precision on real outcomes, not on labels.</p>'
        + table(['gate verdict', 'graded picks', 'ended 0–1 bids',
                 'won by the simulated customer'],
                [[r['verdict'], f'{r["graded"]:,}',
                  f'{r["lonely_rate"]*100:.0f} in 100',
                  f'{r["own_wins"]:,} ({r["own_rate"]*100:.1f}%)']
                 for r in rows]))


def drift_panel(drift):
    if not drift:
        return '<p class="muted">no drift results yet — written by each loop cycle</p>'
    rows = []
    for name, status in drift.get('checks', {}).items():
        cls = ' class="warn"' if str(status).startswith('WARN') else ''
        rows.append(f'<tr{cls}><td>{esc(name)}</td><td>{esc(status)}</td></tr>')
    head = '<th>monitor</th><th>status</th>'
    return (f'<p class="muted">as of {esc(str(drift.get("at", "?"))[:16].replace("T", " "))} UTC</p>'
            f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>')


# ------------------------------------------------------------------- assemble

def main(data_dir=None, models_dir=None):
    # config.data_root / config.models_root, not REPO / '...': the state's
    # location is a deployment's choice, not a fact about where the code
    # happens to sit (doc/STORAGE.md 6.1, and 6.5 for the registry).
    # cycle.py always passes both explicitly.
    data = Path(data_dir) if data_dir else config.data_root()
    models = Path(models_dir) if models_dir else config.models_root()
    out = data / 'reports' / 'dashboard.html'
    registry = read_jsonl(models / 'registry.jsonl')
    # through ledger.py, or the dashboard would keep reading a file the cycle
    # has stopped writing and show a market weeks out of date (STORAGE.md 6.4)
    predictions = ledger.read(data, 'predictions')
    grades = ledger.read(data, 'grades')
    checkpoint = json.loads((data / 'logs' / 'loop_checkpoint.json').read_text(encoding='utf-8')) \
        if (data / 'logs' / 'loop_checkpoint.json').exists() else {}
    drift = json.loads((data / 'logs' / 'drift_latest.json').read_text(encoding='utf-8')) \
        if (data / 'logs' / 'drift_latest.json').exists() else {}
    current = (models / 'CURRENT').read_text(encoding='utf-8').strip() \
        if (models / 'CURRENT').exists() else None
    meta = json.loads((models / current / 'meta.json').read_text(encoding='utf-8')) \
        if current and (models / current / 'meta.json').exists() else {}

    champ_preds = [p for p in predictions if p.get('model') == current]
    scores = [p['score'] for p in champ_preds]
    threshold = meta.get('threshold', 0.5)
    n_flagged = sum(1 for s in scores if s >= threshold)

    tiles = [
        tile(current or '—', 'champion model', f"trained on {meta.get('n_train_lots', '?')} decided lots"),
        tile(f"{meta['val_top_hit']*100:.0f} in 100" if meta.get('val_top_hit') is not None else '—',
             'top-20% picks: lonely rate (validation)',
             f"lift {meta['val_top_lift']:.1f}x vs its window" if meta.get('val_top_lift') else ''),
        tile(f"{meta.get('val_pr_auc', 0):.2f}" if meta.get('val_pr_auc') is not None else '—',
             'sorting score (PR-AUC, validation)', 'chance = the base rate; 1.00 = perfect'),
        tile(f'{len(champ_preds):,}', 'open lots ranked by the champion',
             (lambda h, m: f'{h:,} HIGH · {m:,} MEDIUM tier' if h or m
              else f'{n_flagged:,} above the {threshold} cut-off')(
                 sum(1 for p in champ_preds if p.get('tier') == 'HIGH'),
                 sum(1 for p in champ_preds if p.get('tier') == 'MEDIUM'))),
        tile(f'{len(grades):,}', 'graded outcomes so far',
             'grading starts as awards arrive' if not grades else 'see track record below'),
        tile(checkpoint.get('last_success_at', '—')[:16].replace('T', ' '), 'last successful run',
             f"data through {checkpoint.get('last_success_to', '?')}"),
    ]

    # registry trend as two small multiples (different scales -> never one dual-axis chart)
    reg_rows, pr_points, lift_points = [], [], []
    for r in registry:
        day = str(r.get('trained_at', ''))[:10]
        short = r['model_id'].replace('m2026-', '')
        reg_rows.append([r['model_id'], day, 'yes' if r.get('promoted') else 'no',
                         f"{r['val_pr_auc']:.3f}" if r.get('val_pr_auc') is not None else '—',
                         f"{r['val_top_hit']*100:.0f} in 100" if r.get('val_top_hit') is not None else '—',
                         f"{r['val_top_lift']:.2f}x" if r.get('val_top_lift') is not None else '—'])
        if r.get('val_pr_auc') is not None:
            pr_points.append((short, r['val_pr_auc']))
        if r.get('val_top_lift') is not None:
            lift_points.append((short, r['val_top_lift']))

    # track record (rank-based) or pending panel
    if grades:
        g = sorted(grades, key=lambda x: -x['score'])
        k = max(1, len(g) // 5)
        base = sum(x['label'] for x in g) / len(g)
        hit = sum(x['label'] for x in g[:k]) / k
        by_trade = {}
        for x in grades:
            if x.get('cpv3'):
                by_trade.setdefault(x['cpv3'], []).append(x)
        trade_rows = []
        for cpv3, rows in sorted(by_trade.items()):
            if len(rows) < 10:
                continue
            rs = sorted(rows, key=lambda x: -x['score'])
            tk = max(1, len(rs) // 5)
            tb = sum(x['label'] for x in rs) / len(rs)
            th = sum(x['label'] for x in rs[:tk]) / tk
            trade_rows.append([f'{cpv3} {SECTOR.get(cpv3, "")}', len(rs), f'{tb*100:.0f} in 100',
                               f'{th*100:.0f} in 100', f'{th/tb:.1f}x' if tb else '—'])
        track = (f'<div class="panel"><h3>Verified track record — rank-based (the product view)</h3>'
                 f'<p><b>{len(g)}</b> predicted lots have their outcome. Top 20% of our ranking '
                 f'({k} lots): <b>{hit*100:.0f} in 100</b> ended with 0–1 bids, vs {base*100:.0f} in 100 '
                 f'across all graded lots — <b>lift {f"{hit/base:.1f}x" if base else "—"}</b>.</p>'
                 + (table(['trade', 'graded lots', 'base', 'top-20% of ranking', 'lift'], trade_rows)
                    if trade_rows else '<p class="muted">per-trade rows appear at 10+ graded lots per trade</p>')
                 + '</div>')
    else:
        track = ('<div class="panel"><h3>Verified track record — pending</h3>'
                 f'<p>{len(predictions):,} predictions are written down and frozen; none of their awards '
                 'have been published yet. First grades are expected ~2–4 weeks after the first predictions '
                 '(made 2026-08-01); this panel then fills with the rank-based hit rates, overall and per trade.</p></div>')

    checks = meta.get('gate', {}).get('checks', {})
    check_rows = [[name, status] for name, status in checks.items()]
    fail_rows = [[f'FAILED: {f}', ''] for f in meta.get('gate', {}).get('failures', [])]

    score_stats = ''
    if scores:
        import statistics
        score_stats = table(
            ['ranked lots', 'median score', 'top-20% starts at', f'share above {threshold}'],
            [[f'{len(scores):,}', f'{statistics.median(scores):.2f}',
              f'{sorted(scores, reverse=True)[max(0, len(scores)//5 - 1)]:.2f}',
              f'{n_flagged/len(scores)*100:.0f}%']])

    # A/B arms (doc/EXPERIMENTS.md §4): one verdict line per open experiment.
    # Never fails the dashboard — the experiments module is a convenience here.
    trial_html = ''
    try:
        import experiments
        ov = experiments.overview(data, models, datetime.now(timezone.utc).date().isoformat())
        if ov['open']:
            trial_html = ('<h2>Experiments <span class="muted">(A/B arms — doc/EXPERIMENTS.md)</span></h2>'
                          '<div class="panel">' + ''.join(f'<p>{esc(o["line"])}</p>' for o in ov['open'])
                          + '</div>')
    except Exception as e:                                       # noqa: BLE001
        trial_html = f'<p class="muted">experiments: {esc(str(e))}</p>'

    body = f'''
<h1>TenderMining — model &amp; ledger dashboard</h1>
<p class="muted">generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the loop's result files ·
regenerate with <code>python render_dashboard.py</code></p>

<div class="tiles">{''.join(tiles)}</div>
{trial_html}

<h2>Sorting quality over time <span class="muted">(one point per weekly candidate)</span></h2>
<div class="row">
{line_chart('PR-AUC on the validation window', pr_points)}
{line_chart('Top-20% lift on the validation window', lift_points, fmt='{:.2f}x')}
</div>
<div class="panel">{table(['model', 'trained', 'promoted', 'val PR-AUC', 'top-20% hit', 'top-20% lift'], reg_rows)
                    if reg_rows else '<p class="muted">registry is empty</p>'}</div>

<h2>Current ranking of open tenders</h2>
{histogram(f'Score distribution of the {len(scores):,} open lots ranked by the champion', scores, threshold=threshold)}
<div class="panel">{score_stats}</div>

<h2>Track record</h2>
{track}

<h2>Relevance gate vs reality <span class="muted">(simulated picks for every winner company — SIMULATION.md)</span></h2>
<div class="panel">{gate_panel(data)}</div>

<h2>Slice matrix <span class="muted">(industry × region — which slices carry their weight)</span></h2>
<div class="panel">{slice_matrix(grades)}</div>

<h2>Drift monitors</h2>
<div class="panel">{drift_panel(drift)}</div>

<h2>Champion trust checks</h2>
<div class="panel">{table(['check', 'result'], check_rows + fail_rows) if check_rows or fail_rows
                    else '<p class="muted">no champion meta found</p>'}</div>

<p class="muted">Sources: models/registry.jsonl · models/{esc(current or '?')}/meta.json ·
data/ledger/predictions.jsonl · data/ledger/grades.jsonl · data/logs/loop_checkpoint.json ·
data/logs/drift_latest.json</p>
'''

    page = f'''<!doctype html><html><head><meta charset="utf-8">
<title>TenderMining dashboard</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; color: {INK};
         max-width: 940px; margin: 24px auto; padding: 0 16px; background: #ffffff; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }} h2 {{ font-size: 16px; margin: 28px 0 10px; }}
  h3 {{ font-size: 13px; margin: 0 0 8px; }}
  .muted {{ color: {INK2}; font-size: 12px; font-weight: 400; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin: 16px 0; }}
  .tile {{ border: 1px solid {GRID}; border-radius: 8px; padding: 12px 14px; }}
  .tile-value {{ font-size: 20px; font-weight: 650; }}
  .tile-label {{ font-size: 12px; color: {INK2}; margin-top: 2px; }}
  .tile-sub {{ font-size: 11px; color: {INK2}; margin-top: 4px; }}
  .row {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .panel {{ border: 1px solid {GRID}; border-radius: 8px; padding: 12px; margin: 8px 0; flex: 1; min-width: 300px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid {GRID}; }}
  th {{ color: {INK2}; font-weight: 600; }}
  tr.thin td {{ color: {INK2}; }}
  tr.warn td {{ color: #b45309; font-weight: 600; }}
  svg {{ width: 100%; height: auto; display: block; }}
  code {{ background: #f3f4f6; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
</style></head><body>{body}</body></html>'''

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding='utf-8')
    print(f'[dashboard] wrote {out}')


if __name__ == '__main__':
    main()
