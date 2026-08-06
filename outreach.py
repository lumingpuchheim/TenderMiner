"""TenderMining outreach — build the cold-contact target list.

Every winner company in the awards store is a proven tender bidder; this
script turns them into an addressable list. Per company it aggregates the
win history (wins, single-bid wins, trades, regions, declared size, the
publication numbers of the won notices — ready-made ``profile_refs``),
attaches contact details from the raw award XML (the eForms Organizations
block carries each firm's e-mail / phone / city alongside the buyer's),
counts the company's simulated picks (SIMULATION.md — how much product
their market currently gets), and writes one CSV row per company.

Usage:
    python outreach.py                          # small/micro, >=2 wins
    python outreach.py --min-wins 1 --sizes small micro medium unknown
    python outreach.py --out data/outreach/targets.csv

The output is PRIVATE (personal data; ``data/`` is gitignored). Identity is
the exact winner-name string (SIMULATION.md) — spelling variants of one firm
are separate rows; review by hand before mailing anyone. The e-mail
addresses were published for procurement contact, not advertising: sending
promotional e-mail to them without consent is a §7 UWG problem — see
GO_TO_MARKET.md for the channel decision (default: postal letters).
"""

import argparse
import collections
import csv
import glob
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

from embed import load_label_sidecar, load_sidecar
from features import _lfind, _ltext

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

CONTACT_FIELDS = ('email', 'phone', 'city', 'postal_zone', 'website')
MAX_PROFILE_REFS = 6


def win_history(store_dir):
    """One row per (company, won lot result), with trade and region joined in.

    The tenders table supplies cpv_main / place_nuts3 per lot (any notice
    version — the trade of a lot does not change between versions).
    """
    awards = pd.read_parquet(Path(store_dir) / 'awards.parquet')
    tenders = pd.read_parquet(
        Path(store_dir) / 'tenders.parquet',
        columns=['procedure_id', 'lot_id', 'cpv_main', 'place_nuts3'])
    lot_info = (tenders.dropna(subset=['cpv_main'])
                .drop_duplicates(['procedure_id', 'lot_id'])
                .set_index(['procedure_id', 'lot_id']))
    rows = []
    for _, a in awards.iterrows():
        names = a['winner_names']
        if names is None or not len(names):
            continue
        info = (lot_info.loc[(a['procedure_id'], a['lot_id'])]
                if (a['procedure_id'], a['lot_id']) in lot_info.index else None)
        for name in names:
            rows.append({
                'company': str(name).strip(),
                'procedure_id': a['procedure_id'],
                'lot_id': a['lot_id'],
                'winner_size': a['winner_size'],
                'n_tenders': a['n_tenders'],
                'publication_number': a['publication_number'],
                'publication_date': str(a['publication_date'])[:10],
                'cpv3': (str(info['cpv_main'])[:3] if info is not None else None),
                'nuts1': (str(info['place_nuts3'])[:3]
                          if info is not None and pd.notna(info['place_nuts3'])
                          else (str(a['buyer_nuts'])[:3]
                                if pd.notna(a['buyer_nuts']) else None)),
            })
    return pd.DataFrame(rows)


def contacts_from_xml(xml_dir, company_names):
    """company name -> most frequent contact details across award notices.

    Only award notices (the ones with a NoticeResult block) list winner
    organisations; a cheap substring test skips the rest before parsing.
    The most frequent non-null value wins per field — a firm's e-mail is
    stable across its notices, a sender's typo is not.
    """
    counters = collections.defaultdict(
        lambda: {f: collections.Counter() for f in CONTACT_FIELDS})
    files = glob.glob(str(Path(xml_dir) / '*.xml'))
    parsed = 0
    for i, path in enumerate(files):
        text = Path(path).read_text(encoding='utf-8', errors='ignore')
        if 'NoticeResult' not in text:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        parsed += 1
        for org in _lfind(root, 'UBLExtensions/UBLExtension/ExtensionContent/'
                                'EformsExtension/Organizations/Organization'):
            company = next(iter(_lfind(org, 'Company')), None)
            name = _ltext(company, 'PartyName/Name')
            if not name or name.strip() not in company_names:
                continue
            found = {
                'email': _ltext(company, 'Contact/ElectronicMail'),
                'phone': _ltext(company, 'Contact/Telephone'),
                'city': _ltext(company, 'PostalAddress/CityName'),
                'postal_zone': _ltext(company, 'PostalAddress/PostalZone'),
                'website': _ltext(company, 'WebsiteURI'),
            }
            for f, v in found.items():
                if v:
                    counters[name.strip()][f][v] += 1
        if (i + 1) % 5000 == 0:
            print(f'[outreach] scanned {i + 1}/{len(files)} files '
                  f'({parsed} award notices, {len(counters)} firms with contact)',
                  flush=True)
    print(f'[outreach] scanned {len(files)} files, {parsed} award notices, '
          f'{len(counters)} firms with contact details', flush=True)
    return {name: {f: (c[f].most_common(1)[0][0] if c[f] else None)
                   for f in CONTACT_FIELDS}
            for name, c in counters.items()}


def trade_read(df, hist, data_dir, top_k=3):
    """What each firm's won tenders *read as*, via the embedding sidecar.

    The CPV code on a won lot is the buyer's filing choice and can be wrong
    (a hygiene-services firm shows up under 452 because its lot rode a
    construction procedure). The text does not lie: project each firm's won
    lot texts onto the CPV dictionary labels (same trick as
    calibrate.py --fingerprint) and report the closest labels. Adds three
    columns: trade_read (top labels, 'code label' joined by ';'),
    trade_read3 (cpv3 of the best label), trade_match (does the text
    agree with the won-lot codes' primary trade?).
    """
    rows, mat = load_sidecar(data_dir)
    if not rows:
        print('[outreach] no embedding sidecar — trade_read skipped')
        df['trade_read'] = None
        df['trade_read3'] = None
        df['trade_match'] = None
        return df
    _, lrows, lmat = load_label_sidecar(data_dir)
    pos_of = {(r['procedure_id'], r['lot_id']): i for i, r in enumerate(rows)}
    lots_of = hist.groupby('company').apply(
        lambda g: [pos_of[k] for k in zip(g['procedure_id'], g['lot_id'])
                   if k in pos_of], include_groups=False)
    reads, read3, match = [], [], []
    for _, r in df.iterrows():
        emb = lots_of.get(r['company'], [])
        if not emb:
            reads.append(None), read3.append(None), match.append(None)
            continue
        sims = (lmat @ mat[emb].T).max(axis=1)
        top = np.argsort(-sims)[:top_k]
        reads.append(';'.join(
            f"{lrows[i]['code']} {lrows[i]['label_de'][:40]}" for i in top))
        best3 = str(lrows[top[0]]['code'])[:3]
        read3.append(best3)
        match.append(best3 == str(r['trades']).split(';')[0])
    df['trade_read'], df['trade_read3'], df['trade_match'] = reads, read3, match
    n = sum(1 for m in match if m is False)
    print(f'[outreach] trade read: {sum(1 for x in reads if x)} firms read, '
          f'{n} disagree with their won-lot codes')
    return df


def simulated_picks(ledger_path):
    """company -> number of simulated picks (their market's product volume)."""
    picks = collections.Counter()
    p = Path(ledger_path)
    if not p.exists():
        return picks
    for line in p.read_text(encoding='utf-8').splitlines():
        if line.strip():
            picks[json.loads(line)['company'].strip()] += 1
    return picks


def build(args):
    hist = win_history(Path(args.data_dir) / 'store')
    per_company = []
    for company, g in hist.groupby('company'):
        sizes = g['winner_size'].dropna()
        size = (sizes.mode().iloc[0] if len(sizes) else 'unknown')
        trades = g['cpv3'].dropna().value_counts()
        refs = (g.sort_values('publication_date', ascending=False)
                ['publication_number'].dropna().unique()[:MAX_PROFILE_REFS])
        per_company.append({
            'company': company,
            'size': size,
            'wins': len(g),
            'single_bid_wins': int((g['n_tenders'] <= 1).sum()),
            'trades': ';'.join(trades.index),
            'regions': ';'.join(sorted(set(g['nuts1'].dropna()))),
            'last_win': g['publication_date'].max(),
            'profile_refs': ';'.join(refs),
        })
    df = pd.DataFrame(per_company)

    cache = Path(args.data_dir) / 'outreach' / 'contacts.json'
    if cache.exists() and not args.rescan:
        contacts = json.loads(cache.read_text(encoding='utf-8'))
        print(f'[outreach] contacts from cache ({len(contacts)} firms) — '
              f'--rescan to rebuild after a backfill')
    else:
        contacts = contacts_from_xml(Path(args.data_dir) / 'raw' / 'xml',
                                     set(df['company']))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(contacts, ensure_ascii=False),
                         encoding='utf-8')
    for f in CONTACT_FIELDS:
        df[f] = df['company'].map(lambda n: (contacts.get(n) or {}).get(f))

    picks = simulated_picks(Path(args.data_dir) / 'ledger' / 'simulations.jsonl')
    df['sim_picks'] = df['company'].map(picks).fillna(0).astype(int)

    n_all = len(df)
    df = df[(df['wins'] >= args.min_wins) & df['size'].isin(args.sizes)]
    df = trade_read(df, hist, args.data_dir)
    df = df.sort_values(['sim_picks', 'single_bid_wins', 'wins'],
                        ascending=False)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens German umlauts correctly
    df.to_csv(out, index=False, encoding='utf-8-sig',
              quoting=csv.QUOTE_MINIMAL)
    with_mail = int(df['email'].notna().sum())
    print(f'[outreach] {n_all} winner companies -> {len(df)} targets '
          f'(sizes {"/".join(args.sizes)}, >= {args.min_wins} wins), '
          f'{with_mail} with e-mail -> {out}')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--min-wins', type=int, default=2,
                    help='minimum won lot results (default 2: repeat winners)')
    ap.add_argument('--sizes', nargs='+',
                    default=['small', 'micro'],
                    help='declared company sizes to keep '
                         '(small micro medium large sme unknown)')
    ap.add_argument('--out', default='data/outreach/targets.csv')
    ap.add_argument('--rescan', action='store_true',
                    help='rebuild the contact cache from the raw XML')
    build(ap.parse_args())


if __name__ == '__main__':
    main()
