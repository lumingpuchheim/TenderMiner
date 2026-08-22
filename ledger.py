"""TenderMining ledgers — where the append-only records live.

doc/STORAGE.md phase 2, step 3. Same shape as `subscriptions.py`, one layer
over: callers name a **home directory** and ask for a ledger by name, and this
module decides whether that home's records are in the SQLite database or still
in `<home>/ledger/<name>.jsonl`. A caller never opens either.

    rows = ledger.read(paths.deliveries_home, 'deliveries')
    ledger.append(paths.deliveries_home, 'deliveries', new_rows)

Why a module rather than a few functions in `db.py`: the database is one
storage option, and the fallback-to-file rule, the staleness guard and the
sandbox semantics are decisions about *records*, not about SQL. Keeping them
here means step 4 (predictions, grades) is a call-site change with no new
storage logic, and it means a sandbox — `preview_report.py`, `rewind_report.py` — gets the
same rules the real ledgers get instead of its own private ones.

## The staleness guard

A home can hold both a database and the original files, because
`db.py --migrate` deliberately does not delete anything. The database answers
in that case, so a file holding rows the database lacks would be silently
ignored — a customer served from a record that is missing the last cycle. That
is the failure this guard exists to make loud: `read` raises, and the message
names the fix (`python db.py --migrate`).

The comparison is by ROW COUNT, not by content. These are append-only logs, so
"the file has more lines than the table has rows" is exactly the question, and
it costs one `COUNT(*)` plus a line count rather than a full diff of 90,000
rows on every read.
"""

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


class LedgerError(RuntimeError):
    """Ledger storage that cannot be trusted to be complete."""


def _spec(name):
    import db
    if name not in db.LEDGERS:
        raise LedgerError(f'unknown ledger {name!r}; '
                          f'known: {", ".join(sorted(db.LEDGERS))}')
    return db.LEDGERS[name]


def file_path(home, name):
    """Where this ledger's text file is (or would be) under `home`."""
    return Path(home) / _spec(name)[0]


def storage(home, name):
    """-> ('db', db_path) | ('jsonl', file_path). Never None: an absent file is
    an empty ledger, not an error, because a fresh deployment has no records
    yet and the first append must be allowed to create it."""
    import db
    p = Path(home)
    if p.suffix in ('.jsonl', '.db', '.sqlite'):
        raise LedgerError(
            f'pass the ledger HOME DIRECTORY, not a storage file ({p.name}). '
            f'Storage belongs to ledger.py; call ledger.read(home, name).')
    if db.path_for(p).exists():
        return ('db', db.path_for(p))
    return ('jsonl', file_path(p, name))


def frozen_mentions(home, name, sub_id):
    """True if this ledger's pre-migration text file mentions `sub_id`.

    The stale-file guard (`_assert_not_stale`) compares row COUNTS, so
    deleting database rows that also exist in a frozen file would make every
    later read raise. Erasure (subscriptions.erase) therefore refuses any
    firm the frozen files know — this is the question it asks."""
    p = file_path(home, name)
    if not p.exists():
        return False
    return any(json.loads(line).get('sub_id') == sub_id
               for line in p.read_text(encoding='utf-8').splitlines()
               if line.strip())


def _read_file(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in
            Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def read(home, name):
    """Every row of this ledger, in append order."""
    import db
    kind, path = storage(home, name)
    if kind == 'jsonl':
        return _read_file(path)
    table = _spec(name)[1]
    con = db.connect(home, create=False)
    rows = [json.loads(db.unpack(r['raw'])) for r in
            con.execute(f'SELECT raw FROM {table} ORDER BY seq')]
    _assert_not_stale(con, home, name, table, len(rows))
    return rows


def _assert_not_stale(con, home, name, table, n_db):
    legacy = file_path(home, name)
    if not legacy.exists():
        return
    n_file = sum(1 for line in legacy.read_text(encoding='utf-8').splitlines()
                 if line.strip())
    if n_file > n_db:
        raise LedgerError(
            f'{legacy} has {n_file} rows but the database has {n_db}. The '
            f'database is what gets served, so those {n_file - n_db} row(s) '
            f'would be invisible. Run `python db.py --migrate` to take them '
            f'in (it is idempotent), or remove the stale file.')


def versions(home, *names):
    """One (name, marker) per ledger asked for — a cheap change stamp.

    The app's request cache keys on this (pitch.py): a write to one table
    moves exactly that table's marker and nobody else's, so marking a
    message as sent (app_events) no longer retires the relevance gate that
    took 2.7 s to build. In the database the marker is max(seq); for a
    pre-migration JSONL file it is the file's mtime and size. Read-only and
    a few ms — safe to call on every request."""
    import db
    out = []
    con = None
    for name in names:
        kind, path = storage(home, name)
        if kind == 'jsonl':
            try:
                st = Path(path).stat()
                out.append((name, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((name, None, None))
            continue
        if con is None:
            con = db.connect(home, create=False)
        table = _spec(name)[1]
        out.append((name, con.execute(
            f'SELECT max(seq) FROM {table}').fetchone()[0]))
    if con is not None:
        con.close()
    return tuple(out)


# One commit's worth of rows. The write lock is held from first insert to
# commit, and the app waits 5 s for it (HOSTING.md §1: 0.96 s per 5,000 rows,
# so ~25,000 rows is where a customer's click starts failing). Normal weeks
# append hundreds and commit once, exactly as before; a backfill-sized append
# becomes several commits, each briefly. The trade is explicit: a reader
# between chunks sees the first chunks without the last — the same partial
# state a crash mid-append always could leave, since every consumer dedups on
# the natural key rather than trusting an append to be atomic.
APPEND_CHUNK = 5_000


def append(home, name, rows):
    """Append rows. Returns the number actually written — a row that collides
    with the table's natural key is skipped, which is how re-running a cycle
    stays idempotent instead of duplicating a customer's record."""
    import db
    rows = list(rows)
    if not rows:
        return 0
    kind, path = storage(home, name)
    if kind == 'jsonl':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open('a', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + '\n')
        return len(rows)
    table = _spec(name)[1]
    con = db.connect(home)
    n = 0
    for i in range(0, len(rows), APPEND_CHUNK):
        n += sum(db.insert(con, table, r) for r in rows[i:i + APPEND_CHUNK])
        con.commit()
    return n


# ------------------------------------------- the prediction ledger, by query
#
# `read(home, 'predictions')` would hand back 94,000 rows, and the cycle asked
# four different questions of them — each answered by parsing 51 MB and building
# a dict in memory. These are those four questions, so the database can answer
# them with an index and the file path can keep doing what it always did.
#
# Each has a file branch that is the ORIGINAL code, deliberately: while the
# ledger can still live in a file, "the two paths agree" has to stay checkable,
# and the file branch is the reference the tests compare against.

def prediction_keys(home):
    """The (procedure_id, lot_id, notice_id, model) already scored — the dedup
    rule that makes re-running a cycle idempotent."""
    import db
    kind, path = storage(home, 'predictions')
    if kind == 'jsonl':
        return {(r['procedure_id'], r['lot_id'], r.get('notice_id'), r['model'])
                for r in _read_file(path)}
    con = db.connect(home, create=False)
    return {(r['procedure_id'], r['lot_id'], r['notice_id'], r['model'])
            for r in con.execute('SELECT procedure_id, lot_id, notice_id, model '
                                 'FROM prediction')}


def predictions_by_lot(home, lots=None):
    """{(procedure_id, lot_id): [rows in append order]}, for `lots` only when
    given. Grading needs the last prediction made before each award appeared,
    and only for lots whose award just published — a handful out of 94,000."""
    import db
    kind, path = storage(home, 'predictions')
    out = {}
    if kind == 'jsonl':
        for r in _read_file(path):
            key = (r['procedure_id'], r['lot_id'])
            if lots is None or key in lots:
                out.setdefault(key, []).append(r)
        return out
    con = db.connect(home, create=False)
    if lots is None:
        rows = con.execute('SELECT raw FROM prediction ORDER BY seq')
        for r in rows:
            d = json.loads(db.unpack(r['raw']))
            out.setdefault((d['procedure_id'], d['lot_id']), []).append(d)
        return out
    for pid, lid in lots:
        for r in con.execute('SELECT raw FROM prediction WHERE procedure_id = ? '
                             'AND lot_id = ? ORDER BY seq', (pid, lid)):
            out.setdefault((pid, lid), []).append(json.loads(db.unpack(r['raw'])))
    return out


def prediction_titles(home):
    """{(procedure_id, lot_id): row} for lots whose prediction carries a title or
    buyer — the receipt fallback for delivery rows written before those columns
    were stamped."""
    import db
    kind, path = storage(home, 'predictions')
    out = {}
    if kind == 'jsonl':
        for r in _read_file(path):
            if r.get('title') or r.get('buyer_name'):
                out[(r['procedure_id'], r['lot_id'])] = r
        return out
    con = db.connect(home, create=False)
    for r in con.execute(
            'SELECT raw FROM prediction WHERE title IS NOT NULL '
            'OR buyer_name IS NOT NULL ORDER BY seq'):
        d = json.loads(db.unpack(r['raw']))
        out[(d['procedure_id'], d['lot_id'])] = d
    return out


def prediction_latest_per_lot(home, exclude_models=()):
    """{(procedure_id, lot_id): last row appended} — the cycle report's view of
    the open market. exclude_models: model ids to leave out — the shadow arms
    of an A/B trial (doc/EXPERIMENTS.md §8), which score the same lots and
    must not be what the report or a customer sees."""
    import db
    kind, path = storage(home, 'predictions')
    out = {}
    skip = set(exclude_models)
    if kind == 'jsonl':
        for r in _read_file(path):
            if r['model'] not in skip:
                out[(r['procedure_id'], r['lot_id'])] = r   # last write wins
        return out
    con = db.connect(home, create=False)
    for r in con.execute('SELECT raw FROM prediction ORDER BY seq'):
        d = json.loads(db.unpack(r['raw']))
        if d['model'] not in skip:
            out[(d['procedure_id'], d['lot_id'])] = d
    return out


def predictions_by_model(home, model_id):
    """Every prediction row `model_id` wrote, in append order — what
    `deliver.py` reads instead of the scores a cycle once held in memory
    (RUNBOOK 1): the delivering champion's rows, later narrowed to the lots
    still open. A WHERE on the model column, not a scan of 98,000 rows."""
    import db
    kind, path = storage(home, 'predictions')
    if kind == 'jsonl':
        return [r for r in _read_file(path) if r['model'] == model_id]
    con = db.connect(home, create=False)
    return [json.loads(db.unpack(r['raw'])) for r in
            con.execute('SELECT raw FROM prediction WHERE model = ? ORDER BY seq',
                        (model_id,))]


def prediction_scores_since(home, cutoff_ts, exclude_models=()):
    """Scores appended on or after `cutoff_ts` (an ISO timestamp), for the
    score-distribution drift monitor. The monitor wants a trailing window, and
    a window is a WHERE clause rather than a filter over everything.
    exclude_models: shadow-arm model ids (see prediction_latest_per_lot)."""
    import db
    kind, path = storage(home, 'predictions')
    skip = set(exclude_models)
    if kind == 'jsonl':
        return [r['score'] for r in _read_file(path)
                if str(r.get('ts', '')) >= cutoff_ts and r['model'] not in skip]
    con = db.connect(home, create=False)
    if not skip:
        return [r['score'] for r in
                con.execute('SELECT score FROM prediction WHERE ts >= ?', (cutoff_ts,))]
    marks = ', '.join('?' * len(skip))
    return [r['score'] for r in
            con.execute(f'SELECT score FROM prediction WHERE ts >= ? '
                        f'AND model NOT IN ({marks})', (cutoff_ts, *sorted(skip)))]


def start(home):
    """Create empty storage for a sandbox home, matching whatever the real
    ledgers use. preview_report.py and rewind_report.py call this so their scratch world is
    the shipped storage rather than a second format only sandboxes know."""
    import db
    db.init(home)
    return Path(home)
