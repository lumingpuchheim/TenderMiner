"""Which winner names are the same company.

The winner's name on an award notice is free text a clerk typed into a box, and
nothing checks it. So the awards store holds "SVA GmbH" (31 wins) beside "SVA
System Vertrieb Alexander GmbH" (233) — one company, two firms as far as the
prospect list is concerned — and 815 pairs that differ only in upper and lower
case. It also holds names that are not companies at all: a buyer with no field
for "the winners are not published" types the explanation into the name box.

Nothing in the notice can be trusted on its own. Names are mistyped, the
registration number is mistyped too, and a firm's postcode changes when a
branch office fills the form. What saves us is that they are wrong
INDEPENDENTLY: the clerk who mistypes the name rarely also mistypes the VAT
number, and the firm's other thirty notices outvote the one bad one.

So identity here is a vote of three, with one rule about who may veto:

  * the registration number (VAT or Handelsregister) may say "same" and may
    also say "DIFFERENT", and only it may veto. It is the one field that means
    something specific — on the live store it is what keeps `Siemens AG` apart
    from `Siemens Healthineers AG`, `Becker GmbH & Co. KG` from `Becker &
    Partner Baugesellschaft mbH`, and `STRABAG GmbH` from `STRABAG AG`. It
    also merges a group that files under one VAT number (see
    `_forms_conflict`), which is the price of trusting it.
  * the name may say "same" when nothing contradicts it.
  * the postcode may only SUPPORT a merge, never block one — a firm's branch
    office in another town is still that firm (`XERVON GmbH` / `Xervon GmbH`
    were blocked by exactly that before this rule was written).

Measured over 200 construction duplicate pairs: 192 merged, 8 held back by
genuinely different registration numbers. Over 150 hard cases — different firms
sharing a leading word — 2 merged, and both were the same legal entity under a
branch name. The eight are not a failure: they are the short list a person
looks at, instead of the 22,034 names nobody can read.

Merging is never destructive: a cluster keeps every spelling it was built from,
so a letter can still quote the exact string TED published.
"""

import collections
import difflib
import re
from pathlib import Path

# A name that is really a sentence, a note to the reader, or an empty form. The
# `Keine Angabe …` one covers five BITMARCK lots whose winners are lawfully
# unpublished (§ 39 Abs. 6 VgV) — TED has no field for that, so the explanation
# lands in the name box and we file it as a firm with five wins.
NOT_A_FIRM = re.compile(
    r'^\s*(keine angabe|vertraulich|wird nicht ver(ö|oe)ffentlicht|entf(ä|ae)llt'
    r'|unbekannt|nicht bekannt|anonym|n\.?\s?a\.?|k\.?\s?a\.?|s\.?\s?o\.?|[-–.\s]*)\s*$',
    re.I)
LOOKS_LIKE_PROSE = re.compile(
    r'keine angabe|unterbleibt|nicht ver(ö|oe)ffentlicht|^\s*hinweis\b'
    r'|zur wahrung des lauteren wettbewerbs', re.I)

# Boilerplate a buyer pasted in front of a real name — "Impressum  Angaben
# gemäß § 5 DDG  Hans Andritter GmbH" is Hans Andritter GmbH.
PREFIX = re.compile(
    r'^\s*(impressum(\s+angaben\s+gem(ä|ae)ß\s+§?\s*\d*\s*\w*)?'
    r'|firma,?\s+die\s+den\s+auftrag\s+erhalten\s+hat:?'
    r'|firma|z\.?\s?h(d|dn)\.?|herrn?|frau)\s+', re.I)
# A postal address glued onto the name: "… GmbH, Borsigstr. 26, 65205 Wiesbaden".
# The cut goes at the FIRST part that reads like an address, not the last, or
# the street stays attached to the firm.
POSTCODE = re.compile(r'\b\d{5}\b')
STREET = re.compile(r'(str(\.|aße|asse)\b|\bweg\s+\d|\ballee\s+\d|\bplatz\s+\d'
                    r'|\b\w+(str|weg|platz|allee)\.?\s+\d+)', re.I)

LEGAL_FORM = re.compile(
    r'\b(gmbh|mbh|ggmbh|gesmbh|ag|se|kg|kgaa|ohg|ug|gbr|e\.?\s?k|co|und|aktien'
    r'gesellschaft|haftungsbeschr(ä|ae)nkt|b\.?v|s\.?r\.?o|n\.?v|ltd|inc)\b', re.I)

# The number printed in the form's own help text, and the numbers a person
# types when the box will not let them past. `DE123456789` is filled in by 37
# unrelated firms in this corpus — one of them is KPMG — so it identifies
# nobody, and left alone it chains every firm that used it into one company.
FAKE_ID = re.compile(r'^(0+|(\d)+|123456789\d*|1234567890?|987654321\d*'
                     r'|111111111\d*|999999999\d*)$')

VAT = re.compile(r'^de[\s.\-/]*(\d[\d\s.\-/]{6,14}\d)$', re.I)
REGISTER = re.compile(r'^(hra|hrb|vr|gnr)[\s.\-/]*(\d+)', re.I)

NAME_STRONG = 0.90      # cores this alike are one firm unless the number says no
NAME_WEAK = 0.84        # ... this alike needs the postcode to agree as well
MIN_CORE = 6            # "bau" must never swallow "Baumann"
MAX_BLOCK = 400         # a leading word shared by more names than this says nothing

# What identity needs out of the awards store, and all it needs.
COLUMNS = ('winner_names', 'winner_national_ids', 'winner_postal_zones')


def is_firm_name(name):
    """False for a name that is not a company: a placeholder, or the prose a
    buyer typed when the form had no box for what they meant."""
    if not name or not str(name).strip():
        return False
    text = ' '.join(str(name).split())
    return not (NOT_A_FIRM.match(text) or LOOKS_LIKE_PROSE.search(text))


def clean_name(name):
    """The name as a person would write it: one line, no pasted boilerplate, no
    address glued on the end. Only ever used to DECIDE identity — every original
    spelling is kept, because a letter must quote what TED published."""
    text = ' '.join(str(name or '').split())
    text = PREFIX.sub('', text)
    if POSTCODE.search(text):
        parts = [p.strip() for p in re.split(r'[,;]', text)]
        cut = next((i for i in range(1, len(parts))
                    if any(POSTCODE.search(p) or STREET.search(p)
                           for p in parts[i:])), None)
        stripped = (', '.join(parts[:cut]) if cut
                    else POSTCODE.split(text)[0]).strip(' ,;-')
        # Enough of a name left to still be one — "GW-TEC GmbH" is short but
        # real, so this is a floor on letters, not on the identity core.
        if len(re.sub(r'[^a-zäöüß]', '', stripped.casefold())) >= 3:
            text = stripped
    return text.strip(' ,;-')


def normalise_id(raw):
    """-> ('vat', digits) | ('reg', 'HRB123') | None.

    The box labelled "national registration number" holds VAT numbers,
    Handelsregister numbers, telephone numbers, UUIDs and the word "keine".
    Only the first two mean anything, so only those come back; everything else
    is None, and a firm with None simply falls back to name and postcode.

    The registering court is dropped ('HRB 39150 Leipzig' -> 'HRB39150'):
    the same firm is written with and without it.
    """
    if raw is None:
        return None
    text = ' '.join(str(raw).split())
    m = VAT.match(text)
    if m:
        digits = re.sub(r'\D', '', m.group(1))
        if FAKE_ID.match(digits) or not 8 <= len(digits) <= 12:
            return None
        return ('vat', digits)
    m = REGISTER.match(text)
    if m:
        return ('reg', m.group(1).upper() + m.group(2))
    return None


def core(name):
    """The letters that carry the firm's identity — no legal form, no
    punctuation, no case. "PROFI Engineering Systems AG" and "Profi
    Engineering Systems AG" both become 'profiengineeringsystems'."""
    text = LEGAL_FORM.sub(' ', ' '.join(str(name or '').split()).casefold())
    return re.sub(r'[^a-zäöüß]', '', text)


def legal_forms(name):
    """The legal-form words in a name, as a set — {'gmbh', 'co', 'kg'}.

    `core` throws these away so that case and punctuation cannot split a firm,
    but that makes "Bechtle GmbH" and "Bechtle AG" identical, and they are two
    different companies (as are "STRABAG AG" and "STRABAG GmbH"). So when both
    names state a legal form and the forms disagree, the NAME may no longer
    merge them — only a matching registration number can.
    """
    text = ' '.join(str(name or '').split()).casefold()
    return {m.group(0).replace('.', '').replace(' ', '')
            for m in LEGAL_FORM.finditer(text)} - {'und'}


def one_typo_apart(a, b):
    """True when two registration numbers differ by at most one character —
    'DE 349 570 513' and 'DE 349 507 513' are the same firm and one slip of a
    finger. Anything looser starts merging strangers."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    return sum(1 for d in difflib.ndiff(a, b) if d[0] != ' ') <= 2


CONSORTIUM = re.compile(r'^(arge|bg|bige|biege|bietergemeinschaft|arbeitsgemein'
                        r'schaft|bietergemeinsschaft|konsortium)', re.I)


def first_word(name):
    """The word a firm's spellings almost always agree on. "STRABAG AG,
    Direktion Nord" and "STRABAG GmbH" both start with strabag; a consortium
    marker in front is skipped, because "BG STRABAG / ..." is still about
    strabag."""
    text = CONSORTIUM.sub('', ' '.join(str(name or '').split()), count=1)
    for word in re.split(r'[^a-zäöüß]+', text.casefold()):
        if len(word) >= 2:
            return word
    return ''


def discount_stray_ids(population):
    """Drop the registration numbers that are somebody ELSE's.

    A clerk filling in a winner sometimes pastes the wrong number — the buyer's,
    or the firm's from the line above. One such slip is enough to weld two
    companies together, because a single shared number then links their whole
    families: `Otis GmbH` arrived inside `STRABAG AG` that way.

    So a number gets a majority vote of its own. Whichever leading word it is
    used with most often owns it; a name that starts with a different word and
    used the number once does not get to keep it. Real branch spellings agree on
    that first word — 67 STRABAG spellings share one VAT number and all of them
    begin with strabag — so this costs nothing and removes the strays.
    """
    holders = collections.defaultdict(collections.Counter)
    for f in population:
        for kind in list(f.ids):
            for number, seen in f.ids[kind].items():
                holders[(kind, number)][first_word(f.clean)] += seen
    dropped = 0
    for f in population:
        word = first_word(f.clean)
        for kind in list(f.ids):
            for number in list(f.ids[kind]):
                votes = holders[(kind, number)]
                best = max(votes.values())
                owners = {w for w, n in votes.items() if n == best}
                if word not in owners:
                    del f.ids[kind][number]
                    dropped += 1
            if not f.ids[kind]:
                del f.ids[kind]
    return dropped


class Firm:
    """One winner spelling and what the notices say about it.

    `ids` counts every usable registration number seen for this spelling, per
    kind, so a single mistyped notice cannot outvote thirty good ones.
    """

    def __init__(self, name, wins=0):
        self.name = name
        self.clean = clean_name(name)
        self.core = core(self.clean)
        self.wins = wins
        self.ids = collections.defaultdict(collections.Counter)
        self.zips = collections.Counter()

    def saw(self, national_id=None, postal_zone=None, wins=1):
        self.wins += wins
        kind = normalise_id(national_id)
        if kind:
            self.ids[kind[0]][kind[1]] += 1
        zone = str(postal_zone or '').strip()
        if re.match(r'^\d{5}$', zone):
            self.zips[zone] += 1
        return self

    def numbers(self, kind):
        """Every number of this kind, commonest first — the majority leads, the
        stragglers are still allowed to match."""
        return [n for n, _ in self.ids.get(kind, collections.Counter()).most_common()]

    def __repr__(self):
        return f'<Firm {self.name!r} wins={self.wins}>'


def id_verdict(a, b):
    """What the registration numbers say: 'same', 'different', or None when the
    two firms have no kind of number in common. Compared within a kind only —
    one notice giving a VAT number and the next an HRB number is the ordinary
    case, not a contradiction."""
    verdict = None
    for kind in set(a.ids) & set(b.ids):
        if any(one_typo_apart(x, y)
               for x in a.numbers(kind) for y in b.numbers(kind)):
            return 'same'
        verdict = 'different'
    return verdict


def compare(a, b):
    """-> ('merge' | 'block' | 'unknown', why). The whole rule, in one place."""
    number = id_verdict(a, b)
    if number == 'different':
        return 'block', 'registration numbers differ'
    if number == 'same':
        return 'merge', 'same registration number'
    if len(a.core) < MIN_CORE or len(b.core) < MIN_CORE:
        return 'unknown', 'name too short to judge, no number to check'
    forms_a, forms_b = legal_forms(a.clean), legal_forms(b.clean)
    if forms_a and forms_b and forms_a != forms_b:
        return 'unknown', 'same name but a different legal form'
    likeness = difflib.SequenceMatcher(None, a.core, b.core).ratio()
    if likeness >= NAME_STRONG:
        return 'merge', 'same name'
    if likeness >= NAME_WEAK and (set(a.zips) & set(b.zips)):
        return 'merge', 'near-identical name, same postcode'
    return 'unknown', 'nothing links them'


class _Union:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry


class Cluster:
    """One company, and every spelling the notices gave it."""

    def __init__(self, members, why):
        self.members = sorted(members, key=lambda f: (-f.wins, f.name))
        self.name = self.members[0].name          # the spelling with most wins
        self.spellings = [f.name for f in self.members]
        self.wins = sum(f.wins for f in self.members)
        self.why = why                            # reasons the merges rested on
        self.ids = sorted({f'{k}:{n}' for f in self.members
                           for k in f.ids for n in f.numbers(k)})
        self.zips = sorted({z for f in self.members for z in f.zips})

    @property
    def proven(self):
        """True when at least one merge in this cluster rested on a matching
        registration number — the difference between "we know" and "the names
        look alike"."""
        return any('registration number' in w for w in self.why)

    def __repr__(self):
        return f'<Cluster {self.name!r} spellings={len(self.spellings)} wins={self.wins}>'


def _blocks(firms):
    """Candidate pairs only. 22,034 names is 243 million pairs; comparing names
    that share nothing is both slow and pointless. Three cheap ways to be
    plausibly the same firm: the same name ignoring case, the same registration
    number, or the same leading word."""
    by = collections.defaultdict(list)
    for f in firms:
        by[('name', f.clean.casefold())].append(f)
        for kind in f.ids:
            for number in f.numbers(kind):
                by[('id', kind, number)].append(f)
        first = re.split(r'[^a-zäöüß]+', f.clean.casefold())
        first = next((w for w in first if len(w) >= 4), None)
        if first:
            by[('word', first)].append(f)
    for key, group in by.items():
        # A leading word shared by hundreds of firms ("elektro") is not evidence;
        # anything real in there is caught by name or number anyway.
        if key[0] == 'word' and len(group) > MAX_BLOCK:
            continue
        if len(group) > 1:
            yield group


def _conflict(ids_a, ids_b):
    """True when two GROUPS of spellings hold registration numbers of the same
    kind that do not match — the cluster-level version of the pair rule."""
    for kind in set(ids_a) & set(ids_b):
        if not any(one_typo_apart(x, y) for x in ids_a[kind] for y in ids_b[kind]):
            return True
    return False


def _forms_conflict(forms_a, forms_b):
    """True when two groups state different legal forms.

    The pair rule already refuses "Bechtle GmbH" and "Bechtle AG" on the name.
    It was not enough: the bare spelling "Bechtle" states no form, so it merges
    with each of them and chains all three into one company — which is what a
    store WITHOUT registration numbers showed the first time this ran. A group
    inherits every form its members state, and a name-based merge across two
    stated forms is refused.

    A matching registration number still overrides this, and it is worth being
    clear about what that costs. On the live store `Bechtle AG` carries VAT
    DE143849458 on 27 notices and `Bechtle GmbH` carries the same number among
    its own, because a group files under one VAT and that is what a buyer
    types. So they merge, and the parent and its subsidiaries become one
    company here. That is what the notices say; the store cannot tell them
    apart, and pretending otherwise would mean trusting the typed name over
    the number everywhere else too. Where it matters to a person, the pair is
    in the receipt's "same name, two numbers" list to be read."""
    return bool(forms_a and forms_b and not (forms_a & forms_b))


def resolve(firms):
    """-> (clusters, blocked) — the firms grouped into companies, and the pairs
    a registration number refused to merge. Those are the list a person reads.

    Merges are checked against the whole cluster, never just the pair that
    proposed them. Without that, one bad link welds two families together:
    `Otis GmbH` and `STRABAG AG` are blocked when compared directly, and still
    ended up in one company because a third spelling matched both and
    union-find is transitive. A merge that would put two conflicting
    registration numbers under one roof is refused and reported.

    Proven merges are made first, so that the structure a registration number
    can vouch for exists before a mere resemblance is allowed to extend it.
    """
    firms = [f for f in firms if is_firm_name(f.name)]
    discount_stray_ids(firms)
    proposals, blocked, seen = [], [], set()
    for group in _blocks(firms):
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pair = tuple(sorted((id(a), id(b))))
                if pair in seen:
                    continue
                seen.add(pair)
                verdict, reason = compare(a, b)
                if verdict == 'merge':
                    proposals.append((0 if 'registration' in reason else 1,
                                      -(a.wins + b.wins), a, b, reason))
                elif verdict == 'block' and (
                        difflib.SequenceMatcher(None, a.core, b.core).ratio()
                        >= NAME_WEAK):
                    blocked.append((a, b, reason))
    proposals.sort(key=lambda p: (p[0], p[1]))

    union = _Union()
    ids = {}          # cluster root -> {kind: set of numbers}
    forms = {}        # cluster root -> the legal forms its spellings state
    why = collections.defaultdict(list)
    for f in firms:
        root = union.find(id(f))
        ids[root] = {k: set(f.ids[k]) for k in f.ids if f.ids[k]}
        forms[root] = legal_forms(f.clean)
    refused = 0
    for _, _, a, b, reason in proposals:
        ra, rb = union.find(id(a)), union.find(id(b))
        if ra == rb:
            continue
        if _conflict(ids.get(ra, {}), ids.get(rb, {})):
            refused += 1
            blocked.append((a, b, 'another spelling in one of the two groups '
                                  'has a different registration number'))
            continue
        if ('registration' not in reason
                and _forms_conflict(forms.get(ra, set()), forms.get(rb, set()))):
            refused += 1
            blocked.append((a, b, 'the two groups state different legal forms '
                                  'and no registration number says otherwise'))
            continue
        merged_ids = dict(ids.get(ra, {}))
        for kind, numbers in ids.get(rb, {}).items():
            merged_ids[kind] = merged_ids.get(kind, set()) | numbers
        merged_forms = forms.get(ra, set()) | forms.get(rb, set())
        merged_why = why.pop(ra, []) + why.pop(rb, []) + [reason]
        union.union(ra, rb)
        root = union.find(ra)
        ids[root] = merged_ids
        forms[root] = merged_forms
        why[root] = merged_why

    grouped = collections.defaultdict(list)
    for f in firms:
        grouped[union.find(id(f))].append(f)
    clusters = [Cluster(members, why.get(root, []))
                for root, members in grouped.items()]
    clusters.sort(key=lambda c: (-c.wins, c.name))
    return clusters, blocked


def from_awards(awards):
    """-> [Firm] built from an awards frame carrying the winner identity
    columns. One Firm per exact spelling; the notices are folded into it."""
    firms = {}
    columns = set(getattr(awards, 'columns', []))
    for row in awards.itertuples(index=False):
        names = getattr(row, 'winner_names', None)
        if names is None or isinstance(names, float) or not len(names):
            continue
        ids = getattr(row, 'winner_national_ids', None) if \
            'winner_national_ids' in columns else None
        zips = getattr(row, 'winner_postal_zones', None) if \
            'winner_postal_zones' in columns else None
        for i, raw in enumerate(names):
            name = str(raw).strip()
            if not name:
                continue
            firm = firms.get(name)
            if firm is None:
                firm = firms[name] = Firm(name)
            firm.saw(_at(ids, i), _at(zips, i))
    return list(firms.values())


def _at(values, i):
    if values is None or isinstance(values, float):
        return None
    try:
        return values[i] if i < len(values) else None
    except TypeError:
        return None


# ---------------------------------------------------------------- the store

_cache = {'stamp': None, 'groups': None}


def resolve_store(store_dir):
    """-> (clusters, blocked) for a whole awards store. Twenty seconds over
    22,000 names, which is why exactly one thing calls it — `admin.build_index`,
    the index file's only writer — and everything else reads the answer.

    A store written before the identity columns existed still works: the
    columns are simply absent and the vote falls back to names alone."""
    import pandas as pd
    import pyarrow.parquet as pq
    path = Path(store_dir) / 'awards.parquet'
    have = set(pq.read_schema(path).names)
    awards = pd.read_parquet(path, columns=[c for c in COLUMNS if c in have])
    return resolve(from_awards(awards))


def groups(data_dir, index_file='admin_index.json'):
    """-> {spelling: the company's name} for every winner spelling.

    Read from the operator index, which `admin.build_index` writes with each
    firm's spellings; recomputed from the store only if that file is missing or
    older than the store. A spelling nobody merged maps to itself, so a caller
    can always look up and never has to ask whether identity is available —
    and a deployment with no index behaves exactly as it did before firms.py
    existed, one spelling per company.
    """
    data_dir = Path(data_dir)
    store = data_dir / 'store'
    stamp = _store_stamp(store)
    if _cache['stamp'] == stamp and _cache['groups'] is not None:
        return _cache['groups']
    found = _from_index(data_dir / index_file, stamp)
    if found is None:
        clusters, _ = resolve_store(store)
        found = {spelling: c.name for c in clusters for spelling in c.spellings}
    _cache.update(stamp=stamp, groups=found)
    return found


def _store_stamp(store):
    try:
        return tuple(sorted((f.name, f.stat().st_mtime_ns)
                            for f in Path(store).glob('*.parquet')))
    except OSError:
        return None


def _from_index(path, stamp):
    """The spellings already worked out by the index build, but only if the
    index is not older than the store it describes — a stale map would quietly
    serve last week's companies."""
    import json
    try:
        if stamp and path.stat().st_mtime_ns < max(m for _, m in stamp):
            return None
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    out = {}
    for f in doc.get('firms', []):
        for spelling in f.get('spellings') or [f.get('company')]:
            if spelling:
                out[spelling] = f['company']
    return out or None

